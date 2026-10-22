#!/usr/bin/env python3
"""Foreground Task payload for an official external lifecycle manager."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.store import read,write,require,now


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--config',required=True,type=Path);a=ap.parse_args();c=read(a.config)
    stopped=False
    def stop(*unused):
        nonlocal stopped
        stopped=True
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    def probe():
        r=subprocess.run(c['probe']['cmd'],capture_output=True,text=True,timeout=10)
        return r.returncode==0 and c['probe'].get('expect','') in r.stdout
    journal=Path(c['journal']);directory=journal.parent;directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    def action(verb):
        if journal.exists():
            old=read(journal);require(old['status']=='done' and old['confirmed'],'prior official action unresolved')
        record=dict(action=verb,status='pending',at=now());write(journal,record)
        r=subprocess.run(c[verb],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=35)
        ok=probe()==(verb=='start');record.update(status='done',exitCode=r.returncode,confirmed=r.returncode==0 and ok,finishedAt=now());write(journal,record)
        require(record['confirmed'],'official action unconfirmed')
    if not probe():action('start')
    while not stopped:
        if not probe():return 1
        time.sleep(.5)
    action('stop');return 0

if __name__=='__main__':
    try:sys.exit(main())
    except (OSError,ValueError,subprocess.SubprocessError):print('official manager unavailable or unconfirmed',file=sys.stderr);sys.exit(1)
