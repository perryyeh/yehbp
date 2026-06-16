#!/bin/bash

APP_NAME="yehbp"
APP_TITLE="Yeh Bypass (Gateway)"
APP_VERSION="2026.06.16.1"
REPO_URL="https://github.com/perryyeh/yehbp"
RAW_INSTALL_URL="https://github.com/perryyeh/yehbp/raw/refs/heads/main/install.sh"
RAW_VERSION_URL="https://github.com/perryyeh/yehbp/raw/refs/heads/main/VERSION"
INSTALL_BIN="/usr/local/bin/${APP_NAME}"

download_yehbp_script() {
    local dst="$1"
    local url="${RAW_INSTALL_URL}?t=$(date +%s)"

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$dst" || return 1
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$dst" "$url" || return 1
    else
        echo "❌ 未找到 curl 或 wget，无法下载安装脚本。"
        return 1
    fi

    bash -n "$dst" || {
        echo "❌ 下载的新脚本语法检查失败，已取消。"
        return 1
    }
}

fetch_remote_yehbp_version() {
    local url="${RAW_VERSION_URL}?t=$(date +%s)"

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" 2>/dev/null | tr -d '[:space:]'
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$url" 2>/dev/null | tr -d '[:space:]'
    else
        return 1
    fi
}

install_yehbp_from_file() {
    local src="$1"
    local backup=""

    mkdir -p "$(dirname "$INSTALL_BIN")" || return 1

    if [ -f "$INSTALL_BIN" ]; then
        backup="${INSTALL_BIN}.bak-$(date +%Y%m%d-%H%M%S)"
        cp -a "$INSTALL_BIN" "$backup" || return 1
        echo "🧩 已备份当前版本：$backup"
    fi

    install -m 0755 "$src" "$INSTALL_BIN" || return 1
}

install_yehbp_cli() {
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        echo "❌ 安装 ${APP_NAME} 需要 root 权限，请使用 sudo。"
        return 1
    fi

    local tmp
    tmp="$(mktemp /tmp/${APP_NAME}.install.XXXXXX)" || return 1
    trap 'rm -f "$tmp"' RETURN

    echo "⬇️ 正在安装 ${APP_TITLE} 到 ${INSTALL_BIN} ..."
    download_yehbp_script "$tmp" || return 1
    install_yehbp_from_file "$tmp" || return 1

    echo "✅ 安装完成：${INSTALL_BIN}"
    echo "👉 以后直接运行：${APP_NAME}"
    echo "👉 每次运行会自动检查新版本；确认后才会升级。"
}

update_yehbp_cli() {
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        echo "❌ 升级 ${APP_NAME} 需要 root 权限，请使用 sudo。"
        return 1
    fi

    local tmp backup
    tmp="$(mktemp /tmp/${APP_NAME}.update.XXXXXX)" || return 1
    backup="${INSTALL_BIN}.bak-$(date +%Y%m%d-%H%M%S)"
    trap 'rm -f "$tmp"' RETURN

    echo "⬆️ 正在升级 ${APP_TITLE} ..."
    download_yehbp_script "$tmp" || return 1

    [ ! -f "$INSTALL_BIN" ] && echo "ℹ️ 未检测到 ${INSTALL_BIN}，将执行首次安装。"
    install_yehbp_from_file "$tmp" || return 1
    echo "✅ 升级完成：${INSTALL_BIN}"
}

check_yehbp_update() {
    local tmp remote_version ans

    remote_version="$(fetch_remote_yehbp_version)"
    if [ -z "$remote_version" ]; then
        echo "⚠️ 无法识别远程版本，继续使用当前版本：${APP_VERSION}"
        return 0
    fi

    if [ "$remote_version" = "$APP_VERSION" ]; then
        return 0
    fi

    echo "⬆️ 检测到 ${APP_TITLE} 新版本：${APP_VERSION} -> ${remote_version}"
    read -r -p "是否现在升级？[y/N]: " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        if [ "${EUID:-$(id -u)}" -ne 0 ]; then
            echo "❌ 升级需要 root 权限，请使用 sudo ${APP_NAME} 后重试。"
            return 0
        fi

        tmp="$(mktemp /tmp/${APP_NAME}.update.XXXXXX)" || {
            echo "❌ 无法创建临时文件，继续使用当前版本。"
            return 0
        }
        trap 'rm -f "$tmp"' RETURN

        download_yehbp_script "$tmp" || {
            echo "❌ 下载新版脚本失败，继续使用当前版本。"
            return 0
        }
        install_yehbp_from_file "$tmp" || {
            echo "❌ 升级失败，继续使用当前版本。"
            return 0
        }
        echo "✅ 已升级到 ${remote_version}。请重新运行 ${APP_NAME}。"
        exit 0
    fi

    echo "ℹ️ 已跳过升级，继续使用当前版本：${APP_VERSION}"
}

case "${1:-}" in
    install|--install)
        install_yehbp_cli
        exit $?
        ;;
    "")
        # curl ... | sudo bash：脚本从 stdin 进入时默认执行安装。
        # 已安装后的 yehbp 无参数运行仍进入菜单。
        if [ ! -t 0 ]; then
            case "$(basename "$0")" in
                bash|sh|-bash|-sh)
                    install_yehbp_cli
                    exit $?
                    ;;
            esac
        fi
        ;;
    update|--update)
        update_yehbp_cli
        exit $?
        ;;
esac

check_yehbp_update

# ========== 环境准备 ==========

install_dependencies() {
    echo "🔧 检查并安装依赖（自动适配系统）..."

    deps=(ipcalc curl jq tar)

    # 统一检测函数
    need_install() {
        ! command -v "$1" >/dev/null 2>&1
    }

    # === 1️⃣ Debian / Ubuntu / Armbian ===
    if command -v apt-get >/dev/null 2>&1; then
        echo "📦 使用 apt-get 安装依赖"
        local to_install=()
        for dep in "${deps[@]}"; do
            if need_install "$dep"; then
                to_install+=("$dep")
            else
                echo "✅ $dep 已安装"
            fi
        done

        if [ ${#to_install[@]} -gt 0 ]; then
            echo "⬇️ 正在安装缺少的依赖: ${to_install[*]}"
            apt-get update
            apt-get install -y "${to_install[@]}"
        fi
        return 0
    fi

    # === 2️⃣ 群晖 / 飞牛 OS（Entware）===
    if [ -x /opt/bin/opkg ]; then
        echo "📦 使用 Entware(opkg) 安装依赖"
        export PATH=/opt/bin:$PATH

        local to_install=()
        for dep in "${deps[@]}"; do
            if need_install "$dep"; then
                to_install+=("$dep")
            else
                echo "✅ $dep 已安装"
            fi
        done
        
        if [ ${#to_install[@]} -gt 0 ]; then
             echo "⬇️ 正在安装缺少的依赖: ${to_install[*]}"
            /opt/bin/opkg update
            /opt/bin/opkg install "${to_install[@]}"
        fi

        return 0
    fi

    # === 3️⃣ 兜底 ===
    echo "❌ 未识别的系统，无法自动安装依赖"
    echo "👉 请手动安装：${deps[*]}"
    return 1
}

echo "⚠️ 请以 root 权限运行 ${APP_TITLE}"

# ========== 主菜单 ==========

function show_menu() {
    clear
    echo "============================"
    echo "${APP_TITLE}"
    echo "本脚本提供以下功能："
    echo "----------------------------"
    echo "0）显示菜单"
    echo "1）显示操作系统信息"
    echo "2）显示网卡信息"
    echo "3）显示磁盘信息"
    echo "4）显示docker信息"
    echo "5）格式化磁盘并挂载"
    echo "7）安装docker"
    echo "8）创建macvlan（包括ipv4+ipv6）"
    echo "9）清理macvlan"
    echo "10）安装portainer面板"
    echo "11）安装librespeed测速"
    echo "14）安装adguardhome"
    echo "19）安装mosdns"
    echo "20）安装mihomo"
    echo "21）安装ddns-go"
    echo "22）安装lucky"
    echo "70) 迁移docker目录"
    echo "71) 优化docker日志"
    echo "72) 优化journald日志"
    echo "90）创建macvlan bridge"
    echo "91）清理macvlan bridge"
    echo "97）安装watchtower自动更新"
    echo "98）强制使用watchtower更新一次镜像"
    echo "99）退出"
    echo "============================"
}

# ========== 工具函数 ==========

# 全局保存用户选择的 macvlan 网络名
SELECTED_MACVLAN=""

# 选择macvlan
select_macvlan_or_exit() {
    mapfile -t macvlan_networks < <(docker network ls --format '{{.Name}}' | grep '^macvlan' || true)
    if [ ${#macvlan_networks[@]} -eq 0 ]; then
        echo "❌ 未发现任何以 macvlan 开头的 Docker 网络，请先创建 macvlan 网络。"
        return 1
    fi

    echo "可用的 macvlan 网络："
    for i in "${!macvlan_networks[@]}"; do
        echo "  $i) ${macvlan_networks[$i]}"
    done

    read -r -p "请输入要使用的 macvlan 序号（回车退出安装）: " choice
    if [ -z "$choice" ]; then
        echo "✅ 已退出安装。"
        return 2
    fi
    if [[ ! "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 0 ] || [ "$choice" -ge "${#macvlan_networks[@]}" ]; then
        echo "❌ 无效的序号：$choice"
        return 1
    fi

    SELECTED_MACVLAN="${macvlan_networks[$choice]}"
    echo "📡 选中的 macvlan 网络: $SELECTED_MACVLAN"
    return 0
}

# 计算IP地址对应MAC地址
ip_to_mac() {
  # IPv4 -> MAC: 02:<ip1hex>:<ip2hex>:<ip3hex>:<ip4hex>:86
  # 例：10.0.10.254 -> 02:0a:56:14:fe:86
  local ip1 ip2 ip3 ip4
  IFS='.' read -r ip1 ip2 ip3 ip4 <<< "$1"

  # 基本校验（避免空/非数字）
  if [[ ! "$ip1" =~ ^[0-9]+$ || ! "$ip2" =~ ^[0-9]+$ || ! "$ip3" =~ ^[0-9]+$ || ! "$ip4" =~ ^[0-9]+$ ]]; then
    echo ""
    return 1
  fi
  if (( ip1<0 || ip1>255 || ip2<0 || ip2>255 || ip3<0 || ip3>255 || ip4<0 || ip4>255 )); then
    echo ""
    return 1
  fi

  printf '02:%02x:%02x:%02x:%02x:86\n' "$ip1" "$ip2" "$ip3" "$ip4"
}

# 计算IPv4对应IPv6前缀
ipv4_to_ipv6_prefix() {
  local ip=$1
  local first_octet=$(echo $ip | cut -d'.' -f1)
  local second_octet=$(echo $ip | cut -d'.' -f2)
  local third_octet=$(echo $ip | cut -d'.' -f3)

  if [[ "$first_octet" == "10" ]]; then
    prefix="fd10"
  elif [[ "$first_octet" == "172" ]]; then
    prefix="fd17"
  elif [[ "$first_octet" == "192" ]]; then
    prefix="fd19"
  else
    prefix="fd00"
  fi

  echo "${prefix}:${second_octet}:${third_octet}"
}

# 获取网卡子网
get_subnet_v4() {
  local ip=$1
  local iface=$2
  local cidr=$(ip route | grep -v "^default" | grep "$iface" | grep "$ip" | awk '{print $1}')
  
  if [ -z "$cidr" ]; then
    local prefix_len=$(ip -4 addr show $iface | grep inet | awk '{print $2}' | cut -d'/' -f2)
    local ipcalc_out
    # 移除 -n 选项以提高兼容性（Busybox ipcalc 可能不支持，或者输出不同）
    ipcalc_out=$(ipcalc "$ip/$prefix_len" 2>/dev/null)

    # 1. 尝试 Debian 格式 (Network: 192.168.1.0/24)
    cidr=$(echo "$ipcalc_out" | grep "Network:" | awk '{print $2}')

    # 2. 尝试 Busybox/Entware 格式 (NETWORK=192.168.1.0 + PREFIX=24)
    if [ -z "$cidr" ]; then
      local net_val prefix_val
      net_val=$(echo "$ipcalc_out" | grep "NETWORK=" | cut -d= -f2)
      prefix_val=$(echo "$ipcalc_out" | grep "PREFIX=" | cut -d= -f2)
      if [ -n "$net_val" ] && [ -n "$prefix_val" ]; then
        cidr="${net_val}/${prefix_val}"
      fi
    fi
  fi
  echo $cidr
}

# ---- IPv4 计算工具 ----
ipv4_to_int() { local IFS=.; read -r a b c d <<<"$1"; echo $(( (a<<24)+(b<<16)+(c<<8)+d )); }

mask_from_len() { local l="$1"; echo $(( (0xFFFFFFFF << (32-l)) & 0xFFFFFFFF )); }

cidr_contains_ip() {
  local ip="$1" cidr="$2" net="${cidr%/*}" len="${cidr#*/}"
  local ipi neti mask; ipi=$(ipv4_to_int "$ip"); neti=$(ipv4_to_int "$net"); mask=$(mask_from_len "$len")
  (( (ipi & mask) == (neti & mask) ))
}

macvlan_ipv6_enabled() {
  # 用法：macvlan_ipv6_enabled "macvlan_name"  ; 返回 0=启用且有IPv6子网，1=否则
  local net="$1"
  docker network inspect "$net" 2>/dev/null | jq -e \
    '.[0].EnableIPv6==true and (.[0].IPAM.Config[]?.Subnet | test(":"))' \
    >/dev/null 2>&1
}

write_env_file() {
  local path="$1"; shift
  # 用法：write_env_file ".env" "k1=v1" "k2=v2" ...
  : > "$path" || return 1
  for line in "$@"; do
    printf '%s\n' "$line" >> "$path" || return 1
  done
}

# 全局保存用户选择的 dockerapps 存储目录
SELECTED_DOCKERAPPS_DIR=""

discover_dockerapps_dirs() {
  # 仅扫描常见挂载点和当前用户家目录，避免全盘 find 过慢或产生大量权限噪音
  local search_roots=()
  local root existing duplicate

  for root in /data /vol* /volume* /mnt /srv /opt "${HOME:-}" "/home/${SUDO_USER:-}"; do
    [ -n "$root" ] && [ -d "$root" ] || continue

    duplicate=0
    for existing in ${search_roots[@]+"${search_roots[@]}"}; do
      if [ "$existing" = "$root" ]; then
        duplicate=1
        break
      fi
    done
    [ "$duplicate" -eq 1 ] && continue

    search_roots+=("$root")
  done

  [ ${#search_roots[@]} -eq 0 ] && return 0

  find "${search_roots[@]}" -maxdepth 4 -type d -name dockerapps 2>/dev/null | sort -u
}

select_dockerapps_dir() {
  # 用法：select_dockerapps_dir "服务名"
  # 返回：0=已选择，2=用户回车退出，1=错误
  local app_name="$1"
  local scan_choice manual_dir choice dir
  local -a dockerapps_dirs=()

  SELECTED_DOCKERAPPS_DIR=""

  echo "即将安装 ${app_name}，请选择存储目录。"
  read -r -p "是否扫描本机已有 dockerapps 目录？[Y/n]: " scan_choice

  case "$scan_choice" in
    ""|"y"|"Y"|"yes"|"YES")
      echo "🔎 正在扫描已有 dockerapps 目录..."
      while IFS= read -r dir; do
        [ -n "$dir" ] && dockerapps_dirs+=("$dir")
      done < <(discover_dockerapps_dirs)

      if [ ${#dockerapps_dirs[@]} -gt 0 ]; then
        echo "发现以下 dockerapps 目录："
        for i in "${!dockerapps_dirs[@]}"; do
          echo "  $((i + 1))) ${dockerapps_dirs[$i]}"
        done

        while true; do
          read -r -p "请选择目录序号，输入 m 手工输入，回车退出: " choice
          if [ -z "$choice" ]; then
            return 2
          fi
          case "$choice" in
            "m"|"M")
              break
              ;;
            *)
              if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#dockerapps_dirs[@]}" ]; then
                SELECTED_DOCKERAPPS_DIR="${dockerapps_dirs[$((choice - 1))]}"
                echo "✅ 使用已有目录：${SELECTED_DOCKERAPPS_DIR}"
                return 0
              fi
              echo "❌ 无效选择：$choice"
              ;;
          esac
        done
      else
        echo "⚠️ 未发现已有 dockerapps 目录。"
      fi
      ;;
    "n"|"N"|"no"|"NO")
      ;;
    *)
      echo "❌ 无效选择：$scan_choice"
      return 1
      ;;
  esac

  read -r -p "请输入存储目录（例如 /data/dockerapps），回车退出: " manual_dir
  if [ -z "$manual_dir" ]; then
    return 2
  fi

  SELECTED_DOCKERAPPS_DIR="$manual_dir"
  echo "✅ 使用手工输入目录：${SELECTED_DOCKERAPPS_DIR}"
  return 0
}

calculate_ip_mac() {
  local last_octet=$1
  local net_name="${2:-${SELECTED_MACVLAN:-macvlan}}"

  if [[ ! "$last_octet" =~ ^[0-9]+$ ]]; then
    echo "❌ calculate_ip_mac 输入无效: $last_octet"
    return 1
  fi

  # 1) 获取 docker 网络配置（改为可选网络名）
  network_info=$(docker network inspect "$net_name" 2>/dev/null) || {
    echo "❌ 无法读取网络信息：$net_name"
    return 1
  }

  # 2) IPv4：优先 IPRange，否则 Subnet
  local iprange subnet gateway
  iprange=$(echo "$network_info" | jq -r '.[0].IPAM.Config[] | select(.Subnet | test(":") | not) | (.IPRange // empty)' | head -n1)
  subnet=$(echo  "$network_info" | jq -r '.[0].IPAM.Config[] | select(.Subnet | test(":") | not) | .Subnet' | head -n1)
  gateway=$(echo "$network_info" | jq -r '.[0].IPAM.Config[] | select(.Subnet | test(":") | not) | (.Gateway // empty)' | head -n1)

  local base4
  if [ -n "$iprange" ] && [ "$iprange" != "null" ]; then
    base4=$(echo "$iprange" | cut -d'/' -f1)
  else
    base4=$(echo "$subnet" | cut -d'/' -f1)
  fi
  if [ -z "$base4" ] || [ "$base4" = "null" ]; then
    echo "❌ 网络 $net_name 没有 IPv4 Subnet/IPRange"
    return 1
  fi

  local ip="${base4%.*}.${last_octet}"

  # 3) IPv6：仅当 EnableIPv6=true 且存在 IPv6 Subnet 才生成 ip6（避免 RA-only 网关坑）
  local enable_ipv6 subnet6 gateway6 ip6_prefix ip6
  enable_ipv6=$(echo "$network_info" | jq -r '.[0].EnableIPv6 // false')
  subnet6=$(echo "$network_info" | jq -r '.[0].IPAM.Config[] | select(.Subnet | test(":")) | .Subnet' | head -n1)
  gateway6=$(echo "$network_info" | jq -r '.[0].IPAM.Config[] | select(.Subnet | test(":")) | (.Gateway // empty)' | head -n1)

  ip6=""
  if [ "$enable_ipv6" = "true" ] && [ -n "$subnet6" ] && [ "$subnet6" != "null" ]; then
    ip6_prefix=$(echo "$subnet6" | cut -d'/' -f1)
    local v4_3 v4_4
    v4_3=$(echo "$ip" | cut -d'.' -f3)
    v4_4=$(echo "$ip" | cut -d'.' -f4)

    if [[ "$ip6_prefix" == *"::" ]]; then
      ip6="${ip6_prefix}${v4_3}:${v4_4}"
    else
      ip6="${ip6_prefix}::${v4_3}:${v4_4}"
    fi
  else
    gateway6=""
  fi

  # 4) MAC
  local mac
  mac=$(ip_to_mac "$ip")

  # 5) 输出/回填
  echo "Network: $net_name"
  echo "IPv4: $ip"
  echo "IPv6: $ip6"
  echo "MAC: $mac"
  echo "Gateway: $gateway"
  echo "Gateway6: $gateway6"

  calculated_ip=$ip
  calculated_ip6=$ip6
  calculated_mac=$mac
  calculated_gateway=$gateway
  calculated_gateway6=$gateway6
}

# ---- 自动探测 mihomo 下一跳 IP（返回一个 IPv4 或空串）----
# 参数1: route4_cidr（如 10.0.1.0/24）
# 参数2: network_info（docker network inspect 的 JSON 字符串）
detect_mihomo_ip() {
  local _route4="$1" _netinfo="$2"

  # 1) 环境变量优先（大写/小写都支持）
  if [ -n "$MIHOMO" ]; then echo "$MIHOMO"; return; fi
  if [ -n "$mihomo" ]; then echo "$mihomo"; return; fi

  # 2) systemd 环境文件（可选）
  if [ -f /etc/default/macvlan_env ]; then
    # shellcheck source=/dev/null
    . /etc/default/macvlan_env
    if [ -n "$MIHOMO" ]; then echo "$MIHOMO"; return; fi
    if [ -n "$mihomo" ]; then echo "$mihomo"; return; fi
  fi

  # 3) Docker 容器：名称含 mihomo/clash/clash-meta 的容器；优先选与 _route4 同网段的 IP
  local ids iplist ip best=""
  ids=$(docker ps --format '{{.ID}} {{.Names}}' | grep -Ei '(^|[ _-])(mihomo|clash-meta|clash)($|[ _-])' | awk '{print $1}')
  for id in $ids; do
    iplist=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$id")
    for ip in $iplist; do
      if [ -n "$ip" ] && [ -n "$_route4" ] && cidr_contains_ip "$ip" "$_route4"; then
        echo "$ip"; return
      fi
      [ -z "$best" ] && best="$ip"
    done
  done
  [ -n "$best" ] && { echo "$best"; return; }

  # 4) 回退到 macvlan 的 IPv4 网关
  local gw4
  gw4=$(echo "$_netinfo" | jq -r '.[0].IPAM.Config[] | select(.Subnet | test(":") | not) | .Gateway // empty' | head -n1)
  [ -n "$gw4" ] && { echo "$gw4"; return; }

  # 5) 无可用
  echo ""
}

# 校验参数
env_require_vars() {
    local env_file="$1"; shift
    local missing=0

    for v in "$@"; do
        if ! grep -q "^${v}=" "$env_file"; then
            echo "❌ $env_file 缺少必要变量：$v"
            missing=1
        fi
    done

    [ "$missing" -eq 0 ]
}

remove_compose_ipv6_fields() {
  local compose_file="${1:-docker-compose.yml}"
  [ -f "$compose_file" ] || return 0

  # 无 IPv6 场景：删除固定 IPv6 地址，避免 compose 校验/启动报错。
  sed -i "/ipv6_address: \${ipv6}/d" "$compose_file"

  # 同时删除仅用于 IPv6 RA 路由学习的 endpoint sysctl；IPv4-only macvlan 不需要它。
  sed -i '/driver_opts:/{
    N
    /com\.docker\.network\.endpoint\.sysctls: net\.ipv6\.conf\.IFNAME\.accept_ra_rt_info_max_plen=128/d
  }' "$compose_file"
}

prompt_ipv4_last_octet() {
  # 用法：prompt_ipv4_last_octet "提示语" 默认值
  local prompt="$1"
  local def="$2"
  local v

  read -r -p "$prompt" v
  v="${v:-$def}"

  if [[ ! "$v" =~ ^[0-9]+$ ]] || [ "$v" -lt 1 ] || [ "$v" -gt 254 ]; then
    echo "❌ 无效的 IPv4 最后一段：$v"
    return 1
  fi

 echo "📌 使用 IPv4 最后一段：$v" >&2
 echo "$v"
}

# 仓库更新
repo_stage_update() {
  local name="$1"
  local base="$2"
  local repo_url="$3"
  local dir_name="$4"

  local ts; ts="$(date +%Y%m%d-%H%M%S)"

  TARGET_DIR="${base%/}/${dir_name}"
  WORK_DIR=""
  NEED_SWITCH=0
  NEXT_DIR=""
  BAK_DIR=""

  # === 将 .git URL 转成 tar.gz URL ===
  local tar_url
  tar_url="$(echo "$repo_url" | sed 's/\.git$//')/archive/refs/heads/main.tar.gz"

  # === 若默认分支不是 main，可 fallback master ===
  #（可选增强：后面可以自动探测 default branch）

  if [ -d "$TARGET_DIR" ]; then
    echo "🔄 [$name] 检测到现有目录：$TARGET_DIR（使用 tar.gz 方式部署 next）"

    local tmp="${base%/}/${dir_name}.tmp-${ts}"
    NEXT_DIR="${base%/}/${dir_name}.next-${ts}"
    BAK_DIR="${base%/}/${dir_name}.bak-${ts}"

    rm -rf "$tmp" "$NEXT_DIR" 2>/dev/null || true
    mkdir -p "$tmp" || return 1

    if curl -L "$tar_url" | tar -xz -C "$tmp" --strip-components=1; then
      mv "$tmp" "$NEXT_DIR"
      WORK_DIR="$NEXT_DIR"
      NEED_SWITCH=1
      echo "✅ [$name] next 目录已准备：$NEXT_DIR"
      return 0
    fi

    echo "❌ [$name] next tar 下载失败，保留现状避免断服"
    rm -rf "$tmp" "$NEXT_DIR" 2>/dev/null || true
    return 1
  fi

  # 首次部署（无 next）
  echo "⬇️ [$name] 首次部署，使用 tar.gz 克隆到正式目录：$TARGET_DIR"
  mkdir -p "$TARGET_DIR" || return 1

  if curl -L "$tar_url" | tar -xz -C "$TARGET_DIR" --strip-components=1; then
    WORK_DIR="$TARGET_DIR"
    NEED_SWITCH=0
    return 0
  fi

  echo "❌ [$name] tar 下载失败"
  return 1
}

# 容器层（停旧 → 起新 → 更新/回滚）
compose_deploy_with_repo_switch() {
  # 用法（推荐）：
  #   compose_deploy_with_repo_switch "mihomo" "mihomo" docker-compose.yml docker-compose.ipv6.yml
  #
  # 依赖 repo_stage_update 已经被调用过，且设置了全局变量：
  #   WORK_DIR NEED_SWITCH TARGET_DIR BAK_DIR

  local name="$1"; shift
  local svc="$1"; shift
  local -a files=("$@")

  local -a COMPOSE
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    echo "❌ 未找到 docker compose / docker-compose"
    return 1
  fi

  [ ${#files[@]} -eq 0 ] && files=("docker-compose.yml")

  local -a fargs=()
  for f in "${files[@]}"; do fargs+=("-f" "$f"); done

  # ✅ 固定 project name：确保 next/正式 两次 up 属于同一个项目
  local PROJECT
  PROJECT="$name"         # 或者你想用 "$svc" / dir_name，都行，但要稳定
  local -a pargs=(-p "$PROJECT")

  # A) 先在 WORK_DIR 做 config 校验（不碰容器）
  cd "$WORK_DIR" || { echo "❌ 进入目录失败：$WORK_DIR"; return 1; }

  echo "🔎 [$name] docker compose config 校验..."
  if ! "${COMPOSE[@]}" "${pargs[@]}" "${fargs[@]}" config >/tmp/"$name".compose.check 2>/tmp/"$name".compose.err; then
    echo "❌ [$name] compose 校验失败："
    sed 's/^/  /' /tmp/"$name".compose.err
    return 1
  fi

  # B) 备份旧容器（stop + rename）用于回滚
  local ts backup_cname old_running=""
  ts="$(date +%Y%m%d-%H%M%S)"
  backup_cname=""

  if docker ps -a --format '{{.Names}}' | grep -qx "$svc"; then
    backup_cname="${svc}.bak-${ts}"
    old_running="$(docker inspect -f '{{.State.Running}}' "$svc" 2>/dev/null || echo "")"

    echo "🧩 [$name] 发现旧容器 $svc，先停止并重命名为备份：$backup_cname"
    docker stop "$svc" >/dev/null 2>&1 || true

    if docker ps -a --format '{{.Names}}' | grep -qx "$backup_cname"; then
      echo "❌ [$name] 备份容器名已存在：$backup_cname（请手动处理后重试）"
      return 1
    fi

    docker rename "$svc" "$backup_cname" || {
      echo "❌ [$name] 旧容器重命名失败（无法避免 container_name 冲突）"
      return 1
    }
  fi

  rollback_container() {
    # 删除新容器（如果占名）
    if docker ps -a --format '{{.Names}}' | grep -qx "$svc"; then
      docker rm -f "$svc" >/dev/null 2>&1 || true
    fi
    # 还原旧容器
    if [ -n "$backup_cname" ] && docker ps -a --format '{{.Names}}' | grep -qx "$backup_cname"; then
      docker rename "$backup_cname" "$svc" >/dev/null 2>&1 || true
      [ "$old_running" = "true" ] && docker start "$svc" >/dev/null 2>&1 || true
      echo "🔁 [$name] 已回滚恢复旧容器：$svc"
    fi
  }

  rollback_dir() {
    # 仅当我们真的把正式目录备份走了，才尝试回滚目录
    if [ -n "${BAK_DIR:-}" ] && [ -d "$BAK_DIR" ]; then
      rm -rf "$TARGET_DIR" 2>/dev/null || true
      mv "$BAK_DIR" "$TARGET_DIR" 2>/dev/null || true
      WORK_DIR="$TARGET_DIR"
      NEED_SWITCH=0
      echo "🔁 [$name] 已回滚恢复旧目录：$TARGET_DIR"
    fi
  }

  # C) 在 WORK_DIR 启动新容器（next 或正式都一样）
  echo "🚀 [$name] 启动新容器（WORK_DIR=$WORK_DIR）..."
  if ! "${COMPOSE[@]}" "${pargs[@]}" "${fargs[@]}" up -d --force-recreate; then
    echo "❌ [$name] 新容器启动失败，开始回滚..."
    rollback_container
    return 1
  fi

  # D) 如果 NEED_SWITCH=1：切 next -> 正式，并在正式目录再 up 一次（挂载稳定）
  if [ "${NEED_SWITCH:-0}" -eq 1 ]; then
    echo "🔁 [$name] 新容器运行成功，开始切换目录：next -> 正式"

    # 备份旧目录（如果存在）
    if [ -d "$TARGET_DIR" ]; then
      [ -z "${BAK_DIR:-}" ] && BAK_DIR="${TARGET_DIR}.bak-${ts}"
      mv "$TARGET_DIR" "$BAK_DIR" || {
        echo "❌ [$name] 备份旧目录失败：$TARGET_DIR"
        rollback_container
        return 1
      }
    fi

    # next -> 正式
    if ! mv "$WORK_DIR" "$TARGET_DIR"; then
      echo "❌ [$name] next -> 正式目录切换失败，开始回滚..."
      rollback_dir
      rollback_container
      return 1
    fi

    WORK_DIR="$TARGET_DIR"
    NEED_SWITCH=0

    # 在正式目录再强制重建一次，确保挂载源稳定到正式路径
    cd "$WORK_DIR" || { echo "❌ 进入目录失败：$WORK_DIR"; rollback_dir; rollback_container; return 1; }
    echo "🚀 [$name] 在正式目录再次重建（确保挂载路径稳定）..."
    if ! "${COMPOSE[@]}" "${pargs[@]}" "${fargs[@]}" up -d --force-recreate; then
      echo "❌ [$name] 正式目录重建失败，开始回滚..."
      rollback_dir
      rollback_container
      return 1
    fi
  fi

  # E) 最终 running 检查
  sleep 1
  if ! docker inspect -f '{{.State.Running}}' "$svc" 2>/dev/null | grep -q true; then
    echo "❌ [$name] 容器未处于 running：$svc"
    docker logs --tail=80 "$svc" 2>/dev/null || true
    echo "❌ [$name] running 检查失败，开始回滚..."
    rollback_dir
    rollback_container
    return 1
  fi

  DEPLOY_BACKUP_CONTAINER="$backup_cname"
  [ -n "$backup_cname" ] && echo "✅ [$name] 新容器启动成功，旧容器已备份：$backup_cname" && echo "🧩 确认稳定后可手动 docker rm -f ${DEPLOY_BACKUP_CONTAINER}删除"

  return 0
}

# 删除备份+检查
repo_offer_delete_backup() {
  # 用法：
  # repo_offer_delete_backup "项目名" "$BAK_DIR" "container_name"

  local name="$1"
  local bak="$2"
  local container="$3"

  [ -z "$bak" ] && return 0
  [ ! -d "$bak" ] && return 0

  # 检查容器是否还在挂载 bak
  if [ -n "$container" ]; then
    local m
    m="$(docker inspect -f '{{range .Mounts}}{{println .Source}}{{end}}' "$container" 2>/dev/null | grep -F "$bak" || true)"
    if [ -n "$m" ]; then
      echo "⚠️ [$name] 检测到容器仍挂载备份目录：$bak"
      echo "   为安全起见不允许删除。请确认已在正式目录 --force-recreate 重建后再删。"
      return 0
    fi
  fi

  read -r -p "是否删除旧的 [$name] 目录备份？($bak) [y/N]: " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    rm -rf "$bak"
    echo "🗑️ 已删除：$bak"
  else
    echo "ℹ️ 已保留：$bak"
  fi
}

# ========== 功能函数 ==========

function os_info() { cat /etc/os-release; }

function nic_info() { ip addr; }

function disk_info() { lsblk -o NAME,SIZE,FSTYPE,UUID,MOUNTPOINT; }

function format_disk() {
  echo "📝 当前磁盘列表："
  lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT

  read -p "请输入需要格式化的磁盘名称（例如 sda，不含 /dev/）: " disk_name
  disk_path="/dev/$disk_name"

  # 检查磁盘是否存在
  if [ ! -b "$disk_path" ]; then
    echo "❌ 磁盘 $disk_path 不存在，退出"
    return 1
  fi

  echo "🔍 选择的磁盘信息："
  lsblk $disk_path

  read -p "⚠️ 警告：磁盘 $disk_path 数据将被清除，确认格式化？(y/n): " confirm
  if [ "$confirm" != "y" ]; then
    echo "❌ 操作取消"
    return 1
  fi

  # 检查磁盘上是否有分区
  partitions=$(lsblk -n -o NAME $disk_path | grep -v "^$disk_name$")
  if [ -n "$partitions" ]; then
    echo "🔧 删除磁盘上已有分区..."
    for part in $partitions; do
      sudo wipefs -a /dev/$part
      sudo parted /dev/$disk_name rm $(echo $part | grep -o "[0-9]*$")
    done
  fi

  echo "💽 创建新分区并格式化 ext4"
  sudo parted -s $disk_path mklabel gpt
  sudo parted -s $disk_path mkpart primary ext4 0% 100%
  sudo mkfs.ext4 -F ${disk_path}1

  # 检查是否已挂载
  mountpoint=$(lsblk -no MOUNTPOINT ${disk_path}1)
  if [ -n "$mountpoint" ]; then
    echo "✅ 分区已挂载到：$mountpoint"
  else
    read -p "📁 请输入挂载目录（例如 /data）： " mount_dir
    if [ ! -d "$mount_dir" ]; then
      sudo mkdir -p $mount_dir
    fi
    echo "🔗 挂载分区到 $mount_dir"
    sudo mount ${disk_path}1 $mount_dir

    # 自动写入 /etc/fstab
    uuid=$(sudo blkid -s UUID -o value ${disk_path}1)
    echo "UUID=$uuid $mount_dir ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab

    echo "✅ 格式化并挂载完成：$disk_path -> $mount_dir"
    echo "🔒 永久挂载已添加到 /etc/fstab，重启后自动挂载"
  fi
}

function docker_info() { docker info; }

function install_docker() {
    . /etc/os-release

    sudo apt-get update
    sudo apt-get install -y ca-certificates curl gnupg lsb-release

    sudo install -m 0755 -d /etc/apt/keyrings

    if [[ "$ID" == "debian" ]]; then
        sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
        sudo chmod a+r /etc/apt/keyrings/docker.asc
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    elif [[ "$ID" == "ubuntu" ]]; then
        sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        sudo chmod a+r /etc/apt/keyrings/docker.asc
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    else
        echo "当前系统 $ID 不在支持范围内，请手动安装 Docker。"
        return 1
    fi

    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    sudo systemctl enable docker
    sudo systemctl start docker

    echo "✅ Docker 安装完成，版本信息："
    docker --version
}

# ========== 1. 创建 macvlan 网络 ==========
create_macvlan_network() {
  echo "🔧 开始创建 macvlan 网络"

  # 1) 列出所有可能作为 parent 的接口（不过滤 ovs/bridge；只排除明显不可用的）
  local interfaces=()
  while IFS= read -r iface; do
    case "$iface" in
      # 明确排除：容器/隧道/虚拟/内核专用
      lo|docker0|docker*|br-*|virbr*|veth*|mvbr*|tun*|tap*|wg*|tailscale*|zt*|ifb*|dummy*|gre*|gretap*|ip6gre*|sit*|macvtap*|kube*|cni*|flannel*|calico*|ovs-system* )
        continue
        ;;
      *)
        # 放宽：允许 eth/ens/enp/eno/wlan/bond/team/br/ovs 等以及 VLAN 子接口 (xxx.88)
        # if [[ "$iface" =~ ^(e(n|th|np|ns|no|ni)|ens|enp|eno|eth|wlan|wl|bond|team|br|ovs)([0-9a-zA-Z\.\-:_]+)?$ ]]; then
          interfaces+=("$iface")
        # fi
        ;;
    esac
  done < <(ls /sys/class/net)

  if [ ${#interfaces[@]} -eq 0 ]; then
    echo "❌ 未找到可用的网卡/接口。"
    return 1
  fi

  # 收集 macvlan 网络与 parent 接口的映射
  declare -A MACVLAN_BY_PARENT

  while IFS= read -r net; do
    parent="$(docker network inspect "$net" -f '{{.Options.parent}}' 2>/dev/null)"
    [ -n "$parent" ] && MACVLAN_BY_PARENT["$parent"]+="$net "
  done < <(docker network ls --filter driver=macvlan --format '{{.Name}}')

  echo "请选择 parent 接口（可选物理口 / VLAN 子接口 / OVS bridge 口）："
  local i ip4 ip6 macvlans
  for i in "${!interfaces[@]}"; do
    iface="${interfaces[$i]}"

    ip4="$(ip -4 addr show "$iface" 2>/dev/null | awk '/ inet /{print $2}' | head -n1)"
    ip6="$(ip -6 addr show "$iface" 2>/dev/null | awk '/ inet6 / && $2 ~ /^fd/{print $2}' | head -n1)"

    macvlans="${MACVLAN_BY_PARENT[$iface]}"
    if [ -n "$macvlans" ]; then
      echo "$i) $iface  IPv4: ${ip4:-无}  ULA: ${ip6:-无}"
      echo "    ↳ 已存在 macvlan: $macvlans"
    else
      echo "$i) $iface  IPv4: ${ip4:-无}  ULA: ${ip6:-无}"
    fi
  done

  local netcard_index networkcard
  read -r -p "输入网卡序号(直接回车退出): " netcard_index

  # 直接回车：退出（不报错）
  if [ -z "$netcard_index" ]; then
    echo "退出 macvlan 创建。"
    return 0
  fi

  networkcard="${interfaces[$netcard_index]}"
  [ -n "$networkcard" ] || { echo "❌ 未能获取网卡名称"; return 1; }
  echo "选择的 parent 接口: $networkcard"

  # ========= VLAN 处理 =========
  local vlan_id="" vlan_iface="" vlan_suffix="" parent_iface=""
  if [[ "$networkcard" != *.* ]]; then
    read -r -p "是否为 macvlan 使用 VLAN ID？直接回车表示不使用，输入 VLAN ID（例如 88）: " vlan_id
    if [ -n "$vlan_id" ]; then
      if ! [[ "$vlan_id" =~ ^[0-9]+$ ]] || [ "$vlan_id" -lt 1 ] || [ "$vlan_id" -gt 4094 ]; then
        echo "❌ VLAN ID 无效：$vlan_id"
        return 1
      fi

      vlan_iface="${networkcard}.${vlan_id}"
      echo "🔧 将使用 VLAN 子接口: $vlan_iface (parent: $networkcard, VLAN ID: $vlan_id)"

      if ! ip link show "$vlan_iface" >/dev/null 2>&1; then
        sudo ip link add link "$networkcard" name "$vlan_iface" type vlan id "$vlan_id" || {
          echo "❌ 创建 VLAN 接口失败：$vlan_iface"
          return 1
        }
      fi
      sudo ip link set "$vlan_iface" up || true
      networkcard="$vlan_iface"
    fi
  else
    vlan_suffix="${networkcard#*.}"
    if [[ "$vlan_suffix" =~ ^[0-9]+$ ]]; then
      vlan_id="$vlan_suffix"
    fi
    echo "ℹ️ 检测到带 VLAN 的接口: $networkcard (推测 VLAN ID: ${vlan_id:-未知})"
  fi
  # ========= VLAN 处理结束 =========

  # ========= IPv4：先网关，再算 CIDR & range =========
  local ip="" gateway="" cidr="" iprange="" subnet4="" iprangev4="" suggest_gateway="" suggest_prefixlen="" prefixlen="" auto_cidr=""
  ip="$(ip -4 addr show "$networkcard" 2>/dev/null | awk '/ inet /{print $2}' | head -n1 | cut -d'/' -f1)"

  if [ -n "$ip" ]; then
    # 接口本身有 IP：建议用该接口的前缀长度；网关优先取该接口路由到默认的下一跳
    local cidr_from_iface gw_from_iface
    cidr_from_iface="$(get_subnet_v4 "$ip" "$networkcard")"
    gw_from_iface="$(ip -4 route show default 2>/dev/null | awk -v dev="$networkcard" '$0 ~ (" dev "dev" ") {print $3; exit}')"
    [ -z "$gw_from_iface" ] && gw_from_iface="$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')"
    suggest_gateway="$gw_from_iface"
    suggest_prefixlen="${cidr_from_iface#*/}"
  else
    echo "⚠️ 未在接口 $networkcard 上检测到 IPv4 地址（VLAN/bridge 接口通常没有 IP）"

    parent_iface="${networkcard%%.*}"
    local parent_ip parent_cidr parent_mask p1 p2 p3 p4 third_octet
    parent_ip="$(ip -4 addr show "$parent_iface" 2>/dev/null | awk '/ inet /{print $2}' | head -n1 | cut -d'/' -f1)"

    if [ -n "$parent_ip" ]; then
      parent_cidr="$(get_subnet_v4 "$parent_ip" "$parent_iface")"
      parent_mask="${parent_cidr#*/}"
      IFS='.' read -r p1 p2 p3 p4 <<< "${parent_cidr%/*}"

      if [ -n "$vlan_id" ]; then
        third_octet="$vlan_id"
        suggest_prefixlen="24"
      else
        third_octet="$p3"
        suggest_prefixlen="$parent_mask"
      fi

      suggest_gateway="${p1}.${p2}.${third_octet}.1"
      echo "👉 已根据 trunk 接口 $parent_iface 推算推荐 IPv4 网关：$suggest_gateway"
      echo "👉 推荐前缀长度：/$suggest_prefixlen"
    else
      echo "❌ trunk 接口 $parent_iface 也没有 IPv4，无法推算，需要手动输入网关和网段。"
    fi
  fi

  if [ -n "$suggest_gateway" ]; then
    read -r -p "请输入 IPv4 网关 (回车使用推荐 $suggest_gateway): " gateway
    [ -z "$gateway" ] && gateway="$suggest_gateway"
  else
    read -r -p "请输入 IPv4 网关 (例如 10.88.0.1): " gateway
  fi

  [ -n "$gateway" ] || { echo "❌ IPv4 网关不能为空。"; return 1; }

  prefixlen="${suggest_prefixlen:-24}"
  auto_cidr="${gateway%.*}.0/${prefixlen}"

  echo "👉 已根据 IPv4 网关 $gateway 推算推荐子网：$auto_cidr"
  read -r -p "请输入 macvlan IPv4 子网CIDR (回车使用推荐 $auto_cidr): " cidr
  [ -z "$cidr" ] && cidr="$auto_cidr"

  echo "⚠️ 提示：IPRange 应为 macvlan 专用网段（建议 /24 或更小），不要与 DHCP/静态地址重叠。"
  read -r -p "请输入 macvlan IPv4 range (回车使用 $cidr): " iprange
  [ -z "$iprange" ] && iprange="$cidr"

  iprangev4="$(echo "$iprange" | cut -d'/' -f1)"
  subnet4="$(echo "$iprange" | cut -d'/' -f2)"

  # ========= IPv6：更稳的收敛逻辑 =========
  local gateway6="" cidr6="" iprange6="" subnet6="" iprangev6_prefix="" suggest_gateway6="" suggest_cidr6="" auto_cidr6=""
  # 优先：从接口/父接口拿到 ULA 前缀（fdxx）
  local ip6_cidr ip6_addr prefix_len6 ula_prefix

  ip6_cidr="$(ip -6 addr show "$networkcard" 2>/dev/null | awk '/ inet6 / && $2 ~ /^fd/{print $2; exit}')"
  if [ -z "$ip6_cidr" ]; then
    parent_iface="${networkcard%%.*}"
    ip6_cidr="$(ip -6 addr show "$parent_iface" 2>/dev/null | awk '/ inet6 / && $2 ~ /^fd/{print $2; exit}')"
  fi

  if [ -n "$ip6_cidr" ]; then
    ip6_addr="${ip6_cidr%/*}"
    prefix_len6="${ip6_cidr#*/}"
    # 取前 4 段作为稳定 ULA /64 前缀（fd10:0:1:xx）
    ula_prefix="$(echo "$ip6_addr" | awk -F: '{print $1":"$2":"$3":"$4}')"
    suggest_cidr6="${ula_prefix}::/64"
    suggest_gateway6="${ula_prefix}::1"
  else
    # 没有现成 ULA：退回你原来的“IPv4->ULA 前缀”方案（但只作为建议）
    if [ -n "$gateway" ]; then
      local prefix6
      prefix6="$(ipv4_to_ipv6_prefix "$gateway")"
      suggest_cidr6="${prefix6}::/64"
      suggest_gateway6="${prefix6}::1"
    fi
  fi

  if [ -n "$suggest_gateway6" ]; then
    echo "检测到 IPv6 网关: $suggest_gateway6"
    read -r -p "请输入 fd 开头的 IPv6 网关 (回车使用推荐 $suggest_gateway6，n 表示不开启): " gateway6
    case "$gateway6" in
      "") gateway6="$suggest_gateway6" ;;
      "n"|"N") gateway6="" ;;
    esac
  else
    read -r -p "请输入 fd 开头的 IPv6 网关 (n 表示不开启): " gateway6
    case "$gateway6" in
      "n"|"N") gateway6="" ;;
    esac
  fi

  if [ -z "$gateway6" ]; then
    cidr6=""; iprange6=""; subnet6=""; iprangev6_prefix=""
  else
    auto_cidr6="${suggest_cidr6:-$(ipv4_to_ipv6_prefix "$gateway")::/64}"
    echo "👉 已根据 IPv6 网关 $gateway6 推算推荐子网：$auto_cidr6"
    read -r -p "请输入 IPv6 子网CIDR (回车使用推荐 $auto_cidr6): " cidr6
    [ -z "$cidr6" ] && cidr6="$auto_cidr6"

    echo "⚠️ 提示：IPv6 IPRange 建议 /64（不要与现网 RA/DHCPv6 冲突）。"
    read -r -p "请输入 macvlan IPv6 range (回车使用 $cidr6): " iprange6
    [ -z "$iprange6" ] && iprange6="$cidr6"

    subnet6="$(echo "$iprange6" | cut -d'/' -f2)"
    iprangev6_prefix="$(echo "$iprange6" | cut -d'/' -f1)"
  fi

  # ========== 根据物理网卡 + VLAN ID 生成 macvlan 网络名称 ==========
  local raw_phys safe_phys network_name
  raw_phys="${networkcard%%.*}"
  safe_phys="$(echo "$raw_phys" | sed 's/[^a-zA-Z0-9_-]/_/g')"

  if [ -n "$vlan_id" ]; then
    network_name="macvlan_${safe_phys}_${vlan_id}"
  else
    network_name="macvlan_${safe_phys}"
  fi

  # ========= 最终确认 =========
  echo "macvlan 参数确认："
  [ -n "$vlan_id" ] && echo "VLAN ID     : $vlan_id"
  echo "Parent 接口 : $networkcard"
  echo "IPv4 gateway: $gateway"
  echo "IPv4 subnet : $cidr"
  echo "IPv4 range  : $iprange"
  if [ -n "$gateway6" ]; then
    echo "IPv6 gateway: $gateway6"
    echo "IPv6 subnet : $cidr6"
    echo "IPv6 range  : $iprange6"
  else
    echo "IPv6        : 不启用"
  fi
  echo "网络名称：$network_name"

  local confirm
  read -r -p "是否正确？(y/n): " confirm
  if [ "$confirm" != "y" ]; then
    echo "退出 macvlan 创建。"
    return 1
  fi

  # 启用 promiscuous mode
  sudo ip link set "$networkcard" promisc on || true

  # 创建 docker macvlan 网络
  echo "🔨 正在创建 docker macvlan 网络：$network_name ..."
  if [ -n "$gateway6" ] && [ -n "$cidr6" ]; then
    docker network create -d macvlan \
      --subnet="$cidr"  --ip-range="$iprange"  --gateway="$gateway" \
      --ipv6 --subnet="$cidr6" --ip-range="$iprange6" --gateway="$gateway6" \
      -o parent="$networkcard" "$network_name"
  else
    docker network create -d macvlan \
      --subnet="$cidr" --ip-range="$iprange" --gateway="$gateway" \
      -o parent="$networkcard" "$network_name"
  fi

  echo "✅ macvlan 网络创建完成：$network_name"
}

# ========== 2. 配置 macvlan bridge 与 systemd ==========
create_macvlan_bridge() {
    echo "🔧 开始创建/更新 macvlan bridge（宿主机 <-> macvlan 网络互通）"

    # 1. 列出所有 macvlan 开头的 docker 网络
    mapfile -t macvlan_networks < <(docker network ls --format '{{.Name}}' | grep '^macvlan' || true)
    if [ ${#macvlan_networks[@]} -eq 0 ]; then
        echo "❌ 未发现任何以 macvlan 开头的 Docker 网络，请先创建 macvlan 网络。"
        return 1
    fi

    echo "可用的 macvlan 网络："
    for i in "${!macvlan_networks[@]}"; do
        echo "  $i) ${macvlan_networks[$i]}"
    done

    read -p "请输入要配置 bridge 的 macvlan 序号(默认 0): " idx
    idx=${idx:-0}
    if ! [[ "$idx" =~ ^[0-9]+$ ]] || [ "$idx" -lt 0 ] || [ "$idx" -ge "${#macvlan_networks[@]}" ]; then
        echo "❌ 输入序号无效。"
        return 1
    fi

    macvlan_name="${macvlan_networks[$idx]}"
    echo "📡 选中的 macvlan 网络: $macvlan_name"

    # 2. 获取网络配置
    network_info=$(docker network inspect "$macvlan_name" 2>/dev/null)
    if [ -z "$network_info" ]; then
        echo "❌ 无法 inspect Docker 网络：$macvlan_name"
        return 1
    fi

    # parent 接口（例如 eth0.8）
    parent_if=$(echo "$network_info" | jq -r '.[0].Options.parent // empty')
    if [ -z "$parent_if" ] || [ "$parent_if" = "null" ]; then
        echo "❌ 在 $macvlan_name 中未找到 parent 接口(Options.parent)，请检查该网络是否为 macvlan 类型。"
        return 1
    fi
    echo "🔗 发现 parent 接口: $parent_if"

    # === IPv4 部分：Subnet + IPRange 组合使用 ===
    subnet4_cidr=$(echo "$network_info" | jq -r '.[0].IPAM.Config[] | select(.Subnet | test(":") | not) | .Subnet // empty' | head -n1)
    if [ -z "$subnet4_cidr" ] || [ "$subnet4_cidr" = "null" ]; then
        echo "❌ 无法从 $macvlan_name 中解析 IPv4 Subnet，请确认该网络配置了 IPv4。"
        return 1
    fi
    echo "🌐 IPv4 子网(Subnet): $subnet4_cidr"

    iprange4_cidr=$(echo "$network_info" | jq -r --arg s "$subnet4_cidr" '
      .[0].IPAM.Config[]
      | select((.Subnet // "") == $s)
      | (.IPRange // empty)
    ' | head -n1)
    if [ -n "$iprange4_cidr" ] && [ "$iprange4_cidr" != "null" ]; then
        echo "🌐 IPv4 IPRange: $iprange4_cidr"
        base4="${iprange4_cidr%/*}"   # 例如 10.0.2.0
    else
        base4="${subnet4_cidr%/*}"    # 例如 10.0.2.0
    fi
    # ⭐ 路由/掩码：优先 IPRange，缺省退回 Subnet
    route4_cidr="${iprange4_cidr:-$subnet4_cidr}"
    prefix4="${route4_cidr#*/}"

    # 用 base 前 3 段 + .254 作为 bridge IP
    bridge4="${base4%.*}.254"
    bridge4_cidr="${bridge4}/${prefix4}"
    echo "📍 计划 bridge IPv4: $bridge4_cidr"

    # === 新增：基于 bridge IPv4 生成稳定 MAC（使用已有函数） ===
    bridge_mac="$(ip_to_mac "$bridge4")"
    if [ -z "$bridge_mac" ]; then
      echo "❌ ip_to_mac 计算失败：bridge4=$bridge4"
      return 1
    fi
    echo "🧷 计划固定 bridge MAC: $bridge_mac"

    # === IPv6 部分：IPRange 优先，没有则用 Subnet；统一收敛到 /64，bridge 用 ::eeee ===
    subnet6_cidr=$(echo "$network_info" | jq -r '.[0].IPAM.Config[] | select(.Subnet | test(":")) | .Subnet // empty' | head -n1)
    bridge6_cidr=""
    route6_pref=""

    if [ -n "$subnet6_cidr" ] && [ "$subnet6_cidr" != "null" ]; then
        echo "🌐 IPv6 子网(Subnet): $subnet6_cidr"

        iprange6_cidr=$(echo "$network_info" | jq -r --arg s "$subnet6_cidr" '
          .[0].IPAM.Config[]
          | select((.Subnet // "") == $s)
          | (.IPRange // empty)
        ' | head -n1)
        if [ -n "$iprange6_cidr" ] && [ "$iprange6_cidr" != "null" ]; then
            echo "🌐 IPv6 IPRange: $iprange6_cidr"
            base6="${iprange6_cidr%/*}"    # 比如 fd10:0:20:: 或 fd10:0:20::100
        else
            base6="${subnet6_cidr%/*}"     # 比如 fd10:0:20::
        fi

        # 归一：提纯前缀主体，统一 /64，bridge 固定 ::eeee
        base6_addr="${subnet6_cidr%/*}"   # fd10:0:20::  或 fd10:0:20:1::
        base6_prefix="${base6_addr%%::*}" # fd10:0:20    或 fd10:0:20:1

        bridge6_cidr="${base6_prefix}::eeee/64"
        route6_pref="${base6_prefix}::/64"
        echo "  计划 bridge IPv6: $bridge6_cidr"
    fi

    # 3. 生成接口名 / 脚本名 / service 名（mvb 前缀，尽量保留下划线）

    # VLAN判断：来自 macvlan 名或 parent_if
    vlan_id=""
    if [[ "$macvlan_name" =~ ^macvlan_([0-9]+)$ || "$macvlan_name" =~ ^macvlan-([0-9]+)$ ]]; then
        vlan_id="${BASH_REMATCH[1]}"
    elif [[ "$parent_if" =~ \.([0-9]+)$ ]]; then
        vlan_id="${BASH_REMATCH[1]}"
    fi

    # 物理网卡名（不缩写）
    raw_phys="${parent_if%%.*}"

    # 网络名（无长度限制）
    if [ -n "$vlan_id" ]; then
        safe_name="macvlan_${raw_phys}_${vlan_id}"
    else
        safe_name="macvlan_${raw_phys}"
    fi

    # 目标形式（优先保持）
    if [ -n "$vlan_id" ]; then
        bridge_try="mvb_${raw_phys}_${vlan_id}"
    else
        bridge_try="mvb_${raw_phys}"
    fi

    max_len=15

    # 如果长度 ≤ 15，直接使用
    if [ ${#bridge_try} -le $max_len ]; then
        bridge_if="$bridge_try"
    else
        # 1) 裁剪 phys（保留下划线）
        if [ -n "$vlan_id" ]; then
            prefix="mvb_"
            mid="${raw_phys}"
            suffix="_${vlan_id}"
        else
            prefix="mvb_"
            mid="${raw_phys}"
            suffix=""
        fi

        # 可用空间（保留 prefix 和 suffix）
        keep_len=$(( max_len - ${#prefix} - ${#suffix} ))
        [ $keep_len -lt 0 ] && keep_len=0

        # 裁剪物理网卡名（尾部裁剪）
        mid_cut="${mid: -$keep_len}"

        bridge_if="${prefix}${mid_cut}${suffix}"

        # 2) 若仍超长，移除下划线再重试
        if [ ${#bridge_if} -gt $max_len ]; then
            prefix="mvb"
            if [ -n "$vlan_id" ]; then
                core="${raw_phys}${vlan_id}"    # no underscores
            else
                core="${raw_phys}"
            fi
            keep_len=$(( max_len - ${#prefix} ))
            core_cut="${core: -$keep_len}"
            bridge_if="${prefix}${core_cut}"
        fi

        # 3) 最终保险 — 保留前缀 mvb，裁掉右边
        if [ ${#bridge_if} -gt $max_len ]; then
            bridge_if="mvb${bridge_if: -$((max_len-3))}"
        fi
    fi

    setup_script="/usr/local/bin/${safe_name}.sh"
    service_name="${safe_name}.service"

    echo "🧩 bridge 接口: $bridge_if"
    echo "🧩 配置脚本: $setup_script"
    echo "🧩 systemd 服务: $service_name"

    # —— 在写脚本之前：检测是否安装了 mihomo；若有则询问，否则询问是否指向其他 IP ——
    mihomo_ip=""
    FAKE_IP_GW=""

    # 1. 尝试探测 Mihomo IP
    if docker ps -a --format '{{.Names}}' | grep -qi 'mihomo'; then
      mihomo_ip="$(detect_mihomo_ip "$route4_cidr" "$network_info")"
      if [ -n "$mihomo_ip" ]; then
        echo "🔎 检测到 mihomo 相关容器，探测到 IP: $mihomo_ip"
        read -r -p "是否将 198.18.0.0/15 路由指向 mihomo ($mihomo_ip)？(y/n，默认 n): " yn_mihomo
        if [[ "$yn_mihomo" =~ ^[Yy]$ ]]; then
          FAKE_IP_GW="$mihomo_ip"
        fi
      fi
    fi

    if [ -n "$FAKE_IP_GW" ]; then
      echo "✅ 将写入路由规则: 198.18.0.0/15 via $FAKE_IP_GW"
    else
      echo "ℹ️ 不写入 198.18.0.0/15 的静态路由。"
    fi

    read -p "确认创建/更新以上 bridge？(y/n): " yn
    if [[ ! "$yn" =~ ^[Yy]$ ]]; then
        echo "⚠️ 已取消。"
        return 0
    fi

    # 4. 写入桥接脚本
    sudo mkdir -p /usr/local/bin

    cat <<EOF | sudo tee "$setup_script" >/dev/null
#!/bin/bash
set -e

SUBNET4_CIDR="$subnet4_cidr"
IPRANGE4_CIDR="$iprange4_cidr"
ROUTE6_PREF="$route6_pref"
BRIDGE6_CIDR="$bridge6_cidr"
FAKE_IP_GW="$FAKE_IP_GW"
MIHOMO_IP="$mihomo_ip"

# 1. 物理层清理与创建
ip link del "$bridge_if" 2>/dev/null || true
ip link add "$bridge_if" link "$parent_if" address "$bridge_mac" type macvlan mode bridge

# 🔒 MAC 校验（关键）
ip link show "$bridge_if" | grep -qi "$bridge_mac" || { echo "❌ MAC not set to $bridge_mac"; exit 1; }

# 2. IPv4 地址分配
ip addr replace "$bridge4_cidr" dev "$bridge_if"

# 3. IPv6 地址（有才配置）
if [ -n "\$BRIDGE6_CIDR" ]; then
  sysctl -w "net.ipv6.conf.${bridge_if}.accept_dad=0" >/dev/null || true
  ip -6 addr replace "\$BRIDGE6_CIDR" dev "$bridge_if"
fi
EOF

      cat <<EOF | sudo tee -a "$setup_script" >/dev/null

# 4. 接口启动与混杂模式
ip link set "$bridge_if" up
ip link set "$bridge_if" promisc on
ip link set "$parent_if" up 2>/dev/null || true
ip link set "$parent_if" promisc on

# 5. IPv4 路由：有 IPRange 才拦 IPRange + metric；否则拦 Subnet 不抢 metric
if [ -n "\$IPRANGE4_CIDR" ]; then
  ip route replace "\$IPRANGE4_CIDR" dev "$bridge_if" metric 10
else
  ip route replace "\$SUBNET4_CIDR" dev "$bridge_if"
fi

# 5.1 198.18.0.0/15（Fake-IP / 代理入口）说明 & 防踩坑
#
# ⚠️ 如果 198.18.x.x 由 Mac mini + surge 承载，Mac mini网卡上dns设为自动，不能指定为路由器网关
#
if [ -n "\$FAKE_IP_GW" ]; then
  ip route replace 198.18.0.0/15 via "\$FAKE_IP_GW" dev "$bridge_if" onlink 2>/dev/null || true
fi

# 6. IPv6 路由：不建议用 metric
if [ -n "\$ROUTE6_PREF" ]; then
  ip -6 route replace "\$ROUTE6_PREF" dev "$bridge_if"
fi

# 7. 内核参数调优
sysctl -w "net.ipv4.conf.${bridge_if}.rp_filter=0" >/dev/null || true
sysctl -w "net.ipv4.conf.${parent_if}.rp_filter=0" >/dev/null || true
sysctl -w "net.ipv4.conf.all.rp_filter=0" >/dev/null || true
sysctl -w "net.ipv4.conf.default.rp_filter=0" >/dev/null || true
EOF

    sudo chmod +x "$setup_script"

    # 5. 写入 systemd 服务
    sudo bash -c "cat > /etc/systemd/system/$service_name" <<EOF
[Unit]
Description=macvlan bridge for $macvlan_name ($bridge_if)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$setup_script
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

    # 6. 启用并立即执行
    sudo systemctl daemon-reload 2>/dev/null || true
    sudo systemctl enable "$service_name" 2>/dev/null || true

    # 群晖 systemctl 可能无法 start，兜底直接执行一次脚本
    if ! sudo systemctl start "$service_name" 2>/dev/null; then
        echo "⚠️ systemctl start 不可用，直接执行 bridge 脚本"
        sudo "$setup_script" || return 1
    fi

    echo "✅ 已为 macvlan 网络 $macvlan_name 创建/更新 bridge 接口: $bridge_if"
    echo "   IPv4: $bridge4_cidr"
    if [ -n "$bridge6_cidr" ]; then
        echo "   IPv6: $bridge6_cidr"
    fi
}

install_librespeed() {

    echo "🔧 安装 LibreSpeed（需要选择 macvlan 网络）"

    # 1) 选择 macvlan（回车退出）
    select_macvlan_or_exit
    case $? in
      0) ;;
      2) return 0 ;;
      *) return 1 ;;
    esac

    # 2) 选择 IPv4 最后一段（回车默认 111）
    local last_octet
    last_octet="$(prompt_ipv4_last_octet \
      "请输入 LibreSpeed IPv4 最后一段（1-254，回车默认 111）: " 111)" || return 1

    # 3) 计算 IP / IPv6 / MAC（基于 SELECTED_MACVLAN）
    calculate_ip_mac "$last_octet"
    librespeed="$calculated_ip"
    librespeed6="$calculated_ip6"
    librespeedmac="$calculated_mac"

    # 4) 选择存储目录（回车退出）
    local dockerapps
    select_dockerapps_dir "LibreSpeed"
    case $? in
      0) dockerapps="$SELECTED_DOCKERAPPS_DIR" ;;
      2) echo "✅ 已退出 LibreSpeed 安装。"; return 0 ;;
      *) return 1 ;;
    esac

    mkdir -p "$dockerapps" || return 1
    cd "$dockerapps" || return 1

    # 5) 自定义容器/目录名称（回车用默认librespeed）
    local DEFAULT_CONTAINER_NAME="librespeed"
    local CONTAINER_NAME
    read -r -p "请输入容器名称（回车默认使用 '$DEFAULT_CONTAINER_NAME'）: " CONTAINER_NAME
    CONTAINER_NAME=${CONTAINER_NAME:-$DEFAULT_CONTAINER_NAME}

    # 6) 仓库分阶段更新：目录名使用用户输入的容器名
    local REPO_URL="https://github.com/perryyeh/librespeed.git"
    repo_stage_update "librespeed" "$dockerapps" "$REPO_URL" "$CONTAINER_NAME" || return 1
    cd "$WORK_DIR" || { echo "❌ 进入目录失败：$WORK_DIR"; return 1; }

    # 7) 替换compose配置中的容器相关字段
    if [ "$CONTAINER_NAME" != "$DEFAULT_CONTAINER_NAME" ]; then
        # 1. 替换services下一级的服务名称
        sed -i "s/^  $DEFAULT_CONTAINER_NAME:/  $CONTAINER_NAME:/" docker-compose.yml
        # 2. 替换container_name
        sed -i "s/container_name: $DEFAULT_CONTAINER_NAME/container_name: $CONTAINER_NAME/" docker-compose.yml
        # 3. 替换hostname
        sed -i "s/hostname: $DEFAULT_CONTAINER_NAME/hostname: $CONTAINER_NAME/" docker-compose.yml
        echo "✅ 已自定义容器名称/目录为：$CONTAINER_NAME"
    fi

    # 8) 写 .env（compose 读取）
    cat > .env <<EOF
MACVLAN_NET=${SELECTED_MACVLAN}
ipv4=${librespeed}
ipv6=${librespeed6}
macaddress=${librespeedmac}
EOF

    echo "✅ 已生成 .env："
    cat .env
    echo

    # 8) 无IPv6场景：自动删除compose中的ipv6配置避免报错
    if [ -z "$librespeed6" ]; then
        remove_compose_ipv6_fields docker-compose.yml
    fi

    # 9) 一步部署：校验 -> 停旧备份 -> 起新 -> next->正式 -> 正式再up -> 失败回滚
    compose_deploy_with_repo_switch "librespeed" "$CONTAINER_NAME" "docker-compose.yml" || return 1

    echo "✅ LibreSpeed 已启动"
    echo "访问地址：http://${librespeed}"
    echo "容器名称：${CONTAINER_NAME}"
    if [ -n "$librespeed6" ]; then
        echo "IPv6 地址：${librespeed6}"
    else
        echo "IPv6：未启用（所选 macvlan 未开启 IPv6 或无 IPv6 子网）"
    fi

    # 10) 可选删除备份（带挂载检查）
    repo_offer_delete_backup "librespeed" "$BAK_DIR" "librespeed"
}

install_adguardhome() {

    echo "🔧 安装 AdGuardHome（需要选择 macvlan 网络）"

    # 0) 选择 macvlan（回车退出）
    select_macvlan_or_exit
    case $? in
      0) ;;
      2) return 0 ;;
      *) return 1 ;;
    esac

    # 1) 输入 mosdns IPv4 最后一段（默认 119）-> 计算 mosdns/mosdns6
    local mosdns_last mosdns mosdns6
    mosdns_last="$(prompt_ipv4_last_octet \
      "请输入 mosdns IPv4 最后一段（1-254，回车默认 119）: " \
      119
    )" || return 1
    calculate_ip_mac "$mosdns_last"
    mosdns="$calculated_ip"
    mosdns6="$calculated_ip6"

    # 2) 输入 AdGuardHome IPv4 最后一段（默认 114）-> 计算 adguard/adguard6/adguardmac/gateway
    local adg_last adguard adguard6 adguardmac gateway
    adg_last="$(prompt_ipv4_last_octet \
      "请输入 adguard IPv4 最后一段（1-254，回车默认 114）: " \
      114
    )" || return 1
    calculate_ip_mac "$adg_last"
    adguard="$calculated_ip"
    adguard6="$calculated_ip6"
    adguardmac="$calculated_mac"
    gateway="$calculated_gateway"

    # 3) 选择存储目录（回车退出）
    local dockerapps
    select_dockerapps_dir "AdGuardHome"
    case $? in
      0) dockerapps="$SELECTED_DOCKERAPPS_DIR" ;;
      2) echo "✅ 已退出 AdGuardHome 安装。"; return 0 ;;
      *) return 1 ;;
    esac

    mkdir -p "${dockerapps}/adguardwork" "${dockerapps}" || return 1

    # 4) 自定义容器/目录名称（回车用默认adguardhome）
    local DEFAULT_CONTAINER_NAME="adguardhome"
    local CONTAINER_NAME
    read -r -p "请输入容器名称（回车默认使用 '$DEFAULT_CONTAINER_NAME'）: " CONTAINER_NAME
    CONTAINER_NAME=${CONTAINER_NAME:-$DEFAULT_CONTAINER_NAME}

    # 5) 更新/获取仓库（stage：设置 WORK_DIR / NEED_SWITCH / TARGET_DIR / BAK_DIR）
    local REPO_URL="https://github.com/perryyeh/adguardhome.git"
    repo_stage_update "adguardhome" "$dockerapps" "$REPO_URL" "$CONTAINER_NAME" || return 1
    cd "$WORK_DIR" || { echo "❌ 进入目录失败：$WORK_DIR"; return 1; }

    # 6) 替换compose配置中的容器相关字段
    if [ "$CONTAINER_NAME" != "$DEFAULT_CONTAINER_NAME" ]; then
        # 1. 替换services下一级的服务名称
        sed -i "s/^  $DEFAULT_CONTAINER_NAME:/  $CONTAINER_NAME:/" docker-compose.yml
        # 2. 替换container_name
        sed -i "s/container_name: $DEFAULT_CONTAINER_NAME/container_name: $CONTAINER_NAME/" docker-compose.yml
        # 3. 替换hostname
        sed -i "s/hostname: $DEFAULT_CONTAINER_NAME/hostname: $CONTAINER_NAME/" docker-compose.yml
        echo "✅ 已自定义容器名称/目录为：$CONTAINER_NAME"
    fi

    # 7) 是否启用 IPv6：按你原判定（macvlan 支持 + 有 IPv6 子网）
    local USE_IPV6=0
    if macvlan_ipv6_enabled "$SELECTED_MACVLAN"; then
      USE_IPV6=1
    fi

    # 8) 写 .env（字段不变；但 confdir 用 WORK_DIR，避免 next 启动还挂旧目录）
    write_env_file "$WORK_DIR/.env" \
      "MACVLAN_NET=${SELECTED_MACVLAN}" \
      "ipv4=${adguard}" \
      "ipv6=${adguard6}" \
      "macaddress=${adguardmac}"

    echo "✅ 已生成 .env："
    cat .env
    echo

    # 7) 替换逻辑（必须保留：mosdns / mosdns6 / gateway）
    if [ -f "${WORK_DIR}/AdGuardHome.yaml" ]; then
        sed -i "s/10.0.1.119/${mosdns}/g" "${WORK_DIR}/AdGuardHome.yaml"
        if [ -n "$mosdns6" ]; then
            sed -i "s/#\[fd10::1:119\]/[${mosdns6}]/g" "${WORK_DIR}/AdGuardHome.yaml"
        fi
        if [ -n "$gateway" ] && [ "$gateway" != "null" ]; then
            sed -i "s/10.0.0.1/${gateway}/g" "${WORK_DIR}/AdGuardHome.yaml"
        fi
    else
        echo "ℹ️ 未找到 AdGuardHome.yaml：首次启动后可在 WebUI 配置上游 DNS（或你之后再替换）。"
    fi

    # 8) .env 基本校验（统一用抽象函数）
    local required_vars=(MACVLAN_NET ipv4 macaddress)
    [ "$USE_IPV6" -eq 1 ] && required_vars+=(ipv6)

    env_require_vars ".env" "${required_vars[@]}" || {
        echo "⚠️ .env 校验失败，取消启动，避免影响现有 adguardhome"
        return 1
    }

    # 9) 固定使用主compose文件（已合并双栈配置）
    local compose_files=(docker-compose.yml)
    # 无IPv6场景：自动删除compose中的ipv6_address配置，避免启动报错
    if [ "$USE_IPV6" -eq 0 ]; then
        remove_compose_ipv6_fields docker-compose.yml
    fi

    # 10) 一步部署：校验 -> 停旧备份 -> 起新 -> next->正式 -> 正式再up -> 失败回滚
    #     （注意：第二个参数是容器名，必须和 compose 里的 container_name 一致）
    compose_deploy_with_repo_switch "adguardhome" "$CONTAINER_NAME" "${compose_files[@]}" || return 1

    echo "✅ AdGuardHome 已启动：${adguard}"
    echo "  macvlan 网络: ${SELECTED_MACVLAN}"
    echo "  MAC        : ${adguardmac}"
    echo "  上游 mosdns : ${mosdns}"
    echo "  容器名称   : ${CONTAINER_NAME}"
    if [ "$USE_IPV6" -eq 1 ]; then
        echo "  IPv6       : ${adguard6}"
    else
        echo "  IPv6       : 未启用（所选 macvlan 未开启 IPv6 或无 IPv6 子网）"
    fi

    # 11) 可选删除目录备份（带挂载检查）
    repo_offer_delete_backup "adguardhome" "$BAK_DIR" "adguardhome"
}

install_mosdns() {

    echo "🔧 安装 mosdns（需要选择 macvlan 网络）"

    # 0) 选择 macvlan（回车退出）
    select_macvlan_or_exit
    case $? in
      0) ;;
      2) return 0 ;;
      *) return 1 ;;
    esac

    # 用于写 mosdns 上游：mihomo 写 IPv4；能确定 IPv6 时再写 fake IPv6 上游
    local mihomo_input mihomo mihomo6

    read -r -p "surge请输入198.18.0.2, mihomo请输入输完整IP或最后一段（回车默认 120）: " mihomo_input

    # ✅ 关键修复
    if [ -z "$mihomo_input" ]; then
        calculate_ip_mac 120
        mihomo="$calculated_ip"
        mihomo6="$calculated_ip6"
    elif [[ "$mihomo_input" =~ ^[0-9]+$ ]]; then
        if [ "$mihomo_input" -lt 1 ] || [ "$mihomo_input" -gt 254 ]; then
            echo "❌ 无效的最后一段：$mihomo_input"
            return 1
        fi
        calculate_ip_mac "$mihomo_input"
        mihomo="$calculated_ip"
        mihomo6="$calculated_ip6"
    else
        mihomo="$(echo "$mihomo_input" | grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -n1)"
        [ -n "$mihomo" ] || { echo "❌ 无法解析 IPv4：$mihomo_input"; return 1; }
        if [ "$mihomo" = "198.18.0.2" ]; then
            mihomo6="fd00:6152::2"
        else
            mihomo6=""
            calculate_ip_mac "${mihomo##*.}"
            if [ "$calculated_ip" = "$mihomo" ]; then
                mihomo6="$calculated_ip6"
            fi
        fi
    fi

    if [ "$mihomo" = "198.18.0.2" ]; then
        echo "📌 上游 surge IPv4：$mihomo"
        echo "📌 上游 surge IPv6：$mihomo6"
    else
        echo "📌 上游 mihomo IPv4：$mihomo"
        if [ -n "$mihomo6" ]; then
            echo "📌 上游 mihomo IPv6：$mihomo6"
        else
            echo "📌 上游 mihomo IPv6：无法找到"
        fi
    fi

    # 2) 选择 mosdns IPv4 最后一段（回车默认 119）
    local mosdns_last
    mosdns_last="$(prompt_ipv4_last_octet \
      "请输入 mosdns IPv4 最后一段（1-254，回车默认 119）: " 119)" || return 1

    # 3) 计算 mosdns IP / IPv6 / MAC / 网关（基于 SELECTED_MACVLAN）
    calculate_ip_mac "$mosdns_last"
    local mosdns mosdns6 mosdnsmac gateway
    mosdns="$calculated_ip"
    mosdns6="$calculated_ip6"
    mosdnsmac="$calculated_mac"
    gateway="$calculated_gateway"

    # 是否启用 IPv6（逻辑跟 mihomo 一致：EnableIPv6=true 且存在 IPv6 Subnet）
    local USE_IPV6=0
    if docker network inspect "$SELECTED_MACVLAN" | jq -e '.[0].EnableIPv6==true and (.[0].IPAM.Config[]?.Subnet|test(":"))' >/dev/null 2>&1; then
        USE_IPV6=1
    fi

    # 4) 选择存储目录（回车退出）
    local dockerapps
    select_dockerapps_dir "mosdns"
    case $? in
      0) dockerapps="$SELECTED_DOCKERAPPS_DIR" ;;
      2) echo "✅ 已退出 mosdns 安装。"; return 0 ;;
      *) return 1 ;;
    esac
    mkdir -p "$dockerapps" || return 1

    # 5) 自定义容器/目录名称（回车用默认mosdns）
    local DEFAULT_CONTAINER_NAME="mosdns"
    local CONTAINER_NAME
    read -r -p "请输入容器名称（回车默认使用 '$DEFAULT_CONTAINER_NAME'）: " CONTAINER_NAME
    CONTAINER_NAME=${CONTAINER_NAME:-$DEFAULT_CONTAINER_NAME}

    # 6) 仓库更新：
    local REPO_URL="https://github.com/perryyeh/mosdns.git"
    repo_stage_update "mosdns" "$dockerapps" "$REPO_URL" "$CONTAINER_NAME" || return 1

    # repo_stage_update 会设置：WORK_DIR / NEED_SWITCH / NEXT_DIR / BAK_DIR（全局变量）
    cd "$WORK_DIR" || { echo "❌ 进入目录失败：$WORK_DIR"; return 1; }

    # 7) 替换compose配置中的容器相关字段
    if [ "$CONTAINER_NAME" != "$DEFAULT_CONTAINER_NAME" ]; then
        # 1. 替换services下一级的服务名称
        sed -i "s/^  $DEFAULT_CONTAINER_NAME:/  $CONTAINER_NAME:/" docker-compose.yml
        # 2. 替换container_name
        sed -i "s/container_name: $DEFAULT_CONTAINER_NAME/container_name: $CONTAINER_NAME/" docker-compose.yml
        # 3. 替换hostname
        sed -i "s/hostname: $DEFAULT_CONTAINER_NAME/hostname: $CONTAINER_NAME/" docker-compose.yml
        echo "✅ 已自定义容器名称/目录为：$CONTAINER_NAME"
    fi

    # 8) 替换 dns.yaml 里上游 mihomo / gateway
    if [ -f "dns.yaml" ]; then
        # 用 # 作为分隔符更稳（避免 / 等字符导致 sed 崩）
        sed -i "s#198.18.0.2#${mihomo}#g" dns.yaml
        if [ -n "$mihomo6" ]; then
            sed -i "s#fd00:6152::2#${mihomo6}#g" dns.yaml
        fi
        if [ -n "$gateway" ] && [ "$gateway" != "null" ]; then
            sed -i "s#10.0.0.1#${gateway}#g" dns.yaml
        fi
    else
        echo "❌ 未找到 ${WORK_DIR}/dns.yaml"
        return 1
    fi

    # 9) fake IPv6 开关：不开时 AAAA 也强制走 fake IPv4，避免 fake IPv6 链路不通导致解析可用但访问失败
    if [ -f "config.yaml" ]; then
        local enable_fakeipv6

        if [ "$mihomo" = "198.18.0.2" ]; then
            # surge：IPv6 是写死的，必须询问
            echo "⚠️ Surge fake IPv6 需要确认链路可用，坑较多。"
            read -r -p "是否开启 fake IPv6 解析？[y/N]: " enable_fakeipv6
            if [[ ! "$enable_fakeipv6" =~ ^[Yy]$ ]]; then
                sed -i 's#exec: \$forward_fakeipv6#exec: \$forward_fakeipv4#g' config.yaml
                echo "✅ 已关闭 fake IPv6 解析：AAAA 将走 forward_fakeipv4"
            else
                echo "✅ 已开启 fake IPv6 解析：AAAA 将走 forward_fakeipv6"
            fi
        elif [ -z "$mihomo6" ]; then
            # mihomo 无 IPv6：直接关
            echo "⚠️ mihomo 无 IPv6，关闭 fake IPv6 解析。"
            sed -i 's#exec: \$forward_fakeipv6#exec: \$forward_fakeipv4#g' config.yaml
            echo "✅ 已关闭 fake IPv6 解析：AAAA 将走 forward_fakeipv4"
        else
            # mihomo 有 IPv6：询问
            echo "⚠️ mihomo 有 IPv6，是否开启 fake IPv6 解析？"
            read -r -p "是否开启 fake IPv6 解析？[y/N]: " enable_fakeipv6
            if [[ ! "$enable_fakeipv6" =~ ^[Yy]$ ]]; then
                sed -i 's#exec: \$forward_fakeipv6#exec: \$forward_fakeipv4#g' config.yaml
                echo "✅ 已关闭 fake IPv6 解析：AAAA 将走 forward_fakeipv4"
            else
                echo "✅ 已开启 fake IPv6 解析：AAAA 将走 forward_fakeipv6"
            fi
        fi
    else
        echo "❌ 未找到 ${WORK_DIR}/config.yaml"
        return 1
    fi

    # 10) 可选功能：默认关闭，安装时按需开启
    local enable_rule_sync enable_candidate_auto

    if [ -f "cron/cron.env" ]; then
        read -r -p "是否每日自动更新 ip库 / 直连名单 / 代理名单？[y/N]: " enable_rule_sync
        if [[ "$enable_rule_sync" =~ ^[Yy]$ ]]; then
            sed -i 's/^MOSDNS_SYNC_RULES_ENABLED=.*/MOSDNS_SYNC_RULES_ENABLED=1/' cron/cron.env
            echo "✅ 已开启每日自动更新规则名单"
        else
            sed -i 's/^MOSDNS_SYNC_RULES_ENABLED=.*/MOSDNS_SYNC_RULES_ENABLED=0/' cron/cron.env
            echo "✅ 已关闭每日自动更新规则名单"
        fi

        read -r -p "是否开启日志 + 每小时统计不在名单内域名分流 + 每天自动合并并上报 my 名单？[y/N]: " enable_candidate_auto
        if [[ "$enable_candidate_auto" =~ ^[Yy]$ ]]; then
            sed -i 's/^MOSDNS_SYNC_CANDIDATES_ENABLED=.*/MOSDNS_SYNC_CANDIDATES_ENABLED=1/' cron/cron.env
            sed -i 's/^MOSDNS_PROMOTE_ENABLED=.*/MOSDNS_PROMOTE_ENABLED=1/' cron/cron.env
            sed -i 's/^[[:space:]]*level:[[:space:]]*.*/  level: info/' config.yaml
            echo "✅ 已开启 not-in-list 日志统计与自动合并"
        else
            sed -i 's/^MOSDNS_SYNC_CANDIDATES_ENABLED=.*/MOSDNS_SYNC_CANDIDATES_ENABLED=0/' cron/cron.env
            sed -i 's/^MOSDNS_PROMOTE_ENABLED=.*/MOSDNS_PROMOTE_ENABLED=0/' cron/cron.env
            sed -i 's/^[[:space:]]*level:[[:space:]]*.*/  level: warn/' config.yaml
            echo "✅ 已关闭 not-in-list 日志统计与自动合并"
        fi
    else
        echo "❌ 未找到 ${WORK_DIR}/cron/cron.env"
        return 1
    fi

    # 11) 写 .env（compose 读取）
    cat > .env <<EOF
MACVLAN_NET=${SELECTED_MACVLAN}
ipv4=${mosdns}
ipv6=${mosdns6}
macaddress=${mosdnsmac}
EOF

    echo "✅ 已生成 .env："
    cat .env
    echo

    if [ "$USE_IPV6" -eq 1 ] && [ -z "$mosdns6" ]; then
        echo "❌ 该 macvlan 网络启用了 IPv6，但未能计算出 mosdns6（可能 IPv6 子网解析失败）"
        return 1
    fi


    # 8) .env 基本校验
    local required_vars=(MACVLAN_NET ipv4 macaddress)
    [ "$USE_IPV6" -eq 1 ] && required_vars+=(ipv6)

    env_require_vars ".env" "${required_vars[@]}" || {
        echo "⚠️ .env 校验失败，取消启动，避免影响现有 mosdns"
        return 1
    }

    # 9) 固定使用主compose文件（已合并双栈配置）
    local compose_files=(docker-compose.yml)
    # 无IPv6场景：自动删除compose中的ipv6_address配置，避免启动报错
    if [ "$USE_IPV6" -eq 0 ]; then
        remove_compose_ipv6_fields docker-compose.yml
    fi
    # 10）一步部署：校验 -> 停旧备份 -> 起新 -> next->正式 -> 正式再up -> 失败回滚
    compose_deploy_with_repo_switch "mosdns" "$CONTAINER_NAME" "${compose_files[@]}" || return 1

    echo "✅ mosdns 已启动：${mosdns}"
    echo "  上游 mihomo / surge : ${mihomo}"
    echo "  macvlan 网络: ${SELECTED_MACVLAN}"
    echo "  MAC        : ${mosdnsmac}"
    echo "  容器名称   : ${CONTAINER_NAME}"
    if [ "$USE_IPV6" -eq 1 ]; then
        echo "  IPv6       : ${mosdns6}"
    else
        echo "  IPv6       : 未启用（所选 macvlan 未开启 IPv6 或无 IPv6 子网）"
    fi

    # 12) 可选删除备份（带挂载检查）
    repo_offer_delete_backup "mosdns" "$BAK_DIR" "mosdns"
}

install_mihomo() {

    echo "🔧 安装 mihomo"

    # 0) 选择网络模式
    local MIHOMO_NETWORK_MODE network_choice
    echo "请选择 mihomo 网络模式："
    echo "  1) host（使用宿主机网络，适合在外回家）"
    echo "  2) macvlan（独立 LAN IP/MAC，适合旁路由）"
    read -r -p "请输入选择（回车默认 macvlan）: " network_choice
    case "$network_choice" in
        ""|"2"|"macvlan"|"MACVLAN")
            MIHOMO_NETWORK_MODE="macvlan"
            ;;
        "1"|"host"|"HOST")
            MIHOMO_NETWORK_MODE="host"
            ;;
        *)
            echo "❌ 无效选择：$network_choice"
            return 1
            ;;
    esac
    echo "📡 mihomo 网络模式：$MIHOMO_NETWORK_MODE"

    # 1) macvlan 模式才选择 macvlan 并计算独立 IP/MAC/Gateway
    local mihomo="" mihomo6="" mihomomac="" gateway="" USE_IPV6=0
    if [ "$MIHOMO_NETWORK_MODE" = "macvlan" ]; then
        select_macvlan_or_exit
        case $? in
          0) ;;
          2) return 0 ;;
          *) return 1 ;;
        esac

        local mihomo_last
        mihomo_last="$(prompt_ipv4_last_octet \
          "请输入 mihomo IPv4 最后一段（1-254，回车默认 120）: " 120)" || return 1

        calculate_ip_mac "$mihomo_last"
        mihomo="$calculated_ip"
        mihomo6="$calculated_ip6"
        mihomomac="$calculated_mac"
        gateway="$calculated_gateway"

        if macvlan_ipv6_enabled "$SELECTED_MACVLAN"; then
            USE_IPV6=1
        fi
    else
        echo "⚠️ host 模式会直接占用宿主机端口，请确认 7890/7891/7892/9090 等端口没有冲突。"
    fi

    # 2) 选择存储目录（回车退出）
    local dockerapps
    select_dockerapps_dir "mihomo"
    case $? in
      0) dockerapps="$SELECTED_DOCKERAPPS_DIR" ;;
      2) echo "✅ 已退出 mihomo 安装。"; return 0 ;;
      *) return 1 ;;
    esac

    mkdir -p "$dockerapps" || return 1
    cd "$dockerapps" || return 1

    # 3) 自定义容器/目录名称（回车用默认mihomo）
    local DEFAULT_CONTAINER_NAME="mihomo"
    local CONTAINER_NAME
    read -r -p "请输入容器名称（回车默认使用 '$DEFAULT_CONTAINER_NAME'）: " CONTAINER_NAME
    CONTAINER_NAME=${CONTAINER_NAME:-$DEFAULT_CONTAINER_NAME}

    # 4) repo 分阶段更新（内部会设置 WORK_DIR / NEED_SWITCH / BAK_DIR 等全局变量）
    REPO_URL="https://github.com/perryyeh/mihomo.git"
    repo_stage_update "mihomo" "$dockerapps" "$REPO_URL" "$CONTAINER_NAME" || return 1
    cd "$WORK_DIR" || { echo "❌ 进入目录失败：$WORK_DIR"; return 1; }

    # 5) 根据网络模式选择 compose/config 模板，并复制为正式文件
    local compose_template="docker-compose.${MIHOMO_NETWORK_MODE}.yml"
    local config_template="config.${MIHOMO_NETWORK_MODE}.yaml"
    if [ ! -f "$compose_template" ]; then
        echo "❌ 缺少 compose 模板：$compose_template"
        return 1
    fi
    if [ ! -f "$config_template" ]; then
        echo "❌ 缺少 mihomo 配置模板：$config_template"
        return 1
    fi
    cp "$compose_template" docker-compose.yml || return 1
    cp "$config_template" config.yaml || return 1
    echo "✅ 已选择 compose 模板：$compose_template -> docker-compose.yml"
    echo "✅ 已选择 mihomo 配置模板：$config_template -> config.yaml"

    # 6) 替换compose配置中的容器相关字段
    if [ "$CONTAINER_NAME" != "$DEFAULT_CONTAINER_NAME" ]; then
        # 1. 替换services下一级的服务名称
        sed -i "s/^  $DEFAULT_CONTAINER_NAME:/  $CONTAINER_NAME:/" docker-compose.yml
        # 2. 替换container_name
        sed -i "s/container_name: $DEFAULT_CONTAINER_NAME/container_name: $CONTAINER_NAME/" docker-compose.yml
        # 3. 替换hostname
        sed -i "s/hostname: $DEFAULT_CONTAINER_NAME/hostname: $CONTAINER_NAME/" docker-compose.yml
        echo "✅ 已自定义容器名称/目录为：$CONTAINER_NAME"
    fi

    # 7) macvlan 模式替换 config.yaml 里的网关；host 模式保持仓库默认配置
    if [ "$MIHOMO_NETWORK_MODE" = "macvlan" ] && [ -f "config.yaml" ] && [ -n "$gateway" ] && [ "$gateway" != "null" ]; then
        sed -i "s/10.0.0.1/${gateway}/g" config.yaml
    fi

    # 8) macvlan 模式写 .env；host 模式清掉旧 .env，避免误导
    if [ "$MIHOMO_NETWORK_MODE" = "macvlan" ]; then
        write_env_file "$WORK_DIR/.env" \
          "MACVLAN_NET=${SELECTED_MACVLAN}" \
          "ipv4=${mihomo}" \
          "ipv6=${mihomo6}" \
          "macaddress=${mihomomac}"

        echo "✅ 已生成 .env 文件："
        cat .env
        echo

        if [ "$USE_IPV6" -eq 1 ] && [ -z "$mihomo6" ]; then
            echo "❌ 该 macvlan 网络启用了 IPv6，但未能计算出 mihomo6（可能 IPv6 子网解析失败）"
            return 1
        fi

        local required_vars=(MACVLAN_NET ipv4 macaddress)
        [ "$USE_IPV6" -eq 1 ] && required_vars+=(ipv6)

        env_require_vars ".env" "${required_vars[@]}" || {
            echo "⚠️ .env 校验失败，取消启动，避免断网"
            return 1
        }

        # 无 IPv6 场景：自动删除 compose 中的 ipv6_address 配置，避免启动报错
        if [ "$USE_IPV6" -eq 0 ]; then
            remove_compose_ipv6_fields docker-compose.yml
        fi
    else
        rm -f "$WORK_DIR/.env"
    fi

    # 9) 固定使用安装时生成的正式 compose 文件
    local compose_files=(docker-compose.yml)

    # 10) 一步部署：校验 -> 停旧备份 -> 起新 -> next->正式 -> 正式再up -> 失败回滚
    compose_deploy_with_repo_switch "mihomo" "$CONTAINER_NAME" "${compose_files[@]}" || return 1

    # 11) 输出访问地址
    echo "✅ mihomo 已启动"
    echo "网络模式：${MIHOMO_NETWORK_MODE}"
    echo "容器名称：${CONTAINER_NAME}"
    if [ "$MIHOMO_NETWORK_MODE" = "macvlan" ]; then
        echo "macvlan 网络：${SELECTED_MACVLAN}"
        echo "IPv4 地址：${mihomo}"
        echo "MAC 地址：${mihomomac}"
        echo "访问地址：http://${mihomo}:9090/ui/  密码：admin"
        if [ "$USE_IPV6" -eq 1 ] && [ -n "$mihomo6" ]; then
            echo "IPv6：${mihomo6}"
            echo "IPv6 访问：http://[${mihomo6}]:9090/ui/  密码：admin"
        else
            echo "IPv6：未启用（所选 macvlan 未开启 IPv6 或无 IPv6 子网）"
        fi
    else
        local host_ip
        host_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
        [ -z "$host_ip" ] && host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
        if [ -n "$host_ip" ]; then
            echo "访问地址：http://${host_ip}:9090/ui/  密码：admin"
        else
            echo "访问地址：http://<宿主机IP>:9090/ui/  密码：admin"
        fi
        echo "⚠️ host 模式会直接占用宿主机端口，请确认 7890/7891/7892/9090 等端口没有冲突。"
    fi

    # 12) 可选删除备份（带挂载检查）
    repo_offer_delete_backup "mihomo" "$BAK_DIR" "$CONTAINER_NAME"
}

install_ddnsgo() {
    echo "🔧 安装 ddns-go"

    # 0) 选择网络模式
    local DDNSGO_NETWORK_MODE network_choice
    echo "请选择 ddns-go 网络模式："
    echo "  1) host（使用宿主机网络，适合有 IPv4 + IPv6 的环境）"
    echo "  2) macvlan（独立 LAN IP/MAC，适合只有 IPv4 的环境）"
    read -r -p "请输入选择（回车默认 host）: " network_choice
    case "$network_choice" in
        ""|"1"|"host"|"HOST")
            DDNSGO_NETWORK_MODE="host"
            ;;
        "2"|"macvlan"|"MACVLAN")
            DDNSGO_NETWORK_MODE="macvlan"
            ;;
        *)
            echo "❌ 无效选择：$network_choice"
            return 1
            ;;
    esac
    echo "📡 ddns-go 网络模式：$DDNSGO_NETWORK_MODE"

    # 1) macvlan 模式才选择 macvlan 并计算独立 IP/MAC
    local ddnsgo_ip="" ddnsgo6="" ddnsgomac="" USE_IPV6=0
    if [ "$DDNSGO_NETWORK_MODE" = "macvlan" ]; then
        select_macvlan_or_exit
        case $? in
          0) ;;
          2) return 0 ;;
          *) return 1 ;;
        esac

        local ddnsgo_last
        ddnsgo_last="$(prompt_ipv4_last_octet \
          "请输入 ddns-go IPv4 最后一段（1-254，回车默认 198）: " 198)" || return 1

        calculate_ip_mac "$ddnsgo_last"
        ddnsgo_ip="$calculated_ip"
        ddnsgo6="$calculated_ip6"
        ddnsgomac="$calculated_mac"

        if macvlan_ipv6_enabled "$SELECTED_MACVLAN"; then
            USE_IPV6=1
        fi
    else
        echo "⚠️ host 模式会直接占用宿主机端口，请确认 9876 及 ddns-go 内部配置的端口没有冲突。"
    fi

    # 2) 选择存储目录（回车退出）
    local dockerapps
    select_dockerapps_dir "ddns-go"
    case $? in
      0) dockerapps="$SELECTED_DOCKERAPPS_DIR" ;;
      2) echo "✅ 已退出 ddns-go 安装。"; return 0 ;;
      *) return 1 ;;
    esac

    mkdir -p "$dockerapps" || return 1
    cd "$dockerapps" || return 1

    # 3) 自定义容器/目录名称（回车用默认ddnsgo）
    local DEFAULT_SERVICE_NAME="ddns-go"
    local DEFAULT_CONTAINER_NAME="ddnsgo"
    local CONTAINER_NAME
    read -r -p "请输入容器名称（回车默认使用 '$DEFAULT_CONTAINER_NAME'）: " CONTAINER_NAME
    CONTAINER_NAME=${CONTAINER_NAME:-$DEFAULT_CONTAINER_NAME}

    # 4) repo 分阶段更新：目录名使用用户输入的容器名
    REPO_URL="https://github.com/perryyeh/ddnsgo.git"
    repo_stage_update "ddnsgo" "$dockerapps" "$REPO_URL" "$CONTAINER_NAME" || return 1
    cd "$WORK_DIR" || { echo "❌ 进入目录失败：$WORK_DIR"; return 1; }

    # 5) 根据网络模式选择 compose 模板，并复制为正式 docker-compose.yml
    local compose_template="docker-compose.${DDNSGO_NETWORK_MODE}.yml"
    if [ ! -f "$compose_template" ]; then
        echo "❌ 缺少 compose 模板：$compose_template"
        return 1
    fi
    cp "$compose_template" docker-compose.yml || return 1
    echo "✅ 已选择 compose 模板：$compose_template -> docker-compose.yml"

    # 6) 替换compose配置中的容器相关字段
    if [ "$CONTAINER_NAME" != "$DEFAULT_CONTAINER_NAME" ]; then
        # 1. 替换services下一级的服务名称（ddns-go 默认服务名带连字符）
        sed -i "s/^  $DEFAULT_SERVICE_NAME:/  $CONTAINER_NAME:/" docker-compose.yml
        # 2. 替换container_name
        sed -i "s/container_name: $DEFAULT_CONTAINER_NAME/container_name: $CONTAINER_NAME/" docker-compose.yml
        # 3. 替换hostname
        sed -i "s/hostname: $DEFAULT_CONTAINER_NAME/hostname: $CONTAINER_NAME/" docker-compose.yml
        echo "✅ 已自定义容器名称/目录为：$CONTAINER_NAME"
    fi

    # 7) macvlan 模式写 .env；host 模式清掉旧 .env，避免误导
    if [ "$DDNSGO_NETWORK_MODE" = "macvlan" ]; then
        write_env_file "$WORK_DIR/.env" \
          "MACVLAN_NET=${SELECTED_MACVLAN}" \
          "ipv4=${ddnsgo_ip}" \
          "ipv6=${ddnsgo6}" \
          "macaddress=${ddnsgomac}"

        echo "✅ 已生成 .env："
        cat .env
        echo

        local required_vars=(MACVLAN_NET ipv4 macaddress)
        [ "$USE_IPV6" -eq 1 ] && required_vars+=(ipv6)

        env_require_vars ".env" "${required_vars[@]}" || {
            echo "⚠️ .env 校验失败，取消启动，避免影响现有 ddns-go"
            return 1
        }

        # 无 IPv6 场景：自动删除 compose 中的 ipv6_address 配置，避免启动报错
        if [ "$USE_IPV6" -eq 0 ]; then
            remove_compose_ipv6_fields docker-compose.yml
        fi
    else
        rm -f "$WORK_DIR/.env"
    fi

    # 8) 选择 compose 文件列表（默认只用 docker-compose.yml）
    local compose_files=(docker-compose.yml)

    # 9) 一步部署：校验 -> 停旧备份 -> 起新 -> next->正式 -> 正式再up -> 失败回滚
    compose_deploy_with_repo_switch "ddnsgo" "$CONTAINER_NAME" "${compose_files[@]}" || return 1

    # 10) ddns-go 管理界面地址（默认监听 9876）
    local ddns_port=9876
    echo "✅ ddns-go 已启动"
    echo "网络模式：${DDNSGO_NETWORK_MODE}"
    echo "容器名称：${CONTAINER_NAME}"

    if [ "$DDNSGO_NETWORK_MODE" = "macvlan" ]; then
        echo "macvlan 网络：${SELECTED_MACVLAN}"
        echo "IPv4 地址：${ddnsgo_ip}"
        echo "MAC 地址：${ddnsgomac}"
        echo "访问地址：http://${ddnsgo_ip}:${ddns_port}/"
        if [ "$USE_IPV6" -eq 1 ] && [ -n "$ddnsgo6" ]; then
            echo "IPv6 地址：${ddnsgo6}"
            echo "IPv6 访问：http://[${ddnsgo6}]:${ddns_port}/"
        else
            echo "IPv6：未启用（所选 macvlan 未开启 IPv6 或无 IPv6 子网）"
        fi
    else
        local host_ip
        host_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
        [ -z "$host_ip" ] && host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
        if [ -n "$host_ip" ]; then
            echo "访问地址：http://${host_ip}:${ddns_port}/"
        else
            echo "访问地址：http://<宿主机IP>:${ddns_port}/"
        fi
        echo "⚠️ host 模式会直接占用宿主机端口，请确认 9876 及 ddns-go 内部配置的端口没有冲突。"
    fi

    # 11) 可选删除备份（带挂载检查）
    repo_offer_delete_backup "ddnsgo" "$BAK_DIR" "$CONTAINER_NAME"
}


install_lucky() {
    echo "🔧 安装 Lucky"

    # 0) 选择网络模式
    local LUCKY_NETWORK_MODE network_choice
    echo "请选择 Lucky 网络模式："
    echo "  1) host（使用宿主机网络，适合有 IPv4 + IPv6 的环境）"
    echo "  2) macvlan（独立 LAN IP/MAC，适合只有 IPv4 的环境）"
    read -r -p "请输入选择（回车默认 host）: " network_choice
    case "$network_choice" in
        ""|"1"|"host"|"HOST")
            LUCKY_NETWORK_MODE="host"
            ;;
        "2"|"macvlan"|"MACVLAN")
            LUCKY_NETWORK_MODE="macvlan"
            ;;
        *)
            echo "❌ 无效选择：$network_choice"
            return 1
            ;;
    esac
    echo "📡 Lucky 网络模式：$LUCKY_NETWORK_MODE"

    # 1) macvlan 模式才选择 macvlan 并计算独立 IP/MAC
    local lucky="" lucky6="" luckymac="" USE_IPV6=0
    if [ "$LUCKY_NETWORK_MODE" = "macvlan" ]; then
        select_macvlan_or_exit
        case $? in
          0) ;;
          2) return 0 ;;
          *) return 1 ;;
        esac

        local lucky_last
        lucky_last="$(prompt_ipv4_last_octet \
          "请输入 Lucky IPv4 最后一段（1-254，回车默认 166）: " 166)" || return 1

        calculate_ip_mac "$lucky_last"
        lucky="$calculated_ip"
        lucky6="$calculated_ip6"
        luckymac="$calculated_mac"

        if macvlan_ipv6_enabled "$SELECTED_MACVLAN"; then
            USE_IPV6=1
        fi
    else
        echo "⚠️ host 模式会直接占用宿主机端口，请确认 16601 及 Lucky 内部配置的端口没有冲突。"
    fi

    # 2) 选择存储目录（回车退出）
    local dockerapps
    select_dockerapps_dir "Lucky"
    case $? in
      0) dockerapps="$SELECTED_DOCKERAPPS_DIR" ;;
      2) echo "✅ 已退出 Lucky 安装。"; return 0 ;;
      *) return 1 ;;
    esac

    mkdir -p "$dockerapps" || return 1
    cd "$dockerapps" || return 1

    # 3) 自定义容器/目录名称（回车用默认lucky）
    local DEFAULT_CONTAINER_NAME="lucky"
    local CONTAINER_NAME
    read -r -p "请输入容器名称（回车默认使用 '$DEFAULT_CONTAINER_NAME'）: " CONTAINER_NAME
    CONTAINER_NAME=${CONTAINER_NAME:-$DEFAULT_CONTAINER_NAME}

    # 4) repo 更新：目录名使用用户输入的容器名
    REPO_URL="https://github.com/perryyeh/lucky.git"
    repo_stage_update "lucky" "$dockerapps" "$REPO_URL" "$CONTAINER_NAME" || return 1
    cd "$WORK_DIR" || { echo "❌ 进入目录失败：$WORK_DIR"; return 1; }

    # 5) 根据网络模式选择 compose 模板，并复制为正式 docker-compose.yml
    local compose_template="docker-compose.${LUCKY_NETWORK_MODE}.yml"
    if [ ! -f "$compose_template" ]; then
        echo "❌ 缺少 compose 模板：$compose_template"
        return 1
    fi
    cp "$compose_template" docker-compose.yml || return 1
    echo "✅ 已选择 compose 模板：$compose_template -> docker-compose.yml"

    # 6) 替换compose配置中的容器相关字段
    if [ "$CONTAINER_NAME" != "$DEFAULT_CONTAINER_NAME" ]; then
        # 1. 替换services下一级的服务名称
        sed -i "s/^  $DEFAULT_CONTAINER_NAME:/  $CONTAINER_NAME:/" docker-compose.yml
        # 2. 替换container_name
        sed -i "s/container_name: $DEFAULT_CONTAINER_NAME/container_name: $CONTAINER_NAME/" docker-compose.yml
        # 3. 替换hostname
        sed -i "s/hostname: $DEFAULT_CONTAINER_NAME/hostname: $CONTAINER_NAME/" docker-compose.yml
        echo "✅ 已自定义容器名称/目录为：$CONTAINER_NAME"
    fi

    # 7) macvlan 模式写 .env；host 模式清掉旧 .env，避免误导
    if [ "$LUCKY_NETWORK_MODE" = "macvlan" ]; then
        write_env_file "$WORK_DIR/.env" \
          "MACVLAN_NET=${SELECTED_MACVLAN}" \
          "ipv4=${lucky}" \
          "ipv6=${lucky6}" \
          "macaddress=${luckymac}"

        echo "✅ 已生成 .env："
        cat .env
        echo

        local required_vars=(MACVLAN_NET ipv4 macaddress)
        [ "$USE_IPV6" -eq 1 ] && required_vars+=(ipv6)

        env_require_vars ".env" "${required_vars[@]}" || {
            echo "⚠️ .env 校验失败，取消启动，避免影响现有 lucky"
            return 1
        }

        # 无 IPv6 场景：自动删除 compose 中的 ipv6_address 配置，避免启动报错
        if [ "$USE_IPV6" -eq 0 ]; then
            remove_compose_ipv6_fields docker-compose.yml
        fi
    else
        rm -f "$WORK_DIR/.env"
    fi

    # 8) compose 文件
    local compose_files=(docker-compose.yml)

    # 9) 部署
    compose_deploy_with_repo_switch "lucky" "$CONTAINER_NAME" "${compose_files[@]}" || return 1

    # 10) Lucky Web 面板
    local lucky_port=16601
    echo "✅ Lucky 已启动"
    echo "网络模式：${LUCKY_NETWORK_MODE}"
    echo "容器名称：${CONTAINER_NAME}"

    if [ "$LUCKY_NETWORK_MODE" = "macvlan" ]; then
        echo "macvlan 网络：${SELECTED_MACVLAN}"
        echo "IPv4 地址：${lucky}"
        echo "MAC 地址：${luckymac}"
        echo "访问地址：http://${lucky}:${lucky_port}/"
        if [ "$USE_IPV6" -eq 1 ] && [ -n "$lucky6" ]; then
            echo "IPv6 地址：${lucky6}"
            echo "IPv6 访问：http://[${lucky6}]:${lucky_port}/"
        else
            echo "IPv6：未启用（所选 macvlan 未开启 IPv6 或无 IPv6 子网）"
        fi
    else
        local host_ip
        host_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
        [ -z "$host_ip" ] && host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
        if [ -n "$host_ip" ]; then
            echo "访问地址：http://${host_ip}:${lucky_port}/"
        else
            echo "访问地址：http://<宿主机IP>:${lucky_port}/"
        fi
        echo "⚠️ host 模式会直接占用宿主机端口，请确认 16601 及 Lucky 内部配置的端口没有冲突。"
    fi

    # 11) 可选删除备份
    repo_offer_delete_backup "lucky" "$BAK_DIR" "$CONTAINER_NAME"
}

install_portainer() {
    local dockerapps
    select_dockerapps_dir "portainer"
    case $? in
      0) dockerapps="$SELECTED_DOCKERAPPS_DIR" ;;
      2) echo "✅ 已退出 portainer 安装。"; return 0 ;;
      *) return 1 ;;
    esac

    mkdir -p "${dockerapps}/portainer" || return 1
    docker run -d -p 9443:9443 --name=portainer --restart=always \
    -v /var/run/docker.sock:/var/run/docker.sock -v "${dockerapps}/portainer:/data" portainer/portainer-ce:lts
}

# ========== 删除 docker macvlan 网络 ==========
clean_macvlan_network() {
    echo "🧹 清理 Docker macvlan 网络"

    # 找出所有以 macvlan 开头的 Docker 网络
    mapfile -t macvlan_networks < <(docker network ls --format '{{.Name}}' | grep '^macvlan' || true)

    if [ ${#macvlan_networks[@]} -eq 0 ]; then
        echo "ℹ️ 当前没有任何以 macvlan 开头的 Docker 网络。"
        return 0
    fi

    # 列表展示（含是否使用中）
    echo "检测到以下 macvlan 网络："
    for i in "${!macvlan_networks[@]}"; do
        net="${macvlan_networks[$i]}"
        containers=$(docker network inspect -f '{{range $id,$c := .Containers}}{{printf "%s " $c.Name}}{{end}}' "$net" 2>/dev/null)
        if [ -n "$containers" ]; then
            echo "  $i) $net    (使用中的容器: $containers)"
        else
            echo "  $i) $net"
        fi
    done

    echo
    echo "请输入要删除的网络序号，或输入 a 表示删除全部，回车取消："
    read -p "你的选择: " choice

    if [ -z "$choice" ]; then
        echo "⚠️ 已取消删除 macvlan 网络。"
        return 0
    fi

    local to_delete=()

    if [[ "$choice" =~ ^[0-9]+$ ]]; then
        if [ "$choice" -lt 0 ] || [ "$choice" -ge "${#macvlan_networks[@]}" ]; then
            echo "❌ 无效的序号。"
            return 1
        fi
        to_delete=("${macvlan_networks[$choice]}")
    elif [[ "$choice" =~ ^[Aa]$ ]]; then
        to_delete=("${macvlan_networks[@]}")
    else
        echo "❌ 无效输入。"
        return 1
    fi

    # 先构建剩余网络的 <phys>_<vlan> 索引，用于判断 VLAN 是否仍被其他 macvlan 使用
    declare -A remain_key_count
    for net in "${macvlan_networks[@]}"; do
        skip=false
        for del in "${to_delete[@]}"; do
            [[ "$net" == "$del" ]] && { skip=true; break; }
        done
        $skip && continue
        # 解析 macvlan_<phys> 或 macvlan_<phys>_<vid>
        if [[ "$net" =~ ^macvlan_([A-Za-z0-9_-]+)_([0-9]+)$ ]]; then
            phys="${BASH_REMATCH[1]}"
            vid="${BASH_REMATCH[2]}"
            key="${phys}_${vid}"
            remain_key_count["$key"]=$(( ${remain_key_count["$key"]:-0} + 1 ))
        elif [[ "$net" =~ ^macvlan_([A-Za-z0-9_-]+)$ ]]; then
            phys="${BASH_REMATCH[1]}"
            # 无 VLAN 的网络，不涉及删除子接口
        fi
    done

    for net in "${to_delete[@]}"; do
        echo
        echo "🧻 准备删除 macvlan 网络: $net"

        containers=$(docker network inspect -f '{{range $id,$c := .Containers}}{{printf "%s " $c.Name}}{{end}}' "$net" 2>/dev/null)
        if [ -n "$containers" ]; then
            echo "⚠️ 该网络仍有容器在使用：$containers"
            read -p "是否强制删除该网络？相关容器将失去该网络连接。(y/N): " yn
            if [[ ! "$yn" =~ ^[Yy]$ ]]; then
                echo "⏭ 已跳过 $net"
                continue
            fi
        fi

        if docker network rm "$net"; then
            echo "✅ 已删除 macvlan 网络: $net"
        else
            echo "❌ 删除 macvlan 网络失败: $net"
            continue
        fi

        # —— 尝试同步清理当初创建的 VLAN 子接口（如 eth0.88）——
        # 仅当网络名为 macvlan_<phys>_<vid> 时尝试推断；phys 假定与系统实际接口同名（之前已做过安全化）
        if [[ "$net" =~ ^macvlan_([A-Za-z0-9_-]+)_([0-9]+)$ ]]; then
            phys_safe="${BASH_REMATCH[1]}"
            vid="${BASH_REMATCH[2]}"

            # 如果其它 macvlan 仍在用相同 <phys>_<vid>，则不清理该 VLAN 子接口
            key="${phys_safe}_${vid}"
            if [ "${remain_key_count[$key]:-0}" -gt 0 ]; then
                echo "ℹ️ 仍有其它 macvlan 使用 ${phys_safe}.${vid}，跳过删除该 VLAN 子接口。"
                continue
            fi

            # 推断真实物理口名（之前创建时仅做过“安全字符替换”，常见 eth0/eno1/enpXsY 均一致）
            phys="$phys_safe"
            vlan_if="${phys}.${vid}"

            # 仅当 VLAN 子接口存在时才考虑删除
            if ip link show "$vlan_if" >/dev/null 2>&1; then
                echo "🔎 检测到同名 VLAN 子接口：$vlan_if"
                # 再保险：确认该 VLAN 接口当前没有地址或不在使用中（不强制，但给出提示）
                has_addr4=$(ip -4 addr show "$vlan_if" | awk '/ inet /{print $2}' | wc -l)
                has_addr6=$(ip -6 addr show "$vlan_if" | awk '/ inet6 /{print $2}' | wc -l)

                if [ "$has_addr4" -gt 0 ] || [ "$has_addr6" -gt 0 ]; then
                    echo "⚠️ 注意：$vlan_if 当前仍有 IP 地址：IPv4=$has_addr4, IPv6=$has_addr6"
                fi

                read -p "是否一并删除 VLAN 子接口 $vlan_if ？(y/N): " delv
                if [[ "$delv" =~ ^[Yy]$ ]]; then
                    sudo ip link set "$vlan_if" down 2>/dev/null || true
                    if sudo ip link delete "$vlan_if"; then
                        echo "✅ 已删除 VLAN 子接口：$vlan_if"
                    else
                        echo "❌ 删除 VLAN 子接口失败：$vlan_if"
                    fi
                else
                    echo "⏭ 已保留 VLAN 子接口：$vlan_if"
                fi
            fi
        fi
    done
}

# ========== 删除 docker macvlan bridge ==========
clean_macvlan_bridge() {
    echo "🧹 清理 macvlan bridge（支持多个）"

    # 找 macvlan_* 的 systemd 服务
    local svc_files=()
    if compgen -G "/etc/systemd/system/macvlan*.service" > /dev/null; then
        for f in /etc/systemd/system/macvlan*.service; do
            svc_files+=("$f")
        done
    fi

    if [ ${#svc_files[@]} -eq 0 ]; then
        echo "ℹ️ 未发现 macvlan bridge service。"
        return 0
    fi

    echo "检测到以下 macvlan bridge 服务："
    local i
    for i in "${!svc_files[@]}"; do
        local svc_path="${svc_files[$i]}"
        local svc_name=$(basename "$svc_path")
        local safe_name="${svc_name%.service}"
        local setup_script="/usr/local/bin/${safe_name}.sh"

        # ⭐直接从脚本中提取 bridge_if（最可靠）
        local bridge_if=""
        if [ -f "$setup_script" ]; then
            bridge_if=$(grep -E 'ip link add "[^"]+"' "$setup_script" | \
                        head -n1 | sed -E 's/.*add "([^"]+)".*/\1/')
        fi

        echo "  $i) 服务: $svc_name   接口: ${bridge_if:-未知}   脚本: $setup_script"
    done

    echo
    read -p "请输入要清理的序号，或输入 a 表示清理全部，回车取消: " choice
    [ -z "$choice" ] && { echo "⚠️ 已取消"; return 0; }

    local to_clean=()
    if [[ "$choice" =~ ^[0-9]+$ ]]; then
        to_clean=("${svc_files[$choice]}")
    elif [[ "$choice" =~ ^[Aa]$ ]]; then
        to_clean=("${svc_files[@]}")
    else
        echo "❌ 无效输入"
        return 1
    fi

    for svc_path in "${to_clean[@]}"; do
        local svc_name=$(basename "$svc_path")
        local safe_name="${svc_name%.service}"
        local setup_script="/usr/local/bin/${safe_name}.sh"

        # ⭐再从脚本中提取一次 bridge_if
        local bridge_if=""
        if [ -f "$setup_script" ]; then
            bridge_if=$(grep -E 'ip link add "[^"]+"' "$setup_script" | \
                        head -n1 | sed -E 's/.*add "([^"]+)".*/\1/')
        fi

        echo "🧻 清理: $svc_name"
        echo "   bridge_if: ${bridge_if:-未知}"
        echo "   脚本: $setup_script"

        # 停止服务
        systemctl disable --now "$svc_name" 2>/dev/null || true

        # 删除网卡
        [ -n "$bridge_if" ] && ip link del "$bridge_if" 2>/dev/null || true

        # 删除脚本
        [ -f "$setup_script" ] && rm -f "$setup_script"

        # 删除 service
        rm -f "$svc_path"
    done

    systemctl daemon-reload
    echo "✅ 清理完成。"
}

install_watchtower() {
    echo "🔧 安装并启动常驻 watchtower..."

    # 检查并删除旧容器（不管状态）
    if docker ps -a --format '{{.Names}}' | grep -q '^watchtower$'; then
        echo "🗑️ 发现旧的 watchtower 容器，强制删除..."
        docker rm -f watchtower >/dev/null 2>&1 || true
    fi

    # 拉最新镜像
    echo "📦 拉取最新 watchtower 镜像..."
    docker pull containrrr/watchtower:latest

    API=$(docker version --format '{{.Server.APIVersion}}')

    docker run -d \
      --name watchtower \
      --network host \
      --restart=always \
      -e DOCKER_API_VERSION="$API" \
      -e TZ="Asia/Shanghai" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      containrrr/watchtower:latest \
      --cleanup \
      --include-restarting \
      --revive-stopped

    echo "✅ watchtower 已常驻运行"
}

run_watchtower_once() {
    echo "🔧 正在执行 watchtower --run-once 更新所有容器（排除 watchtower 自身）..."
    API=$(docker version --format '{{.Server.APIVersion}}')   # 预期=1.52
    docker run --rm \
        -e DOCKER_API_VERSION="$API" \
        -v /var/run/docker.sock:/var/run/docker.sock \
        containrrr/watchtower:latest \
        --run-once \
        --cleanup \
        --include-stopped \
        --disable-containers watchtower
    echo "✅ watchtower run-once 更新完成"
}

# =====================
#  功能 70：迁移 Docker 目录
# =====================
migrate_docker_datadir() {
    # 前置校验
    if [ -z "${BASH_VERSION:-}" ]; then exec /usr/bin/env bash "$0" "$@"; fi
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then echo "请以 root 权限运行（sudo bash $0）"; return 1; fi
    if ! command -v docker >/dev/null 2>&1; then
        echo "未检测到 Docker，请先安装 Docker 后再迁移。"
        return 1
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "未检测到 systemctl，无法停止/启动 docker 服务。"
        return 1
    fi

    local DEFAULT_ROOT="/var/lib/docker"
    local CURRENT_ROOT=""
    local NEW_ROOT=""
    local DAEMON_JSON="/etc/docker/daemon.json"
    local BACKUP_SUFFIX
    BACKUP_SUFFIX="$(date +%Y%m%d-%H%M%S)"

    # 读取当前 Docker Root Dir（优先 docker info）
    CURRENT_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
    if [[ -z "$CURRENT_ROOT" ]]; then
        # docker daemon 可能没起，兜底从 daemon.json 读
        if [[ -f "$DAEMON_JSON" ]]; then
            CURRENT_ROOT="$(sed -n 's/.*"data-root"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$DAEMON_JSON" | head -n1)"
        fi
        [[ -z "$CURRENT_ROOT" ]] && CURRENT_ROOT="$DEFAULT_ROOT"
    fi

    echo "📌 Docker 默认目录：$DEFAULT_ROOT"
    echo "📌 Docker 当前目录：$CURRENT_ROOT"
    echo

    # 如果已经不是默认目录，询问是否继续迁移
    if [[ "$CURRENT_ROOT" != "$DEFAULT_ROOT" ]]; then
        echo "⚠️ 检测到 Docker 已不在默认目录（已迁移过）。"
        read -r -p "是否要再次迁移到新的目录？(y/N，回车默认不迁移): " again
        if [[ ! "$again" =~ ^[Yy]$ ]]; then
            echo "✅ 已取消迁移。"
            return 0
        fi
    fi

    # 读取用户输入的新目录（回车退出不迁移）
    read -r -p "请输入迁移目标目录（例如 /data/docker；回车退出不迁移）: " NEW_ROOT
    if [[ -z "$NEW_ROOT" ]]; then
        echo "✅ 未输入路径，已退出迁移。"
        return 0
    fi

    # 规范化路径：去掉末尾 /
    NEW_ROOT="${NEW_ROOT%/}"

    # 目标目录必须存在（按你原逻辑）
    if [[ ! -d "$NEW_ROOT" ]]; then
        echo "❌ 目录不存在：$NEW_ROOT  —— 已取消迁移。"
        return 1
    fi

    if [[ "$NEW_ROOT" == "$CURRENT_ROOT" ]]; then
        echo "✅ 目标目录与当前目录相同，无需迁移。"
        return 0
    fi

    # 停止 docker + socket（避免 socket 抢跑旧参数）
    systemctl stop docker docker.socket >/dev/null 2>&1 || true

    # 依赖：rsync
    if ! command -v rsync >/dev/null 2>&1; then
        echo "安装 rsync ..."
        apt-get update -y && apt-get install -y rsync
    fi

    # 同步数据（从 CURRENT_ROOT -> NEW_ROOT）
    mkdir -p "$NEW_ROOT"
    if [[ -d "$CURRENT_ROOT" && -n "$(ls -A "$CURRENT_ROOT" 2>/dev/null || true)" ]]; then
        rsync -aHAX --delete --numeric-ids "$CURRENT_ROOT"/ "$NEW_ROOT"/
        echo "✅ 数据已同步到 $NEW_ROOT"
    else
        echo "ℹ️ $CURRENT_ROOT 为空或不存在，将创建全新 Docker 根目录"
    fi

    # 备份旧目录以便回滚（备份 CURRENT_ROOT）
    local OLD_BAK=""
    if [[ -d "$CURRENT_ROOT" ]]; then
        OLD_BAK="${CURRENT_ROOT}.bak-${BACKUP_SUFFIX}"
        mv "$CURRENT_ROOT" "$OLD_BAK"
        echo "🧩 已备份旧目录到 $OLD_BAK"
    fi
    mkdir -p "$CURRENT_ROOT"  # 占位，防止某些脚本依赖路径存在

    # 目录链权限：父目录至少 755；data-root 目录 711；所有权 root:root
    chmod 755 "$(dirname "$NEW_ROOT")" 2>/dev/null || true
    chmod 711 "$NEW_ROOT" 2>/dev/null || true
    chown -R root:root "$NEW_ROOT" 2>/dev/null || true

    # 备份并写回 daemon.json（显式设置 data-root + 日志轮转）
    mkdir -p "$(dirname "$DAEMON_JSON")"
    if [[ -f "$DAEMON_JSON" ]]; then
        cp -a "$DAEMON_JSON" "${DAEMON_JSON}.bak-${BACKUP_SUFFIX}"
        echo "🧩 已备份 $DAEMON_JSON 为 ${DAEMON_JSON}.bak-${BACKUP_SUFFIX}"
    fi
    tee "$DAEMON_JSON" >/dev/null <<EOF
{
  "data-root": "$NEW_ROOT",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "3"
  }
}
EOF

    # 如果 systemd 里写死了 --data-root，则覆写为不带该参数（使用 daemon.json）
    if systemctl cat docker 2>/dev/null | grep -q -- "--data-root="; then
        mkdir -p /etc/systemd/system/docker.service.d
        tee /etc/systemd/system/docker.service.d/override.conf >/dev/null <<'OVR'
[Service]
ExecStart=
ExecStart=/usr/bin/dockerd -H fd://
OVR
        echo "🧩 已写入 systemd override，移除 --data-root 覆盖"
    fi

    systemctl daemon-reload

    # 启动 docker（不启动 socket，直接启 service）
    systemctl start docker || { echo "❌ 启动 docker 失败，请查看：journalctl -u docker --no-pager -n 200"; goto_rollback=1; }

    # 校验根目录是否生效
    local ROOT_DIR
    ROOT_DIR="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
    if [[ "$ROOT_DIR" == "$NEW_ROOT" ]]; then
        echo "✅ 迁移成功：Docker Root Dir = $ROOT_DIR"
        if [[ -n "$OLD_BAK" ]]; then
            echo "🧹 如确认正常，可删除备份释放空间：rm -rf $OLD_BAK"
        fi
        return 0
    fi

    # 未生效则回滚
    echo "❌ 迁移校验失败：当前 Docker Root Dir = ${ROOT_DIR:-未知}"
    echo "↩️ 回滚到迁移前……"

    systemctl stop docker docker.socket >/dev/null 2>&1 || true

    # 恢复 daemon.json：回滚到 CURRENT_ROOT（迁移前）
    tee "$DAEMON_JSON" >/dev/null <<EOF
{
  "data-root": "$CURRENT_ROOT",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "3"
  }
}
EOF

    # 恢复目录：移除占位，恢复备份
    rm -rf "$CURRENT_ROOT"
    if [[ -n "$OLD_BAK" && -d "$OLD_BAK" ]]; then
        mv "$OLD_BAK" "$CURRENT_ROOT"
        echo "🧩 已恢复旧目录：$CURRENT_ROOT"
    else
        echo "⚠️ 未找到旧目录备份（$OLD_BAK），请手动检查。"
    fi

    systemctl daemon-reload
    systemctl start docker >/dev/null 2>&1 || true
    echo "已回滚至迁移前状态。"
    return 1
}

# =====================
#  功能 71：优化 Docker 日志（设置轮转）
# =====================
optimize_docker_logs() {
    # 前置校验
    if [ -z "${BASH_VERSION:-}" ]; then exec /usr/bin/env bash "$0" "$@"; fi
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then echo "请以 root 权限运行（sudo bash $0）"; return 1; fi
    if ! command -v docker >/dev/null 2>&1; then
        echo "未检测到 Docker，请先安装 Docker。"
        return 1
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "未检测到 systemctl，无法重启 docker 服务。"
        return 1
    fi

    local DAEMON_JSON="/etc/docker/daemon.json"
    local BACKUP_SUFFIX; BACKUP_SUFFIX="$(date +%Y%m%d-%H%M%S)"
    local TMP="/tmp/daemon.json.$$"

    mkdir -p "$(dirname "$DAEMON_JSON")"

    # 备份
    if [[ -f "$DAEMON_JSON" ]]; then
        cp -a "$DAEMON_JSON" "${DAEMON_JSON}.bak-${BACKUP_SUFFIX}"
        echo "🧩 已备份 $DAEMON_JSON 为 ${DAEMON_JSON}.bak-${BACKUP_SUFFIX}"
    fi

    # 写入/合并配置：只保证 json-file + 轮转参数，不破坏 data-root 和其它键
    if command -v jq >/dev/null 2>&1; then
        if [[ -s "$DAEMON_JSON" ]] && jq '.' "$DAEMON_JSON" >/dev/null 2>&1; then
            # 文件存在且 JSON 正常 → 合并（保留其它键与现有 log-opts 其它字段）
            jq '
              .["log-driver"] = "json-file"
              | .["log-opts"] = (.["log-opts"] // {})
              | .["log-opts"]["max-size"] = "20m"
              | .["log-opts"]["max-file"] = "3"
            ' "$DAEMON_JSON" > "$TMP"
        else
            # 文件不存在/空/损坏 → 重写（尽力保留 data-root）
            local CURRENT_ROOT=""
            if [[ -s "$DAEMON_JSON" ]]; then
                CURRENT_ROOT="$(sed -n 's/.*"data-root"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$DAEMON_JSON" | head -n1)"
            fi
            if [[ -z "$CURRENT_ROOT" ]]; then
                CURRENT_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
            fi
            if [[ -n "$CURRENT_ROOT" ]]; then
                cat > "$TMP" <<EOF
{
  "data-root": "$CURRENT_ROOT",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "3"
  }
}
EOF
            else
                cat > "$TMP" <<EOF
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "3"
  }
}
EOF
            fi
        fi
        mv -f "$TMP" "$DAEMON_JSON"
    else
        # 没有 jq：尽力保留现有 data-root，再重写日志配置
        local CURRENT_ROOT=""
        if [[ -f "$DAEMON_JSON" ]]; then
            CURRENT_ROOT="$(sed -n 's/.*"data-root"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$DAEMON_JSON" | head -n1)"
        fi
        if [[ -z "$CURRENT_ROOT" ]]; then
            CURRENT_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
        fi

        if [[ -n "$CURRENT_ROOT" ]]; then
            cat > "$DAEMON_JSON" <<EOF
{
  "data-root": "$CURRENT_ROOT",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "3"
  }
}
EOF
        else
            cat > "$DAEMON_JSON" <<EOF
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "3"
  }
}
EOF
        fi
    fi

    # 使配置生效
    systemctl restart docker || { echo "❌ docker 重启失败，请查看：journalctl -u docker --no-pager -n 200"; return 1; }

    # 回显确认
    local ROOT_DIR LOG_DRIVER
    ROOT_DIR="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
    LOG_DRIVER="$(docker info --format '{{.LoggingDriver}}' 2>/dev/null || true)"
    echo "✅ Docker 日志轮转已启用（20m x 3），RootDir：${ROOT_DIR:-未知}，LogDriver：${LOG_DRIVER:-未知}"

    # 提示：Docker 轮转不等于 gzip 压缩（避免误判）
    local CID
    CID="$(docker ps -q 2>/dev/null | head -n1 || true)"
    if [[ -n "$CID" && -n "$ROOT_DIR" ]]; then
        echo "🔎 示例容器日志路径：$ROOT_DIR/containers/$CID/$CID-json.log（Docker 只轮转 .log/.log.1，不会自动生成 .gz）"
    fi
}

# =====================
#  功能 72：优化 systemd-journald（日记只写内存）
# =====================
optimize_journald_to_volatile() {
    # 前置校验
    if [ -z "${BASH_VERSION:-}" ]; then exec /usr/bin/env bash "$0" "$@"; fi
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then echo "请以 root 权限运行（sudo bash $0）"; return 1; fi
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "未检测到 systemctl，无法配置 journald。"
        return 1
    fi
    if ! command -v journalctl >/dev/null 2>&1; then
        echo "未检测到 journalctl，无法检查 journald 状态。"
        return 1
    fi

    local CONF="/etc/systemd/journald.conf"
    local DROPIN_DIR="/etc/systemd/journald.conf.d"
    local DROPIN_FILE="${DROPIN_DIR}/volatile.conf"
    local BACKUP_SUFFIX; BACKUP_SUFFIX="$(date +%Y%m%d-%H%M%S)"

    # 备份主配置（如果存在）
    if [[ -f "$CONF" ]]; then
        cp -a "$CONF" "${CONF}.bak-${BACKUP_SUFFIX}"
        echo "🧩 已备份 $CONF 为 ${CONF}.bak-${BACKUP_SUFFIX}"
    fi

    # 写入 drop-in（推荐：避免主配置被包更新覆盖）
    mkdir -p "$DROPIN_DIR"
    cat > "$DROPIN_FILE" <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=32M
SystemMaxUse=0
EOF
    echo "✅ 已写入 $DROPIN_FILE（Storage=volatile）"

    # 删除持久化日志目录（关键：否则 Storage=auto/persistent 会回写磁盘）
    if [[ -d /var/log/journal ]]; then
        rm -rf /var/log/journal
        echo "🧹 已删除 /var/log/journal（禁用持久化日志目录）"
    fi

    # 重启 journald 使配置生效
    systemctl restart systemd-journald || {
        echo "❌ journald 重启失败，请查看：journalctl -u systemd-journald --no-pager -n 200"
        return 1
    }

    # 回显确认：Storage 必须是 volatile；disk-usage 应显示无持久化日志
    local STORAGE DISK_USAGE
    STORAGE="$(systemctl show systemd-journald --property=Storage --value 2>/dev/null || true)"
    DISK_USAGE="$(journalctl --disk-usage 2>/dev/null || true)"

    echo "🔎 journald Storage：${STORAGE:-未知}"
    echo "🔎 journald 磁盘占用：${DISK_USAGE:-未知}"

    if [[ "$STORAGE" != "volatile" ]]; then
        echo "⚠️ 警告：Storage 不是 volatile，可能仍会写盘。请检查 $DROPIN_FILE 是否生效。"
    fi

    if echo "$DISK_USAGE" | grep -qiE 'take up|takes up'; then
        echo "⚠️ 警告：仍检测到持久化日志占用（可能其它目录残留）。可检查是否存在 /var/log/journal 并确保已删除。"
    else
        echo "✅ journald 已切换为内存日志（重启后日志会清空）。"
    fi
}

# ========== 主循环 ==========

install_dependencies
show_menu

while true; do
    read -p "请输入选项: " choice
    case $choice in
        0) show_menu ;;
        1) os_info ;;
        2) nic_info ;;
        3) disk_info ;;
        4) docker_info ;;
        5) format_disk ;;
        7) install_docker ;;
        8) create_macvlan_network ;;
        9) clean_macvlan_network ;;
        10) install_portainer ;;
        11) install_librespeed ;;
        14) install_adguardhome ;;
        19) install_mosdns ;;
        20) install_mihomo ;;
        21) install_ddnsgo ;;
        22) install_lucky ;;
        70) migrate_docker_datadir ;;
        71) optimize_docker_logs ;;
        72) optimize_journald_to_volatile ;;
        90) create_macvlan_bridge ;;
        91) clean_macvlan_bridge ;;
        97) install_watchtower ;;
        98) run_watchtower_once ;;
        99) echo "退出脚本。"; exit 0 ;;
        *) echo "无效选项，请重新输入。" ;;
    esac
done