import subprocess

import pytest

import bluray

FOUND = ("internal.c:158: [bdplus] Found cached conversion table at "
         "/home/u/.config/bdplus/convtab/839F01323FC980EF69E9EF047CDA0EAC_PREDATOR_2_F4.bin\n")
MISSING = "internal.c:160: [bdplus] No cached conversion table found\n"


@pytest.mark.parametrize(("stderr", "found"), [(FOUND, True), (MISSING, False), ("", False)])
def test_bdplus_table_found_reads_the_libbdplus_log(
        monkeypatch: pytest.MonkeyPatch, stderr: str, found: bool) -> None:
    calls: list[dict[str, object]] = []

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        assert cmd[-1] == "/dev/sr0"
        calls.append(kw)
        return subprocess.CompletedProcess(cmd, 0, "", stderr)

    monkeypatch.setattr(bluray.subprocess, "run", run)
    monkeypatch.setenv("BD_DEBUG_MASK", "0")             # would hide libbdplus' line
    assert bluray.bdplus_table_found("/dev/sr0") is found
    env = calls[0]["env"]
    assert isinstance(env, dict) and "BD_DEBUG_MASK" not in env


def test_bdplus_table_found_gives_up_on_a_stuck_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr(bluray.subprocess, "run", run)
    assert not bluray.bdplus_table_found("/dev/sr0")


@pytest.mark.parametrize("bdplus", [True, False])
def test_disc_starts_libbdplus_on_bdplus_discs(monkeypatch: pytest.MonkeyPatch,
                                               bdplus: bool) -> None:
    # libbluray loads the cached conversion table only on bd_get_titles():
    # without it BD+ clips are read unfixed (#87)
    titles: list[object] = []

    class Info:
        bdplus_detected = bdplus

    class Ptr:
        contents = Info()

    class Lib:
        def bd_open(self, path: bytes, key: None) -> int:
            return 1

        def bd_get_disc_info(self, bd: int) -> Ptr:
            return Ptr()

        def bd_get_titles(self, bd: int, flags: int, min_seconds: int) -> int:
            titles.append(bd)
            return 0

        def bd_close(self, bd: int) -> None:
            pass

    monkeypatch.setattr(bluray, "_libbluray", Lib)
    disc = bluray.Disc("/dev/sr0")
    assert titles == ([1] if bdplus else [])
    disc.close()
