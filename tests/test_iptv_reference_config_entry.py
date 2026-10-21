"""Unified config= entry for every IPTV operation. Synthetic data only."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iptv_reference_loader import iptv as c
from test_iptv_reference_cli import config, row
from test_iptv_reference_directory import directory


class UnifiedConfigEntryTests(unittest.TestCase):
    def prepare(self, root):
        paths = c.config_paths(root / 'tel')
        c.private_dir(paths[0])
        c.atomic_json(paths[1], config())
        c.atomic_json(paths[2], [row(UserChannelID='1')])
        c.atomic_json(c.metadata_path(paths), directory([dict(mixNo='1', name='fixture')]))
        return paths

    def test_commands_accept_config_relative_to_script_not_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            script = root / 'iptv.py'
            script.write_bytes(Path(c.__file__).read_bytes())
            paths = c.config_paths(root / 'tel')
            c.private_dir(paths[0])
            c.atomic_json(paths[1], config())
            c.atomic_json(paths[2], [row(UserChannelID='1')])
            c.atomic_json(c.metadata_path(paths), directory([dict(mixNo='1', name='fixture')]))
            elsewhere = root / 'cwd'
            elsewhere.mkdir()
            meta = directory([dict(mixNo='1', name='refreshed')])
            with patch.object(c, '__file__', str(script)), patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(UserChannelID='1')]) as login, patch.object(c, 'portal_directory', return_value=meta) as portal, patch.object(c.socket, 'socket', side_effect=AssertionError('offline convert')):
                self.assertEqual(c.main(['convert', 'config=tel']), 0)
                self.assertEqual(login.call_count, 0)
                self.assertEqual(c.main(['run', 'config=tel']), 0)
                self.assertEqual(login.call_count, 1)
                portal.assert_called_once()
                self.assertEqual(c.main(['fetch', 'config=tel']), 0)
                self.assertEqual(login.call_count, 2)
            result = subprocess.run(
                [sys.executable, str(script), 'convert', 'config=tel'],
                cwd=elsewhere, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(elsewhere.iterdir()), [])
            self.assertTrue(paths[3].is_file())

    def test_bare_run_fetches_and_default_convert_stays_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve())
            with patch.object(c.socket, 'socket', side_effect=AssertionError('offline')), patch.object(c, 'login', side_effect=AssertionError('login forbidden')):
                self.assertEqual(c.main(['config=' + str(paths[0])]), 0)
                self.assertEqual(c.main(['convert', 'config=' + str(paths[0])]), 0)
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row()]) as login, patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])):
                self.assertEqual(c.main(['run', 'config=' + str(paths[0])]), 0)
            login.assert_called_once()

    def test_configure_prompts_hide_identity_and_skip_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = c.config_paths(Path(tmp).resolve() / 'tel')
            lines = ['fixture', 'box', '00:11:22:33:44:55', 'eth0.85',
                     'http://127.0.0.1:5555',
                     '/udp/', '/proxy/rtsp/', '${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}',
                     c.DEFAULT_ORIGIN]
            with patch.object(c.getpass, 'getpass', side_effect=lines), patch.object(c.socket, 'socket', side_effect=AssertionError('network forbidden')), patch.object(c, 'login', side_effect=AssertionError('login forbidden')):
                self.assertEqual(c.main(['configure', 'config=' + str(paths[0])]), 0)
            cfg = c.load(paths[1])
            keys = ['userid', 'sn', 'mac', 'interface',
                    'authurl', 'rtp2http', 'live', 'replay', 'seek']
            self.assertEqual([cfg[k] for k in keys], lines[:4] + [c.DEFAULT_ORIGIN] + lines[4:-1])
            self.assertEqual(cfg['allowed_origins'], [])
            self.assertFalse(paths[2].exists())

    def test_help_uses_config_examples_and_legacy_flags_still_work(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(c.main(['--help']), 0)
        text = out.getvalue()
        self.assertIn('config=tel', text)
        self.assertIn('run config=tel', text)
        self.assertNotIn('configure config=tel', text)
        self.assertNotIn('--stdin', text)
        self.assertNotIn('--config-dir', text)
        self.assertNotIn('--output-dir', text)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(c.main(['--config-dir', str(paths[0])]), 1)
                self.assertEqual(c.main(['config=' + str(paths[0]), '--output-dir', str(paths[0])]), 1)
            self.assertIn('config=', err.getvalue())
            self.assertIn('output=', err.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err), patch.object(c, 'fetch', side_effect=AssertionError('fetch forbidden')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'directory_metadata=false']), 1)
            self.assertIn('directory_metadata', err.getvalue())

    def test_output_rules_create_copy_and_skip_uncreatable_directory(self):
        cases = [
            ('file.m3u', lambda root: root / 'cwd' / 'file.m3u'),
            ('/ABS/channels.M3U', lambda root: root / 'channels.M3U'),
            ('rel/list.m3u', lambda root: root / 'cwd' / 'rel' / 'list.m3u'),
            ('exports', lambda root: root / 'cwd' / 'exports' / 'tel.m3u'),
            ('lists.backup', lambda root: root / 'cwd' / 'lists.backup' / 'tel.m3u'),
            ('nested/missing/dir', lambda root: root / 'cwd' / 'nested' / 'missing' / 'dir' / 'tel.m3u'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            paths = c.config_paths(root / 'tel')
            c.private_dir(paths[0])
            c.atomic_json(paths[1], config())
            c.atomic_json(paths[2], [row(UserChannelID='1')])
            c.atomic_json(c.metadata_path(paths), directory([dict(mixNo='1', name='fixture')]))
            cwd = root / 'cwd'
            cwd.mkdir()
            for value, expected in cases:
                with self.subTest(value=value):
                    target = expected(root)
                    if value.startswith('/ABS/'):
                        value = str(root / value[5:])
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out), patch.object(c.os, 'getcwd', return_value=str(cwd)), patch.object(c.socket, 'socket', side_effect=AssertionError('offline')):
                        self.assertEqual(c.main(['config=' + str(paths[0]), 'output=' + value]), 0)
                    self.assertEqual(target.read_bytes(), paths[3].read_bytes())
                    self.assertIn('本次生成的播放列表在：' + str(target.resolve()), out.getvalue())
            blocked = root / 'not-a-dir'
            blocked.write_text('file')
            old = (blocked / 'tel.m3u')
            primary = paths[3].read_bytes()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(c.os, 'getcwd', return_value=str(cwd)), patch.object(c.socket, 'socket', side_effect=AssertionError('offline')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'output=' + str(blocked / 'child')]), 0)
            self.assertFalse(old.exists())
            self.assertEqual(paths[3].read_bytes(), primary)
            combined = out.getvalue() + err.getvalue()
            self.assertIn('新建目录失败，未导出播放列表。原播放列表在：' + str(paths[3].resolve()), combined)
            self.assertNotIn('本次生成的播放列表在：', combined)
            occupied = cwd / 'occupied.m3u'
            occupied.mkdir()
            err = io.StringIO()
            with contextlib.redirect_stderr(err), patch.object(c.os, 'getcwd', return_value=str(cwd)), patch.object(c.socket, 'socket', side_effect=AssertionError('offline')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'output=occupied.m3u']), 1)
            self.assertIn('不是目录', err.getvalue())
            self.assertEqual(list(occupied.iterdir()), [])
            existing = root / 'already.m3u'
            existing.write_text('old copy')
            with patch.object(c.os, 'getcwd', return_value=str(cwd)):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'output=' + str(existing)]), 0)
            self.assertEqual(existing.read_bytes(), paths[3].read_bytes())


if __name__ == '__main__':
    unittest.main()
