"""Object declaration contract. Paths organize files; kind and id define objects."""
import copy
import os
import re
from pathlib import Path
from .store import read,require,digest

FINAL=frozenset(('succeeded','failed','canceled'))
EXEC_KEYS=frozenset(('argv','needs','cwd','env','resources'))
COMMON=frozenset(('kind','id','name','publisher','node','revision'))

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
    for k in ('kind','id','publisher','node','revision'):require(k in d,'missing '+k)
    for k in ('id','publisher','node'):name(d[k])
    require(type(d['revision']) is int and d['revision']>0,'positive revision required')
    if 'name' in d:text(d['name'])
    if d['kind']=='Task':
        require(set(d)<=COMMON|EXEC_KEYS|{'desired','cancelMode','service'},'unknown Task fields')
        require(d.get('desired') in ('run','cancel'),'Task desired must be run/cancel')
        require(d.get('cancelMode','terminate') in ('terminate','kill'),'invalid cancel mode')
        execution({k:v for k,v in d.items() if k in EXEC_KEYS})
        if 'service' in d:
            s=d['service'];require(isinstance(s,dict) and set(s)=={'id','version','declarationDigest'},'invalid Service reference');name(s['id'])
    elif d['kind']=='Service':
        require(set(d)<=COMMON|{'version','desired','restart','task','probe'},'unknown Service fields')
        require(type(d.get('version')) is int and d['version']>0,'positive Service version required')
        require(d.get('desired') in ('online','offline'),'Service desired must be online/offline')
        require(d.get('restart') in ('manual','on-failure','always'),'invalid restart policy')
        execution(d.get('task'))
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
    require(all(before[k]==after[k] for k in ('kind','id','publisher','node')),'object identity/writer/target cannot change')
    require(after['revision']>=before['revision'],'declaration revision rollback')
    if after['revision']==before['revision']:
        require(after==before,'revision content conflict');return
    if after['kind']=='Task':
        a={k:v for k,v in after.items() if k not in ('desired','cancelMode','revision')}
        b={k:v for k,v in before.items() if k not in ('desired','cancelMode','revision')}
        require(a==b,'Task execution is immutable')
        require(before['desired']!='cancel' or after==dict(before,revision=after['revision']),'Task cancellation is irreversible')
    else:
        require(after['version']>=before['version'],'Service version rollback')
        keys=('task','restart','probe')
        require(all(before.get(k)==after.get(k) for k in keys) or after['version']>before['version'],'changed execution requires higher Service version')

def node_config(path):
    c=read(path)
    for k in ('node','sharedRoot','privateRoot','python','home','path','publishers'):require(k in c,'missing node '+k)
    name(c['node'])
    for k in ('sharedRoot','privateRoot','python','home'):require(Path(c[k]).is_absolute(),'absolute '+k+' required')
    a,b=Path(c['sharedRoot']),Path(c['privateRoot']);require(a!=b and a not in b.parents and b not in a.parents,'shared/private roots overlap')
    require(c['publishers'].get('owner-'+c['node'])==c['node'],'node owner publisher required')
    if 'scheduler' in c:
        s=c['scheduler'];require(isinstance(s,dict),'scheduler object required');name(s['node'])
        require(type(s.get('maxConcurrent',4)) is int and s.get('maxConcurrent',4)>0,'positive maxConcurrent required')
    return c

def discover(root):
    result={};errors=[]
    paths=[]
    for folder,dirs,files in os.walk(root):
        if Path(folder)==Path(root):dirs[:]=[d for d in dirs if d not in ('agents','configs','gc','scheduling')]
        if 'declaration.json' in files:paths.append(Path(folder)/'declaration.json')
    for p in sorted(paths):
        try:
            d=validate(read(p));require(d['id'] not in result,'duplicate object identity: '+d['id']);result[d['id']]=(p,d)
        except (ValueError,OSError,KeyError,TypeError) as e:errors.append(str(p)+': '+str(e))
    return result,errors

def publish(root,d,*,writer,publishers,path=None):
    from .store import write
    validate(d);require(publishers.get(d['publisher'])==writer,'not declaration origin writer')
    found,errors=discover(root);require(not errors,'; '.join(errors))
    prior=found.get(d['id'])
    if prior:changed(prior[1],d);p=prior[0]
    else:p=Path(path) if path else Path(root)/('services' if d['kind']=='Service' else 'tasks')/d['id']/'declaration.json'
    write(p,d);return p
