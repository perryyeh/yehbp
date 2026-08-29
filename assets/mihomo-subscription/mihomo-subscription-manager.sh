# YehBP Mihomo complete-subscription manager. This file is sourced by install.sh.

mihomo_subscription_ensure_python_yaml() {
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then
    return 0
  fi
  echo "⬇️ 外部完整订阅需要 python3 与 PyYAML，正在安装…"
  if is_openwrt; then
    opkg update && opkg install python3 python3-yaml
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y python3 python3-yaml
  elif [ -x /opt/bin/opkg ]; then
    export PATH=/opt/bin:$PATH
    /opt/bin/opkg update && /opt/bin/opkg install python3 py3-yaml
  else
    echo "❌ 未识别的系统；请安装 python3 和 PyYAML 后重试。"
    return 1
  fi
  python3 -c 'import yaml' >/dev/null 2>&1 || {
    echo "❌ python3-yaml 安装后仍无法 import yaml。"
    return 1
  }
}

mihomo_subscription_list_targets() {
  local id name image dir
  while IFS='|' read -r id name image; do
    [[ "$image" == *mihomo* || "$name" == *mihomo* ]] || continue
    dir="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/root/.config/mihomo"}}{{.Source}}{{end}}{{end}}' "$id" 2>/dev/null || true)"
    [ -n "$dir" ] && [ -d "$dir" ] && [ -f "$dir/config.yaml" ] || continue
    # YehBP's macvlan install directory retains this source template.
    [ -f "$dir/config.macvlan.yaml" ] || continue
    printf '%s|%s|%s\n' "$name" "$dir" "$image"
  done < <(docker ps -a --format '{{.ID}}|{{.Names}}|{{.Image}}')
}

mihomo_subscription_select_target() {
  local choice line
  local -a targets=()
  while IFS= read -r line; do targets+=("$line"); done < <(mihomo_subscription_list_targets)
  if [ ${#targets[@]} -eq 0 ]; then
    echo "❌ 未找到 YehBP 安装的 macvlan Mihomo 容器。"
    return 1
  fi
  echo "请选择要配置的 macvlan Mihomo 安装目录/容器："
  local i name dir image
  for i in "${!targets[@]}"; do
    IFS='|' read -r name dir image <<<"${targets[$i]}"
    echo "  $((i + 1))）容器：$name  目录：$dir"
  done
  echo "  0）返回"
  read -r -p "请输入要操作的序号: " choice
  [ -n "$choice" ] && [[ "$choice" =~ ^[0-9]+$ ]] || return 2
  [ "$choice" -ge 1 ] && [ "$choice" -le "${#targets[@]}" ] || return 2
  IFS='|' read -r MIHOMO_SUBSCRIPTION_CONTAINER MIHOMO_SUBSCRIPTION_DIR _ <<<"${targets[$((choice - 1))]}"
  return 0
}

mihomo_subscription_unit_id() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '_'
}

mihomo_subscription_timer_paths() {
  local unit_id
  unit_id="$(mihomo_subscription_unit_id "$MIHOMO_SUBSCRIPTION_CONTAINER")"
  MIHOMO_SUBSCRIPTION_SERVICE="yehbp-mihomo-subscription-${unit_id}.service"
  MIHOMO_SUBSCRIPTION_TIMER="yehbp-mihomo-subscription-${unit_id}.timer"
  MIHOMO_SUBSCRIPTION_CRON_TAG="# yehbp-mihomo-subscription:${unit_id}"
}

mihomo_subscription_remove_timer() {
  mihomo_subscription_timer_paths
  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now "$MIHOMO_SUBSCRIPTION_TIMER" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$MIHOMO_SUBSCRIPTION_SERVICE" "/etc/systemd/system/$MIHOMO_SUBSCRIPTION_TIMER"
    systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  if [ -f /etc/crontabs/root ]; then
    grep -F -v "$MIHOMO_SUBSCRIPTION_CRON_TAG" /etc/crontabs/root > /etc/crontabs/root.yehbp.tmp || true
    mv /etc/crontabs/root.yehbp.tmp /etc/crontabs/root
    /etc/init.d/cron restart >/dev/null 2>&1 || true
  elif command -v crontab >/dev/null 2>&1; then
    (crontab -l 2>/dev/null || true) | grep -F -v "$MIHOMO_SUBSCRIPTION_CRON_TAG" | crontab -
  fi
}

mihomo_subscription_install_timer() {
  local hours="$1" script="$MIHOMO_SUBSCRIPTION_DIR/config.subscription.update.sh"
  mihomo_subscription_timer_paths
  if command -v systemctl >/dev/null 2>&1; then
    cat >"/etc/systemd/system/$MIHOMO_SUBSCRIPTION_SERVICE" <<EOF
[Unit]
Description=YehBP Mihomo subscription update (${MIHOMO_SUBSCRIPTION_CONTAINER})
After=docker.service

[Service]
Type=oneshot
Environment=MIHOMO_CONTAINER_NAME=${MIHOMO_SUBSCRIPTION_CONTAINER}
ExecStart=${script} --scheduled
EOF
    cat >"/etc/systemd/system/$MIHOMO_SUBSCRIPTION_TIMER" <<EOF
[Unit]
Description=YehBP Mihomo subscription update timer (${MIHOMO_SUBSCRIPTION_CONTAINER})

[Timer]
OnBootSec=10min
OnUnitActiveSec=${hours}h
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemd-analyze verify "/etc/systemd/system/$MIHOMO_SUBSCRIPTION_SERVICE" "/etc/systemd/system/$MIHOMO_SUBSCRIPTION_TIMER" || return 1
    systemctl daemon-reload && systemctl enable --now "$MIHOMO_SUBSCRIPTION_TIMER" || return 1
    echo "✅ 已创建 systemd 定时任务：每 ${hours} 小时更新一次。"
    return 0
  fi

  # BusyBox/OpenWrt crond accepts the standard step expression. The task runs
  # at minute 0 every N hours; it remains entirely host-side and changes no compose.
  local cron_line="0 */${hours} * * * MIHOMO_CONTAINER_NAME=$(printf '%q' "$MIHOMO_SUBSCRIPTION_CONTAINER") $(printf '%q' "$script") --scheduled ${MIHOMO_SUBSCRIPTION_CRON_TAG}"
  if [ -f /etc/crontabs/root ]; then
    grep -F -v "$MIHOMO_SUBSCRIPTION_CRON_TAG" /etc/crontabs/root > /etc/crontabs/root.yehbp.tmp || true
    printf '%s\n' "$cron_line" >> /etc/crontabs/root.yehbp.tmp
    mv /etc/crontabs/root.yehbp.tmp /etc/crontabs/root
    /etc/init.d/cron restart >/dev/null 2>&1 || true
    echo "✅ 已创建 crond 定时任务：每 ${hours} 小时更新一次。"
    return 0
  fi
  if command -v crontab >/dev/null 2>&1; then
    ((crontab -l 2>/dev/null || true) | grep -F -v "$MIHOMO_SUBSCRIPTION_CRON_TAG"; printf '%s\n' "$cron_line") | crontab - || return 1
    echo "✅ 已创建 crontab 定时任务：每 ${hours} 小时更新一次。"
    return 0
  fi
  echo "❌ 未检测到 systemd 或 crond/crontab，无法创建自动更新任务。"
  return 1
}

mihomo_subscription_install_script() {
  local script="$MIHOMO_SUBSCRIPTION_DIR/config.subscription.update.sh"
  download_yehbp_asset "assets/mihomo-subscription/config.subscription.update.sh" "$script" || return 1
  chmod 0755 "$script"
  bash -n "$script"
}

mihomo_subscription_run_update() {
  MIHOMO_CONTAINER_NAME="$MIHOMO_SUBSCRIPTION_CONTAINER" \
    "$MIHOMO_SUBSCRIPTION_DIR/config.subscription.update.sh"
}

mihomo_subscription_add_or_replace() {
  local url hours backup conf
  mihomo_subscription_select_target || return $?
  mihomo_subscription_ensure_python_yaml || return 1
  backup="$MIHOMO_SUBSCRIPTION_DIR/config.macvlan.backup.yaml"
  conf="$MIHOMO_SUBSCRIPTION_DIR/config.subscription.conf"

  if [ -f "$conf" ]; then
    echo "当前已配置外部完整订阅（URL 为敏感信息，不显示）。"
  fi
  read -r -p "请输入完整 Mihomo YAML 订阅 URL: " url
  [[ "$url" =~ ^https?://[^[:space:]\']+$ ]] || { echo "❌ URL 无效，仅接受不含空格或单引号的 http(s) URL。"; return 1; }
  read -r -p "更新间隔（小时，1-23；回车默认 8）: " hours
  hours="${hours:-8}"
  [[ "$hours" =~ ^[0-9]+$ ]] && [ "$hours" -ge 1 ] && [ "$hours" -le 23 ] || { echo "❌ 更新间隔必须是 1-23 小时。"; return 1; }

  if [ ! -f "$backup" ]; then
    cp "$MIHOMO_SUBSCRIPTION_DIR/config.yaml" "$backup" || return 1
    echo "✅ 已保存本地 macvlan 配置备份：$backup"
  fi
  mihomo_subscription_install_script || return 1
  umask 077
  printf 'URL=%q\nINTERVAL_HOURS=%s\n' "$url" "$hours" >"$conf"
  chmod 0600 "$conf"

  echo "🔎 正在下载、修补并验证订阅…"
  if ! mihomo_subscription_run_update; then
    echo "❌ 首次更新失败；已保留当前运行配置，未启用定时任务。请查看：$MIHOMO_SUBSCRIPTION_DIR/config.subscription.update.log"
    return 1
  fi
  mihomo_subscription_remove_timer
  mihomo_subscription_install_timer "$hours" || {
    echo "⚠️ 首次订阅更新已成功，但自动定时任务创建失败；可通过菜单手动更新。"
    return 1
  }
}

mihomo_subscription_manual_update() {
  mihomo_subscription_select_target || return $?
  [ -f "$MIHOMO_SUBSCRIPTION_DIR/config.subscription.conf" ] || { echo "❌ 未配置外部订阅。"; return 1; }
  mihomo_subscription_ensure_python_yaml || return 1
  mihomo_subscription_install_script || return 1
  mihomo_subscription_run_update
}

mihomo_subscription_delete() {
  local current tmp confirm
  mihomo_subscription_select_target || return $?
  local dir="$MIHOMO_SUBSCRIPTION_DIR" backup="$MIHOMO_SUBSCRIPTION_DIR/config.macvlan.backup.yaml"
  [ -f "$backup" ] || { echo "❌ 未找到 $backup，拒绝删除订阅以免无法恢复。"; return 1; }
  read -r -p "确认删除外部订阅、恢复本地 macvlan 配置并重启 Mihomo？[y/N]: " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { echo "ℹ️ 已取消。"; return 0; }
  current="$(mktemp "$dir/.config.subscription.current.XXXXXX")"
  tmp="$(mktemp "$dir/.config.subscription.restore.XXXXXX.yaml")"
  cp "$dir/config.yaml" "$current"
  cp "$backup" "$tmp"
  if ! docker cp "$tmp" "$MIHOMO_SUBSCRIPTION_CONTAINER:/tmp/config.subscription.restore.yaml" >/dev/null || \
     ! docker exec "$MIHOMO_SUBSCRIPTION_CONTAINER" /mihomo -t -f /tmp/config.subscription.restore.yaml >/dev/null 2>&1; then
    docker exec "$MIHOMO_SUBSCRIPTION_CONTAINER" rm -f /tmp/config.subscription.restore.yaml >/dev/null 2>&1 || true
    rm -f "$current" "$tmp"
    echo "❌ 本地 macvlan 备份未通过当前 Mihomo 校验，未恢复。"
    return 1
  fi
  docker exec "$MIHOMO_SUBSCRIPTION_CONTAINER" rm -f /tmp/config.subscription.restore.yaml >/dev/null 2>&1 || true
  mv "$tmp" "$dir/config.yaml"
  if ! docker restart "$MIHOMO_SUBSCRIPTION_CONTAINER" >/dev/null || ! docker inspect -f '{{.State.Running}}' "$MIHOMO_SUBSCRIPTION_CONTAINER" | grep -qx true; then
    mv "$current" "$dir/config.yaml"
    docker restart "$MIHOMO_SUBSCRIPTION_CONTAINER" >/dev/null 2>&1 || true
    echo "❌ 恢复后的 Mihomo 未正常运行，已回滚订阅配置。"
    return 1
  fi
  rm -f "$current"
  mihomo_subscription_remove_timer
  rm -f "$dir/config.subscription.conf" "$dir/config.subscription.update.sh" "$dir/config.subscription.update.log" "$backup"
  echo "✅ 已恢复本地 macvlan 配置、重启 Mihomo，并删除外部订阅及其定时任务。"
}

mihomo_subscription_show_log() {
  mihomo_subscription_select_target || return $?
  local log="$MIHOMO_SUBSCRIPTION_DIR/config.subscription.update.log"
  [ -f "$log" ] || { echo "ℹ️ 暂无订阅更新日志。"; return 0; }
  printf '%s\n' "----- $log（最新在前）-----"
  cat "$log"
}

manage_mihomo_subscription() {
  echo "🔧 Mihomo 外部完整订阅配置（仅 macvlan）"
  echo "1）添加/修改外部订阅（默认每 8 小时更新）"
  echo "2）立即更新外部订阅"
  echo "3）删除外部订阅并恢复本地 macvlan 配置"
  echo "4）查看订阅更新日志"
  echo "0）返回"
  local choice
  read -r -p "请输入要操作的序号: " choice
  case "$choice" in
    1) mihomo_subscription_add_or_replace ;;
    2) mihomo_subscription_manual_update ;;
    3) mihomo_subscription_delete ;;
    4) mihomo_subscription_show_log ;;
    0|"") return 0 ;;
    *) echo "❌ 无效选项。"; return 1 ;;
  esac
}
