"""
pipeline.py — encoder choice and the decode chain, independent of the source.

    <source video command>              # Annex B H.264 + MVC (see sources.py)
      | edge264_test - -Ok              # MVC -> Y4M SBS 3840x1080
      | ffmpeg -i - <source audio> ...  # encode + audio -> output
"""
import shlex
import subprocess
from dataclasses import dataclass

from sources import Source

# Video encoders, best first. Bitrate is constant (CBR) because the virtual
# file maps bytes to seconds linearly.
ENCODERS = {
    "nvenc": "-c:v h264_nvenc -preset p4 -rc cbr -b:v {b} -maxrate {b} -bufsize {half} "
             "-forced-idr 1",
    "x264": "-c:v libx264 -preset veryfast -b:v {b} -maxrate {b} -bufsize {half} "
            "-x264-params nal-hrd=cbr",
}


@dataclass(frozen=True)
class Quality:
    name: str         # "normal" / "light"
    video: int        # video bit/s
    muxrate: int      # bit/s of the whole TS: video + audio + muxer headroom

    @property
    def bytes_per_sec(self) -> int:
        return self.muxrate // 8


# The byte <-> time mapping needs one constant bitrate per file, so a file cannot
# adapt to the network like a streaming service: a "light" version is offered
# next to the normal one instead, for rooms with weak Wi-Fi.
QUALITIES = {
    ("3d", "normal"): Quality("normal", 20_000_000, 24_000_000),   # 3840x1080 SBS
    ("3d", "light"): Quality("light", 8_000_000, 10_000_000),
    ("2d", "normal"): Quality("normal", 12_000_000, 15_000_000),   # 1920x1080
    ("2d", "light"): Quality("light", 5_000_000, 6_500_000),
    ("dvd", "normal"): Quality("normal", 4_000_000, 5_000_000),    # 1024x576 / 854x480
    ("dvd", "light"): Quality("light", 1_800_000, 2_400_000),
}


def quality_for(source: Source, level: str) -> Quality:
    return QUALITIES[(source.quality_key, level)]


def encoder_args(encoder: str, video_bitrate: int) -> str:
    kbit = video_bitrate // 1000
    return ENCODERS[encoder].format(b=f"{kbit}k", half=f"{kbit // 2}k")


def pick_encoder(requested: str = "auto") -> str:
    """'auto' tries NVENC with a tiny test encode, falling back to x264 (CPU)."""
    if requested != "auto":
        return requested
    test = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "color=s=256x256",
         "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"],
        check=False,
    )
    return "nvenc" if test.returncode == 0 else "x264"


def decode_command(source: Source, start: float, output_args: str) -> str:
    """Full shell pipeline from keyframe `start`.

    output_args: codecs and output of the last ffmpeg."""
    if source.two_d:
        # 2D Blu-ray / DVD: one stream with video, audio and maybe a subtitle
        # track, ffmpeg decodes it all; the subtitle is drawn onto the picture
        pre, post = source.pre_filter or "null", source.post_filter or "null"
        if source.sub is not None:
            graph = (f"[{source.video_map}]{pre}[pre];"
                     f"[pre][{source.sub_ref(0)}]overlay=eof_action=pass[ov];[ov]{post}[v]")
            sub_args = source.sub_decoder_args()
        else:
            graph, sub_args = f"[{source.video_map}]{pre},{post}[v]", ""
        return (
            f"{source.video_command(start)} "
            f"| ffmpeg -nostdin -v warning {sub_args} -f {source.input_format} "
            f"-analyzeduration 2000000 -probesize 10000000 -i - "
            f"-filter_complex {shlex.quote(graph)} -map '[v]' {source.audio_map(0)} {output_args}"
        )
    # 3D: the subtitle comes with the audio (input #1) and is drawn on both halves,
    # each copy moved inward so that it floats slightly in front of the screen
    if source.sub is not None:
        d, half = source.sub_depth, int(source.frame_size.split("x")[0]) // 2
        graph = (f"[{source.sub_ref(1)}]split[sa][sb];"
                 f"[0:v][sa]overlay=x={d}:y=0:eof_action=pass[l];"
                 f"[l][sb]overlay=x={half - d}:y=0:eof_action=pass[v]")
        video = f"-filter_complex {shlex.quote(graph)} -map '[v]'"
        sub_args = source.sub_decoder_args()
    else:
        video, sub_args = "-map 0:v", ""
    return (
        f"{source.video_command(start)} "
        f"| edge264_test - -Ok "
        f"| ffmpeg -nostdin -v warning -i - {sub_args} {source.audio_input(start)} "
        f"{video} {source.audio_map()} {output_args}"
    )
