"""Sandbox regressions for installer status, OpenWrt dependencies and Compose validation."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = os.environ.get('TMPDIR')
INSTALL = (ROOT / 'install.sh').read_text()


def function(name, text=INSTALL):
    match = re.search(r'^' + re.escape(name) + r'\(\) \{.*?^\}\n', text, re.M | re.S)
    if not match:
        raise AssertionError('function missing: ' + name)
    return match.group()


def run(code, data='', env=None):
    return subprocess.run(['bash', '-c', code], input=data, text=True,
                          capture_output=True, timeout=5, env=env)


class VLANTests(unittest.TestCase):
    code = function('rtp2httpd_prompt_vlan', (ROOT / 'assets/iptv/rtp2httpd.sh').read_text())

    def test_retry_has_only_numeric_stdout(self):
        r = run(self.code + '\nv=$(rtp2httpd_prompt_vlan prompt); rc=$?; printf "%s" "$v"; exit "$rc"',
                'bad\n0\n4095\n85\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, '85')
        self.assertEqual(r.stderr.count('VLAN ID'), 3)

    def test_eof_and_empty_cancel(self):
        for data in ['', '\n', 'bad\n', '51']:
            with self.subTest(data=data):
                r = run(self.code + '\nrtp2httpd_prompt_vlan prompt', data)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(r.stdout, '')


    def test_install_stops_on_vlan_eof_before_download_or_network_changes(self):
        source = (ROOT / 'assets/iptv/rtp2httpd.sh').read_text()
        with tempfile.TemporaryDirectory(prefix='vlan-eof-', dir=SCRATCH) as temp:
            code = self.code + function('rtp2httpd_install', source) + '''
rtp2httpd_require_commands() { :; }
rtp2httpd_list_instances() { RTP2HTTPD_LIST_SERVICES=(); }
select_dockerapps_dir() { SELECTED_DOCKERAPPS_DIR="$SANDBOX"; }
rtp2httpd_prompt_instance() {
    RTP2HTTPD_INSTANCE=test
    RTP2HTTPD_CONFIG_PATH="$SANDBOX/config"
    RTP2HTTPD_STATE_PATH="$SANDBOX/state"
    RTP2HTTPD_SERVICE_PATH="$SANDBOX/unit"
}
rtp2httpd_select_parent() { RTP2HTTPD_PARENT_IF=eth0; }
rtp2httpd_prompt_fcc_ipv4() { echo UNEXPECTED; return 99; }
rtp2httpd_download_binary() { echo UNEXPECTED; return 99; }
nmcli() { echo UNEXPECTED; return 99; }
rtp2httpd_install
'''
            for data in ['', '51\n']:
                r = run(code, data, env=dict(os.environ, SANDBOX=temp))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertNotIn('UNEXPECTED', r.stdout + r.stderr)
                self.assertFalse((Path(temp) / 'rtp2httpd').exists())


class InstallerStatusTests(unittest.TestCase):
    def entry(self):
        return INSTALL[INSTALL.index('case "${1:-}" in'):INSTALL.index('\ncheck_yehbp_update\n')]

    def test_install_and_pipe_status(self):
        for arg in ['', 'install', '--install']:
            for rc in [0, 37]:
                with self.subTest(arg=arg, rc=rc):
                    r = subprocess.run(['bash', '-c', f'install_yehbp_cli() {{ return {rc}; }}\n' + self.entry(),
                                        'bash', *([arg] if arg else [])], input='remaining pipe data',
                                       text=True, capture_output=True, timeout=5)
                    self.assertEqual(r.returncode, rc, r.stderr)

    def test_real_stdin_script_preserves_status_and_drains_tail(self):
        for arg in [[], ['install']]:
            for rc in [0, 37]:
                script = f'install_yehbp_cli() {{ return {rc}; }}\n' + self.entry()
                script += '\necho SHOULD_NOT_EXECUTE\n' * 2000
                r = subprocess.run(['bash', '-s', '--', *arg], input=script, text=True,
                                   capture_output=True, timeout=5)
                self.assertEqual(r.returncode, rc, r.stderr)
                self.assertNotIn('SHOULD_NOT_EXECUTE', r.stdout)

    def test_explicit_install_does_not_wait_for_input(self):
        p = subprocess.Popen(['bash', '-c', 'install_yehbp_cli() { return 37; }\n' + self.entry(),
                              'install.sh', 'install'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(p.wait(timeout=1), 37)
        finally:
            if p.poll() is None:
                p.kill()
            p.communicate()


class DependencyTests(unittest.TestCase):
    def code(self, manager, update=0, install=0, missing='curl jq tar python3'):
        helpers = ''.join(function(n) for n in ['openwrt_package_manager', 'install_openwrt_packages']
                          if re.search(r'^' + n + r'\(\)', INSTALL, re.M))
        return helpers + function('install_dependencies') + function('install_python3') + f'''
PLATFORM=openwrt
is_openwrt() {{ [ "$PLATFORM" = openwrt ]; }}
command() {{
    if [ "$1" = -v ]; then
        case " {missing} " in *" $2 "*) return 1 ;; esac
        case "$2" in apk|opkg) [ "$2" = "{manager}" ]; return ;; esac
    fi
    builtin command "$@"
}}
apk() {{ printf 'apk:%s\\n' "$*" >&2; [ "$1" = update ] && return {update}; return {install}; }}
opkg() {{ printf 'opkg:%s\\n' "$*" >&2; [ "$1" = update ] && return {update}; return {install}; }}
'''

    def test_base_packages_both_managers(self):
        for manager, action in [('apk', 'add'), ('opkg', 'install')]:
            r = run(self.code(manager) + '\ninstall_dependencies')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stderr.splitlines(), [f'{manager}:update', f'{manager}:{action} curl jq tar'])

    def test_update_failure_never_installs(self):
        for manager in ['apk', 'opkg']:
            for target in ['install_dependencies', 'install_python3']:
                r = run(self.code(manager, update=31) + '\n' + target)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(r.stderr.splitlines(), [f'{manager}:update'])

    def test_install_failure_propagates_even_if_python_appears(self):
        for manager, action in [('apk', 'add'), ('opkg', 'install')]:
            for target in ['install_dependencies', 'install_python3']:
                code = self.code(manager, install=32)
                # A broken manager can leave a runnable binary; exit status remains authoritative.
                code = code.replace(f'return 32; }}', 'command() { return 0; }; return 32; }')
                r = run(code + '\n' + target)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(f'{manager}:{action}', r.stderr)

    def test_python_success_and_missing_manager(self):
        for manager, action in [('apk', 'add'), ('opkg', 'install')]:
            code = self.code(manager)
            # Return python availability only after the manager has run.
            code += f'\n{manager}() {{ printf "{manager}:%s\\n" "$*" >&2; if [ "$1" != update ]; then command() {{ return 0; }}; fi; }}\n'
            r = run(code + 'install_python3')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f'{manager}:{action} python3', r.stderr)
        r = run(self.code('none') + '\ninstall_dependencies')
        self.assertNotEqual(r.returncode, 0)

    def test_existing_dependencies_do_not_invoke_manager(self):
        r = run(self.code('none', missing='') + '\ninstall_dependencies; install_python3')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, '')

    def test_debian_python_and_base_keep_apt(self):
        code = self.code('none') + '''
PLATFORM=debian
apt-get() { printf 'apt:%s\\n' "$*" >&2; if [ "$1" = install ]; then command() { return 0; }; fi; }
'''
        r = run(code + 'install_dependencies; install_python3')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.splitlines(), ['apt:update', 'apt:install -y ipcalc curl jq tar'])
        r = run(code + 'install_python3')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.splitlines(), ['apt:update', 'apt:install -y python3'])

    def test_menu_does_not_run_after_failed_dependencies(self):
        tail = INSTALL[INSTALL.index('# ========== 主循环 =========='):]
        r = run('install_dependencies() { return 41; }; show_menu() { echo UNEXPECTED; };\n' + tail, '99\n')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('UNEXPECTED', r.stdout)


class ComposeValidationTests(unittest.TestCase):
    def test_private_cleanup_success_failure_retry_and_signals(self):
        self.assertNotIn('/tmp/"$name".compose.', function('compose_deploy_with_repo_switch'))
        for mode in ['success', 'failure', 'retry', 'TERM', 'INT', 'HUP']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='compose-check-', dir=SCRATCH) as temp:
                base = Path(temp)
                fake = base / 'docker'
                fake.write_text(f'''#!{sys.executable}
import json, os, signal, sys
from pathlib import Path
base = Path(os.environ['SANDBOX'])
a = sys.argv[1:]
if a == ['compose', 'version']: sys.exit(0)
if a[-1] != 'config': sys.exit(98)
paths = list(base.glob('yehbp-compose.*/*'))
with (base / 'observed').open('a') as f:
    f.write(json.dumps([[str(p), p.stat().st_mode & 0o777, p.parent.stat().st_mode & 0o777] for p in paths]) + '\\n')
print('SENTINEL_CONFIG_SECRET')
print('SENTINEL_ERROR_SECRET', file=sys.stderr)
mode = os.environ['MODE']
if mode in ['TERM', 'INT', 'HUP']: os.kill(os.getppid(), getattr(signal, 'SIG' + mode)); sys.exit(1)
if mode == 'failure': sys.exit(1)
if mode == 'retry' and not (base / 'retried').exists(): sys.exit(1)
sys.exit(0)
''')
                fake.chmod(0o755)
                helpers = function('validate_compose_config') if 'validate_compose_config()' in INSTALL else ''
                code = helpers + function('compose_deploy_with_repo_switch') + '''
remove_unsupported_compose_endpoint_sysctls() { :; }
remove_incompatible_host_time_mounts() { :; }
convert_compose_network_mac_for_legacy_docker() { :; }
yehbp_prefetch_compose_images() { echo PREFETCH; return 77; }
apply_compose_schema_compatibility_fallback() {
    [ "$MODE" = retry ] || return 1
    grep -q SENTINEL_ERROR_SECRET "$1" || return 1
    touch "$SANDBOX/retried"
}
trap 'echo CALLER_EXIT' EXIT
trap 'echo CALLER_TERM' TERM
before_traps=$(trap -p); before_umask=$(umask)
WORK_DIR="$SANDBOX"
compose_deploy_with_repo_switch safe compose.yaml
rc=$?
[ "$before_traps" = "$(trap -p)" ] && [ "$before_umask" = "$(umask)" ] || exit 99
exit "$rc"
'''
                env = dict(os.environ, PATH=str(base) + ':' + os.environ['PATH'],
                           TMPDIR=str(base), SANDBOX=str(base), MODE=mode)
                r = run(code, env=env)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual('PREFETCH' in r.stdout, mode in ['success', 'retry'])
                self.assertNotIn('SENTINEL', r.stdout + r.stderr)
                self.assertNotIn('CALLER_TERM', r.stdout)
                self.assertIn('CALLER_EXIT', r.stdout)
                observed = [json.loads(x) for x in (base / 'observed').read_text().splitlines()]
                self.assertTrue(all(len(batch) == 1 for batch in observed), observed)
                for batch in observed:
                    for path, filemode, dirmode in batch:
                        self.assertEqual(filemode & 0o077, 0)
                        self.assertEqual(dirmode & 0o077, 0)
                        self.assertFalse(Path(path).exists())
                self.assertEqual(list(base.glob('yehbp-compose.*')), [])
                self.assertEqual(len(observed), 2 if mode == 'retry' else 1)


if __name__ == '__main__':
    unittest.main()
