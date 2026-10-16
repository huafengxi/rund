"""Local root contract v1; all files are synthetic and retained.

Adapted from agent-platform/test_platform_paths.py. No MODULES/marker required.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from platform_paths import workspace_root


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="serviced-root-")).resolve()
        self.ws = self.root / "caller with spaces"
        self.repo = self.ws / "arbitrary-checkout"
        self.repo.mkdir(parents=True)
        self.entry = self.repo / "entry.py"
        self.entry.touch()
        self.alias = self.root / "entry alias.py"
        self.alias.symlink_to(self.entry)

    def test_default_real_source_parent(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(workspace_root(self.entry), str(self.ws))
            self.assertEqual(workspace_root(self.alias), str(self.ws))
        self.assertFalse((self.repo / ".platform-root").exists())

    def test_empty_override_defaults(self):
        with patch.dict(os.environ, {"PLATFORM_WORKSPACE": ""}, clear=True):
            self.assertEqual(workspace_root(self.alias), str(self.ws))

    def test_nonempty_override_separate_from_source(self):
        with patch.dict(os.environ, {"PLATFORM_WORKSPACE": str(self.root / "elsewhere")}):
            self.assertEqual(workspace_root(self.alias), str(self.root / "elsewhere"))
            self.assertEqual(self.alias.resolve(), self.entry)

    def test_tilde_override(self):
        with patch.dict(os.environ, {"HOME": str(self.root), "PLATFORM_WORKSPACE": "~/custom root"}):
            self.assertEqual(workspace_root(self.alias), str(self.root / "custom root"))

    def test_relative_override_is_absolute(self):
        with patch.dict(os.environ, {"PLATFORM_WORKSPACE": "relative workspace"}):
            self.assertEqual(workspace_root(self.entry), str(Path("relative workspace").resolve()))

    def test_helper_cli_requires_entry(self):
        helper = Path(__file__).resolve().with_name("platform_paths.py")
        run = subprocess.run([sys.executable, str(helper)], cwd=self.root,
                             capture_output=True, text=True, timeout=5)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("usage:", run.stderr)

    def test_helper_cli_from_different_cwd_with_relative_and_symlink_entries(self):
        helper = Path(__file__).resolve().with_name("platform_paths.py")
        env = dict(os.environ)
        env.pop("PLATFORM_WORKSPACE", None)
        for source in (str(self.entry), str(self.alias), os.path.relpath(self.alias, self.root)):
            run = subprocess.run([sys.executable, str(helper), source], cwd=self.root,
                                 env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), str(self.ws))


if __name__ == "__main__":
    unittest.main()
