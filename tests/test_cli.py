"""Installed CLI with a real runtime Service, processes and frozen terminal state."""
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
        self.base=Path(tempfile.mkdtemp(prefix='runctl-')).resolve();self.prefix=self.base/'node';self.root=self.prefix/'shared';self.bin=self.prefix/'bin/runctl'
        c=dict(node='fixture',sharedRoot=str(self.root),privateRoot=str(self.prefix/'local'),python=sys.executable,home=str(self.base),path=os.environ['PATH'],publishers={'operator':'fixture','rund-fixture':'fixture','bootstrap-fixture':'fixture'},bootstrapService='runtime-fixture')
        install(self.prefix,c,Path(__file__).resolve().parents[1],'a'*40)
        self.publish(dict(kind='Service',id='runtime-fixture',name='rund',node='fixture',publisher='operator',revision=1,version=1,desired='offline',restart='always',task=dict(argv=[sys.executable,'-B','${runtime}/rund.py','--config','${private}/node.json'])))
        self.call('start','rund')
    def call(self,*args,good=True):
        p=subprocess.run([str(self.bin),*args],capture_output=True,text=True,timeout=15)
        if good:self.assertEqual(p.returncode,0,p.stderr)
        return p
    def tearDown(self):self.call('stop','runtime-fixture')
    def publish(self,d):
        f=self.base/'input.json';write(f,d);self.call('publish','--input',str(f))
    def test_service_stop_waits_for_terminal_task_and_new_layout(self):
        self.publish(dict(kind='Service',id='demo',name='demo',node='fixture',publisher='operator',revision=1,version=1,desired='offline',restart='manual',task=dict(argv=[sys.executable,'-c','import time;print("running",flush=True);time.sleep(60)'])))
        running=json.loads(self.call('start','demo').stdout);self.assertEqual(running['actual'],'online')
        self.call('stop','demo');state=read(self.root/'tasks'/running['task']/'state.json');self.assertEqual(state['phase'],'canceled');self.assertTrue(state['waitObserved'])
        private=self.prefix/'local';self.assertTrue((private/'tasks'/running['task']/'stdout').is_file());self.assertTrue((private/'tasks'/running['task']/'work/tmp').is_dir());self.assertFalse((private/'health.json').exists());self.assertIn('diagnostics',read(private/'runtime.json'))
        self.assertFalse((self.prefix/'Makefile').exists());self.assertFalse((self.prefix/'runtime.json').exists());self.assertFalse((self.prefix/'workspace').exists())
    def test_cancel_before_dependencies_and_runtime_stop_guard(self):
        self.publish(dict(kind='Task',id='waiting',node='fixture',publisher='operator',revision=1,desired='run',argv=[sys.executable,'-c','raise AssertionError("must not execute")'],needs=[str(self.base/'missing')]))
        end=time.monotonic()+5
        while not (self.root/'tasks/waiting/state.json').exists() and time.monotonic()<end:time.sleep(.02)
        self.assertNotEqual(self.call('stop','rund',good=False).returncode,0)
        self.call('cancel','waiting')
        while read(self.root/'tasks/waiting/state.json')['phase']!='canceled' and time.monotonic()<end:time.sleep(.02)
        self.assertEqual(read(self.root/'tasks/waiting/state.json')['phase'],'canceled')

    def until(self,fn):
        end=time.monotonic()+10
        while time.monotonic()<end:
            value=fn()
            if value:return value
            time.sleep(.03)
        self.fail('condition timed out')

    def test_rund_restart_has_normal_task_real_wait_and_frozen_history(self):
        first=json.loads(self.call('status','rund').stdout)
        self.call('stop','rund')
        state=read(self.root/'tasks'/first['task']/'state.json')
        self.assertEqual(state['phase'],'canceled');self.assertTrue(state['waitObserved'])
        frozen=(self.root/'tasks'/first['task']/'state.json').read_bytes()
        second=json.loads(self.call('start','rund').stdout)
        self.assertNotEqual(first['task'],second['task'])
        self.assertNotEqual(first['pid'],second['pid'])
        self.assertEqual(frozen,(self.root/'tasks'/first['task']/'state.json').read_bytes())
        declaration=read(self.root/'services/runtime-fixture/declaration.json')
        self.assertEqual(declaration['kind'],'Service')
        updated=dict(declaration,revision=declaration['revision']+1,version=2)
        self.publish(updated)
        self.until(lambda:json.loads(self.call('status','rund').stdout)['run_version']==2)
        old=read(self.root/'tasks'/second['task']/'state.json')
        self.assertTrue(old['waitObserved']);self.assertEqual(old['phase'],'canceled')

    def test_foreground_run_and_duplicate_start_do_not_create_another_executor(self):
        self.call('stop','rund')
        foreground=subprocess.Popen([str(self.bin),'run','rund'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            self.until(lambda:json.loads(self.call('status','rund').stdout)['actual']=='online')
            initial=json.loads(self.call('status','rund').stdout)
            repeated=json.loads(self.call('start','rund').stdout)
            self.assertEqual(initial['task'],repeated['task'])
            duplicate=self.call('run','rund',good=False)
            self.assertNotEqual(duplicate.returncode,0)
            foreground.terminate();self.assertEqual(foreground.wait(timeout=10),0)
            state=read(self.root/'tasks'/initial['task']/'state.json')
            self.assertEqual(state['phase'],'canceled');self.assertTrue(state['waitObserved'])
        finally:
            if foreground.poll() is None:
                self.call('stop','rund');foreground.wait(timeout=10)
            foreground.communicate()

    def test_service_name_is_not_a_bootstrap_dispatch_rule(self):
        declaration=read(self.root/'services/runtime-fixture/declaration.json')
        self.publish(dict(declaration,name='host-runtime',revision=declaration['revision']+1))
        self.publish(dict(kind='Service',id='application-rund',name='rund',node='fixture',publisher='operator',revision=1,version=1,desired='offline',restart='manual',task=dict(argv=[sys.executable,'-c','import time;time.sleep(60)'])))
        row=json.loads(self.call('start','rund').stdout)
        self.assertEqual(row['id'],'application-rund')
        self.call('stop','rund')
        self.assertEqual(json.loads(self.call('status','host-runtime').stdout)['actual'],'online')

    def test_external_stop_waits_for_runtime_quiescence_before_signaling(self):
        self.publish(dict(kind='Service',id='load',node='fixture',publisher='operator',revision=1,version=1,desired='offline',restart='manual',task=dict(argv=[sys.executable,'-c','import time;time.sleep(60)'])))
        load=json.loads(self.call('start','load').stdout)
        runtime=json.loads(self.call('status','rund').stdout)
        self.call('desired','runtime-fixture','offline')
        self.until(lambda:read(self.root/'tasks'/runtime['task']/'state.json')['phase']=='canceling')
        self.assertFalse(read(self.root/'tasks'/runtime['task']/'state.json').get('cancelRequested',False))
        self.assertEqual(json.loads(self.call('status','load').stdout)['pid'],load['pid'])
        self.call('stop','load')
        self.call('stop','rund')
        state=read(self.root/'tasks'/runtime['task']/'state.json')
        self.assertTrue(state['waitObserved'])

class RoutedDesired(unittest.TestCase):
    def test_origin_transports_intent_without_a_running_sync_service(self):
        from unittest.mock import patch
        from runtime import cli
        old=dict(id='example',publisher='operator',desired='online')
        new=dict(old,desired='offline')
        config=dict(node='origin',sharedRoot='fixture',publishers={'operator':'origin'})
        with patch.object(cli,'get',return_value=(old,None)),patch.object(cli,'desired',return_value=new),patch.object(cli,'refresh') as refresh:
            self.assertEqual(cli.set_desired(config,'example','offline'),new)
        refresh.assert_called_once_with(config)

    def test_stale_offline_replica_cannot_complete_start(self):
        from unittest.mock import patch
        from runtime import cli
        old=dict(kind='Service',id='demo',name='demo',node='target',publisher='operator',revision=1,desired='offline',version=1)
        new=dict(old,revision=2,desired='online')
        rows=[{'services':[dict(id='demo',actual='offline',desired='offline',state='ok')]},
              {'services':[dict(id='demo',actual='online',desired='online',state='ok')]}]
        with patch.object(cli,'objects',return_value={'demo':(None,old)}), \
             patch.object(cli,'set_desired',return_value=new), \
             patch.object(cli,'refresh'),patch.object(cli.time,'sleep'), \
             patch.object(cli,'status',side_effect=rows) as status, \
             patch.object(cli,'get',side_effect=[(old,{'currentTask':None}),(new,{'currentTask':None})]):
            result=cli.service({'node':'target','sharedRoot':'fixture'},'demo','start')
        self.assertEqual(status.call_count,2)
        self.assertEqual(result['actual'],'online')

if __name__=='__main__':unittest.main()
