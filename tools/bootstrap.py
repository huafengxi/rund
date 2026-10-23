#!/usr/bin/env python3
"""Install only the fixed generic runtime, node configuration and runctl."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import node_config
from runtime.store import write,directory,require


def install(prefix,config,source,revision,upgrade=False):
    prefix=Path(prefix).absolute();source=Path(source).absolute()
    require((source/'runctl.py').is_file(),'fixed runtime source required')
    require(len(revision)==40 and all(c in '0123456789abcdef' for c in revision),'fixed revision required')
    if prefix.exists():require(upgrade,'new installation target required')
    directory(prefix);target=prefix/'local/node.json';write(target,dict(config,runtimeSource=str(source)));c=node_config(target)
    for p in (c['sharedRoot'],c['privateRoot']):directory(p)
    for rel in ('artifacts','locks','tasks'):directory(Path(c['privateRoot'])/rel)
    script='#!'+c['python']+'\nimport json,sys\nsys.dont_write_bytecode=True\nfrom pathlib import Path\nconfig=Path(__file__).resolve().parents[1]/"local/node.json"\nc=json.loads(config.read_text());sys.path.insert(0,c["runtimeSource"])\nfrom runtime.cli import main\nraise SystemExit(main(default_config=config))\n'
    entry=directory(prefix/'bin')/'runctl';entry.write_text(script);entry.chmod(0o700)

def main(argv=None):
    p=argparse.ArgumentParser(description='Bootstrap a configured node from two Git checkouts.')
    p.add_argument('--deployment',type=Path,help='configuration checkout containing bootstrap.json')
    p.add_argument('--node',help='node id; default is the configured hostname mapping')
    p.add_argument('--check',action='store_true',help='validate deployment inputs without installing or starting')
    p.add_argument('--timeout',type=float,default=300)
    p.add_argument('--prefix',type=Path);p.add_argument('--node-config',type=Path)
    p.add_argument('--runtime-source',type=Path);p.add_argument('--revision');p.add_argument('--upgrade',action='store_true')
    a=p.parse_args(argv)
    try:
        if a.deployment:
            require(not any((a.prefix,a.node_config,a.runtime_source,a.revision,a.upgrade)),'deployment and low-level install arguments are exclusive')
            require(a.timeout>0,'positive convergence timeout required')
            from tools.deployment import bootstrap
            print(json.dumps(bootstrap(a.deployment,a.node,check=a.check,timeout=a.timeout)))
        else:
            require(all((a.prefix,a.node_config,a.runtime_source,a.revision)),'deployment checkout or all low-level installation arguments required')
            require(not a.node and not a.check,'node/check require a deployment checkout')
            install(a.prefix,json.loads(a.node_config.read_text()),a.runtime_source,a.revision,a.upgrade)
        return 0
    except (OSError,ValueError,KeyError,TypeError,__import__('subprocess').SubprocessError) as e:
        print('bootstrap: '+str(e),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())
