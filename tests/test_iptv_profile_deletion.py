"""Exercise real IPTV Bash functions with sandboxed NetworkManager/service commands."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = os.environ.get('TMPDIR')
U1 = '11111111-1111-1111-1111-111111111111'
U2 = '22222222-2222-2222-2222-222222222222'
U3 = '33333333-3333-3333-3333-333333333333'

FAKE_NM = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p = Path(os.environ['NM_DB'])
d = json.loads(p.read_text())
a = sys.argv[1:]
with open(os.environ['NM_LOG'], 'a') as f: f.write(json.dumps(a) + '\\n')
if os.environ.get('QUERY_FAIL') and 'show' in a: sys.exit(1)
if os.environ.get('FIELD_FAIL') and a[:2] == ['-g', os.environ['FIELD_FAIL']]: sys.exit(1)
if os.environ.get('POST_DELETE_QUERY_FAIL') and 'show' in a and len(d) < 2: sys.exit(1)
if a == ['-g', 'UUID', 'connection', 'show']:
    print('\\n'.join(d)); sys.exit(0)
if a[:4] == ['connection', 'modify', 'uuid', a[3]] and 'ipv4.ignore-auto-dns' in a and 'ipv6.ignore-auto-dns' in a and a[-1] == 'yes':
    u = a[3]
    if u not in d: sys.exit(10)
    d[u]['ipv4.ignore-auto-dns'] = 'yes'
    d[u]['ipv6.ignore-auto-dns'] = 'yes'
    p.write_text(json.dumps(d)); sys.exit(0)
if a[:3] == ['connection', 'reapply', 'uuid'] or a[:3] == ['connection', 'up', 'uuid']:
    sys.exit(0)
if a[:3] == ['connection', 'delete', 'uuid']:
    if a[3] == os.environ.get('FAIL_UUID'): sys.exit(1)
    d.pop(a[3], None); p.write_text(json.dumps(d)); sys.exit(0)
if 'show' in a:
    u = a[-1]
    if u not in d: sys.exit(10)
    if a[0] == '-g':
        values = [d[u].get(k, '') for k in a[1].split(',')]
        # nmcli 1.42 prints one field per line; keep comma mode as the other real format.
        if os.environ.get('NMCLI_NEWLINE'):
            print(chr(10).join(values))
        else:
            print(','.join(values))
    sys.exit(0)
sys.exit(99)
'''


class IPTVProfileDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='iptv-delete-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        for name, text in [('nmcli', FAKE_NM), ('systemctl', '#!/bin/bash\nprintf "%s\\n" "$*" >> "$SERVICE_LOG"\nexit "${SERVICE_FAIL:-0}"\n')]:
            p = self.bin / name
            p.write_text(text)
            p.chmod(0o755)
        self.state = self.base / 'rtp2httpd_tel.env'
        self.config = self.base / 'rtp2httpd_tel.conf'
        self.config.write_text('keep config')
        self.db = self.base / 'nm.json'
        self.log = self.base / 'nm.log'
        self.service_log = self.base / 'service.log'
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                        NM_DB=str(self.db), NM_LOG=str(self.log), SERVICE_LOG=str(self.service_log))
        self.write_state()
        self.profiles = {U1: self.profile('mcast', 51), U2: self.profile('fcc', 85)}

    def profile(self, role, vlan, instance='tel'):
        return {'connection.type': 'vlan', 'connection.id': f'rtp2httpd-{instance}-{role}-eth0-{vlan}',
                'connection.interface-name': f'eth0.{vlan}', 'vlan.parent': 'eth0', 'vlan.id': str(vlan),
                'ipv4.method': 'auto' if role == 'fcc' else 'disabled', 'ipv6.method': 'disabled',
                'ipv4.ignore-auto-dns': 'no', 'ipv6.ignore-auto-dns': 'no', 'GENERAL.STATE': 'activated'}

    def write_state(self, **changes):
        fields = dict(MANAGED_BY='yehbp-rtp2httpd', INSTANCE='tel', PARENT_IF='eth0',
                      MULTICAST_VLAN='51', FCC_VLAN='85', MULTICAST_PROFILE_UUID=U1, FCC_PROFILE_UUID=U2)
        fields.update(changes)
        self.state.write_text(''.join(f'{k}={v}\n' for k, v in fields.items()))

    def run_function(self, body=None):
        self.db.write_text(json.dumps(self.profiles))
        command = body or f'rtp2httpd_remove_network_profiles "{self.state}"'
        sandbox = f'''rtp2httpd_list_instances() {{ RTP2HTTPD_LIST_SERVICES=(); }}
rm() {{
    local p
    for p in "$@"; do
        case "$p" in
            -f) ;;
            /etc/systemd/system/*) ;; # Virtual unit only; never touch host /etc.
            "{self.base}"/*) command rm -f -- "$p" ;;
            *) echo "unsafe rm: $p" >&2; return 99 ;;
        esac
    done
}}
'''
        r = subprocess.run(['bash', '-c', f'source "{ROOT}/assets/iptv/rtp2httpd.sh"\n{sandbox}\n{command}'],
                           env=self.env, text=True, errors='replace', capture_output=True)
        self.remaining = json.loads(self.db.read_text())
        self.calls = [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []
        self.deleted = [a[-1] for a in self.calls if a[:3] == ['connection', 'delete', 'uuid']]
        return r

    def test_only_owned_uuids_deleted(self):
        self.profiles[U3] = self.profile('mcast', 51, 'other')
        user = '44444444-4444-4444-4444-444444444444'
        self.profiles[user] = dict(self.profile('fcc', 85), **{'connection.id': 'user-original'})
        r = self.run_function()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.deleted, [U1, U2])
        self.assertEqual(set(self.remaining), {U3, user})

    def test_same_name_unrecorded_uuid_preserved(self):
        self.profiles[U3] = self.profile('mcast', 51)
        self.assertEqual(self.run_function().returncode, 0)
        self.assertIn(U3, self.remaining)

    def test_missing_uuid_keeps_state_and_profiles(self):
        self.write_state(MULTICAST_PROFILE_UUID='')
        r = self.run_function(f'rtp2httpd_remove_instance rtp2httpd_tel.service "{self.config}" "{self.state}"')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.deleted, [])
        self.assertTrue(self.state.exists())
        self.assertTrue(self.config.exists())

    def test_stale_uuid_never_falls_back_to_vlan(self):
        self.profiles[U3] = self.profiles.pop(U1)
        r = self.run_function()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.deleted, [U2])
        self.assertIn(U3, self.remaining)
        self.assertIn('UUID', r.stderr)

    def test_mismatched_properties_refuse_all_deletion(self):
        for field, value in [('connection.id', 'user'), ('connection.type', 'ethernet'),
                             ('connection.interface-name', 'eth9.51'), ('vlan.parent', 'eth9'), ('vlan.id', '52')]:
            with self.subTest(field=field):
                self.profiles[U1] = dict(self.profile('mcast', 51), **{field: value})
                self.log.unlink(missing_ok=True)
                self.assertNotEqual(self.run_function().returncode, 0)
                self.assertEqual(self.deleted, [])

    def test_bad_instance_marker_refuses_deletion(self):
        for changes in [dict(MANAGED_BY='other'), dict(INSTANCE='other')]:
            with self.subTest(changes=changes):
                self.write_state(**changes)
                self.assertNotEqual(self.run_function().returncode, 0)
                self.assertEqual(self.deleted, [])

    def test_delete_failure_preserves_retry_state(self):
        self.env['FAIL_UUID'] = U2
        body = f'rtp2httpd_remove_instance rtp2httpd_tel.service "{self.config}" "{self.state}"'
        r = self.run_function(body)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.state.exists())
        self.assertTrue(self.config.exists())
        self.assertFalse(self.service_log.exists())
        self.profiles = self.remaining
        self.env.pop('FAIL_UUID')
        self.log.unlink()
        r = self.run_function()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.deleted, [U2])

    def test_query_failure_preserves_state(self):
        self.env['QUERY_FAIL'] = '1'
        r = self.run_function(f'rtp2httpd_remove_instance rtp2httpd_tel.service "{self.config}" "{self.state}"')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.deleted, [])
        self.assertTrue(self.state.exists())
        self.assertTrue(self.config.exists())

    def test_failed_type_query_with_present_inventory_refuses_deletion(self):
        self.env['FIELD_FAIL'] = 'connection.type'
        r = self.run_function()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.deleted, [])
        self.assertIn(['-g', 'UUID', 'connection', 'show'], self.calls)

    def test_post_delete_query_failure_preserves_retry_state(self):
        self.env['POST_DELETE_QUERY_FAIL'] = '1'
        body = f'rtp2httpd_remove_instance rtp2httpd_tel.service "{self.config}" "{self.state}"'
        self.assertNotEqual(self.run_function(body).returncode, 0)
        self.assertTrue(self.state.exists())
        self.assertTrue(self.config.exists())
        self.profiles = self.remaining
        self.env.pop('POST_DELETE_QUERY_FAIL')
        self.assertEqual(self.run_function(body).returncode, 0)
        self.assertFalse(self.state.exists())

    def test_service_stop_failure_preserves_state_and_config(self):
        self.env['SERVICE_FAIL'] = '1'
        r = self.run_function(f'rtp2httpd_remove_instance rtp2httpd_tel.service "{self.config}" "{self.state}"')
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.state.exists())
        self.assertTrue(self.config.exists())

    def test_shared_vlan_uuid_deleted_once(self):
        self.write_state(FCC_VLAN='51', FCC_PROFILE_UUID=U1)
        self.profiles.pop(U2)
        self.assertEqual(self.run_function().returncode, 0)
        self.assertEqual(self.deleted, [U1])

    def test_replacement_stops_before_prompts_if_shared_reference(self):
        (self.base / 'rtp2httpd_other.env').write_text(f'MULTICAST_PROFILE_UUID={U1}\n')
        body = f'''rtp2httpd_require_commands() {{ :; }}
select_dockerapps_dir() {{ SELECTED_DOCKERAPPS_DIR="{self.base}"; }}
rtp2httpd_prompt_instance() {{ RTP2HTTPD_INSTANCE=tel; RTP2HTTPD_CONFIG_PATH="{self.config}"; RTP2HTTPD_STATE_PATH="{self.state}"; RTP2HTTPD_SERVICE=rtp2httpd_tel.service; RTP2HTTPD_SERVICE_PATH="{self.base}/unit"; }}
rtp2httpd_select_parent() {{ echo unsafe >> "{self.base}/continued"; return 1; }}
rtp2httpd_install <<< y'''
        r = self.run_function(body)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.deleted, [])
        self.assertTrue(self.state.exists())
        self.assertFalse((self.base / 'continued').exists())

    def test_success_removes_config_and_state(self):
        r = self.run_function(f'rtp2httpd_remove_instance rtp2httpd_tel.service "{self.config}" "{self.state}"')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.config.exists())
        self.assertEqual(self.service_log.read_text().splitlines(), ['disable --now rtp2httpd_tel.service', 'daemon-reload'])

    def test_daemon_reload_failure_keeps_state(self):
        (self.bin / 'systemctl').write_text('#!/bin/bash\n[ "$1" != daemon-reload ]\n')
        r = self.run_function(f'rtp2httpd_remove_instance rtp2httpd_tel.service "{self.config}" "{self.state}"')
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.state.exists())

    def test_unreadable_state_preserves_config_and_state(self):
        self.state.chmod(0)
        self.addCleanup(lambda: self.state.exists() and self.state.chmod(0o600))
        if os.access(self.state, os.R_OK):
            self.skipTest('host can read mode-000 files')
        r = self.run_function(f'rtp2httpd_remove_instance rtp2httpd_tel.service "{self.config}" "{self.state}"')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.deleted, [])
        self.assertTrue(self.state.exists())
        self.assertTrue(self.config.exists())
        self.assertFalse(self.service_log.exists())

    def test_parent_uuid_conservatively_refuses_deletion(self):
        # NetworkManager accepts UUID parents, but this installer creates an
        # interface-name parent; a changed UUID parent is not ownership proof.
        self.profiles[U1]['vlan.parent'] = U3
        self.assertNotEqual(self.run_function().returncode, 0)
        self.assertEqual(self.deleted, [])

    def test_second_role_mismatch_prevents_first_role_deletion(self):
        self.profiles[U2]['connection.id'] = 'user-profile'
        self.assertNotEqual(self.run_function().returncode, 0)
        self.assertEqual(self.deleted, [])

    def test_legacy_missing_state_warns_without_network_deletion(self):
        self.state.unlink()
        r = self.run_function()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.deleted, [])
        self.assertIn('跳过', r.stdout)

    def test_existing_instance_entry_applies_dns_without_changing_methods(self):
        self.profiles[U2]['ipv6.method'] = 'auto'
        self.profiles[U3] = self.profile('fcc', 85, 'other')
        body = f'''rtp2httpd_service_config_path() {{ printf '%s\\n' "{self.config}"; }}
rtp2httpd_rtsp_config() {{ printf 'unchanged\\n'; }}
printf '0\\n' | rtp2httpd_settings rtp2httpd_tel.service'''
        r = self.run_function(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.remaining[U1]['ipv4.method'], 'disabled')
        self.assertEqual(self.remaining[U1]['ipv6.method'], 'disabled')
        self.assertEqual(self.remaining[U2]['ipv4.method'], 'auto')
        self.assertEqual(self.remaining[U2]['ipv6.method'], 'auto')
        self.assertEqual(self.remaining[U1]['ipv4.ignore-auto-dns'], 'yes')
        self.assertEqual(self.remaining[U1]['ipv6.ignore-auto-dns'], 'yes')
        self.assertEqual(self.remaining[U2]['ipv4.ignore-auto-dns'], 'yes')
        self.assertEqual(self.remaining[U2]['ipv6.ignore-auto-dns'], 'yes')
        self.assertEqual(self.remaining[U3]['ipv4.ignore-auto-dns'], 'no')
        self.assertEqual(self.remaining[U3]['ipv6.ignore-auto-dns'], 'no')
        self.assertEqual(self.deleted, [])
        self.assertIn(['connection', 'reapply', 'uuid', U1], self.calls)
        self.assertIn(['connection', 'reapply', 'uuid', U2], self.calls)
        self.assertNotIn('connection add', ' '.join(' '.join(a) for a in self.calls))

    def test_existing_instance_entry_accepts_nmcli_142_newlines(self):
        self.env['NMCLI_NEWLINE'] = '1'
        self.profiles[U2]['ipv6.method'] = 'disabled'
        body = f'''rtp2httpd_service_config_path() {{ printf '%s\\n' "{self.config}"; }}
rtp2httpd_rtsp_config() {{ printf 'unchanged\\n'; }}
printf '0\\n' | rtp2httpd_settings rtp2httpd_tel.service'''
        r = self.run_function(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.remaining[U1]['ipv4.method'], 'disabled')
        self.assertEqual(self.remaining[U2]['ipv4.method'], 'auto')
        self.assertEqual(self.remaining[U2]['ipv6.method'], 'disabled')
        self.assertEqual(self.remaining[U1]['ipv6.ignore-auto-dns'], 'yes')
        self.assertEqual(self.remaining[U2]['ipv6.ignore-auto-dns'], 'yes')
        self.assertEqual(self.deleted, [])

    def test_new_install_sets_both_ignore_auto_dns_flags(self):
        text = (ROOT / 'assets/iptv/rtp2httpd.sh').read_text()
        self.assertIn('ipv4.method auto ipv4.ignore-auto-dns yes', text)
        self.assertIn('ipv4.method disabled ipv4.never-default yes ipv4.ignore-auto-dns yes ipv6.method disabled ipv6.ignore-auto-dns yes', text)
        self.assertIn('ipv6.method disabled ipv6.ignore-auto-dns yes', text)
        self.assertNotIn('rtp2httpd_ensure_owned_dhcp_dns', text)


if __name__ == '__main__':
    unittest.main()
