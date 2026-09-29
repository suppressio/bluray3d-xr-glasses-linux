#!/usr/bin/env bash
# Update a native install (scripts/install.sh) to the latest version of this
# repository: pulls it, then copies the program to /opt/bluray3d-xr. The system
# setup (packages, Samba share, FUSE) is left alone; when the pinned edge264
# version changed, or nothing is installed yet, it runs install.sh instead.
set -euo pipefail

PREFIX=/opt/bluray3d-xr
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user, not as root."; exit 1; }

old=$(cat "$PREFIX/app/version" 2>/dev/null || true)
if git -C "$REPO_DIR" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    git -C "$REPO_DIR" pull --ff-only
fi
# the release (git tag) of the checkout, e.g. v1.1.0, or v1.1.0-2-gabc1234 two commits later
new=$(git -C "$REPO_DIR" describe --tags --always)

pinned=$(sed -n 's/^EDGE264_COMMIT=//p' "$REPO_DIR/scripts/install.sh")
if [ ! -x "$PREFIX/bin/edge264_test" ] || [ "$(cat "$PREFIX/bin/edge264.commit" 2>/dev/null)" != "$pinned" ]; then
    echo "edge264 or the installation itself needs setting up: running install.sh"
    exec "$REPO_DIR/scripts/install.sh"
fi

if [ "$old" = "$new" ]; then
    echo "Already up to date ($new)."
    exit 0
fi
sudo install -m 644 "$REPO_DIR"/src/*.py "$PREFIX/app/"
echo "$new" | sudo tee "$PREFIX/app/version" >/dev/null
echo "Updated from ${old:-an unknown version} to $new:"
# git reads both tags and describe output (v1.0.0-3-gabc1234) as commits
if [ -n "$old" ] && git -C "$REPO_DIR" rev-parse --verify --quiet "$old^{commit}" >/dev/null; then
    git -C "$REPO_DIR" log --oneline "$old..HEAD" | sed 's/^/  /'
fi
if pgrep -f "$PREFIX/app/bd3d_fs.py" >/dev/null; then
    echo "bluray3d-xr is running: stop it (Ctrl+C) and start it again to use the new version."
fi
