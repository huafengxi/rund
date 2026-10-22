"""Lightweight object client. Never imports Owner or process execution."""
from pathlib import Path
from .config import discover,publish,validate,FINAL
from .store import read,write,digest,require,canonical

def objects(root):
    found,errors=discover(root);require(not errors,'; '.join(errors));return found

def get(root,id):
    found=objects(root);require(id in found,'object not found: '+id);p,d=found[id]
    s=p.with_name('state.json');return d,read(s) if s.exists() else None

def desired(root,id,value,*,writer,publishers,mode=None):
    d,s=get(root,id)
    if d['desired']==value:return d
    d=dict(d,desired=value,revision=d['revision']+1)
    if mode:d['cancelMode']=mode
    publish(root,d,writer=writer,publishers=publishers);return d
