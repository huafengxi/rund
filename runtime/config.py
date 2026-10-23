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
        require(set(d)<=COMMON|EXEC_KEYS|{'desired','cancelMode','service'},'unknown Task fields')
        require(d.get('desired') in ('run','cancel'),'Task desired must be run/cancel')
        require(d.get('cancelMode','terminate') in ('terminate','kill'),'invalid cancel mode')
        execution({k:v for k,v in d.items() if k in EXEC_KEYS})
        if 'service' in d:
            s=d['service'];require(isinstance(s,dict) and set(s)=={'id','version','declarationDigest'},'invalid Service reference');name(s['id'])
    elif d['kind']=='Service':
        require(set(d)<=COMMON|EXEC_KEYS|{'version','desired','restart','probe'},'unknown Service fields')
        require(type(d.get('version')) is int and d['version']>0,'positive Service version required')
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
        a={k:v for k,v in after.items() if k not in ('desired','cancelMode')}
        b={k:v for k,v in before.items() if k not in ('desired','cancelMode')}
        require(a==b,'Task execution is immutable')
        require(before['desired']!='cancel' or after==before,'Task cancellation is irreversible')
    else:
        require(after['version']>=before['version'],'Service version rollback')
        keys=tuple(EXEC_KEYS)+('restart','probe')
        require(all(before.get(k)==after.get(k) for k in keys) or after['version']>before['version'],'changed execution requires higher Service version')


def node_config(root=None):
    from .services import resolve_config
    return resolve_config(Path(root or Path.home()/'.rund').expanduser().resolve())


DECLARATION=re.compile(r'declaration-v([1-9][0-9]*)\.json')

def declaration_revision(path):
    match=DECLARATION.fullmatch(Path(path).name)
    require(match is not None,'versioned declaration filename required')
    return int(match[1])


def discover(root):
    result={};errors=[]
    for folder,dirs,files in os.walk(root):
        if Path(folder)==Path(root):dirs[:]=[d for d in dirs if d not in ('agents','configs','gc','scheduling')]
        paths=sorted((Path(folder)/f for f in files if DECLARATION.fullmatch(f)),key=declaration_revision)
        prior=None
        try:
            for path in paths:
                d=validate(read(path))
                if prior is not None:changed(prior,d)
                prior=d
            if paths:
                require(prior['id'] not in result,'duplicate object identity: '+prior['id'])
                result[prior['id']]=(paths[-1],prior)
        except (ValueError,OSError,KeyError,TypeError) as e:errors.append(str(folder)+': '+str(e))
    return result,errors


def publish(root,d,*,path=None,revision=None):
    from .store import write_once
    validate(d)
    found,errors=discover(root);require(not errors,'; '.join(errors))
    prior=found.get(d['id'])
    if prior:
        old,previous=prior
        if previous==d and path is None and revision is None:return old
        changed(previous,d)
        number=declaration_revision(old)+1 if revision is None else revision
        require(number>=declaration_revision(old),'declaration revision rollback')
        folder=old.parent
    else:
        number=1 if revision is None else revision
        folder=Path(root)/('services' if d['kind']=='Service' else 'tasks')/d['id']
    require(type(number) is int and number>0,'positive declaration revision required')
    p=Path(path) if path else folder/('declaration-v'+str(number)+'.json')
    require(declaration_revision(p)==number,'declaration filename revision mismatch')
    if prior and number==declaration_revision(prior[0]):require(previous==d,'declaration version content conflict')
    write_once(p,d)
    return p
