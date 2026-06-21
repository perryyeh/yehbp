#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ROOT_DIR", "/vol2/1000/dockerapps"))


def run(cmd, cwd=None):
    return subprocess.check_output(cmd, cwd=cwd, text=True, stderr=subprocess.STDOUT)


def compose_config(compose_file: Path):
    try:
        out = run(["docker", "compose", "config", "--format", "json"], cwd=str(compose_file.parent))
        return json.loads(out)
    except Exception as e:
        print(f"WARN config failed {compose_file}: {e}", file=sys.stderr)
        return None


def inspect_containers():
    ids = run(["docker", "ps", "-q"]).split()
    if not ids:
        return []
    return json.loads(run(["docker", "inspect"] + ids))


def normalize_mac(m):
    return (m or "").strip().lower()


def main():
    containers = inspect_containers()
    by_file = {}
    for c in containers:
        labels = c.get("Config", {}).get("Labels") or {}
        cf = labels.get("com.docker.compose.project.config_files")
        svc = labels.get("com.docker.compose.service")
        if cf and svc:
            first = Path(cf.split(",")[0])
            by_file.setdefault(first, []).append((svc, c))

    mismatches = []
    checked = 0
    for cf, items in sorted(by_file.items(), key=lambda x: str(x[0])):
        if not str(cf).startswith(str(ROOT)):
            continue
        cfg = compose_config(cf)
        if not cfg:
            continue
        networks_cfg = cfg.get("networks", {}) or {}
        services_cfg = cfg.get("services", {}) or {}
        for svc, c in items:
            svc_cfg = services_cfg.get(svc, {}) or {}
            svc_nets = svc_cfg.get("networks", {}) or {}
            if isinstance(svc_nets, list):
                continue
            for net_key, net_opts in svc_nets.items():
                if not isinstance(net_opts, dict):
                    continue
                expected = normalize_mac(net_opts.get("mac_address"))
                if not expected:
                    continue
                actual_net_name = (networks_cfg.get(net_key, {}) or {}).get("name") or net_key
                actual = normalize_mac(
                    ((c.get("NetworkSettings", {}).get("Networks") or {}).get(actual_net_name) or {}).get("MacAddress")
                )
                cname = c.get("Name", "").lstrip("/")
                checked += 1
                if expected != actual:
                    mismatches.append((cname, actual_net_name, expected, actual or "<missing>"))

    if checked == 0:
        print("MAC-CHECK: no compose mac_address entries found")
        return 0
    if mismatches:
        print("MAC-CHECK: MISMATCH")
        for cname, net, expected, actual in mismatches:
            print(f"  {cname} {net}: expected={expected} actual={actual}")
        return 2
    print(f"MAC-CHECK: OK ({checked} endpoints checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
