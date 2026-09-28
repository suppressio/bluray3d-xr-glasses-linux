"""
pipeline.py — shared pieces: MKV probing, keyframe lookup, encoder choice, decode command.

The decode chain:

    ffmpeg -ss K -i movie.mkv -c copy -bsf h264_mp4toannexb -f h264 -   # demux only, no decoding
      | edge264_test - -Ok                                                # MVC -> Y4M SBS 3840x1080
      | ffmpeg -i - -ss K -i movie.mkv ... <output>                       # encode + audio
"""
import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Optional

# Video encoders, best first. Bitrate is constant (CBR) because the virtual
# file maps bytes to seconds linearly.
ENCODERS = {
    "nvenc": "-c:v h264_nvenc -preset p4 -rc cbr -b:v 20M -maxrate 20M -bufsize 10M",
    "x264": "-c:v libx264 -preset veryfast -b:v 20M -maxrate 20M -bufsize 10M "
            "-x264-params nal-hrd=cbr",
}


def ffprobe_json(*args: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", *args],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


@dataclass
class FilmInfo:
    path: str
    duration: float
    audio_index: Optional[int]
    audio_desc: str


def probe_file(path: str, audio_lang: Optional[str] = None) -> FilmInfo:
    """Duration and audio track to use (the one in audio_lang if present, else the first)."""
    info = ffprobe_json(
        "-show_entries", "format=duration:stream=index,codec_type,codec_name,channels"
        ":stream_tags=language,title",
        path,
    )
    duration = float(info["format"].get("duration", 0))
    audio = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    chosen = next((s for s in audio if s.get("tags", {}).get("language") == audio_lang),
                  audio[0] if audio else None)
    if chosen is None:
        return FilmInfo(path, duration, None, "none")
    tags = chosen.get("tags", {})
    desc = (f"#{chosen['index']} {tags.get('language', '?')} {chosen.get('codec_name')} "
            f"{tags.get('title', '')}").strip()
    return FilmInfo(path, duration, chosen["index"], desc)


def has_mvc(path: str) -> bool:
    """True if the first video track carries an MVC (3D) dependent view.

    Looks for subset SPS (NAL 15) or coded slice extension (NAL 20) in the first
    few frames. A 2D MKV has neither; neither has an MKV where the rip dropped MVC.
    """
    data = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", path, "-map", "0:v:0", "-c", "copy",
         "-bsf:v", "h264_mp4toannexb", "-frames:v", "3", "-f", "h264", "-"],
        capture_output=True,
    ).stdout
    nal_types = {data[m.end()] & 0x1F for m in re.finditer(b"\x00\x00\x01", data)
                 if m.end() < len(data)}
    return bool(nal_types & {15, 20})


def pick_encoder(requested: str = "auto") -> str:
    """'auto' tries NVENC with a tiny test encode, falling back to x264 (CPU)."""
    if requested != "auto":
        return requested
    test = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "color=s=256x256",
         "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"],
    )
    return "nvenc" if test.returncode == 0 else "x264"


def keyframe_at_or_before(path: str, seconds: float) -> float:
    """Timestamp of the keyframe ffmpeg -ss will actually start from (the one <= seconds).

    Video and audio both start from this exact time, so they stay aligned.
    """
    if seconds <= 0:
        return 0.0
    info = ffprobe_json(
        "-select_streams", "v:0",
        "-read_intervals", f"{seconds}%+#1",
        "-show_entries", "packet=pts_time,flags",
        path,
    )
    for pkt in info.get("packets", []):
        if "K" in pkt.get("flags", "") and pkt.get("pts_time") not in (None, "N/A"):
            return float(pkt["pts_time"])
    return seconds


def decode_command(film: FilmInfo, start: float, encode_args: str) -> str:
    """Full shell pipeline; encode_args holds codec options and output of the last ffmpeg."""
    f = shlex.quote(film.path)
    ss = f"-ss {start:.3f} " if start > 0 else ""
    # Video: for DTS-seeking formats with B-frames, ffmpeg moves the seek point
    # back by 3/23 s. Asking for exactly keyframe K would land on the PREVIOUS
    # keyframe (~1 s earlier) and the picture would lag the audio by that much.
    # Asking for K+0.2 lands on K; stream copy keeps packets from the keyframe
    # on, so the first frame is still K.
    ss_video = f"-ss {start + 0.2:.3f} " if start > 0 else ""
    audio_map = f"-map 1:{film.audio_index} " if film.audio_index is not None else ""
    return (
        f"ffmpeg -nostdin -v error {ss_video}-i {f} -map 0:v:0 -c copy "
        f"-bsf:v h264_mp4toannexb -f h264 - "
        f"| edge264_test - -Ok "
        f"| ffmpeg -nostdin -v warning -i - {ss}-i {f} -map 0:v {audio_map}"
        f"{encode_args}"
    )
