# Troubleshooting Guide

This guide covers common errors and resolution steps for Beets Web Manager.

---

## Common Deployment Failures

### 1. `pull access denied for beets-engine`
* **Cause**: Docker encountered a local-only Beets engine image name (`beets-engine:local` or `beets-engine:dev`) without a local build context (e.g., running outside the repository root or copying service blocks into another directory).
* **Fix**: Do **NOT** run `docker login` (this is not an authentication issue).
  1. For standard web-manager deployments: Use the production `docker-compose.yml` (which has no `beets` engine build) and point `BEETS_API_URL` to an existing Beets control agent endpoint.
  2. For bundled engine deployments: Run `docker compose -f docker-compose.full.yml up -d --build` or `docker compose -f docker-compose.dev.yml up -d --build` directly from the repository root where `Dockerfile.beets` and build contexts are located.

---

### 2. `pull access denied for beets-web-manager`
* **Cause**: Compose is attempting to pull a local-only image name (e.g. `beets-web-manager:0.1.17`) from Docker Hub instead of using the published GitHub Container Registry image.
* **Fix**: Ensure your `docker-compose.yml` references the official published registry image:
  ```yaml
  image: ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}
  ```
  If you intended to build locally from source, use the development Compose file instead:
  ```bash
  docker compose -f docker-compose.dev.yml up -d --build
  ```

---

### 2. `failed to read dockerfile`
* **Cause**: Docker Compose is attempting to execute a build directive (`build: .`) when running outside the source repository root directory (e.g. when copying Compose service blocks into an existing TrueNAS stack).
* **Fix**: Use the production `docker-compose.yml` or Compose fragment from [docs/EXAMPLES.md](EXAMPLES.md). Production deployments use published pre-built images and do **not** require a local `Dockerfile` or repository source code.

---

### 3. `cannot connect to Beets API`
* **Cause**: `BEETS_API_URL`, Docker network routing, agent service availability, or `BEETS_API_TOKEN` is incorrect.
* **Fix**:
  1. Confirm the Beets control agent container is running and healthy.
  2. Verify network connectivity from `beets-web-manager` to `BEETS_API_URL`.
  3. Ensure `BEETS_API_TOKEN` in `.env` matches the token configured in the Beets control agent.
  4. If Beets runs in a separate Compose stack or host, update `BEETS_API_URL` to point to the host IP (e.g. `http://192.168.1.50:8338`).

---

### 4. `authentication is required` (HTTP 503 / 401)
* **Cause**: Web manager authentication configuration is incomplete or secrets failed to resolve at startup.
* **Fix**:
  1. Set `BEETS_WEB_AUTH_TOKEN` in `.env` for API clients.
  2. Set `BEETS_WEB_USERNAME` and `BEETS_WEB_PASSWORD` (32+ chars with upper, lower, number, special) for browser Basic Auth.
  3. On fresh installs, read the auto-generated API token from where it was persisted (never printed to logs): `docker exec <container> cat /web-manager-data/.auth_token`.
  4. Ensure `/web-manager-data` volume is writable by container user (UID 1000) — if it isn't, startup now fails closed with a clear error rather than running with an unrecoverable in-memory-only token.

---

### 5. `Import path is outside approved roots` or `Import path not found`
* **Cause**: The import source path is outside configured intake directories (`BEETS_IMPORT_SOURCE_ROOTS`), is a root directory itself, contains relative traversal segments (`..`), or does not exist on the Beets engine filesystem.
* **Fix**:
  1. Confirm the directory exists inside the `beets` engine container: `docker compose exec beets ls -la <path>`.
  2. Make sure you provide the container-internal path where your downloads/media are mounted in the `beets` container (e.g. `/data/torrents/music` or `/data/downloads`), not the host filesystem path.
  3. If your media is mounted at a custom directory in the `beets` container, add it to `BEETS_IMPORT_SOURCE_ROOTS` in `.env` (e.g. `BEETS_IMPORT_SOURCE_ROOTS=/srv/intake,/data/downloads`).
  4. Use the **Approved Intake Roots** dropdown in the Import UI to pick an approved root directory.

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