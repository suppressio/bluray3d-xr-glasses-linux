"""fuse_fs.py: Bd3dFS, the thin FUSE adapter over a Tree library.py builds —
getattr/lookup/readdir/open/read/statfs, and that a read failing on one file
does not take the program down."""
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import trio

import fuse_fs
from fuse_fs import Bd3dFS
from library import SidecarFile
from test_library import DiscSource, make_library, movies
from tree import current_reader


def test_file_system_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree, lib = make_library(tmp_path, audio_files="single", sub_langs=[])
    fs = Bd3dFS(tree)
    source = DiscSource(str(tmp_path / "d"))
    source.sidecars = [(".srt", str(tmp_path / "x.srt"))]
    (tmp_path / "x.srt").write_text("hello")
    lib.add(source)
    movie = movies(tree)[0]
    sidecar = next(e for e in tree.files() if isinstance(e, SidecarFile))
    folder = tree.folder("Blu-ray")
    listed: list[bytes] = []

    def reply(token: object, name: bytes, attr: object, next_id: int) -> bool:
        listed.append(name)
        return True

    monkeypatch.setattr(fuse_fs.pyfuse3, "readdir_reply", reply)
    token = cast("fuse_fs.pyfuse3.ReaddirToken", object())

    async def check() -> None:
        attr = await fs.getattr(movie.inode)
        assert attr.st_size == movie.size and attr.st_mode & 0o777 == 0o444
        assert (await fs.lookup(folder.inode, b"Movie.ts")).st_ino == movie.inode
        assert (await fs.getattr(folder.inode)).st_mode & 0o040000
        with pytest.raises(fuse_fs.pyfuse3.FUSEError):
            await fs.lookup(folder.inode, b"missing.ts")
        with pytest.raises(fuse_fs.pyfuse3.FUSEError):                # not UTF-8: not found
            await fs.lookup(folder.inode, b"Movie\xff.ts")
        with pytest.raises(fuse_fs.pyfuse3.FUSEError):
            await fs.getattr(9999)
        assert await fs.opendir(folder.inode, None) == folder.inode
        with pytest.raises(fuse_fs.pyfuse3.FUSEError):
            await fs.opendir(movie.inode, None)
        await fs.readdir(folder.inode, 0, token)
        assert listed == [b"Movie.ts", b"Movie.srt"]
        # movies bypass the page cache (their bytes can change), subtitles do not
        assert (await fs.open(movie.inode, 0, None)).direct_io
        assert not (await fs.open(sidecar.inode, 0, None)).direct_io
        with pytest.raises(fuse_fs.pyfuse3.FUSEError):
            await fs.open(movie.inode, 1, None)                     # O_WRONLY
        fh = (await fs.open(sidecar.inode, 0, None)).fh
        assert await fs.read(fh, 0, 100) == b"hello"
        await fs.release(fh)
        with pytest.raises(fuse_fs.pyfuse3.FUSEError):               # closed handle
            await fs.read(fh, 0, 10)
        with pytest.raises(fuse_fs.pyfuse3.FUSEError):
            await fs.read(12345, 0, 10)
        s = await fs.statfs(None)
        assert s.f_bavail == 0 and s.f_blocks >= movie.size // s.f_frsize

    trio.run(check)


def test_a_failing_read_does_not_stop_the_program(tmp_path: Path,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    tree, lib = make_library(tmp_path, audio_files="single", sub_langs=[])
    fs = Bd3dFS(tree)
    lib.add(DiscSource(str(tmp_path / "d")))
    movie = movies(tree)[0]

    def broken(offset: int, size: int) -> bytes:
        raise OSError("no navigation pack at sector 35546")

    movie.read = broken  # type: ignore[method-assign]

    async def read() -> None:
        fh = (await fs.open(movie.inode, 0, None)).fh
        with pytest.raises(fuse_fs.pyfuse3.FUSEError):
            await fs.read(fh, 0, 10)

    trio.run(read)
    assert "read at 0 failed" in caplog.text and "navigation pack" in caplog.text


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
    ctx = cast("fuse_fs.pyfuse3.RequestContext", SimpleNamespace(pid=os.getpid()))

    async def check() -> None:
        fh = (await fs.open(movie.inode, 0, ctx)).fh
        await fs.read(fh, 0, 1)

    # read in a worker thread: still named after the process, not the thread
    trio.run(check)
    with open(f"/proc/{os.getpid()}/comm") as f:
        assert seen == [f"pid {os.getpid()} ({f.read().strip()})"]
    assert current_reader() == "an unknown reader"


def test_two_opens_of_a_file_have_their_own_handles(tmp_path: Path) -> None:
    # Samba opens a file many times: closing one must not break the others
    tree, lib = make_library(tmp_path, audio_files="single", sub_langs=[])
    fs = Bd3dFS(tree)
    source = DiscSource(str(tmp_path / "d"))
    source.sidecars = [(".srt", str(tmp_path / "x.srt"))]
    (tmp_path / "x.srt").write_text("hello")
    lib.add(source)
    sidecar = next(e for e in tree.files() if isinstance(e, SidecarFile))

    async def check() -> None:
        a = (await fs.open(sidecar.inode, 0, None)).fh
        b = (await fs.open(sidecar.inode, 0, None)).fh
        assert a != b
        await fs.release(a)
        assert await fs.read(b, 0, 100) == b"hello"

    trio.run(check)


@pytest.mark.parametrize("unmounted", [False, True])
def test_serve_ends(monkeypatch: pytest.MonkeyPatch, unmounted: bool) -> None:
    # serve() ends when stop is set, even before FUSE runs (a signal at start-up),
    # ending FUSE from trio's own thread; or when the file system is unmounted
    # from outside, with stop never set
    stop = threading.Event()
    terminated: list[bool] = []
    exit_loop = trio.Event()

    async def main() -> None:
        if not unmounted:
            await exit_loop.wait()

    def terminate() -> None:
        trio.lowlevel.current_trio_token()              # raises outside trio
        terminated.append(True)
        exit_loop.set()

    monkeypatch.setattr(fuse_fs.pyfuse3, "main", main)
    monkeypatch.setattr(fuse_fs.pyfuse3, "terminate", terminate)
    if not unmounted:
        stop.set()
    fuse_fs.serve(stop)
    assert terminated == ([] if unmounted else [True])
    stop.set()                                          # frees the thread left waiting on it
