"""Pinned upstream pure selection + synthetic foreground manifest transports.

No actual SSH/rsync/GC, no agentd dependency, no named-fixture deletion.
Set Y_MANIFEST_OUTPUT to a new absolute directory outside protocol trees.
"""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parent

def load():
    spec=importlib.util.spec_from_file_location('combo_sync',HERE/'ssh-sync.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

ss=load()

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


HELPER=r'''
import json,os,signal,sys,time
from pathlib import Path
role,mode,area,payload,seq=sys.argv[1:]
area=Path(area)
if mode in ('block','timeout'):
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    (area/'ready').write_text('ready')
    while True:time.sleep(.01)
if role=='manifest':
    sys.stdout.buffer.write(Path(payload).read_bytes())
else:
    (area/('stdin-'+seq+'.bin')).write_bytes(sys.stdin.buffer.read())
if mode=='exit7':sys.exit(7)
'''


class ManifestTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=Path(os.environ['Y_MANIFEST_OUTPUT'])
        if not cls.base.is_absolute():raise ValueError('new absolute output root required')
        cls.base.mkdir(mode=0o700,exist_ok=False)

    def setUp(self):
        self.area=self.base/self._testMethodName;self.area.mkdir(mode=0o700)
        self.y=load();self.stop=threading.Event()
        self.manager=self.y.ForegroundTransport(self.stop);self.y._FOREGROUND=self.manager
        self.events=[];self.owned=[];self.threads=[];self.calls=[];self.mode='success';self.block_role='manifest'
        self.payload=self.area/'manifest.bin';self.payload.write_bytes(b'tree/\0tree/original\0tree/fresh\0empty/\0')
        self.args=SimpleNamespace(verbose=False,dry_run=False,bwlimit=None)
        self.raw_transport=self.y._transport_run;self.real_popen=subprocess.Popen
        def guard(argv,**kw):
            self.assertEqual(argv[0],sys.executable,'no actual external transport allowed')
            p=self.real_popen(argv,**kw);self.owned.append(p)
            text=(Path('/proc')/str(p.pid)/'stat').read_text().rsplit(')',1)[1].split()
            self.assertEqual(int(text[2]),os.getpgrp());self.assertEqual(int(text[3]),os.getsid(0))
            uid=(Path('/proc')/str(p.pid)).stat().st_uid;self.assertEqual(uid,os.geteuid())
            self.events.append({'event':'spawn','pid':p.pid,'procStart':int(text[19]),'pgid':int(text[2]),'session':int(text[3]),'uid':uid})
            return p
        self.guard=patch.object(self.y.subprocess,'Popen',side_effect=guard);self.guard.start()
        self.routing=patch.object(self.y,'_transport_run',side_effect=self.mapped);self.routing.start()
        self.gc=patch.object(self.y,'apply_gc_deletions');self.gc_mock=self.gc.start()
        self.scan=patch.object(self.y,'unmarked_files',return_value=(['tree/original'],True));self.scan_mock=self.scan.start()
        self.lists=patch.object(self.y,'gc_exclude_and_targets',return_value=([],[],[]));self.lists_mock=self.lists.start()

    def mapped(self,argv,**kw):
        role='manifest' if argv[0]=='ssh' else 'transfer'
        if role=='manifest':
            self.assertIn('ControlMaster=no',argv);self.assertIn('ControlPersist=no',argv)
            self.assertIn('ForkAfterAuthentication=no',argv)
        else:
            self.assertEqual(argv[0],self.y.RSYNC_BIN)
            self.assertIn('ControlMaster=no',' '.join(argv));self.assertNotIn('--trust-sender',argv)
        self.assertEqual(kw['timeout'],600)
        seq=str(len(self.calls));effective=dict(kw)
        mode=self.mode if role==self.block_role else 'success'
        if mode=='timeout':effective['timeout']=.2
        self.calls.append({'role':role,'argv':argv,'timeoutRequested':kw['timeout'],'timeoutEffective':effective['timeout'],
                           'input':kw.get('input'),'stdoutBinary':not kw.get('text',False)})
        if self.mode=='actual-scan' and role=='manifest':
            actual=[sys.executable,'-B','-c',self.y.REMOTE_PY_MANIFEST,str(self.sender)]
        else:actual=[sys.executable,'-B','-c',HELPER,role,mode,str(self.area),str(self.payload),seq]
        return self.raw_transport(actual,**effective)

    def pull(self):
        return self.y.watch_rsync('not-read','fixture',str(self.area/'local'),'/not-read',self.args,'pull',-1)

    def stop_when_ready(self):
        seen=[]
        def stopper():
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if (self.area/'ready').exists():seen.append(True);break
                time.sleep(.005)
            self.stop.set()  # Always unblock, including fixture startup error.
        t=threading.Thread(target=stopper);self.threads.append(t);t.start()
        try:
            with self.assertRaises(self.y.TransportStopped):self.pull()
        finally:t.join(4)
        self.assertFalse(t.is_alive());self.assertTrue(seen)
        self.assertEqual(self.gc_mock.call_count,0)
        self.assertIn(-9,[p.returncode for p in self.owned])

    def tearDown(self):
        self.stop.set()
        for t in self.threads:t.join(4)
        self.manager.close()
        for p in self.owned:
            self.y.ForegroundTransport.finish(p)
            for f in (p.stdin,p.stdout,p.stderr):
                if f is not None and not f.closed:f.close()
        for obj in (self.lists,self.scan,self.gc,self.routing,self.guard):obj.stop()
        self.assertFalse(self.manager.children)
        self.assertTrue(all(p.returncode is not None for p in self.owned))
        self.events.append({'event':'final-waits','waits':[{'pid':p.pid,'rc':p.returncode} for p in self.owned],
                            'remaining':[],'registered':len(self.manager.children)})
        (self.area/'evidence.json').write_text(json.dumps({'events':self.events,'calls':self.calls},indent=2)+'\n')

    def test_binary_manifest_minimal_stdin_and_success_gc_boundary(self):
        self.assertEqual(self.pull(),(True,0))
        self.assertEqual([c['role'] for c in self.calls],['manifest','transfer'])
        self.assertTrue(self.calls[0]['stdoutBinary']);self.assertFalse(self.calls[1]['stdoutBinary'])
        self.assertEqual((self.area/'stdin-1.bin').read_bytes(),b'empty\0tree/fresh\0')
        self.assertEqual(self.gc_mock.call_count,1)
        self.scan_mock.assert_called_once_with(str(self.area/'local'),-1,protect_nonregular=True)
        self.lists_mock.assert_called_once_with(str(self.area/'local'),strict=True)
        for flag in ('--from0','--no-recursive','--dirs','--8-bit-output','--out-format=SSH_SYNC %n'):
            self.assertIn(flag,self.calls[1]['argv'])
        self.assertNotIn('--delete',self.calls[1]['argv'])

    def test_each_cycle_rebuilds_manifest_without_candidates(self):
        self.assertEqual(self.pull(),(True,0));self.payload.write_bytes(b'new\0')
        self.assertEqual(self.pull(),(True,0))
        self.assertEqual((self.area/'stdin-3.bin').read_bytes(),b'new\0')
        self.assertEqual(self.gc_mock.call_count,2);self.assertEqual(self.scan_mock.call_count,2)

    def test_empty_selection_still_has_one_transfer(self):
        self.payload.write_bytes(b'')
        self.assertEqual(self.pull(),(True,0));self.assertEqual((self.area/'stdin-1.bin').read_bytes(),b'')
        self.assertEqual(self.gc_mock.call_count,1)

    def test_literal_globs_and_utf8_are_literal_bytes(self):
        self.payload.write_bytes('literal/\0literal/[heartbeat run].msg\0literal/结果*?.txt\0'.encode())
        self.assertEqual(self.pull(),(True,0))
        self.assertEqual((self.area/'stdin-1.bin').read_bytes(),'literal/[heartbeat run].msg\0literal/结果*?.txt\0'.encode())
        for path in ('task/a[0]', 'task/a*b', 'task/a?b', 'task/a\nb'):
            for forced in (False,True):
                self.assertIsNotNone(self.y.validate_delete_path(path,force=forced))
                self.assertIsNotNone(self.y._gc_shape_refused(path,True,forced))

    def test_large_stdin_not_truncated(self):
        values=[f'new/{i:06d}/结果' for i in range(40000)]
        self.payload.write_bytes(('\0'.join(values)+'\0').encode())
        self.assertEqual(self.pull(),(True,0))
        self.assertEqual((self.area/'stdin-1.bin').read_bytes(),self.payload.read_bytes())

    def test_failed_scan_never_contacts_transport(self):
        self.scan_mock.return_value=([],False)
        self.assertEqual(self.pull(),(False,0));self.assertEqual(self.calls,[]);self.gc_mock.assert_not_called()

    def test_unreadable_gc_aborts_before_local_or_remote_scan(self):
        self.lists_mock.side_effect=PermissionError('synthetic unreadable GC')
        self.assertEqual(self.pull(),(False,0));self.scan_mock.assert_not_called();self.assertEqual(self.calls,[])

    def test_manifest_exit7_does_not_transfer_or_gc(self):
        self.mode='exit7'
        self.assertEqual(self.pull(),(False,0));self.assertEqual(len(self.calls),1)
        self.assertEqual(self.owned[0].returncode,7);self.gc_mock.assert_not_called()

    def test_malformed_manifest_does_not_transfer_or_gc(self):
        for value in (b'unterminated',b'../escape\0',b'bad//\0',b'bad\xff\0'):
            self.payload.write_bytes(value)
            self.assertEqual(self.pull(),(False,0))
        self.assertEqual([c['role'] for c in self.calls],['manifest']*4);self.gc_mock.assert_not_called()

    def test_actual_scanner_no_follow_and_empty_directory(self):
        self.mode='actual-scan';self.sender=self.area/'sender';self.sender.mkdir()
        (self.sender/'empty').mkdir();(self.sender/'literal[1]').write_text('fixture')
        other=self.area/'outside';other.mkdir();(other/'not-walked').write_text('keep')
        (self.sender/'link').symlink_to(other,target_is_directory=True)
        self.assertEqual(set(self.y.remote_manifest('not-read','fixture',str(self.sender))),{'empty','literal[1]','link'})
        self.assertEqual((other/'not-walked').read_text(),'keep')

    def test_manifest_timeout_waits_and_skips_transfer(self):
        self.mode='timeout'
        self.assertEqual(self.pull(),(False,0));self.assertEqual(len(self.calls),1)
        self.assertEqual(self.owned[0].returncode,-9);self.gc_mock.assert_not_called()

    def test_transfer_timeout_and_exit7_skip_gc(self):
        self.block_role='transfer';self.mode='timeout'
        self.assertEqual(self.pull(),(False,0));self.assertEqual(self.owned[-1].returncode,-9)
        self.mode='exit7';self.assertEqual(self.pull(),(False,0));self.assertEqual(self.owned[-1].returncode,7)
        self.gc_mock.assert_not_called()

    def test_stop_during_manifest_waits_and_never_transfers(self):
        self.mode='block';self.stop_when_ready()
        self.assertEqual([c['role'] for c in self.calls],['manifest'])

    def test_stop_during_transfer_waits_and_never_applies_gc(self):
        self.mode='block';self.block_role='transfer';self.stop_when_ready()
        self.assertEqual([c['role'] for c in self.calls],['manifest','transfer'])

    def test_default_manifest_retains_default_ssh_policy(self):
        self.y._FOREGROUND=None;self.routing.stop()
        with patch.object(self.y.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'normal\0',b'')) as run:
            self.assertEqual(self.y.remote_manifest('not-read','fixture','/not-read'),['normal'])
        self.routing.start()
        self.assertIn('ControlMaster=auto',run.call_args.args[0]);self.assertEqual(run.call_args.kwargs['timeout'],600)
        self.assertEqual(self.owned,[])

    def test_dry_run_never_applies_gc(self):
        self.args.dry_run=True
        self.assertEqual(self.pull(),(True,0));self.assertIn('--dry-run',self.calls[1]['argv']);self.gc_mock.assert_not_called()

    def test_push_routes_ssh_and_literal_stdin_without_gc(self):
        self.scan_mock.return_value=(['literal/a*b','literal/[x]'],True)
        with patch.object(self.y,'_transport_run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            self.assertEqual(self.y.watch_rsync('not-read','fixture',str(self.area/'local'),'/not-read',self.args,'push',-1),(True,0))
        self.assertEqual(run.call_count,2)
        self.assertIn('ControlMaster=no',run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[1].kwargs['input'],'literal/[x]\0literal/a*b\0')
        self.assertEqual(run.call_args_list[1].kwargs['timeout'],600)
        self.gc_mock.assert_not_called();self.assertEqual(self.owned,[])

    def test_stop_admission_spawns_nothing(self):
        self.stop.set()
        with self.assertRaises(self.y.TransportStopped):self.pull()
        self.assertEqual(self.owned,[]);self.gc_mock.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
