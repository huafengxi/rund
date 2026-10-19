"""Deterministic owned-poll/exit sampling regression; real wait is never mocked.

Reuse only the retained Linux fixture/strong-identity cleanup harness. The first
real poll(None) is delayed until the cooperative foreground child is a zombie.
load_tests deliberately selects NEW cases only (the original 43 run separately).
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from runtime import test_lifecycle as fixtures
from runtime.kernel import proc, same_identity, read_json, group_members
from runtime.lifecycle import Runtime


class OwnedWaitTests(fixtures.LifecycleTests):
    def record(self, event, **data):
        row = {'event': event, 'at': time.monotonic(), **data}
        self.evidence.append(row)
        with (self.area/'trace.jsonl').open('a') as f:
            f.write(json.dumps(row) + '\n')

    def arm(self, r, run, work, release='release'):
        k = r.kernel
        key = k.key(run)
        p = k.processes[key]
        original = p.poll
        identity = copy.deepcopy(run)
        calls = []
        self.addCleanup(setattr, p, 'poll', original)
        def poll():
            rc = original()  # always a real nonblocking wait, including retries
            calls.append(rc)
            self.record('real-poll', rc=rc, identity=proc(p.pid),
                        owner=os.getpid(), payload=p.pid, argv=p.args,
                        ownedWait=key in k.processes)
            if len(calls) == 1:
                self.assertIsNone(rc)
                self.assertTrue(same_identity(proc(p.pid), identity))
                (work/release).touch()
                fixtures.wait_for(lambda: (a:=proc(p.pid)) and a['state']=='Z', seconds=3)
                self.record('delayed-None-return', identity=proc(p.pid),
                            ownedWait=key in k.processes, leader_snapshot=proc(p.pid),
                            ready=read_json(work/'ready'))
            return rc  # never substitutes an exit status
        p.poll = poll
        return p, calls

    def raced_exit(self, code, seconds=10):
        r = self.runtime()
        d = self.declaration(code=code)
        run = r.accept_task(d)
        work = self.ready(run)
        p, calls = self.arm(r, run, work)
        waited = []
        r.kernel.hook = lambda event, run: waited.append(event)
        before = time.monotonic()
        done = r.wait_task('demo', 'a1', seconds)
        self.record('wait-return', elapsed=time.monotonic()-before, result=done,
                    calls=calls, waited=waited, actual=proc(p.pid),
                    result_exists=(r.kernel.run_dir(run)/'result.json').exists())
        self.assertEqual((done['phase'], done['exitCode'], done['waitObserved']),
                         ('succeeded' if code == 0 else 'failed', code, True))
        self.assertEqual(calls, [None, code])
        self.assertEqual(waited, ['waited'])
        self.assertNotIn(r.kernel.key(run), r.kernel.processes)
        self.assertNotIn('leaseBusy', done['observation'])
        self.assertFalse(any(m['state'] not in ('Z','X') for m in done['observation']['members']))
        result = r.kernel.run_dir(run)/'result.json'
        saved = (result.read_bytes(), result.stat().st_ino)
        self.assertEqual(r.accept_task(d), done)
        self.assertEqual(r.start_task('demo','a1'), done)
        self.assertEqual(r.observe_task('demo','a1'), done)
        self.assertEqual((result.read_bytes(), result.stat().st_ino), saved)
        self.assertEqual(len(list(self.root.glob('work/Task/demo/a1/*/ready'))), 1)

    def test_owned_exit_zero(self):
        self.raced_exit(0)

    def test_owned_exit_nonzero(self):
        self.raced_exit(7)

    def test_owned_zero_deadline(self):
        # wait_task has always performed one observation even at deadline zero.
        self.raced_exit(0, 0)

    def test_owned_tiny_deadline(self):
        self.raced_exit(9, .000001)

    def test_owned_live_deadlines(self):
        r = self.runtime(); run = r.accept_task(self.declaration(mode='hold')); self.ready(run)
        p = r.kernel.processes[r.kernel.key(run)]; original = p.poll
        calls = []
        def poll():
            rc = original(); calls.append(rc); return rc
        with patch.object(p, 'poll', poll):
            for seconds in (0, .000001):
                before = time.monotonic(); done = r.wait_task('demo','a1',seconds)
                self.assertLess(time.monotonic()-before, 1)
                self.assertEqual(done['phase'], 'running')
                self.assertFalse(done['waitObserved'])
        self.assertEqual(calls, [None, None])  # no second poll for live leader
        self.assertTrue(same_identity(proc(run['pid']),run))
        self.record('live-deadlines', result=done, calls=calls)

    def test_owned_descendant_blocks_without_private_fd(self):
        r = self.runtime(); run = r.accept_task(self.declaration(mode='leader-exit'))
        work = self.ready(run); child = read_json(work/'child-ready')
        self.arm(r, run, work, 'exit-leader')
        blocked = r.wait_task('demo','a1',10)
        self.record('descendant-blocked', result=blocked, child=proc(child['pid']))
        self.assertEqual(blocked['phase'],'cleanupUnknown')
        self.assertEqual((blocked['observation']['exitCode'], blocked['observation']['waitObserved']), (0, True))
        self.assertTrue(any(m['pid']==child['pid'] and m['state'] not in ('Z','X')
                            for m in blocked['observation']['members']))
        with patch('runtime.kernel.os.killpg', side_effect=AssertionError('no group kill authority')):
            stopped = r.control(self.ctl(),term_seconds=0,kill_seconds=0)
        self.assertEqual(stopped['phase'],'cleanupUnknown')
        self.assertTrue(same_identity(proc(child['pid']), child))
        self.assertFalse((r.kernel.run_dir(run)/'result.json').exists())
        self.independent_kill(child)  # separate harness wait never goes to runtime
        done = r.observe_task('demo','a1')
        self.assertEqual((done['phase'],done['exitCode'],done['waitObserved']),('succeeded',0,True))

    def guarded_race(self, fault):
        r = self.runtime(); run = r.accept_task(self.declaration()); work = self.ready(run)
        p, calls = self.arm(r,run,work)
        if fault == 'mismatch':
            run['procStart'] += 1; r.kernel.save(run)
            # arm checks the original immutable identity from the prior run.
        if fault == 'boot':
            run['bootId'] = None; r.kernel.save(run)
        if fault == 'unavailable':
            original_proc = proc
            def unavailable(pid):
                if pid == p.pid: raise PermissionError('owned observation fixture')
                return original_proc(pid)
            context = patch('runtime.kernel.proc', side_effect=unavailable)
        elif fault == 'group-unavailable':
            context = patch('runtime.kernel.group_members', side_effect=PermissionError('owned group fixture'))
        else:
            from contextlib import nullcontext
            context = nullcontext()
        with context, patch('runtime.kernel.os.killpg', side_effect=AssertionError('unknown PGID')):
            blocked = r.wait_task('demo','a1',0)
        self.record('guard-blocked', fault=fault, result=blocked, calls=calls)
        self.assertEqual(blocked['phase'],'cleanupUnknown')
        self.assertFalse(blocked['waitObserved'])
        self.assertEqual(calls, [None])
        self.assertIn(r.kernel.key(run),r.kernel.processes)
        self.assertFalse((r.kernel.run_dir(run)/'result.json').exists())

    def test_owned_mismatch(self): self.guarded_race('mismatch')
    def test_owned_boot_unknown(self): self.guarded_race('boot')
    def test_owned_read_unavailable(self): self.guarded_race('unavailable')
    def test_owned_group_unavailable(self): self.guarded_race('group-unavailable')

    def test_owned_ambiguous_group(self):
        r=self.runtime(); run=r.accept_task(self.declaration()); work=self.ready(run)
        p,calls=self.arm(r,run,work)
        real_group=group_members
        def ambiguous(recorded):
            return [dict(m, session=m['session']+1) for m in real_group(recorded)]
        with patch('runtime.kernel.group_members',side_effect=ambiguous):
            blocked=r.wait_task('demo','a1',0)
        self.record('ambiguous-group', result=blocked, calls=calls)
        self.assertEqual(calls,[None])
        self.assertEqual(blocked['reason'],'group-identity-ambiguous')
        self.assertFalse(blocked['waitObserved'])

    def test_owned_resample_unavailable_after_wait(self):
        r=self.runtime(); run=r.accept_task(self.declaration()); work=self.ready(run)
        p,calls=self.arm(r,run,work)
        def group(recorded):
            if len(calls)==2: raise PermissionError('post-wait group sample unavailable')
            return group_members(recorded)
        with patch('runtime.kernel.group_members',side_effect=group):
            blocked=r.wait_task('demo','a1',0)
        self.record('post-wait-unavailable', result=blocked, calls=calls)
        self.assertEqual(calls,[None,0])
        self.assertEqual(blocked['phase'],'cleanupUnknown')
        self.assertTrue(blocked['observation']['waitObserved'])
        self.assertFalse((r.kernel.run_dir(run)/'result.json').exists())

    def test_owned_second_real_poll_none(self):
        # Synthetic dead-state *sample* while the child is really barrier-live;
        # both poll returns remain real None. Not a product red/green trace.
        r = self.runtime(); run = r.accept_task(self.declaration(mode='hold')); self.ready(run)
        p = r.kernel.processes[r.kernel.key(run)]; original = p.poll; calls=[]
        def poll():
            rc=original(); calls.append(rc); return rc
        def stale(pid):
            actual=proc(pid)
            if pid==p.pid and actual: actual=dict(actual,state='Z')
            return actual
        with patch.object(p,'poll',poll), patch('runtime.kernel.proc',side_effect=stale):
            blocked=r.wait_task('demo','a1',0)
        self.record('synthetic-state-real-None', calls=calls, result=blocked, actual=proc(p.pid))
        self.assertEqual(calls,[None,None])
        self.assertEqual(blocked['phase'],'cleanupUnknown')
        self.assertFalse(blocked['waitObserved'])
        self.assertEqual(blocked['reason'],'owned-wait-unavailable')
        self.assertTrue(same_identity(proc(run['pid']),run))

    def lost_owner(self, descendant):
        d = self.declaration(mode='leader-exit' if descendant else 'exit')
        self.crash_driver(d)  # real other OS coordinator killed, no local Popen
        run = read_json(self.root/'state/runs/Task/demo/a1/1/run.json')
        work = Path(run['cwd']); (work/('exit-leader' if descendant else 'release')).touch()
        fixtures.wait_for(lambda: (a:=proc(run['pid'])) and a['state']=='Z', seconds=3)
        r = self.runtime()
        self.assertEqual(r.kernel.processes,{})
        with patch('runtime.kernel.os.killpg',side_effect=AssertionError('no blind group kill')):
            done = r.wait_task('demo','a1',0)
        if descendant:
            self.assertEqual(done['phase'],'cleanupUnknown')
            self.independent_kill(read_json(work/'child-ready'))
            done=r.observe_task('demo','a1')
        self.record('lost-owner', descendant=descendant, result=done)
        self.assertEqual((done['phase'],done['exitCode'],done['signal'],done['waitObserved']),
                         ('lost',None,None,False))
        self.assertEqual(r.accept_task(d),done)
        self.assertEqual(len(list(self.root.glob('work/Task/demo/a1/*/ready'))),1)

    def test_owned_lost_owner(self): self.lost_owner(False)
    def test_owned_lost_owner_descendant(self): self.lost_owner(True)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(OwnedWaitTests(name) for name in sorted(OwnedWaitTests.__dict__)
                              if name.startswith('test_owned_'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
