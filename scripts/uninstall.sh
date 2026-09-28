#!/usr/bin/env bash
# Undo scripts/install.sh. apt packages and the user_allow_other line in
# /etc/fuse.conf are left in place (other software may use them).
set -euo pipefail

MARK_BEGIN="# >>> bluray3d-xr >>>"
MARK_END="# <<< bluray3d-xr <<<"

if mountpoint -q /srv/bd3d; then
    fusermount3 -u /srv/bd3d || sudo umount /srv/bd3d
fi
if grep -qF "$MARK_BEGIN" /etc/samba/smb.conf; then
    sudo sed -i "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" /etc/samba/smb.conf
    sudo systemctl restart smbd 2>/dev/null || sudo service smbd restart
    echo "Samba share [3D] removed"
fi
sudo rm -rf /opt/bluray3d-xr /usr/local/bin/bluray3d-xr
sudo rmdir /srv/bd3d 2>/dev/null || true
echo "Removed. Packages left installed: ffmpeg python3-pyfuse3 fuse3 samba libbluray libaacs0 libbdplus0"
echo "(remove them with apt if nothing else needs them)"
