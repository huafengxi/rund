"""Installed CLI with a real Owner, processes and frozen terminal state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.bootstrap import install
from runtime.store import write,read

class CLI(unittest.TestCase):
    def setUp(self):
        self.base=Path(tempfile.mkdtemp(prefix='servicectl-')).resolve();self.prefix=self.base/'node';self.root=self.prefix/'shared';self.bin=self.prefix/'bin/servicectl'
        c=dict(node='fixture',sharedRoot=str(self.root),privateRoot=str(self.prefix/'local'),python=sys.executable,home=str(self.base),path=os.environ['PATH'],publishers={'operator':'fixture','owner-fixture':'fixture'},version=1)
        install(self.prefix,c,Path(__file__).resolve().parents[1],'a'*40);self.call('owner','start')
    def call(self,*args,good=True):
        p=subprocess.run([str(self.bin),*args],capture_output=True,text=True,timeout=15)
        if good:self.assertEqual(p.returncode,0,p.stderr)
        return p
    def tearDown(self):self.call('owner','stop')
    def publish(self,d):
        f=self.base/'input.json';write(f,d);self.call('publish','--input',str(f))
    def test_service_stop_waits_for_terminal_task_and_new_layout(self):
        self.publish(dict(kind='Service',id='demo',name='demo',node='fixture',publisher='operator',revision=1,version=1,desired='offline',restart='manual',task=dict(argv=[sys.executable,'-c','import time;print("running",flush=True);time.sleep(60)'])))
        running=json.loads(self.call('start','demo').stdout);self.assertEqual(running['actual'],'online')
        self.call('stop','demo');state=read(self.root/'tasks'/running['task']/'state.json');self.assertEqual(state['phase'],'canceled');self.assertTrue(state['waitObserved'])
        private=self.prefix/'local';self.assertTrue((private/'tasks'/running['task']/'stdout').is_file());self.assertTrue((private/'tasks'/running['task']/'work/tmp').is_dir());self.assertFalse((private/'health.json').exists());self.assertIn('diagnostics',read(private/'owner.json'))
        self.assertFalse((self.prefix/'Makefile').exists());self.assertFalse((self.prefix/'runtime.json').exists());self.assertFalse((self.prefix/'workspace').exists())
    def test_cancel_before_dependencies_and_owner_stop_guard(self):
        self.publish(dict(kind='Task',id='waiting',node='fixture',publisher='operator',revision=1,desired='run',argv=[sys.executable,'-c','raise AssertionError("must not execute")'],needs=[str(self.base/'missing')]))
        end=time.monotonic()+5
        while not (self.root/'tasks/waiting/state.json').exists() and time.monotonic()<end:time.sleep(.02)
        self.assertNotEqual(self.call('owner','stop',good=False).returncode,0)
        self.call('cancel','waiting')
        while read(self.root/'tasks/waiting/state.json')['phase']!='canceled' and time.monotonic()<end:time.sleep(.02)
        self.assertEqual(read(self.root/'tasks/waiting/state.json')['phase'],'canceled')

if __name__=='__main__':unittest.main()
