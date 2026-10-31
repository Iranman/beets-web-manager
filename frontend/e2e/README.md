# End-to-end tests

Playwright drives the real app (Flask, embedded Beets, built frontend) against a synthetic library rebuilt in a temp directory on every start. Non-loopback connections are refused, and the run fails if the app tried one. MusicBrainz is answered with canned data at the provider boundary; every other provider looks unreachable.

Linux only: the app treats config paths as POSIX paths.

Anywhere Docker runs, from the repository root:

```sh
docker build -f frontend/e2e/Dockerfile -t bwm-e2e .
docker run --rm -v "$PWD:/src" -v bwm-e2e-nm:/src/frontend/node_modules bwm-e2e
```

On Linux with Python 3.13, Node 24, `ffmpeg` and `requirements-dev.txt` installed, from `frontend/`:

```sh
npm ci && npx playwright install --with-deps chromium
npm run build && npm run e2e
```

Set `E2E_PYTHON` if `python` is not the right interpreter. CI runs the same suite in `.github/workflows/e2e.yml`.

- `harness/serve.py`: boots the app with isolated paths and network stubs. `--no-frontend` serves it as if the build were missing.
- `harness/build_fixture.py`: builds the synthetic library and downloads folders.
- `specs/`: one file per feature. Specs share one server per run and execute in file order, so the Clean All flow runs before anything is imported. `11-known-defects.spec.ts` holds expected failures that pin open product bugs.
