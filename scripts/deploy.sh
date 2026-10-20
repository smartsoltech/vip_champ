#!/usr/bin/env bash
# Executed on the approved application host. Environment/secrets are external.
set -Eeuo pipefail
umask 077

if [[ $# -ne 4 ]]; then
  echo "Usage: deploy.sh ABS_DEPLOY_PATH FULL_GIT_SHA ARCHIVE ARCHIVE_SHA256" >&2
  exit 2
fi
deploy_path=$1
revision=$2
archive=$3
archive_hash=$4
[[ "$deploy_path" = /* && "$deploy_path" != / ]] || { echo "Invalid deployment path" >&2; exit 2; }
[[ "$revision" =~ ^[0-9a-f]{40}$ && "$archive_hash" =~ ^[0-9a-f]{64}$ ]] || exit 2
mkdir -p "$deploy_path"
deploy_path=$(realpath "$deploy_path")
[[ "$deploy_path" != / ]] || { echo "Deployment path resolves to root" >&2; exit 2; }
exec 9>"$deploy_path/deploy.lock"
flock -n 9 || { echo "A deployment is already running" >&2; exit 1; }
printf '%s  %s\n' "$archive_hash" "$archive" | sha256sum --check --status
[[ -f "$deploy_path/shared/.env" ]] || { echo "Missing externally managed shared/.env" >&2; exit 1; }
# The deploy user cannot traverse this 0700 directory owned by runtime UID 10001.
# Validate its presence here; the backup helper validates/creates its child as UID 10001.
[[ -d "$deploy_path/shared/db" ]] || {
  echo "Prepare shared/db with ownership 10001:10001 and mode 0700 first." >&2
  exit 1
}
env_permissions=$(stat -c '%a' "$deploy_path/shared/.env")
[[ "$env_permissions" = 600 || "$env_permissions" = 400 ]] || {
  echo "shared/.env must have mode 0600 or 0400" >&2; exit 1;
}
mkdir -p "$deploy_path/releases"
release="$deploy_path/releases/$revision-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir "$release"
if tar -tf "$archive" | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then
  echo "Unsafe archive paths" >&2
  exit 1
fi
if tar -tvf "$archive" | awk 'substr($1,1,1) == "l" || substr($1,1,1) == "h" {found=1} END {exit !found}'; then
  echo "Release archives may not contain symbolic or hard links" >&2
  exit 1
fi
tar --extract --file="$archive" --directory="$release" --no-same-owner --no-same-permissions
printf '%s\n' "$revision" > "$release/REVISION"
export ENV_FILE="$deploy_path/shared/.env"
export DB_HOST_DIR="$deploy_path/shared/db"
export APP_REVISION="$revision"
compose() { docker compose --project-name vip_champ --file "$release/docker-compose.yml" "$@"; }
health_gate() {
  checked_release=$1
  expected_revision=$2
  checked_container=$(docker compose --project-name vip_champ --file "$checked_release/docker-compose.yml" ps -q telegram-bot)
  [[ -n "$checked_container" ]] || return 1
  for attempt in $(seq 1 36); do
    state=$(docker inspect "$checked_container" --format '{{.State.Status}}')
    health=$(docker inspect "$checked_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')
    checked_revision=$(docker inspect "$checked_container" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
    if [[ "$state" = running && "$health" = healthy && "$checked_revision" = "$expected_revision" ]]; then
      return 0
    fi
    [[ "$state" != exited && "$state" != dead ]] || return 1
    sleep 5
  done
  return 1
}
previous=""
if [[ -L "$deploy_path/current" ]]; then
  previous=$(readlink -f "$deploy_path/current")
  [[ "$previous" = "$deploy_path/releases/"* ]] || { echo "Invalid current release" >&2; exit 1; }
fi
compose build --pull telegram-bot
image_revision=$(docker image inspect "vip-champ:$revision" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
[[ "$image_revision" = "$revision" ]] || { echo "Image revision mismatch" >&2; exit 1; }

# Reject missing/invalid external configuration before touching the database or service.
compose run --rm --no-deps --entrypoint python telegram-bot -c 'from config import load_settings; load_settings()'

# Back up the database before any application initialization/migrations.
# The helper passes PostgreSQL credentials through the subprocess environment.
compose run --rm --no-deps --entrypoint python telegram-bot scripts/backup_database.py
replacement_started=0
rollback() {
  status=$?
  [[ "$status" != 0 ]] || status=1
  trap - ERR INT TERM
  if [[ "$replacement_started" = 1 ]]; then
    compose stop telegram-bot || true
    if [[ -n "$previous" ]]; then
      export APP_REVISION
      APP_REVISION=$(cat "$previous/REVISION")
      if docker compose --project-name vip_champ --file "$previous/docker-compose.yml" up -d --no-build telegram-bot \
          && health_gate "$previous" "$APP_REVISION"; then
        echo "Previous application release is healthy; database backup retained. Database restoration requires review." >&2
      else
        echo "ROLLBACK FAILED its health gate. Database backup and release artifacts retained; operator action required." >&2
      fi
    else
      echo "First deployment failed; application stopped and database backup retained." >&2
    fi
  fi
  exit "$status"
}
trap rollback ERR INT TERM
replacement_started=1
compose up -d --no-build telegram-bot
health_gate "$release" "$revision" || { echo "Deployment health gate failed" >&2; false; }
next_link="$deploy_path/current.$revision.$$.next"
ln -s "$release" "$next_link"
mv -Tf "$next_link" "$deploy_path/current"
trap - ERR INT TERM
echo "Deployment healthy: revision=$revision"
