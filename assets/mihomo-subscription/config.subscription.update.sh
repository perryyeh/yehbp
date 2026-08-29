#!/usr/bin/env bash
# YehBP Mihomo full-configuration subscription updater.
# This script is installed beside config.yaml and is invoked by YehBP or a host timer.
set -euo pipefail

APP_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONFIG="$APP_DIR/config.yaml"
BACKUP="$APP_DIR/config.macvlan.backup.yaml"
SUBSCRIPTION_CONF="$APP_DIR/config.subscription.conf"
LOG_FILE="$APP_DIR/config.subscription.update.log"
LOCK_DIR="$APP_DIR/.config.subscription.update.lock"
CONTAINER_NAME="${MIHOMO_CONTAINER_NAME:-}"

log_event() {
  local message="$1" body existing tmp
  mkdir -p "$APP_DIR"
  body="$(mktemp "$APP_DIR/.subscription-log.XXXXXX")"
  {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$message"
  } >"$body"

  # Keep log entries dated within the most recent seven calendar days, newest first.
  # Python is already required for the structural YAML transformation below.
  existing="$(mktemp "$APP_DIR/.subscription-log-existing.XXXXXX")"
  if [ -f "$LOG_FILE" ]; then
    python3 - "$LOG_FILE" "$existing" <<'PY'
from datetime import date, timedelta
from pathlib import Path
import re, sys
src, dst = map(Path, sys.argv[1:])
cutoff = date.today() - timedelta(days=6)
blocks = re.split(r'\n{2,}', src.read_text(encoding='utf-8', errors='replace').strip())
kept = []
for block in blocks:
    m = re.match(r'^\[(\d{4}-\d{2}-\d{2}) ', block)
    if not m:
        continue
    try:
        if date.fromisoformat(m.group(1)) >= cutoff:
            kept.append(block.strip())
    except ValueError:
        pass
Path(dst).write_text('\n\n'.join(kept) + ('\n' if kept else ''), encoding='utf-8')
PY
  else
    : >"$existing"
  fi

  tmp="$(mktemp "$APP_DIR/.subscription-log-new.XXXXXX")"
  cat "$body" >"$tmp"
  if [ -s "$existing" ]; then
    printf '\n' >>"$tmp"
    cat "$existing" >>"$tmp"
  fi
  # Collapse accidental duplicate blank lines before atomically publishing.
  python3 - "$tmp" "$LOG_FILE" <<'PY'
from pathlib import Path
import re, sys
src, dst = map(Path, sys.argv[1:])
text = src.read_text(encoding='utf-8', errors='replace').strip()
text = re.sub(r'\n{3,}', '\n\n', text)
Path(dst).write_text((text + '\n') if text else '', encoding='utf-8')
PY
  rm -f "$body" "$existing" "$tmp"
}

cleanup() {
  rm -rf "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log_event "跳过：已有订阅更新任务正在执行。"
  exit 0
fi

if [ ! -r "$SUBSCRIPTION_CONF" ]; then
  log_event "失败：未找到 config.subscription.conf。"
  exit 1
fi

# shellcheck disable=SC1090
. "$SUBSCRIPTION_CONF"
URL="${URL:-}"
if [[ ! "$URL" =~ ^https?://[^[:space:]]+$ ]]; then
  log_event "失败：config.subscription.conf 中的 URL 无效。"
  exit 1
fi
if [ ! -r "$BACKUP" ]; then
  log_event "失败：未找到 config.macvlan.backup.yaml，拒绝覆盖当前配置。"
  exit 1
fi
if [ -z "$CONTAINER_NAME" ]; then
  log_event "失败：未提供 MIHOMO_CONTAINER_NAME。"
  exit 1
fi
if ! command -v docker >/dev/null 2>&1 || ! docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  log_event "失败：Mihomo 容器不存在：$CONTAINER_NAME。"
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  log_event "失败：宿主机未安装 curl。"
  exit 1
fi

raw="$(mktemp "$APP_DIR/.subscription-download.XXXXXX.yaml")"
candidate="$(mktemp "$APP_DIR/.subscription-candidate.XXXXXX.yaml")"
trap 'rm -f "$raw" "$candidate"; cleanup' EXIT INT TERM

if ! curl --connect-timeout 15 --max-time 120 --fail --location --silent --show-error "$URL" -o "$raw"; then
  log_event "失败：订阅下载失败，运行中的 config.yaml 未修改。"
  exit 1
fi
if [ ! -s "$raw" ]; then
  log_event "失败：订阅下载为空，运行中的 config.yaml 未修改。"
  exit 1
fi

if ! python3 - "$raw" "$BACKUP" "$candidate" <<'PY'
from pathlib import Path
import copy
import sys
try:
    import yaml
except ImportError:
    raise SystemExit('缺少 PyYAML：请通过 YehBP 配置菜单安装 python3-yaml 后重试。')

source_path, backup_path, candidate_path = map(Path, sys.argv[1:])
try:
    source = yaml.safe_load(source_path.read_text(encoding='utf-8'))
    backup = yaml.safe_load(backup_path.read_text(encoding='utf-8'))
except Exception as e:
    raise SystemExit(f'YAML 解析失败：{e}')
if not isinstance(source, dict):
    raise SystemExit('订阅根节点必须是 YAML mapping。')
if not isinstance(backup, dict):
    raise SystemExit('config.macvlan.backup.yaml 根节点必须是 YAML mapping。')

# Only gateway-owned settings are overlaid. Nodes, groups, rules, and upstream
# DNS servers remain owned by the full upstream subscription.
def require_backup(path):
    value = backup
    for part in path:
        if not isinstance(value, dict) or part not in value:
            raise SystemExit('本地 macvlan 备份缺少必要字段：' + '.'.join(path))
        value = value[part]
    return copy.deepcopy(value)

def set_path(path, value):
    target = source
    for part in path[:-1]:
        if not isinstance(target.get(part), dict):
            target[part] = {}
        target = target[part]
    target[path[-1]] = value

for key in ('secret', 'bind-address', 'allow-lan', 'ipv6', 'mode',
            'external-controller', 'external-ui', 'external-ui-url'):
    set_path((key,), require_backup((key,)))
for key in ('store-selected', 'store-fake-ip'):
    set_path(('profile', key), require_backup(('profile', key)))
for key in ('enable', 'stack', 'udp-timeout', 'auto-route',
            'auto-redirect', 'auto-detect-interface'):
    set_path(('tun', key), require_backup(('tun', key)))
for key in ('enable', 'listen', 'enhanced-mode', 'respect-rules'):
    set_path(('dns', key), require_backup(('dns', key)))

# Do not introduce fake-IP address families absent from the upstream complete
# configuration. If present, replace them with the site-local macvlan values.
source_dns = source.get('dns')
if not isinstance(source_dns, dict):
    raise SystemExit('订阅 dns 必须是 YAML mapping。')
backup_dns = backup.get('dns')
if not isinstance(backup_dns, dict):
    raise SystemExit('本地 macvlan 备份 dns 必须是 YAML mapping。')
for fake_key in ('fake-ip-range', 'fake-ip-range6'):
    if fake_key in source_dns:
        if fake_key not in backup_dns:
            raise SystemExit(f'订阅包含 {fake_key}，但本地 macvlan 备份没有对应目标网段。')
        source_dns[fake_key] = copy.deepcopy(backup_dns[fake_key])

candidate_path.write_text(
    yaml.safe_dump(source, allow_unicode=True, sort_keys=False, default_flow_style=False),
    encoding='utf-8',
)
PY
then
  log_event "失败：订阅 YAML 修补失败，运行中的 config.yaml 未修改。"
  exit 1
fi

# Use the exact core that is currently running to validate the generated file.
if ! docker cp "$candidate" "$CONTAINER_NAME:/tmp/config.subscription.candidate.yaml" >/dev/null; then
  log_event "失败：无法将候选配置送入 Mihomo 容器，运行中的 config.yaml 未修改。"
  exit 1
fi
if ! docker exec "$CONTAINER_NAME" /mihomo -t -f /tmp/config.subscription.candidate.yaml >/dev/null 2>&1; then
  docker exec "$CONTAINER_NAME" rm -f /tmp/config.subscription.candidate.yaml >/dev/null 2>&1 || true
  log_event "失败：Mihomo 校验未通过，运行中的 config.yaml 未修改。"
  exit 1
fi
docker exec "$CONTAINER_NAME" rm -f /tmp/config.subscription.candidate.yaml >/dev/null 2>&1 || true

if cmp -s "$candidate" "$CONFIG"; then
  log_event "完成：订阅有效，但生成配置未变化，无需重启 Mihomo。"
  exit 0
fi

# mv is atomic because candidate and config.yaml share the bind-mounted directory.
mv "$candidate" "$CONFIG"
if docker restart "$CONTAINER_NAME" >/dev/null; then
  if docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -qx true; then
    log_event "完成：订阅有效，已替换 config.yaml 并重启 Mihomo。"
    exit 0
  fi
fi
log_event "警告：新 config.yaml 已通过校验并已替换，但 Mihomo 重启后未处于运行状态；请检查容器日志。"
exit 1
