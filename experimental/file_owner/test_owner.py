"""Real OS client/foreground-owner integration; isolated retained Linux roots."""
import ast
import copy
import ctypes
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from runtime.kernel import proc, same_identity, publish, read_json
from runtime.prepare import canonical, digest
from experimental.file_owner.protocol import API, receipt, snapshot, outer_key, request_body

BASE = Path(os.environ['RUND_FILE_TEST_OUTPUT']) if 'RUND_FILE_TEST_OUTPUT' in os.environ else Path(tempfile.mkdtemp(prefix='rund-file-tests-'))
if 'RUND_FILE_TEST_OUTPUT' in os.environ:
    BASE.mkdir(parents=True, exist_ok=False)
for part in ('home', 'tmp', 'cwd'):
    (BASE / part).mkdir()
ENV = {'HOME': str(BASE / 'home'), 'TMPDIR': str(BASE / 'tmp'), 'PYTHONPATH': str(REPO),
       'PATH': str(Path(sys.executable).parent) + os.pathsep + os.defpath,
       'PYTHONDONTWRITEBYTECODE': '1', 'LC_ALL': 'C.UTF-8'}
os.environ.clear()
os.environ.update(ENV)
os.chdir(BASE / 'cwd')
libc = ctypes.CDLL(None, use_errno=True)
if platform.machine() not in ('x86_64', 'aarch64') or libc.prctl(36, 1, 0, 0, 0):
    raise RuntimeError('Linux subreaper/pidfd ABI required')
libc.syscall.restype = ctypes.c_long


def pidfd(pid):
    fd = libc.syscall(434, pid, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'pidfd_open')
    return fd


def psignal(fd, sig):
    if libc.syscall(424, fd, sig, ctypes.c_void_p(0), 0):
        raise OSError(ctypes.get_errno(), 'pidfd_send_signal')


fd = pidfd(os.getpid())
psignal(fd, 0)
os.close(fd)


def wait(fn, seconds=8):
    end = time.monotonic() + seconds
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError('deadline: ' + repr(fn))
        time.sleep(.01)


def command(argv, *, env=ENV, good=True):
    start = time.time()
    p = subprocess.run(list(map(str, argv)), cwd=BASE / 'cwd', env=env,
                       capture_output=True, text=True, timeout=60)
    with (BASE / 'commands.jsonl').open('a') as f:
        f.write(json.dumps({'argv': list(map(str, argv)), 'env': env, 'cwd': str(BASE / 'cwd'),
                            'startedAt': start, 'endedAt': time.time(), 'rc': p.returncode,
                            'stdout': p.stdout, 'stderr': p.stderr}) + '\n')
    if good and p.returncode:
        raise AssertionError(p.stderr + p.stdout)
    return p


DRIVER = r'''
import json,sys,time,traceback
from pathlib import Path
from experimental.file_owner.owner import Owner
from runtime.kernel import publish
import runtime.kernel as kernel
root,protocol,area,event,rid = sys.argv[1:]
area=Path(area)
def hook(at,e):
    if e.get('requestId')==rid and at==event:
        publish(area/'barrier.json',{'event':at,'envelope':e})
        while not (area/'continue').exists(): time.sleep(.005)
def kernel_hook(at,run):
    if event=='kernel-spawned' and at=='spawned':
        p=next(iter(owner.runtime.kernel.processes.values()))
        identity=kernel.proc(p.pid)
        publish(area/'stack-unrecorded-1.json',{'run':identity,'stack':traceback.format_stack()})
        publish(area/'barrier.json',{'event':at,'run':run,'actualIdentity':identity})
        while not (area/'continue').exists(): time.sleep(.005)
    if at=='recorded':
        publish(area/('stack-'+run['id']+'-'+str(run['generation'])+'.json'),
                {'run':run,'stack':traceback.format_stack()})
    if event=='identity-unreadable' and at=='recorded':
        original=kernel.proc
        def denied(pid):
            if pid==run['pid']: raise PermissionError('injected strong identity read failure')
            return original(pid)
        kernel.proc=denied
owner=Owner(root,protocol,'node-a',hook=hook,kernel_hook=kernel_hook)
if event=='receipt-failure':
    original=owner.runtime.receipt
    def failing(path,record,value):
        if record.get('body',{}).get('envelope',{}).get('requestId')==rid:
            raise OSError('injected outer receipt publication failure')
        return original(path,record,value)
    owner.runtime.receipt=failing
try: owner.run(.02)
finally: owner.close()
'''

READER = r'''
import json,sys,time
from pathlib import Path
p,out=sys.argv[1:]; end=time.monotonic()+1; count=0; errors=[]
while time.monotonic()<end:
    try:
        v=json.loads(Path(p).read_bytes()); assert v['semantics']=='last-observed'; count+=1
    except Exception as e: errors.append(str(e))
Path(out).write_text(json.dumps({'count':count,'errors':errors}))
'''


class FileOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BASE / 'source'; cls.source.mkdir()
        command(['git', '-C', cls.source, 'init', '-q', '--template='])
        (cls.source / 'fixture.py').write_bytes((REPO / 'experimental/file_owner/fixture.py').read_bytes())
        command(['git', '-C', cls.source, 'add', '--', 'fixture.py'])
        msg = BASE / 'fixture-message'; msg.write_text('csusii isolated cooperative foreground fixture\n')
        command(['git', '-C', cls.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-q', '-F', msg, '--', 'fixture.py'])
        cls.pin = command(['git', '-C', cls.source, 'rev-parse', 'HEAD']).stdout.strip()
        (BASE / 'environment.json').write_text(json.dumps({'python': sys.executable, 'env': ENV,
                      'source': str(cls.source), 'pin': cls.pin, 'subreaper': True}, indent=2))

    def setUp(self):
        self.area = BASE / self.id().split('.')[-1]; self.area.mkdir(mode=0o700)
        self.root, self.protocol = self.area / 'runtime', self.area / 'protocol'
        self.owners, self.evidence, self.identities = [], [], {}
        self.serial = 0

    def start_owner(self, event='', rid='fault', *, cli=False, node='node-a'):
        log = (self.area / ('owner-' + str(len(self.owners)) + '.log')).open('w')
        argv = ([sys.executable, '-B', '-m', 'experimental.file_owner', 'owner', '--root', str(self.root),
                 '--protocol', str(self.protocol), '--node', node] if cli else
                [sys.executable, '-B', '-c', DRIVER, str(self.root), str(self.protocol), str(self.area), event, rid])
        p = subprocess.Popen(argv, env=ENV, cwd=BASE / 'cwd', stdout=log, stderr=subprocess.STDOUT)
        self.owners.append((p, log))
        self.evidence.append({'event': 'owner-start', 'argv': argv, 'env': ENV, 'cwd': str(BASE / 'cwd'),
                              'at': time.time(), 'identity': proc(p.pid)})
        return p

    def boot(self, event='', rid='fault', cli=False):
        p = self.start_owner(event, rid, cli=cli)
        wait(lambda: (self.protocol / 'snapshot.json').exists() or p.poll() is not None)
        self.assertIsNone(p.poll(), (self.area / 'owner-0.log').read_text())
        return p

    def decl(self, kind='Task', id='demo', attempt='a1', mode='hold', code=0, policy='manual'):
        spec = {'node': 'node-a', 'sources': [{'alias': 'main', 'repository': self.source.as_uri(),
                'revision': self.pin, 'requires': []}], 'prepare': [], 'toolchain': {'python': sys.executable},
                'argv': [sys.executable, '${code.main}/fixture.py', mode, str(code)], 'cwd': '${work}',
                'env': {}, 'credentialRefs': []}
        if kind == 'Task':
            spec.update(attemptId=attempt, requestId=id + '-' + attempt)
        else:
            spec.update(desired='online', restart={'policy': policy, 'initialSeconds': .4, 'maxSeconds': .8})
        return {'apiVersion': 'rund/v1alpha1', 'kind': kind, 'metadata': {'id': id, 'version': 1}, 'spec': spec}

    def req(self, action, d=None, rid=None, **kw):
        self.serial += 1
        e = {'apiVersion': API, 'requestId': rid or 'r' + str(self.serial), 'node': 'node-a', 'action': action}
        if d is not None:
            e.update(declaration=d, declarationDigest=digest(canonical(d)))
        e.update(kw)
        return e

    def send(self, e, good=True):
        path = self.area / ('client-' + str(time.time_ns()) + '.json')
        publish(path, e)
        p = command([sys.executable, '-B', '-m', 'experimental.file_owner', 'send',
                     '--protocol', self.protocol, '--request', path], good=good)
        self.evidence.append({'event': 'client-exited', 'at': time.time(), 'request': e, 'rc': p.returncode})
        if good:
            self.assertEqual(json.loads(p.stdout)['digest'], digest(canonical(request_body(e))))
        return p

    def exact(self, rid):
        rec = wait(lambda: receipt(self.protocol, rid))
        self.evidence.append({'event': 'receipt', 'at': time.time(), 'rid': rid, 'record': rec})
        return rec

    def done(self, e):
        self.send(e)
        rec = wait(lambda: (r if (r := receipt(self.protocol, e['requestId'])) and r.get('status') == 'done' else None))
        self.evidence.append({'event': 'done', 'at': time.time(), 'request': e, 'record': rec})
        self.assertEqual(rec['body'], request_body(e))
        self.assertEqual(rec['digest'], digest(canonical(request_body(e))))
        return rec['result']

    def snap(self):
        value = snapshot(self.protocol)
        self.evidence.append({'event': 'snapshot', 'at': time.time(), 'value': value})
        return value

    def runfact(self, id='demo', phase=None, attempt=None):
        for run in self.snap()['runs']:
            if run['id'] == id and (phase is None or run['phase'] == phase) and (attempt is None or run['attemptId'] == attempt):
                return run
        return None

    def work(self, kind='Task', id='demo', attempt='a1', generation=1):
        return self.root / 'work' / kind / id / attempt / str(generation)

    def ready(self, **kw):
        work = self.work(**kw)
        wait(lambda: (work / 'ready.json').exists())
        ident = read_json(work / 'ready.json'); self.identities[ident['pid']] = ident
        if (work / 'child.json').exists():
            child = read_json(work / 'child.json'); self.identities[child['pid']] = child
        self.evidence.append({'event': 'ready', 'at': time.time(), 'identity': ident})
        return work

    def control(self, action, kind='Task', id='demo', gen=1, rid=None):
        return self.req(action, rid=rid, kind=kind, id=id,
                        attemptId='service' if kind == 'Service' else 'a1', expectedGeneration=gen)

    def kill_owner(self, p):
        p.kill(); self.assertEqual(p.wait(timeout=5), -signal.SIGKILL)
        self.evidence.append({'event': 'owner-crashed', 'at': time.time(), 'pid': p.pid, 'rc': p.returncode})

    def independent_kill(self, ident):
        actual = proc(ident['pid'])
        if actual and actual['state'] not in ('Z', 'X'):
            self.assertTrue(same_identity(actual, ident))
            fd = pidfd(ident['pid'])
            try:
                self.assertTrue(same_identity(proc(ident['pid']), ident))
                psignal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        wait(lambda: (p := proc(ident['pid'])) is None or p['state'] in ('Z', 'X'))
        try:
            os.waitpid(ident['pid'], os.WNOHANG)
        except ChildProcessError:
            pass

    def tearDown(self):
        # First freeze only own coordinator Popen handles; then own recorded fixture identities.
        for p, log in self.owners:
            if p.poll() is None:
                p.terminate()
            p.wait(timeout=5); log.close()
            self.evidence.append({'event': 'owner-reaped', 'pid': p.pid, 'rc': p.returncode, 'at': time.time()})
        if self.root.exists():
            for filename in ('ready.json', 'child.json'):
                for path in (self.root / 'work').rglob(filename):
                    ident = read_json(path); self.identities[ident['pid']] = ident
            # Handles crashes after spawn before ready; only hook's original known run identities.
            for path in self.area.glob('stack-*.json'):
                ident = read_json(path)['run']
                self.identities[ident['pid']] = ident
        for ident in self.identities.values():
            self.evidence.append({'event': 'cleanup-before', 'identity': ident, 'actual': proc(ident['pid']), 'at': time.time()})
            self.independent_kill(ident)
            self.evidence.append({'event': 'cleanup-after', 'pid': ident['pid'], 'actual': proc(ident['pid']), 'at': time.time()})
        (self.area / 'evidence.json').write_text(json.dumps(self.evidence, indent=2))

    def test_01_real_client_lifetime_task_terminals_and_rerun(self):
        owner = self.boot(cli=True)
        e = self.req('release', self.decl())
        result = self.done(e); work = self.ready()
        self.assertEqual(result['pid'], read_json(work / 'ready.json')['pid'])
        self.assertEqual(proc(result['pid'])['pgid'], result['pgid'])
        self.assertIsNone(owner.poll())
        exact_before = (self.root / 'state/requests' / (outer_key(e['requestId']) + '.json')).read_bytes()
        self.send(e); time.sleep(.1)
        self.assertEqual(exact_before, (self.root / 'state/requests' / (outer_key(e['requestId']) + '.json')).read_bytes())
        work.joinpath('release').touch()
        final = wait(lambda: self.runfact(phase='succeeded'))
        self.assertEqual((final['exitCode'], final['waitObserved']), (0, True))
        resultpath = self.root / 'state/runs/Task/demo/a1/1/result.json'
        old, inode = resultpath.read_bytes(), resultpath.stat().st_ino
        self.done(self.control('cancel'))
        self.done(self.req('start', kind='Task', id='demo', attemptId='a1'))
        self.done(self.req('release', e['declaration']))
        self.assertEqual((old, inode), (resultpath.read_bytes(), resultpath.stat().st_ino))
        d = self.decl(attempt='a2', mode='exit', code=7)
        self.done(self.req('rerun', d, id='demo', oldAttempt='a1'))
        fail = wait(lambda: self.runfact(phase='failed', attempt='a2'))
        self.assertEqual(fail['exitCode'], 7)
        time.sleep(.2)
        self.assertEqual(len(list((self.root / 'work/Task/demo').iterdir())), 2)
        self.assertEqual(old, resultpath.read_bytes())

    def test_02_queued_running_cancel_accept_start_observe(self):
        self.boot()
        value = self.done(self.req('accept', self.decl()))
        self.assertEqual(value['phase'], 'accepted')
        self.assertFalse(self.work().exists())
        self.done(self.control('cancel'))
        self.assertEqual(wait(lambda: self.runfact(phase='canceled'))['waitObserved'], False)
        d = self.decl(id='other')
        self.done(self.req('accept', d))
        self.done(self.req('start', kind='Task', id='other', attemptId='a1'))
        self.ready(id='other')
        seen = self.done(self.req('observe', kind='Task', id='other', attemptId='a1'))
        self.assertEqual(seen['phase'], 'running')
        canceled = self.done(self.control('cancel', id='other'))
        self.assertEqual(canceled['phase'], 'canceled')
        self.assertTrue(canceled['waitObserved'])
        self.assertEqual(canceled['signal'], signal.SIGTERM)

    def test_03_strict_rejections_conflicts_partial_links(self):
        self.boot()
        original = self.req('accept', self.decl(), rid='original')
        self.done(original)
        recordpath = self.root / 'state/requests' / (outer_key('original') + '.json')
        old = recordpath.read_bytes()
        changed = dict(original, action='release')
        publish(self.protocol / 'requests/original.json', changed)
        wait(lambda: (self.protocol / 'rejections/original.json').exists())
        self.assertEqual(recordpath.read_bytes(), old)
        self.assertFalse(self.work().exists())
        bads = []
        e = self.req('release', self.decl(id='bad1')); e['node'] = 'other'; bads.append(canonical(e))
        e = self.req('release', self.decl(id='bad2')); e['extra'] = 1; bads.append(canonical(e))
        e = self.req('release', self.decl(id='bad3')); e['declaration']['spec']['sources'][0]['revision'] = 'main'
        e['declarationDigest'] = digest(canonical(e['declaration'])); bads.append(canonical(e))
        e = self.req('release', self.decl(id='bad4')); e['declarationDigest'] = '0' * 64; bads.append(canonical(e))
        bads += [b'{"requestId":"duplicate","requestId":"duplicate"}', b'{', b' ' * 65537]
        for i, raw in enumerate(bads):
            path = self.protocol / 'requests' / ('bad' + str(i) + '.json')
            path.write_bytes(raw); path.chmod(0o600)
        temp = self.protocol / 'requests/.tmp-incomplete'; temp.write_text('{'); temp.chmod(0o600)
        outside = self.area / 'outside'; publish(outside, self.req('release', self.decl(id='linked'), rid='linked'))
        (self.protocol / 'requests/linked.json').symlink_to(outside)
        os.link(outside, self.protocol / 'requests/hard.json')
        wait(lambda: len(list((self.protocol / 'rejections').iterdir())) >= len(bads) + 3)
        self.assertEqual(list((self.root / 'state/objects/Task').iterdir()), [self.root / 'state/objects/Task/demo'])
        self.assertFalse(self.work().exists())

    def test_04_policies_backoff_hold_stale_controls_one_kernel(self):
        self.boot()
        self.done(self.req('release', self.decl(id='task', mode='exit')))
        for id, policy, code in [('manual', 'manual', 7), ('success', 'on-failure', 0),
                                 ('failure', 'on-failure', 7), ('always', 'always', 0)]:
            self.done(self.req('submit', self.decl('Service', id=id, policy=policy, mode='exit', code=code)))
        wait(lambda: all(self.runfact(id=id) and self.runfact(id=id)['waitObserved']
                         for id in ('task', 'manual', 'success', 'failure', 'always')))
        snap = self.snap()
        for id in ('manual', 'success'):
            service = next(s for s in snap['services'] if s['id'] == id)
            self.assertEqual((service['generation'], service['phase']), (1, 'exited'))
        for id in ('failure', 'always'):
            # Find a freshly entered backoff; bound is .4 then .8, no early allocation.
            head = wait(lambda: next((s for s in self.snap()['services'] if s['id'] == id and s['phase'] == 'backoff'
                                      and s['nextStartAt'] - time.time() > .1), None))
            gen, deadline = head['generation'], head['nextStartAt']
            terminal = self.root / 'state/runs/Service' / id / 'service' / str(gen) / 'result.json'
            old_result, old_inode = terminal.read_bytes(), terminal.stat().st_ino
            while time.time() < deadline - .04:
                self.assertEqual(next(s for s in self.snap()['services'] if s['id'] == id)['generation'], gen)
                time.sleep(.01)
            wait(lambda: next(s for s in self.snap()['services'] if s['id'] == id)['generation'] == gen + 1)
            self.done(self.control('stop', 'Service', id=id, gen=gen+1))
            self.assertEqual((terminal.read_bytes(), terminal.stat().st_ino), (old_result, old_inode))
        d = self.decl('Service', id='held', policy='always')
        self.done(self.req('submit', d)); self.ready(kind='Service', id='held', attempt='service')
        e = self.control('restart', 'Service', id='held', rid='restart')
        restarted = self.done(e); self.assertEqual(restarted['generation'], 2)
        self.ready(kind='Service', id='held', attempt='service', generation=2)
        self.send(e); time.sleep(.1)
        stale = self.done(self.control('stop', 'Service', id='held', gen=1))
        self.assertTrue(stale['noop'])
        stopped = self.done(self.control('stop', 'Service', id='held', gen=2)); self.assertTrue(stopped['hold'])
        self.done(self.control('stop', 'Service', id='held', gen=2))
        status = self.done(self.req('status', kind='Service', id='held', attemptId='service'))
        time.sleep(.9)
        self.assertEqual(next(s for s in self.snap()['services'] if s['id'] == 'held')['generation'], status['generation'])
        stacks = [read_json(p) for p in self.area.glob('stack-*.json')]
        self.assertTrue(any(s['run']['kind'] == 'Task' for s in stacks))
        self.assertTrue(any(s['run']['kind'] == 'Service' for s in stacks))
        self.assertEqual(len({r['run']['ownerEpoch'] for r in stacks}), 1)
        self.assertTrue(all('kernel.py' in ''.join(s['stack']) for s in stacks))

    def test_05_same_root_lock_and_wrong_node(self):
        first = self.boot(cli=True)
        for node in ('node-a', 'node-b'):
            p = self.start_owner(cli=True, node=node)
            self.assertEqual(p.wait(timeout=5), 2)
        self.assertEqual(read_json(self.root / 'state/owners/node.json'), {'node': 'node-a'})
        self.kill_owner(first)
        wrong = self.start_owner(cli=True, node='node-b')
        self.assertEqual(wrong.wait(timeout=5), 2)
        self.assertEqual(read_json(self.root / 'state/owners/node.json'), {'node': 'node-a'})

    def test_06_crash_reopen_live_then_lost_null_no_relaunch(self):
        p = self.boot()
        self.done(self.req('release', self.decl()))
        work = self.ready(); wait(lambda: self.runfact(phase='running')); before = self.snap()
        self.kill_owner(p)
        frozen = (self.protocol / 'snapshot.json').read_bytes(); time.sleep(.1)
        self.assertEqual(frozen, (self.protocol / 'snapshot.json').read_bytes())
        self.boot()
        after = wait(lambda: (v if (v := self.snap())['ownerEpoch'] != before['ownerEpoch'] else None))
        self.assertEqual(after['runs'][0]['pid'], before['runs'][0]['pid'])
        self.assertEqual(after['runs'][0]['generation'], 1)
        work.joinpath('release').touch()
        result = wait(lambda: self.runfact(phase='lost'))
        self.assertEqual((result['waitObserved'], result['exitCode'], result['signal']), (False, None, None))
        self.assertEqual(len(list((self.root / 'work/Task/demo/a1').iterdir())), 1)

    def test_07_leader_dead_child_lease_blocks_always(self):
        self.boot()
        self.done(self.req('submit', self.decl('Service', policy='always', mode='fork')))
        work = self.ready(kind='Service', attempt='service')
        wait(lambda: (work / 'child.json').exists())
        child = read_json(work / 'child.json'); self.identities[child['pid']] = child
        result = wait(lambda: self.runfact(phase='cleanupUnknown'))
        self.assertTrue(result['observation']['leaseBusy'])
        self.assertTrue(result['observation']['waitObserved'])
        stop = self.done(self.control('stop', 'Service'))
        self.assertEqual(stop['phase'], 'cleanupUnknown')
        time.sleep(.6)
        self.assertEqual(next(s for s in self.snap()['services'] if s['id'] == 'demo')['generation'], 1)
        self.assertNotIn(proc(child['pid'])['state'], ('Z', 'X'))

    def test_08_injected_identity_unreadable_no_signal_or_generation(self):
        self.boot('identity-unreadable')
        self.done(self.req('submit', self.decl('Service', policy='always')))
        self.ready(kind='Service', attempt='service')
        run = wait(lambda: self.runfact(phase='cleanupUnknown'))
        stop = self.done(self.control('restart', 'Service'))
        self.assertEqual(stop['phase'], 'cleanupUnknown')
        self.assertEqual(stop['generation'], 1)
        time.sleep(.5)
        self.assertNotIn(proc(run['pid'])['state'], ('Z', 'X'))
        self.assertIn('identity-observation-unavailable', run['observation']['reason'])

    def test_09_pending_before_effect_blocks_new_rid_and_reopen(self):
        p = self.boot('pending')
        e = self.req('release', self.decl(), rid='fault'); self.send(e)
        wait(lambda: (self.area / 'barrier.json').exists())
        self.kill_owner(p)
        self.assertEqual(self.exact('fault')['status'], 'pending')
        self.boot()
        self.send(e)
        blocked = self.done(self.req('release', e['declaration'], rid='new-rid'))
        self.assertEqual(blocked['status'], 'blocked')
        time.sleep(.1)
        self.assertFalse(self.work().exists())
        self.assertEqual(self.exact('fault')['status'], 'pending')

    def test_10_effect_before_receipt_pending_blocks_restart_and_policy(self):
        p = self.boot('effect')
        e = self.req('submit', self.decl('Service', policy='always', mode='exit'), rid='fault')
        self.send(e); wait(lambda: (self.area / 'barrier.json').exists()); self.kill_owner(p)
        self.ready(kind='Service', attempt='service')
        self.boot()
        self.assertEqual(self.exact('fault')['status'], 'pending')
        blocked = self.done(self.control('restart', 'Service', rid='new-control'))
        self.assertEqual(blocked['status'], 'blocked')
        time.sleep(.6)
        self.assertEqual(len(list((self.root / 'work/Service/demo/service').iterdir())), 1)

    def test_11_receipt_publication_failure_no_false_done(self):
        self.boot('receipt-failure')
        e = self.req('release', self.decl(), rid='fault'); self.send(e)
        self.ready()
        wait(lambda: (self.protocol / 'rejections/fault.json').exists())
        self.assertEqual(self.exact('fault')['status'], 'pending')
        blocked = self.done(self.req('start', kind='Task', id='demo', attemptId='a1'))
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(len(list((self.root / 'work/Task/demo/a1').iterdir())), 1)

    def test_12_done_crash_exact_query_and_atomic_concurrent_snapshots(self):
        p = self.boot('done')
        e = self.req('release', self.decl(), rid='fault'); self.send(e)
        wait(lambda: (self.area / 'barrier.json').exists()); self.ready()
        rec = self.exact('fault'); self.assertEqual(rec['status'], 'done')
        self.kill_owner(p); self.boot()
        self.assertEqual(rec, self.exact('fault'))
        readers = []
        for i in range(3):
            output = self.area / ('reader-' + str(i) + '.json')
            pp = subprocess.Popen([sys.executable, '-B', '-c', READER, str(self.protocol / 'snapshot.json'), str(output)],
                                  cwd=BASE / 'cwd', env=ENV)
            readers.append((pp, output))
        for pp, output in readers:
            self.assertEqual(pp.wait(timeout=5), 0)
            value = read_json(output); self.assertGreater(value['count'], 0); self.assertEqual(value['errors'], [])
            self.evidence.append({'event': 'concurrent-reader', 'pid': pp.pid, **value})
        result = self.done(self.req('status', kind='Task', id='demo', attemptId='a1'))
        self.assertEqual(result['pid'], rec['result']['pid'])
        queried = command([sys.executable, '-B', '-m', 'experimental.file_owner', 'receipt',
                           '--protocol', self.protocol, '--rid', 'fault'])
        self.assertEqual(json.loads(queried.stdout), rec)

    def test_13_unindexed_and_corrupt_request_block_effects(self):
        p = self.boot()
        self.done(self.req('submit', self.decl('Service', policy='always')))
        self.ready(kind='Service', attempt='service')
        self.kill_owner(p)
        (self.root / 'state/runs/Service/demo/service/2').mkdir(mode=0o700)
        self.boot()
        wait(lambda: any('unindexed' in str(d) for d in self.snap()['diagnostics']))
        blocked = self.done(self.control('restart', 'Service'))
        self.assertEqual(blocked['status'], 'blocked')
        damaged = self.root / 'state/requests/damaged.json'; damaged.write_text('{'); damaged.chmod(0o600)
        blocked = self.done(self.req('release', self.decl(id='other')))
        self.assertEqual(blocked['status'], 'blocked')
        self.assertFalse(self.work(id='other').exists())

    def test_14_spawned_missing_identity_pending_never_replayed(self):
        p = self.boot('kernel-spawned')
        e = self.req('release', self.decl(), rid='fault'); self.send(e)
        wait(lambda: (self.area / 'barrier.json').exists()); self.ready()
        self.kill_owner(p); self.boot()
        run = wait(lambda: self.runfact(phase='cleanupUnknown'))
        self.assertIsNone(run['pid'])
        self.assertTrue(run['observation']['leaseBusy'])
        self.assertFalse(run['observation']['waitObserved'])
        blocked = self.done(self.req('start', kind='Task', id='demo', attemptId='a1'))
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(self.exact('fault')['status'], 'pending')
        self.assertEqual(len(list((self.root / 'work/Task/demo/a1').iterdir())), 1)

    def test_15_offline_hold_prepare_failure_and_disjoint_roots(self):
        owner = self.boot()
        d = self.decl('Service', policy='always')
        self.done(self.req('submit', d)); self.ready(kind='Service', attempt='service')
        d2 = copy.deepcopy(d); d2['metadata']['version'] = 2; d2['spec']['desired'] = 'offline'
        offline = self.done(self.req('submit', d2))
        self.assertTrue(offline['hold']); self.assertEqual(offline['generation'], 1)
        epoch = self.snap()['ownerEpoch']
        self.kill_owner(owner); self.boot()
        wait(lambda: self.snap()['ownerEpoch'] != epoch)
        time.sleep(.6)
        self.assertEqual(next(s for s in self.snap()['services'] if s['id'] == 'demo')['generation'], 1)
        bad = copy.deepcopy(d); bad['metadata']['version'] = 3; bad['spec']['sources'][0]['revision'] = '0' * 40
        rejected = self.done(self.req('submit', bad))
        self.assertEqual(rejected['phase'], 'failed'); self.assertTrue(rejected['previousUnchanged'])
        self.assertTrue(next(s for s in self.snap()['services'] if s['id'] == 'demo')['hold'])
        on = copy.deepcopy(d); on['metadata']['version'] = 4
        running = self.done(self.req('submit', on)); self.assertEqual(running['generation'], 2)
        self.ready(kind='Service', attempt='service', generation=2)
        # Clients validate syntax, but owner also rejects source/protocol overlap itself.
        overlap = self.decl(id='overlap')
        overlap['spec']['sources'][0]['repository'] = self.protocol.as_uri()
        e = self.req('release', overlap)
        self.send(e)
        wait(lambda: (self.protocol / 'rejections' / (e['requestId'] + '.json')).exists())
        self.assertFalse(self.work(id='overlap').exists())

    def test_16_unknown_status_is_readonly_and_bad_bindings_refused(self):
        owner = self.boot()
        for kind in ('Task', 'Service'):
            e = self.req('status', kind=kind, id='unknown', attemptId='a1' if kind == 'Task' else 'service')
            self.send(e)
            wait(lambda: (self.protocol / 'rejections' / (e['requestId'] + '.json')).exists())
            self.assertFalse((self.root / 'state/runs' / kind / 'unknown').exists())
            self.assertFalse((self.root / 'state/objects' / kind / 'unknown').exists())
        for bad in ({'apiVersion': 'future'}, {'requestId': '../bad'}, {'node': 'other'}):
            e = self.req('release', self.decl(id='mustnotstart')); e.update(bad)
            result = self.send(e, good=False); self.assertEqual(result.returncode, 2)
        self.assertFalse(self.work(id='mustnotstart').exists())
        # Explicit same-root protocol alias is rejected before Runtime creation.
        rejected = command([sys.executable, '-B', '-m', 'experimental.file_owner', 'owner',
                            '--root', self.root, '--protocol', self.root / 'protocol', '--node', 'node-a'], good=False)
        self.assertEqual(rejected.returncode, 2)
        self.done(self.req('release', self.decl()))
        self.ready(); self.kill_owner(owner)
        path = self.root / 'state/runs/Task/demo/a1/1/run.json'
        corrupted = read_json(path); corrupted['generation'] = 2
        publish(path, corrupted)  # explicit disk-corruption fault, not a fabricated valid run
        self.boot()
        wait(lambda: any('run binding mismatch' in str(d) for d in self.snap()['diagnostics']))
        blocked = self.done(self.req('start', kind='Task', id='demo', attemptId='a1'))
        self.assertEqual(blocked['status'], 'blocked')
        self.assertFalse((self.root / 'state/runs/Task/demo/a1/2').exists())

    def test_17_wrapper_audit_no_second_execution_layer(self):
        findings = {}
        for filename in ('owner.py', 'protocol.py', '__main__.py'):
            tree = ast.parse((REPO / 'experimental/file_owner' / filename).read_text())
            calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
            forbidden = [c for c in calls if any(t in c for t in ('Popen', 'kill', 'fork', 'execv', 'group_members', 'os.system'))]
            self.assertEqual(forbidden, [])
            findings[filename] = calls
        (self.area / 'ast-calls.json').write_text(json.dumps(findings, indent=2))


if __name__ == '__main__':
    unittest.main(verbosity=2)
