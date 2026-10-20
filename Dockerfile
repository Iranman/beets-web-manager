# syntax=docker/dockerfile:1

# ---- Frontend build stage --------------------------------------------------
# Base images are pinned by digest (SEC-9); Dependabot (docker ecosystem)
# proposes digest bumps. The tag is kept for readability.
FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Deno (yt-dlp JS challenge runtime) -------------------------------------
# yt-dlp's YouTube signature/n challenges run in a JS runtime: yt-dlp-ejs
# (installed with yt-dlp) supplies the solver scripts, Deno >= 2.3 runs them.
# Digest-pinned like the other bases (SEC-9); Dependabot tracks this FROM.
FROM denoland/deno:bin-2.9.7@sha256:bc5aa4466e21b6d3021226a85ba2e1911f7c386254d97b9d797903ab74edace2 AS deno

# ---- Runtime stage ----------------------------------------------------------
FROM python:3.15-slim-bookworm@sha256:2a41267d9de9d358f77a1de356e0f16ecba8d39827855be0ce9ef4792edce402 AS runtime

# Immutable image provenance. Required, not optional: a blank/missing
# VCS_REF fails the build rather than silently producing an unlabeled image.
ARG VCS_REF
# CI passes the release version; a local build without it is labelled "dev".
ARG VERSION=dev
ARG BUILD_DATE
RUN test -n "${VCS_REF}" || (echo "ERROR: VCS_REF build-arg is required and must not be blank" >&2 && exit 1)
LABEL org.opencontainers.image.title="Beets Web Manager" \
      org.opencontainers.image.description="Web UI and control manager for Beets" \
      org.opencontainers.image.source="https://github.com/Iranman/beets-web-manager" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# SEC-9: `apt-get upgrade` picks up Debian security fixes newer than the
# pinned base; `git` is not installed (nothing at runtime uses it, and it
# pulls in perl, the source of most CRITICAL OS findings).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    tini \
    gosu \
    ffmpeg \
    libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# Deno for yt-dlp (see the deno stage). Its cache goes to /tmp: the hardened
# Compose runs read_only with /tmp as tmpfs and a read-only HOME. No update
# checks or prompts; YTDLP_REMOTE_COMPONENTS stays empty, so no solver code
# is fetched at runtime.
COPY --from=deno /deno /usr/local/bin/deno
ENV DENO_DIR=/tmp/deno \
    DENO_NO_UPDATE_CHECK=1 \
    DENO_NO_PROMPT=1
RUN deno --version

# Baked-in default identity. The entrypoint remaps this to the runtime
# PUID/PGID (default unchanged, 1000/1000) before dropping privileges --
# see docker/web-manager-entrypoint.sh.
RUN groupadd -g 1000 beets \
    && useradd -u 1000 -g 1000 -m -d /home/beets -s /usr/sbin/nologin beets

WORKDIR /app

COPY requirements.txt ./
# SEC-9: install dependencies, then remove pip itself. Nothing installs
# packages at runtime (backend/ytdlp_service.py blocks runtime installs), and
# pip's vendored copies of urllib3/msgpack/setuptools (pkg_resources) carry
# their own advisories that would otherwise ship in every image.
RUN pip install --no-cache-dir -r requirements.txt     && pip uninstall -y pip

COPY app.py helpers_mb.py job_engine.py routes_*.py ./
COPY backend/ ./backend/
COPY beetsplug/ ./beetsplug/
COPY config.yaml.example .env.example VERSION ./
COPY --from=frontend /src/frontend/dist ./frontend/dist
# The running build's identity, from the same build args as the OCI labels
# above; backend/build_info.py reports it via /health/live and
# /api/setup/status. json.dumps, not printf, so no value can break the JSON.
RUN VERSION="${VERSION}" VCS_REF="${VCS_REF}" BUILD_DATE="${BUILD_DATE}" python -c \
    'import json, os; print(json.dumps({"version": os.environ["VERSION"], "revision": os.environ["VCS_REF"], "created": os.environ["BUILD_DATE"]}))' \
    > /app/BUILD_INFO.json

RUN mkdir -p /web-manager-data /config /music /downloads \
    && chown -R beets:beets /app /web-manager-data /config /music /downloads

VOLUME ["/web-manager-data"]

ENV WEBCONTROL_PORT=8337 \
    BEETSDIR=/config \
    MUSIC_LIBRARY_PATH=/music \
    DOWNLOAD_PATH=/downloads \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
# PYTHONPATH: a `beet` run inside the container finds the image's own Beets
# plugins (beetsplug/webmanager, discpath) as the app does (ADR 0006).

EXPOSE 8337

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WEBCONTROL_PORT\",\"8337\")}/api/health', timeout=4).read(); sys.exit(0)" || exit 1

COPY docker/web-manager-entrypoint.sh /usr/local/bin/web-manager-entrypoint.sh
RUN chmod +x /usr/local/bin/web-manager-entrypoint.sh

# Starts as root (required to remap PUID/PGID and fix bind-mount ownership)
# then execs `gosu <PUID:PGID> tini -- <app>`: tini ends up as PID 1 with
# the app's uid, so it can forward SIGTERM to the app without CAP_KILL under
# the hardened (cap_drop: ALL) Compose settings, and still reaps zombies.
# Neither tini nor the application ever runs as root.
ENTRYPOINT ["/usr/local/bin/web-manager-entrypoint.sh"]
CMD ["python", "app.py"]
