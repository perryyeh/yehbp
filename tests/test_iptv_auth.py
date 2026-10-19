"""Offline authentication contracts; all identities and endpoints are fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('iptv_auth', ROOT / 'assets/iptv/iptv-auth.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


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
        config = dict(userid='fixture', sn='box', mac='00:11:22:33:44:55', interface='eth0', auth_origin=a.DEFAULT_ORIGIN, allowed_origins=[])
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            d=Path(directory)/'private'; a.private_dir(d)
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
        config=dict(auth_origin=a.DEFAULT_ORIGIN,allowed_origins=[],interface='eth0')
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
        config=dict(interface='eth0',auth_origin=endpoint,allowed_origins=[])
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

    def test_timer_isolated_ownership_and_preserved_data(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            d=Path(directory); units=d/'units'; units.mkdir(); private=d/'private'
            a.private_dir(private)
            output=d/'shcatchup.json'; a.atomic_json(output,[{'old':'keep'}])
            config=dict(userid='fixture',sn='fixture',mac='00:11:22:33:44:55',interface='eth0',auth_origin=a.DEFAULT_ORIGIN,allowed_origins=[],output=str(output))
            a.atomic_json(private/'identity.json',config)
            with patch.object(a,'UNIT_DIR',units), patch.object(a,'systemctl',return_value='fixture-next-run') as ctl:
                a.timer(private,True)
                name=a.unit_name(private)
                self.assertIn('OnCalendar=*-*-* 05:00:00',(units/(name+'.timer')).read_text())
                service=(units/(name+'.service')).read_text()
                self.assertIn(' refresh --directory ',service)
                self.assertNotIn('fixture',service)
                ctl.assert_any_call('enable','--now',name+'.timer')
                unrelated=units/'unrelated.timer'; unrelated.write_text('keep')
                a.timer(private,False)
                self.assertEqual([x.name for x in units.iterdir()],['unrelated.timer'])
                self.assertEqual(json.loads(output.read_text()),[{'old':'keep'}])
                self.assertTrue((private/'identity.json').exists())
                (units/(name+'.timer')).write_text('not managed')
                with self.assertRaises(a.SafeError): a.timer(private,False)

    def test_config_hidden_input_retains_values_and_cli_redacts(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            d=Path(directory); private=d/'private'; a.private_dir(private)
            c=dict(provider='shanghai-telecom',userid='secret-user',sn='secret-sn',mac='00:11:22:33:44:55',interface='eth0',auth_origin=a.DEFAULT_ORIGIN,allowed_origins=[],output=str(d/'shcatchup.json'))
            a.atomic_json(private/'identity.json',c)
            out=io.StringIO()
            with patch.object(a.getpass,'getpass',return_value=''),patch('builtins.input',return_value=''),redirect_stdout(out):
                a.configure(private,'eth0',str(d/'shcatchup.json'))
                a.status(private)
            self.assertEqual(a.load_config(private),c)
            self.assertNotIn('secret',out.getvalue())
            with patch.object(sys,'argv',['iptv-auth.py','refresh','--directory',str(private)]),patch.object(a,'refresh',side_effect=RuntimeError('secret-user token url')),redirect_stderr(out):
                self.assertEqual(a.main(),1)
            self.assertNotIn('secret',out.getvalue())

    def test_authenticator_exact_protocol_payload(self):
        import hashlib
        c=dict(userid='fixture',sn='box',mac='aa:bb:cc:dd:ee:ff')
        with patch.object(a,'aes_encrypt',return_value=b'cipher') as enc,patch.object(a.secrets,'randbelow',return_value=42):
            self.assertEqual(a.authenticator('token',c,'1.2.3.4'),b'cipher'.hex())
        self.assertEqual(enc.call_args[0][1],hashlib.md5(b'123456').digest())
        self.assertEqual(json.loads(enc.call_args[0][0]),dict(Randon='00000042',EncryToken='token',UserID='fixture',SN='box',IP='001,002,003,004',MAC='AA:BB:CC:DD:EE:FF',MagicCode='CTC',UpdateTime='20230301175307'))

    def test_shell_menu_routes_and_removal_isolation(self):
        import subprocess
        source=ROOT/'assets/iptv/rtp2httpd.sh'
        for number,command in [(6,'configure'),(7,'refresh'),(8,'enable-daily'),(9,'disable-daily'),(10,'status')]:
            script='source "$1"\nrtp2httpd_auth_action() { printf "CALLED:%s\\n" "$1"; }\nmanage_rtp2httpd\n'
            result=subprocess.run(['bash','-c',script,'test',str(source)],input=str(number)+'\n',text=True,capture_output=True,check=True)
            self.assertIn('CALLED:'+command,result.stdout)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            app=Path(directory); private=app/'.iptv-auth-rtp2httpd_fixture'; private.mkdir()
            (app/'iptv-auth.py').write_text('# fixture')
            identity=private/'identity.json'; identity.write_text('keep')
            output=app/'shcatchup.json'; output.write_text('keep')
            script='source "$1"\npython3() { printf "%s\\n" "$@"; }\nrtp2httpd_auth_remove_timer "$2/rtp2httpd_fixture.conf" rtp2httpd_fixture.service\n'
            result=subprocess.run(['bash','-c',script,'test',str(source),str(app)],capture_output=True,text=True,check=True)
            self.assertIn('disable-daily\n--directory\n'+str(private),result.stdout)
            self.assertEqual(identity.read_text(),'keep')
            self.assertEqual(output.read_text(),'keep')

    def test_two_instances_paths_locks_timers_and_output_isolated(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            app=Path(directory); units=app/'units'; units.mkdir()
            first=app/'.iptv-auth-rtp2httpd_alpha'; second=app/'.iptv-auth-rtp2httpd_beta'
            for private in (first,second):
                a.private_dir(private)
                suffix=private.name.rsplit('_',1)[1]
                config=dict(provider='shanghai-telecom',userid='fixture',sn='fixture',mac='00:11:22:33:44:55',interface='eth0',auth_origin=a.DEFAULT_ORIGIN,allowed_origins=[],output=str(app/('catchup_'+suffix+'.json')))
                a.atomic_json(a.config_path(private),config)
            self.assertEqual(a.config_path(first).name,'rtp2httpd_alpha.auth.json')
            self.assertEqual(a.config_path(second).name,'rtp2httpd_beta.auth.json')
            with a.lock(first), a.lock(second): pass
            with patch.object(a,'UNIT_DIR',units),patch.object(a,'systemctl',return_value='fixture-next'):
                a.timer(first,True); a.timer(second,True)
                self.assertNotEqual(a.unit_name(first),a.unit_name(second))
                a.timer(first,False)
                self.assertTrue((units/(a.unit_name(second)+'.timer')).exists())
            with patch.object(a,'local_ipv4',return_value='192.0.2.1'),patch.object(a,'login',return_value=[{'ChannelID':'fixture'}]):
                a.refresh(first); a.refresh(second)
            self.assertTrue((app/'catchup_alpha.json').exists())
            self.assertTrue((app/'catchup_beta.json').exists())
            self.assertFalse((app/'shcatchup.json').exists())
            bad=a.load_config(second); bad['provider']='other'
            with self.assertRaises(a.SafeError): a.validate(bad)

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

    def test_interface_reloaded_from_instance_and_disable_during_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory).resolve(); private = app/'private'; a.private_dir(private)
            conf = app/'rtp2httpd_fixture.conf'; conf.write_text('upstream-interface-multicast=eth0.85\n')
            c = dict(userid='fixture', sn='fixture', mac='00:11:22:33:44:55', interface='old0', auth_origin=a.DEFAULT_ORIGIN, allowed_origins=[], output=str(app/'catchup_fixture.json'), instance_config=str(conf))
            a.atomic_json(private/'identity.json', c)
            with patch.object(a, 'local_ipv4', return_value='192.0.2.1') as ip, patch.object(a, 'login', return_value=[{'ChannelID':'1'}]) as login:
                a.refresh(private)
                ip.assert_called_with('eth0.85')
                self.assertEqual(login.call_args[0][0]['interface'], 'eth0.85')
                conf.write_text('upstream-interface-multicast=eth0.86\n')
                a.refresh(private)
                ip.assert_called_with('eth0.86')
            saved = (app/'catchup_fixture.json').read_bytes()
            conf.write_text('upstream-interface-multicast=eth0.85\nupstream-interface-multicast=eth0.86\n')
            with self.assertRaises(a.SafeError): a.refresh(private)
            self.assertEqual((app/'catchup_fixture.json').read_bytes(), saved)
            units=app/'units'; units.mkdir()
            with patch.object(a, 'UNIT_DIR', units), patch.object(a, 'systemctl', return_value='') as ctl:
                name=a.unit_name(private)
                for ext in ('service', 'timer'): (units/(name+'.'+ext)).write_text(a.UNIT_MARKER)
                with a.lock(private): a.timer(private, False)
                ctl.assert_any_call('stop', name+'.service')

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
                h=a.HTTP(dict(interface='eth0',auth_origin=endpoint,allowed_origins=[]))
                with self.assertRaises(a.SafeError): h.request('GET', endpoint)
                self.assertEqual(bound.call_count, 1)
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_existing_helper_disable_needs_no_download(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            app=Path(directory).resolve(); conf=app/'rtp2httpd_fixture.conf'
            conf.write_text('upstream-interface-multicast=eth0.85\n')
            (app/'iptv-auth.py').write_text('# fixture')
            # Stub unit discovery separately; the real config awk still runs.
            script='source "$1"\nrtp2httpd_list_instances() { RTP2HTTPD_LIST_SERVICES=(rtp2httpd_fixture.service); }\n' + 'awk() { if [[ "$*" == *ExecStart* ]]; then printf "%s\\n" "' + str(conf) + '"; else command awk "$@"; fi; }\n' + 'download_yehbp_asset() { echo UNEXPECTED_DOWNLOAD; return 1; }\npython3() { printf "INVOKED:%s\\n" "$1"; }\nip() { :; }\nsystemctl() { :; }\nrtp2httpd_auth_action disable-daily\n'
            result=subprocess.run(['bash','-c',script,'test',str(ROOT/'assets/iptv/rtp2httpd.sh')],input='1\n',text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn('UNEXPECTED_DOWNLOAD',result.stdout)
            self.assertIn('INVOKED:',result.stdout)

    def test_refresh_failure_keeps_old(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            d=Path(directory); target=d/'shcatchup.json'; a.atomic_json(target,[{'old':'yes'}])
            c=dict(userid='fixture',sn='fixture',mac='00:11:22:33:44:55',interface='eth0',auth_origin=a.DEFAULT_ORIGIN,allowed_origins=[],output=str(target))
            a.atomic_json(d/'identity.json',c)
            with patch.object(a,'local_ipv4',return_value='192.0.2.1'), patch.object(a,'login',side_effect=a.SafeError('认证失败')):
                with self.assertRaises(a.SafeError): a.refresh(d)
            self.assertEqual(json.loads(target.read_text()),[{'old':'yes'}])


if __name__ == '__main__': unittest.main()
