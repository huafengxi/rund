"""Isolated real-exec/close_fds helper fixture; no inherited runtime FD.

Copied into a new pinned source Git by test_exec. No daemonization/group escape.
Filesystem barriers are fixture inputs, never runtime facts or control receipts.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def identity():
    raw = Path('/proc/self/stat').read_text()
    tail = raw[raw.rindex(')') + 2:].split()
    fds = {}
    for p in Path('/proc/self/fd').iterdir():
        try:
            fds[p.name] = os.readlink(p)
        except FileNotFoundError:
            pass
    return {'pid': os.getpid(), 'procStart': int(tail[19]),
            'pgid': os.getpgrp(), 'session': os.getsid(0), 'uid': os.getuid(),
            'cwd': os.getcwd(), 'environment': dict(os.environ),
            'fds': fds, 'argv': sys.argv}


def mark(name, value):
    p = Path(name)
    temp = p.with_name(p.name + '.tmp-' + str(os.getpid()))
    temp.write_text(json.dumps(value))
    os.replace(temp, p)


def until(path):
    while not Path(path).exists():
        time.sleep(.005)


mode, code, ignore = sys.argv[1], int(sys.argv[2]), sys.argv[3] == '1'
stopping = False


def on_term(*unused):
    global stopping
    stopping = True
    mark('term-' + str(os.getpid()), identity())


signal.signal(signal.SIGTERM, signal.SIG_IGN if ignore else on_term)


def replace(role, marker):
    mark(marker, identity())
    os.execv(sys.executable, [sys.executable, '-B', __file__, role,
                             str(code), '1' if ignore else '0'])


if mode == 'root':
    replace('post-root', 'before-exec')
elif mode in ('parent', 'subprocess-parent'):
    helper = None
    if mode == 'parent':
        child = os.fork()
        if child == 0:
            replace('post-child', 'child-before-exec')
    else:
        # Deliberately ordinary Popen: default close_fds, no private protocol.
        helper = subprocess.Popen([sys.executable, '-B', __file__, 'post-child',
                                   str(code), '1' if ignore else '0'])
        child = helper.pid
    until('child-ready')
    mark('ready', identity())
    while not stopping and not Path('exit-leader').exists():
        time.sleep(.005)
    if stopping:
        if helper is None:
            os.waitpid(child, 0)
        else:
            helper.wait()
    mark('leader-exit', identity())
    sys.exit(code)
elif mode == 'post-child':
    mark('child-ready', identity())
    while not stopping and not Path('release-child').exists():
        time.sleep(.005)
    mark('child-exit', identity())
    sys.exit(code)
elif mode == 'post-root':
    mark('ready', identity())
    while not stopping and not Path('release').exists():
        time.sleep(.005)
    mark('exit', identity())
    sys.exit(code)
else:
    raise ValueError('unknown fixture role')
