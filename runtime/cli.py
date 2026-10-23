"""Direct node startup and checkout-backed Service lifecycle."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from .config import node_config,FINAL
from .store import read,require,lock,directory
from .protocol import desired,publish
from .process import identity,same,alive,expand
from . import services


def executor_alive(c,label='runtime'):
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
    found,errors=services.declarations(c);rows=[];diagnostics=list(errors)
    running=executor_alive(c)
    for identifier,(_,d) in found.items():
        if d['node']!=c['node']:continue
        p=services.state_path(c,identifier);s=read(p) if p.exists() else None
        actual='offline';pid=None
        if s and s.get('process'):
            pid=s['process']['pid'];a=identity(pid)
            if same(a,s['process']) and alive(a):actual='online' if running else 'unknown'
            elif s['phase']=='unknown':actual='unknown'
        if s and s['phase'] in ('unknown','blocked'):actual='unknown'
        note=s.get('reason') if s else 'not accepted'
        if actual=='online':
            ok,note=probe(d,c)
            if not ok:actual='offline'
        version=s.get('runVersion') if s else None;wanted=services.wanted(c,d)
        drift=actual!=wanted or actual=='online' and version!=d['version']
        rows.append(dict(id=identifier,name=d.get('name',identifier),version=d['version'],run_version=version,desired=wanted,
                         actual=actual,state='DRIFT' if drift else 'ok',pid=pid,run=s.get('runId') if s else None,note=note))
    health=Path(c['privateRoot'])/'runtime.json';record=read(health) if health.exists() else {}
    diagnostics.extend(record.get('diagnostics',[]))
    return dict(node=c['node'],rund=dict(actual='online' if running else 'offline',pid=record.get('pid'),
                startedAt=record.get('startedAt'),status=record.get('status'),revision=record.get('runtimeRevision')),total=len(rows),drift=sum(r['state']!='ok' for r in rows),
                services=rows,diagnostics=diagnostics,blockedTargets=[r['id'] for r in rows if r['actual']=='unknown'])


def launcher(c,path):
    private=directory(c['privateRoot']);fd=lock(private/'locks/launch.lock')
    try:
        if executor_alive(c):return
        with (private/'rund.log').open('ab',buffering=0) as log:
            child=subprocess.Popen([c['python'],'-B',str(Path(__file__).resolve().parents[1]/'rund.py'),'--config',str(path)],
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,
                env=dict(HOME=c['home'],PATH=c['path'],LC_ALL='C.UTF-8',PYTHONDONTWRITEBYTECODE='1'))
        end=time.monotonic()+10
        while time.monotonic()<end:
            require(child.poll() is None,'rund exited during startup')
            if executor_alive(c):return
            time.sleep(.05)
        raise ValueError('rund startup pending')
    finally:os.close(fd)


def refresh(c):
    if c.get('syncConfig'):
        subprocess.run([c['python'],'-B',str(Path(__file__).resolve().parents[1]/'tools/sync.py'),'--config',c['syncConfig'],'once'],check=True,timeout=100)


def select(c,selector):
    found,errors=services.declarations(c);require(not errors,'; '.join(errors))
    matches=[d for _,d in found.values() if d['node']==c['node'] and selector in (d['id'],d.get('name'))]
    require(len(matches)==1,'local service selector not unique');return matches[0]


def set_desired(c,identifier,value,mode=None):
    d,_=services.get(c,identifier)
    if d['kind']=='Service' and d['node']==c['node']:return services.set_intent(c,d,value)
    origin=d['node'] if d['kind']=='Service' else c['publishers'][d['publisher']]
    if origin==c['node']:
        document=desired(c['sharedRoot'],identifier,value,writer=c['node'],publishers=c['publishers'],mode=mode)
        refresh(c);return document
    import shlex
    argv=['desired',identifier,value]+(['--mode',mode] if mode else [])
    result=subprocess.run(['ssh','-o','BatchMode=yes',origin,'exec "$HOME/.rund/rund/runctl" '+shlex.join(argv)],capture_output=True,text=True,timeout=130)
    require(result.returncode==0,'publisher unavailable: '+result.stderr[-1000:])
    return json.loads(result.stdout)


def workloads_closed(c):
    for p,d in services.objects(c).values():
        if d['node']!=c['node'] or d['kind']!='Task' or d.get('service'):continue
        sp=p.with_name('state.json')
        if sp.exists():require(read(sp)['phase'] in FINAL,'stop/resolve all Tasks before rund: '+d['id'])


def node_action(c,path,action):
    if action=='start':launcher(c,path)
    elif action=='stop' and executor_alive(c):
        workloads_closed(c)
        record=read(Path(c['privateRoot'])/'runtime.json')
        require(same(identity(record['pid']),record),'rund process identity changed')
        os.kill(record['pid'],signal.SIGTERM)
        end=time.monotonic()+120
        while executor_alive(c):
            require(time.monotonic()<end,'rund shutdown pending');time.sleep(.1)
    return status(c)['rund']


def service(c,selector,action,config_path=None):
    d=select(c,selector);wanted='online' if action=='start' else 'offline'
    services.set_intent(c,d,wanted)
    require(executor_alive(c),'rund is offline; start the node first')
    end=time.monotonic()+120
    while time.monotonic()<end:
        row=next(s for s in status(c)['services'] if s['id']==d['id'])
        state=services.get(c,d['id'])[1]
        if row['actual']==wanted and row['state']=='ok' and (action=='start' or state and state['phase']=='stopped'):return row
        time.sleep(.2)
    raise ValueError('Service intent recorded; execution remains pending')


def main(argv=None,default_config=None):
    p=argparse.ArgumentParser(prog='runctl',description='Node runtime, Service and Task control.')
    p.add_argument('--config',type=Path,default=default_config or Path.home()/'.rund/config/rund.json')
    sub=p.add_subparsers(dest='action',required=True)
    st=sub.add_parser('status');st.add_argument('selector',nargs='?')
    for name in ('start','stop'):
        cmd=sub.add_parser(name);cmd.add_argument('selector',nargs='?')
    sub.add_parser('run');sub.add_parser('sync')
    pub=sub.add_parser('publish');pub.add_argument('--input',required=True)
    des=sub.add_parser('desired');des.add_argument('id');des.add_argument('value');des.add_argument('--mode',choices=('terminate','kill'))
    can=sub.add_parser('cancel');can.add_argument('id');can.add_argument('--mode',choices=('terminate','kill'),default='terminate')
    a=p.parse_args(argv)
    try:
        c=node_config(a.config)
        if a.action=='run':
            from .engine import Engine
            engine=Engine(c)
            try:engine.run()
            finally:engine.close()
        elif a.action=='sync':refresh(c)
        elif a.action=='status':
            result=status(c)
            if a.selector:
                rows=[s for s in result['services'] if a.selector in (s['name'],s['id'])]
                if rows:require(len(rows)==1,'service selector not unique');result=rows[0]
                else:
                    d,state=services.get(c,a.selector);require(d['kind']=='Task','selector not found');result=dict(declaration=d,state=state)
            print(json.dumps(result))
        elif a.action in ('start','stop'):
            print(json.dumps(service(c,a.selector,a.action,a.config) if a.selector else node_action(c,a.config,a.action)))
        elif a.action=='publish':
            document=json.load(sys.stdin) if a.input=='-' else read(a.input)
            result=services.publish(c,document) if document['kind']=='Service' else publish(c['sharedRoot'],document,writer=c['node'],publishers=c['publishers'])
            print(result)
        else:
            if a.action=='cancel':
                document,_=services.get(c,a.id);require(document['kind']=='Task','cancel requires a Task')
            print(json.dumps(set_desired(c,a.id,'cancel' if a.action=='cancel' else a.value,a.mode)))
        return 0
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError) as e:
        print('runctl: '+str(e),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())
