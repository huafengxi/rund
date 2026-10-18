"""Cooperating immutable test-only foreground process; retains lease FD."""
import json
import os
from pathlib import Path
import sys
import time


def identity():
    tail = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': os.getpid(), 'procStart': int(tail[19]), 'pgid': os.getpgid(0),
            'session': os.getsid(0), 'uid': os.getuid()}


mode, code = sys.argv[1], int(sys.argv[2])
# Test has no close/daemonize/setsid operation. Forked child retains RUND_LEASE_FD.
os.fstat(int(os.environ['RUND_LEASE_FD']))
Path('ready.json').write_text(json.dumps(identity()))
if mode == 'fork':
    child = os.fork()
    if child:
        while not Path('child.json').exists():
            time.sleep(.005)
        sys.exit(code)
    Path('child.json').write_text(json.dumps(identity()))
if mode == 'exit':
    sys.exit(code)
while not Path('release').exists():
    time.sleep(.005)
sys.exit(code)
