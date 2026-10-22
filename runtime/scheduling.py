"""Single-writer global admission. Resource names are opaque global identities."""
from pathlib import Path
from .config import FINAL, discover
from .store import digest, read, write, now


def execution_digest(d):
    return digest({k: v for k, v in d.items() if k not in ('revision', 'desired', 'cancelMode')})


def admission_path(c):
    return Path(c['sharedRoot']) / 'scheduling/state.json'


def admitted(c, d):
    if 'resources' not in d:
        return True
    p = admission_path(c)
    if not p.exists():
        return False
    s = read(p)
    return s.get('node') == c['scheduler']['node'] and s.get('grants', {}).get(d['id']) == execution_digest(d)


def reconcile(c):
    policy = c.get('scheduler')
    if not policy or policy['node'] != c['node']:
        return
    found, errors = discover(c['sharedRoot'])
    if errors:
        raise ValueError('; '.join(errors))
    path = admission_path(c)
    if not path.exists():
        for p,d in found.values():
            sp=p.with_name('state.json')
            if d['kind']=='Task' and 'resources' in d and sp.exists():
                if read(sp)['phase'] in ('starting','running','canceling','unknown'):
                    raise ValueError('restore missing admission record before scheduling')
    prior = read(path) if path.exists() else {'node': c['node'], 'grants': {}}
    grants = dict(prior['grants'])
    held = set()
    for tid in list(grants):
        entry = found.get(tid)
        # Missing inputs or outcomes retain the reservation until resolved.
        if entry is None:
            return
        p, d = entry
        s = read(p.with_name('state.json')) if p.with_name('state.json').exists() else None
        if s and s['phase'] in FINAL:
            del grants[tid]
        else:
            held.update(d.get('resources', []))
    queue = []
    for tid, (p, d) in found.items():
        if d['kind'] != 'Task' or 'resources' not in d or tid in grants or d['desired'] != 'run':
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
