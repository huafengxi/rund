"""Checkout-backed Service declarations and hostname-selected common configuration."""
import os
from pathlib import Path
import socket
import sys
import subprocess
from .config import discover,changed,EXEC_KEYS
from .store import read,write,require,digest


def resolve_config(path,c):
    root=path.parent;hosts={}
    for line in (root/c['hosts']).read_text().splitlines():
        line=line.split('#',1)[0].strip()
        if not line:continue
        host,node=line.split();host=host.lower()
        require(host not in hosts,'duplicate hostname: '+host);hosts[host]=node
    hostname=socket.gethostname().lower()
    require(hostname in hosts,'hostname not registered: '+hostname)
    home=str(Path.home());prefix=str(root.parent)
    mapping={'home':home,'root':prefix,'config':str(root),'node':hosts[hostname]}
    def expand(v):
        if isinstance(v,str):
            for k,x in mapping.items():v=v.replace('${'+k+'}',x)
            return v
        if isinstance(v,dict):return {k:expand(x) for k,x in v.items()}
        if isinstance(v,list):return [expand(x) for x in v]
        return v
    result=expand({k:v for k,v in c.items() if k not in ('format','hosts')})
    result.update(node=hosts[hostname],home=home,sharedRoot=prefix+'/shared',privateRoot=prefix+'/local',
                  configFile=str(path),configRoot=str(root),servicesRoot=str(root/'objects/services'),
                  runtimeSource=str(Path(__file__).resolve().parents[1]))
    result['runtimeRevision']=subprocess.check_output(['git','-C',result['runtimeSource'],'rev-parse','HEAD'],text=True).strip()
    result.setdefault('python',sys.executable)
    result.setdefault('path',os.environ.get('PATH',os.defpath))
    return result


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
    return put(c['servicesRoot'],d,writer=c['node'],publishers=c['publishers'],
               path=Path(c['servicesRoot'])/d['id']/'declaration.json')


def objects(c):
    """Task protocol plus checkout inputs; shared Service declarations are history."""
    shared,errors=discover(c['sharedRoot'])
    result={i:(p,d) for i,(p,d) in shared.items() if d['kind']=='Task'}
    if c.get('configRoot'):
        source,issues=discover(Path(c['configRoot'])/'objects');errors.extend(issues)
        for i,(_,d) in source.items():
            if d['kind']=='Service':require('task' not in d,'direct Service execution fields required')
            if i in result:require(result[i][1]==d,'checkout/protocol declaration conflict: '+i)
            folder='services' if d['kind']=='Service' else 'tasks'
            result[i]=(Path(c['sharedRoot'])/folder/i/'declaration.json',d)
    require(not errors,'; '.join(errors))
    return result


def get(c,identifier):
    found=objects(c);require(identifier in found,'object not found: '+identifier)
    p,d=found[identifier];s=p.with_name('state.json')
    return d,read(s) if s.exists() else None
