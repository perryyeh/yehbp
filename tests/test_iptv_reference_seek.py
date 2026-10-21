"""Replay defaults and explicit overrides; synthetic fixtures, no provider access."""
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


LOCAL_SEEK = '${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}'
PREVIOUS_DEFAULT = '{utc:YmdHMS}-{utcend:YmdHMS}'


class ReplaySeekTests(unittest.TestCase):
    def check_playlist(self, paths, seek):
        rows = [row(ChannelName=' 高清 HD 4K '), row(ChannelName='重复')]
        before = [dict(item) for item in rows]
        c.atomic_json(paths[2], rows)
        cfg_bytes = paths[1].read_bytes()
        source_bytes = paths[2].read_bytes()
        with patch.object(c.socket, 'socket', side_effect=AssertionError('network forbidden')):
            self.assertEqual(c.main(['config=' + str(paths[0])]), 0)
        playlist = paths[3].read_text()
        expected = ('http://127.0.0.1:5140/rtsp/example.test:554/path%2Fsub'
                    '?AuthInfo=A%2fb+C==&x=1&x=2&playseek=' + seek)
        self.assertEqual(playlist.count('catchup-source="' + expected + '"'), 2)
        self.assertEqual([line.rsplit(',', 1)[1] for line in playlist.splitlines()
                          if line.startswith('#EXTINF:')], [' 高清 HD 4K ', '重复'])
        self.assertEqual([line for line in playlist.splitlines() if line.startswith('http')],
                         ['http://127.0.0.1:5140/rtp/239.1.2.3:1234'] * 2)
        self.assertEqual(paths[1].read_bytes(), cfg_bytes)
        self.assertEqual(paths[2].read_bytes(), source_bytes)
        self.assertEqual(c.read_json(paths[2]), before)

    def test_first_parameters_without_seek_save_and_emit_local_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = c.config_paths(Path(tmp).resolve() / 'tel')
            args = ['config=' + str(paths[0]), 'userid=fixture', 'sn=box',
                    'mac=00:11:22:33:44:55', 'interface=eth0.85']
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row()]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])), patch.object(c.socket, 'socket', side_effect=AssertionError('network forbidden')):
                self.assertEqual(c.main(args + ['filter=false']), 0)
            self.assertEqual(c.load(paths[1])['seek'], LOCAL_SEEK)
            self.check_playlist(paths, LOCAL_SEEK)

    def test_interactive_empty_seek_uses_local_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = c.config_paths(Path(tmp).resolve() / 'tel')
            answers = ['fixture', 'box', '00:11:22:33:44:55', 'eth0.85'] + [''] * 5
            with patch.object(c.getpass, 'getpass', side_effect=answers), patch.object(c.socket, 'socket', side_effect=AssertionError('network forbidden')):
                self.assertEqual(c.main(['configure', 'config=' + str(paths[0])]), 0)
            self.assertEqual(c.load(paths[1])['seek'], LOCAL_SEEK)
            saved = c.load(paths[1])
            saved['filter'] = False
            c.atomic_json(paths[1], saved)
            self.check_playlist(paths, LOCAL_SEEK)

    def test_explicit_custom_seek_is_saved_and_retained_on_other_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = c.config_paths(Path(tmp).resolve() / 'tel')
            cfg = config()
            cfg['rtp2http'] = 'http://127.0.0.1:5140'
            c.private_dir(paths[0])
            c.atomic_json(paths[1], cfg)
            custom = '{utc:custom}-{utcend:custom}'
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row()]) as login, patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='fixture')])), patch.object(c.socket, 'socket', side_effect=AssertionError('network forbidden')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'seek=' + custom]), 0)
                self.assertEqual(c.main(['config=' + str(paths[0]), 'live=/rtp/']), 0)
            self.assertEqual(login.call_count, 2)
            self.assertEqual(c.load(paths[1])['seek'], custom)
            self.check_playlist(paths, custom)

    def test_missing_provider_loads_and_save_drops_obsolete_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = c.config_paths(Path(tmp).resolve() / 'tel')
            cfg = config()
            self.assertNotIn('provider', cfg)
            c.validate(cfg)
            cfg['provider'] = 'shanghai-telecom'
            cfg['rtp2http'] = 'http://127.0.0.1:5140'
            c.private_dir(paths[0])
            c.atomic_json(paths[1], cfg)
            loaded = c.load(paths[1])
            self.assertEqual(loaded['provider'], 'shanghai-telecom')
            c.save_fields(paths, {'live': '/rtp/'})
            saved = json.loads(paths[1].read_text())
            self.assertNotIn('provider', saved)
            self.assertEqual(saved['userid'], 'fixture')
            self.assertEqual(saved['live'], '/rtp/')

    def test_saved_previous_default_is_not_silently_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = c.config_paths(Path(tmp).resolve() / 'tel')
            cfg = config()
            cfg.update(rtp2http='http://127.0.0.1:5140', seek=PREVIOUS_DEFAULT,
                       multicast='', fcc='')
            c.private_dir(paths[0])
            c.atomic_json(paths[1], cfg)
            before = paths[1].read_bytes()
            c.save_fields(paths, {'live': '/rtp/'})
            self.assertEqual(paths[1].read_bytes(), before)
            self.check_playlist(paths, PREVIOUS_DEFAULT)


if __name__ == '__main__':
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main()
