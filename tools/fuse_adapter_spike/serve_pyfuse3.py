#!/usr/bin/env python3
"""#129 spike, pyfuse3 side: mounts the fake VirtualFile of common.py with the
real src/fuse_fs.py, unmodified, and logs every read() call to the path in
argv[2] (see common.ReadLog). Run: serve_pyfuse3.py <mountpoint> <read-log>
[<pipeline-log>]
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pyfuse3
import trio
from common import ReadLog, build_tree

from fuse_fs import Bd3dFS
from tree import current_reader

log = logging.getLogger("fuse_spike")
logging.basicConfig(level=logging.INFO, format="%(message)s")


class LoggingBd3dFS(Bd3dFS):
    def __init__(self, tree: object, read_log: ReadLog) -> None:
        super().__init__(tree)  # type: ignore[arg-type]
        self._read_log = read_log

    async def read(self, fh: int, off: int, size: int) -> bytes:
        started = time.monotonic()
        _, reader = self._handles.get(fh, (0, ""))
        data = await super().read(fh, off, size)
        self._read_log.record(offset=off, size=size, reader=reader or current_reader(),
                              pid=os.getpid(), tid=threading.get_native_id(),
                              started=started, ended=time.monotonic())
        return data


def main() -> None:
    mount_point, read_log_path = sys.argv[1], sys.argv[2]
    default_pipeline_log = str(Path(tempfile.gettempdir()) / "fuse-spike-pyfuse3-pipeline.log")
    pipeline_log = sys.argv[3] if len(sys.argv) > 3 else default_pipeline_log
    tree, _vf = build_tree(pipeline_log)
    fs = LoggingBd3dFS(tree, ReadLog(read_log_path))
    options = set(pyfuse3.default_options)
    options |= {"fsname=bd3d-spike", "ro"}
    pyfuse3.init(fs, mount_point, options)
    log.info("pyfuse3 mounted on %s", mount_point)
    stop = threading.Event()

    async def run() -> None:
        async with trio.open_nursery() as nursery:
            async def fuse() -> None:
                await pyfuse3.main()
                nursery.cancel_scope.cancel()
            nursery.start_soon(fuse)
            await trio.to_thread.run_sync(stop.wait, abandon_on_cancel=True)
            pyfuse3.terminate()

    try:
        trio.run(run)
    finally:
        pyfuse3.close()


if __name__ == "__main__":
    main()
