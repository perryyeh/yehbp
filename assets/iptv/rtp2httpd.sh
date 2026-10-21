#!/usr/bin/env bash
# Loaded by YehBP menu 80. Requires the parent script's download_yehbp_asset
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
        read -r -p "$prompt" value || return 2
        [ -n "$value" ] || return 2
        if [[ "$value" =~ ^[0-9]+$ ]] && [ "$value" -ge 1 ] && [ "$value" -le 4094 ]; then
            printf '%s\n' "$value"
            return 0
        fi
        echo "❌ VLAN ID 必须是 1–4094。" >&2
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
    read -r -p "请输入配置名（例如 tel；回车自动分配 _1、_2…）: " input || return 2
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

rtp2httpd_owned_profile_uuids() (
    # Keep sourced state isolated from caller and previous instances.
    local allow_missing=0
    if [ "${1:-}" = --allow-missing ]; then
        allow_missing=1
        shift
    fi
    local state="$1" uuid type parent vlan name iface role expected other config service inventory actual_vlan missing
    local MANAGED_BY= INSTANCE= PARENT_IF= MULTICAST_VLAN= FCC_VLAN=
    local MULTICAST_PROFILE_UUID= FCC_PROFILE_UUID=
    local -a candidates=() states=()
    if [ ! -f "$state" ] || [ ! -r "$state" ]; then
        echo "❌ 实例状态文件不可读或不是普通文件；未修改网络：$state" >&2
        return 1
    fi
    # State files are generated by this script and contain shell-safe values only.
    # shellcheck disable=SC1090
    . "$state" || return 1
    if [ "$MANAGED_BY" != yehbp-rtp2httpd ] ||
       [[ ! "$INSTANCE" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] ||
       [ "${state##*/}" != "rtp2httpd_${INSTANCE}.env" ] ||
       [[ ! "$PARENT_IF" =~ ^[A-Za-z0-9_.:-]+$ ]] ||
       [[ ! "$MULTICAST_VLAN" =~ ^[0-9]+$ ]] ||
       [[ ! "$FCC_VLAN" =~ ^[0-9]+$ ]] ||
       ((10#$MULTICAST_VLAN < 1 || 10#$MULTICAST_VLAN > 4094 ||
         10#$FCC_VLAN < 1 || 10#$FCC_VLAN > 4094)); then
        echo "❌ 状态文件的实例归属、父接口或 VLAN ID 无效；未修改网络。" >&2
        return 1
    fi
    # Check sibling states and services in other dockerapps directories. Do not
    # source other instances' state just to discover references.
    states=("${state%/*}"/rtp2httpd*.env)
    rtp2httpd_list_instances
    for service in "${RTP2HTTPD_LIST_SERVICES[@]}"; do
        config="$(awk -F' -c ' '/^ExecStart=/ {print $2; exit}' "/etc/systemd/system/${service}")"
        [ -n "$config" ] && states+=("${config%.conf}.env")
    done
    # UUID plus generated instance name and VLAN attributes establish ownership;
    # parent/VLAN alone do not. A failed query does not prove absence.
    for role in mcast fcc; do
        if [ "$role" = mcast ]; then
            uuid="$MULTICAST_PROFILE_UUID"; vlan="$MULTICAST_VLAN"
        else
            uuid="$FCC_PROFILE_UUID"; vlan="$FCC_VLAN"
        fi
        if [[ ! "$uuid" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
            echo "❌ 状态缺少有效 ${role} UUID；未修改网络。" >&2
            return 1
        fi
        for other in "${states[@]}"; do
            [ -e "$other" ] || continue
            [ "$other" -ef "$state" ] && continue
            if [ ! -r "$other" ] || grep -Eiq "^(MULTICAST|FCC)_PROFILE_UUID=${uuid}$" "$other"; then
                echo "❌ UUID ${uuid} 被其他实例引用或状态不可读：${other}；未修改网络。" >&2
                return 1
            fi
        done
        if ! type="$(nmcli -g connection.type connection show uuid "$uuid" 2>/dev/null)"; then
            inventory="$(nmcli -g UUID connection show 2>/dev/null)" || return 1
            if printf '%s\n' "$inventory" | grep -Fqx "$uuid"; then
                echo "❌ 无法检查 UUID ${uuid}；未修改网络。" >&2
                return 1
            fi
            echo "⚠️ 已记录 UUID ${uuid} 不存在；不按名称或 VLAN 查找替代连接。" >&2
            missing=1
            continue
        fi
        expected="rtp2httpd-${INSTANCE}-${role}-${PARENT_IF}-${vlan}"
        if [ "$FCC_VLAN" = "$MULTICAST_VLAN" ] && [ "$uuid" = "$MULTICAST_PROFILE_UUID" ]; then
            expected="rtp2httpd-${INSTANCE}-mcast-${PARENT_IF}-${vlan}"
        fi
        name="$(nmcli -g connection.id connection show uuid "$uuid" 2>/dev/null)" || return 1
        parent="$(nmcli -g vlan.parent connection show uuid "$uuid" 2>/dev/null)" || return 1
        iface="$(nmcli -g connection.interface-name connection show uuid "$uuid" 2>/dev/null)" || return 1
        actual_vlan="$(nmcli -g vlan.id connection show uuid "$uuid" 2>/dev/null)" || return 1
        if [ "$type" != vlan ] || [ "$name" != "$expected" ] || [ "$parent" != "$PARENT_IF" ] ||
           [ "$iface" != "${PARENT_IF}.${vlan}" ] || [ "$actual_vlan" != "$vlan" ]; then
            echo "❌ UUID ${uuid} 的 profile 与实例 ${INSTANCE} 不匹配；未修改网络。" >&2
            return 1
        fi
        [ "${candidates[0]:-}" = "$uuid" ] || candidates+=("$uuid")
    done
    if [ "$allow_missing" != 1 ] && [ "${missing:-0}" = 1 ]; then
        echo "❌ 状态记录的连接已不存在；未修改网络。" >&2
        return 1
    fi
    printf '%s\n' "${candidates[@]}"
)

rtp2httpd_apply_owned_dns() {
    local state="$1" uuid method v4 v6 active list
    list="$(rtp2httpd_owned_profile_uuids "$state")" || return $?
    [ -n "$list" ] || return 1
    while IFS= read -r uuid; do
        [ -n "$uuid" ] || continue
        method="$(nmcli -g ipv4.method,ipv6.method connection show uuid "$uuid" 2>/dev/null)" || return $?
        # nmcli 1.42 prints one -g field per line; newer builds may use commas.
        method="$(printf '%s' "$method" | tr '\n' ',')"
        method="${method%,}"
        v4="${method%%,*}"
        v6="${method#*,}"
        # Only suppress automatic DNS. Preserve the existing address methods,
        # including an unexpected enabled IPv6 configuration.
        nmcli connection modify uuid "$uuid" ipv4.ignore-auto-dns yes ipv6.ignore-auto-dns yes || return 1
        readback="$(nmcli -g ipv4.method,ipv6.method,ipv4.ignore-auto-dns,ipv6.ignore-auto-dns connection show uuid "$uuid")" || return 1
        readback="$(printf '%s' "$readback" | tr '\n' ',')"
        readback="${readback%,}"
        [ "$readback" = "${v4},${v6},yes,yes" ] || return 1
        active="$(nmcli -g GENERAL.STATE connection show uuid "$uuid" 2>/dev/null || true)"
        if [[ "$active" == activated* ]]; then
            nmcli connection reapply uuid "$uuid" >/dev/null 2>&1 || nmcli connection up uuid "$uuid" >/dev/null || return 1
        fi
        echo "✅ 已忽略实例连接 ${uuid} 的 IPv4/IPv6 自动 DNS。"
    done <<EOF
$list
EOF
}

rtp2httpd_remove_network_profiles() (
    local state="$1" uuid line list
    local -a candidates=()
    local failed=0
    if [ ! -e "$state" ] && [ ! -L "$state" ]; then
        echo "⚠️ 未找到实例状态文件，跳过 VLAN profile 删除：$state"
        return 0
    fi
    list="$(rtp2httpd_owned_profile_uuids --allow-missing "$state")" || {
        echo "❌ 无法确认 VLAN profile 归属；保留连接和状态。" >&2
        return 1
    }
    while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] && candidates+=("$line")
    done <<EOF
$list
EOF
    for uuid in "${candidates[@]}"; do
        if ! nmcli connection delete uuid "$uuid" >/dev/null 2>&1; then
            echo "❌ 无法删除 VLAN profile：$uuid"
            failed=1
            continue
        fi
        if ! inventory="$(nmcli -g UUID connection show 2>/dev/null)" ||
           printf '%s\n' "$inventory" | grep -Fixq "$uuid"; then
            echo "❌ 无法确认删除 VLAN profile：$uuid；保留状态以便重试。"
            failed=1
        else
            echo "✅ 已删除已验证归属的 VLAN profile：$uuid"
        fi
    done
    return "$failed"
)


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
    rm -f "/etc/systemd/system/${service}" "$config" || return 1
    systemctl daemon-reload || {
        echo "❌ systemd 重载失败；已保留状态文件，可重试删除。"
        return 1
    }
    rm -f "$state" || return 1
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
RTSP_IF=${RTP2HTTPD_RTSP_IF}
BIND_ADDRESS=${RTP2HTTPD_BIND_ADDRESS}
PORT=${RTP2HTTPD_PORT}
MULTICAST_PROFILE_UUID=${RTP2HTTPD_MULTICAST_PROFILE_UUID}
FCC_PROFILE_UUID=${RTP2HTTPD_FCC_PROFILE_UUID}
EOF
    chmod 0600 "$state"
}

rtp2httpd_valid_interface() {
    [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9_.:-]*$ ]] && [ "${#1}" -le 15 ] && ip link show dev "$1" >/dev/null 2>&1
}

rtp2httpd_select_rtsp_interface() {
    local links addresses addr_status=0 name state ipv4 selection i=0
    local -a interfaces=()
    links=$(ip -o link show 2>/dev/null) || { echo "❌ 无法读取已有接口。"; return 1; }
    addresses=$(ip -o -4 addr show 2>/dev/null) || addr_status=1
    echo "已有接口（名称 / IPv4 / 状态；不代表能到达 RTSP 服务）："
    while IFS='|' read -r name state ipv4; do
        [ -n "$name" ] || continue
        interfaces+=("$name")
        i=$((i + 1))
        [ "$addr_status" = 0 ] || ipv4="IPv4 未知"
        printf '  %s）%s / %s / %s\n' "$i" "$name" "$ipv4" "$state"
    done < <(printf '%s\n' "$links" | RTP2HTTPD_RTSP_ADDRESSES="$addresses" awk '
        BEGIN {
            n = split(ENVIRON["RTP2HTTPD_RTSP_ADDRESSES"], lines, "\n")
            for (i = 1; i <= n; i++) {
                split(lines[i], fields, /[[:space:]]+/)
                name = fields[2]; sub(/@.*/, "", name)
                if (fields[3] == "inet") ips[name] = ips[name] (ips[name] ? ", " : "") fields[4]
            }
        }
        /^[0-9]+: / {
            name = $2; sub(/:$/, "", name); sub(/@.*/, "", name)
            if (seen[name]++) next
            state = "UNKNOWN"
            for (i = 3; i < NF; i++) if ($i == "state") state = $(i + 1)
            printf "%s|%s|%s\n", name, state, (ips[name] ? ips[name] : "无 IPv4")
        }')
    echo "  0）返回"
    read -r -p "请选择接口序号或输入已有接口名（回车返回）: " selection || return 2
    case "$selection" in
        ""|0) return 2 ;;
        *[!0-9]*) RTP2HTTPD_SELECTED_RTSP_IF="$selection" ;;
        *)
            # Compare strings to avoid interpreting untrusted input as shell arithmetic.
            for ((i = 0; i < ${#interfaces[@]}; i++)); do
                if [ "$selection" = "$((i + 1))" ]; then
                    RTP2HTTPD_SELECTED_RTSP_IF="${interfaces[$i]}"
                    return 0
                fi
            done
            echo "❌ 接口序号无效。"; return 1 ;;
    esac
}

rtp2httpd_prompt_rtsp() {
    local fcc="$1" multicast="$2" choice iface RTP2HTTPD_SELECTED_RTSP_IF
    echo "  1）复用 FCC 接口（${fcc:-未配置}）"
    echo "  2）复用组播接口（${multicast:-未配置}）"
    echo "  3）选择其他已有接口"
    echo "  4）不指定，沿用通用接口 / 系统路由回退"
    echo "  0）返回"
    read -r -p "请输入要操作的序号（无默认）: " choice || return 2
    case "$choice" in
        1) [ -n "$fcc" ] || { echo "❌ 未配置 FCC 接口。"; return 1; }; iface="$fcc" ;;
        2) [ -n "$multicast" ] || { echo "❌ 未配置组播接口。"; return 1; }; iface="$multicast" ;;
        3) rtp2httpd_select_rtsp_interface || return $?; iface="$RTP2HTTPD_SELECTED_RTSP_IF" ;;
        4) RTP2HTTPD_RTSP_IF=""; return 0 ;;
        *) return 2 ;;
    esac
    # Installation may select its planned FCC/multicast VLAN before creation.
    if [ "${RTP2HTTPD_RTSP_PLANNED:-0}" = 1 ] &&
       { [ "$choice" = 1 ] || [ "$choice" = 2 ]; }; then
        [[ "$iface" =~ ^[A-Za-z0-9_][A-Za-z0-9_.:-]*$ ]] && [ "${#iface}" -le 15 ] || return 1
    else
        rtp2httpd_valid_interface "$iface" || { echo "❌ 接口名无效或接口不存在。"; return 1; }
    fi
    RTP2HTTPD_RTSP_IF="$iface"
}

rtp2httpd_rtsp_line() {
    if [ -n "${RTP2HTTPD_RTSP_IF:-}" ]; then
        printf 'upstream-interface-rtsp = %s\n' "$RTP2HTTPD_RTSP_IF"
    else
        printf '# RTSP: no dedicated interface; fallback to upstream-interface, then system routing\n'
    fi
}

# Parse text as data; never source instance state. Reject ambiguous syntax before
# staging either file. Keep unrelated bytes, metadata and comments unchanged.
rtp2httpd_rtsp_config() {
    python3 - "$@" <<'PY'
import contextlib
import datetime
import fcntl
import ipaddress
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile


def interrupted(signum, frame):
    raise InterruptedError('收到信号 ' + str(signum))


for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(signum, interrupted)


def interface(value):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]{0,14}', value):
        raise ValueError('接口名无效')
    if subprocess.run(['ip', 'link', 'show', 'dev', value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise ValueError('接口不存在')


def snapshot(path, optional=False):
    if not path.exists() and not path.is_symlink() and optional:
        return None, None
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError('配置 / 状态必须是普通文件且无硬链接')
    return path.read_bytes(), s


def stage(path, data, metadata):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            if metadata:
                os.fchown(f.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(f.fileno(), stat.S_IMODE(metadata.st_mode) if metadata else 0o600)
            f.flush()
            os.fsync(f.fileno())
        return name
    except BaseException:
        os.unlink(name)
        raise


def rtsp_display(dedicated, fallback):
    return dedicated or ('未设置（沿用通用接口：' + fallback + '）' if fallback else '未设置（沿用系统路由）')


def main():
    action, config_name, state_name, *args = sys.argv[1:]
    config, state = Path(config_name), Path(state_name)
    if config == state or config.parent != state.parent:
        raise ValueError('配置 / 状态路径必须为同目录不同文件')
    # Lock the directory without leaving a lock file in existing installations.
    with contextlib.ExitStack() as stack:
        directory = os.open(str(config.parent), os.O_RDONLY)
        stack.callback(os.close, directory)
        fcntl.flock(directory, fcntl.LOCK_EX)
        original, meta = snapshot(config)
        env_original, env_meta = snapshot(state, True)
        text = original.decode('utf-8')
        lines = text.splitlines(keepends=True)
        section = None
        sections = set()
        values, indices = {}, {}
        binds = []
        for i, line in enumerate(lines):
            clean = line.strip()
            if not clean or clean.startswith(('#', ';')):
                continue
            header = re.fullmatch(r'\[([a-z][a-z0-9_-]*)\]\s*(?:[#;].*)?', clean)
            if header:
                section = header[1]
                if section in sections:
                    raise ValueError('重复配置段；请先人工整理')
                sections.add(section)
                continue
            if clean.startswith('[') or section is None:
                raise ValueError('不支持的配置段语法或段外参数')
            if section == 'global':
                match = re.fullmatch(r'\s*([a-z][a-z0-9_-]*)\s*=\s*([^#;\r\n]*?)(\s*(?:[#;].*)?)(\r?\n)?', line)
                if not match or match[1] in values:
                    raise ValueError('global 语法不明确或存在重复 key；请先人工整理')
                values[match[1]] = match[2].strip()
                indices[match[1]] = i
            elif section == 'bind':
                binds.append(i)
        if 'global' not in sections:
            raise ValueError('缺少唯一 [global]')
        env_lines = (env_original or b'').decode('utf-8').splitlines(keepends=True)
        env_indices = [i for i, line in enumerate(env_lines) if re.match(r'^\s*(?:export\s+)?RTSP_IF\s*=', line)]
        if len(env_indices) > 1:
            raise ValueError('env 存在重复 RTSP_IF')
        dedicated = values.get('upstream-interface-rtsp', '')
        fallback = values.get('upstream-interface', '')
        effective = dedicated or fallback or '系统路由'
        if action == 'status':
            print('RTSP 专用接口：' + (dedicated or '未设置') + '；有效接口：' + effective + '；移除专用项后回退：' + (fallback or '系统路由'))
            print('优先级：URL r2h-ifname > RTSP > upstream-interface > 系统路由')
            return
        if action == 'get':
            field = args[0]
            if field in ('address', 'port'):
                if len(binds) != 1:
                    raise ValueError('监听配置非单一行；本入口不支持修改')
                match = re.fullmatch(r'(\s*)(\S+)(\s+)(\d+)(\s*(?:[#;].*)?)(\r?\n)?', lines[binds[0]])
                if not match:
                    raise ValueError('不支持的 bind 语法')
                print(match[2 if field == 'address' else 4])
            elif field == 'rtsp-display':
                print(rtsp_display(dedicated, fallback))
            else:
                print(values.get('upstream-interface-' + field, ''))
            return
        if action != 'apply':
            raise ValueError('未知操作')
        service, field, value = args
        if not re.fullmatch(r'rtp2httpd(?:_[A-Za-z0-9][A-Za-z0-9_-]*)?\.service', service):
            raise ValueError('service 名无效')
        if field in ('rtsp', 'multicast', 'fcc'):
            if value:
                interface(value)
            elif field != 'rtsp':
                raise ValueError('接口不能为空')
            key = 'upstream-interface-' + field
            replacement = key + ' = ' + value if value else '# RTSP: fallback to upstream-interface, then system routing'
            if key in indices:
                i = indices[key]
                match = re.fullmatch(r'(\s*' + re.escape(key) + r'\s*=\s*)([^#;\r\n]*?)(\s*(?:[#;].*)?)(\r?\n)?', lines[i])
                lines[i] = (match[1] + value if value else replacement) + match[3] + (match[4] or '')
            else:
                i = next(i for i, line in enumerate(lines) if re.fullmatch(r'\s*\[global\]\s*(?:[#;].*)?\s*', line)) + 1
                if not lines[i-1].endswith('\n'):
                    lines[i-1] += '\n'
                lines.insert(i, replacement + '\n')
        elif field in ('address', 'port'):
            if len(binds) != 1:
                raise ValueError('监听配置非单一行；本入口不支持修改')
            i = binds[0]
            match = re.fullmatch(r'(\s*)(\S+)(\s+)(\d+)(\s*(?:[#;].*)?)(\r?\n)?', lines[i])
            if not match:
                raise ValueError('不支持的 bind 语法')
            parts = list(match.groups())
            if field == 'address':
                ipaddress.ip_address(value)
                parts[1] = value
            else:
                if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 65535:
                    raise ValueError('端口必须为 1–65535')
                parts[3] = value
            lines[i] = ''.join(p or '' for p in parts)
        else:
            raise ValueError('未知字段')
        candidate = ''.join(lines).encode('utf-8')
        env_candidate = env_original
        if field == 'rtsp':
            replacement = 'RTSP_IF=' + value + '\n'
            if env_indices:
                old_line = env_lines[env_indices[0]]
                match = re.fullmatch(r'(\s*(?:export\s+)?RTSP_IF\s*=\s*)([^#;\r\n]*?)([ \t]*(?:[#;].*)?)(\r?\n)?', old_line)
                if not match:
                    raise ValueError('env RTSP_IF 语法不明确')
                env_lines[env_indices[0]] = match[1] + value + match[3] + (match[4] or '')
            else:
                if env_lines and not env_lines[-1].endswith('\n'):
                    env_lines[-1] += '\n'
                env_lines.append(replacement)
            env_candidate = ''.join(env_lines).encode('utf-8')
        paths = [(config, original, candidate, meta)]
        if field == 'rtsp':
            paths.append((state, env_original, env_candidate, env_meta))
        active = subprocess.run(['systemctl', 'show', '--property=ActiveState', '--value', service],
                                capture_output=True, text=True, check=True).stdout.strip()
        if active not in ('active', 'inactive', 'failed'):
            raise ValueError('服务状态不稳定或无法确定；取消应用')
        staged, backups, replaced = [], [], []
        service_restored = True

        def report_retained(message):
            retained = [name for name in backups if name and os.path.exists(name)]
            print(message, file=sys.stderr)
            if retained:
                print('❌ 已保留本次可恢复备份：' + '、'.join(retained), file=sys.stderr)

        try:
            for path, old, new, metadata in paths:
                staged.append(stage(path, new, metadata))
                backups.append(stage(path, old, metadata) if old is not None else None)
            for path, old, new, metadata in paths:
                current, current_meta = snapshot(path, old is None)
                identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_mtime_ns) if s else None
                if current != old or identity(current_meta) != identity(metadata):
                    raise ValueError('文件已被外部修改；取消应用')
            stamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')
            for (path, old, new, metadata), backup in zip(paths, backups):
                if backup:
                    dest = str(path) + '.bak-' + stamp
                    os.replace(backup, dest)
                    backups[backups.index(backup)] = dest
            for n, (path, old, new, metadata) in enumerate(paths):
                blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})
                try:
                    os.replace(staged[n], path)
                    replaced.append(n)
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
            if subprocess.run(['systemctl', 'restart', service]).returncode or subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode:
                raise ValueError('服务应用失败')
            print('✅ 已应用所选字段并重启 ' + service)
            if field == 'rtsp':
                print('RTSP 接口（回放）：' + rtsp_display(value, fallback))
        except BaseException as error:
            service_restored = not replaced
            for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                signal.signal(signum, signal.SIG_IGN)
            restore_error = None
            for n in reversed(replaced):
                path, old, new, metadata = paths[n]
                temporary = None
                try:
                    if old is None:
                        path.unlink()
                    else:
                        temporary = stage(path, old, metadata)
                        os.replace(temporary, path)
                        temporary = None
                except BaseException as item_error:
                    service_restored = False
                    if restore_error is None:
                        restore_error = item_error
                finally:
                    if temporary and os.path.exists(temporary):
                        try:
                            os.unlink(temporary)
                        except OSError:
                            pass
            if restore_error is not None:
                report_retained('❌ 恢复配置 / 状态失败：' + str(restore_error))
                print('❌ 配置 / 状态未完整恢复；未删除本次备份。', file=sys.stderr)
            if replaced:
                operation = 'restart' if active == 'active' else 'stop'
                if restore_error is None:
                    print('⚠️ 已恢复旧配置 / 状态；尝试恢复所选实例原先的运行状态。', file=sys.stderr)
                result = subprocess.run(['systemctl', operation, service]).returncode
                restored = subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode
                service_matches = not result and (restored == 0) == (active == 'active')
                if restore_error is None:
                    service_restored = service_matches
                    if not service_restored:
                        report_retained('❌ 恢复运行状态失败，请检查 journalctl -u ' + service)
                elif not service_matches:
                    print('❌ 恢复运行状态失败，请检查 journalctl -u ' + service, file=sys.stderr)
            if restore_error is not None:
                raise restore_error from error
            raise
        finally:
            for name in staged:
                if os.path.exists(name):
                    try:
                        os.unlink(name)
                    except OSError:
                        pass
            # Remove only this transaction's backups after the files and service
            # are back in their intended state. A failed restore keeps them.
            # Deletion failure must not turn a completed apply into a settings failure.
            if service_restored:
                failed = []
                for name in backups:
                    if not name or not os.path.exists(name):
                        continue
                    try:
                        os.unlink(name)
                    except OSError as cleanup_error:
                        failed.append(name + '（' + str(cleanup_error) + '）')
                if failed:
                    print('⚠️ 配置已完成，但删除本次备份失败：' + '、'.join(failed), file=sys.stderr)


try:
    main()
except (OSError, ValueError, UnicodeError) as error:
    print('❌ 设置失败：' + str(error), file=sys.stderr)
    sys.exit(1)
PY
}

rtp2httpd_apply_rtsp() {
    rtp2httpd_rtsp_config apply "$2" "$3" "$1" rtsp "$4"
}

rtp2httpd_service_config_path() {
    # Accept only the installer's direct binary -c absolute-path unit syntax.
    python3 - "$1" <<'PY'
import re
import subprocess
import sys
service = sys.argv[1]
if not re.fullmatch(r'rtp2httpd(?:_[A-Za-z0-9][A-Za-z0-9_-]*)?\.service', service):
    sys.exit('service 名无效')
# Refuse overrides rather than guessing which ExecStart is effective.
if subprocess.check_output(['systemctl', 'show', '--property=DropInPaths', '--value', service], text=True).strip():
    sys.exit('存在 service drop-in；请先人工核对配置路径')
unit = subprocess.check_output(['systemctl', 'cat', '--', service], text=True)
section = None
lines = []
for line in unit.splitlines():
    if line.strip().startswith(('#', ';')) or not line.strip():
        continue
    if line.startswith('['):
        section = line.strip()
    elif section == '[Service]' and line.startswith('ExecStart='):
        lines.append(line)
if len(lines) != 1:
    sys.exit('无法确定唯一 ExecStart；未修改')
m = re.fullmatch(r'ExecStart=(/[^\s"\x27]+/rtp2httpd) -c (/[^\s"\x27]+\.conf)', lines[0])
if not m or any(c in m[1] + m[2] for c in '%$\\'):
    sys.exit('不支持的 ExecStart 参数 / 转义；未修改')
print(m[2])
PY
}

rtp2httpd_settings() {
    local choice service="${1:-}" config="${2:-}" state address port multicast fcc rtsp field value answer i
    if [ -z "$service" ]; then
    rtp2httpd_list_instances
    [ "${#RTP2HTTPD_LIST_SERVICES[@]}" -gt 0 ] || { echo "ℹ️ 未找到实例。"; return 0; }
    echo "请选择要设置的实例："
    for i in "${!RTP2HTTPD_LIST_SERVICES[@]}"; do
        printf '  %d）%s\n' "$((i + 1))" "${RTP2HTTPD_LIST_SERVICES[$i]}"
    done
    echo "  0）返回"
    read -r -p "请输入要操作的序号: " choice || return 0
    [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#RTP2HTTPD_LIST_SERVICES[@]}" ] || return 0
    service="${RTP2HTTPD_LIST_SERVICES[$((choice - 1))]}"
    fi
    config="$(rtp2httpd_service_config_path "$service")" || return 1
    state="${config%.conf}.env"
    address="$(rtp2httpd_rtsp_config get "$config" "$state" address)" || return 1
    port="$(rtp2httpd_rtsp_config get "$config" "$state" port)" || return 1
    multicast="$(rtp2httpd_rtsp_config get "$config" "$state" multicast)" || return 1
    fcc="$(rtp2httpd_rtsp_config get "$config" "$state" fcc)" || return 1
    rtsp="$(rtp2httpd_rtsp_config get "$config" "$state" rtsp-display)" || return 1
    if ! rtp2httpd_apply_owned_dns "$state"; then
        echo "❌ 所选实例的 IPTV 连接 DNS 策略未完成；未进入其他设置。"
        return 1
    fi
    echo "仅修改服务 conf 字段；不重建 VLAN。未选字段保留原值。"
    echo "  1）监听地址：${address}"
    echo "  2）监听端口：${port}"
    echo "  3）组播接口（直播）：${multicast:-未设置}"
    echo "  4）FCC 接口（快速换台）：${fcc:-未设置}"
    echo "  5）RTSP 接口（回放）：${rtsp}"
    echo "  0）返回"
    read -r -p "请选择单项修改: " choice || return 0
    case "$choice" in
        1) field=address ;;
        2) field=port ;;
        3) field=multicast ;;
        4) field=fcc ;;
        5)
            field=rtsp
            rtp2httpd_prompt_rtsp "$fcc" "$multicast" || return 0
            value="$RTP2HTTPD_RTSP_IF"
            ;;
        *) return 0 ;;
    esac
    if [ "$field" != rtsp ]; then
        read -r -p "请输入新值（回车保留当前值并返回；接口必须已存在）: " value || return 0
        [ -n "$value" ] || return 0
    fi
    if [ "$field" = rtsp ] && [ -z "$value" ]; then
        echo "ℹ️ 将移除 RTSP 专用项；若通用 upstream-interface 存在，实际仍回退到该接口，不会删除通用项。"
    fi
    read -r -p "将备份并应用所选字段，仅重启 ${service}；失败恢复旧配置并尝试恢复服务。继续？[y/N]: " answer || return 0
    [[ "$answer" =~ ^[yY]([eE][sS])?$ ]] || return 0
    rtp2httpd_rtsp_config apply "$config" "$state" "$service" "$field" "$value"
}

rtp2httpd_install() {
    local dockerapps answer mcast_name fcc_name mcast_sysctl_if tmp_conf tmp_service fcc_network_start_pre
    local shared_vlan=0 fcc_ipv4_args=()
    local choice service instance i
    # Use the same service inventory as “查看已安装信息”, across app directories.
    # Existing settings resolve ExecStart safely; no install/network dependency
    # checks or directory creation are needed until explicitly creating an instance.
    rtp2httpd_list_instances
    if [ "${#RTP2HTTPD_LIST_SERVICES[@]}" -gt 0 ]; then
        echo "请选择已有实例进行设置，或新建实例："
        for i in "${!RTP2HTTPD_LIST_SERVICES[@]}"; do
            service="${RTP2HTTPD_LIST_SERVICES[$i]}"
            instance="${service#${RTP2HTTPD_SERVICE_BASE}}"
            instance="${instance%.service}"
            instance="${instance#_}"
            printf '  %d）%s（%s）\n' "$((i + 1))" "${instance:-默认}" "$service"
        done
        echo "  n）新建实例"
        echo "  0）返回"
        read -r -p "请输入要操作的序号: " choice || return 0
        case "$choice" in
            n|N) ;;
            *)
                [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#RTP2HTTPD_LIST_SERVICES[@]}" ] || return 0
                rtp2httpd_settings "${RTP2HTTPD_LIST_SERVICES[$((choice - 1))]}"
                return $?
                ;;
        esac
    fi
    rtp2httpd_require_commands || return 1
    select_dockerapps_dir "安装IPTV(rtp2httpd)"
    case $? in 0) dockerapps="$SELECTED_DOCKERAPPS_DIR" ;; 2) return 0 ;; *) return 1 ;; esac
    RTP2HTTPD_APP_DIR="${dockerapps}/rtp2httpd"
    rtp2httpd_prompt_instance
    case $? in 0) ;; 2) return 0 ;; *) return 1 ;; esac

    if [ -e "$RTP2HTTPD_CONFIG_PATH" ] || [ -e "$RTP2HTTPD_STATE_PATH" ] || [ -e "$RTP2HTTPD_SERVICE_PATH" ]; then
        echo "ℹ️ 已有配置 ${RTP2HTTPD_INSTANCE}：载入当前服务参数；父接口 / VLAN / 地址获取方式保持现状。"
        rtp2httpd_settings "$RTP2HTTPD_SERVICE" "$RTP2HTTPD_CONFIG_PATH"
        return $?
    fi

    rtp2httpd_select_parent
    case $? in 0) ;; 2) return 0 ;; *) return 1 ;; esac
    RTP2HTTPD_MULTICAST_VLAN="$(rtp2httpd_prompt_vlan '请输入组播 VLAN ID（回车退出）: ')" || return 0
    RTP2HTTPD_FCC_VLAN="$(rtp2httpd_prompt_vlan '请输入 FCC/DHCP VLAN ID（回车退出）: ')" || return 0
    if [ "$RTP2HTTPD_MULTICAST_VLAN" = "$RTP2HTTPD_FCC_VLAN" ]; then
        shared_vlan=1
        echo "ℹ️ 组播和 FCC/DHCP 共用 VLAN ${RTP2HTTPD_MULTICAST_VLAN}。"
    fi
    rtp2httpd_prompt_fcc_ipv4
    case $? in 0) ;; 2) return 0 ;; *) return 1 ;; esac
    rtp2httpd_prompt_igmp_version || return 0
    rtp2httpd_prompt_bind || return 0
    RTP2HTTPD_RTSP_PLANNED=1
    rtp2httpd_prompt_rtsp "${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_FCC_VLAN}" "${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_MULTICAST_VLAN}"
    local rtsp_result=$?
    RTP2HTTPD_RTSP_PLANNED=0
    [ "$rtsp_result" -eq 0 ] || return "$rtsp_result"
    rtp2httpd_fcc_routing_params || return 1

    mkdir -p "$RTP2HTTPD_APP_DIR" || return 1
    if [ ! -x "${RTP2HTTPD_APP_DIR}/rtp2httpd" ]; then
        echo "⬇️ 未找到共享 rtp2httpd 二进制，正在下载…"
        rtp2httpd_download_binary "$RTP2HTTPD_APP_DIR" || return 1
    fi

    download_yehbp_asset "assets/iptv/rtp2httpd.conf.tpl" "${RTP2HTTPD_APP_DIR}/rtp2httpd.conf.tpl" || return 1
    download_yehbp_asset "assets/iptv/rtp2httpd.service.tpl" "${RTP2HTTPD_APP_DIR}/rtp2httpd.service.tpl" || return 1

    mcast_name="rtp2httpd-${RTP2HTTPD_INSTANCE}-mcast-${RTP2HTTPD_PARENT_IF}-${RTP2HTTPD_MULTICAST_VLAN}"
    fcc_name="rtp2httpd-${RTP2HTTPD_INSTANCE}-fcc-${RTP2HTTPD_PARENT_IF}-${RTP2HTTPD_FCC_VLAN}"
    if nmcli connection show "$mcast_name" >/dev/null 2>&1 || \
       { [ "$shared_vlan" -eq 0 ] && nmcli connection show "$fcc_name" >/dev/null 2>&1; }; then
        echo "❌ 同名 NetworkManager profile 已存在；为避免覆盖未知配置，已取消。"
        return 1
    fi

    if ! nmcli connection add type vlan con-name "$mcast_name" ifname "${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_MULTICAST_VLAN}" dev "$RTP2HTTPD_PARENT_IF" id "$RTP2HTTPD_MULTICAST_VLAN" >/dev/null; then
        echo "❌ 无法创建组播 VLAN profile。"
        return 1
    fi
    RTP2HTTPD_MULTICAST_PROFILE_UUID="$(nmcli -g connection.uuid connection show "$mcast_name")"
    if [ "$shared_vlan" -eq 0 ] && ! nmcli connection modify uuid "$RTP2HTTPD_MULTICAST_PROFILE_UUID" connection.autoconnect yes ipv4.method disabled ipv4.never-default yes ipv4.ignore-auto-dns yes ipv6.method disabled ipv6.ignore-auto-dns yes; then
        nmcli connection delete uuid "$RTP2HTTPD_MULTICAST_PROFILE_UUID" >/dev/null 2>&1 || true
        return 1
    fi

    if [ "$shared_vlan" -eq 1 ]; then
        RTP2HTTPD_FCC_PROFILE_UUID="$RTP2HTTPD_MULTICAST_PROFILE_UUID"
        fcc_network_start_pre="# FCC/DHCP 与组播共用 VLAN，仅启用一次 NetworkManager profile"
    else
        if ! nmcli connection add type vlan con-name "$fcc_name" ifname "${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_FCC_VLAN}" dev "$RTP2HTTPD_PARENT_IF" id "$RTP2HTTPD_FCC_VLAN" >/dev/null; then
            nmcli connection delete uuid "$RTP2HTTPD_MULTICAST_PROFILE_UUID" >/dev/null 2>&1 || true
            echo "❌ 无法创建 FCC/DHCP VLAN profile。"
            return 1
        fi
        RTP2HTTPD_FCC_PROFILE_UUID="$(nmcli -g connection.uuid connection show "$fcc_name")"
        fcc_network_start_pre="ExecStartPre=/usr/bin/nmcli --wait 60 connection up uuid ${RTP2HTTPD_FCC_PROFILE_UUID}"
    fi
    if [ "$RTP2HTTPD_FCC_IPV4_METHOD" = "static" ]; then
        fcc_ipv4_args=(ipv4.method manual ipv4.addresses "$RTP2HTTPD_FCC_IPV4_ADDRESS" ipv4.gateway "$RTP2HTTPD_FCC_IPV4_GATEWAY")
    else
        # DHCP 提供地址。IPv4/IPv6 自动 DNS 都显式忽略；IPv6 方法保持 disabled。
        fcc_ipv4_args=(ipv4.method auto ipv4.ignore-auto-dns yes)
    fi
    if ! nmcli connection modify uuid "$RTP2HTTPD_FCC_PROFILE_UUID" \
        connection.autoconnect yes "${fcc_ipv4_args[@]}" ipv4.never-default no \
        ipv4.route-table "$RTP2HTTPD_FCC_ROUTE_TABLE" \
        ipv4.routing-rules "priority ${RTP2HTTPD_FCC_ROUTE_PRIORITY} oif ${RTP2HTTPD_PARENT_IF}.${RTP2HTTPD_FCC_VLAN} table ${RTP2HTTPD_FCC_ROUTE_TABLE}" \
        ipv6.method disabled ipv6.ignore-auto-dns yes; then
        [ "$shared_vlan" -eq 1 ] || nmcli connection delete uuid "$RTP2HTTPD_FCC_PROFILE_UUID" >/dev/null 2>&1 || true
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
        "__RTSP_INTERFACE_CONFIG__=$(rtp2httpd_rtsp_line)" \
        "__BIND_ADDRESS__=${RTP2HTTPD_BIND_ADDRESS}" "__PORT__=${RTP2HTTPD_PORT}" || return 1
    rtp2httpd_render_template "${RTP2HTTPD_APP_DIR}/rtp2httpd.service.tpl" "$tmp_service" \
        "__APP_DIR__=${RTP2HTTPD_APP_DIR}" "__CONFIG_PATH__=${RTP2HTTPD_CONFIG_PATH}" \
        "__MULTICAST_PROFILE_UUID__=${RTP2HTTPD_MULTICAST_PROFILE_UUID}" \
        "__FCC_PROFILE_UUID__=${RTP2HTTPD_FCC_PROFILE_UUID}" \
        "__FCC_NETWORK_START_PRE__=${fcc_network_start_pre}" \
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

rtp2httpd_show_config() {
    local choice service instance config i
    local -a selected_services=()

    rtp2httpd_list_instances
    if [ ${#RTP2HTTPD_LIST_SERVICES[@]} -eq 0 ]; then
        echo "ℹ️ 未找到 rtp2httpd 开机启动服务（rtp2httpd.service 或 rtp2httpd_*.service）。"
        return 0
    fi
    echo "目前已安装的rtp2httpd信息："
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
        if [ -z "$config" ] || [ ! -r "$config" ]; then
            echo "❌ 无法读取 ${service} 的配置文件：${config:-未找到}"
            continue
        fi
        printf '\n=== %s（%s）===\n' "$service" "$config"
        cat "$config"
        rtp2httpd_rtsp_config status "$config" "${config%.conf}.env" || return 1
    done
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
        echo "✅ 已删除 rtp2httpd 配置 ${display_name} 和关联 service；已验证归属的 VLAN profile 已处理，跳过的连接请按上述提示核对；共享二进制已保留。"
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
    echo "1）查看配置"
    echo "2）安装/设置 rtp2httpd"
    echo "3）删除配置"
    echo "4）升级rtp2httpd版本"
    echo "5）重启 rtp2httpd 实例"
    echo "0）返回"
    read -r -p "请输入要操作的序号: " choice
    case "$choice" in
        1) rtp2httpd_show_config ;;
        2) rtp2httpd_install ;;
        3) rtp2httpd_delete ;;
        4) rtp2httpd_upgrade ;;
        5) rtp2httpd_restart ;;
        *) return 0 ;;
    esac
}
