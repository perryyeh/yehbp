#!/usr/bin/env bash
set -euo pipefail

BASE_DIR_DEFAULT="/vol2/1000/dockerapps/_auto_update"
CONFIG_FILE="${CONFIG_FILE:-${BASE_DIR_DEFAULT}/auto-update.conf}"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"

ROOT_DIR="${ROOT_DIR:-/vol2/1000/dockerapps}"
BASE_DIR="${BASE_DIR:-$BASE_DIR_DEFAULT}"
LOG_DIR="${LOG_DIR:-$BASE_DIR/logs}"
DOCKCHECK_EXTRA_ARGS="${DOCKCHECK_EXTRA_ARGS:--m -t 30}"
AUTO_PRUNE="${AUTO_PRUNE:-false}"
DELAY_DAYS="${DELAY_DAYS:-0}"
CHECK_MAC="${CHECK_MAC:-true}"

mkdir -p "$LOG_DIR"
LOCK_FILE="$BASE_DIR/.docker-auto-update.lock"
LOG_FILE="$LOG_DIR/update-$(date +%Y%m%d).log"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -Is) another update is already running" >> "$LOG_FILE"
  exit 0
fi

export PATH="$BASE_DIR/bin:$PATH"
export ROOT_DIR
export HOME="${HOME:-/root}"

mode="update"
case "${1:-}" in
  --check-only|-n) mode="check" ;;
  --help|-h)
    echo "Usage: $0 [--check-only]"
    exit 0
    ;;
esac

{
  echo "=== $(date -Is) yehbp docker auto update start mode=$mode ==="
  echo "ROOT_DIR=$ROOT_DIR"
  echo "BASE_DIR=$BASE_DIR"

  cd "$BASE_DIR"
  args=()
  # Split configured args intentionally; config is root-owned local file.
  extra=( $DOCKCHECK_EXTRA_ARGS )
  args+=("${extra[@]}")
  if [ "$DELAY_DAYS" != "0" ]; then
    args+=("-d" "$DELAY_DAYS")
  fi
  if [ "$AUTO_PRUNE" = "true" ]; then
    args+=("-p")
  fi
  if [ "$mode" = "check" ]; then
    args+=("-n")
  else
    args+=("-a")
  fi

  echo "+ ./dockcheck.sh ${args[*]}"
  ./dockcheck.sh "${args[@]}"

  if [ "$CHECK_MAC" = "true" ]; then
    echo "+ ./check-compose-macs.py"
    ./check-compose-macs.py
  fi

  echo "=== $(date -Is) yehbp docker auto update done ==="
} 2>&1 | tee -a "$LOG_FILE"
