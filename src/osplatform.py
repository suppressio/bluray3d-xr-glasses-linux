"""
osplatform.py — the handful of calls that differ by operating system: whether a
disc is in a drive, which drives to look at by default, how to load a shared
library, and where temporary files and the program's log go by default. Linux
is the only platform implemented; a port adds branches here instead of
touching every module that assumes Linux today.

Named osplatform, not platform: the standard library already has a `platform`
module (imported by trio's own dependencies), and a module of ours with that
name would shadow it for anything importing from src/.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import glob
import os
import sys
from enum import Enum, auto
from pathlib import Path

LINUX = sys.platform == "linux"
MACOS = sys.platform == "darwin"


class DriveStatus(Enum):
    EMPTY = auto()        # no disc, or the tray is open
    DISC = auto()         # a disc is in and ready
    UNKNOWN = auto()      # the drive is busy, or the call failed: not an eject


_CDROM_DRIVE_STATUS = 0x5326
_CDS_NO_DISC, _CDS_TRAY_OPEN, _CDS_DISC_OK = 1, 2, 4


def drive_status(device: str) -> DriveStatus:
    """Ask the drive whether a disc is in, without reading it. A busy drive can
    answer "not ready" or fail the call: that is not an eject (UNKNOWN)."""
    if not LINUX:
        return DriveStatus.UNKNOWN
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return DriveStatus.UNKNOWN
    try:
        status = fcntl.ioctl(fd, _CDROM_DRIVE_STATUS, 0)
    except OSError:
        return DriveStatus.UNKNOWN
    finally:
        os.close(fd)
    if status == _CDS_DISC_OK:
        return DriveStatus.DISC
    if status in (_CDS_NO_DISC, _CDS_TRAY_OPEN):
        return DriveStatus.EMPTY
    return DriveStatus.UNKNOWN


def list_drives() -> list[str]:
    """Optical drives on this machine, for when none are named on the command
    line."""
    return sorted(glob.glob("/dev/sr*")) if LINUX else []


_LIBRARY_VERSIONS: dict[str, tuple[int, ...]] = {
    "bluray": (4, 2),
    "dvdread": (8, 4),
    "mmbd": (0,),
}


def _names(name: str) -> list[str]:
    """Candidate file names for a shared library on this OS, newest first."""
    versions = _LIBRARY_VERSIONS.get(name, ())
    if MACOS:
        return [f"lib{name}.{v}.dylib" for v in versions] + [f"lib{name}.dylib"]
    if sys.platform == "win32":
        return [f"{name}-{v}.dll" for v in versions] + [f"{name}.dll"]
    return [f"lib{name}.so.{v}" for v in versions]


def load_library(name: str, advice: str) -> ctypes.CDLL:
    """Load a shared library by its common name ("bluray", "dvdread"): the
    system's own lookup first, then the version suffixes Debian/Ubuntu ship."""
    for candidate in filter(None, [ctypes.util.find_library(name), *_names(name)]):
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            pass
    raise OSError(f"lib{name} not found ({advice})")


def has_library(name: str) -> bool:
    """Whether a shared library is installed, without loading it: libmmbd
    (MakeMKV) is picked as a decryption backend only when present, since it is
    optional and proprietary."""
    if ctypes.util.find_library(name):
        return True
    return LINUX and any(Path("/usr/lib", n).exists() for n in _names(name))


def default_temp_dir() -> str:
    """Where this run's temporary files (the pipeline log, audio FIFOs, IFO
    copies) go by default."""
    if MACOS:
        return os.environ.get("TMPDIR", "/tmp")  # noqa: S108  the documented default
    return "/tmp"  # noqa: S108  the documented default, not a secret


def default_program_log() -> Path:
    """Where the program's own log is saved by default (--log to change it)."""
    if MACOS:
        return Path.home() / "Library" / "Logs" / "bluray3d-xr" / "bluray3d-xr.log"
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "bluray3d-xr" / "bluray3d-xr.log"
