"""Retained real Owner tests for single-writer desired snapshots.

Set RUND_SHARED_OUTPUT to a new absolute directory. No production transport used.
"""
import copy
import json
import os
from pathlib import Path
import sys
import time
import unittest

os.environ['RUND_CLI_TEST_OUTPUT'] = os.environ['RUND_SHARED_OUTPUT']
from test_service_cli import CLITests, until
from runtime.desired import envelope, selected
from runtime.registry import declaration
from runtime.kernel import publish


class SharedTests(CLITests):
    # Inherit fixture helpers, not legacy tests (those intentionally use the
    # snapshot-writing legacy CLI contract).
    test_failed_official_probe_is_offline_even_with_matching_output = None
    test_selected_bootstrap_source_and_live_binding_reload = None
    test_sync_keeps_unstarted_external_style_profile_offline = None
    test_pinned_bootstrap_lifecycle_and_restart = None

    def setUp(self):
        super().setUp()
        svc = dict(name='demo', version=1, cmd=[sys.executable, '${code.runtime}/hold.py'],
                   env_file='envfile', require_env=['KEY'])
        self.d = declaration(self.config, svc)
        self.input = self.area / 'desired.json'
        self.config.update(registryFile=str(self.input), bootstrapVersion=1, seedServices=[], interval=.1)
        self.path.write_text(json.dumps(self.config))
        self.seq = 0

    def tearDown(self):
        if (self.area / 'protocol/snapshot.json').exists():
            values, _ = selected(self.config)
            records = []
            for value in values:
                d = copy.deepcopy(value['_declaration']); d['spec']['desired'] = 'offline'
                records.append(dict(name=value['name'], declaration=d))
            self.release(records)
            until(lambda: not self.head() or self.head()['phase'] == 'stopped')
        super().tearDown()

    def release(self, records):
        self.seq += 1
        publish(self.input, envelope(dict(node='fixture', generation=self.seq, services=records)))

    def record(self, d=None):
        return dict(name='demo', declaration=copy.deepcopy(d or self.d))

    def snapshot(self):
        return json.loads((self.area / 'protocol/snapshot.json').read_text())

    def head(self):
        return next((h for h in self.snapshot()['services'] if h['id'] == 'svc-demo'), None)

    def test_snapshot_update_offline_delete_and_replay(self):
        self.release([self.record()])
        self.cli('start', 'service-sync')
        until(lambda: self.head() and self.head()['phase'] == 'running')
        first = self.head()['generation']
        for _ in range(3):
            self.release([self.record()])
            time.sleep(.15)
        self.assertEqual(self.head()['generation'], first)
        self.d['metadata']['version'] = 2
        self.d['spec']['env']['UPDATE'] = 'yes'
        self.release([self.record()])
        until(lambda: self.head()['declaration']['metadata']['version'] == 2)
        until(lambda: self.head()['phase'] == 'running')
        self.assertGreater(self.head()['generation'], first)
        self.release([])  # deletion is a retained offline tombstone
        until(lambda: self.head()['hold'] and self.head()['phase'] == 'stopped')
        self.assertTrue(any(r['waitObserved'] and r['signal'] is None and r['exitCode'] == 0
                            for r in self.snapshot()['runs'] if r['id'] == 'svc-demo'))
        values, error = selected(self.config)
        self.assertIsNone(error)
        self.assertEqual(values[0]['desired'], 'offline')
        self.assertNotEqual(self.cli('start', 'demo', good=False).returncode, 0)
        self.cli('stop', 'service-sync')
        self.cli('start', 'service-sync')
        self.assertEqual(self.head()['phase'], 'stopped')

    def test_invalid_and_rollback_preserve_last_good(self):
        self.release([self.record()]); self.cli('start', 'service-sync')
        until(lambda: self.head() and self.head()['phase'] == 'running')
        generation = self.head()['generation']
        good = self.input.read_bytes()
        self.input.write_text('{broken')
        until(lambda: self.snapshot()['diagnostics'])
        self.assertEqual(self.head()['generation'], generation)
        self.input.write_bytes(good)
        until(lambda: not self.snapshot()['diagnostics'])
        d = copy.deepcopy(self.d); d['spec']['env']['CHANGED'] = 'without-bump'
        self.release([self.record(d)])
        until(lambda: any('version bump' in e for e in self.snapshot()['diagnostics']))
        self.assertEqual(self.head()['generation'], generation)
        self.d['spec']['desired'] = 'offline'
        self.release([self.record()])
        until(lambda: self.head()['phase'] == 'stopped')
        self.input.write_bytes(good)
        until(lambda: any('rollback' in e for e in self.snapshot()['diagnostics']))
        self.assertEqual(self.head()['phase'], 'stopped')

    def test_remote_source_is_not_treated_as_a_local_protocol_path(self):
        from runtime.registry import RegistryOwner
        self.d['spec']['sources'][0].update(repository='ssh://fixture/objects.git', sshConfig=str(self.area / 'ssh-config'))
        owner = RegistryOwner(self.config)
        try:
            owner.check_declaration(self.d)
        finally:
            owner.close()

    def test_completed_prepare_failure_retries_without_a_new_publication(self):
        import subprocess
        missing = self.area / 'arrives-later'
        self.d['spec']['sources'][0]['repository'] = missing.as_uri()
        self.release([self.record()]); self.cli('start', 'service-sync')
        until(lambda: any('prepare:' in e for e in self.snapshot()['diagnostics']))
        self.assertIsNone(self.head())
        subprocess.run(['/usr/bin/git', 'clone', '--no-hardlinks', str(self.repo), str(missing)], check=True, capture_output=True)
        until(lambda: self.head() and self.head()['phase'] == 'running')
        self.assertEqual(self.snapshot()['registry']['generation'], self.seq)
        self.d['spec']['desired'] = 'offline'; self.release([self.record()])
        until(lambda: self.head()['phase'] == 'stopped')

    def test_seed_runs_without_shared_input_and_failed_fetch_diagnoses(self):
        self.config['seedServices'] = [self.record()]
        self.path.write_text(json.dumps(self.config))
        self.cli('start', 'service-sync')
        until(lambda: self.head() and self.head()['phase'] == 'running')
        self.assertTrue(self.snapshot()['diagnostics'])
        d = copy.deepcopy(self.d); d['metadata']['version'] = 2
        d['spec']['sources'][0]['revision'] = '0' * 40
        self.release([self.record(d)])
        until(lambda: any('prepare:' in e for e in self.snapshot()['diagnostics']))
        self.assertEqual(self.head()['declaration']['metadata']['version'], 1)
        self.assertEqual(self.head()['phase'], 'running')
        # A corrected higher version converges, not a fake running success.
        self.d['metadata']['version'] = 3
        self.release([self.record()])
        until(lambda: self.head()['declaration']['metadata']['version'] == 3)
        self.d['spec']['desired'] = 'offline'; self.release([self.record()])
        until(lambda: self.head()['phase'] == 'stopped')


# The base TestCase import is only a helper and must not run twice.
del CLITests
if __name__ == '__main__':
    unittest.main(verbosity=2)
