#!/usr/bin/env bash
# Apply mosdns my-list mode and refresh cron scripts.
# List URLs stay in cron/sync.conf; this file only changes switches, log level, and schedule files.

MOSDNS_CRON_RAW_BASE="${MOSDNS_CRON_RAW_BASE:-https://raw.githubusercontent.com/perryyeh/mosdns/main/cron}"

mosdns_http_get() {
    local url="$1" dst="$2"
    if command -v yehbp_curl >/dev/null 2>&1; then
        yehbp_curl --connect-timeout 10 --max-time 60 -fsSL "$url" -o "$dst"
    elif command -v curl >/dev/null 2>&1; then
        command curl --connect-timeout 10 --max-time 60 -fsSL "$url" -o "$dst"
    else
        wget -q -T 60 -O "$dst" "$url"
    fi
}

mosdns_env_get() {
    local file="$1" key="$2" value
    [ -f "$file" ] || return 1
    value="$(awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); found=1 } END { if (!found) exit 1 }' "$file")" || return 1
    printf '%s\n' "$value"
}

mosdns_env_set() {
    local file="$1" key="$2" value="$3" tmp
    [ -f "$file" ] || return 1
    case "$value" in
        0|1|2) ;;
        *) return 1 ;;
    esac
    tmp="$(mktemp "$(dirname "$file")/.mosdns-env.XXXXXX")" || return 1
    if ! awk -v key="$key" -v value="$value" '
        BEGIN { found = 0 }
        $0 ~ "^" key "=" { print key "=" value; found = 1; next }
        { print }
        END { if (!found) print key "=" value }
    ' "$file" > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    cat "$tmp" > "$file" || { rm -f "$tmp"; return 1; }
    rm -f "$tmp"
}

mosdns_effective_my_mode() {
    local file="$1" mode candidates promote
    mode="$(mosdns_env_get "$file" MOSDNS_MY_MODE 2>/dev/null || true)"
    case "$mode" in
        0|1|2) printf '%s\n' "$mode"; return 0 ;;
        "") ;;
        *) printf '0\n'; return 0 ;;
    esac
    candidates="$(mosdns_env_get "$file" MOSDNS_SYNC_CANDIDATES_ENABLED 2>/dev/null || true)"
    promote="$(mosdns_env_get "$file" MOSDNS_PROMOTE_ENABLED 2>/dev/null || true)"
    if [ "$candidates" = "1" ] || [ "$promote" = "1" ]; then
        printf '1\n'
    else
        printf '0\n'
    fi
}

mosdns_set_log_level() {
    local file="$1" level="$2" tmp
    [ -f "$file" ] || return 1
    case "$level" in
        info|warn) ;;
        *) return 1 ;;
    esac
    tmp="$(mktemp "$(dirname "$file")/.mosdns-log.XXXXXX")" || return 1
    if ! awk -v level="$level" '
        BEGIN { done = 0 }
        !done && /^[[:space:]]*level:[[:space:]]*/ {
            print "  level: " level
            done = 1
            next
        }
        { print }
        END { if (!done) exit 1 }
    ' "$file" > "$tmp"; then
        rm -f "$tmp"
        echo "❌ 未找到 mosdns 日志级别，取消修改" >&2
        return 1
    fi
    cat "$tmp" > "$file" || { rm -f "$tmp"; return 1; }
    rm -f "$tmp"
}

mosdns_apply_rules_enabled() {
    local dir="$1" enabled="$2"
    mosdns_env_set "$dir/cron/cron.env" MOSDNS_SYNC_RULES_ENABLED "$enabled"
}

mosdns_apply_my_mode() {
    local dir="$1" mode="$2" candidates=0 promote=0 level=warn
    case "$mode" in
        0) ;;
        1) candidates=1; promote=1; level=info ;;
        2) ;;
        *) echo "❌ 无效的私人规则模式：$mode" >&2; return 1 ;;
    esac
    mosdns_env_set "$dir/cron/cron.env" MOSDNS_MY_MODE "$mode" || return 1
    mosdns_env_set "$dir/cron/cron.env" MOSDNS_SYNC_CANDIDATES_ENABLED "$candidates" || return 1
    mosdns_env_set "$dir/cron/cron.env" MOSDNS_PROMOTE_ENABLED "$promote" || return 1
    mosdns_set_log_level "$dir/config.yaml" "$level" || return 1
}

mosdns_show_private_rule_modes() {
    echo "私人规则处理方式："
    echo "  0）关闭"
    echo "  1）自动统计与合并"
    echo "  2）每日同步私人规则"
    echo "提示：自动统计与合并、私人规则同步只能选择一种，避免同步覆盖本机合并的规则。"
}

mosdns_prompt_feature_switches() {
    local dir="$1" rules_answer mode_answer rules_value=0
    [ -f "$dir/cron/cron.env" ] && [ -f "$dir/config.yaml" ] || {
        echo "❌ 未找到 $dir/cron/cron.env 或 config.yaml"
        return 1
    }
    read -r -p "是否每日自动更新公共规则？[y/N]: " rules_answer || return 1
    mosdns_show_private_rule_modes
    read -r -p "请选择 [0/1/2]（回车默认 0）: " mode_answer || return 1
    mode_answer="${mode_answer:-0}"
    case "$mode_answer" in
        0|1|2) ;;
        *) echo "❌ 无效的私人规则模式：$mode_answer" >&2; return 1 ;;
    esac
    case "$rules_answer" in
        [Yy]*) rules_value=1 ;;
        *) rules_value=0 ;;
    esac
    mosdns_apply_rules_enabled "$dir" "$rules_value" || return 1
    mosdns_apply_my_mode "$dir" "$mode_answer" || return 1
    if [ "$rules_value" = "1" ]; then
        echo "✅ 已开启公共规则每日自动更新"
    else
        echo "✅ 已关闭公共规则每日自动更新"
    fi
    case "$mode_answer" in
        0) echo "✅ 已关闭私人规则自动处理" ;;
        1) echo "✅ 已开启私人规则自动统计与合并" ;;
        2) echo "✅ 已开启私人规则每日同步，并关闭自动统计与合并" ;;
    esac
}

mosdns_merge_sync_conf() {
    local dest="$1" upstream="$2" tmp line target found="" required
    local -a required_names=(my/whitelist.txt my/greylist.txt my/blacklist.txt my/hosts.txt)
    [ -f "$dest" ] && [ -f "$upstream" ] || return 1
    tmp="$(mktemp "$(dirname "$dest")/.sync-conf.XXXXXX")" || return 1
    cp "$dest" "$tmp" || { rm -f "$tmp"; return 1; }
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ""|\#*) continue ;;
        esac
        target="$(printf '%s\n' "$line" | awk -F'|' 'NR==1 { gsub(/^[ \t]+|[ \t]+$/, "", $1); print $1; exit }')"
        case "$target" in
            my/whitelist.txt|my/greylist.txt|my/blacklist.txt|my/hosts.txt) ;;
            my/*)
                echo "❌ sync.conf 含未允许的 my 目标：$target" >&2
                rm -f "$tmp"
                return 1
                ;;
            *) continue ;;
        esac
        found="$found $target"
        if grep -Eq "^[[:space:]]*${target}[[:space:]]*\\|" "$tmp"; then
            continue
        fi
        printf '%s\n' "$line" >> "$tmp"
    done < "$upstream"
    for required in "${required_names[@]}"; do
        case " $found " in
            *" $required "*) ;;
            *)
                echo "❌ 上游 sync.conf 缺少 $required" >&2
                rm -f "$tmp"
                return 1
                ;;
        esac
    done
    if cmp -s "$dest" "$tmp"; then
        rm -f "$tmp"
        return 0
    fi
    cat "$tmp" > "$dest" || { rm -f "$tmp"; return 1; }
    rm -f "$tmp"
}

mosdns_update_cron_scripts() {
    local dir="$1" stage file merged base name
    local -a files=(fetch.sh my-mode.sh sync-target.sh sync-rules.sh sync-my.sh sync-candidates.sh promote-candidates.sh reload-if-needed.sh crontab.conf)
    [ -d "$dir/cron" ] && [ -f "$dir/cron/sync.conf" ] || return 1
    stage="$(mktemp -d "$(dirname "$dir")/.mosdns-cron.XXXXXX")" || return 1
    base="${MOSDNS_CRON_RAW_BASE%/}"
    for file in "${files[@]}" sync.conf; do
        if ! mosdns_http_get "$base/$file" "$stage/$file"; then
            echo "❌ 下载 cron 文件失败：$file" >&2
            rm -rf "$stage"
            return 1
        fi
    done
    for file in fetch.sh my-mode.sh sync-target.sh sync-rules.sh sync-my.sh sync-candidates.sh promote-candidates.sh reload-if-needed.sh; do
        if ! sh -n "$stage/$file"; then
            echo "❌ cron 脚本语法检查失败：$file" >&2
            rm -rf "$stage"
            return 1
        fi
    done
    if ! grep -Fq 'sync-my.sh' "$stage/crontab.conf" \
        || ! grep -Fq 'reload-if-needed.sh rules' "$stage/crontab.conf" \
        || ! grep -Fq 'reload-if-needed.sh my' "$stage/crontab.conf" \
        || ! grep -Fq '0 1 * * *' "$stage/crontab.conf" \
        || ! grep -Fq '10 1 * * *' "$stage/crontab.conf"; then
        echo "❌ 下载的 crontab 缺少私人规则同步计划" >&2
        rm -rf "$stage"
        return 1
    fi
    cp "$dir/cron/sync.conf" "$stage/local-sync.conf" || { rm -rf "$stage"; return 1; }
    if ! mosdns_merge_sync_conf "$stage/local-sync.conf" "$stage/sync.conf"; then
        rm -rf "$stage"
        return 1
    fi
    for file in "${files[@]}"; do
        cat "$stage/$file" > "$dir/cron/$file" || { rm -rf "$stage"; return 1; }
        case "$file" in
            *.sh) chmod 755 "$dir/cron/$file" ;;
            *) chmod 644 "$dir/cron/$file" ;;
        esac
    done
    cat "$stage/local-sync.conf" > "$dir/cron/sync.conf" || { rm -rf "$stage"; return 1; }
    rm -rf "$stage"
    echo "✅ 已更新 mosdns cron 脚本和计划"
}

mosdns_discover_instances() {
    local id name dir image seen=""
    docker ps -a --format '{{.ID}} {{.Names}}' | while read -r id name; do
        [ -n "$id" ] || continue
        dir="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$id" 2>/dev/null || true)"
        image="$(docker inspect -f '{{.Config.Image}}' "$id" 2>/dev/null || true)"
        [ -n "$dir" ] && [ "$dir" != "<no value>" ] || continue
        [ -f "$dir/cron/cron.env" ] && [ -f "$dir/config.yaml" ] && [ -f "$dir/compose.yaml" ] || continue
        if ! printf '%s\n' "$image" | grep -qi mosdns; then
            grep -qi mosdns "$dir/compose.yaml" || continue
        fi
        case " $seen " in
            *" $dir "*) continue ;;
        esac
        seen="$seen $dir"
        printf '%s\t%s\n' "$dir" "$name"
    done
}

mosdns_load_crontab() {
    local name="$1"
    if ! docker exec "$name" crontab /etc/mosdns/cron/crontab.conf; then
        echo "❌ 未能加载容器 crontab。未重启容器；容器下次由 start.sh 启动时会再加载。" >&2
        return 1
    fi
    echo "✅ 已加载容器 crontab，未重启容器。"
}

mosdns_configure_existing() {
    local list choice dir name mode rules_answer rules_value current_mode current_rules
    local -a dirs=() names=()
    list="$(mosdns_discover_instances)" || return 1
    if [ -z "$list" ]; then
        echo "ℹ️ 未发现带 cron.env 的 mosdns 实例。"
        return 0
    fi
    while IFS=$'\t' read -r dir name; do
        [ -n "$dir" ] || continue
        dirs+=("$dir")
        names+=("$name")
        printf '%d）%s\n' "${#dirs[@]}" "$name"
    done <<< "$list"
    echo "0）返回"
    read -r -p "请选择 mosdns 实例: " choice || return 1
    [ -n "$choice" ] && [ "$choice" != "0" ] || return 0
    if [[ ! "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#dirs[@]}" ]; then
        echo "❌ 无效选择。" >&2
        return 1
    fi
    dir="${dirs[$((choice - 1))]}"
    name="${names[$((choice - 1))]}"
    current_mode="$(mosdns_effective_my_mode "$dir/cron/cron.env")"
    current_rules="$(mosdns_env_get "$dir/cron/cron.env" MOSDNS_SYNC_RULES_ENABLED 2>/dev/null || echo 0)"
    echo "当前私人规则模式：${current_mode}"
    mosdns_show_private_rule_modes
    echo "当前公共规则同步：${current_rules}"
    read -r -p "请选择私人规则处理方式 [0/1/2]（回车保持当前）: " mode || return 1
    mode="${mode:-$current_mode}"
    case "$mode" in
        0|1|2) ;;
        *) echo "❌ 无效的私人规则模式：$mode" >&2; return 1 ;;
    esac
    read -r -p "是否每日同步公共规则？[y/n]（回车保持当前）: " rules_answer || return 1
    if [ -z "$rules_answer" ]; then
        rules_value="$current_rules"
    elif [[ "$rules_answer" =~ ^[Yy]$ ]]; then
        rules_value=1
    elif [[ "$rules_answer" =~ ^[Nn]$ ]]; then
        rules_value=0
    else
        echo "❌ 请输入 y 或 n。" >&2
        return 1
    fi
    case "$rules_value" in
        0|1) ;;
        *) echo "❌ 当前公共规则开关无法识别，请明确输入 y 或 n。" >&2; return 1 ;;
    esac
    mosdns_update_cron_scripts "$dir" || return 1
    mosdns_apply_my_mode "$dir" "$mode" || return 1
    mosdns_apply_rules_enabled "$dir" "$rules_value" || return 1
    mosdns_load_crontab "$name" || return 1
    echo "✅ 已更新名单计划。需要让新进程入口生效时再重启一次容器。"
}
