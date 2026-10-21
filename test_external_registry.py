"""Retained official-manager command and crash-boundary fixtures.

Set RUND_CLI_TEST_OUTPUT to a new directory. Fixture managers model an official
API; their receipts are never daemon process/wait evidence.
"""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

from test_service_cli import CLITests
from runtime.desired import envelope, selected
from runtime.external import reconcile, state
from runtime.kernel import publish
from runtime.registry import RegistryOwner, declaration


class ExternalTests(CLITests):
    test_failed_official_probe_is_offline_even_with_matching_output = None
    test_selected_bootstrap_source_and_live_binding_reload = None
    test_sync_keeps_unstarted_external_style_profile_offline = None
    test_pinned_bootstrap_lifecycle_and_restart = None

    def setUp(self):
        super().setUp()
        self.manager = self.area / 'manager.py'
        self.manager.write_text('''import os,sys
from pathlib import Path
action=sys.argv[1]; marker=Path(sys.argv[2]); log=marker.with_suffix('.log')
if action=='probe':
 print('online' if marker.exists() else 'offline');sys.exit(0 if marker.exists() else 1)
assert os.environ['KEY']=='synthetic-private-value'
assert os.environ['DECLARED']=='expanded'
with log.open('a') as f:f.write(action+'\\n')
if action=='start':marker.write_text('official-fixture')
elif action=='stop':marker.unlink(missing_ok=True)
if os.environ.get('FAIL_AFTER_EFFECT'):sys.exit(2)
''')
        self.marker = self.area / 'resource'
        self.d = declaration(self.config, dict(name='demo', version=1,
            cmd=[sys.executable, str(self.manager), 'start', str(self.marker)],
            env_file='envfile', require_env=['KEY'], extra_env={'DECLARED':'expanded'}))
        self.record = dict(name='demo', declaration=self.d, external=True,
            status=dict(cmd=[sys.executable,str(self.manager),'probe',str(self.marker)],expect='online'),
            stop_cmd=[sys.executable,str(self.manager),'stop',str(self.marker)])
        self.input = self.area / 'desired.json'
        self.config.update(registryFile=str(self.input), bootstrapVersion=1, seedServices=[], interval=.1)
        self.owner = RegistryOwner(self.config)
        self.seq = 0

    def tearDown(self):
        self.owner.close()
        super().tearDown()

    def step(self):
        self.seq += 1
        publish(self.input,envelope(dict(node='fixture',generation=self.seq,services=[self.record])))
        self.owner.reconcile_registry()
        return self.owner.registry_errors

    def log(self):
        p=self.marker.with_suffix('.log')
        return p.read_text().splitlines() if p.exists() else []

    def test_update_offline_delete_and_restart_do_not_repeat_actions(self):
        self.assertEqual(self.step(),[])
        self.assertTrue(self.marker.exists())
        for _ in range(3):self.assertEqual(self.step(),[])
        self.assertEqual(self.log(),['start'])
        self.d['metadata']['version']=2
        self.assertEqual(self.step(),[])
        self.assertEqual(self.log(),['start','stop','start'])
        self.owner.close();self.owner=RegistryOwner(self.config)
        self.owner.reconcile_registry();self.assertEqual(self.log(),['start','stop','start'])
        self.seq+=1
        publish(self.input,envelope(dict(node='fixture',generation=self.seq,services=[])))
        self.owner.reconcile_registry();self.assertFalse(self.marker.exists())
        self.assertEqual(self.log(),['start','stop','start','stop'])
        self.assertFalse(self.owner.runtime.root.joinpath('state/objects/Service/svc-demo').exists())
        records=list((self.owner.runtime.root/'external/demo').glob('manager-*.json'))
        self.assertTrue(all(json.loads(p.read_text())['managerWaitObserved'] for p in records))
        self.assertNotIn('synthetic-private-value',''.join(p.read_text() for p in records))

    def test_unknown_effect_blocks_even_new_intent_and_owner_restart(self):
        self.d['spec']['env']['FAIL_AFTER_EFFECT']='1'
        self.assertTrue(self.step());self.assertTrue(self.marker.exists())
        self.owner.close();self.owner=RegistryOwner(self.config)
        self.d['spec']['desired']='offline'
        self.assertTrue(self.step())
        self.assertTrue(self.marker.exists());self.assertEqual(self.log(),['start'])
        snapshot=self.owner.round()
        self.assertIn(('Service','svc-demo'),snapshot['blockedTargets'])
        self.assertEqual(snapshot['externalServices'][0]['actual'],'unknown')
        from runtime.service_cli import rows
        self.assertEqual(next(r for r in rows(self.config) if r['name']=='demo')['actual'],'unknown')

    def test_crash_after_effect_retains_pending_without_replay(self):
        def crash(event,value):
            if event=='external-effect':raise SystemExit('injected crash')
        self.owner.hook=crash
        with self.assertRaises(SystemExit):self.step()
        self.assertTrue(self.marker.exists())
        self.owner.close();self.owner=RegistryOwner(self.config)
        self.assertTrue(self.step());self.assertEqual(self.log(),['start'])
        head=state(self.config['root'],'demo')
        receipt=json.loads((Path(self.config['root'])/'external/demo'/(head['request']+'.json')).read_text())
        self.assertEqual(receipt['status'],'pending')

    def test_completed_receipt_recovers_interrupted_head_projection(self):
        self.assertEqual(self.step(),[])
        old=state(self.config['root'],'demo')['binding']
        self.d['metadata']['version']=2
        self.assertEqual(self.step(),[])
        head=state(self.config['root'],'demo');head.pop('appliedSequence');head['binding']=old
        publish(Path(self.config['root'])/'external/demo/head.json',head)
        self.owner.close();self.owner=RegistryOwner(self.config)
        self.assertEqual(self.step(),[]);self.assertEqual(self.log(),['start','stop','start'])

    def test_readiness_failure_and_timeout_never_replay(self):
        import subprocess
        from unittest.mock import patch
        original=subprocess.run
        def timeout_after_effect(argv,*args,**kwargs):
            result=original(argv,*args,**kwargs)
            if argv[:3]==[sys.executable,str(self.manager),'start']:
                raise subprocess.TimeoutExpired('fixture manager',35)
            return result
        with patch('runtime.external.subprocess.run',side_effect=timeout_after_effect):
            self.assertTrue(self.step())
        self.assertTrue(self.step());self.assertEqual(self.log(),['start'])

    def test_update_prepares_before_stopping_old_resource(self):
        self.assertEqual(self.step(),[])
        self.d['metadata']['version']=2
        self.d['spec']['sources'][0]['revision']='0'*40
        self.assertTrue(self.step())
        self.assertTrue(self.marker.exists());self.assertEqual(self.log(),['start'])

    def test_external_contract_and_kind_change_are_rejected(self):
        from runtime.desired import check
        bad=copy.deepcopy(self.record);bad.pop('status')
        with self.assertRaises(ValueError):
            check(envelope(dict(node='fixture',generation=1,services=[bad])),'fixture')
        self.assertEqual(self.step(),[])
        self.record['external']=False;self.d['metadata']['version']=2
        self.assertTrue(self.step());self.assertEqual(self.log(),['start'])


del CLITests
if __name__=='__main__':unittest.main(verbosity=2)
