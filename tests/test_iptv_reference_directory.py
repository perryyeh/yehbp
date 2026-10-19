"""Synthetic directory protocol and failure regression fixtures."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from iptv_reference_loader import iptv as c
from test_iptv_reference_cli import config, row


def directory(items):
    return dict(schema=1, categories=[dict(category=cat, type=kind, response=dict(data=copy.deepcopy(items), extra='preserved')) for cat, kind in c.CATEGORIES])


class DirectoryTests(unittest.TestCase):
    def test_verbatim_names_mixed_mix_records_and_ambiguity(self):
        names = [' CCTV 1 HD ', '上海 高清', '频道4K(高清)', '  HD 4K 高清  ']
        meta = directory([dict(mixNo=str(i), name=n, group='raw') for i,n in enumerate(names)] + [dict(mixNo='conflict',name='A'),dict(mixNo='conflict',name='A HD')])
        rows = [dict(row(),UserChannelID=str(i)) for i in range(4)] + [dict(row(),UserChannelID='0'),dict(row(),UserChannelID='conflict'),row()]
        before = copy.deepcopy(rows), copy.deepcopy(meta)
        resolved, counts = c.resolve_names(rows, meta)
        self.assertEqual(resolved, names + [names[0], '42', '42'])
        self.assertEqual(counts, dict(source=0,matched=5,ambiguous=1,unmatched=1))
        rendered=c.render(rows,config(),meta)
        self.assertEqual(rendered.count('#EXTINF:'),7)
        for name in names: self.assertIn('tvg-name="'+name+'"',rendered)
        self.assertEqual((rows,meta),before)
        self.assertEqual(c.directory_index(meta)['conflict'],['A','A HD'])

    def test_directory_bad_response_preserves_both_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=c.instance_paths(Path(tmp).resolve()/'private','x'); c.private_dir(paths[0])
            cfg=config(); cfg['directory_metadata']=True
            c.atomic_json(paths[1],cfg); c.atomic_json(paths[2],[row()]); meta=c.metadata_path(paths,'x')
            old=directory([dict(mixNo='1',name='Old')]); c.atomic_json(meta,old)
            before=paths[2].read_bytes(),meta.read_bytes()
            broken=copy.deepcopy(old); broken['categories'][-1]['response']={'error':'bad'}
            class HTTP:
                def request(self,*args,**kwargs): return json.dumps({'error':'bad'}),'http://fixture'
            with self.assertRaises(c.SafeError): c.directory_index(broken)
            failed=copy.deepcopy(old);failed['categories'][0]['response']['status']='0'
            with self.assertRaises(c.SafeError): c.directory_index(failed)
            with patch.object(c,'local_ipv4',return_value='192.0.2.1'),patch.object(c,'login',return_value=[row(),row()]),patch.object(c,'portal_directory',side_effect=c.SafeError('bad')):
                self.assertEqual(c.main(['fetch','--directory',str(paths[0]),'--instance','x']),1)
            self.assertEqual((paths[2].read_bytes(),meta.read_bytes()),before)

    def test_m3u_output_config_and_cli_precedence_and_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); paths=c.instance_paths(root/'private','x');c.private_dir(paths[0])
            cfg=config();cfg['m3u_output']=str(root/'custom.m3u')
            c.atomic_json(paths[1],cfg);c.atomic_json(paths[2],[row()])
            c.convert(paths,'x'); self.assertTrue((root/'custom.m3u').is_file());self.assertFalse(paths[3].exists())
            override=root/'override.m3u';c.convert(paths,'x',str(override))
            self.assertEqual(override.read_bytes(),(root/'custom.m3u').read_bytes())
            before=paths[2].read_bytes()
            with self.assertRaises(c.SafeError): c.convert(paths,'x',str(paths[2]))
            self.assertEqual(paths[2].read_bytes(),before)

    def test_single_file_default_paths_independent_of_cwd(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); install=root/'install'; install.mkdir(); elsewhere=root/'cwd';elsewhere.mkdir()
            script=install/'iptv.py';script.write_bytes(Path(c.__file__).read_bytes())
            result=subprocess.run([sys.executable,str(script),'configure','--instance','x','--identity-stdin','--m3u-output',str(root/'result.m3u')],input='fixture\nfixture-box\n00:11:22:33:44:55\n',text=True,capture_output=True,cwd=elsewhere)
            self.assertEqual(result.returncode,0,result.stderr)
            c.atomic_json(install/'catchup_x.json',[row()])
            result=subprocess.run([sys.executable,str(script),'convert','--instance','x'],capture_output=True,text=True,cwd=elsewhere)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((root/'result.m3u').exists());self.assertEqual(list(elsewhere.iterdir()),[])

    def test_portal_protocol_context_cookies_and_raw_response(self):
        class HTTP:
            def __init__(self): self.calls=[]
            def request(self,method,url,fields=None,referer=None):
                self.calls.append((method,url,fields,referer))
                if len(self.calls)==1: body="top.document.location='/balanced';"
                elif len(self.calls)==2: body="<form action='/portal-auth'><input name='keep' value='original'></form>"
                elif len(self.calls)==3: body="jsSetConfig('IpPort','218.83.165.36:8084'); jsSetConfig('framecode','fixture'); jsSetConfig('SessionID','fixture');"
                elif len(self.calls)==4: body='portal'
                elif len(self.calls)==5: body='{"bimsUserToken":"fixture"}'
                else: body=json.dumps(dict(data=[dict(mixNo='42',name=' 高清 HD 4K ',extra=['keep'])],extra='complete'))
                return body,url
        http=HTTP();ctx=dict(http=http,text="var usertoken='fixture'; var epggroup='1060'; EPGDomain=http://218.83.165.50:8084/index\"",dynamic_ip='fixture',referer='http://218.83.165.50:8084/ott')
        with patch.object(c,'set_cookie') as cookie: result=c.portal_directory(config(),ctx)
        self.assertEqual(len(http.calls),17)
        self.assertEqual(http.calls[0][2]['DynamicAuthIP'],'fixture')
        self.assertEqual(http.calls[2][2]['keep'],'original')
        self.assertTrue(cookie.called)
        self.assertEqual(result['categories'][0]['response'],dict(data=[dict(mixNo='42',name=' 高清 HD 4K ',extra=['keep'])],extra='complete'))
        self.assertEqual(c.directory_index(result)['42'],[' 高清 HD 4K '])
