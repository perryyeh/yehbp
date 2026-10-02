"""Exercise optional ISP rendering and prompts locally without DNS/network calls."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
ASSET = REPO / 'assets/mosdns/isp-upstream.sh'
DNS = '''plugins:

  # no ecs
  - tag: no_ecs
    type: "ecs_handler"
    args:
      forward: false # 是否转发来自下游的 ecs
      preset: "" # 发送预设 ecs
      send: false # 是否发送 ecs
      mask4: 24
      mask6: 48

  # 附加 ecs cn 信息
  - tag: ecs_cn
    type: "ecs_handler"
    args:
      forward: false # 是否转发来自下游的 ecs
      preset: "116.228.111.118" # 发送预设 ecs
      send: true # 是否发送 ecs
      mask4: 24 # ipv4 掩码。默认 24
      mask6: 48 # ipv6 掩码。默认 48

  # google doh dot
  - tag: google
    type: forward
    args:
      concurrent: 1
      upstreams:
        - addr: "https://dns.google/dns-query"
          # dial_addr: "8.8.8.8" # 8.8.4.4 2001:4860:4860::8888 2001:4860:4860::8844
          # socks5: "10.0.0.120:6153"  #修改点，改为自己的代理
          bootstrap: "198.18.0.2" #修改点，改为域名解析服务器
        - addr: "tls://dns.google"
          # dial_addr: "8.8.4.4"
          # socks5: "10.0.0.120:6153"
          bootstrap: "198.18.0.2"
          enable_pipeline: true

  # cloudflare doh dot
  - tag: cloudflare
    type: forward
    args:
      concurrent: 1
      upstreams:
        - addr: "https://cloudflare-dns.com/dns-query"
          # dial_addr: "1.1.1.1" # 1.0.0.1 2606:4700:4700::1111 2606:4700:4700::1001
          # socks5: "10.0.0.120:6153"
          bootstrap: "198.18.0.2"
        - addr: "tls://cloudflare-dns.com"
          # dial_addr: "1.0.0.1"
          # socks5: "10.0.0.120:6153"
          bootstrap: "198.18.0.2"
          enable_pipeline: true

  # ali doh dot
  - tag: ali
    type: forward
    args:
      concurrent: 2
      upstreams:
        - addr: "https://dns.alidns.com/dns-query"
          bootstrap: "223.5.5.5"
        - addr: "tls://dns.alidns.com"
          bootstrap: "223.6.6.6"
          enable_pipeline: true

  # tencent doh dot
  - tag: tencent
    type: forward
    args:
      concurrent: 1
      upstreams:
        - addr: "https://doh.pub/dns-query"
          bootstrap: "1.12.12.12"
        - addr: "tls://dot.pub"
          bootstrap: "120.53.53.53"
          enable_pipeline: true

  # 运营商 DNS 占位；由 yehbp 安装时替换，默认不参与国内解析。
  - tag: isp
    type: forward
    args:
      concurrent: 2
      upstreams:
        - addr: "udp://192.0.2.1:53"

  # local dns
  - tag: local
    type: forward
    args:
      concurrent: 1
      upstreams:
        - addr: "udp://10.0.0.1"

  # fake ip
  - tag: forward_fakeipv4
    type: forward
    args:
      concurrent: 1
      upstreams:
        - addr: "udp://198.18.0.2:53"

  # fake ip v6
  - tag: forward_fakeipv6
    type: forward
    args:
      concurrent: 1
      upstreams:
        - addr: "udp://[2001:2:0:6152::2]:53"

  # fakeip 地址段，用于匹配 fakeip 返回结果
  - tag: fakeip_cidr
    type: ip_set
    args:
      ips:
        - "198.18.0.0/15"
        - "2001:2:0:6152:0:9::/96"
'''
CONFIG = '''log:
  level: warn
  file: "/dev/shm/mosdns.log"

api:
  http: "0.0.0.0:9091"

include:
  - "./data.yaml"
  - "./dns.yaml"

plugins:
  # 转发组 - 直连
  - tag: forward_public_direct_group
    type: fallback
    args:
      primary: ali
      secondary: tencent
      threshold: 50
      always_standby: true

  # 运营商优先；公共 DNS 同时待命。默认入口不使用此组。
  - tag: forward_isp_direct_group
    type: fallback
    args:
      primary: isp
      secondary: forward_public_direct_group
      threshold: 50
      always_standby: true

  # 国内入口：yehbp 仅在启用运营商时切换 exec 目标。
  - tag: forward_direct_group
    type: sequence
    args:
      - exec: $forward_public_direct_group

  # 转发组 - 代理
  - tag: forward_proxy_group
    type: fallback
    args:
      primary: cloudflare
      secondary: google
      threshold: 50
      always_standby: true

  # fakeip 分发：AAAA → v6, A → v4
  - tag: sequence_fakeip
    type: sequence
    args:
      - matches: "qtype 28"
        exec: $forward_fakeipv6
      - matches: "qtype 1"
        exec: $forward_fakeipv4

  # proxy 域名通用处理：拦截 64/65，A/AAAA 走 fakeip，其他走代理直查
  - tag: sequence_proxy_fakeip
    type: sequence
    args:
      - matches: "qtype 64 65"
        exec: reject 0
      - matches: "qtype 64 65"
        exec: return
      - exec: $sequence_fakeip
      - matches: has_resp
        exec: return
      - exec: $forward_proxy_group

  # 不在名单中的域名
  - tag: sequence_not_in_list
    type: sequence
    args:
      # SVCB/HTTPS (qtype 64/65) 强制 NODATA，防止 ipv6hint/ipv4hint 泄露真实 IP
      # 必须在 forward 之前拦截，否则 remote 返回的 SVCB 会被透传
      - matches: "qtype 64 65"
        exec: reject 0
      - matches: "qtype 64 65"
        exec: return

      # 先带 ECS 查 Cloudflare/Google：
      # - 非 A/AAAA：不参与国内/海外 IP 判定，直接返回 Cloudflare/Google 真实结果。
      # - A/AAAA 返回空：不补查国内 DNS，后续交给 fakeip 兜底。
      # - A/AAAA 返回 CN IP：清 ECS 后用国内 DNS 重查，拿更合适的国内 IP。
      # - A/AAAA 返回海外 IP：后续交给 fakeip。
      - exec: $ecs_cn
      - exec: $forward_proxy_group

      # 非 A/AAAA：64/65 已在前面拦截；其余类型直接返回带 ECS 的 Cloudflare/Google 结果。
      - matches: "!qtype 1 28"
        exec: return

      # A/AAAA 是 CN IP → 清 ECS 后用国内 DNS 重查，拿更合适的国内 IP
      - matches:
        - "qtype 1 28"
        - "resp_ip $geoip_cn"
        exec: $no_ecs
      - matches:
        - "qtype 1 28"
        - "resp_ip $geoip_cn"
        exec: $forward_direct_group
      # 国内 DNS 返回 CN IP → 记录并返回真实 IP；若返回海外 IP，继续走 fakeip
      - matches: "resp_ip $geoip_cn"
        exec: query_summary not_in_list_direct
      - matches: "resp_ip $geoip_cn"
        exec: return

      # 剩下的 A/AAAA（CF/Google 空结果、海外 IP、或国内 DNS 重查后非 CN）→ fakeip
      - exec: drop_resp
      - exec: $sequence_fakeip
      - matches: "resp_ip $fakeip_cidr"
        exec: query_summary not_in_list_fake
      - exec: return

  # 主逻辑
  - tag: sequence_main
    type: sequence
    args:
      # 1. 自定义 hosts：直接读取里面的解析地址，压过所有公共/内置规则
      - exec: $geosite_my_hosts
      - matches: has_resp
        exec: accept

      # 2. 自定义黑名单：最高优先级，压过 local、公共规则和自定义白/灰名单
      - matches: qname $geosite_my_blacklist
        exec: reject 3
      - matches: qname $geosite_my_blacklist
        exec: return

      # 3. 自定义灰名单：64/65 拦截，A/AAAA 走 fakeip，其他 qtype 走代理真实查询；命中后不降级到后续规则
      - matches: qname $geosite_my_greylist
        exec: $sequence_proxy_fakeip
      - matches: has_resp
        exec: accept
      - matches: qname $geosite_my_greylist
        exec: return

      # 4. 自定义白名单：走 ali 和 tencent，压过 local 和公共 proxy
      - matches: qname $geosite_my_whitelist
        exec: $forward_direct_group
      - matches: has_resp
        exec: accept

      # 5. geosite_local 交给 local 去解析，低于 my/* 自定义规则
      - matches: qname $geosite_local
        exec: $local
      - matches: qname $geosite_local
        exec: accept

      # 6. 公共 blocklist：直接返回无法解析
      - matches: qname $geosite_block
        exec: reject 3
      - matches: qname $geosite_block
        exec: return

      # 7. 公共 proxy：64/65 拦截，A/AAAA 走 fakeip，其他 qtype 走代理真实查询；优先于公共 direct
      - matches: qname $geosite_proxy
        exec: $sequence_proxy_fakeip
      - matches: has_resp
        exec: accept
      - matches: qname $geosite_proxy
        exec: return

      # 8. 公共 direct：走 ali 和 tencent 去解析
      - matches: qname $geosite_direct
        exec: $forward_direct_group
      - matches: has_resp
        exec: accept

      # 9. 剩下的不在名单中的域名 兜底逻辑
      - exec: $sequence_not_in_list
      - matches: has_resp
        exec: accept

  # 启动服务器
  - tag: udp_main
    type: udp_server
    args:
      entry: sequence_main
      listen: ":53"

  - tag: tcp_main
    type: tcp_server
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
        expected = DNS.replace('        - addr: "udp://192.0.2.1:53"',
                               '        - addr: "udp://192.0.2.53:53"\n        - addr: "udp://198.51.100.53:53"')
        self.assertEqual(dns, expected)
        self.assertEqual(config, CONFIG.replace('      - exec: $forward_public_direct_group',
                                                '      - exec: $forward_isp_direct_group'))
        self.assert_clean()

    def test_single_isp(self):
        self.assertEqual(self.render('192.0.2.53').returncode, 0)
        self.assertEqual((self.root / 'dns.yaml').read_text(), DNS.replace('udp://192.0.2.1:53', 'udp://192.0.2.53:53'))

    def test_invalid_addresses_leave_files_untouched(self):
        for ip in ('999.1.1.1', '01.2.3.4', '1.2.3', 'dns.example', '1.1.1.1:53',
                   '0.0.0.0', '192.0.2.1', '255.255.255.255', '1.1.1.1\nplugins:', '$(touch BAD)'):
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
        for config in ('plugins: []\n', CONFIG + '\n  - tag: forward_direct_group\n    type: sequence\n    args:\n      - exec: $forward_public_direct_group\n'):
            (self.root / 'config.yaml').write_text(config)
            self.assertNotEqual(self.render('192.0.2.53').returncode, 0)
            self.assertEqual((self.root / 'config.yaml').read_text(), config)
            self.assertEqual((self.root / 'dns.yaml').read_text(), DNS)
            self.assert_clean()

    def test_missing_placeholder_is_safe(self):
        dns = DNS.replace('udp://192.0.2.1:53', 'udp://192.0.2.9:53')
        (self.root / 'dns.yaml').write_text(dns)
        self.assertNotEqual(self.render('192.0.2.53').returncode, 0)
        self.assertEqual((self.root / 'dns.yaml').read_text(), dns)
        self.assertEqual((self.root / 'config.yaml').read_text(), CONFIG)
        self.assert_clean()

    @unittest.skipUnless(os.environ.get('MOSDNS_TEST_BINARY'), 'set MOSDNS_TEST_BINARY for real startup')
    def test_real_mosdns_startup_default_single_multiple(self):
        for ips in ((), ('192.0.2.53',), ('192.0.2.53', '198.51.100.53')):
            with self.subTest(ips=ips):
                (self.root / 'dns.yaml').write_text(DNS)
                (self.root / 'config.yaml').write_text(CONFIG)
                if ips:
                    self.assertEqual(self.render(*ips).returncode, 0)
                # Isolate the exact domestic groups and all upstream definitions.
                config = (self.root / 'config.yaml').read_text().split('  # 转发组 - 代理')[0]
                config = config.replace('  - "./data.yaml"\n', '')
                config = config.replace('/dev/shm/mosdns.log', str(self.root / 'mosdns.log'))
                config = config.replace('0.0.0.0:9091', '127.0.0.1:0')
                config += '  - tag: test_server\n    type: udp_server\n    args:\n      entry: forward_direct_group\n      listen: "127.0.0.1:0"\n'
                (self.root / 'config.yaml').write_text(config)
                proc = subprocess.Popen([os.environ['MOSDNS_TEST_BINARY'], 'start',
                                         '-c', 'config.yaml', '-d', str(self.root)],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    time.sleep(0.5)
                    self.assertIsNone(proc.poll(), 'mosdns failed to stay running')
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                    output, _ = proc.communicate(timeout=5)
                self.assertIn('main config loaded', output)

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
        self.assertIn('      - exec: $forward_isp_direct_group', (self.root / 'config.yaml').read_text())
        self.assert_clean()

    def test_eof_and_download_failure_stop_without_changes(self):
        for answers, fail in (('', False), ('y\n', False), ('y\n', True)):
            self.assertNotEqual(self.run_option(answers, fail).returncode, 0)
            self.assertEqual((self.root / 'dns.yaml').read_text(), DNS)
            self.assertEqual((self.root / 'config.yaml').read_text(), CONFIG)
            self.assert_clean()


if __name__ == '__main__':
    unittest.main()
