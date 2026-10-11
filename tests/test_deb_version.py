import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deb-version.sh"


@pytest.mark.parametrize(("app_version", "expected"), [
    ("v1.2.0", "1.2.0"),
    ("v1.2.0-rc.1", "1.2.0~rc.1"),
    ("v1.2.0-3-gabc1234", "1.2.0-3-gabc1234"),
    ("20811e4", "0~20811e4"),
    ("unknown", "0~unknown"),
    ("release", "0~release"),
])
def test_deb_package_version(app_version: str, expected: str) -> None:
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; deb_package_version "$2"', "_", str(SCRIPT), app_version],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == expected


def test_digit_starting_hash_version_sorts_before_release() -> None:
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; deb_package_version "$2"', "_", str(SCRIPT), "20811e4"],
        check=True,
        capture_output=True,
        text=True,
    )

    subprocess.run(
        ["dpkg", "--compare-versions", result.stdout.strip(), "lt", "1.2.0"],
        check=True,
    )


def test_prerelease_version_sorts_before_release() -> None:
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; deb_package_version "$2"', "_", str(SCRIPT), "v1.2.0-rc.1"],
        check=True,
        capture_output=True,
        text=True,
    )

    subprocess.run(
        ["dpkg", "--compare-versions", result.stdout.strip(), "lt", "1.2.0"],
        check=True,
    )
