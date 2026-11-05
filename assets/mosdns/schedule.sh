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
    local current="$1" mode label mark
    echo "私人规则处理方式："
    for mode in 0 1 2; do
        case "$mode" in
            0) label="不处理" ;;
            1) label="根据本机日志自动统计与合并" ;;
            2) label="每日从 YehBP 更新私人规则" ;;
        esac
        mark=""
        [ "$mode" != "$current" ] || mark="【当前选择】"
        printf '  %s）%s%s\n' "$mode" "$label" "$mark"
    done
    echo "提示：本机统计合并与每日更新只能选择一种。"
}

mosdns_read_rule_choices() {
    local dir="$1" rules_answer mode_answer current_rules current_mode
    [ -f "$dir/cron/cron.env" ] && [ -f "$dir/config.yaml" ] || {
        echo "❌ 未找到 $dir/cron/cron.env 或 config.yaml"
        return 1
    }
    current_rules="$(mosdns_env_get "$dir/cron/cron.env" MOSDNS_SYNC_RULES_ENABLED 2>/dev/null || echo 0)"
    current_mode="$(mosdns_effective_my_mode "$dir/cron/cron.env")"
    echo "公共规则处理方式："
    local option label mark
    for option in 0 1; do
        case "$option" in
            0) label="不处理" ;;
            1) label="每日同步 v2ray-rules 公共规则" ;;
        esac
        mark=""
        [ "$option" != "$current_rules" ] || mark="【当前选择】"
        printf '  %s）%s%s\n' "$option" "$label" "$mark"
    done
    read -r -p "请选择 [0/1]（回车保持当前选择）: " rules_answer || return 1
    rules_answer="${rules_answer:-$current_rules}"
    case "$rules_answer" in
        0|1) ;;
        *) echo "❌ 请输入 0 或 1。" >&2; return 1 ;;
    esac
    mosdns_show_private_rule_modes "$current_mode"
    read -r -p "请选择 [0/1/2]（回车保持当前选择）: " mode_answer || return 1
    mode_answer="${mode_answer:-$current_mode}"
    case "$mode_answer" in
        0|1|2) ;;
        *) echo "❌ 无效的私人规则模式：$mode_answer" >&2; return 1 ;;
    esac
    MOSDNS_RULES_CHOICE="$rules_answer"
    MOSDNS_MY_CHOICE="$mode_answer"
}

mosdns_apply_rule_choices() {
    local dir="$1"
    mosdns_apply_rules_enabled "$dir" "$MOSDNS_RULES_CHOICE" || return 1
    mosdns_apply_my_mode "$dir" "$MOSDNS_MY_CHOICE" || return 1
    echo "✅ 已设置公共规则同步：${MOSDNS_RULES_CHOICE}，私人规则模式：${MOSDNS_MY_CHOICE}"
}

mosdns_configure_instance() {
    local dir="$1" name="$2"
    mosdns_read_rule_choices "$dir" || return 1
    mosdns_update_cron_scripts "$dir" || return 1
    mosdns_apply_rule_choices "$dir" || return 1
    mosdns_load_crontab "$name" || return 1
    echo "✅ 已更新规则计划。需要让新日志级别生效时再重启一次容器。"
}

mosdns_offer_rule_configuration() {
    local dir="$1" name="$2" choice
    echo "是否现在配置规则更新？"
    echo "  0）稍后配置"
    echo "  1）现在配置"
    read -r -p "请选择 [0/1]（回车默认 0）: " choice || return 1
    case "${choice:-0}" in
        0) echo "ℹ️ 已跳过，可稍后通过 19 → 2 设置规则更新。" ;;
        1)
            mosdns_configure_instance "$dir" "$name" || return 1
            # 新安装默认 warn；选择每日合并时需重新启动，加载 info 日志。
            if [ "$MOSDNS_MY_CHOICE" = "1" ]; then
                docker restart "$name" || return 1
                echo "✅ 已重启 mosdns，规则设置已应用。"
            fi
            ;;
        *) echo "❌ 请输入 0 或 1。" >&2; return 1 ;;
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
    local list choice dir name
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
    mosdns_configure_instance "$dir" "$name"
}
