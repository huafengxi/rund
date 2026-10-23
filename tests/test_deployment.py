import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.store import read,write,digest
from tools.deployment import load

SOURCE=Path(__file__).resolve().parents[1]

class Deployment(unittest.TestCase):
    def fixture(self):
        base=Path(tempfile.mkdtemp(prefix='rund-deployment-')).resolve();repo=base/'config';repo.mkdir();prefix=base/'node'
        transport=base/'transport';transport.mkdir();(transport/'transport.py').write_text('import sys\nsys.exit(0)\n')
        def git(*args):return subprocess.check_output(['git','-C',str(transport),*args],stderr=subprocess.STDOUT,text=True).strip()
        git('init','-q','--template=');git('add','--','transport.py');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
        pin=git('rev-parse','HEAD');runtime_pin=subprocess.check_output(['git','-C',str(SOURCE),'rev-parse','HEAD'],text=True).strip()
        c=dict(node='fixture',sharedRoot=str(prefix/'shared'),privateRoot=str(prefix/'local'),python=sys.executable,home=str(base),path=os.environ['PATH'],publishers={'rund-fixture':'fixture','bootstrap-fixture':'fixture','operator':'fixture'},bootstrapService='runtime',runtimeSource=str(prefix/'runtime'/runtime_pin),syncConfig=str(prefix/'local/sync.json'),interval=.03)
        sync=dict(nodeConfig=str(prefix/'local/node.json'),tool=str(prefix/'runtime'/pin/'transport.py'),remote='fixture:unused',probeRoot=str(base/'probe'),remoteProbeRoot='/unused',cycleLock=str(prefix/'local/locks/sync.lock'),sshConfig=str(base/'ssh-config'))
        (base/'ssh-config').write_text('')
        write(repo/'nodes/fixture.json',c);write(repo/'nodes/fixture.sync.json',sync)
        plan=dict(format='rund-bootstrap/v1',runtime='code',objects='objects',sources=[dict(alias='code',repository=SOURCE.as_uri(),revision=runtime_pin,requires=[]),dict(alias='transport',repository=transport.as_uri(),revision=pin,requires=[])],nodes={'fixture':dict(config='nodes/fixture.json',sync='nodes/fixture.sync.json',requiredFiles=[str(base/'ssh-config')])},hosts={})
        write(repo/'bootstrap.json',plan)
        for tid,desired,argv in [('runtime','online',[sys.executable,'-B','${runtime}/rund.py','--config',str(prefix/'local/node.json')]),('app','online',[sys.executable,'-c','import time;time.sleep(600)']),('unused','offline',[sys.executable,'-c','raise SystemExit(7)'])]:
            write(repo/'objects/services'/tid/'declaration.json',dict(kind='Service',id=tid,name=tid,publisher='operator',node='fixture',revision=1,version=1,desired=desired,restart='always',task=dict(argv=argv,cwd=str(base))))
        attachment={'test':'immutable'};write(repo/'objects/configs'/(digest(attachment)+'.json'),attachment)
        return base,repo,prefix,c

    def test_two_checkouts_clean_bootstrap_and_idempotent_retry(self):
        base,repo,prefix,c=self.fixture();command=[sys.executable,'-B',str(SOURCE/'tools/bootstrap.py'),'--deployment',str(repo),'--node','fixture','--timeout','20']
        try:
            check=subprocess.run(command+['--check'],capture_output=True,text=True,timeout=20)
            self.assertEqual(check.returncode,0,check.stderr);self.assertFalse(prefix.exists())
            first=subprocess.run(command,capture_output=True,text=True,timeout=90);self.assertEqual(first.returncode,0,first.stderr)
            status=json.loads(first.stdout)['status'];self.assertEqual(status['drift'],0);self.assertEqual(status['total'],3)
            self.assertEqual(next(s for s in status['services'] if s['id']=='unused')['actual'],'offline')
            tasks={s['id']:s['task'] for s in status['services']}
            root_task=read(prefix/'shared/tasks'/tasks['runtime']/'declaration.json');self.assertEqual(root_task['publisher'],'bootstrap-fixture')
            root_state=read(prefix/'shared/tasks'/tasks['runtime']/'state.json');self.assertIsNotNone(root_state['process'])
            second=subprocess.run(command,capture_output=True,text=True,timeout=90);self.assertEqual(second.returncode,0,second.stderr)
            self.assertEqual({s['id']:s['task'] for s in json.loads(second.stdout)['status']['services']},tasks)
            self.assertEqual(read(prefix/'local/node.json'),c);self.assertTrue((prefix/'local/bootstrap-result.json').exists())
        finally:
            cli=prefix/'bin/runctl'
            if cli.exists():
                for tid in ('app','runtime'):subprocess.run([str(cli),'stop',tid],capture_output=True,text=True,timeout=40)

    def test_invalid_inputs_fail_before_installation(self):
        base,repo,prefix,c=self.fixture();p=repo/'objects/configs'/('0'*64+'.json');write(p,{'wrong':'digest'})
        with self.assertRaisesRegex(ValueError,'digest mismatch'):load(repo,'fixture')
        self.assertFalse(prefix.exists());p.unlink()
        plan=read(repo/'bootstrap.json');plan['nodes']['fixture']['config']='../escape.json';write(repo/'bootstrap.json',plan)
        with self.assertRaises(ValueError):load(repo,'fixture')
        self.assertFalse(prefix.exists())

    def test_hostname_mapping_file_is_the_identity_authority(self):
        import socket
        base,repo,prefix,c=self.fixture();plan=read(repo/'bootstrap.json');plan['hosts']='host-id';write(repo/'bootstrap.json',plan)
        (repo/'host-id').write_text('# identity\n'+socket.gethostname()+' fixture\n')
        self.assertEqual(load(repo)[3]['node'],'fixture')
        (repo/'host-id').write_text(socket.gethostname()+' fixture\n'+socket.gethostname()+' other\n')
        with self.assertRaisesRegex(ValueError,'duplicate hostname'):load(repo)
        self.assertFalse(prefix.exists())

    def test_existing_configuration_change_requires_explicit_upgrade(self):
        base,repo,prefix,c=self.fixture();write(prefix/'local/node.json',dict(c,interval=9));write(prefix/'local/sync.json',read(repo/'nodes/fixture.sync.json'))
        with self.assertRaisesRegex(ValueError,'explicit upgrade'):load(repo,'fixture')
        self.assertEqual(read(prefix/'local/node.json')['interval'],9)

    def test_missing_identity_and_service_are_errors(self):
        base,repo,prefix,c=self.fixture()
        with self.assertRaisesRegex(ValueError,'node not configured'):load(repo,'unknown')
        (repo/'objects/services/runtime/declaration.json').unlink()
        with self.assertRaisesRegex(ValueError,'Service declaration missing'):load(repo,'fixture')
        self.assertFalse(prefix.exists())

if __name__=='__main__':unittest.main()
