"""The virtual file: byte <-> time mapping, pipelines, jumps, late reads, the
loading animation, the tree of files, the drive watcher. FFmpeg is replaced by
a small program writing numbered TS packets, so every byte served can be
checked against the offset it was read at."""
import copy
import logging
import re
import shlex
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Self, cast, override

import pytest
import trio
import trio.testing

import bd3d_fs
from bd3d_fs import (
    AUDIO_TRACK_MUX,
    NULL_PACKET,
    TS_PACKET,
    Bd3dFS,
    Entry,
    FileEntry,
    Folder,
    Library,
    SidecarFile,
    VirtualFile,
    null_padding,
)
from pipeline import Quality
from sources import Source, SubTrack

PPS = 500                                  # TS packets per second of movie
BPS = PPS * TS_PACKET                      # bytes per second
QUALITY = Quality("normal", 1, BPS * 8)
KEYFRAME_EVERY = 50                        # packets (0.1 s)

PRODUCER = """
import sys, time
time.sleep({delay})
i, tag, out = {first}, {tag!r}, sys.stdout.buffer
while True:
    chunk = bytearray()
    for _ in range(500):
        key = i % {every} == 0
        head = bytes([0x47, 0x41 if key else 0x01, 0x00, 0x30 if key else 0x10,
                      7 if key else 0, 0x40 if key else 0])
        chunk += head + bytes(6) + i.to_bytes(8, "big") + tag + bytes(188 - 21)
        i += 1
    out.write(chunk)
    out.flush()
"""


def producer(start: float, tag: bytes, delay: float = 0.0) -> str:
    first = int(start * BPS) // TS_PACKET
    code = PRODUCER.format(delay=delay, first=first, tag=tag, every=KEYFRAME_EVERY)
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


class FakeSource(Source):
    """A movie with a keyframe every second."""

    def __init__(self, path: str, duration: float = 600.0, max_pipelines: int = 1,
                 langs: list[str] | None = None, delay: float = 0.0) -> None:
        self.path, self.name, self.duration = path, "Movie", duration
        self.max_pipelines, self.delay = max_pipelines, delay
        self.audio_langs = langs or ["ita"]
        self.audio_desc, self.sidecars, self.mtime_ns = "fake", [], 0
        self.subs = []

    @override
    def keyframe_at_or_before(self, seconds: float) -> float:
        return float(int(max(0.0, seconds)))

    @override
    def video_command(self, start: float) -> str:
        return ""

    @override
    def audio_input(self, start: float) -> str:
        return ""

    @override
    def audio_map(self, input_index: int = 1) -> str:
        return ""


def fake_decode(source: Source, start: float, output_args: str) -> str:
    return producer(start, b"M", source.delay if isinstance(source, FakeSource) else 0.0)


def fake_filler(vf: VirtualFile, video_input: str, t0: float, duration: float | None = None,
                extra: str = "", *, sbs: bool = False) -> str:
    return producer(t0, b"L")


def no_invalidate(inode: int, name: bytes, deleted: int = 0, ignore_enoent: bool = False) -> None:
    pass


_running: list[VirtualFile] = []


@pytest.fixture(autouse=True)
def small_file_system(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Smaller caches and distances, fake pipelines; every file stopped afterwards."""
    for name, value in {"AHEAD_MAX": 256 * 1024, "BEHIND_KEEP": 128 * 1024,
                        "JUMP_TOLERANCE": 256 * 1024, "EDGE_CACHE": 64 * 1024}.items():
        monkeypatch.setattr(bd3d_fs, name, value)
    monkeypatch.setattr(bd3d_fs, "decode_command", fake_decode)
    monkeypatch.setattr(VirtualFile, "filler_command", fake_filler)
    monkeypatch.setattr(bd3d_fs.pyfuse3, "invalidate_entry_async", no_invalidate)
    yield
    while _running:
        _running.pop().stop()


def vfile(tmp_path: Path, source: FakeSource | None = None,
          loader: str | None = None) -> VirtualFile:
    source = source or FakeSource(str(tmp_path / "disc"))
    vf = VirtualFile(10, source, "x264", QUALITY, str(tmp_path / "pipeline.log"), loader=loader)
    _running.append(vf)
    return vf


def packets(data: bytes, offset: int) -> list[tuple[int, bytes]]:
    """(index, tag) of every whole packet in data read at offset."""
    first = -offset % TS_PACKET
    return [(int.from_bytes(data[o + 12:o + 20], "big"), data[o + 20:o + 21])
            for o in range(first, len(data) - TS_PACKET + 1, TS_PACKET)]


def assert_movie(data: bytes, offset: int, tag: bytes = b"M") -> None:
    got = packets(data, offset)
    assert got, "no whole packet"
    first = (offset + TS_PACKET - 1) // TS_PACKET
    assert got == [(first + n, tag) for n in range(len(got))]


def test_null_padding() -> None:
    assert null_padding(0, 376) == NULL_PACKET * 2
    assert null_padding(100, 400) == (NULL_PACKET * 4)[100:500]
    assert len(null_padding(187, 1)) == 1


def test_names_sizes_and_bitrate(tmp_path: Path) -> None:
    s = FakeSource(str(tmp_path / "d"), duration=100.5, langs=["ita", "eng", "deu"])
    s.label = "ITAsubENG"
    vf = vfile(tmp_path, s)
    assert vf.name == "ITAsubENG - Movie - 3D SBS.ts"
    assert vf.muxrate == QUALITY.muxrate + 2 * AUDIO_TRACK_MUX       # two extra audio tracks
    assert vf.size % TS_PACKET == 0
    assert vf.size == int(100.5 * vf.bytes_per_sec) // TS_PACKET * TS_PACKET
    assert vf.tail_start % bd3d_fs.TAIL_ALIGN == 0 and vf.tail_start <= vf.size - 64 * 1024


def test_sequential_reads(tmp_path: Path) -> None:
    vf = vfile(tmp_path)
    offset = 0
    for size in (4096, 65536, 131072, 1000, 70000):
        data = vf.read(offset, size)
        assert len(data) == size
        assert_movie(data, offset)
        offset += size
    assert len(vf.gens) == 1
    assert vf.head is not None and len(vf.head) == 64 * 1024     # the start stays cached
    assert vf.read(vf.size, 10) == b"" and vf.read(vf.size + 5, 10) == b""


def test_small_jump_forward_keeps_the_pipeline(tmp_path: Path) -> None:
    vf = vfile(tmp_path)
    vf.read(0, 4096)
    gen = vf.gens[0]
    data = vf.read(200 * 1024, 4096)                      # within JUMP_TOLERANCE
    assert_movie(data, 200 * 1024)
    assert vf.gens == [gen]


def test_jump_restarts_from_the_keyframe(tmp_path: Path) -> None:
    vf = vfile(tmp_path)
    vf.read(0, 4096)
    old = vf.gens[0]
    offset = int(123.6 * BPS) + 17                          # mid-packet, mid-second
    data = vf.read(offset, 20000)
    assert_movie(data, offset)
    new = vf.gens[0]
    assert new is not old and old.stopped                   # one pipeline per disc
    assert new.base == 123 * BPS                            # the keyframe before, aligned
    assert vf.stale is old


def test_late_reads_for_the_old_position(tmp_path: Path) -> None:
    vf = vfile(tmp_path)
    vf.read(0, 100_000)
    old = vf.gens[0]
    vf.read(300 * BPS, 4096)                                 # the jump
    new = vf.gens[0]
    # the player's reads for where it was, still in flight: from the old data...
    held = vf.read(50_000, 4096)
    assert_movie(held, 50_000)
    # ...or, past what the old pipeline had made, null packets, without
    # touching the new pipeline
    late_offset = old.end + 10_000
    late = vf.read(late_offset, 4096)
    assert late == null_padding(late_offset, 4096)
    assert vf.gens == [new] and not new.stopped


def test_going_back_for_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bd3d_fs, "BOTH_READ", 0.2)
    vf = vfile(tmp_path)
    vf.read(0, 100_000)
    old = vf.gens[0]
    vf.read(300 * BPS, 4096)
    new = vf.gens[0]
    time.sleep(0.3)                                  # the new position is not read any more
    back = old.end + 10_000
    data = vf.read(back, 4096)
    assert_movie(data, back)
    assert new.stopped and vf.gens[0] is not new


def test_two_pipelines_for_files(tmp_path: Path) -> None:
    vf = vfile(tmp_path, FakeSource(str(tmp_path / "m.mkv"), max_pipelines=2))
    vf.read(0, 4096)
    vf.read(400 * BPS, 4096)                        # e.g. a player reading the duration
    assert len(vf.gens) == 2
    assert_movie(vf.read(100_000, 4096), 100_000)   # past the cached start of the file
    vf.read(200 * BPS, 4096)                         # a third place: the least recent goes
    assert len(vf.gens) == 2 and vf.gens[0].base == 0


def test_synthetic_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vf = vfile(tmp_path)
    length = vf.size - vf.tail_start

    def tail(self: VirtualFile) -> bytes:
        return b"T" * length

    monkeypatch.setattr(VirtualFile, "_synthetic_tail", tail)
    assert vf.read(vf.size - 1000, 5000) == b"T" * 1000
    assert vf.gens == []                             # the end never starts a pipeline
    across = vf.read(vf.tail_start - 376, 1000)       # playback reaching the end
    assert_movie(across[:376], vf.tail_start - 376)
    assert across[376:] == b"T" * 624


def test_loading_animation_after_a_jump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the movie gets ready while the animation plays: it must be allowed to run
    # ahead of the player at least as far as the animation lasts
    monkeypatch.setattr(bd3d_fs, "AHEAD_MAX", 8 * 1024 * 1024)
    source = FakeSource(str(tmp_path / "disc"), delay=1.5)
    vf = vfile(tmp_path, source, loader="spinner.mp4")
    vf.read(0, 4096)                                     # first open: no animation
    assert vf.gens[0].loader is None
    offset = 200 * BPS
    seen: list[tuple[int, bytes]] = []
    while offset < 200 * BPS + 6 * BPS:
        data = vf.read(offset, 32768)
        got = packets(data, offset)
        first = (offset + TS_PACKET - 1) // TS_PACKET
        assert [i for i, _ in got] == list(range(first, first + len(got)))   # no gap
        seen += got
        offset += 32768
    tags = [tag for _, tag in seen]
    switch = tags.index(b"M")
    assert switch > 0 and set(tags[:switch]) == {b"L"} and set(tags[switch:]) == {b"M"}
    # the movie takes over at one of its keyframes, once it is ready
    assert seen[switch][0] % KEYFRAME_EVERY == 0
    assert seen[switch][0] > 200 * PPS + PPS
    assert vf.gens[0].loader is None                     # the animation is stopped


def test_no_animation_when_paused(tmp_path: Path) -> None:
    vf = vfile(tmp_path, loader="spinner.mp4")
    vf.read(0, 4096)
    vf.last_read = time.monotonic() - 60                 # nothing read for a minute: paused
    vf.read(200 * BPS, 4096)
    assert vf.gens[0].loader is None


def test_other_files_of_the_disc_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    disc = str(tmp_path / "disc")
    ita, eng = vfile(tmp_path, FakeSource(disc)), vfile(tmp_path, FakeSource(disc))
    ita.read(0, 4096)
    eng.read(0, 4096)                                    # both read right now: both run
    assert ita.gens and eng.gens
    monkeypatch.setattr(bd3d_fs, "SIBLING_IDLE", 0)
    time.sleep(0.01)
    eng.read(100 * BPS, 4096)                            # a new pipeline for English
    assert ita.gens == [] and eng.gens


def test_stop_if_idle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vf = vfile(tmp_path)
    vf.read(0, 4096)
    gen = vf.gens[0]
    vf.stop_if_idle()
    assert vf.gens == [gen]
    monkeypatch.setattr(bd3d_fs, "IDLE_STOP", -1)
    vf.stop_if_idle()
    assert vf.gens == [] and gen.stopped


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def ready(seconds: float) -> Callable[[int], float]:
    """A stand-in for VirtualFile.ready_ahead: always `seconds` of movie ready."""
    def ready_ahead(offset: int) -> float:
        return seconds
    return ready_ahead


def test_network_too_slow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                          caplog: pytest.LogCaptureFixture) -> None:
    vf = vfile(tmp_path)
    clock = Clock()
    monkeypatch.setattr(bd3d_fs.time, "monotonic", clock)
    monkeypatch.setattr(vf, "ready_ahead", ready(30.0))             # the movie is ready
    caplog.set_level(logging.INFO, logger="bd3d_fs")
    offset = 0
    for _ in range(50):                                  # 25 s at 60% of the movie's rate
        vf._track_rate(offset, int(0.3 * vf.bytes_per_sec))
        vf.last_read = clock.now
        offset += int(0.3 * vf.bytes_per_sec)
        clock.now += 0.5
    percent = re.search(r"receives (\d+)% of the data rate", caplog.text)
    assert percent and 55 <= int(percent[1]) <= 65 and "(try Light/)" in caplog.text
    for _ in range(100):                                 # then real time again
        vf._track_rate(offset, vf.bytes_per_sec // 2)
        vf.last_read = clock.now
        offset += vf.bytes_per_sec // 2
        clock.now += 0.5
    assert "the network (or the player)" in caplog.text
    assert "back to real time" in caplog.text


def test_disc_too_slow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                       caplog: pytest.LogCaptureFixture) -> None:
    """The same slow reads, but nothing ready ahead: the disc side is to blame."""
    vf = vfile(tmp_path)
    clock = Clock()
    monkeypatch.setattr(bd3d_fs.time, "monotonic", clock)
    monkeypatch.setattr(vf, "ready_ahead", ready(0.5))
    caplog.set_level(logging.INFO, logger="bd3d_fs")
    offset = 0
    for _ in range(50):
        vf._track_rate(offset, int(0.3 * vf.bytes_per_sec))
        vf.last_read = clock.now
        offset += int(0.3 * vf.bytes_per_sec)
        clock.now += 0.5
    assert "the disc or the decoding cannot keep up" in caplog.text
    assert "Light/" not in caplog.text                   # a lighter file would not help


def test_clock_time() -> None:
    assert bd3d_fs.clock_time(4150.7) == "1:09:10"
    assert bd3d_fs.clock_time(59) == "0:00:59"


def test_pipeline_log_appends_and_rotates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "pipeline.log"
    for title in ("first", "second"):
        with bd3d_fs.open_pipeline_log(str(path), title) as f:
            f.write(f"output of {title}\n")
    text = path.read_text()
    assert "first ===" in text and "output of first" in text and "second ===" in text
    monkeypatch.setattr(bd3d_fs, "PIPELINE_LOG_MAX", 10)
    with bd3d_fs.open_pipeline_log(str(path), "third"):
        pass
    assert "output of first" in (tmp_path / "pipeline.log.1").read_text()
    assert "first" not in path.read_text() and "third ===" in path.read_text()


def test_log_to_file(tmp_path: Path) -> None:
    path = tmp_path / "state" / "bluray3d-xr.log"
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        bd3d_fs.log_to_file(str(path))
        bd3d_fs.log.warning("hello from the test")
        for h in root.handlers:
            h.flush()
        assert "hello from the test" in path.read_text()
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    bd3d_fs.log_to_file("none")                          # no file, no error
    assert root.handlers == before


# --- the tree of files ---------------------------------------------------------

class DiscSource(FakeSource):
    """Three audio languages, three subtitle languages."""

    def __init__(self, path: str) -> None:
        super().__init__(path, langs=["ita", "eng", "deu"])
        self.category, self.name_suffix = "Blu-ray", ""
        self.subs = [SubTrack(1, "ita"), SubTrack(2, "eng"), SubTrack(3, "fra")]

    @override
    def variants(self) -> Sequence[Self]:
        out: list[Self] = []
        for lang in self.audio_langs:
            v = copy.copy(self)
            v.audio_langs, v.label = [lang], lang.upper()
            out.append(v)
        return out


def tree(fs: Bd3dFS) -> list[str]:
    def path(e: Entry) -> str:
        parts: list[str] = []
        while e.inode != bd3d_fs.pyfuse3.ROOT_INODE:
            parts.append(e.name)
            e = fs.nodes[e.parent]
        return "/".join(reversed(parts))
    return sorted(path(e) for e in fs.nodes.values() if e.inode != bd3d_fs.pyfuse3.ROOT_INODE)


def movies(fs: Bd3dFS) -> list[VirtualFile]:
    return [e for e in fs.files() if isinstance(e, VirtualFile)]


def library(tmp_path: Path, audio_files: str = "per-language", light: bool = False,
            sub_langs: list[str] | None = None) -> tuple[Bd3dFS, Library]:
    fs = Bd3dFS()
    return fs, Library(fs, "x264", str(tmp_path / "log"), audio_files, light, sub_langs)


def test_library_per_language(tmp_path: Path) -> None:
    fs, lib = library(tmp_path, sub_langs=["eng", "ita", "jpn"])
    lib.add(DiscSource(str(tmp_path / "d")))
    assert tree(fs) == sorted(["Blu-ray"] + [f"Blu-ray/{a}{s} - Movie.ts"
                                             for a in ("ITA", "ENG", "DEU")
                                             for s in ("", "subENG", "subITA")])
    files = {e.name: e for e in movies(fs)}
    # the plain file carries the forced subtitles of its language, if the disc has them
    assert (files["ITA - Movie.ts"].source.sub, files["ITA - Movie.ts"].source.forced_only) == \
        (SubTrack(1, "ita"), True)
    assert files["DEU - Movie.ts"].source.sub is None
    assert files["DEUsubENG - Movie.ts"].source.sub == SubTrack(2, "eng")
    assert not files["DEUsubENG - Movie.ts"].source.forced_only


def test_library_all_and_no_subtitles(tmp_path: Path) -> None:
    fs, lib = library(tmp_path, audio_files="single")
    lib.add(DiscSource(str(tmp_path / "d")))
    assert tree(fs) == ["Blu-ray", "Blu-ray/Movie.ts", "Blu-ray/subENG - Movie.ts",
                        "Blu-ray/subFRA - Movie.ts", "Blu-ray/subITA - Movie.ts"]
    fs, lib = library(tmp_path, audio_files="single", sub_langs=[])
    lib.add(DiscSource(str(tmp_path / "d2")))
    assert tree(fs) == ["Blu-ray", "Blu-ray/Movie.ts"]


def test_library_both_light_and_removal(tmp_path: Path) -> None:
    fs, lib = library(tmp_path, audio_files="both", light=True, sub_langs=[])
    source = DiscSource(str(tmp_path / "d"))
    source.sidecars = [(".ita.srt", str(tmp_path / "x.ita.srt"))]
    (tmp_path / "x.ita.srt").write_text("1\n")
    added = lib.add(source)
    expected: list[str] = []
    for folder in ("Blu-ray", "Blu-ray/Light"):
        for name in ("ITA - Movie", "ENG - Movie", "DEU - Movie", "Multi-audio/Movie"):
            expected += [f"{folder}/{name}.ts", f"{folder}/{name}.ita.srt"]
        expected += [folder, f"{folder}/Multi-audio"]
    assert tree(fs) == sorted(expected)
    light = next(e for e in movies(fs) if e.name == "ITA - Movie.ts" and "Light" in e.path)
    assert light.quality.name == "light" and light.path == "Blu-ray/Light/ITA - Movie.ts"
    multi = next(e for e in movies(fs) if e.name == "Movie.ts" and "Light" not in e.path)
    assert multi.muxrate == multi.quality.muxrate + 2 * AUDIO_TRACK_MUX
    sidecar = next(e for e in fs.files() if isinstance(e, SidecarFile))
    assert sidecar.read(0, 100) == b"1\n"

    lib.remove(added)
    assert tree(fs) == []                              # empty folders go too
    assert isinstance(fs.nodes[bd3d_fs.pyfuse3.ROOT_INODE], Folder)


def test_file_system_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fs, lib = library(tmp_path, audio_files="single", sub_langs=[])
    source = DiscSource(str(tmp_path / "d"))
    source.sidecars = [(".srt", str(tmp_path / "x.srt"))]
    (tmp_path / "x.srt").write_text("hello")
    lib.add(source)
    movie = movies(fs)[0]
    sidecar = next(e for e in fs.files() if isinstance(e, SidecarFile))
    folder = fs.folder("Blu-ray")
    listed: list[bytes] = []

    def reply(token: object, name: bytes, attr: object, next_id: int) -> bool:
        listed.append(name)
        return True

    monkeypatch.setattr(bd3d_fs.pyfuse3, "readdir_reply", reply)
    token = cast("bd3d_fs.pyfuse3.ReaddirToken", object())

    async def check() -> None:
        attr = await fs.getattr(movie.inode)
        assert attr.st_size == movie.size and attr.st_mode & 0o777 == 0o444
        assert (await fs.lookup(folder.inode, b"Movie.ts")).st_ino == movie.inode
        assert (await fs.getattr(folder.inode)).st_mode & 0o040000
        with pytest.raises(bd3d_fs.pyfuse3.FUSEError):
            await fs.lookup(folder.inode, b"missing.ts")
        with pytest.raises(bd3d_fs.pyfuse3.FUSEError):
            await fs.getattr(9999)
        assert await fs.opendir(folder.inode, None) == folder.inode
        with pytest.raises(bd3d_fs.pyfuse3.FUSEError):
            await fs.opendir(movie.inode, None)
        await fs.readdir(folder.inode, 0, token)
        assert listed == [b"Movie.ts", b"Movie.srt"]
        # movies bypass the page cache (their bytes can change), subtitles do not
        assert (await fs.open(movie.inode, 0, None)).direct_io
        assert not (await fs.open(sidecar.inode, 0, None)).direct_io
        with pytest.raises(bd3d_fs.pyfuse3.FUSEError):
            await fs.open(movie.inode, 1, None)                     # O_WRONLY
        assert await fs.read(sidecar.inode, 0, 100) == b"hello"
        with pytest.raises(bd3d_fs.pyfuse3.FUSEError):
            await fs.read(12345, 0, 10)
        s = await fs.statfs(None)
        assert s.f_bavail == 0 and s.f_blocks >= movie.size // s.f_frsize

    trio.run(check)


# --- the drive watcher ---------------------------------------------------------

def test_watch_drive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ok, no_disc, tray, busy = 4, 1, 2, 3
    statuses = iter([ok, ok, busy, ok, no_disc, ok, busy, no_disc, tray, tray, ok, ok])
    events: list[str] = []

    class NoMoreStatusError(Exception):
        pass

    def status(device: str) -> int:
        try:
            return next(statuses)
        except StopIteration:
            raise NoMoreStatusError from None

    opened = iter([OSError("cannot decrypt"), DiscSource(str(tmp_path / "d"))])

    def open_disc(device: str, langs: list[str] | None) -> list[Source]:
        item = next(opened)
        if isinstance(item, Exception):
            raise item
        return [item]

    class Lib:
        def add(self, source: Source) -> list[FileEntry]:
            events.append("add")
            return []

        def remove(self, entries: Sequence[FileEntry]) -> None:
            events.append(f"remove {len(entries)}")

    monkeypatch.setattr(bd3d_fs, "drive_status", status)
    monkeypatch.setattr(bd3d_fs, "open_disc", open_disc)

    async def main() -> None:
        with pytest.raises(NoMoreStatusError):
            await bd3d_fs.watch_drive("/dev/sr0", Lib(), None)

    trio.run(main, clock=trio.testing.MockClock(autojump_threshold=0))
    # 1st disc: cannot be opened, not retried while it stays in; a busy drive and
    # a single "no disc" are not an eject; two in a row are; the 2nd disc opens
    assert events == ["remove 0", "add"]


# --- version ---------------------------------------------------------------------

def test_program_version(tmp_path: Path) -> None:
    (tmp_path / "version").write_text("v1.0.0-3-gabc1234\n")
    assert bd3d_fs.program_version(tmp_path) == "v1.0.0-3-gabc1234"
    empty = tmp_path / "not-installed"
    empty.mkdir()
    assert bd3d_fs.program_version(empty) == "unknown"          # no file, not a git clone
    # from a clone: whatever git describe says (a tag, or the commit before any tag)
    assert bd3d_fs.program_version().strip()


def test_a_failing_read_does_not_stop_the_program(tmp_path: Path,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    fs, lib = library(tmp_path, audio_files="single", sub_langs=[])
    lib.add(DiscSource(str(tmp_path / "d")))
    movie = movies(fs)[0]

    def broken(offset: int, size: int) -> bytes:
        raise OSError("no navigation pack at sector 35546")

    movie.read = broken  # type: ignore[method-assign]

    async def read() -> None:
        with pytest.raises(bd3d_fs.pyfuse3.FUSEError):
            await fs.read(movie.inode, 0, 10)

    trio.run(read)
    assert "read at 0 failed" in caplog.text and "navigation pack" in caplog.text


def test_slow_pipeline_is_logged(monkeypatch: pytest.MonkeyPatch,
                                 caplog: pytest.LogCaptureFixture) -> None:
    """A read that waits for the pipeline names the disc side and the position."""
    clock = Clock()
    monkeypatch.setattr(bd3d_fs.time, "monotonic", clock)
    gen = object.__new__(bd3d_fs.Generator)
    gen.buf, gen.buf_start, gen.reader_pos = bytearray(), 4_150_000, 4_150_000
    gen.eof = gen.stopped = gen.is_loader = False
    gen.slow_logged, gen.last_used = 0.0, 0.0
    gen.path, gen.bytes_per_sec = "Blu-ray 3D/ITA - x - 3D SBS.ts", 1000

    class Cond:                          # every wait lasts 3 s; data comes after two
        waits = 0

        def __enter__(self) -> None: ...

        def __exit__(self, *exc: object) -> None: ...

        def notify_all(self) -> None: ...

        def wait(self, timeout: float) -> None:
            clock.now += 3
            Cond.waits += 1
            if Cond.waits == 2:
                gen.buf += b"x" * 5000

    gen.cond = cast(threading.Condition, Cond())
    caplog.set_level(logging.INFO, logger="bd3d_fs")
    assert gen.read(4_150_000, 100) == b"x" * 100
    assert "waited 6s for the movie at 1:09:10" in caplog.text
