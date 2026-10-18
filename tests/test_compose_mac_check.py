import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'assets/docker-auto-update/check-compose-macs.py'
SPEC = importlib.util.spec_from_file_location('compose_mac_check', SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
MAC = '02:42:ac:11:00:02'


class ComposeMacTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='compose mac ', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'working dir'
        self.work.mkdir()
        self.files = [self.root / 'custom base.yaml', self.root / 'override.yaml']
        for file in self.files:
            file.write_text('services: {}\n')
        self.root_patch = patch.object(check, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def container(self, project='custom-project', service='app', files=None):
        return {'Name': '/app', 'Config': {'Labels': {
            'com.docker.compose.project': project,
            'com.docker.compose.service': service,
            'com.docker.compose.project.working_dir': str(self.work),
            'com.docker.compose.project.config_files': ','.join(map(str, files or self.files)),
        }}, 'NetworkSettings': {'Networks': {'real-net': {'MacAddress': MAC}}}}

    def config(self, mac=MAC):
        return {'name': 'custom-project', 'services': {'app': {'networks': {'lan': {'mac_address': mac}}}},
                'networks': {'lan': {'name': 'real-net', 'external': True}}}

    def invoke(self, containers, configs):
        commands = []
        def output(cmd, cwd=None, **kwargs):
            commands.append((cmd, cwd))
            if cmd[:2] == ['docker', 'ps']:
                return 'a b' if containers else ''
            if cmd[:2] == ['docker', 'inspect']:
                return json.dumps(containers)
            value = configs.pop(0)
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, str) else json.dumps(value)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(check.subprocess, 'check_output', side_effect=output), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = check.main()
        return rc, stdout.getvalue() + stderr.getvalue(), commands

    def test_ordered_files_project_workdir_and_deduplication(self):
        rc, out, commands = self.invoke([self.container(), self.container()], [self.config()])
        self.assertEqual(rc, 0, out)
        compose = [(cmd, cwd) for cmd, cwd in commands if cmd[:2] == ['docker', 'compose']]
        self.assertEqual(len(compose), 1)
        cmd, cwd = compose[0]
        self.assertEqual(cwd, str(self.work))
        self.assertEqual(cmd[cmd.index('-p') + 1], 'custom-project')
        self.assertEqual([cmd[i + 1] for i, arg in enumerate(cmd) if arg == '-f'], list(map(str, self.files)))
        self.assertEqual(cmd[cmd.index('--project-directory') + 1], str(self.work))

    def test_override_mac_is_checked(self):
        rc, out, _ = self.invoke([self.container()], [self.config('02:42:ac:11:00:03')])
        self.assertEqual(rc, 2, out)
        self.assertIn('MISMATCH', out)

    def test_failed_config_is_nonzero_and_redacted(self):
        failure = subprocess.CalledProcessError(1, ['docker'], output='password=SECRET')
        rc, out, _ = self.invoke([self.container()], [failure])
        self.assertEqual(rc, 1, out)
        self.assertIn('config failed', out)
        self.assertNotIn('SECRET', out)
        self.assertNotIn('no compose', out)

    def test_invalid_json_and_schema_fail(self):
        for cfg in ('SECRET invalid', '[]', '{}', '{"services": []}', '{"services": {"app": {"networks": []}}}'):
            with self.subTest(cfg=cfg):
                rc, out, _ = self.invoke([self.container()], [cfg])
                self.assertEqual(rc, 1, out)
                self.assertNotIn('SECRET', out)

    def test_partial_project_failure_fails_run(self):
        rc, out, commands = self.invoke([self.container(), self.container(project='other')],
                                        [self.config(), subprocess.CalledProcessError(1, ['docker'])])
        self.assertEqual(rc, 1, out)
        self.assertEqual(sum(cmd[:2] == ['docker', 'compose'] for cmd, _ in commands), 2)
        self.assertNotIn('MAC-CHECK: OK', out)

    def test_no_fixed_mac_succeeds(self):
        rc, out, _ = self.invoke([self.container()], [self.config('')])
        self.assertEqual(rc, 0, out)
        self.assertIn('no compose mac_address', out)

    def test_no_compose_containers_succeeds(self):
        for containers in ([], [{'Config': {'Labels': {}}}]):
            self.assertEqual(self.invoke(containers, [])[0], 0)

    def test_missing_service_fails_coverage(self):
        self.assertEqual(self.invoke([self.container(service='missing')], [self.config()])[0], 1)

    def test_missing_project_context_fails(self):
        for label in ('project', 'project.working_dir', 'project.config_files', 'service'):
            c = self.container()
            del c['Config']['Labels']['com.docker.compose.' + label]
            self.assertEqual(self.invoke([c], [self.config()])[0], 1)

    def test_root_sibling_is_excluded(self):
        sibling = Path(str(self.root) + '-other') / 'custom.yaml'
        c = self.container(files=[sibling])
        c['Config']['Labels']['com.docker.compose.project.working_dir'] = str(sibling.parent)
        rc, _, commands = self.invoke([c], [])
        self.assertEqual(rc, 0)
        self.assertFalse(any(cmd[:2] == ['docker', 'compose'] for cmd, _ in commands))

    def test_comma_path_and_empty_component_fail(self):
        comma = self.root / 'base,override.yaml'
        comma.write_text('services: {}')
        for files in ([comma], [self.files[0]]):
            c = self.container(files=files)
            if files != [comma]:
                c['Config']['Labels']['com.docker.compose.project.config_files'] += ','
            self.assertEqual(self.invoke([c], [self.config()])[0], 1)

    def test_network_name_fallback_uses_project(self):
        cfg = self.config()
        cfg['networks']['lan'] = {}
        c = self.container()
        c['NetworkSettings']['Networks']['custom-project_lan'] = {'MacAddress': MAC.upper()}
        self.assertEqual(self.invoke([c], [cfg])[0], 0)

    def test_missing_endpoint_is_mismatch(self):
        c = self.container()
        c['NetworkSettings']['Networks'] = {}
        self.assertEqual(self.invoke([c], [self.config()])[0], 2)

    def test_service_mac_single_network(self):
        cfg = self.config('')
        cfg['services']['app']['mac_address'] = MAC
        rc, out, _ = self.invoke([self.container()], [cfg])
        self.assertEqual(rc, 0, out)
        self.assertIn('1 endpoints checked', out)

    def test_recorded_env_files_restored(self):
        c = self.container()
        envfile = self.work / 'custom env'
        envfile.write_text('X=1')
        c['Config']['Labels']['com.docker.compose.project.environment_file'] = str(envfile)
        rc, out, commands = self.invoke([c], [self.config()])
        self.assertEqual(rc, 0, out)
        cmd = commands[-1][0]
        self.assertEqual(cmd[cmd.index('--env-file') + 1], str(envfile))

    def test_interpolation_warning_fails_without_leaking(self):
        rc, out, _ = self.invoke([self.container()], ['warning SECRET unset\n' + json.dumps(self.config())])
        self.assertEqual(rc, 1, out)
        self.assertNotIn('SECRET', out)

    def test_service_mac_uses_highest_priority_network(self):
        cfg = self.config('')
        cfg['services']['app']['mac_address'] = MAC
        cfg['services']['app']['networks'] = {'lan': {'priority': 100}, 'second': {'priority': 0}}
        rc, out, _ = self.invoke([self.container()], [cfg])
        self.assertEqual(rc, 0, out)
        self.assertIn('1 endpoints checked', out)

    def test_service_mac_priority_tie_uses_network_key_order(self):
        cfg = self.config('')
        cfg['services']['app']['mac_address'] = MAC
        cfg['services']['app']['networks'] = {'z-last': {}, 'lan': {}}
        self.assertEqual(self.invoke([self.container()], [cfg])[0], 0)

    def test_service_mac_preserves_other_endpoint_mac(self):
        cfg = self.config()
        cfg['services']['app']['mac_address'] = MAC
        cfg['services']['app']['networks']['lan']['priority'] = 100
        other_mac = '02:42:ac:11:00:03'
        cfg['services']['app']['networks']['second'] = {'mac_address': other_mac}
        c = self.container()
        c['NetworkSettings']['Networks']['custom-project_second'] = {'MacAddress': other_mac}
        rc, out, _ = self.invoke([c], [cfg])
        self.assertEqual(rc, 0, out)
        self.assertIn('2 endpoints checked', out)

    def test_service_mac_conflicting_main_endpoint_fails(self):
        cfg = self.config('02:42:ac:11:00:03')
        cfg['services']['app']['mac_address'] = MAC
        self.assertEqual(self.invoke([self.container()], [cfg])[0], 1)

    def test_oneoff_container_is_excluded(self):
        c = self.container(service='temporary-command')
        c['Config']['Labels']['com.docker.compose.oneoff'] = 'True'
        rc, _, commands = self.invoke([c], [])
        self.assertEqual(rc, 0)
        self.assertFalse(any(cmd[:2] == ['docker', 'compose'] for cmd, _ in commands))

    def test_all_scaled_replicas_are_checked(self):
        first, second = self.container(), self.container()
        second['Name'] = '/app-2'
        second['NetworkSettings']['Networks']['real-net']['MacAddress'] = '02:42:ac:11:00:03'
        rc, out, commands = self.invoke([first, second], [self.config()])
        self.assertEqual(rc, 2, out)
        self.assertIn('app-2 real-net', out)
        self.assertEqual(sum(cmd[:2] == ['docker', 'compose'] for cmd, _ in commands), 1)

    def test_recorded_relative_environment_cannot_establish_launch_cwd(self):
        (self.work / 'launch.env').write_text('X=1')
        c = self.container()
        c['Config']['Labels']['com.docker.compose.project.environment_file'] = 'launch.env'
        rc, _, commands = self.invoke([c], [])
        self.assertEqual(rc, 1)
        self.assertFalse(any(cmd[:2] == ['docker', 'compose'] for cmd, _ in commands))

    def test_profiled_services_rendered_but_only_running_services_checked(self):
        cfg = self.config()
        cfg['services']['stopped'] = {'profiles': ['optional'], 'networks': {'lan': {'mac_address': '02:42:ac:11:00:03'}}}
        rc, out, commands = self.invoke([self.container()], [cfg])
        self.assertEqual(rc, 0, out)
        cmd = commands[-1][0]
        self.assertEqual(cmd[cmd.index('--profile') + 1], '*')

    def test_unavailable_recorded_environment_fails(self):
        c = self.container()
        c['Config']['Labels']['com.docker.compose.project.environment_file'] = 'missing.env'
        rc, _, commands = self.invoke([c], [])
        self.assertEqual(rc, 1)
        self.assertFalse(any(cmd[:2] == ['docker', 'compose'] for cmd, _ in commands))

    def test_default_network_service_mac(self):
        cfg = {'services': {'app': {'mac_address': MAC}}}
        c = self.container()
        c['NetworkSettings']['Networks'] = {'custom-project_default': {'MacAddress': MAC}}
        self.assertEqual(self.invoke([c], [cfg])[0], 0)

    def test_inspect_failure_is_redacted(self):
        with patch.object(check.subprocess, 'check_output', side_effect=subprocess.CalledProcessError(1, ['docker'], output='SECRET')):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(check.main(), 1)
            self.assertNotIn('SECRET', stderr.getvalue())

    def test_wrapper_propagates_check_failure(self):
        # Exercise the real wrapper with sandboxed commands, never Docker/update.
        wrapper = REPO / 'assets/docker-auto-update/docker-auto-update.sh'
        base = self.root / 'wrapper'
        base.mkdir()
        (base / 'docker-auto-update.sh').write_bytes(wrapper.read_bytes())
        (base / 'dockcheck.sh').write_text('#!/bin/bash\nexit 0\n')
        (base / 'check-compose-macs.py').write_text('#!/bin/bash\necho MAC-CHECK: failed\nexit 1\n')
        bin_dir = base / 'bin'
        bin_dir.mkdir()
        for name, body in {'setsid': 'exec "$@"', 'flock': 'exit 0',
                           'date': 'case "$1" in -Is) echo 2026-10-18T00:00:00Z;; *) /bin/date "$@";; esac',
                           'sleep': '/bin/sleep 0.01'}.items():
            (bin_dir / name).write_text('#!/bin/bash\n' + body + '\n')
        for file in [base / 'dockcheck.sh', base / 'check-compose-macs.py', *bin_dir.iterdir()]:
            file.chmod(0o755)
        # macOS ships Bash 3 without mapfile; this fixture has no failed
        # recreates, so provide only the empty-input compatibility behavior.
        bash_env = base / 'bash-env'
        bash_env.write_text('if ! type mapfile >/dev/null 2>&1; then\nmapfile() { eval "$2=()"; }; fi\n')
        env = dict(os.environ, BASH_ENV=str(bash_env), PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], ROOT_DIR=str(self.root),
                   BASE_DIR=str(base), CONFIG_FILE=str(base / 'absent.conf'), HEARTBEAT_INTERVAL='1')
        for expected_rc in (1, 2, 0):
            with self.subTest(expected_rc=expected_rc):
                (base / 'check-compose-macs.py').write_text(
                    '#!/bin/bash\necho MAC-CHECK: fixture\nexit ' + str(expected_rc) + '\n')
                result = subprocess.run(['bash', str(base / wrapper.name), '--check-only'], env=env,
                                        capture_output=True, text=True, errors='replace', timeout=10)
                self.assertEqual(result.returncode, expected_rc, result.stdout + result.stderr)
                self.assertIn('MAC-CHECK: fixture', result.stdout)
                self.assertEqual('auto update done' in result.stdout, expected_rc == 0)
                if expected_rc:
                    self.assertIn('退出码 ' + str(expected_rc), result.stdout)
                self.assertFalse((base / '.docker-auto-update.running').exists())


if __name__ == '__main__':
    unittest.main()
