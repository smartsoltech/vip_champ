#!/usr/bin/env sh
# Only called by the protected master push pipeline after verification.
set +x
set -eu
umask 077
: "${DRONE_COMMIT_SHA:?}"
: "${DEPLOY_HOST:?}"
: "${DEPLOY_USER:?}"
: "${DEPLOY_SSH_KEY:?}"
: "${DEPLOY_KNOWN_HOSTS:?}"
: "${DEPLOY_PATH:?}"
case "${TELEGRAM_API_TOKEN:-}" in
  ""|CHANGE_ME_IN_DRONE_SETTINGS)
    echo "Set Drone secret vip_champ_telegram_api_token before deployment." >&2
    exit 1
    ;;
esac
case "$DEPLOY_HOST:$DEPLOY_USER:$DEPLOY_PATH" in
  *[!a-zA-Z0-9._:/@-]*) echo "Unsupported deployment target characters" >&2; exit 2 ;;
esac
case "$DRONE_COMMIT_SHA" in *[!0-9a-f]*|"") exit 2 ;; esac
[ "${#DRONE_COMMIT_SHA}" -eq 40 ]
ssh_dir=$(mktemp -d)
trap 'rm -f "$ssh_dir/key" "$ssh_dir/known_hosts"; rmdir "$ssh_dir"' EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$ssh_dir/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$ssh_dir/known_hosts"
unset DEPLOY_SSH_KEY DEPLOY_KNOWN_HOSTS
git archive --format=tar.gz --output=/tmp/vip-champ-release.tar.gz "$DRONE_COMMIT_SHA"
archive_hash=$(sha256sum /tmp/vip-champ-release.tar.gz | cut -d ' ' -f 1)
remote_archive="/tmp/vip-champ-$DRONE_COMMIT_SHA.tar.gz"
remote_script="/tmp/vip-champ-deploy-$DRONE_COMMIT_SHA.sh"
scp -i "$ssh_dir/key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$ssh_dir/known_hosts" \
  /tmp/vip-champ-release.tar.gz "$DEPLOY_USER@$DEPLOY_HOST:$remote_archive"
scp -i "$ssh_dir/key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$ssh_dir/known_hosts" \
  scripts/deploy.sh "$DEPLOY_USER@$DEPLOY_HOST:$remote_script"
printf '%s' "$TELEGRAM_API_TOKEN" | ssh -i "$ssh_dir/key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$ssh_dir/known_hosts" \
  "$DEPLOY_USER@$DEPLOY_HOST" \
  "bash '$remote_script' '$DEPLOY_PATH' '$DRONE_COMMIT_SHA' '$remote_archive' '$archive_hash'"
