"""Exercise optional ISP rendering and prompts locally without DNS/network calls."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
ASSET = REPO / 'assets/mosdns/isp-upstream.sh'
DNS = '''plugins:
  - tag: ali
    type: forward
    args:
      concurrent: 2
      upstreams:
        - addr: "https://dns.alidns.com/dns-query"
  - tag: tencent
    type: forward
    args:
      concurrent: 1
      upstreams:
        - addr: "https://doh.pub/dns-query"
'''
CONFIG = '''plugins:
  - tag: forward_direct_group
    type: fallback
    args:
      primary: ali
      secondary: tencent
      threshold: 50
      always_standby: true
  - tag: sequence_main
    type: sequence
    args:
      - exec: $forward_direct_group
  - tag: udp_main
    type: udp_server
    args:
      entry: sequence_main
      listen: ":53"
'''
SOURCE = (REPO / 'install.sh').read_text()
OPTION = SOURCE.split('    # 运营商 DNS 可选；默认保持原阿里/腾讯行为。', 1)[1].split('    # 10) 可选功能', 1)[0]


class IspUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'dns.yaml').write_text(DNS)
        (self.root / 'config.yaml').write_text(CONFIG)
        for name in ('dns.yaml', 'config.yaml'):
            (self.root / name).chmod(0o600)

    def render(self, *ips):
        return subprocess.run(['bash', '-c', 'source "$ASSET"; mosdns_add_isp_upstream "$@"',
                               'test', str(self.root), *ips],
                              env=dict(os.environ, ASSET=str(ASSET)), capture_output=True,
                              text=True, timeout=10)

    def assert_clean(self):
        self.assertEqual(list(self.root.glob('.isp-*')), [])
        for name in ('dns.yaml', 'config.yaml'):
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)

    def test_multiple_isp_preserves_public_and_rules(self):
        result = self.render('192.0.2.53', '198.51.100.53')
        self.assertEqual(result.returncode, 0, result.stderr)
        dns = (self.root / 'dns.yaml').read_text()
        config = (self.root / 'config.yaml').read_text()
        self.assertTrue(dns.startswith(DNS))
        for ip in ('192.0.2.53', '198.51.100.53'):
            self.assertIn(f'"udp://{ip}:53"', dns)
        preserved = CONFIG.replace('tag: forward_direct_group', 'tag: forward_public_direct_group')
        original_group, original_rules = preserved.split('  - tag: sequence_main', 1)
        self.assertTrue(config.startswith(original_group))
        self.assertTrue(config.endswith('  - tag: sequence_main' + original_rules))
        self.assertLess(config.index('  - tag: forward_direct_group'), config.index('  - tag: sequence_main'))
        self.assertIn('primary: isp\n      secondary: forward_public_direct_group\n      threshold: 50\n      always_standby: true', config)
        self.assert_clean()

    def test_single_isp(self):
        self.assertEqual(self.render('192.0.2.53').returncode, 0)
        self.assertEqual((self.root / 'dns.yaml').read_text().count('udp://'), 1)

    def test_invalid_addresses_leave_files_untouched(self):
        for ip in ('999.1.1.1', '01.2.3.4', '1.2.3', 'dns.example', '1.1.1.1:53',
                   '0.0.0.0', '255.255.255.255', '1.1.1.1\nplugins:', '$(touch BAD)'):
            result = self.render(ip)
            self.assertNotEqual(result.returncode, 0, ip)
            self.assertEqual((self.root / 'dns.yaml').read_text(), DNS)
            self.assertEqual((self.root / 'config.yaml').read_text(), CONFIG)
        self.assert_clean()

    def test_empty_addresses_rejected(self):
        self.assertNotEqual(self.render().returncode, 0)
        self.assertEqual((self.root / 'dns.yaml').read_text(), DNS)

    def test_repeated_render_fails_without_changes(self):
        self.assertEqual(self.render('192.0.2.53').returncode, 0)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertNotEqual(self.render('192.0.2.54').returncode, 0)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        self.assert_clean()

    def test_missing_or_duplicate_direct_group_is_safe(self):
        for config in ('plugins: []\n', CONFIG + '\n  - tag: forward_direct_group\n    type: fallback\n'):
            (self.root / 'config.yaml').write_text(config)
            self.assertNotEqual(self.render('192.0.2.53').returncode, 0)
            self.assertEqual((self.root / 'config.yaml').read_text(), config)
            self.assertEqual((self.root / 'dns.yaml').read_text(), DNS)
            self.assert_clean()

    def run_option(self, answers, fail_download=False):
        harness = '''
download_yehbp_asset() { cp "$ASSET" "$2"; }
run_option() {
''' + OPTION + '\n}\nrun_option\n'
        if fail_download:
            harness = harness.replace('cp "$ASSET" "$2"', 'return 1')
        return subprocess.run(['bash', '-c', harness], input=answers, text=True,
                              capture_output=True, timeout=10,
                              env=dict(os.environ, ASSET=str(ASSET), WORK_DIR=str(self.root)))

    def test_default_no_changes_or_download(self):
        for answers in ('\n', 'n\n'):
            self.assertEqual(self.run_option(answers, fail_download=True).returncode, 0)
            self.assertEqual((self.root / 'dns.yaml').read_text(), DNS)
            self.assertEqual((self.root / 'config.yaml').read_text(), CONFIG)
        self.assert_clean()

    def test_yes_retries_empty_and_invalid(self):
        result = self.run_option('y\n\n999.1.1.1\n192.0.2.53 198.51.100.53\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('primary: isp', (self.root / 'config.yaml').read_text())
        self.assert_clean()

    def test_eof_and_download_failure_stop_without_changes(self):
        for answers, fail in (('', False), ('y\n', False), ('y\n', True)):
            self.assertNotEqual(self.run_option(answers, fail).returncode, 0)
            self.assertEqual((self.root / 'dns.yaml').read_text(), DNS)
            self.assertEqual((self.root / 'config.yaml').read_text(), CONFIG)
            self.assert_clean()


if __name__ == '__main__':
    unittest.main()
