#!/usr/bin/env python3
"""Install only the fixed generic runtime, node configuration and servicectl."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import node_config
from runtime.store import write,directory,require


def install(prefix,config,source,revision,upgrade=False):
    prefix=Path(prefix).absolute();source=Path(source).absolute()
    require((source/'servicectl.py').is_file(),'fixed runtime source required')
    require(len(revision)==40 and all(c in '0123456789abcdef' for c in revision),'fixed revision required')
    if prefix.exists():require(upgrade,'new installation target required')
    directory(prefix);target=prefix/'local/node.json';write(target,dict(config,runtimeSource=str(source)));c=node_config(target)
    for p in (c['sharedRoot'],c['privateRoot']):directory(p)
    for rel in ('artifacts','locks','tasks'):directory(Path(c['privateRoot'])/rel)
    script='#!'+c['python']+'\nimport json,sys\nsys.dont_write_bytecode=True\nfrom pathlib import Path\nconfig=Path(__file__).resolve().parents[1]/"local/node.json"\nc=json.loads(config.read_text());sys.path.insert(0,c["runtimeSource"])\nfrom runtime.cli import main\nraise SystemExit(main(default_config=config))\n'
    entry=directory(prefix/'bin')/'servicectl';entry.write_text(script);entry.chmod(0o700)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--prefix',required=True,type=Path);p.add_argument('--node-config',required=True,type=Path);p.add_argument('--runtime-source',required=True,type=Path);p.add_argument('--revision',required=True);p.add_argument('--upgrade',action='store_true');a=p.parse_args()
    install(a.prefix,json.loads(a.node_config.read_text()),a.runtime_source,a.revision,a.upgrade)
