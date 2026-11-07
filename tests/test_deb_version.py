import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deb-version.sh"
# the ordering checks ask Debian's own dpkg: absent on macOS and Windows
needs_dpkg = pytest.mark.skipif(shutil.which("dpkg") is None, reason="needs dpkg")


@pytest.mark.parametrize(("app_version", "expected"), [
    ("v1.2.0", "1.2.0"),
    ("v1.2.0-rc.1", "1.2.0~rc.1"),
    ("v1.2.0-3-gabc1234", "1.2.0+3.gabc1234"),
    ("v1.2.0-rc.1-3-gabc1234", "1.2.0~rc.1+3.gabc1234"),
    ("v1.2.0-beta2", "1.2.0~beta2"),
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


@needs_dpkg
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


@needs_dpkg
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


def _version(app_version: str) -> str:
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; deb_package_version "$2"', "_", str(SCRIPT), app_version],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@needs_dpkg
@pytest.mark.parametrize(("lower", "higher"), [
    ("v1.2.0", "v1.2.0-3-gabc1234"),
    ("v1.2.0-3-gabc1234", "v1.2.1"),
    ("v1.2.0-3-gabc1234", "v1.2.0-12-gdef5678"),
    ("v1.2.0-rc.1", "v1.2.0-rc.1-3-gabc1234"),
    ("v1.2.0-rc.1-3-gabc1234", "v1.2.0"),
])
def test_dev_build_sorts_between_its_tag_and_the_next(lower: str, higher: str) -> None:
    subprocess.run(
        ["dpkg", "--compare-versions", _version(lower), "lt", _version(higher)],
        check=True,
    )


@pytest.mark.parametrize("app_version", ["v1.2.0-3-gabc1234", "v1.2.0-rc.1-3-gabc1234"])
def test_no_hyphen_left(app_version: str) -> None:
    # a hyphen makes Debian read the rest as a Debian revision (lintian error, #82)
    assert "-" not in _version(app_version)
