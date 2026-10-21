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
                    'MAC=00:11:22:33:44:55', 'interface=eth0.85', 'filter=false']
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(UserChannelID='1')]), patch.object(c, 'portal_directory', return_value=meta) as portal:
                self.assertEqual(c.main(args), 0)
            portal.assert_called_once()
            self.assertNotIn('directory_metadata', c.load(paths[1]))
            self.assertEqual(c.read_json(c.metadata_path(paths)), meta)
            self.assertIn('高清 HD 4K', paths[3].read_text())
            cfg = c.load(paths[1]); cfg['directory_metadata'] = False
            c.atomic_json(paths[1], cfg)
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row()]), patch.object(c, 'portal_directory', return_value=meta):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'rtp2http=http://127.0.0.1:5140']), 0)
            self.assertNotIn('directory_metadata', c.load(paths[1]))

    def test_interactive_prompts_keep_identity_and_field_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); paths = c.config_paths(root / 'new')
            lines = ['fixture', 'box', '00:11:22:33:44:55', 'eth0.85',
                     'http://127.0.0.1:5555',
                     '/udp/', '/proxy/rtsp/', '${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}',
                     c.DEFAULT_ORIGIN]
            with patch.object(c.getpass, 'getpass', side_effect=lines), patch.object(c.socket, 'socket', side_effect=AssertionError('network forbidden')):
                self.assertEqual(c.main(['configure', 'config=' + str(paths[0])]), 0)
            cfg = c.load(paths[1])
            keys = ['userid', 'sn', 'mac', 'interface',
                    'auth_origin', 'base_url', 'live_path', 'replay_path', 'seek_template']
            self.assertEqual([cfg[k] for k in keys], lines[:-1][:4] + [c.DEFAULT_ORIGIN] + lines[4:-1])
            self.assertEqual(cfg['allowed_origins'], [])
            self.assertNotIn('directory_metadata', cfg)
            cfg['directory_metadata'] = False; cfg['extra'] = 'keep'
            c.atomic_json(paths[1], cfg)
            before = c.load(paths[1])
            with patch.object(c.getpass, 'getpass', side_effect=[''] * 9):
                self.assertEqual(c.main(['configure', 'config=' + str(paths[0])]), 0)
            saved = c.load(paths[1])
            self.assertNotIn('directory_metadata', saved)
            before.pop('directory_metadata')
            self.assertEqual(saved, before)
            with patch.object(c.getpass, 'getpass', return_value=''):
                self.assertEqual(c.main(['configure', 'config=' + str(paths[0])]), 0)
            self.assertEqual(c.load(paths[1]), saved)

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

    def test_optional_playback_overrides_keep_clear_and_reject_old_address_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve())
            cfg = config()
            cfg['multicast_interface'] = 'eth0.51'
            cfg['fcc_interface'] = 'eth0.86'
            c.atomic_json(paths[1], cfg)
            c.atomic_json(paths[2], [row(UserChannelID='1', ChannelFCCIP='10.0.0.1', ChannelFCCPort='8027')])
            before = paths[1].read_bytes()
            with patch.object(c.socket, 'socket', side_effect=AssertionError('network forbidden')), patch.object(c, 'login', side_effect=AssertionError('login forbidden')):
                self.assertEqual(c.main(['config=' + str(paths[0])]), 0)
            self.assertEqual(paths[1].read_bytes(), before)
            text = paths[3].read_text()
            self.assertIn('fcc=10.0.0.1:8027', text)
            self.assertIn('r2h-ifname-fcc=eth0.86', text)
            self.assertIn('r2h-ifname=eth0.51', text)
            self.assertNotIn('r2h-ifname=eth0.85', text)
            catchup = text.split('catchup-source="', 1)[1].split('"', 1)[0]
            self.assertNotIn('r2h-ifname', catchup)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), patch.object(c, 'fetch', side_effect=AssertionError('fetch forbidden')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'userinfo=']), 1)
                self.assertEqual(c.main(['config=' + str(paths[0]), 'interface=']), 1)
                self.assertEqual(c.main(['config=' + str(paths[0]), 'baseURL=http://127.0.0.1:5140']), 1)
                self.assertEqual(c.main(['config=' + str(paths[0]), 'rtp2http=http://127.0.0.1:9', 'base_url=http://127.0.0.1:8']), 1)
                self.assertEqual(c.main(['config=' + str(paths[0]), 'allowed_origins=http://example.test']), 1)
            message = err.getvalue()
            self.assertIn('配置字段缺失', message)
            self.assertIn('未知或重复参数键', message)
            self.assertEqual(paths[1].read_bytes(), before)
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(ChannelFCCIP='10.0.0.1', ChannelFCCPort='8027')]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'multicast_interface=']), 0)
            saved = c.load(paths[1])
            self.assertEqual(saved['multicast_interface'], '')
            self.assertEqual(saved['fcc_interface'], 'eth0.86')
            cleared = paths[3].read_text()
            self.assertIn('r2h-ifname-fcc=eth0.86', cleared)
            self.assertNotIn('r2h-ifname=eth0.51', cleared)
            self.assertNotIn('r2h-ifname=', cleared.split('r2h-ifname-fcc')[-1])
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(ChannelFCCIP='10.0.0.1', ChannelFCCPort='8027')]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'multicast_interface=', 'fcc_interface=']), 0)
            both = c.load(paths[1])
            self.assertEqual(both['multicast_interface'], '')
            self.assertEqual(both['fcc_interface'], '')
            playlist = paths[3].read_text()
            self.assertIn('fcc=10.0.0.1:8027', playlist)
            self.assertNotIn('r2h-ifname', playlist)
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1') as bound, patch.object(c, 'login', return_value=[row()]) as login, patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'rtp2http=http://192.0.2.10:5140']), 0)
            bound.assert_called_with('eth0.85')
            self.assertEqual(login.call_args.args[1], '192.0.2.1')
            self.assertEqual(c.load(paths[1])['base_url'], 'http://192.0.2.10:5140')
            self.assertIn('http://192.0.2.10:5140/rtp/', paths[3].read_text())
            seen = []
            def login(cfg, address, context=None):
                seen.append(cfg['auth_origin'])
                return [row()]
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', side_effect=login), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'auth=http://203.0.113.10:7001 http://203.0.113.11:7001', 'live=/udp/', 'replay=/proxy/rtsp/', 'seek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}']), 0)
            saved = c.load(paths[1])
            self.assertEqual(seen, ['http://203.0.113.10:7001'])
            self.assertEqual(saved['auth_origin'], 'http://203.0.113.10:7001')
            self.assertEqual(saved['allowed_origins'], ['http://203.0.113.11:7001'])
            self.assertEqual(saved['live_path'], '/udp/')
            self.assertEqual(saved['replay_path'], '/proxy/rtsp/')
            self.assertTrue(saved['seek_template'].startswith('${(b)'))
            self.assertTrue(c.trusted('http://203.0.113.11:7001/next', saved))
            with self.assertRaises(c.SafeError):
                c.trusted('http://203.0.113.12:7001/next', saved)
            self.assertTrue(c.trusted('http://124.75.29.176:7001/next', saved))
            entry = saved['auth_origin'].rstrip('/') + '/iptv3a/4kLogAuth.do'
            self.assertTrue(entry.startswith('http://203.0.113.10:7001/'))
            self.assertNotIn('203.0.113.11', entry)
            rendered = paths[3].read_text()
            self.assertIn('/udp/239.', rendered)
            self.assertIn('/proxy/rtsp/', rendered)
            self.assertIn('${(b)yyyyMMddHHmmss}', rendered)
            before = paths[1].read_bytes()
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row()]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'rtp2http=http://192.0.2.10:5140']), 0)
            kept = c.load(paths[1])
            self.assertEqual(kept['allowed_origins'], ['http://203.0.113.11:7001'])
            self.assertEqual(kept['auth_origin'], 'http://203.0.113.10:7001')
            self.assertEqual(kept['live_path'], '/udp/')


if __name__ == '__main__':
    unittest.main()
