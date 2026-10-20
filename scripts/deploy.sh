#!/usr/bin/env bash
# Load the CI artifact and promote configuration only after verified readiness.
set +x
set -Eeuo pipefail
umask 077

if [[ $# -ne 7 ]]; then
  echo "Usage: deploy.sh DEPLOY_PATH REVISION SOURCE_ARCHIVE SOURCE_SHA256 IMAGE_ARCHIVE IMAGE_SHA256 IMAGE_ID" >&2
  exit 2
fi
deploy_path=$1
revision=$2
archive=$3
archive_hash=$4
image_archive=$5
image_hash=$6
image_id=$7
[[ "$deploy_path" = /* && "$deploy_path" != / ]] || { echo "Invalid deployment path" >&2; exit 2; }
[[ "$revision" =~ ^[0-9a-f]{40}$ && "$archive_hash" =~ ^[0-9a-f]{64}$ ]] || exit 2
[[ "$image_hash" =~ ^[0-9a-f]{64}$ && "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || exit 2
mkdir -p "$deploy_path"
deploy_path=$(realpath "$deploy_path")
[[ "$deploy_path" != / ]] || { echo "Deployment path resolves to root" >&2; exit 2; }
exec 9>"$deploy_path/deploy.lock"
flock -n 9 || { echo "A deployment is already running" >&2; exit 1; }
printf '%s  %s\n' "$archive_hash" "$archive" | sha256sum --check --status
printf '%s  %s\n' "$image_hash" "$image_archive" | sha256sum --check --status
shared_env="$deploy_path/shared/.env"
[[ -f "$shared_env" && ! -L "$shared_env" ]] || { echo "Missing protected shared/.env regular file" >&2; exit 1; }
# UID 10001 owns this 0700 directory; the deploy user cannot traverse it.
[[ -d "$deploy_path/shared/db" ]] || { echo "Prepare shared/db for UID 10001 with mode 0700 first." >&2; exit 1; }
env_permissions=$(stat -c '%a' "$shared_env")
[[ "$env_permissions" = 600 || "$env_permissions" = 400 ]] || {
  echo "shared/.env must have mode 0600 or 0400" >&2; exit 1;
}
mkdir -p "$deploy_path/releases"
release=$(mktemp -d "$deploy_path/releases/$revision-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
if tar -tf "$archive" | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then
  echo "Unsafe archive paths" >&2; exit 1
fi
if tar -tvf "$archive" | awk 'substr($1,1,1) == "l" || substr($1,1,1) == "h" {found=1} END {exit !found}'; then
  echo "Release archives may not contain symbolic or hard links" >&2; exit 1
fi
tar --extract --file="$archive" --directory="$release" --no-same-owner --no-same-permissions
printf '%s\n' "$revision" > "$release/REVISION"
printf '%s\n' "$image_id" > "$release/IMAGE_ID"
export DB_HOST_DIR="$deploy_path/shared/db"
export APP_REVISION="$revision"
export APP_IMAGE="$image_id"
compose() { docker compose --project-name vip_champ --file "$release/docker-compose.yml" "$@"; }
health_timeout=720
health_gate() {
  local checked_release=$1 expected_revision=$2 expected_image=$3
  local checked_container state health checked_revision checked_image attempt
  checked_container=$(docker compose --project-name vip_champ --file "$checked_release/docker-compose.yml" ps -q telegram-bot)
  [[ -n "$checked_container" ]] || return 1
  for attempt in $(seq 1 "$(((health_timeout + 4) / 5))"); do
    state=$(docker inspect "$checked_container" --format '{{.State.Status}}')
    health=$(docker inspect "$checked_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')
    checked_revision=$(docker inspect "$checked_container" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
    checked_image=$(docker inspect "$checked_container" --format '{{.Image}}')
    if [[ "$state" = running && "$health" = healthy && "$checked_revision" = "$expected_revision" && "$checked_image" = "$expected_image" ]]; then
      return 0
    fi
    [[ "$state" != exited && "$state" != dead ]] || return 1
    sleep 5
  done
  return 1
}
previous=""
previous_revision=""
previous_image=""
if [[ -L "$deploy_path/current" ]]; then
  previous=$(readlink -f "$deploy_path/current")
  [[ "$previous" = "$deploy_path/releases/"* ]] || { echo "Invalid current release" >&2; exit 1; }
  previous_revision=$(cat "$previous/REVISION")
  [[ "$previous_revision" =~ ^[0-9a-f]{40}$ ]] || exit 1
  if [[ -f "$previous/IMAGE_ID" ]]; then
    previous_image=$(cat "$previous/IMAGE_ID")
  else
    previous_image=$(docker image inspect "vip-champ:$previous_revision" --format '{{.Id}}')
  fi
  [[ "$previous_image" =~ ^sha256:[0-9a-f]{64}$ ]] || exit 1
fi

candidate_env=""
rollback_env=""
next_link=""
replacement_started=0
environment_promoted=0
current_promoted=0
preserve_rollback_env=0
cleanup() {
  [[ -z "$candidate_env" ]] || rm -f -- "$candidate_env"
  if [[ -n "$rollback_env" && "$preserve_rollback_env" = 0 ]]; then rm -f -- "$rollback_env"; fi
  [[ -z "$next_link" ]] || rm -f -- "$next_link"
}
rollback() {
  local status=$?
  [[ "$status" != 0 ]] || status=1
  trap - ERR INT TERM HUP
  if [[ "$replacement_started" = 1 ]]; then
    if [[ "$environment_promoted" = 1 ]]; then export ENV_FILE="$shared_env"; fi
    compose stop telegram-bot || true
  fi
  if [[ "$environment_promoted" = 1 ]]; then
    if ! mv -Tf -- "$rollback_env" "$shared_env"; then
      preserve_rollback_env=1
      echo "ROLLBACK FAILED to restore environment. Protected recovery file retained; operator action required." >&2
      exit "$status"
    fi
  fi
  export ENV_FILE="$shared_env"
  if [[ "$current_promoted" = 1 ]]; then
    if [[ -n "$previous" ]]; then
      next_link="$deploy_path/current.$revision.$$.rollback"
      if ! ln -s "$previous" "$next_link" || ! mv -Tf "$next_link" "$deploy_path/current"; then
        echo "ROLLBACK FAILED to restore current release pointer; operator action required." >&2
        exit "$status"
      fi
    elif [[ -L "$deploy_path/current" && "$(readlink "$deploy_path/current")" = "$release" ]]; then
      rm -- "$deploy_path/current"
    fi
  fi
  if [[ "$replacement_started" = 1 ]]; then
    if [[ -n "$previous" ]]; then
      export APP_REVISION="$previous_revision" APP_IMAGE="$previous_image"
      if docker compose --project-name vip_champ --file "$previous/docker-compose.yml" up -d --no-build --pull never telegram-bot \
          && health_gate "$previous" "$previous_revision" "$previous_image"; then
        echo "Previous application release and environment are healthy; database backup retained." >&2
      else
        echo "ROLLBACK FAILED its health gate. Original environment and database backup retained; operator action required." >&2
      fi
    else
      echo "First deployment failed; application stopped and original environment retained." >&2
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap rollback ERR INT TERM HUP
original_env_hash=$(sha256sum "$shared_env" | cut -d ' ' -f 1)
rollback_env=$(mktemp "$deploy_path/shared/.env.previous.XXXXXX")
cp --preserve=mode -- "$shared_env" "$rollback_env"
candidate_env=$(mktemp "$deploy_path/shared/.env.candidate.XXXXXX")
cp -- "$shared_env" "$candidate_env"
chmod 0600 "$candidate_env"
python3 "$release/scripts/set_bot_token.py" "$candidate_env"
export ENV_FILE="$candidate_env"

# Never rebuild on the application host: use the exact artifact verified in CI.
docker load --input "$image_archive"
loaded_id=$(docker image inspect "$image_id" --format '{{.Id}}')
loaded_revision=$(docker image inspect "$image_id" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
[[ "$loaded_id" = "$image_id" && "$loaded_revision" = "$revision" ]] || {
  echo "CI image identity or revision mismatch" >&2; false;
}
# This authenticates getMe; it does not poll or consume updates from the old bot.
if health_timeout=$(compose run --rm --no-deps --pull never --entrypoint python telegram-bot scripts/preflight.py); then
  :
else
  exit "$?"
fi
[[ "$health_timeout" =~ ^[0-9]{3,9}$ ]] || { echo "Invalid candidate startup budget" >&2; false; }
(( health_timeout >= 720 )) || health_timeout=720
compose run --rm --no-deps --pull never --entrypoint python telegram-bot scripts/backup_database.py
[[ "$(sha256sum "$shared_env" | cut -d ' ' -f 1)" = "$original_env_hash" ]] || {
  echo "External configuration changed during deployment; refusing to overwrite it." >&2; false;
}
replacement_started=1
compose up -d --no-build --pull never telegram-bot
health_gate "$release" "$revision" "$image_id" || { echo "Deployment health gate failed" >&2; false; }
[[ "$(sha256sum "$shared_env" | cut -d ' ' -f 1)" = "$original_env_hash" ]] || {
  echo "External configuration changed during readiness check; refusing to overwrite it." >&2; false;
}
environment_promoted=1
mv -Tf -- "$candidate_env" "$shared_env"
export ENV_FILE="$shared_env"
next_link="$deploy_path/current.$revision.$$.next"
ln -s "$release" "$next_link"
current_promoted=1
mv -Tf "$next_link" "$deploy_path/current"
trap - ERR INT TERM HUP
echo "Deployment healthy: revision=$revision image=$image_id"
