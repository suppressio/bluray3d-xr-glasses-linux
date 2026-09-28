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
"""
import copy
import ctypes.util
import itertools
import json
import logging
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from langs import lang as to_lang

log = logging.getLogger("bd3d_fs")


def ffprobe_json(*args: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", *args],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


class Source(ABC):
    name: str            # shown in the share as "<name> - 3D SBS.ts"
    duration: float      # seconds
    audio_desc: str      # for the log
    sidecars: list       # (suffix, path) of external subtitle files, e.g. (".ita.srt", "/x/movie.ita.srt")
    mtime_ns: int        # timestamp shown for the virtual file

    label: str = ""      # prefix of the file name of a variant, e.g. "ITA"
    category: str = "Blu-ray 3D"    # folder of the share it goes in
    name_suffix: str = " - 3D SBS"  # after the movie name in the file name
    two_d: bool = False  # True: video_command gives one stream with video and audio for ffmpeg
    frame_size: str = "3840x1080"   # output picture size (Full-SBS for 3D)
    frame_rate: str = "24000/1001"
    quality_key: str = "3d"         # which bitrates (pipeline.QUALITIES)
    input_format: str = "mpegts"    # two_d: format of video_command's output
    video_map: str = "0:i:0x1011"   # two_d: the video stream in it
    pre_filter: str = ""            # video filters before the subtitle overlay (deinterlace)
    post_filter: str = ""           # ... and after it (scale)
    subs: list = []                 # subtitle tracks that can be drawn in (.stream, .lang)
    sub = None                      # the track drawn into this file, if any
    forced_only: bool = False       # draw only its forced subtitles (foreign-language parts)
    sub_depth: int = 8              # 3D: pixels each eye's copy is moved inward (in front of the screen)

    def sub_ref(self, input_index: int) -> str:
        """The subtitle stream as an ffmpeg stream specifier on that input."""
        return f"{input_index}:i:{self.sub.stream:#x}"

    # Size of the picture the subtitles are drawn for. ffmpeg takes it from a video
    # stream of the same input; Blu-ray subtitles travel with the audio (3D) where
    # there is none, and ffmpeg then falls back to 720x576: they came out small,
    # half-way up on the left. Blu-ray PGS are always 1920x1080.
    sub_canvas: str = "1920x1080"

    def sub_decoder_args(self) -> str:
        """ffmpeg input options for the subtitle decoder."""
        args = f"-canvas_size {self.sub_canvas}" if self.sub_canvas else ""
        return args + (" -forced_subs_only 1" if self.forced_only else "")
    audio_langs: list    # languages of the audio tracks the pipeline outputs, in order
    # Pipelines that may run at once on this source. A disc is one optical drive:
    # two pipelines reading far-apart places make its head jump back and forth
    # on every read and both crawl.
    max_pipelines: int = 2

    def variants(self) -> list["Source"]:
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
    def audio_map(self) -> str:
        """ffmpeg -map option(s) selecting the audio track from input #1 ('' = no audio)."""


class MkvSource(Source):
    """A MakeMKV rip. MakeMKV keeps the MVC NAL units inside the video track."""

    def __init__(self, path: str, audio_langs: Optional[list[str]] = None):
        self.path = path
        self.name = Path(path).stem
        self.mtime_ns = os.stat(path).st_mtime_ns
        self.sidecars = find_sidecars(path)
        info = ffprobe_json(
            "-show_entries", "format=duration:stream=index,codec_type,codec_name,channels"
            ":stream_tags=language,title",
            path,
        )
        self.duration = float(info["format"].get("duration", 0))
        audio = [s for s in info["streams"] if s.get("codec_type") == "audio"]
        # one track per language: the requested ones in that order, or every
        # language of the file (audio_langs None); none found -> the first track
        lang_of = lambda t: to_lang(t.get("tags", {}).get("language", "und"))
        langs = audio_langs or list(dict.fromkeys(lang_of(t) for t in audio))
        chosen = []
        for lang in langs:
            track = next((t for t in audio if lang_of(t) == lang), None)
            if track is not None:
                chosen.append(track)
        if not chosen and audio:
            chosen = [audio[0]]
        self.audio_indexes = [s["index"] for s in chosen]
        self.audio_langs = [lang_of(s) for s in chosen]
        # PGS subtitle tracks, one per language, drawn from the same file (input #1)
        pgs = [s for s in info["streams"] if s.get("codec_name") == "hdmv_pgs_subtitle"]
        self.subs = _first_per_lang(SubTrack(s["index"], lang_of(s)) for s in pgs)
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
            capture_output=True,
        ).stdout
        nal_types = {data[m.end()] & 0x1F for m in re.finditer(b"\x00\x00\x01", data)
                     if m.end() < len(data)}
        return bool(nal_types & {15, 20})

    def keyframe_at_or_before(self, seconds: float) -> float:
        if seconds <= 0:
            return 0.0
        info = ffprobe_json(
            "-select_streams", "v:0",
            "-read_intervals", f"{seconds}%+#1",
            "-show_entries", "packet=pts_time,flags",
            self.path,
        )
        for pkt in info.get("packets", []):
            if "K" in pkt.get("flags", "") and pkt.get("pts_time") not in (None, "N/A"):
                return float(pkt["pts_time"])
        return seconds

    def video_command(self, start: float) -> str:
        # For DTS-seeking formats with B-frames, ffmpeg moves the seek point back
        # by 3/23 s. Asking for exactly keyframe K would land on the PREVIOUS
        # keyframe (~1 s earlier) and the picture would lag the audio by that
        # much. Asking for K+0.2 lands on K; stream copy keeps packets from the
        # keyframe on, so the first frame is still K.
        ss = f"-ss {start + 0.2:.3f} " if start > 0 else ""
        return (f"ffmpeg -nostdin -v error {ss}-i {shlex.quote(self.path)} -map 0:v:0 "
                f"-c copy -bsf:v h264_mp4toannexb -f h264 -")

    def audio_input(self, start: float) -> str:
        ss = f"-ss {start:.3f} " if start > 0 else ""
        return f"{ss}-i {shlex.quote(self.path)}"

    def sub_ref(self, input_index: int) -> str:
        return f"{input_index}:{self.sub.stream}"

    def audio_map(self) -> str:
        return " ".join(f"-map 1:{i}" for i in self.audio_indexes)

    def variants(self) -> list[Source]:
        if len(self.audio_indexes) < 2:
            return [self]
        out = []
        for index, lang in zip(self.audio_indexes, self.audio_langs):
            v = copy.copy(self)
            v.audio_indexes, v.audio_langs = [index], [lang]
            v.audio_desc = next(d for d in self.audio_desc.split(", ") if d.startswith(f"#{index} "))
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
    found = []
    for p in sorted(movie.parent.iterdir()):
        if (p.is_file() and p.suffix.lower() in SUBTITLE_EXTENSIONS
                and p.name.startswith(movie.stem + ".")):
            found.append((p.name[len(movie.stem):], str(p)))
    return found


HERE = Path(__file__).resolve().parent


def _first_per_lang(tracks) -> list:
    """The first track of each language, in the disc's order."""
    seen: dict = {}
    for t in tracks:
        seen.setdefault(t.lang, t)
    return list(seen.values())


@dataclass
class SubTrack:
    stream: int         # stream index (MKV) or PID (Blu-ray)
    lang: str
_fifo_ids = itertools.count()


def _decrypt_backends() -> list[dict]:
    """Environments to try for libbluray, in order: libaacs (+ KEYDB.cfg), then
    MakeMKV's libmmbd when installed. Keys are never shipped with this project."""
    envs = [{}]
    if ctypes.util.find_library("mmbd") or os.path.exists("/usr/lib/libmmbd.so.0"):
        envs.append({"LIBAACS_PATH": "libmmbd", "LIBBDPLUS_PATH": "libmmbd"})
    return envs


def _open_disc(path: str):
    """Open a disc/ISO/BDMV with the first decryption backend that works."""
    from bluray import Disc
    last = None
    for env in _decrypt_backends():
        saved = {k: os.environ.get(k) for k in ("LIBAACS_PATH", "LIBBDPLUS_PATH")}
        os.environ.update(env)
        try:
            disc = Disc(path)
            info = disc.info()
            if info.decrypted:
                return disc, info, env
            last = f"cannot decrypt (AACS error {info.aacs_error})"
            disc.close()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    raise OSError(f"{path}: {last or 'cannot open'}. Provide a KEYDB.cfg for libaacs "
                  f"or install MakeMKV (libmmbd).")


def _safe_name(name: str) -> str:
    """Disc title -> file name: drop the 'Blu-ray' suffix, no characters SMB forbids."""
    name = re.sub(r"\s*[-–]\s*Blu-ray.*$", "", name, flags=re.I).strip()
    name = re.sub(r'\s*[:/\\*?"<>|]\s*', " - ", name)
    return re.sub(r"\s+", " ", name).strip(" .-") or "Blu-ray"


AUDIO_PREFERENCE = ["dts-hd ma", "dts-hd", "dts", "ac3", "eac3", "lpcm", "truehd"]


def _pick_audio(audio: list, langs: Optional[list[str]]) -> list:
    """One track per language: the requested ones in that order, or every language
    on the disc (langs None, in disc order); none found -> the first track.
    Within a language, TrueHD comes last: its PID also carries an AC-3 core that
    ffmpeg shows as a second stream, and players rarely decode it anyway."""
    rank = {c: n for n, c in enumerate(AUDIO_PREFERENCE)}
    chosen = []
    for lang in langs or list(dict.fromkeys(a.lang for a in audio)):
        tracks = [a for a in audio if a.lang == lang]
        if tracks:
            chosen.append(min(tracks, key=lambda a: rank.get(a.codec, len(rank))))
    return chosen or audio[:1]


class BlurayDiscSource(Source):
    """A Blu-ray title read straight from a drive, an ISO or a BDMV folder.

    3D: the longest playlist whose clips all have an MVC dependent view, decoded
    by edge264 into Full-SBS. Otherwise 2D: the longest playlist, decoded by ffmpeg.
    """

    max_pipelines = 1

    def __init__(self, path: str, audio_langs: Optional[list[str]] = None):
        from bdmv import parse_clpi, parse_mpls
        self.path = path
        disc, info, self.env = _open_disc(path)
        try:
            titles = sorted(disc.titles(), key=lambda t: -t.duration)
            if not titles:
                raise OSError(f"{path}: no title found")
            chosen = None
            if info.has_3d:
                for title in titles:
                    items = parse_mpls(disc.read_file(f"BDMV/PLAYLIST/{title.playlist}.mpls"))
                    if items and all(it.dep_clip for it in items):
                        chosen = (title, items)
                        break
            self.two_d = chosen is None
            if self.two_d:
                title = titles[0]
                chosen = (title, parse_mpls(disc.read_file(f"BDMV/PLAYLIST/{title.playlist}.mpls")))
            title, items = chosen
            self.playlist, self.items = title.playlist, items
            self.clips = {}
            for it in items:
                base = parse_clpi(disc.read_file(f"BDMV/CLIPINF/{it.clip}.clpi"))
                dep = None if self.two_d else \
                    parse_clpi(disc.read_file(f"BDMV/CLIPINF/{it.dep_clip}.clpi"), pid=0x1012)
                self.clips[it.clip] = (base, dep)
        finally:
            disc.close()

        if self.two_d:
            self.category, self.name_suffix, self.frame_size = "Blu-ray", "", "1920x1080"
            self.quality_key = "2d"
        self.name = _safe_name(info.name or info.volume_id.replace("_", " ").title())
        self.duration = sum(it.duration for it in items)
        self.starts = list(itertools.accumulate([0.0] + [it.duration for it in items[:-1]]))
        st = os.stat(path)
        self.mtime_ns = time.time_ns() if stat.S_ISBLK(st.st_mode) else st.st_mtime_ns
        self.sidecars = [] if stat.S_ISBLK(st.st_mode) else find_sidecars(path)

        self.subs = _first_per_lang(items[0].subs or [])
        chosen_audio = _pick_audio(items[0].audio or [], audio_langs)
        self.audio_pids = [a.pid for a in chosen_audio]
        self.audio_langs = [a.lang for a in chosen_audio]
        self.audio_desc = ", ".join(f"{a.pid:#x} {a.lang} {a.codec}" for a in chosen_audio) or "none"
        self._fifo_dir = None if self.two_d else tempfile.mkdtemp(prefix="bd3d-")
        self._fifo = None

    def variants(self) -> list[Source]:
        if len(self.audio_pids) < 2:
            return [self]
        out = []
        for pid, lang in zip(self.audio_pids, self.audio_langs):
            v = copy.copy(self)
            v.audio_pids, v.audio_langs, v.label = [pid], [lang], lang.upper()
            v.audio_desc = next(d for d in self.audio_desc.split(", ") if d.startswith(f"{pid:#x} "))
            out.append(v)
        return out

    def keyframe_at_or_before(self, seconds: float) -> float:
        from bdmv import m2ts_seek, ssif_seek
        i = max(n for n, t in enumerate(self.starts) if t <= max(0.0, seconds))
        item = self.items[i]
        base, dep = self.clips[item.clip]
        rel = seconds - self.starts[i]
        sp = m2ts_seek(item, base, rel) if self.two_d else ssif_seek(item, base, dep, rel)
        # the first keyframe of a clip can sit a few ms before the play item's
        # in time: never report a start before the clip itself (or before 0)
        return self.starts[i] + max(0.0, sp.time)

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
        self._fifo = os.path.join(self._fifo_dir, f"audio{next(_fifo_ids)}.ts")
        os.mkfifo(self._fifo)
        return f"{cmd} --audio-out {shlex.quote(self._fifo)}".strip()

    def audio_input(self, start: float) -> str:
        return f"-f mpegts -analyzeduration 1000000 -probesize 5000000 -i {shlex.quote(self._fifo)}"

    def audio_map(self, input_index: int = 1) -> str:
        # the disc's PMT carries no language: take it from the playlist's STN table
        return " ".join(f"-map {input_index}:i:{p:#x} -metadata:s:a:{n} language={lang}"
                        for n, (p, lang) in enumerate(zip(self.audio_pids, self.audio_langs)))


class DvdSource(Source):
    """The main title of a DVD-Video: drive, ISO or folder with VIDEO_TS/ (dvd_reader.py)."""

    max_pipelines = 1
    two_d = True
    category, name_suffix, quality_key = "DVD", "", "dvd"
    input_format, video_map = "mpeg", "0:i:0x1e0"
    sub_canvas = ""                 # DVD: the picture in the same stream gives it

    def __init__(self, path: str, audio_langs: Optional[list[str]] = None):
        from dvd import Dvd, main_title
        self.path = path
        self._dvd = Dvd(path)
        try:
            self.title = main_title(self._dvd)
            label = self._dvd.volume_id()
        except OSError:
            self._dvd.close()
            raise
        self._vobs = self._dvd.title_vobs(self.title.vts)
        self._lock = threading.Lock()
        self._ifo_copy = None
        self.name = _safe_name(label.replace("_", " ").title() if label else Path(path).stem)
        self.duration = self.title.duration
        self.frame_size, self.frame_rate = self.title.frame_size, self.title.frame_rate
        w, h = self.frame_size.split("x")
        # DVDs are often interlaced, and their pixels are not square; subtitles
        # are drawn in between, on the 720-pixel-wide picture they are made for
        self.pre_filter = "bwdif=deint=interlaced"
        self.post_filter = f"scale={w}:{h},setsar=1"
        # one normal track per language (commentary and other kinds left out)
        self.subs = _first_per_lang(s for s in self.title.subs if s.kind == "normal")
        st = os.stat(path)
        self.mtime_ns = time.time_ns() if stat.S_ISBLK(st.st_mode) else st.st_mtime_ns
        self.sidecars = [] if stat.S_ISBLK(st.st_mode) else find_sidecars(path)
        # commentaries only if a language has nothing else
        main = [a for a in self.title.audio if not a.commentary]
        self.sub_desc = ", ".join(s.lang for s in self.subs) or "none"
        chosen = _pick_audio(main or self.title.audio, audio_langs)
        self.audio_pids = [a.stream for a in chosen]
        self.audio_langs = [a.lang for a in chosen]
        self.audio_desc = ", ".join(f"{a.stream:#x} {a.lang} {a.codec}" for a in chosen) or "none"

    def variants(self) -> list[Source]:
        return BlurayDiscSource.variants(self)

    def sub_decoder_args(self) -> str:
        # the subpicture colours come from the disc's IFO. ffmpeg refuses -palette as
        # an input option (the dvdsub encoder has an option of the same name), but
        # takes -ifo_palette: a copy of the title's IFO in a temporary folder
        if self._ifo_copy is None:
            self._ifo_copy = os.path.join(tempfile.mkdtemp(prefix="bd3d-dvd-"), "VTS.IFO")
            with open(self._ifo_copy, "wb") as f:
                f.write(self._ifo_for_palette())
        return f"-ifo_palette {shlex.quote(self._ifo_copy)} " + super().sub_decoder_args()

    def _ifo_for_palette(self) -> bytes:
        """The VTS IFO with the main title's palette in its first PGC, where
        ffmpeg reads it (the main title is not always the first PGC)."""
        from dvd import _u32
        v = bytearray(self._dvd.ifo(self.title.vts))
        pgci = _u32(v, 0xCC) * 2048
        first = pgci + _u32(v, pgci + 8 + 4)
        v[first + 0xA4:first + 0xA4 + 64] = self.title.palette_raw
        return bytes(v)

    def keyframe_at_or_before(self, seconds: float) -> float:
        from dvd import seek
        with self._lock:
            return seek(self._vobs, self.title, seconds).time

    def video_command(self, start: float) -> str:
        audio = " ".join(f"--audio {p:#x}" for p in self.audio_pids)
        # +2 ms: `start` is a VOBU time, make sure the same VOBU is found
        return (f"{shlex.quote(sys.executable)} {shlex.quote(str(HERE / 'dvd_reader.py'))} "
                f"{shlex.quote(self.path)} --title {self.title.number} "
                f"--start {start + 0.002:.3f} {audio}"
                + (f" --sub {self.sub.stream:#x}" if self.sub else ""))

    def audio_input(self, start: float) -> str:
        return ""

    def audio_map(self, input_index: int = 0) -> str:
        return BlurayDiscSource.audio_map(self, input_index)


def open_disc(path: str, audio_langs: Optional[list[str]] = None) -> Source:
    """A Blu-ray (3D or 2D) or a DVD, whichever the drive/ISO/folder holds."""
    try:
        return BlurayDiscSource(path, audio_langs)
    except OSError as bd_error:
        try:
            return DvdSource(path, audio_langs)
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


def discover(paths: list[str], audio_langs: Optional[list[str]] = None) -> list[Source]:
    """Turn command line arguments (files, folders) into sources."""
    candidates = []
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
                sources.append(open_disc(str(p), audio_langs))
            except OSError as e:
                log.info("skipped: %s", e)
        elif p.suffix.lower() != ".mkv":
            log.info("skipped (not a 3D Blu-ray MKV, ISO or BDMV folder): %s", p)
        elif not MkvSource.has_mvc(str(p)):
            log.info("skipped (no MVC 3D video): %s", p)
        else:
            sources.append(MkvSource(str(p), audio_langs))
    return sources
