# YehBP native Mihomo complete-configuration subscription manager.
# This file is sourced by install.sh. It currently targets OpenWrt procd services.

native_mihomo_list_targets() {
  local service command bin dir
  [ "$PLATFORM" = "openwrt" ] || return 0

  for service in /etc/init.d/*; do
    [ -f "$service" ] || continue
    command="$(sed -n 's/^[[:space:]]*procd_set_param[[:space:]]\{1,\}command[[:space:]]\{1,\}\(.*\)$/\1/p' "$service" | sed -n '1p')"
    case "$command" in
      *mihomo*' -d '*) ;;
      *) continue ;;
    esac
    bin="$(printf '%s\n' "$command" | sed -n 's/^\([^[:space:]]*mihomo[^[:space:]]*\)[[:space:]]\{1,\}-d[[:space:]]\{1,\}.*/\1/p')"
    dir="$(printf '%s\n' "$command" | sed -n 's/^.*[[:space:]]-d[[:space:]]\{1,\}\([^[:space:]]*\).*$/\1/p')"
    [ -x "$bin" ] && [ -f "$dir/config.yaml" ] || continue
    printf '%s|%s|%s\n' "$service" "$bin" "$dir"
  done
}

native_mihomo_select_target() {
  local choice line
  local -a targets=()
  while IFS= read -r line; do
    [ -n "$line" ] && targets+=("$line")
  done < <(native_mihomo_list_targets)

  if [ ${#targets[@]} -eq 0 ]; then
    echo "❌ 未找到由 OpenWrt procd 管理的原生 Mihomo 实例。"
    return 1
  fi

  echo "请选择原生 Mihomo 实例："
  local i service bin dir
  for i in "${!targets[@]}"; do
    IFS='|' read -r service bin dir <<<"${targets[$i]}"
    echo "  $((i + 1))）服务：$(basename "$service")  配置目录：$dir"
  done
  echo "  0）返回"
  read -r -p "请输入要操作的序号: " choice
  [ -n "$choice" ] && [[ "$choice" =~ ^[0-9]+$ ]] || return 2
  [ "$choice" -ge 1 ] && [ "$choice" -le "${#targets[@]}" ] || return 2
  IFS='|' read -r NATIVE_MIHOMO_SERVICE NATIVE_MIHOMO_BIN NATIVE_MIHOMO_DIR <<<"${targets[$((choice - 1))]}"
  return 0
}

native_mihomo_log_event() {
  local dir="$1" message="$2" log body existing tmp
  log="$dir/subscription.log"
  body="$(mktemp "$dir/.subscription-log.XXXXXX")" || return 1
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$message" >"$body"
  existing="$(mktemp "$dir/.subscription-log-existing.XXXXXX")" || { rm -f "$body"; return 1; }

  if [ -f "$log" ]; then
    python3 - "$log" "$existing" <<'PY'
from datetime import date, timedelta
from pathlib import Path
import re, sys
src, dst = map(Path, sys.argv[1:])
cutoff = date.today() - timedelta(days=6)
blocks = re.split(r'\n{2,}', src.read_text(encoding='utf-8', errors='replace').strip())
kept = []
for block in blocks:
    match = re.match(r'^\[(\d{4}-\d{2}-\d{2}) ', block)
    if not match:
        continue
    try:
        if date.fromisoformat(match.group(1)) >= cutoff:
            kept.append(block.strip())
    except ValueError:
        pass
Path(dst).write_text('\n\n'.join(kept) + ('\n' if kept else ''), encoding='utf-8')
PY
  else
    : >"$existing"
  fi

  tmp="$(mktemp "$dir/.subscription-log-new.XXXXXX")" || { rm -f "$body" "$existing"; return 1; }
  cat "$body" >"$tmp"
  if [ -s "$existing" ]; then
    printf '\n' >>"$tmp"
    cat "$existing" >>"$tmp"
  fi
  python3 - "$tmp" "$log" <<'PY'
from pathlib import Path
import re, sys
src, dst = map(Path, sys.argv[1:])
text = src.read_text(encoding='utf-8', errors='replace').strip()
text = re.sub(r'\n{3,}', '\n\n', text)
Path(dst).write_text((text + '\n') if text else '', encoding='utf-8')
PY
  chmod 0600 "$log"
  rm -f "$body" "$existing" "$tmp"
}

native_mihomo_require_python3() {
  command -v python3 >/dev/null 2>&1 && return 0
  install_python3 "原生 Mihomo 订阅日志保留" || return 1
  command -v python3 >/dev/null 2>&1
}

native_mihomo_read_url() {
  local conf="$1"
  sed -n 's/^URL=//p' "$conf" | sed -n '1p'
}

native_mihomo_valid_url() {
  case "$1" in
    http://*|https://*) return 0 ;;
    *) return 1 ;;
  esac
}

native_mihomo_wait_for_process() {
  local bin="$1" process i=0
  process="$(basename "$bin")"
  while [ "$i" -lt 15 ]; do
    pidof "$process" >/dev/null 2>&1 && return 0
    sleep 1
    i=$((i + 1))
  done
  return 1
}

native_mihomo_add_or_replace() {
  local conf existing url tmp
  native_mihomo_select_target || return $?
  native_mihomo_require_python3 || return 1
  conf="$NATIVE_MIHOMO_DIR/subscription.conf"
  existing=""
  [ -f "$conf" ] && existing="$(native_mihomo_read_url "$conf")"

  if [ -n "$existing" ]; then
    read -r -p "订阅 URL（回车保留当前值，不回显）: " url
    url="${url:-$existing}"
  else
    read -r -p "请输入完整 Mihomo 配置订阅 URL: " url
  fi
  native_mihomo_valid_url "$url" || { echo "❌ URL 无效，仅接受 http(s) URL。"; return 1; }

  tmp="$(mktemp "$NATIVE_MIHOMO_DIR/.subscription-conf.XXXXXX")" || return 1
  umask 077
  printf 'URL=%s\n' "$url" >"$tmp"
  chmod 0600 "$tmp" && mv "$tmp" "$conf"
  echo "✅ 已保存订阅配置：$conf"
  echo "ℹ️ 可选择菜单 2 立即下载、校验并应用配置。"
}

native_mihomo_manual_update() {
  local conf url config candidate backup rollback mode lock
  native_mihomo_select_target || return $?
  native_mihomo_require_python3 || return 1
  conf="$NATIVE_MIHOMO_DIR/subscription.conf"
  config="$NATIVE_MIHOMO_DIR/config.yaml"
  backup="$NATIVE_MIHOMO_DIR/config.yaml.backup"
  lock="$NATIVE_MIHOMO_DIR/.subscription.lock"

  [ -r "$conf" ] || { echo "❌ 未配置订阅：$conf"; return 1; }
  url="$(native_mihomo_read_url "$conf")"
  native_mihomo_valid_url "$url" || { echo "❌ subscription.conf 中的 URL 无效。"; return 1; }
  if ! mkdir "$lock" 2>/dev/null; then
    echo "❌ 已有订阅更新任务正在执行。"
    return 1
  fi

  candidate="$(mktemp "$NATIVE_MIHOMO_DIR/.subscription-config.XXXXXX")" || { rmdir "$lock"; return 1; }
  if ! curl --connect-timeout 15 --max-time 120 --fail --location --silent --show-error "$url" -o "$candidate" || [ ! -s "$candidate" ]; then
    rm -f "$candidate"
    rmdir "$lock"
    native_mihomo_log_event "$NATIVE_MIHOMO_DIR" "失败：订阅下载失败，运行中的 config.yaml 未修改。"
    echo "❌ 订阅下载失败，当前配置未修改。"
    return 1
  fi

  if ! "$NATIVE_MIHOMO_BIN" -d "$NATIVE_MIHOMO_DIR" -t -f "$candidate" >/dev/null 2>&1; then
    rm -f "$candidate"
    rmdir "$lock"
    native_mihomo_log_event "$NATIVE_MIHOMO_DIR" "失败：订阅配置未通过 Mihomo 校验，运行中的 config.yaml 未修改。"
    echo "❌ 订阅配置未通过 Mihomo 校验，当前配置未修改。"
    return 1
  fi

  if cmp -s "$candidate" "$config"; then
    rm -f "$candidate"
    rmdir "$lock"
    native_mihomo_log_event "$NATIVE_MIHOMO_DIR" "完成：订阅有效，但 config.yaml 未变化，无需重启 Mihomo。"
    echo "✅ 订阅配置有效且未变化，未替换文件、未重启 Mihomo。"
    return 0
  fi

  if [ ! -e "$backup" ]; then
    cp "$config" "$backup" && chmod 0600 "$backup" || {
      rm -f "$candidate"
      rmdir "$lock"
      native_mihomo_log_event "$NATIVE_MIHOMO_DIR" "失败：无法创建原始 config.yaml 备份，运行中的配置未修改。"
      echo "❌ 无法创建原始配置备份，已取消更新。"
      return 1
    }
  fi

  rollback="$(mktemp "$NATIVE_MIHOMO_DIR/.subscription-previous.XXXXXX")" || {
    rm -f "$candidate"
    rmdir "$lock"
    return 1
  }
  cp "$config" "$rollback" || { rm -f "$candidate" "$rollback"; rmdir "$lock"; return 1; }
  mode="$(stat -c '%a' "$config" 2>/dev/null || printf '600')"
  mv "$candidate" "$config" && chmod "$mode" "$config" || {
    cp "$rollback" "$config"
    chmod "$mode" "$config"
    rm -f "$rollback"
    rmdir "$lock"
    native_mihomo_log_event "$NATIVE_MIHOMO_DIR" "失败：替换 config.yaml 失败，已恢复更新前配置。"
    echo "❌ 替换配置失败，已恢复更新前配置。"
    return 1
  }

  if "$NATIVE_MIHOMO_SERVICE" restart >/dev/null 2>&1 && native_mihomo_wait_for_process "$NATIVE_MIHOMO_BIN"; then
    rm -f "$rollback"
    rmdir "$lock"
    native_mihomo_log_event "$NATIVE_MIHOMO_DIR" "完成：订阅有效，已替换 config.yaml 并重启 Mihomo。"
    echo "✅ 已替换 config.yaml 并重启 Mihomo。"
    return 0
  fi

  cp "$rollback" "$config"
  chmod "$mode" "$config"
  rm -f "$rollback"
  "$NATIVE_MIHOMO_SERVICE" restart >/dev/null 2>&1 || true
  rmdir "$lock"
  native_mihomo_log_event "$NATIVE_MIHOMO_DIR" "失败：新配置重启 Mihomo 失败，已恢复更新前配置并尝试重启。"
  echo "❌ 新配置重启失败，已恢复更新前配置并尝试重启。"
  return 1
}

native_mihomo_delete_subscription() {
  local confirm conf log
  native_mihomo_select_target || return $?
  conf="$NATIVE_MIHOMO_DIR/subscription.conf"
  log="$NATIVE_MIHOMO_DIR/subscription.log"
  [ -e "$conf" ] || { echo "ℹ️ 未配置原生 Mihomo 订阅。"; return 0; }
  read -r -p "确认删除订阅配置和更新日志？不会修改 config.yaml 或重启 Mihomo。[y/N]: " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { echo "ℹ️ 已取消。"; return 0; }
  rm -f "$conf" "$log"
  echo "✅ 已删除 subscription.conf 和 subscription.log；当前 config.yaml 未修改。"
}

native_mihomo_show_status() {
  local conf backup url
  native_mihomo_select_target || return $?
  conf="$NATIVE_MIHOMO_DIR/subscription.conf"
  backup="$NATIVE_MIHOMO_DIR/config.yaml.backup"
  echo "配置目录：$NATIVE_MIHOMO_DIR"
  echo "服务：$(basename "$NATIVE_MIHOMO_SERVICE")"
  if [ -r "$conf" ] && native_mihomo_valid_url "$(native_mihomo_read_url "$conf")"; then
    echo "订阅：已配置（URL 不回显）"
  else
    echo "订阅：未配置"
  fi
  sha256sum "$NATIVE_MIHOMO_DIR/config.yaml" 2>/dev/null || true
  [ -e "$backup" ] && echo "原始配置备份：$backup" || echo "原始配置备份：尚未创建"
}

manage_native_mihomo_subscription_menu() {
  local choice
  [ "$PLATFORM" = "openwrt" ] || { echo "ℹ️ 原生 Mihomo 配置订阅当前仅支持 OpenWrt procd。"; return 0; }
  echo "🔧 原生 Mihomo 完整配置订阅"
  echo "1）添加/修改配置订阅 URL"
  echo "2）立即更新配置"
  echo "3）删除订阅配置和更新日志"
  echo "4）查看订阅状态"
  echo "0）返回"
  read -r -p "请输入要操作的序号: " choice
  case "$choice" in
    1) native_mihomo_add_or_replace ;;
    2) native_mihomo_manual_update ;;
    3) native_mihomo_delete_subscription ;;
    4) native_mihomo_show_status ;;
    0|"") return 0 ;;
    *) echo "❌ 无效选项。"; return 1 ;;
  esac
}
