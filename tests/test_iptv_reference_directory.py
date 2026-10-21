"""Synthetic directory protocol and failure regression fixtures."""
import copy
import io
import json
import os
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
        self.assertEqual(counts, dict(override=0, source=0,matched=5,ambiguous=1,unmatched=1))
        rendered, counts = c.render(rows, config(), meta, apply_filter=False)
        self.assertEqual(rendered.count('#EXTINF:'), 7)
        self.assertEqual(counts['kept'], 7)
        for name in names: self.assertIn('tvg-name="'+name+'"',rendered)
        self.assertEqual((rows,meta),before)
        self.assertEqual(c.directory_index(meta)['conflict'],['A','A HD'])

    def test_directory_bad_response_preserves_both_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=c.config_paths(Path(tmp).resolve()/'private'); c.private_dir(paths[0])
            cfg=config()
            c.atomic_json(paths[1],cfg); c.atomic_json(paths[2],[row()]); meta=c.metadata_path(paths)
            old=directory([dict(mixNo='1',name='Old')]); c.atomic_json(meta,old)
            before=paths[2].read_bytes(),meta.read_bytes()
            broken=copy.deepcopy(old); broken['categories'][-1]['response']={'error':'bad'}
            class HTTP:
                def request(self,*args,**kwargs): return json.dumps({'error':'bad'}),'http://fixture'
            with self.assertRaises(c.SafeError): c.directory_index(broken)
            failed=copy.deepcopy(old);failed['categories'][0]['response']['status']='0'
            with self.assertRaises(c.SafeError): c.directory_index(failed)
            with patch.object(c,'local_ipv4',return_value='192.0.2.1'),patch.object(c,'login',return_value=[row(),row()]),patch.object(c,'portal_directory',side_effect=c.SafeError('bad')):
                self.assertEqual(c.main(['fetch','config='+str(paths[0])]),1)
            self.assertEqual((paths[2].read_bytes(),meta.read_bytes()),before)

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

    def test_channel_names_exact_id_beats_portal_and_filters_mapped_name(self):
        rows = [
            row(ChannelID='5000027', UserChannelID='1', ChannelName='原始名'),
            row(ChannelID='5000028', UserChannelID='2'),
            row(ChannelID='6000138', UserChannelID='3'),
            row(ChannelID='unmapped', UserChannelID='4'),
            row(ChannelID='suffix-5000027', UserChannelID='5'),
        ]
        meta = directory([
            dict(mixNo='1', name='门户聚鲨'),
            dict(mixNo='2', name='门户名'),
            dict(mixNo='3', name='门户名'),
            dict(mixNo='4', name='门户保留'),
            dict(mixNo='5', name='后缀不该命中'),
        ])
        cfg = config()
        cfg['filter'] = dict(enabled=True, min_height=0, exclude_names=['聚鲨'])
        cfg['channel_names'] = {'5000027': '聚鲨', '5000028': '多彩文体4K', 'missing-id': '未使用'}
        before_rows, before_meta = copy.deepcopy(rows), copy.deepcopy(meta)
        resolved, counts = c.resolve_names(rows, meta, cfg)
        self.assertEqual(resolved, ['聚鲨', '多彩文体4K', '门户名', '门户保留', '后缀不该命中'])
        self.assertEqual(counts['override'], 2)
        self.assertEqual(counts['source'], 0)
        rendered, decisions = c.render(rows, cfg, meta, resolutions=None)
        self.assertNotIn('tvg-id="5000027"', rendered)
        self.assertIn('tvg-name="多彩文体4K"', rendered)
        self.assertIn('tvg-id="6000138"', rendered)
        self.assertIn(',门户保留', rendered)
        self.assertEqual(decisions['excluded_name'], 1)
        self.assertEqual(decisions['kept'], 4)
        self.assertEqual(rows[0]['ChannelID'], '5000027')
        self.assertEqual(rows[0]['ChannelName'], '原始名')
        self.assertEqual((rows, meta), (before_rows, before_meta))
        self.assertNotIn('5000027', json.dumps(meta, ensure_ascii=False))

    def test_unknown_channel_name_key_rejected_and_save_keeps_mapping(self):
        with self.assertRaises(c.SafeError):
            c.validate(dict(config(), channel_names={'5000027': ''}))
        with self.assertRaises(c.SafeError):
            c.validate(dict(config(), channel_names=['5000027']))
        with tempfile.TemporaryDirectory() as tmp:
            paths = c.config_paths(Path(tmp).resolve() / 'private')
            c.private_dir(paths[0])
            cfg = config()
            cfg['filter'] = dict(enabled=True, min_height=720, exclude_names=['购物'])
            cfg['channel_names'] = {'6000141': '江苏卫视4K'}
            cfg['epg'] = 'http://example.test/epg.xml'
            c.atomic_json(paths[1], cfg)
            c.atomic_json(paths[2], [row(ChannelID='6000141', UserChannelID='9')])
            c.atomic_json(c.metadata_path(paths), directory([dict(mixNo='9', name='门户江苏')]))
            os.chmod(paths[1], 0o600)
            with patch.object(c, 'login', side_effect=AssertionError('login')), patch.object(c, 'probe_fact', side_effect=AssertionError('probe')):
                self.assertEqual(c.main(['config=' + str(paths[0]), 'filter=false']), 0)
            saved = c.load(paths[1])
            self.assertEqual(saved['channel_names'], {'6000141': '江苏卫视4K'})
            self.assertEqual(saved['filter']['exclude_names'], ['购物'])
            self.assertIs(saved['filter']['enabled'], False)
            self.assertEqual(saved['epg'], 'http://example.test/epg.xml')
            playlist = paths[3].read_text()
            self.assertIn('tvg-id="6000141"', playlist)
            self.assertIn('tvg-name="江苏卫视4K"', playlist)
            self.assertNotIn('门户江苏', playlist)
