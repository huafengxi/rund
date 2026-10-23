"""Never pull when a local original's ownership/protection is unknown."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ssh_sync', Path(__file__).with_name('ssh-sync.py'))
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


class ProtectTests(unittest.TestCase):
    def test_stat_failure_is_not_a_complete_walk(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'original').write_text('mine')
            with patch.object(ss.os, 'lstat', side_effect=PermissionError('unreadable')):
                self.assertEqual(ss.unmarked_files(root, 12345), ([], False))
            with patch.object(ss.os, 'lstat', side_effect=FileNotFoundError()):
                self.assertEqual(ss.unmarked_files(root, 12345), ([], True))

    def test_unprotectable_names_abort_before_rsync(self):
        args = SimpleNamespace(verbose=False)
        with tempfile.TemporaryDirectory() as root:
            for rel in ('task/a/a\nb', 'task/a/a\\b', 'task/a/a\0b',
                        '/absolute', 'task/../a', 'task/a/ a', 'task/a/\udcff'):
                with self.subTest(rel=rel), \
                     patch.object(ss, 'gc_exclude_and_targets', return_value=([], [], [])), \
                     patch.object(ss, 'unmarked_files', return_value=([rel], True)), \
                     patch.object(ss.subprocess, 'run') as transfer:
                    self.assertEqual(ss.watch_rsync('cfg', 'hub', root, '/hub', args,
                                                    'pull', 12345), (False, 0))
                    transfer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
