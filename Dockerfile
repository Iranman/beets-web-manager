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

# ffmpeg: audio conversion/replaygain; chromaprint (fpcalc): AcoustID fingerprinting.
# git: needed for any pip package installed directly from a VCS URL.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libchromaprint-tools \
    git \
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
COPY beetsplug/ ./beetsplug/
COPY tests/ ./tests/
COPY config.yaml.example .env.example VERSION ./
COPY --from=frontend /src/frontend/dist ./frontend/dist

# /config: beets config + app JSON state + musiclibrary.blb
# /data/media/music: music library root
# /data/torrents: download/import staging root
RUN mkdir -p /config /data/media/music /data/torrents \
    && chown -R beets:beets /app /config /data

VOLUME ["/config", "/data/media/music", "/data/torrents"]

ENV BEETSDIR=/config \
    WEBCONTROL_PORT=8337 \
    PYTHONUNBUFFERED=1

EXPOSE 8337

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WEBCONTROL_PORT\",\"8337\")}/api/health', timeout=4).read(); sys.exit(0)" || exit 1

USER beets

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]
