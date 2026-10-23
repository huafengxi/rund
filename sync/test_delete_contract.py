"""Shared deletion guards at both ends of the wire, including root entries."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import shutil
import unittest

import delete_contract as contract



def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gc = load("gc_contract_cli", "gc.py")
sync = load("sync_contract_consumer", "ssh-sync.py")


class DeleteContractTests(unittest.TestCase):
    def test_producer_and_consumer_share_decisions(self):
        for entry in (".", "./", "", "/abs", "a/../b", "task/", "queue/",
                      "gc/", "gc/other", "gc/delete-list.000001", "wild*card",
                      "task/item/", "legacy", "topic/dispatcher/", "queue/dispatcher/inbox/a.msg"):
            for forced in (False, True):
                with self.subTest(entry=entry, forced=forced):
                    expected = contract.validate_path(entry, force=forced)
                    self.assertEqual(gc.validate_path(entry, force=forced), expected)
                    self.assertEqual(sync._gc_shape_refused(entry.rstrip("/"), entry.endswith("/"), forced), expected)

    def test_sync_literal_names_do_not_relax_gc_glob_guards(self):
        self.assertEqual(contract.GLOB_UNSAFE, frozenset('*?[\n'))
        for rel in ('task/a/[heartbeat run].msg', 'task/a/a*b', 'task/a/a?b',
                    'task/a/a\nb'):
            for forced in (False, True):
                with self.subTest(rel=rel, forced=forced):
                    self.assertIsNotNone(contract.validate_path(rel, force=forced))
                    self.assertIsNotNone(gc.validate_path(rel, force=forced))
                    self.assertIsNotNone(sync._gc_shape_refused(rel, False, forced))
        base = Path(tempfile.mkdtemp(prefix='delete-contract-test-'))
        root = base / 'work'
        root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(root))
        (root / 'gc').mkdir()
        entries = ['task/a/[heartbeat run].msg', 'task/a/a*b', 'task/a/a?b']
        (root / 'gc/delete-list.000001').write_text(
            '\n'.join(entries + [p + ' #FORCED:test' for p in entries]) + '\n')
        excludes, targets, refused = sync.gc_exclude_and_targets(str(root))
        self.assertEqual(excludes, [])
        self.assertEqual(targets, [])
        self.assertEqual(set(refused), set(entries))

    def test_root_entries_never_become_deletion_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gc").mkdir()
            marker = root / "sentinel"
            marker.write_text("keep")
            (root / "gc/delete-list.000001").write_text(".\n./\n. #FORCED:test\n./ #FORCED:test\n")
            excludes, targets, refused = sync.gc_exclude_and_targets(tmp)
            self.assertEqual(excludes, [])
            self.assertEqual(targets, [])
            self.assertEqual(set(refused), {".", "./"})
            sync.apply_gc_deletions(tmp)
            self.assertEqual(marker.read_text(), "keep")

    def test_hub_layout_policy_does_not_change_consumer_contract(self):
        original = gc.FORBIDDEN_LAYOUT_DIRS
        try:
            gc.FORBIDDEN_LAYOUT_DIRS = ("temporary",)
            self.assertIsNotNone(gc.validate_path("temporary/item"))
            self.assertIsNone(sync._gc_shape_refused("temporary/item", False))
            self.assertIsNone(gc.validate_path("temporary/item", force=True))
        finally:
            gc.FORBIDDEN_LAYOUT_DIRS = original


if __name__ == "__main__":
    unittest.main()
