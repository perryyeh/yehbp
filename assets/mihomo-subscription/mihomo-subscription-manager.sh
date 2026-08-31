# YehBP Mihomo complete-subscription manager. This file is sourced by install.sh.

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

# Remove timers created by YehBP releases before container-internal scheduling.
mihomo_subscription_remove_legacy_timer() {
  local unit_id service timer tag output
  unit_id="$(mihomo_subscription_unit_id "$MIHOMO_SUBSCRIPTION_CONTAINER")"
  service="yehbp-mihomo-subscription-${unit_id}.service"
  timer="yehbp-mihomo-subscription-${unit_id}.timer"
  tag="# yehbp-mihomo-subscription:${unit_id}"

  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now "$timer" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$service" "/etc/systemd/system/$timer"
    output="$(systemctl daemon-reload 2>&1)" || {
      echo "⚠️ 旧 systemd 定时任务已移除，但 daemon-reload 失败：$output"
      return 1
    }
  fi
  if [ -f /etc/crontabs/root ]; then
    grep -F -v "$tag" /etc/crontabs/root > /etc/crontabs/root.yehbp.tmp || true
    mv /etc/crontabs/root.yehbp.tmp /etc/crontabs/root
    /etc/init.d/cron restart >/dev/null 2>&1 || true
  elif command -v crontab >/dev/null 2>&1; then
    (crontab -l 2>/dev/null || true) | grep -F -v "$tag" | crontab - || true
  fi
}

mihomo_subscription_install_script() {
  local script="$MIHOMO_SUBSCRIPTION_DIR/config.subscription.update.sh"
  download_yehbp_asset "assets/mihomo-subscription/config.subscription.update.sh" "$script" || return 1
  chmod 0700 "$script"
  sh -n "$script"
}

# New Mihomo installs already contain this file. Fetch it only for deployments
# created before the overlay template existed; never overwrite local customizations.
mihomo_subscription_install_replace_template() {
  local replace="$MIHOMO_SUBSCRIPTION_DIR/config.replace.yaml" tmp
  [ -r "$replace" ] && return 0
  tmp="$(mktemp "$MIHOMO_SUBSCRIPTION_DIR/.config.replace.XXXXXX")" || return 1
  if ! yehbp_curl --connect-timeout 10 --max-time 60 -fsSL \
      "https://raw.githubusercontent.com/perryyeh/mihomo/main/config.replace.yaml" -o "$tmp"; then
    rm -f "$tmp"
    echo "❌ 无法下载 Mihomo config.replace.yaml；请重试。"
    return 1
  fi
  if ! python3 - "$tmp" <<'PY'
from pathlib import Path
import sys, yaml
value = yaml.safe_load(Path(sys.argv[1]).read_text(encoding='utf-8'))
if not isinstance(value, dict):
    raise SystemExit('config.replace.yaml 根节点必须是 YAML mapping。')
PY
  then
    rm -f "$tmp"
    echo "❌ 下载的 config.replace.yaml 无效。"
    return 1
  fi
  chmod 0600 "$tmp" && mv "$tmp" "$replace"
}

mihomo_subscription_runtime_ready() {
  if ! docker inspect -f '{{.State.Running}}' "$MIHOMO_SUBSCRIPTION_CONTAINER" 2>/dev/null | grep -qx true; then
    echo "❌ Mihomo 容器未运行。"
    return 1
  fi
  docker exec "$MIHOMO_SUBSCRIPTION_CONTAINER" sh -c '
    test -x /root/.config/mihomo/entrypoint.sh &&
    command -v curl >/dev/null &&
    command -v python3 >/dev/null &&
    python3 -c "import yaml"
  ' >/dev/null 2>&1
}

# Convert a pre-container-scheduler YehBP macvlan deployment in place.  This
# preserves config.yaml, .env, container name, network fields and all other
# Compose settings; only the legacy image stanza becomes the current local
# runtime.  The original Compose file is restored and brought back up if the
# rebuilt container is not healthy.
mihomo_subscription_enable_runtime() {
  local confirm dir compose_file compose_backup compose_candidate stage dockerfile entrypoint
  local -a compose

  mihomo_subscription_runtime_ready && return 0
  if ! docker inspect -f '{{.State.Running}}' "$MIHOMO_SUBSCRIPTION_CONTAINER" 2>/dev/null | grep -qx true; then
    echo "❌ Mihomo 容器未运行，无法配置自动更新。"
    return 1
  fi

  dir="$MIHOMO_SUBSCRIPTION_DIR"
  compose_file="$dir/docker-compose.yml"
  [ -f "$compose_file" ] || {
    echo "❌ 未找到该安装目录的 docker-compose.yml，无法安全配置自动更新。"
    return 1
  }
  if grep -Eq '^[[:space:]]+build:' "$compose_file"; then
    echo "❌ 当前实例已有自定义 build 运行时，但不具备订阅自动更新依赖；为避免覆盖自定义镜像，未修改。"
    return 1
  fi
  if [ -e "$dir/Dockerfile" ] || [ -e "$dir/entrypoint.sh" ]; then
    echo "❌ 当前实例含有未识别的 Dockerfile 或 entrypoint.sh；为避免覆盖本地自定义文件，未修改。"
    return 1
  fi

  echo "⚠️ 当前 Mihomo 未配置容器内订阅自动更新。"
  echo "将保留安装目录和现有配置，仅更新该实例的运行时并重建容器以启用自动更新。"
  read -r -p "现在配置自动更新，并在成功后继续添加外部订阅？[y/N]: " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { echo "ℹ️ 未配置自动更新，未添加外部订阅。"; return 1; }

  if docker compose version >/dev/null 2>&1; then
    compose=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    compose=(docker-compose)
  else
    echo "❌ 未找到 docker compose / docker-compose，无法配置自动更新。"
    return 1
  fi

  compose_backup="$(mktemp "$dir/.docker-compose.before-subscription-runtime.XXXXXX")" || return 1
  compose_candidate="$(mktemp "$dir/.docker-compose.subscription-runtime.XXXXXX")" || {
    rm -f "$compose_backup"
    return 1
  }
  stage="$(mktemp -d "$dir/.mihomo-subscription-runtime.XXXXXX")" || {
    rm -f "$compose_backup" "$compose_candidate"
    return 1
  }
  dockerfile="$stage/Dockerfile"
  entrypoint="$stage/entrypoint.sh"
  cp "$compose_file" "$compose_backup" || { rm -rf "$stage"; rm -f "$compose_backup" "$compose_candidate"; return 1; }

  if ! yehbp_curl --connect-timeout 10 --max-time 60 -fsSL \
      "https://raw.githubusercontent.com/perryyeh/mihomo/main/Dockerfile" -o "$dockerfile" || \
     ! yehbp_curl --connect-timeout 10 --max-time 60 -fsSL \
      "https://raw.githubusercontent.com/perryyeh/mihomo/main/entrypoint.sh" -o "$entrypoint" || \
     ! grep -qx 'FROM metacubex/mihomo:latest' "$dockerfile" || ! sh -n "$entrypoint"; then
    echo "❌ 自动更新运行时下载或校验失败，未修改该实例。"
    rm -rf "$stage"
    rm -f "$compose_backup" "$compose_candidate"
    return 1
  fi

  # Legacy YehBP templates have one service image stanza. Replace it with the
  # current build/image/entrypoint fields, removing a legacy entrypoint if any.
  if ! awk '
    /^[[:space:]]+entrypoint:[[:space:]]*/ { next }
    /^[[:space:]]+image:[[:space:]]*/ && !done {
      match($0, /^[[:space:]]*/)
      indent = substr($0, RSTART, RLENGTH)
      print indent "build:"
      print indent "  context: ."
      print indent "  dockerfile: Dockerfile"
      print indent "image: yehbp-mihomo:latest"
      print indent "entrypoint: [\"/root/.config/mihomo/entrypoint.sh\"]"
      done = 1
      next
    }
    { print }
    END { if (!done) exit 1 }
  ' "$compose_file" >"$compose_candidate"; then
    echo "❌ 无法识别旧版 Compose 的 image 字段，未修改该实例。"
    rm -rf "$stage"
    rm -f "$compose_backup" "$compose_candidate"
    return 1
  fi

  if ! (cd "$dir" && "${compose[@]}" -f "$compose_candidate" config >/dev/null); then
    echo "❌ 自动更新 Compose 校验失败，未修改该实例。"
    rm -rf "$stage"
    rm -f "$compose_backup" "$compose_candidate"
    return 1
  fi

  mv "$dockerfile" "$dir/Dockerfile" && chmod 0644 "$dir/Dockerfile" && \
    mv "$entrypoint" "$dir/entrypoint.sh" && chmod 0755 "$dir/entrypoint.sh" && \
    mv "$compose_candidate" "$compose_file" || {
      echo "❌ 写入自动更新运行时失败，正在恢复原 Compose。"
      cp "$compose_backup" "$compose_file" || true
      rm -rf "$stage"
      rm -f "$compose_backup" "$compose_candidate"
      return 1
    }
  rm -rf "$stage"

  echo "🔄 正在重建 Mihomo 容器以启用自动更新…"
  if (cd "$dir" && "${compose[@]}" -f "$compose_file" up -d --build) && \
     mihomo_subscription_wait_for_mihomo && mihomo_subscription_runtime_ready; then
    rm -f "$compose_backup"
    echo "✅ 已配置容器内自动更新。"
    return 0
  fi

  echo "❌ 自动更新运行时未能正常启动，正在恢复原 Compose 和容器。"
  cp "$compose_backup" "$compose_file" && (cd "$dir" && "${compose[@]}" -f "$compose_file" up -d) || \
    echo "⚠️ 原 Compose 自动恢复失败，请检查：$compose_file"
  rm -f "$dir/Dockerfile" "$dir/entrypoint.sh" "$compose_backup" "$compose_candidate"
  return 1
}

mihomo_subscription_wait_for_mihomo() {
  local i
  for i in $(seq 1 30); do
    docker inspect -f '{{.State.Running}}' "$MIHOMO_SUBSCRIPTION_CONTAINER" 2>/dev/null | grep -qx true && return 0
    sleep 1
  done
  echo "❌ Mihomo 容器未在 30 秒内恢复运行。"
  return 1
}

mihomo_subscription_run_update() {
  docker exec -e MIHOMO_WAIT_RELOAD=1 "$MIHOMO_SUBSCRIPTION_CONTAINER" \
    /root/.config/mihomo/config.subscription.update.sh --once
}

mihomo_subscription_add_or_replace() {
  local url hours backup conf
  mihomo_subscription_select_target || return $?
  mihomo_subscription_enable_runtime || return 1
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
    chmod 0600 "$backup"
    echo "✅ 已保存本地 macvlan 配置备份：$backup"
  fi
  mihomo_subscription_install_script || return 1
  mihomo_subscription_install_replace_template || return 1
  umask 077
  printf 'URL=%q\nINTERVAL_HOURS=%s\n' "$url" "$hours" >"$conf"
  chmod 0600 "$conf"

  echo "🔎 正在由 Mihomo 容器下载、修补并验证订阅…"
  if ! mihomo_subscription_run_update; then
    echo "❌ 首次更新失败；已保留当前运行配置，未启用容器内定时更新。请查看：$MIHOMO_SUBSCRIPTION_DIR/config.subscription.update.log"
    return 1
  fi
  mihomo_subscription_remove_legacy_timer || return 1
  echo "✅ 已启用容器内定时更新：每 ${hours} 小时一次。"
}

mihomo_subscription_manual_update() {
  mihomo_subscription_select_target || return $?
  [ -f "$MIHOMO_SUBSCRIPTION_DIR/config.subscription.conf" ] || { echo "❌ 未配置外部订阅。"; return 1; }
  mihomo_subscription_runtime_ready || return 1
  mihomo_subscription_install_script || return 1
  mihomo_subscription_install_replace_template || return 1
  mihomo_subscription_run_update
}

mihomo_subscription_delete() {
  local confirm dir backup
  mihomo_subscription_select_target || return $?
  mihomo_subscription_runtime_ready || return 1
  dir="$MIHOMO_SUBSCRIPTION_DIR"
  backup="$dir/config.macvlan.backup.yaml"
  [ -f "$backup" ] || { echo "❌ 未找到 $backup，拒绝删除订阅以免无法恢复。"; return 1; }
  read -r -p "确认删除外部订阅、恢复本地 macvlan 配置并重载 Mihomo？[y/N]: " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { echo "ℹ️ 已取消。"; return 0; }
  if ! docker exec -e MIHOMO_WAIT_RELOAD=1 "$MIHOMO_SUBSCRIPTION_CONTAINER" \
      /root/.config/mihomo/config.subscription.update.sh --restore; then
    echo "❌ 本地 macvlan 备份未通过当前 Mihomo 校验或重载失败，未删除订阅。"
    return 1
  fi
  mihomo_subscription_remove_legacy_timer || return 1
  rm -f "$dir/config.subscription.conf" "$dir/config.subscription.update.sh" "$dir/config.subscription.update.log" "$backup"
  echo "✅ 已恢复本地 macvlan 配置、重载 Mihomo，并删除外部订阅。"
}

mihomo_subscription_show_log() {
  mihomo_subscription_select_target || return $?
  local log="$MIHOMO_SUBSCRIPTION_DIR/config.subscription.update.log"
  [ -f "$log" ] || { echo "ℹ️ 暂无订阅更新日志。"; return 0; }
  printf '%s\n' "----- $log（最新在前）-----"
  cat "$log"
}

manage_mihomo_subscription() {
  echo "🔧 Mihomo 外部完整订阅配置（仅 macvlan；容器内更新）"
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
