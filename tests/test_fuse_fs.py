"""fuse_fs.py: Bd3dFS, the thin FUSE adapter over a Tree library.py builds —
getattr/readdir/open/read/statfs by path, that a read failing on one file does
not take the program down, and a real mount on libfuse3 when /dev/fuse is
usable (mount, two readers at once, unmount with a file still open)."""
import ctypes
import errno
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import fuse_fs
from fuse_fs import Bd3dFS
from library import SidecarFile
from test_library import DiscSource, make_library, movies
from tree import ROOT_ID, Tree, current_reader

# through fuse_fs: it picks libfuse3 before mfusepy is first imported
mfusepy = fuse_fs.mfusepy


def fi(flags: int = os.O_RDONLY) -> Any:  # noqa: ANN401  what mfusepy hands open() with raw_fi
    return SimpleNamespace(flags=flags, fh=0, direct_io=0)


def errno_of(call: Any) -> int:  # noqa: ANN401
    with pytest.raises(mfusepy.FuseOSError) as e:
        call()
    return e.value.errno or 0


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> None:
    # open() asks libfuse who is calling: outside a request there is nobody
    monkeypatch.setattr(mfusepy, "fuse_get_context", lambda: (0, 0, os.getpid()))


@pytest.mark.usefixtures("context")
def test_file_system_operations(tmp_path: Path) -> None:
    tree, lib = make_library(tmp_path, audio_files="single", sub_langs=[])
    fs = Bd3dFS(tree)
    source = DiscSource(str(tmp_path / "d"))
    source.sidecars = [(".srt", str(tmp_path / "x.srt"))]
    (tmp_path / "x.srt").write_text("hello")
    lib.add(source)
    movie = movies(tree)[0]
    sidecar = next(e for e in tree.files() if isinstance(e, SidecarFile))

    attr = fs.getattr("/Blu-ray/Movie.ts")
    assert attr["st_size"] == movie.size and attr["st_mode"] & 0o777 == 0o444
    assert attr["st_ino"] == movie.inode
    assert fs.getattr("/Blu-ray")["st_mode"] & 0o040000
    assert fs.getattr("/")["st_ino"] == ROOT_ID
    assert errno_of(lambda: fs.getattr("/Blu-ray/missing.ts")) == errno.ENOENT
    # a name that is not UTF-8 reaches us with surrogates: not found
    assert errno_of(lambda: fs.getattr("/Blu-ray/Movie\udcff.ts")) == errno.ENOENT
    assert fs.readdir("/Blu-ray", 0) == [".", "..", "Movie.ts", "Movie.srt"]
    assert errno_of(lambda: fs.readdir("/Blu-ray/Movie.ts", 0)) == errno.ENOTDIR
    # movies bypass the page cache (their bytes can change), subtitles do not
    f = fi()
    fs.open("/Blu-ray/Movie.ts", f)
    assert f.direct_io == 1
    f = fi()
    fs.open("/Blu-ray/Movie.srt", f)
    assert f.direct_io == 0
    assert errno_of(lambda: fs.open("/Blu-ray/Movie.ts", fi(os.O_WRONLY))) == errno.EROFS
    assert errno_of(lambda: fs.open("/Blu-ray", fi())) == errno.EISDIR
    assert fs.read("/Blu-ray/Movie.srt", 100, 0, f) == b"hello"
    fs.release("/Blu-ray/Movie.srt", f)
    assert errno_of(lambda: fs.read("/Blu-ray/Movie.srt", 10, 0, f)) == errno.EIO  # closed
    assert errno_of(lambda: fs.read("/x", 10, 0, SimpleNamespace(fh=12345))) == errno.EIO
    s = fs.statfs("/")
    assert s["f_bavail"] == 0 and s["f_blocks"] >= movie.size // s["f_frsize"]
    assert sidecar.size == 5


@pytest.mark.usefixtures("context")
def test_a_read_of_an_ejected_file_is_an_io_error(tmp_path: Path) -> None:
    tree, lib = make_library(tmp_path, audio_files="single", sub_langs=[])
    fs = Bd3dFS(tree)
    lib.add(DiscSource(str(tmp_path / "d")))
    movie = movies(tree)[0]
    f = fi()
    fs.open("/Blu-ray/Movie.ts", f)
    tree.remove(movie)
    assert errno_of(lambda: fs.read("/Blu-ray/Movie.ts", 10, 0, f)) == errno.EIO


@pytest.mark.usefixtures("context")
def test_a_failing_read_does_not_stop_the_program(tmp_path: Path,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    tree, lib = make_library(tmp_path, audio_files="single", sub_langs=[])
    fs = Bd3dFS(tree)
    lib.add(DiscSource(str(tmp_path / "d")))
    movie = movies(tree)[0]

    def broken(offset: int, size: int) -> bytes:
        raise OSError("no navigation pack at sector 35546")

    movie.read = broken  # type: ignore[method-assign]
    f = fi()
    fs.open("/Blu-ray/Movie.ts", f)
    assert errno_of(lambda: fs.read("/Blu-ray/Movie.ts", 10, 0, f)) == errno.EIO
    assert "read at 0 failed" in caplog.text and "navigation pack" in caplog.text

    def crashing(offset: int, size: int) -> bytes:
        raise ValueError("a bug")

    movie.read = crashing  # type: ignore[method-assign]
    assert errno_of(lambda: fs.read("/Blu-ray/Movie.ts", 10, 0, f)) == errno.EIO


@pytest.mark.usefixtures("context")
def test_a_read_names_the_process_that_opened_the_file(tmp_path: Path) -> None:
    # read() gets no request context: who opened the handle is remembered (#78)
    tree, lib = make_library(tmp_path, audio_files="single", sub_langs=[])
    fs = Bd3dFS(tree)
    lib.add(DiscSource(str(tmp_path / "d")))
    movie = movies(tree)[0]
    seen: list[str] = []

    def read(offset: int, size: int) -> bytes:
        seen.append(current_reader())
        return b"x"

    movie.read = read  # type: ignore[method-assign]
    f = fi()
    fs.open("/Blu-ray/Movie.ts", f)
    # read in another thread, as libfuse does: still named after the process
    t = threading.Thread(target=fs.read, args=("/Blu-ray/Movie.ts", 1, 0, f))
    t.start()
    t.join()
    with open(f"/proc/{os.getpid()}/comm") as comm:
        assert seen == [f"pid {os.getpid()} ({comm.read().strip()})"]
    assert current_reader() == "an unknown reader"


def test_the_reader_keeps_the_whole_smbd_title(tmp_path: Path) -> None:
    # smbd's name stops at 15 characters ("smbd[192.168.68"), cutting the client's IP
    title = "smbd: client [192.168.68.65]"
    smbd = tmp_path / "smbd[192.168.68.65]"
    smbd.symlink_to("/bin/cat")
    cmd = ["bash", "-c", f'exec -a "{title}" "{smbd}"']
    with subprocess.Popen(cmd, stdin=subprocess.PIPE) as p:
        try:
            expected = f"pid {p.pid} ({title})"
            got = ""
            for _ in range(500):  # until bash has exec'd cat and its argv is set
                got = fuse_fs._process(p.pid)  # pyright: ignore[reportPrivateUsage]
                if got == expected:
                    break
                time.sleep(0.01)
            assert got == expected
        finally:
            p.kill()


@pytest.mark.usefixtures("context")
def test_two_opens_of_a_file_have_their_own_handles(tmp_path: Path) -> None:
    # Samba opens a file many times: closing one must not break the others
    tree, lib = make_library(tmp_path, audio_files="single", sub_langs=[])
    fs = Bd3dFS(tree)
    source = DiscSource(str(tmp_path / "d"))
    source.sidecars = [(".srt", str(tmp_path / "x.srt"))]
    (tmp_path / "x.srt").write_text("hello")
    lib.add(source)
    a, b = fi(), fi()
    fs.open("/Blu-ray/Movie.srt", a)
    fs.open("/Blu-ray/Movie.srt", b)
    assert a.fh != b.fh
    fs.release("/Blu-ray/Movie.srt", a)
    assert fs.read("/Blu-ray/Movie.srt", 100, 0, b) == b"hello"


def test_init_sets_up_libfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    fs = Bd3dFS(Tree())
    exited: list[int | None] = []

    def fuse_exit(ptr: ctypes.c_void_p) -> None:
        exited.append(ptr.value)

    fake = SimpleNamespace(
        fuse_get_context=lambda: SimpleNamespace(contents=SimpleNamespace(fuse=1234)),
        fuse_exit=fuse_exit,
    )
    monkeypatch.setattr(fuse_fs, "_libfuse", fake)
    config = SimpleNamespace(use_ino=0, entry_timeout=1.0, attr_timeout=1.0)
    assert not fs.end_loop()                    # not mounted yet
    fs.init_with_config(None, config)
    assert fs.mounted.is_set()
    assert (config.use_ino, config.entry_timeout, config.attr_timeout) == (1, 5, 5)
    assert fs.end_loop() and exited == [1234]
    fs.destroy("/")
    assert not fs.end_loop() and exited == [1234]    # never after libfuse freed it


@pytest.mark.usefixtures("context")
def test_the_times_reach_libfuse(tmp_path: Path) -> None:
    # mfusepy reads st_mtime & co. (in ns with use_ns): other keys are dropped
    tree, lib = make_library(tmp_path, audio_files="single", sub_langs=[])
    fs = Bd3dFS(tree)
    lib.add(DiscSource(str(tmp_path / "d")))
    movie = movies(tree)[0]
    movie.mtime_ns = 1_700_000_000_123_456_789
    st = mfusepy.c_stat()
    mfusepy.set_st_attrs(st, fs.getattr("/Blu-ray/Movie.ts"), use_ns=True)  # pyright: ignore[reportUnknownMemberType]
    assert (st.st_mtimespec.tv_sec, st.st_mtimespec.tv_nsec) == (1_700_000_000, 123_456_789)
    assert st.st_size == movie.size and st.st_ino == movie.inode


@pytest.mark.usefixtures("context")
def test_an_open_file_keeps_its_own_attributes(tmp_path: Path) -> None:
    # the disc is swapped while Samba holds the old file open: fstat() on it
    # must not describe the new disc's file at the same path
    tree, lib = make_library(tmp_path, audio_files="single", sub_langs=[])
    fs = Bd3dFS(tree)
    lib.add(DiscSource(str(tmp_path / "d")))
    old = movies(tree)[0]
    f = fi()
    fs.open("/Blu-ray/Movie.ts", f)
    assert fs.getattr("/Blu-ray/Movie.ts", f)["st_ino"] == old.inode
    tree.remove(old)
    lib.add(DiscSource(str(tmp_path / "d")))
    new = movies(tree)[0]
    assert new.inode != old.inode
    assert errno_of(lambda: fs.getattr("/Blu-ray/Movie.ts", f)) == errno.ENOENT
    assert fs.getattr("/Blu-ray/Movie.ts")["st_ino"] == new.inode     # by path: the new one
    assert errno_of(lambda: fs.getattr(None)) == errno.ENOENT


def _fuse_usable() -> bool:
    # the program mounts with allow_other (smbd reads as another user), which
    # a normal user may only do with user_allow_other in /etc/fuse.conf
    try:
        conf = Path("/etc/fuse.conf").read_text()
    except OSError:
        return False
    return (shutil.which("fusermount3") is not None
            and os.access("/dev/fuse", os.R_OK | os.W_OK)
            and any(line.strip() == "user_allow_other" for line in conf.splitlines()))


MOUNT_SCRIPT = """
import os, sys, threading, time
sys.path[:0] = [{src!r}, {tests!r}]
import fuse_fs
from library import SidecarFile
from tree import ROOT_ID, Tree

class Slow(SidecarFile):
    def read(self, offset, size):
        time.sleep(0.2)                       # a disc-speed backend
        return super().read(offset, size)

tree = Tree()
for name in ("a", "b"):
    tree.add(Slow(tree.new_id(), name, {data!r}, ROOT_ID))
fuse_fs.mount(tree, {mount!r})
stop = threading.Event()
serving = threading.Thread(target=fuse_fs.serve, args=(stop,))
serving.start()
print("mounted", flush=True)
sys.stdin.readline()
stop.set()
serving.join()
fuse_fs.unmount()
print("unmounted", os.path.ismount({mount!r}), flush=True)
"""

READER = ("import sys, time; t = time.monotonic(); open(sys.argv[1], 'rb').read(); "
          "print(time.monotonic() - t)")


@pytest.mark.skipif(not _fuse_usable(), reason="needs /dev/fuse, fusermount3 and user_allow_other")
def test_a_real_mount(tmp_path: Path) -> None:
    # in a process of its own, as the program runs it; the readers are other
    # processes, as smbd is
    data = tmp_path / "data"
    data.write_bytes(b"x" * 4096)
    mount = tmp_path / "mnt"
    mount.mkdir()
    src = str(Path(fuse_fs.__file__).parent)
    code = MOUNT_SCRIPT.format(src=src, tests=str(Path(__file__).parent), data=str(data),
                               mount=str(mount))
    server = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert server.stdout is not None and server.stdin is not None
        assert server.stdout.readline().strip() == "mounted"
        assert sorted(os.listdir(mount)) == ["a", "b"]
        assert (mount / "a").stat().st_size == 4096
        # two readers at once are served at once, not one after the other
        readers = [subprocess.Popen([sys.executable, "-c", READER, str(mount / n)],
                                    stdout=subprocess.PIPE, text=True) for n in ("a", "b")]
        took = [float(r.communicate(timeout=20)[0]) for r in readers]
        assert min(took) >= 0.2
        assert max(took) < 0.2 * 2                 # one after the other would take 0.4 s
        # a file still open (Samba keeps them open) does not hold the unmount
        holder = (mount / "a").open("rb")
        try:
            out, _ = server.communicate("\n", timeout=30)
        finally:
            holder.close()
        assert out.strip().endswith("unmounted False")
        assert not os.path.ismount(mount)
    finally:
        if server.poll() is None:
            server.kill()
            subprocess.run(["fusermount3", "-u", "-z", str(mount)], check=False)
