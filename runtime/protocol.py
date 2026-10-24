"""Lightweight object client. Never imports executor or process execution."""
from pathlib import Path
from .config import discover,publish
from .store import read,write_once,require


def objects(root):
    found,errors=discover(root,exclude=('services',));require(not errors,'; '.join(errors));return found


def get(root,id):
    found=objects(root);require(id in found,'object not found: '+id);p,d=found[id]
    s=p.with_name('state.json');return d,read(s) if s.exists() else None


def cancellation(path):
    p=Path(path).with_name('cancel.json')
    if not p.exists():return None
    value=read(p)
    require(isinstance(value,dict) and set(value)=={'mode'} and value['mode'] in ('terminate','kill'),'invalid Task cancellation')
    return value


def desired(root,id,value,*,mode=None):
    found=objects(root);require(id in found,'object not found: '+id)
    p,d=found[id]
    require(d['kind']=='Task','Task cancellation required')
    require(value in ('run','cancel'),'Task desired must be run/cancel')
    prior=cancellation(p)
    if value=='run':
        require(prior is None,'Task cancellation is irreversible')
        return d
    require(mode in (None,'terminate','kill'),'invalid cancel mode')
    if prior is None:
        prior={'mode':mode or 'terminate'}
        write_once(p.with_name('cancel.json'),prior)
    return dict(d,desired='cancel',cancelMode=prior['mode'])
