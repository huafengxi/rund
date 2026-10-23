#!/usr/bin/env python3
"""Convert stopped-node legacy declarations to immutable version files."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import node_config,validate,discover,declaration_revision
from runtime.store import read,write,write_once,require,lock,directory,digest
from runtime.admission import execution_digest


def convert(d):
    result={k:v for k,v in d.items() if k not in ('publisher','revision')}
    return validate(result)


def migrate(root):
    c=node_config(root);private=directory(c['privateRoot'])
    from runtime.cli import executor_alive
    require(not executor_alive(c),'stop rund before migration')
    fd=lock(private/'locks/runtime.lock')
    import os
    try:
        files=[]
        for tree in (Path(c['configRoot'])/'objects',Path(c['sharedRoot'])):
            for p in tree.rglob('declaration.json'):
                if tree==Path(c['sharedRoot']) and p.relative_to(tree).parts[0] in ('agents','configs','gc','scheduling'):continue
                d=read(p)
                if tree==Path(c['sharedRoot']) and d.get('kind')=='Service':continue
                converted=convert(d);revision=d.get('revision',1)
                target=p.with_name('declaration-v'+str(revision)+'.json')
                # Existing versioned history can already include this snapshot.
                if not any(p.parent.glob('declaration-v*.json')):write_once(target,converted);files.append(str(target))
        accepted=private/'accepted.json'
        if accepted.exists():
            records=read(accepted)
            if any('publisher' in x['declaration'] or 'revision' in x['declaration'] for x in records.values()):
                write_once(private/'migrations/versioned-declarations/accepted.json',records)
                for entry in records.values():
                    old=entry['declaration'];entry['declaration']=convert(old)
                    path=Path(entry['path'])
                    if path.name=='declaration.json':entry['path']=str(path.with_name('declaration-v'+str(old.get('revision',1))+'.json'))
                write(accepted,records)
        # Intent remains valid for the equivalent declaration after metadata removal.
        for p in (private/'services').glob('*/intent.json'):
            intent=read(p);record=read(private/'migrations/versioned-declarations/accepted.json') if (private/'migrations/versioned-declarations/accepted.json').exists() else {}
            before=record.get(p.parent.name,{}).get('declaration')
            if before and intent['declarationDigest']==digest(before):
                write(p,dict(intent,declarationDigest=digest(convert(before))))
        # Grants depend on execution content; migrate them only on the hub.
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
