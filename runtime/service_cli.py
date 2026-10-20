"""Service CLI and bootstrap for the single explicit local owner.

Bootstrap manages only the coordinator identity. All managed workloads execute
through RegistryOwner's one Runtime. External daemons retain their own manager.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid
from .contract import require
from .prepare import canonical,digest,prepare,init_root,directory
from .kernel import proc,same_identity,publish,private_fd,read_json
from .native import boot_id
from .registry import node_config,services_here,declaration
from experimental.file_owner import protocol as C


def config_path(explicit=None):
    if explicit:return Path(explicit).expanduser().resolve()
    configured=os.environ.get('SERVICED_RUNTIME_CONFIG')
    if configured:return Path(configured).expanduser().resolve()
    workspace=Path(os.environ.get('PLATFORM_WORKSPACE') or Path(__file__).resolve().parents[2]).resolve()
    host=socket.gethostname();node=host
    mapping=workspace/'env/host-id'
    if mapping.exists():
        for line in mapping.read_text().splitlines():
            bits=line.split()
            if len(bits)>=2 and bits[0]==host:node=bits[1];break
    return workspace/'env/rund'/f'{node}.json'


def owner_record(c):
    path=Path(c['root'])/'bootstrap.json'
    return read_json(path) if path.exists() else None


def owner_alive(c):
    record=owner_record(c)
    if not record:return False
    actual=proc(record['pid'])
    return record.get('bootId')==boot_id() and same_identity(actual,record) and actual['state'] not in ('Z','X')


def bootstrap_version(c):
    name=c.get('bootstrapService','service-sync')
    for svc in services_here(c):
        if svc['name']==name:return svc['version']
    raise ValueError('bootstrap service is not locally declared')


def start_owner(c,path):
    if owner_alive(c):return owner_record(c)
    root=init_root(c['root']);lock=private_fd(root/'bootstrap.lock')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if owner_alive(c):return owner_record(c)
        alias=c.get('bootstrapSource','runtime')
        require(alias in c['sources'],'bootstrap source alias unavailable')
        selected={alias};pending=[alias]
        while pending:
            for dependency in c['sources'][pending.pop()]['requires']:
                require(dependency in c['sources'],'unknown bootstrap source dependency')
                if dependency not in selected:selected.add(dependency);pending.append(dependency)
        sources=[c['sources'][key] for key in sorted(selected)]
        d=dict(apiVersion='rund/v1alpha1',kind='Service',metadata=dict(id='bootstrap-source',version=1),
               spec=dict(node=c['node'],sources=sources,prepare=[],toolchain={'python':c['python']},
                         argv=[c['python'],'${code.'+alias+'}/rund.py'],cwd='${work}',env={},context={'path':c['path']},credentialRefs=[],desired='offline',
                         restart=dict(policy='manual',initialSeconds=1,maxSeconds=60)))
        release=prepare(d,root/'bootstrap-source')
        code=Path(release['code'])/alias/'rund.py'
        require(code.is_file(),'pinned runtime source has no rund.py')
        logs=directory(root/'bootstrap-logs');env={key:os.environ[key] for key in c.get('inheritEnv',[]) if key in os.environ}
        env.update(HOME=c['home'],PATH=c['path'],LC_ALL='C.UTF-8',PYTHONDONTWRITEBYTECODE='1',PLATFORM_WORKSPACE=c['workspace'])
        argv=[c['python'],'-B',str(code),'--config',str(path),'owner']
        with (logs/'owner.log').open('ab',buffering=0) as log:
            child=subprocess.Popen(argv,cwd=c['workspace'],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        identity=proc(child.pid);require(identity is not None,'bootstrap identity unavailable')
        record=dict(identity,bootId=boot_id(),version=bootstrap_version(c),sourceCommits={v['alias']:v['revision'] for v in sources},argv=argv,startedAt=time.time())
        publish(root/'bootstrap.json',record)
        end=time.monotonic()+15
        while time.monotonic()<end:
            rc=child.poll();require(rc is None,'owner exited during startup (exit '+str(rc)+')')
            snapshot=Path(c['protocol'])/'snapshot.json'
            if snapshot.exists():
                observed=C.snapshot(c['protocol'])
                if observed['owner']['pid']==child.pid and observed['owner']['procStart']==identity['procStart']:return record
            time.sleep(.05)
        raise ValueError('owner startup not confirmed; retained bootstrap identity')
    finally:os.close(lock)


def stop_owner(c):
    root=init_root(c['root']);lock=private_fd(root/'bootstrap.lock')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        record=owner_record(c)
        if not record or not owner_alive(c):return {'stopped':True,'alreadyAbsent':True}
        # This one external bootstrap identity is not a managed workload group.
        # Closing the coordinator deliberately leaves separately-grouped loads.
        for sig,seconds in ((signal.SIGTERM,35),(signal.SIGKILL,3)):
            if not owner_alive(c):break
            actual=proc(record['pid']);require(same_identity(actual,record),'bootstrap identity changed')
            os.kill(record['pid'],sig);end=time.monotonic()+seconds
            while time.monotonic()<end and owner_alive(c):time.sleep(.05)
        require(not owner_alive(c),'owner closure unconfirmed')
        return {'stopped':True,'identity':record,'waitObserved':False}
    finally:os.close(lock)


def exchange(c,action,**fields):
    require(owner_alive(c),'runtime owner unavailable')
    request=dict(apiVersion=C.API,requestId='cli-'+uuid.uuid4().hex,node=c['node'],action=action,**fields)
    # Retain the exact CLI intent before publishing. On timeout, query this rid;
    # never turn a pending request into an automatic replacement action.
    journal=directory(Path(c['root'])/'cli');publish(journal/(request['requestId']+'.json'),request,final=True)
    C.send(c['protocol'],request);end=time.monotonic()+45
    while time.monotonic()<end:
        record=C.receipt(c['protocol'],request['requestId'])
        if record and record['status']=='done':return record['result']
        time.sleep(.05)
    raise ValueError('exact request still pending: '+request['requestId'])


def service_head(c,name):
    snapshot=C.snapshot(c['protocol'])
    return next((h for h in snapshot['services'] if h['id']=='svc-'+name),None)


def act(c,action,name,path):
    if name==c.get('bootstrapService','service-sync'):
        if action=='start':return start_owner(c,path)
        if action=='stop':return stop_owner(c)
    require('registryFile' not in c, 'worker is read-only: publish desired state on the controller')
    require(owner_alive(c),'runtime owner unavailable')
    svc=next((s for s in services_here(c) if s['name']==name),None);require(svc is not None,'service not declared locally')
    if name in c.get('external',[]):
        # Use its official lifecycle API, never infer/kill an unrelated group.
        argv=svc.get('stop_cmd') if action=='stop' else svc['cmd']
        require(argv,'external stop command required')
        from .registry import expand
        result=subprocess.run([expand(a,c) for a in argv],cwd=c['workspace'],timeout=35)
        require(result.returncode==0,'external manager command failed');return {'external':name,'action':action}
    head=service_head(c,name)
    if action=='stop':
        if head is None or not head['generation']:return {'noop':True,'reason':'never-started'}
        return exchange(c,'stop',kind='Service',id=head['id'],attemptId='service',expectedGeneration=head['generation'])
    d=declaration(c,svc)
    if action=='start':
        if head is None or head['declaration']!=d:
            require(head is None or d['metadata']['version']>head['declaration']['metadata']['version'],
                    'changed service declaration requires a profile version bump')
            return exchange(c,'submit',declaration=d,declarationDigest=digest(canonical(d)))
        if head['hold'] and head['generation']:
            return exchange(c,'restart',kind='Service',id=head['id'],attemptId='service',expectedGeneration=head['generation'])
        return head
    raise ValueError('unsupported service action')


def rows(c):
    alive=owner_alive(c)
    snapshot=C.snapshot(c['protocol']) if Path(c['protocol'],'snapshot.json').exists() else {'services':[],'runs':[]}
    result=[]
    for svc in services_here(c):
        name=svc['name'];version=None;pid=None;actual='offline';note=''
        if name==c.get('bootstrapService','service-sync'):
            if alive:
                record=owner_record(c);actual='online';version=record['version'];pid=record['pid']
        elif name in c.get('external',[]) or svc.get('external'):
            # Existing generic probe helpers, with no legacy start/stop/reconcile.
            from .probes import online
            ok,note=online(svc,c);actual='online' if ok else 'offline'
            version=svc['version'] if ok else None
        else:
            head=next((h for h in snapshot['services'] if h['id']=='svc-'+name),None)
            run=next((r for r in snapshot['runs'] if head and r['kind']=='Service' and r['id']==head['id'] and r['generation']==head['generation']),None)
            if run:
                version=run['declaration']['metadata']['version'];pid=run.get('pid')
                observed=proc(pid) if pid else None
                if run.get('bootId')==boot_id() and same_identity(observed,run) and observed['state'] not in ('Z','X'):
                    actual='online' if alive else 'unknown'
                elif run['phase']=='cleanupUnknown':actual='unknown'
                note=run.get('reason','')
        if actual=='online' and svc.get('status') and name not in c.get('external',[]) and not svc.get('external'):
            from .probes import online
            ok,note=online(svc,c)
            if not ok:actual='offline'
        stale=actual=='online' and version!=svc['version'];drift=actual!=svc['desired'] or stale
        result.append(dict(name=name,version=svc['version'],run_version=version,desired=svc['desired'],actual=actual,state='DRIFT' if drift else 'ok',stale=stale,pid=pid,note=note))
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config')
    sub=p.add_subparsers(dest='action',required=True)
    sub.add_parser('owner');sub.add_parser('sync');sub.add_parser('trim')
    for action in ('start','stop'):
        q=sub.add_parser(action);q.add_argument('name')
    q=sub.add_parser('status');q.add_argument('name',nargs='?');q.add_argument('--machine',action='store_true')
    a=p.parse_args(argv);path=config_path(a.config)
    try:
        c=node_config(path)
        if a.action=='owner':
            from .registry import RegistryOwner
            stopped=False
            def stop(*unused):
                nonlocal stopped;stopped=True
            for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
            owner=RegistryOwner(c,path)
            try:
                # Confirm the coordinator independently of workload preparation.
                from experimental.file_owner.owner import Owner
                Owner.round(owner)
                while not stopped:owner.round();time.sleep(.05)
            finally:owner.close()
            return 0
        if a.action in ('start','stop'):
            print(json.dumps(act(c,a.action,a.name,path)));return 0
        if a.action=='sync':
            if 'registryFile' in c:
                require(owner_alive(c),'runtime owner unavailable')
                print(json.dumps(dict(readOnly=True, message='Owner consumes controller declarations periodically')))
                return 0
            # Explicit operator invocation uses the same exact file requests.
            for svc in services_here(c):
                if svc['name']==c.get('bootstrapService','service-sync'):continue
                act(c,'start' if svc['desired']=='online' else 'stop',svc['name'],path)
            return 0
        if a.action=='trim':
            for log in (Path(c['workspace'])/'run/logs').glob('*.log'):
                with log.open('r+') as f:f.truncate()
            return 0
        values=rows(c)
        if a.name:
            row=next((r for r in values if r['name']==a.name),None);require(row is not None,'service not declared locally')
            print(json.dumps(row));return 0 if row['actual']=='online' else 1
        snapshot=C.snapshot(c['protocol']) if Path(c['protocol'],'snapshot.json').exists() else {}
        diagnostics=list(snapshot.get('diagnostics',[]))
        if 'registryFile' in c:
            from .desired import selected
            _,error=selected(c)
            if error and error not in diagnostics:diagnostics.append(error)
        result=dict(host=socket.gethostname(),total=len(values),drift=sum(r['state']!='ok' for r in values),services=values,
                    diagnostics=diagnostics,blockedTargets=snapshot.get('blockedTargets',[]),registry=snapshot.get('registry'))
        if a.machine:print(json.dumps(result))
        else:
            print('SERVICE          VERSION DESIRED  ACTUAL   STATE')
            for row in values:print(f"{row['name']:<16} {row['version']:<7} {row['desired']:<8} {row['actual']:<8} {row['state']}")
            print(f"\n{result['total']} services, {result['drift']} drift")
            for diagnostic in diagnostics:print('diagnostic: '+diagnostic)
            if result['blockedTargets']:print('blocked targets: '+str(result['blockedTargets']))
        return 0
    except (OSError,ValueError,KeyError,TypeError) as error:
        print('rund: '+str(error),file=sys.stderr);return 2
