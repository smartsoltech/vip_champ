#!/usr/bin/env sh
# Protected master pushes transfer the checked image; the host never rebuilds it.
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
    echo "Set Drone secret vip_champ_telegram_api_token before deployment." >&2; exit 1 ;;
esac
case "$DEPLOY_HOST:$DEPLOY_USER:$DEPLOY_PATH" in
  *[!a-zA-Z0-9._:/@-]*) echo "Unsupported deployment target characters" >&2; exit 2 ;;
esac
case "$DRONE_COMMIT_SHA" in *[!0-9a-f]*|"") exit 2 ;; esac
[ "${#DRONE_COMMIT_SHA}" -eq 40 ]
[ "$(cat .artifacts/revision)" = "$DRONE_COMMIT_SHA" ]
image_id=$(cat .artifacts/image-id)
image_digest=${image_id#sha256:}
case "$image_digest" in *[!0-9a-f]*|"") exit 2 ;; esac
[ "$image_id" = "sha256:$image_digest" ] && [ "${#image_digest}" -eq 64 ]
image_hash=$(cat .artifacts/image-sha256)
case "$image_hash" in *[!0-9a-f]*|"") exit 2 ;; esac
[ "${#image_hash}" -eq 64 ]
printf '%s  %s\n' "$image_hash" .artifacts/image.tar.gz | sha256sum -c >/dev/null

ssh_dir=$(mktemp -d)
remote_dir=""
ssh_target="$DEPLOY_USER@$DEPLOY_HOST"
remote() {
  ssh -i "$ssh_dir/key" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o UserKnownHostsFile="$ssh_dir/known_hosts" "$ssh_target" "$@"
}
cleanup() {
  if [ -n "$remote_dir" ]; then
    remote "rm -f -- '$remote_dir/source.tar.gz' '$remote_dir/image.tar.gz' '$remote_dir/deploy.sh'; rmdir -- '$remote_dir'" </dev/null >/dev/null 2>&1 || true
  fi
  rm -f "$ssh_dir/key" "$ssh_dir/known_hosts" "$ssh_dir/deploy.sh" "$ssh_dir/source.tar.gz"
  rmdir "$ssh_dir"
}
trap cleanup EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$ssh_dir/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$ssh_dir/known_hosts"
unset DEPLOY_SSH_KEY DEPLOY_KNOWN_HOSTS
git archive --format=tar.gz --output="$ssh_dir/source.tar.gz" "$DRONE_COMMIT_SHA"
git show "$DRONE_COMMIT_SHA:scripts/deploy.sh" > "$ssh_dir/deploy.sh"
archive_hash=$(sha256sum "$ssh_dir/source.tar.gz" | cut -d ' ' -f 1)
remote_dir=$(remote 'umask 077; mktemp -d /tmp/vip-champ-deploy.XXXXXXXX')
case "$remote_dir" in
  /tmp/vip-champ-deploy.*) ;;
  *) remote_dir=""; echo "Invalid remote transfer directory" >&2; exit 1 ;;
esac
remote_suffix=${remote_dir#/tmp/vip-champ-deploy.}
case "$remote_suffix" in *[!a-zA-Z0-9]*) remote_dir=""; exit 1 ;; esac
[ "${#remote_suffix}" -eq 8 ] || { remote_dir=""; exit 1; }
copy() {
  scp -i "$ssh_dir/key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 \
    -o UserKnownHostsFile="$ssh_dir/known_hosts" "$1" "$ssh_target:$remote_dir/$2"
}
copy "$ssh_dir/source.tar.gz" source.tar.gz
copy "$ssh_dir/deploy.sh" deploy.sh
copy .artifacts/image.tar.gz image.tar.gz
printf '%s' "$TELEGRAM_API_TOKEN" | remote \
  "bash '$remote_dir/deploy.sh' '$DEPLOY_PATH' '$DRONE_COMMIT_SHA' '$remote_dir/source.tar.gz' '$archive_hash' '$remote_dir/image.tar.gz' '$image_hash' '$image_id'"
