#!/usr/bin/env python3
"""Retained, isolated Linux lifecycle tests. No production processes or cleanup roots.

RUND_LIFECYCLE_OUTPUT must be a NEW absolute directory (or a new mkdtemp is
allocated). All processes are this suite's children/descendants. Harness owns
subreaper adoption, independent strong identities and pidfds for cleanup; it
does not rely on the candidate's stop implementation to clean up a failure.
"""
import copy
import ctypes
import fcntl
import json
import os
import platform
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from runtime.kernel import proc, same_identity, read_json, publish
from runtime.lifecycle import Runtime, TASK_FINAL

BASE = Path(os.environ['RUND_LIFECYCLE_OUTPUT']) if 'RUND_LIFECYCLE_OUTPUT' in os.environ else Path(tempfile.mkdtemp(prefix='rund-lifecycle-'))
if 'RUND_LIFECYCLE_OUTPUT' in os.environ:
    BASE.mkdir(parents=True, exist_ok=False)
for n in ('home', 'tmp', 'neutral cwd'):
    (BASE / n).mkdir()
ENV = {'HOME': str(BASE / 'home'), 'TMPDIR': str(BASE / 'tmp'),
       'PATH': str(Path(sys.executable).parent) + os.pathsep + os.defpath,
       'PYTHONPATH': str(REPO), 'PYTHONDONTWRITEBYTECODE': '1', 'LC_ALL': 'C.UTF-8'}
os.environ.clear()
os.environ.update(ENV)
os.chdir(BASE / 'neutral cwd')
# Only this isolated harness adopts its own orphaned fixture descendants.
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'test subreaper unavailable')
# This Conda build lacks os.pidfd_open/signal.pidfd_send_signal. Use the
# documented Linux syscall ABI only on these architectures, and preflight it
# BEFORE creating any payload. No weak PID-only cleanup fallback.
if platform.machine() not in ('x86_64', 'aarch64'):
    raise RuntimeError('harness pidfd syscall ABI unverified on this architecture')
libc.syscall.restype = ctypes.c_long


def pidfd_open(pid):
    fd = libc.syscall(434, pid, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'pidfd_open')
    return fd


def pidfd_signal(fd, sig):
    if libc.syscall(424, fd, sig, ctypes.c_void_p(0), 0) != 0:
        raise OSError(ctypes.get_errno(), 'pidfd_send_signal')


_preflight = pidfd_open(os.getpid())
try:
    pidfd_signal(_preflight, 0)
finally:
    os.close(_preflight)


def command(argv, good=True, env=None):
    p = subprocess.run([str(a) for a in argv], env=env or ENV, cwd=BASE / 'neutral cwd',
                       capture_output=True, text=True, timeout=120)
    with (BASE / 'commands.jsonl').open('a') as f:
        f.write(json.dumps({'argv': [str(a) for a in argv], 'env': env or ENV,
                            'cwd': str(BASE / 'neutral cwd'), 'rc': p.returncode,
                            'stdout': p.stdout, 'stderr': p.stderr}) + '\n')
    if good and p.returncode:
        raise AssertionError(p.stderr)
    return p


def wait_for(fn, seconds=5):
    deadline = time.monotonic() + seconds
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError('barrier/observation deadline exceeded')
        time.sleep(0.005)


DRIVER = r'''
import json, sys, time
from pathlib import Path
from runtime.lifecycle import Runtime
from runtime.kernel import publish
root, decl, barrier, event = sys.argv[1:]
b = Path(barrier)
def hook(at, run):
    if at == event:
        publish(b/'hook.json', {'event':at, 'run':run})
        while not (b/'continue').exists(): time.sleep(.005)
d=json.loads(Path(decl).read_text())
with Runtime(root, 'node-a', hook=hook) as r:
    if d['kind']=='Task':
        run=r.accept_task(d)
        if event=='waited': r.wait_task(d['metadata']['id'], d['spec']['attemptId'], 60)
    else: run=r.submit_service(d, 'driver-service')
    publish(b/'returned.json', run)
    while not (b/'finish').exists(): time.sleep(.005)
'''


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BASE / 'immutable synthetic source'
        cls.source.mkdir()
        command(['git', '-C', cls.source, 'init', '-q', '--template='])
        (cls.source / 'fixture.py').write_bytes((REPO / 'runtime/synthetic_payload.py').read_bytes())
        command(['git', '-C', cls.source, 'add', '--', 'fixture.py'])
        message = BASE / 'fixture-message'; message.write_text('68wblg isolated immutable foreground fixture\n')
        command(['git', '-C', cls.source, '-c', 'user.name=Synthetic', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-q', '-F', message, '--', 'fixture.py'])
        cls.pin = command(['git', '-C', cls.source, 'rev-parse', 'HEAD']).stdout.strip()
        (BASE / 'environment.json').write_text(json.dumps({'python':sys.executable, 'env':ENV,
                 'sourceCommit':cls.pin, 'source':str(cls.source), 'subreaper':True}, indent=2))

    def setUp(self):
        self.area = BASE / self.id().split('.')[-1]; self.area.mkdir()
        self.root = self.area / 'node root'
        self.identities, self.coordinators, self.runtimes = {}, [], []
        self.evidence = []

    def runtime(self):
        r = Runtime(self.root, 'node-a')
        self.runtimes.append(r)
        return r

    def declaration(self, kind='Task', mode='exit', code=0, id='demo', attempt='a1', request='release-1', policy='manual'):
        spec = {'node':'node-a', 'sources':[{'alias':'main', 'repository':self.source.as_uri(), 'revision':self.pin, 'requires':[]}],
                'prepare':[], 'toolchain':{'python':sys.executable}, 'cwd':'${work}', 'env':{'SYNTHETIC_VALUE':'literal ; not shell'},
                'credentialRefs':[], 'argv':[sys.executable, '${code.main}/fixture.py', mode, str(code)]}
        if kind == 'Task':
            spec.update(attemptId=attempt, requestId=request)
        else:
            spec.update(desired='online', restart={'policy':policy, 'initialSeconds':.1, 'maxSeconds':.2})
        return {'apiVersion':'rund/v1alpha1', 'kind':kind, 'metadata':{'id':id, 'version':1}, 'spec':spec}

    def work(self, kind='Task', id='demo', attempt='a1', gen=1):
        return self.root / 'work' / kind / id / attempt / str(gen)

    def remember(self, identity):
        self.identities[identity['pid']] = identity
        return identity

    def ready(self, run=None, *, kind='Task', id='demo', attempt='a1', gen=1):
        if run:
            kind, id, attempt, gen = (run[k] for k in ('kind','id','attemptId','generation'))
        work = self.work(kind,id,attempt,gen)
        wait_for(lambda: (work / 'ready').exists())
        record = self.remember(read_json(work / 'ready'))
        if (work / 'child-ready').exists():
            self.remember(read_json(work / 'child-ready'))
        self.evidence.append({'event':'fixture-ready', 'at':time.time(), 'identity':record})
        return work

    def ctl(self, kind='Task', action='cancel', request='cancel-1', gen=1, attempt='a1', id='demo'):
        return {'apiVersion':'rund-control/v1alpha1', 'requestId':request, 'action':action,
                'kind':kind, 'id':id, 'attemptId':attempt if kind=='Task' else 'service', 'expectedGeneration':gen}

    def independent_kill(self, identity):
        actual = proc(identity['pid'])
        self.evidence.append({'event':'harness-cleanup-before', 'at':time.time(), 'recorded':identity, 'actual':actual})
        if actual and actual['state'] not in ('Z','X'):
            self.assertTrue(same_identity(actual,identity), 'harness refuses identity mismatch')
            fd = pidfd_open(identity['pid'])
            try:
                self.assertTrue(same_identity(proc(identity['pid']),identity))
                pidfd_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        wait_for(lambda: (p:=proc(identity['pid'])) is None or p['state'] in ('Z','X'))
        try:
            os.waitpid(identity['pid'], os.WNOHANG)
        except ChildProcessError:
            pass
        self.evidence.append({'event':'harness-cleanup-after', 'at':time.time(), 'pid':identity['pid'], 'actual':proc(identity['pid'])})

    def crash_driver(self, d, event='recorded'):
        decl = self.area / 'driver-declaration.json'; decl.write_text(json.dumps(d))
        b = self.area / 'barrier'; b.mkdir()
        log = (self.area / 'coordinator.log').open('w')
        argv = [sys.executable,'-B','-c',DRIVER,str(self.root),str(decl),str(b),event]
        p = subprocess.Popen(argv, env=ENV, cwd=BASE/'neutral cwd', stdout=log, stderr=subprocess.STDOUT)
        self.coordinators.append((p,log))
        self.evidence.append({'event':'driver-spawn', 'argv':argv, 'env':ENV, 'pid':p.pid, 'identity':proc(p.pid)})
        if event == 'waited':
            work = self.ready()
            (work/'release').touch()
        wait_for(lambda: (b/'hook.json').exists() or p.poll() is not None)
        self.assertIsNone(p.poll(), (self.area/'coordinator.log').read_text())
        hook = read_json(b/'hook.json')
        if event != 'starting':
            self.ready(kind=d['kind'],attempt='a1' if d['kind']=='Task' else 'service')
        p.kill(); self.assertEqual(p.wait(timeout=5), -signal.SIGKILL)
        self.evidence.append({'event':'coordinator-killed-at-barrier','at':time.time(),'hook':hook, 'rc':p.returncode})
        return hook

    def tearDown(self):
        # Snapshot BEFORE cleanup (including failures); then stop only this test's
        # exact child identities, without candidate stop or blind group killing.
        for p, log in self.coordinators:
            if p.poll() is None:
                # Enumerate only the known coordinator's direct children while
                # it is still alive, even when payload readiness failed.
                children = Path('/proc')/str(p.pid)/'task'/str(p.pid)/'children'
                if children.exists():
                    for pid in children.read_text().split():
                        identity = proc(int(pid))
                        if identity:
                            self.remember(identity)
                p.kill(); p.wait(timeout=5)
            log.close()
        # Discover this test's deterministic fixture barriers even if assertions
        # failed between spawn and ready. Never scan production processes by name.
        for _ in range(2):
            for name in ('ready','child-ready'):
                for path in self.root.glob('work/*/*/*/*/'+name):
                    self.remember(read_json(path))
            time.sleep(.02)
        self.evidence.append({'event':'before-cleanup', 'at':time.time(),
                              'identities':{pid:proc(pid) for pid in self.identities}})
        cleanup_errors = []
        for identity in list(self.identities.values()):
            try:
                self.independent_kill(identity)
            except BaseException as e:
                cleanup_errors.append(repr(e))
        for r in self.runtimes:
            for p in list(r.kernel.processes.values()):
                # Popen is independently owned even if fixture failed before ready.
                if p.poll() is None:
                    identity = proc(p.pid)
                    if identity:
                        self.independent_kill(identity)
                p.wait(timeout=5)
            r.kernel.close()
        # Reap adopted dead children; only saved identities, never waitpid(-1).
        for pid in self.identities:
            try: os.waitpid(pid, os.WNOHANG)
            except ChildProcessError: pass
        live = {pid:proc(pid) for pid in self.identities if proc(pid) is not None}
        self.evidence.append({'event':'final-cleanup', 'at':time.time(), 'remaining':live, 'errors':cleanup_errors})
        (self.area/'evidence.json').write_text(json.dumps(self.evidence,indent=2))
        self.assertEqual(cleanup_errors, [])
        self.assertEqual(live, {}, 'test descendants must be exited and reaped')

    def test_01_task_success_and_execution_contract(self):
        with self.runtime() as r:
            run = r.accept_task(self.declaration())
            work = self.ready(run)
            ready = read_json(work/'ready')
            self.assertEqual(ready['cwd'],str(work))
            self.assertEqual(set(ready['environment']), {'PATH','HOME','TMPDIR','LC_ALL','SYNTHETIC_VALUE','RUND_LEASE_FD'})
            self.assertEqual(ready['environment']['SYNTHETIC_VALUE'],'literal ; not shell')
            lease = r.kernel.run_dir(run)/'lease.lock'
            self.assertEqual(ready['leaseInode'],lease.stat().st_ino)
            self.assertEqual(ready['leaseDev'],lease.stat().st_dev)
            self.assertTrue(r.kernel.lease_busy(run))
            self.assertEqual((Path('/proc')/str(run['pid'])/'fd'/str(ready['leaseFd'])).stat().st_ino,lease.stat().st_ino)
            (work/'release').touch()
            done = r.wait_task('demo','a1')
            self.assertEqual((done['phase'],done['exitCode'],done['signal'],done['waitObserved']),('succeeded',0,None,True))
            self.assertEqual([e['phase'] for e in done['history']],['accepted','preparing','starting','running','succeeded'])
            self.assertEqual(done['sourceCommits'],{'main':self.pin})
            self.assertEqual(len(done['prepareKey']),64)
            self.assertIn('synthetic stdout',Path(done['stdout']).read_text())
            self.assertIn('synthetic stderr',Path(done['stderr']).read_text())
            self.assertFalse(list((self.root/'protocol').iterdir()))
            self.evidence.append({'result':done})

    def test_02_nonzero_and_terminal_absorption(self):
        with self.runtime() as r:
            d=self.declaration(code=7); run=r.accept_task(d); work=self.ready(run); (work/'release').touch()
            done=r.wait_task('demo','a1'); self.assertEqual((done['phase'],done['exitCode']),('failed',7))
            result=r.kernel.run_dir(done)/'result.json'; before=result.read_bytes(); inode=result.stat().st_ino
            self.assertEqual(r.accept_task(d),done)
            self.assertEqual(r.start_task('demo','a1'),done)
            self.assertTrue(r.control(self.ctl())['noop'])
            with self.assertRaisesRegex(ValueError,'immutable'):
                r.kernel.transition(done,'running','illegal')
            with self.assertRaisesRegex(ValueError,'overwritten'):
                publish(result,{'forged':True},final=True)
            self.assertEqual((result.read_bytes(),result.stat().st_ino),(before,inode))

    def test_03_prepare_failure_never_spawns(self):
        with self.runtime() as r:
            d=self.declaration(); d['spec']['sources'][0]['revision']='f'*40
            done=r.accept_task(d)
            self.assertEqual(done['phase'],'failed'); self.assertTrue(done['reason'].startswith('prepare:'))
            self.assertIsNone(done['pid']); self.assertIsNone(done['exitCode'])
            self.assertFalse(list((self.root/'work').iterdir()))
            self.assertEqual(r.accept_task(d),done)

    def test_04_cancel_before_start_and_explicit_rerun(self):
        with self.runtime() as r:
            d=self.declaration(); run=r.accept_task(d,start=False)
            self.assertEqual(run['phase'],'accepted')
            self.assertEqual(r.accept_task(d)['phase'],'accepted','duplicate release must not start accepted run')
            done=r.control(self.ctl()); self.assertEqual(done['phase'],'canceled'); self.assertIsNone(done['pid'])
            d2=self.declaration(attempt='a2',request='release-2')
            with self.assertRaisesRegex(ValueError,'explicit rerun'): r.accept_task(d2)
            oldbytes=(r.kernel.run_dir(done)/'result.json').read_bytes()
            run=r.rerun_task('demo','a1',d2); work=self.ready(run)
            # A delayed cancel of the old attempt cannot affect the new attempt.
            self.assertTrue(r.control(self.ctl(request='late-old'))['noop'])
            self.assertIsNotNone(proc(run['pid']))
            (work/'release').touch(); self.assertEqual(r.wait_task('demo','a2')['phase'],'succeeded')
            self.assertEqual((r.kernel.run_dir(done)/'result.json').read_bytes(),oldbytes)

    def test_05_duplicate_conflict_and_late_generation(self):
        with self.runtime() as r:
            d=self.declaration(mode='hold'); run=r.accept_task(d); self.ready(run)
            self.assertEqual(r.accept_task(d)['pid'],run['pid'])
            conflict=copy.deepcopy(d); conflict['spec']['argv'][-1]='9'
            with self.assertRaisesRegex(ValueError,'digest conflict'): r.accept_task(conflict)
            other=self.declaration(id='other',request='release-1')
            with self.assertRaisesRegex(ValueError,'requestId conflicts'): r.accept_task(other)
            self.assertTrue(r.control(self.ctl(gen=99))['noop'])
            self.assertIsNotNone(proc(run['pid']))
            done=r.control(self.ctl(request='valid-cancel'))
            self.assertEqual(done['phase'],'canceled')
            self.assertEqual(r.control(self.ctl(request='valid-cancel')),done)

    def test_06_term_group_confirmation(self):
        with self.runtime() as r:
            run=r.accept_task(self.declaration(mode='group')); work=self.ready(run)
            child=read_json(work/'child-ready')
            self.assertEqual(child['leaseInode'],read_json(work/'ready')['leaseInode'])
            self.assertEqual((Path('/proc')/str(child['pid'])/'fd'/str(child['leaseFd'])).stat().st_ino,child['leaseInode'])
            done=r.control(self.ctl(),term_seconds=1)
            self.assertEqual(done['phase'],'canceled'); self.assertEqual(done['exitCode'],0)
            self.assertEqual([e['signal'] for e in done['stopEvidence'] if e['event']=='signal'],[signal.SIGTERM])
            self.assertEqual(done['stopEvidence'][-1]['status'],'exited')
            self.assertFalse(done['stopEvidence'][-1]['leaseBusy'])
            self.assertFalse(any(m['state'] not in ('Z','X') for m in done['stopEvidence'][-1]['members']))
            self.assertIsNone(proc(child['pid']))
            self.assertTrue((work/'child-exit').exists())
            self.evidence.append({'stopResult':done,'fixtureExit':read_json(work/'exit'),'childExit':read_json(work/'child-exit')})

    def test_07_ignored_term_requires_kill(self):
        with self.runtime() as r:
            run=r.accept_task(self.declaration(mode='ignore')); self.ready(run)
            done=r.control(self.ctl(),term_seconds=.05)
            self.assertEqual((done['phase'],done['exitCode'],done['signal'],done['waitObserved']),('canceled',None,9,True))
            self.assertEqual([e['signal'] for e in done['stopEvidence'] if e['event']=='signal'],[15,9])
            self.assertEqual(done['stopEvidence'][-1]['status'],'exited')
            self.evidence.append({'stopResult':done})

    def test_08_natural_exit_wins_cancel_race(self):
        with self.runtime() as r:
            run=r.accept_task(self.declaration()); work=self.ready(run); (work/'release').touch()
            wait_for(lambda:(p:=proc(run['pid'])) is None or p['state']=='Z')
            done=r.control(self.ctl())
            self.assertEqual(done['phase'],'succeeded'); self.assertEqual(done['exitCode'],0)
            self.assertFalse(any(e['event']=='signal' for e in done['stopEvidence']))
            failed=r.accept_task(self.declaration(id='fail-race',request='fail-race',code=11))
            work=self.ready(failed); (work/'release').touch()
            wait_for(lambda:(p:=proc(failed['pid'])) is None or p['state']=='Z')
            failed=r.control(self.ctl(id='fail-race',request='fail-race-cancel'))
            self.assertEqual((failed['phase'],failed['exitCode']),('failed',11))
            self.assertFalse(any(e['event']=='signal' for e in failed['stopEvidence']))

    def test_09_identity_mismatch_refuses_signal(self):
        with self.runtime() as r:
            run=r.accept_task(self.declaration(mode='hold')); self.ready(run)
            original=copy.deepcopy(run); run['procStart']+=1; r.kernel.save(run)
            blocked=r.control(self.ctl())
            self.assertEqual(blocked['phase'],'cleanupUnknown'); self.assertEqual(blocked['reason'],'identity-mismatch')
            self.assertFalse(any(e['event']=='signal' for e in blocked['stopEvidence']))
            self.assertTrue(same_identity(proc(original['pid']),original))
            self.evidence.append({'refusedResult':blocked,'actual':proc(original['pid'])})
            r.kernel.save(original)
            self.assertEqual(r.control(self.ctl(request='correct-cancel'))['phase'],'canceled')

    def test_10_leader_exit_child_blocks_no_blind_kill(self):
        with self.runtime() as r:
            run=r.accept_task(self.declaration(mode='leader-exit')); work=self.ready(run)
            child=read_json(work/'child-ready'); (work/'exit-leader').touch()
            wait_for(lambda:(p:=proc(run['pid'])) is None or p['state']=='Z')
            blocked=r.observe_task('demo','a1'); self.assertEqual(blocked['phase'],'cleanupUnknown')
            blocked=r.control(self.ctl(),term_seconds=.01,kill_seconds=.01)
            self.assertEqual(blocked['phase'],'cleanupUnknown')
            self.assertFalse(any(e['event']=='signal' for e in blocked['stopEvidence']))
            self.assertTrue(same_identity(proc(child['pid']),child))
            self.evidence.append({'blocked':blocked,'child':proc(child['pid'])})
            self.independent_kill(child)
            done=r.observe_task('demo','a1'); self.assertEqual((done['phase'],done['exitCode']),('succeeded',0))

    def test_11_node_lock_rejects_second_coordinator(self):
        with self.runtime() as r:
            run=r.accept_task(self.declaration(mode='hold')); self.ready(run)
            p=command([sys.executable,'-B','-c','from runtime.lifecycle import Runtime; Runtime(__import__("sys").argv[1],"node-a")',self.root],good=False)
            self.assertNotEqual(p.returncode,0); self.assertIn('BlockingIOError',p.stderr)
            self.assertTrue(same_identity(proc(run['pid']),run))
            self.assertEqual(r.control(self.ctl())['phase'],'canceled')
        with self.assertRaisesRegex(ValueError,'another node'): Runtime(self.root,'node-b')

    def test_12_fd_survives_owner_close_and_orphan_stop(self):
        r=self.runtime(); run=r.accept_task(self.declaration(mode='hold')); work=self.ready(run)
        r.kernel.close()
        with self.runtime() as replacement:
            observed=replacement.observe_task('demo','a1'); self.assertEqual(observed['phase'],'running')
            self.assertNotEqual(observed['ownerEpoch'],run['ownerEpoch'])
            self.assertTrue(replacement.kernel.lease_busy(observed),'actual inherited FD must fence a second lock open')
            self.assertEqual((Path('/proc')/str(run['pid'])/'fd'/str(read_json(work/'ready')['leaseFd'])).stat().st_ino,
                             (replacement.kernel.run_dir(run)/'lease.lock').stat().st_ino)
            done=replacement.control(self.ctl())
            self.assertEqual(done['phase'],'canceled'); self.assertIsNone(done['exitCode']); self.assertIsNone(done['signal'])
            self.assertFalse(done['waitObserved'],'replacement must not fabricate wait status')
            self.evidence.append({'orphanStop':done})

    def test_13_crash_recorded_orphan_dies_lost(self):
        self.crash_driver(self.declaration(mode='hold'))
        with self.runtime() as r:
            observed=r.observe_task('demo','a1'); self.assertEqual(observed['phase'],'running')
            self.independent_kill(self.identities[observed['pid']])
            done=r.observe_task('demo','a1'); self.assertEqual((done['phase'],done['exitCode'],done['signal']),('lost',None,None))
            self.assertEqual(r.accept_task(self.declaration(mode='hold')),done)
            self.assertFalse(done['waitObserved']); self.evidence.append({'lost':done})

    def test_14_crash_starting_before_spawn_no_replay(self):
        self.crash_driver(self.declaration(mode='hold'),'starting')
        with self.runtime() as r:
            done=r.observe_task('demo','a1')
            self.assertEqual(done['phase'],'lost'); self.assertIsNone(done['pid'])
            self.assertFalse(r.kernel.lease_busy(done))
            self.assertEqual(r.accept_task(self.declaration(mode='hold')),done)
            self.assertFalse(list(self.root.glob('work/*/*/*/*/ready')))
            self.evidence.append({'lost':done})

    def test_15_crash_spawned_before_record_busy_no_duplicate(self):
        self.crash_driver(self.declaration(mode='hold'),'spawned')
        with self.runtime() as r:
            blocked=r.observe_task('demo','a1'); self.assertEqual(blocked['phase'],'cleanupUnknown')
            self.assertIsNone(blocked['pid']); self.assertTrue(blocked['observation']['leaseBusy'])
            self.assertEqual(r.accept_task(self.declaration(mode='hold')),blocked)
            blocked=r.control(self.ctl()); self.assertEqual(blocked['phase'],'cleanupUnknown')
            self.assertFalse(any(e['event']=='signal' for e in blocked['stopEvidence']))
            self.assertEqual(len(self.identities),1)
            self.evidence.append({'startingBusy':blocked,'independentPayload':list(self.identities.values())})
            self.independent_kill(next(iter(self.identities.values())))
            done=r.observe_task('demo','a1'); self.assertEqual(done['phase'],'lost'); self.assertIsNone(done['exitCode'])

    def test_16_crash_after_real_wait_before_publication_lost(self):
        self.crash_driver(self.declaration(),'waited')
        with self.runtime() as r:
            done=r.observe_task('demo','a1'); self.assertEqual(done['phase'],'lost')
            self.assertIsNone(done['exitCode']); self.assertFalse(done['waitObserved'])
            self.evidence.append({'lost':done})

    def test_17_service_manual_no_restart(self):
        with self.runtime() as r:
            d=self.declaration('Service',code=8); s=r.submit_service(d,'service-release'); work=self.ready(kind='Service',attempt='service')
            (work/'release').touch(); wait_for(lambda:(p:=proc(r._service_current(s)['pid'])) is None or p['state']=='Z')
            s=r.tick_service('demo'); self.assertEqual(s['phase'],'exited'); self.assertEqual(s['generation'],1)
            for _ in range(3): self.assertEqual(r.tick_service('demo')['generation'],1)
            self.assertEqual(r.submit_service(d,'service-release')['generation'],1)
            s=r.control(self.ctl('Service','restart',request='restart-1'))
            self.assertEqual(s['generation'],2); self.ready(kind='Service',attempt='service',gen=2)
            self.assertEqual(r.control(self.ctl('Service','restart',request='restart-1'))['generation'],2)
            self.assertTrue(r.control(self.ctl('Service','stop',request='late-stop',gen=1))['noop'])
            s=r.control(self.ctl('Service','stop',request='stop-2',gen=2))
            self.assertEqual(s['phase'],'stopped'); self.assertEqual(r.tick_service('demo')['generation'],2)

    def test_18_service_on_failure_bounded_backoff_success_stops(self):
        with self.runtime() as r:
            d=self.declaration('Service',code=6,policy='on-failure'); s=r.submit_service(d,'service-release')
            for generation,delay in ((1,.1),(2,.2),(3,.2)):
                work=self.ready(kind='Service',attempt='service',gen=generation); (work/'release').touch()
                run=r._service_current(s); wait_for(lambda:(p:=proc(run['pid'])) is None or p['state']=='Z')
                s=r.tick_service('demo'); self.assertEqual(s['phase'],'backoff'); self.assertEqual(s['backoffSeconds'],delay)
                self.assertEqual(r.tick_service('demo')['generation'],generation)
                if generation<3:
                    with patch('runtime.lifecycle.time.time',return_value=s['nextStartAt']+.001): s=r.tick_service('demo')
            s=r.control(self.ctl('Service','stop',request='offline-backoff',gen=3))
            self.assertEqual(s['phase'],'stopped')
            with patch('runtime.lifecycle.time.time',return_value=time.time()+100): self.assertEqual(r.tick_service('demo')['generation'],3)
            good=self.declaration('Service',code=0,policy='on-failure'); good['metadata']['version']=2
            s=r.submit_service(good,'version-success'); self.assertEqual(s['generation'],4)
            work=self.ready(kind='Service',attempt='service',gen=4); (work/'release').touch()
            run=r._service_current(s); wait_for(lambda:(p:=proc(run['pid'])) is None or p['state']=='Z')
            self.assertEqual(r.tick_service('demo')['phase'],'exited')
            self.assertEqual(r.tick_service('demo')['generation'],4)
            self.evidence.append({'service':r.service('demo')})

    def test_19_service_version_prepare_first_offline_boundary(self):
        with self.runtime() as r:
            d=self.declaration('Service',mode='hold',policy='on-failure'); s=r.submit_service(d,'service-release'); self.ready(kind='Service',attempt='service')
            original=r._service_current(s)
            bad=copy.deepcopy(d); bad['metadata']['version']=2; bad['spec']['sources'][0]['revision']='f'*40
            self.assertTrue(r.submit_service(bad,'bad-version')['previousUnchanged'])
            self.assertTrue(same_identity(proc(original['pid']),original))
            new=copy.deepcopy(d); new['metadata']['version']=2
            s=r.submit_service(new,'version-2'); self.assertEqual(s['generation'],2); self.ready(kind='Service',attempt='service',gen=2)
            old=r.read_run('Service','demo','service',1); self.assertEqual(old['phase'],'stopped')
            self.assertFalse(old['stopEvidence'][-1]['leaseBusy'])
            self.assertIsNone(proc(original['pid']))
            self.assertTrue(r.control(self.ctl('Service','stop',request='old-stop'))['noop'])
            offline=copy.deepcopy(new); offline['metadata']['version']=3; offline['spec']['desired']='offline'
            s=r.submit_service(offline,'version-offline'); self.assertEqual(s['phase'],'stopped')
            self.assertEqual(r.tick_service('demo')['generation'],2)
            always=self.declaration('Service',policy='always',id='always')
            with self.assertRaisesRegex(ValueError,'always'): r.submit_service(always,'unsupported')
            self.assertFalse((self.root/'work/Service/always').exists())
            self.evidence.append({'oldResult':old,'offline':s})

    def test_20_service_leader_child_blocks_then_converges(self):
        with self.runtime() as r:
            d=self.declaration('Service',mode='leader-exit',policy='on-failure'); s=r.submit_service(d,'service-release')
            work=self.ready(kind='Service',attempt='service'); child=read_json(work/'child-ready'); run=r._service_current(s)
            (work/'exit-leader').touch(); wait_for(lambda:(p:=proc(run['pid'])) is None or p['state']=='Z')
            s=r.tick_service('demo'); self.assertEqual(s['phase'],'cleanupUnknown'); self.assertEqual(s['generation'],1)
            blocked=r.control(self.ctl('Service','restart',request='blocked-restart'))
            self.assertEqual(blocked['phase'],'cleanupUnknown'); self.assertEqual(blocked['generation'],1)
            self.assertTrue(same_identity(proc(child['pid']),child))
            self.evidence.append({'blockedRun':r._service_current(blocked),'service':blocked})
            self.independent_kill(child)
            s=r.tick_service('demo'); self.assertEqual(s['phase'],'stopped'); self.assertEqual(s['generation'],1)
            self.assertEqual(r.tick_service('demo')['generation'],1)

    def test_21_service_orphan_lost_only_restarts_after_convergence(self):
        d=self.declaration('Service',mode='hold',policy='on-failure'); self.crash_driver(d)
        with self.runtime() as r:
            s=r.tick_service('demo'); self.assertEqual(s['phase'],'running'); old=r._service_current(s)
            self.independent_kill(self.identities[old['pid']])
            s=r.tick_service('demo'); self.assertEqual(s['phase'],'backoff'); self.assertEqual(s['generation'],1)
            result=r.read_run('Service','demo','service',1)
            self.assertIsNone(result['exitCode']); self.assertIsNone(result['signal']); self.assertFalse(result['waitObserved'])
            self.assertFalse(result['observation']['leaseBusy'])
            with patch('runtime.lifecycle.time.time',return_value=s['nextStartAt']+.001): s=r.tick_service('demo')
            self.assertEqual(s['generation'],2); self.ready(kind='Service',attempt='service',gen=2)
            self.assertEqual(r.control(self.ctl('Service','stop',request='stop-new',gen=2))['phase'],'stopped')
            self.evidence.append({'oldResult':result,'newService':s})

    def test_22_service_starting_identity_unavailable_stays_blocked(self):
        d=self.declaration('Service',mode='hold',policy='on-failure'); self.crash_driver(d,'spawned')
        with self.runtime() as r:
            s=r.tick_service('demo'); self.assertEqual((s['phase'],s['generation']),('cleanupUnknown',1))
            self.independent_kill(next(iter(self.identities.values())))
            s=r.tick_service('demo'); self.assertEqual((s['phase'],s['generation']),('cleanupUnknown',1))
            new=copy.deepcopy(d); new['metadata']['version']=2
            blocked=r.submit_service(new,'new-version'); self.assertEqual(blocked['phase'],'cleanupUnknown')
            self.assertEqual(blocked['generation'],1)
            self.evidence.append({'blocked':blocked,'run':r._service_current(blocked)})

    def test_23_explicit_cli_clean_cwd_input_and_tool_failure(self):
        d=self.declaration(); decl=self.area/'declaration.json'; decl.write_text(json.dumps(d))
        # CLI accept + pre-start cancel are a finite explicit operation batch.
        operations=self.area/'operations.json'
        operations.write_text(json.dumps([{'action':'accept','declaration':str(decl)}, {'action':'control','control':self.ctl()}]))
        p=command([sys.executable,'-B','-m','runtime.cli','execute','--root',self.root,'--node','node-a','--operations',operations])
        values=json.loads(p.stdout); self.assertEqual(values[-1]['phase'],'canceled')
        self.assertFalse(list((self.root/'work').iterdir()))
        # Missing declared tool is a terminal failure without any payload.
        with self.runtime() as r:
            bad=self.declaration(id='tool-failure',request='tool-release'); bad['spec']['toolchain']['python']='no-such-python-68wblg'
            done=r.accept_task(bad); self.assertEqual(done['phase'],'failed'); self.assertIsNone(done['pid'])
        malformed=self.area/'invalid.json'; malformed.write_text('{"apiVersion":null}')
        operations.write_text(json.dumps([{'action':'release','declaration':str(malformed)}]))
        p=command([sys.executable,'-B','-m','runtime.cli','execute','--root',self.area/'invalid-node','--node','node-a','--operations',operations],good=False)
        self.assertEqual(p.returncode,2); self.assertFalse((self.area/'invalid-node').exists())

    def test_24_explicit_cli_actual_argv_wait(self):
        d=self.declaration(); decl=self.area/'declaration.json'; decl.write_text(json.dumps(d))
        operations=self.area/'operations.json'
        operations.write_text(json.dumps([{'action':'release','declaration':str(decl)},
                                          {'action':'wait','id':'demo','attemptId':'a1','seconds':10}]))
        stdout=(self.area/'cli-stdout.json').open('w'); stderr=(self.area/'cli-stderr.log').open('w')
        argv=[sys.executable,'-B','-m','runtime.cli','execute','--root',str(self.root),'--node','node-a','--operations',str(operations)]
        p=subprocess.Popen(argv,env=ENV,cwd=BASE/'neutral cwd',stdout=stdout,stderr=stderr)
        self.coordinators.append((p,stderr))
        work=self.ready(); (work/'release').touch()
        self.assertEqual(p.wait(timeout=15),0)
        stdout.close(); stderr.flush()
        values=read_json(self.area/'cli-stdout.json')
        self.assertEqual((values[-1]['phase'],values[-1]['exitCode'],values[-1]['waitObserved']),('succeeded',0,True))
        self.evidence.append({'argv':argv,'env':ENV,'cwd':str(BASE/'neutral cwd'),'rc':p.returncode,'result':values[-1]})

    def test_25_pre_spawn_execution_guards_and_closed_owner(self):
        with self.runtime() as r:
            for id,edit in (
                ('no-executable',lambda d:d['spec']['argv'].__setitem__(0,'missing-executable-68wblg')),
                ('reserved-env',lambda d:d['spec']['env'].update(PATH='/not/used'))):
                d=self.declaration(id=id,request=id); edit(d)
                done=r.accept_task(d); self.assertEqual(done['phase'],'failed')
                self.assertTrue(done['reason'].startswith('start:')); self.assertIsNone(done['pid'])
                self.assertFalse(r.kernel.lease_busy(done)); self.assertFalse(list(self.root.glob('work/*/*/*/*/ready')))
            r.kernel.close()
            with self.assertRaisesRegex(ValueError,'closed'): r.accept_task(self.declaration())
        # Execution layout gets the same no-symlink validation as P2a paths.
        bait=self.area/'harmless-bait'; bait.mkdir(); (bait/'retained').write_text('unchanged')
        (self.root/'logs/Task/demo').symlink_to(bait)
        with self.runtime() as r:
            done=r.accept_task(self.declaration())
            self.assertEqual(done['phase'],'failed'); self.assertIn('symlink',done['reason'])
            self.assertIsNone(done['pid']); self.assertFalse(r.kernel.lease_busy(done))
        self.assertEqual((bait/'retained').read_text(),'unchanged')

    def test_26_ignored_term_kill_entire_real_child_group(self):
        with self.runtime() as r:
            run=r.accept_task(self.declaration(mode='ignore-group')); work=self.ready(run)
            child=read_json(work/'child-ready')
            self.assertTrue(same_identity(proc(child['pid']),child))
            done=r.control(self.ctl(),term_seconds=.05)
            self.assertEqual((done['phase'],done['signal'],done['waitObserved']),('canceled',9,True))
            self.assertEqual([e['signal'] for e in done['stopEvidence'] if e['event']=='signal'],[15,9])
            self.assertEqual(done['stopEvidence'][-1]['status'],'exited')
            self.assertFalse(done['stopEvidence'][-1]['leaseBusy'])
            self.assertFalse(any(m['state'] not in ('Z','X') for m in done['stopEvidence'][-1]['members']))
            child_state=proc(child['pid'])
            self.assertTrue(child_state is None or child_state['state'] in ('Z','X'), 'KILL must exit child, not just leader')
            self.evidence.append({'stopResult':done,'childAfterStop':child_state,'childBefore':child})
            # A real wait by the independent harness also confirms child's KILL;
            # runtime never consumes this harness exit status.
            pid,status=os.waitpid(child['pid'],0)
            self.assertEqual(pid,child['pid']); self.assertTrue(os.WIFSIGNALED(status)); self.assertEqual(os.WTERMSIG(status),9)
            self.evidence.append({'event':'harness-child-wait','pid':pid,'rawStatus':status,'at':time.time()})

    def test_27_identity_recheck_unavailable_refuses_kill(self):
        with self.runtime() as r:
            run=r.accept_task(self.declaration(mode='hold')); self.ready(run)
            observed=r.kernel.observe(run); self.assertEqual(observed['status'],'running')
            with patch.object(r.kernel,'observe',return_value=observed), patch('runtime.kernel.proc',side_effect=PermissionError('synthetic read failure')):
                blocked=r.control(self.ctl())
            self.assertEqual(blocked['phase'],'cleanupUnknown')
            self.assertIn('identity-recheck-unavailable',blocked['reason'])
            self.assertFalse(any(e['event']=='signal' for e in blocked['stopEvidence']))
            self.assertTrue(same_identity(proc(run['pid']),run))
            self.evidence.append({'refusedResult':blocked,'actual':proc(run['pid'])})
            self.assertEqual(r.control(self.ctl(request='retry-with-identity'))['phase'],'canceled')


if __name__ == '__main__':
    unittest.main(verbosity=2)
