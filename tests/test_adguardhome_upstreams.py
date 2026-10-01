"""Local AdGuard Home tests; no Docker, downloads, or remote hosts."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / "install.sh").read_text()
HELPER = SOURCE.split("adguardhome_replace_upstreams() {", 1)[1].split("\ninstall_adguardhome() {", 1)[0]
HELPER = "adguardhome_replace_upstreams() {" + HELPER
FLOW = SOURCE.split("install_adguardhome() {", 1)[1].split("\n    # 2) 输入 AdGuardHome", 1)[0]
FLOW = "install_adguardhome() {" + FLOW + '\nprintf "MODE=%s\\nMOSDNS=%s\\n" "$dns_mode" "$mosdns"\nprintf "UPSTREAM=%s\\n" "${adguard_upstreams[@]}"\n}\n'
FIXTURE = """dns:
  upstream_dns:
    - 10.0.1.119:53
    - '#[fd10::1:119]:53'
  upstream_dns_file: ""
  bootstrap_dns:
    - 9.9.9.10
  fallback_dns:
    - 10.0.0.1
    - 223.5.5.5
  upstream_mode: fastest_addr
  fastest_timeout: 3s
  cache_size: 134217728
http:
  address: 0.0.0.0:80
"""


class AdguardUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "AdGuardHome.yaml"
        self.config.write_text(FIXTURE)
        self.config.chmod(0o600)

    def run_replace(self, *upstreams):
        return subprocess.run(
            ["bash", "-c", HELPER + '\nadguardhome_replace_upstreams "$@"',
             "test", str(self.config), *upstreams],
            text=True, capture_output=True, timeout=10)

    def assert_preserved(self):
        text = self.config.read_text()
        self.assertEqual(text.split('  upstream_dns_file:', 1)[1],
                         FIXTURE.split('  upstream_dns_file:', 1)[1])
        self.assertNotIn("10.0.1.119", text)
        self.assertNotIn("fd10::1:119", text)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.config.parent.glob("*.tmp.*")), [])

    def test_ali_tencent_replace_entire_list(self):
        result = self.run_replace("223.5.5.5", "119.29.29.29")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("    - '223.5.5.5'\n    - '119.29.29.29'\n", self.config.read_text())
        self.assert_preserved()

    def test_custom_ip_ipv6_doh_dot(self):
        addresses = ("192.0.2.53", "[2001:db8::53]:5353", "https://dns.example/dns-query", "tls://dns.example")
        result = self.run_replace(*addresses)
        self.assertEqual(result.returncode, 0, result.stderr)
        for address in addresses:
            self.assertIn(f"    - '{address}'\n", self.config.read_text())
        self.assert_preserved()

    def test_yaml_quote_and_backslash_are_literal(self):
        result = self.run_replace("https://dns.example/a'b\\c#fragment")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("'https://dns.example/a''b\\c#fragment'", self.config.read_text())
        self.assert_preserved()

    def test_missing_or_duplicate_key_fails_without_write(self):
        for content in ('dns:\n  fallback_dns: []\n', FIXTURE + '  upstream_dns:\n    - 1.1.1.1\n'):
            self.config.write_text(content)
            result = self.run_replace("223.5.5.5")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.config.read_text(), content)
            self.assertEqual(list(self.config.parent.glob("*.tmp.*")), [])

    def test_empty_and_newline_rejected(self):
        for addresses in ((), ("",), ("1.1.1.1\n  fallback_dns: []",)):
            result = self.run_replace(*addresses)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.config.read_text(), FIXTURE)

    def run_selection(self, answers):
        mocks = '''
select_macvlan_or_exit() { return 0; }
prompt_ipv4_last_octet() { printf 'MOSDNS_PROMPT\\n' >&2; printf '119\\n'; }
calculate_ip_mac() { calculated_ip=192.0.2.119; calculated_ip6=fd00::119; }
'''
        return subprocess.run(["bash", "-c", mocks + FLOW + '\ninstall_adguardhome'],
                              input=answers, text=True, capture_output=True, timeout=10)

    def test_default_and_explicit_one_use_old_flow(self):
        for answers in ("\n", "1\n"):
            result = self.run_selection(answers)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("MODE=1\nMOSDNS=192.0.2.119", result.stdout)
            self.assertIn("MOSDNS_PROMPT", result.stderr)

    def test_two_skips_mosdns(self):
        result = self.run_selection("2\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UPSTREAM=223.5.5.5\nUPSTREAM=119.29.29.29", result.stdout)
        self.assertNotIn("MOSDNS_PROMPT", result.stderr)

    def test_three_multiple_upstreams_and_empty_retry(self):
        result = self.run_selection("3\n   \n192.0.2.53 tls://dns.example\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("请至少输入一个", result.stdout)
        self.assertIn("UPSTREAM=192.0.2.53\nUPSTREAM=tls://dns.example", result.stdout)
        self.assertNotIn("MOSDNS_PROMPT", result.stderr)

    def test_invalid_choice_retries(self):
        result = self.run_selection("9\n2\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("请选择 1、2 或 3", result.stdout)
        self.assertIn("MODE=2", result.stdout)

    def test_eof_cancels(self):
        for answers in ("", "3\n"):
            self.assertNotEqual(self.run_selection(answers).returncode, 0)


if __name__ == "__main__":
    unittest.main()
