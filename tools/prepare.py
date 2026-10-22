#!/usr/bin/env python3
"""Ordinary Task: fixed Git objects -> one atomic, immutable artifact."""
import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.store import digest,read,require,canonical
from tools.source_plan import validate,repository
from tools.source_export import Git,directory,lock,source_objects,export,verify_tree,write_new,sync_dir


def build(plan,output,cache):
    validate(plan);output=Path(output);cache=directory(Path(cache))
    directory(output.parent);directory(cache/'git');directory(cache/'git-home');directory(cache/'locks')
    with ExitStack() as stack:
        stack.enter_context(lock(cache/'locks'/('artifact-'+digest(str(output))+'.lock')))
        key=digest(plan);ready=output/'ready.json'
        if ready.exists():
            result=read(ready);require(result['inputDigest']==key,'artifact path has different content')
            require(set(p.name for p in output.iterdir())==set(result['files'])|{'ready.json'},'unexpected artifact entry')
            for alias,inventory in result['files'].items():verify_tree(output/alias,inventory,sealed=True)
            return result
        require(not output.exists(),'incomplete final artifact retained')
        repos={repository(s['repository']) for s in plan['sources']}
        for repo in sorted(repos,key=str):stack.enter_context(lock(cache/'locks'/('repo-'+digest(str(repo))+'.lock')))
        git=Git(cache);stage=directory(output.with_name('.artifact-'+uuid.uuid4().hex));inventories={}
        for src in plan['sources']:
            repo=repository(src['repository']);db=git.cache(repo)
            if isinstance(repo,Path):objects=source_objects(repo,cache)
            else:
                try:git.object(db,db/'objects',src['revision'],'commit')
                except ValueError:git.fetch(db,src)
                objects=db/'objects'
            target=directory(stage/src['alias']);inventory=export(git,db,objects,src['revision'],target);verify_tree(target,inventory)
            for path in src.get('lockFiles',[]):require(path in inventory and inventory[path]['gitMode'] in ('100644','100755'),'lockFile must be a tracked regular file')
            inventories[src['alias']]=inventory
        result=dict(inputDigest=key,sources={s['alias']:s['revision'] for s in plan['sources']},files=inventories)
        write_new(stage/'ready.json',canonical(result),0o444)
        for base,dirs,files in os.walk(stage,topdown=False):
            sync_dir(Path(base))
            if Path(base)!=stage:Path(base).chmod(0o555)
        os.rename(stage,output);output.chmod(0o555);sync_dir(output.parent)
        return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--plan',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--cache',required=True,type=Path);a=p.parse_args()
    try:build(read(a.plan),a.output,a.cache)
    except (OSError,ValueError,KeyError,__import__('subprocess').SubprocessError) as e:print('prepare failed: '+str(e),file=sys.stderr);sys.exit(2)
