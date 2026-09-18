#!/usr/bin/env bash
# Container entrypoint: seed /config on first run, match the host's uid/gid, drop root.
#
# Everything here is idempotent - it runs on every container start, and must never
# overwrite a file the user owns.
set -euo pipefail

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

log() { echo "[entrypoint] $*"; }

# --- 1. The directories the app expects to exist -----------------------------------
# /config/instance is where the generated Flask secret key and both backup dirs live;
# /app/instance is a symlink to it (see the Dockerfile).
mkdir -p /config/instance /dvr

# --- 2. First-run config seeding ---------------------------------------------------
# Only when absent. config.docker.yaml carries the handful of paths that differ inside a
# container; every key it omits falls back to the app's built-in defaults, which is why it
# is 20 lines rather than a second copy of config.example.yaml.
if [ ! -e /config/config.yaml ]; then
    cp /app/docker/config.docker.yaml /config/config.yaml
    log "seeded /config/config.yaml (first run)"
fi

# The full annotated template, refreshed every start so it tracks the image's version.
# Named .example so it is never mistaken for the live file.
cp -f /app/config.example.yaml /config/config.example.yaml

# --- 3. Match the host's uid/gid ----------------------------------------------------
# The image ships channelbin as 1000:1000; remap when the host user differs, so files
# written into the bind mounts are owned by a real account on the host side.
if [ "$PGID" != "$(id -g channelbin)" ]; then
    groupmod -o -g "$PGID" channelbin
fi
if [ "$PUID" != "$(id -u channelbin)" ]; then
    usermod -o -u "$PUID" channelbin
fi

# /config only. /dvr is deliberately never chowned: it is routinely a multi-terabyte
# share, and walking it at every container start would be both slow and presumptuous
# about files the user may share with other apps.
chown -R "$PUID:$PGID" /config /app/capture-logs

# No writability check here. Whether a folder can be written is the app's question,
# asked of the folders config.yaml actually names (which need not include /dvr at all),
# and answered where the user looks: the Readiness check, the Alert Center and the Logs
# page (app/storage_dirs.py, dev/changelog/1009). A line on stdout reaches none of them.

# Tells the app it is tini's monitored child, not a detached background service -
# app/routes/settings.py::api_restart_now reads this to know it must exit cleanly and
# rely on the container's restart policy instead of shelling out to restart.sh (which
# would pkill this process and take tini, and the whole container, down with it). gosu
# preserves the environment (unlike su), so this reaches the process below.
export CHANNELBIN_DOCKER=1

log "starting ChannelBin as uid $PUID gid $PGID"
exec gosu channelbin python3 /app/run.py
