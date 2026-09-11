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

# Pinned to a named Debian release, never the floating `python:3.12-slim`. The two resolve to
# the same image today, but the floating tag follows whatever Debian release it currently
# tracks - and that float is how this image's ffmpeg went from 5.1 to 7.1.5 with no Dockerfile
# edit and nothing noticing, the day Debian 13 went stable. The Debian release is what decides
# the ffmpeg series, so moving this tag IS an ffmpeg upgrade: change it only together with
# FFMPEG_SERIES below, and only once the app has been verified against the new series.
#
# 7.1 has been through that verification (dev/changelog/913): the full suite, plus real
# captures, conversions, concatenation, probes, health checks and screenshots up to 4K HEVC
# 10-bit, measured against 6.1.1 side by side. The pin below is a verified target, not a
# placeholder for whatever the base image happened to carry.
FROM python:3.12-slim-trixie

# The ffmpeg series this image is built against, asserted after the install below. Debian
# carries exactly one ffmpeg version per release and its mirrors serve only the current build,
# so an exact `ffmpeg=7:7.1.5-0+deb13u1` apt pin would fail the build the day Debian publishes
# a security update - it would turn every CVE backport into a broken image. Asserting the
# series instead lets in-release patches (7.1.5 -> 7.1.6) through while a series or major move
# fails the build loudly rather than shipping a substituted capture engine. Also the one value
# a container smoke test reads to know which ffmpeg the image is supposed to contain.
ARG FFMPEG_SERIES=7.1

# ffmpeg: the capture/convert engine. tini: PID 1, so ffmpeg grandchildren are reaped and
# SIGTERM reaches the app (run.py's handler kills live captures before exiting). gosu: drops
# to PUID/PGID in the entrypoint. procps: pgrep/pkill, used by restart.sh's busy-guard sweep
# if an operator execs in and runs it manually - the in-app Restart button does not shell out
# to it in a container (see docker/entrypoint.sh, app/routes/settings.py::api_restart_now).
# tzdata: zoneinfo data for display.timezone.
#
# The version check covers ffprobe as well as ffmpeg because the app needs both and a missing
# ffprobe is silent at runtime - every probe returns an empty dict. An absent binary reports an
# empty version here and fails the same case arm as a wrong one.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        gosu \
        procps \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && for bin in ffmpeg ffprobe; do \
           version="$("$bin" -version 2>/dev/null | head -1 | awk '{print $3}')"; \
           echo "channelbin: $bin $version"; \
           case "$version" in \
               "${FFMPEG_SERIES}."*) ;; \
               *) echo "channelbin: expected $bin ${FFMPEG_SERIES}.x, got '$version'. The base" \
                       "image's ffmpeg moved series (or ffprobe is absent). Verify the app" \
                       "against the new build, then update the FROM tag and FFMPEG_SERIES" \
                       "together." >&2; \
                  exit 1 ;; \
           esac; \
       done

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
