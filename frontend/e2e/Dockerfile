# Run the E2E suite anywhere Docker runs, on the Linux the app targets (the app
# treats config paths as POSIX, so a native Windows host cannot run it fully).
# Same Python and Node as CI. Test-only; never published. See frontend/e2e/README.md.
FROM python:3.13-slim
# Same audio tools as the production image: imports refuse to run without ffprobe.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:24-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY requirements.txt requirements-dev.txt /tmp/req/
RUN pip install --no-cache-dir -r /tmp/req/requirements-dev.txt
COPY frontend/package.json frontend/package-lock.json /tmp/pw/
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PYTHONDONTWRITEBYTECODE=1
RUN cd /tmp/pw && npm ci --no-audit --no-fund && npx playwright install --with-deps chromium
WORKDIR /src/frontend
CMD ["sh", "-c", "npm ci --no-audit --no-fund && npm run build && npx playwright test"]
