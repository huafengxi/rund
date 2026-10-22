#!/usr/bin/env python3
"""Finite installer for an explicitly supplied fixed runtime and node config."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import node_config
from runtime.store import write,directory,require


def install(prefix,config,source,revision,upgrade=False):
    prefix=Path(prefix).absolute();source=Path(source).absolute();require(source.is_dir() and (source/'rund.py').is_file(),'fixed runtime source required')
    require(len(revision)==40 and all(c in '0123456789abcdef' for c in revision),'fixed revision required')
    if prefix.exists():require(upgrade,'new installation target required')
    directory(prefix)
    target=prefix/'local/node.json'
    write(target,config);c=node_config(target)
    for p in (c['sharedRoot'],c['privateRoot']):directory(p)
    for rel in ('artifacts','locks','logs','work','tmp','keys'):directory(Path(c['privateRoot'])/rel)
    write(prefix/'runtime.json',dict(runtime=str(source),revision=revision))
    temporary=prefix/'.launcher.next';shutil.copyfile(source/'tools/launch.py',temporary);temporary.chmod(0o700);os.replace(temporary,prefix/'agent-runtime.py')
    make='.PHONY: status serviced.status serviced.sync start stop\nPYTHON := '+c['python']+'\nCONTROL := $(CURDIR)/agent-runtime.py\n\nstatus serviced.status:\n\t@$(PYTHON) -B "$(CONTROL)" status\n\nstart:\n\t@$(PYTHON) -B "$(CONTROL)" service start service-sync\n\nstop:\n\t@$(PYTHON) -B "$(CONTROL)" service stop service-sync\n\nserviced.sync:\n\t@$(PYTHON) -B "$(CONTROL)" service sync\n\n%.start:\n\t@$(PYTHON) -B "$(CONTROL)" service start $*\n\n%.stop:\n\t@$(PYTHON) -B "$(CONTROL)" service stop $*\n\n%.status:\n\t@$(PYTHON) -B "$(CONTROL)" service status $*\n'
    (prefix/'Makefile').write_text(make)
    import shlex
    for name,action in (('agentctl','ctl'),('agent-report','report')):
        p=directory(prefix/'bin')/name;p.write_text('#!/bin/sh\nexec '+shlex.join([c['python'],'-B',str(prefix/'agent-runtime.py'),action])+' "$@"\n');p.chmod(0o700)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--prefix',required=True,type=Path);p.add_argument('--node-config',required=True,type=Path);p.add_argument('--runtime-source',required=True,type=Path);p.add_argument('--revision',required=True);p.add_argument('--upgrade',action='store_true');a=p.parse_args()
    install(a.prefix,json.loads(a.node_config.read_text()),a.runtime_source,a.revision,a.upgrade)
