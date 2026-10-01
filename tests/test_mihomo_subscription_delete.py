"""Local deletion-flow regression tests; Docker calls are isolated shell mocks."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
HARNESS = r'''
source "$ASSET"
mihomo_subscription_select_target() {
  MIHOMO_SUBSCRIPTION_CONTAINER=selected-mihomo
  MIHOMO_SUBSCRIPTION_DIR="$TARGET"
  return "${SELECT_STATUS:-0}"
}
mihomo_subscription_runtime_ready() { echo unexpected-runtime >> "$TRACE"; return 99; }
mihomo_subscription_install_script() { echo unexpected-download >> "$TRACE"; return 99; }
mihomo_subscription_remove_legacy_timer() {
  test ! -e "$TARGET/subscription.conf" || return 98
  echo timer-cleanup >> "$TRACE"
  return "${TIMER_STATUS:-0}"
}
docker() {
  test "$#" = 5 && test "$1" = exec && test "$2" = selected-mihomo &&
    test "$3" = sh && test "$4" = -c || return 97
  echo delete-metadata >> "$TRACE"
  local script="$5"
  script="${script//\/root\/.config\/mihomo/$TARGET}"
  /bin/sh -c "$script"
}
mihomo_subscription_delete
'''


class SubscriptionDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "mihomo"
        self.target.mkdir()
        self.other = self.root / "mihomoin"
        self.other.mkdir()
        self.config = b"current configuration\n"
        (self.target / "config.yaml").write_bytes(self.config)
        for name in ("subscription.conf", "subscription.log", ".subscription.next-run"):
            (self.target / name).write_text("fixture\n")
        self.backup = self.target / "config.macvlan.backup.yaml"
        self.backup.write_bytes(b"original backup\n")
        self.updater = self.target / "subscription.sh"
        self.updater.write_text("untouched updater\n")
        (self.other / "config.yaml").write_bytes(b"other instance\n")
        self.trace = self.root / "trace"

    def run_flow(self, answers="y\n", **extra):
        env = dict(os.environ, ASSET=str(REPO / "assets/mihomo/subscription.sh"),
                   TARGET=str(self.target), TRACE=str(self.trace), **extra)
        result = subprocess.run(["bash", "-c", HARNESS], input=answers, text=True,
                                capture_output=True, env=env, timeout=10)
        self.assertEqual((self.target / "config.yaml").read_bytes(), self.config)
        self.assertEqual((self.other / "config.yaml").read_bytes(), b"other instance\n")
        self.assertEqual(self.updater.read_text(), "untouched updater\n")
        return result

    def assert_metadata(self, exists):
        for name in ("subscription.conf", "subscription.log", ".subscription.next-run"):
            self.assertEqual((self.target / name).exists(), exists, name)

    def test_delete_preserves_config_and_backup_without_restore(self):
        result = self.run_flow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_metadata(False)
        self.assertEqual(self.backup.read_bytes(), b"original backup\n")
        self.assertFalse((self.target / ".subscription.lock").exists())
        self.assertEqual(self.trace.read_text().splitlines(), ["delete-metadata", "timer-cleanup"])
        self.assertIn("保留了当前配置", result.stdout)
        self.assertNotIn("如何处理", result.stdout)

    def test_no_backup_required(self):
        self.backup.unlink()
        result = self.run_flow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_metadata(False)

    def test_cancel_changes_nothing(self):
        result = self.run_flow("n\n")
        self.assertEqual(result.returncode, 0)
        self.assert_metadata(True)
        self.assertTrue(self.backup.exists())
        self.assertFalse(self.trace.exists())

    def test_eof_cancels(self):
        result = self.run_flow("")
        self.assertEqual(result.returncode, 0)
        self.assert_metadata(True)
        self.assertFalse(self.trace.exists())

    def test_busy_updater_refuses_deletion(self):
        lock = self.target / ".subscription.lock"
        lock.mkdir()
        result = self.run_flow()
        self.assertNotEqual(result.returncode, 0)
        self.assert_metadata(True)
        self.assertTrue(lock.exists())
        self.assertTrue(self.backup.exists())
        self.assertEqual(self.trace.read_text().splitlines(), ["delete-metadata"])
        self.assertIn("未删除订阅", result.stdout)

    def test_target_selection_cancel(self):
        result = self.run_flow(SELECT_STATUS="2")
        self.assertEqual(result.returncode, 2)
        self.assert_metadata(True)
        self.assertFalse(self.trace.exists())

    def test_timer_failure_reports_partial_cleanup(self):
        result = self.run_flow(TIMER_STATUS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assert_metadata(False)
        self.assertTrue(self.backup.exists())
        self.assertIn("旧定时任务清理失败", result.stdout)
        self.assertNotIn("✅", result.stdout)

    def test_repeated_delete_is_safe(self):
        self.assertEqual(self.run_flow().returncode, 0)
        self.assertEqual(self.run_flow().returncode, 0)
        self.assert_metadata(False)
        self.assertTrue(self.backup.exists())


if __name__ == "__main__":
    unittest.main()
