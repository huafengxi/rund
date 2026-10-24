import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import validate,publish
from runtime.engine import Engine
from runtime.services import source_issue
from runtime.store import write,read,digest

class Sources(unittest.TestCase):
    def test_tag_readiness_and_service_restart_use_declaration_digest(self):
        with tempfile.TemporaryDirectory(prefix='rund-source-') as temp:
            root=Path(temp);repo=root/'repo';repo.mkdir()
            def git(*a):return subprocess.check_output(['git','-C',str(repo),*a],stderr=subprocess.STDOUT,text=True).strip()
            git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
            (repo/'code').write_text('one');git('add','code');git('commit','-qm','one');git('tag','v1')
            c=dict(node='a',hub='a',sharedRoot=str(root/'shared'),privateRoot=str(root/'local'),configRoot=str(root/'config'),servicesRoot=str(root/'config/objects/services'),python=sys.executable,home=temp,path=os.environ['PATH'])
            write(root/'config/repos.json',{'code':{'path':str(repo),'url':'unused','branch':'main'}})
            d=dict(kind='Service',id='svc',node='a',desired='online',restart='manual',repos={'code':'v1'},argv=[sys.executable,'-c','import time;time.sleep(100)'])
            self.assertIsNone(source_issue(c,d))
            o=Engine(c)
            try:
                from runtime.services import publish as put
                put(c,d);o.round();first=read(root/'shared/services/svc/state.json');self.assertEqual(first['phase'],'running')
                # Publishing a new tag does not itself change or restart the declaration.
                (repo/'code').write_text('two');git('commit','-qam','two');git('tag','v2');o.round();self.assertEqual(read(root/'shared/services/svc/state.json')['runId'],first['runId'])
                new=dict(d,repos={'code':'missing'});put(c,new)
                deadline=time.monotonic()+6
                while time.monotonic()<deadline:
                    o.round();s=read(root/'shared/services/svc/state.json')
                    if s.get('reason','').startswith('source dependency:'):break
                    time.sleep(.02)
                self.assertEqual(s['phase'],'running');self.assertEqual(s['declarationDigest'],digest(d))
                git('tag','missing')
                deadline=time.monotonic()+6
                while time.monotonic()<deadline:
                    o.round();s=read(root/'shared/services/svc/state.json')
                    if s['phase']=='running' and s['runId']!=first['runId']:break
                    time.sleep(.02)
                self.assertEqual(s['phase'],'running');self.assertNotEqual(s['runId'],first['runId']);self.assertEqual(s['declarationDigest'],digest(new));self.assertNotIn('runVersion',s)
            finally:
                for child in o.processes.handles.values():
                    if child.poll() is None:child.kill()
                    child.wait(timeout=5)
                o.close()
    def test_service_numeric_version_rejected(self):
        d=dict(kind='Service',id='svc',node='a',desired='offline',restart='manual',argv=['/bin/true'],version=1)
        with self.assertRaises(ValueError):validate(d)

if __name__=='__main__':unittest.main()
