# End-to-end tests

Playwright drives the real app (Flask, embedded Beets, built frontend) against a synthetic library rebuilt in a temp directory on every start. MusicBrainz is answered with canned data; every other provider looks unreachable.

**Never run this outside a throwaway container or CI runner against a machine with real data.** Importing the app moves legacy state files out of a hardcoded `/config`, whatever the settings say, and the harness then deletes its temp root on the next start. `harness/serve.py` therefore refuses to start when:

- the host is not Linux or another POSIX system (on Windows, `/config` is a real folder on the current drive);
- `/config` exists, unless this is the image below with nothing mounted there;
- the network namespace has any route other than loopback.

## Run it

Anywhere Docker runs, from the repository root. Only the first two commands use the network:

```sh
docker build -f frontend/e2e/Dockerfile -t bwm-e2e .
docker run --rm -v "$PWD:/src" -v bwm-e2e-nm:/src/frontend/node_modules bwm-e2e sh -c "npm ci && npm run build"
docker run --rm --network none -v "$PWD:/src" -v bwm-e2e-nm:/src/frontend/node_modules bwm-e2e
```

On a Linux host with no `/config`, with Python 3.13, Node 24, `ffmpeg` and `requirements-dev.txt` installed, from `frontend/`:

```sh
npm ci && npx playwright install --with-deps chromium && npm run build
sudo unshare -n sh -c "ip link set lo up && exec sudo -u $USER env PATH=$PATH HOME=$HOME CI=$CI npx playwright test"
```

Set `E2E_PYTHON` if `python` is not the right interpreter. CI runs the same suite in `.github/workflows/e2e.yml`.

## What stops network access, and what is not covered

- **Enforced:** the empty network namespace. Nothing in it can reach anything but loopback, in any language.
- **Fails the run:** an audit hook, installed before the app is imported, logs and refuses `connect`, `connect_ex`, `sendto`, `getaddrinfo` and `gethostbyname` for non-loopback hosts, and any child process other than `ffmpeg`, `ffprobe`, `fpcalc`, `ld`, `ldconfig` or a bare `<program> --version`. Teardown fails the suite if the log exists.

Not covered:

- What an allowed child process does on the network, and native code or `ctypes` calling sockets directly. The namespace stops these leaving, but nothing fails the run. Kernel no-route counters cannot detect them because Chromium increments them on its own, even with no page open to anything but loopback.
- The browser's own traffic. It cannot leave the namespace either, and a page asking for an external URL does not fail the run.
- Loopback traffic is not inspected. In the namespace, only the suite is listening.
- Files under the hardcoded `/config` paths are written to the throwaway container's filesystem, or fail with a permission error on an unprivileged CI runner. Specs do not check those writes.

## Layout

- `harness/serve.py`: boots the app with isolated paths and network stubs. `--no-frontend` serves it as if the build were missing.
- `harness/build_fixture.py`: builds the synthetic library and downloads folders.
- `specs/`: one file per feature. Specs share one server per run and execute in file order, so the Clean All flow runs before anything is imported. `11-known-defects.spec.ts` holds expected failures that pin open product bugs.
