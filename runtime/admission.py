"""Per-object grants, written only by the hub scheduler."""
from pathlib import Path
from .store import digest,read,require


def execution_digest(d):
    return digest({k:v for k,v in d.items() if k not in ('desired','cancelMode')})


def admission_path(c,d):
    return Path(c['sharedRoot'])/('services' if d['kind']=='Service' else 'tasks')/d['id']/'grant.json'


def grants(c):
    result={}
    for kind in ('services','tasks'):
        for path in (Path(c['sharedRoot'])/kind).glob('*/grant.json'):
            value=read(path)
            require(value.get('node')==c['hub'],'grant belongs to another scheduler')
            require(value.get('id')==path.parent.name,'grant identity mismatch')
            require(type(value.get('active')) is bool,'grant active flag required')
            if value['active']:result[value['id']]=(path,value)
    return result


def admitted(c,d):
    if 'resources' not in d:return True
    p=admission_path(c,d)
    if not p.exists():return False
    s=read(p)
    return s.get('node')==c['hub'] and s.get('id')==d['id'] and s.get('active') is True and s.get('executionDigest')==execution_digest(d)
