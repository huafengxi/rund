import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import publish,changed
from runtime.engine import Engine
from runtime.process import identity,same,alive
from runtime.store import read,write
from runtime.protocol import get,desired

class Objects(unittest.TestCase):
    def setUp(self):
        self.base=Path(tempfile.mkdtemp(prefix='rund-objects-'))
        self.c=dict(node='a',sharedRoot=str(self.base/'shared'),privateRoot=str(self.base/'private'),python=sys.executable,home=str(self.base),path=os.environ['PATH'],hub='a')
        self.c.update(configRoot=str(self.base/'config'),servicesRoot=str(self.base/'config/objects/services'))
        self.o=Engine(self.c)
    def tearDown(self):
        for p in self.o.processes.handles.values():
            if p.poll() is None:
                try:os.killpg(p.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            p.wait(timeout=5)
        self.o.close()
    def put(self,d,path=None):
        if d['kind']=='Service':
            from runtime.services import publish as put_service
            return put_service(self.c,d)
        return publish(self.c['sharedRoot'],d,path=path)
    def task(self,id='task',argv=None,**kw):return dict(kind='Task',id=id,node='a',desired='run',argv=argv or [sys.executable,'-c','print("ok")'],**kw)
    def until(self,fn,seconds=8):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            self.o.round();v=fn()
            if v:return v
            time.sleep(.03)
        self.fail('timeout: '+str(self.o.errors))
    def state(self,id):
        from runtime.services import get as get_object
        return get_object(self.c,id)[1]
    def test_task_real_wait_and_terminal_freeze(self):
        self.put(self.task());s=self.until(lambda:(s if (s:=self.state('task')) and s['phase']=='succeeded' else None))
        self.assertTrue(s['waitObserved']);self.assertEqual(s['exitCode'],0)
        self.o.round();self.assertEqual(s,self.state('task'));self.assertNotIn('sequence',s)
    def test_file_dependency_and_cancel_before_start(self):
        self.put(self.task(needs=[str(self.base/'missing')]))
        self.o.round();self.assertEqual(self.state('task')['phase'],'preparing')
        desired(self.c['sharedRoot'],'task','cancel')
        self.o.round();s=self.state('task');self.assertEqual(s['phase'],'canceled');self.assertIsNone(s['process']);self.assertFalse(s['waitObserved'])
    def test_dependency_file_releases_execution(self):
        f=self.base/'ready';self.put(self.task(needs=[str(f)]));self.o.round();self.assertEqual(self.state('task')['phase'],'preparing')
        f.write_text('ready');self.until(lambda:self.state('task') and self.state('task')['phase']=='succeeded')
    def test_cancellation_real_wait(self):
        self.put(self.task(argv=[sys.executable,'-c','import time;time.sleep(100)']))
        self.until(lambda:self.state('task') and self.state('task')['phase']=='running')
        desired(self.c['sharedRoot'],'task','cancel',mode='kill')
        s=self.until(lambda:(s if (s:=self.state('task'))['phase']=='canceled' else None))
        self.assertTrue(s['waitObserved']);self.assertEqual(s['signal'],9)
    def test_task_immutable_execution(self):
        d=self.task();self.put(d)
        with self.assertRaises(ValueError):self.put(dict(d,argv=['bad']))
        with self.assertRaises(ValueError):publish(self.c['sharedRoot'],dict(d,desired='cancel'),revision=1)
        d=dict(d,desired='cancel');self.put(d)
        with self.assertRaises(ValueError):self.put(dict(d,desired='run'))
    def test_service_restart_has_direct_process_and_no_child_task(self):
        d=dict(kind='Service',id='svc',node='a',version=1,desired='online',restart='always',argv=[sys.executable,'-c','pass'])
        self.put(d);first=self.until(lambda:self.state('svc') and self.state('svc').get('runId'))
        self.until(lambda:self.state('svc')['runId']!=first)
        self.assertFalse(any(v['kind']=='Task' for _,v in self.o.objects.values()))
        from runtime.services import set_intent
        set_intent(self.c,d,'offline');self.until(lambda:self.state('svc')['phase']=='stopped')
        self.assertTrue(self.state('svc')['waitObserved'])
    def test_service_offline_then_online(self):
        d=dict(kind='Service',id='svc',node='a',version=1,desired='offline',restart='manual',argv=[sys.executable,'-c','import time;time.sleep(100)'])
        self.put(d);self.o.round();self.assertEqual(self.state('svc')['phase'],'stopped')
        from runtime.services import set_intent
        set_intent(self.c,d,'online');self.until(lambda:self.state('svc')['phase']=='running')
        first=self.state('svc')['runId'];set_intent(self.c,d,'offline');self.until(lambda:self.state('svc')['phase']=='stopped')
        set_intent(self.c,d,'online');self.until(lambda:self.state('svc')['phase']=='running' and self.state('svc')['runId']!=first)
    def test_kind_not_path(self):
        self.put(self.task(),self.base/'shared/anything/else/declaration-v1.json');self.until(lambda:self.state('task') and self.state('task')['phase']=='succeeded')
    def test_duplicate_identity_blocks(self):
        d=self.task();self.put(d);write(self.base/'shared/elsewhere/declaration-v1.json',d);self.o.round();self.assertTrue(self.o.errors)
    def test_second_executor_refused(self):
        with self.assertRaises(BlockingIOError):Engine(self.c)
    def test_unknown_executor_restart_does_not_spawn(self):
        p=self.put(self.task());s=self.o.initial(self.task());s.update(phase='starting',updatedAt='2020-01-01T00:00:00+00:00');write(p.with_name('state.json'),s)
        self.o.round();self.assertEqual(self.state('task')['phase'],'unknown');self.assertFalse(self.o.processes.handles)
    def test_shared_business_files_are_not_execution_objects(self):
        write(self.base/'shared/agents/task/demo/declaration-v1.json',{'business':'opaque'})
        self.put(self.task());self.until(lambda:self.state('task') and self.state('task')['phase']=='succeeded')
        self.assertFalse(self.o.errors)
    def test_credentials_wrapper_redacts_and_execs(self):
        secret='fixture-secret-never-in-state';f=self.base/'env';f.write_text('KEY='+secret)
        cfg=self.base/'creds.json';write(cfg,dict(files=[dict(file=str(f),loader=[])],require=['KEY']))
        tool=Path(__file__).resolve().parents[1]/'tools/env_exec.py'
        self.put(self.task(argv=[sys.executable,str(tool),'--config',str(cfg),'--',sys.executable,'-c','import os;assert os.environ["KEY"]=="'+secret+'"']))
        self.until(lambda:self.state('task') and self.state('task')['phase']=='succeeded')
        self.assertNotIn(secret,json.dumps(self.state('task')))

if __name__=='__main__':unittest.main()
