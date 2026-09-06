#!/usr/bin/env bash
# Deploy LMS Cache to the NAS over SSH. Settings come from deploy.env next to this repo (see deploy.env.example).
#   scripts/deploy.sh sync    copy this folder to the NAS (tar over ssh, which copes with spaces in share names) and
#                             render docker-compose.yaml there with the values from deploy.env
#   scripts/deploy.sh up      build the image and (re)start the container; refuses while transfers are in flight (--force overrides)
#   scripts/deploy.sh logs    follow the container log
# Requires an ssh alias for the NAS whose user is in the NAS's docker group.
set -euo pipefail
cd "$(dirname "$0")/.."
export COPYFILE_DISABLE=1   # keep macOS tar from adding ._ AppleDouble files
if [ -f deploy.env ]; then
  while IFS='=' read -r key value; do
    case "$key" in ''|\#*) continue;; esac
    value="${value%%#*}"; value="${value%"${value##*[![:space:]]}"}"   # strip trailing comments and spaces
    [ -n "${!key:-}" ] || export "$key=$value"
  done < deploy.env
fi
HOST="${LMSCACHE_HOST:-nas}"
REMOTE_DIR="${LMSCACHE_REMOTE_DIR:-/volume1/models/lmscache/src}"
SHARE_DIR="${LMSCACHE_SHARE:-/volume1/models}"

render_compose() {  # substitute ${VAR:-default} placeholders with the configured values
  local out="$1" var val
  cp docker-compose.yaml "$out"
  for var in LMSCACHE_SHARE LMSCACHE_CONFIG LMSCACHE_UID LMSCACHE_GID LMSCACHE_PORT; do
    val="${!var:-}"
    [ -n "$val" ] || continue
    sed -i.bak "s#\${$var:-[^}]*}#$val#g" "$out" && rm -f "$out.bak"
  done
}

case "${1:-sync}" in
  sync)
    tmp=$(mktemp -d)
    render_compose "$tmp/docker-compose.yaml"
    tar czf - --exclude .venv --exclude .dev --exclude __pycache__ --exclude '*.egg-info' --exclude build --exclude .git \
        --exclude deploy.env --exclude docker-compose.yaml . \
      | ssh "$HOST" "mkdir -p \"$REMOTE_DIR\" && find \"$REMOTE_DIR\" -mindepth 1 -delete && tar xzf - -C \"$REMOTE_DIR\""
    ssh "$HOST" "cat > \"$REMOTE_DIR/docker-compose.yaml\"" < "$tmp/docker-compose.yaml"
    rm -rf "$tmp"
    echo "synced to $HOST:$REMOTE_DIR"; ssh "$HOST" "grep -E 'user:|:/models|:/config' \"$REMOTE_DIR/docker-compose.yaml\""
    ;;
  up)
    if [ "${2:-}" != "--force" ] && ssh "$HOST" "find \"$SHARE_DIR/.incoming\" -type f -mmin -2 2>/dev/null | grep -q ." ; then
      echo "Transfers are in progress on the NAS (files in .incoming changed within 2 minutes). Retry later or add --force." >&2
      exit 1
    fi
    ssh "$HOST" "cd \"$REMOTE_DIR\" && chmod 700 ../config && docker compose up -d --build && docker compose ps"
    ;;
  logs)
    ssh -t "$HOST" "cd \"$REMOTE_DIR\" && docker compose logs -f --tail=100"
    ;;
  *) echo "usage: $0 sync|up [--force]|logs" >&2; exit 2;;
esac
