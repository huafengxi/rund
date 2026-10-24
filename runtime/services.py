"""Checkout-backed Service declarations and hostname-selected common configuration."""
import os
import re
from pathlib import Path
import socket
import subprocess
from .config import discover
from .store import read,write,require,digest


def resolve_config(prefix):
    root=prefix/'config';hosts={};nodes=set();hub=None
    for line in (root/'host-id').read_text().splitlines():
        line=line.split('#',1)[0].strip()
        if not line:continue
        host,node=line.split();host=host.lower()
        from .config import name
        name(node)
        require(host not in hosts,'duplicate hostname: '+host)
        require(node not in nodes,'duplicate node: '+node)
        hosts[host]=node;nodes.add(node)
        if hub is None:hub=node
    require(hub is not None,'host-id must contain a hub')
    hostname=socket.gethostname().lower()
    require(hostname in hosts,'hostname not registered: '+hostname)
    home=Path.home();runtime=Path(__file__).resolve().parents[1]
    bins=[prefix/'local/tools/bin',home/'miniconda3/bin',home/'.local/node/bin',home/'.local/bin',home/'bin']
    nvm=home/'.nvm/versions/node'
    if nvm.exists():
        versions=sorted((p for p in nvm.iterdir() if re.fullmatch(r'v[0-9]+(?:\.[0-9]+)*',p.name)),
                        key=lambda p:tuple(int(x) for x in p.name[1:].split('.')),reverse=True)
        bins.extend(p/'bin' for p in versions)
    bins.extend(Path(p) for p in ('/opt/homebrew/bin','/opt/homebrew/sbin','/usr/local/bin','/usr/bin','/bin','/usr/sbin','/sbin'))
    return dict(node=hosts[hostname],hub=hub,nodes=sorted(nodes),home=str(home),root=str(prefix),
                sharedRoot=str(prefix/'shared'),privateRoot=str(prefix/'local'),
                configRoot=str(root),servicesRoot=str(root/'objects/services'),runtimeSource=str(runtime),
                python=str(home/'miniconda3/bin/python3'),path=os.pathsep.join(map(str,bins)),interval=.2,
                runtimeRevision=subprocess.check_output(['git','-C',str(runtime),'rev-parse','HEAD'],text=True).strip())


def declarations(c):
    root=c.get('servicesRoot')
    if not root:return {},[]
    found,errors=discover(root)
    for tid,(p,d) in list(found.items()):
        if d['kind']!='Service' or 'task' in d:
            errors.append(str(p)+': direct Service execution fields required');del found[tid]
    return found,errors


def state_path(c,identifier):return Path(c['sharedRoot'])/'services'/identifier/'state.json'


def intent_path(c,identifier):return Path(c['privateRoot'])/'services'/identifier/'intent.json'


def wanted(c,d):
    p=intent_path(c,d['id'])
    if p.exists():
        v=read(p)
        if v['declarationDigest']==digest(d):return v['desired']
    return d['desired']


def set_intent(c,d,value):
    require(d['node']==c['node'],'Service intent must be written on its execution node')
    require(value in ('online','offline'),'Service desired must be online/offline')
    write(intent_path(c,d['id']),dict(declarationDigest=digest(d),desired=value))
    return dict(d,desired=value)


def publish(c,d):
    require(d['kind']=='Service','Service declaration required')
    from .config import publish as put
    require('servicesRoot' in c,'configuration checkout required for Service publication')
    return put(c['servicesRoot'],d)


def objects(c):
    """Combine current checkout declarations with immutable shared Task snapshots."""
    shared,errors=discover(c['sharedRoot'],exclude=('services',))
    result={i:(p,d) for i,(p,d) in shared.items() if d['kind']=='Task'}
    if c.get('configRoot'):
        source,issues=discover(Path(c['configRoot'])/'objects');errors.extend(issues)
        for i,(source_path,d) in source.items():
            if i in result:
                require(d==result[i][1],'Task execution is immutable: '+i)
                continue
            folder='services' if d['kind']=='Service' else 'tasks'
            result[i]=(Path(c['sharedRoot'])/folder/i/'declaration.json',d)
    require(not errors,'; '.join(errors))
    return result


def get(c,identifier):
    found=objects(c);require(identifier in found,'object not found: '+identifier)
    p,d=found[identifier];s=p.with_name('state.json')
    return d,read(s) if s.exists() else None
