"""Read-only grant checks used by every node execution Owner."""
from pathlib import Path
from .store import digest, read


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
    origin = c['publishers'][c['admission']['publisher']]
    return s.get('node') == origin and s.get('grants', {}).get(d['id']) == execution_digest(d)
