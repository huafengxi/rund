#!/usr/bin/env python3
"""Run shared-tree transport using host-id and fixed runtime paths."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import node_config
from runtime.store import directory,require


def hub_paths(c):
    # A neutral exchange tree keeps the hub node's own execution files separate.
    if c['node']==c['hub']:
        base=Path(c['root'])/'hub'
        shared=directory(base/'shared');probe=directory(base/'probe');probe.chmod(0o700)
        return str(shared.resolve()),str(probe.resolve())
    script="from pathlib import Path; import json; b=Path.home()/'.rund/hub'; s=b/'shared'; p=b/'probe'; s.mkdir(parents=True,exist_ok=True); p.mkdir(mode=0o700,parents=True,exist_ok=True); p.chmod(0o700); print(json.dumps([str(s.resolve()),str(p.resolve())]))"
    result=subprocess.run(['ssh','-F',str(Path(c['home'])/'.ssh/config'),'-o','BatchMode=yes',
                           '-o','ConnectTimeout=8',c['hub'],
                           'exec "$HOME/miniconda3/bin/python3" -c '+shlex.quote(script)],
                          check=True,capture_output=True,text=True,timeout=20)
    shared,probe=json.loads(result.stdout)
    require(Path(shared).is_absolute() and Path(probe).is_absolute(),'absolute hub paths required')
    return shared,probe


def command(root,action):
    c=node_config(root);private=Path(c['privateRoot'])
    tool=Path(__file__).resolve().parents[1]/'sync/ssh-sync.py'
    require(tool.is_file(),'bundled transport is missing: '+str(tool))
    shared,remote_probe=hub_paths(c)
    probe=directory(private/'apps/agents-sync/probe');probe.chmod(0o700)
    directory(c['sharedRoot']);directory(private/'locks')
    argv=[c['python'],'-B',str(tool),'watch',c['sharedRoot'],c['hub']+':'+shared,
          '--foreground','--probe-root',str(probe.resolve()),'--remote-probe-root',remote_probe,
          '--cycle-lock',str(private/'locks/shared-sync.lock'),'--interval','15','--min-cycle','3']
    if action=='once':argv.append('--once')
    env=dict(HOME=c['home'],PATH=c['path'],LC_ALL='C.UTF-8',PYTHONDONTWRITEBYTECODE='1',
             SSH_SYNC_CONFIG=str(Path(c['home'])/'.ssh/config'))
    return argv,env


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path);p.add_argument('action',choices=('watch','once'));a=p.parse_args()
    argv,env=command(a.root,a.action);os.execve(argv[0],argv,env)

if __name__=='__main__':main()
