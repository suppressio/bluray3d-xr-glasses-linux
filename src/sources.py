"""
sources.py — where the 3D video comes from.

A Source provides what the decode pipeline needs from a movie:
  - an Annex B H.264 stream with the MVC dependent view interleaved
    (base + dependent NAL units of each access unit, as edge264 expects);
  - the audio, as ffmpeg input options;
  - the duration, and the keyframe to start from for a given second (seeking).

Everything downstream (edge264 -> encoder -> virtual file -> SMB share) does
not care where the movie comes from.

Implemented:
  MkvSource         MakeMKV rip (.mkv) that kept the MVC stream.
  BlurayDiscSource  the disc itself, an ISO or a BDMV folder, read and decrypted
                    on the fly (disc_reader.py), with no rip at all.
  DvdSource         the main title of a DVD-Video (dvd_reader.py).
"""
import atexit
import copy
import functools
import itertools
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self, override

import bluray
import dvd
from bdmv import Clip, PlayItem, m2ts_seek, parse_clpi, parse_mpls, ssif_seek
from langs import lang as to_lang
from osplatform import has_library

log = logging.getLogger("bd3d_fs")


def ffprobe_json(*args: str) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", *args],
        capture_output=True, text=True, check=True,
    ).stdout
    result: dict[str, Any] = json.loads(out)
    return result


class HasLang(Protocol):
    lang: str


class SubtitleTrack(Protocol):
    stream: int         # stream index (MKV), PID (Blu-ray) or subpicture stream id (DVD)
    lang: str


class AudioTrack(Protocol):
    lang: str
    codec: str


class Source(ABC):
    path: str            # drive, ISO, folder or file the movie is read from
    name: str            # shown in the share as "<name> - 3D SBS.ts"
    duration: float      # seconds
    audio_desc: str      # for the log
    # (suffix, path) of external subtitle files, e.g. (".ita.srt", "/x/movie.ita.srt")
    sidecars: list[tuple[str, str]]
    mtime_ns: int        # timestamp shown for the virtual file
    audio_langs: list[str]    # languages of the audio tracks the pipeline outputs, in order
    subs: list[SubtitleTrack]  # subtitle tracks that can be drawn in

    label: str = ""      # prefix of the file name of a variant, e.g. "ITA"
    category: str = "Blu-ray 3D"    # folder of the share it goes in
    subfolder: str = ""             # inside it, e.g. "Extra" for a series' bonus titles
    name_suffix: str = " - 3D SBS"  # after the movie name in the file name
    two_d: bool = False  # True: video_command gives one stream with video and audio for ffmpeg
    frame_size: str = "3840x1080"   # output picture size (Full-SBS for 3D)
    frame_rate: str = "24000/1001"
    quality_key: str = "3d"         # which bitrates (pipeline.QUALITIES)
    input_format: str = "mpegts"    # two_d: format of video_command's output
    video_map: str = "0:i:0x1011"   # two_d: the video stream in it
    # two_d: how much of that stream ffmpeg reads to find its tracks before it starts
    probe_args: str = "-analyzeduration 2000000 -probesize 10000000"
    pre_filter: str = ""            # video filters before the subtitle overlay (deinterlace)
    post_filter: str = ""           # ... and after it (scale)
    sub: SubtitleTrack | None = None  # the track drawn into this file, if any
    forced_only: bool = False       # draw only its forced subtitles (foreign-language parts)
    sub_depth: int = 8              # 3D: pixels each eye's copy is moved inward
    # Pipelines that may run at once on this source. A disc is one optical drive:
    # two pipelines reading far-apart places make its head jump back and forth
    # on every read and both crawl.
    max_pipelines: int = 2

    # Size of the picture the subtitles are drawn for. ffmpeg takes it from a video
    # stream of the same input; Blu-ray subtitles travel with the audio (3D) where
    # there is none, and ffmpeg then falls back to 720x576: they came out small,
    # half-way up on the left. Blu-ray PGS are always 1920x1080.
    sub_canvas: str = "1920x1080"

    def drawn_sub(self) -> SubtitleTrack:
        """The subtitle track drawn into this file (only called when there is one)."""
        if self.sub is None:
            raise ValueError(f"{self.name}: no subtitle track to draw")
        return self.sub

    def sub_ref(self, input_index: int) -> str:
        """The subtitle stream as an ffmpeg stream specifier on that input."""
        return f"{input_index}:i:{self.drawn_sub().stream:#x}"

    def sub_decoder_args(self) -> str:
        """ffmpeg input options for the subtitle decoder."""
        args = f"-canvas_size {self.sub_canvas}" if self.sub_canvas else ""
        return args + (" -forced_subs_only 1" if self.forced_only else "")

    def variants(self) -> Sequence[Self]:
        """One source per audio language: players such as the VITURE 3D Player
        have no audio track menu and pick a track on their own, so each language
        becomes its own file. A single language keeps the plain name."""
        return [self]

    @abstractmethod
    def keyframe_at_or_before(self, seconds: float) -> float:
        """Start time of the keyframe the video will actually start from."""

    @abstractmethod
    def video_command(self, start: float) -> str:
        """Shell command writing Annex B H.264 + MVC to stdout, from keyframe `start`."""

    @abstractmethod
    def audio_input(self, start: float) -> str:
        """ffmpeg input options adding the audio as input #1, starting at `start`."""

    @abstractmethod
    def audio_map(self, input_index: int = 1) -> str:
        """ffmpeg -map option(s) selecting the audio tracks from that input ('' = no audio)."""


def _lang_of(stream: dict[str, Any]) -> str:
    tags: dict[str, str] = stream.get("tags", {})
    return to_lang(tags.get("language", "und"))


class MkvSource(Source):
    """A MakeMKV rip. MakeMKV keeps the MVC NAL units inside the video track."""

    def __init__(self, path: str, audio_langs: list[str] | None = None) -> None:
        self.path = path
        self.name = Path(path).stem
        self.mtime_ns = Path(path).stat().st_mtime_ns
        self.sidecars = find_sidecars(path)
        info = ffprobe_json(
            "-show_entries", "format=duration:stream=index,codec_type,codec_name,channels"
            ":stream_tags=language,title",
            path,
        )
        self.duration = float(info["format"].get("duration", 0))
        streams: list[dict[str, Any]] = info["streams"]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        # one track per language: the requested ones in that order, or every
        # language of the file (audio_langs None); none found -> the first track
        langs = audio_langs or list(dict.fromkeys(_lang_of(t) for t in audio))
        chosen: list[dict[str, Any]] = []
        for lang in langs:
            track = next((t for t in audio if _lang_of(t) == lang), None)
            if track is not None:
                chosen.append(track)
        if not chosen and audio:
            chosen = [audio[0]]
        self.audio_indexes: list[int] = [s["index"] for s in chosen]
        self.audio_langs = [_lang_of(s) for s in chosen]
        # PGS subtitle tracks, one per language, drawn from the same file (input #1)
        pgs = [s for s in streams if s.get("codec_name") == "hdmv_pgs_subtitle"]
        self.subs = list(_first_per_lang(SubTrack(s["index"], _lang_of(s)) for s in pgs))
        self.audio_desc = ", ".join(
            f"#{s['index']} {s.get('tags', {}).get('language', '?')} {s.get('codec_name')}"
            for s in chosen) or "none"

    @staticmethod
    def has_mvc(path: str) -> bool:
        """True if the first video track carries an MVC (3D) dependent view.

        Looks for subset SPS (NAL 15) or coded slice extension (NAL 20) in the
        first frames. A 2D MKV has neither; neither has a rip that dropped MVC.
        """
        data = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", path, "-map", "0:v:0", "-c", "copy",
             "-bsf:v", "h264_mp4toannexb", "-frames:v", "3", "-f", "h264", "-"],
            capture_output=True, check=False,
        ).stdout
        nal_types = {data[m.end()] & 0x1F for m in re.finditer(b"\x00\x00\x01", data)
                     if m.end() < len(data)}
        return bool(nal_types & {15, 20})

    @override
    def keyframe_at_or_before(self, seconds: float) -> float:
        if seconds <= 0:
            return 0.0
        info = ffprobe_json(
            "-select_streams", "v:0",
            "-read_intervals", f"{seconds}%+#1",
            "-show_entries", "packet=pts_time,flags",
            self.path,
        )
        packets: list[dict[str, str]] = info.get("packets", [])
        for pkt in packets:
            if "K" in pkt.get("flags", "") and pkt.get("pts_time") not in (None, "N/A"):
                return float(pkt["pts_time"])
        return seconds

    @override
    def video_command(self, start: float) -> str:
        # For DTS-seeking formats with B-frames, ffmpeg moves the seek point back
        # by 3/23 s. Asking for exactly keyframe K would land on the PREVIOUS
        # keyframe (~1 s earlier) and the picture would lag the audio by that
        # much. Asking for K+0.2 lands on K; stream copy keeps packets from the
        # keyframe on, so the first frame is still K.
        ss = f"-ss {start + 0.2:.3f} " if start > 0 else ""
        return (f"ffmpeg -nostdin -v error {ss}-i {shlex.quote(self.path)} -map 0:v:0 "
                f"-c copy -bsf:v h264_mp4toannexb -f h264 -")

    @override
    def audio_input(self, start: float) -> str:
        ss = f"-ss {start:.3f} " if start > 0 else ""
        return f"{ss}-i {shlex.quote(self.path)}"

    @override
    def sub_ref(self, input_index: int) -> str:
        return f"{input_index}:{self.drawn_sub().stream}"

    @override
    def audio_map(self, input_index: int = 1) -> str:
        return " ".join(f"-map {input_index}:{i}" for i in self.audio_indexes)

    @override
    def variants(self) -> Sequence[Self]:
        if len(self.audio_indexes) < 2:
            return [self]
        out: list[Self] = []
        for index, lang in zip(self.audio_indexes, self.audio_langs, strict=True):
            v = copy.copy(self)
            v.audio_indexes, v.audio_langs = [index], [lang]
            v.audio_desc = next(d for d in self.audio_desc.split(", ")
                                if d.startswith(f"#{index} "))
            v.label = lang.upper()
            out.append(v)
        return out


SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".sup", ".sub", ".idx", ".vtt")


def find_sidecars(movie_path: str) -> list[tuple[str, str]]:
    """External subtitle files next to the movie: "movie.srt", "movie.ita.srt", ...

    Returned as (suffix after the movie name, path), so they can be exposed next
    to the virtual file with the same suffix and players pick them up.
    """
    movie = Path(movie_path)
    return [(p.name[len(movie.stem):], str(p)) for p in sorted(movie.parent.iterdir())
            if p.is_file() and p.suffix.lower() in SUBTITLE_EXTENSIONS
            and p.name.startswith(movie.stem + ".")]


HERE = Path(__file__).resolve().parent


def _first_per_lang[T: HasLang](tracks: Iterable[T]) -> list[T]:
    """The first track of each language, in the disc's order."""
    seen: dict[str, T] = {}
    for t in tracks:
        seen.setdefault(t.lang, t)
    return list(seen.values())


@dataclass
class SubTrack:
    stream: int         # stream index (MKV)
    lang: str


_temp_ids = itertools.count()


@functools.cache
def _run_dir() -> str:
    """One folder in /tmp for the files of this run, removed at exit."""
    folder = tempfile.mkdtemp(prefix="bd3d-")
    atexit.register(shutil.rmtree, folder, True)
    return folder


def _temp_file(name: str) -> str:
    """A path for a file of this run (audio FIFOs, IFO copies)."""
    return os.path.join(_run_dir(), f"{next(_temp_ids)}-{name}")


def _decrypt_backends() -> list[dict[str, str]]:
    """Environments to try for libbluray, in order: libaacs (+ KEYDB.cfg), then
    MakeMKV's libmmbd when installed. Keys are never shipped with this project."""
    envs: list[dict[str, str]] = [{}]
    if has_library("mmbd"):
        envs.append({"LIBAACS_PATH": "libmmbd", "LIBBDPLUS_PATH": "libmmbd"})
    return envs


def _decrypted_with(info: bluray.DiscInfo, env: dict[str, str]) -> str:
    """For the log and problem reports: which protection, opened by which backend."""
    if not info.protection:
        return "not encrypted"
    if env.get("LIBAACS_PATH") == "libmmbd":
        backend = "MakeMKV (libmmbd)"
    elif "BD+" in info.protection:
        backend = "libaacs + libbdplus"
    else:
        backend = "libaacs"
    return f"{info.protection}, decrypted with {backend}"


class UnsupportedDiscError(OSError):
    """A disc this program will never be able to open, whatever the drive: a
    definitive rejection (e.g. BD+ with no way to decrypt it here), not a
    drive/media problem. The caller (watch_drive) does not retry these."""


AACS_ERROR_CORRUPTED_DISC = -1
"""libaacs: opening or reading of the AACS files on the disc failed."""
AACS_ERROR_MMC_OPEN = -6
"""libaacs: opening the drive (MMC) failed."""
AACS_ERROR_MMC_FAILURE = -7
"""libaacs: a command to the drive (MMC) failed."""
AACS_READ_ERRORS = frozenset({AACS_ERROR_CORRUPTED_DISC, AACS_ERROR_MMC_OPEN,
                              AACS_ERROR_MMC_FAILURE})
"""AACS errors that are drive or media problems (bad disc, drive dropout), not
a missing or wrong key: no KEYDB/MakeMKV hint for these."""


def _open_disc(path: str) -> tuple[bluray.Disc, bluray.DiscInfo, dict[str, str]]:
    """Open a disc/ISO/BDMV with the first decryption backend that works.
    BD+ only through libbdplus with a cached conversion table: libmmbd's BD+ fixes
    go wrong after a seek, libbdplus' own run of the disc's BD+ program fails on
    newer discs."""
    last: bluray.DiscInfo | None = None
    for env in _decrypt_backends():
        saved = {k: os.environ.get(k) for k in ("LIBAACS_PATH", "LIBBDPLUS_PATH")}
        os.environ.update(env)
        try:
            disc = bluray.Disc(path)
            info = disc.info()
            bdplus_by_mmbd = ("BD+" in info.protection
                              and os.environ.get("LIBBDPLUS_PATH") == "libmmbd")
            if info.decrypted and not bdplus_by_mmbd:
                if "BD+" in info.protection and not bluray.bdplus_table_found(path):
                    disc.close()
                    raise UnsupportedDiscError(
                        "BD+ disc: no conversion table for it in "
                        "~/.config/bdplus/convtab (libbdplus), see "
                        "Decryption in the README")
                log.info("%s: %s", path, _decrypted_with(info, env))
                return disc, info, env
            last = info
            disc.close()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    if last is not None and last.aacs_error in AACS_READ_ERRORS:
        raise OSError(f"cannot read the disc (AACS error {last.aacs_error}): "
                      "drive or media problem, not a missing key")
    if last is not None and "BD+" in last.protection:
        raise UnsupportedDiscError(
            "BD+ disc: not decrypted (libbdplus needs ~/.config/bdplus/vm0 "
            "and a conversion table, see Decryption in the README; MakeMKV's "
            "libmmbd is not used for BD+: it corrupts the picture after a seek)")
    raise OSError(f"cannot decrypt (AACS error {last.aacs_error if last else 'unknown'}). "
                  f"Provide a KEYDB.cfg for libaacs or install MakeMKV (libmmbd).")


def _safe_name(name: str) -> str:
    """Disc title -> file name: drop the 'Blu-ray' suffix, no characters SMB forbids."""
    name = re.sub(r"\s*[-\u2013]\s*Blu-ray.*$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r'(\s*[:/\\*?"<>|])+\s*', " - ", name)
    return re.sub(r"\s+", " ", name).strip(" .-") or "Blu-ray"


# season markers in the disc name/label, one word per language (#27); the bare
# "S1"/"S01" form last, so "Staffel 1" etc. are tried first
_SEASON_PATTERNS = [re.compile(rf"\b{word}\s*0*(\d{{1,2}})\b", re.IGNORECASE) for word in
                    ("staffel", "stagione", "saison", "season", "s")]
# a disc that names itself "Disc 2", "Vol. 1", "Teil 2"... is only part of a season:
# its first episode number cannot be known offline (#27)
_DISC_PART_PATTERN = re.compile(
    r"\b(disc|disk|disco|dischi|disque|vol|volume|teil|part|parte)\.?\s*0*(\d{1,2})\b",
    re.IGNORECASE)


def _parse_season(*names: str) -> tuple[int, str] | None:
    """Season number and show name from a disc's names, most preferred first (the
    disc's own name, then its volume label), e.g. "Rick_and_Morty_Staffel1" ->
    (1, "Rick and Morty"). The season may come from any of them; the show name is
    the preferred name, without the season when that is where it was found. None if
    no season is recognised, or if any name marks this disc as only part of a season
    (a split season cannot be numbered offline: the first episode's number would not
    be known)."""
    norms = [n.replace("_", " ") for n in names if n]
    if any(_DISC_PART_PATTERN.search(n) for n in norms):
        return None
    for i, norm in enumerate(norms):
        for pattern in _SEASON_PATTERNS:
            m = pattern.search(norm)
            if m:
                show = norm[:m.start()] + " " + norm[m.end():] if i == 0 else norms[0]
                return int(m.group(1)), re.sub(r"\s+", " ", show).strip(" -")
    return None


def _episode_name(name: str, raw_names: list[str], episode: int, extra: bool
                  ) -> tuple[str, str]:
    """An episode's file name and subfolder: extras (#39) are numbered apart and go
    in Extra/; an episode gets S<season>E<episode> first when the season is known
    and reliable (#27), today's "<name> - <episode>" otherwise."""
    if extra:
        return f"{name} - Extra {episode}", "Extra"
    season_show = _parse_season(*raw_names)
    if season_show is not None:
        season, show = season_show
        return f"S{season:02d}E{episode:02d} - {_safe_name(show)}", ""
    return f"{name} - {episode}", ""


AUDIO_PREFERENCE = ["dts-hd ma", "dts-hd", "dts", "ac3", "eac3", "lpcm", "truehd"]


def _pick_audio[T: AudioTrack](audio: list[T], langs: list[str] | None,
                                log_as: str | None = None) -> list[T]:
    """One track per language: the requested ones in that order, or every language
    on the disc (langs None, in disc order); none found -> the first track. The
    fallback is logged only when `log_as` (the source name) is given: callers
    that pick tracks for a real source pass it, so there it is not silent.
    Within a language, TrueHD comes last: its PID also carries an AC-3 core that
    ffmpeg shows as a second stream, and players rarely decode it anyway."""
    rank = {c: n for n, c in enumerate(AUDIO_PREFERENCE)}
    chosen: list[T] = []
    for lang in langs or list(dict.fromkeys(a.lang for a in audio)):
        tracks = [a for a in audio if a.lang == lang]
        if tracks:
            chosen.append(min(tracks, key=lambda a: rank.get(a.codec, len(rank))))
    if chosen:
        return chosen
    if log_as and langs and audio:
        log.warning("%s: no %s audio track, using %s", log_as, "/".join(langs), audio[0].lang)
    return audio[:1]


# extras / episodes count for _split_extras: a second copy of every episode, plus a few bonuses
EXTRAS_RATIO = (0.8, 1.5)


def _split_extras[T](titles: list[T], langs_of: Callable[[T], frozenset[str]]
                     ) -> tuple[list[T], list[T]]:
    """Split a series' titles into episodes and extras by their audio languages.

    Some discs carry a second copy of every episode with fewer dubs (Rick and Morty S1,
    German edition: 11 episodes in ita+eng, then 11 eng-only copies and a bonus). The
    group whose languages include every other group's, and has more of them, is the
    episodes; the rest are extras. Only when the extras look like such a second set:
    about as many titles as the episodes (EXTRAS_RATIO), and at least 2 on each side.
    A series whose last episodes were never dubbed (7 dubbed, 3 not), or where a few
    episodes carry one more dub, keeps all its episodes. Otherwise: all episodes.
    """
    groups: dict[frozenset[str], list[T]] = {}
    for t in titles:
        groups.setdefault(langs_of(t), []).append(t)
    if len(groups) < 2:
        return titles, []
    richest = max(groups, key=len)
    if not all(g < richest for g in groups if g != richest):
        return titles, []
    episodes = groups[richest]
    extras = [t for t in titles if langs_of(t) != richest]
    low, high = EXTRAS_RATIO
    if len(episodes) < 2 or len(extras) < 2 or \
            not low * len(episodes) <= len(extras) <= high * len(episodes):
        return titles, []
    return episodes, extras


def _play_alls(titles: list[bluray.Title], items_by_playlist: dict[str, list[PlayItem]],
               sigs: dict[str, str]) -> set[str]:
    """Playlists that chain the opening clips of at least two other titles: "play all"
    playlists. They start with the same clip as an episode, so they must be set aside
    before deduplicating by opening clip: with a lower playlist number one would
    otherwise be kept in place of that episode, and its double length would make the
    disc fail the similar-duration test.
    """
    found: set[str] = set()
    for t in titles:
        others = {sigs[o.playlist] for o in titles if o.playlist != t.playlist}
        clips = {it.clip for it in items_by_playlist.get(t.playlist, [])}
        if len(clips & others) >= 2:
            found.add(t.playlist)
    return found


def _play_all_order(titles: list[bluray.Title], items_by_playlist: dict[str, list[PlayItem]],
                    episodes: list[bluray.Title], sigs: dict[str, str]
                    ) -> list[bluray.Title] | None:
    """The play-all playlist among `titles`, if any: one whose PlayItems visit the
    opening clip of every title in `episodes` (`sigs`: playlist -> opening clip)
    exactly once, in sequence (docs/RESEARCH.md, Q4). That sequence is
    the authoritative episode order, more reliable than sorting by playlist number
    (which may reflect authoring order, not air order). None when no such title is
    found: the caller keeps the current playlist-number order.
    """
    by_sig = {sigs[t.playlist]: t for t in episodes}
    wanted = set(by_sig)
    for t in titles:
        clips = [it.clip for it in items_by_playlist.get(t.playlist, [])]
        matches = [c for c in clips if c in wanted]
        if len(matches) == len(wanted) and set(matches) == wanted:
            return [by_sig[c] for c in matches]
    return None


def _movie_title(path: str, disc: bluray.Disc, info: bluray.DiscInfo
                 ) -> tuple[bluray.Title, list[PlayItem], bool]:
    """The movie on a Blu-ray: (title, its play items, 2D?). The longest title whose
    clips all have an MVC dependent view when the disc is 3D, else the longest one."""
    titles = sorted(disc.titles(), key=lambda t: -t.duration)
    if not titles:
        raise OSError(f"{path}: no title found")
    longest: tuple[bluray.Title, list[PlayItem]] | None = None
    for title in titles:
        try:
            items = parse_mpls(disc.read_file(f"BDMV/PLAYLIST/{title.playlist}.mpls"))
        except Exception as e:  # noqa: BLE001 — #65: malformed MPLS, as in all_on()
            log.warning("%s: playlist %s skipped: %s", path, title.playlist, e)
            continue
        if info.has_3d and items and all(it.dep_clip for it in items):
            return title, items, False
        longest = longest or (title, items)
        if not info.has_3d:
            break
    if longest is None:
        raise OSError(f"{path}: no readable title")
    return *longest, True


class DiscSource(Source):
    """A disc (Blu-ray or DVD): its audio tracks are PIDs / stream ids of one stream."""

    max_pipelines = 1
    audio_pids: list[int]

    @override
    def variants(self) -> Sequence[Self]:
        if len(self.audio_pids) < 2:
            return [self]
        out: list[Self] = []
        for pid, lang in zip(self.audio_pids, self.audio_langs, strict=True):
            v = copy.copy(self)
            v.audio_pids, v.audio_langs, v.label = [pid], [lang], lang.upper()
            v.audio_desc = next(d for d in self.audio_desc.split(", ")
                                if d.startswith(f"{pid:#x} "))
            out.append(v)
        return out

    @override
    def audio_map(self, input_index: int = 1) -> str:
        # the disc's PMT carries no language: take it from the playlist's STN table
        return " ".join(f"-map {input_index}:i:{p:#x} -metadata:s:a:{n} language={lang}"
                        for n, (p, lang) in enumerate(zip(self.audio_pids, self.audio_langs,
                                                          strict=True)))


class BlurayDiscSource(DiscSource):
    """A Blu-ray title read straight from a drive, an ISO or a BDMV folder.

    3D: the longest playlist whose clips all have an MVC dependent view, decoded
    by edge264 into Full-SBS. Otherwise 2D: the longest playlist, decoded by ffmpeg.

    For multi-episode discs (all titles have similar duration), use all_on() to get
    one source per episode; it mirrors DvdSource.all_on().
    """

    @classmethod
    def all_on(cls, path: str, audio_langs: list[str] | None = None) -> list[Source]:
        """One source per episode when the disc holds a TV series; one source otherwise.

        A single bd_open (+ AACS handshake) for the whole disc, kept open while every
        episode source is built from it (see the `_disc` parameter of __init__), instead
        of each one reopening the disc on its own.
        """
        disc, info, env = _open_disc(path)
        try:
            titles = sorted(disc.titles(), key=lambda t: t.playlist)
            # Read the first clip of each playlist to deduplicate alternate-audio/subtitle
            # variants: same opening clip = same episode (variants may append short clips
            # for language cards or warnings; keep the lowest playlist number per episode).
            # The full PlayItem list is kept too, to find a play-all playlist afterwards.
            langs: dict[str, frozenset[str]] = {}
            items_by_playlist: dict[str, list[PlayItem]] = {}
            malformed: set[str] = set()

            def first_clip(t: bluray.Title) -> str:
                try:
                    items = parse_mpls(disc.read_file(f"BDMV/PLAYLIST/{t.playlist}.mpls"))
                    items_by_playlist[t.playlist] = items
                    # every play item: an intro clip may carry other audio than the episode
                    langs[t.playlist] = frozenset(a.lang for it in items for a in it.audio)
                    return items[0].clip if items else t.playlist
                except Exception as e:  # noqa: BLE001 — malformed MPLS: skip the playlist
                    log.warning("%s: playlist %s skipped: %s", path, t.playlist, e)
                    malformed.add(t.playlist)
                    return t.playlist
            sigs = {t.playlist: first_clip(t) for t in titles}
            # #63: a broken playlist (often an extra) must not take the whole disc down,
            # nor weigh in the play-all, dedup and episode checks below
            titles = [t for t in titles if t.playlist not in malformed]

            if len(titles) < 2:
                return [cls(path, audio_langs, _disc=disc, _info=info, _env=env)]

            # "Play all" playlists are set aside first: they only give the episode order.
            play_all_ids = _play_alls(titles, items_by_playlist, sigs)
            play_alls = [t for t in titles if t.playlist in play_all_ids]
            candidates = [t for t in titles if t.playlist not in play_all_ids] or titles

            # Deduplicate: keep only the first title (lowest playlist number) per opening
            # clip. This removes alternate-audio/subtitle variants.
            seen_sigs: set[str] = set()
            unique: list[bluray.Title] = []
            for t in candidates:
                sig = sigs[t.playlist]
                if sig not in seen_sigs:
                    seen_sigs.add(sig)
                    unique.append(t)

            if len(unique) >= 2:
                by_dur = sorted(unique, key=lambda t: -t.duration)
                longest, shortest = by_dur[0].duration, by_dur[-1].duration
                # All titles long enough and similar enough in duration → episodes
                if shortest > 300 and longest / shortest < 1.5:
                    episodes, extras = _split_extras(
                        unique, lambda t: langs.get(t.playlist, frozenset()))
                    # #67: on a 3D disc, a 3D play-all of every part is a movie split in
                    # parts (Tron: Legacy 3D), not a series: that play-all is the movie.
                    # As long as the parts together: a montage of excerpts is not it.
                    parts = sum(t.duration for t in episodes)
                    movie = next((p for p in play_alls if info.has_3d
                                  and p.duration >= 0.95 * parts
                                  and _play_all_order([p], items_by_playlist, episodes, sigs)
                                  and all(it.dep_clip for it in items_by_playlist[p.playlist])),
                                 None)
                    if movie is not None:
                        return [cls(path, audio_langs, _title=movie,
                                    _disc=disc, _info=info, _env=env)]
                    play_all_order = _play_all_order(play_alls, items_by_playlist, episodes, sigs)
                    if play_all_order is not None:
                        episodes = play_all_order
                    found: list[Source] = [cls(path, audio_langs, _title=t, _episode=i,
                                               _disc=disc, _info=info, _env=env)
                                           for i, t in enumerate(episodes, 1)]
                    found += [cls(path, audio_langs, _title=t, _episode=i, _extra=True,
                                  _disc=disc, _info=info, _env=env)
                              for i, t in enumerate(extras, 1)]
                    return found
            return [cls(path, audio_langs, _disc=disc, _info=info, _env=env)]
        finally:
            disc.close()

    def __init__(self, path: str, audio_langs: list[str] | None = None,
                 _title: bluray.Title | None = None, _episode: int = 0, _extra: bool = False,
                 _disc: bluray.Disc | None = None, _info: bluray.DiscInfo | None = None,
                 _env: dict[str, str] | None = None) -> None:
        self.path = path
        # _disc: an already-open disc handed down by all_on(), shared by every episode
        # source instead of each one opening (and AACS-handshaking) its own; all_on()
        # closes it once every episode has been built, so it is left open here.
        if _disc is not None and _info is not None and _env is not None:
            disc, info, self.env = _disc, _info, _env
            opened_here = False
        else:
            disc, info, self.env = _open_disc(path)
            opened_here = True
        try:
            if _title is not None:
                # specific title supplied by all_on() for a multi-episode disc
                items = parse_mpls(disc.read_file(f"BDMV/PLAYLIST/{_title.playlist}.mpls"))
                self.two_d = not info.has_3d or not (items and all(it.dep_clip for it in items))
                title = _title
            else:
                title, items, self.two_d = _movie_title(path, disc, info)
            self.playlist: str = title.playlist
            self.items: list[PlayItem] = items
            self.clips: dict[str, tuple[Clip, Clip | None]] = {}
            for it in items:
                base = parse_clpi(disc.read_file(f"BDMV/CLIPINF/{it.clip}.clpi"))
                dep = None if self.two_d else \
                    parse_clpi(disc.read_file(f"BDMV/CLIPINF/{it.dep_clip}.clpi"), pid=0x1012)
                self.clips[it.clip] = (base, dep)
        finally:
            if opened_here:  # also when only part of disc/info/env was passed
                disc.close()

        if self.two_d:
            self.category, self.name_suffix, self.frame_size = "Blu-ray", "", "1920x1080"
            self.quality_key = "2d"
        label = info.volume_id.replace("_", " ").title()
        self.name = _safe_name(info.name or label)
        if _episode:  # extras (#39) are numbered apart and go in Extra/
            self.name, self.subfolder = _episode_name(self.name, [info.name, label],
                                                      _episode, _extra)
        self.duration = sum(it.duration for it in items)
        self.starts = list(itertools.accumulate([0.0] + [it.duration for it in items[:-1]]))
        st = Path(path).stat()
        self.mtime_ns = time.time_ns() if stat.S_ISBLK(st.st_mode) else st.st_mtime_ns
        self.sidecars = [] if stat.S_ISBLK(st.st_mode) else find_sidecars(path)

        self.subs = list(_first_per_lang(items[0].subs))
        chosen_audio = _pick_audio(items[0].audio, audio_langs, log_as=self.name)
        self.audio_pids = [a.pid for a in chosen_audio]
        self.audio_langs = [a.lang for a in chosen_audio]
        self.audio_desc = ", ".join(f"{a.pid:#x} {a.lang} {a.codec}"
                                    for a in chosen_audio) or "none"
        self._fifo: str | None = None

    @override
    def keyframe_at_or_before(self, seconds: float) -> float:
        i = max(n for n, t in enumerate(self.starts) if t <= max(0.0, seconds))
        item = self.items[i]
        base, dep = self.clips[item.clip]
        rel = seconds - self.starts[i]
        sp = m2ts_seek(item, base, rel) if dep is None else ssif_seek(item, base, dep, rel)
        # the first keyframe of a clip can sit a few ms before the play item's
        # in time: never report a start before the clip itself (or before 0)
        return self.starts[i] + max(0.0, sp.time)

    @override
    def video_command(self, start: float) -> str:
        env = " ".join(f"{k}={v}" for k, v in self.env.items())
        # the subtitle stream travels like an audio one (same filtering and start)
        pids = " ".join(f"--audio-pid {p:#x}" for p in
                        self.audio_pids + ([self.sub.stream] if self.sub is not None else []))
        # +2 ms: `start` is an EP_map time (truncated), make sure the same entry is found
        cmd = (f"{env} {shlex.quote(sys.executable)} {shlex.quote(str(HERE / 'disc_reader.py'))} "
               f"{shlex.quote(self.path)} --playlist {self.playlist} --start {start + 0.002:.3f} "
               f"{pids}")
        if self.two_d:
            return f"{cmd} --mode 2d".strip()
        # 3D: one FIFO per pipeline start, disc_reader writes the audio TS into it
        if self._fifo and os.path.exists(self._fifo):
            os.unlink(self._fifo)
        self._fifo = _temp_file("audio.ts")
        os.mkfifo(self._fifo)
        return f"{cmd} --audio-out {shlex.quote(self._fifo)}".strip()

    @override
    def audio_input(self, start: float) -> str:
        if self._fifo is None:
            raise ValueError("audio_input() before video_command()")
        return f"-f mpegts -analyzeduration 1000000 -probesize 5000000 -i {shlex.quote(self._fifo)}"


class DvdSource(DiscSource):
    """A title of a DVD-Video: the movie, or one episode of a series (drive, ISO
    or folder with VIDEO_TS/, read by dvd_reader.py). all_on() finds them."""

    two_d = True
    category, name_suffix, quality_key = "DVD", "", "dvd"
    input_format, video_map = "mpeg", "0:i:0x1e0"
    # a program stream announces no tracks: ffmpeg knows one when it meets a
    # packet of it. Titles often open with seconds of logos and warnings without
    # audio, so it looks further (the disc is read far faster than it plays)
    probe_args = "-analyzeduration 8000000 -probesize 20000000"
    sub_canvas = ""                 # DVD: the picture in the same stream gives it

    @classmethod
    def all_on(cls, path: str, audio_langs: list[str] | None = None) -> list["DvdSource"]:
        """The movie, or one source per episode ("<disc> - 1", "- 2"...)."""
        with dvd.Dvd(path) as disc:
            chosen = dvd.main_titles(disc)
        if len(chosen) == 1:
            return [cls(path, audio_langs, chosen[0])]
        return [cls(path, audio_langs, t, episode) for episode, t in enumerate(chosen, 1)]

    def __init__(self, path: str, audio_langs: list[str] | None = None,
                 title: dvd.DvdTitle | None = None, episode: int = 0) -> None:
        self.path = path
        self._dvd = dvd.Dvd(path)
        try:
            self.title = title or dvd.main_titles(self._dvd)[0]
            label = self._dvd.volume_id()
        except OSError:
            self._dvd.close()
            raise
        self._vobs = self._dvd.title_vobs(self.title.vts)
        # once per disc (the first episode): each check is a seek of the drive
        if episode <= 1:
            if dvd.scrambled(self._vobs.read(self.title.cells[0].first, 16)):
                self._dvd.close()
                raise OSError(f"{path}: the DVD is encrypted (CSS) and libdvdcss is missing or "
                              f"failed: install it (Debian/Ubuntu: libdvd-pkg, see the README)")
            log.info("%s: %s", path, "DVD, read through libdvdcss (for CSS)"
                     if dvd.has_dvdcss() else "DVD not encrypted")
        self._lock = threading.Lock()
        self._ifo_copy: str | None = None
        self.name = _safe_name(label.replace("_", " ").title() if label else Path(path).stem)
        if episode:
            self.name = f"{self.name} - {episode}"
        self.duration = self.title.duration
        self.frame_size, self.frame_rate = self.title.frame_size, self.title.frame_rate
        w, h = self.frame_size.split("x")
        # DVDs are often interlaced, and their pixels are not square; subtitles
        # are drawn in between, on the 720-pixel-wide picture they are made for
        self.pre_filter = "bwdif=deint=interlaced"
        self.post_filter = f"scale={w}:{h},setsar=1"
        # one normal track per language (commentary and other kinds left out)
        self.subs = list(_first_per_lang(s for s in self.title.subs if s.kind == "normal"))
        st = Path(path).stat()
        self.mtime_ns = time.time_ns() if stat.S_ISBLK(st.st_mode) else st.st_mtime_ns
        self.sidecars = [] if stat.S_ISBLK(st.st_mode) else find_sidecars(path)
        # commentaries only if a language has nothing else
        main = [a for a in self.title.audio if not a.commentary]
        self.sub_desc = ", ".join(s.lang for s in self.subs) or "none"
        chosen = _pick_audio(main or self.title.audio, audio_langs, log_as=self.name)
        self.audio_pids = [a.stream for a in chosen]
        self.audio_langs = [a.lang for a in chosen]
        self.audio_desc = ", ".join(f"{a.stream:#x} {a.lang} {a.codec}"
                                    for a in chosen) or "none"

    @override
    def sub_decoder_args(self) -> str:
        # the subpicture colours come from the disc's IFO. ffmpeg refuses -palette as
        # an input option (the dvdsub encoder has an option of the same name), but
        # takes -ifo_palette: a copy of the title's IFO in a temporary folder
        if self._ifo_copy is None:
            self._ifo_copy = _temp_file("VTS.IFO")
            Path(self._ifo_copy).write_bytes(self._ifo_for_palette())
        return f"-ifo_palette {shlex.quote(self._ifo_copy)} " + super().sub_decoder_args()

    def _ifo_for_palette(self) -> bytes:
        """The VTS IFO with the main title's palette in its first PGC, where
        ffmpeg reads it (the main title is not always the first PGC)."""
        v = bytearray(self._dvd.ifo(self.title.vts))
        pgci = int.from_bytes(v[0xCC:0xD0], "big") * dvd.SECTOR
        first = pgci + int.from_bytes(v[pgci + 12:pgci + 16], "big")
        v[first + 0xA4:first + 0xA4 + 64] = self.title.palette_raw
        return bytes(v)

    @override
    def keyframe_at_or_before(self, seconds: float) -> float:
        with self._lock:
            return dvd.seek(self._vobs, self.title, seconds).time

    @override
    def video_command(self, start: float) -> str:
        audio = " ".join(f"--audio {p:#x}" for p in self.audio_pids)
        # +2 ms: `start` is a VOBU time, make sure the same VOBU is found
        return (f"{shlex.quote(sys.executable)} {shlex.quote(str(HERE / 'dvd_reader.py'))} "
                f"{shlex.quote(self.path)} --title {self.title.number} "
                f"--start {start + 0.002:.3f} {audio}"
                + (f" --sub {self.sub.stream:#x}" if self.sub else ""))

    @override
    def audio_input(self, start: float) -> str:
        return ""

    @override
    def audio_map(self, input_index: int = 0) -> str:
        return super().audio_map(input_index)


def open_disc(path: str, audio_langs: list[str] | None = None) -> list[Source]:
    """What the drive/ISO/folder holds: a Blu-ray (3D or 2D), or a DVD (its
    movie, or its episodes)."""
    try:
        return BlurayDiscSource.all_on(path, audio_langs)
    except OSError as bd_error:
        try:
            return list(DvdSource.all_on(path, audio_langs))
        except OSError:
            raise bd_error from None


def is_dvd(path: Path) -> bool:
    return path.is_dir() and (path / "VIDEO_TS" / "VIDEO_TS.IFO").exists()


def is_bluray(path: Path) -> bool:
    """A drive, an ISO or a folder with BDMV/ inside."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return (stat.S_ISBLK(mode) or path.suffix.lower() == ".iso"
            or (path.is_dir() and (path / "BDMV" / "index.bdmv").exists()))


def discover(paths: list[str], audio_langs: list[str] | None = None) -> list[Source]:
    """Turn command line arguments (files, folders) into sources."""
    candidates: list[Path] = []
    for s in paths:
        p = Path(s).expanduser()
        if is_bluray(p) or is_dvd(p):
            candidates.append(p)
        elif p.is_dir():
            for x in sorted(p.rglob("*")):
                if x.suffix.lower() in (".mkv", ".iso"):
                    candidates.append(x)
                elif x.name in ("BDMV", "VIDEO_TS") and x.is_dir():
                    candidates.append(x.parent)
        elif p.is_file():
            candidates.append(p)
        else:
            raise SystemExit(f"not found: {s}")

    sources: list[Source] = []
    for p in candidates:
        if is_bluray(p) or is_dvd(p):
            try:
                sources += open_disc(str(p), audio_langs)
            except OSError as e:
                # some errors already start with the path: keep it once
                log.info("%s: skipped: %s", p, str(e).removeprefix(f"{p}: "))
        elif p.suffix.lower() != ".mkv":
            log.info("skipped (not a 3D Blu-ray MKV, ISO or BDMV folder): %s", p)
        elif not MkvSource.has_mvc(str(p)):
            log.info("skipped (no MVC 3D video): %s", p)
        else:
            sources.append(MkvSource(str(p), audio_langs))
    return sources
