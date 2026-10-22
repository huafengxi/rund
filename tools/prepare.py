#!/usr/bin/env python3
"""Ordinary Task payload: fixed Git sources -> immutable artifact directory."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.source_export import prepare
from runtime.store import digest,read,write,require,directory,lock


def build(plan,output,cache):
    output=Path(output);cache=directory(cache)
    fd=lock(cache/('artifact-'+digest(str(output))+'.lock'))
    try:
        key=digest(plan);ready=output/'ready.json'
        if ready.exists():
            require(read(ready)['inputDigest']==key,'artifact path has different content');return
        require(not output.exists(),'incomplete final artifact retained')
        d=dict(apiVersion='rund/v1alpha1',kind='Task',metadata=dict(id='prepare-artifact',version=1),spec=dict(node='prepare',attemptId='once',requestId='prepare',sources=plan['sources'],prepare=[],toolchain={'python':sys.executable},argv=[sys.executable],cwd='${work}',env={},credentialRefs=[]))
        result=prepare(d,cache/'objects')
        stage=output.with_name('.artifact-'+uuid.uuid4().hex);directory(stage)
        for source in plan['sources']:
            shutil.copytree(Path(result['code'])/source['alias'],stage/source['alias'],copy_function=shutil.copy2)
        write(stage/'ready.json',dict(inputDigest=key,sources={s['alias']:s['revision'] for s in plan['sources']}))
        for base,dirs,files in os.walk(stage,topdown=False):
            for name in files:
                p=Path(base)/name;p.chmod(0o555 if p.stat().st_mode&0o111 else 0o444)
            if Path(base)!=stage:Path(base).chmod(0o555)
        directory(output.parent);os.rename(stage,output);output.chmod(0o555)
    finally:os.close(fd)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--plan',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--cache',required=True,type=Path);a=p.parse_args()
    try:build(read(a.plan),a.output,a.cache)
    except (OSError,ValueError,KeyError,__import__('subprocess').SubprocessError) as e:print('prepare failed: '+str(e),file=sys.stderr);sys.exit(2)
