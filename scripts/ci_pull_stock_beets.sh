#!/usr/bin/env bash
# CI only (#234): make the stock Beets image available to a job without an
# anonymous registry pull.
#
# Which image: the pinned `beets` image in docker-compose.yml (the single
# source of truth, e.g. lscr.io/linuxserver/beets:2.14.1-ls356@sha256:...).
# STOCK_BEETS_PIN_OVERRIDE may name another lscr.io/linuxserver/beets tag
# only together with STOCK_BEETS_ALLOW_FLOATING=1, which only the scheduled
# `:latest` canary sets; anywhere else a mismatch with the pin fails. Only
# this script (through $GITHUB_ENV) sets STOCK_BEETS_IMAGE for later steps.
#
# lscr.io is LinuxServer's redirector in front of ghcr.io/linuxserver; both
# names serve the same manifest, so the digest is identical. Anonymous pulls
# from shared GitHub runner IPs keep failing with
#   toomanyrequests: retry-after: ..., allowed: 44000/minute
# (GHCR's limit). With GHCR_TOKEN set (the job's GITHUB_TOKEN) the pull is
# authenticated and counted against that token; the login is removed again
# before the tests run. Consumers then use the ghcr.io tag@digest reference
# exported as STOCK_BEETS_IMAGE (the tests, and the production acceptance's
# Compose override), so there is exactly one registry pull and no lscr.io
# request at all. A re-tag cannot help: the classic image store does not
# resolve `lscr.io/...:tag@sha256:...` from a re-tag of another repository.
# It is still the unmodified upstream image: nothing is built or published.
#
# Never `docker login lscr.io`: lscr.io is a third-party proxy, not GitHub,
# and GHCR's bearer token for a credentialed login is the base64 of the
# credential itself, so every request to lscr.io would carry GITHUB_TOKEN.
set -euo pipefail

COMPOSE="$(dirname "$0")/../docker-compose.yml"
PIN="$(sed -n 's/^    image: \(lscr\.io\/linuxserver\/beets:[^ ]*\)$/\1/p' "$COMPOSE" | head -n 1)"
IMAGE="${STOCK_BEETS_PIN_OVERRIDE:-$PIN}"
case "$IMAGE" in
  lscr.io/linuxserver/beets:*) ;;
  *) echo "::error::no stock lscr.io/linuxserver/beets image found (got '${IMAGE}')" >&2; exit 1 ;;
esac
if [ "$IMAGE" != "$PIN" ] && [ "${STOCK_BEETS_ALLOW_FLOATING:-}" != "1" ]; then
  echo "::error::'${IMAGE}' is not the docker-compose.yml pin '${PIN}' (set STOCK_BEETS_ALLOW_FLOATING=1 only in the canary)" >&2
  exit 1
fi
SRC="ghcr.io/${IMAGE#lscr.io/}"
ATTEMPTS="${ATTEMPTS:-5}"

# Throwaway Docker client config: the ghcr.io login never touches the
# runner's own ~/.docker, and is deleted with it on exit.
DOCKER_CONFIG="$(mktemp -d)"
export DOCKER_CONFIG
trap 'docker logout ghcr.io >/dev/null 2>&1 || true; rm -rf "$DOCKER_CONFIG"' EXIT
if [ -n "${GHCR_TOKEN:-}" ]; then
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GITHUB_ACTOR:-ci}" --password-stdin >/dev/null \
    || echo "::warning::ghcr.io login failed; pulling anonymously (rate-limited)" >&2
fi

pull() {
  for i in $(seq 1 "$ATTEMPTS"); do
    if docker pull --quiet "$1"; then
      return 0
    fi
    if [ "$i" -eq "$ATTEMPTS" ]; then
      echo "::error::docker pull $1 failed after $ATTEMPTS attempts" >&2
      exit 1
    fi
    delay=$(( i * 15 + RANDOM % 10 ))
    echo "pull attempt $i/$ATTEMPTS failed; retrying in ${delay}s" >&2
    sleep "$delay"
  done
}

pull "$SRC"
# Provenance: record exactly which image and digest this job tested against.
echo "stock Beets image: $SRC (pin: $IMAGE)"
echo "stock Beets digest: $(docker inspect --format '{{index .RepoDigests 0}}' "$SRC")"
{
  echo "STOCK_BEETS_IMAGE=$SRC"
  echo "STOCK_BEETS_PREPULLED=1"
} >> "${GITHUB_ENV:-/dev/null}"
