"""Ordinary scheduler Service; no process execution or node executor coupling."""
import os
import signal
import time
from pathlib import Path
from .config import FINAL, node_config
from .protocol import cancellation
from .services import objects
from .store import read, write, now, require, lock
from .admission import admission_path, execution_digest, grants as active_grants


def reconcile(c, policy):
    found = objects(c)
    grants=active_grants(c)
    for tid,(p,d) in found.items():
        sp=p.with_name('state.json')
        if 'resources' in d and sp.exists() and tid not in grants:
            if read(sp)['phase'] in ('starting','running','canceling','stopping','unknown'):
                raise ValueError('restore missing admission record before scheduling: '+tid)
    held=set()
    for tid,(path,grant) in list(grants.items()):
        entry=found.get(tid)
        if entry is None:return
        p,d=entry;s=read(p.with_name('state.json')) if p.with_name('state.json').exists() else None
        if s and (s['phase'] in FINAL or d['kind']=='Service' and (s['phase'] in ('stopped','backoff','preparing') or
                s['phase']=='queued' and s.get('waitObserved') and grant['executionDigest']!=execution_digest(d))):
            write(path,dict(grant,active=False,updatedAt=now()))
            del grants[tid]
        else:held.update(grant['resources'])
    queue=[]
    for tid,(p,d) in found.items():
        if 'resources' not in d or tid in grants or d['kind']=='Task' and cancellation(p) is not None:continue
        sp=p.with_name('state.json');s=read(sp) if sp.exists() else None
        if s and s['phase']=='queued':queue.append((s.get('queuedAt',s['updatedAt']),tid,d))
    for _,tid,d in sorted(queue):
        if len(grants)>=policy.get('maxConcurrent',4):break
        if held.intersection(d['resources']):continue
        grant=dict(node=c['node'],id=tid,active=True,executionDigest=execution_digest(d),resources=d['resources'],updatedAt=now())
        path=admission_path(c,d);write(path,grant)
        grants[tid]=(path,grant);held.update(d['resources'])


class Scheduler:
    def __init__(self, root=None, *, max_concurrent=4, interval=.2):
        self.c = node_config(root)
        require(self.c['node']==self.c['hub'],'scheduler must run on the hub')
        require(type(max_concurrent) is int and max_concurrent>0,'positive maxConcurrent required')
        require(type(interval) in (int,float) and interval>0,'positive scheduler interval required')
        self.policy=dict(maxConcurrent=max_concurrent,interval=interval)
        self.fd = lock(Path(self.c['privateRoot']) / 'locks/scheduler.lock')
        self.stopped = False

    def close(self):
        os.close(self.fd)

    def round(self):
        reconcile(self.c, self.policy)

    def run(self):
        def stop(*unused):
            self.stopped = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        while not self.stopped:
            self.round()
            time.sleep(self.policy.get('interval', .2))
