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
cat*)
 if [ -n "$UNIT_DIR" ]; then /bin/cat "$UNIT_DIR/${@: -1}"; else printf '%s\\n' "$UNIT"; fi; exit 0;;
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
        self.assertEqual(r.stdout.splitlines(), [
            '✅ 已应用所选字段并重启 rtp2httpd_demo.service',
            'RTSP 接口（回放）：wan.7',
        ])

    def test_route_retains_general_and_reports_fallback(self):
        for general, expected in [('media', '未设置（沿用通用接口：media）'),
                                  ('', '未设置（沿用系统路由）')]:
            with self.subTest(general=general):
                self.conf.write_text(self.original.replace('upstream-interface = media', 'upstream-interface = ' + general))
                r = self.apply('')
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn('upstream-interface = ' + general + '\n', self.conf.read_text())
                self.assertNotIn('\nupstream-interface-rtsp =', self.conf.read_text())
                self.assertEqual(r.stdout.splitlines(), [
                    '✅ 已应用所选字段并重启 rtp2httpd_demo.service',
                    'RTSP 接口（回放）：' + expected,
                ])
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
        self.assertIn('  3）组播接口（直播）：未设置', r.stdout.splitlines())
        self.assertIn('  4）FCC 接口（快速换台）：wan.7', r.stdout.splitlines())
        self.assertIn('  5）RTSP 接口（回放）：media', r.stdout.splitlines())
        self.assertNotIn('RTSP 专用接口：', r.stdout)
        self.assertNotIn('优先级：URL', r.stdout)
        self.assertIn('监听地址', r.stdout)
        self.assertEqual(self.conf.read_text(), self.original.replace('127.0.0.1 5140', '127.0.0.1 6000'))
        self.assertEqual(self.state.read_bytes(), old_state)
        self.assertNotIn('peer', self.log.read_text())
        self.assertNotIn('forbidden', self.log.read_text())

    def test_generic_settings_rtsp_fallback_labels(self):
        for general, expected in [('media', '未设置（沿用通用接口：media）'),
                                  ('', '未设置（沿用系统路由）')]:
            with self.subTest(general=general):
                text = self.original.replace('upstream-interface-rtsp = media # operator\n', '')
                text = text.replace('upstream-interface = media', 'upstream-interface = ' + general)
                self.conf.write_text(text)
                before = self.state.read_bytes()
                r = self.run_install_menu('2\n1\n0\n')
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn('  5）RTSP 接口（回放）：' + expected, r.stdout.splitlines())
                self.assertNotIn('RTSP 专用接口：', r.stdout)
                self.assertNotIn('优先级：URL', r.stdout)
                self.assertEqual(self.conf.read_text(), text)
                self.assertEqual(self.state.read_bytes(), before)
                self.assertNotIn('restart', self.log.read_text())

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

    def run_install_menu(self, stdin, instances=('tel',), extra=''):
        units = self.base / 'units'
        units.mkdir(exist_ok=True)
        for instance in instances:
            (units / f'rtp2httpd_{instance}.service').touch()
        # Exercise the real filesystem discovery with only its systemd root relocated.
        asset = self.base / 'asset.sh'
        asset.write_text(SCRIPT.read_text().replace('/etc/systemd/system/', str(units) + '/'))
        installer = (ROOT / 'install.sh').read_text()
        start = installer.index('manage_rtp2httpd_menu() {')
        menu = installer[start:installer.index('\n}\n', start) + 3]
        body = f'''source {shlex.quote(str(asset))}
{menu}
load_rtp2httpd_asset() {{ :; }}
select_dockerapps_dir() {{ echo directory >> "$LOG"; SELECTED_DOCKERAPPS_DIR={shlex.quote(str(self.base))}; }}
rtp2httpd_require_commands() {{ echo require >> "$LOG"; }}
rtp2httpd_select_parent() {{ echo parent >> "$LOG"; return 2; }}
{extra}
manage_rtp2httpd_menu'''
        return self.run_shell(body, stdin)

    def test_menu_two_discovers_tel_and_edits_real_path_without_directory(self):
        peer = self.base / 'peer.conf'
        peer.write_text('unrelated service config')
        before = self.state.read_bytes()
        r = self.run_install_menu('2\n1\n2\n6002\ny\n')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('tel', r.stdout)
        self.assertIn('新建实例', r.stdout)
        self.assertIn('监听端口：5140', r.stdout)
        self.assertEqual(self.conf.read_text(), self.original.replace('127.0.0.1 5140', '127.0.0.1 6002'))
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(peer.read_text(), 'unrelated service config')
        log = self.log.read_text()
        for forbidden in ('directory', 'require', 'parent', 'forbidden', 'peer'):
            self.assertNotIn(forbidden, log)
        self.assertIn('restart rtp2httpd_tel.service', log)

    def test_menu_two_new_instance_and_first_install_select_directory(self):
        for instances, stdin in [(('tel',), '2\nn\nnew\n'), ((), '2\nnew\n')]:
            with self.subTest(instances=instances):
                for unit in (self.base / 'units').glob('*') if (self.base / 'units').exists() else []:
                    unit.unlink()
                self.log.unlink(missing_ok=True)
                r = self.run_install_menu(stdin, instances)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.log.read_text(), 'require\ndirectory\nparent\n')
                self.assertFalse((self.base / 'rtp2httpd').exists())

    def test_menu_two_new_instance_eof_at_name_does_not_write(self):
        for instances, stdin in [(('tel',), '2\nn\n'), ((), '2\n')]:
            with self.subTest(instances=instances):
                for unit in (self.base / 'units').glob('*') if (self.base / 'units').exists() else []:
                    unit.unlink()
                self.log.unlink(missing_ok=True)
                r = self.run_install_menu(stdin, instances)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.log.read_text(), 'require\ndirectory\n')
                self.assertFalse((self.base / 'rtp2httpd').exists())

    def test_menu_two_cancel_and_eof_do_nothing(self):
        for stdin in ('2\n0\n', '2\n\n', '2\n', '2\nx\n', '2\n99\n', '2\n1\n0\n'):
            with self.subTest(stdin=stdin):
                self.log.unlink(missing_ok=True)
                before = self.state.read_bytes()
                r = self.run_install_menu(stdin)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.conf.read_text(), self.original)
                self.assertEqual(self.state.read_bytes(), before)
                log = self.log.read_text() if self.log.exists() else ''
                for forbidden in ('directory', 'require', 'parent', 'restart', 'forbidden'):
                    self.assertNotIn(forbidden, log)
                self.assertFalse(list(self.base.glob('*.bak-*')))

    def test_menu_two_multiple_directories_resolve_selected_service(self):
        other = self.base / 'other'
        other.mkdir()
        conf = other / 'rtp2httpd_zeta.conf'
        conf.write_text(self.original.replace('5140', '6100'))
        units = self.base / 'units'
        units.mkdir()
        self.env['UNIT_DIR'] = str(units)
        (units / 'rtp2httpd_tel.service').write_text(self.env['UNIT'])
        (units / 'rtp2httpd_zeta.service').write_text(f'[Service]\nExecStart=/elsewhere/rtp2httpd -c {conf}')
        r = self.run_install_menu('2\n2\n2\n6101\ny\n', ('tel', 'zeta'))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('监听端口：6100', r.stdout)
        self.assertEqual(conf.read_text(), self.original.replace('5140', '6101'))
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertIn('restart rtp2httpd_zeta.service', self.log.read_text())
        self.assertNotIn('directory', self.log.read_text())

    def test_menu_two_new_same_name_still_edits_existing_config(self):
        app = self.base / 'rtp2httpd'
        app.mkdir()
        conf = app / 'rtp2httpd_tel.conf'
        conf.write_text(self.original)
        self.env['UNIT'] = f'[Service]\nExecStart=/opt/rtp2httpd -c {conf}'
        r = self.run_install_menu('2\nn\ntel\n2\n6200\ny\n')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(conf.read_text(), self.original.replace('5140', '6200'))
        self.assertIn('directory', self.log.read_text())
        self.assertNotIn('parent', self.log.read_text())
        self.assertNotIn('forbidden', self.log.read_text())

    def test_menu_two_rejects_dropins_or_ambiguous_execstart(self):
        for env in ({'DROPINS': '/override.conf'}, {'UNIT': self.env['UNIT'] + '\nExecStart=/bad/rtp2httpd -c /bad.conf'}):
            with self.subTest(env=env):
                self.log.unlink(missing_ok=True)
                original_env = self.env.copy()
                self.env.update(env)
                r = self.run_install_menu('2\n1\n2\n6000\ny\n')
                self.env = original_env
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.conf.read_text(), self.original)
                self.assertNotIn('restart', self.log.read_text())
                self.assertNotIn('directory', self.log.read_text())

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

    def prompt_with_inventory(self, stdin, args='wan.7 media', planned=False):
        (self.bin / 'ip').write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$LOG.ip"
case "$*" in
'-o link show')
 printf '%s\\n' '2: wan.7@wan: <UP> mtu 1500 state UP link/ether aa:bb:cc:dd:ee:ff' '3: media: <UP> mtu 1500 state UNKNOWN link/ether aa:bb:cc:dd:ee:ff' '4: tailscale0: <UP> mtu 1280 state UNKNOWN link/none' '5: br-demo: <DOWN> mtu 1500 state DOWN link/ether aa:bb:cc:dd:ee:ff' '6: veth123@if77: <UP> mtu 1500 state UP link/ether aa:bb:cc:dd:ee:ff';;
'-o -4 addr show') printf '%s\\n' '2: wan.7 inet 192.0.2.2/24 scope global wan.7' '5: br-demo inet 198.51.100.1/24 scope global br-demo';;
'link show dev wan.7'|'link show dev media'|'link show dev tailscale0'|'link show dev br-demo'|'link show dev veth123') exit 0;;
*) exit 1;;
esac
''')
        return self.run_shell(f'''RTP2HTTPD_RTSP_IF=keep
RTP2HTTPD_RTSP_PLANNED={int(planned)}
rtp2httpd_prompt_rtsp {args}
rc=$?
printf '\\nRESULT=%s\\n' "$RTP2HTTPD_RTSP_IF"
exit "$rc"''', stdin)

    def test_rtsp_reuse_and_fallback_do_not_enumerate(self):
        for choice, expected in [('1', 'wan.7'), ('2', 'media'), ('4', '')]:
            with self.subTest(choice=choice):
                Path(str(self.log) + '.ip').unlink(missing_ok=True)
                r = self.prompt_with_inventory(choice + '\n')
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn('RESULT=' + expected + '\n', r.stdout)
                calls = Path(str(self.log) + '.ip')
                self.assertNotIn('-o', calls.read_text() if calls.exists() else '')
                self.assertNotIn('tailscale0', r.stdout)
                self.assertNotIn('aa:bb', r.stdout)
                self.assertFalse(self.log.exists())

    def test_rtsp_other_interfaces_are_compact_and_numbered(self):
        for selection, expected in [('1', 'wan.7'), ('2', 'media'), ('3', 'tailscale0'),
                                    ('4', 'br-demo'), ('5', 'veth123'), ('wan.7', 'wan.7')]:
            with self.subTest(selection=selection):
                r = self.prompt_with_inventory('3\n' + selection + '\n')
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn('RESULT=' + expected + '\n', r.stdout)
                for text in ('wan.7', '192.0.2.2/24', 'media', '无 IPv4', 'UNKNOWN', 'DOWN', 'tailscale0', 'br-demo', 'veth123'):
                    self.assertIn(text, r.stdout)
                for text in ('link/ether', 'aa:bb:cc:dd:ee:ff', '@if77', 'mtu', 'link/none'):
                    self.assertNotIn(text, r.stdout)
                self.assertIn('link show dev ' + expected, Path(str(self.log) + '.ip').read_text())
                self.assertFalse(self.log.exists())

    def test_rtsp_cancel_eof_and_bad_selection_preserve_value(self):
        for stdin, status in [('', 2), ('0\n', 2), ('\n', 2), ('3\n', 2), ('3\n0\n', 2),
                              ('3\n\n', 2), ('3\n99999999999999999999999\n', 1), ('3\nabsent\n', 1)]:
            with self.subTest(stdin=stdin):
                r = self.prompt_with_inventory(stdin)
                self.assertEqual(r.returncode, status, r.stdout + r.stderr)
                self.assertIn('RESULT=keep\n', r.stdout)
                self.assertFalse(self.log.exists())

    def test_rtsp_missing_reuse_and_planned_install(self):
        for choice in ('1', '2'):
            r = self.prompt_with_inventory(choice + '\n', args='"" ""', planned=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn('未配置', r.stdout)
            self.assertIn('RESULT=keep\n', r.stdout)
        r = self.prompt_with_inventory('1\n', args='planned.7 media', planned=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('RESULT=planned.7\n', r.stdout)

    def test_rtsp_inventory_failure_and_disappeared_interface(self):
        for mode in ('link-failure', 'addr-failure', 'disappeared'):
            with self.subTest(mode=mode):
                self.prompt_with_inventory('0\n')
                ip = self.bin / 'ip'
                text = ip.read_text()
                if mode == 'link-failure':
                    text = text.replace("'-o link show')", "'-o link show') exit 1;;\nunused)")
                elif mode == 'addr-failure':
                    text = text.replace("'-o -4 addr show')", "'-o -4 addr show') exit 1;;\nunused)")
                else:
                    text = text.replace("'link show dev wan.7'|", '')
                ip.write_text(text)
                r = self.run_shell('''RTP2HTTPD_RTSP_IF=keep
rtp2httpd_prompt_rtsp wan.7 media
rc=$?
printf '\\nRESULT=%s\\n' "$RTP2HTTPD_RTSP_IF"
exit "$rc"''', '3\n1\n')
                self.assertEqual(r.returncode, 0 if mode == 'addr-failure' else 1, r.stdout + r.stderr)
                self.assertIn('RESULT=wan.7\n' if mode == 'addr-failure' else 'RESULT=keep\n', r.stdout)
                if mode == 'addr-failure':
                    self.assertIn('IPv4 未知', r.stdout)
                    self.assertNotIn('无 IPv4', r.stdout)
                self.assertFalse(self.log.exists())

    def test_settings_rtsp_numeric_selection_and_cancel(self):
        self.prompt_with_inventory('0\n')
        body = f'''rtp2httpd_list_instances() {{ RTP2HTTPD_LIST_SERVICES=(rtp2httpd_demo.service); }}
rtp2httpd_service_config_path() {{ printf '%s\\n' {shlex.quote(str(self.conf))}; }}
rtp2httpd_settings'''
        before = self.state.read_bytes()
        for stdin in ('1\n5\n0\n', '1\n5\n3\n0\n', '1\n5\n3\n'):
            r = self.run_shell(body, stdin)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.conf.read_text(), self.original)
            self.assertEqual(self.state.read_bytes(), before)
            self.assertFalse(self.log.exists())
        r = self.run_shell(body, '1\n5\n3\n3\ny\n')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.conf.read_text(), self.original.replace('= media # operator', '= tailscale0 # operator'))
        self.assertNotIn('forbidden', self.log.read_text())

    def test_no_implicit_choice(self):
        r = self.run_shell('rtp2httpd_prompt_rtsp "" ""', '\n')
        self.assertNotEqual(r.returncode, 0)


if __name__ == '__main__':
    unittest.main()
