"""Caller-provided environment loaders without workspace Python imports."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).with_name("serviced.py")
spec = importlib.util.spec_from_file_location("serviced_env_test", MODULE)
sv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sv)


class EnvironmentLoaderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="serviced-env-test-"))
        self.source = self.root / "env file 'literal'.env"
        self.source.write_text("TOKEN=plain-test-value\n")
        self.addCleanup(patch.stopall)
        patch.object(sv, "WS", self.root).start()
        patch.object(sv, "PIDS_DIR", self.root / "run/pids").start()
        patch.object(sv, "LOGS_DIR", self.root / "run/logs").start()
        # Dependency location is configuration, not a task identity. Retain it
        # for the status subprocess even in a non-sibling release layout.
        env = {"HOME": str(self.root)}
        if "AGENTD_DIR" in os.environ:
            env["AGENTD_DIR"] = os.environ["AGENTD_DIR"]
        patch.dict(os.environ, env, clear=True).start()

    def service(self, **extra):
        return dict(name="sample", env_file=self.source.name, require_env=["TOKEN"], **extra)

    def loader(self, body):
        script = self.root / "caller loader.py"
        script.write_text(body)
        return [sys.executable, str(script)]

    def test_plaintext_grammar_and_overlay_order(self):
        self.source.write_text("# comment\nexport TOKEN = 'plain test value'\n"
                               "exportFOO = unchanged\nUNPAIRED='literal\n")
        env = sv.build_env(self.service(extra_env={"TOKEN": "override", "DIR": "~/data"}))
        self.assertEqual(env["TOKEN"], "override")
        self.assertEqual(env["exportFOO"], "unchanged")
        self.assertEqual(env["UNPAIRED"], "'literal")
        self.assertEqual(env["DIR"], str(self.root / "data"))

    def test_loader_receives_literal_path_clean_environment_and_workspace(self):
        loader = self.loader("import os,pathlib,sys\n"
                             "assert 'AGENT_SELF' not in os.environ\n"
                             "assert pathlib.Path.cwd() == pathlib.Path(sys.argv[1]).parent.resolve()\n"
                             "assert pathlib.Path(sys.argv[1]).read_text().startswith('TOKEN=')\n"
                             "print(" + repr("export TOKEN = 'loaded 测试 value'") + ")\n")
        with patch.dict(os.environ, {"AGENT_SELF": "task/outer"}):
            env = sv.build_env(self.service(env_loader=loader))
        self.assertEqual(env["TOKEN"], "loaded 测试 value")

    def test_loader_failure_is_bounded_and_does_not_leak_output(self):
        for body in ("import sys\nprint('DO_NOT_LOG_TEST_VALUE')\n"
                     "print('DO_NOT_LOG_TEST_VALUE', file=sys.stderr)\nsys.exit(1)\n",
                     "import time\ntime.sleep(10)\n"):
            with self.subTest(body=body):
                loader = self.loader(body)
                out = io.StringIO()
                with patch.object(sv, "ENV_LOAD_TIMEOUT", 0.1), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                     self.assertRaises(SystemExit) as error:
                    sv.build_env(self.service(env_loader=loader))
                self.assertIn("TOKEN not set", str(error.exception))
                self.assertNotIn("DO_NOT_LOG_TEST_VALUE", str(error.exception) + out.getvalue())

    def test_failed_load_preserves_exported_value_and_required_gate(self):
        loader = self.loader("raise SystemExit(1)\n")
        with patch.dict(os.environ, {"TOKEN": "exported-test-value"}):
            self.assertEqual(sv.build_env(self.service(env_loader=loader))["TOKEN"], "exported-test-value")
        self.source.unlink()
        with self.assertRaises(SystemExit):
            sv.build_env(self.service())

    def test_invalid_loader_is_a_registry_error(self):
        for loader in (None, "shell command", [], [""], [3]):
            with self.subTest(loader=loader), self.assertRaises(SystemExit):
                sv._validate_entry(dict(name="sample", cmd=["true"], env_file="file", env_loader=loader))
        with self.assertRaises(SystemExit):
            sv._validate_entry(dict(name="sample", cmd=["true"], env_loader=["loader"]))

    def test_require_env_refuses_before_spawn(self):
        with patch.object(sv, "svc_entry", return_value=self.service(cmd=["unused"])), \
             patch.object(sv, "pidfile_alive", return_value=None), \
             patch.object(sv.subprocess, "Popen") as spawn, \
             self.assertRaises(SystemExit):
            self.source.write_text("OTHER=not-token\n")
            sv.cmd_start_name("sample")
        spawn.assert_not_called()

    def test_loader_argv_no_shell_and_timeout(self):
        loader = ["literal executable", "one ; touch should-not-exist"]
        result = type("Result", (), {"stdout": "TOKEN=loaded\n"})()
        with patch.object(sv.subprocess, "run", return_value=result) as run:
            self.assertEqual(sv.build_env(self.service(env_loader=loader))["TOKEN"], "loaded")
        args, kw = run.call_args
        self.assertEqual(args[0], loader + [str(self.source)])
        self.assertNotIn("shell", kw)
        self.assertEqual(kw["timeout"], sv.ENV_LOAD_TIMEOUT)
        self.assertEqual(kw["cwd"], str(self.root))

    def test_envscrub_is_single_dependency_policy(self):
        # Derive policy cases from the dependency, never mirror its scrub list.
        dirty = {k: "outer" for k in sv.envscrub.ENV_SCRUB_EXACT}
        dirty.update({p + "TEST": "outer" for p in sv.envscrub.ENV_SCRUB_PREFIXES})
        dirty.update(OPENAI_API_KEY="service-key", ORDINARY="ordinary")
        with patch.dict(os.environ, dirty):
            env = sv.clean_env()
            expected = sv.envscrub.scrub_env(keep=frozenset())
        env.pop("PATH")
        expected.pop("PATH", None)
        self.assertEqual(env, expected)
        self.assertEqual(env["OPENAI_API_KEY"], "service-key")
        self.assertEqual(env["ORDINARY"], "ordinary")

    def test_status_works_in_workspace_without_encrypt_module(self):
        profiles = self.root / "services/profiles"
        profiles.mkdir(parents=True)
        (self.root / "services/daemons").mkdir()
        (profiles / "sample.json").write_text(json.dumps({"version": 1, "cmd": ["true"]}))
        env = dict(os.environ, PLATFORM_WORKSPACE=str(self.root))
        run = subprocess.run([sys.executable, str(MODULE), "status"], env=env,
                             text=True, capture_output=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("0 services, 0 drift", run.stdout)
        self.assertFalse((self.root / "encrypt").exists())


if __name__ == "__main__":
    unittest.main()
