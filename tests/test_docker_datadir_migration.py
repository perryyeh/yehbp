"""Run the real migration function against sandbox files and fake services."""
import json
import os
import shlex
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
SCRATCH = os.environ.get("TMPDIR")


def function(name):
    start = SOURCE.index(name + "() {")
    return SOURCE[start:SOURCE.index("\n}\n\n", start) + 2]


class DockerMigrationTest(unittest.TestCase):
    def run_case(self, failure="", active=True, config=True, override=True, socket_active=None,
                 symlink_config=False, symlink_override=False, layout="", metadata=False,
                 config_text=None, exec_start=None, docker_host=None, docker_context=None):
        with tempfile.TemporaryDirectory(prefix="yehbp-migration-", dir=SCRATCH) as temp:
            root = Path(temp).resolve()
            old, new = root / "old", root / "new"
            old.mkdir()
            new.mkdir()
            if layout == "ancestor":
                old.rmdir()
                old = new / "old"
                old.mkdir()
            elif layout == "descendant":
                new.rmdir()
                new = old / "new"
                new.mkdir()
            (old / "unique-data").write_text("original-data")
            target = str(new)
            source = str(old)
            if layout in ("nonempty", "hidden", "newline_file"):
                name = {"nonempty": "unrelated", "hidden": ".hidden", "newline_file": "\n"}[layout]
                (new / name).write_text("keep-me")
            elif layout == "missing_target":
                target = str(root / "missing")
            elif layout == "missing_source":
                source = str(root / "missing")
            elif layout == "root_target":
                target = "/"
            elif layout == "root_source":
                source = "/"
            elif layout == "same":
                target = str(old)
            elif layout == "dot_alias":
                target = str(old / ".." / old.name) + "///"
            elif layout == "symlink_alias":
                alias = root / "alias"
                alias.symlink_to(old, target_is_directory=True)
                target = str(alias)
            elif layout == "symlink_descendant":
                child = old / "child"
                child.mkdir()
                alias = root / "alias"
                alias.symlink_to(old, target_is_directory=True)
                target = str(alias / "child")
            elif layout == "source_alias":
                alias = root / "alias"
                alias.symlink_to(old, target_is_directory=True)
                source = str(alias)
                target = str(old)
            elif layout == "source_trailing_newline":
                old.rename(root / "old\n")
                old = root / "old\n"
                source = str(old)
                # A distinct existing directory must never be mistaken for the source.
                (root / "old").mkdir()
                (root / "old/unrelated").write_text("keep-me")
            elif layout == "target_symlink":
                alias = root / "alias"
                alias.symlink_to(new, target_is_directory=True)
                target = str(alias)
            elif layout == "spaces":
                new.rename(root / "new space")
                new = root / "new space"
                target = str(new / ".." / new.name) + "///"
            if layout == "special":
                new.rename(root / 'new "quote" \\ tab\t雪')
                new = root / 'new "quote" \\ tab\t雪'
                target = str(new)
            def snapshot(path):
                info = path.lstat()
                return {"uid": info.st_uid, "gid": info.st_gid,
                        "mode": stat.S_IMODE(info.st_mode),
                        "link": os.readlink(path) if path.is_symlink() else None}

            before = {}
            if metadata:
                # Real, unprivileged fixtures: another supplementary GID is safe
                # only inside this disposable sandbox. No simulated UID claim.
                root.chmod(0o750)
                old.chmod(0o710)
                data = old / "unique-data"
                data.chmod(0o640)
                directory = old / "private"
                directory.mkdir(mode=0o700)
                hardlink = old / "hardlink"
                os.link(data, hardlink)
                external = root / "external"
                external.write_text("outside-data")
                external.chmod(0o600)
                (old / "external-link").symlink_to(external)
                alternative_gid = next((gid for gid in os.getgroups()
                                        if gid != data.stat().st_gid), None)
                if alternative_gid is not None:
                    os.chown(data, -1, alternative_gid)
                for name in (".", "unique-data", "private", "hardlink", "external-link"):
                    before[name] = snapshot(old / name)
                before["parent"] = snapshot(root)
                before["external"] = snapshot(external)
            daemon = root / "etc/docker/daemon.json"
            unit = root / "etc/systemd/system/docker.service.d/override.conf"
            if failure == "fallback":
                config_text = json.dumps({"data-root": str(old), "custom": True})
            for path, exists, content in [(daemon, config, config_text if config_text is not None else '{"custom":true}\n'),
                                          (unit, override, "# original override\n")]:
                path.parent.mkdir(parents=True, exist_ok=True)
                if exists:
                    path.write_text(content)
                    path.chmod(0o600)
            original_daemon_stat = daemon.stat() if config else None
            for path, linked in ((daemon, symlink_config), (unit, symlink_override)):
                if linked:
                    config_target = path.with_suffix(".original")
                    path.rename(config_target)
                    path.symlink_to(config_target)
            probe = socket.socket(socket.AF_UNIX)
            sock = root / "docker.sock"
            try:
                probe.bind(str(sock))
            finally:
                probe.close()
            code = "\n".join(function(name) for name in (
                "docker_assert_local_daemon",
                "docker_classify_storage",
                "docker_migration_guard",
                "migrate_docker_datadir",
            ))
            code = code.replace("/etc/docker/daemon.json", str(daemon))
            code = code.replace("/etc/systemd/system/docker.service.d", str(unit.parent))
            code = code.replace('${EUID:-$(id -u)}', '0')
            env = dict(os.environ, OLD=source, NEW=str(new), CASE=failure,
                       LOG=str(root / "calls"), ACTIVE=str(int(active)),
                       SOCKET_ACTIVE=str(int(active if socket_active is None else socket_active)),
                       METADATA=str(int(metadata)),
                       EXEC_START=exec_start if exec_start is not None else
                       "{ path=/usr/bin/dockerd ; argv[]=/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")
            for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONFIG"):
                env.pop(key, None)
            env["SOCK"] = str(sock)
            if docker_host == "SANDBOX":
                env["TEST_DOCKER_HOST"] = "unix://" + str(sock)
            elif docker_host is not None:
                env["TEST_DOCKER_HOST"] = docker_host
            if docker_context is not None:
                env["TEST_DOCKER_CONTEXT"] = docker_context
            setup = r'''
PATH=/usr/bin:/bin
unset DOCKER_HOST DOCKER_CONTEXT
if [ -n "${TEST_DOCKER_HOST+x}" ]; then export DOCKER_HOST="$TEST_DOCKER_HOST"; fi
if [ -n "${TEST_DOCKER_CONTEXT+x}" ]; then export DOCKER_CONTEXT="$TEST_DOCKER_CONTEXT"; fi
command() {
    if [ "$1" = -v ]; then
        [ "$CASE" != no_python ] || [ "$2" != python3 ] || return 1
        [ "$CASE" != no_rsync ] || [ "$2" != rsync ] || return 1
        [ "$CASE" != no_jq ] || [ "$2" != jq ] || return 1
    fi
    builtin command "$@"
}
is_openwrt() { return 1; }
SERVICE=$ACTIVE
SOCKET=$SOCKET_ACTIVE
STOP_COUNT=0
START_COUNT=0
systemctl_impl() {
    printf '%s\n' "$*" >> "$LOG"
    case "$1" in
        is-active)
            case "${@: -1}" in docker.socket) [ "$SOCKET" = 1 ];; *) [ "$SERVICE" = 1 ];; esac
            [ "$?" = 0 ] && return 0 || return 3;;
        stop)
            STOP_COUNT=$((STOP_COUNT + 1))
            if [ "$CASE" = stop ]; then SERVICE=0; return 1; fi
            [ "$CASE" != still_active ] || return 0
            if [ "$CASE" = rollback_stop ] && [ "$STOP_COUNT" -gt 1 ]; then return 1; fi
            SERVICE=0; SOCKET=0;;
        start)
            START_COUNT=$((START_COUNT + 1))
            if [ "$CASE" = start ] && [ "$START_COUNT" = 1 ]; then return 1; fi
            for arg in "${@:2}"; do
                case "$arg" in docker) SERVICE=1;; docker.socket) SOCKET=1;; esac
            done;;
        show) [ "$CASE" != unit_read ] || return 1; printf '%s\n' "$EXEC_START";;
        cat) printf '%s\n' 'ExecStart=/usr/bin/dockerd -H fd://';;
    esac
}
printf '%s %s %s %s\n' "$SERVICE" "$SOCKET" "$STOP_COUNT" "$START_COUNT" > "${LOG}.state"
systemctl() {
    read -r SERVICE SOCKET STOP_COUNT START_COUNT < "${LOG}.state"
    systemctl_impl "$@"
    local result=$?
    printf '%s %s %s %s\n' "$SERVICE" "$SOCKET" "$STOP_COUNT" "$START_COUNT" > "${LOG}.state"
    return "$result"
}
python3() {
    if [ "$CASE" = prepare ] && [ "${3:-}" = update ]; then return 1; fi
    builtin command python3 "$@"
}
docker() {
    case "$1" in
        context)
            case "$2" in
                show) printf '%s\n' "${CONTEXT_NAME:-default}" ;;
                inspect)
                    [ "$CASE" != context_fail ] || return 1
                    target="$3"
                    if [ "$target" = remote ] || [ "$CASE" = remote ]; then
                        printf '%s\n' 'tcp://192.0.2.10:2375'
                    elif [ "$CASE" = ssh ]; then
                        printf '%s\n' 'ssh://root@192.0.2.10'
                    elif [ "$CASE" = fd_host ]; then
                        printf '%s\n' 'fd://'
                    elif [ "$CASE" = empty_host ]; then
                        printf '\n'
                    elif [ "$CASE" = missing_socket ]; then
                        printf '%s\n' "unix://${SOCK}.missing"
                    elif [ "$CASE" = relative_socket ]; then
                        printf '%s\n' 'unix://relative.sock'
                    else
                        printf '%s\n' "unix://${SOCK}"
                    fi
                    ;;
                *) return 1 ;;
            esac
            return 0
            ;;
    esac
    if [ "$1" = info ] && [[ "$*" == *'{{json .}}'* ]]; then
        case "$CASE" in
            fallback|info_fail) return 1 ;;
            bad_info) printf '%s\n' '{'; return 0 ;;
            containerd)
                printf '%s\n' '{"Driver":"overlayfs","DockerRootDir":"/data/docker","DriverStatus":[["driver-type","io.containerd.snapshotter.v1"]]}'
                return 0 ;;
            unknown_driver)
                printf '%s\n' '{"Driver":"btrfs","DockerRootDir":"/data/docker","DriverStatus":[]}'
                return 0 ;;
            foreign_driver_type)
                printf '%s\n' '{"Driver":"overlay2","DockerRootDir":"/data/docker","DriverStatus":[["driver-type","other.snapshotter"]]}'
                return 0 ;;
            duplicate_driver_type)
                printf '%s\n' '{"Driver":"overlay2","DockerRootDir":"/data/docker","DriverStatus":[["driver-type","overlay2"],["driver-type","io.containerd.snapshotter.v1"]]}'
                return 0 ;;
            null_status)
                printf '%s\n' '{"Driver":"overlay2","DockerRootDir":"/data/docker","DriverStatus":null}'
                return 0 ;;
            overlayfs)
                printf '%s\n' '{"Driver":"overlayfs","DockerRootDir":"/data/docker","DriverStatus":[["Backing Filesystem","extfs"]]}'
                return 0 ;;
        esac
        printf '%s\n' '{"Driver":"overlay2","DockerRootDir":"/data/docker","DriverStatus":[["Backing Filesystem","extfs"]]}'
        return 0
    fi
    if [ "$STOP_COUNT" = 0 ]; then
        [ "$CASE" != fallback ] || return 1
        printf '%s\n' "$OLD"; return
    fi
    if [ "$CASE" = restore_config ] || [ "$CASE" = verify ] || [ "$CASE" = missing_backup ] || [ "$CASE" = rollback_stop ] || [ "$CASE" = occupied_placeholder ]; then
        if [ "$CASE" = missing_backup ]; then
            command mv "$OLD".bak-* "$OLD.recovered"
            command cp -a "$OLD.recovered/." "$OLD/"
        fi
        [ "$CASE" != occupied_placeholder ] || printf rescue > "$OLD/rescue"
        printf '%s\n' /wrong
    else printf '%s\n' "$NEW"; fi
}
ls() {
    [ "$CASE" != unreadable ] || return 2
    command ls "$@"
}
pwd() {
    [ "$CASE" != resolve ] || return 2
    builtin pwd "$@"
}
rsync() {
    printf 'COPY %s\n' "$*" >> "$LOG"
    printf '%s\0' "$@" >> "${LOG}.rsync-args"
    # macOS openrsync supports archive/hardlinks/numeric IDs, but not Linux
    # ACL/xattr flags. Record the production flags above; exercise the
    # supported subset for real metadata fixtures, cp -a for failure cases.
    if [ "$METADATA" = 1 ]; then
        command /usr/bin/rsync -aH --numeric-ids "${@: -2:1}" "${@: -1}" || return
    else
        command cp -a "${@: -2:1}." "${@: -1}"
    fi
    [ "$CASE" != copy ]
}
mv() {
    printf 'MOVE %s\n' "$*" >> "$LOG"
    [ "$1" != -- ] || shift
    if [ "$CASE" = backup ] && [ "$1" = "$OLD" ]; then return 1; fi
    command mv "$@"
}
cp() {
    printf 'CP %s\n' "$*" >> "$LOG"
    if [ "$CASE" = restore_config ] && [ "${@: -1}" = "${OLD%/old}/etc/docker/daemon.json" ]; then return 1; fi
    if [ "$CASE" = config_backup ] && [[ "$*" = *daemon.json* ]]; then return 1; fi
    command cp "$@"
}
TEE_COUNT=0
tee() {
    printf 'TEE %s\n' "$*" >> "$LOG"
    TEE_COUNT=$((TEE_COUNT + 1))
    command tee "$@"
    if [ "$CASE" = write ] && [ "$TEE_COUNT" = 1 ]; then return 1; fi
}
chmod() {
    printf 'CHMOD %s\n' "$*" >> "$LOG"
    printf 'chmod\0%s\0' "$@" >> "${LOG}.permissions"
    command chmod "$@"
}
chown() {
    # Record every real argument, but never try privileged root ownership.
    printf 'CHOWN %s\n' "$*" >> "$LOG"
    printf 'chown\0%s\0' "$@" >> "${LOG}.permissions"
    return 99
}
apt-get() { printf 'UNEXPECTED INSTALL\n' >> "$LOG"; return 99; }
'''
            result = subprocess.run(["/bin/bash", "-c", setup + code +
                                     '\nmigrate_docker_datadir\nstatus=$?\nread -r SERVICE SOCKET STOP_COUNT START_COUNT < "${LOG}.state"\nprintf "FINAL %s %s\\n" "$SERVICE" "$SOCKET" >> "$LOG"\nexit "$status"'],
                                    input=f"y\n{target}\n", env=env, text=True,
                                    capture_output=True, errors="replace", timeout=10)
            if original_daemon_stat is not None:
                for path in (daemon, *daemon.parent.glob("daemon.json.bak-*")):
                    if path.exists():
                        info = path.stat()
                        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid),
                                         (0o600, original_daemon_stat.st_uid, original_daemon_stat.st_gid))
            files = {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}
            if metadata:
                after = {name: snapshot(new / name) for name in
                         (".", "unique-data", "private", "hardlink", "external-link")}
                after["parent"] = snapshot(root)
                after["external"] = snapshot(root / "external")
                files["metadata"] = json.dumps({"before": before, "after": after,
                                     "hardlinks_preserved":
                                     (new / "unique-data").stat().st_ino ==
                                     (new / "hardlink").stat().st_ino})
            return result, files

    def assert_failed_safely(self, case, **kwargs):
        result, files = self.run_case(case, **kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(value == "original-data" for key, value in files.items()
                            if key.startswith("old")), files)
        return files

    def test_stop_failure_never_copies_and_restores_active_services(self):
        files = self.assert_failed_safely("stop")
        self.assertNotIn("COPY", files["calls"])
        self.assertIn("FINAL 1 1", files["calls"])
        self.assertEqual(files["old/unique-data"], "original-data")

    def test_copy_failure_never_moves_or_switches(self):
        files = self.assert_failed_safely("copy")
        self.assertNotIn("MOVE", files["calls"])
        self.assertEqual(files["etc/docker/daemon.json"], '{"custom":true}\n')
        self.assertIn("FINAL 1 1", files["calls"])

    def test_backup_failure_preserves_only_original(self):
        files = self.assert_failed_safely("backup")
        self.assertEqual(files["old/unique-data"], "original-data")
        self.assertEqual(files["etc/docker/daemon.json"], '{"custom":true}\n')

    def test_config_backup_failure_never_moves_data(self):
        files = self.assert_failed_safely("config_backup")
        self.assertNotIn("MOVE", files["calls"])

    def test_start_and_verify_failure_restore_data_and_exact_config(self):
        for case in ("start", "verify"):
            with self.subTest(case=case):
                files = self.assert_failed_safely(case)
                self.assertEqual(files["old/unique-data"], "original-data")
                self.assertEqual(files["etc/docker/daemon.json"], '{"custom":true}\n')
                self.assertEqual(files["etc/systemd/system/docker.service.d/override.conf"], "# original override\n")
                self.assertIn("FINAL 1 1", files["calls"])

    def test_rollback_restores_absent_config_and_inactive_services(self):
        files = self.assert_failed_safely("verify", active=False, config=False, override=False)
        self.assertNotIn("etc/docker/daemon.json", files)
        self.assertNotIn("etc/systemd/system/docker.service.d/override.conf", files)
        self.assertIn("FINAL 0 0", files["calls"])

    def test_missing_backup_does_not_delete_current_directory(self):
        files = self.assert_failed_safely("missing_backup")
        self.assertIn("old.recovered/unique-data", files)
        self.assertEqual(files["old/unique-data"], "original-data")
        self.assertNotIn("已回滚至迁移前状态", self.run_case("missing_backup")[0].stdout)

    def test_nonempty_placeholder_is_preserved(self):
        files = self.assert_failed_safely("occupied_placeholder")
        self.assertEqual(files["old/rescue"], "rescue")
        self.assertTrue(any(key.startswith("old.bak-") for key in files))

    def test_rollback_stop_failure_preserves_backup(self):
        files = self.assert_failed_safely("rollback_stop")
        self.assertTrue(any(key.startswith("old.bak-") for key in files))

    def test_successful_stop_with_active_service_never_copies(self):
        files = self.assert_failed_safely("still_active")
        self.assertNotIn("COPY", files["calls"])

    def test_write_failure_restores_original_config(self):
        files = self.assert_failed_safely("write")
        self.assertEqual(files["old/unique-data"], "original-data")
        self.assertEqual(files["etc/docker/daemon.json"], '{"custom":true}\n')

    def test_rollback_restores_independent_service_states(self):
        for active, socket in ((True, False), (False, True)):
            files = self.assert_failed_safely("verify", active=active, socket_active=socket)
            self.assertIn(f"FINAL {int(active)} {int(socket)}", files["calls"])

    def test_symlink_configs_are_rejected_before_stopping(self):
        for kwargs in ({"symlink_config": True}, {"symlink_override": True}):
            with self.subTest(kwargs=kwargs):
                result, files = self.run_case("verify", **kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("stop docker", files["calls"])
                self.assertIn("配置不是普通文件", result.stdout + result.stderr)
                self.assertEqual(files["etc/docker/daemon.json"], '{"custom":true}\n')
                self.assertEqual(files["etc/systemd/system/docker.service.d/override.conf"], "# original override\n")

    def test_config_restore_failure_never_reports_success_or_restarts(self):
        result, files = self.run_case("restore_config")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("回滚未完成", result.stdout)
        self.assertNotIn("已回滚至迁移前状态", result.stdout)
        self.assertEqual(files["calls"].count("start docker\n"), 1)
        self.assertIn("FINAL 0 0", files["calls"])
        self.assertTrue(any(key.startswith("etc/docker/daemon.json.bak-") for key in files))

    def test_unsafe_targets_rejected_before_any_changes(self):
        for layout in ("nonempty", "hidden", "same", "dot_alias", "symlink_alias",
                       "ancestor", "descendant", "symlink_descendant", "source_alias",
                       "missing_source", "missing_target", "root_source", "root_target", "newline_file"):
            with self.subTest(layout=layout):
                result, files = self.run_case(layout=layout)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                for action in ("stop docker", "COPY", "MOVE", "CP ", "TEE ", "UNEXPECTED INSTALL"):
                    self.assertNotIn(action, files["calls"], files["calls"])
                self.assertFalse(any(".bak-" in key for key in files), files)
                self.assertEqual(files["etc/docker/daemon.json"], '{"custom":true}\n')
                self.assertEqual(files["etc/systemd/system/docker.service.d/override.conf"], "# original override\n")
                original_key = "new/old/unique-data" if layout == "ancestor" else "old/unique-data"
                self.assertEqual(files[original_key], "original-data")
                self.assertEqual(files["calls"], "FINAL 1 1\n")
                if layout in ("nonempty", "hidden", "newline_file"):
                    name = {"nonempty": "unrelated", "hidden": ".hidden", "newline_file": "\n"}[layout]
                    self.assertEqual(files[f"new/{name}"], "keep-me")

    def test_path_resolution_and_directory_read_fail_closed(self):
        for failure in ("unreadable", "resolve"):
            with self.subTest(failure=failure):
                result, files = self.run_case(failure)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(files["calls"], "FINAL 1 1\n")
                self.assertEqual(files["old/unique-data"], "original-data")
                self.assertEqual(files["etc/docker/daemon.json"], '{"custom":true}\n')
                self.assertFalse(any(".bak-" in key for key in files))

    def test_empty_target_with_spaces_and_alias_succeeds(self):
        for layout, directory in (("spaces", "new space"), ("target_symlink", "new")):
            with self.subTest(layout=layout):
                result, files = self.run_case(layout=layout)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(files[f"{directory}/unique-data"], "original-data")
                self.assertNotIn("--delete", files["calls"])

    def test_source_trailing_newline_preserves_exact_directory(self):
        result, files = self.run_case(layout="source_trailing_newline")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(files["new/unique-data"], "original-data")
        self.assertEqual(files["old/unrelated"], "keep-me")
        self.assertNotIn("new/unrelated", files)
        self.assertTrue(any(key.startswith("old\n.bak-") for key in files))

    def test_copy_contract_preserves_numeric_ids_and_attributes_without_permission_reset(self):
        result, files = self.run_case()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = files["calls.rsync-args"].split("\0")[:-1]
        self.assertEqual(args[:2], ["-aHAX", "--numeric-ids"])
        self.assertEqual(len(args), 4)
        self.assertTrue(args[2].endswith("/old/"), args)
        self.assertTrue(args[3].endswith("/new/"), args)
        self.assertNotIn("CHOWN", files["calls"])
        self.assertNotIn("CHMOD", files["calls"])
        self.assertNotIn("calls.permissions", files)

    def test_real_sandbox_metadata_and_external_symlink_target_are_preserved(self):
        result, files = self.run_case(metadata=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata = json.loads(files["metadata"])
        self.assertEqual(metadata["after"], metadata["before"])
        self.assertTrue(metadata["hardlinks_preserved"])
        self.assertEqual(files["new/external-link"], "outside-data")
        self.assertNotIn("calls.permissions", files)

    def test_success_changes_only_data_root_and_preserves_unit(self):
        original = {"registry-mirrors": ["https://mirror.example"], "dns": ["1.1.1.1"],
                    "userns-remap": "default", "log-driver": "local",
                    "log-opts": {"max-size": "7m", "custom": "value"},
                    "custom": {"nested": [True, None, "雪"]}, "data-root": "/previous"}
        for layout in ("", "special"):
            with self.subTest(layout=layout):
                result, files = self.run_case(config_text=json.dumps(original), layout=layout)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                actual = json.loads(files["etc/docker/daemon.json"])
                destination = actual.pop("data-root")
                self.assertEqual(actual, {k: v for k, v in original.items() if k != "data-root"})
                directory = 'new "quote" \\ tab\t雪' if layout else "new"
                self.assertTrue(destination.endswith("/" + directory), destination)
                self.assertEqual(files[directory + "/unique-data"], "original-data")
                self.assertEqual(files["etc/systemd/system/docker.service.d/override.conf"], "# original override\n")
                writes = [line for line in files["calls"].splitlines() if line.startswith("TEE ")]
                self.assertEqual(len(writes), 1)
                self.assertTrue(writes[0].endswith("/etc/docker/daemon.json"))

    def test_unavailable_daemon_is_rejected_before_stop_copy_or_backup(self):
        result, files = self.run_case("fallback", active=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("不再在守护进程不可用时继续", result.stdout + result.stderr)
        for action in ("stop docker", "COPY", "MOVE", "CP ", "TEE "):
            self.assertNotIn(action, files["calls"])
        self.assertFalse(any(".bak-" in key for key in files))
        self.assertEqual(files["old/unique-data"], "original-data")
        self.assertIn('"custom": true', files["etc/docker/daemon.json"])

    def test_absent_config_adds_only_data_root(self):
        result, files = self.run_case(config=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(json.loads(files["etc/docker/daemon.json"])), ["data-root"])

    def test_invalid_configuration_and_missing_tools_fail_before_mutation(self):
        for kwargs in ({"config_text": "{"}, {"config_text": "[]"},
                       {"config_text": "null"}, {"config_text": ""},
                       {"failure": "no_python"}, {"failure": "no_rsync"},
                       {"failure": "unit_read"}, {"failure": "prepare"}):
            with self.subTest(kwargs=kwargs):
                result, files = self.run_case(failure=kwargs.get("failure", ""),
                                              config_text=kwargs.get("config_text"))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                for action in ("stop docker", "COPY", "MOVE", "CP ", "TEE ", "UNEXPECTED INSTALL"):
                    self.assertNotIn(action, files["calls"])
                self.assertEqual(files["old/unique-data"], "original-data")
                self.assertFalse(any(".bak-" in key for key in files))
                if "config_text" in kwargs:
                    self.assertEqual(files["etc/docker/daemon.json"], kwargs["config_text"])

    def test_unsafe_effective_execstart_is_rejected_without_mutation(self):
        template = "{ path=%s ; argv[]=%s ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
        for executable, args in (("/usr/bin/dockerd", "--data-root=/old"),
                                 ("/usr/bin/dockerd", "--data-root /old"),
                                 ("/usr/bin/dockerd", "-g /old"),
                                 ("/usr/bin/dockerd", "-g/old"),
                                 ("/usr/bin/dockerd", "--config-file=/other"),
                                 ("/bin/sh", "-c dockerd"),
                                 ("/usr/bin/dockerd", "$OPTIONS"),
                                 ("/usr/bin/dockerd", "--label=%i"),
                                 ("/usr/bin/dockerd", '--label="quoted value"')):
            with self.subTest(args=args):
                unit = template % (executable, executable + " " + args)
                result, files = self.run_case(exec_start=unit)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                for action in ("stop docker", "COPY", "MOVE", "CP ", "TEE "):
                    self.assertNotIn(action, files["calls"])
                self.assertEqual(files["etc/systemd/system/docker.service.d/override.conf"], "# original override\n")
        for unit in ("", "unrecognized", template % ("/usr/bin/dockerd", "/usr/bin/dockerd") + " ; " + template % ("/usr/bin/dockerd", "/usr/bin/dockerd")):
            result, files = self.run_case(exec_start=unit)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("stop docker", files["calls"])

    def test_official_execstart_status_layouts_accept_standard_dockerd(self):
        # Format literals verified against systemd v252/main systemctl-show.c.
        template = ("{ path=/usr/bin/dockerd ; argv[]=/usr/bin/dockerd -H fd:// "
                    "--containerd=/run/containerd/containerd.sock ; ignore_errors=no "
                    "; start_time=[%s] ; stop_time=[n/a] ; pid=123 ; code=%s ; status=%s }")
        for timestamp, code, status in (("n/a", "(null)", "0/0"),
                                         ("Sun 2026-10-18 09:00:00 +08", "exited", "0"),
                                         ("Sun 2026-10-18 09:00:00 +08", "killed", "9/KILL")):
            result, files = self.run_case(exec_start=template % (timestamp, code, status))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(files["new/unique-data"], "original-data")

    def test_ambiguous_json_fails_before_mutation(self):
        for original in ('{"dns":[],"dns":["9.9.9.9"]}',
                         '{"custom":{"same":1,"same":2}}',
                         '{"custom":0.12345678901234567890123456789}',
                         '{"custom":1e999}'):
            with self.subTest(original=original):
                result, files = self.run_case(config_text=original)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(files["etc/docker/daemon.json"], original)
                for action in ("stop docker", "COPY", "MOVE", "CP ", "TEE "):
                    self.assertNotIn(action, files["calls"])

    def test_large_integer_and_exact_decimal_values_are_preserved(self):
        original = {"custom": [123456789012345678901234567890123456789, 0.25]}
        result, files = self.run_case(config_text=json.dumps(original))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(files["etc/docker/daemon.json"])["custom"], original["custom"])

    def test_execstart_argument_cannot_impersonate_status_fields(self):
        # systemd v252 and main use strv_join(argv, " "), without escaping.
        # The inner delimiter is literal argv data, followed by the real suffix.
        unit = ("{ path=/usr/bin/dockerd ; argv[]=/usr/bin/dockerd --label=x "
                "; ignore_errors=no ; --data-root=/other ; ignore_errors=no "
                "; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")
        result, files = self.run_case(exec_start=unit)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("stop docker", files["calls"])

    def test_failure_restores_configuration_bytes_including_whitespace(self):
        original = ' { "dns" : ["9.9.9.9"], "custom": "雪", "log-driver":"local" }\n\n'
        result, files = self.run_case("verify", config_text=original)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(files["etc/docker/daemon.json"], original)

    def test_success_retains_backup_and_copies_data(self):
        result, files = self.run_case()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(files["new/unique-data"], "original-data")
        self.assertTrue(any(key.startswith("old.bak-") for key in files))
        self.assertIn('"data-root":', files["etc/docker/daemon.json"])


    def test_storage_and_remote_endpoints_are_rejected_before_mutation(self):
        cases = (
            "containerd", "unknown_driver", "foreign_driver_type", "duplicate_driver_type",
            "null_status", "bad_info", "info_fail", "context_fail", "remote", "ssh",
            "fd_host", "empty_host", "missing_socket", "relative_socket", "no_jq",
        )
        for case in cases:
            with self.subTest(case=case):
                result, files = self.run_case(case)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                for action in ("stop docker", "COPY", "MOVE", "CP ", "TEE ", "UNEXPECTED INSTALL"):
                    self.assertNotIn(action, files["calls"])
                self.assertFalse(any(".bak-" in key for key in files), files)
                self.assertEqual(files["etc/docker/daemon.json"], '{"custom":true}\n')
                self.assertEqual(files["old/unique-data"], "original-data")
                if case == "containerd":
                    self.assertIn("containerd image store", result.stderr)
                    self.assertIn("28", result.stderr)

    def test_env_host_overrides_context_and_remote_host_is_rejected(self):
        refused, files = self.run_case(docker_host="tcp://192.0.2.10:2375")
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("TCP", refused.stderr)
        self.assertNotIn("stop docker", files["calls"])
        self.assertNotIn("COPY", files["calls"])
        allowed, copied = self.run_case("remote", docker_host="SANDBOX")
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        self.assertEqual(copied["new/unique-data"], "original-data")
        context_refused, remote_files = self.run_case(docker_context="remote")
        self.assertNotEqual(context_refused.returncode, 0, context_refused.stdout + context_refused.stderr)
        self.assertIn("TCP", context_refused.stderr)
        self.assertNotIn("COPY", remote_files["calls"])

    def test_overlayfs_without_containerd_driver_type_still_migrates(self):
        result, files = self.run_case("overlayfs")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(files["new/unique-data"], "original-data")


class OpenWrtDockerMigrationTest(unittest.TestCase):
    def run_openwrt(self, case="ok", jq=True):
        with tempfile.TemporaryDirectory(prefix="yehbp-openwrt-docker-", dir=SCRATCH) as temp:
            root = Path(temp).resolve()
            old, new = root / "old", root / "new"
            old.mkdir()
            new.mkdir()
            (old / "unique-data").write_text("original-data")
            cfg = root / "dockerd.conf"
            cfg.write_text("config dockerd 'globals'\n")
            init = root / "dockerd.init"
            init.write_text("#!/bin/bash\nprintf '%s\\n' \"$1\" >> \"$LOG\"\ncase \"$1\" in\n"
                            "  stop) printf stopped > \"$STATE\" ;;\n"
                            "  start) printf started > \"$STATE\" ;;\n"
                            "esac\nexit 0\n")
            init.chmod(0o755)
            probe = socket.socket(socket.AF_UNIX)
            sock = root / "docker.sock"
            try:
                probe.bind(str(sock))
            finally:
                probe.close()
            code = "\n".join(function(name) for name in (
                "docker_assert_local_daemon", "docker_classify_storage",
                "docker_migration_guard", "migrate_docker_datadir_openwrt"))
            code = code.replace("/etc/init.d/dockerd", str(init))
            code = code.replace("/etc/config/dockerd", str(cfg))
            self.assertNotIn("/etc/init.d/dockerd", code)
            self.assertNotIn("/etc/config/dockerd", code)
            env = dict(os.environ, OLD=str(old), NEW=str(new), CASE=case, SOCK=str(sock),
                       LOG=str(root / "calls"), STATE=str(root / "state"), JQ=str(int(jq)))
            for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONFIG"):
                env.pop(key, None)
            script = r'''
unset DOCKER_HOST DOCKER_CONTEXT
command() {
    if [ "$1" = -v ] && [ "$2" = jq ] && [ "$JQ" = 0 ]; then return 1; fi
    builtin command "$@"
}
uci() { printf 'uci %s\n' "$*" >> "$LOG"; return 0; }
sleep() { :; }
docker() {
    case "$1" in
        context)
            case "$2" in
                show) printf '%s\n' default ;;
                inspect)
                    if [ "$CASE" = remote ]; then printf '%s\n' 'tcp://192.0.2.10:2375'; else printf '%s\n' "unix://${SOCK}"; fi
                    ;;
                *) return 1 ;;
            esac
            return 0 ;;
    esac
    if [ "$1" = info ] && [[ "$*" == *'{{json .}}'* ]]; then
        case "$CASE" in
            containerd)
                printf '%s\n' '{"Driver":"overlayfs","DockerRootDir":"'"$OLD"'","DriverStatus":[["driver-type","io.containerd.snapshotter.v1"]]}' ;;
            bad_info) printf '%s\n' '{' ;;
            info_fail) return 1 ;;
            *) printf '%s\n' '{"Driver":"overlay2","DockerRootDir":"'"$OLD"'","DriverStatus":[["Backing Filesystem","extfs"]]}' ;;
        esac
        return 0
    fi
    if [[ "$*" == *DockerRootDir* ]]; then
        if [ "$(cat "$STATE" 2>/dev/null)" = started ]; then printf '%s\n' "$NEW"; else printf '%s\n' "$OLD"; fi
        return 0
    fi
}
''' + code + '\nmigrate_docker_datadir_openwrt\n'
            result = subprocess.run(["/bin/bash", "-c", script], input=f"{new}\ny\n", env=env,
                                    text=True, capture_output=True, errors="replace", timeout=20)
            files = {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}
            return result, files

    def test_overlay2_openwrt_migration_still_copies(self):
        result, files = self.run_openwrt()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(files["new/unique-data"], "original-data")
        log = files["calls"]
        self.assertLess(log.index("stop\n"), log.index("uci set dockerd.globals.data_root="))
        self.assertLess(log.index("uci commit"), log.index("\nstart\n"))
        self.assertTrue(any(key.startswith("old.bak-") for key in files))

    def test_openwrt_refuses_before_stop_copy_or_backup(self):
        for case, jq in (("containerd", True), ("remote", True), ("bad_info", True),
                         ("info_fail", True), ("ok", False)):
            with self.subTest(case=case, jq=jq):
                result, files = self.run_openwrt(case, jq=jq)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("stop", files.get("calls", ""))
                self.assertNotIn("uci set", files.get("calls", ""))
                self.assertEqual(files["old/unique-data"], "original-data")
                self.assertFalse(any(".bak-" in key for key in files), files)
                if case == "containerd":
                    self.assertIn("containerd image store", result.stderr)
                if not jq:
                    self.assertIn("缺少 jq", result.stderr)


def _load(*names):
    return "\n".join(function(name) for name in names)


class DockerLogOptimizationTest(unittest.TestCase):
    def run_logs(self, case="ok", config=None, missing=False, symlink=False, confirm="n\n",
                 docker_host=None):
        with tempfile.TemporaryDirectory(prefix="yehbp-docker-logs-", dir=SCRATCH) as temp:
            root = Path(temp).resolve()
            daemon = root / "daemon.json"
            if symlink:
                target = root / "daemon.target"
                target.write_text(config or "{}\n")
                daemon.symlink_to(target)
            elif not missing:
                daemon.write_text(config if config is not None else '{"custom":true}\n')
                daemon.chmod(0o600)
            proc = root / "proc"
            sock_dir = proc / "4242" / "root" / "var" / "run"
            sock_dir.mkdir(parents=True)
            probe = socket.socket(socket.AF_UNIX)
            sock = root / "docker.sock"
            try:
                probe.bind(str(sock))
            finally:
                probe.close()
            os.link(sock, sock_dir / "docker.sock")
            inventory = root / "inventory"
            inventory.write_text("aaaa true 4242\nbbbb true 7\n")
            code = _load(
                "docker_assert_local_daemon", "docker_dev_inode", "docker_capture_inventory",
                "docker_verify_inventory", "docker_restore_service_state",
                "docker_offer_socket_restarts", "docker_json_merge_logs", "optimize_docker_logs")
            code = code.replace('"/proc/', f'"{proc}/')
            code = code.replace("/etc/docker/daemon.json", str(daemon))
            code = code.replace("${EUID:-$(id -u)}", "0")
            self.assertNotIn("/etc/docker/daemon.json", code)
            self.assertNotIn('"/proc/', code)
            env = dict(os.environ, CASE=case, SOCK=str(sock), LOG=str(root / "calls"),
                       INV=str(inventory), PROC=str(proc), DAEMON=str(daemon))
            for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONFIG"):
                env.pop(key, None)
            if docker_host:
                env["TEST_DOCKER_HOST"] = docker_host
            script = r'''
unset DOCKER_HOST DOCKER_CONTEXT
if [ -n "${TEST_DOCKER_HOST+x}" ]; then export DOCKER_HOST="$TEST_DOCKER_HOST"; fi
command() {
    if [ "$1" = -v ] && [ "$2" = python3 ] && [ "$CASE" = no_python ]; then return 1; fi
    builtin command "$@"
}
systemctl() {
    printf 'systemctl %s\n' "$*" >> "$LOG"
    case "$1" in
        is-active) return 0 ;;
        restart)
            if [ "$CASE" = restart_fail ] || [ "$CASE" = restore_fail ]; then return 1; fi
            if [ "$CASE" = swap_socket ]; then
                mv "$SOCK" "$SOCK.previous" || return 1
                python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])' "$SOCK" || return 1
            fi
            ;;
        start|stop) ;;
    esac
    return 0
}
cp() {
    printf 'CP %s\n' "$*" >> "$LOG"
    if [ "$CASE" = restore_fail ] && [ "${@: -1}" = "$DAEMON" ]; then return 1; fi
    command cp "$@"
}
tee() {
    printf 'TEE %s\n' "$*" >> "$LOG"
    if [ "$CASE" = write_fail ]; then return 1; fi
    command tee "$@"
}
docker() {
    printf 'docker %s\n' "$*" >> "$LOG"
    case "$1" in
        context)
            case "$2" in
                show) printf '%s\n' default ;;
                inspect) printf '%s\n' "unix://${SOCK}" ;;
            esac
            return 0 ;;
        restart) printf 'RESTART %s\n' "$2" >> "$LOG"; return 0 ;;
        ps)
            if [[ "$*" == *"-aq"* ]]; then awk '{print $1}' "$INV"; elif [[ "$*" == *-q* ]]; then awk '$2=="true"{print $1}' "$INV"; fi
            return 0 ;;
        inspect)
            id="${@: -1}"
            fmt="$3"
            case "$fmt" in
                *Running*) awk -v id="$id" '$1==id {print $2}' "$INV" ;;
                *Pid*) awk -v id="$id" '$1==id {print $3}' "$INV" ;;
                *Mounts*)
                    if [ "$id" = aaaa ]; then printf 'bind\t/var/run/docker.sock\t/var/run/docker.sock\n'; fi
                    ;;
            esac
            return 0 ;;
        info)
            if [[ "$*" == *json* ]]; then
                printf '%s\n' '{"Driver":"overlay2","DockerRootDir":"/data/docker","DriverStatus":[["Backing Filesystem","extfs"]]}'
            elif [[ "$*" == *LoggingDriver* ]]; then printf '%s\n' json-file
            else printf '%s\n' /data/docker
            fi
            return 0 ;;
    esac
}
''' + code + "\noptimize_docker_logs\n"
            before = daemon.read_text() if daemon.is_file() and not daemon.is_symlink() else None
            before_mode = daemon.stat().st_mode if daemon.is_file() and not daemon.is_symlink() else None
            result = subprocess.run(["/bin/bash", "-c", script], input=confirm, env=env, text=True,
                                    capture_output=True, errors="replace", timeout=20)
            after = {"symlink": daemon.is_symlink(), "text": None, "mode": None, "uid": None, "target": None}
            if daemon.is_symlink():
                after["target"] = daemon.resolve().read_text()
            elif daemon.is_file():
                after["text"] = daemon.read_text()
                info = daemon.stat()
                after["mode"] = stat.S_IMODE(info.st_mode)
                after["uid"] = info.st_uid
            files = {str(p.relative_to(root)): p.read_text(errors="replace")
                     for p in root.rglob("*") if p.is_file() and not p.is_symlink()}
            return result, files, after

    def test_merges_only_log_fields_and_keeps_owner_mode(self):
        original = {
            "features": {"buildkit": True},
            "live-restore": True,
            "registry-mirrors": ["https://mirror.example"],
            "data-root": "/data/docker",
            "log-opts": {"max-size": "1m", "env": "FOO"},
            "custom": {"n": 123456789012345678901234567890, "f": 0.5},
        }
        result, files, after = self.run_logs(config=json.dumps(original) + "\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = json.loads(after["text"])
        self.assertEqual(actual["features"], original["features"])
        self.assertTrue(actual["live-restore"])
        self.assertEqual(actual["registry-mirrors"], original["registry-mirrors"])
        self.assertEqual(actual["data-root"], original["data-root"])
        self.assertEqual(actual["custom"], original["custom"])
        self.assertEqual(actual["log-opts"]["env"], "FOO")
        self.assertEqual(actual["log-opts"]["max-size"], "20m")
        self.assertEqual(actual["log-opts"]["max-file"], "3")
        self.assertEqual(actual["log-driver"], "json-file")
        self.assertEqual(after["mode"], 0o600)
        self.assertEqual(after["uid"], os.getuid())
        self.assertNotIn("RESTART ", files["calls"])
        self.assertIn("docker.sock 未更换", result.stdout)
        self.assertIn("systemctl restart docker", files["calls"])

    def test_invalid_input_does_not_mutate_or_restart(self):
        samples = {
            "bad": "{",
            "array": "[]",
            "null": "null",
            "duplicate": '{"dns":[],"dns":["9.9.9.9"]}',
            "nested-duplicate": '{"custom":{"same":1,"same":2}}',
            "float": '{"custom":0.1234567890123456789}',
            "opts": '{"log-opts":"nope"}',
            "empty": "",
        }
        for name, text in samples.items():
            with self.subTest(name=name):
                result, files, after = self.run_logs(config=text)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(after["text"], text)
                self.assertNotIn("TEE ", files.get("calls", ""))
                self.assertNotIn("systemctl", files.get("calls", ""))
                self.assertFalse(any(".bak-" in key for key in files))
                self.assertNotIn("不存在，将新建", result.stdout)
                if name == "bad":
                    self.assertIn("不是合法 JSON", result.stderr)

    def test_missing_file_is_distinct_from_bad_file(self):
        result, files, after = self.run_logs(missing=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("不存在，将新建", result.stdout)
        self.assertNotIn("不是合法 JSON", result.stderr)
        actual = json.loads(after["text"])
        self.assertEqual(set(actual), {"log-driver", "log-opts"})
        self.assertEqual(actual["log-opts"], {"max-size": "20m", "max-file": "3"})

    def test_symlink_and_missing_parser_are_rejected(self):
        result, files, after = self.run_logs(symlink=True, config='{"keep":true}\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不是普通文件", result.stdout + result.stderr)
        self.assertTrue(after["symlink"])
        self.assertEqual(after["target"], '{"keep":true}\n')
        self.assertNotIn("TEE ", files.get("calls", ""))
        self.assertNotIn("systemctl", files.get("calls", ""))
        refused, refused_files, refused_after = self.run_logs(case="no_python")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("缺少 python3", refused.stdout + refused.stderr)
        self.assertEqual(refused_after["text"], '{"custom":true}\n')
        self.assertNotIn("systemctl", refused_files.get("calls", ""))

    def test_restart_failure_restores_config_and_service_state(self):
        original = '{"features":{"ok":true},"live-restore":true}\n'
        result, files, after = self.run_logs(case="restart_fail", config=original)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(after["text"], original)
        self.assertEqual(after["mode"], 0o600)
        self.assertIn("已恢复原配置和服务状态", result.stdout)
        self.assertNotIn("回滚未完成", result.stdout)
        self.assertNotIn("✅", result.stdout)
        self.assertIn("systemctl start docker", files["calls"])
        self.assertIn("systemctl start docker.socket", files["calls"])

    def test_failed_restore_is_not_reported_as_success(self):
        original = '{"custom":1}\n'
        result, files, after = self.run_logs(case="restore_fail", config=original)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("回滚未完成", result.stdout)
        self.assertNotIn("已恢复原配置和服务状态", result.stdout)
        self.assertNotIn("✅", result.stdout)
        write_result, write_files, write_after = self.run_logs(case="write_fail", config=original)
        self.assertNotEqual(write_result.returncode, 0, write_result.stdout + write_result.stderr)
        self.assertEqual(write_after["text"], original)
        self.assertIn("已恢复原配置", write_result.stdout)
        self.assertNotIn("回滚未完成", write_result.stdout)
        self.assertNotIn("systemctl restart", write_files["calls"])

    def test_socket_restart_is_scoped_and_decline_does_not_restart(self):
        original = '{"live-restore":true,"features":{"containerd-snapshotter":false}}\n'
        declined, files, after = self.run_logs(case="swap_socket", config=original, confirm="\n")
        self.assertEqual(declined.returncode, 0, declined.stdout + declined.stderr)
        self.assertIn("未恢复", declined.stdout)
        self.assertNotIn("RESTART ", files["calls"])
        self.assertNotIn("✅", declined.stdout)
        self.assertTrue(json.loads(after["text"])["live-restore"])
        accepted, accepted_files, _ = self.run_logs(case="swap_socket", config=original, confirm="y\n")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("RESTART aaaa", accepted_files["calls"])
        self.assertNotIn("RESTART bbbb", accepted_files["calls"])
        self.assertIn("✅", accepted.stdout)

    def test_unchanged_socket_does_not_restart_despite_live_restore(self):
        original = '{"live-restore":true}\n'
        result, files, after = self.run_logs(config=original, confirm="y\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("不重启容器", result.stdout)
        self.assertNotIn("RESTART ", files["calls"])
        self.assertTrue(json.loads(after["text"])["live-restore"])

    def test_remote_client_is_rejected_before_log_changes(self):
        result, files, after = self.run_logs(docker_host="ssh://root@192.0.2.10")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SSH", result.stderr)
        self.assertEqual(after["text"], '{"custom":true}\n')
        self.assertNotIn("TEE ", files.get("calls", ""))
        self.assertNotIn("systemctl", files.get("calls", ""))


class DockerInstallStorageTest(unittest.TestCase):
    def run_install(self, mode="upgrade", storage="overlay2", ps_ok=True, failure=""):
        with tempfile.TemporaryDirectory(prefix="yehbp-docker-install-", dir=SCRATCH) as temp:
            root = Path(temp).resolve()
            os_release = root / "os-release"
            os_release.write_text("ID=debian\nVERSION_CODENAME=bookworm\n")
            keyrings = root / "keyrings"
            docker_list = root / "docker.list"
            probe = socket.socket(socket.AF_UNIX)
            sock = root / "docker.sock"
            try:
                probe.bind(str(sock))
            finally:
                probe.close()
            code = _load(
                "docker_assert_local_daemon", "docker_classify_storage", "docker_show_storage_note",
                "docker_dev_inode", "docker_capture_inventory", "docker_verify_inventory",
                "docker_restore_service_state", "docker_offer_socket_restarts",
                "docker_is_standard_installation", "docker_show_available_upgrades", "install_docker")
            for old, new in (("/etc/os-release", str(os_release)),
                             ("/etc/apt/keyrings", str(keyrings)),
                             ("/etc/apt/sources.list.d/docker.list", str(docker_list))):
                code = code.replace(old, new)
            self.assertNotIn("/etc/os-release", code)
            self.assertNotIn("/etc/apt/", code)
            env = dict(os.environ, MODE=mode, STORAGE=storage, PS_OK=str(int(ps_ok)),
                       SOCK=str(sock), LOG=str(root / "calls"))
            for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONFIG"):
                env.pop(key, None)
            script = r'''
unset DOCKER_HOST DOCKER_CONTEXT
DOCKER_CHECKS=0
is_openwrt() { return 1; }
read() { printf 'READ %s\n' "$*" >> "$LOG"; builtin read "$@"; }
sudo() { "$@"; }
command() {
    if [ "$1" = -v ] && [ "$2" = docker ] && [ "$MODE" = fresh ] && [ "$DOCKER_CHECKS" = 0 ]; then
        DOCKER_CHECKS=1
        return 1
    fi
    builtin command "$@"
}
dpkg-query() {
    case "$*" in
        *Status-Status*) printf '%s\n' installed ;;
        *) printf '%s\n' '2:28.0.0' ;;
    esac
}
dpkg() { printf '%s\n' amd64; }
apt-cache() { printf '%s\n' '  Candidate: 2:29.0.0'; }
apt-get() { printf 'apt %s\n' "$*" >> "$LOG"; return 0; }
systemctl() {
    printf 'systemctl %s\n' "$*" >> "$LOG"
    if [ "$1" = show ]; then printf '%s\n' /usr/lib/systemd/system/docker.service; fi
    return 0
}
yehbp_curl() {
    prev=""
    for arg in "$@"; do
        if [ "$prev" = -o ]; then printf 'key\n' > "$arg"; fi
        prev="$arg"
    done
    return 0
}
docker() {
    printf 'docker %s\n' "$*" >> "$LOG"
    case "$1" in
        context)
            case "$2" in
                show) printf '%s\n' default ;;
                inspect) printf '%s\n' "unix://${SOCK}" ;;
            esac
            return 0 ;;
        ps) [ "$PS_OK" = 1 ] || return 1; printf '%s\n' abcdef; return 0 ;;
        inspect) printf '%s\n' true; return 0 ;;
        info)
            if [ "$STORAGE" = containerd ]; then
                printf '%s\n' '{"Driver":"overlayfs","DockerRootDir":"/var/lib/docker","DriverStatus":[["driver-type","io.containerd.snapshotter.v1"]]}'
            else
                printf '%s\n' '{"Driver":"overlay2","DockerRootDir":"/var/lib/docker","DriverStatus":[["Backing Filesystem","extfs"]]}'
            fi
            return 0 ;;
        --version|version) printf '%s\n' 'Docker version test'; return 0 ;;
    esac
    return 0
}
''' + code
            overrides = {
                "recovery": 'docker_offer_socket_restarts() { return 1; }',
                "decline": 'docker_offer_socket_restarts() { return 2; }',
                "inventory": 'docker_verify_inventory() { return 1; }',
                "storage": 'docker_show_storage_note() { return 1; }',
                "packages": 'apt-get() { case "$*" in *"install -y docker-ce"*) return 1 ;; esac; return 0; }',
            }
            script += "\n" + overrides.get(failure, "") + "\ninstall_docker\n"
            result = subprocess.run(["/bin/bash", "-c", script], input="y\n", env=env, text=True,
                                    capture_output=True, errors="replace", timeout=20)
            log = (root / "calls").read_text() if (root / "calls").exists() else ""
            result.wrote_daemon = (root / "daemon.json").exists()
            return result, log, root

    def test_upgrade_records_inventory_before_packages_and_shows_storage(self):
        result, log, root = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(log.index("docker ps -aq"), log.index("apt install -y docker-ce"))
        self.assertNotIn("docker restart", log)
        self.assertIn("Docker 存储模式：overlay2", result.stdout)
        self.assertIn("Docker Root Dir：/var/lib/docker", result.stdout)
        self.assertEqual(log.count("是否确认升级"), 1)
        self.assertNotIn("是否仅重启", log)
        self.assertFalse(result.wrote_daemon)

    def test_containerd_note_does_not_rewrite_config_or_pin(self):
        result, log, root = self.run_install(storage="containerd")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("containerd image store", result.stdout)
        self.assertIn("不会自动修改 daemon.json", result.stdout)
        self.assertIn("固定在 28", result.stdout)
        self.assertNotIn("docker restart", log)
        self.assertFalse(result.wrote_daemon)

    def test_fresh_install_shows_storage_without_a_second_confirmation(self):
        result, log, root = self.run_install(mode="fresh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Docker 存储模式：overlay2", result.stdout)
        self.assertEqual(log.count("是否确认安装"), 1)
        self.assertNotIn("是否确认升级", log)
        self.assertNotIn("docker ps -aq", log)

    def test_upgrade_failures_do_not_report_success(self):
        for failure in ("recovery", "inventory", "storage", "packages"):
            with self.subTest(failure=failure):
                result, _, _ = self.run_install(failure=failure)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("✅ Docker 升级完成", result.stdout)

    def test_declining_socket_recovery_is_not_upgrade_failure(self):
        result, _, _ = self.run_install(failure="decline")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("✅ Docker 升级完成", result.stdout)

    def test_inventory_failure_cancels_upgrade_before_package_install(self):
        result, log, root = self.run_install(ps_ok=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("无法记录现有容器清单", result.stdout)
        self.assertNotIn("apt install -y docker-ce", log)


if __name__ == "__main__":
    unittest.main()
