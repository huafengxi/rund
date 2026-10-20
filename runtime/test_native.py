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
if mode=='context':
 import hashlib
 assert os.getcwd()==os.environ['EXPECT_CWD'] and os.environ['HOME']==os.environ['EXPECT_HOME']
 print(hashlib.sha256(os.environ['SYNTHETIC_SECRET'].encode()).hexdigest());sys.exit(0)
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
 def test_file_request_exclusive_publication_and_exact_receipt(self):
  from experimental.file_owner.owner import Owner
  from experimental.file_owner import protocol as C
  d=self.declaration();e=dict(apiVersion=C.API,requestId='native-request',node='fixture',action='accept',declaration=d,declarationDigest=C.digest(C.canonical(d)))
  o=Owner(self.area/'file-node',self.area/'protocol','fixture')
  try:
   C.send(o.protocol,e);C.send(o.protocol,e)
   for _ in range(3):o.round()
   receipt=C.receipt(o.protocol,e['requestId']);self.assertEqual(receipt['body'],C.request_body(e));self.assertEqual(receipt['status'],'done')
   changed=copy.deepcopy(e);changed['declaration']['metadata']['version']=2;changed['declarationDigest']=C.digest(C.canonical(changed['declaration']))
   with self.assertRaises(ValueError):C.send(o.protocol,changed)
   self.assertEqual(C.receipt(o.protocol,e['requestId']),receipt)
  finally:o.close()
 def test_context_credentials_and_redacted_results(self):
  import hashlib
  caller=self.area/'caller';caller.mkdir();home=self.area/'caller-home';home.mkdir()
  secret='synthetic-private-value';cred=self.area/'credentials';cred.write_text('SYNTHETIC_SECRET='+secret+'\n');cred.chmod(0o600)
  d=self.declaration(mode='context');d['spec']['context']=dict(cwd=str(caller),home=str(home),path='/usr/bin:/bin')
  d['spec']['env'].update(EXPECT_CWD=str(caller),EXPECT_HOME=str(home))
  d['spec']['credentialRefs']=[dict(file=str(cred),loader=[],require=['SYNTHETIC_SECRET'])]
  self.r.accept_task(d);done=self.r.wait_task('demo','a1',8)
  self.assertEqual((done['phase'],done['exitCode']),('succeeded',0));self.assertNotIn(secret,json.dumps(done))
  self.assertEqual(Path(done['stdout']).read_text().strip(),hashlib.sha256(secret.encode()).hexdigest())
 def test_executable_uses_declared_context_path(self):
  bindir=self.area/'bin';bindir.mkdir();command=bindir/'caller-tool'
  command.write_text('#!/bin/sh\nprintf "caller path ok\\n"\n');command.chmod(0o700)
  d=self.declaration();d['spec']['argv']=['caller-tool'];d['spec']['context']={'path':str(bindir)+':/usr/bin:/bin'}
  self.r.accept_task(d);done=self.r.wait_task('demo','a1',8)
  self.assertEqual((done['phase'],done['exitCode']),('succeeded',0));self.assertEqual(done['argv'][0],str(command))
  self.assertEqual(Path(done['stdout']).read_text(),'caller path ok\n')
 def test_failed_credential_loader_does_not_expose_output(self):
  loader=self.area/'loader.py';loader.write_text("import sys;print('synthetic-private-value',file=sys.stderr);sys.exit(7)")
  d=self.declaration();d['spec']['credentialRefs']=[dict(file=str(self.area/'not-read'),loader=[sys.executable,str(loader)],require=['KEY'])]
  done=self.r.accept_task(d);self.assertEqual(done['phase'],'failed');self.assertIsNone(done['pid']);self.assertNotIn('synthetic-private-value',json.dumps(done));self.assertIn('credential loader failed',done['reason'])
 def test_registry_owner_runs_updates_and_stops_service(self):
  import socket
  from runtime.registry import RegistryOwner
  ws=self.area/'caller-root';(ws/'services/profiles').mkdir(parents=True);(ws/'services/daemons').mkdir()
  profile=ws/'services/profiles/demo.json';daemon=ws/'services/daemons/fixture.demo.json'
  definition=dict(version=1,cmd=[sys.executable,'${code.main}/payload.py','hold'])
  profile.write_text(json.dumps(definition));daemon.write_text(json.dumps(dict(hostname=socket.gethostname(),profile='demo',desired='online')))
  config=dict(apiVersion='rund-node/v1',node='fixture',root=str(self.area/'registry-node'),protocol=str(self.area/'registry-protocol'),workspace=str(ws),python=sys.executable,home=str(ws),path='/usr/bin:/bin',sources={'main':self.declaration()['spec']['sources'][0]},interval=60)
  owner=RegistryOwner(config)
  try:
   value=owner.round();self.assertFalse(value['diagnostics']);until(lambda:(ws/'ready').exists())
   head=owner.runtime.service('svc-demo');self.assertEqual(head['generation'],1)
   definition['version']=2;definition['cmd'][-1]='exit';profile.write_text(json.dumps(definition));owner.reconcile_registry()
   head=owner.runtime.service('svc-demo');self.assertEqual(head['declaration']['metadata']['version'],2);self.assertEqual(head['generation'],2)
   old=owner.runtime.read_run('Service','svc-demo','service',1);self.assertEqual((old['phase'],old['exitCode'],old['waitObserved']),('stopped',0,True))
   daemon.write_text(json.dumps(dict(hostname=socket.gethostname(),profile='demo',desired='offline')));owner.reconcile_registry();self.assertTrue(owner.runtime.service('svc-demo')['hold'])
  finally:
   for h in owner.runtime.kernel.processes.values():
    if h.poll() is None:h.terminate()
    h.wait(timeout=3)
   owner.close()
 def test_second_owner_rejected(self):
  with self.assertRaises(BlockingIOError):Runtime(self.r.root,'fixture')

if __name__=='__main__':unittest.main(verbosity=2)
