"""Focused checks for the native and Docker dependency decisions."""
from pathlib import Path

ROOT = Path(__file__).parents[1]
INSTALL = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
UNINSTALL = (ROOT / "scripts/uninstall.sh").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _case_body(script: str, start: str, end: str) -> str:
    return script.split(start, 1)[1].split(end, 1)[0]


def test_native_installer_keeps_runtime_and_removes_python_fuse_packages() -> None:
    assert "packages=(ffmpeg python3 " in INSTALL
    assert "python3-trio" not in INSTALL
    assert "python3-pyfuse3" not in INSTALL
    assert 'case "$SHARE" in samba|both) packages+=(fuse3 samba) ;; esac' in INSTALL


def test_native_installer_dependency_modes() -> None:
    packages = INSTALL.split("packages=(", 1)[1].split(")", 1)[0]
    assert "fuse3" not in packages and "samba" not in packages
    optional = _case_body(INSTALL, 'case "$SHARE" in samba|both)', "esac")
    assert "fuse3 samba" in optional
    assert "python3-pyfuse3" not in optional
    assert "python3-trio" not in optional


def test_uninstaller_does_not_claim_removed_python_packages() -> None:
    assert "python3-pyfuse3" not in UNINSTALL
    assert "python3-trio" not in UNINSTALL
    assert "fuse3 samba" in UNINSTALL


def test_docker_uses_vendored_adapter_and_system_fuse() -> None:
    assert "python3-pyfuse3" not in DOCKERFILE
    assert "python3-trio" not in DOCKERFILE
    assert "ffmpeg python3 fuse3 samba tini" in DOCKERFILE
    assert "COPY src/ /app/" in DOCKERFILE
    assert "COPY --from=version /version /app/version" in DOCKERFILE
