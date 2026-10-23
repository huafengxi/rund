"""Standalone module imports and caller configuration, without network or state."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class IndependentTests(unittest.TestCase):
    def test_modules_use_only_their_real_local_helper(self):
        with tempfile.TemporaryDirectory(prefix="isolated imports '") as tmp:
            root = Path(tmp)
            repo = root / "release with spaces"
            repo.mkdir()
            # A parent helper is deliberately hostile: no silent monorepo fallback.
            (root / "platform_paths.py").write_text("raise RuntimeError('parent helper must not load')\n")
            for name in ("platform_paths.py", "delete_contract.py", "foreground_transport.py", "probe_scratch.py", "gc.py", "ssh-sync.py", "replica-tag.py"):
                shutil.copy2(HERE / name, repo / name)
            links = root / "links"
            links.mkdir()
            workspace = root / "caller root"
            env = {**os.environ, "PLATFORM_WORKSPACE": str(workspace), "AGENTS_SYNC_HUB": "fixture-hub", "AGENTS_SYNC_REMOTE": "fixture.invalid:/neutral"}
            script = "import runpy,sys,json; x=runpy.run_path(sys.argv[1]); print(json.dumps({k:x.get(k) for k in ['_WORKSPACE','DEFAULT_REMOTE','HUB']}))"
            for module in ("gc.py", "ssh-sync.py", "replica-tag.py"):
                link = links / module
                link.symlink_to(repo / module)
                for path in (repo / module, link):
                    with self.subTest(module=module, path=path):
                        r = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script, str(path)], cwd=links, env=env, text=True, capture_output=True)
                        self.assertEqual(r.returncode, 0, r.stderr)
                        values = json.loads(r.stdout)
                        self.assertEqual(values['_WORKSPACE'], str(workspace))
                        if module == "ssh-sync.py":
                            self.assertEqual(values['DEFAULT_REMOTE'], 'fixture.invalid:/neutral')
                        if module == "replica-tag.py":
                            self.assertEqual(values['HUB'], 'fixture-hub')
                        help_result = subprocess.run([sys.executable, "-I", "-S", "-B", str(path), "--help"], cwd=links, env=env, text=True, capture_output=True)
                        self.assertEqual(help_result.returncode, 0, help_result.stderr)

    def test_missing_endpoint_is_rejected_before_transport(self):
        env = dict(os.environ)
        env.pop('AGENTS_SYNC_REMOTE', None)
        r = subprocess.run([sys.executable, str(HERE / 'ssh-sync.py'), 'watch'], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn('remote required', r.stderr)

    def test_gc_hub_resolution_priority(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('independent_gc', HERE / 'gc.py')
        gc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gc)
        with tempfile.TemporaryDirectory() as root:
            neutral = Path(root) / 'neutral'
            neutral.mkdir()
            with patch.object(gc, 'NEUTRAL_HUB_DIR', str(neutral)):
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(gc.resolve_agents_dir(root), str(neutral))
                with patch.dict(os.environ, {'GC_WORKSPACE': root}, clear=True):
                    self.assertEqual(gc.resolve_agents_dir(root), str(Path(root) / 'agents'))
                with patch.dict(os.environ, {'GC_WORKSPACE': root, 'GC_AGENTS_DIR': str(neutral / 'explicit')}, clear=True):
                    self.assertEqual(gc.resolve_agents_dir(root), str(neutral / 'explicit'))


if __name__ == '__main__':
    unittest.main()
