"""Run real RTSP Bash entry points with only sandbox service/network commands."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'assets/iptv/rtp2httpd.sh'


class RTSPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rtsp-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.log = self.base / 'services'
        for name, text in {
            'systemctl': '''#!/bin/bash
echo "$*" >> "$LOG"
case "$*" in
*--property=ActiveState*) echo "${ACTIVE:-active}"; exit 0;;
stop*) echo inactive > "$LOG.state"; exit 0;;
is-active*) [ ! -e "$LOG.state" ] || [ "$(< "$LOG.state")" = active ]; exit $?;;
*--property=DropInPaths*) echo "$DROPINS"; exit 0;;
cat*) printf '%s\\n' "$UNIT"; exit 0;;
restart*)
 if [ "$SIGNAL" = yes ] && [ ! -e "$LOG.once" ]; then
  touch "$LOG.once"; kill -TERM "$PPID"; exit 0
 fi;;
esac
if [ "$FAIL" = yes ]; then exit 1; fi
''',
            'nmcli': '#!/bin/bash\necho forbidden >> "$LOG"\nexit 99\n',
            'ip': '#!/bin/bash\ncase "$*" in\n"-o link show") printf "1: lo: flags\\n2: wan.7@wan: flags\\n3: media: flags\\n";;\n"link show dev wan.7"|"link show dev media") exit 0;;\n*) exit 1;; esac\n',
        }.items():
            p = self.bin / name
            p.write_text(text)
            p.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{self.bin}:' + os.environ['PATH'], LOG=str(self.log))
        self.conf = self.base / 'rtp2httpd_demo.conf'
        self.state = self.conf.with_suffix('.env')
        self.original = '# keep\n[global]\nverbosity = 4\nupstream-interface = media\nupstream-interface-fcc = wan.7\n# upstream-interface-rtsp = old\nupstream-interface-rtsp = media # operator\n\n[bind]\n127.0.0.1 5140\n'
        self.conf.write_text(self.original)
        self.env['UNIT'] = f'[Service]\nExecStart=/opt/rtp2httpd -c {self.conf}'
        self.state.write_text('# custom\nFCC_PROFILE_UUID=unchanged\nCUSTOM=$(touch NEVER)\n')
        self.conf.chmod(0o640)
        self.state.chmod(0o600)

    def run_shell(self, body, stdin=''):
        return subprocess.run(['bash', '-c', f'source {shlex.quote(str(SCRIPT))}\n{body}'],
                              input=stdin, env=self.env, text=True, capture_output=True)

    def apply(self, iface='wan.7'):
        return self.run_shell('rtp2httpd_apply_rtsp ' + ' '.join(map(shlex.quote,
            ['rtp2httpd_demo.service', str(self.conf), str(self.state), iface])))

    def test_exact_update_metadata_and_isolation(self):
        peer = self.base / 'rtp2httpd_peer.conf'
        peer.write_text('peer')
        before = self.conf.stat()
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.conf.read_text(), self.original.replace('= media # operator', '= wan.7 # operator'))
        self.assertEqual(self.state.read_text(), '# custom\nFCC_PROFILE_UUID=unchanged\nCUSTOM=$(touch NEVER)\nRTSP_IF=wan.7\n')
        self.assertEqual(peer.read_text(), 'peer')
        self.assertEqual(self.conf.stat().st_mode, before.st_mode)
        self.assertEqual((self.conf.stat().st_uid, self.conf.stat().st_gid), (before.st_uid, before.st_gid))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.log.read_text(), 'show --property=ActiveState --value rtp2httpd_demo.service\nrestart rtp2httpd_demo.service\nis-active --quiet rtp2httpd_demo.service\n')
        self.assertTrue(list(self.base.glob('*.bak-*')))

    def test_route_retains_general_and_reports_fallback(self):
        r = self.apply('')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('upstream-interface = media', self.conf.read_text())
        self.assertNotIn('\nupstream-interface-rtsp =', self.conf.read_text())
        self.assertIn('media', r.stdout)
        self.assertIn('RTSP_IF=\n', self.state.read_text())

    def test_missing_state_supported(self):
        self.state.unlink()
        self.assertEqual(self.apply().returncode, 0)
        self.assertEqual(self.state.read_text(), 'RTSP_IF=wan.7\n')
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_invalid_or_missing_interface_keeps_files(self):
        for iface in ['absent', 'x;touch BAD', '../media', 'abcdefghijklmnop']:
            with self.subTest(iface=iface):
                self.assertNotEqual(self.apply(iface).returncode, 0)
                self.assertEqual(self.conf.read_text(), self.original)
                self.assertFalse(self.log.exists())

    def test_ambiguous_config_or_state_fails_without_mutation(self):
        for text in [self.original + '[global]\n', self.original.replace('verbosity = 4', 'verbosity = 4\nverbosity = 5'),
                     self.original.replace('verbosity = 4', 'bad syntax'), self.original.replace('[global]', '[GLOBAL]')]:
            with self.subTest(text=text):
                self.conf.write_text(text)
                self.assertNotEqual(self.apply().returncode, 0)
                self.assertEqual(self.conf.read_text(), text)
                self.assertFalse(self.log.exists())
        self.conf.write_text(self.original)
        self.state.write_text('RTSP_IF=media\nRTSP_IF=wan.7\n')
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertEqual(self.conf.read_text(), self.original)

    def test_restart_failure_rolls_back_both_files(self):
        self.env['FAIL'] = 'yes'
        old_state = self.state.read_bytes()
        r = self.apply()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertEqual(self.state.read_bytes(), old_state)
        self.assertEqual(self.log.read_text().splitlines(), ['show --property=ActiveState --value rtp2httpd_demo.service', 'restart rtp2httpd_demo.service', 'restart rtp2httpd_demo.service', 'is-active --quiet rtp2httpd_demo.service'])
        self.assertIn('恢复', r.stdout + r.stderr)

    def test_status_never_sources_state(self):
        r = self.run_shell(f'rtp2httpd_rtsp_config status {shlex.quote(str(self.conf))} {shlex.quote(str(self.state))}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('media', r.stdout)
        self.assertIn('r2h-ifname', r.stdout)
        self.assertFalse((ROOT / 'NEVER').exists())

    def test_four_install_choices_render_whole_line(self):
        for choice, expected in [('1\n', 'wan.7'), ('2\n', 'media'), ('3\nwan.7\n', 'wan.7'), ('4\n', '')]:
            with self.subTest(choice=choice):
                dst = self.base / 'rendered'
                body = f'''rtp2httpd_prompt_rtsp wan.7 media || exit $?
rtp2httpd_render_template {ROOT}/assets/iptv/rtp2httpd.conf.tpl {dst} "__RTSP_INTERFACE_CONFIG__=$(rtp2httpd_rtsp_line)"
'''
                r = self.run_shell(body, choice)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                text = dst.read_text()
                self.assertNotIn('__RTSP_INTERFACE_CONFIG__', text)
                self.assertIn('upstream-interface-rtsp = ' + expected if expected else '# RTSP:', text)

    def test_generic_settings_changes_only_selected_field(self):
        body = f'''rtp2httpd_list_instances() {{ RTP2HTTPD_LIST_SERVICES=(rtp2httpd_demo.service rtp2httpd_peer.service); }}
rtp2httpd_service_config_path() {{ printf '%s\\n' {shlex.quote(str(self.conf))}; }}
rtp2httpd_settings'''
        old_state = self.state.read_bytes()
        r = self.run_shell(body, '1\n2\n6000\ny\n')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('RTSP 接口', r.stdout)
        self.assertIn('FCC 接口', r.stdout)
        self.assertIn('监听地址', r.stdout)
        self.assertEqual(self.conf.read_text(), self.original.replace('127.0.0.1 5140', '127.0.0.1 6000'))
        self.assertEqual(self.state.read_bytes(), old_state)
        self.assertNotIn('peer', self.log.read_text())
        self.assertNotIn('forbidden', self.log.read_text())

    def test_generic_settings_rtsp_reuses_current_fcc(self):
        body = f'''rtp2httpd_list_instances() {{ RTP2HTTPD_LIST_SERVICES=(rtp2httpd_demo.service); }}
rtp2httpd_service_config_path() {{ printf '%s\\n' {shlex.quote(str(self.conf))}; }}
rtp2httpd_settings'''
        r = self.run_shell(body, '1\n5\n1\ny\n')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.conf.read_text(), self.original.replace('= media # operator', '= wan.7 # operator'))

    def test_generic_other_fields_preserve_unselected_bytes(self):
        for field, value, old, new in [('address', '127.0.0.2', '127.0.0.1 5140', '127.0.0.2 5140'),
                                      ('fcc', 'media', 'upstream-interface-fcc = wan.7', 'upstream-interface-fcc = media'),
                                      ('multicast', 'wan.7', '[global]\n', '[global]\nupstream-interface-multicast = wan.7\n')]:
            with self.subTest(field=field):
                self.conf.write_text(self.original)
                before = self.state.read_bytes()
                r = self.run_shell('rtp2httpd_rtsp_config apply ' + ' '.join(map(shlex.quote,
                    [str(self.conf), str(self.state), 'rtp2httpd_demo.service', field, value])))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.conf.read_text(), self.original.replace(old, new))
                self.assertEqual(self.state.read_bytes(), before)

    def test_missing_env_rollback_removes_new_state(self):
        self.state.unlink()
        self.env['FAIL'] = 'yes'
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.conf.read_text(), self.original)

    def test_symlink_state_fails_before_service_or_config_change(self):
        target = self.base / 'target'
        target.write_text('keep')
        self.state.unlink()
        self.state.symlink_to(target)
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertEqual(target.read_text(), 'keep')
        self.assertFalse(self.log.exists())

    def test_same_name_install_uses_settings_without_network_rebuild(self):
        app = self.base / 'rtp2httpd'
        app.mkdir()
        conf = app / 'rtp2httpd_demo.conf'
        state = conf.with_suffix('.env')
        conf.write_text(self.original)
        state.write_text('FCC_PROFILE_UUID=keep\n')
        self.env['UNIT'] = f'[Service]\nExecStart=/opt/rtp2httpd -c {conf}'
        body = f'''rtp2httpd_require_commands() {{ :; }}
select_dockerapps_dir() {{ SELECTED_DOCKERAPPS_DIR={shlex.quote(str(self.base))}; }}
rtp2httpd_install'''
        r = self.run_shell(body, 'demo\n2\n6001\ny\n')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(conf.read_text(), self.original.replace('127.0.0.1 5140', '127.0.0.1 6001'))
        self.assertEqual(state.read_text(), 'FCC_PROFILE_UUID=keep\n')
        self.assertNotIn('forbidden', self.log.read_text())
        self.assertNotIn('6）', r.stdout)

    def test_env_rtsp_comment_preserved(self):
        self.state.write_text('# keep\nRTSP_IF=media # operator\nUUID=keep\n')
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.state.read_text(), '# keep\nRTSP_IF=wan.7 # operator\nUUID=keep\n')

    def test_signal_rolls_back_and_restores_active_service(self):
        self.env['SIGNAL'] = 'yes'
        before = self.state.read_bytes()
        r = self.apply()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.log.read_text().count('restart rtp2httpd_demo.service'), 2)
        self.assertIn('恢复', r.stderr)

    def test_inactive_service_stays_stopped_after_failure(self):
        self.env.update(ACTIVE='inactive', FAIL='yes')
        r = self.apply()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertIn('stop rtp2httpd_demo.service', self.log.read_text())
        self.assertEqual(Path(str(self.log) + '.state').read_text().strip(), 'inactive')
        self.assertNotIn('恢复运行状态失败', r.stderr)
        self.assertEqual(self.log.read_text().count('restart rtp2httpd_demo.service'), 1)

    def test_execstart_safe_parser_rejects_ambiguity(self):
        body = 'rtp2httpd_service_config_path rtp2httpd_demo.service'
        self.assertEqual(self.run_shell(body).stdout.strip(), str(self.conf))
        for unit in [self.env['UNIT'] + '\nExecStart=/other/rtp2httpd -c /other.conf',
                     '[Service]\nExecStart=/opt/rtp2httpd -c /tmp/%i.conf',
                     '[Service]\nExecStart=/opt/$BIN/rtp2httpd -c /tmp/a.conf',
                     '[Service]\nExecStart=/opt/rtp2httpd -c /tmp/a.conf --extra',
                     '[Service]\nExecStart=/bin/sh -c whatever',
                     '[Unit]\nExecStart=/opt/rtp2httpd -c /tmp/a.conf']:
            self.env['UNIT'] = unit
            self.assertNotEqual(self.run_shell(body).returncode, 0, unit)
        self.env['UNIT'] = f'[Service]\nExecStart=/opt/rtp2httpd -c {self.conf}'
        self.env['DROPINS'] = '/etc/systemd/system/demo.d/override.conf'
        self.assertNotEqual(self.run_shell(body).returncode, 0)
        self.assertEqual(self.conf.read_text(), self.original)

    def test_partial_replace_failure_restores_files_and_directory_metadata(self):
        # Inject one filesystem error in the actual embedded production Python.
        hook = self.base / 'hooks'
        hook.mkdir()
        (hook / 'sitecustomize.py').write_text('''import os
_real = os.replace
_failed = False
def replace(src, dst):
    global _failed
    if str(dst).endswith('.env') and not _failed:
        _failed = True
        raise OSError('injected state replacement failure')
    return _real(src, dst)
os.replace = replace
''')
        self.env['PYTHONPATH'] = str(hook)
        before = self.base.stat()
        env_bytes = self.state.read_bytes()
        r = self.apply()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertEqual(self.state.read_bytes(), env_bytes)
        self.assertEqual((self.base.stat().st_mode, self.base.stat().st_uid, self.base.stat().st_gid),
                         (before.st_mode, before.st_uid, before.st_gid))
        self.assertIn('injected', r.stderr)
        self.assertFalse(list(self.base.glob('.rtp2httpd*')))

    def test_no_implicit_choice(self):
        r = self.run_shell('rtp2httpd_prompt_rtsp "" ""', '\n')
        self.assertNotEqual(r.returncode, 0)


if __name__ == '__main__':
    unittest.main()
