"""Read-only independent artifact verifier. Run on the private deployment host.
python3 tests/verify_iptv_artifacts.py /absolute/config/dir [--m3u /absolute/list.m3u]
Does not import implementation or print IDs/URLs/identities.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--m3u',type=Path);a=p.parse_args()
a.m3u=a.m3u or a.directory/'playlist.m3u'
source=a.directory/'catchup.json';catalog=a.directory/'channel.json';facts=a.directory/'resolution.json'
config=json.loads((a.directory/'config.json').read_text())
rows=json.loads(source.read_text());metadata=json.loads(catalog.read_text()) if catalog.exists() else {'categories':[]}
resolutions=json.loads(facts.read_text())['channels'] if facts.exists() else {}
candidates={}
for batch in metadata['categories']:
    for item in batch['response']['data']:
        candidates.setdefault(str(item['mixNo']),set()).add(item['name'])
entries=[];lives=[]
for line in a.m3u.read_text().splitlines():
    if line.startswith('#EXTINF:'):entries.append(line)
    elif line.startswith(('http://','https://')):lives.append(line)
assert len(entries)==len(lives)

def append_query(url, params):
    if not params: return url
    return url + ('&' if '?' in url and not url.endswith(('?', '&')) else '' if '?' in url else '?') + '&'.join(params)

def live_for(row):
    raw=row['ChannelURL']; body=raw.split('://',1)[1]
    url=config['rtp2http'].rstrip('/')+config['live']+body
    params=[]
    ip, port = row.get('ChannelFCCIP'), row.get('ChannelFCCPort')
    if ip and port and str(ip).lower()!='null' and str(port).lower()!='null':
        params.append('fcc='+str(ip)+':'+str(port))
        if config.get('fcc'): params.append('r2h-ifname-fcc='+config['fcc'])
    if config.get('multicast'): params.append('r2h-ifname='+config['multicast'])
    return append_query(url, params)

def shown_name(row):
    own=row.get('ChannelName')
    names=candidates.get(str(row.get('UserChannelID','')),set())
    if own not in (None,'','null'): return own
    if len(names)==1: return next(iter(names))
    return row['ChannelID']

visible=[]
for row in rows:
    name=shown_name(row)
    if config.get('filter') is not False and isinstance(name,str) and any(word in name for word in ('购物','理财','聚鲨')): continue
    key=row['ChannelID']+'\n'+hashlib.sha256(live_for(row).encode()).hexdigest()
    height=(resolutions.get(key) or {}).get('height')
    if config.get('filter') is not False and isinstance(height,int) and not isinstance(height,bool) and height<720: continue
    visible.append(row)
assert len(visible)==len(entries)
counts=Counter()
for row,entry,live in zip(visible,entries,lives):
    names=candidates.get(str(row.get('UserChannelID','')),set())
    name=shown_name(row)
    if row.get('ChannelName') not in (None,'','null'): counts['source']+=1
    elif len(names)==1:name=next(iter(names));counts['matched']+=1
    else:name=row['ChannelID'];counts['ambiguous' if len(names)>1 else 'unmatched']+=1
    assert entry.split('catchup=',1)[1].split(',',1)[1]==name
    assert re.search(r'tvg-id="([^\"]+)"',entry).group(1)==row['ChannelID']
    assert '/rtp/'+row['ChannelURL'].split('://',1)[1] in live
print(json.dumps(dict(records=len(visible),raw=len(rows),names=dict(counts),replay=sum('catchup-source=' in e for e in entries),none=sum('catchup="none"' in e for e in entries),no_fcc=sum('fcc=' not in u for u in lives),filtered=len(rows)-len(visible),hashes={str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in [source,catalog,a.m3u,facts] if f.exists()}),indent=2))
