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
  MkvSource   MakeMKV rip (.mkv) that kept the MVC stream.

Planned (see ROADMAP.md):
  BlurayDisc  the disc itself / an ISO / a BDMV folder, read and decrypted on
              the fly, with no rip at all.
"""
import json
import logging
import os
import re
import shlex
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

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
        # one track per requested language, in the requested order (the player
        # lists them in this order); none found -> the first track
        chosen = []
        for lang in audio_langs or []:
            track = next((s for s in audio if s.get("tags", {}).get("language") == lang), None)
            if track is not None:
                chosen.append(track)
        if not chosen and audio:
            chosen = [audio[0]]
        self.audio_indexes = [s["index"] for s in chosen]
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

    def audio_map(self) -> str:
        return " ".join(f"-map 1:{i}" for i in self.audio_indexes)


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


def discover(paths: list[str], audio_langs: Optional[list[str]] = None) -> list[Source]:
    """Turn command line arguments (files, folders) into sources."""
    candidates = []
    for s in paths:
        p = Path(s).expanduser()
        if p.is_dir():
            candidates += sorted(x for x in p.rglob("*") if x.suffix.lower() == ".mkv")
        elif p.is_file():
            candidates.append(p)
        else:
            raise SystemExit(f"not found: {s}")

    sources: list[Source] = []
    for p in candidates:
        if p.suffix.lower() != ".mkv":
            log.info("skipped (only .mkv is supported for now): %s", p)
        elif not MkvSource.has_mvc(str(p)):
            log.info("skipped (no MVC 3D video): %s", p)
        else:
            sources.append(MkvSource(str(p), audio_langs))
    return sources
