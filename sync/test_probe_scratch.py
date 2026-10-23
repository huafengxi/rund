"""Isolated probe tests. Caller must authorize own scratch cleanup in a new root.

Remote program runs locally as Python; transfer is a copy stub, never SSH/rsync.
Only Probe's known nonce files/empty directories are deleted; other fixtures stay.
"""
import grp
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parent
BASE=Path(os.environ['Y_PROBE_OUTPUT'])
if not BASE.is_absolute():raise ValueError('new absolute Y_PROBE_OUTPUT required')
BASE.mkdir(mode=0o700,parents=True,exist_ok=False)
for name in ('home','tmp','cwd'):(BASE/name).mkdir(mode=0o700)
ENV={'HOME':str(BASE/'home'),'TMPDIR':str(BASE/'tmp'),'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin',
     'PYTHONPATH':str(REPO),'PYTHONDONTWRITEBYTECODE':'1','LC_ALL':'C.UTF-8'}
os.environ.clear();os.environ.update(ENV);os.chdir(BASE/'cwd');sys.path.insert(0,str(REPO))
import probe_scratch as ps


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.area=BASE/self._testMethodName;self.area.mkdir(mode=0o700)
        self.local=self.area/'local';self.remote=self.area/'remote'
        for parent in (self.local,self.remote):
            parent.mkdir(mode=0o700)
            for name in ('scratch','data'):(parent/name).mkdir(mode=0o700)
            (parent/'data/original').write_bytes(b'untouched original')
        self.events=[];self.requests=[];self.mode='success'
        spec=importlib.util.spec_from_file_location('probe_watch_'+self._testMethodName,REPO/'ssh-sync.py')
        self.y=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.y)
        self.y._FOREGROUND=object();self.y._STOP=threading.Event()
        self.y.REPLICA_GROUP=grp.getgrgid(os.getegid()).gr_name
        self.y._PROBE_SETTINGS={'localRoot':str(self.local/'scratch'),'localData':str(self.local/'data'),
                               'remoteRoot':str(self.remote/'scratch'),'remoteData':str(self.remote/'data')}
        self.y.run=self.fake_run

    def execute_remote(self,req):
        for key in ('root','data'):
            self.assertEqual(os.path.commonpath([str(self.area),req[key]]),str(self.area),'remote test escaped authorized case')
        argv=shlex.split(ps.remote_command(req));self.assertEqual(argv[0],'python3')
        actual=[sys.executable,'-B']+argv[1:]
        p=subprocess.Popen(actual,cwd=self.area,env=ENV,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:out,err=p.communicate(timeout=5)
        except BaseException:
            p.kill();p.wait();raise
        self.events.append({'event':'remote-python','argv':actual,'env':ENV,'cwd':str(self.area),'pid':p.pid,
                            'rc':p.returncode,'stdout':out,'stderr':err,'request':req})
        return subprocess.CompletedProcess(actual,p.returncode,out,err)

    def fake_run(self,cmd,verbose=False,check=True,capture=False):
        if cmd[0]=='ssh':
            parts=shlex.split(cmd[-1]);self.assertEqual(parts[0],'python3')
            req=json.loads(parts[-1]);self.requests.append(dict(req))
            if self.mode=='stop-cleanup' and req['action']=='cleanup':
                self.y._STOP.set();raise self.y.TransportStopped()
            r=self.execute_remote(req)
            if self.mode=='forged-create' and req['action']=='create' and r.returncode==0:
                reply=json.loads(r.stdout);reply['path']=str(self.area/'victim')
                return subprocess.CompletedProcess(cmd,0,json.dumps(reply),'')
            return r
        self.assertEqual(cmd[0],self.y.RSYNC_BIN,'no real tool permitted')
        self.assertIn('--protect-args',cmd)
        src=Path(cmd[-2]);dst=Path(cmd[-1].partition(':')[2])/'probe'
        self.assertEqual(os.path.commonpath([str(self.local/'scratch'),str(src)]),str(self.local/'scratch'))
        self.assertEqual(os.path.commonpath([str(self.remote/'scratch'),str(dst)]),str(self.remote/'scratch'))
        if self.mode=='transfer-failure':return subprocess.CompletedProcess(cmd,23,'synthetic transfer failure','')
        shutil.copyfile(src,dst)
        os.chown(dst,-1,os.getegid())
        if self.mode=='bad-content':dst.write_bytes(b'x'*32)
        self.events.append({'event':'copy-stub-not-rsync','argv':cmd,'source':str(src),'destination':str(dst)})
        return subprocess.CompletedProcess(cmd,0,'synthetic transfer','')

    def tearDown(self):
        # No harness directory cleanup, including failed/unknown probe remains.
        for parent in (self.local,self.remote):
            self.assertEqual((parent/'data/original').read_bytes(),b'untouched original')
        self.events.append({'event':'retained-fixtures','local':list(map(str,(self.local/'scratch').iterdir())),
                            'remote':list(map(str,(self.remote/'scratch').iterdir()))})
        (self.area/'evidence.json').write_text(json.dumps(self.events,indent=2)+'\n')

    def test_anonymous_membership_no_named_file_or_fd_leak(self):
        root=self.local/'data';before=set(root.iterdir());fds=set(os.listdir('/proc/self/fd'))
        self.assertTrue(ps.anonymous_membership(root,os.getegid()))
        self.assertEqual(set(root.iterdir()),before);self.assertEqual(set(os.listdir('/proc/self/fd')),fds)
        self.assertTrue(self.y.check_replica_membership(str(root),os.getegid()))
        self.assertFalse((root/'.ssh-sync-chown-probe').exists())

    def test_anonymous_unavailable_does_not_fallback(self):
        with patch.object(ps.os,'open',side_effect=OSError('synthetic O_TMPFILE unsupported')):
            self.assertFalse(self.y.check_replica_membership(str(self.local/'data'),os.getegid()))
        self.assertFalse((self.local/'data/.ssh-sync-chown-probe').exists())

    def test_darwin_membership_uses_private_scratch_and_access(self):
        data=self.local/'data';scratch=self.local/'scratch';before=set(data.iterdir())
        with patch.object(ps.sys,'platform','darwin'):
            self.assertTrue(ps.membership(data,os.getegid(),scratch))
            with patch.object(ps.os,'access',return_value=False):
                with self.assertRaisesRegex(ps.ProbeError,'effective'):
                    ps.membership(data,os.getegid(),scratch)
        self.assertEqual(set(data.iterdir()),before)
        self.assertEqual(list(scratch.iterdir()),[])

    def test_local_probe_known_content_cleanup(self):
        with ps.Probe(self.local/'scratch',self.local/'data') as probe:
            p=probe.write();self.assertEqual(p.read_bytes(),probe.req['nonce'].encode())
            path=probe.path
        self.assertFalse(path.exists());self.assertTrue((self.local/'scratch').is_dir())

    def test_unknown_entry_retained_before_any_unlink(self):
        with self.assertRaisesRegex(ps.ProbeError,'unknown scratch content'):
            with ps.Probe(self.local/'scratch',self.local/'data') as probe:
                p=probe.write();(probe.path/'keep').write_text('unknown')
        self.assertTrue(p.exists());self.assertEqual((probe.path/'keep').read_text(),'unknown')

    def test_symlink_and_hardlink_not_deleted(self):
        victim=self.area/'victim';victim.write_text('v'*32)
        for link in ('symlink','hardlink'):
            with self.subTest(link=link),self.assertRaises(ps.ProbeError):
                with ps.Probe(self.local/'scratch',self.local/'data') as probe:
                    p=probe.path/'probe'
                    if link=='symlink':p.symlink_to(victim)
                    else:os.link(victim,p)
            self.assertTrue(p.is_symlink() or p.exists());self.assertEqual(victim.read_text(),'v'*32)

    def test_unknown_content_retained(self):
        with self.assertRaisesRegex(ps.ProbeError,'unknown probe content'):
            with ps.Probe(self.local/'scratch',self.local/'data') as probe:
                p=probe.write();p.write_bytes(b'x'*32)
        self.assertTrue(p.exists())

    def test_no_adoption_of_existing_nonce_directory(self):
        nonce='a'*32;existing=self.local/'scratch'/('ssh-sync-'+nonce);existing.mkdir(mode=0o700)
        (existing/'keep').write_text('keep')
        with self.assertRaises(FileExistsError):
            with ps.Probe(self.local/'scratch',self.local/'data',nonce=nonce):pass
        self.assertEqual((existing/'keep').read_text(),'keep')

    def test_roots_disjoint_private_real_and_same_filesystem(self):
        root=self.local/'scratch';data=self.local/'data'
        root.chmod(0o755)
        with self.assertRaises(ps.ProbeError):ps.validate_roots(root,data)
        root.chmod(0o700)
        with self.assertRaises(ps.ProbeError):ps.validate_roots(data,data)
        inside=data/'scratch';inside.mkdir(mode=0o700)
        with self.assertRaises(ps.ProbeError):ps.validate_roots(inside,data)
        alias=self.local/'alias';alias.symlink_to(root,target_is_directory=True)
        with self.assertRaises(ps.ProbeError):ps.validate_roots(alias,data)
        with self.assertRaises(ps.ProbeError):ps.validate_roots(self.local,data)
        original=ps.Path.stat
        def wrong_owner(path,*a,**kw):
            st=original(path,*a,**kw)
            if path==root:
                values=list(st);values[4]+=12345;return os.stat_result(values)
            return st
        with patch.object(ps.Path,'stat',new=wrong_owner):
            with self.assertRaisesRegex(ps.ProbeError,'owned'):ps.validate_roots(root,data)
        def different(path,*a,**kw):
            st=original(path,*a,**kw)
            if path==data:
                values=list(st);values[2]+=12345;return os.stat_result(values)
            return st
        with patch.object(ps.Path,'stat',new=different):
            with self.assertRaisesRegex(ps.ProbeError,'same filesystem'):ps.validate_roots(root,data)

    def test_request_nonce_or_unknown_action_rejected(self):
        req={'action':'create','root':str(self.remote/'scratch'),'data':str(self.remote/'data'),'nonce':'../victim','group':None}
        r=self.execute_remote(req);self.assertNotEqual(r.returncode,0)
        req.update(nonce='b'*32,action='rm-rf')
        r=self.execute_remote(req);self.assertNotEqual(r.returncode,0)
        req.update(action='create',arbitrary_delete_path=str(self.area/'victim'))
        r=self.execute_remote(req);self.assertNotEqual(r.returncode,0)
        self.assertEqual(list((self.remote/'scratch').iterdir()),[])

    def test_pipeline_success_and_both_scratch_roots_retained(self):
        with patch.object(self.y.tempfile,'mkdtemp',side_effect=AssertionError('legacy probe')),patch.object(self.y.shutil,'rmtree',side_effect=AssertionError('no recursive removal')):
            self.assertIsNone(self.y.probe_rsync_chown_pipeline('/not-read','fixture'))
        self.assertEqual([r['action'] for r in self.requests],['create','inspect','cleanup'])
        for parent in (self.local,self.remote):self.assertEqual(list((parent/'scratch').iterdir()),[])

    def test_wrong_group_is_failure_not_green(self):
        self.y.REPLICA_GROUP='nonexistent-test-replica-group'
        why=self.y.probe_rsync_chown_pipeline('/not-read','fixture')
        self.assertIn('landed group',why)
        self.assertEqual(list((self.remote/'scratch').iterdir()),[])

    def test_diagnostics_use_bounded_program(self):
        self.assertEqual(self.y._diagnose_remote_group('/not-read','fixture'),[])
        self.assertEqual([r['action'] for r in self.requests],['create','group','cleanup'])
        self.y.REPLICA_GROUP='nonexistent-test-replica-group'
        self.assertTrue(self.y._diagnose_remote_group('/not-read','fixture'))
        self.assertEqual(list((self.remote/'scratch').iterdir()),[])

    def test_failed_transfer_keeps_error_and_cleans_known_empty_remote(self):
        self.mode='transfer-failure'
        why=self.y.probe_rsync_chown_pipeline('/not-read','fixture');self.assertIn('exit 23',why)
        for parent in (self.local,self.remote):self.assertEqual(list((parent/'scratch').iterdir()),[])

    def test_bad_transferred_content_retained(self):
        self.mode='bad-content'
        self.assertIsNotNone(self.y.probe_rsync_chown_pipeline('/not-read','fixture'))
        children=list((self.remote/'scratch').iterdir());self.assertEqual(len(children),1)
        self.assertEqual((children[0]/'probe').read_bytes(),b'x'*32)

    def test_forged_stdout_path_never_becomes_cleanup_authority(self):
        victim=self.area/'victim';victim.write_text('keep');self.mode='forged-create'
        self.assertIsNotNone(self.y.probe_rsync_chown_pipeline('/not-read','fixture'))
        self.assertEqual([r['action'] for r in self.requests],['create'])
        self.assertEqual(victim.read_text(),'keep')
        self.assertEqual(len(list((self.remote/'scratch').iterdir())),1)

    def test_stop_during_cleanup_is_not_replayed(self):
        self.mode='stop-cleanup'
        with self.assertRaises(self.y.TransportStopped):self.y.probe_rsync_chown_pipeline('/not-read','fixture')
        self.assertEqual([r['action'] for r in self.requests],['create','inspect','cleanup'])
        self.assertEqual(len(list((self.remote/'scratch').iterdir())),1)
        self.assertEqual(list((self.local/'scratch').iterdir()),[])

    def test_cli_requires_explicit_absolute_roots_before_transport(self):
        cases=[['--foreground'],['--foreground','--probe-root',str(self.local/'scratch')],
               ['--foreground','--probe-root','relative','--remote-probe-root',str(self.remote/'scratch')],
               ['--probe-root',str(self.local/'scratch'),'--remote-probe-root',str(self.remote/'scratch')]]
        for extra in cases:
            with self.subTest(extra=extra),patch.object(sys,'argv',['ssh-sync.py','watch',str(self.local/'data'),'fixture:/not-used']+extra):
                with self.assertRaises(SystemExit) as e:self.y.main()
                self.assertEqual(e.exception.code,2)
        self.assertEqual(self.requests,[])

    def test_remote_program_rejects_nonprivate_root_without_creation(self):
        root=self.remote/'scratch';root.chmod(0o755)
        req={'action':'create','root':str(root),'data':str(self.remote/'data'),'nonce':'c'*32,'group':None}
        r=self.execute_remote(req)
        self.assertNotEqual(r.returncode,0);self.assertIn('0700',r.stdout)
        self.assertEqual(list(root.iterdir()),[])

    def test_quoted_roots_survive_command_encoding_and_cleanup(self):
        self.local=self.area/"local ' quoted";self.remote=self.area/'remote ; $(not-a-command)'
        for parent in (self.local,self.remote):
            parent.mkdir(mode=0o700)
            for name in ('scratch','data'):(parent/name).mkdir(mode=0o700)
            (parent/'data/original').write_bytes(b'untouched original')
        self.y._PROBE_SETTINGS={'localRoot':str(self.local/'scratch'),'localData':str(self.local/'data'),
                               'remoteRoot':str(self.remote/'scratch'),'remoteData':str(self.remote/'data')}
        self.assertIsNone(self.y.probe_rsync_chown_pipeline('/not-read','fixture'))
        for parent in (self.local,self.remote):self.assertEqual(list((parent/'scratch').iterdir()),[])


if __name__=='__main__':unittest.main(verbosity=2)
