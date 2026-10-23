"""Service lifecycle, publication and external bootstrap through one CLI."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from .config import node_config,FINAL
from .store import read,require,lock,directory
from .protocol import objects,get,desired,publish
from .process import identity,same,alive,expand


def executor_alive(c,label):
    p=Path(c['privateRoot'])/(label+'.json')
    if not p.exists():return False
    record=read(p);actual=identity(record['pid'])
    return same(actual,record) and alive(actual)


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
    for identifier,(p,d) in found.items():
        if d['node']!=c['node'] or d['kind']!='Service':continue
        external=identifier==c.get('bootstrapService');label='bootstrap' if external else 'runtime'
        running=executor_alive(c,label)
        s=read(p.with_name('state.json')) if p.with_name('state.json').exists() else None
        ts=None;td=None
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
        version=td['service']['version'] if ts else None
        drift=actual!=d['desired'] or actual=='online' and version!=d['version']
        rows.append(dict(id=identifier,name=d.get('name',identifier),version=d['version'],run_version=version,desired=d['desired'],actual=actual,state='DRIFT' if drift else 'ok',pid=pid,task=s.get('currentTask') if s else None,note=note))
    for label in ('runtime','bootstrap'):
        health=Path(c['privateRoot'])/(label+'.json')
        if health.exists():diagnostics.extend(label+': '+error for error in read(health).get('diagnostics',[]))
    return dict(node=c['node'],total=len(rows),drift=sum(r['state']!='ok' for r in rows),services=rows,diagnostics=diagnostics,blockedTargets=[r['id'] for r in rows if r['actual']=='unknown'])


def launcher(c,path):
    """Start the external foreground supervisor; workload spawn stays in Engine."""
    private=directory(c['privateRoot']);fd=lock(private/'locks/launch.lock')
    try:
        if executor_alive(c,'bootstrap'):return
        with (private/'bootstrap.log').open('ab',buffering=0) as log:
            child=subprocess.Popen([c['python'],'-B',str(Path(__file__).resolve().parents[1]/'runctl.py'),'--config',str(path),'run',c['bootstrapService']],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,env=dict(HOME=c['home'],PATH=c['path'],LC_ALL='C.UTF-8',PYTHONDONTWRITEBYTECODE='1'))
        end=time.monotonic()+10
        while time.monotonic()<end:
            require(child.poll() is None,'bootstrap exited during startup')
            if executor_alive(c,'bootstrap'):return
            time.sleep(.05)
        raise ValueError('bootstrap startup pending')
    finally:os.close(fd)


def refresh(c):
    if c.get('syncConfig'):
        subprocess.run([c['python'],'-B',str(Path(__file__).resolve().parents[1]/'tools/sync.py'),'--config',c['syncConfig'],'once'],check=True,timeout=100)


def set_desired(c,identifier,value,mode=None):
    d,_=get(c['sharedRoot'],identifier);origin=c['publishers'][d['publisher']]
    if origin==c['node']:
        document=desired(c['sharedRoot'],identifier,value,writer=c['node'],publishers=c['publishers'],mode=mode)
        refresh(c)
        return document
    import shlex
    argv=['desired',identifier,value]+(['--mode',mode] if mode else [])
    result=subprocess.run(['ssh','-o','BatchMode=yes',origin,'exec "$HOME/.rund/bin/runctl" '+shlex.join(argv)],capture_output=True,text=True,timeout=130)
    require(result.returncode==0,'publisher unavailable: '+result.stderr[-1000:])
    return json.loads(result.stdout)


def select(c,selector):
    found=[d for _,d in objects(c['sharedRoot']).values() if d['kind']=='Service' and d['node']==c['node'] and selector in (d['id'],d.get('name'))]
    require(len(found)==1,'local service selector not unique');return found[0]


def workloads_closed(c):
    for p,d in objects(c['sharedRoot']).values():
        if d['node']!=c['node'] or d['kind']!='Task' or d.get('service',{}).get('id')==c.get('bootstrapService'):continue
        sp=p.with_name('state.json')
        if sp.exists():require(read(sp)['phase'] in FINAL,'stop/resolve all Tasks before rund: '+d['id'])


def service(c,selector,action,config_path=None):
    d=select(c,selector);external=d['id']==c.get('bootstrapService')
    if external and action=='stop':workloads_closed(c)
    wanted='online' if action=='start' else 'offline'
    expected=set_desired(c,d['id'],wanted);refresh(c)
    if external and action=='start':launcher(c,config_path)
    end=time.monotonic()+120
    while time.monotonic()<end:
        if external and action=='start' and not executor_alive(c,'bootstrap'):launcher(c,config_path)
        row=next(s for s in status(c)['services'] if s['id']==d['id'])
        observed,state=get(c['sharedRoot'],d['id'])
        closed=not state or not state.get('currentTask') or (get(c['sharedRoot'],state['currentTask'])[1] or {}).get('phase') in FINAL
        received=observed['revision']>=expected['revision'] and observed['desired']==wanted
        supervisor_closed=not external or action!='stop' or not executor_alive(c,'bootstrap')
        if received and row['actual']==wanted and row['state']=='ok' and (action=='start' or closed) and supervisor_closed:
            if action=='stop':refresh(c)
            return row
        time.sleep(.3)
    raise ValueError('desired published; execution remains pending')


def main(argv=None,default_config=None):
    p=argparse.ArgumentParser(prog='runctl',description='Single-writer Service and Task lifecycle.')
    p.add_argument('--config',type=Path,default=default_config or Path.home()/'.rund/local/node.json')
    sub=p.add_subparsers(dest='action',required=True)
    st=sub.add_parser('status');st.add_argument('selector',nargs='?')
    for name in ('start','stop','run'):
        cmd=sub.add_parser(name);cmd.add_argument('selector')
    sub.add_parser('sync')
    pub=sub.add_parser('publish');pub.add_argument('--input',required=True)
    des=sub.add_parser('desired');des.add_argument('id');des.add_argument('value');des.add_argument('--mode',choices=('terminate','kill'))
    can=sub.add_parser('cancel');can.add_argument('id');can.add_argument('--mode',choices=('terminate','kill'),default='terminate')
    a=p.parse_args(argv)
    try:
        c=node_config(a.config)
        if a.action=='run':
            d=select(c,a.selector);require(d['id']==c.get('bootstrapService'),'foreground run requires the configured bootstrap Service')
            from .engine import Engine
            engine=Engine(c,external=True)
            try:
                set_desired(c,d['id'],'online');refresh(c);engine.run()
            finally:engine.close()
        elif a.action=='sync':refresh(c)
        elif a.action=='status':
            result=status(c)
            if a.selector:
                rows=[s for s in result['services'] if a.selector in (s['name'],s['id'])]
                if rows:require(len(rows)==1,'service selector not unique');result=rows[0]
                else:
                    d,state=get(c['sharedRoot'],a.selector);require(d['kind']=='Task','selector not found');result=dict(declaration=d,state=state)
            print(json.dumps(result))
        elif a.action in ('start','stop'):
            if a.action=='start' and not executor_alive(c,'runtime'):refresh(c)
            print(json.dumps(service(c,a.selector,a.action,a.config)))
        elif a.action=='publish':
            document=json.load(sys.stdin) if a.input=='-' else read(a.input)
            print(publish(c['sharedRoot'],document,writer=c['node'],publishers=c['publishers']))
        else:
            if a.action=='cancel':
                document,_=get(c['sharedRoot'],a.id);require(document['kind']=='Task','cancel requires a Task')
            print(json.dumps(set_desired(c,a.id,'cancel' if a.action=='cancel' else a.value,a.mode)))
        return 0
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError) as e:
        print('runctl: '+str(e),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())
