#!/usr/bin/env bash
# Native install on Debian / Ubuntu. Run it as your normal user: it asks for
# sudo only where needed. Safe to run again (it skips what is already done).
#
# What it changes on the system (undo with scripts/uninstall.sh):
#   - apt packages: ffmpeg python3-pyfuse3 fuse3 samba libbluray libaacs libbdplus
#                   libdvdread (+ build tools for edge264)
#   - /opt/bluray3d-xr            edge264 decoder + the program
#   - /usr/local/bin/bluray3d-xr  command to start it
#   - /srv/bd3d                   mount point of the virtual files
#   - /etc/fuse.conf              enables user_allow_other (Samba must read the mount)
#   - /etc/samba/smb.conf         adds the read-only guest share [Disks]
set -euo pipefail

PREFIX=/opt/bluray3d-xr
MOUNT=/srv/bd3d
EDGE264_REPO=https://github.com/jens-duttke/edge264-mvc.git
EDGE264_COMMIT=5757e71f3decbeced1150e742e802d263fbd0df4
MARK_BEGIN="# >>> bluray3d-xr >>>"
MARK_END="# <<< bluray3d-xr <<<"
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
ME=$(id -un)

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user, not as root."; exit 1; }
command -v apt-get >/dev/null || { echo "This script supports Debian/Ubuntu (apt) only."; exit 1; }

step "1/5 Packages"
sudo apt-get update
# libbluray's package name changes with its ABI (libbluray2 up to 1.3, libbluray4 from 1.4)
libbluray=libbluray2
apt-cache show libbluray4 >/dev/null 2>&1 && libbluray=libbluray4
libdvdread=libdvdread8
apt-cache show libdvdread8t64 >/dev/null 2>&1 && libdvdread=libdvdread8t64
sudo apt-get install -y --no-install-recommends \
    ffmpeg python3 python3-pyfuse3 fuse3 samba "$libbluray" libaacs0 libbdplus0 "$libdvdread" \
    git build-essential ca-certificates

step "2/5 edge264-mvc decoder (MVC 3D)"
# built once per pinned commit: the one installed is recorded next to it
if [ "$(cat "$PREFIX/bin/edge264.commit" 2>/dev/null)" = "$EDGE264_COMMIT" ] \
        && [ -x "$PREFIX/bin/edge264_test" ]; then
    echo "already built (${EDGE264_COMMIT:0:12})"
else
    build=$(mktemp -d)
    git clone --quiet "$EDGE264_REPO" "$build/edge264"
    git -C "$build/edge264" checkout --quiet "$EDGE264_COMMIT"
    # plain system PATH: toolchains from Homebrew/conda & co. can break the link step
    env PATH=/usr/local/bin:/usr/bin:/bin make -C "$build/edge264" -j"$(nproc)" CC=/usr/bin/gcc >/dev/null
    sudo install -d "$PREFIX/bin" "$PREFIX/app"
    sudo install -m 755 "$build/edge264/edge264_test" "$PREFIX/bin/"
    sudo install -m 644 "$build/edge264/libedge264.so.1" "$PREFIX/bin/"
    echo "$EDGE264_COMMIT" | sudo tee "$PREFIX/bin/edge264.commit" >/dev/null
    rm -rf "$build"
fi
"$PREFIX/bin/edge264_test" -h >/dev/null && echo "edge264_test OK"

step "3/5 Program"
sudo install -d "$PREFIX/app"
sudo install -m 644 "$REPO_DIR"/src/*.py "$PREFIX/app/"
git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null | sudo tee "$PREFIX/app/version" >/dev/null || true
sudo tee /usr/local/bin/bluray3d-xr >/dev/null <<WRAP
#!/bin/sh
PATH="$PREFIX/bin:\$PATH" exec python3 "$PREFIX/app/bd3d_fs.py" "\$@"
WRAP
sudo chmod 755 /usr/local/bin/bluray3d-xr

step "4/5 FUSE mount point"
sudo install -d -o "$ME" -g "$(id -gn)" "$MOUNT"
if ! grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null; then
    echo user_allow_other | sudo tee -a /etc/fuse.conf >/dev/null
fi
echo "$MOUNT ready, user_allow_other enabled"

step "5/5 Samba share [Disks]"
if grep -qF "$MARK_BEGIN" /etc/samba/smb.conf; then
    echo "share already configured"
else
    sudo tee -a /etc/samba/smb.conf >/dev/null <<SHARE

$MARK_BEGIN
[Disks]
   path = $MOUNT
   comment = Blu-ray and DVD, played on the fly
   read only = yes
   guest ok = yes
   browseable = yes
   force user = $ME
$MARK_END
SHARE
fi
testparm -s >/dev/null 2>&1 || { echo "smb.conf check failed: run 'testparm' to see why"; exit 1; }
sudo systemctl restart smbd 2>/dev/null || sudo service smbd restart
echo "Samba share [Disks] active"

# Firewall: only suggest, never open ports on our own
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
    lan=$(ip -o -f inet addr show scope global | awk '{print $4; exit}')
    net=$(python3 -c "import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)" "$lan")
    printf '\nufw is active. To let devices on your LAN reach the share:\n'
    printf '    sudo ufw allow from %s to any port 445 proto tcp\n' "$net"
fi

cat <<DONE

Done. Start it with:
    bluray3d-xr --audio-lang eng --subs eng /dev/sr0       # the disc in the drive
    bluray3d-xr --audio-lang eng /path/to/your/movies      # ISO, BDMV/VIDEO_TS folders, MKV rips
Every option: OPTIONS.md
Commercial Blu-rays need a decryption key database for libaacs in ~/.config/aacs/KEYDB.cfg
(this project does not provide one), or MakeMKV installed (used via its libmmbd).
Encrypted DVDs (CSS) need libdvdcss, not installed by this script. On Debian/Ubuntu:
    sudo apt install libdvd-pkg && sudo dpkg-reconfigure libdvd-pkg   (Debian: "contrib")
Then on the glasses open the network share \\\\$(hostname -I | awk '{print $1}')\\Disks
Stop it with Ctrl+C.
DONE
