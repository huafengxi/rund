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
from runtime.admission import admission_path,grants as active_grants
from runtime.scheduling import Scheduler
from runtime.store import read,write


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.base=Path(tempfile.mkdtemp(prefix='rund-scheduling-'))
        self.root=self.base/'shared';self.root.mkdir();self.engines=[]
        for node in ('a','b'):
            root=self.base/node;(root/'config').mkdir(parents=True)
            import socket
            (root/'config/host-id').write_text((socket.gethostname()+' a\n') if node=='a' else 'hub-host a\n'+socket.gethostname()+' b\n')
            (root/'shared').symlink_to(self.root,target_is_directory=True)
            if node=='a':(root/'config/objects').mkdir()
            else:(root/'config/objects').symlink_to(self.base/'a/config/objects',target_is_directory=True)
            from runtime.config import node_config
            c=node_config(root)
            self.engines.append(Engine(c))
        self.scheduler=Scheduler(self.base/'a',max_concurrent=2,interval=.02)
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
        d=dict(kind='Task',id=tid,node=node,desired='run',resources=resources,argv=[sys.executable,'-c','import time;time.sleep(100)'])
        publish(self.root,d)
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
        desired(self.root,'first','cancel',mode='kill')
        self.until(lambda:self.state('second')['phase']=='running')
        self.assertTrue(self.state('first')['waitObserved'])
        self.assertEqual(self.state('first')['phase'],'canceled')
    def test_host_resource_names_are_opaque_and_unknown_holds(self):
        self.put('one','a',['a-repo']);self.put('two','b',['b-repo']);self.rounds()
        self.assertEqual(self.state('one')['phase'],'running');self.assertEqual(self.state('two')['phase'],'running')
        self.put('three','b',[]);self.rounds()
        self.assertEqual(self.state('three')['phase'],'queued')
        grants=active_grants(self.engines[0].c)
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
        self.assertFalse(active_grants(self.engines[0].c))
        self.rounds()
        self.assertEqual(self.state('waiting')['phase'],'running')

    def test_cancel_queued_task_does_not_take_a_grant(self):
        self.put('canceled','a',[])
        for engine in self.engines:engine.round()
        desired(self.root,'canceled','cancel')
        self.scheduler.round()
        self.assertNotIn('canceled',active_grants(self.engines[0].c))
        self.rounds()
        self.assertEqual(self.state('canceled')['phase'],'canceled')
        self.assertFalse(self.engines[0].processes.handles)

    def test_scheduler_origin_and_single_writer_lock(self):
        with self.assertRaises((OSError,ValueError)):Scheduler(self.base/'a')
        with self.assertRaises(ValueError):Scheduler(self.base/'b')

    def test_missing_ledger_refuses_new_admission(self):
        self.put('running','a',[]);self.rounds()
        path=self.root/'tasks/running/grant.json';backup=path.read_bytes();path.unlink()
        try:
            self.put('waiting','b',[])
            for engine in self.engines:engine.round()
            with self.assertRaisesRegex(ValueError,'restore missing admission record'):self.scheduler.round()
            self.assertEqual(self.state('waiting')['phase'],'queued')
        finally:path.write_bytes(backup)

    def test_scheduler_is_an_ordinary_service_and_can_restart_independently(self):
        self.scheduler.close();self.scheduler=None
        declaration=dict(kind='Service',id='scheduler-service',node='a',
                         desired='online',restart='always',argv=[sys.executable,'-B',
                         str(Path(__file__).resolve().parents[1]/'tools/scheduler.py'),'--root',str(self.base/'a'),'--max-concurrent','2'])
        from runtime.services import publish as put_service,get as get_service,set_intent
        c=self.engines[0].c;put_service(c,declaration)
        self.put('first','b',['repo']);self.until(lambda:self.state('first') and self.state('first')['phase']=='running')
        run=get_service(c,'scheduler-service')[1]['runId']
        set_intent(c,declaration,'offline');self.until(lambda:get_service(c,'scheduler-service')[1]['phase']=='stopped')
        self.assertTrue(get_service(c,'scheduler-service')[1]['waitObserved'])
        self.put('second','a',[]);self.rounds();self.assertEqual(self.state('second')['phase'],'queued')
        first_process=self.state('first')['process'];set_intent(c,declaration,'online')
        self.until(lambda:self.state('second')['phase']=='running')
        self.assertEqual(self.state('first')['process'],first_process)
        self.assertNotEqual(get_service(c,'scheduler-service')[1]['runId'],run)
        self.assertFalse(any(d.get('service') for _,d in self.engines[0].objects.values()))

    def test_services_share_resource_grants_without_child_tasks(self):
        from runtime.services import publish as put_service,get as get_service,set_intent
        def state(node,tid):return get_service(self.engines[node].c,tid)[1]
        ds=[]
        for i,node in enumerate(('a','b')):
            d=dict(kind='Service',id='svc-'+node,node=node,desired='online',restart='manual',
                   resources=['exclusive'],argv=[sys.executable,'-c','import time;time.sleep(100)'])
            put_service(self.engines[0].c,d);ds.append(d)
        self.until(lambda:state(0,'svc-a') and state(0,'svc-a')['phase']=='running')
        self.assertEqual(state(1,'svc-b')['phase'],'queued')
        set_intent(self.engines[0].c,ds[0],'offline')
        self.until(lambda:state(1,'svc-b')['phase']=='running')
        self.assertTrue(state(0,'svc-a')['waitObserved'])
        self.assertFalse(any(d['kind']=='Task' for _,d in self.engines[0].objects.values()))

    def test_offline_service_can_start_and_replace_its_resource_version(self):
        from runtime.services import publish as put_service,get as get_service,set_intent
        c=self.engines[0].c
        d=dict(kind='Service',id='resource-service',node='a',desired='offline',restart='manual',
               resources=['repo'],argv=[sys.executable,'-c','import time;time.sleep(100)'])
        put_service(c,d);set_intent(c,d,'online')
        self.until(lambda:(get_service(c,d['id'])[1] or {}).get('phase')=='running')
        first=get_service(c,d['id'])[1]['runId']
        updated=dict(d,desired='online',env={'VERSION':'2'});put_service(c,updated)
        self.until(lambda:(get_service(c,d['id'])[1] or {}).get('runId')!=first and get_service(c,d['id'])[1]['phase']=='running')
        self.assertNotEqual(first,get_service(c,d['id'])[1]['runId'])

if __name__=='__main__':unittest.main()
