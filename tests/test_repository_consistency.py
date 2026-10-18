"""Repository contracts previously checked by temporary review probes."""
from pathlib import Path
import re
import unittest

REPO = Path(__file__).resolve().parents[1]


class RepositoryConsistencyTests(unittest.TestCase):
    def test_version_matches_installer(self):
        version = (REPO / 'VERSION').read_text().strip()
        self.assertRegex(version, r'^\d{4}\.\d{2}\.\d{2}\.\d{2}$')
        source = (REPO / 'install.sh').read_text()
        versions = re.findall(r'^APP_VERSION="([^"]+)"$', source, re.M)
        self.assertEqual(versions, [version])

    def test_readme_documents_all_numeric_menu_entries(self):
        source = (REPO / 'install.sh').read_text()
        menu = source.split('function show_menu()', 1)[1].split('# ========== 工具函数', 1)[0]
        entries = set(re.findall(r'echo "(\d+)）', menu))
        readme = (REPO / 'README.md').read_text()
        table = readme.split('## ⚙️ 脚本菜单说明', 1)[1].split('## 🚀 使用方法', 1)[0]
        documented = set(re.findall(r'^\| (\d+)\s*(?:\||/)', table, re.M))
        self.assertTrue(entries, 'no menu entries found')
        self.assertEqual(entries, documented)


if __name__ == '__main__':
    unittest.main()
