"""Default directory refresh and legacy configuration compatibility."""
import contextlib
import io
import json
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
        cfg.pop('filter', None)
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
                    url = c.live_url(rows[0], c.load(paths[1]))
                    c.atomic_json(c.resolution_path(paths), {'schema': 1, 'channels': {
                        rows[0]['ChannelID'] + '\n' + c.live_fingerprint(url): {
                            'status': 'identified', 'width': 1920, 'height': 1080}}})
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
                    saved = c.load(paths[1])
                    self.assertEqual(saved['filter'], c.default_filter(True))
                    saved.pop('filter')
                    self.assertEqual(saved, json.loads(before))
                    if command == 'run':
                        self.assertEqual(paths[3].read_text().count('#EXTINF:'), 2)
                        self.assertIn(' 高清 HD 4K ', paths[3].read_text())

    def test_first_full_parameters_refresh_directory_and_save_no_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            paths = c.config_paths(root / 'instance')
            meta = directory([dict(mixNo='1', name='高清 HD 4K')])
            args = ['config=' + str(paths[0]), 'userid=fixture', 'sn=box',
                    'mac=00:11:22:33:44:55', 'interface=eth0.85', 'filter=false']
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
            self.assertEqual(c.load(paths[1])['filter'], c.default_filter(False))

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
                    'authurl', 'rtp2http', 'live', 'replay', 'seek']
            self.assertEqual([cfg[k] for k in keys], lines[:-1][:4] + [c.DEFAULT_ORIGIN] + lines[4:-1])
            self.assertEqual(cfg['allowed_origins'], [])
            self.assertEqual(cfg['filter'], c.default_filter(True))
            self.assertNotIn('directory_metadata', cfg)
            cfg['directory_metadata'] = False; cfg['extra'] = 'keep'; cfg.pop('filter')
            c.atomic_json(paths[1], cfg)
            before = c.load(paths[1])
            with patch.object(c.getpass, 'getpass', side_effect=[''] * 9):
                self.assertEqual(c.main(['configure', 'config=' + str(paths[0])]), 0)
            saved = c.load(paths[1])
            self.assertNotIn('directory_metadata', saved)
            before.pop('directory_metadata')
            self.assertEqual(saved.pop('filter'), c.default_filter(True))
            self.assertEqual(saved, before)
            with patch.object(c.getpass, 'getpass', return_value=''):
                self.assertEqual(c.main(['configure', 'config=' + str(paths[0])]), 0)
            again = c.load(paths[1])
            again.pop('filter')
            self.assertEqual(again, saved)

    def test_legacy_false_offline_export_forbids_network_and_keeps_input_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); paths = self.prepare(root, legacy=True)
            cfg = c.load(paths[1]); cfg['filter'] = False
            c.atomic_json(paths[1], cfg)
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
            cfg = c.load(paths[1])
            cfg['filter'] = False
            c.atomic_json(paths[1], cfg)
            c.convert(paths)
            before = {p: p.read_bytes() for p in (paths[1], paths[2], paths[3], c.metadata_path(paths))}
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(), row()]), patch.object(c, 'portal_directory', side_effect=c.SafeError('fixture')), patch.object(c, 'convert', side_effect=AssertionError('convert forbidden')), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.main(['run', 'config=' + str(paths[0])]), 1)
            self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_optional_playback_overrides_keep_clear_and_reject_old_address_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve())
            cfg = config()
            cfg['multicast'] = 'eth0.51'
            cfg['fcc'] = 'eth0.86'
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
                self.assertEqual(c.main(['config=' + str(paths[0]), 'userid=']), 1)
                self.assertEqual(c.main(['config=' + str(paths[0]), 'interface=']), 1)
                self.assertEqual(c.main(['config=' + str(paths[0]), 'baseURL=http://127.0.0.1:5140']), 1)
                self.assertEqual(c.main(['config=' + str(paths[0]), 'rtp2http=http://127.0.0.1:9', 'base_url=http://127.0.0.1:8']), 1)
                self.assertEqual(c.main(['config=' + str(paths[0]), 'allowed_origins=http://example.test']), 1)
                for old in ('userinfo=x', 'SN=x', 'MAC=00:11:22:33:44:55', 'UserID=x', 'Sn=x', 'Mac=00:11:22:33:44:55',
                            'multicast_interface=eth0', 'fcc_interface=eth0', 'auth=http://203.0.113.10:7001',
                            'auth_origin=http://203.0.113.10:7001', 'epg_url=http://203.0.113.10/epg.xml',
                            'base_url=http://127.0.0.1:5140', 'live_path=/rtp/', 'replay_path=/rtsp/', 'seek_template=x'):
                    self.assertEqual(c.main(['config=' + str(paths[0]), old]), 1)
            message = err.getvalue()
            self.assertIn('配置字段缺失', message)
            self.assertIn('未知或重复参数键', message)
            self.assertEqual(paths[1].read_bytes(), before)
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(ChannelFCCIP='10.0.0.1', ChannelFCCPort='8027')]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'multicast=']), 0)
            saved = c.load(paths[1])
            self.assertEqual(saved['multicast'], '')
            self.assertEqual(saved['fcc'], 'eth0.86')
            cleared = paths[3].read_text()
            self.assertIn('r2h-ifname-fcc=eth0.86', cleared)
            self.assertNotIn('r2h-ifname=eth0.51', cleared)
            self.assertNotIn('r2h-ifname=', cleared.split('r2h-ifname-fcc')[-1])
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(ChannelFCCIP='10.0.0.1', ChannelFCCPort='8027')]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'multicast=', 'fcc=']), 0)
            both = c.load(paths[1])
            self.assertEqual(both['multicast'], '')
            self.assertEqual(both['fcc'], '')
            playlist = paths[3].read_text()
            self.assertIn('fcc=10.0.0.1:8027', playlist)
            self.assertNotIn('r2h-ifname', playlist)
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1') as bound, patch.object(c, 'login', return_value=[row()]) as login, patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'rtp2http=http://192.0.2.10:5140']), 0)
            bound.assert_called_with('eth0.85')
            self.assertEqual(login.call_args.args[1], '192.0.2.1')
            self.assertEqual(c.load(paths[1])['rtp2http'], 'http://192.0.2.10:5140')
            self.assertIn('http://192.0.2.10:5140/rtp/', paths[3].read_text())
            seen = []
            def login(cfg, address, context=None):
                seen.append(cfg['authurl'])
                return [row()]
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', side_effect=login), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'authurl=http://203.0.113.10:7001 http://203.0.113.11:7001', 'live=/udp/', 'replay=/proxy/rtsp/', 'seek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}']), 0)
            saved = c.load(paths[1])
            self.assertEqual(seen, ['http://203.0.113.10:7001'])
            self.assertEqual(saved['authurl'], 'http://203.0.113.10:7001')
            self.assertEqual(saved['allowed_origins'], ['http://203.0.113.11:7001'])
            self.assertEqual(saved['live'], '/udp/')
            self.assertEqual(saved['replay'], '/proxy/rtsp/')
            self.assertTrue(saved['seek'].startswith('${(b)'))
            self.assertTrue(c.trusted('http://203.0.113.11:7001/next', saved))
            with self.assertRaises(c.SafeError):
                c.trusted('http://203.0.113.12:7001/next', saved)
            self.assertTrue(c.trusted('http://124.75.29.176:7001/next', saved))
            entry = saved['authurl'].rstrip('/') + '/iptv3a/4kLogAuth.do'
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
            self.assertEqual(kept['authurl'], 'http://203.0.113.10:7001')
            self.assertEqual(kept['live'], '/udp/')

    def legacy_config(self):
        fresh = config()
        fresh.update(multicast='eth0.51', fcc='eth0.86', epg='http://203.0.113.10/guide.xml',
                     filter=dict(enabled=True, min_height=720, exclude_names=['购物'], exclude_channels=['YOUR_CHANNEL_ID']),
                     channel_names={'YOUR_CHANNEL_ID': '显示名'},
                     allowed_origins=['http://203.0.113.11:7001'])
        old = dict(fresh)
        for new, previous in {
                'multicast': 'multicast_interface', 'fcc': 'fcc_interface', 'authurl': 'auth_origin',
                'rtp2http': 'base_url', 'live': 'live_path', 'replay': 'replay_path',
                'seek': 'seek_template', 'epg': 'epg_url'}.items():
            old[previous] = old.pop(new)
        return fresh, old

    def test_save_migrates_stored_keys_once_without_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = c.config_paths(Path(tmp).resolve() / 'tel')
            c.private_dir(paths[0])
            fresh, old = self.legacy_config()
            c.atomic_json(paths[1], old)
            with patch.object(c, 'login', side_effect=AssertionError('login')):
                c.save_filter(paths, True)
            saved = c.load(paths[1])
            for key in ('multicast', 'fcc', 'authurl', 'rtp2http', 'live', 'replay', 'seek', 'epg'):
                self.assertEqual(saved[key], fresh[key])
            for previous in ('multicast_interface', 'fcc_interface', 'auth_origin', 'base_url', 'live_path', 'replay_path', 'seek_template', 'epg_url'):
                self.assertNotIn(previous, saved)
            self.assertEqual(saved['filter'], fresh['filter'])
            self.assertEqual(saved['channel_names'], fresh['channel_names'])
            self.assertEqual(saved['allowed_origins'], fresh['allowed_origins'])
            self.assertEqual(saved['userid'], fresh['userid'])
            self.assertNotIn('exclude_ids', saved['filter'])
            with patch.object(c, 'login', side_effect=AssertionError('login')):
                c.save_filter(paths, True)
            self.assertEqual(c.load(paths[1]), saved)
            conflicted = dict(saved)
            conflicted['base_url'] = 'http://127.0.0.1:9'
            c.atomic_json(paths[1], conflicted)
            before = paths[1].read_bytes()
            with self.assertRaises(c.SafeError):
                c.save_filter(paths, True)
            self.assertEqual(paths[1].read_bytes(), before)

    def test_load_migrates_exclude_ids_once_without_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = c.config_paths(Path(tmp).resolve() / 'tel')
            c.private_dir(paths[0])
            fresh, _old = self.legacy_config()
            stored = dict(fresh)
            stored['filter'] = dict(stored['filter'])
            stored['filter']['exclude_ids'] = stored['filter'].pop('exclude_channels')
            c.atomic_json(paths[1], stored)
            with patch.object(c, 'login', side_effect=AssertionError('login')):
                loaded = c.load(paths[1])
            self.assertEqual(loaded['filter']['exclude_channels'], ['YOUR_CHANNEL_ID'])
            self.assertNotIn('exclude_ids', loaded['filter'])
            self.assertNotIn('exclude_ids', json.loads(paths[1].read_text())['filter'])
            frozen = paths[1].read_bytes()
            with patch.object(c, 'login', side_effect=AssertionError('login')):
                self.assertEqual(c.load(paths[1]), loaded)
            self.assertEqual(paths[1].read_bytes(), frozen)
            both = json.loads(frozen)
            both['filter']['exclude_ids'] = ['OTHER_CHANNEL_ID']
            c.atomic_json(paths[1], both)
            conflict = paths[1].read_bytes()
            with self.assertRaises(c.SafeError):
                c.load(paths[1])
            self.assertEqual(paths[1].read_bytes(), conflict)

    def test_fetch_writes_channel_json_after_migrating_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = c.config_paths(Path(tmp).resolve() / 'tel')
            c.private_dir(paths[0])
            fresh, old = self.legacy_config()
            c.atomic_json(paths[1], old)
            catalog = directory([dict(mixNo='1', name='fixture')])
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), \
                    patch.object(c, 'login', return_value=[row()]), \
                    patch.object(c, 'portal_directory', return_value=catalog), \
                    patch.object(c, 'refresh_after_fetch', return_value=[]):
                c.fetch(paths)
            self.assertEqual(c.metadata_path(paths).name, 'channel.json')
            self.assertTrue(c.metadata_path(paths).is_file())
            self.assertFalse((paths[0] / 'channels.json').exists())
            self.assertEqual(c.read_json(c.metadata_path(paths)), catalog)
            saved = c.load(paths[1])
            self.assertEqual(saved['authurl'], fresh['authurl'])
            self.assertEqual(saved['rtp2http'], fresh['rtp2http'])
            self.assertNotIn('auth_origin', saved)
            self.assertNotIn('base_url', saved)


if __name__ == '__main__':
    unittest.main()
