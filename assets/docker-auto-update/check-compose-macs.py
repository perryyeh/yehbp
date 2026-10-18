#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ROOT_DIR", "/vol2/1000/dockerapps"))
PREFIX = "com.docker.compose."


class CheckError(Exception):
    pass


def run(cmd, cwd=None):
    # Keep warnings with stdout: config with interpolation warnings must not
    # silently pass as a complete configuration. Never print this output on error.
    return subprocess.check_output(cmd, cwd=cwd, text=True, stderr=subprocess.STDOUT)


def labeled_files(value, workdir=None):
    # Compose writes strings.Join(paths, ","), not CSV (no quoting/escaping).
    # Commas inside filenames cannot be recovered safely from this label.
    parts = value.split(",")
    files = []
    for start in range(len(parts)):
        for end in range(start + 2, len(parts) + 1):
            candidate = Path(",".join(parts[start:end]))
            if workdir is not None and not candidate.is_absolute():
                candidate = workdir / candidate
            if candidate.is_file():
                raise CheckError("ambiguous comma in file label; coverage incomplete")
    for part in parts:
        path = Path(part)
        if workdir is not None and not path.is_absolute():
            path = workdir / path
        if not part or not path.is_absolute() or not path.is_file():
            raise CheckError("missing/unreadable or ambiguous comma-separated file label")
        files.append(str(path))
    return tuple(files)


def compose_config(project, workdir, files, env_files):
    cmd = ["docker", "compose", "-p", project, "--project-directory", workdir]
    for file in files:
        cmd.extend(["-f", file])
    for file in env_files:
        cmd.extend(["--env-file", file])
    # Include profiled services without pretending to recover the launch profile.
    cmd.extend(["--profile", "*", "config", "--format", "json"])
    try:
        cfg = json.loads(run(cmd, cwd=workdir))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise CheckError("config failed (command, JSON or interpolation warning); verify files and required environment") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("services"), dict):
        raise CheckError("config failed: invalid services object")
    if not isinstance(cfg.get("networks", {}), dict):
        raise CheckError("config failed: invalid networks object")
    return cfg


def inspect_containers():
    ids = run(["docker", "ps", "-q"]).split()
    if not ids:
        return []
    containers = json.loads(run(["docker", "inspect"] + ids))
    if not isinstance(containers, list) or not all(isinstance(c, dict) for c in containers):
        raise CheckError("invalid container inspection")
    return containers


def normalize_mac(m):
    if not isinstance(m, str):
        raise CheckError("invalid MAC value in configuration/inspection")
    return m.strip().lower()


def in_root(path):
    return Path(path).is_absolute() and (Path(path).resolve() == ROOT.resolve() or ROOT.resolve() in Path(path).resolve().parents)


def check_service(cfg, project, svc, c):
    svc_cfg = cfg["services"].get(svc)
    if not isinstance(svc_cfg, dict):
        raise CheckError("running service absent from config; coverage incomplete")
    svc_nets = svc_cfg.get("networks")
    if svc_nets is None:
        svc_nets = {} if svc_cfg.get("network_mode") else {"default": {}}
    if not isinstance(svc_nets, dict):
        raise CheckError("invalid service networks object; coverage incomplete")
    service_mac = normalize_mac(svc_cfg.get("mac_address") or "")
    # Compose applies the service MAC to NetworksByPriority()[0]: highest
    # priority first, with network-key order breaking ties (compose-go).
    main_net = None
    if service_mac:
        if not svc_nets:
            raise CheckError("service mac_address without network; coverage incomplete")
        priorities = {}
        for key, opts in svc_nets.items():
            if opts is not None and not isinstance(opts, dict):
                raise CheckError("invalid network options; coverage incomplete")
            priority = (opts or {}).get("priority", 0)
            if not isinstance(priority, int) or isinstance(priority, bool):
                raise CheckError("invalid network priority; coverage incomplete")
            priorities[key] = priority
        main_net = min(priorities, key=lambda key: (-priorities[key], key))
        endpoint_mac = normalize_mac((svc_nets[main_net] or {}).get("mac_address") or "")
        if endpoint_mac and endpoint_mac != service_mac:
            raise CheckError("conflicting service and main-network MAC; coverage incomplete")
    checked, mismatches = 0, []
    networks_cfg = cfg.get("networks", {})
    for net_key, net_opts in svc_nets.items():
        if net_opts is None:
            net_opts = {}
        if not isinstance(net_opts, dict):
            raise CheckError("invalid network options; coverage incomplete")
        expected = normalize_mac(net_opts.get("mac_address") or (service_mac if net_key == main_net else ""))
        if not expected:
            continue
        net_cfg = networks_cfg.get(net_key, {})
        if not isinstance(net_cfg, dict):
            raise CheckError("invalid network definition; coverage incomplete")
        actual_net_name = net_cfg.get("name") or (net_key if net_cfg.get("external") else f"{project}_{net_key}")
        actual = normalize_mac(
            ((c.get("NetworkSettings", {}).get("Networks") or {}).get(actual_net_name) or {}).get("MacAddress") or ""
        )
        cname = c.get("Name", "").lstrip("/")
        checked += 1
        if expected != actual:
            mismatches.append((cname, actual_net_name, expected, actual or "<missing>"))
    return checked, mismatches


def main():
    try:
        containers = inspect_containers()
    except (CheckError, OSError, subprocess.SubprocessError, ValueError):
        print("MAC-CHECK: ERROR: cannot inspect running containers", file=sys.stderr)
        return 1
    projects = {}
    failures = 0
    for c in containers:
        labels = c.get("Config", {}).get("Labels") or {}
        if not any(key.startswith(PREFIX) for key in labels):
            continue
        # `compose run` can override service settings and is not a deployed
        # service replica; its labels do not describe its effective config.
        if str(labels.get(PREFIX + "oneoff", "")).lower() == "true":
            continue
        try:
            project = labels.get(PREFIX + "project")
            svc = labels.get(PREFIX + "service")
            workdir = labels.get(PREFIX + "project.working_dir")
            raw_files = labels.get(PREFIX + "project.config_files")
            # Out-of-scope projects are excluded using path components, not a
            # string prefix. Missing paths cannot establish safe exclusion.
            paths = ([workdir] if workdir else []) + (raw_files.split(",") if raw_files else [])
            if paths and all(Path(p).is_absolute() for p in paths) and not any(in_root(p) for p in paths):
                continue
            if not all(isinstance(v, str) and v for v in (project, svc, workdir, raw_files)):
                raise CheckError("missing Compose project/service/working_dir/config_files labels")
            if not Path(workdir).is_absolute() or not Path(workdir).is_dir():
                raise CheckError("project working directory unavailable")
            files = labeled_files(raw_files)
            raw_env = labels.get(PREFIX + "project.environment_file")
            # Standard Compose CLI records absolute env-file paths. A relative
            # label cannot recover the original launch cwd (working_dir may
            # instead come from --project-directory).
            env_files = labeled_files(raw_env) if raw_env else ()
            key = (project, workdir, files, env_files)
            projects.setdefault(key, []).append((svc, c))
        except (CheckError, OSError, ValueError) as e:
            failures += 1
            print(f"MAC-CHECK: ERROR: {e}", file=sys.stderr)

    mismatches = []
    checked = 0
    for (project, workdir, files, env_files), items in projects.items():
        try:
            cfg = compose_config(project, workdir, files, env_files)
            for svc, c in items:
                count, missing = check_service(cfg, project, svc, c)
                checked += count
                mismatches.extend(missing)
        except (CheckError, OSError, ValueError) as e:
            failures += 1
            print(f"MAC-CHECK: ERROR project={project!r}: {e}", file=sys.stderr)

    if failures:
        print(f"MAC-CHECK: FAILED ({failures} project/context errors; coverage incomplete)")
        return 1
    if mismatches:
        print("MAC-CHECK: MISMATCH")
        for cname, net, expected, actual in mismatches:
            print(f"  {cname} {net}: expected={expected} actual={actual}")
        return 2
    if checked == 0:
        print("MAC-CHECK: no compose mac_address entries found")
        return 0
    print(f"MAC-CHECK: OK ({checked} endpoints checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
