"""Default directory refresh and legacy configuration compatibility."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from iptv_reference_loader import iptv as c
from test_iptv_reference_cli import config, row
from test_iptv_reference_directory import directory


class DirectoryDefaultsTests(unittest.TestCase):
    def prepare(self, root, legacy=False):
        paths = c.config_paths(root / 'instance')
        c.private_dir(paths[0])
        cfg: dict = config()
        if legacy:
            cfg['directory_metadata'] = False
        cfg['extra'] = {'preserve': [1, 1, None]}
        c.atomic_json(paths[1], cfg)
        return paths

    def test_fetch_and_run_always_refresh_directory_without_rewriting_config(self):
        for command in ('fetch', 'run'):
            for legacy in (False, True):
                with self.subTest(command=command, legacy=legacy), tempfile.TemporaryDirectory() as tmp:
                    paths = self.prepare(Path(tmp).resolve(), legacy)
                    before = paths[1].read_bytes()
                    rows = [row(UserChannelID='1'), row(UserChannelID='1')]
                    meta = directory([dict(mixNo='1', name=' 高清 HD 4K ', extra=['raw', 'raw'])])
                    context = {}
                    def login(cfg, address, context):
                        context['session'] = 'fixture'
                        return rows
                    def portal(cfg, ctx):
                        context.update(ctx)
                        return meta
                    with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', side_effect=login), patch.object(c, 'portal_directory', side_effect=portal) as fetch_directory:
                        self.assertEqual(c.main([command, 'config=' + str(paths[0])]), 0)
                    fetch_directory.assert_called_once()
                    self.assertEqual(context, {'session': 'fixture'})
                    self.assertEqual(c.read_json(paths[2]), rows)
                    self.assertEqual(c.read_json(c.metadata_path(paths)), meta)
                    self.assertEqual(paths[1].read_bytes(), before)
                    if command == 'run':
                        self.assertEqual(paths[3].read_text().count('#EXTINF:'), 2)
                        self.assertIn(' 高清 HD 4K ', paths[3].read_text())

    def test_first_full_parameters_refresh_directory_and_save_no_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            paths = c.config_paths(root / 'instance')
            meta = directory([dict(mixNo='1', name='高清 HD 4K')])
            args = ['config=' + str(paths[0]), 'userinfo=fixture', 'SN=box',
                    'MAC=00:11:22:33:44:55', 'interface=eth0.85']
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(UserChannelID='1')]), patch.object(c, 'portal_directory', return_value=meta) as portal:
                self.assertEqual(c.main(args), 0)
            portal.assert_called_once()
            self.assertNotIn('directory_metadata', c.load(paths[1]))
            self.assertEqual(c.read_json(c.metadata_path(paths)), meta)
            self.assertIn('高清 HD 4K', paths[3].read_text())
            cfg = c.load(paths[1]); cfg['directory_metadata'] = False
            c.atomic_json(paths[1], cfg)
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row()]), patch.object(c, 'portal_directory', return_value=meta):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'baseURL=http://127.0.0.1:5140']), 0)
            self.assertNotIn('directory_metadata', c.load(paths[1]))

    def test_interactive_prompts_keep_identity_and_field_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); paths = c.config_paths(root / 'new')
            lines = ['fixture', 'box', '00:11:22:33:44:55', 'eth0.85', 'eth0.51',
                     'eth0.86', c.DEFAULT_ORIGIN, 'http://127.0.0.1:5555',
                     '/udp/', '/proxy/rtsp/', '${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}',
                     'http://example.test:8080']
            with patch.object(c.getpass, 'getpass', side_effect=lines), patch.object(c.socket, 'socket', side_effect=AssertionError('network forbidden')):
                self.assertEqual(c.main(['configure', 'config=' + str(paths[0])]), 0)
            cfg = c.load(paths[1])
            keys = ['userid', 'sn', 'mac', 'interface', 'multicast_interface', 'fcc_interface',
                    'auth_origin', 'base_url', 'live_path', 'replay_path', 'seek_template']
            self.assertEqual([cfg[k] for k in keys], lines[:-1])
            self.assertEqual(cfg['allowed_origins'], [lines[-1]])
            self.assertNotIn('directory_metadata', cfg)
            cfg['directory_metadata'] = False; cfg['extra'] = 'keep'
            c.atomic_json(paths[1], cfg)
            before = paths[1].read_bytes()
            with patch.object(c.getpass, 'getpass', side_effect=[''] * 11), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.main(['configure', 'config=' + str(paths[0])]), 1)
            self.assertEqual(paths[1].read_bytes(), before)
            with patch.object(c.getpass, 'getpass', return_value=''):
                self.assertEqual(c.main(['configure', 'config=' + str(paths[0])]), 0)
            cfg.pop('directory_metadata')
            self.assertEqual(c.load(paths[1]), cfg)

    def test_legacy_false_offline_export_forbids_network_and_keeps_input_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); paths = self.prepare(root, legacy=True)
            c.atomic_json(paths[2], [row(UserChannelID='1')])
            c.atomic_json(c.metadata_path(paths), directory([dict(mixNo='1', name=' HD 高清 4K ')]))
            before = {p: p.read_bytes() for p in (paths[1], paths[2], c.metadata_path(paths))}
            with patch.object(c.socket, 'socket', side_effect=AssertionError('network forbidden')), patch.object(c, 'login', side_effect=AssertionError('login forbidden')), patch.object(c, 'portal_directory', side_effect=AssertionError('portal forbidden')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'output=' + str(root / 'copy.m3u')]), 0)
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            self.assertEqual(paths[3].read_bytes(), (root / 'copy.m3u').read_bytes())
            self.assertIn(' HD 高清 4K ', paths[3].read_text())

    def test_removed_cli_switch_fails_clearly_without_mutation_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), legacy=True)
            before = paths[1].read_bytes(); err = io.StringIO()
            with contextlib.redirect_stderr(err), patch.object(c, 'fetch', side_effect=AssertionError('fetch forbidden')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'directory_metadata=false']), 1)
            self.assertIn('directory_metadata', err.getvalue())
            self.assertIn('无需', err.getvalue())
            self.assertEqual(paths[1].read_bytes(), before)

    def test_directory_failure_in_run_preserves_all_outputs_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), legacy=True)
            c.atomic_json(paths[2], [row()])
            c.atomic_json(c.metadata_path(paths), directory([dict(mixNo='1', name='Old')]))
            c.convert(paths)
            before = {p: p.read_bytes() for p in (paths[1], paths[2], paths[3], c.metadata_path(paths))}
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(), row()]), patch.object(c, 'portal_directory', side_effect=c.SafeError('fixture')), patch.object(c, 'convert', side_effect=AssertionError('convert forbidden')), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.main(['run', 'config=' + str(paths[0])]), 1)
            self.assertEqual(before, {p: p.read_bytes() for p in before})
