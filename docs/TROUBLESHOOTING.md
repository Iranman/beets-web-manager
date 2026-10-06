# Troubleshooting Guide

This guide covers common errors and resolution steps for Beets Web Manager.

---

## Common Deployment Failures

### 1. `pull access denied for beets` or `beets-web-manager`
* **Cause**: Incorrect image tag or registry name.
* **Fix**:
  1. For standard deployments: Use the production `docker-compose.yml` with official images `lscr.io/linuxserver/beets:latest` and `ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}` (the tag defaults to `stable`).
  2. For development builds from source: Run `docker compose -f docker-compose.dev.yml up -d --build`.

---

### 2. `failed to read dockerfile`
* **Cause**: Docker Compose is attempting to execute a build directive (`build: .`) when running outside the source repository root directory.
* **Fix**: Use the production `docker-compose.yml`. Production deployments use published pre-built images and do **not** require a local `Dockerfile` or repository source code.

---

### 3. Web UI is unreachable (`http://<server-ip>:8337`)
* **Cause**: Port binding or network firewall.
* **Fix**:
  1. Confirm the container is running: `docker compose ps`.
  2. Check container logs: `docker compose logs beets-web-manager`.
  3. Ensure port `8337` is not blocked by host firewall.

---

### 4. Setting up administrator password
* **First Run**: Open `http://<server-ip>:8337` in your browser. The setup wizard lets you set your username and password directly.
* **Reset Password**: You can set `BEETS_WEB_PASSWORD` in your Compose environment variables or remove `./web-manager/.browser_password` to re-trigger the initial setup.

---

### 5. Setup says "Music library path ... is not accessible"
* **Cause**: Web Manager cannot read its library mount. It checks `MUSIC_ROOT` (default `/music`) inside the `beets-web-manager` container.
* **Fix**:
  1. Confirm the music volume is mounted into `beets-web-manager` at `/music` (read-only is fine), as in `docker-compose.yml`.
  2. If you mount it somewhere else, set `MUSIC_ROOT` to that path under the `beets-web-manager` service's `environment:`. It must match Beets' `directory:` in the `beets` container (see `docs/CONFIGURATION.md`).
  3. Check that the container's `PUID`/`PGID` can read the host directory.

---

## Operational Diagnostics

### Check Container Health
```bash
docker compose ps beets-web-manager
curl -fsS http://127.0.0.1:8337/api/health
```

### View Recent Logs
```bash
docker compose logs --tail=100 beets-web-manager
```

### Inspect Beets Setup Status
```bash
curl -fsS -H "Authorization: Bearer <your-token>" http://127.0.0.1:8337/api/setup/status
```