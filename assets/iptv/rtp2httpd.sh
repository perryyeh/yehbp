#!/usr/bin/env bash
# Loaded by YehBP menu 30. Requires the parent script's download_yehbp_asset
# and select_dockerapps_dir helpers.

RTP2HTTPD_SERVICE_BASE="rtp2httpd"
RTP2HTTPD_RELEASE_API="https://api.github.com/repos/stackia/rtp2httpd/releases/latest"

rtp2httpd_require_commands() {
    local missing=() cmd
    for cmd in curl nmcli systemctl ip sysctl tr sha256sum uname python3; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [ ${#missing[@]} -ne 0 ]; then
        echo "❌ rtp2httpd 需要以下命令：${missing[*]}"
        echo "   当前功能仅支持使用 NetworkManager（nmcli）的 systemd 主机。"
        return 1
    fi
}

rtp2httpd_require_delete_commands() {
    local missing=() cmd
    for cmd in nmcli systemctl; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [ ${#missing[@]} -ne 0 ]; then
        echo "❌ 删除 rtp2httpd 需要以下命令：${missing[*]}"
        return 1
    fi
}

rtp2httpd_curl() {
    if declare -F yehbp_curl >/dev/null 2>&1; then
        yehbp_curl "$@"
    else
        curl "$@"
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
    local iface sys_path choice ipv4
    while IFS= read -r iface; do
        iface="${iface%%@*}"
        [ "$iface" = "lo" ] && continue
        sys_path="$(readlink -f "/sys/class/net/${iface}" 2>/dev/null || true)"
        # IPTV parent 必须是实际物理网卡；过滤 Docker bridge、veth、macvlan
        # 以及已有 VLAN 子接口，避免用户再次选择 enp3s0.51/enp3s0.85。
        [[ "$sys_path" == */virtual/* ]] && continue
        [ -e "/sys/class/net/${iface}/device" ] || continue
        interfaces+=("$iface")
    done < <(ip -o link show | awk -F ': ' '{print $2}' | awk '!seen[$0]++')

    [ ${#interfaces[@]} -gt 0 ] || { echo "❌ 未找到可用网卡。"; return 1; }
    echo "请选择承载 IPTV VLAN 的父网卡："
    for i in "${!interfaces[@]}"; do
        ipv4="$(ip -4 -o addr show dev "${interfaces[$i]}" scope global 2>/dev/null \
            | awk '{sub(/\/.*/, "", $4); printf "%s%s", sep, $4; sep=", "}')"
        printf '  %d）%s（IP：%s）\n' "$((i + 1))" "${interfaces[$i]}" "${ipv4:--}"
    done
    echo "  0）返回"
    read -r -p "请输入要操作的序号: " choice
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

rtp2httpd_prompt_igmp_version() {
    local value
    while true; do
        read -r -p "指定 IGMP 版本（0=不指定，2/3=强制；默认 0）: " value
        value="${value:-0}"
        case "$value" in
            0|2|3)
                RTP2HTTPD_IGMP_VERSION="$value"
                return 0
                ;;
            *) echo "❌ 请输入 0、2 或 3。" ;;
        esac
    done
}

rtp2httpd_prompt_fcc_ipv4() {
    local choice address gateway
    while true; do
        read -r -p "FCC 地址方式（1=DHCP，2=静态 IPv4；默认 1）: " choice
        choice="${choice:-1}"
        case "$choice" in
            1)
                RTP2HTTPD_FCC_IPV4_METHOD="dhcp"
                RTP2HTTPD_FCC_IPV4_ADDRESS=""
                RTP2HTTPD_FCC_IPV4_GATEWAY=""
                return 0
                ;;
            2) break ;;
            *) echo "❌ 请输入 1 或 2。" ;;
        esac
    done

    while true; do
        read -r -p "请输入 FCC 静态 IPv4 地址/前缀（例如 192.168.10.2/24，回车退出）: " address
        [ -n "$address" ] || return 2
        read -r -p "请输入 FCC IPv4 网关（回车退出）: " gateway
        [ -n "$gateway" ] || return 2
        if python3 - "$address" "$gateway" <<'PY'
import ipaddress
import sys

try:
    interface = ipaddress.IPv4Interface(sys.argv[1])
    gateway = ipaddress.IPv4Address(sys.argv[2])
    valid = (
        interface.network.prefixlen < 32
        and gateway in interface.network
        and gateway != interface.ip
        and not interface.ip.is_unspecified
        and not interface.ip.is_multicast
        and not gateway.is_unspecified
        and not gateway.is_multicast
    )
except ValueError:
    valid = False
sys.exit(0 if valid else 1)
PY
        then
            RTP2HTTPD_FCC_IPV4_METHOD="static"
            RTP2HTTPD_FCC_IPV4_ADDRESS="$address"
            RTP2HTTPD_FCC_IPV4_GATEWAY="$gateway"
            return 0
        fi
        echo "❌ 静态地址必须是有效 IPv4 CIDR，网关须与地址在同一网段且不能相同。"
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

    rtp2httpd_curl --connect-timeout 10 --max-time 60 -fsSL "$RTP2HTTPD_RELEASE_API" -o "$tmp_json" || {
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
    rtp2httpd_curl --connect-timeout 10 --max-time 120 -fL "$url" -o "$tmp_bin" || {
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

rtp2httpd_instance_paths() {
    RTP2HTTPD_CONFIG_PATH="${RTP2HTTPD_APP_DIR}/rtp2httpd_${RTP2HTTPD_INSTANCE}.conf"
    RTP2HTTPD_STATE_PATH="${RTP2HTTPD_APP_DIR}/rtp2httpd_${RTP2HTTPD_INSTANCE}.env"
    RTP2HTTPD_SERVICE="${RTP2HTTPD_SERVICE_BASE}_${RTP2HTTPD_INSTANCE}.service"
    RTP2HTTPD_SERVICE_PATH="/etc/systemd/system/${RTP2HTTPD_SERVICE}"
}

rtp2httpd_fcc_routing_params() {
    local key checksum
    key="${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_FCC_VLAN}"
    checksum="$(printf '%s' "$key" | cksum | awk '{print $1}')" || return 1
    # 从接口+VLAN 推导策略表/优先级，不依赖运营商、DHCP 网关或 FCC 服务器地址。
    RTP2HTTPD_FCC_ROUTE_TABLE=$((10000 + checksum % 10000))
    RTP2HTTPD_FCC_ROUTE_PRIORITY=$((20000 + checksum % 10000))
}

rtp2httpd_prompt_instance() {
    local input n=1
    read -r -p "请输入配置名（例如 tel；回车自动分配 _1、_2…）: " input
    if [ -z "$input" ]; then
        while :; do
            RTP2HTTPD_INSTANCE="$n"
            rtp2httpd_instance_paths
            if [ ! -e "$RTP2HTTPD_CONFIG_PATH" ] && [ ! -e "$RTP2HTTPD_STATE_PATH" ] && [ ! -e "$RTP2HTTPD_SERVICE_PATH" ]; then
                return 0
            fi
            n=$((n + 1))
        done
    fi
    if [[ ! "$input" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
        echo "❌ 配置名仅可包含字母、数字、下划线和连字符，且必须以字母或数字开头。"
        return 1
    fi
    RTP2HTTPD_INSTANCE="$input"
    rtp2httpd_instance_paths
}

rtp2httpd_remove_network_profiles() {
    local state="$1" uuid type parent vlan failed=0 matched=0
    if [ ! -r "$state" ]; then
        # 兼容旧的无状态实例：仍可删除其 service/config，但绝不按 VLAN ID
        # 猜测 profile，避免误删用户已有网络配置。
        echo "⚠️ 未找到实例状态文件，跳过 VLAN profile 删除：$state"
        return 0
    fi
    # State files are generated by this script and contain shell-safe values only.
    # shellcheck disable=SC1090
    . "$state"
    if [[ ! "${PARENT_IF:-}" =~ ^[A-Za-z0-9_.:-]+$ ]] || \
       [[ ! "${MULTICAST_VLAN:-}" =~ ^[0-9]+$ ]] || \
       [[ ! "${FCC_VLAN:-}" =~ ^[0-9]+$ ]] || \
       ((10#$MULTICAST_VLAN < 1 || 10#$MULTICAST_VLAN > 4094 ||
         10#$FCC_VLAN < 1 || 10#$FCC_VLAN > 4094)); then
        echo "❌ 状态文件中的父接口或 VLAN ID 无效，拒绝删除。"
        return 1
    fi

    # UUID may change when a NetworkManager profile is rebuilt. Enumerate every
    # current VLAN profile and remove all profiles for this instance's parent/VLAN
    # pairs, rather than trusting UUIDs written during a previous installation.
    while IFS= read -r uuid; do
        [[ "$uuid" =~ ^[0-9a-fA-F-]{36}$ ]] || continue
        type="$(nmcli -g connection.type connection show uuid "$uuid" 2>/dev/null)"
        [ "$type" = "vlan" ] || continue
        parent="$(nmcli -g vlan.parent connection show uuid "$uuid" 2>/dev/null)"
        vlan="$(nmcli -g vlan.id connection show uuid "$uuid" 2>/dev/null)"
        [ "$parent" = "$PARENT_IF" ] || continue
        [ "$vlan" = "$MULTICAST_VLAN" ] || [ "$vlan" = "$FCC_VLAN" ] || continue

        matched=$((matched + 1))
        if ! nmcli connection delete uuid "$uuid" >/dev/null 2>&1 || nmcli connection show uuid "$uuid" >/dev/null 2>&1; then
            echo "❌ 无法删除或确认删除 VLAN profile：$uuid（${parent}.${vlan}）"
            failed=1
            continue
        fi
        echo "✅ 已删除 VLAN profile：$uuid（${parent}.${vlan}）"
    done < <(nmcli -g UUID connection show)

    [ "$matched" -gt 0 ] || echo "ℹ️ 未找到 ${PARENT_IF}.${MULTICAST_VLAN} 或 ${PARENT_IF}.${FCC_VLAN} 的 VLAN profile。"
    return "$failed"
}

rtp2httpd_remove_instance() {
    local service="$1" config="$2" state="$3"
    rtp2httpd_remove_network_profiles "$state" || {
        echo "❌ VLAN profile 未完全删除；已保留 ${service}、配置和状态文件，可修复后重试。"
        return 1
    }
    systemctl disable --now "$service" >/dev/null 2>&1 || {
        echo "❌ 无法停止 ${service}；已保留配置和状态文件。"
        return 1
    }
    rm -f "/etc/systemd/system/${service}" "$config" "$state" || return 1
    systemctl daemon-reload || return 1
}

rtp2httpd_write_state() {
    local state="$1"
    cat > "$state" <<EOF
MANAGED_BY=yehbp-rtp2httpd
INSTANCE=${RTP2HTTPD_INSTANCE}
PARENT_IF=${RTP2HTTPD_PARENT_IF}
MULTICAST_VLAN=${RTP2HTTPD_MULTICAST_VLAN}
FCC_VLAN=${RTP2HTTPD_FCC_VLAN}
FCC_IPV4_METHOD=${RTP2HTTPD_FCC_IPV4_METHOD}
FCC_IPV4_ADDRESS=${RTP2HTTPD_FCC_IPV4_ADDRESS}
FCC_IPV4_GATEWAY=${RTP2HTTPD_FCC_IPV4_GATEWAY}
FCC_ROUTE_TABLE=${RTP2HTTPD_FCC_ROUTE_TABLE}
FCC_ROUTE_PRIORITY=${RTP2HTTPD_FCC_ROUTE_PRIORITY}
IGMP_VERSION=${RTP2HTTPD_IGMP_VERSION}
BIND_ADDRESS=${RTP2HTTPD_BIND_ADDRESS}
PORT=${RTP2HTTPD_PORT}
MULTICAST_PROFILE_UUID=${RTP2HTTPD_MULTICAST_PROFILE_UUID}
FCC_PROFILE_UUID=${RTP2HTTPD_FCC_PROFILE_UUID}
EOF
    chmod 0600 "$state"
}

rtp2httpd_install() {
    local dockerapps answer mcast_name fcc_name mcast_sysctl_if tmp_conf tmp_service fcc_ipv4_args=()
    rtp2httpd_require_commands || return 1
    select_dockerapps_dir "安装IPTV(rtp2httpd)"
    case $? in 0) dockerapps="$SELECTED_DOCKERAPPS_DIR" ;; 2) return 0 ;; *) return 1 ;; esac
    RTP2HTTPD_APP_DIR="${dockerapps}/rtp2httpd"
    mkdir -p "$RTP2HTTPD_APP_DIR" || return 1
    rtp2httpd_prompt_instance || return 1

    if [ -e "$RTP2HTTPD_CONFIG_PATH" ] || [ -e "$RTP2HTTPD_STATE_PATH" ] || [ -e "$RTP2HTTPD_SERVICE_PATH" ]; then
        read -r -p "检测到配置 ${RTP2HTTPD_INSTANCE} 已存在；是否替换其配置和开机启动服务？[y/N]: " answer
        [[ "$answer" =~ ^[yY]([eE][sS])?$ ]] || return 0
        rtp2httpd_remove_instance "$RTP2HTTPD_SERVICE" "$RTP2HTTPD_CONFIG_PATH" "$RTP2HTTPD_STATE_PATH" || return 1
    fi

    rtp2httpd_select_parent
    case $? in 0) ;; 2) return 0 ;; *) return 1 ;; esac
    RTP2HTTPD_MULTICAST_VLAN="$(rtp2httpd_prompt_vlan '请输入组播 VLAN ID（回车退出）: ')" || return 0
    RTP2HTTPD_FCC_VLAN="$(rtp2httpd_prompt_vlan '请输入 FCC/DHCP VLAN ID（回车退出）: ')" || return 0
    if [ "$RTP2HTTPD_MULTICAST_VLAN" = "$RTP2HTTPD_FCC_VLAN" ]; then
        echo "❌ 组播和 FCC/DHCP VLAN ID 必须不同。"
        return 1
    fi
    rtp2httpd_prompt_fcc_ipv4
    case $? in 0) ;; 2) return 0 ;; *) return 1 ;; esac
    rtp2httpd_prompt_igmp_version || return 0
    rtp2httpd_prompt_bind || return 0
    rtp2httpd_fcc_routing_params || return 1

    if [ ! -x "${RTP2HTTPD_APP_DIR}/rtp2httpd" ]; then
        echo "⬇️ 未找到共享 rtp2httpd 二进制，正在下载…"
        rtp2httpd_download_binary "$RTP2HTTPD_APP_DIR" || return 1
    fi

    download_yehbp_asset "assets/iptv/rtp2httpd.conf.tpl" "${RTP2HTTPD_APP_DIR}/rtp2httpd.conf.tpl" || return 1
    download_yehbp_asset "assets/iptv/rtp2httpd.service.tpl" "${RTP2HTTPD_APP_DIR}/rtp2httpd.service.tpl" || return 1

    mcast_name="rtp2httpd-${RTP2HTTPD_INSTANCE}-mcast-${RTP2HTTPD_PARENT_IF}-${RTP2HTTPD_MULTICAST_VLAN}"
    fcc_name="rtp2httpd-${RTP2HTTPD_INSTANCE}-fcc-${RTP2HTTPD_PARENT_IF}-${RTP2HTTPD_FCC_VLAN}"
    if nmcli connection show "$mcast_name" >/dev/null 2>&1 || nmcli connection show "$fcc_name" >/dev/null 2>&1; then
        echo "❌ 同名 NetworkManager profile 已存在；为避免覆盖未知配置，已取消。"
        return 1
    fi

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
    if [ "$RTP2HTTPD_FCC_IPV4_METHOD" = "static" ]; then
        fcc_ipv4_args=(ipv4.method manual ipv4.addresses "$RTP2HTTPD_FCC_IPV4_ADDRESS" ipv4.gateway "$RTP2HTTPD_FCC_IPV4_GATEWAY")
    else
        fcc_ipv4_args=(ipv4.method auto)
    fi
    if ! nmcli connection modify uuid "$RTP2HTTPD_FCC_PROFILE_UUID" \
        connection.autoconnect yes "${fcc_ipv4_args[@]}" ipv4.never-default no \
        ipv4.route-table "$RTP2HTTPD_FCC_ROUTE_TABLE" \
        ipv4.routing-rules "priority ${RTP2HTTPD_FCC_ROUTE_PRIORITY} oif ${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_FCC_VLAN} table ${RTP2HTTPD_FCC_ROUTE_TABLE}" \
        ipv6.method disabled; then
        nmcli connection delete uuid "$RTP2HTTPD_FCC_PROFILE_UUID" >/dev/null 2>&1 || true
        nmcli connection delete uuid "$RTP2HTTPD_MULTICAST_PROFILE_UUID" >/dev/null 2>&1 || true
        return 1
    fi

    tmp_conf="${RTP2HTTPD_CONFIG_PATH}.tmp"
    tmp_service="${RTP2HTTPD_SERVICE_PATH}.tmp"
    mcast_sysctl_if="${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_MULTICAST_VLAN}"
    mcast_sysctl_if="$(printf '%s' "$mcast_sysctl_if" | tr . /)"
    rtp2httpd_render_template "${RTP2HTTPD_APP_DIR}/rtp2httpd.conf.tpl" "$tmp_conf" \
        "__MULTICAST_IF__=${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_MULTICAST_VLAN}" \
        "__FCC_IF__=${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_FCC_VLAN}" \
        "__BIND_ADDRESS__=${RTP2HTTPD_BIND_ADDRESS}" "__PORT__=${RTP2HTTPD_PORT}" || return 1
    rtp2httpd_render_template "${RTP2HTTPD_APP_DIR}/rtp2httpd.service.tpl" "$tmp_service" \
        "__APP_DIR__=${RTP2HTTPD_APP_DIR}" "__CONFIG_PATH__=${RTP2HTTPD_CONFIG_PATH}" \
        "__MULTICAST_PROFILE_UUID__=${RTP2HTTPD_MULTICAST_PROFILE_UUID}" \
        "__FCC_PROFILE_UUID__=${RTP2HTTPD_FCC_PROFILE_UUID}" \
        "__MULTICAST_SYSCTL_IF__=${mcast_sysctl_if}" \
        "__IGMP_VERSION__=${RTP2HTTPD_IGMP_VERSION}" || return 1
    rtp2httpd_write_state "$RTP2HTTPD_STATE_PATH"
    mv -f "$tmp_conf" "$RTP2HTTPD_CONFIG_PATH"
    mv -f "$tmp_service" "$RTP2HTTPD_SERVICE_PATH"

    systemctl daemon-reload
    systemctl enable --now "$RTP2HTTPD_SERVICE" || {
        echo "❌ 服务启动失败；保留配置、service 和 VLAN profile 以便检查/修复。"
        return 1
    }
    echo "✅ rtp2httpd 配置 ${RTP2HTTPD_INSTANCE} 已安装并启动：http://${RTP2HTTPD_BIND_ADDRESS}:${RTP2HTTPD_PORT}/status"
}

rtp2httpd_list_instances() {
    local service_path base suffix instance
    RTP2HTTPD_LIST_SERVICES=()
    shopt -s nullglob
    for service_path in \
        "/etc/systemd/system/${RTP2HTTPD_SERVICE_BASE}.service" \
        /etc/systemd/system/${RTP2HTTPD_SERVICE_BASE}_*.service; do
        [ -e "$service_path" ] || continue
        base="${service_path##*/}"
        suffix="${base#${RTP2HTTPD_SERVICE_BASE}}"
        suffix="${suffix%.service}"
        case "$suffix" in
            "") instance="" ;;
            _*) instance="${suffix#_}" ;;
            *) continue ;;
        esac
        RTP2HTTPD_LIST_SERVICES+=("$base")
    done
    shopt -u nullglob
}

rtp2httpd_service_binary_path() {
    local service="$1"
    awk -F' -c ' '/^ExecStart=/ {sub(/^ExecStart=/, "", $1); print $1; exit}' "/etc/systemd/system/${service}"
}

rtp2httpd_upgrade() {
    local -a app_dirs=() services=()
    local service binary app_dir choice i selected restarted=0

    rtp2httpd_require_commands || return 1
    rtp2httpd_list_instances
    for service in "${RTP2HTTPD_LIST_SERVICES[@]}"; do
        binary="$(rtp2httpd_service_binary_path "$service")"
        [ -x "$binary" ] || continue
        app_dir="$(dirname "$binary")"
        if [[ " ${app_dirs[*]} " != *" ${app_dir} "* ]]; then
            app_dirs+=("$app_dir")
        fi
    done
    if [ ${#app_dirs[@]} -eq 0 ]; then
        echo "❌ 未从 rtp2httpd service 找到可升级的共享二进制。"
        return 1
    fi
    if [ ${#app_dirs[@]} -eq 1 ]; then
        selected="${app_dirs[0]}"
    else
        echo "检测到多个 rtp2httpd 共享二进制目录："
        for i in "${!app_dirs[@]}"; do
            printf '  %d）%s\n' "$((i + 1))" "${app_dirs[$i]}"
        done
        echo "  0）返回"
        read -r -p "请输入要操作的序号: " choice
        [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#app_dirs[@]}" ] || return 0
        selected="${app_dirs[$((choice - 1))]}"
    fi

    echo "⬇️ 正在升级共享二进制：${selected}/rtp2httpd"
    rtp2httpd_download_binary "$selected" || return 1
    for service in "${RTP2HTTPD_LIST_SERVICES[@]}"; do
        binary="$(rtp2httpd_service_binary_path "$service")"
        [ "$binary" = "${selected}/rtp2httpd" ] || continue
        services+=("$service")
        if systemctl is-active --quiet "$service"; then
            systemctl restart "$service" || {
                echo "❌ ${service} 重启失败；请检查：journalctl -u ${service}"
                return 1
            }
            restarted=$((restarted + 1))
        fi
    done
    echo "✅ rtp2httpd 二进制已升级；重启 ${restarted} 个运行中的实例。"
    [ ${#services[@]} -gt 0 ] || echo "ℹ️ 当前没有引用该二进制的 YehBP 命名 service。"
}

rtp2httpd_delete() {
    local choice service instance config state answer display_name i
    local -a selected_services=()
    rtp2httpd_require_delete_commands || return 1
    rtp2httpd_list_instances
    if [ ${#RTP2HTTPD_LIST_SERVICES[@]} -eq 0 ]; then
        echo "ℹ️ 未找到 rtp2httpd 开机启动服务（rtp2httpd.service 或 rtp2httpd_*.service）。"
        return 0
    fi
    echo "可删除的 rtp2httpd 配置："
    for i in "${!RTP2HTTPD_LIST_SERVICES[@]}"; do
        service="${RTP2HTTPD_LIST_SERVICES[$i]}"
        instance="${service#${RTP2HTTPD_SERVICE_BASE}}"
        instance="${instance%.service}"
        instance="${instance#_}"
        printf '  %d）%s（%s）\n' "$((i + 1))" "${instance:-默认}" "$service"
    done
    echo "  0）返回"
    echo "  a）全部"
    read -r -p "请输入要操作的序号: " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#RTP2HTTPD_LIST_SERVICES[@]}" ]; then
        selected_services=("${RTP2HTTPD_LIST_SERVICES[$((choice - 1))]}")
    elif [[ "$choice" =~ ^[Aa]$ ]]; then
        selected_services=("${RTP2HTTPD_LIST_SERVICES[@]}")
    else
        return 0
    fi

    for service in "${selected_services[@]}"; do
        config="$(awk -F' -c ' '/^ExecStart=/ {print $2; exit}' "/etc/systemd/system/${service}")"
        if [ -z "$config" ] || { [[ "$config" != */rtp2httpd.conf ]] && [[ "$config" != */rtp2httpd_*.conf ]]; }; then
            echo "❌ ${service} 的配置路径不符合 YehBP 命名规则，拒绝删除配置文件。"
            return 1
        fi
    done
    read -r -p "将停止并删除所选 rtp2httpd 配置、service 和 YehBP 创建的 VLAN profile。继续？[y/N]: " answer
    [[ "$answer" =~ ^[yY]([eE][sS])?$ ]] || return 0

    for service in "${selected_services[@]}"; do
        instance="${service#${RTP2HTTPD_SERVICE_BASE}}"
        instance="${instance%.service}"
        instance="${instance#_}"
        config="$(awk -F' -c ' '/^ExecStart=/ {print $2; exit}' "/etc/systemd/system/${service}")"
        state="${config%.conf}.env"
        display_name="${instance:-默认}"
        rtp2httpd_remove_instance "$service" "$config" "$state" || return 1
        echo "✅ 已删除 rtp2httpd 配置 ${display_name}、关联 service 和 YehBP 创建的 VLAN profile；共享二进制已保留。"
    done
}

rtp2httpd_restart() {
    local choice service instance i
    local -a selected_services=()
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "❌ 重启 rtp2httpd 需要 systemctl。"
        return 1
    fi
    rtp2httpd_list_instances
    if [ ${#RTP2HTTPD_LIST_SERVICES[@]} -eq 0 ]; then
        echo "ℹ️ 未找到 rtp2httpd 开机启动服务（rtp2httpd.service 或 rtp2httpd_*.service）。"
        return 0
    fi
    echo "可重启的 rtp2httpd 实例："
    for i in "${!RTP2HTTPD_LIST_SERVICES[@]}"; do
        service="${RTP2HTTPD_LIST_SERVICES[$i]}"
        instance="${service#${RTP2HTTPD_SERVICE_BASE}}"
        instance="${instance%.service}"
        instance="${instance#_}"
        printf '  %d）%s（%s）\n' "$((i + 1))" "${instance:-默认}" "$service"
    done
    echo "  0）返回"
    echo "  a）全部"
    read -r -p "请输入要操作的序号: " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#RTP2HTTPD_LIST_SERVICES[@]}" ]; then
        selected_services=("${RTP2HTTPD_LIST_SERVICES[$((choice - 1))]}")
    elif [[ "$choice" =~ ^[Aa]$ ]]; then
        selected_services=("${RTP2HTTPD_LIST_SERVICES[@]}")
    else
        return 0
    fi

    for service in "${selected_services[@]}"; do
        if ! systemctl restart "$service"; then
            echo "❌ ${service} 重启失败；请检查：journalctl -u ${service}"
            return 1
        fi
        echo "✅ 已重启 ${service}。"
    done
}

manage_rtp2httpd() {
    local choice
    printf '\n=== IPTV（rtp2httpd） ===\n'
    echo "1）安装 / 替换配置"
    echo "2）删除配置"
    echo "3）仅升级 rtp2httpd 二进制"
    echo "4）重启 rtp2httpd 实例"
    echo "0）返回"
    read -r -p "请输入要操作的序号: " choice
    case "$choice" in
        1) rtp2httpd_install ;;
        2) rtp2httpd_delete ;;
        3) rtp2httpd_upgrade ;;
        4) rtp2httpd_restart ;;
        *) return 0 ;;
    esac
}
