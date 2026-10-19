"""Cooperating foreground Linux fixture ONLY. Copied into a fresh pinned Git repo.

No private runtime FD is required; no exec or daemonization in this fixture.
Files in cwd are deterministic test barriers, not a runtime control channel.
"""
import json
import os
from pathlib import Path
import signal
import sys
import time


def identity():
    raw = Path('/proc/self/stat').read_text()
    tail = raw[raw.rindex(')') + 2:].split()
    return {'pid': os.getpid(), 'procStart': int(tail[19]), 'pgid': os.getpgrp(),
            'session': os.getsid(0), 'uid': os.getuid(), 'cwd': os.getcwd(),
            'environment': dict(os.environ)}


def mark(name, value):
    p = Path(name)
    temp = p.with_suffix('.tmp-' + str(os.getpid()))
    temp.write_text(json.dumps(value))
    os.replace(temp, p)


def until(path):
    while not Path(path).exists():
        time.sleep(0.005)


mode = sys.argv[1]
terminated = False


def on_term(*unused):
    global terminated
    terminated = True
    mark('term-' + str(os.getpid()), {'at': time.time(), 'identity': identity()})


signal.signal(signal.SIGTERM, signal.SIG_IGN if mode in ('ignore', 'ignore-group', 'leader-exit') else on_term)
if mode in ('group', 'ignore-group', 'leader-exit'):
    child = os.fork()
    if child == 0:
        mark('child-ready', identity())
        while not terminated:
            time.sleep(0.005)
        mark('child-exit', {'at': time.time(), 'identity': identity()})
        sys.exit(0)
    until('child-ready')
mark('ready', identity())
print('synthetic stdout', flush=True)
print('synthetic stderr', file=sys.stderr, flush=True)
if mode == 'exit':
    until('release')
    mark('exit', {'at': time.time(), 'identity': identity()})
    sys.exit(int(sys.argv[2]))
if mode == 'leader-exit':
    until('exit-leader')
    mark('leader-exit', {'at': time.time(), 'identity': identity()})
    sys.exit(0)
while not terminated:
    time.sleep(0.005)
if mode == 'group':
    os.waitpid(child, 0)
mark('exit', {'at': time.time(), 'identity': identity()})
sys.exit(0)
