"""No per-run files/FDs: ordinary helpers, conservative unknown and real wait.

Fresh retained Linux roots; inherited strong-identity/pidfd cleanup only.
State-sample fault tests never mock a wait result or read fixture facts in S.
"""
import copy
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from runtime import test_lifecycle as fixtures
from runtime.test_exec import ExecTests
from runtime.kernel import proc, read_json, same_identity


class NoLeaseTests(ExecTests):
    def inspect_payload(self, work):
        root=read_json(work/'ready'); child=read_json(work/'child-ready')
        for identity in (root,child):
            self.assertEqual(set(identity['fds']),{'0','1','2'})
            self.assertNotIn('RUND_LEASE_FD',identity['environment'])
            self.assertEqual((identity['pgid'],identity['session'],identity['uid']),
                             (root['pgid'],root['session'],root['uid']))
        self.assertFalse(list((self.root/'state/runs').rglob('*.lock')))
        self.evidence.append({'event':'ordinary-close-fds-helper','root':root,'child':child})
        return root,child

    def helper_stop(self, ignore):
        r=self.runtime(); actual_popen=subprocess.Popen
        with patch('runtime.kernel.subprocess.Popen',wraps=actual_popen) as calls:
            run=r.accept_task(self.declaration_exec(role='subprocess-parent',ignore=ignore))
        spawns=[c for c in calls.call_args_list if c.kwargs.get('start_new_session')]
        self.assertEqual(len(spawns),1)
        self.assertNotIn('pass_fds',spawns[0].kwargs)
        work=self.ready(run); self.inspect_payload(work)
        done=r.control(self.ctl(),term_seconds=.1,kill_seconds=2)
        self.assertEqual((done['phase'],done['waitObserved']),('canceled',True))
        self.assertEqual((done['exitCode'],done['signal']),(None,9) if ignore else (0,None))
        self.assertEqual([e['signal'] for e in done['stopEvidence'] if e['event']=='signal'],[15,9] if ignore else [15])
        self.assertFalse(any(m['state'] not in ('Z','X') for m in done['stopEvidence'][-1]['members']))
        self.evidence.append({'event':'ordinary-helper-stop','result':done})

    def test_no_lease_helper_term(self): self.helper_stop(False)
    def test_no_lease_helper_kill(self): self.helper_stop(True)

    def test_no_lease_helper_blocks_service_after_leader_exit(self):
        r=self.runtime(); r.submit_service(self.declaration_exec(kind='Service',role='subprocess-parent',policy='always'),'submit')
        run=r._service_current(r.service('demo')); work=self.ready(run); root,child=self.inspect_payload(work)
        (work/'exit-leader').touch()
        fixtures.wait_for(lambda:(p:=proc(run['pid'])) and p['state']=='Z')
        for _ in range(3):
            head=r.tick_service('demo')
            self.assertEqual((head['generation'],head['phase']),(1,'cleanupUnknown'))
        current=r._service_current(head)
        self.assertEqual((current['observation']['exitCode'],current['observation']['waitObserved']),(0,True))
        self.assertNotIn(r.kernel.key(run),r.kernel.processes)
        with patch('runtime.kernel.os.killpg',side_effect=AssertionError('absent leader cannot authorize kill')):
            head=r.control(self.ctl(kind='Service',action='stop'),term_seconds=0,kill_seconds=0)
        self.assertEqual(head['phase'],'cleanupUnknown')
        self.assertTrue(same_identity(proc(child['pid']),child))
        self.assertFalse((r.kernel.run_dir(run)/'result.json').exists())
        (work/'release-child').touch()
        fixtures.wait_for(lambda:(p:=proc(child['pid'])) is None or p['state']=='Z')
        head=r.tick_service('demo'); self.assertEqual((head['phase'],head['generation'],head['hold']),('stopped',1,True))
        self.evidence.append({'event':'residual-helper-converged','head':head,'result':read_json(r.kernel.run_dir(run)/'result.json')})

    def unknown_starting(self, kind, event):
        d=self.declaration_exec(kind=kind,role='subprocess-parent',policy='always')
        self.crash_driver(d,event)
        attempt='a1' if kind=='Task' else 'service'
        r=self.runtime(); run=r.read_run(kind,'demo',attempt,1)
        self.assertIsNone(run['pid'])
        for _ in range(2):
            observed=r.observe_task('demo','a1') if kind=='Task' else r._service_current(r.tick_service('demo'))
            self.assertEqual(observed['phase'],'cleanupUnknown')
            self.assertEqual(observed['reason'],'starting-identity-unavailable')
            self.assertFalse(observed['waitObserved'])
        with patch('runtime.kernel.os.killpg',side_effect=AssertionError('no recorded identity')):
            stopped=r.control(self.ctl(kind=kind,action='cancel' if kind=='Task' else 'stop'),term_seconds=0,kill_seconds=0)
        self.assertEqual(stopped['phase'],'cleanupUnknown')
        if kind=='Task':
            self.assertEqual(r.accept_task(d)['phase'],'cleanupUnknown')
            self.assertEqual(r.start_task('demo','a1')['phase'],'cleanupUnknown')
            with self.assertRaisesRegex(ValueError,'old attempt must be terminal'):
                r.rerun_task('demo','a1',self.declaration_exec(attempt='a2',request='a2'))
        for identity in list(self.identities.values()):
            self.independent_kill(identity)
        after=r.observe_task('demo','a1') if kind=='Task' else r._service_current(r.tick_service('demo'))
        self.assertEqual(after['phase'],'cleanupUnknown')
        self.assertFalse((r.kernel.run_dir(run)/'result.json').exists())
        self.evidence.append({'event':'starting-stays-unknown','kind':kind,'barrier':event,'result':after})

    def test_no_lease_task_pre_spawn(self): self.unknown_starting('Task','starting')
    def test_no_lease_task_post_spawn(self): self.unknown_starting('Task','spawned')
    def test_no_lease_service_pre_spawn(self): self.unknown_starting('Service','starting')
    def test_no_lease_service_post_spawn(self): self.unknown_starting('Service','spawned')

    def test_no_lease_real_owner_loss_and_residual_helper(self):
        self.crash_driver(self.declaration_exec(role='subprocess-parent'))
        run=read_json(self.root/'state/runs/Task/demo/a1/1/run.json')
        work=Path(run['cwd']); root,child=self.inspect_payload(work)
        (work/'exit-leader').touch()
        fixtures.wait_for(lambda:(p:=proc(root['pid'])) and p['state']=='Z')
        r=self.runtime(); self.assertEqual(r.kernel.processes,{})
        with patch('runtime.kernel.os.killpg',side_effect=AssertionError('no leader')):
            stopped=r.control(self.ctl(),term_seconds=0,kill_seconds=0)
        self.assertEqual(stopped['phase'],'cleanupUnknown')
        self.assertFalse(stopped['waitObserved'])
        (work/'release-child').touch()
        fixtures.wait_for(lambda:(p:=proc(child['pid'])) is None or p['state']=='Z')
        done=r.wait_task('demo','a1',0)
        self.assertEqual((done['phase'],done['exitCode'],done['signal'],done['waitObserved']),('lost',None,None,False))
        self.evidence.append({'event':'real-owner-loss','result':done})

    def test_no_lease_node_lock_still_exclusive(self):
        r=self.runtime(); run=r.accept_task(self.declaration_exec(role='subprocess-parent'))
        work=self.ready(run); self.inspect_payload(work)
        self.assertTrue((self.root/'state/owners/node.lock').is_file())
        p=fixtures.command([sys.executable,'-B','-c',
            'from runtime.lifecycle import Runtime; Runtime(__import__("sys").argv[1],"node-a")',self.root],good=False)
        self.assertNotEqual(p.returncode,0); self.assertIn('BlockingIOError',p.stderr)
        self.assertEqual(r.control(self.ctl(),term_seconds=.2)['phase'],'canceled')

    def test_no_lease_empty_sample_does_not_replace_owned_wait(self):
        r=self.runtime(); run=r.accept_task(self.declaration_exec()); self.ready(run)
        p=r.kernel.processes[r.kernel.key(run)]; real_poll=p.poll; calls=[]
        def poll():
            rc=real_poll(); calls.append(rc); return rc
        def stale(pid):
            a=proc(pid)
            return dict(a,state='Z') if a and pid==p.pid else a
        with patch.object(p,'poll',poll), patch('runtime.kernel.proc',side_effect=stale), patch('runtime.kernel.group_members',return_value=[]):
            done=r.wait_task('demo','a1',0)
        self.assertEqual(calls,[None,None])  # real wait, synthetic stale state/empty scan
        self.assertEqual((done['phase'],done['reason'],done['waitObserved']),('cleanupUnknown','owned-wait-unavailable',False))
        self.assertFalse((r.kernel.run_dir(run)/'result.json').exists())
        self.evidence.append({'event':'synthetic-stale-sample-real-poll','calls':calls,'result':done})
        self.assertEqual(r.control(self.ctl(),term_seconds=.2)['phase'],'canceled')


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(NoLeaseTests(n) for n in sorted(NoLeaseTests.__dict__) if n.startswith('test_no_lease_'))


if __name__=='__main__':
    unittest.main(verbosity=2)
