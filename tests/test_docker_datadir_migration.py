"""Run the real migration function against sandbox files and fake services."""
import json
import os
import shlex
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
    def run_case(self, failure="", active=True, config=True, override=True, socket=None,
                 symlink_config=False, symlink_override=False, layout="", metadata=False,
                 config_text=None, exec_start=None):
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
            code = function("migrate_docker_datadir")
            code = code.replace("/etc/docker/daemon.json", str(daemon))
            code = code.replace("/etc/systemd/system/docker.service.d", str(unit.parent))
            code = code.replace('${EUID:-$(id -u)}', '0')
            env = dict(os.environ, OLD=source, NEW=str(new), CASE=failure,
                       LOG=str(root / "calls"), ACTIVE=str(int(active)),
                       SOCKET_ACTIVE=str(int(active if socket is None else socket)),
                       METADATA=str(int(metadata)),
                       EXEC_START=exec_start if exec_start is not None else
                       "{ path=/usr/bin/dockerd ; argv[]=/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")
            setup = r'''
PATH=/usr/bin:/bin
command() {
    if [ "$1" = -v ]; then
        [ "$CASE" != no_python ] || [ "$2" != python3 ] || return 1
        [ "$CASE" != no_rsync ] || [ "$2" != rsync ] || return 1
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
            files = self.assert_failed_safely("verify", active=active, socket=socket)
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

    def test_inactive_daemon_reads_root_with_real_json_parser(self):
        result, files = self.run_case("fallback", active=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(files["new/unique-data"], "original-data")
        self.assertTrue(json.loads(files["etc/docker/daemon.json"])["custom"])

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


if __name__ == "__main__":
    unittest.main()
