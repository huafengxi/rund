"""Retained real Linux/Darwin Runtime tests, no production or protocol tree."""
import copy,json,os,signal,subprocess,sys,time,unittest
from pathlib import Path
from runtime.kernel import proc,same_identity,group_members,read_json
from runtime.lifecycle import Runtime
from runtime.native import boot_id
BASE=Path(os.environ['RUND_NATIVE_OUTPUT']).resolve();BASE.mkdir(mode=0o700)
SOURCE=BASE/'source';SOURCE.mkdir();PAYLOAD=SOURCE/'payload.py'
PAYLOAD.write_text('''import os,signal,sys,time
from pathlib import Path
mode=sys.argv[1]
if mode=='exec':os.execv(sys.executable,[sys.executable,'-c',"print('exec ok')"])
if mode=='exit':sys.exit(0)
signal.signal(signal.SIGTERM,lambda *a:sys.exit(0))
Path('ready').write_text(str(os.getpid()))
while True:time.sleep(.01)
''')
G='/usr/bin/git'
def git(*a):return subprocess.check_output([G,'-C',str(SOURCE),*a],stderr=subprocess.STDOUT).decode().strip()
git('init','-q','--template=');git('add','--','payload.py');msg=BASE/'message';msg.write_text('6ggidb native isolated fixture\n');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-q','-F',str(msg),'--','payload.py');REV=git('rev-parse','HEAD')

def until(fn,seconds=8):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  v=fn()
  if v:return v
  time.sleep(.01)
 raise AssertionError('bounded observation expired')

class NativeTests(unittest.TestCase):
 def setUp(self):
  self.area=BASE/self._testMethodName;self.area.mkdir();self.r=Runtime(self.area/'node','fixture');self.handles=[]
 def tearDown(self):
  for p in self.r.kernel.processes.values():
   if p.poll() is None:
    actual=proc(p.pid)
    if actual and actual['uid']==os.getuid() and actual['pgid']==p.pid:os.killpg(p.pid,signal.SIGTERM)
   p.wait(timeout=3)
  self.r.__exit__(None,None,None)
 def declaration(self,kind='Task',mode='exit'):
  spec=dict(node='fixture',sources=[dict(alias='main',repository=SOURCE.as_uri(),revision=REV,requires=[])],prepare=[],toolchain={'python':sys.executable},cwd='${work}',env={},credentialRefs=[],argv=[sys.executable,'${code.main}/payload.py',mode])
  if kind=='Task':spec.update(attemptId='a1',requestId='release')
  else:spec.update(desired='online',restart=dict(policy='always',initialSeconds=.01,maxSeconds=.02))
  return dict(apiVersion='rund/v1alpha1',kind=kind,metadata=dict(id='demo',version=1),spec=spec)
 def control(self,kind='Task',gen=1):
  return dict(apiVersion='rund-control/v1alpha1',requestId='stop-'+str(gen),action='cancel' if kind=='Task' else 'stop',kind=kind,id='demo',attemptId='a1' if kind=='Task' else 'service',expectedGeneration=gen)
 def test_native_identity_and_group(self):
  own=proc(os.getpid());self.assertTrue(same_identity(own,own));self.assertTrue(boot_id());self.assertIn(os.getpid(),[p['pid'] for p in group_members(own)]);self.assertIsNone(proc(999999999))
 def test_task_exit_and_immutable_result(self):
  self.r.accept_task(self.declaration());done=self.r.wait_task('demo','a1',8);self.assertEqual((done['phase'],done['exitCode'],done['waitObserved']),('succeeded',0,True));self.assertEqual(self.r.observe_task('demo','a1'),done)
 def test_task_exec_preserves_identity_and_wait(self):
  self.r.accept_task(self.declaration(mode='exec'));done=self.r.wait_task('demo','a1',8);self.assertEqual((done['phase'],done['exitCode'],done['waitObserved']),('succeeded',0,True))
 def test_task_stop_real_wait_and_empty_group(self):
  run=self.r.accept_task(self.declaration(mode='hold'));until(lambda:(self.r.kernel.root/'work/Task/demo/a1/1/ready').exists());done=self.r.control(self.control(),term_seconds=1,kill_seconds=2);self.assertEqual((done['phase'],done['exitCode'],done['waitObserved']),('canceled',0,True));self.assertFalse([p for p in group_members(run) if p['state'] not in ('Z','X')])
 def test_service_stop_holds_generation(self):
  self.r.submit_service(self.declaration(kind='Service',mode='hold'),'submit');until(lambda:(self.r.root/'work/Service/demo/service/1/ready').exists());head=self.r.control(self.control('Service'),term_seconds=1,kill_seconds=2);done=self.r._service_current(head);self.assertEqual((done['phase'],done['exitCode'],done['waitObserved']),('stopped',0,True));self.assertTrue(head['hold']);self.assertEqual(self.r.tick_service('demo')['generation'],1)
 def test_always_exit_restarts(self):
  self.r.submit_service(self.declaration(kind='Service'),'submit');head=until(lambda:(h if (h:=self.r.tick_service('demo'))['generation']>=2 else None));self.assertGreaterEqual(head['generation'],2);stopped=self.r.control(self.control('Service',head['generation']));self.assertTrue(stopped['hold'])
 def test_identity_mismatch_never_authorizes(self):
  own=proc(os.getpid());bad=dict(own,procStart=own['procStart']+1);self.assertFalse(same_identity(own,bad))
 def test_unreadable_native_identity_remains_unknown(self):
  from unittest.mock import patch
  run=self.r.accept_task(self.declaration(mode='hold'));until(lambda:(self.r.root/'work/Task/demo/a1/1/ready').exists())
  with patch('runtime.kernel.proc',side_effect=RuntimeError('unavailable native ABI')):
   fact=self.r.kernel.observe(run)
  self.assertEqual(fact['status'],'unknown');self.assertFalse(fact['waitObserved'])
 def test_second_owner_rejected(self):
  with self.assertRaises(BlockingIOError):Runtime(self.r.root,'fixture')

if __name__=='__main__':unittest.main(verbosity=2)
