"""Local helper contract: independent layouts, overrides and real entry identity."""
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
        temp = tempfile.TemporaryDirectory(prefix="local roots '")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "arbitrary checkout"
        self.repo.mkdir()
        self.entry = self.repo / "entry.py"
        self.entry.touch()
        self.link = self.root / "other" / "linked entry.py"
        self.link.parent.mkdir()
        self.link.symlink_to(self.entry)

    def test_real_and_symlink_default_ignore_cwd_and_marker(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(workspace_root(self.entry), str(self.root))
            self.assertEqual(workspace_root(self.link), str(self.root))
            (self.repo / ".platform-root").touch()
            self.assertEqual(workspace_root(self.entry), str(self.root))

    def test_override_without_marker_and_blank_fallback(self):
        with patch.dict(os.environ, {"PLATFORM_WORKSPACE": str(self.root / "workspace space")}):
            for source in (self.entry, self.link):
                self.assertEqual(workspace_root(source), str(self.root / "workspace space"))
        with patch.dict(os.environ, {"PLATFORM_WORKSPACE": ""}):
            self.assertEqual(workspace_root(self.entry), str(self.root))

    def test_expanduser_relative_and_cli(self):
        with patch.dict(os.environ, {"HOME": str(self.root), "PLATFORM_WORKSPACE": "~/chosen"}):
            self.assertEqual(workspace_root(self.link), str(self.root / "chosen"))
        env = {**os.environ, "PLATFORM_WORKSPACE": "relative space"}
        result = subprocess.check_output([sys.executable, str(Path(__file__).with_name("platform_paths.py")), str(self.link)], cwd=self.root, env=env, text=True)
        self.assertEqual(result.strip(), str(self.root / "relative space"))


if __name__ == "__main__":
    unittest.main()
