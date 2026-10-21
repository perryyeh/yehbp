"""Synthetic ffprobe/ffmpeg responses. These tests do not open a live stream."""
import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iptv_reference_loader import iptv as c
from test_iptv_reference_cli import config, row
from test_iptv_reference_directory import directory


NOW = 1_761_000_000


class Result:
    def __init__(self, stdout=b'', stderr=b'', code=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, code


def ffprobe(width, height, codec='h264', field='progressive', extra=None):
    streams = list(extra or [])
    streams.append({'codec_type': 'video', 'codec_name': codec, 'width': width, 'height': height, 'field_order': field})
    body = json.dumps({'streams': streams})
    return Result(body.encode())


def ffmpeg_log(width, height, frames='frame=1'):
    text = 'Stream #0:0: Video: h264 (High), yuv420p, %dx%d\n%s fps=25\n' % (width, height, frames)
    return Result(b'', text.encode())


class ResolutionTests(unittest.TestCase):
    def prepare(self, root, rows, filtering=True):
        paths = c.config_paths(root / 'tel')
        c.private_dir(paths[0])
        cfg = config()
        if filtering:
            cfg.pop('filter', None)
        c.atomic_json(paths[1], cfg)
        c.atomic_json(paths[2], rows)
        c.atomic_json(c.metadata_path(paths), directory([dict(mixNo='1', name='fixture')]))
        return paths

    def key(self, channel, url):
        return channel + '\n' + hashlib.sha256(url.encode()).hexdigest()

    def test_incremental_probe_skips_unchanged_and_keeps_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), [
                row(ChannelID='a', ChannelName='甲', ChannelURL='igmp://239.1.2.3:1001'),
                row(ChannelID='a', ChannelName='甲', ChannelURL='igmp://239.1.2.3:1001'),
                row(ChannelID='b', ChannelName='乙', ChannelURL='igmp://239.1.2.4:1002'),
            ])
            calls = []

            def runner(command, **_kwargs):
                calls.append(command[-1])
                return ffprobe(1920, 1080) if '1001' in command[-1] else Result(b'{}', b'no video', 1)

            with patch.object(c.shutil, 'which', side_effect=lambda name: '/usr/bin/ffprobe' if name == 'ffprobe' else None):
                targets = c.refresh_after_fetch(paths, now=NOW, runner=runner)
            self.assertEqual(len(targets), 2)
            self.assertEqual(len(calls), 2)
            stored = json.loads(c.resolution_path(paths).read_text())['channels']
            low = self.key('b', calls[1] if '1002' in calls[1] else calls[0])
            self.assertEqual(stored[low]['status'], 'failed')
            self.assertIsNone(stored[low]['width'])
            self.assertNotIn('AuthInfo', c.resolution_path(paths).read_text())
            before = c.resolution_path(paths).read_bytes()
            with patch.object(c.shutil, 'which', side_effect=lambda name: '/usr/bin/ffprobe' if name == 'ffprobe' else None):
                again = c.refresh_after_fetch(paths, now=NOW + 10, runner=runner)
            self.assertEqual(again, [])
            self.assertEqual(c.resolution_path(paths).read_bytes(), before)
            changed = [row(ChannelID='a', ChannelName='甲', ChannelURL='igmp://239.1.2.3:1001'), row(ChannelID='c', ChannelName='丙', ChannelURL='igmp://239.9.9.9:1003')]
            c.atomic_json(paths[2], changed)
            with patch.object(c.shutil, 'which', side_effect=lambda name: '/usr/bin/ffprobe' if name == 'ffprobe' else None):
                added = c.refresh_after_fetch(paths, now=NOW + 20, runner=runner)
            self.assertEqual(len(added), 1)
            self.assertIn('1003', calls[-1])
            self.assertIn(self.key('a', calls[0]), json.loads(c.resolution_path(paths).read_text())['channels'])

    def test_manual_rechecks_every_unique_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), [row(ChannelID='a', ChannelName='甲'), row(ChannelID='a', ChannelName='甲'), row(ChannelID='b', ChannelName='乙', ChannelURL='igmp://239.1.2.4:4')])
            seen = []

            def runner(command, **_kwargs):
                seen.append(command[-1])
                return ffprobe(1280, 720)

            with patch.object(c.shutil, 'which', return_value='/usr/bin/ffprobe'):
                c.refresh_after_fetch(paths, now=NOW, runner=runner)
                seen.clear()
                c.manual_resolution(paths, now=NOW, runner=runner)
            self.assertEqual(len(seen), 2)

    def test_offline_and_epg_do_not_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            paths = self.prepare(root, [row(ChannelName='购物频道'), row(ChannelID='keep', ChannelName='普通')], filtering=False)
            with patch.object(c, 'probe_fact', side_effect=AssertionError('probe')), patch.object(c, 'login', side_effect=AssertionError('login')):
                self.assertEqual(c.main(['config=' + str(paths[0])]), 0)
            self.assertFalse(c.resolution_path(paths).exists())
            self.assertIn('购物', paths[3].read_text())
            self.assertIn('tvg-id="keep"', paths[3].read_text())
            self.assertIn('tvg-id="42"', paths[3].read_text())
            before = paths[1].read_bytes(), paths[2].read_bytes()
            with patch.object(c, 'refresh_epg', return_value=False) as refresh, patch.object(c, 'probe_fact', side_effect=AssertionError('probe')):
                self.assertEqual(c.main(['epg', 'config=' + str(paths[0])]), 0)
            refresh.assert_called_once()
            self.assertEqual((paths[1].read_bytes(), paths[2].read_bytes()), before)

    def test_saved_rules_drive_filter_and_cli_keeps_custom_rules(self):
        cfg = config()
        cfg['filter'] = dict(enabled=True, min_height=1080, exclude_names=['自定'])
        urls = [c.live_url(row(ChannelID='edge', ChannelName='边缘'), cfg)]
        facts = {self.key('edge', urls[0]): dict(height=720, width=1280, status='identified')}
        rows = [row(ChannelID='edge', ChannelName='边缘'), row(ChannelID='ok', ChannelName='保留', ChannelURL='igmp://239.1.1.8:8')]
        text, counts = c.playlist_body(rows, cfg, resolutions={'config': cfg, 'channels': facts})
        self.assertEqual(counts['excluded_height'], 2)
        self.assertNotIn('tvg-id="edge"', text)
        self.assertNotIn('tvg-id="ok"', text)
        with self.assertRaises(c.SafeError):
            c.render(rows, cfg, resolutions={'config': cfg, 'channels': facts})
        cfg['filter']['min_height'] = 720
        text, counts = c.render(rows, cfg, resolutions={'config': cfg, 'channels': facts})
        self.assertEqual(counts, dict(excluded_id=0, excluded_height=1, excluded_name=0, kept=1))
        self.assertIn('tvg-id="edge"', text)
        cfg['filter']['min_height'] = 1080
        kept, _ = c.render([row(ChannelID='named', ChannelName='自定频道'), row(ChannelID='ok', ChannelName='保留')], dict(cfg, filter=dict(enabled=False, min_height=1080, exclude_names=['自定'])), resolutions=None, apply_filter=True)
        self.assertIn('tvg-id="named"', kept)
        self.assertIn('tvg-id="ok"', kept)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), [row(ChannelName='普通')], filtering=False)
            c.atomic_json(paths[1], cfg)
            before = {k: v for k, v in c.load(paths[1]).items() if k != 'filter'}
            with patch.object(c, 'login', side_effect=AssertionError('login')), patch.object(c, 'probe_fact', side_effect=AssertionError('probe')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'filter=false']), 0)
            saved = c.load(paths[1])
            self.assertEqual(saved['filter'], dict(enabled=False, min_height=1080, exclude_names=['自定']))
            self.assertEqual({k: saved[k] for k in before}, before)
            bad = dict(cfg)
            bad['filter'] = dict(enabled='yes', min_height=-1, exclude_names=[])
            with self.assertRaises(c.SafeError):
                c.validate(bad)
            self.assertEqual(c.load(paths[1])['filter']['min_height'], 1080)

    def test_legacy_bool_migrates_without_losing_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), [row(ChannelName='普通')])
            legacy = config()
            legacy['filter'] = False
            c.atomic_json(paths[1], legacy)
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(ChannelName='普通')]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='普通')])), patch.object(c, 'probe_fact', return_value=dict(checked_at='t', status='identified', codec='h264', width=1920, height=1080, field_order='progressive', playable=None)):
                self.assertEqual(c.main(['fetch', 'config=' + str(paths[0])]), 0)
            self.assertEqual(c.load(paths[1])['filter'], c.default_filter(False))
            enabled = config()
            enabled.pop('filter')
            c.atomic_json(paths[1], enabled)
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(ChannelName='普通')]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='普通')])), patch.object(c, 'probe_fact', side_effect=AssertionError('cached')):
                self.assertEqual(c.main(['fetch', 'config=' + str(paths[0])]), 0)
            self.assertEqual(c.load(paths[1])['filter'], c.default_filter(True))

    def test_probe_saves_each_batch_and_redacts_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), [row(ChannelID=str(i), ChannelName='甲', ChannelURL='igmp://239.1.2.%d:1' % i) for i in range(11)])
            calls = []

            def runner(command, **_kwargs):
                calls.append(command[-1])
                if len(calls) == 3:
                    self.assertEqual(len(json.loads(c.resolution_path(paths).read_text())['channels']), 0)
                return ffprobe(1920, 1080)

            with patch.object(c.shutil, 'which', return_value='/usr/bin/ffprobe'), contextlib.redirect_stdout(io.StringIO()) as out:
                c.refresh_resolutions(paths, c.auth.read_json(paths[2]) if False else json.loads(paths[2].read_text()), c.load(paths[1]), now=NOW, runner=runner)
            self.assertEqual(len(calls), 11)
            self.assertEqual(len(json.loads(c.resolution_path(paths).read_text())['channels']), 11)
            self.assertIn('分辨率进度：10/11', out.getvalue())
            self.assertNotIn('AuthInfo', out.getvalue())
            self.assertNotIn('239.1.2.', out.getvalue())

    def test_filter_treats_unusable_height_as_zero_without_rewriting_facts(self):
        cfg = dict(provider='shanghai-telecom', userid='fixture', sn='fixture-box', mac='00:11:22:33:44:55',
                   interface='eth0.85', authurl=c.DEFAULT_ORIGIN, allowed_origins=[],
                   rtp2http='http://127.0.0.1:5140/prefix', live='/rtp/', replay='/rtsp/',
                   seek=c.SEEK)
        rows = [row(ChannelID='hd', ChannelName='高清'), row(ChannelID='sd', ChannelName='标清'),
                row(ChannelID='shop', ChannelName='聚鲨购物'), row(ChannelID='miss', ChannelName='未知'),
                row(ChannelID='zero', ChannelName='零高', ChannelURL='igmp://239.1.1.9:9'),
                row(ChannelID='bad', ChannelName='失败', ChannelURL='igmp://239.1.1.8:8'),
                row(ChannelID='moved', ChannelName='地址变了', ChannelURL='igmp://239.1.1.7:7')]
        urls = [c.live_url(item, cfg) for item in rows]
        facts = {
            self.key('hd', urls[0]): dict(height=1080, width=1920, status='identified'),
            self.key('sd', urls[1]): dict(height=576, width=720, status='identified'),
            self.key('shop', urls[2]): dict(height=1080, width=1920, status='identified'),
            self.key('zero', urls[4]): dict(height=0, width=0, status='identified'),
            self.key('bad', urls[5]): dict(height=1080, width=1920, status='failed'),
            self.key('other', urls[0]): dict(height=2160, width=3840, status='identified'),
        }
        before = json.dumps(facts, sort_keys=True)
        text, counts = c.render(rows, cfg, resolutions={'config': cfg, 'channels': facts})
        ids = [line.split('tvg-id="', 1)[1].split('"', 1)[0] for line in text.splitlines() if line.startswith('#EXTINF:')]
        self.assertEqual(ids, ['hd'])
        self.assertEqual(counts, dict(excluded_id=0, excluded_height=5, excluded_name=1, kept=1))
        self.assertEqual(json.dumps(facts, sort_keys=True), before)
        self.assertEqual(facts[self.key('zero', urls[4])]['height'], 0)
        cfg['filter'] = dict(enabled=False, min_height=1080, exclude_names=['自定'])
        full, off = c.render(rows, cfg, resolutions={'config': cfg, 'channels': facts})
        self.assertEqual(full.count('#EXTINF:'), 7)
        self.assertEqual(off['kept'], 7)
        self.assertEqual(json.dumps(facts, sort_keys=True), before)

    def test_ffprobe_skips_leading_zero_video_and_retries_only_zero_facts(self):
        placeholder = {'codec_type': 'video', 'codec_name': 'hevc', 'width': 0, 'height': 0, 'field_order': None}
        secret = 'http://127.0.0.1/rtp/239.1.1.1:1?fcc=1.2.3.4:5'
        with patch.object(c.shutil, 'which', return_value='/usr/bin/ffprobe'):
            fact = c.probe_fact(secret, NOW, runner=lambda command, **_k: ffprobe(3840, 2160, codec='hevc', field='progressive', extra=[placeholder, {'codec_type': 'audio', 'codec_name': 'ac3'}]))
        self.assertEqual(fact['status'], 'identified')
        self.assertEqual((fact['codec'], fact['width'], fact['height'], fact['field_order']), ('hevc', 3840, 2160, 'progressive'))
        self.assertNotIn(secret, json.dumps(fact))
        seen = {}

        def capture(command, **_k):
            seen['command'] = command
            return ffprobe(1280, 720, codec='mpeg2video', extra=[
                {'codec_type': 'video', 'codec_name': 'hevc', 'width': 0, 'height': 0},
                {'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080},
            ])

        with patch.object(c.shutil, 'which', return_value='/usr/bin/ffprobe'):
            chosen = c.probe_fact(secret, NOW, runner=capture)
        self.assertEqual((chosen['status'], chosen['codec'], chosen['width'], chosen['height']), ('identified', 'h264', 1920, 1080))
        self.assertNotIn('select_streams', seen['command'])
        self.assertNotIn(secret, seen['command'][:-1])
        with patch.object(c.shutil, 'which', return_value='/usr/bin/ffprobe'):
            only_zero = c.probe_fact(secret, NOW, runner=lambda command, **_k: ffprobe(0, 0, codec='hevc', extra=[]))
        self.assertEqual(only_zero['status'], 'failed')
        self.assertIsNone(only_zero['width'])
        self.assertIsNone(only_zero['height'])
        zero = dict(checked_at='t', status='identified', codec='hevc', width=0, height=0, field_order=None, playable=None)
        sized = dict(zero, width=1920, height=1080)
        rows = [row(ChannelID='zero', ChannelURL='igmp://239.1.1.9:9'), row(ChannelID='ok', ChannelURL='igmp://239.1.1.8:8')]
        cfg = config()
        stored = {self.key('zero', c.live_url(rows[0], cfg)): zero, self.key('ok', c.live_url(rows[1], cfg)): sized}
        targets = c.resolution_targets(rows, cfg, stored)
        self.assertEqual([item[1] for item in targets], [self.key('zero', c.live_url(rows[0], cfg))])
        failed = dict(zero, status='failed', width=None, height=None)
        self.assertEqual(c.resolution_targets([rows[0]], cfg, {self.key('zero', c.live_url(rows[0], cfg)): failed}), [])

    def test_timeout_failure_and_secret_redaction(self):
        secret = 'http://127.0.0.1/rtp/239.1.1.1:1?fcc=1.2.3.4:5&AuthInfo=SECRET'
        with patch.object(c.shutil, 'which', return_value='/usr/bin/ffmpeg'):
            with patch.object(c, 'which_probe', return_value=('ffmpeg', '/usr/bin/ffmpeg')):
                def timed_out(*_args, **_kwargs):
                    raise subprocess.TimeoutExpired(cmd=['ffmpeg', secret], timeout=12)
                fact = c.probe_fact(secret, NOW, runner=timed_out)
                self.assertEqual(fact['status'], 'timeout')
                self.assertIsNone(fact['width'])
                self.assertNotIn('SECRET', json.dumps(fact))
                leaked = Result(b'', ('Input #0, mpegts, from %s\nServer returned 404\n' % secret).encode(), 1)
                failed = c.probe_fact(secret, NOW, runner=lambda *_a, **_k: leaked)
                self.assertEqual(failed['status'], 'unreachable')
                self.assertNotIn('SECRET', json.dumps(failed))
                self.assertNotIn(secret, json.dumps(failed))
                played = c.probe_fact(secret, NOW, runner=lambda *_a, **_k: ffmpeg_log(1920, 1080))
                self.assertEqual((played['status'], played['playable'], played['height']), ('played', True, 1080))

    def test_filter_toggle_is_offline_and_preserves_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), [row(ChannelName='理财频道'), row(ChannelID='plain', ChannelName='普通')], filtering=False)
            c.atomic_json(c.resolution_path(paths), {'schema': 1, 'channels': {
                self.key('42', c.live_url(row(ChannelName='理财频道'), config())): {'height': 1080, 'width': 1920, 'status': 'identified'},
                self.key('plain', c.live_url(row(ChannelID='plain', ChannelName='普通'), config())): {'height': 1080, 'width': 1920, 'status': 'identified'},
            }})
            original = {k: v for k, v in c.load(paths[1]).items() if k != 'filter'}
            with patch.object(c, 'login', side_effect=AssertionError('login')), patch.object(c, 'probe_fact', side_effect=AssertionError('probe')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'filter=false']), 0)
            saved = c.load(paths[1])
            self.assertEqual(saved['filter'], c.default_filter(False))
            self.assertEqual({k: saved[k] for k in original}, original)
            self.assertIn('理财', paths[3].read_text())
            self.assertIn('tvg-id="plain"', paths[3].read_text())
            with patch.object(c, 'login', side_effect=AssertionError('login')), patch.object(c, 'probe_fact', side_effect=AssertionError('probe')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'filter=true']), 0)
            self.assertEqual(c.load(paths[1])['filter'], c.default_filter(True))
            self.assertNotIn('理财', paths[3].read_text())
            err = io.StringIO()
            before = paths[1].read_bytes()
            with contextlib.redirect_stderr(err):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'filter=maybe']), 1)
            self.assertIn('filter只能是true或false', err.getvalue())
            self.assertEqual(paths[1].read_bytes(), before)

    def test_filter_switch_does_not_control_probing(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), [row(ChannelName='普通')], filtering=False)
            calls = []

            def runner(command, **_kwargs):
                calls.append(command[-1])
                return ffprobe(1920, 1080)

            with patch.object(c.shutil, 'which', return_value='/usr/bin/ffprobe'), patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(ChannelName='普通')]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='普通')])), patch.object(c, 'probe_fact', wraps=c.probe_fact) as probe:
                c.refresh_after_fetch(paths, now=NOW, runner=runner)
                self.assertEqual(c.main(['config=' + str(paths[0]), 'filter=true']), 0)
                probed = probe.call_count
                self.assertEqual(c.main(['config=' + str(paths[0]), 'filter=false']), 0)
            self.assertEqual(probe.call_count, probed)
            self.assertTrue(calls)
            self.assertTrue(c.resolution_path(paths).is_file())
            self.assertIs(c.load(paths[1])['filter']['enabled'], False)

    def test_run_persists_missing_filter_and_keeps_explicit_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), [row(ChannelName='普通')])
            other = {k: v for k, v in c.load(paths[1]).items()}
            self.assertNotIn('filter', other)
            before_rows = paths[2].read_bytes()
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(ChannelName='普通')]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='普通')])), patch.object(c.shutil, 'which', return_value='/usr/bin/ffprobe'), patch.object(c, 'probe_fact', return_value=dict(checked_at='t', status='identified', codec='h264', width=1920, height=1080, field_order='progressive', playable=None)):
                self.assertEqual(c.main(['run', 'config=' + str(paths[0])]), 0)
            saved = c.load(paths[1])
            self.assertEqual(saved['filter'], c.default_filter(True))
            saved.pop('filter')
            self.assertEqual(saved, other)
            explicit = dict(saved)
            explicit['filter'] = c.default_filter(False)
            c.atomic_json(paths[1], explicit)
            frozen = paths[1].read_bytes()
            with patch.object(c, 'local_ipv4', return_value='192.0.2.1'), patch.object(c, 'login', return_value=[row(ChannelName='普通')]), patch.object(c, 'portal_directory', return_value=directory([dict(mixNo='1', name='普通')])), patch.object(c, 'probe_fact', side_effect=AssertionError('already measured')):
                self.assertEqual(c.main(['fetch', 'config=' + str(paths[0])]), 0)
            self.assertEqual(paths[1].read_bytes(), frozen)
            self.assertEqual(c.load(paths[1])['filter'], c.default_filter(False))
            offline = paths[1].read_bytes()
            with patch.object(c, 'login', side_effect=AssertionError('login')):
                self.assertEqual(c.main(['config=' + str(paths[0])]), 0)
                self.assertEqual(c.main(['resolution', 'config=' + str(paths[0])]), 0)
            self.assertEqual(paths[1].read_bytes(), offline)
            self.assertNotEqual(before_rows, b'')

    def test_missing_tools_do_not_install_and_keep_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), [row(ChannelName='普通')], filtering=False)
            with patch.object(c.shutil, 'which', return_value=None):
                c.refresh_after_fetch(paths, now=NOW, runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('ran')))
            fact = next(iter(json.loads(c.resolution_path(paths).read_text())['channels'].values()))
            self.assertEqual(fact['status'], 'unavailable')
            with patch.object(c, 'probe_fact', side_effect=AssertionError('offline')):
                c.convert(paths)
            self.assertIn('#EXTINF:', paths[3].read_text())

    def test_exclude_ids_exact_before_name_and_off_keeps_all(self):
        cfg = config()
        cfg['filter'] = dict(enabled=True, min_height=0, exclude_names=['购物'], exclude_ids=['exact-id'])
        cfg['channel_names'] = {'exact-id': '保留名', 'other-id': '购物台'}
        rows = [row(ChannelID='exact-id'), row(ChannelID='other-id'), row(ChannelID='suffix-exact-id', ChannelName='普通')]
        text, counts = c.render(rows, cfg, resolutions=None)
        self.assertEqual(counts['excluded_id'], 1)
        self.assertEqual(counts['excluded_name'], 1)
        self.assertEqual(counts['kept'], 1)
        self.assertNotIn('tvg-id="exact-id"', text)
        self.assertIn('tvg-id="suffix-exact-id"', text)
        self.assertNotIn('购物台', text)
        cfg['filter']['enabled'] = False
        full, off = c.render(rows, cfg, resolutions=None)
        self.assertEqual(off['kept'], 3)
        self.assertIn('tvg-id="exact-id"', full)
        self.assertIn('购物台', full)
        with self.assertRaises(c.SafeError):
            c.validate(dict(config(), filter=dict(enabled=True, min_height=0, exclude_names=['购物'], exclude_ids=['exact-id', 'not exact'])))


if __name__ == '__main__':
    unittest.main()
