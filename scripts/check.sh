#!/usr/bin/env bash
# Formal checks and tests, the same ones GitHub runs on every push:
#   scripts/check.sh                  ruff, pyright (strict), shellcheck, unit tests
#   scripts/check.sh --integration    also the tests on the disc in the drive
# The first run creates .venv with the pinned tools (requirements-dev.txt); the
# program's own dependencies (pyfuse3, trio) come from the system, as installed
# by scripts/install.sh or: sudo apt install python3-pyfuse3 python3-trio python3-venv ffmpeg
set -euo pipefail
cd "$(dirname "$0")/.."
venv=.venv
# Windows (CI's windows-latest, run under Git bash): no "python3" command, and
# venv lays the tools out in Scripts/ rather than bin/
py=python3
command -v python3 >/dev/null 2>&1 || py=python

if ! cmp -s requirements-dev.txt "${venv}/requirements-dev.txt"; then
    "${py}" -m venv --system-site-packages "${venv}"
    bin="${venv}/bin"; [[ -d "${venv}/Scripts" ]] && bin="${venv}/Scripts"
    "${bin}/pip" install --quiet --disable-pip-version-check -r requirements-dev.txt
    cp requirements-dev.txt "${venv}/requirements-dev.txt"
fi
bin="${venv}/bin"; [[ -d "${venv}/Scripts" ]] && bin="${venv}/Scripts"
pybin="${bin}/python"; [[ -x "${pybin}" ]] || pybin="${pybin}.exe"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
step "ruff"
"${bin}/ruff" check .
step "pyright (strict)"
"${bin}/pyright" --pythonpath "${pybin}"
step "shellcheck"
"${bin}/shellcheck" scripts/*.sh scripts/deb-postinst scripts/deb-postrm docker/*.sh && echo "ok"
step "tests"
"${pybin}" -m pytest -q
if [[ "${1:-}" == "--integration" ]]; then
    step "integration tests (real disc / BD3D_TEST_MKV)"
    "${pybin}" -m pytest -q -m integration
fi
