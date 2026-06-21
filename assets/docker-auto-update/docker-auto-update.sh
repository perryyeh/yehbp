#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BASE_DIR_DEFAULT="$SCRIPT_DIR"
CONFIG_FILE="${CONFIG_FILE:-${SCRIPT_DIR}/auto-update.conf}"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"

ROOT_DIR="${ROOT_DIR:-$(dirname -- "$BASE_DIR_DEFAULT")}"
BASE_DIR="${BASE_DIR:-$BASE_DIR_DEFAULT}"
LOG_DIR="${LOG_DIR:-$BASE_DIR/logs}"
DOCKCHECK_EXTRA_ARGS="${DOCKCHECK_EXTRA_ARGS:--m -t 30}"
AUTO_PRUNE="${AUTO_PRUNE:-false}"
DELAY_DAYS="${DELAY_DAYS:-0}"
CHECK_MAC="${CHECK_MAC:-true}"
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-15}"

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

run_with_heartbeat() {
  local start now elapsed pid rc sleep_pid
  start="$(date +%s)"
  rc=0

  "$@" &
  pid=$!

  while kill -0 "$pid" 2>/dev/null; do
    sleep "$HEARTBEAT_INTERVAL" &
    sleep_pid=$!
    wait "$sleep_pid" 2>/dev/null || true
    if kill -0 "$pid" 2>/dev/null; then
      now="$(date +%s)"
      elapsed=$((now - start))
      echo "⏳ Dockcheck 仍在运行，已等待 ${elapsed}s ..."
    fi
  done

  wait "$pid" || rc=$?
  return "$rc"
}

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
  if [ ! -x ./dockcheck.sh ]; then
    echo "❌ 未找到可执行的 Dockcheck 脚本：$BASE_DIR/dockcheck.sh"
    echo "👉 请重新执行 96 安装 Dockcheck 自动更新。"
    exit 1
  fi

  args=()
  # Split configured args intentionally; config is root-owned local file.
  extra=( $DOCKCHECK_EXTRA_ARGS )
  args+=("${extra[@]}")
  if [ "$DELAY_DAYS" != "0" ]; then
    args+=("-d" "$DELAY_DAYS")
  fi
  if [ "$AUTO_PRUNE" = "true" ] && [ "$mode" = "update" ]; then
    args+=("-p")
  fi
  if [ "$mode" = "check" ]; then
    args+=("-n")
  else
    args+=("-a")
  fi

  echo "+ ./dockcheck.sh ${args[*]}"
  run_with_heartbeat ./dockcheck.sh "${args[@]}"

  if [ "$CHECK_MAC" = "true" ]; then
    echo "+ ./check-compose-macs.py"
    ./check-compose-macs.py
  fi

  echo "=== $(date -Is) yehbp docker auto update done ==="
} 2>&1 | tee -a "$LOG_FILE"
