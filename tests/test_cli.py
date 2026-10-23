import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.config import node_config
from runtime.cli import main
from runtime.services import get
from runtime.store import write,read

SOURCE=Path(__file__).resolve().parents[1]

class CLI(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='rund-direct-');self.base=Path(self.temp.name)
        self.repo=self.base/'config';self.config=self.repo/'rund.json'
        write(self.config,dict(format='rund/v1',hosts='host-id',python=sys.executable,path=os.environ['PATH'],interval=.03,publishers={'operator':'fixture','rund-fixture':'fixture'}))
        (self.repo/'host-id').write_text(socket.gethostname()+' fixture\n')
        self.declaration=dict(kind='Service',id='demo',name='demo',publisher='operator',node='fixture',revision=1,version=1,
                              desired='online',restart='always',argv=[sys.executable,'-c','import time;time.sleep(100)'])
        self.path=self.repo/'objects/services/demo/declaration.json';write(self.path,self.declaration)
        self.c=node_config(self.config)
    def call(self,*args,check=True):
        p=subprocess.run([sys.executable,'-B',str(SOURCE/'runctl'),'--config',str(self.config),*args],capture_output=True,text=True,timeout=20)
        if check:self.assertEqual(p.returncode,0,p.stderr)
        return json.loads(p.stdout) if check else p
    def until(self,fn):
        end=time.monotonic()+8
        while time.monotonic()<end:
            v=fn()
            if v:return v
            time.sleep(.05)
        self.fail('timeout '+str(self.call('status')))
    def tearDown(self):
        self.call('stop',check=False);self.temp.cleanup()
    def test_cold_start_no_installer_no_root_service_no_child_tasks(self):
        sentinel=self.base/'shared/agents/business';sentinel.parent.mkdir(parents=True);sentinel.write_bytes(b'preserve')
        self.call('start');self.until(lambda:self.call('status','demo')['actual']=='online')
        first=self.call('status','demo');self.call('start');self.assertEqual(first['run'],self.call('status','demo')['run'])
        self.assertFalse((self.base/'runtime').exists());self.assertFalse((self.base/'local/node.json').exists());self.assertFalse((self.base/'bin/runctl').exists())
        self.assertEqual(sentinel.read_bytes(),b'preserve');self.assertFalse((self.base/'shared/tasks').exists())
        self.assertNotIn('currentTask',get(self.c,'demo')[1]);self.assertEqual(self.call('status')['total'],1)
        self.call('stop','demo');state=get(self.c,'demo')[1];self.assertTrue(state['waitObserved']);self.assertEqual(state['phase'],'stopped')
        frozen=read(self.base/'local/services/demo/runs'/first['run']/'state.json')
        self.call('start','demo');self.assertNotEqual(first['run'],self.call('status','demo')['run'])
        self.assertEqual(frozen,read(self.base/'local/services/demo/runs'/first['run']/'state.json'))
    def test_config_revision_restarts_direct_process_and_resets_intent(self):
        self.call('start');self.until(lambda:self.call('status','demo')['actual']=='online');first=self.call('status','demo')
        self.call('stop','demo');write(self.path,dict(self.declaration,revision=2,version=2,env={'EXAMPLE':'2'}))
        self.until(lambda:self.call('status','demo')['actual']=='online')
        self.assertEqual(self.call('status','demo')['run_version'],2);self.assertNotEqual(first['pid'],self.call('status','demo')['pid'])
    def test_unknown_hostname_and_duplicate_mapping_fail(self):
        with patch('socket.gethostname',return_value='missing'):
            with self.assertRaisesRegex(ValueError,'hostname not registered'):node_config(self.config)
        with (self.repo/'host-id').open('a') as f:f.write(socket.gethostname()+' other\n')
        with self.assertRaisesRegex(ValueError,'duplicate hostname'):node_config(self.config)
    def test_foreground_runtime_has_no_external_supervisor(self):
        p=subprocess.Popen([sys.executable,'-B',str(SOURCE/'runctl'),'--config',str(self.config),'run'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            self.until(lambda:self.call('status')['rund']['actual']=='online');self.assertEqual(self.call('status')['rund']['pid'],p.pid)
            self.until(lambda:self.call('status','demo')['actual']=='online');self.call('stop');self.assertEqual(p.wait(timeout=8),0)
        finally:
            if p.poll() is None:p.kill();p.wait()
            p.stderr.close()
    def test_static_task_cancellation_uses_protocol_and_freezes(self):
        d=dict(kind='Task',id='static-task',publisher='operator',node='fixture',revision=1,desired='run',
               argv=[sys.executable,'-c','raise SystemExit(99)'],needs=[str(self.base/'missing')])
        source=self.repo/'objects/tasks/static-task/declaration.json';write(source,d);before=source.read_bytes()
        self.call('start');self.until(lambda:(get(self.c,'static-task')[1] or {}).get('phase')=='preparing')
        self.call('cancel','static-task');self.until(lambda:get(self.c,'static-task')[1]['phase']=='canceled')
        self.assertEqual(before,source.read_bytes());state=get(self.c,'static-task')[1]
        self.assertFalse(state['waitObserved']);self.assertIsNone(state['process'])
        self.call('stop');self.call('start');self.assertEqual(state,get(self.c,'static-task')[1])
        source.unlink();self.assertEqual(state,get(self.c,'static-task')[1])
    def test_default_configuration_is_checkout(self):
        with patch('runtime.cli.Path.home',return_value=self.base),patch('runtime.cli.node_config',side_effect=ValueError('fixture')) as load:
            self.assertEqual(main(['status']),2)
        self.assertEqual(load.call_args.args[0],self.base/'.rund/config/rund.json')

if __name__=='__main__':unittest.main()
