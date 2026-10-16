"""Pinned agentd envscrub contract in standalone release/development layouts.

Only copies the pinned dependency into self-created fixtures. Never consults a
production platform checkout or starts a service.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
DEPENDENCY = json.loads((HERE / "recovery/dependencies.json").read_text())["agentd"]


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="serviced-dependency-")).resolve()
        self.repo = self.root / "release with spaces" / "serviced"
        self.repo.mkdir(parents=True)
        for name in ("serviced.py", "platform_paths.py"):
            shutil.copy2(HERE / name, self.repo / name)
        self.dep = Path(os.environ.get("AGENTD_DIR", HERE.parent / "agentd")).expanduser().resolve()
        self.scrub = self.dep / "envscrub.py"
        self.assertEqual(hashlib.sha256(self.scrub.read_bytes()).hexdigest(), DEPENDENCY["envscrub_sha256"])
        self.ws = self.root / "workspace"
        (self.ws / "services/profiles").mkdir(parents=True)
        (self.ws / "services/daemons").mkdir()
        (self.ws / "services/profiles/sample.json").write_text(json.dumps({"version": 1, "cmd": ["unused"]}))
        self.env = {"HOME": str(self.root), "PATH": os.environ.get("PATH", os.defpath),
                    "PLATFORM_WORKSPACE": str(self.ws), "PYTHONDONTWRITEBYTECODE": "1"}

    def dep_copy(self, target):
        target.mkdir(parents=True)
        shutil.copy2(self.scrub, target / "envscrub.py")
        return target

    def run_status(self, **env):
        return subprocess.run([sys.executable, str(self.repo / "serviced.py"), "status"],
                              env=dict(self.env, **env), cwd=self.root,
                              capture_output=True, text=True, timeout=5)

    def assert_ok(self, run):
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("0 services, 0 drift", run.stdout)

    def test_explicit_non_sibling_pinned_checkout(self):
        self.assert_ok(self.run_status(AGENTD_DIR=str(self.dep)))
        self.assertFalse((self.repo.parent / "agentd").exists())

    def test_development_sibling_exact_pinned_module(self):
        self.dep_copy(self.repo.parent / "agentd")
        self.assert_ok(self.run_status())

    def test_explicit_wins_over_broken_sibling(self):
        sibling = self.repo.parent / "agentd"
        sibling.mkdir()
        (sibling / "envscrub.py").write_text("raise RuntimeError('must not import sibling')\n")
        self.assert_ok(self.run_status(AGENTD_DIR=str(self.dep)))

    def test_explicit_tilde_and_relative_resolution(self):
        self.dep_copy(self.root / "dependency with spaces")
        self.assert_ok(self.run_status(AGENTD_DIR="~/dependency with spaces"))
        self.assert_ok(self.run_status(AGENTD_DIR="dependency with spaces"))

    def test_missing_dependency_fails_clearly_not_unified_or_sys_path(self):
        # A would-be old unified layout and an import-path module cannot rescue it.
        self.dep_copy(self.root / "agent-platform/agentd")
        poison = self.dep_copy(self.root / "pythonpath")
        run = self.run_status(PYTHONPATH=str(poison))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("serviced requires agentd envscrub", run.stderr)
        self.assertIn("AGENTD_DIR", run.stderr)
        self.assertNotIn("Traceback", run.stderr)

    def test_invalid_explicit_never_falls_back_to_valid_sibling(self):
        self.dep_copy(self.repo.parent / "agentd")
        for explicit in (str(self.root / "missing"), ""):
            run = self.run_status(AGENTD_DIR=explicit)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("AGENTD_DIR", run.stderr)

    def test_missing_interface_is_clear_failure(self):
        bad = self.root / "incompatible"
        bad.mkdir()
        (bad / "envscrub.py").write_text("VALUE = 1\n")
        run = self.run_status(AGENTD_DIR=str(bad))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("scrub_env interface missing", run.stderr)

    def test_import_uses_exact_file_and_actual_policy(self):
        probe = '''import importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location("sv",sys.argv[1])
sv=importlib.util.module_from_spec(spec);spec.loader.exec_module(sv)
assert sv.SOURCE_DIR == __import__("pathlib").Path(sys.argv[1]).resolve().parent
base={k:"outer" for k in sv.envscrub.ENV_SCRUB_EXACT}
base.update({p+"CASE":"outer" for p in sv.envscrub.ENV_SCRUB_PREFIXES})
base.update(OPENAI_API_KEY="service-key",NORMAL="normal")
os.environ.update(base)
actual=sv.clean_env();expected=sv.envscrub.scrub_env(keep=frozenset())
actual.pop("PATH",None);expected.pop("PATH",None)
assert actual==expected
assert actual["OPENAI_API_KEY"]=="service-key"
assert actual["NORMAL"]=="normal"
print(sv.envscrub.__file__)
'''
        alias = self.root / "service alias.py"
        alias.symlink_to(self.repo / "serviced.py")
        run = subprocess.run([sys.executable, "-c", probe, str(alias)],
                              env=dict(self.env, AGENTD_DIR=str(self.dep)), cwd=self.root,
                              text=True, capture_output=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(Path(run.stdout.strip()), self.scrub)


if __name__ == "__main__":
    unittest.main()
