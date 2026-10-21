"""Synthetic caller-registry tests against the current Runtime adapter.

No Owner, production workspace, service process or agentd dependency is used.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtime.registry import declaration, document, node_config, services_here, validate_profile


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profiles = self.root / 'services/profiles'
        self.daemons = self.root / 'services/daemons'
        self.profiles.mkdir(parents=True)
        self.daemons.mkdir()
        self.config = dict(apiVersion='rund-node/v1', node='fixture',
            root=str(self.root / 'node'), protocol=str(self.root / 'protocol'),
            workspace=str(self.root), python=sys.executable, home=str(self.root / 'home'),
            path='/declared/bin:/usr/bin:/bin', sources={'main': dict(alias='main',
                repository=(self.root / 'source').as_uri(), revision='1' * 40, requires=[])})
        self.addCleanup(patch.stopall)
        patch('runtime.registry.socket.gethostname', return_value='Fixture.Example').start()

    def write(self, path, value):
        path.write_text(json.dumps(value))
        return path

    def service(self, name='demo', hostname='fixture', desired='online', **profile):
        self.write(self.profiles / (name + '.json'), dict(version=1, cmd=['true'], **profile))
        self.write(self.daemons / ('fixture.' + name + '.json'),
                   dict(hostname=hostname, profile=name, desired=desired))

    def test_document_rejects_bad_json_duplicates_and_non_objects(self):
        path = self.root / 'input.json'
        for content in ('{broken', '{"version":1,"version":2}', '[]'):
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    document(path)

    def test_profile_validation(self):
        good = dict(version=1, cmd=['true'])
        self.assertEqual(validate_profile(good), good)
        for extra in (dict(version=True), dict(version=0), dict(version='1'),
                      dict(cmnd=['typo']), dict(cmd=[]), dict(cmd=['']),
                      dict(wrapper='daemon'), dict(env_loader=[]),
                      dict(env_loader=['loader']), dict(require_env=['NOT-VALID']),
                      dict(status={'http':'http://fixture', 'cmd':['true']})):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_profile(dict(good, **extra))

    def test_local_face_is_sorted_and_keeps_profile_fields(self):
        self.service('beta', hostname='FIXTURE.EXAMPLE', desired='offline',
                     env_file='env/fixture.env', require_env=['TOKEN'])
        self.service('alpha')
        self.service('elsewhere', hostname='fixture-other')
        rows = services_here(self.config)
        self.assertEqual([r['name'] for r in rows], ['alpha', 'beta'])
        self.assertEqual([r['desired'] for r in rows], ['online', 'offline'])
        self.assertEqual(rows[1]['require_env'], ['TOKEN'])
        self.assertEqual(document(self.profiles / 'beta.json')['version'], 1)
        self.assertNotIn('desired', document(self.profiles / 'beta.json'))

    def test_empty_face_is_empty_without_creating_execution_state(self):
        self.assertEqual(services_here(self.config), [])
        self.assertFalse(Path(self.config['root']).exists())
        self.assertFalse(Path(self.config['protocol']).exists())

    def test_invalid_local_daemon_and_profile_binding(self):
        self.service()
        path = self.daemons / 'fixture.demo.json'
        good = document(path)
        for extra in (dict(desired='up'), dict(profile='missing'), dict(typo=True)):
            with self.subTest(extra=extra), self.assertRaises((ValueError, OSError)):
                self.write(path, dict(good, **extra))
                services_here(self.config)
        self.write(path, good)
        self.write(self.profiles / 'demo.json', dict(version=1, cmd=['true'], name='other'))
        with self.assertRaisesRegex(ValueError, 'profile name mismatch'):
            services_here(self.config)
        self.write(self.profiles / 'demo.json', dict(version=1, cmd=['true']))
        path.rename(self.daemons / 'wrong.demo.json')
        with self.assertRaisesRegex(ValueError, 'daemon node/name mismatch'):
            services_here(self.config)

    def test_declaration_carries_explicit_context_sources_and_credential_references(self):
        svc = dict(name='demo', version=2, cmd=['~/bin/tool'], cwd='data',
                   env_file='env/private.env', env_loader=['~/bin/python', 'tools/load.py'],
                   require_env=['TOKEN'], extra_env={'EXTRA':'~/value'})
        d = declaration(self.config, svc)
        self.assertEqual(d['metadata'], dict(id='svc-demo', version=2))
        spec = d['spec']
        self.assertEqual(spec['argv'], [self.config['home'] + '/bin/tool'])
        self.assertEqual(spec['context'], dict(cwd=str(self.root / 'data'),
            home=self.config['home'], path=self.config['path'], inheritEnv=[]))
        self.assertEqual(spec['sources'], list(self.config['sources'].values()))
        self.assertEqual(spec['credentialRefs'], [dict(file=str(self.root / 'env/private.env'),
            loader=[self.config['home'] + '/bin/python', str(self.root / 'tools/load.py')],
            require=['TOKEN'])])
        self.assertEqual(spec['env']['EXTRA'], self.config['home'] + '/value')
        self.assertFalse(Path(self.config['root']).exists())

    def test_shared_node_rejects_hidden_bindings(self):
        config = copy.deepcopy(self.config)
        config.update(registryFile=str(self.root / 'desired.json'), bootstrapVersion=1,
                      seedServices=[], services={'demo':{}})
        path = self.write(self.root / 'node.json', config)
        with self.assertRaisesRegex(ValueError, 'shared registry cannot hide'):
            node_config(path)
        config.pop('services')
        self.write(path, config)
        self.assertEqual(node_config(path), config)


if __name__ == '__main__':
    unittest.main(verbosity=2)
