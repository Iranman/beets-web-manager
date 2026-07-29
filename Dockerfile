# syntax=docker/dockerfile:1

# ---- Frontend build stage --------------------------------------------------
FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Runtime stage ----------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Immutable image provenance. Required, not optional: a blank/missing
# VCS_REF fails the build rather than silently producing an unlabeled
# image (the beets-web-manager:0.1.0 image running in production as of
# 2026-07-28 has no OCI labels at all -- its source commit cannot be
# verified -- which this exists to prevent from happening again).
ARG VCS_REF
RUN test -n "${VCS_REF}" || (echo "ERROR: VCS_REF build-arg is required and must not be blank" >&2 && exit 1)
LABEL org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/Iranman/beets-web-manager"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tini \
    && rm -rf /var/lib/apt/lists/*

ARG PUID=1000
ARG PGID=1000
RUN groupadd -g "${PGID}" beets \
    && useradd -u "${PUID}" -g "${PGID}" -m -d /home/beets -s /usr/sbin/nologin beets

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py helpers_mb.py job_engine.py routes_jobs.py routes_lidarr.py routes_setup.py routes_submissions.py ./
COPY backend/ ./backend/
COPY tests/ ./tests/
COPY config.yaml.example .env.example VERSION ./
COPY --from=frontend /src/frontend/dist ./frontend/dist

RUN mkdir -p /web-manager-data \
    && chown -R beets:beets /app /web-manager-data

VOLUME ["/web-manager-data"]

ENV WEBCONTROL_PORT=8337 \
    BEETS_API_URL=http://beets:8338 \
    PYTHONUNBUFFERED=1

EXPOSE 8337

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WEBCONTROL_PORT\",\"8337\")}/api/health', timeout=4).read(); sys.exit(0)" || exit 1

USER beets

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]
