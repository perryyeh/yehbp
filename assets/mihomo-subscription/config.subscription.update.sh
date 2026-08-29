#!/bin/sh
# YehBP Mihomo full-configuration subscription updater.
# Runs inside the Mihomo container; its supervisor owns schedule and core restart.
set -eu
umask 077

APP_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONFIG="$APP_DIR/config.yaml"
BACKUP="$APP_DIR/config.macvlan.backup.yaml"
REPLACE="$APP_DIR/config.replace.yaml"
SUBSCRIPTION_CONF="$APP_DIR/config.subscription.conf"
LOG_FILE="$APP_DIR/config.subscription.update.log"
LOCK_DIR="$APP_DIR/.config.subscription.update.lock"
RELOAD_REQUEST="$APP_DIR/.config.subscription.reload.request"
RELOAD_DONE="$APP_DIR/.config.subscription.reload.done"
MODE="${1:---once}"

log_event() {
  message="$1"
  body="$(mktemp "$APP_DIR/.subscription-log.XXXXXX")"
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$message" >"$body"

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
  python3 - "$tmp" "$LOG_FILE" <<'PY'
from pathlib import Path
import re, sys
src, dst = map(Path, sys.argv[1:])
text = src.read_text(encoding='utf-8', errors='replace').strip()
text = re.sub(r'\n{3,}', '\n\n', text)
Path(dst).write_text((text + '\n') if text else '', encoding='utf-8')
PY
  chmod 0600 "$LOG_FILE"
  rm -f "$body" "$existing" "$tmp"
}

cleanup() {
  status="${1:-$?}"
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  return "$status"
}
trap 'status=$?; cleanup "$status"' 0 1 2 15

request_reload() {
  [ "${MIHOMO_SUPERVISOR_BOOT:-0}" = 1 ] && return 0
  rm -f "$RELOAD_DONE"
  : >"$RELOAD_REQUEST"
  [ "${MIHOMO_WAIT_RELOAD:-0}" = 1 ] || return 0
  i=0
  while [ "$i" -lt 30 ]; do
    [ -f "$RELOAD_DONE" ] && return 0
    sleep 1
    i=$((i + 1))
  done
  log_event "警告：新 config.yaml 已通过校验并已替换，但容器内 Mihomo 未在 30 秒内确认重载。"
  return 1
}

CONFIG_CHANGED=0

validate_and_publish() {
  candidate="$1"
  if ! /mihomo -t -f "$candidate" >/dev/null 2>&1; then
    log_event "失败：Mihomo 校验未通过，运行中的 config.yaml 未修改。"
    return 1
  fi
  if cmp -s "$candidate" "$CONFIG"; then
    log_event "完成：订阅有效，但生成配置未变化，无需重载 Mihomo。"
    return 0
  fi
  mv "$candidate" "$CONFIG"
  chmod 0600 "$CONFIG"
  CONFIG_CHANGED=1
  request_reload
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log_event "跳过：已有订阅更新任务正在执行。"
  exit 0
fi

case "$MODE" in
  --restore)
    if [ ! -r "$BACKUP" ]; then
      log_event "失败：未找到 config.macvlan.backup.yaml，拒绝恢复。"
      exit 1
    fi
    candidate="$(mktemp "$APP_DIR/.subscription-restore.XXXXXX")"
    cp "$BACKUP" "$candidate"
    if validate_and_publish "$candidate"; then
      log_event "完成：已恢复本地 macvlan 配置并请求重载 Mihomo。"
      exit 0
    fi
    rm -f "$candidate"
    exit 1
    ;;
  --once|--scheduled|--internal|"")
    ;;
  *)
    log_event "失败：未知更新参数：$MODE"
    exit 2
    ;;
esac

if [ ! -r "$SUBSCRIPTION_CONF" ]; then
  log_event "失败：未找到 config.subscription.conf。"
  exit 1
fi
URL="$(sed -n 's/^URL=//p' "$SUBSCRIPTION_CONF" | sed -n '1p')"
case "$URL" in
  http://*|https://*) ;;
  *) log_event "失败：config.subscription.conf 中的 URL 无效。"; exit 1 ;;
esac
if [ ! -r "$BACKUP" ]; then
  log_event "失败：未找到 config.macvlan.backup.yaml，拒绝覆盖当前配置。"
  exit 1
fi
if [ ! -r "$REPLACE" ]; then
  log_event "失败：未找到 config.replace.yaml，拒绝覆盖当前配置。"
  exit 1
fi

raw="$(mktemp "$APP_DIR/.subscription-download.XXXXXX")"
candidate="$(mktemp "$APP_DIR/.subscription-candidate.XXXXXX")"
trap 'status=$?; rm -f "$raw" "$candidate"; cleanup "$status"' 0 1 2 15

if ! curl --connect-timeout 15 --max-time 120 --fail --location --silent --show-error "$URL" -o "$raw"; then
  log_event "失败：订阅下载失败，运行中的 config.yaml 未修改。"
  exit 1
fi
if [ ! -s "$raw" ]; then
  log_event "失败：订阅下载为空，运行中的 config.yaml 未修改。"
  exit 1
fi

if ! python3 - "$raw" "$REPLACE" "$candidate" <<'PY'
from pathlib import Path
import copy
import sys
try:
    import yaml
except ImportError:
    raise SystemExit('缺少 PyYAML。')

source_path, replace_path, candidate_path = map(Path, sys.argv[1:])
try:
    source = yaml.safe_load(source_path.read_text(encoding='utf-8'))
    replace = yaml.safe_load(replace_path.read_text(encoding='utf-8'))
except Exception as e:
    raise SystemExit(f'YAML 解析失败：{e}')
if not isinstance(source, dict):
    raise SystemExit('订阅根节点必须是 YAML mapping。')
if not isinstance(replace, dict):
    raise SystemExit('config.replace.yaml 根节点必须是 YAML mapping。')

conditional_paths = {
    ('dns', 'fake-ip-range'),
    ('dns', 'fake-ip-range6'),
}

def merge(target, overlay, path=()):
    for key, value in overlay.items():
        child_path = path + (key,)
        # Do not add fake-IP ranges that are absent from the upstream config.
        if child_path in conditional_paths and key not in target:
            continue
        if isinstance(value, dict):
            current = target.get(key)
            if not isinstance(current, dict):
                current = {}
                target[key] = current
            merge(current, value, child_path)
        else:
            target[key] = copy.deepcopy(value)

merge(source, replace)

candidate_path.write_text(
    yaml.safe_dump(source, allow_unicode=True, sort_keys=False, default_flow_style=False),
    encoding='utf-8',
)
PY
then
  log_event "失败：订阅 YAML 修补失败，运行中的 config.yaml 未修改。"
  exit 1
fi

if validate_and_publish "$candidate"; then
  log_event "完成：订阅有效，已替换 config.yaml 并请求重载 Mihomo。"
  exit 0
fi
rm -f "$candidate"
exit 1
