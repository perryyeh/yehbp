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
source=a.directory/'catchup.json';catalog=a.directory/'channels.json'
rows=json.loads(source.read_text());metadata=json.loads(catalog.read_text()) if catalog.exists() else {'categories':[]};candidates={}
for batch in metadata['categories']:
    for item in batch['response']['data']:
        candidates.setdefault(str(item['mixNo']),set()).add(item['name'])
entries=[];lives=[]
for line in a.m3u.read_text().splitlines():
    if line.startswith('#EXTINF:'):entries.append(line)
    elif line.startswith(('http://','https://')):lives.append(line)
assert len(rows)==len(entries)==len(lives)
counts=Counter()
for row,entry,live in zip(rows,entries,lives):
    names=candidates.get(str(row.get('UserChannelID','')),set())
    if row.get('ChannelName') not in (None,'','null'): name=row['ChannelName'];counts['source']+=1
    elif len(names)==1:name=next(iter(names));counts['matched']+=1
    else:name=row['ChannelID'];counts['ambiguous' if len(names)>1 else 'unmatched']+=1
    assert entry.split('catchup=',1)[1].split(',',1)[1]==name
    assert re.search(r'tvg-id="([^\"]+)"',entry).group(1)==row['ChannelID']
    assert '/rtp/'+row['ChannelURL'].split('://',1)[1] in live
print(json.dumps(dict(records=len(rows),names=dict(counts),replay=sum('catchup-source=' in e for e in entries),none=sum('catchup="none"' in e for e in entries),no_fcc=sum('fcc=' not in u for u in lives),hashes={str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in [source,catalog,a.m3u] if f.exists()}),indent=2))
