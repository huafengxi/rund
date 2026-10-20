"""Real retained bootstrap -> immutable Git owner -> service lifecycle smoke."""
import json,os,shutil,socket,subprocess,sys,time,unittest
from pathlib import Path
S=Path(__file__).resolve().parent
BASE=Path(os.environ['RUND_CLI_TEST_OUTPUT']).resolve();BASE.mkdir(mode=0o700)

def until(fn,seconds=15):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  v=fn()
  if v:return v
  time.sleep(.02)
 raise AssertionError('bounded CLI observation expired')

class CLITests(unittest.TestCase):
 def setUp(self):
  self.area=BASE/self._testMethodName;self.area.mkdir();self.repo=self.area/'source';self.repo.mkdir()
  for folder in ('runtime','experimental'):
   for p in (S/folder).rglob('*.py'):
    if p.name.startswith('test_') or '__pycache__' in p.parts:continue
    dest=self.repo/p.relative_to(S);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(p.read_bytes())
  (self.repo/'rund.py').write_bytes((S/'rund.py').read_bytes())
  (self.repo/'hold.py').write_text('''import hashlib,os,signal,sys,time
from pathlib import Path
assert os.environ['KEY']=='synthetic-private-value'
Path('ready').write_text(str(os.getpid()))
signal.signal(signal.SIGTERM,lambda *a:sys.exit(0))
while True:time.sleep(.01)
''')
  def git(*args):return subprocess.check_output(['/usr/bin/git','-C',str(self.repo),*args],stderr=subprocess.STDOUT).decode().strip()
  git('init','-q','--template=');paths=[str(p.relative_to(self.repo)) for p in self.repo.rglob('*.py')];git('add','--',*paths);msg=self.area/'message';msg.write_text('6ggidb pinned CLI fixture\n');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-q','-F',str(msg),'--',*paths);pin=git('rev-parse','HEAD')
  self.ws=self.area/'caller';(self.ws/'services/profiles').mkdir(parents=True);(self.ws/'services/daemons').mkdir();(self.ws/'home').mkdir()
  (self.ws/'envfile').write_text('KEY=synthetic-private-value\n')
  self.profile('service-sync',dict(version=1,cmd=['unused-bootstrap']), 'online')
  self.profile('demo',dict(version=1,cmd=[sys.executable,'${code.runtime}/hold.py'],env_file='envfile',require_env=['KEY']),'offline')
  self.config=dict(apiVersion='rund-node/v1',node='fixture',root=str(self.area/'node'),protocol=str(self.area/'protocol'),workspace=str(self.ws),python=sys.executable,home=str(self.ws/'home'),path=str(Path(sys.executable).parent)+':/usr/bin:/bin',sources={'runtime':dict(alias='runtime',repository=self.repo.as_uri(),revision=pin,requires=[])},interval=60)
  self.path=self.area/'node.json';self.path.write_text(json.dumps(self.config));self.records=[]
 def profile(self,name,data,desired):
  (self.ws/f'services/profiles/{name}.json').write_text(json.dumps(data))
  (self.ws/f'services/daemons/fixture.{name}.json').write_text(json.dumps(dict(hostname=socket.gethostname(),profile=name,desired=desired)))
 def cli(self,*args,good=True):
  argv=[sys.executable,'-B',str(S/'rund.py'),'--config',str(self.path),*args]
  p=subprocess.run(argv,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),cwd=self.area,capture_output=True,text=True,timeout=45)
  self.records.append(dict(argv=argv,rc=p.returncode,stdout=p.stdout,stderr=p.stderr))
  (self.area/'commands.json').write_text(json.dumps(self.records,indent=2))
  if good:self.assertEqual(p.returncode,0,p.stderr)
  return p
 def tearDown(self):self.cli('stop','service-sync',good=False)
 def test_pinned_bootstrap_lifecycle_and_restart(self):
  first=json.loads(self.cli('start','service-sync').stdout)
  self.assertIn('/bootstrap-source/code/',first['argv'][2]);self.assertTrue(Path(first['argv'][2]).is_file())
  self.profile('demo',dict(version=1,cmd=[sys.executable,'${code.runtime}/hold.py'],env_file='envfile',require_env=['KEY']),'online')
  self.cli('start','demo');until(lambda:(self.ws/'ready').exists())
  status=json.loads(self.cli('status','--machine').stdout);self.assertEqual((status['total'],status['drift']),(2,0))
  demo=next(x for x in status['services'] if x['name']=='demo');self.assertEqual(demo['actual'],'online')
  snapshot=json.loads((self.area/'protocol/snapshot.json').read_text());self.assertNotIn('synthetic-private-value',json.dumps(snapshot))
  self.profile('demo',dict(version=1,cmd=[sys.executable,'${code.runtime}/hold.py'],env_file='envfile',require_env=['KEY']),'offline')
  self.cli('stop','demo');self.assertEqual(json.loads(self.cli('status','--machine').stdout)['drift'],0)
  self.cli('stop','service-sync');self.assertEqual(json.loads(self.cli('status','--machine').stdout)['drift'],1)
  second=json.loads(self.cli('start','service-sync').stdout);self.assertNotEqual(first['pid'],second['pid']);self.assertGreater(second['startedAt'],first['startedAt'])
  self.assertEqual(json.loads(self.cli('status','--machine').stdout)['drift'],0)

if __name__=='__main__':unittest.main(verbosity=2)
