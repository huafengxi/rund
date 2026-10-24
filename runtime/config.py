"""Object declaration contract. Paths organize files; kind and id define objects."""
import os
import re
from pathlib import Path
from .store import read,require

FINAL=frozenset(('succeeded','failed','canceled'))
EXEC_KEYS=frozenset(('argv','needs','cwd','env','resources'))
COMMON=frozenset(('kind','id','name','node'))

def name(v):require(isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}',v),'invalid object identity')
def text(v):require(isinstance(v,str) and v and '\0' not in v,'nonempty text required')
def execution(e):
    require(isinstance(e,dict) and set(e)<=EXEC_KEYS,'unknown execution fields')
    require(isinstance(e.get('argv'),list) and e['argv'],'nonempty argv required')
    for x in e['argv']:text(x)
    require(isinstance(e.get('needs',[]),list),'needs must be file paths')
    for p in e.get('needs',[]):text(p)
    if 'resources' in e:
        require(isinstance(e['resources'],list),'resources must be an array')
        for resource in e['resources']:text(resource)
        require(len(set(e['resources']))==len(e['resources']),'duplicate resource')
    if 'cwd' in e:text(e['cwd'])
    require(isinstance(e.get('env',{}),dict),'env object required')
    for k,v in e.get('env',{}).items():
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',k),'invalid env name');require(isinstance(v,str) and '\0' not in v,'invalid env value')

def validate(d):
    require(isinstance(d,dict),'declaration object required')
    for k in ('kind','id','node'):require(k in d,'missing '+k)
    for k in ('id','node'):name(d[k])
    if 'name' in d:text(d['name'])
    if d['kind']=='Task':
        require(set(d)<=COMMON|EXEC_KEYS|{'desired','service'},'unknown Task fields')
        require(d.get('desired')=='run','Task desired must be run; cancel through cancel.json')
        execution({k:v for k,v in d.items() if k in EXEC_KEYS})
        if 'service' in d:
            s=d['service'];require(isinstance(s,dict) and set(s)=={'id','version','declarationDigest'},'invalid Service reference');name(s['id'])
    elif d['kind']=='Service':
        require(set(d)<=COMMON|EXEC_KEYS|{'repos','desired','restart','probe'},'unknown Service fields')
        require(isinstance(d.get('repos',{}),dict),'repos must map repository names to tags')
        for repo,tag in d.get('repos',{}).items():
            name(repo);text(tag)
            require(not tag.startswith('-') and not any(x in tag for x in ('..','~','^',':','?','*','[','\\','@{')) and not any(c.isspace() for c in tag),'invalid Git tag')
        require(d.get('desired') in ('online','offline'),'Service desired must be online/offline')
        require(d.get('restart') in ('manual','on-failure','always'),'invalid restart policy')
        execution({k:v for k,v in d.items() if k in EXEC_KEYS})
        if 'probe' in d:
            p=d['probe'];require(isinstance(p,dict),'probe object required')
            require(('http' in p)^('cmd' in p),'one probe type required')
            require(set(p)<=({'http','timeout'} if 'http' in p else {'cmd','expect','timeout'}),'unknown probe fields')
            if 'http' in p:text(p['http'])
            else:
                require(isinstance(p['cmd'],list) and p['cmd'],'probe command required')
                for v in p['cmd']:text(v)
    else:raise ValueError('kind must be Service or Task')
    return d

def changed(before,after):
    validate(after)
    require(all(before[k]==after[k] for k in ('kind','id','node')),'object identity/target cannot change')
    if after['kind']=='Task':
        require(before==after,'Task execution is immutable')


def node_config(root=None):
    from .services import resolve_config
    return resolve_config(Path(root or Path.home()/'.rund').expanduser().resolve())


def discover(root,*,exclude=()):
    result={};errors=[]
    for folder,dirs,files in os.walk(root):
        if Path(folder)==Path(root):dirs[:]=[d for d in dirs if d not in ('agents','configs','gc','scheduling')+tuple(exclude)]
        if 'declaration.json' not in files:continue
        path=Path(folder)/'declaration.json'
        try:
            d=validate(read(path))
            require(d['id'] not in result,'duplicate object identity: '+d['id'])
            result[d['id']]=(path,d)
        except (ValueError,OSError,KeyError,TypeError) as e:errors.append(str(folder)+': '+str(e))
    return result,errors


def publish(root,d,*,path=None):
    from .store import write,write_once
    validate(d)
    found,errors=discover(root,exclude=('services',) if d['kind']=='Task' else ());require(not errors,'; '.join(errors))
    prior=found.get(d['id'])
    if prior:
        old,previous=prior
        changed(previous,d)
        require(path is None or Path(path)==old,'object already published at another path')
        p=old
    else:
        folder=Path(root)/('services' if d['kind']=='Service' else 'tasks')/d['id']
        p=Path(path) if path else folder/'declaration.json'
    require(p.name=='declaration.json','declaration.json filename required')
    (write_once if d['kind']=='Task' else write)(p,d)
    return p
