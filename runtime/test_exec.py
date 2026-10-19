"""Real root/descendant exec preserves identity and conservative group/wait gates.

Only new exec tests are selected; original lifecycle43/owned-wait15 run apart.
The retained harness provides isolated roots and strong-identity cleanup only.
"""
import json
from pathlib import Path
import signal
import sys
import time
import unittest
from unittest.mock import patch

from runtime import test_lifecycle as fixtures
from runtime.kernel import proc, read_json, same_identity


class ExecTests(fixtures.LifecycleTests):
    @classmethod
    def setUpClass(cls):
        cls.source = fixtures.BASE / 'immutable exec source'
        cls.source.mkdir()
        fixtures.command(['git', '-C', cls.source, 'init', '-q', '--template='])
        def git(*args):
            top = fixtures.command(['git', '-C', cls.source, 'rev-parse', '--show-toplevel']).stdout.strip()
            common = fixtures.command(['git', '-C', cls.source, 'rev-parse', '--git-common-dir']).stdout.strip()
            if Path(top).resolve() != cls.source.resolve() or (cls.source/common).resolve() != cls.source/'.git':
                raise AssertionError('fixture Git root mismatch')
            return fixtures.command(['git', '-C', cls.source, *args])
        (cls.source/'fixture.py').write_bytes((fixtures.REPO/'runtime/exec_payload.py').read_bytes())
        git('add', '--', 'fixture.py')
        message = fixtures.BASE/'exec-fixture-message'
        message.write_text('6ggidb isolated ordinary exec fixture\n')
        git('-c', 'user.name=Synthetic', '-c', 'user.email=fixture@example.invalid',
            'commit', '-q', '-F', message, '--', 'fixture.py')
        cls.pin = git('rev-parse', 'HEAD').stdout.strip()
        (fixtures.BASE/'environment.json').write_text(json.dumps({
            'python':sys.executable, 'env':fixtures.ENV, 'source':str(cls.source),
            'sourceCommit':cls.pin, 'subreaper':True}, indent=2))

    def declaration_exec(self, kind='Task', role='root', code=0, ignore=False, **kw):
        d = self.declaration(kind=kind, mode=role, code=code, **kw)
        d['spec']['argv'].append('1' if ignore else '0')
        return d

    def check_exec(self, work, child=False):
        before = read_json(work/('child-before-exec' if child else 'before-exec'))
        after = read_json(work/('child-ready' if child else 'ready'))
        self.assertTrue(same_identity(after, before))
        self.assertNotIn('RUND_LEASE_FD', after['environment'])
        self.assertEqual(set(after['fds']), {'0','1','2'})
        self.assertNotEqual(after['argv'][1], before['argv'][1])
        self.evidence.append({'event':'real-exec', 'before':before, 'after':after})
        return after

    def exit_exec(self, code):
        r = self.runtime(); d = self.declaration_exec(code=code)
        run = r.accept_task(d); work = self.ready(run)
        self.check_exec(work)
        (work/'release').touch()
        done = r.wait_task('demo','a1',5)
        self.assertEqual((done['phase'],done['exitCode'],done['waitObserved']),
                         ('succeeded' if code==0 else 'failed',code,True))
        self.assertNotIn('leaseBusy', done['observation'])
        self.assertFalse(any(p['state'] not in ('Z','X') for p in done['observation']['members']))
        saved = (r.kernel.run_dir(run)/'result.json').read_bytes()
        self.assertEqual(r.accept_task(d),done)
        self.assertEqual((r.kernel.run_dir(run)/'result.json').read_bytes(),saved)
        self.evidence.append({'event':'exec-result','result':done})

    def test_exec_root_zero(self): self.exit_exec(0)
    def test_exec_root_nonzero(self): self.exit_exec(7)

    def cancel_exec(self, child):
        r=self.runtime(); run=r.accept_task(self.declaration_exec(role='parent' if child else 'root',ignore=True))
        work=self.ready(run); self.check_exec(work,child)
        done=r.control(self.ctl(),term_seconds=.05,kill_seconds=2)
        self.assertEqual((done['phase'],done['signal'],done['waitObserved']),('canceled',signal.SIGKILL,True))
        fact=done['stopEvidence'][-1]
        self.assertEqual(fact['status'],'exited')
        self.assertNotIn('leaseBusy', fact)
        self.assertFalse(any(p['state'] not in ('Z','X') for p in fact['members']))
        self.evidence.append({'event':'exec-cancel','child':child,'result':done})

    def test_exec_root_cancel(self): self.cancel_exec(False)
    def test_exec_descendant_cancel(self): self.cancel_exec(True)

    def test_exec_descendant_blocks_always_and_blind_kill(self):
        r=self.runtime(); r.submit_service(self.declaration_exec(kind='Service',role='parent',policy='always'),'submit-1')
        run=r._service_current(r.service('demo')); work=self.ready(run)
        child=self.check_exec(work,True)
        (work/'exit-leader').touch()
        fixtures.wait_for(lambda:(a:=proc(run['pid'])) and a['state']=='Z')
        for _ in range(3):
            blocked=r.tick_service('demo')
            self.assertEqual((blocked['phase'],blocked['generation']),('cleanupUnknown',1))
        self.assertTrue(same_identity(proc(child['pid']),child))
        with patch('runtime.kernel.os.killpg',side_effect=AssertionError('no live leader: no kill authority')):
            blocked=r.control(self.ctl(kind='Service',action='stop',attempt='service'),term_seconds=0,kill_seconds=0)
        self.assertEqual(blocked['phase'],'cleanupUnknown')
        self.assertFalse((r.kernel.run_dir(run)/'result.json').exists())
        (work/'release-child').touch()
        fixtures.wait_for(lambda:(a:=proc(child['pid'])) is None or a['state']=='Z')
        done=r.tick_service('demo')
        self.assertEqual((done['phase'],done['generation'],done['hold']),('stopped',1,True))
        result=read_json(r.kernel.run_dir(run)/'result.json')
        self.assertEqual((result['exitCode'],result['waitObserved']),(0,True))
        self.evidence.append({'event':'exec-descendant-block-and-release','result':result,'head':done})

    def test_exec_reopened_owner_has_no_wait(self):
        self.crash_driver(self.declaration_exec())
        run=read_json(self.root/'state/runs/Task/demo/a1/1/run.json')
        work=Path(run['cwd']); self.check_exec(work)
        r=self.runtime()
        self.assertEqual(r.kernel.processes,{})
        self.assertTrue(same_identity(proc(run['pid']),run))
        (work/'release').touch()
        fixtures.wait_for(lambda:(a:=proc(run['pid'])) and a['state']=='Z')
        done=r.wait_task('demo','a1',0)
        self.assertEqual((done['phase'],done['exitCode'],done['signal'],done['waitObserved']),('lost',None,None,False))
        self.evidence.append({'event':'exec-owner-reopened','result':done})

    def always_exec(self, code):
        r=self.runtime(); r.submit_service(self.declaration_exec(kind='Service',code=code,policy='always'),'submit-1')
        run=r._service_current(r.service('demo')); work=self.ready(run); self.check_exec(work)
        (work/'release').touch()
        def backoff():
            h=r.tick_service('demo')
            return h if h['phase']=='backoff' else None
        head=fixtures.wait_for(backoff)
        old=read_json(r.kernel.run_dir(run)/'result.json')
        self.assertEqual((old['exitCode'],old['waitObserved']),(code,True))
        with patch('runtime.lifecycle.time.time',return_value=head['nextStartAt']+.01):
            head=r.tick_service('demo')
        self.assertEqual(head['generation'],2)
        new=r._service_current(head); work=self.ready(new); self.check_exec(work)
        done=r.control(self.ctl(kind='Service',action='stop',attempt='service',gen=2),term_seconds=.2,kill_seconds=2)
        self.assertEqual((done['phase'],done['hold']),('stopped',True))
        self.evidence.append({'event':'exec-always','old':old,'head':done})

    def test_exec_always_zero(self): self.always_exec(0)
    def test_exec_always_nonzero(self): self.always_exec(7)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ExecTests(name) for name in sorted(ExecTests.__dict__) if name.startswith('test_exec_'))


if __name__=='__main__':
    unittest.main(verbosity=2)
