#!/usr/bin/env bash
# Loaded by YehBP menu 80. Requires the parent script's download_yehbp_asset
# and select_dockerapps_dir helpers.

RTP2HTTPD_SERVICE="yehbp-rtp2httpd.service"
RTP2HTTPD_RELEASE_API="https://api.github.com/repos/stackia/rtp2httpd/releases/latest"

rtp2httpd_require_commands() {
    local missing=() cmd
    for cmd in curl nmcli systemctl ip sha256sum uname python3; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [ ${#missing[@]} -ne 0 ]; then
        echo "❌ rtp2httpd 需要以下命令：${missing[*]}"
        echo "   当前功能仅支持使用 NetworkManager（nmcli）的 systemd 主机。"
        return 1
    fi
}

rtp2httpd_is_ipv4() {
    local ip="$1" a b c d
    IFS=. read -r a b c d <<< "$ip"
    [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ && "$c" =~ ^[0-9]+$ && "$d" =~ ^[0-9]+$ ]] || return 1
    ((a <= 255 && b <= 255 && c <= 255 && d <= 255))
}

rtp2httpd_is_local_ipv4() {
    local wanted="$1"
    ip -4 -o addr show | awk -v ip="$wanted" '$4 ~ ("^" ip "/") { found=1 } END { exit !found }'
}

rtp2httpd_select_parent() {
    local -a interfaces=()
    local iface choice
    while IFS= read -r iface; do
        iface="${iface%%@*}"
        [ "$iface" = "lo" ] || interfaces+=("$iface")
    done < <(ip -o link show | awk -F ': ' '{print $2}' | awk '!seen[$0]++')

    [ ${#interfaces[@]} -gt 0 ] || { echo "❌ 未找到可用网卡。"; return 1; }
    echo "请选择承载 IPTV VLAN 的父网卡："
    for i in "${!interfaces[@]}"; do
        printf '  %d) %s\n' "$((i + 1))" "${interfaces[$i]}"
    done
    read -r -p "网卡序号（回车退出）: " choice
    [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#interfaces[@]}" ] || return 2
    RTP2HTTPD_PARENT_IF="${interfaces[$((choice - 1))]}"
}

rtp2httpd_prompt_vlan() {
    local prompt="$1" value
    while true; do
        read -r -p "$prompt" value
        [ -n "$value" ] || return 2
        if [[ "$value" =~ ^[0-9]+$ ]] && [ "$value" -ge 1 ] && [ "$value" -le 4094 ]; then
            printf '%s\n' "$value"
            return 0
        fi
        echo "❌ VLAN ID 必须是 1–4094。"
    done
}

rtp2httpd_prompt_bind() {
    local value port
    while true; do
        read -r -p "请输入本机已配置的 IPv4 监听地址（回车退出）: " value
        [ -n "$value" ] || return 2
        if rtp2httpd_is_ipv4 "$value" && rtp2httpd_is_local_ipv4 "$value"; then
            RTP2HTTPD_BIND_ADDRESS="$value"
            break
        fi
        echo "❌ 该地址不是本机当前已配置的 IPv4 地址：$value"
    done
    while true; do
        read -r -p "请输入监听端口（默认 5140，回车使用默认）: " port
        port="${port:-5140}"
        if [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ]; then
            RTP2HTTPD_PORT="$port"
            return 0
        fi
        echo "❌ 端口必须是 1–65535。"
    done
}

rtp2httpd_arch_asset() {
    case "$(uname -m)" in
        x86_64|amd64) printf '%s\n' 'x86_64' ;;
        aarch64|arm64) printf '%s\n' 'aarch64' ;;
        armv7l|armv7) printf '%s\n' 'armv7-eabihf' ;;
        armv6l|armv6) printf '%s\n' 'arm-eabihf' ;;
        *) echo "❌ 不支持的 CPU 架构：$(uname -m)" >&2; return 1 ;;
    esac
}

rtp2httpd_download_binary() {
    local app_dir="$1" tmp_json tmp_bin suffix release_tag url digest expected actual
    suffix="$(rtp2httpd_arch_asset)" || return 1
    tmp_json="$(mktemp)" || return 1
    tmp_bin="${app_dir}/.rtp2httpd.download"
    trap 'rm -f "$tmp_json" "$tmp_bin"' RETURN

    curl --connect-timeout 10 --max-time 60 -fsSL "$RTP2HTTPD_RELEASE_API" -o "$tmp_json" || {
        echo "❌ 无法读取 rtp2httpd 官方 release 信息。"
        return 1
    }
    read -r release_tag url digest < <(python3 - "$tmp_json" "$suffix" <<'PY'
import json
import re
import sys
release = json.load(open(sys.argv[1]))
suffix = re.escape(sys.argv[2])
pattern = re.compile(r"^rtp2httpd-[0-9]+(?:\.[0-9]+)+-" + suffix + r"$")
for item in release.get("assets", []):
    if pattern.fullmatch(item.get("name", "")):
        print(release.get("tag_name", "unknown"), item.get("browser_download_url", ""), item.get("digest", ""))
        break
PY
)
    expected="${digest#sha256:}"
    if [ -z "$url" ] || [[ ! "$expected" =~ ^[0-9a-fA-F]{64}$ ]]; then
        echo "❌ 官方 release 未提供 Linux ${suffix} 二进制或 SHA-256 digest，已拒绝下载。"
        return 1
    fi
    curl --connect-timeout 10 --max-time 120 -fL "$url" -o "$tmp_bin" || {
        echo "❌ rtp2httpd 二进制下载失败。"
        return 1
    }
    actual="$(sha256sum "$tmp_bin" | awk '{print $1}')"
    if [ "$actual" != "$expected" ]; then
        echo "❌ 二进制 SHA-256 不匹配，已拒绝安装。"
        return 1
    fi
    install -m 0755 "$tmp_bin" "${app_dir}/.rtp2httpd.verified"
    mv -f "${app_dir}/.rtp2httpd.verified" "${app_dir}/rtp2httpd"
    printf '%s\n' "${release_tag:-unknown}" > "${app_dir}/VERSION"
}

rtp2httpd_render_template() {
    local src="$1" dst="$2"
    shift 2
    python3 - "$src" "$dst" "$@" <<'PY'
import sys
from pathlib import Path
src, dst, *pairs = sys.argv[1:]
text = Path(src).read_text()
for pair in pairs:
    key, value = pair.split("=", 1)
    text = text.replace(key, value)
Path(dst).write_text(text)
PY
}

rtp2httpd_remove_managed() {
    local app_dir="$1" state uuid
    state="${app_dir}/install.env"
    if [ -r "$state" ]; then
        # install.env is generated by this script and contains shell-safe values only.
        # shellcheck disable=SC1090
        . "$state"
        for uuid in "${MULTICAST_PROFILE_UUID:-}" "${FCC_PROFILE_UUID:-}"; do
            [ -n "$uuid" ] && nmcli connection delete uuid "$uuid" >/dev/null 2>&1 || true
        done
    fi
    systemctl disable --now "$RTP2HTTPD_SERVICE" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${RTP2HTTPD_SERVICE}"
    systemctl daemon-reload
}

rtp2httpd_write_state() {
    local app_dir="$1"
    cat > "${app_dir}/install.env" <<EOF
MANAGED_BY=yehbp-rtp2httpd
PARENT_IF=${RTP2HTTPD_PARENT_IF}
MULTICAST_VLAN=${RTP2HTTPD_MULTICAST_VLAN}
FCC_VLAN=${RTP2HTTPD_FCC_VLAN}
BIND_ADDRESS=${RTP2HTTPD_BIND_ADDRESS}
PORT=${RTP2HTTPD_PORT}
MULTICAST_PROFILE_UUID=${RTP2HTTPD_MULTICAST_PROFILE_UUID}
FCC_PROFILE_UUID=${RTP2HTTPD_FCC_PROFILE_UUID}
EOF
    chmod 0600 "${app_dir}/install.env"
}

rtp2httpd_install() {
    local dockerapps app_dir stage_dir mcast_name fcc_name answer
    rtp2httpd_require_commands || return 1
    select_dockerapps_dir "rtp2httpd IPTV 组播转 HTTP 单播"
    case $? in 0) dockerapps="$SELECTED_DOCKERAPPS_DIR" ;; 2) return 0 ;; *) return 1 ;; esac
    app_dir="${dockerapps}/rtp2httpd"

    if [ -e "$app_dir" ]; then
        read -r -p "检测到 ${app_dir}，安装将替换其内容及 YehBP 管理的 VLAN profile。继续？[y/N]: " answer
        [[ "$answer" =~ ^[yY]([eE][sS])?$ ]] || return 0
        rtp2httpd_remove_managed "$app_dir"
        rm -rf "$app_dir"
    fi

    rtp2httpd_select_parent
    case $? in 0) ;; 2) return 0 ;; *) return 1 ;; esac
    RTP2HTTPD_MULTICAST_VLAN="$(rtp2httpd_prompt_vlan '请输入组播 VLAN ID（回车退出）: ')" || return 0
    RTP2HTTPD_FCC_VLAN="$(rtp2httpd_prompt_vlan '请输入 FCC/DHCP VLAN ID（回车退出）: ')" || return 0
    if [ "$RTP2HTTPD_MULTICAST_VLAN" = "$RTP2HTTPD_FCC_VLAN" ]; then
        echo "❌ 组播和 FCC/DHCP VLAN ID 必须不同。"
        return 1
    fi
    rtp2httpd_prompt_bind || return 0

    mkdir -p "$dockerapps" || return 1
    stage_dir="${dockerapps}/.rtp2httpd.stage.$$"
    mkdir -p "$stage_dir" || return 1
    trap 'rm -rf "$stage_dir"' RETURN

    mcast_name="yehbp-rtp2httpd-mcast-${RTP2HTTPD_PARENT_IF}-${RTP2HTTPD_MULTICAST_VLAN}"
    fcc_name="yehbp-rtp2httpd-fcc-${RTP2HTTPD_PARENT_IF}-${RTP2HTTPD_FCC_VLAN}"
    if nmcli connection show "$mcast_name" >/dev/null 2>&1 || nmcli connection show "$fcc_name" >/dev/null 2>&1; then
        echo "❌ 同名 NetworkManager profile 已存在；为避免覆盖未知配置，已取消。"
        return 1
    fi

    rtp2httpd_download_binary "$stage_dir" || return 1
    download_yehbp_asset "assets/iptv/rtp2httpd.conf.tpl" "${stage_dir}/rtp2httpd.conf.tpl" || return 1
    download_yehbp_asset "assets/iptv/rtp2httpd.service.tpl" "${stage_dir}/rtp2httpd.service.tpl" || return 1

    if ! nmcli connection add type vlan con-name "$mcast_name" ifname "${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_MULTICAST_VLAN}" dev "$RTP2HTTPD_PARENT_IF" id "$RTP2HTTPD_MULTICAST_VLAN" >/dev/null; then
        echo "❌ 无法创建组播 VLAN profile。"
        return 1
    fi
    RTP2HTTPD_MULTICAST_PROFILE_UUID="$(nmcli -g connection.uuid connection show "$mcast_name")"
    if ! nmcli connection modify uuid "$RTP2HTTPD_MULTICAST_PROFILE_UUID" connection.autoconnect yes ipv4.method disabled ipv4.never-default yes ipv6.method disabled; then
        nmcli connection delete uuid "$RTP2HTTPD_MULTICAST_PROFILE_UUID" >/dev/null 2>&1 || true
        return 1
    fi

    if ! nmcli connection add type vlan con-name "$fcc_name" ifname "${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_FCC_VLAN}" dev "$RTP2HTTPD_PARENT_IF" id "$RTP2HTTPD_FCC_VLAN" >/dev/null; then
        nmcli connection delete uuid "$RTP2HTTPD_MULTICAST_PROFILE_UUID" >/dev/null 2>&1 || true
        echo "❌ 无法创建 FCC/DHCP VLAN profile。"
        return 1
    fi
    RTP2HTTPD_FCC_PROFILE_UUID="$(nmcli -g connection.uuid connection show "$fcc_name")"
    if ! nmcli connection modify uuid "$RTP2HTTPD_FCC_PROFILE_UUID" connection.autoconnect yes ipv4.method auto ipv4.never-default yes ipv6.method disabled; then
        nmcli connection delete uuid "$RTP2HTTPD_FCC_PROFILE_UUID" >/dev/null 2>&1 || true
        nmcli connection delete uuid "$RTP2HTTPD_MULTICAST_PROFILE_UUID" >/dev/null 2>&1 || true
        return 1
    fi

    rtp2httpd_render_template "${stage_dir}/rtp2httpd.conf.tpl" "${stage_dir}/rtp2httpd.conf" \
        "__MULTICAST_IF__=${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_MULTICAST_VLAN}" \
        "__FCC_IF__=${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_FCC_VLAN}" \
        "__BIND_ADDRESS__=${RTP2HTTPD_BIND_ADDRESS}" "__PORT__=${RTP2HTTPD_PORT}" || return 1
    rtp2httpd_write_state "$stage_dir"
    mv "$stage_dir" "$app_dir"
    trap - RETURN

    rtp2httpd_render_template "${app_dir}/rtp2httpd.service.tpl" "/etc/systemd/system/${RTP2HTTPD_SERVICE}" \
        "__APP_DIR__=${app_dir}" "__MULTICAST_PROFILE_UUID__=${RTP2HTTPD_MULTICAST_PROFILE_UUID}" \
        "__FCC_PROFILE_UUID__=${RTP2HTTPD_FCC_PROFILE_UUID}" || return 1
    # Keep the downloaded template next to the installation for transparent repair/update.
    systemctl daemon-reload
    systemctl enable --now "$RTP2HTTPD_SERVICE" || {
        echo "❌ 服务启动失败；保留安装目录和 profile 以便检查/修复。"
        return 1
    }
    echo "✅ rtp2httpd 已安装并启动：http://${RTP2HTTPD_BIND_ADDRESS}:${RTP2HTTPD_PORT}/status"
}

rtp2httpd_find_installation() {
    local service_path
    service_path="$(systemctl show "$RTP2HTTPD_SERVICE" -p FragmentPath --value 2>/dev/null || true)"
    [ -r "$service_path" ] || return 1
    awk -F' -c ' '/^ExecStart=/ {sub(/^ExecStart=/, "", $1); sub(/\/rtp2httpd$/, "", $1); print $1; exit}' "$service_path"
}

rtp2httpd_upgrade() {
    local app_dir
    rtp2httpd_require_commands || return 1
    app_dir="$(rtp2httpd_find_installation)"
    [ -n "$app_dir" ] && [ -r "${app_dir}/install.env" ] || { echo "❌ 未找到 YehBP 管理的 rtp2httpd 安装。"; return 1; }
    rtp2httpd_download_binary "$app_dir" || return 1
    systemctl restart "$RTP2HTTPD_SERVICE" || { echo "❌ 新版本重启失败；请检查：journalctl -u ${RTP2HTTPD_SERVICE}"; return 1; }
    echo "✅ rtp2httpd 已升级并重启。"
}

rtp2httpd_delete() {
    local app_dir answer
    app_dir="$(rtp2httpd_find_installation)"
    [ -n "$app_dir" ] && [ -r "${app_dir}/install.env" ] || { echo "❌ 未找到 YehBP 管理的 rtp2httpd 安装；拒绝删除未知 VLAN profile。"; return 1; }
    read -r -p "将停止服务、删除 YehBP 创建的两个 VLAN profile 及 ${app_dir}。继续？[y/N]: " answer
    [[ "$answer" =~ ^[yY]([eE][sS])?$ ]] || return 0
    rtp2httpd_remove_managed "$app_dir"
    rm -rf "$app_dir"
    echo "✅ rtp2httpd、关联 service 和 YehBP 创建的 VLAN profile 已删除。"
}

rtp2httpd_status() {
    local app_dir
    app_dir="$(rtp2httpd_find_installation)"
    if [ -z "$app_dir" ] || [ ! -r "${app_dir}/install.env" ]; then
        echo "ℹ️ 未找到 YehBP 管理的 rtp2httpd 安装。"
        return 0
    fi
    cat "${app_dir}/install.env"
    systemctl --no-pager --full status "$RTP2HTTPD_SERVICE" || true
}

manage_rtp2httpd() {
    local choice
    echo "\n=== rtp2httpd：IPTV 组播转 HTTP 单播 ==="
    echo "1) 安装 / 替换安装"
    echo "2) 升级官方 rtp2httpd 二进制"
    echo "3) 删除 YehBP 管理的 rtp2httpd"
    echo "4) 查看状态"
    read -r -p "请选择（回车返回）: " choice
    case "$choice" in
        1) rtp2httpd_install ;;
        2) rtp2httpd_upgrade ;;
        3) rtp2httpd_delete ;;
        4) rtp2httpd_status ;;
        "") return 0 ;;
        *) echo "❌ 无效选择。" ;;
    esac
}
