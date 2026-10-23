#!/usr/bin/env python3
"""Reproducible pull scale experiment; synthetic trees ONLY, never production.

Usage: python3 agents-sync/bench_pull_manifest.py [--originals 220000]
Requires local rsync and replica group membership. Creates hardlinked originals
and DIFFERENT sender contents (separate inodes) to keep disk use modest. Runs
the real remote scanner and real rsync locally, changing only the SSH endpoint.
"""
import argparse
import grp
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('bench_sync', HERE / 'ssh-sync.py')
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)
sys.path.insert(0, str(HERE.parent / 'agentd'))
from safe_delete import safe_rmtree


def emit(**data):
    print(json.dumps(data), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--originals', type=int, default=220000)
    args = parser.parse_args()
    gid = grp.getgrnam(ss.REPLICA_GROUP).gr_gid
    if gid == os.getgid():
        parser.error('run with a non-replica primary group')
    # Isolated filter cost: same 3000 sender files as the old glob experiment.
    manifest = [f'remote-{i:06d}' for i in range(3000)]
    for n in (10000, 50000, args.originals):
        originals = [f'task/t{i:06d}/original' for i in range(n)]
        start = time.perf_counter()
        selected = ss.pull_files(manifest, originals, [])
        emit(phase='filter-only', originals=n, sender_files=len(manifest),
             selected=len(selected), seconds=time.perf_counter() - start)

    base = Path(tempfile.mkdtemp(prefix='pull-manifest-bench-'))
    work = base / 'work'
    dst = work / 'receiver'
    src = work / 'sender'
    dst.mkdir(parents=True)
    src.mkdir()
    original = work / 'original-content'
    remote = work / 'remote-content'
    original.write_text('LOCAL_ORIGINAL')
    remote.write_text('REMOTE_DIFFERENT')
    os.chown(remote, -1, gid)  # membership preflight; sender metadata irrelevant
    try:
        start = time.perf_counter()
        original_seed, remote_seed = original, remote
        for i in range(args.originals):
            # Stay below filesystem per-inode hardlink limits (often 65000).
            if i and i % 10000 == 0:
                original_seed = work / f'original-content-{i}'
                remote_seed = work / f'remote-content-{i}'
                original_seed.write_text('LOCAL_ORIGINAL')
                remote_seed.write_text('REMOTE_DIFFERENT')
            rel = Path('task') / f't{i // 100:06d}' / f'{i % 100:03d}'
            if i % 100 == 0:
                (dst / rel.parent).mkdir(parents=True, exist_ok=True)
                (src / rel.parent).mkdir(parents=True, exist_ok=True)
            os.link(original_seed, dst / rel)
            os.link(remote_seed, src / rel)
        for i in range(3000):
            os.link(remote, src / f'remote-{i:06d}')
        emit(phase='build', originals=args.originals, sender_files=args.originals + 3000,
             seconds=time.perf_counter() - start)
        real_run = subprocess.run
        real_scan = ss.unmarked_files
        measurements = {}

        def scan(*a, **kw):
            t = time.perf_counter()
            result = real_scan(*a, **kw)
            measurements['local_scan_seconds'] = time.perf_counter() - t
            measurements['protected'] = len(result[0])
            return result

        def adapter(cmd, **kw):
            t = time.perf_counter()
            if cmd[0] == 'ssh':
                r = real_run([sys.executable, '-c', ss.REMOTE_PY_MANIFEST, str(src)], **kw)
                measurements['hub_scan_seconds'] = time.perf_counter() - t
                measurements['manifest_bytes'] = len(r.stdout)
                return r
            listed = [p for p in kw['input'].split('\0') if p]
            # All sender originals must be filtered, not merely checksum-skipped.
            assert not any(p.startswith('task/') and len(p.split('/')) == 3 for p in listed)
            measurements['rsync_entries'] = len(listed)
            r = real_run([str(x).removeprefix('hub:') for x in cmd], **kw)
            measurements['rsync_seconds'] = time.perf_counter() - t
            return r

        opts = SimpleNamespace(verbose=False, dry_run=False, bwlimit=None)
        with patch.object(ss, 'unmarked_files', side_effect=scan), \
             patch.object(ss.subprocess, 'run', side_effect=adapter), \
             patch.object(ss, '_watch_ssh_e', return_value=[]):
            for cycle in (1, 2):
                start = time.perf_counter()
                ok, moved = ss.watch_rsync('cfg', 'hub', str(dst), str(src), opts, 'pull', gid)
                emit(phase='full-pull', cycle=cycle, ok=ok, moved=moved,
                     seconds=time.perf_counter() - start, **measurements)
                assert ok
        assert (dst / 'task/t000000/000').read_text() == 'LOCAL_ORIGINAL'
        assert (dst / 'task/t000000/000').stat().st_ino == original.stat().st_ino
        assert (dst / 'remote-000000').read_text() == 'REMOTE_DIFFERENT'
        assert (dst / 'remote-000000').stat().st_gid == gid
    finally:
        safe_rmtree(work, authorized_root=base)


if __name__ == '__main__':
    main()
