"""bd3d_fs.py: the program around library.py — argument parsing, the program's
own log file, its version string, and the drive watcher that adds/removes a
disc's files on insertion/eject."""
import logging
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import Mock

import pytest
import trio
import trio.testing

import bd3d_fs
from osplatform import DriveStatus
from sources import Source
from test_library import DiscSource
from tree import FileEntry


def test_program_version(tmp_path: Path) -> None:
    (tmp_path / "version").write_text("v1.0.0-3-gabc1234\n")
    assert bd3d_fs.program_version(tmp_path) == "v1.0.0-3-gabc1234"
    empty = tmp_path / "not-installed"
    empty.mkdir()
    assert bd3d_fs.program_version(empty) == "unknown"          # no file, not a git clone
    # from a clone: whatever git describe says (a tag, or the commit before any tag)
    assert bd3d_fs.program_version().strip()


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


def test_watch_drive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ok, no_disc, tray, busy = (DriveStatus.DISC, DriveStatus.EMPTY, DriveStatus.EMPTY,
                                DriveStatus.UNKNOWN)
    # 1st disc: 3 failed attempts (retried every RETRY_AFTER = 3 polls), then left alone
    statuses = iter([ok, ok, busy, ok, no_disc, ok, ok, ok, ok, busy, no_disc, tray, tray,
                     ok, ok, no_disc, no_disc, ok, ok, ok, ok, ok])
    events: list[str] = []

    class NoMoreStatusError(Exception):
        pass

    def status(device: str) -> DriveStatus:
        try:
            return next(statuses)
        except StopIteration:
            raise NoMoreStatusError from None

    opened = iter([OSError("cannot decrypt")] * 3 + [DiscSource(str(tmp_path / "d")),
                                                       OSError("read error"),
                                                       DiscSource(str(tmp_path / "d"))])

    def open_disc(device: str, langs: list[str] | None, *,
                  lookup_enabled: bool = False) -> list[Source]:
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
    monkeypatch.setattr(bd3d_fs, "RETRY_AFTER", 9)

    async def main() -> None:
        with pytest.raises(NoMoreStatusError):
            await bd3d_fs.watch_drive("/dev/sr0", Lib(), None, False)

    trio.run(main, clock=trio.testing.MockClock(autojump_threshold=0))
    # 1st disc: cannot be opened, retried twice, then not again while it stays in;
    # a busy drive and a single "no disc" are not an eject; two in a row are;
    # the 2nd disc opens; the 3rd fails once (a read error) and opens on the retry
    assert events == ["remove 0", "add", "remove 0", "add"]
    assert next(opened, None) is None


@pytest.mark.parametrize("prefixed", [False, True])
def test_watch_drive_logs_the_device_once(monkeypatch: pytest.MonkeyPatch,
                                          caplog: pytest.LogCaptureFixture,
                                          prefixed: bool) -> None:
    device = "/dev/sr0"
    statuses = iter([DriveStatus.DISC])                       # disc ok, then no more polls

    class NoMoreStatusError(Exception):
        pass

    def status(dev: str) -> DriveStatus:
        try:
            return next(statuses)
        except StopIteration:
            raise NoMoreStatusError from None

    def open_disc(dev: str, langs: list[str] | None, *,
                  lookup_enabled: bool = False) -> list[Source]:
        # pathless (AACS) and path-bearing ("no title found", CSS) errors alike
        prefix = f"{dev}: " if prefixed else ""
        raise OSError(prefix + "no title found")

    class Lib:
        def add(self, source: Source) -> list[FileEntry]:
            return []

        def remove(self, entries: Sequence[FileEntry]) -> None:
            pass

    monkeypatch.setattr(bd3d_fs, "drive_status", status)
    monkeypatch.setattr(bd3d_fs, "open_disc", open_disc)

    async def main() -> None:
        with pytest.raises(NoMoreStatusError):
            await bd3d_fs.watch_drive(device, Lib(), None, False)

    with caplog.at_level(logging.INFO, logger=bd3d_fs.log.name):
        trio.run(main, clock=trio.testing.MockClock(autojump_threshold=0))
    messages = [r.getMessage() for r in caplog.records]
    assert f"{device}: no title found" in messages              # the device, exactly once
    assert f"{device}: {device}: no title found" not in messages


@pytest.mark.parametrize("value", ["0", "-1", "65536", "http"])
def test_http_port_rejects_unusable_ports(value: str) -> None:
    with pytest.raises(SystemExit):
        bd3d_fs.build_parser().parse_args(["--http-port", value, "x.mkv"])


def test_http_port_accepts_a_real_port() -> None:
    assert bd3d_fs.build_parser().parse_args(["--http-port", "8200", "x.mkv"]).http_port == 8200


def test_lookup_off_by_default() -> None:
    assert bd3d_fs.build_parser().parse_args(["x.mkv"]).lookup is False


def test_lookup_flag() -> None:
    assert bd3d_fs.build_parser().parse_args(["--lookup", "x.mkv"]).lookup is True


def test_dlna_only_does_not_need_fuse() -> None:
    # without pyfuse3 the program still imports (--share dlna runs) ...
    src = Path(bd3d_fs.__file__).parent
    code = "import sys; sys.modules['pyfuse3'] = None; import bd3d_fs; print('ok')"
    done = subprocess.run([sys.executable, "-c", code], cwd=src, capture_output=True,
                          text=True, check=False)
    assert done.stdout.strip() == "ok", done.stderr


def test_require_samba_looks_in_sbin(monkeypatch: pytest.MonkeyPatch) -> None:
    # Debian: smbd is in /usr/sbin, and a normal user's PATH has no sbin
    which = Mock(return_value="/usr/sbin/smbd")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    monkeypatch.setattr(bd3d_fs.shutil, "which", which)
    bd3d_fs.require_samba("samba")
    path = which.call_args.kwargs["path"].split(os.pathsep)
    assert path[:3] == ["/usr/local/bin", "/usr/bin", "/bin"]
    assert {"/usr/sbin", "/sbin"} <= set(path)


@pytest.mark.parametrize("share", ["samba", "both", "dlna"])
@pytest.mark.parametrize("missing", [None, "samba", "fuse"])
def test_share_dependencies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                            share: str, missing: str | None) -> None:
    fuse = Mock()
    which = Mock(return_value=None if missing == "samba" else "/usr/sbin/smbd")
    import_module = Mock(wraps=bd3d_fs.importlib.import_module)
    run = Mock(side_effect=KeyboardInterrupt)
    drive = tmp_path / "sr0"
    drive.touch()
    monkeypatch.setattr(sys, "argv", ["bd3d_fs.py", "--share", share, "--log", "none",
                                      "--loader", "none", str(drive)])
    monkeypatch.setattr(bd3d_fs.stat, "S_ISBLK", Mock(return_value=True))
    monkeypatch.setattr(bd3d_fs, "pick_encoder", Mock(return_value="x264"))
    monkeypatch.setattr(bd3d_fs.shutil, "which", which)
    monkeypatch.setattr(bd3d_fs.importlib, "import_module", import_module)
    monkeypatch.setattr(bd3d_fs.trio, "run", run)
    monkeypatch.setattr(bd3d_fs, "Dlna", Mock())
    monkeypatch.setitem(sys.modules, "fuse_fs", None if missing == "fuse" else fuse)

    if share != "dlna" and missing is not None:
        with pytest.raises(SystemExit) as error:
            bd3d_fs.main()
        message = str(error.value)
        assert f"--share {share}" in message
        assert f"scripts/install.sh --share {share}" in message
        if missing == "samba":
            assert "smbd is not installed" in message
            assert "sudo apt install samba" in message
        else:
            assert "FUSE, which is not installed" in message
            assert "install python3-pyfuse3 and fuse3" in message
        fuse.mount.assert_not_called()
        run.assert_not_called()
    else:
        bd3d_fs.main()
        run.assert_called_once()
        if share == "dlna":
            which.assert_not_called()
            import_module.assert_not_called()
            fuse.mount.assert_not_called()
        else:
            which.assert_called_once()
            assert which.call_args.args == ("smbd",)
            fuse.mount.assert_called_once()
            fuse.unmount.assert_called_once()
