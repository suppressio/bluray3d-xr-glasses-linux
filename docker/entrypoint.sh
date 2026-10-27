#!/bin/sh
set -e
mkdir -p /srv/bd3d
share="${SHARE:-samba}"
# samba/both still need the smbd in the image; dlna-only needs neither it nor FUSE
case "$share" in samba|both) smbd --foreground --no-process-group & ;; esac
set -- --mount /srv/bd3d --encoder "${ENCODER:-auto}" --share "$share"
[ -n "$HTTP_PORT" ] && set -- "$@" --http-port "$HTTP_PORT"
[ -n "$AUDIO_LANG" ] && set -- "$@" --audio-lang "$AUDIO_LANG"
[ -n "$AUDIO_FILES" ] && set -- "$@" --audio-files "$AUDIO_FILES"
[ "$LIGHT" = "off" ] && set -- "$@" --no-light
[ -n "$SUBS" ] && set -- "$@" --subs "$SUBS"
[ -n "$SUB_DEPTH" ] && set -- "$@" --sub-depth "$SUB_DEPTH"
[ -n "$LOADER" ] && set -- "$@" --loader "$LOADER"
[ "$LOOKUP" = "on" ] && set -- "$@" --lookup
[ -n "$TMDB_API_KEY" ] && set -- "$@" --tmdb-key "$TMDB_API_KEY"
# exec: bd3d_fs.py receives docker stop's SIGTERM (via tini) and unmounts cleanly
# DRIVE: a Blu-ray drive passed to the container (the movie appears when a disc is in)
exec python3 /app/bd3d_fs.py "$@" /films ${DRIVE:+"$DRIVE"}
