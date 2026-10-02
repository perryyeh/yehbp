#!/usr/bin/env bash
# Optional installer renderer: preserve ali/tencent definitions and existing rules.

mosdns_validate_isp_ipv4() {
    local ip="$1" part
    local -a parts=()
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    IFS=. read -r -a parts <<< "$ip"
    for part in "${parts[@]}"; do
        [[ "$part" = 0 || "$part" != 0* ]] || return 1
        [ "${#part}" -le 3 ] && [ "$part" -le 255 ] || return 1
    done
    [ "$ip" != 0.0.0.0 ] && [ "$ip" != 255.255.255.255 ]
}

mosdns_add_isp_upstream() {
    local dir="$1" ip tmp_dns tmp_config status=0
    shift
    [ "$#" -gt 0 ] || return 1
    for ip in "$@"; do
        mosdns_validate_isp_ipv4 "$ip" || { echo "❌ 无效运营商 IPv4 DNS：$ip" >&2; return 1; }
    done
    [ -f "$dir/dns.yaml" ] && [ -f "$dir/config.yaml" ] || return 1
    # Refuse duplicate/reserved tags; do not overwrite a previously rendered config.
    if ! awk '
        /^  - tag: (isp|forward_public_direct_group)([[:space:]]|$)/ { bad=1 }
        END { exit bad ? 1 : 0 }
    ' "$dir/dns.yaml" "$dir/config.yaml"; then
        echo "❌ 运营商上游标签已存在，取消修改" >&2
        return 1
    fi
    tmp_dns="$(mktemp "$dir/.isp-dns.XXXXXX")" || return 1
    tmp_config="$(mktemp "$dir/.isp-config.XXXXXX")" || { rm -f "$tmp_dns"; return 1; }
    if ! awk '
        function wrapper() {
            print ""
            print "  # 运营商优先，失败或超时回退原阿里/腾讯组合"
            print "  - tag: forward_direct_group"
            print "    type: fallback"
            print "    args:"
            print "      primary: isp"
            print "      secondary: forward_public_direct_group"
            print "      threshold: 100"
            print "      always_standby: false"
            print ""
        }
        /^  - tag:/ && pending { wrapper(); pending=0 }
        /^  - tag: forward_direct_group([[:space:]]|$)/ {
            if (found++) exit 1
            sub(/tag: forward_direct_group/, "tag: forward_public_direct_group")
            pending=1
        }
        { print }
        END {
            if (found != 1) exit 1
            if (pending) wrapper()
        }
    ' "$dir/config.yaml" > "$tmp_config"; then
        rm -f "$tmp_dns" "$tmp_config"
        echo "❌ 未找到唯一的国内转发组，取消修改" >&2
        return 1
    fi
    cat "$dir/dns.yaml" > "$tmp_dns" || status=1
    {
        printf '\n  # 运营商 DNS（普通 UDP）\n  - tag: isp\n    type: forward\n    args:\n      concurrent: 2\n      upstreams:\n'
        for ip in "$@"; do
            printf '        - addr: "udp://%s:53"\n' "$ip"
        done
    } >> "$tmp_dns" || status=1
    if [ "$status" -eq 0 ]; then
        cat "$tmp_dns" > "$dir/dns.yaml" && cat "$tmp_config" > "$dir/config.yaml" || status=1
    fi
    rm -f "$tmp_dns" "$tmp_config"
    return "$status"
}
