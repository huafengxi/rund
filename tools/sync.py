#!/usr/bin/env python3
"""Single-writer object transport. Replicas never overwrite local originals."""
import argparse
import gzip
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import validate,changed,discover,node_config
from runtime.store import read,write,canonical,require


def writer_for(rel,body,c,root):
    if rel.startswith('configs/'):return c['configWriter']
    p=Path(rel)
    require(p.name in ('declaration.json','state.json'),'unknown synchronized file')
    if p.name=='declaration.json':return c['publishers'][validate(body)['publisher']]
    dp=Path(root)/p.with_name('declaration.json');require(dp.exists(),'state before declaration')
    d=validate(read(dp));require(body['id']==d['id'] and body['kind']==d['kind'] and body['node']==d['node'],'state binding mismatch')
    return d['node']

def export(c,*,all_origins=False):
    root=Path(c['sharedRoot']);records=[]
    files=sorted(root.rglob('*'),key=lambda p:(p.name=='state.json',str(p)))
    for p in files:
        if not p.is_file() or p.name.startswith('.'):continue
        rel=p.relative_to(root).as_posix()
        if p.name not in ('declaration.json','state.json') and not rel.startswith('configs/'):continue
        body=read(p);writer=writer_for(rel,body,c,root)
        if all_origins or writer==c['node']:records.append(dict(path=rel,writer=writer,body=body))
    return records

def receive(c,records,*,source=None):
    root=Path(c['sharedRoot']);errors=[]
    for r in sorted(records,key=lambda r:(Path(r['path']).name=='state.json',r['path'])):
        try:
            rel=r['path'];p=Path(rel);require(not p.is_absolute() and '..' not in p.parts,'unsafe replica path')
            writer=writer_for(rel,r['body'],c,root);require(writer==r['writer'],'replica writer mismatch')
            if source:require(writer==source,'foreign origin in upload')
            if writer==c['node']:continue
            out=root/p
            if out.exists():
                before=read(out);after=r['body']
                if p.name=='declaration.json':
                    if after['revision']<before['revision']:continue
                    changed(before,after)
                elif p.name=='state.json':
                    if before['kind']=='Task' and before.get('phase') in ('succeeded','failed','canceled'):
                        require(before==after,'terminal Task replica changed');continue
                else:require(before==r['body'],'immutable config collision');continue
            write(out,r['body'])
        except (OSError,ValueError,KeyError,TypeError) as e:errors.append(str(e))
    require(not errors,'; '.join(errors))

def transport(c,action,data=None):
    import shlex
    peer=c['hub'];argv=['ssh','-F',c['sshConfig'],'-o','BatchMode=yes','-o','ConnectTimeout=10',peer['host'],shlex.join([peer['python'],'-B',peer['tool'],'--config',peer['config'],action]+(['--source',c['node']] if action=='receive' else ['--all']))]
    payload=gzip.compress(canonical(data)) if data is not None else None
    p=subprocess.run(argv,input=payload,capture_output=True,timeout=c.get('timeout',45));require(p.returncode==0,'sync peer failed: '+p.stderr.decode(errors='replace')[-1000:])
    return json.loads(gzip.decompress(p.stdout)) if action=='export' else None

def sync_config(path):
    link=read(path)
    return dict(node_config(link['nodeConfig']),**{k:v for k,v in link.items() if k!='nodeConfig'})


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True,type=Path);p.add_argument('action',choices=('watch','once','export','receive'));p.add_argument('--all',action='store_true');p.add_argument('--source');a=p.parse_args();c=sync_config(a.config)
    if a.action=='export':sys.stdout.buffer.write(gzip.compress(canonical(export(c,all_origins=a.all))));return
    if a.action=='receive':receive(c,json.loads(gzip.decompress(sys.stdin.buffer.read())),source=a.source);return
    stopped=False
    def stop(*unused):
        nonlocal stopped
        stopped=True
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    while not stopped:
        try:
            if c.get('hub'):
                transport(c,'receive',export(c));receive(c,transport(c,'export'))
            if a.action=='once':return
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as e:
            if a.action=='once':raise
            print('sync: '+str(e),flush=True)
        end=time.monotonic()+c.get('interval',3)
        while not stopped and time.monotonic()<end:time.sleep(.1)

if __name__=='__main__':main()
