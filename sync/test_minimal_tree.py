#!/usr/bin/env python3
"""Minimal pull descriptions: only self-created fixtures, no production tree.

SSH_SYNC_TEST_HOST may name an SSH alias for THIS machine (the fixture paths
must be visible unchanged). Set it explicitly to also exercise native remote
sender validation, which the local transport adapter cannot measure.
"""
import hashlib
import os
from pathlib import Path
import random
import stat
import subprocess
import unittest
from unittest.mock import patch

from test_pull_manifest import Fixture, grp, ss

SSH_HOST = os.environ.get('SSH_SYNC_TEST_HOST')


def closure(paths):
    result = set()
    for path in paths:
        parts = path.split('/')
        result.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
    return result


class MinimalTreeTests(unittest.TestCase):
    def test_empty_single_duplicates_and_generator(self):
        self.assertEqual(ss.minimal_pull_tree([]), [])
        self.assertEqual(ss.minimal_pull_tree(['only']), ['only'])
        self.assertEqual(ss.minimal_pull_tree(iter(['a', 'a/b', 'a/b'])), ['a/b'])

    def test_literal_components_not_string_or_glob_prefixes(self):
        paths = ['a', 'a/b', 'a/bc', 'a-b', 'aa', 'a[b]', 'a[b]/结果',
                 'a*', 'a*/file', 'a?', 'a?/space name', 'ax/file']
        result = ss.minimal_pull_tree(reversed(paths))
        self.assertEqual(result, sorted(set(paths) - {'a', 'a[b]', 'a*', 'a?'}))
        self.assertEqual(closure(result), closure(paths))

    def test_directory_only_tree_retains_empty_leaves(self):
        self.assertEqual(ss.minimal_pull_tree(['a', 'a/empty', 'b', 'b/deep',
                                               'b/deep/empty', 'standalone']),
                         ['a/empty', 'b/deep/empty', 'standalone'])

    def test_filter_before_reduction_preserves_now_empty_directory(self):
        selected = ss.pull_files(['a', 'a/original'], ['a/original'], [])
        self.assertEqual(ss.minimal_pull_tree(selected), ['a'])
        selected = ss.pull_files(['a', 'a/dead', 'a/dead/file'], [], ['/a/dead/'])
        self.assertEqual(ss.minimal_pull_tree(selected), ['a'])
        selected = ss.pull_files(['a', 'a/x.pending', 'a/x.pending/file'], [], [],
                                 ['*.pending'])
        self.assertEqual(ss.minimal_pull_tree(selected), ['a'])

    def test_filtered_type_conflict_does_not_reappear_as_implied_directory(self):
        paths = ['collision', 'collision/child', 'collision-neighbor/file']
        selected = ss.pull_files(paths, ['collision'], [])
        result = ss.minimal_pull_tree(selected)
        self.assertEqual(result, ['collision-neighbor/file'])
        self.assertNotIn('collision', closure(result))

    def test_random_trees_preserve_exact_ancestor_closure(self):
        rng = random.Random(0xA151EC)
        for _ in range(100):
            leaves = ['/'.join(rng.choices(['a', 'b', 'c', 'literal*?[x]', '中文'],
                                          k=rng.randrange(1, 12)))
                      for _ in range(100)]
            tree = closure(leaves)
            result = ss.minimal_pull_tree(tree)
            self.assertEqual(closure(result), tree)
            self.assertTrue(set(result) <= tree)
            self.assertEqual(ss.minimal_pull_tree(result), result)
            ancestors = set()
            for path in result:
                ancestors.update(closure([path]) - {path})
            self.assertFalse(set(result) & ancestors)

    def test_large_deep_tree_no_file_loss(self):
        leaves = [f'task/t{i // 100}/validation/deep/state/run/{i}/result'
                  for i in range(22000)]
        tree = closure(leaves)
        result = ss.minimal_pull_tree(tree)
        self.assertEqual(result, sorted(leaves))
        self.assertEqual(closure(result), tree)


class MinimalTransferTests(Fixture):
    def setUp(self):
        super().setUp()
        try:
            self.gid = grp.getgrnam(ss.REPLICA_GROUP).gr_gid
            p = self.write(self.dst, 'probe')
            os.chown(p, -1, self.gid)
        except (KeyError, OSError):
            self.skipTest('replica group membership unavailable')
        if self.gid == os.getgid():
            self.skipTest('need distinct primary and replica groups')
        # mac /tmp can inherit wheel; emulate a production primary-group root.
        os.chown(self.dst, -1, os.getgid())

    def snapshot(self, root):
        result = {}
        for current, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                p = Path(current) / name
                rel = str(p.relative_to(root))
                if rel == 'probe':
                    continue
                st = p.lstat()
                value = [stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode),
                         st.st_gid, st.st_mtime_ns]
                if stat.S_ISLNK(st.st_mode):
                    value.append(os.readlink(p))
                elif stat.S_ISREG(st.st_mode):
                    value.append(hashlib.sha256(p.read_bytes()).hexdigest())
                result[rel] = value
        return result

    def old_times(self, root):
        for current, dirs, files in os.walk(root, followlinks=False):
            for p in [Path(current)] + [Path(current) / n for n in dirs + files]:
                os.utime(p, ns=(1700000000123456000, 1700000000123456000),
                         follow_symlinks=False)

    def restore_fixture_permissions(self):
        for current, dirs, _ in os.walk(self.work, followlinks=False):
            for p in [Path(current)] + [Path(current) / n for n in dirs]:
                st = p.lstat()
                if stat.S_ISDIR(st.st_mode):
                    os.chmod(p, stat.S_IMODE(st.st_mode) | stat.S_IRWXU,
                             follow_symlinks=False)

    def full_pull_into(self, dst):
        previous = self.dst
        self.dst = dst
        try:
            with patch.object(ss, 'minimal_pull_tree', side_effect=lambda p: list(p)):
                return self.pull(self.gid)
        finally:
            self.dst = previous

    def test_full_vs_minimal_tree_and_readonly_parent_update(self):
        self.addCleanup(self.restore_fixture_permissions)
        full = self.work / 'full-description'
        full.mkdir()
        os.chown(full, -1, os.getgid())
        for name in ['nested/readonly/file', 'nested/sibling/value',
                     '字面*?[x]/sent filename', 'standalone']:
            self.write(self.src, name, name)
        (self.src / 'empty/deep/leaf').mkdir(parents=True)
        (self.src / 'broken-link').symlink_to('missing target')
        os.mkfifo(self.src / 'fifo')
        (self.src / 'nested/readonly').chmod(0o555)
        self.old_times(self.src)
        self.assertTrue(self.full_pull_into(full)[0])
        full_request = self.transfers[-1][1].split('\0')[:-1]
        self.assertTrue(self.pull(self.gid)[0])
        minimal_request = self.transfers[-1][1].split('\0')[:-1]
        self.assertLess(len(minimal_request), len(full_request))
        self.assertEqual(self.snapshot(full), self.snapshot(self.dst))
        self.assertEqual(closure(minimal_request), closure(full_request))
        self.assertIn('empty/deep/leaf', minimal_request)
        self.assertNotIn('nested', minimal_request)
        src = self.src / 'nested/readonly/file'
        src.write_text('NEW CONTENT')
        os.utime(src, ns=(1700000010123456000, 1700000010123456000))
        self.assertTrue(self.full_pull_into(full)[0])
        self.assertTrue(self.pull(self.gid)[0])
        self.assertEqual(self.snapshot(full), self.snapshot(self.dst))
        self.assertEqual((self.dst / 'nested/readonly/file').read_text(), 'NEW CONTENT')
        self.assertEqual(stat.S_IMODE((self.dst / 'nested/readonly').stat().st_mode), 0o555)
        # Parent-only metadata changes must survive even when every payload
        # checksum matches and only implied ancestors carry the new attrs.
        (self.src / 'nested').chmod(0o750)
        (self.src / 'empty/deep').chmod(0o710)
        for rel in ['nested', 'empty/deep']:
            os.utime(self.src / rel, ns=(1700000020123456000, 1700000020123456000))
        self.assertTrue(self.full_pull_into(full)[0])
        self.assertTrue(self.pull(self.gid)[0])
        self.assertEqual(self.snapshot(full), self.snapshot(self.dst))
        self.assertEqual(stat.S_IMODE((self.dst / 'nested').stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE((self.dst / 'empty/deep').stat().st_mode), 0o710)

    def test_all_blocked_children_leave_parent_but_never_expand_siblings(self):
        original = self.write(self.dst, 'parent/original', 'LOCAL')
        before = original.stat()
        self.write(self.src, 'parent/original', 'REMOTE')
        self.write(self.src, 'parent/dead/file', 'DELETED')
        self.write(self.src, 'parent/omit.pending/file', 'UNPUBLISHED')
        self.write(self.dst, 'gc/delete-list.000001', 'parent/dead/\n')
        with patch.object(ss, 'WATCH_EXCLUDES', ['*.pending']):
            self.assertTrue(self.pull(self.gid)[0])
        wire = self.transfers[-1][1].split('\0')[:-1]
        self.assertIn('parent', wire)
        self.assertFalse((self.dst / 'parent/dead').exists())
        self.assertFalse((self.dst / 'parent/omit.pending').exists())
        self.assertEqual(original.read_text(), 'LOCAL')
        self.assertEqual((original.stat().st_ino, original.stat().st_gid),
                         (before.st_ino, before.st_gid))

    def test_implied_parent_replaces_replica_symlink_without_following_target(self):
        outside = self.write(self.work, 'outside/keep', 'OUTSIDE')
        before = outside.stat()
        (self.dst / 'parent').symlink_to('../outside')
        os.chown(self.dst / 'parent', -1, self.gid, follow_symlinks=False)
        self.write(self.src, 'parent/deep/file', 'REMOTE')
        self.assertTrue(self.pull(self.gid)[0])
        self.assertFalse((self.dst / 'parent').is_symlink())
        self.assertEqual((self.dst / 'parent/deep/file').read_text(), 'REMOTE')
        self.assertEqual(outside.read_text(), 'OUTSIDE')
        self.assertEqual((outside.stat().st_ino, outside.stat().st_gid),
                         (before.st_ino, before.st_gid))

    def test_implied_parent_replaces_replica_file_but_original_is_opaque(self):
        p = self.write(self.dst, 'parent', 'REPLICA')
        os.chown(p, -1, self.gid)
        self.write(self.src, 'parent/deep/file', 'REMOTE')
        self.assertTrue(self.pull(self.gid)[0])
        self.assertEqual((self.dst / 'parent/deep/file').read_text(), 'REMOTE')
        p = self.write(self.dst, 'original-file', 'LOCAL')
        before = p.stat()
        self.write(self.src, 'original-file/deep/file', 'FORBIDDEN')
        self.assertTrue(self.pull(self.gid)[0])
        self.assertEqual(p.read_text(), 'LOCAL')
        self.assertEqual(p.stat().st_ino, before.st_ino)
        self.assertNotIn('original-file', closure(self.transfers[-1][1].split('\0')[:-1]))

    def test_implied_directories_restore_primary_group_on_darwin(self):
        self.write(self.src, 'new/deep/file', 'REMOTE')
        with patch.object(ss.sys, 'platform', 'darwin'):
            self.assertTrue(self.pull(self.gid)[0])
        self.assertEqual((self.dst / 'new/deep/file').stat().st_gid, self.gid)
        for rel in ['new', 'new/deep']:
            self.assertEqual((self.dst / rel).stat().st_gid, os.getgid())

    def test_single_transfer_and_native_validation_flags_preserved(self):
        self.write(self.src, 'a/b/result', 'REMOTE')
        self.assertTrue(self.pull(self.gid)[0])
        self.assertEqual((self.scans, len(self.transfers)), (1, 1))
        cmd, wire = self.transfers[0]
        self.assertEqual(wire, 'a/b/result\0')
        self.assertIn('c', cmd[1])
        for flag in ['--files-from=-', '--from0', '--no-recursive', '--dirs']:
            self.assertIn(flag, cmd)
        for flag in ['--trust-sender', '--old-args', '--delete', '--no-implied-dirs']:
            self.assertNotIn(flag, cmd)
        self.assertEqual(self.gc_applied, 1)

    def test_vanished_leaf_failure_then_empty_directory_retry(self):
        leaf = self.write(self.src, 'parent/deep/leaf', 'VANISH')
        vanished = False
        def disappearing_sender(cmd, **kw):
            nonlocal vanished
            if cmd[0] == ss.RSYNC_BIN and not vanished:
                leaf.rename(self.work / 'withdrawn-leaf')
                vanished = True
            return self.adapter(cmd, **kw)
        with patch.object(ss.subprocess, 'run', side_effect=disappearing_sender), \
             patch.object(ss, '_watch_ssh_e', return_value=[]), \
             patch.object(ss, 'apply_gc_deletions') as gc_apply:
            result = ss.watch_rsync('cfg', 'hub', str(self.dst), str(self.src),
                                    self.args, 'pull', self.gid)
        self.assertFalse(result[0])
        self.assertEqual(gc_apply.call_count, 0)
        self.assertTrue(self.pull(self.gid)[0])
        self.assertTrue((self.dst / 'parent/deep').is_dir())
        self.assertEqual(list((self.dst / 'parent/deep').iterdir()), [])
        self.assertEqual(self.transfers[-1][1], 'parent/deep\0')

    def test_new_gc_list_applies_after_single_minimal_transfer(self):
        self.write(self.src, 'gc/delete-list.000001', 'task/dead/\n')
        self.write(self.src, 'task/dead/replica', 'RETIRED')
        self.write(self.dst, 'task/dead/original', 'AUTHORIZED_GC')
        with patch.object(ss.subprocess, 'run', side_effect=self.adapter), \
             patch.object(ss, '_watch_ssh_e', return_value=[]):
            result = ss.watch_rsync('cfg', 'hub', str(self.dst), str(self.src),
                                    self.args, 'pull', self.gid)
        self.assertTrue(result[0])
        self.assertEqual(len(self.transfers), 1)
        self.assertFalse((self.dst / 'task/dead').exists())
        self.assertEqual((self.dst / 'gc/delete-list.000001').stat().st_gid, self.gid)
        self.assertTrue(self.pull(self.gid)[0])
        self.assertFalse((self.dst / 'task/dead').exists())

    @unittest.skipUnless(SSH_HOST, 'set SSH_SYNC_TEST_HOST to this machine SSH alias')
    def test_real_ssh_native_sender_validation_and_checksum(self):
        self.write(self.src, 'dir*?[x]/chosen', 'AAAA')
        self.write(self.src, 'dir*?[x]/unlisted', 'FORBIDDEN')
        self.write(self.src, 'dirX/unlisted', 'FORBIDDEN')
        original = self.write(self.dst, 'dir*?[x]/original', 'LOCAL')
        before = original.stat()
        self.write(self.src, 'dir*?[x]/original', 'FORBIDDEN')
        dst = self.write(self.dst, 'dir*?[x]/chosen', 'BBBB')
        os.chown(dst, -1, self.gid)
        st = (self.src / 'dir*?[x]/chosen').stat()
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        (self.src / 'empty/leaf').mkdir(parents=True)
        # Deliberately supply a partial logical tree, so sibling expansion
        # would be observable. Real SSH and real remote rsync are not adapted.
        manifest = ['dir*?[x]', 'dir*?[x]/chosen', 'dir*?[x]/original',
                    'empty', 'empty/leaf']
        recorded = []
        def real_transport(cmd, **kw):
            if cmd[0] == ss.RSYNC_BIN:
                recorded.append((cmd, kw.get('input')))
            return self.real_run(cmd, **kw)
        with patch.object(ss, 'remote_manifest', return_value=manifest), \
             patch.object(ss.subprocess, 'run', side_effect=real_transport), \
             patch.object(ss, '_WATCH_REMOTE_RSYNC_OPTS', ['--rsync-path', ss.RSYNC_BIN]), \
             patch.object(ss, 'apply_gc_deletions') as gc_apply:
            result = ss.watch_rsync(ss.ssh_config_path(), SSH_HOST, str(self.dst),
                                    str(self.src), self.args, 'pull', self.gid)
        self.assertEqual(result, (True, 1))
        self.assertEqual(dst.read_text(), 'AAAA')
        self.assertEqual(dst.stat().st_gid, self.gid)
        self.assertEqual(original.read_text(), 'LOCAL')
        self.assertEqual((original.stat().st_ino, original.stat().st_gid),
                         (before.st_ino, before.st_gid))
        self.assertTrue((self.dst / 'empty/leaf').is_dir())
        self.assertFalse((self.dst / 'dir*?[x]/unlisted').exists())
        self.assertFalse((self.dst / 'dirX').exists())
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][1], 'dir*?[x]/chosen\0empty/leaf\0')
        self.assertNotIn('--trust-sender', recorded[0][0])
        self.assertEqual(gc_apply.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
