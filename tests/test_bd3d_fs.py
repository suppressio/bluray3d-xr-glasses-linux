"""bd3d_fs.py: the program around library.py — argument parsing, the program's
own log file, its version string, and the drive watcher that adds/removes a
disc's files on insertion/eject."""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Self, cast
from unittest.mock import Mock

import pytest

import bd3d_fs
import sources
from fakes import FakeBluray, opener
from library import Library
from osplatform import DriveStatus
from sources import Source, UnsupportedDiscError
from test_library import DiscSource
from tree import FileEntry, Tree


class FakeStop:
    """A stand-in for the threading.Event watch_drive polls: wait() advances a
    fake clock instead of really sleeping, and never reports stopped."""

    def __init__(self) -> None:
        self.now = 0.0

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float) -> bool:
        self.now += timeout
        return False


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
        assert lookup_enabled                    # --lookup reaches every open (#84)
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

    fake_stop = FakeStop()
    monkeypatch.setattr(bd3d_fs.time, "monotonic", lambda: fake_stop.now)
    with pytest.raises(NoMoreStatusError):
        bd3d_fs.watch_drive("/dev/sr0", Lib(), None, True, cast("threading.Event", fake_stop))
    # 1st disc: cannot be opened, retried twice, then not again while it stays in;
    # a busy drive and a single "no disc" are not an eject; two in a row are;
    # the 2nd disc opens; the 3rd fails once (a read error) and opens on the retry
    assert events == ["remove 0", "add", "remove 0", "add"]
    assert next(opened, None) is None


def test_watch_drive_does_not_retry_a_definitive_rejection(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        caplog: pytest.LogCaptureFixture) -> None:
    # a BD+ disc this program cannot decrypt (#89): one attempt, no retry, even
    # though RETRY_AFTER is already passed at the very next poll; eject/reinsert
    # starts over like any other disc
    ok, no_disc = DriveStatus.DISC, DriveStatus.EMPTY
    statuses = iter([ok, ok, ok, no_disc, no_disc, ok])

    class NoMoreStatusError(Exception):
        pass

    def status(device: str) -> DriveStatus:
        try:
            return next(statuses)
        except StopIteration:
            raise NoMoreStatusError from None

    opened = iter([UnsupportedDiscError("BD+ disc: not decrypted"),
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

    events: list[str] = []
    monkeypatch.setattr(bd3d_fs, "drive_status", status)
    monkeypatch.setattr(bd3d_fs, "open_disc", open_disc)
    monkeypatch.setattr(bd3d_fs, "RETRY_AFTER", 1)   # would be retried almost at once if allowed

    fake_stop = FakeStop()
    monkeypatch.setattr(bd3d_fs.time, "monotonic", lambda: fake_stop.now)
    with caplog.at_level(logging.INFO, logger=bd3d_fs.log.name), \
            pytest.raises(NoMoreStatusError):
        bd3d_fs.watch_drive("/dev/sr0", Lib(), None, False, cast("threading.Event", fake_stop))
    assert events == ["remove 0", "add"]
    assert next(opened, None) is None            # open_disc called exactly twice: no retry
    messages = [r.getMessage() for r in caplog.records]
    assert any("giving up" in m for m in messages)
    assert not any("attempt 2" in m for m in messages)


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

    fake_stop = FakeStop()
    monkeypatch.setattr(bd3d_fs.time, "monotonic", lambda: fake_stop.now)
    with caplog.at_level(logging.INFO, logger=bd3d_fs.log.name), pytest.raises(NoMoreStatusError):
        bd3d_fs.watch_drive(device, Lib(), None, False, cast("threading.Event", fake_stop))
    messages = [r.getMessage() for r in caplog.records]
    assert f"{device}: no title found" in messages              # the device, exactly once
    assert f"{device}: {device}: no title found" not in messages


@pytest.fixture
def signal_handlers() -> Iterator[None]:
    """run() installs its own SIGINT/SIGTERM handlers: put pytest's back after."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    yield
    for s, handler in saved.items():
        signal.signal(s, handler)


@pytest.mark.usefixtures("signal_handlers")
def test_run_fails_when_a_watcher_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    # a dead watcher stops the program, instead of leaving it running deaf to its drive
    def broken(*args: object) -> None:
        raise OSError("drive gone")

    monkeypatch.setattr(bd3d_fs, "watch_drive", broken)
    with pytest.raises(OSError, match="drive gone"):
        bd3d_fs.run(Tree(), cast("Library", Mock()), ["/dev/sr0"], None, use_fuse=False,
                    lookup_enabled=False)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="os.kill(SIGTERM) terminates the process on Windows, no handler runs"
                           " (SPN#10)")
@pytest.mark.usefixtures("signal_handlers")
@pytest.mark.parametrize("busy_in", ["open_disc", "add"])
def test_stop_while_a_disc_is_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                     busy_in: str) -> None:
    # SIGTERM while a disc is loaded. Still being read: run() returns without
    # waiting for it (bounded by WORKERS_JOIN), and it is not added to the
    # library main() is tearing down. Already being added: run() waits for it,
    # so main()'s cleanup sees its files.
    busy, release, cleaning = threading.Event(), threading.Event(), threading.Event()
    added: list[Source] = []

    class SpyLock:
        """library_changing, telling when run() (the main thread) asks for it."""

        def __init__(self) -> None:
            self.lock = threading.Lock()

        def __enter__(self) -> None:
            if threading.current_thread() is threading.main_thread():
                cleaning.set()
            self.lock.acquire()

        def __exit__(self, *exc: object) -> None:
            self.lock.release()

    def open_disc(device: str, langs: list[str] | None, *,
                  lookup_enabled: bool = False) -> list[Source]:
        if busy_in == "open_disc":
            busy.set()
            assert release.wait(5)
        return [DiscSource(str(tmp_path / "d"))]

    class Lib:
        def add(self, source: Source) -> list[FileEntry]:
            if busy_in == "add":
                busy.set()
                assert release.wait(5)
            added.append(source)
            return []

    def terminate() -> None:
        assert busy.wait(5)
        os.kill(os.getpid(), signal.SIGTERM)
        if busy_in == "add":
            cleaning.wait(5)                         # run() is past WORKERS_JOIN's reach
            release.set()

    monkeypatch.setattr(bd3d_fs, "drive_status", Mock(return_value=DriveStatus.DISC))
    monkeypatch.setattr(bd3d_fs, "open_disc", open_disc)
    monkeypatch.setattr(bd3d_fs, "WORKERS_JOIN", 0.1)
    monkeypatch.setattr(bd3d_fs, "library_changing", SpyLock())
    helper = threading.Thread(target=terminate)
    helper.start()
    try:
        bd3d_fs.run(Tree(), cast("Library", Lib()), ["/dev/sr0"], None, use_fuse=False,
                    lookup_enabled=False)
        done_before_return = list(added)
        watcher = [t for t in threading.enumerate() if t.name == "/dev/sr0"]
    finally:
        release.set()
        helper.join(5)
    if busy_in == "add":
        assert len(done_before_return) == 1          # run() waited for it
        assert not watcher
    else:
        assert len(watcher) == 1                     # still reading: not waited for
        watcher[0].join(5)
        assert not watcher[0].is_alive()
        assert added == []


@pytest.mark.parametrize("value", ["0", "-1", "65536", "http"])
def test_http_port_rejects_unusable_ports(value: str) -> None:
    with pytest.raises(SystemExit):
        bd3d_fs.build_parser().parse_args(["--http-port", value, "x.mkv"])


def test_http_port_accepts_a_real_port() -> None:
    assert bd3d_fs.build_parser().parse_args(["--http-port", "8200", "x.mkv"]).http_port == 8200


def test_http_port_default_avoids_the_minidlna_port() -> None:
    # 8200 is minidlna's own default; sharing it with a common home-server
    # package would make --share dlna/both fail to start out of the box
    assert bd3d_fs.build_parser().parse_args(["x.mkv"]).http_port != 8200


@pytest.mark.parametrize(("port", "hint_expected"), [(8200, True), (8201, False)])
def test_dlna_start_failure_hints_at_minidlna_on_port_8200(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, port: int, hint_expected: bool) -> None:
    drive = tmp_path / "sr0"
    drive.touch()
    monkeypatch.setattr(sys, "argv", ["bd3d_fs.py", "--share", "dlna", "--log", "none",
                                      "--loader", "none", "--http-port", str(port), str(drive)])
    monkeypatch.setattr(bd3d_fs.stat, "S_ISBLK", Mock(return_value=True))
    monkeypatch.setattr(bd3d_fs, "pick_encoder", Mock(return_value="x264"))
    monkeypatch.setattr(bd3d_fs, "Dlna", Mock(side_effect=OSError("Address already in use")))

    with pytest.raises(SystemExit) as error:
        bd3d_fs.main()
    message = str(error.value)
    assert f"cannot start on port {port}" in message
    assert ("minidlna" in message) == hint_expected


def test_lookup_off_by_default() -> None:
    assert bd3d_fs.build_parser().parse_args(["x.mkv"]).lookup is False


def test_lookup_flag() -> None:
    assert bd3d_fs.build_parser().parse_args(["--lookup", "x.mkv"]).lookup is True


def test_dlna_only_does_not_need_fuse() -> None:
    # without FUSE the program still imports (--share dlna runs) ...
    src = Path(bd3d_fs.__file__).parent
    code = "import sys; sys.modules['mfusepy'] = None; import bd3d_fs; print('ok')"
    done = subprocess.run([sys.executable, "-c", code], cwd=src, capture_output=True,
                          text=True, check=False)
    assert done.stdout.strip() == "ok", done.stderr


def test_require_samba_looks_in_sbin(monkeypatch: pytest.MonkeyPatch) -> None:
    # Debian: smbd is in /usr/sbin, and a normal user's PATH has no sbin
    which = Mock(return_value="/usr/sbin/smbd")
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/local/bin", "/usr/bin", "/bin"]))
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
    monkeypatch.setattr(bd3d_fs, "run", run)
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
            assert "install fuse3, or use --share dlna" in message
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


def test_no_sources_watches_every_drive_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # no positional argument at all: list_drives() stands in for a drive named on
    # the command line, and every drive it finds is watched (#90)
    run = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(sys, "argv", ["bd3d_fs.py", "--share", "dlna", "--log", "none",
                                      "--loader", "none"])
    monkeypatch.setattr(bd3d_fs, "list_drives", Mock(return_value=["/dev/sr0", "/dev/sr1"]))
    monkeypatch.setattr(bd3d_fs, "pick_encoder", Mock(return_value="x264"))
    monkeypatch.setattr(bd3d_fs, "run", run)
    monkeypatch.setattr(bd3d_fs, "Dlna", Mock())
    bd3d_fs.main()
    run.assert_called_once()
    assert run.call_args.args[2] == ["/dev/sr0", "/dev/sr1"]


def test_no_sources_and_no_drive_found_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # same "no source found" error as before #90, now also reached with no
    # positional argument when the machine has no optical drive either
    monkeypatch.setattr(sys, "argv", ["bd3d_fs.py", "--log", "none", "--loader", "none"])
    monkeypatch.setattr(bd3d_fs, "list_drives", Mock(return_value=[]))
    monkeypatch.setattr(bd3d_fs, "pick_encoder", Mock(return_value="x264"))
    with pytest.raises(SystemExit, match="no source found"):
        bd3d_fs.main()


def nothing_may_start(monkeypatch: pytest.MonkeyPatch) -> list[Mock]:
    """Every piece main() starts for a share, failing the test if called."""
    started: list[Mock] = []
    for name in ("pick_encoder", "Library", "Dlna", "run", "require_fuse", "discover"):
        started.append(Mock(side_effect=AssertionError(name)))
        monkeypatch.setattr(bd3d_fs, name, started[-1])
    monkeypatch.setitem(sys.modules, "fuse_fs", None)
    return started


def test_dump_structure_writes_and_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder, out = tmp_path / "Tron", tmp_path / "tron.json"
    folder.mkdir()
    disc = FakeBluray()
    monkeypatch.setattr(sources, "_open_disc", opener(disc))
    started = nothing_may_start(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["bd3d_fs.py", "--log", "none", "--dump-structure", str(out),
                                      str(folder)])
    bd3d_fs.main()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert (data["format"], data["kind"], data["volume_id"]) == (1, "bluray", "TRON_LEGACY_3D")
    assert disc.closed
    for mock in started:
        mock.assert_not_called()


def test_dump_structure_takes_one_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # not the first and a silent drop of the others: the user named two discs
    out = tmp_path / "out.json"
    nothing_may_start(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["bd3d_fs.py", "--log", "none", "--dump-structure", str(out),
                                      str(tmp_path), "/dev/sr0"])
    with pytest.raises(SystemExit, match="takes one disc, drive, ISO or folder, not 2"):
        bd3d_fs.main()
    assert not out.exists()


@pytest.mark.parametrize("error", [None, "no title found"])
def test_dump_structure_unreadable_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                          error: str | None) -> None:
    folder, out = tmp_path / "Tron", tmp_path / "out.json"
    if error is not None:
        folder.mkdir()

    def dump_structure(path: str) -> str:
        raise OSError(f"{path}: {error}")       # as open_disc's errors: the path first

    monkeypatch.setattr(bd3d_fs, "dump_structure", dump_structure)
    nothing_may_start(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["bd3d_fs.py", "--log", "none", "--dump-structure", str(out),
                                      str(folder)])
    with pytest.raises(SystemExit) as exc:
        bd3d_fs.main()
    message = str(exc.value)
    assert message == (f"not found: {folder}" if error is None else f"{folder}: {error}")
    assert not out.exists()


def test_dump_structure_unwritable_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "missing" / "out.json"
    monkeypatch.setattr(bd3d_fs, "dump_structure", Mock(return_value="{}\n"))
    nothing_may_start(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["bd3d_fs.py", "--log", "none", "--dump-structure", str(out),
                                      str(tmp_path)])
    with pytest.raises(SystemExit, match="--dump-structure: cannot write"):
        bd3d_fs.main()


def link_to(kind: str, target: Path, link: Path) -> None:
    """`link` as the same file as `target`: itself, a symlink or a hard link."""
    try:
        if kind == "symlink":
            link.symlink_to(target)
        elif kind == "hardlink":
            link.hardlink_to(target)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"no {kind} here: {e}")


@pytest.mark.parametrize("kind", ["same", "symlink", "hardlink"])
def test_dump_structure_never_overwrites_the_source(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path, kind: str) -> None:
    iso = tmp_path / "Tron.iso"
    iso.write_bytes(b"ISO BYTES")
    out = iso if kind == "same" else tmp_path / "out.json"
    link_to(kind, iso, out)
    dump = Mock(return_value="{}\n")
    monkeypatch.setattr(bd3d_fs, "dump_structure", dump)
    with pytest.raises(SystemExit, match="exists already, give the name of a new file"):
        bd3d_fs.write_structure([str(iso)], str(out))
    dump.assert_not_called()                    # refused before the disc is read
    assert iso.read_bytes() == b"ISO BYTES"


def test_dump_structure_keeps_an_existing_file(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    out.write_text("an older dump")
    monkeypatch.setattr(bd3d_fs, "dump_structure", Mock(return_value="{}\n"))
    with pytest.raises(SystemExit, match="exists already"):
        bd3d_fs.write_structure([str(tmp_path)], str(out))
    assert out.read_text() == "an older dump"


def test_dump_structure_keeps_a_file_created_while_reading(monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path) -> None:
    # it appeared after the first check, while the disc was read: still not ours
    out = tmp_path / "out.json"

    def dump_structure(path: str) -> str:
        out.write_text("someone else's")
        return "{}\n"

    monkeypatch.setattr(bd3d_fs, "dump_structure", dump_structure)
    with pytest.raises(SystemExit, match="exists already"):
        bd3d_fs.write_structure([str(tmp_path)], str(out))
    assert out.read_text() == "someone else's"


def test_dump_structure_removes_its_half_written_file(monkeypatch: pytest.MonkeyPatch,
                                                     tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    real_open = open

    class DiskFull:
        def __init__(self, path: str, mode: str, encoding: str) -> None:
            self.f = real_open(path, mode, encoding=encoding)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            self.f.close()

        def write(self, text: str) -> int:
            self.f.write(text[:len(text) // 2])
            self.f.flush()
            assert out.stat().st_size > 0        # half of it is on disk
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(bd3d_fs, "open", DiskFull, raising=False)
    monkeypatch.setattr(bd3d_fs, "dump_structure", Mock(return_value='{"format": 1}\n'))
    with pytest.raises(SystemExit, match=r"cannot write .*No space left on device"):
        bd3d_fs.write_structure([str(tmp_path)], str(out))
    assert not out.exists()
