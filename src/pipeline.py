"""
pipeline.py — encoder choice and the decode chain, independent of the source.

    <source video command>              # Annex B H.264 + MVC (see sources.py)
      | edge264_test - -Ok              # MVC -> Y4M SBS 3840x1080
      | ffmpeg -i - <source audio> ...  # encode + audio -> output
"""
import subprocess

from sources import Source

# Video encoders, best first. Bitrate is constant (CBR) because the virtual
# file maps bytes to seconds linearly.
ENCODERS = {
    "nvenc": "-c:v h264_nvenc -preset p4 -rc cbr -b:v 20M -maxrate 20M -bufsize 10M "
             "-forced-idr 1",
    "x264": "-c:v libx264 -preset veryfast -b:v 20M -maxrate 20M -bufsize 10M "
            "-x264-params nal-hrd=cbr",
}


def pick_encoder(requested: str = "auto") -> str:
    """'auto' tries NVENC with a tiny test encode, falling back to x264 (CPU)."""
    if requested != "auto":
        return requested
    test = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "color=s=256x256",
         "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"],
    )
    return "nvenc" if test.returncode == 0 else "x264"


def decode_command(source: Source, start: float, output_args: str) -> str:
    """Full shell pipeline from keyframe `start`; output_args = codecs + output of the last ffmpeg."""
    return (
        f"{source.video_command(start)} "
        f"| edge264_test - -Ok "
        f"| ffmpeg -nostdin -v warning -i - {source.audio_input(start)} "
        f"-map 0:v {source.audio_map()} {output_args}"
    )
