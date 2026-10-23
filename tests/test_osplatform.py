"""osplatform.py: the Linux-specific calls (drive status, library loading, the
default temp dir and log path) behind a platform-independent interface."""
import ctypes
from pathlib import Path

import pytest

import osplatform
from osplatform import DriveStatus, default_program_log, default_temp_dir, drive_status


def test_drive_status_no_such_device() -> None:
    assert drive_status("/no/such/device") == DriveStatus.UNKNOWN


@pytest.mark.parametrize(("ioctl_result", "expected"), [
    (4, DriveStatus.DISC),       # CDS_DISC_OK
    (1, DriveStatus.EMPTY),      # CDS_NO_DISC
    (2, DriveStatus.EMPTY),      # CDS_TRAY_OPEN
    (3, DriveStatus.UNKNOWN),    # CDS_DRIVE_NOT_READY: busy, not an eject
    (0, DriveStatus.UNKNOWN),
])
def test_drive_status_maps_the_ioctl_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                            ioctl_result: int, expected: DriveStatus) -> None:
    def ioctl(fd: int, request: int, arg: int) -> int:
        return ioctl_result

    device = tmp_path / "sr0"
    device.touch()
    monkeypatch.setattr(osplatform.fcntl, "ioctl", ioctl)
    assert drive_status(str(device)) == expected


def test_drive_status_ioctl_failure_is_unknown(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    def fail(fd: int, request: int, arg: int) -> int:
        raise OSError("drive busy")

    device = tmp_path / "sr0"
    device.touch()
    monkeypatch.setattr(osplatform.fcntl, "ioctl", fail)
    assert drive_status(str(device)) == DriveStatus.UNKNOWN


def test_drive_status_not_linux_is_unknown(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path) -> None:
    device = tmp_path / "sr0"
    device.touch()
    monkeypatch.setattr(osplatform, "LINUX", False)
    assert drive_status(str(device)) == DriveStatus.UNKNOWN


def test_list_drives(monkeypatch: pytest.MonkeyPatch) -> None:
    def glob(pattern: str) -> list[str]:
        return ["/dev/sr1", "/dev/sr0"]

    monkeypatch.setattr(osplatform.glob, "glob", glob)
    assert osplatform.list_drives() == ["/dev/sr0", "/dev/sr1"]
    monkeypatch.setattr(osplatform, "LINUX", False)
    assert osplatform.list_drives() == []


REAL_CDLL = ctypes.CDLL          # ctypes.CDLL gets monkeypatched below: keep the real one


def test_load_library_tries_candidates_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[str] = []

    def nothing(name: str) -> str | None:
        return None

    def cdll(name: str) -> ctypes.CDLL:
        tried.append(name)
        if name != "libbluray.so.2":
            raise OSError("not found")
        return REAL_CDLL(None)

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    osplatform.load_library("bluray", "apt install libbluray2")
    assert tried == ["libbluray.so.4", "libbluray.so.2"]


def test_load_library_prefers_the_system_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[str] = []

    def found(name: str) -> str | None:
        return "libbluray.so.4.1.0"

    def cdll(name: str) -> ctypes.CDLL:
        tried.append(name)
        return REAL_CDLL(None)

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", found)
    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    osplatform.load_library("bluray", "apt install libbluray2")
    assert tried[0] == "libbluray.so.4.1.0"


def test_load_library_raises_with_the_advice_when_nothing_works(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def nothing(name: str) -> str | None:
        return None

    def cdll(name: str) -> ctypes.CDLL:
        raise OSError("not found")

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.ctypes, "CDLL", cdll)
    with pytest.raises(OSError, match=r"libbluray not found .*apt install libbluray2"):
        osplatform.load_library("bluray", "apt install libbluray2")


def test_has_library_found_by_the_system_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def found(name: str) -> str | None:
        return "libmmbd.so.0"

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", found)
    assert osplatform.has_library("mmbd") is True


def test_has_library_falls_back_to_the_known_path(monkeypatch: pytest.MonkeyPatch) -> None:
    known = "/usr/lib/libmmbd.so.0"

    def nothing(name: str) -> str | None:
        return None

    def exists(self: Path) -> bool:
        return str(self) == known

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.Path, "exists", exists)
    assert osplatform.has_library("mmbd") is True


def test_has_library_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def nothing(name: str) -> str | None:
        return None

    def missing(self: Path) -> bool:
        return False

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.Path, "exists", missing)
    assert osplatform.has_library("mmbd") is False


def test_default_temp_dir_is_slash_tmp_on_linux() -> None:
    assert default_temp_dir() == "/tmp"                       # noqa: S108


def test_default_program_log_under_xdg_state_home(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_program_log() == tmp_path / "bluray3d-xr" / "bluray3d-xr.log"


def test_default_program_log_falls_back_to_dot_local_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert default_program_log() == Path.home() / ".local" / "state" / "bluray3d-xr" \
        / "bluray3d-xr.log"
