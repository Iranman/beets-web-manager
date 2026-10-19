#!/usr/bin/env bash
# CI only (#234): make the stock Beets image available to a job without an
# anonymous registry pull.
#
# Which image: the pinned `beets` image in docker-compose.yml (the single
# source of truth, e.g. lscr.io/linuxserver/beets:2.14.1-ls356@sha256:...),
# unless STOCK_BEETS_IMAGE overrides it (the scheduled `:latest` canary).
#
# lscr.io is LinuxServer's redirector in front of ghcr.io/linuxserver; both
# names serve the same manifest, so the digest is identical. Anonymous pulls
# from shared GitHub runner IPs keep failing with
#   toomanyrequests: retry-after: ..., allowed: 44000/minute
# (GHCR's limit). With GHCR_TOKEN set (the job's GITHUB_TOKEN) the pull is
# authenticated and counted against that token; the login is removed again
# before the tests run. The image is then pulled under the lscr.io name the
# tests and Compose files use (layers are already local, so that pull only
# resolves the manifest). It is still the unmodified upstream image: nothing
# is built or published.
set -euo pipefail

COMPOSE="$(dirname "$0")/../docker-compose.yml"
IMAGE="${STOCK_BEETS_IMAGE:-$(sed -n 's/^    image: \(lscr\.io\/linuxserver\/beets:[^ ]*\)$/\1/p' "$COMPOSE" | head -n 1)}"
case "$IMAGE" in
  lscr.io/linuxserver/beets:*) ;;
  *) echo "::error::no stock lscr.io/linuxserver/beets image found (got '${IMAGE}')" >&2; exit 1 ;;
esac
SRC="ghcr.io/${IMAGE#lscr.io/}"
ATTEMPTS="${ATTEMPTS:-5}"

if [ -n "${GHCR_TOKEN:-}" ]; then
  trap 'docker logout ghcr.io >/dev/null 2>&1 || true; docker logout lscr.io >/dev/null 2>&1 || true' EXIT
  for host in ghcr.io lscr.io; do
    printf '%s' "$GHCR_TOKEN" | docker login "$host" -u "${GITHUB_ACTOR:-ci}" --password-stdin >/dev/null \
      || echo "::warning::$host login failed; pulling anonymously (rate-limited)" >&2
  done
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
pull "$IMAGE"
# Provenance: record exactly which image and digest this job tested against.
echo "stock Beets image: $IMAGE"
echo "stock Beets digest: $(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE")"
{
  echo "STOCK_BEETS_IMAGE=$IMAGE"
  echo "STOCK_BEETS_PREPULLED=1"
} >> "${GITHUB_ENV:-/dev/null}"
