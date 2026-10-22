#!/usr/bin/env python3
"""Invoke the caller-pinned shared-tree transport; no object-specific transport."""
import argparse
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import node_config
from runtime.store import read,require


def command(path, action):
    link=read(path);c=node_config(link['nodeConfig'])
    for key in ('tool','remote','probeRoot','remoteProbeRoot','cycleLock','sshConfig'):
        require(key in link,'missing sync '+key)
    argv=[c['python'],'-B',link['tool'],'watch',c['sharedRoot'],link['remote'],
          '--foreground','--probe-root',link['probeRoot'],'--remote-probe-root',link['remoteProbeRoot'],
          '--cycle-lock',link['cycleLock'],'--interval',str(link.get('interval',15)),
          '--min-cycle',str(link.get('minCycle',3))]
    if action=='once':argv.append('--once')
    env=dict(HOME=c['home'],PATH=c['path'],LC_ALL='C.UTF-8',PYTHONDONTWRITEBYTECODE='1',SSH_SYNC_CONFIG=link['sshConfig'])
    return argv,env


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True,type=Path);p.add_argument('action',choices=('watch','once'));a=p.parse_args()
    argv,env=command(a.config,a.action);os.execve(argv[0],argv,env)

if __name__=='__main__':main()
