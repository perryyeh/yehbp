import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from iptv_reference_loader import iptv as c
from iptv_reference_loader import iptv as auth


def config():
    return dict(provider='shanghai-telecom', userid='fixture', sn='fixture-box', mac='00:11:22:33:44:55', interface='eth0.85', fcc_interface='eth0.51', auth_origin=auth.DEFAULT_ORIGIN, allowed_origins=[], base_url='http://127.0.0.1:5140/prefix', live_path='/rtp/', replay_path='/rtsp/', seek_template=c.SEEK)


def row(**kw):
    return dict(ChannelID='42', ChannelURL='igmp://239.1.2.3:1234', TimeShiftURL='rtsp://example.test/path%2Fsub?AuthInfo=A%2fb+C==&x=1&x=2', Unknown='preserve', **kw)


class CliTests(unittest.TestCase):
    def test_all_records_order_duplicates_and_placeholder(self):
        rows=[row(),row(ChannelName='真实频道'),row()]
        m=c.render(rows,config())
        self.assertEqual(m.count('#EXTINF:'),3)
        self.assertEqual([x.rsplit(',',1)[1] for x in m.splitlines() if x.startswith('#EXTINF:')],['42','真实频道','42'])
        self.assertIn('ChannelID is a placeholder',m)
        self.assertNotIn('group-title',m)
        self.assertNotIn('x-tvg-url',m)
        self.assertEqual(rows[0]['Unknown'],'preserve')

    def test_operator_names_preserve_hd_4k_spaces_verbatim(self):
        names=[' CCTV 1 HD ', '上海 高清', '频道4K(高清)', '  HD 4K 高清  ']
        m=c.render([row(ChannelName=n) for n in names],config())
        self.assertEqual([x.rsplit(',',1)[1] for x in m.splitlines() if x.startswith('#EXTINF:')],names)
        for n in names: self.assertIn('tvg-name="'+n+'"',m)

    def test_null_empty_replay_disabled_and_missing_fcc(self):
        for null in (None,'null','NULL','', ' null '):
            r=row(); r['TimeShiftURL']=null
            m=c.render([r],config())
            self.assertIn('catchup="none"',m)
            self.assertNotIn('catchup-source=',m)
            self.assertNotIn('fcc=',m)
        for values in ({},{'ChannelFCCIP':'null','ChannelFCCPort':'null'},{'ChannelFCCIP':'10.0.0.1'},{'ChannelFCCIP':'','ChannelFCCPort':''}):
            self.assertNotIn('fcc=',c.live_url(row(**values),config()))
        self.assertIn('?fcc=10.0.0.1:8027&r2h-ifname-fcc=eth0.51&r2h-ifname=eth0.85',c.live_url(row(ChannelFCCIP='10.0.0.1',ChannelFCCPort='8027'),config()))

    def test_rtsp_exact_query_and_templates(self):
        url=c.replay_url(row()['TimeShiftURL'],config())
        self.assertIn('/rtsp/example.test:554/path%2Fsub?AuthInfo=A%2fb+C==&x=1&x=2&playseek='+c.SEEK,url)
        self.assertTrue(url.endswith('&r2h-ifname=eth0.85'))
        cfg=config(); cfg['multicast_interface']='eth0.51'
        self.assertTrue(c.live_url(row(),cfg).endswith('r2h-ifname=eth0.51'))
        self.assertTrue(c.replay_url(row()['TimeShiftURL'],cfg).endswith('r2h-ifname=eth0.85'))
        cfg=config(); cfg.update(base_url='http://example.test:333',live_path='/udp/',replay_path='/proxy/rtsp/',seek_template='${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}')
        self.assertIn('/proxy/rtsp/host:8554/a?token=%26%3D+&playseek=${(b)',c.replay_url('rtsp://host:8554/a?token=%26%3D+',cfg))
        self.assertIn('/udp/239.',c.live_url(row(),cfg))
        for invalid in ('http://host/a','rtsp://u:p@host/a','rtsp://host/a#frag','rtsp://host/a?playseek=old'):
            with self.assertRaises(auth.SafeError): c.replay_url(invalid,config())

    def test_native_config_stdin_permissions_retention_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp).resolve()/'private'
            args=['configure','--config-dir',str(d),'--stdin']
            with patch('sys.stdin',io.StringIO('fixture\nfixture-sn\n00:11:22:33:44:55\neth0.85\n' + '\n'*8)): self.assertEqual(c.main(args),0)
            paths=c.config_paths(d)
            cfg=c.load(paths[1]); self.assertEqual(cfg['sn'],'fixture-sn')
            self.assertEqual(d.stat().st_mode&0o777,0o700)
            self.assertEqual(paths[1].stat().st_mode&0o777,0o600)
            with patch('sys.stdin',io.StringIO('\n'*12)): self.assertEqual(c.main(args),0)
            self.assertEqual(c.load(paths[1]),cfg)
            beta=c.config_paths(d.parent/'beta'); auth.private_dir(beta[0]); auth.atomic_json(beta[1],cfg)
            from test_iptv_reference_directory import directory
            with patch.object(auth,'local_ipv4',return_value='192.0.2.1'), patch.object(auth,'login',return_value=[row(),row()]), patch.object(auth,'portal_directory',return_value=directory([dict(mixNo='1',name='fixture')])):
                c.fetch(paths); c.fetch(beta)
            c.convert(paths); c.convert(beta)
            self.assertEqual(json.loads(paths[2].read_text()),[row(),row()])
            self.assertEqual(paths[3].read_text(),beta[3].read_text())
            paths[2].unlink(); self.assertTrue(beta[2].exists())
            self.assertFalse((d/'catchup_tel.json').exists())
            for p in d.iterdir(): self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_failures_preserve_previous_outputs_and_redact(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp).resolve()/'private'; auth.private_dir(d)
            paths=c.config_paths(d); auth.atomic_json(paths[1],config())
            auth.atomic_json(paths[2],[row()]); c.convert(paths)
            before=paths[2].read_bytes(),paths[3].read_bytes()
            with patch.object(auth,'local_ipv4',return_value='192.0.2.1'), patch.object(auth,'login',side_effect=auth.SafeError('fixture')):
                with self.assertRaises(auth.SafeError): c.fetch(paths)
            self.assertEqual(paths[2].read_bytes(),before[0])
            paths[2].write_text('[{"ChannelID":"42","TimeShiftURL":"rtsp://secret/?AuthInfo=secret"}]')
            out=io.StringIO()
            with contextlib.redirect_stderr(out): self.assertEqual(c.main(['convert','--config-dir',str(d)]),1)
            self.assertNotIn('secret',out.getvalue()); self.assertEqual(paths[3].read_bytes(),before[1])
            with patch.object(os,'replace',side_effect=OSError('fixture')):
                with self.assertRaises(OSError): c.atomic_text(paths[3],'new')
            self.assertEqual(paths[3].read_bytes(),before[1]); self.assertFalse(list(d.glob('.iptv-*')))

    def test_real_three_step_http_login(self):
        import http.server
        import threading
        seen=[]
        raw='var usertoken="fixture"; var EPGDomain="http://fixture/"; var channelArray=[\'ChannelID="1" ChannelURL="igmp://239.1.2.3:1234" TimeShiftURL="null" Extra="x"\',\'ChannelID="1" ChannelURL="igmp://239.1.2.3:1234" TimeShiftURL="null" Extra="x"\'];'
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def send(self,body):
                self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Set-Cookie','fixture=1; Path=/'); self.end_headers(); self.wfile.write(body.encode())
            def do_GET(self):
                seen.append(('GET',self.headers.get('Cookie'))); self.send('<form action="/step2"><input name="fixture" value="a&amp;b"></form>')
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']))
                seen.append(('POST',self.headers.get('Cookie')))
                if self.path=='/step2':
                    self.send('<script>var encrytoken="fixture-token";</script><form id="initform" action="/step3"><input name="x" value="y"></form>')
                else:
                    self.server.authenticator_present=b'authenticator=' in body
                    self.send(raw)
        server=http.server.HTTPServer(('127.0.0.1',0),Handler); thread=threading.Thread(target=server.serve_forever); thread.start()
        cfg=config(); cfg['auth_origin']='http://127.0.0.1:'+str(server.server_port)
        def fixture_socket(interface,timeout):
            s=auth.socket.socket(auth.socket.AF_INET,auth.socket.SOCK_STREAM); s.settimeout(timeout); return s
        try:
            with patch.object(auth,'bound_socket',side_effect=fixture_socket): rows=auth.login(cfg,'192.0.2.1')
            self.assertEqual(len(rows),2); self.assertEqual(rows[0],rows[1]); self.assertTrue(server.authenticator_present)
            self.assertEqual(seen,[('GET',None),('POST','fixture=1'),('POST','fixture=1')])
        finally: server.shutdown(); thread.join(); server.server_close()


if __name__=='__main__': unittest.main()
