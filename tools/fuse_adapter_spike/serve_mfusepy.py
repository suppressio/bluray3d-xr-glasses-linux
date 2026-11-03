#!/usr/bin/env python3
"""#129 spike, mfusepy side: a from-scratch Operations subclass (no equivalent
exists in src/fuse_fs.py) serving the same fake VirtualFile as serve_pyfuse3.py,
logging every read() call the same way. Needs mfusepy from PyPI (not in Debian/
Ubuntu, see the spike's write-up). Run: serve_mfusepy.py <mountpoint> <read-log>
[<pipeline-log>]
"""
from __future__ import annotations

import errno
import itertools
import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

import mfusepy
from common import ReadLog, build_tree, process_title

from tree import Entry, Folder, Tree, read_as


class SpikeFS(mfusepy.Operations):
    use_ns = True

    def __init__(self, tree: Tree, read_log: ReadLog) -> None:
        self.tree = tree
        self.read_log = read_log
        # file handle -> (entry, who opened it): mfusepy's read() gets no
        # request context either, same as pyfuse3 (see fuse_fs.Bd3dFS)
        self._handles: dict[int, tuple[Entry, str]] = {}
        self._next_fh = itertools.count(1)

    def _attr(self, e: Entry) -> dict[str, int]:
        meta = {"st_uid": os.getuid(), "st_gid": os.getgid(),
               "st_atime_ns": e.mtime_ns, "st_mtime_ns": e.mtime_ns, "st_ctime_ns": e.mtime_ns}
        if isinstance(e, Folder):
            return {**meta, "st_mode": stat.S_IFDIR | 0o555, "st_nlink": 2, "st_size": 0}
        return {**meta, "st_mode": stat.S_IFREG | 0o444, "st_nlink": 1, "st_size": e.size}

    def getattr(self, path: str, fh: int | None = None) -> dict[str, int]:  # noqa: ARG002
        e = self.tree.lookup(path)
        if e is None:
            raise mfusepy.FuseOSError(errno.ENOENT)
        return self._attr(e)

    def readdir(self, path: str, fh: int) -> list[str]:  # noqa: ARG002
        e = self.tree.lookup(path)
        if not isinstance(e, Folder):
            raise mfusepy.FuseOSError(errno.ENOTDIR)
        return [".", "..", *(c.name for c in self.tree.children_of(e.inode))]

    def open(self, path: str, fi: mfusepy.fuse_file_info) -> int:
        e = self.tree.lookup(path)
        if e is None or isinstance(e, Folder):
            raise mfusepy.FuseOSError(errno.ENOENT)
        fh = next(self._next_fh)
        _, _, pid = mfusepy.fuse_get_context()
        self._handles[fh] = (e, process_title(pid))
        fi.fh = fh
        if getattr(e, "changing", False):
            fi.direct_io = 1
        return 0

    def release(self, path: str, fh: mfusepy.fuse_file_info) -> int:  # noqa: ARG002
        self._handles.pop(fh.fh, None)
        return 0

    def read(self, path: str, size: int, offset: int,  # noqa: ARG002
             fh: mfusepy.fuse_file_info) -> bytes:
        started = time.monotonic()
        entry, reader = self._handles.get(fh.fh, (None, ""))
        if entry is None:
            raise mfusepy.FuseOSError(errno.EIO)
        try:
            data = read_as(reader, entry, offset, size)
        except OSError as err:
            raise mfusepy.FuseOSError(err.errno or errno.EIO) from None
        self.read_log.record(offset=offset, size=size, reader=reader, pid=os.getpid(),
                             tid=threading.get_native_id(), started=started,
                             ended=time.monotonic())
        return data


def main() -> None:
    mount_point, read_log_path = sys.argv[1], sys.argv[2]
    default_pipeline_log = str(Path(tempfile.gettempdir()) / "fuse-spike-mfusepy-pipeline.log")
    pipeline_log = sys.argv[3] if len(sys.argv) > 3 else default_pipeline_log
    tree, _vf = build_tree(pipeline_log)
    fs = SpikeFS(tree, ReadLog(read_log_path))
    # raw_fi: direct access to fuse_file_info (fh, direct_io), as Bd3dFS needs
    mfusepy.FUSE(fs, mount_point, raw_fi=True, foreground=True, ro=True,
                fsname="bd3d-mfusepy-spike")


if __name__ == "__main__":
    main()
