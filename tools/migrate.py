#!/usr/bin/env python3
"""Convert stopped-node declarations to single current files and cancellation requests."""
import argparse
import os
from pathlib import Path
import re
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import node_config,validate
from runtime.store import read,write,write_once,require,lock,directory,digest
from runtime.admission import execution_digest

VERSIONED=re.compile(r'declaration-v([1-9][0-9]*)\.json')


def convert(d):
    result={k:v for k,v in d.items() if k not in ('publisher','revision','cancelMode')}
    if result['kind']=='Task':result['desired']='run'
    return validate(result)


def cancel(path,d):
    if d['kind']=='Task' and d.get('desired')=='cancel':
        write_once(path.with_name('cancel.json'),{'mode':d.get('cancelMode','terminate')})


def migrate(root):
    c=node_config(root);private=directory(c['privateRoot'])
    from runtime.cli import executor_alive
    require(not executor_alive(c),'stop rund before migration')
    fd=lock(private/'locks/runtime.lock')
    try:
        files=[]
        for tree in (Path(c['configRoot'])/'objects',Path(c['sharedRoot'])):
            for folder,dirs,names in os.walk(tree):
                if Path(folder)==tree:dirs[:]=[d for d in dirs if d not in ('agents','configs','gc','scheduling')+ (('services',) if tree==Path(c['sharedRoot']) else ())]
                versions=sorted((Path(folder)/n for n in names if VERSIONED.fullmatch(n)),key=lambda p:int(VERSIONED.fullmatch(p.name)[1]))
                target=Path(folder)/'declaration.json'
                if not target.exists() and not versions:continue
                current=read(target) if target.exists() else None
                legacy=current is not None and ('publisher' in current or 'revision' in current or current.get('desired')=='cancel')
                source=versions[-1] if versions and (current is None or legacy) else target
                before=read(source)
                if legacy:
                    backup=private/'migrations/single-declaration'/('config' if tree==Path(c['configRoot'])/'objects' else 'shared')/target.relative_to(tree)
                    if not backup.exists():
                        directory(backup.parent);shutil.copyfile(target,backup)
                if tree==Path(c['sharedRoot']) and before.get('kind')=='Service':continue
                converted=convert(before)
                # Cancel is independent and is published before its immutable definition.
                cancel(target,before)
                write(target,converted)
                if source!=target or before!=converted:files.append(str(target))
                if tree==Path(c['configRoot'])/'objects':
                    for p in versions:p.unlink()
        accepted=private/'accepted.json'
        if accepted.exists():
            records=read(accepted)
            backup=private/'migrations/single-declaration/accepted.json'
            if not backup.exists():write_once(backup,records)
            for entry in records.values():
                before=entry['declaration'];entry['declaration']=convert(before)
                path=Path(c['sharedRoot'])/entry['path']
                entry['path']=str(Path(entry['path']).with_name('declaration.json'))
                if before['kind']=='Task':
                    cancel(path,before)
                    write_once(path.with_name('declaration.json'),entry['declaration'])
                intent=private/'services'/before['id']/'intent.json'
                if before['kind']=='Service' and intent.exists():
                    value=read(intent)
                    if value['declarationDigest']==digest(before):write(intent,dict(value,declarationDigest=digest(entry['declaration'])))
            write(accepted,records)
        ledger=Path(c['sharedRoot'])/'scheduling/state.json'
        if c['node']==c['hub'] and ledger.exists():
            from runtime.services import objects
            state=read(ledger);found=objects(c)
            for tid in state['grants']:
                if tid in found:state['grants'][tid]=execution_digest(found[tid][1])
            write(ledger,state)
        return files
    finally:os.close(fd)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path);a=p.parse_args()
    for path in migrate(a.root):print(path)
