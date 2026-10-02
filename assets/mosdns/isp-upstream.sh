#!/usr/bin/env bash
# Render template ISP placeholder and switch only the domestic entry.

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
    local dir="$1" ip tmp_dns tmp_config
    shift
    [ "$#" -gt 0 ] || return 1
    for ip in "$@"; do
        mosdns_validate_isp_ipv4 "$ip" && [ "$ip" != 192.0.2.1 ] || {
            echo "❌ 无效或未替换的运营商 IPv4 DNS：$ip" >&2; return 1;
        }
    done
    [ -f "$dir/dns.yaml" ] && [ -f "$dir/config.yaml" ] || return 1
    tmp_dns="$(mktemp "$dir/.isp-dns.XXXXXX")" || return 1
    tmp_config="$(mktemp "$dir/.isp-config.XXXXXX")" || { rm -f "$tmp_dns"; return 1; }
    # Only replace the placeholder inside isp; never rewrite other upstreams.
    if ! awk -v ips="$*" '
        /^  - tag:/ { in_isp=($3 == "isp") }
        in_isp && $0 == "        - addr: \"udp://192.0.2.1:53\"" {
            found++; n=split(ips, a, " ");
            for (i=1; i<=n; i++) print "        - addr: \"udp://" a[i] ":53\"";
            next
        }
        { print }
        END { if (found != 1) exit 1 }
    ' "$dir/dns.yaml" > "$tmp_dns" || ! awk '
        /^  - tag:/ { direct=($3 == "forward_direct_group") }
        direct && $0 == "      - exec: $forward_public_direct_group" {
            found++; sub(/forward_public_direct_group/, "forward_isp_direct_group")
        }
        { print }
        END { if (found != 1) exit 1 }
    ' "$dir/config.yaml" > "$tmp_config"; then
        rm -f "$tmp_dns" "$tmp_config"
        echo "❌ 模板占位或国内入口缺失/重复，取消修改" >&2
        return 1
    fi
    local status=0
    cat "$tmp_dns" > "$dir/dns.yaml" && cat "$tmp_config" > "$dir/config.yaml" || status=1
    rm -f "$tmp_dns" "$tmp_config"
    return "$status"
}
