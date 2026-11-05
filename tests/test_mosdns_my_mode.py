"""YehBP mosdns schedule: modes, legacy switches, and existing-instance updates."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import textwrap
import unittest

REPO = Path(__file__).resolve().parents[1]
ASSET = REPO / "assets/mosdns/schedule.sh"
INSTALL = REPO / "install.sh"


class MosdnsScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inst = self.root / "mosdns"
        (self.inst / "cron").mkdir(parents=True)
        (self.inst / "my").mkdir()
        (self.inst / "rule").mkdir()
        (self.inst / "cron/cron.env").write_text(textwrap.dedent("""\
            MOSDNS_SYNC_RULES_ENABLED=0
            MOSDNS_SYNC_CANDIDATES_ENABLED=1
            MOSDNS_PROMOTE_ENABLED=1
            MOSDNS_PROMOTE_LOOKBACK_DAYS=7
            MOSDNS_PROMOTE_DIRECT_THRESHOLD=50
            """))
        (self.inst / "config.yaml").write_text("log:\n  level: warn\n  file: \"/dev/shm/mosdns.log\"\nplugins:\n  level: keep-me\n")
        (self.inst / "compose.yaml").write_text("services:\n  mosdns:\n    image: irinesistiana/mosdns:latest\n    networks:\n      macvlan:\n        ipv4_address: ${ipv4}\n")
        (self.inst / "dns.yaml").write_text("dns-sentinel\n")
        (self.inst / ".env").write_text("MACVLAN=test\nipv4=192.0.2.119\n")
        (self.inst / "my/whitelist.txt").write_text("local-list\n")
        (self.inst / "rule/geoip_cn.txt").write_text("203.0.113.0/24\n")
        (self.inst / "cron/sync.conf").write_text("geoip_cn.txt | yes | https://example.invalid/keep\n")
        self.env = {"PATH": f"/usr/bin:/bin:{os.environ.get('PATH', '')}", "HOME": str(self.root / "home"),
                    "TMPDIR": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}
        (self.root / "home").mkdir()

    def preserved(self):
        return {
            "compose": (self.inst / "compose.yaml").read_bytes(),
            "dns": (self.inst / "dns.yaml").read_bytes(),
            "dotenv": (self.inst / ".env").read_bytes(),
            "my": (self.inst / "my/whitelist.txt").read_bytes(),
            "rule": (self.inst / "rule/geoip_cn.txt").read_bytes(),
        }

    def run_bash(self, script, stdin="", extra=None):
        env = dict(self.env)
        if extra:
            env.update(extra)
        return subprocess.run(["bash", "-c", script], input=stdin, text=True, capture_output=True,
                              timeout=30, env=env, cwd=str(self.root))

    def source(self, call, stdin=""):
        script = f'source "{ASSET}"; {call}'
        return self.run_bash(script, stdin)

    def test_asset_does_not_embed_my_list_urls(self):
        text = ASSET.read_text()
        self.assertNotIn("main/my/whitelist.txt", text)
        self.assertNotIn("MOSDNS_MY_SOURCE", text)
        self.assertIn("main/cron", text)

    def test_prompt_modes_clear_opposite_switches_and_keep_other_settings(self):
        before = self.preserved()
        cases = (
            ("0\n0\n", "0", "0", "0", "0", "warn"),
            ("1\n1\n", "1", "1", "1", "1", "info"),
            ("0\n2\n", "0", "2", "0", "0", "warn"),
        )
        for stdin, rules, mode, candidates, promote, level in cases:
            with self.subTest(stdin=stdin):
                (self.inst / "cron/cron.env").write_text(textwrap.dedent("""\
                    MOSDNS_SYNC_RULES_ENABLED=0
                    MOSDNS_SYNC_CANDIDATES_ENABLED=1
                    MOSDNS_PROMOTE_ENABLED=1
                    MOSDNS_PROMOTE_LOOKBACK_DAYS=7
                    """))
                (self.inst / "config.yaml").write_text("log:\n  level: warn\n  file: \"/dev/shm/mosdns.log\"\nplugins:\n  level: keep-me\n")
                result = self.source(f'mosdns_read_rule_choices "{self.inst}" && mosdns_apply_rule_choices "{self.inst}"', stdin)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                env = (self.inst / "cron/cron.env").read_text()
                self.assertIn(f"MOSDNS_SYNC_RULES_ENABLED={rules}\n", env)
                self.assertIn(f"MOSDNS_MY_MODE={mode}\n", env)
                self.assertIn(f"MOSDNS_SYNC_CANDIDATES_ENABLED={candidates}\n", env)
                self.assertIn(f"MOSDNS_PROMOTE_ENABLED={promote}\n", env)
                self.assertIn("MOSDNS_PROMOTE_LOOKBACK_DAYS=7\n", env)
                config = (self.inst / "config.yaml").read_text()
                self.assertIn(f"  level: {level}\n", config)
                self.assertIn("  level: keep-me\n", config)
                self.assertIn('file: "/dev/shm/mosdns.log"', config)
                self.assertEqual(self.preserved(), before)
                self.assertEqual((self.inst / "cron/sync.conf").read_text(),
                                 "geoip_cn.txt | yes | https://example.invalid/keep\n")
        result = self.source(f'mosdns_read_rule_choices "{self.inst}" && mosdns_apply_rule_choices "{self.inst}"', "1\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MOSDNS_PROMOTE_LOOKBACK_DAYS=7\n", (self.inst / "cron/cron.env").read_text())

    def test_legacy_mode_and_switch_to_mode2(self):
        legacy = self.inst / "cron/cron.env"
        result = self.source(f'mosdns_effective_my_mode "{legacy}"')
        self.assertEqual(result.stdout.strip(), "1")
        self.assertEqual(self.source(f'mosdns_apply_my_mode "{self.inst}" 2').returncode, 0)
        text = legacy.read_text()
        self.assertIn("MOSDNS_MY_MODE=2\n", text)
        self.assertIn("MOSDNS_SYNC_CANDIDATES_ENABLED=0\n", text)
        self.assertIn("MOSDNS_PROMOTE_ENABLED=0\n", text)
        self.assertIn("MOSDNS_SYNC_RULES_ENABLED=0\n", text)
        self.assertIn("  level: warn\n", (self.inst / "config.yaml").read_text())

    def test_merge_appends_only_four_my_lines(self):
        upstream = self.root / "upstream.conf"
        upstream.write_text(textwrap.dedent("""\
            rule/geoip_cn.txt | no | https://example.invalid/changed
            my/whitelist.txt | yes | https://raw.githubusercontent.com/perryyeh/mosdns/main/my/whitelist.txt
            my/greylist.txt | yes | https://raw.githubusercontent.com/perryyeh/mosdns/main/my/greylist.txt
            my/blacklist.txt | yes | https://raw.githubusercontent.com/perryyeh/mosdns/main/my/blacklist.txt
            my/hosts.txt | yes | https://raw.githubusercontent.com/perryyeh/mosdns/main/my/hosts.txt
            """))
        dest = self.inst / "cron/sync.conf"
        result = self.source(f'mosdns_merge_sync_conf "{dest}" "{upstream}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = dest.read_text()
        self.assertTrue(text.startswith("geoip_cn.txt | yes | https://example.invalid/keep\n"))
        self.assertNotIn("example.invalid/changed", text)
        self.assertEqual(sum(1 for line in text.splitlines() if line.split("|", 1)[0].strip() == "my/whitelist.txt"), 1)
        bad = self.root / "bad.conf"
        bad.write_text(upstream.read_text() + "my/whitelist.txt.before-apple | yes | https://example.invalid/backup\n")
        before = dest.read_text()
        result = self.source(f'mosdns_merge_sync_conf "{dest}" "{bad}"')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(dest.read_text(), before)

    def test_existing_instance_updates_cron_without_touching_deployment_files(self):
        before = self.preserved()
        fixture = self.root / "fixture"
        fixture.mkdir()
        for name in ("fetch.sh", "my-mode.sh", "sync-target.sh", "sync-rules.sh", "sync-my.sh",
                     "sync-candidates.sh", "promote-candidates.sh", "reload-if-needed.sh"):
            (fixture / name).write_text("#!/bin/sh\ntrue\n")
        (fixture / "crontab.conf").write_text("\n".join([
            "0 0 * * * /etc/mosdns/cron/sync-rules.sh",
            "30 0 * * * /etc/mosdns/cron/reload-if-needed.sh rules",
            "0 1 * * * /etc/mosdns/cron/sync-my.sh",
            "10 1 * * * /etc/mosdns/cron/reload-if-needed.sh my",
        ]) + "\n")
        (fixture / "sync.conf").write_text(textwrap.dedent("""\
            my/whitelist.txt | yes | https://raw.githubusercontent.com/perryyeh/mosdns/main/my/whitelist.txt
            my/greylist.txt | yes | https://raw.githubusercontent.com/perryyeh/mosdns/main/my/greylist.txt
            my/blacklist.txt | yes | https://raw.githubusercontent.com/perryyeh/mosdns/main/my/blacklist.txt
            my/hosts.txt | yes | https://raw.githubusercontent.com/perryyeh/mosdns/main/my/hosts.txt
            """))
        docker_log = self.root / "docker.log"
        bindir = self.root / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            if [ "$1" = ps ]; then printf '%s\\n' 'cid123 mosdns-test'; exit 0; fi
            if [ "$1" = inspect ]; then
              case "$3" in
                *working_dir*) printf '%s\\n' '{self.inst}' ;;
                *Image*) printf '%s\\n' 'irinesistiana/mosdns:latest' ;;
              esac
              exit 0
            fi
            if [ "$1" = exec ]; then printf '%s\\n' "$*" >> '{docker_log}'; exit 0; fi
            exit 1
            """))
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        wrapper = textwrap.dedent(f"""\
            yehbp_curl() {{
              dest=""
              url=""
              prev=""
              for arg in "$@"; do
                if [ "$prev" = -o ]; then dest=$arg
                else
                  case "$arg" in
                    http://*|https://*) url=$arg ;;
                  esac
                fi
                prev=$arg
              done
              cp "{fixture}/$(basename "$url")" "$dest"
            }}
            source "{ASSET}"
            mosdns_configure_existing
            """)
        result = self.run_bash(wrapper, "1\n1\n2\n", {"PATH": f"{bindir}:{self.env['PATH']}"})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        env = (self.inst / "cron/cron.env").read_text()
        self.assertIn("MOSDNS_MY_MODE=2\n", env)
        self.assertIn("MOSDNS_SYNC_CANDIDATES_ENABLED=0\n", env)
        self.assertIn("MOSDNS_PROMOTE_ENABLED=0\n", env)
        self.assertIn("MOSDNS_SYNC_RULES_ENABLED=1\n", env)
        self.assertIn("MOSDNS_PROMOTE_LOOKBACK_DAYS=7\n", env)
        self.assertIn("sync-my.sh", (self.inst / "cron/crontab.conf").read_text())
        self.assertIn("main/my/hosts.txt", (self.inst / "cron/sync.conf").read_text())
        self.assertIn("example.invalid/keep", (self.inst / "cron/sync.conf").read_text())
        self.assertEqual(self.preserved(), before)
        self.assertIn("crontab /etc/mosdns/cron/crontab.conf", docker_log.read_text())
        self.assertNotIn("restart", docker_log.read_text())
        self.assertIn("未重启容器", result.stdout)

    def run_installer(self, inputs, deploy_rc=0, restart_rc=0):
        text = INSTALL.read_text()
        start = text.index("install_mosdns() {")
        function = text[start:text.index("\n}\n", start) + 3]
        script = textwrap.dedent(f"""\
            source "{ASSET}"
            select_macvlan_or_exit() {{ SELECTED_MACVLAN=test; }}
            select_mihomo_upstream() {{ MIHOMO_ENDPOINT4=192.0.2.120:53; MIHOMO_ENDPOINT6=; }}
            prompt_ipv4_last_octet() {{ echo 119; }}
            calculate_ip_mac() {{ calculated_ip=192.0.2.119; calculated_ip6=; calculated_mac=02:00:00:00:00:01; calculated_gateway=; }}
            docker() {{
                if [ "$1" = network ]; then return 1; fi
                echo "DOCKER $*"
                if [ "$1" = restart ]; then return {restart_rc}; fi
            }}
            jq() {{ return 1; }}
            select_dockerapps_dir() {{ SELECTED_DOCKERAPPS_DIR="{self.root}"; }}
            validate_compose_app_name() {{ return 0; }}
            repo_stage_update() {{ WORK_DIR="{self.inst}"; BAK_DIR=; }}
            sed() {{ :; }}
            disable_mosdns_fakeipv6() {{ :; }}
            load_mosdns_schedule_asset() {{ :; }}
            env_require_vars() {{ :; }}
            remove_compose_ipv6_fields() {{ :; }}
            compose_deploy_with_repo_switch() {{ echo DEPLOY; return {deploy_rc}; }}
            repo_offer_delete_backup() {{ :; }}
            mosdns_update_cron_scripts() {{ echo UPDATE_CRON; }}
            {function}
            install_mosdns
            """)
        return self.run_bash(script, inputs)

    def test_install_later_keeps_automation_off_without_cron_update(self):
        result = self.run_installer("\nn\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        env = (self.inst / "cron/cron.env").read_text()
        for key in ("MOSDNS_SYNC_RULES_ENABLED", "MOSDNS_MY_MODE",
                    "MOSDNS_SYNC_CANDIDATES_ENABLED", "MOSDNS_PROMOTE_ENABLED"):
            self.assertIn(f"{key}=0\n", env)
        self.assertLess(result.stdout.index("DEPLOY"), result.stdout.index("是否现在配置规则更新"))
        self.assertNotIn("私人规则处理方式", result.stdout)
        self.assertNotIn("UPDATE_CRON", result.stdout)
        self.assertNotIn("DOCKER restart", result.stdout)

    def test_install_now_uses_shared_flow_without_container_picker(self):
        for rules, mode in (("1", "1"), ("0", "2")):
            with self.subTest(rules=rules, mode=mode):
                result = self.run_installer(f"\nn\n1\n{rules}\n{mode}\n")
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                env = (self.inst / "cron/cron.env").read_text()
                self.assertIn(f"MOSDNS_SYNC_RULES_ENABLED={rules}\n", env)
                self.assertIn(f"MOSDNS_MY_MODE={mode}\n", env)
                self.assertLess(result.stdout.index("公共规则处理方式"),
                                result.stdout.index("私人规则处理方式"))
                self.assertNotIn("请选择 mosdns 实例", result.stdout)
                self.assertEqual(result.stdout.count("UPDATE_CRON"), 1)
                self.assertIn("DOCKER exec mosdns crontab", result.stdout)
                if mode == "1":
                    self.assertIn("DOCKER restart mosdns", result.stdout)
                else:
                    self.assertNotIn("DOCKER restart", result.stdout)

    def test_failed_install_does_not_offer_rule_configuration(self):
        result = self.run_installer("\nn\n", deploy_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("是否现在配置规则更新", result.stdout)
        self.assertNotIn("mosdns 已启动", result.stdout)

    def test_post_install_restart_failure_is_reported(self):
        result = self.run_installer("\nn\n1\n1\n1\n", restart_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("规则设置已应用", result.stdout)

    def test_invalid_or_incomplete_choices_do_not_change_config(self):
        before = (self.inst / "cron/cron.env").read_bytes()
        config = (self.inst / "config.yaml").read_bytes()
        for stdin in ("y\n2\n", "1\n9\n", "1\n", ""):
            result = self.source(f'mosdns_configure_instance "{self.inst}" mosdns', stdin)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((self.inst / "cron/cron.env").read_bytes(), before)
            self.assertEqual((self.inst / "config.yaml").read_bytes(), config)

    def test_empty_choices_preserve_current_rules_and_mode(self):
        result = self.source(f'mosdns_read_rule_choices "{self.inst}" && mosdns_apply_rule_choices "{self.inst}"', "\n\n")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        env = (self.inst / "cron/cron.env").read_text()
        self.assertIn("MOSDNS_SYNC_RULES_ENABLED=0\n", env)
        self.assertIn("MOSDNS_MY_MODE=1\n", env)

    def test_installer_menu_points_at_schedule_asset(self):
        text = INSTALL.read_text()
        self.assertIn('echo "19）安装/设置 mosdns"', text)
        self.assertIn("19) mosdns_menu ;;", text)
        self.assertIn("assets/mosdns/schedule.sh", text)
        self.assertIn("mosdns_offer_rule_configuration", text)
        self.assertNotIn("每天自动合并并上报 my 名单", text)


if __name__ == "__main__":
    unittest.main()
