"""Ordinary scheduler Service; no process execution or node executor coupling."""
import os
import signal
import time
from pathlib import Path
from .config import FINAL, node_config
from .protocol import cancellation
from .services import objects,wanted
from .store import read, write, now, require, lock
from .admission import admission_path, execution_digest


def reconcile(c, policy):
    found = objects(c)
    path = admission_path(c)
    if not path.exists():
        for p,d in found.values():
            sp=p.with_name('state.json')
            if 'resources' in d and sp.exists():
                if read(sp)['phase'] in ('starting','running','canceling','stopping','unknown'):
                    raise ValueError('restore missing admission record before scheduling')
    prior = read(path) if path.exists() else {'node': c['node'], 'grants': {}}
    require(prior['node'] == c['node'], 'scheduler record belongs to another node')
    grants = dict(prior['grants'])
    held = set()
    for tid in list(grants):
        entry = found.get(tid)
        # Missing inputs or outcomes retain the reservation until resolved.
        if entry is None:
            return
        p, d = entry
        s = read(p.with_name('state.json')) if p.with_name('state.json').exists() else None
        if s and (s['phase'] in FINAL or d['kind']=='Service' and (s['phase'] in ('stopped','backoff','preparing') or
                s['phase']=='queued' and s.get('waitObserved') and grants[tid]!=execution_digest(d))):
            del grants[tid]
        else:
            held.update(d.get('resources', []))
    queue = []
    for tid, (p, d) in found.items():
        if 'resources' not in d or tid in grants or d['kind']=='Task' and cancellation(p) is not None:
            continue
        sp = p.with_name('state.json')
        s = read(sp) if sp.exists() else None
        if s and s['phase'] == 'queued':
            queue.append((s.get('queuedAt', s['updatedAt']), tid, d))
    for _, tid, d in sorted(queue):
        if len(grants) >= policy.get('maxConcurrent', 4):
            break
        if held.intersection(d['resources']):
            continue
        grants[tid] = execution_digest(d)
        held.update(d['resources'])
    limit=policy.get('maxConcurrent',4)
    if grants != prior['grants'] or prior.get('maxConcurrent') != limit or not path.exists():
        write(path, {'node': c['node'], 'grants': grants, 'maxConcurrent': limit, 'updatedAt': now()})


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
