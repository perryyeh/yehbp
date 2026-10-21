"""Independent offline protocol fixtures; no customer data."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from iptv_reference_loader import iptv as a
ROOT=Path(__file__).resolve().parents[1]

class AuthTests(unittest.TestCase):
    def test_aes_binary_md5_key_and_padding(self):
        # Independently known AES-128 ECB block (NIST FIPS-197), plus PKCS7 block.
        encrypted = a.aes_encrypt(bytes.fromhex('00112233445566778899aabbccddeeff'), bytes.fromhex('000102030405060708090a0b0c0d0e0f'))
        self.assertEqual(encrypted[:16].hex(), '69c4e0d86a7b0430d8cdb78070b4c55a')
        self.assertEqual(len(encrypted), 32)


    def test_array_preserves_duplicates_unknown_null_and_entities(self):
        row = 'ChannelID="1" TimeShiftURL="null" Unknown="a&amp;b"'
        source = "var channelArray = ['" + row + "','" + row + "'];"
        rows = a.channels(source)
        self.assertEqual(rows, [dict(ChannelID='1', TimeShiftURL='null', Unknown='a&b')]*2)
        for bad in ["var channelArray=[];", "var channelArray=['A=\"x\"', BAD];", "var channelArray=['A=\"x\" junk'];", "var channelArray=['A=\"x\" A=\"y\"'];"]:
            with self.subTest(bad=bad), self.assertRaises(a.SafeError):
                a.channels(bad)


    def test_three_steps_dynamic_forms_entities(self):
        config = dict(userid='fixture', sn='box', mac='00:11:22:33:44:55', interface='eth0', authurl=a.DEFAULT_ORIGIN, allowed_origins=[])
        replies = ["<form action='http://124.75.29.176:7001/next?a=1&amp;b=2'><input name='x' value='a&amp;b'></form>", "<script>var encrytoken='fixture-token';</script><form id='initform' action='/finish'><input name='q' value='1'></form>", "var usertoken='ok'; var EPGDomain='http://fixture/'; var channelArray=['ChannelID=\"1\" TimeShiftURL=\"null\"'];"]
        class HTTP:
            def __init__(self): self.calls=[]
            def request(self, method, url, fields=None, referer=None):
                self.calls.append((method,url,fields)); return replies.pop(0), url
        http = HTTP()
        with patch.object(a, 'authenticator', return_value='cipher') as auth:
            self.assertEqual(len(a.login(config,'192.0.2.3',http)), 1)
        self.assertEqual([x[0] for x in http.calls], ['GET','POST','POST'])
        self.assertIn('a=1&b=2', http.calls[1][1])
        self.assertEqual(http.calls[1][2], {'x':'a&b'})
        self.assertEqual(http.calls[2][2]['authenticator'], 'cipher')
        auth.assert_called_once_with('fixture-token', config, '192.0.2.3')
        with self.assertRaises(a.SafeError): a.trusted('http://evil.example/path',config)


    def test_private_atomic_and_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            d=Path(directory).resolve()/'private'; a.private_dir(d)
            self.assertEqual(d.stat().st_mode & 0o777,0o700)
            target=d/'records.json'; a.atomic_json(target,[{'x':'old'}])
            self.assertEqual(target.stat().st_mode & 0o777,0o600)
            with a.lock(d):
                with self.assertRaises(a.SafeError):
                    with a.lock(d): pass
            link=d/'link'; link.symlink_to(target)
            with self.assertRaises(a.SafeError): a.atomic_json(link, [])
            with patch.object(a.os, 'replace', side_effect=OSError('secret')):
                with self.assertRaises(OSError): a.atomic_json(target,[{'x':'new'}])
            self.assertEqual(json.loads(target.read_text()),[{'x':'old'}])
            self.assertFalse(list(d.glob('.iptv-*')))


    def test_no_proxy_and_bound_socket(self):
        config=dict(authurl=a.DEFAULT_ORIGIN,allowed_origins=[],interface='eth0')
        with patch.dict(os.environ, {'http_proxy':'http://evil.example:1'}):
            h=a.HTTP(config)
        self.assertEqual(h.opener.handlers[0].__class__.__name__, 'UnknownHandler')
        with patch.object(a.socket,'socket') as socket:
            a.bound_socket('eth0', 3)
            socket.return_value.setsockopt.assert_called_once_with(a.socket.SOL_SOCKET,25,b'eth0\0')


    def test_http_transport_cookie_charset_and_redaction(self):
        import http.server
        import threading
        import urllib.request
        seen=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                seen.append(self.headers.get('Cookie',''))
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=gbk')
                self.send_header('Set-Cookie','fixture=ok; Path=/')
                self.send_header('Content-Encoding','gzip')
                self.end_headers(); self.wfile.write(a.gzip.compress('频道'.encode('gbk')))
        server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever); thread.start()
        endpoint='http://127.0.0.1:'+str(server.server_port)
        config=dict(interface='eth0',authurl=endpoint,allowed_origins=[])
        def socket_for_fixture(interface,timeout):
            sock=a.socket.socket(a.socket.AF_INET,a.socket.SOCK_STREAM)
            sock.settimeout(timeout); return sock
        try:
            with patch.dict(os.environ, {'http_proxy':'http://127.0.0.1:1','HTTP_PROXY':'http://127.0.0.1:1'}), patch.object(a,'bound_socket',side_effect=socket_for_fixture) as bound:
                h=a.HTTP(config)
                self.assertEqual(h.request('GET',endpoint)[0],'频道')
                h.request('GET',endpoint)
                self.assertEqual(seen,['','fixture=ok'])
                self.assertEqual(bound.call_count,2)
                self.assertEqual(bound.call_args[0][0],'eth0')
            with patch.object(h.opener,'open',side_effect=RuntimeError('http://secret/token?UserID=private')):
                with self.assertRaises(a.SafeError) as error: h.request('GET',endpoint)
            self.assertNotIn('secret',str(error.exception))
            self.assertNotIn('private',str(error.exception))
        finally:
            server.shutdown(); thread.join(); server.server_close()


    def test_authenticator_exact_protocol_payload(self):
        import hashlib
        c=dict(userid='fixture',sn='box',mac='aa:bb:cc:dd:ee:ff')
        with patch.object(a,'aes_encrypt',return_value=b'cipher') as enc,patch.object(a.secrets,'randbelow',return_value=42):
            self.assertEqual(a.authenticator('token',c,'1.2.3.4'),b'cipher'.hex())
        self.assertEqual(enc.call_args[0][1],hashlib.md5(b'123456').digest())
        self.assertEqual(json.loads(enc.call_args[0][0]),dict(Randon='00000042',EncryToken='token',UserID='fixture',SN='box',IP='001,002,003,004',MAC='AA:BB:CC:DD:EE:FF',MagicCode='CTC',UpdateTime='20230301175307'))


    def test_provider_string_encoding_and_complete_delimiters(self):
        # Provider attribute grammar from the pinned parser, with independently
        # authored fixtures. No real identities, tokens or response bodies.
        raw = r'''var channelArray=['ChannelID="1",TimeShiftURL="rtsp:\/\/fixture\/a?q=1&amp;x=2" Unknown="\\path\x2f\u9891\u9053\uD83D\uDE00"Empty=""','ChannelID="1" TimeShiftURL="null"'];'''
        rows = a.channels(raw)
        self.assertEqual(rows[0], dict(ChannelID='1', TimeShiftURL='rtsp://fixture/a?q=1&x=2', Unknown='\\path/频道😀', Empty=''))
        self.assertEqual(rows[1], dict(ChannelID='1', TimeShiftURL='null'))
        for source in [raw.replace('Empty=', '??Empty='), raw.replace('ChannelID="1",', 'ChannelID="1",ChannelID="2",'), raw.replace('\\uDE00', '')]:
            with self.subTest(source=source), self.assertRaises(a.SafeError):
                a.channels(source)


    def test_untrusted_redirect_never_connects(self):
        import http.server
        import threading
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', 'http://untrusted.invalid/identity')
                self.end_headers()
        server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever); thread.start()
        endpoint='http://127.0.0.1:'+str(server.server_port)
        def fixture_socket(interface,timeout):
            sock=a.socket.socket(a.socket.AF_INET,a.socket.SOCK_STREAM); sock.settimeout(timeout); return sock
        try:
            with patch.object(a, 'bound_socket', side_effect=fixture_socket) as bound:
                h=a.HTTP(dict(interface='eth0',authurl=endpoint,allowed_origins=[]))
                with self.assertRaises(a.SafeError): h.request('GET', endpoint)
                self.assertEqual(bound.call_count, 1)
        finally:
            server.shutdown(); thread.join(); server.server_close()
