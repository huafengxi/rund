#!/usr/bin/env python3
"""Full-tree pull selection: scale, fail-closed scans and REAL local rsync.

Only self-created /tmp fixtures are used; no daemon, SSH host or agents tree.
The transport adapter runs the actual remote scanner locally and changes only
rsync's source endpoint from hub:/tmp/... to /tmp/... . All pull flags, stdin,
checksum arbitration and post-transfer marking come from watch_rsync itself.
"""
import grp
import importlib.util
import os
import shutil
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('manifest_sync', HERE / 'ssh-sync.py')
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


class SelectionTests(unittest.TestCase):
    def test_large_protection_set_and_subtree_type_conflicts(self):
        originals = [f'task/t{i:06d}/original' for i in range(220000)]
        manifest = originals + [p + '/child' for p in originals]
        manifest += ['task/new', 'task/new/empty', 'task/new/result']
        self.assertEqual(ss.pull_files(manifest, originals, []),
                         ['task/new', 'task/new/empty', 'task/new/result'])

    def test_gc_and_exclusion_ancestor_semantics(self):
        manifest = ['task/dead', 'task/dead/x', 'other/task/dead/x',
                    'task/deadish/x', 'a.pending', 'a.pending/child',
                    'nested/a.pending', 'nested/a.pending/child',
                    'a.pending.msg', 'x/ack/id', 'x/in.msg']
        self.assertEqual(ss.pull_files(manifest, [], ['/task/dead/'], ['*.pending']),
                         sorted(['other/task/dead/x', 'task/deadish/x',
                                 'a.pending.msg', 'x/ack/id', 'x/in.msg']))
        # File-style GC entries also block sender directories of that name.
        self.assertEqual(ss.pull_files(['task/dead/x'], [], ['/task/dead']), [])

    def test_unsafe_manifest_is_rejected_not_normalized(self):
        for rel in ('/abs', '../x', 'a/../x', 'a//x', 'a/./x', '', '.',
                    'a/', 'a\nb', 'a\\b', 'a\udcff', ' a', 'a\t',
                    'a\u2028b', 'a\u0085b'):
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                ss.pull_files([rel], [], [])

    def test_literal_globs_are_not_protection_patterns(self):
        paths = ['queue/dispatcher/inbox/[heartbeat run].msg',
                 'queue/dispatcher/ack/[heartbeat run]',
                 'literal/a*b', 'literal/a?b', 'literal/a]b']
        self.assertEqual(ss.pull_files(paths, [], []), sorted(paths))
        self.assertEqual(ss.pull_files(paths, paths, []), [])
        self.assertEqual(ss.pull_files(['a*b', 'axxb', 'a*b/child'], ['a*b'], []),
                         ['axxb'])

    def test_prefixed_output_has_unambiguous_names(self):
        files, dirs = ss._rsync_transferred(
            'receiving file list\nSSH_SYNC sent name\n'
            'SSH_SYNC link -> name\nSSH_SYNC empty/\n'
            'protected-original\nsent 100 bytes\ntotal size is 42\n', prefixed=True)
        self.assertEqual(files, {'sent name', 'link -> name'})
        self.assertEqual(dirs, {'empty'})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='pull-manifest-test-'))
        self.work = self.base / 'work'
        self.addCleanup(lambda: shutil.rmtree(self.work))
        self.src = self.work / 'sender'
        self.dst = self.work / 'receiver'
        self.src.mkdir(parents=True)
        self.dst.mkdir()
        self.args = SimpleNamespace(verbose=False, dry_run=False, bwlimit=None)
        self.real_run = subprocess.run
        self.transfers = []
        self.scans = 0

    def write(self, root, rel, content='payload'):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def adapter(self, cmd, **kw):
        if cmd[0] == 'ssh':
            self.scans += 1
            return self.real_run([sys.executable, '-c', ss.REMOTE_PY_MANIFEST,
                                  str(self.src)], **kw)
        self.transfers.append((list(cmd), kw.get('input')))
        cmd = [str(x).removeprefix('hub:') for x in cmd]
        return self.real_run(cmd, **kw)

    def pull(self, gid):
        with patch.object(ss.subprocess, 'run', side_effect=self.adapter), \
             patch.object(ss, '_watch_ssh_e', return_value=[]), \
             patch.object(ss, 'apply_gc_deletions') as gc_apply:
            result = ss.watch_rsync('cfg', 'hub', str(self.dst), str(self.src),
                                    self.args, 'pull', gid)
            self.gc_applied = gc_apply.call_count
            return result


class FailureTests(Fixture):
    def test_failed_local_scan_never_contacts_sender(self):
        with patch.object(ss, 'unmarked_files', return_value=([], False)):
            self.assertEqual(self.pull(12345), (False, 0))
        self.assertEqual((self.scans, len(self.transfers)), (0, 0))

    def test_gc_read_failure_aborts_before_remote_contact(self):
        with patch.object(ss, 'gc_exclude_and_targets', side_effect=PermissionError('GC unreadable')):
            self.assertEqual(self.pull(12345), (False, 0))
        self.assertEqual(self.scans, 0)
        (self.dst / 'gc').mkdir()
        with patch.object(ss.os, 'listdir', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                ss.read_gc_lists(str(self.dst), strict=True)
        self.write(self.dst, 'gc/delete-list.000001', 'task/dead/')
        with patch('builtins.open', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                ss.read_gc_lists(str(self.dst), strict=True)

    def test_failed_or_malformed_remote_scan_never_transfers(self):
        results = [subprocess.CompletedProcess([], 1, b'partial\0', b'failure'),
                   subprocess.CompletedProcess([], 0, b'unterminated', b''),
                   subprocess.CompletedProcess([], 0, b'../escape\0', b''),
                   subprocess.CompletedProcess([], 0, b'a//\0', b''),
                   subprocess.TimeoutExpired('ssh', 600)]
        for result in results:
            with self.subTest(result=result), \
                 patch.object(ss.subprocess, 'run', side_effect=[result]) as run:
                self.assertEqual(ss.watch_rsync('cfg', 'hub', str(self.dst),
                                                str(self.src), self.args,
                                                'pull', 12345), (False, 0))
                self.assertEqual(run.call_count, 1)

    def test_scan_missing_subtree_is_nonzero_not_partial_success(self):
        # Actual remote script: fail on scandir even after producing a path.
        code = ('import os\nreal=os.scandir\n'
                'def broken(p):\n'
                '    if p.endswith(b"broken"): raise PermissionError("denied")\n'
                '    return real(p)\n'
                'os.scandir=broken\n' + ss.REMOTE_PY_MANIFEST)
        (self.src / 'broken').mkdir()
        r = self.real_run([sys.executable, '-c', code, str(self.src)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b'broken/\0', r.stdout)

    def test_walk_protects_unmarked_symlinks_and_specials(self):
        self.write(self.dst, 'original')
        os.symlink('outside', self.dst / 'link')
        os.mkfifo(self.dst / 'fifo')
        originals, ok = ss.unmarked_files(str(self.dst), -1,
                                          protect_nonregular=True)
        self.assertTrue(ok)
        self.assertEqual(set(originals), {'original', 'link', 'fifo'})


@unittest.skipUnless(os.name == 'posix', 'rsync fixtures require POSIX')
class RealRsyncTests(Fixture):
    def setUp(self):
        super().setUp()
        try:
            self.gid = grp.getgrnam(ss.REPLICA_GROUP).gr_gid
        except KeyError:
            self.skipTest('replica group unavailable')
        if self.gid == os.getgid():
            self.skipTest('need a different primary group for originals')
        p = self.write(self.dst, 'probe')
        try:
            os.chown(p, -1, self.gid)
        except OSError:
            self.skipTest('cannot chgrp to replica')

    def test_raw_rsync_files_from_is_literal_and_nonrecursive(self):
        # Establish rsync's wire semantics independently of our validator.
        names = ['[ab].msg', 'a*b.msg', 'a?b.msg', 'a]b.msg']
        for rel in names + ['a.msg', 'axxb.msg', 'axb.msg']:
            self.write(self.src, rel, rel)
        self.write(self.src, 'dir[*?]/omitted', 'NOT_LISTED')
        r = self.real_run(
            [ss.RSYNC_BIN, ss.WATCH_RSYNC_FLAGS, '--files-from=-', '--from0',
             '--no-recursive', '--dirs', '--8-bit-output',
             '--out-format=SSH_SYNC %n', f'--chown=:{ss.REPLICA_GROUP}',
             str(self.src) + '/', str(self.dst) + '/'],
            input='\0'.join(names + ['dir[*?]']) + '\0',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(set(p.name for p in self.dst.iterdir()),
                         set(names + ['dir[*?]', 'probe']))
        for rel in names:
            self.assertEqual((self.dst / rel).read_text(), rel)
        self.assertEqual(list((self.dst / 'dir[*?]').iterdir()), [])
        self.assertEqual(ss._rsync_transferred(r.stdout, prefixed=True)[0], set(names))

    def test_literal_glob_originals_and_received_files_are_exact_and_marked(self):
        pairs = [('[ab].msg', 'a.msg'), ('a*b.msg', 'axxb.msg'),
                 ('a?b.msg', 'axb.msg'), ('a]b.msg', 'ab.msg')]
        originals, received = {}, {}
        for i, (literal, lookalike) in enumerate(pairs):
            for branch, original, incoming in [('protect', literal, lookalike),
                                               ('receive', lookalike, literal)]:
                prefix = f'{branch}/{i}/'
                p = self.write(self.dst, prefix + original, 'LOCAL')
                originals[prefix + original] = p.stat()
                self.write(self.src, prefix + original, 'DO_NOT_OVERWRITE')
                rel = prefix + incoming
                received[rel] = 'REMOTE ' + rel
                self.write(self.src, rel, received[rel])
        self.write(self.dst, 'collision[*?]', 'LOCAL_FILE')
        self.write(self.src, 'collision[*?]/hidden', 'MUST_NOT_RECURSE')
        self.write(self.src, 'collisionX/allowed', 'NEIGHBOR')
        received['collisionX/allowed'] = 'NEIGHBOR'
        # Also exercise the fix-up parser/chgrp on a build ignoring --chown.
        with patch.object(ss, 'REPLICA_GROUP', grp.getgrgid(os.getgid()).gr_name):
            ok, moved = self.pull(self.gid)
        self.assertTrue(ok)
        self.assertEqual(moved, len(received))
        selected = set(self.transfers[-1][1].split('\0'))
        for rel, before in originals.items():
            p = self.dst / rel
            self.assertEqual(p.read_text(), 'LOCAL', rel)
            self.assertEqual((p.stat().st_ino, p.stat().st_gid),
                             (before.st_ino, before.st_gid), rel)
            self.assertNotIn(rel, selected)
        for rel, content in received.items():
            self.assertEqual((self.dst / rel).read_text(), content, rel)
            self.assertEqual((self.dst / rel).stat().st_gid, self.gid, rel)
            self.assertIn(rel, selected)
        self.assertEqual((self.dst / 'collision[*?]').read_text(), 'LOCAL_FILE')
        self.assertNotIn('collision[*?]/hidden', selected)

    def test_unlisted_glob_directory_children_never_expand(self):
        manifest = []
        for name in ('parent[ab]', 'parent*', 'parent?'):
            self.write(self.src, name + '/omitted', 'MUST_NOT_ARRIVE')
            self.write(self.src, name + '/allowed', 'OK')
            manifest += [name, name + '/allowed']
        self.write(self.src, 'parentX/unlisted', 'MUST_NOT_MATCH')
        with patch.object(ss, 'remote_manifest', return_value=manifest):
            self.assertTrue(self.pull(self.gid)[0])
        for name in ('parent[ab]', 'parent*', 'parent?'):
            self.assertFalse((self.dst / name / 'omitted').exists())
            self.assertEqual((self.dst / name / 'allowed').read_text(), 'OK')
            self.assertEqual((self.dst / name / 'allowed').stat().st_gid, self.gid)
        self.assertFalse((self.dst / 'parentX').exists())

    def test_originals_new_entries_gc_excludes_and_marks(self):
        self.write(self.dst, 'task/a/original', 'MINE')
        self.write(self.src, 'task/a/original', 'HUB!')
        self.write(self.dst, 'collision', 'LOCAL_FILE')
        self.write(self.src, 'collision/child', 'REMOTE_SUBTREE')
        self.write(self.dst, 'link-target/keep', 'LOCAL_TARGET')
        os.symlink('link-target', self.dst / 'local-link')
        self.write(self.src, 'local-link/keep', 'REMOTE_TARGET')
        os.mkfifo(self.dst / 'local-fifo')
        self.write(self.src, 'local-fifo', 'DO_NOT_REPLACE')
        self.write(self.src, 'task/a/fresh', 'NEW')
        (self.src / 'task/a/empty').mkdir()
        self.write(self.dst, 'gc/delete-list.000001', 'task/dead/\ntask/dead-file\n')
        self.write(self.src, 'task/dead/x')
        self.write(self.src, 'task/dead-file/child')
        self.write(self.src, 'nested/no.pending/hidden')
        self.write(self.src, 'nested/a.pending')
        self.write(self.src, 'nested/a.pending.msg')
        self.write(self.src, 'task/a/ack/final')
        self.write(self.src, 'sent filename', 'PREFIXED_OUTPUT')
        self.write(self.src, '远端/结果', 'UTF8')
        os.symlink('task/a/fresh', self.src / 'new-link')
        os.mkfifo(self.src / 'new-fifo')
        # Force the post-pull fix-up path, as on builds ignoring --chown.
        group_without_mark = grp.getgrgid(os.getgid()).gr_name
        with patch.object(ss, 'WATCH_EXCLUDES', ['*.pending']), \
             patch.object(ss, 'REPLICA_GROUP', group_without_mark):
            ok, _ = self.pull(self.gid)
        self.assertTrue(ok)
        self.assertEqual((self.dst / 'task/a/original').read_text(), 'MINE')
        self.assertEqual((self.dst / 'collision').read_text(), 'LOCAL_FILE')
        self.assertTrue((self.dst / 'local-link').is_symlink())
        self.assertEqual((self.dst / 'link-target/keep').read_text(), 'LOCAL_TARGET')
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.dst / 'local-fifo').st_mode))
        self.assertTrue((self.dst / 'task/a/empty').is_dir())
        self.assertEqual((self.dst / 'task/a/fresh').read_text(), 'NEW')
        for rel in ('task/dead', 'task/dead-file', 'nested/no.pending', 'nested/a.pending'):
            self.assertFalse((self.dst / rel).exists(), rel)
        for rel in ('task/a/fresh', 'nested/a.pending.msg', 'task/a/ack/final',
                    'new-link', 'new-fifo', 'sent filename', '远端/结果'):
            self.assertEqual(os.lstat(self.dst / rel).st_gid, self.gid, rel)
        self.assertEqual(self.gc_applied, 1)
        self.assertFalse(any(x.endswith('/') for x in self.transfers[-1][1].split('\0') if x))

    def test_sender_symlink_is_not_walked_or_chowned_through(self):
        outside = self.write(self.work, 'outside/original', 'OUTSIDE')
        os.symlink('../outside', self.src / 'link-to-outside')
        before = outside.stat()
        self.assertTrue(self.pull(self.gid)[0])
        self.assertTrue((self.dst / 'link-to-outside').is_symlink())
        self.assertNotIn('link-to-outside/original', self.transfers[-1][1])
        self.assertEqual(outside.stat().st_gid, before.st_gid)
        self.assertEqual(outside.read_text(), 'OUTSIDE')

    def test_unlisted_directory_children_never_expand(self):
        self.write(self.src, 'parent/omitted', 'MUST_NOT_ARRIVE')
        self.write(self.src, 'parent/allowed', 'OK')
        with patch.object(ss, 'remote_manifest', return_value=['parent', 'parent/allowed']):
            self.assertTrue(self.pull(self.gid)[0])
        self.assertFalse((self.dst / 'parent/omitted').exists())
        self.assertEqual((self.dst / 'parent/allowed').read_text(), 'OK')

    def test_checksum_not_mtime_and_fresh_scan_each_cycle(self):
        src = self.write(self.src, 'replica', 'AAAA')
        dst = self.write(self.dst, 'replica', 'BBBB')
        os.chown(dst, -1, self.gid)
        os.utime(dst, ns=(src.stat().st_atime_ns, src.stat().st_mtime_ns))
        self.assertTrue(self.pull(self.gid)[0])
        self.assertEqual(dst.read_text(), 'AAAA')
        before = dst.stat()
        self.write(self.src, 'created-later/empty/file', 'LATER')
        self.assertTrue(self.pull(self.gid)[0])
        after = dst.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))
        self.assertEqual((self.dst / 'created-later/empty/file').read_text(), 'LATER')
        self.assertEqual(self.scans, 2)

    def test_vanished_sender_fails_cycle_then_full_retry_converges(self):
        with patch.object(ss, 'remote_manifest', return_value=['vanished']):
            self.assertFalse(self.pull(self.gid)[0])
        self.assertEqual(self.gc_applied, 0)
        self.write(self.src, 'new-after-failure', 'RECOVERED')
        self.assertTrue(self.pull(self.gid)[0])
        self.assertEqual((self.dst / 'new-after-failure').read_text(), 'RECOVERED')

    def test_sender_non_directory_cannot_remove_original_descendants(self):
        self.write(self.dst, 'parent/original', 'LOCAL')
        os.symlink('elsewhere', self.src / 'parent')
        self.assertFalse(self.pull(self.gid)[0])
        self.assertEqual((self.dst / 'parent/original').read_text(), 'LOCAL')

    def test_dry_run_never_applies_gc_or_creates_replicas(self):
        self.write(self.src, 'fresh', 'REMOTE')
        self.args.dry_run = True
        self.assertTrue(self.pull(self.gid)[0])
        self.assertFalse((self.dst / 'fresh').exists())
        self.assertEqual(self.gc_applied, 0)

    def test_empty_manifest_still_reaches_gc_and_no_tree_recursion(self):
        self.assertTrue(self.pull(self.gid)[0])
        self.assertEqual(self.gc_applied, 1)
        self.assertEqual(self.transfers[-1][1], '')
        self.assertEqual(list(self.dst.iterdir()), [self.dst / 'probe'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
