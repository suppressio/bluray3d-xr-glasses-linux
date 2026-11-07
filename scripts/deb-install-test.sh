#!/usr/bin/env bash
# Installs the .deb in a clean, throwaway Debian/Ubuntu container (as root),
# checks it and removes it: run by the Debian package workflow.
#   scripts/deb-install-test.sh <package.deb> recommends      the default install
#   scripts/deb-install-test.sh <package.deb> no-recommends   DLNA only, no FUSE/Samba
# No /dev/fuse needed: FUSE is loaded, not mounted. Never run it on a real system.
set -euo pipefail

deb=$(realpath "$1")
mode=$2
app=/opt/bluray3d-xr/app

control=$(dpkg-deb -f "$deb" Depends Recommends)
echo "$control"
if grep -E 'pyfuse3|trio' <<<"$control"; then echo "former FUSE packages in $deb" >&2; exit 1; fi
grep -qxF 'Recommends: fuse3, samba' <<<"$control"

installed() { [ "$(dpkg-query -W -f '${db:Status-Status}' "$1" 2>/dev/null)" = installed ]; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
case "$mode" in
    recommends) apt-get install -y "$deb" ;;
    no-recommends) apt-get install -y --no-install-recommends "$deb" ;;
    *) echo "unknown mode: $mode" >&2; exit 2 ;;
esac

# the FUSE adapter is the vendored mfusepy: the former Python packages stay out
for package in python3-pyfuse3 python3-trio; do
    if installed "$package"; then echo "$package was installed" >&2; exit 1; fi
done
grep -q '^# Permission to use, copy, modify, and distribute this software' "$app/mfusepy.py"

bluray3d-xr --version
# the DLNA path imports no FUSE module
python3 -c "import sys; sys.path.insert(0, '$app'); import bd3d_fs"

if [ "$mode" = recommends ]; then
    testparm -s | grep -q '^\[Disks\]$'
    grep -qxF user_allow_other /etc/fuse.conf
    # loads libfuse3 through mfusepy and builds the adapter, without mounting
    python3 - "$app" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import fuse_fs
from tree import Tree
assert "fuse3" in fuse_fs._LIBFUSE3, fuse_fs._LIBFUSE3
libfuse = fuse_fs.mfusepy._libfuse
assert libfuse._name == fuse_fs._LIBFUSE3, libfuse._name
assert libfuse.fuse_version() >= 300, libfuse.fuse_version()
fuse_fs.Bd3dFS(Tree())
print("FUSE adapter loaded:", fuse_fs._LIBFUSE3, libfuse.fuse_version())
PY
else
    for package in fuse3 samba python3-pyfuse3 python3-trio; do
        if installed "$package"; then echo "$package was installed" >&2; exit 1; fi
    done
    test ! -e /etc/fuse.conf
    # without libfuse3 the FUSE module fails with ImportError, which bd3d_fs
    # turns into a clear message for --share samba/both
    python3 - "$app" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
try:
    import fuse_fs  # noqa: F401
except ImportError as e:
    print("FUSE not available, as expected:", e)
else:
    raise SystemExit("fuse_fs imported without fuse3")
PY
fi

apt-get remove -y bluray3d-xr
# "! cmd" does not trip set -e: fail explicitly
if grep -qsF '# >>> bluray3d-xr >>>' /etc/samba/smb.conf; then exit 1; fi
test ! -e /srv/bd3d
if [ "$mode" = recommends ]; then
    grep -qxF user_allow_other /etc/fuse.conf
fi
apt-get purge -y bluray3d-xr
if getent group bluray3d-xr; then exit 1; fi
echo "install test passed: $mode"
