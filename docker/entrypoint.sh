#!/bin/sh
set -e
mkdir -p /srv/bd3d
smbd --foreground --no-process-group &
set -- --mount /srv/bd3d --encoder "${ENCODER:-auto}"
[ -n "$AUDIO_LANG" ] && set -- "$@" --audio-lang "$AUDIO_LANG"
# exec: bd3d_fs.py receives docker stop's SIGTERM (via tini) and unmounts cleanly
exec python3 /app/bd3d_fs.py "$@" /films
