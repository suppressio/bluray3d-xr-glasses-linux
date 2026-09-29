"""
Integration tests on a real disc (or a 3D MKV rip): run with

    pytest -m integration                         # the disc in /dev/sr0
    BD3D_TEST_DRIVE=/dev/sr1 pytest -m integration
    BD3D_TEST_MKV=~/Videos/movie.mkv pytest -m integration

The first run on a disc records a reference: what the program finds on it
(title, duration, languages, keyframes) and fingerprints of what it reads from
it (the stream handed to the decoders, the decoded 3D frames). Later runs
compare against it, so a change that alters a single byte of that output fails.
The reference stays in ~/.cache/bluray3d-xr/reference/ (hashes only, no disc
content); BD3D_UPDATE_REFERENCE=1 records it again after an intended change.

The encoded output (NVENC/x264) is not compared byte for byte, only checked:
streams, picture size, timestamps.
"""
import hashlib
import json
import os
import signal
import stat
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bd3d_fs import CDS_DISC_OK, VirtualFile, drive_status
from pipeline import pick_encoder, quality_for
from sources import BlurayDiscSource, MkvSource, Source, open_disc

pytestmark = pytest.mark.integration

REFERENCE_DIR = Path.home() / ".cache" / "bluray3d-xr" / "reference"
EDGE264_DIRS = ["/opt/bluray3d-xr/bin", str(Path.home() / ".local" / "bin")]


@pytest.fixture(scope="module", autouse=True)
def edge264_on_path() -> Iterator[None]:
    saved = os.environ["PATH"]
    os.environ["PATH"] = os.pathsep.join([*EDGE264_DIRS, saved])
    yield
    os.environ["PATH"] = saved


def _drive() -> str | None:
    drive = os.environ.get("BD3D_TEST_DRIVE", "/dev/sr0")
    try:
        if not stat.S_ISBLK(os.stat(drive).st_mode):
            return None
    except OSError:
        return None
    return drive if drive_status(drive) == CDS_DISC_OK else None


def _sources() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    drive = _drive()
    if drive:
        out.append(("disc", drive))
    mkv = os.environ.get("BD3D_TEST_MKV")
    if mkv:
        out.append(("mkv", os.path.expanduser(mkv)))
    return out


@pytest.fixture(scope="module", params=_sources() or [pytest.param(None, marks=pytest.mark.skip(
    reason="no disc in the drive and BD3D_TEST_MKV not set"))], ids=lambda p: p and p[0])
def source(request: pytest.FixtureRequest) -> Source:
    kind, path = request.param
    return MkvSource(path) if kind == "mkv" else open_disc(path)


def _kind(s: Source) -> str:
    return "mkv" if isinstance(s, MkvSource) else s.quality_key


def _reference_key(s: Source) -> str:
    return f"{_kind(s)}-{s.name}".replace("/", "_")


class Reference:
    """Recorded values of one source; the first run records, later runs compare."""

    def __init__(self, key: str) -> None:
        self.path = REFERENCE_DIR / f"{key}.json"
        self.update = os.environ.get("BD3D_UPDATE_REFERENCE") == "1"
        self.values: dict[str, Any] = {}
        if self.path.exists() and not self.update:
            self.values = json.loads(self.path.read_text())

    def check(self, name: str, value: object) -> None:
        value = json.loads(json.dumps(value))            # the same types as when read back
        if name not in self.values:
            self.values[name] = value
            REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.values, indent=1, sort_keys=True))
            return
        assert value == self.values[name], f"{name} changed (reference: {self.path})"


@pytest.fixture(scope="module")
def reference(source: Source) -> Reference:
    return Reference(_reference_key(source))


def _read_command(cmd: str, size: int, fifo: str | None = None) -> tuple[bytes, bytes]:
    """The first `size` bytes a reader command writes, and the first 256 KB of
    the audio it writes into `fifo` (3D Blu-ray)."""
    audio = bytearray()

    def drain() -> None:
        with Path(str(fifo)).open("rb") as f:
            while len(audio) < 256 * 1024 and (chunk := f.read(65536)):
                audio.extend(chunk)
            while f.read(1 << 20):                         # keep it flowing until the end
                pass

    reader = threading.Thread(target=drain, daemon=True) if fifo else None
    if reader:
        reader.start()
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        ["bash", "-o", "pipefail", "-c", cmd], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True)
    assert proc.stdout is not None
    data = bytearray()
    while len(data) < size and (chunk := proc.stdout.read(min(1 << 20, size - len(data)))):
        data.extend(chunk)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    if reader:
        reader.join(timeout=10)
    return bytes(data), bytes(audio[:256 * 1024])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_what_the_disc_holds(source: Source, reference: Reference) -> None:
    assert source.duration > 600
    reference.check("metadata", {
        "kind": _kind(source), "name": source.name, "category": source.category,
        "duration": round(source.duration, 3), "audio": source.audio_desc,
        "audio_langs": source.audio_langs,
        "subs": [(t.stream, t.lang) for t in source.subs],
        "variants": [v.label for v in source.variants()],
    })


def test_keyframes(source: Source, reference: Reference) -> None:
    times = [0.0, 1.0] + [source.duration * f for f in (0.1, 0.37, 0.5, 0.83)]
    found = [round(source.keyframe_at_or_before(t), 3) for t in times]
    for t, k in zip(times, found, strict=True):
        assert 0 <= k <= t + 0.01 and t - k < 10
    reference.check("keyframes", found)


def _decoder_input(source: Source, at: float) -> tuple[bytes, bytes]:
    start = source.keyframe_at_or_before(at)
    cmd = source.video_command(start)
    fifo = getattr(source, "_fifo", None) if isinstance(source, BlurayDiscSource) and \
        not source.two_d else None
    return _read_command(cmd, 12 * 1024 * 1024, fifo)


def test_decoder_input(source: Source, reference: Reference) -> None:
    """The bytes handed to edge264 / ffmpeg: demuxed, decrypted, filtered, from a keyframe."""
    at = source.duration * 0.37
    video, audio = _decoder_input(source, at)
    assert len(video) == 12 * 1024 * 1024
    checked = {"video": _sha(video)}
    if audio:
        assert len(audio) == 256 * 1024
        checked["audio"] = _sha(audio)
    reference.check("decoder input at 37%", checked)


def test_decoded_3d_frames(source: Source, reference: Reference) -> None:
    if source.two_d:
        pytest.skip("2D: ffmpeg decodes it directly")
    video, _ = _decoder_input(source, source.duration * 0.5)
    frames = subprocess.run(
        # no pipefail: ffmpeg stops after 48 frames and edge264 then gets SIGPIPE
        ["bash", "-c",
         "edge264_test - -Ok | ffmpeg -v error -f yuv4mpegpipe -i - -frames:v 48 -f framemd5 -"],
        input=video, capture_output=True, check=True).stdout.decode()
    hashes = [line.rsplit(",", 1)[1].strip() for line in frames.splitlines()
              if line and not line.startswith("#")]
    assert len(hashes) == 48
    header = subprocess.run(["bash", "-c", "edge264_test - -Ok | head -c 64"],
                            input=video[:4_000_000], capture_output=True, check=False).stdout
    assert header.startswith(b"YUV4MPEG2 W3840 H1080 "), header
    reference.check("48 SBS frames at 50%", _sha("\n".join(hashes).encode()))


def test_virtual_file(source: Source, tmp_path: Path) -> None:
    """A piece of the virtual file, through the real pipeline: streams, picture
    size and timestamps where the byte <-> time mapping says."""
    variant = source.variants()[0]
    vf = VirtualFile(1000, variant, pick_encoder(), quality_for(variant, "light"),
                     str(tmp_path / "pipeline.log"))
    try:
        offset = int(vf.size * 0.3) // 188 * 188
        data = vf.read(offset, 3 * 1024 * 1024)
    finally:
        vf.stop()
    (tmp_path / "piece.ts").write_bytes(data)
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", "-show_entries",
         "stream=codec_type,codec_name,width,height:packet=pts_time", "-read_intervals", "%+#1",
         str(tmp_path / "piece.ts")], capture_output=True, text=True, check=True).stdout)
    streams = {(s["codec_type"], s["codec_name"]) for s in probe["streams"]}
    assert ("video", "h264") in streams and ("audio", "aac") in streams
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert f"{video['width']}x{video['height']}" == variant.frame_size
    first = float(next(p["pts_time"] for p in probe["packets"] if p.get("pts_time")))
    expected = offset / vf.bytes_per_sec
    assert abs(first - expected) < 3.0, (first, expected)
    assert "error" not in (tmp_path / "pipeline.log").read_text().lower()

