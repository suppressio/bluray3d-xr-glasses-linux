"""fuse_fs.py — the FUSE front end (--share samba/both): mounts the Tree as a
read-only file system for Samba to share. The only module that needs pyfuse3:
bd3d_fs.py imports it only when FUSE is used, so --share dlna runs without
FUSE installed.
"""
from __future__ import annotations  # annotations are not evaluated: pyfuse3 versions differ

import errno
import logging
import os
import stat
from typing import override

import pyfuse3
import trio

from tree import Entry, Folder, Tree

log = logging.getLogger("bd3d_fs")


class Bd3dFS(pyfuse3.Operations):
    """Serves a Tree over FUSE (for Samba): a thin adapter translating pyfuse3
    calls into lookups on the tree, which Library builds and discs change
    while mounted. Tree ids double as FUSE inodes (both start at ROOT_ID)."""

    def __init__(self, tree: Tree) -> None:
        super().__init__()
        self.tree = tree
        tree.on_remove.append(self._invalidate)

    def _invalidate(self, e: Entry) -> None:
        # drop what the kernel cached about the name, or it would linger
        pyfuse3.invalidate_entry_async(e.parent, e.name.encode(), ignore_enoent=True)

    def _attr(self, inode: int) -> pyfuse3.EntryAttributes:
        e = self.tree.nodes[inode]
        a = pyfuse3.EntryAttributes()
        a.st_ino = inode
        a.st_uid = os.getuid()
        a.st_gid = os.getgid()
        a.entry_timeout = 5          # short: the tree changes when discs come and go
        a.attr_timeout = 300
        a.st_blksize = 1 << 20
        if isinstance(e, Folder):
            a.st_mode = stat.S_IFDIR | 0o555
            a.st_nlink = 2
            a.st_size = 0
        else:
            a.st_mode = stat.S_IFREG | 0o444
            a.st_nlink = 1
            a.st_size = e.size
        a.st_blocks = (a.st_size + 511) // 512
        a.st_atime_ns = a.st_mtime_ns = a.st_ctime_ns = e.mtime_ns
        return a

    @override
    async def getattr(self, inode: int,
                      ctx: pyfuse3.RequestContext | None = None) -> pyfuse3.EntryAttributes:
        if inode not in self.tree.nodes:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attr(inode)

    @override
    async def lookup(self, parent_inode: int, name: bytes,
                     ctx: pyfuse3.RequestContext | None = None) -> pyfuse3.EntryAttributes:
        # names are bytes: one that is not valid UTF-8 cannot be in the tree
        # (str keys), so it is simply not found rather than a decode error
        e = self.tree.by_name.get((parent_inode, os.fsdecode(name)))
        if e is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attr(e.inode)

    @override
    async def opendir(self, inode: int, ctx: pyfuse3.RequestContext | None) -> int:
        if not isinstance(self.tree.nodes.get(inode), Folder):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    @override
    async def readdir(self, fh: int, start_id: int, token: pyfuse3.ReaddirToken) -> None:
        entries = sorted(self.tree.children_of(fh), key=lambda e: e.inode)
        for e in entries:
            if e.inode <= start_id:
                continue
            if not pyfuse3.readdir_reply(token, e.name.encode(), self._attr(e.inode), e.inode):
                break

    @override
    async def open(self, inode: int, flags: int,
                   ctx: pyfuse3.RequestContext | None) -> pyfuse3.FileInfo:
        e = self.tree.nodes.get(inode)
        if e is None or isinstance(e, Folder):
            raise pyfuse3.FUSEError(errno.ENOENT)
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise pyfuse3.FUSEError(errno.EROFS)
        # no page cache for the movies: the same bytes can differ between two
        # reads (the loading animation, or the movie once it is ready)
        return pyfuse3.FileInfo(fh=inode, direct_io=getattr(e, "changing", False))

    @override
    async def statfs(self, ctx: pyfuse3.RequestContext | None) -> pyfuse3.StatvfsData:
        # SMB clients ask for the share's free space: answer "full"
        s = pyfuse3.StatvfsData()
        s.f_bsize = s.f_frsize = 1 << 20
        s.f_blocks = sum(f.size for f in self.tree.files()) // s.f_frsize + 1
        s.f_bfree = s.f_bavail = 0
        s.f_files = len(self.tree.nodes)
        s.f_ffree = s.f_favail = 0
        s.f_namemax = 255
        return s

    @override
    async def read(self, fh: int, off: int, size: int) -> bytes:
        e = self.tree.nodes.get(fh)
        if e is None or isinstance(e, Folder):     # the disc was ejected while playing
            raise pyfuse3.FUSEError(errno.EIO)
        try:
            return await trio.to_thread.run_sync(e.read, off, size)
        except pyfuse3.FUSEError:
            raise
        # anything else going wrong for one read (a disc that cannot be read
        # there, a pipeline that cannot start) is an I/O error for that read:
        # it must not stop the program and every other file with it
        except Exception:
            log.exception("%s: read at %d failed", e.path, off)
            raise pyfuse3.FUSEError(errno.EIO) from None


def mount(tree: Tree, mount_point: str) -> None:
    if os.path.ismount(mount_point):
        raise SystemExit(f"{mount_point} is already in use: is bluray3d-xr already running? "
                         f"(if not: fusermount3 -u {mount_point})")
    options = set(pyfuse3.default_options)
    options |= {"fsname=bd3d", "ro", "allow_other"}
    pyfuse3.init(Bd3dFS(tree), mount_point, options)
    log.info("mounted on %s — Ctrl+C to unmount", mount_point)


async def serve() -> None:
    await pyfuse3.main()


def terminate() -> None:
    pyfuse3.terminate()


def unmount() -> None:
    pyfuse3.close(unmount=True)
