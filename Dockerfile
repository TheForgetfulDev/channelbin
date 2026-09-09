# ChannelBin container image.
#
# Layout (see docker/entrypoint.sh and docker-compose.example.yml):
#   /app      the application code (this image)
#   /config   your data: config.yaml, dvr.db, instance/ (secret key + backups)
#   /dvr      your recordings, thumbnails and test screenshots
#
# The app resolves config.yaml and instance/ relative to its own directory and has no
# environment-variable seam for either, so both are symlinked into /config below rather
# than patched in code. Everything else that persists (database.path, the two backup
# dirs, dvr_output_dir) is a config value, set in docker/config.docker.yaml.

FROM python:3.12-slim

# ffmpeg: the capture/convert engine. tini: PID 1, so ffmpeg grandchildren are reaped and
# SIGTERM reaches the app (run.py's handler kills live captures before exiting). gosu: drops
# to PUID/PGID in the entrypoint. procps: pgrep/pkill, used by restart.sh's busy-guard sweep
# if an operator execs in and runs it manually - the in-app Restart button does not shell out
# to it in a container (see docker/entrypoint.sh, app/routes/settings.py::api_restart_now).
# tzdata: zoneinfo data for display.timezone.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        gosu \
        procps \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so a code change does not re-run the pip install layer.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# config.yaml and instance/ are the only two paths the app pins to its own directory
# (app/config.py::_CONFIG_PATH, app/__init__.py::_resolve_secret_key). Symlinking them into
# the volume is what makes an image upgrade keep your settings, database and session key.
# .dockerignore keeps a real config.yaml/instance out of the build context, so these never
# land on top of a copied file.
RUN ln -s /config/config.yaml /app/config.yaml \
    && ln -s /config/instance /app/instance \
    && mkdir -p /app/capture-logs /config /dvr \
    && groupadd -g 1000 channelbin \
    && useradd -u 1000 -g 1000 -d /app -s /usr/sbin/nologin channelbin \
    && chown channelbin:channelbin /app/capture-logs

VOLUME ["/config", "/dvr"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["python3", "/app/docker/healthcheck.py"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
