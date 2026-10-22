"""Installed node entry: Owner lifecycle and single-writer object operations."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from .config import node_config,FINAL
from .store import read,write,require,lock,directory
from .protocol import objects,get,desired,publish
from .process import identity,same,alive,expand

def owner_alive(c):
    p=Path(c['privateRoot'])/'owner.json'
    return p.exists() and alive(identity(read(p)['pid'])) and same(identity(read(p)['pid']),read(p))

def probe(d,c):
    p=d.get('probe')
    if not p:return True,''
    try:
        if 'http' in p:
            import urllib.request,urllib.error
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(p['http'],timeout=p.get('timeout',2)):pass
            except urllib.error.HTTPError:pass
            return True,p['http']
        env=dict(HOME=c['home'],PATH=c['path'])
        r=subprocess.run([expand(a,c,d['id']) for a in p['cmd']],env=env,cwd=c['home'],capture_output=True,text=True,timeout=p.get('timeout',3))
        return r.returncode==0 and p.get('expect','') in r.stdout,'command probe'
    except (OSError,ValueError,subprocess.SubprocessError):return False,'probe unavailable'

def status(c):
    found=objects(c['sharedRoot']);rows=[];diagnostics=[]
    running=owner_alive(c)
    rows.append(dict(name='service-sync',version=c['version'],run_version=c['version'] if running else None,desired='online',actual='online' if running else 'offline',state='ok' if running else 'DRIFT',pid=read(Path(c['privateRoot'])/'owner.json')['pid'] if running else None))
    for id,(p,d) in found.items():
        if d['node']!=c['node'] or d['kind']!='Service':continue
        s=read(p.with_name('state.json')) if p.with_name('state.json').exists() else None
        ts=None
        if s and s.get('currentTask') in found:
            tp,td=found[s['currentTask']];ts=read(tp.with_name('state.json')) if tp.with_name('state.json').exists() else None
        actual='offline';pid=None
        if ts and ts.get('process'):
            pid=ts['process']['pid'];a=identity(pid)
            if same(a,ts['process']) and alive(a):actual='online' if running else 'unknown'
            elif ts['phase']=='unknown':actual='unknown'
        if s and s['phase']=='blocked':actual='unknown'
        note=s.get('reason') if s else 'not accepted'
        if actual=='online':
            ok,note=probe(d,c)
            if not ok:actual='offline'
        version=found[s['currentTask']][1]['service']['version'] if ts else None
        drift=actual!=d['desired'] or actual=='online' and version!=d['version']
        rows.append(dict(id=id,name=d.get('name',id),version=d['version'],run_version=version,desired=d['desired'],actual=actual,state='DRIFT' if drift else 'ok',pid=pid,task=s.get('currentTask') if s else None,note=note))
    health=Path(c['privateRoot'])/'health.json'
    if health.exists():diagnostics=read(health)['diagnostics']
    return dict(node=c['node'],total=len(rows),drift=sum(r['state']!='ok' for r in rows),services=rows,diagnostics=diagnostics,blockedTargets=[r.get('id') for r in rows if r['actual']=='unknown'])

def launcher(c,path,action):
    private=directory(c['privateRoot']);fd=lock(private/'locks/bootstrap.lock')
    try:
        if action=='start':
            if owner_alive(c):return
            logfile=directory(private/'logs')/'owner.log'
            with logfile.open('ab',buffering=0) as f:
                child=subprocess.Popen([c['python'],'-B',str(Path(__file__).resolve().parents[1]/'rund.py'),'--config',str(path),'owner'],stdin=subprocess.DEVNULL,stdout=f,stderr=f,start_new_session=True,env=dict(HOME=c['home'],PATH=c['path'],LC_ALL='C.UTF-8',PYTHONDONTWRITEBYTECODE='1'))
            end=time.monotonic()+10
            while time.monotonic()<end:
                require(child.poll() is None,'Owner exited during startup')
                if owner_alive(c):return
                time.sleep(.05)
            raise ValueError('Owner startup pending')
        if not owner_alive(c):return
        for p,d in objects(c['sharedRoot']).values():
            if d['kind']=='Task' and d['node']==c['node'] and p.with_name('state.json').exists():
                s=read(p.with_name('state.json'));require(s['phase'] in FINAL,'stop/resolve all Tasks before Owner: '+d['id'])
        record=read(private/'owner.json');require(same(identity(record['pid']),record),'Owner identity changed');os.kill(record['pid'],signal.SIGTERM)
        end=time.monotonic()+10
        while owner_alive(c) and time.monotonic()<end:time.sleep(.05)
        require(not owner_alive(c),'Owner stop pending')
    finally:os.close(fd)

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--config',required=True,type=Path)
    s=p.add_subparsers(dest='action',required=True)
    for k in ('owner','status','start','stop'):s.add_parser(k)
    pub=s.add_parser('publish');pub.add_argument('--input',required=True,type=Path)
    des=s.add_parser('desired');des.add_argument('id');des.add_argument('value');des.add_argument('--mode',choices=('terminate','kill'))
    a=p.parse_args(argv)
    try:
        c=node_config(a.config)
        if a.action=='owner':
            from .owner import Owner
            o=Owner(c)
            try:o.run()
            finally:o.close()
        elif a.action=='status':print(json.dumps(status(c)))
        elif a.action in ('start','stop'):launcher(c,a.config,a.action)
        elif a.action=='publish':print(publish(c['sharedRoot'],read(a.input),writer=c['node'],publishers=c['publishers']))
        else:print(json.dumps(desired(c['sharedRoot'],a.id,a.value,writer=c['node'],publishers=c['publishers'],mode=a.mode)))
        return 0
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as e:print('serviced: '+str(e),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())
