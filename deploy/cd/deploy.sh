#!/usr/bin/env bash
# Deploy one immutable image to the Dograh VM, verify it, roll back if it is
# unhealthy. Runs ON the VM; CD copies it over and invokes it via ssh.
#
#   deploy.sh <git-sha> <image-repo> [--force]
#   deploy.sh rollback
#
# Only the api service is ever touched. postgres, redis, minio, coturn, nginx
# and ui are left running: the database, Redis and the recordings in MinIO all
# live in volumes attached to those containers, and this is a phone system, so
# restarting them costs live calls for no reason.
#
# Deliberately never used here: `down` (stops the whole stack), `-v` (deletes
# the volumes holding the database and recordings), `image prune` (would
# discard the image rollback needs), and any write to .env, certs/,
# config/coturn/ or /etc/asterisk.

set -euo pipefail

DEPLOY_DIR=/opt/dograh/dograh
STATE_DIR=/opt/dograh/deploy-state
BACKUP_DIR=/opt/dograh/backups
HEALTH_URL=http://localhost:8000/api/v1/health
# Compose files are listed explicitly, which also stops docker-compose.override
# .yaml being auto-loaded -- it pins the locally-built image with
# pull_policy:never and a build: section, both wrong for deploying a registry
# image.
BASE_FILE="$DEPLOY_DIR/docker-compose.yaml"
DEPLOY_FILE="$DEPLOY_DIR/docker-compose.deploy.yaml"

say()  { printf '\n=== %s ===\n' "$1"; }
fail() { printf '\n!!! %s\n' "$1" >&2; exit 1; }

compose() { docker compose -f "$BASE_FILE" -f "$DEPLOY_FILE" "$@"; }

# ── health ────────────────────────────────────────────────────────────────
# Healthy means the api answers 200 *and* reports the commit we deployed.
# Without the second half a rollback that silently failed would still look
# green, because the old container answers 200 perfectly well.
wait_healthy() {
    local want="$1" deadline=$((SECONDS + 180)) got=""
    while (( SECONDS < deadline )); do
        if curl -fsS --max-time 3 "$HEALTH_URL" -o /tmp/health.json 2>/dev/null; then
            got=$(python3 -c 'import json,sys;print(json.load(open("/tmp/health.json")).get("build_sha",""))' 2>/dev/null || echo "")
            [[ "$got" == "$want" ]] && { echo "  healthy, build_sha=$got"; return 0; }
        fi
        sleep 3
    done
    echo "  unhealthy after 180s (last build_sha='${got:-none}', wanted '$want')"
    return 1
}

write_deploy_file() {
    local ref="$1"
    cat > "$DEPLOY_FILE" <<YAML
# Written by deploy/cd/deploy.sh -- do not edit by hand.
# Pins api to one immutable image so production always maps to a known commit.
services:
  api:
    image: ${ref}
    pull_policy: always
    restart: unless-stopped
    environment:
      # Carried over from docker-compose.override.yaml, which is not loaded
      # when compose files are passed explicitly.
      ARQ_WORKERS: "\${ARQ_WORKERS:-1}"
YAML
}

current_image() {
    docker inspect dograh-api-1 --format '{{.Config.Image}}' 2>/dev/null || echo ""
}

# ── rollback ──────────────────────────────────────────────────────────────
do_rollback() {
    local prev
    prev=$(cat "$STATE_DIR/previous_image" 2>/dev/null || echo "")
    [[ -n "$prev" ]] || fail "no previous image recorded; cannot roll back automatically"
    say "rolling back to $prev"
    write_deploy_file "$prev"
    compose up -d --no-deps api
    local prev_sha
    prev_sha=$(docker run --rm --entrypoint printenv "$prev" GIT_SHA 2>/dev/null || echo "unknown")
    if wait_healthy "$prev_sha"; then
        echo "  rollback healthy on $prev"
        return 0
    fi
    fail "ROLLBACK ALSO UNHEALTHY -- manual intervention required. Stack left running on $prev."
}

if [[ "${1:-}" == "rollback" ]]; then
    do_rollback
    exit 0
fi

SHA="${1:?usage: deploy.sh <git-sha> <image-repo> [--force]}"
REPO="${2:?usage: deploy.sh <git-sha> <image-repo> [--force]}"
FORCE="${3:-}"
NEW_REF="${REPO}:${SHA}"

mkdir -p "$STATE_DIR" "$BACKUP_DIR"
[[ -d "$DEPLOY_DIR" ]] || fail "deploy dir $DEPLOY_DIR missing"
[[ -f "$DEPLOY_DIR/.env" ]] || fail ".env missing -- refusing to deploy into an unconfigured host"

# ── 1. do not hang up on anybody ──────────────────────────────────────────
# Recreating the api container tears down its websocket to Asterisk, which
# drops whatever calls are in progress. Asterisk is the authoritative source
# and needs no credentials; /health/active-calls would need
# DOGRAH_DEVOPS_SECRET, which this host does not set.
say "1. live call check"
ACTIVE=$(asterisk -rx 'core show channels' 2>/dev/null | awk '/active calls/{print $1}' || echo 0)
echo "  active calls: ${ACTIVE:-unknown}"
if [[ "${ACTIVE:-0}" != "0" && "$FORCE" != "--force" ]]; then
    fail "$ACTIVE call(s) in progress. Re-run with --force to deploy anyway (it will drop them)."
fi

# ── 2. record what we are replacing ───────────────────────────────────────
say "2. record rollback point"
CURRENT=$(current_image)
[[ -n "$CURRENT" ]] || fail "api container not running; refusing to deploy blind"
echo "  current image: $CURRENT"
if [[ "$CURRENT" == "$NEW_REF" ]]; then
    echo "  already running $NEW_REF -- nothing to do"
    exit 0
fi
echo "$CURRENT" > "$STATE_DIR/previous_image"
date -Is > "$STATE_DIR/previous_recorded_at"

# ── 3. database safety net ────────────────────────────────────────────────
# The image entrypoint runs `alembic upgrade head` on start. Migrations are
# forward-only, so a dump taken here is what makes a bad one recoverable.
say "3. database backup"
TS=$(date +%Y%m%d-%H%M%S)
docker exec "$(docker ps -qf name=postgres)" pg_dumpall -U postgres \
    | gzip > "$BACKUP_DIR/pre-deploy-$TS.sql.gz"
echo "  $BACKUP_DIR/pre-deploy-$TS.sql.gz ($(du -h "$BACKUP_DIR/pre-deploy-$TS.sql.gz" | cut -f1))"

# ── 4. pull before touching anything running ──────────────────────────────
# A pull failure here leaves the current container untouched.
say "4. pull $NEW_REF"
docker pull "$NEW_REF"
PULLED_SHA=$(docker run --rm --entrypoint printenv "$NEW_REF" GIT_SHA 2>/dev/null || echo "")
[[ "$PULLED_SHA" == "$SHA" ]] || fail "image reports GIT_SHA='$PULLED_SHA', expected '$SHA'"
echo "  image confirms GIT_SHA=$PULLED_SHA"

# ── 5. swap only the api service ──────────────────────────────────────────
say "5. recreate api"
write_deploy_file "$NEW_REF"
compose up -d --no-deps api

# ── 6. verify, or put it back ─────────────────────────────────────────────
say "6. health check"
if ! wait_healthy "$SHA"; then
    echo "  deploy FAILED health check -- rolling back"
    do_rollback
    fail "deployed $SHA was unhealthy; rolled back to $(cat "$STATE_DIR/previous_image")"
fi

# ── 7. post-deploy sanity ─────────────────────────────────────────────────
say "7. post-deploy checks"
compose ps --format '  {{.Service}} {{.Status}}' 2>/dev/null | head -8
echo "  SIP registrations: $(asterisk -rx 'pjsip show registrations' 2>/dev/null | grep -c Registered || echo 0)"
echo "  alembic: $(docker exec "$(docker ps -qf name=postgres)" psql -U postgres -tAc 'select version_num from alembic_version;' 2>/dev/null | tr -d ' ')"

echo "$NEW_REF" > "$STATE_DIR/current_image"
echo "$SHA"     > "$STATE_DIR/current_sha"

say "deployed $SHA"
echo "  rollback with: $0 rollback"
