"""Exercise installer-only Dockcheck dependencies without installing packages."""
import re
import subprocess
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "install.sh").read_text()


def function(name):
    start = SOURCE.index(name + "() {")
    end = SOURCE.index("\n}", start) + 2
    return SOURCE[start:end]


class DockcheckDependenciesTest(unittest.TestCase):
    def run_case(self, manager="apk", compatible=False, install_ok=True,
                 fixes_xargs=True, openwrt=True, update_ok=True):
        setup = f"""
PATH=/usr/bin:/bin
COMPAT={int(compatible)}
is_openwrt() {{ return {0 if openwrt else 1}; }}
flock() {{ :; }}
xargs() {{ [ "$COMPAT" = 1 ]; }}
"""
        if manager:
            setup += f"""
{manager}() {{
    printf 'PKG %s\\n' "$*"
    if [ "$1" = update ]; then return {0 if update_ok else 1}; fi
    COMPAT={int(fixes_xargs)}
    return {0 if install_ok else 1}
}}
"""
        code = setup + function("openwrt_dockcheck_xargs_compatible") + "\n"
        code += function("install_openwrt_dockcheck_dependencies") + "\n"
        code += "install_openwrt_dockcheck_dependencies\n"
        return subprocess.run(["/bin/bash", "-c", code], text=True,
                              capture_output=True)

    def test_apk_installs_only_xargs(self):
        result = self.run_case()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PKG update", result.stdout)
        self.assertIn("PKG add findutils-xargs", result.stdout)
        self.assertNotIn("PKG add findutils\n", result.stdout)

    def test_opkg_keeps_existing_package_name(self):
        result = self.run_case(manager="opkg")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PKG install findutils", result.stdout)

    def test_compatible_xargs_does_not_install(self):
        result = self.run_case(compatible=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("PKG", result.stdout)

    def test_non_openwrt_does_not_install(self):
        result = self.run_case(openwrt=False)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("PKG", result.stdout)

    def test_install_failure_stops(self):
        self.assertNotEqual(self.run_case(install_ok=False).returncode, 0)

    def test_index_failure_does_not_install(self):
        result = self.run_case(update_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PKG add", result.stdout)

    def test_installed_but_incompatible_xargs_fails(self):
        result = self.run_case(fixes_xargs=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("xargs 仍不支持", result.stdout)

    def test_no_package_manager_fails_clearly(self):
        result = self.run_case(manager="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未找到 apk 或 opkg", result.stdout)

    def test_dependency_helper_called_only_by_install(self):
        calls = list(re.finditer(r"^\s+install_openwrt_dockcheck_dependencies \|\| return 1$",
                                 SOURCE, re.MULTILINE))
        self.assertEqual(len(calls), 1)
        self.assertIn(calls[0].group(0), function("install_dockcheck_auto_update"))


if __name__ == "__main__":
    unittest.main()
