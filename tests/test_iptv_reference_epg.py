"""Synthetic portal programme fixtures. No operator credentials or live requests."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iptv_reference_loader import iptv as c
from test_iptv_reference_cli import config, row
from test_iptv_reference_directory import directory


PORTAL = 'http://218.83.165.36:8084/iptvepg/fixture'
NOW = 1_761_000_000


def session():
    return dict(schema=1, portal_host=PORTAL, epg_domain='http://218.83.165.50:8084/index',
                referer=PORTAL + '/portal.jsp', cookies={'JSESSIONID': 'fixture-session'})


def catalog():
    item = dict(mixNo='7', name='频道HD', code='fixture-code', ID='fixture-portal')
    other = dict(mixNo='8', name='另一档', code='other-code', ID='other-portal')
    return directory([item, other])


def rows():
    return [row(ChannelID='42', UserChannelID='7', ChannelName='频道HD & <4K>'),
            row(ChannelID='42', UserChannelID='7'),
            row(ChannelID='99', UserChannelID='missing')]


class Portal:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or set()
        self.opener = c.request.build_opener(c.request.HTTPCookieProcessor(c.CookieJar()))

    def request(self, method, url, fields=None, referer=None):
        self.calls.append((method, url, dict(fields or {}), referer))
        action = (fields or {}).get('action')
        if action in self.fail:
            raise c.SafeError('fixture portal rejected')
        if action == 'getChannelList':
            body = json.dumps(dict(data=[dict(mixNo='7', name='频道HD', code='fixture-code', ID='fixture-portal')]))
        elif action == 'getChannelProg':
            body = json.dumps(dict(data=[dict(name='早间 & <HD>', startTime=NOW * 1000, endTime=NOW * 1000 + 60000)]))
        else:
            body = '{}'
        return body, url


class EpgTests(unittest.TestCase):
    def prepare(self, root, saved=True):
        paths = c.config_paths(root / 'tel')
        c.private_dir(paths[0])
        c.atomic_json(paths[1], config())
        c.atomic_json(paths[2], rows())
        c.atomic_json(c.metadata_path(paths), catalog())
        if saved:
            c.atomic_json(c.session_path(paths), session())
        return paths

    def test_saved_session_lists_programmes_without_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve())
            portal = Portal()
            with patch.object(c, 'HTTP', return_value=portal), patch.object(c, 'login', side_effect=AssertionError('login')):
                self.assertFalse(c.refresh_epg(paths, now=NOW))
            self.assertFalse(any(call[2].get('action') != 'getChannelList' and 'login' in call[1] for call in portal.calls))
            prog = [call for call in portal.calls if call[2].get('action') == 'getChannelProg']
            self.assertEqual(len(prog), 1)
            fields = prog[0][2]
            self.assertEqual(fields['code'], 'fixture-code')
            self.assertEqual(fields['channelID'], 'fixture-portal')
            self.assertEqual(fields['offset'], '0')
            self.assertEqual(fields['limit'], '2000')
            self.assertEqual(int(fields['endTime']) - int(fields['startTime']), 10 * 86400 * 1000)
            self.assertEqual(c.epg_path(paths).stat().st_mode & 0o777, 0o600)
            self.assertEqual(c.session_path(paths).stat().st_mode & 0o777, 0o600)
            text = c.epg_path(paths).read_text()
            self.assertIn('channel id="42"', text)
            self.assertIn('早间 &amp; &lt;HD&gt;', text)
            self.assertIn(' +0800', text)
            self.assertNotIn('fixture-session', text)
            self.assertNotIn('userid', c.session_path(paths).read_text())
            with patch.object(c.socket, 'socket', side_effect=AssertionError('offline')):
                c.convert(paths)
            playlist = paths[3].read_text()
            self.assertTrue(playlist.startswith('#EXTM3U x-tvg-url="epg.xml"\n'))
            self.assertEqual(playlist.count('tvg-id="42"'), 2)
            self.assertIn('tvg-id="99"', playlist)
            self.assertEqual(paths[2].read_bytes(), json.dumps(rows(), ensure_ascii=False, indent=2).encode() + b'\n')

    def test_expired_session_logs_in_without_replacing_catchup(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve())
            before = paths[1].read_bytes(), paths[2].read_bytes()
            fresh = Portal()
            def logged_in(*_args, **kwargs):
                kwargs['context']['http'] = fresh
                kwargs['context'].update(portal_host='218.83.165.36:8084', framecode='fixture',
                                         text="var usertoken='fixture'; EPGDomain=http://218.83.165.50:8084/index")
                c.set_cookie(fresh, 'JSESSIONID', 'fixture-session', PORTAL)
                return rows()
            with patch.object(c, 'HTTP', return_value=Portal(fail={'getChannelList'})), \
                    patch.object(c, 'local_ipv4', return_value='192.0.2.1'), \
                    patch.object(c, 'portal_directory', return_value=catalog()), \
                    patch.object(c, 'login', side_effect=logged_in) as login:
                self.assertTrue(c.refresh_epg(paths, now=NOW))
            login.assert_called_once()
            self.assertEqual(len([call for call in fresh.calls if call[2].get('action') == 'getChannelProg']), 1)
            self.assertEqual((paths[1].read_bytes(), paths[2].read_bytes()), before)
            self.assertIn('早间', c.epg_path(paths).read_text())

    def test_programme_failure_keeps_previous_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve())
            c.atomic_text(c.epg_path(paths), '<tv>old</tv>\n')
            c.atomic_text(paths[3], '#EXTM3U\nold\n')
            before = paths[2].read_bytes(), c.metadata_path(paths).read_bytes(), c.epg_path(paths).read_bytes(), paths[3].read_bytes()
            with patch.object(c, 'HTTP', return_value=Portal(fail={'getChannelProg'})), \
                    patch.object(c, 'login', side_effect=AssertionError('login')), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.main(['epg', 'config=' + str(paths[0])]), 1)
            self.assertEqual((paths[2].read_bytes(), c.metadata_path(paths).read_bytes(), c.epg_path(paths).read_bytes(), paths[3].read_bytes()), before)

    def test_offline_without_epg_stays_plain_and_does_not_connect(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.prepare(Path(tmp).resolve(), saved=False)
            with patch.object(c.socket, 'socket', side_effect=AssertionError('offline')), \
                    patch.object(c, 'login', side_effect=AssertionError('login')):
                self.assertEqual(c.main(['config=' + str(paths[0])]), 0)
            self.assertTrue(paths[3].read_text().startswith('#EXTM3U\n'))
            self.assertNotIn('x-tvg-url', paths[3].read_text())

    def test_external_epg_url_and_export_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            paths = self.prepare(root)
            c.atomic_text(c.epg_path(paths), '<tv>kept</tv>\n')
            cfg = config()
            cfg['epg_url'] = 'http://203.0.113.10/tel/epg.xml'
            c.atomic_json(paths[1], cfg)
            with patch.object(c.socket, 'socket', side_effect=AssertionError('offline')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'output=' + str(root / 'share')]), 0)
            exported = (root / 'share' / 'tel.m3u').read_text()
            self.assertIn('x-tvg-url="http://203.0.113.10/tel/epg.xml"', exported)
            self.assertEqual((root / 'share' / 'epg.xml').read_text(), '<tv>kept</tv>\n')
            self.assertNotIn('file://', exported)

    def test_timestamp_uses_milliseconds_and_plus_eight(self):
        self.assertEqual(c.xmltv_stamp(1_792_512_000_000), '20261021000000 +0800')
        quoted = c.xml_text('A&B<C>"D"')
        self.assertIn('&amp;', quoted)
        self.assertIn('&lt;', quoted)
        self.assertIn('&quot;', quoted)


if __name__ == '__main__':
    unittest.main()
