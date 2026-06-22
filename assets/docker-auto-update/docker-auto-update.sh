#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BASE_DIR_DEFAULT="$SCRIPT_DIR"
CONFIG_FILE="${CONFIG_FILE:-${SCRIPT_DIR}/auto-update.conf}"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"

ROOT_DIR="${ROOT_DIR:-$(dirname -- "$BASE_DIR_DEFAULT")}"
BASE_DIR="${BASE_DIR:-$BASE_DIR_DEFAULT}"
LOG_DIR="${LOG_DIR:-$BASE_DIR/logs}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-7}"
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

cleanup_old_logs() {
  if [[ "$LOG_RETENTION_DAYS" =~ ^[0-9]+$ ]] && [ "$LOG_RETENTION_DAYS" -gt 0 ]; then
    find "$LOG_DIR" -maxdepth 1 -type f -name 'update-*.log' -mtime +$((LOG_RETENTION_DAYS - 1)) -print -delete \
      | sed 's/^/🧹 deleted old log: /'
  fi
}

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

recreate_compose_container() {
  local cname="$1"
  local project service workdir files file
  local -a compose_files fargs=()

  project="$(docker inspect "$cname" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)"
  service="$(docker inspect "$cname" --format '{{index .Config.Labels "com.docker.compose.service"}}' 2>/dev/null || true)"
  workdir="$(docker inspect "$cname" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
  files="$(docker inspect "$cname" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' 2>/dev/null || true)"

  if [ -z "$project" ] || [ -z "$service" ] || [ -z "$workdir" ] || [ -z "$files" ]; then
    echo "❌ 无法重建 $cname：缺少 compose 标签。"
    return 1
  fi

  IFS=',' read -r -a compose_files <<< "$files"
  for file in "${compose_files[@]}"; do
    fargs+=("-f" "$file")
  done

  echo "🔁 重建 $cname，恢复 compose 中固定的 MAC 地址..."
  (cd "$workdir" && docker compose -p "$project" "${fargs[@]}" up -d --force-recreate "$service")
}

offer_fix_mac_mismatches() {
  local mac_output="$1"
  local ans cname
  local -a containers=()

  while IFS= read -r cname; do
    [ -n "$cname" ] && containers+=("$cname")
  done < <(printf '%s\n' "$mac_output" | awk '/^  [^ ]+ [^ ]+: expected=/{print $1}' | sort -u)
  [ ${#containers[@]} -gt 0 ] || return 0

  if [ ! -t 0 ]; then
    echo "ℹ️ 检测到 MAC 不一致；当前不是交互终端，跳过自动重建。"
    return 2
  fi

  echo "检测到以下容器 MAC 与 compose 不一致：${containers[*]}"
  read -r -p "是否立即重建这些容器以恢复固定 MAC？[y/N]: " ans
  if [[ ! "$ans" =~ ^[Yy]$ ]]; then
    echo "ℹ️ 已跳过 MAC 修复。"
    return 2
  fi

  for cname in "${containers[@]}"; do
    recreate_compose_container "$cname" || return 1
  done
}

mode="update"
ignore_delay="false"
fix_mac_interactive="false"
docker_run="false"
include_names=()
for arg in "$@"; do
  case "$arg" in
    --check-only|-n) mode="check" ;;
    --ignore-delay) ignore_delay="true" ;;
    --fix-mac-interactive) fix_mac_interactive="true" ;;
    --docker-run) docker_run="true" ;;
    --help|-h)
      echo "Usage: $0 [--check-only|--ignore-delay|--fix-mac-interactive|--docker-run] [container[,container...]]"
      exit 0
      ;;
    --*)
      echo "❌ 未知参数：$arg"
      exit 2
      ;;
    *) include_names+=("$arg") ;;
  esac
done

{
  echo "=== $(date -Is) yehbp docker auto update start mode=$mode ==="
  echo "ROOT_DIR=$ROOT_DIR"
  echo "BASE_DIR=$BASE_DIR"
  cleanup_old_logs

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
  if [ "$DELAY_DAYS" != "0" ] && [ "$ignore_delay" != "true" ]; then
    args+=("-d" "$DELAY_DAYS")
  fi
  if [ "$docker_run" = "true" ]; then
    args+=("-r")
  fi
  if [ "$AUTO_PRUNE" = "true" ] && [ "$mode" = "update" ]; then
    args+=("-p")
  fi
  if [ "$mode" = "check" ]; then
    args+=("-n")
  else
    args+=("-a")
  fi
  if [ ${#include_names[@]} -gt 0 ]; then
    args+=("${include_names[@]}")
  fi

  echo "+ ./dockcheck.sh ${args[*]}"
  run_with_heartbeat ./dockcheck.sh "${args[@]}"

  if [ "$CHECK_MAC" = "true" ]; then
    echo "+ ./check-compose-macs.py"
    set +e
    mac_output="$(./check-compose-macs.py 2>&1)"
    mac_rc=$?
    set -e
    printf '%s\n' "$mac_output"
    if [ "$mac_rc" -eq 2 ] && [ "$fix_mac_interactive" = "true" ]; then
      set +e
      offer_fix_mac_mismatches "$mac_output"
      fix_rc=$?
      set -e
      if [ "$fix_rc" -eq 0 ]; then
        echo "+ ./check-compose-macs.py"
        ./check-compose-macs.py
      else
        exit "$mac_rc"
      fi
    elif [ "$mac_rc" -ne 0 ]; then
      exit "$mac_rc"
    fi
  fi

  echo "=== $(date -Is) yehbp docker auto update done ==="
} 2>&1 | tee -a "$LOG_FILE"
