"""Shared setup for the #129 spike: a Tree with one big VirtualFile, served by
a fake pipeline (the same stand-in test_library.py uses), fast enough that the
front end (pyfuse3 or mfusepy) is always the bottleneck. Both serve scripts and
the driver import this module; none of it is product code (see tools/fuse_adapter_spike.md).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT / "src", REPO_ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import library  # noqa: E402
from library import VirtualFile  # noqa: E402
from pipeline import Quality  # noqa: E402
from test_library import FakeSource, fake_decode, fake_filler  # noqa: E402
from tree import ROOT_ID, Tree  # noqa: E402

# a movie far longer than any real one, so a benchmark run never hits the
# synthetic tail: only throughput in the middle of the file is measured
DURATION = 24 * 3600.0
QUALITY = Quality("spike", 1, 500 * library.TS_PACKET * 8)


def build_tree(log_path: str) -> tuple[Tree, VirtualFile]:
    """A Tree holding one VirtualFile, fed by the fake pipeline of
    tests/test_library.py instead of a real disc and ffmpeg."""
    library.decode_command = fake_decode
    VirtualFile.filler_command = fake_filler  # type: ignore[method-assign]
    tree = Tree()
    source = FakeSource(str(REPO_ROOT), duration=DURATION)
    vf = VirtualFile(tree.new_id(), source, "x264", QUALITY, log_path, parent=ROOT_ID)
    tree.add(vf)
    return tree, vf


class ReadLog:
    """Every front end's read() call, appended as one JSON line: offset, size,
    who asked (reader, pid, tid) and when it started/ended. The driver reads
    this back to compute throughput, overlap (concurrency) and the largest
    size the kernel ever asked for in one call (max_read)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def record(self, *, offset: int, size: int, reader: str, pid: int, tid: int,
               started: float, ended: float) -> None:
        line = json.dumps({"offset": offset, "size": size, "reader": reader, "pid": pid,
                           "tid": tid, "started": started, "ended": ended})
        with Path(self.path).open("a") as f:
            f.write(line + "\n")


def process_title(pid: int) -> str:
    """Same lookup Bd3dFS._process does, for the "who opened it" check."""
    try:
        with Path(f"/proc/{pid}/status").open() as f:
            fields = dict(line.split(":", 1) for line in f if ":" in line)
        tgid, name = fields["Tgid"].strip(), fields["Name"].strip()
    except (OSError, KeyError):
        return f"pid {pid}"
    return f"pid {tgid} ({name})"


def wait_for_mount(mount_point: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.ismount(mount_point):
            return
        time.sleep(0.05)
    raise TimeoutError(f"{mount_point} never became a mount point")
