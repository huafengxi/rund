import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import publish
from runtime.engine import Engine
from runtime.protocol import get,desired
from runtime.admission import admission_path
from runtime.scheduling import Scheduler
from runtime.store import read,write


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.base=Path(tempfile.mkdtemp(prefix='rund-scheduling-'))
        self.root=self.base/'shared';self.engines=[]
        for node in ('a','b'):
            c=dict(node=node,sharedRoot=str(self.root),privateRoot=str(self.base/node),python=sys.executable,home=str(self.base),path=os.environ['PATH'],publishers={'operator':'a','rund-a':'a','rund-b':'b','scheduler':'a'},version=1,admission={'publisher':'scheduler'})
            write(self.base/(node+'.json'),c)
            self.engines.append(Engine(c))
        self.policy=dict(nodeConfig=str(self.base/'a.json'),publisher='scheduler',maxConcurrent=2,interval=.02)
        self.scheduler=Scheduler(self.policy)
    def tearDown(self):
        if self.scheduler:self.scheduler.close()
        for o in self.engines:
            for p in o.processes.handles.values():
                if p.poll() is None:
                    try:os.killpg(p.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                p.wait(timeout=5)
            o.close()
    def put(self,tid,node,resources):
        d=dict(kind='Task',id=tid,publisher='operator',node=node,revision=1,desired='run',resources=resources,argv=[sys.executable,'-c','import time;time.sleep(100)'])
        publish(self.root,d,writer='a',publishers=self.engines[0].c['publishers'])
    def state(self,tid):return get(self.root,tid)[1]
    def rounds(self,n=3):
        for _ in range(n):
            if self.scheduler:self.scheduler.round()
            for o in self.engines:o.round()
    def until(self,fn):
        end=time.monotonic()+8
        while time.monotonic()<end:
            self.rounds(1)
            if fn():return
            time.sleep(.03)
        self.fail(str([o.errors for o in self.engines]))
    def test_cross_node_resource_and_wait_release(self):
        self.put('first','a',['repo']);self.rounds()
        self.put('second','b',['repo']);self.rounds()
        self.assertEqual(self.state('first')['phase'],'running')
        self.assertEqual(self.state('second')['phase'],'queued')
        desired(self.root,'first','cancel',writer='a',publishers=self.engines[0].c['publishers'],mode='kill')
        self.until(lambda:self.state('second')['phase']=='running')
        self.assertTrue(self.state('first')['waitObserved'])
        self.assertEqual(self.state('first')['phase'],'canceled')
    def test_host_resource_names_are_opaque_and_unknown_holds(self):
        self.put('one','a',['a-repo']);self.put('two','b',['b-repo']);self.rounds()
        self.assertEqual(self.state('one')['phase'],'running');self.assertEqual(self.state('two')['phase'],'running')
        self.put('three','b',[]);self.rounds()
        self.assertEqual(self.state('three')['phase'],'queued')
        grants=read(admission_path(self.engines[0].c))['grants']
        self.assertEqual(set(grants),{'one','two'})
        # Lose wait authority without terminating the process: it must retain its slot.
        handle=self.engines[0].processes.handles.pop('one')
        handle.terminate()
        handle.wait(timeout=5)
        self.rounds()
        self.assertEqual(self.state('one')['phase'],'unknown')
        self.assertEqual(self.state('three')['phase'],'queued')

    def test_engines_never_schedule_and_empty_resources_still_need_a_grant(self):
        self.put('waiting','a',[])
        for _ in range(4):
            for engine in self.engines:engine.round()
        self.assertEqual(self.state('waiting')['phase'],'queued')
        self.assertFalse(admission_path(self.engines[0].c).exists())
        self.rounds()
        self.assertEqual(self.state('waiting')['phase'],'running')

    def test_scheduler_origin_and_single_writer_lock(self):
        with self.assertRaises((OSError,ValueError)):Scheduler(self.policy)
        with self.assertRaises(ValueError):Scheduler(dict(self.policy,nodeConfig=str(self.base/'b.json')))

    def test_missing_ledger_refuses_new_admission(self):
        self.put('running','a',[]);self.rounds()
        path=admission_path(self.engines[0].c);backup=path.read_bytes();path.unlink()
        try:
            self.put('waiting','b',[])
            for engine in self.engines:engine.round()
            with self.assertRaisesRegex(ValueError,'restore missing admission record'):self.scheduler.round()
            self.assertEqual(self.state('waiting')['phase'],'queued')
        finally:path.write_bytes(backup)

    def test_scheduler_is_an_ordinary_service_and_can_restart_independently(self):
        self.scheduler.close();self.scheduler=None
        config=self.base/'scheduler.json';write(config,self.policy)
        declaration=dict(kind='Service',id='scheduler-service',publisher='operator',node='a',revision=1,
                         version=1,desired='online',restart='always',task=dict(argv=[sys.executable,'-B',
                         str(Path(__file__).resolve().parents[1]/'tools/scheduler.py'),'--config',str(config)]))
        publish(self.root,declaration,writer='a',publishers=self.engines[0].c['publishers'])
        self.put('first','b',['repo'])
        self.until(lambda:self.state('first') and self.state('first')['phase']=='running')
        child=get(self.root,'scheduler-service')[1]['currentTask']
        self.assertNotIn('resources',get(self.root,child)[0])
        desired(self.root,'scheduler-service','offline',writer='a',publishers=self.engines[0].c['publishers'])
        self.until(lambda:get(self.root,'scheduler-service')[1]['phase']=='stopped')
        self.assertTrue(self.state(child)['waitObserved'])
        self.put('second','a',[]);self.rounds()
        self.assertEqual(self.state('second')['phase'],'queued')
        self.assertEqual(self.state('first')['phase'],'running')
        first_process=self.state('first')['process']
        grants=read(admission_path(self.engines[0].c))['grants']
        desired(self.root,'scheduler-service','online',writer='a',publishers=self.engines[0].c['publishers'])
        self.until(lambda:self.state('second')['phase']=='running')
        self.assertEqual(self.state('first')['process'],first_process)
        self.assertIn('first',grants)
        self.assertIn('first',read(admission_path(self.engines[0].c))['grants'])
        self.assertNotEqual(get(self.root,'scheduler-service')[1]['currentTask'],child)

if __name__=='__main__':unittest.main()
