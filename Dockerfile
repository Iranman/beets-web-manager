# syntax=docker/dockerfile:1

# ---- Frontend build stage --------------------------------------------------
# Base images are pinned by digest (SEC-9); Dependabot (docker ecosystem)
# proposes digest bumps. The tag is kept for readability.
FROM node:25-bookworm-slim@sha256:81db02c4b671288a03915da9534dbd54f96d0e7c24d80ccc54f5b36b2e684370 AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Runtime stage ----------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS runtime

# Immutable image provenance. Required, not optional: a blank/missing
# VCS_REF fails the build rather than silently producing an unlabeled image.
ARG VCS_REF
ARG VERSION=0.1.18
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

RUN mkdir -p /web-manager-data /config /music /downloads \
    && chown -R beets:beets /app /web-manager-data /config /music /downloads

VOLUME ["/web-manager-data"]

ENV WEBCONTROL_PORT=8337 \
    BEETSDIR=/config \
    MUSIC_LIBRARY_PATH=/music \
    DOWNLOAD_PATH=/downloads \
    PYTHONUNBUFFERED=1

EXPOSE 8337

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WEBCONTROL_PORT\",\"8337\")}/api/health', timeout=4).read(); sys.exit(0)" || exit 1

COPY docker/web-manager-entrypoint.sh /usr/local/bin/web-manager-entrypoint.sh
RUN chmod +x /usr/local/bin/web-manager-entrypoint.sh

# Starts as root (required to remap PUID/PGID and fix bind-mount ownership
# below) then execs the application as the unprivileged `beets` user via
# gosu -- the application process itself never runs as root.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/web-manager-entrypoint.sh"]
CMD ["python", "app.py"]
