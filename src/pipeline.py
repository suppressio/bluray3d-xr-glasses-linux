"""
pipeline.py — encoder choice and the decode chain, independent of the source.

    <source video command>              # Annex B H.264 + MVC (see sources.py)
      | edge264mvc_test - -O              # MVC -> Y4M SBS 3840x1080
      | ffmpeg -i - <source audio> ...  # encode + audio -> output
"""
import glob
import shlex
import subprocess
from dataclasses import dataclass

from sources import Source

# Video encoders, best first. Bitrate is constant (CBR) because the virtual
# file maps bytes to seconds linearly. VAAPI only encodes frames already in
# a hardware surface, hence its device (baked in here: no --vaapi-device
# option, see pick_encoder()) and FILTER_SUFFIX below.
ENCODERS = {
    "nvenc": "-c:v h264_nvenc -preset p4 -rc cbr -b:v {b} -maxrate {b} -bufsize {half} "
             "-forced-idr 1",
    "vaapi": "-vaapi_device {device} -c:v h264_vaapi -rc_mode CBR -b:v {b} -maxrate {b} "
             "-bufsize {half}",
    "x264": "-c:v libx264 -preset veryfast -b:v {b} -maxrate {b} -bufsize {half} "
            "-x264-params nal-hrd=cbr",
}

# ffmpeg -c:v value tested when picking an encoder (see pick_encoder())
_CODECS = {"nvenc": "h264_nvenc", "vaapi": "h264_vaapi", "x264": "libx264"}

# a per-encoder filter chain appended to the video output of every graph that
# feeds the encoder (decode_command's branches, VirtualFile.filler_command):
# h264_vaapi only takes frames already uploaded to the GPU
FILTER_SUFFIX = {"nvenc": "", "vaapi": "format=nv12,hwupload", "x264": ""}


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


def encoder_args(encoder: str, video_bitrate: int, vaapi_device: str = "") -> str:
    kbit = video_bitrate // 1000
    return ENCODERS[encoder].format(b=f"{kbit}k", half=f"{kbit // 2}k", device=vaapi_device)


def filter_suffix(encoder: str) -> str:
    return FILTER_SUFFIX[encoder]


def with_filter_suffix(graph: str, suffix: str) -> str:
    """`graph`, a filter_complex chain ending in the label '[v]', with `suffix`
    (filter_suffix(), e.g. VAAPI's format=nv12,hwupload) chained onto its video
    output just before that label. `graph` unchanged when `suffix` is empty."""
    return f"{graph[:-len('[v]')]},{suffix}[v]" if suffix else graph


def _test_encode(codec: str, *, filter_args: str = "", vaapi_device: str = "") -> bool:
    """A real 1-frame encode: the only reliable way to tell a codec ffmpeg knows
    the name of from one that actually works here (the right GPU, the right
    driver) — never a silent fallback for a choice (forced or automatic) that
    looks right on paper but fails the moment it decodes something."""
    device = ["-vaapi_device", vaapi_device] if vaapi_device else []
    vf = ["-vf", filter_args] if filter_args else []
    test = subprocess.run(
        ["ffmpeg", "-v", "quiet", *device, "-f", "lavfi", "-i", "color=s=256x256",
         *vf, "-frames:v", "1", "-c:v", codec, "-f", "null", "-"],
        check=False,
    )
    return test.returncode == 0


def _vaapi_device_that_works() -> str | None:
    """The first /dev/dri/renderD* that actually encodes h264_vaapi: on a
    machine with both an Intel and an NVIDIA GPU, the NVIDIA render node
    exists but its VA driver does not encode (seen on RICK, SPN#156)."""
    for device in sorted(glob.glob("/dev/dri/renderD*")):
        if _test_encode(_CODECS["vaapi"], filter_args=FILTER_SUFFIX["vaapi"],
                        vaapi_device=device):
            return device
    return None


def pick_encoder(requested: str = "auto") -> tuple[str, str]:
    """(encoder, vaapi_device): vaapi_device is "" unless encoder == "vaapi".

    'auto' tries NVENC, then VAAPI (scanning /dev/dri/renderD*), each with a
    real test encode, falling back to x264 (CPU, untested: nothing is left to
    fall back to). A forced encoder (anything but "auto") whose test encode
    fails stops the program instead of silently falling back."""
    if requested not in ("auto", *ENCODERS):
        raise ValueError(f"unknown encoder {requested!r}")
    if requested in ("auto", "nvenc") and _test_encode(_CODECS["nvenc"]):
        return "nvenc", ""
    if requested == "nvenc":
        raise SystemExit("--encoder nvenc: the test encode failed, no usable NVENC GPU here")
    if requested in ("auto", "vaapi"):
        device = _vaapi_device_that_works()
        if device is not None:
            return "vaapi", device
        if requested == "vaapi":
            raise SystemExit("--encoder vaapi: the test encode failed on every "
                             "/dev/dri/renderD* device")
    if requested == "x264" and not _test_encode(_CODECS["x264"]):
        raise SystemExit("--encoder x264: the test encode failed")
    return "x264", ""


def decode_command(source: Source, start: float, output_args: str,
                   filter_suffix: str = "") -> str:
    """Full shell pipeline from keyframe `start`.

    output_args: codecs and output of the last ffmpeg. filter_suffix: the
    chosen encoder's (see pipeline.filter_suffix()), chained onto the video
    output of whichever branch below feeds the encoder."""
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
        graph = with_filter_suffix(graph, filter_suffix)
        return (
            f"{source.video_command(start)} "
            f"| ffmpeg -nostdin -v warning {sub_args} -f {source.input_format} "
            f"{source.probe_args} -i - "
            f"-filter_complex {shlex.quote(graph)} -map '[v]' {source.audio_map(0)} {output_args}"
        )
    # 3D: the subtitle comes with the audio (input #1) and is drawn on both halves,
    # each copy moved inward so that it floats slightly in front of the screen
    if source.sub is not None:
        d, half = source.sub_depth, int(source.frame_size.split("x")[0]) // 2
        graph = (f"[{source.sub_ref(1)}]split[sa][sb];"
                 f"[0:v][sa]overlay=x={d}:y=0:eof_action=pass[l];"
                 f"[l][sb]overlay=x={half - d}:y=0:eof_action=pass[v]")
        graph = with_filter_suffix(graph, filter_suffix)
        video = f"-filter_complex {shlex.quote(graph)} -map '[v]'"
        sub_args = source.sub_decoder_args()
    elif filter_suffix:
        graph = f"[0:v]{filter_suffix}[v]"
        video, sub_args = f"-filter_complex {shlex.quote(graph)} -map '[v]'", ""
    else:
        video, sub_args = "-map 0:v", ""
    return (
        f"{source.video_command(start)} "
        f"| edge264mvc_test - -O "
        f"| ffmpeg -nostdin -v warning -i - {sub_args} {source.audio_input(start)} "
        f"{video} {source.audio_map()} {output_args}"
    )
