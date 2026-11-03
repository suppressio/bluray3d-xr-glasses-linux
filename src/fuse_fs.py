"""fuse_fs.py — the FUSE front end (--share samba/both): mounts the Tree as a
read-only file system for Samba to share, through mfusepy on libfuse3. The
only module that needs FUSE: bd3d_fs.py imports it only when FUSE is used, so
--share dlna runs without FUSE installed.

mfusepy (src/mfusepy.py) is vendored as published (version 3.1.1, ISC
license): Debian and Ubuntu do not package it, and it is one pure-Python
file. Why mfusepy and how it is set up: docs/RESEARCH.md, "FUSE adapter".
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import itertools
import logging
import os
import stat
import subprocess
import threading
import uuid
from typing import Any

_LIBFUSE3 = ctypes.util.find_library("fuse3")
if _LIBFUSE3 is None:
    raise ImportError("libfuse3 not found")
# mfusepy loads libfuse 2 when both are installed: only libfuse3 is supported
os.environ["FUSE_LIBRARY_PATH"] = _LIBFUSE3

import mfusepy  # noqa: E402  needs FUSE_LIBRARY_PATH set first
from tree import FileEntry, Folder, Tree, read_as  # noqa: E402

log = logging.getLogger("bd3d_fs")

# the loaded libfuse3 itself, for the calls mfusepy does not wrap
_libfuse: Any = mfusepy._libfuse  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

ENTRY_TIMEOUT = 5          # short: the tree changes when discs come and go
ATTR_TIMEOUT = 300
EXIT_WAIT = 10             # seconds for the FUSE loop to end on unmount


class Bd3dFS(mfusepy.Operations):
    """Serves a Tree over FUSE (for Samba): a thin adapter translating FUSE
    calls, by path, into lookups on the tree, which Library builds and discs
    change while mounted. Calls come from several libfuse threads at once."""

    use_ns = True

    def __init__(self, tree: Tree) -> None:
        self.tree = tree
        # file handle -> (entry, who opened it): read() gets no request context
        self._handles: dict[int, tuple[FileEntry, str]] = {}
        self._next_fh = itertools.count(1)
        # libfuse's struct fuse, from init() to destroy(): for invalidations
        # and for ending the loop. The lock keeps it from being freed under them.
        self._fuse: int | None = None
        self._fuse_lock = threading.Lock()
        self.mounted = threading.Event()
        tree.on_remove.append(self._invalidate)

    def init_with_config(self, conn_info: Any, config_3: Any) -> None:  # noqa: ANN401, ARG002
        with self._fuse_lock:
            self._fuse = _libfuse.fuse_get_context().contents.fuse
        if config_3 is not None:
            config_3.use_ino = 1                    # tree ids as inode numbers
            config_3.entry_timeout = ENTRY_TIMEOUT
            config_3.attr_timeout = ATTR_TIMEOUT
        self.mounted.set()

    def destroy(self, path: str) -> None:  # noqa: ARG002
        with self._fuse_lock:
            self._fuse = None

    def end_loop(self) -> bool:
        """Asks the FUSE loop to end: True if it was running."""
        with self._fuse_lock:
            if self._fuse is None:
                return False
            _libfuse.fuse_exit(ctypes.c_void_p(self._fuse))
        return True

    def _invalidate(self, e: Folder | FileEntry) -> None:
        # drop what the kernel cached about the path (its size above all), or
        # another disc's file with the same name would show the old one's. In a
        # thread: the kernel may wait on a read of that file, and the caller
        # (a drive watcher) must not wait with it.
        path = self.tree.path_of(e.parent)
        path = f"/{path}/{e.name}" if path else f"/{e.name}"

        def invalidate() -> None:
            with self._fuse_lock:
                if self._fuse is not None:
                    _libfuse.fuse_invalidate_path(ctypes.c_void_p(self._fuse), path.encode())

        threading.Thread(target=invalidate, daemon=True, name="FUSE invalidate").start()

    def _entry(self, path: str) -> Folder | FileEntry:
        e = self.tree.lookup(path)
        if e is None:
            raise mfusepy.FuseOSError(errno.ENOENT)
        return e

    def _attr(self, e: Folder | FileEntry) -> dict[str, int]:
        a = {"st_ino": e.inode, "st_uid": os.getuid(), "st_gid": os.getgid(),
             "st_blksize": 1 << 20,
             "st_atime_ns": e.mtime_ns, "st_mtime_ns": e.mtime_ns, "st_ctime_ns": e.mtime_ns}
        if isinstance(e, Folder):
            a |= {"st_mode": stat.S_IFDIR | 0o555, "st_nlink": 2, "st_size": 0}
        else:
            a |= {"st_mode": stat.S_IFREG | 0o444, "st_nlink": 1, "st_size": e.size}
        a["st_blocks"] = (a["st_size"] + 511) // 512
        return a

    def getattr(self, path: str, fh: int | None = None) -> dict[str, int]:  # noqa: ARG002
        return self._attr(self._entry(path))

    def readdir(self, path: str, fh: int) -> list[str]:  # noqa: ARG002
        e = self._entry(path)
        if not isinstance(e, Folder):
            raise mfusepy.FuseOSError(errno.ENOTDIR)
        children = sorted(self.tree.children_of(e.inode), key=lambda c: c.inode)
        return [".", "..", *(c.name for c in children)]

    # with raw_fi, mfusepy hands open() the whole fuse_file_info, not the flags
    def open(self, path: str, fi: Any) -> int:  # noqa: ANN401  # pyright: ignore[reportIncompatibleMethodOverride]
        e = self._entry(path)
        if isinstance(e, Folder):
            raise mfusepy.FuseOSError(errno.EISDIR)
        if fi.flags & (os.O_WRONLY | os.O_RDWR):
            raise mfusepy.FuseOSError(errno.EROFS)
        fh = next(self._next_fh)
        _uid, _gid, pid = mfusepy.fuse_get_context()
        self._handles[fh] = (e, _process(pid))
        fi.fh = fh
        # no page cache for the movies: the same bytes can differ between two
        # reads (the loading animation, or the movie once it is ready)
        fi.direct_io = 1 if getattr(e, "changing", False) else 0
        return 0

    def release(self, path: str, fh: Any) -> int:  # noqa: ARG002, ANN401
        self._handles.pop(fh.fh, None)
        return 0

    def statfs(self, path: str) -> dict[str, int]:  # noqa: ARG002
        # SMB clients ask for the share's free space: answer "full"
        block = 1 << 20
        return {"f_bsize": block, "f_frsize": block,
                "f_blocks": sum(f.size for f in self.tree.files()) // block + 1,
                "f_bfree": 0, "f_bavail": 0,
                "f_files": len(self.tree.nodes), "f_ffree": 0, "f_favail": 0,
                "f_namemax": 255}

    def read(self, path: str, size: int, offset: int, fh: Any) -> bytes:  # noqa: ARG002, ANN401
        handle = self._handles.get(fh.fh)
        # an unknown handle, or a file whose disc was ejected while playing
        if handle is None or self.tree.nodes.get(handle[0].inode) is not handle[0]:
            raise mfusepy.FuseOSError(errno.EIO)
        e, reader = handle
        try:
            return read_as(reader, e, offset, size)
        except OSError as err:
            # EIO is expected (no pipeline can serve the file) and was already
            # explained when it happened: no traceback for every read after it
            if err.errno != errno.EIO:
                log.warning("%s: read at %d failed: %s", e.path, offset, err)
            raise mfusepy.FuseOSError(err.errno or errno.EIO) from None
        # anything else going wrong for one read (a disc that cannot be read
        # there, a pipeline that cannot start) is an I/O error for that read:
        # it must not stop the program and every other file with it
        except Exception:
            log.exception("%s: read at %d failed", e.path, offset)
            raise mfusepy.FuseOSError(errno.EIO) from None


class _Mount:
    """The file system being served: libfuse runs its loop (and mounts and
    unmounts) in a thread of its own."""

    def __init__(self, fs: Bd3dFS, mount_point: str) -> None:
        self.fs, self.mount_point = fs, mount_point
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="FUSE", daemon=True)

    def _run(self) -> None:
        try:
            mfusepy.FUSE(self.fs, self.mount_point, raw_fi=True, foreground=True,
                         ro=True, allow_other=True, fsname="bd3d")
        except BaseException as e:  # noqa: BLE001  reported by mount() or serve()
            self.error = e


_mounted: _Mount | None = None


def mount(tree: Tree, mount_point: str) -> None:
    global _mounted  # noqa: PLW0603  one mount per program
    if os.path.ismount(mount_point):
        raise SystemExit(f"{mount_point} is already in use: is bluray3d-xr already running? "
                         f"(if not: fusermount3 -u {mount_point})")
    m = _Mount(Bd3dFS(tree), mount_point)
    m.thread.start()
    while not m.fs.mounted.wait(0.1):
        if not m.thread.is_alive():
            raise SystemExit(f"cannot mount on {mount_point}: {m.error or 'libfuse failed'}")
    _mounted = m
    log.info("mounted on %s — Ctrl+C to unmount", mount_point)


def serve(stop: threading.Event) -> None:
    """Serves until stop is set, or until the file system is unmounted from
    outside (the FUSE loop ends by itself)."""
    m = _mounted
    while m is not None and m.thread.is_alive() and not stop.wait(1):
        pass


def unmount() -> None:
    """Ends the FUSE loop; libfuse then unmounts (lazily, as Samba may still
    hold files open)."""
    m = _mounted
    if m is None or not m.thread.is_alive():
        return
    if m.fs.end_loop():
        # the loop sees the exit flag only with the next request: send one, a
        # lookup of a name that does not exist (never cached). In a thread: the
        # answer may never come, the connection just closes.
        wake = os.path.join(m.mount_point, f".bd3d-exit-{uuid.uuid4().hex}")
        threading.Thread(target=os.path.exists, args=(wake,), daemon=True).start()
    m.thread.join(EXIT_WAIT)
    if m.thread.is_alive():
        log.warning("%s: FUSE did not stop, unmounting it", m.mount_point)
        subprocess.run(["fusermount3", "-u", "-z", m.mount_point], check=False)
        m.thread.join(EXIT_WAIT)


def _process(tid: int) -> str:
    """The process reading through FUSE, for the log: with Samba an smbd child,
    one per client. FUSE gives the id of the calling thread: its process (Tgid)
    is what smbstatus shows. Name in status stops at 15 characters, which cuts
    smbd's client IP: then the process title is used ("smbd: client
    [192.168.68.65]"), capped, as for any other process it is its whole argv."""
    try:
        with open(f"/proc/{tid}/status") as f:
            fields = dict(line.split(":", 1) for line in f if ":" in line)
        pid, name = fields["Tgid"].strip(), fields["Name"].strip()
    except (OSError, KeyError):
        return f"pid {tid}"
    title = ""
    if len(name) >= 15:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read(256)
            title = " ".join(raw.decode(errors="replace").replace("\0", " ").split())[:80]
        except OSError:
            pass
    return f"pid {pid} ({title or name})"
