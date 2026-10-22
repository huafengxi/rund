import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import publish
from runtime.owner import Owner
from runtime.protocol import get,desired
from runtime.scheduling import admission_path
from runtime.store import read


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.base=Path(tempfile.mkdtemp(prefix='serviced-scheduling-'))
        self.root=self.base/'shared';self.owners=[]
        for node in ('a','b'):
            c=dict(node=node,sharedRoot=str(self.root),privateRoot=str(self.base/node),python=sys.executable,home=str(self.base),path=os.environ['PATH'],publishers={'operator':'a','owner-a':'a','owner-b':'b'},version=1,scheduler={'node':'a','maxConcurrent':2})
            self.owners.append(Owner(c))
    def tearDown(self):
        for o in self.owners:
            for p in o.processes.handles.values():
                if p.poll() is None:
                    try:os.killpg(p.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                p.wait(timeout=5)
            o.close()
    def put(self,tid,node,resources):
        d=dict(kind='Task',id=tid,publisher='operator',node=node,revision=1,desired='run',resources=resources,argv=[sys.executable,'-c','import time;time.sleep(100)'])
        publish(self.root,d,writer='a',publishers=self.owners[0].c['publishers'])
    def state(self,tid):return get(self.root,tid)[1]
    def rounds(self,n=3):
        for _ in range(n):
            for o in self.owners:o.round()
    def until(self,fn):
        end=time.monotonic()+8
        while time.monotonic()<end:
            self.rounds(1)
            if fn():return
            time.sleep(.03)
        self.fail(str([o.errors for o in self.owners]))
    def test_cross_node_resource_and_wait_release(self):
        self.put('first','a',['repo']);self.rounds()
        self.put('second','b',['repo']);self.rounds()
        self.assertEqual(self.state('first')['phase'],'running')
        self.assertEqual(self.state('second')['phase'],'queued')
        desired(self.root,'first','cancel',writer='a',publishers=self.owners[0].c['publishers'],mode='kill')
        self.until(lambda:self.state('second')['phase']=='running')
        self.assertTrue(self.state('first')['waitObserved'])
        self.assertEqual(self.state('first')['phase'],'canceled')
    def test_host_resource_names_are_opaque_and_unknown_holds(self):
        self.put('one','a',['a-repo']);self.put('two','b',['b-repo']);self.rounds()
        self.assertEqual(self.state('one')['phase'],'running');self.assertEqual(self.state('two')['phase'],'running')
        self.put('three','b',[]);self.rounds()
        self.assertEqual(self.state('three')['phase'],'queued')
        grants=read(admission_path(self.owners[0].c))['grants']
        self.assertEqual(set(grants),{'one','two'})
        # Lose wait authority without terminating the process: it must retain its slot.
        handle=self.owners[0].processes.handles.pop('one')
        handle.terminate()
        handle.wait(timeout=5)
        self.rounds()
        self.assertEqual(self.state('one')['phase'],'unknown')
        self.assertEqual(self.state('three')['phase'],'queued')

if __name__=='__main__':unittest.main()
