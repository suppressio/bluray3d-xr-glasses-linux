#!/usr/bin/env bash
# Formal checks and tests, the same ones GitHub runs on every push:
#   scripts/check.sh                  ruff, pyright (strict), shellcheck, unit tests
#   scripts/check.sh --integration    also the tests on the disc in the drive
# The first run creates .venv with the pinned tools (requirements-dev.txt); the
# program's own dependencies (pyfuse3, trio) come from the system, as installed
# by scripts/install.sh or: sudo apt install python3-pyfuse3 python3-trio python3-venv
set -euo pipefail
cd "$(dirname "$0")/.."
venv=.venv

if ! cmp -s requirements-dev.txt "${venv}/requirements-dev.txt"; then
    python3 -m venv --system-site-packages "${venv}"
    "${venv}/bin/pip" install --quiet --disable-pip-version-check -r requirements-dev.txt
    cp requirements-dev.txt "${venv}/requirements-dev.txt"
fi

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
step "ruff"
"${venv}/bin/ruff" check .
step "pyright (strict)"
"${venv}/bin/pyright" --pythonpath "${venv}/bin/python"
step "shellcheck"
"${venv}/bin/shellcheck" scripts/*.sh docker/*.sh && echo "ok"
step "tests"
"${venv}/bin/python" -m pytest -q
if [[ "${1:-}" == "--integration" ]]; then
    step "integration tests (real disc / BD3D_TEST_MKV)"
    "${venv}/bin/python" -m pytest -q -m integration
fi
