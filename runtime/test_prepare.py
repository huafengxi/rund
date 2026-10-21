#!/usr/bin/env python3
"""Independent retained synthetic fixtures; no production root or auto cleanup.

Run with the desired Python: /absolute/python -B /checkout/runtime/test_prepare.py
RUND_TEST_OUTPUT may name a new independent artifact directory.
"""
import copy
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from runtime.contract import ContractError, load, validate
from runtime.prepare import init_root

BASE = Path(os.environ['RUND_TEST_OUTPUT']) if 'RUND_TEST_OUTPUT' in os.environ else Path(tempfile.mkdtemp(prefix='rund-synthetic-'))
if 'RUND_TEST_OUTPUT' in os.environ:
    BASE.mkdir(parents=True, exist_ok=False)
for n in ('home', 'tmp', 'cwd unrelated'):
    (BASE / n).mkdir()
ENV = {'HOME': str(BASE / 'home'), 'TMPDIR': str(BASE / 'tmp'), 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.defpath,
       'PYTHONPATH': str(REPO), 'PYTHONDONTWRITEBYTECODE': '1', 'LC_ALL': 'C.UTF-8'}
EVENTS = BASE / 'commands.jsonl'


def run(cmd, *, env=None, cwd=None, check=True):
    environ = env or ENV
    where = cwd or BASE / 'cwd unrelated'
    p = subprocess.run([str(x) for x in cmd], env=environ, cwd=where, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
    with EVENTS.open('a') as f:
        f.write(json.dumps({'command': [str(x) for x in cmd], 'environment': environ, 'cwd': str(where), 'rc': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}) + '\n')
    if check and p.returncode:
        raise AssertionError(f'{cmd}: rc={p.returncode}\n{p.stdout}\n{p.stderr}')
    return p


def git(repo, *args):
    return run(['git', '-C', repo, *args]).stdout.strip()


class PrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = BASE / 'synthetic source with spaces'
        cls.repo.mkdir()
        git(cls.repo, 'init', '-q', '--template=')
        (cls.repo / 'hello.txt').write_text('pinned content\n')
        (cls.repo / 'run.sh').write_text('#!/bin/sh\nprintf never\n')
        (cls.repo / 'run.sh').chmod(0o755)
        (cls.repo / 'lock.txt').write_text('locked synthetic input\n')
        (cls.repo / 'nested').mkdir()
        (cls.repo / 'nested/space name').write_bytes(b'\x00\xff\n')
        git(cls.repo, 'add', '--', 'hello.txt', 'run.sh', 'lock.txt', 'nested/space name')
        msg = BASE / 'fixture-message'
        msg.write_text('synthetic fixture for rund prepare tests\n')
        git(cls.repo, '-c', 'user.name=Synthetic', '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '-F', msg, '--', 'hello.txt', 'run.sh', 'lock.txt', 'nested/space name')
        cls.pin = git(cls.repo, 'rev-parse', 'HEAD')
        cls.blob = git(cls.repo, 'rev-parse', 'HEAD:hello.txt')
        cls.tree = git(cls.repo, 'rev-parse', 'HEAD^{tree}')
        git(cls.repo, '-c', 'user.name=Synthetic', '-c', 'user.email=fixture@example.invalid', 'tag', '-a', 'annotated', '-F', msg)
        cls.tag = git(cls.repo, 'rev-parse', 'annotated')
        cls.marker = BASE / 'PAYLOAD_MUST_NOT_EXIST'
        common = {'node': 'node-a', 'sources': [{'alias': 'main', 'repository': cls.repo.as_uri(), 'revision': cls.pin, 'requires': [], 'lockFiles': ['lock.txt']}],
                  'prepare': [], 'toolchain': {'python': sys.executable}, 'cwd': '${work}', 'env': {}, 'credentialRefs': [],
                  'argv': [sys.executable, '-c', 'from pathlib import Path; Path(__import__("sys").argv[1]).write_text("EXECUTED")', str(cls.marker)]}
        cls.task = {'apiVersion': 'rund/v1alpha1', 'kind': 'Task', 'metadata': {'id': 'demo-task', 'version': 1}, 'spec': dict(common, attemptId='attempt-001', requestId='request-001')}
        cls.service = {'apiVersion': 'rund/v1alpha1', 'kind': 'Service', 'metadata': {'id': 'demo-service', 'version': 1}, 'spec': dict(common, desired='online', restart={'policy': 'on-failure', 'initialSeconds': 1, 'maxSeconds': 8})}
        (BASE / 'service.json').write_text(json.dumps(cls.service, indent=2))
        (BASE / 'task.json').write_text(json.dumps(cls.task, indent=2))
        (BASE / 'fixture.json').write_text(json.dumps({'pin': cls.pin, 'blob': cls.blob, 'tree': cls.tree, 'annotatedTag': cls.tag, 'repo': str(cls.repo)}, indent=2))

    def setUp(self):
        self.area = BASE / self.id().split('.')[-1]
        self.area.mkdir()
        self.node = self.area / 'node root with spaces'

    def cli(self, d=None, *, root=None, env=None, command='prepare', good=True):
        decl = self.area / ('declaration-' + str(time.time_ns()) + '.json')
        decl.write_text(json.dumps(d if d is not None else self.task))
        cmd = [sys.executable, '-B', '-m', 'runtime.cli', command, '--declaration', decl]
        if command == 'prepare':
            cmd += ['--root', root or self.node]
        p = run(cmd, env=env, check=False)
        if good:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn('error', p.stderr)
        self.assertNotIn('Traceback', p.stderr)
        return p

    def bad(self, change, expected=None):
        d = copy.deepcopy(self.task)
        change(d)
        p = self.cli(d, good=False)
        if expected:
            self.assertIn(expected, p.stderr)

    def test_service_task_and_content_mode(self):
        for d in (self.service, self.task):
            v = self.cli(d, command='validate')
            self.assertEqual(v['kind'], d['kind'])
            r = self.cli(d)
            src = Path(r['code']) / 'main'
            self.assertEqual((src / 'hello.txt').read_text(), 'pinned content\n')
            self.assertEqual((src / 'nested/space name').read_bytes(), b'\x00\xff\n')
            self.assertEqual(stat.S_IMODE((src / 'run.sh').stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE((src / 'hello.txt').stat().st_mode), 0o444)
            m = json.loads(Path(r['manifest']).read_text())
            self.assertTrue(m['complete'])
            self.assertEqual(m['inputs']['declaration'], d)
            self.assertEqual(len(m['inputs']['locks']['main']['lock.txt']), 64)
            self.assertNotEqual(Path(r['work']), src)
        self.assertFalse(self.marker.exists(), 'prepare executed payload argv')
        self.assertFalse((BASE / 'home/m').exists())

    def test_idempotence_and_work_isolation(self):
        a = self.cli()
        (Path(a['work']) / 'payload-data').write_text('work is separate')
        b = self.cli()
        self.assertFalse(a['cacheHit'])
        self.assertTrue(b['cacheHit'])
        self.assertEqual(a['key'], b['key'])
        self.assertEqual(a['code'], b['code'])
        self.assertEqual(len(list((self.node / 'manifests').glob('*.json'))), 1)
        self.assertFalse((Path(b['code']) / 'main/payload-data').exists())

    def test_pin_not_branch_or_worktree(self):
        source = self.area / 'moving-branch-source'
        run(['git', 'clone', self.repo, source])
        (source / 'hello.txt').write_text('new HEAD, must not be prepared for old pin')
        (source / 'run.sh').chmod(0o644)
        git(source, 'add', '--', 'hello.txt', 'run.sh')
        msg = self.area / 'message'; msg.write_text('synthetic new branch head')
        git(source, '-c', 'user.name=Synthetic', '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '-F', msg, '--', 'hello.txt', 'run.sh')
        self.assertNotEqual(git(source, 'rev-parse', 'HEAD'), self.pin)
        (source / 'hello.txt').write_text('dirty developer tree, also must not be used')
        d = copy.deepcopy(self.task); d['spec']['sources'][0]['repository'] = source.as_uri()
        r = self.cli(d)
        self.assertEqual((Path(r['code']) / 'main/hello.txt').read_text(), 'pinned content\n')
        self.assertEqual(stat.S_IMODE((Path(r['code']) / 'main/run.sh').stat().st_mode), 0o555)

    def test_multiple_sources_dag_and_key(self):
        d = copy.deepcopy(self.task)
        src = copy.deepcopy(d['spec']['sources'][0]); src.update(alias='dependency')
        d['spec']['sources'].append(src)
        d['spec']['sources'][0]['requires'] = ['dependency']
        r = self.cli(d)
        self.assertEqual({p.name for p in Path(r['code']).iterdir()}, {'main', 'dependency'})
        self.assertTrue(self.cli(d)['cacheHit'])
        d['spec']['env'] = {'SYNTHETIC': 'changed'}
        self.assertNotEqual(self.cli(d)['key'], r['key'])

    def test_schema_rejections(self):
        changes = [lambda d: d.update(apiVersion='rund/v2'), lambda d: d.update(kind='Daemon'),
                   lambda d: d.update(extra=True), lambda d: d['metadata'].update(version=True),
                   lambda d: d['spec'].update(restart={}), lambda d: d['spec'].update(attemptId='../escape'),
                   lambda d: d['spec'].update(prepare=[['true']]), lambda d: d['spec'].update(credentialRefs=['secret']),
                   lambda d: d['spec'].update(toolchain={'node': 'node'}), lambda d: d['spec'].update(cwd='/tmp'),
                   lambda d: d['spec'].update(argv=['${code.missing}/x']), lambda d: d['spec']['sources'][0].update(unknown='x'),
                   lambda d: d['spec']['sources'][0].update(lockFiles=['../escape']),
                   lambda d: d['spec']['sources'][0].update(requires=['missing'])]
        for change in changes:
            self.bad(change)
        d = copy.deepcopy(self.service)
        d['spec']['attemptId'] = 'task-not-service'
        self.cli(d, good=False)
        d = copy.deepcopy(self.service)
        d['spec']['restart']['maxSeconds'] = -1
        self.cli(d, good=False)

    def test_alias_cycle_rejections(self):
        for alias in ('main', 'MAIN'):
            d = copy.deepcopy(self.task)
            s = copy.deepcopy(d['spec']['sources'][0]); s['alias'] = alias
            d['spec']['sources'].append(s)
            self.cli(d, good=False)
        self.bad(lambda d: d['spec']['sources'][0].update(requires=['main']), 'cycle')
        d = copy.deepcopy(self.task)
        s = copy.deepcopy(d['spec']['sources'][0]); s.update(alias='other', requires=['main'])
        d['spec']['sources'][0]['requires'] = ['other']; d['spec']['sources'].append(s)
        self.cli(d, good=False)

    def test_duplicate_json_rejected(self):
        f = self.area / 'duplicate.json'
        f.write_text('{"kind":"Task","kind":"Service"}')
        p = run([sys.executable, '-B', '-m', 'runtime.cli', 'validate', '--declaration', f], check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn('duplicate JSON', p.stderr)

    def test_bad_pin_branch_tag_noncommit(self):
        for pin in ('main', 'refs/heads/main', 'annotated', self.pin[:12], self.pin.upper(), '0' * 40, self.blob, self.tree, self.tag):
            self.bad(lambda d: d['spec']['sources'][0].update(revision=pin))

    def test_url_and_source_boundaries(self):
        for url in ('https://example.invalid/repo', 'ssh://example.invalid/repo', 'file://localhost/tmp/repo', self.repo.as_uri() + '?x=1', 'file:///tmp/../escape'):
            self.bad(lambda d: d['spec']['sources'][0].update(repository=url))
        link = self.area / 'source-link'; link.symlink_to(self.repo, target_is_directory=True)
        self.bad(lambda d: d['spec']['sources'][0].update(repository=link.as_uri()), 'symlink')
        # Harmless own sibling with shared string prefix must not be mistaken for containment.
        root = self.area / 'repo'; root.mkdir()
        source = self.area / 'repo-sibling'; source.mkdir()
        run(['git', 'clone', '--bare', self.repo, source])
        d = copy.deepcopy(self.task); d['spec']['sources'][0]['repository'] = source.as_uri()
        self.cli(d, root=root)
        self.cli(d, root=source / 'node', good=False)

    def test_missing_tools(self):
        env = dict(ENV, PATH=str(self.area))
        self.cli(env=env, good=False)
        self.bad(lambda d: d['spec']['toolchain'].update(python='missing-tool-nrz225'), 'missing declared')
        self.bad(lambda d: d['spec']['toolchain'].update(python='/bin/true'), 'only current Python')

    def test_permission_failure(self):
        if os.geteuid() == 0:
            self.skipTest('root can bypass chmod; no permission-failure claim')
        init_root(self.node)
        target = self.node / 'manifests'
        target.chmod(0o500)
        try:
            p = self.cli(good=False)
            self.assertIn('Permission denied', p.stderr)
            self.assertEqual(list(target.iterdir()), [])
        finally:
            target.chmod(0o700)

    def test_partial_artifacts_recover_without_deletion(self):
        init_root(self.node)
        old = self.node / 'state/staging' / 'interrupted-owned-fixture'
        old.mkdir(); (old / 'partial').write_text('incomplete')
        orphan = self.node / 'code/orphan-owned-fixture'
        orphan.mkdir(); (orphan / 'partial').write_text('not a manifest')
        hidden = self.node / 'cache/.interrupted-owned-fixture'
        hidden.mkdir(); (hidden / 'partial').write_text('cache initialization interrupted')
        r = self.cli()
        self.assertFalse(r['cacheHit'])
        self.assertTrue(self.cli()['cacheHit'])
        self.assertEqual((old / 'partial').read_text(), 'incomplete')
        self.assertTrue((orphan / 'partial').exists())
        self.assertTrue((hidden / 'partial').exists())

    def test_actual_killed_preparer_recovers(self):
        ready = self.area / 'ready'
        decl = self.area / 'kill.json'; decl.write_text(json.dumps(self.task))
        script = '''import json,sys,time
from pathlib import Path
import runtime.prepare as p
old=p.Git.object
def blocked(self,*a,**k):
 r=old(self,*a,**k)
 if a[-1]=='blob':
  Path(sys.argv[3]).write_text('first blob imported')
  time.sleep(120)
 return r
p.Git.object=blocked
p.prepare(json.loads(Path(sys.argv[1]).read_text()),sys.argv[2])
'''
        cmd = [sys.executable, '-B', '-c', script, str(decl), str(self.node), str(ready)]
        log = self.area / 'killed.log'
        with log.open('w') as out:
            proc = subprocess.Popen(cmd, env=ENV, cwd=BASE / 'cwd unrelated', stdout=out, stderr=out)
            try:
                deadline = time.monotonic() + 30
                while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), log.read_text())
                proc.kill(); rc = proc.wait(timeout=10)
                with EVENTS.open('a') as f:
                    f.write(json.dumps({'command': cmd, 'environment': ENV, 'cwd': str(BASE / 'cwd unrelated'), 'rc': rc, 'log': str(log), 'injection': 'test-only monkeypatch waits after first imported blob; SIGKILL'}) + '\n')
                self.assertEqual(rc, -signal.SIGKILL)
            finally:
                if proc.poll() is None:
                    proc.kill(); proc.wait()
        self.assertEqual(list((self.node / 'manifests').glob('*.json')), [])
        partials = set((self.node / 'state/staging').iterdir())
        self.assertTrue(partials)
        self.assertFalse(self.cli()['cacheHit'])
        self.assertTrue(self.cli()['cacheHit'])
        self.assertTrue(partials <= set((self.node / 'state/staging').iterdir()))

    def test_two_process_concurrency(self):
        decl = self.area / 'concurrent.json'; decl.write_text(json.dumps(self.task))
        for initialized in (False, True):
            root = self.node / str(initialized)
            if initialized:
                init_root(root)
            cmd = [sys.executable, '-B', '-m', 'runtime.cli', 'prepare', '--root', str(root), '--declaration', str(decl)]
            ps = [subprocess.Popen(cmd, env=ENV, cwd=BASE / 'cwd unrelated', stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
            results = []
            for p in ps:
                out, err = p.communicate(timeout=60)
                with EVENTS.open('a') as f:
                    f.write(json.dumps({'command': cmd, 'environment': ENV, 'cwd': str(BASE / 'cwd unrelated'), 'rc': p.returncode, 'stdout': out, 'stderr': err, 'concurrentPid': p.pid}) + '\n')
                self.assertEqual(p.returncode, 0, err)
                results.append(json.loads(out))
            self.assertEqual(results[0]['code'], results[1]['code'])
            self.assertEqual(sorted(r['cacheHit'] for r in results), [False, True])
            self.assertEqual(len(list((root / 'manifests').glob('*.json'))), 1)
            self.assertEqual(len(list((root / 'code').iterdir())), 1)
            self.assertTrue(self.cli(root=root)['cacheHit'])

    def test_first_initialization_marker_interleave(self):
        # Pause only this test's child after O_EXCL marker creation, before any
        # write. A second child must reach the SAME locked inode and block.
        ready = self.area / 'marker-created'
        waiting = self.area / 'second-blocked'
        release = self.area / 'release-marker-write'
        decl = self.area / 'interleave.json'; decl.write_text(json.dumps(self.task))
        first = '''import sys,time
from pathlib import Path
import runtime.prepare as p
from runtime.cli import main
original=p.os.open
def paused(path,*args,**kwargs):
 fd=original(path,*args,**kwargs)
 if Path(path).name=='.rund-root.json':
  Path(sys.argv[3]).write_text('O_EXCL created marker; no bytes written')
  while not Path(sys.argv[4]).exists():time.sleep(.01)
 return fd
p.os.open=paused
sys.exit(main(['prepare','--declaration',sys.argv[1],'--root',sys.argv[2]]))
'''
        second = '''import os,sys
from pathlib import Path
import runtime.prepare as p
from runtime.cli import main
original=p.fcntl.flock
def observed(fd,operation):
 st=os.fstat(fd); target=(Path(sys.argv[2])/'.init.lock').stat()
 if (st.st_dev,st.st_ino)==(target.st_dev,target.st_ino):
  try: original(fd,operation|p.fcntl.LOCK_NB)
  except BlockingIOError:
   Path(sys.argv[3]).write_text('same init lock inode is held by first child')
   return original(fd,operation)
  raise AssertionError('second initializer failed to wait for first')
 return original(fd,operation)
p.fcntl.flock=observed
sys.exit(main(['prepare','--declaration',sys.argv[1],'--root',sys.argv[2]]))
'''
        commands = [[sys.executable, '-B', '-c', first, str(decl), str(self.node), str(ready), str(release)],
                    [sys.executable, '-B', '-c', second, str(decl), str(self.node), str(waiting)]]
        children = []
        results = []
        try:
            for cmd, event in zip(commands, (ready, waiting)):
                proc = subprocess.Popen(cmd, env=ENV, cwd=BASE / 'cwd unrelated', stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append(proc)
                deadline = time.monotonic() + 30
                while not event.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                if not event.exists():
                    self.fail('child did not reach scheduling barrier: ' + str(cmd))
            self.assertEqual((self.node / '.rund-root.json').read_bytes(), b'')
            self.assertIsNone(children[0].poll())
            self.assertIsNone(children[1].poll())
            release.touch()
            for proc, cmd in zip(children, commands):
                out, err = proc.communicate(timeout=60)
                result = {'command': cmd, 'environment': ENV, 'cwd': str(BASE / 'cwd unrelated'),
                          'rc': proc.returncode, 'stdout': out, 'stderr': err, 'pid': proc.pid}
                results.append(result)
                with EVENTS.open('a') as f:
                    f.write(json.dumps(result) + '\n')
            self.assertEqual([r['rc'] for r in results], [0, 0], results)
            prepared = [json.loads(r['stdout']) for r in results]
            self.assertEqual(sorted(r['cacheHit'] for r in prepared), [False, True])
            self.assertEqual(prepared[0]['code'], prepared[1]['code'])
            self.assertEqual(prepared[0]['manifest'], prepared[1]['manifest'])
            manifest = json.loads(Path(prepared[0]['manifest']).read_text())
            self.assertTrue(manifest['complete'])
            self.assertEqual(manifest['inputs']['declaration'], self.task)
            self.assertEqual(len(list((self.node / 'manifests').glob('*.json'))), 1)
            self.assertTrue(self.cli()['cacheHit'])
        finally:
            release.touch()
            for proc, cmd in zip(children, commands):
                if proc.poll() is None:
                    proc.kill()
                if proc.stdout is not None and not proc.stdout.closed:
                    out, err = proc.communicate(timeout=10)
                    results.append({'command': cmd, 'rc': proc.returncode, 'stdout': out, 'stderr': err, 'cleanup': True})
            (self.area / 'interleave-results.json').write_text(json.dumps(results, indent=2))

    def test_nonobject_final_manifest_refused(self):
        r = self.cli()
        manifest = Path(r['manifest']); original = manifest.read_bytes()
        for value in (None, [], [1], 'text', 0, 1.5, True, False):
            with self.subTest(value=value):
                bad = json.dumps(value).encode()
                manifest.write_bytes(bad)
                p = self.cli(good=False)
                self.assertIn('manifest must be a JSON object', p.stderr)
                self.assertEqual(p.stdout, '')
                self.assertEqual(manifest.read_bytes(), bad)
                self.assertEqual(len(list((self.node / 'manifests').glob('*.json'))), 1)
        manifest.write_bytes(original)
        self.assertTrue(self.cli()['cacheHit'])

    def test_root_marker_rejections(self):
        for index, bad in enumerate((b'', b'{broken', b'null', b'{"format":"not-our-root"}')):
            root = self.area / ('bad-marker-' + str(index)); root.mkdir()
            marker = root / '.rund-root.json'; marker.write_bytes(bad)
            self.cli(root=root, good=False)
            self.assertEqual(marker.read_bytes(), bad)
            self.assertFalse((root / 'cache').exists())
        bait = self.area / 'marker-bait'; bait.write_text('untouched')
        for name in ('symlink', 'hardlink', 'directory'):
            root = self.area / name; root.mkdir()
            marker = root / '.rund-root.json'
            if name == 'symlink':
                marker.symlink_to(bait)
            elif name == 'hardlink':
                os.link(bait, marker)
            else:
                marker.mkdir()
            self.cli(root=root, good=False)
            self.assertFalse((root / 'cache').exists())
        self.assertEqual(bait.read_text(), 'untouched')

    def test_half_manifest_refused(self):
        r = self.cli()
        p = Path(r['manifest']); p.write_text('{"complete":false}')
        self.cli(good=False)
        self.assertEqual(p.read_text(), '{"complete":false}')

    def test_corrupted_code_not_hit(self):
        r = self.cli()
        p = Path(r['code']) / 'main/hello.txt'
        p.chmod(0o644); p.write_text('corrupt')
        self.cli(good=False)

    def test_cache_config_and_object_corruption_refused(self):
        r = self.cli()
        db = next(p for p in (self.node / 'cache').iterdir() if not p.name.startswith('.'))
        config = db / 'config'
        original = config.read_bytes()
        config.write_bytes(original + b'\n[core]\n hooksPath = /nonexistent/synthetic\n')
        self.cli(good=False)
        config.write_bytes(original)
        self.assertTrue(self.cli()['cacheHit'])
        # Corrupt a disposable source object in our own cloned fixture only.
        source = self.area / 'bad-object'
        run(['git', 'clone', '--no-hardlinks', self.repo, source])
        obj = source / '.git/objects' / self.blob[:2] / self.blob[2:]
        obj.chmod(0o644); obj.write_bytes(b'not a zlib Git object')
        d = copy.deepcopy(self.task); d['spec']['sources'][0]['repository'] = source.as_uri()
        self.cli(d, good=False)

    def test_directory_and_manifest_tampering_refused(self):
        r = self.cli()
        src = Path(r['code']) / 'main'
        src.chmod(0o755)
        self.cli(good=False)
        extra = src / 'unexpected-empty-directory'; extra.mkdir()
        src.chmod(0o555)
        self.cli(good=False)
        mpath = Path(r['manifest']); manifest = json.loads(mpath.read_text())
        manifest['codeDirectory'] = '../../harmless-bait'
        mpath.write_text(json.dumps(manifest))
        self.cli(good=False)

    def test_protocol_and_symlink_boundaries(self):
        r = self.cli()
        self.assertEqual(list((self.node / 'protocol').iterdir()), [])
        for n in ('cache', 'code', 'manifests', 'work', 'state', 'protocol'):
            self.assertFalse((self.node / n).is_symlink())
        # Only own harmless target, never production paths.
        bait = self.area / 'harmless-bait'; bait.mkdir()
        new = self.area / 'node-link'; new.symlink_to(bait, target_is_directory=True)
        self.cli(root=new, good=False)
        self.assertEqual(list(bait.iterdir()), [])
        (self.node / 'protocol').rename(self.node / 'protocol-original')
        (self.node / 'protocol').symlink_to(bait, target_is_directory=True)
        self.cli(good=False)
        self.assertEqual(list(bait.iterdir()), [])
        unmanaged = self.area / 'unmanaged'; unmanaged.mkdir(); (unmanaged / 'keep').write_text('bait')
        self.cli(root=unmanaged, good=False)
        self.assertEqual(set(p.name for p in unmanaged.iterdir()), {'keep'})
        self.assertFalse(self.marker.exists())

    def test_git_config_hooks_filters_never_execute(self):
        source = self.area / 'malicious-config-fixture'
        run(['git', 'clone', self.repo, source])
        mark = self.area / 'GIT_MUST_NOT_EXECUTE'
        command = f'touch "{mark}"'
        git(source, 'config', 'core.fsmonitor', command)
        git(source, 'config', 'uploadpack.packObjectsHook', command)
        git(source, 'config', 'core.sshCommand', command)
        git(source, 'config', 'filter.bait.smudge', command)
        hook = source / '.git/hooks/post-checkout'; hook.write_text('#!/bin/sh\n' + command + '\n'); hook.chmod(0o755)
        # Source config can include a nonexistent file: it must not be read at all.
        with (source / '.git/config').open('a') as f:
            f.write('\n[include]\n\tpath = /nonexistent/rund-synthetic-config\nthis is deliberately invalid git config\n')
        global_config = self.area / 'global.gitconfig'
        global_config.write_text('[alias]\n version = !' + command + '\n')
        env = dict(ENV, GIT_CONFIG_GLOBAL=str(global_config), GIT_CONFIG_SYSTEM=str(global_config), GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.hooksPath', GIT_CONFIG_VALUE_0=str(source / '.git/hooks'))
        d = copy.deepcopy(self.task); d['spec']['sources'][0]['repository'] = source.as_uri()
        self.cli(d, env=env)
        self.assertFalse(mark.exists())
        self.assertFalse(self.marker.exists())

    def test_git_special_tree_modes_and_lfs_rejected(self):
        for mode in ('symlink', 'submodule', 'lfs'):
            source = self.area / mode
            run(['git', 'clone', self.repo, source])
            path = source / 'unsupported'
            if mode == 'symlink':
                path.symlink_to('hello.txt')
                git(source, 'add', '--', 'unsupported')
            elif mode == 'submodule':
                git(source, 'update-index', '--add', '--cacheinfo', '160000,' + self.pin + ',unsupported')
            else:
                path.write_text('version https://git-lfs.github.com/spec/v1\noid sha256:' + '0'*64 + '\nsize 1\n')
                git(source, 'add', '--', 'unsupported')
            msg = self.area / 'message'; msg.write_text('synthetic unsupported tree')
            # Index-only gitlink fixture needs ordinary full-index commit in the isolated fixture, not a shared repository.
            git(source, '-c', 'user.name=Synthetic', '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '-F', msg)
            d = copy.deepcopy(self.task); d['spec']['sources'][0].update(repository=source.as_uri(), revision=git(source, 'rev-parse', 'HEAD'))
            self.cli(d, good=False)

    def test_git_attributes_filters_never_execute(self):
        source = self.area / 'attribute-fixture'
        run(['git', 'clone', self.repo, source])
        mark = self.area / 'FILTER_MUST_NOT_EXECUTE'
        (source / '.gitattributes').write_text('hello.txt filter=bait\n')
        git(source, 'add', '--', '.gitattributes')
        msg = self.area / 'attribute-message'; msg.write_text('synthetic attributes fixture')
        git(source, '-c', 'user.name=Synthetic', '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '-F', msg, '--', '.gitattributes')
        pin = git(source, 'rev-parse', 'HEAD')
        git(source, 'config', 'filter.bait.smudge', f'touch "{mark}"')
        git(source, 'config', 'filter.bait.required', 'true')
        d = copy.deepcopy(self.task); d['spec']['sources'][0].update(repository=source.as_uri(), revision=pin)
        r = self.cli(d)
        self.assertEqual((Path(r['code']) / 'main/hello.txt').read_text(), 'pinned content\n')
        self.assertFalse(mark.exists())

    def test_source_alternates_rejected(self):
        source = self.area / 'alternate'
        run(['git', 'clone', '--bare', self.repo, source])
        (source / 'objects/info/alternates').write_text(str(self.repo / '.git/objects') + '\n')
        self.bad(lambda d: d['spec']['sources'][0].update(repository=source.as_uri()), 'alternate')

    def test_tools_and_contract_change_key(self):
        a = self.cli()
        d = copy.deepcopy(self.task); d['metadata']['version'] += 1
        b = self.cli(d)
        self.assertNotEqual(a['key'], b['key'])
        d = copy.deepcopy(self.task); d['spec']['sources'][0]['lockFiles'] = []
        c = self.cli(d)
        self.assertNotEqual(a['key'], c['key'])
        m = json.loads(Path(a['manifest']).read_text())
        for identity in m['inputs']['tools'].values():
            self.assertTrue(Path(identity['path']).is_absolute())
            self.assertEqual(len(identity['sha256']), 64)
            self.assertTrue(identity['version'])


if __name__ == '__main__':
    print('retained_root=' + str(BASE), flush=True)
    print('python=' + sys.executable + ' uid=' + str(os.geteuid()), flush=True)
    unittest.main(verbosity=2)
