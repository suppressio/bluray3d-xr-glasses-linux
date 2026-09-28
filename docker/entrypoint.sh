#!/bin/sh
set -e
mkdir -p /srv/bd3d
smbd --foreground --no-process-group &
set -- --mount /srv/bd3d --encoder "${ENCODER:-auto}"
[ -n "$AUDIO_LANG" ] && set -- "$@" --audio-lang "$AUDIO_LANG"
[ -n "$AUDIO_FILES" ] && set -- "$@" --audio-files "$AUDIO_FILES"
[ "$LIGHT" = "off" ] && set -- "$@" --no-light
[ -n "$SUBS" ] && set -- "$@" --subs "$SUBS"
[ -n "$SUB_DEPTH" ] && set -- "$@" --sub-depth "$SUB_DEPTH"
# exec: bd3d_fs.py receives docker stop's SIGTERM (via tini) and unmounts cleanly
# DRIVE: a Blu-ray drive passed to the container (the movie appears when a disc is in)
exec python3 /app/bd3d_fs.py "$@" /films ${DRIVE:+"$DRIVE"}
