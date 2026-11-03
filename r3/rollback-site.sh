#!/usr/bin/env bash
# Site code/images only: schema/data rollback is not performed.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
release="${1:?exact previously verified 40-character release SHA required}"
[[ "$release" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid immutable release tag' >&2; exit 2; }
exec 8>.git/deploy.lock
flock 8
for image in personal-site-backend personal-site-web; do docker image inspect "$image:$release" >/dev/null; done
export RELEASE_TAG="$release"
docker compose config --quiet
docker compose up -d --no-build --wait --wait-timeout 120 backend web
docker compose exec -T backend python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=10)"
printf '%s\n' "$release" > .git/last-ready-release
