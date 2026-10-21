"""Current Runtime credential loading: isolation, bounds and non-disclosure."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtime.environment import execution_environment, parse_env, validate_credentials
from runtime.registry import validate_profile


class EnvironmentLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "env file 'literal'.env"
        self.source.write_text('TOKEN=synthetic-private-value\n')
        self.baseline = dict(HOME=str(self.root), PATH=os.defpath)

    def spec(self, loader=None, **extra):
        return dict(credentialRefs=[dict(file=str(self.source), loader=loader or [],
                    require=['TOKEN'])], env={}, **extra)

    def loader(self, body):
        script = self.root / 'caller loader.py'
        script.write_text(body)
        return [sys.executable, str(script)]

    def test_plaintext_grammar_and_override_order(self):
        self.source.write_text("# comment\nexport TOKEN = 'plain test value'\n"
                               "exportFOO = unchanged\nUNPAIRED='literal\n")
        spec = self.spec()
        spec['env'] = {'TOKEN':'declared-override'}
        env = execution_environment(spec, self.baseline)
        self.assertEqual(env['TOKEN'], 'declared-override')
        self.assertEqual(env['exportFOO'], 'unchanged')
        self.assertEqual(env['UNPAIRED'], "'literal")
        self.assertEqual(parse_env('IGNORE\nTOKEN="quoted"\n'), {'TOKEN':'quoted'})
        self.assertNotIn('TOKEN', self.baseline)

    def test_loader_receives_literal_path_and_isolated_environment(self):
        loader = self.loader("import os,pathlib,sys\n"
            "assert 'AGENT_SELF' not in os.environ\n"
            "assert 'AMBIENT_SECRET' not in os.environ\n"
            "assert pathlib.Path(sys.argv[1]).read_text().startswith('TOKEN=')\n"
            "print(" + repr("export TOKEN = 'loaded 测试 value'") + ")\n")
        with patch.dict(os.environ, {'AGENT_SELF':'task/outer', 'AMBIENT_SECRET':'private'}):
            env = execution_environment(self.spec(loader), self.baseline)
        self.assertEqual(env['TOKEN'], 'loaded 测试 value')
        self.assertNotIn('AGENT_SELF', env)
        self.assertNotIn('AMBIENT_SECRET', env)

    def test_loader_failure_does_not_leak_output_or_use_ambient_fallback(self):
        loader = self.loader("import sys\nprint('DO_NOT_LOG_TEST_VALUE')\n"
                             "print('DO_NOT_LOG_TEST_VALUE', file=sys.stderr)\nsys.exit(1)\n")
        out = io.StringIO()
        with patch.dict(os.environ, {'TOKEN':'ambient-fallback'}), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
             self.assertRaisesRegex(ValueError, 'credential loader failed') as error:
            execution_environment(self.spec(loader), self.baseline)
        self.assertNotIn('DO_NOT_LOG_TEST_VALUE', str(error.exception) + out.getvalue())

    def test_loader_timeout_is_bounded_and_redacted(self):
        failure = subprocess.TimeoutExpired('synthetic-loader', 30,
            output=b'DO_NOT_LOG_TEST_VALUE', stderr=b'DO_NOT_LOG_TEST_VALUE')
        with patch('runtime.environment.subprocess.run', side_effect=failure) as run, \
             self.assertRaisesRegex(ValueError, 'credential loader unavailable') as error:
            execution_environment(self.spec(['synthetic-loader']), self.baseline)
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertNotIn('DO_NOT_LOG_TEST_VALUE', str(error.exception))

    def test_required_values_missing_file_and_non_utf8_loader_fail_closed(self):
        self.source.write_text('OTHER=value\n')
        with self.assertRaisesRegex(ValueError, 'required environment missing: TOKEN'):
            execution_environment(self.spec(), self.baseline)
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, 'credential file unavailable'):
            execution_environment(self.spec(), self.baseline)
        loader = self.loader("import sys\nsys.stdout.buffer.write(b'\\xff')\n")
        with self.assertRaisesRegex(ValueError, 'not UTF-8'):
            execution_environment(self.spec(loader), self.baseline)

    def test_context_inherits_only_declared_names(self):
        spec = self.spec(context=dict(home=str(self.root / 'home'), path='/explicit/bin',
                                     inheritEnv=['ALLOW']))
        with patch.dict(os.environ, {'ALLOW':'yes', 'DENY':'no'}, clear=True):
            env = execution_environment(spec, self.baseline)
        self.assertEqual(env['ALLOW'], 'yes')
        self.assertNotIn('DENY', env)
        self.assertEqual(env['HOME'], str(self.root / 'home'))
        self.assertEqual(env['PATH'], '/explicit/bin')

    def test_invalid_loader_and_credential_references_are_rejected(self):
        for loader in (None, 'shell command', [], [''], [3]):
            with self.subTest(loader=loader), self.assertRaises(ValueError):
                validate_profile(dict(version=1, cmd=['true'], env_file='file', env_loader=loader))
        for ref in (dict(file='relative', loader=[], require=[]),
                    dict(file=str(self.source), loader='shell', require=[]),
                    dict(file=str(self.source), loader=[], require=['NOT-VALID'])):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                validate_credentials([ref])


if __name__ == '__main__':
    unittest.main(verbosity=2)
