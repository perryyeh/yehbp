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
    return dict(userid='fixture', sn='fixture-box', mac='00:11:22:33:44:55', interface='eth0.85', authurl=auth.DEFAULT_ORIGIN, rtp2http='http://127.0.0.1:5140/prefix', live='/rtp/', replay='/rtsp/', seek=c.SEEK, filter=False)


def row(**kw):
    base=dict(ChannelID='42', ChannelURL='igmp://239.1.2.3:1234', TimeShiftURL='rtsp://example.test/path%2Fsub?AuthInfo=A%2fb+C==&x=1&x=2', Unknown='preserve')
    base.update(kw)
    return base


class CliTests(unittest.TestCase):
    def test_all_records_order_duplicates_and_placeholder(self):
        rows=[row(),row(ChannelName='真实频道'),row()]
        m, counts = c.render(rows, config(), apply_filter=False)
        self.assertEqual(m.count('#EXTINF:'), 3)
        self.assertEqual(counts['kept'], 3)
        self.assertEqual([x.rsplit(',',1)[1] for x in m.splitlines() if x.startswith('#EXTINF:')],['42','真实频道','42'])
        self.assertIn('ChannelID is a placeholder',m)
        self.assertNotIn('group-title',m)
        self.assertNotIn('x-tvg-url',m)
        self.assertEqual(rows[0]['Unknown'],'preserve')

    def test_operator_names_preserve_hd_4k_spaces_verbatim(self):
        names=[' CCTV 1 HD ', '上海 高清', '频道4K(高清)', '  HD 4K 高清  ']
        m, _counts = c.render([row(ChannelName=n) for n in names], config(), apply_filter=False)
        self.assertEqual([x.rsplit(',',1)[1] for x in m.splitlines() if x.startswith('#EXTINF:')],names)
        for n in names: self.assertIn('tvg-name="'+n+'"',m)

    def test_name_cleanup_and_groups_follow_config(self):
        cfg = config()
        cfg['name_cleanup'] = dict(
            remove=['HD', '高清'],
            groups=[
                dict(name='央视', names=['CCTV-1', 'CCTV-5+', '兵器科技']),
                dict(name='卫视', names=['东方卫视', '湖南卫视']),
                dict(name='4K', contains='4k'),
                dict(name='上海台', names=['新闻综合', '五星体育']),
                dict(name='其它'),
            ])
        rows = [
            row(ChannelID='hd-1', ChannelName='CCTV-1HD'),
            row(ChannelID='mark-bing', ChannelName='兵器科技'),
            row(ChannelID='sat-1', ChannelName='湖南卫视高清'),
            row(ChannelID='plus-1', ChannelName='CCTV-5+HD'),
            row(ChannelID='k4-1', ChannelName='CCTV4K'),
            row(ChannelID='k4-2', ChannelName='百事通电影4K'),
            row(ChannelID='sh-1', ChannelName=' 新闻综合 高清 '),
            row(ChannelID='other-1', ChannelName='金鹰纪实'),
            row(ChannelID='dup-1', ChannelName='东方卫视'),
            row(ChannelID='dup-2', ChannelName='东方卫视'),
            row(ChannelID='en-1', ChannelName='HDTV Sample'),
            row(ChannelID='mark-1', ChannelName='频道4K(高清)'),
        ]
        before = json.dumps(rows, ensure_ascii=False)
        m, counts = c.render(rows, cfg, apply_filter=False)
        names = [x.rsplit(',', 1)[1] for x in m.splitlines() if x.startswith('#EXTINF:')]
        groups = [x.split('group-title="', 1)[1].split('"', 1)[0] for x in m.splitlines() if x.startswith('#EXTINF:')]
        self.assertEqual(names, ['CCTV-1', 'CCTV-5+', '兵器科技', '东方卫视', '东方卫视', '湖南卫视', 'CCTV4K', '百事通电影4K', '频道4K( )', '新闻综合', '金鹰纪实', 'HDTV Sample'])
        self.assertEqual(groups, ['央视', '央视', '央视', '卫视', '卫视', '卫视', '4K', '4K', '4K', '上海台', '其它', '其它'])
        self.assertEqual(counts['kept'], 12)
        self.assertIn('tvg-id="dup-1"', m)
        self.assertIn('tvg-id="dup-2"', m)
        self.assertIn('tvg-name="HDTV Sample"', m)
        self.assertEqual(json.dumps(rows, ensure_ascii=False), before)
        mapped = config()
        mapped['channel_names'] = {'id-1': 'CCTV-1HD'}
        mapped['name_cleanup'] = cfg['name_cleanup']
        mapped['filter'] = dict(enabled=True, min_height=0, exclude_names=['购物'])
        cleaned, decisions = c.render([row(ChannelID='id-1', ChannelName='原始')], mapped, resolutions=None)
        self.assertIn('tvg-name="CCTV-1"', cleaned)
        self.assertIn('group-title="央视"', cleaned)
        self.assertEqual(decisions['kept'], 1)
        partial = config()
        partial['name_cleanup'] = dict(remove=['HD'])
        only, _counts = c.render([row(ChannelID='p', ChannelName='湖南卫视高清')], partial, apply_filter=False)
        self.assertIn(',湖南卫视高清', only)
        self.assertNotIn('group-title', only)
        with self.assertRaises(c.SafeError):
            c.validate(dict(config(), name_cleanup=dict(remove=[''])))
        with self.assertRaises(c.SafeError):
            c.validate(dict(config(), name_cleanup=dict(groups=[dict(name='4K')])))
        with self.assertRaises(c.SafeError):
            c.validate(dict(config(), name_cleanup=dict(groups=[dict(name='4K', contains='4K', order=['4K', '4K'])])))

    def test_group_order_follows_names_not_input_and_contains_stays_first(self):
        cfg = config()
        cfg['name_cleanup'] = dict(groups=[
            dict(name='卫视', names=['东方卫视', '延边卫视', '湖南卫视']),
            dict(name='4K', contains='4K', order=['CCTV4K', '东方卫视4K', '百事通电影4K']),
            dict(name='其它', order=['金鹰纪实']),
        ])
        rows = [
            row(ChannelID='tail-b', ChannelName='未列乙'),
            row(ChannelID='hn', ChannelName='湖南卫视'),
            row(ChannelID='df-1', ChannelName='东方卫视'),
            row(ChannelID='df-2', ChannelName='东方卫视'),
            row(ChannelID='k-movie', ChannelName='百事通电影4K'),
            row(ChannelID='k-east', ChannelName='东方卫视4K'),
            row(ChannelID='k-cctv', ChannelName='CCTV4K'),
            row(ChannelID='k-dup-a', ChannelName='多彩文体4K'),
            row(ChannelID='k-dup-b', ChannelName='多彩文体4K'),
            row(ChannelID='other-doc', ChannelName='金鹰纪实'),
            row(ChannelID='tail-a', ChannelName='未列甲'),
        ]
        before = json.dumps(rows, ensure_ascii=False)
        m, counts = c.render(rows, cfg, apply_filter=False)
        names = [x.rsplit(',', 1)[1] for x in m.splitlines() if x.startswith('#EXTINF:')]
        groups = [x.split('group-title="', 1)[1].split('"', 1)[0] for x in m.splitlines() if x.startswith('#EXTINF:')]
        ids = [x.split('tvg-id="', 1)[1].split('"', 1)[0] for x in m.splitlines() if x.startswith('#EXTINF:')]
        self.assertEqual(names, ['东方卫视', '东方卫视', '湖南卫视', 'CCTV4K', '东方卫视4K', '百事通电影4K', '多彩文体4K', '多彩文体4K', '金鹰纪实', '未列乙', '未列甲'])
        self.assertEqual(groups, ['卫视', '卫视', '卫视', '4K', '4K', '4K', '4K', '4K', '其它', '其它', '其它'])
        self.assertEqual(ids, ['df-1', 'df-2', 'hn', 'k-cctv', 'k-east', 'k-movie', 'k-dup-a', 'k-dup-b', 'other-doc', 'tail-b', 'tail-a'])
        self.assertEqual(counts['kept'], 11)
        self.assertEqual(json.dumps(rows, ensure_ascii=False), before)
        self.assertEqual(groups[3], '4K')
        self.assertEqual(names[3], 'CCTV4K')

    def test_null_empty_replay_disabled_and_missing_fcc(self):
        for null in (None,'null','NULL','', ' null '):
            r=row(); r['TimeShiftURL']=null
            m, _counts = c.render([r], config(), apply_filter=False)
            self.assertIn('catchup="none"',m)
            self.assertNotIn('catchup-source=',m)
            self.assertNotIn('fcc=',m)
        for values in ({},{'ChannelFCCIP':'null','ChannelFCCPort':'null'},{'ChannelFCCIP':'10.0.0.1'},{'ChannelFCCIP':'','ChannelFCCPort':''}):
            self.assertNotIn('fcc=',c.live_url(row(**values),config()))
        self.assertIn('?fcc=10.0.0.1:8027',c.live_url(row(ChannelFCCIP='10.0.0.1',ChannelFCCPort='8027'),config()))
        self.assertNotIn('r2h-ifname',c.live_url(row(ChannelFCCIP='10.0.0.1',ChannelFCCPort='8027'),config()))

    def test_rtsp_exact_query_and_templates(self):
        url=c.replay_url(row()['TimeShiftURL'],config())
        self.assertIn('/rtsp/example.test:554/path%2Fsub?AuthInfo=A%2fb+C==&x=1&x=2&playseek='+c.SEEK,url)
        self.assertNotIn('r2h-ifname',url)
        cfg=config(); cfg['multicast']='eth0.51'; cfg['fcc']='eth0.86'
        live=c.live_url(row(ChannelFCCIP='10.0.0.1',ChannelFCCPort='8027'),cfg)
        self.assertIn('?fcc=10.0.0.1:8027&r2h-ifname-fcc=eth0.86&r2h-ifname=eth0.51',live)
        self.assertNotIn('eth0.85',live)
        replay=c.replay_url(row()['TimeShiftURL'],cfg)
        self.assertTrue(replay.endswith('playseek='+c.SEEK))
        self.assertNotIn('r2h-ifname',replay)
        only_mcast=config(); only_mcast['multicast']='eth0.51'
        mcast_url=c.live_url(row(ChannelFCCIP='10.0.0.1',ChannelFCCPort='8027'),only_mcast)
        self.assertIn('fcc=10.0.0.1:8027',mcast_url)
        self.assertIn('r2h-ifname=eth0.51',mcast_url)
        self.assertNotIn('r2h-ifname-fcc',mcast_url)
        only_fcc=config(); only_fcc['fcc']='eth0.86'
        fcc_url=c.live_url(row(ChannelFCCIP='10.0.0.1',ChannelFCCPort='8027'),only_fcc)
        self.assertIn('r2h-ifname-fcc=eth0.86',fcc_url)
        self.assertNotIn('r2h-ifname=',fcc_url)
        blank=config(); blank['multicast']=''; blank['fcc']=''
        self.assertNotIn('r2h-ifname',c.live_url(row(ChannelFCCIP='10.0.0.1',ChannelFCCPort='8027'),blank))
        no_fcc=c.live_url(row(),cfg)
        self.assertNotIn('fcc=',no_fcc)
        self.assertTrue(no_fcc.endswith('?r2h-ifname=eth0.51'))
        source='igmp://239.1.2.3:1234?r2h-ifname=eth9&fcc=kept&r2h-ifname-fcc=eth8'
        kept=c.live_url(row(ChannelURL=source,ChannelFCCIP='10.0.0.1',ChannelFCCPort='8027'),cfg)
        self.assertIn('?r2h-ifname=eth9&fcc=kept&r2h-ifname-fcc=eth8&fcc=10.0.0.1:8027&r2h-ifname-fcc=eth0.86&r2h-ifname=eth0.51',kept)
        replay_source='rtsp://example.test/path?AuthInfo=A%2fb+C==&r2h-ifname=eth9'
        replay=c.replay_url(replay_source,cfg)
        self.assertIn('AuthInfo=A%2fb+C==&r2h-ifname=eth9&playseek=',replay)
        self.assertEqual(replay.count('r2h-ifname'),1)
        cfg=config(); cfg.update(rtp2http='http://example.test:333',live='/udp/',replay='/proxy/rtsp/',seek='${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}')
        self.assertIn('/proxy/rtsp/host:8554/a?token=%26%3D+&playseek=${(b)',c.replay_url('rtsp://host:8554/a?token=%26%3D+',cfg))
        self.assertIn('/udp/239.',c.live_url(row(),cfg))
        for invalid in ('http://host/a','rtsp://u:p@host/a','rtsp://host/a#frag','rtsp://host/a?playseek=old'):
            with self.assertRaises(auth.SafeError): c.replay_url(invalid,config())

    def test_native_config_prompts_permissions_retention_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp).resolve()/'private'
            args=['configure','config='+str(d)]
            with patch.object(c.getpass,'getpass',side_effect=['fixture','fixture-sn','00:11:22:33:44:55','eth0.85']+['']*6): self.assertEqual(c.main(args),0)
            paths=c.config_paths(d)
            cfg=c.load(paths[1]); self.assertEqual(cfg['sn'],'fixture-sn')
            self.assertEqual(d.stat().st_mode&0o777,0o700)
            self.assertEqual(paths[1].stat().st_mode&0o777,0o600)
            with patch.object(c.getpass,'getpass',return_value=''): self.assertEqual(c.main(args),0)
            self.assertEqual(c.load(paths[1]),cfg)
            beta=c.config_paths(d.parent/'beta'); auth.private_dir(beta[0]); auth.atomic_json(beta[1],cfg)
            from test_iptv_reference_directory import directory
            with patch.object(auth,'local_ipv4',return_value='192.0.2.1'), patch.object(auth,'login',return_value=[row(),row()]), patch.object(auth,'portal_directory',return_value=directory([dict(mixNo='1',name='fixture')])):
                c.fetch(paths); c.fetch(beta)
            cfg['filter']=False; auth.atomic_json(paths[1],cfg); auth.atomic_json(beta[1],cfg)
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
            with contextlib.redirect_stderr(out): self.assertEqual(c.main(['convert','config='+str(d)]),1)
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
        cfg=config(); cfg['authurl']='http://127.0.0.1:'+str(server.server_port)
        def fixture_socket(interface,timeout):
            s=auth.socket.socket(auth.socket.AF_INET,auth.socket.SOCK_STREAM); s.settimeout(timeout); return s
        try:
            with patch.object(auth,'bound_socket',side_effect=fixture_socket): rows=auth.login(cfg,'192.0.2.1')
            self.assertEqual(len(rows),2); self.assertEqual(rows[0],rows[1]); self.assertTrue(server.authenticator_present)
            self.assertEqual(seen,[('GET',None),('POST','fixture=1'),('POST','fixture=1')])
        finally: server.shutdown(); thread.join(); server.server_close()


if __name__=='__main__': unittest.main()
