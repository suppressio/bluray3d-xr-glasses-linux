#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
OUTPUT_DIR=${1:-"$REPO_DIR/dist"}
ARCH=$(dpkg --print-architecture)

[ "$ARCH" = amd64 ] || { echo "Only amd64 packages are supported." >&2; exit 1; }
for command in gcc git make dpkg-deb nproc strip; do
    command -v "$command" >/dev/null || { echo "Missing build tool: $command" >&2; exit 1; }
done

app_version=$(git -C "$REPO_DIR" describe --tags --always 2>/dev/null || echo unknown)
source "$REPO_DIR/scripts/deb-version.sh"
package_version=$(deb_package_version "$app_version")
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
package="$build/package"
edge264="$build/edge264"
edge264_repo=https://github.com/jens-duttke/edge264-mvc.git
edge264_commit=44e30b66d43a17418e0b342eba54d69d0c3be829

git clone --quiet "$edge264_repo" "$edge264"
git -C "$edge264" checkout --quiet "$edge264_commit"
make -C "$edge264" -j"$(nproc)" V=no CC=/usr/bin/gcc \
    CFLAGS=-march=x86-64 VARIANTS=x86-64-v2,x86-64-v3,logs
"$edge264/edge264_test" -h >/dev/null
strip --strip-unneeded "$edge264/edge264_test" "$edge264/libedge264.so.1"

install -d "$package/DEBIAN" \
    "$package/opt/bluray3d-xr/app" \
    "$package/opt/bluray3d-xr/loaders" \
    "$package/opt/bluray3d-xr/bin" \
    "$package/usr/bin" \
    "$package/usr/share/doc/bluray3d-xr" \
    "$package/usr/share/lintian/overrides"
install -m 644 "$REPO_DIR"/src/*.py "$package/opt/bluray3d-xr/app/"
install -m 644 "$REPO_DIR"/loaders/*.mp4 "$package/opt/bluray3d-xr/loaders/"
install -m 755 "$edge264/edge264_test" "$package/opt/bluray3d-xr/bin/"
install -m 644 "$edge264/libedge264.so.1" "$package/opt/bluray3d-xr/bin/"
install -m 644 "$edge264/LICENSE_BSD.txt" "$package/usr/share/doc/bluray3d-xr/"
install -m 644 "$REPO_DIR/LICENSE" "$package/usr/share/doc/bluray3d-xr/copyright"
printf '%s\n' "$app_version" > "$package/opt/bluray3d-xr/app/version"
printf '%s\n' "$edge264_commit" > "$package/opt/bluray3d-xr/bin/edge264.commit"
chmod 644 "$package/opt/bluray3d-xr/app/version" "$package/opt/bluray3d-xr/bin/edge264.commit"

{
    printf 'bluray3d-xr (%s) unstable; urgency=medium\n\n' "$package_version"
    printf '  * Automated release package build.\n\n'
    printf ' -- Suppressio <suppressio@users.noreply.github.com>  %s\n' "$(date -R)"
} | gzip -9n > "$package/usr/share/doc/bluray3d-xr/changelog.gz"

cat > "$package/usr/share/lintian/overrides/bluray3d-xr" <<'OVERRIDES'
bluray3d-xr: dir-or-file-in-opt
bluray3d-xr: read-in-maintainer-script
OVERRIDES
chmod 644 "$package/usr/share/doc/bluray3d-xr/changelog.gz" \
    "$package/usr/share/lintian/overrides/bluray3d-xr"

cat > "$package/usr/bin/bluray3d-xr" <<'WRAPPER'
#!/bin/sh
PATH="/opt/bluray3d-xr/bin:$PATH" exec python3 /opt/bluray3d-xr/app/bd3d_fs.py "$@"
WRAPPER
chmod 755 "$package/usr/bin/bluray3d-xr"

cat > "$package/DEBIAN/control" <<CONTROL
Package: bluray3d-xr
Version: $package_version
Section: video
Priority: optional
Architecture: amd64
Maintainer: Suppressio <suppressio@users.noreply.github.com>
Depends: libc6, python3 (>= 3.12), python3-pyfuse3, python3-trio, fuse3, ffmpeg, samba, libbluray4 | libbluray2, libaacs0, libbdplus0, libdvdread8t64 | libdvdread8
Description: Stream Blu-ray and DVD movies over the home network, including 3D
 Read discs on Linux and serve them to network players as ordinary video files.
CONTROL
install -m 755 "$REPO_DIR/scripts/deb-postinst" "$package/DEBIAN/postinst"
install -m 755 "$REPO_DIR/scripts/deb-postrm" "$package/DEBIAN/postrm"

mkdir -p "$OUTPUT_DIR"
deb="$OUTPUT_DIR/bluray3d-xr_${package_version}_amd64.deb"
dpkg-deb --build --root-owner-group -Zxz "$package" "$deb"
if command -v lintian >/dev/null; then
    lintian --fail-on error "$deb"
fi
echo "Built $deb"
