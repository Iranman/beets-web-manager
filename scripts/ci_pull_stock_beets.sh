#!/usr/bin/env bash
# CI only (#234): make lscr.io/linuxserver/beets:latest available to a job
# without an anonymous registry pull.
#
# lscr.io is LinuxServer's redirector in front of ghcr.io/linuxserver; both
# names serve the same manifest, so the digest is identical. Anonymous pulls
# from shared GitHub runner IPs keep failing with
#   toomanyrequests: retry-after: ..., allowed: 44000/minute
# (GHCR's limit). With GHCR_TOKEN set (the job's GITHUB_TOKEN) the pull is
# authenticated and counted against that token; the login is removed again
# before the tests run. The image is then tagged under the lscr.io name the
# tests and Compose files use. It is still the unmodified upstream image:
# nothing is built or published.
set -euo pipefail

SRC="ghcr.io/linuxserver/beets:latest"
DST="lscr.io/linuxserver/beets:latest"
ATTEMPTS="${ATTEMPTS:-5}"

if [ -n "${GHCR_TOKEN:-}" ]; then
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GITHUB_ACTOR:-ci}" --password-stdin >/dev/null \
    || echo "::warning::ghcr.io login failed; pulling anonymously (rate-limited)" >&2
  trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT
fi

for i in $(seq 1 "$ATTEMPTS"); do
  if docker pull --quiet "$SRC"; then
    break
  fi
  if [ "$i" -eq "$ATTEMPTS" ]; then
    echo "::error::docker pull $SRC failed after $ATTEMPTS attempts" >&2
    exit 1
  fi
  delay=$(( i * 15 + RANDOM % 10 ))
  echo "pull attempt $i/$ATTEMPTS failed; retrying in ${delay}s" >&2
  sleep "$delay"
done

docker tag "$SRC" "$DST"
# Provenance: the compatibility lane floats on :latest, so record exactly
# which digest this job tested against.
echo "stock Beets digest: $(docker inspect --format '{{index .RepoDigests 0}}' "$SRC")"
echo "STOCK_BEETS_PREPULLED=1" >> "${GITHUB_ENV:-/dev/null}"
