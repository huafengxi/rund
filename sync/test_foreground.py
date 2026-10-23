"""Retained isolated Python transport fixtures. Never run SSH/rsync/probe/GC.

Only owned direct Popen/strong identities/pidfds are used for failure cleanup.
Mocked transports prove wiring/options only, not real data or SSH capability.
"""
import ast
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parent
BASE=Path(os.environ['Y_FOREGROUND_OUTPUT']); BASE.mkdir(parents=True,exist_ok=False)
for name in ('home','tmp','cwd'): (BASE/name).mkdir()
ENV={'HOME':str(BASE/'home'),'TMPDIR':str(BASE/'tmp'),'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin',
     'PYTHONPATH':str(REPO),'PYTHONDONTWRITEBYTECODE':'1','LC_ALL':'C.UTF-8'}
os.environ.clear(); os.environ.update(ENV); os.chdir(BASE/'cwd')
sys.path.insert(0,str(REPO))
import foreground_transport as ft


def identity(pid):
    try:
        p=Path('/proc')/str(pid); t=(p/'stat').read_text().rsplit(')',1)[1].split()
        return {'pid':pid,'start':int(t[19]),'pgid':int(t[2]),'session':int(t[3]),'uid':p.stat().st_uid,'state':t[0]}
    except (FileNotFoundError,ProcessLookupError): return None


def wait(fn,seconds=3):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=fn()
        if value:return value
        time.sleep(.005)
    raise AssertionError('fixture deadline')


HELPER=r'''
import json,os,signal,sys,time
from pathlib import Path
mode,mark=sys.argv[1:]
signal.signal(signal.SIGTERM,signal.SIG_IGN if 'ignore' in mode else lambda *a:sys.exit(0))
t=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()
p=Path(mark);tmp=p.with_suffix('.tmp')
tmp.write_text(json.dumps({'pid':os.getpid(),'start':int(t[19]),'pgid':os.getpgrp(),'session':os.getsid(0),'uid':os.getuid()}));os.replace(tmp,p)
if mode=='exit':sys.exit(7)
if mode=='echo':
 time.sleep(.25);data=sys.stdin.read();sys.stdout.write(data);sys.stderr.write('tail');sys.exit(0)
if mode=='stream':print('EV fixture',flush=True);sys.exit(0)
if mode=='close-output-ignore':os.close(1)
while True:time.sleep(.01)
'''
HELPER_FILE=BASE/'ordinary helper.py';HELPER_FILE.write_text(HELPER);HELPER_FILE.chmod(0o444)
libc=ctypes.CDLL(None,use_errno=True);libc.syscall.restype=ctypes.c_long
if os.uname().machine not in ('x86_64','aarch64'):raise RuntimeError('pidfd ABI unsupported')
fd=libc.syscall(434,os.getpid(),0)
if fd<0:raise OSError(ctypes.get_errno(),'pidfd preflight')
os.close(fd)


class ForegroundTests(unittest.TestCase):
    def setUp(self):
        self.area=BASE/self._testMethodName;self.area.mkdir()
        self.saved=[];self.evidence=[];self.count=0;self.stop=threading.Event()
        spec=importlib.util.spec_from_file_location('watch_'+self._testMethodName,REPO/'ssh-sync.py')
        self.y=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.y)
        self.y._STOP=self.stop
        self.manager=ft.ForegroundTransport(self.stop)
        actual=subprocess.Popen
        def spawn(argv,**kw):
            self.assertEqual(str(argv[0]),sys.executable,'only synthetic Python helper permitted')
            p=actual(argv,**kw);self.saved.append((p,identity(p.pid),list(map(str,argv))))
            return p
        self.spawn_patch=patch.object(ft.subprocess,'Popen',side_effect=spawn);self.spawn_patch.start()
        self.signals={s:signal.getsignal(s) for s in (signal.SIGINT,signal.SIGTERM)}

    def command(self,mode='hold'):
        self.count+=1;mark=self.area/('ready-'+str(self.count))
        return [sys.executable,'-B',str(HELPER_FILE),mode,str(mark)],mark

    def start(self,mode='hold'):
        cmd,mark=self.command(mode);p=self.manager.popen(cmd,env=ENV,cwd=self.area,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        wait(mark.exists);return p,mark

    def tearDown(self):
        self.stop.set();self.manager.close()
        for p,recorded,argv in self.saved:
            actual=identity(p.pid)
            self.evidence.append({'event':'before-cleanup','recorded':recorded,'actual':actual,'argv':argv})
            if p.poll() is None:
                current=identity(p.pid)
                self.assertTrue(recorded and current and all(current[k]==recorded[k] for k in ('pid','start','pgid','session','uid')))
                fd=libc.syscall(434,p.pid,0)
                if fd<0:raise OSError(ctypes.get_errno(),'pidfd_open')
                try:
                    current=identity(p.pid);self.assertEqual(current['start'],recorded['start'])
                    if libc.syscall(424,fd,signal.SIGKILL,ctypes.c_void_p(0),0):raise OSError(ctypes.get_errno(),'pidfd_send_signal')
                finally:os.close(fd)
            rc=p.wait(timeout=3)
            for stream in (p.stdin,p.stdout,p.stderr):
                if stream and not stream.closed:stream.close()
            self.assertIsNone(identity(p.pid))
            self.evidence.append({'event':'reaped','pid':p.pid,'returncode':rc,'remaining':identity(p.pid)})
        self.spawn_patch.stop()
        for s,h in self.signals.items():signal.signal(s,h)
        (self.area/'evidence.json').write_text(json.dumps(self.evidence,indent=2)+'\n')

    def test_no_new_implicit_command_deadline(self):
        import inspect
        self.assertIsNone(inspect.signature(ft.ForegroundTransport.run).parameters['timeout'].default)

    def test_real_exit_nonzero(self):
        cmd,_=self.command('exit');r=self.manager.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=ENV,cwd=self.area)
        self.assertEqual(r.returncode,7);self.assertEqual(self.manager.children,set())

    def test_large_stdin_survives_communicate_retries(self):
        cmd,_=self.command('echo');data='x'*400000+'éλ你好'
        r=self.manager.run(cmd,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=3,env=ENV,cwd=self.area)
        self.assertEqual((r.returncode,r.stdout,r.stderr),(0,data,'tail'))
        with self.assertRaises(ValueError):
            self.manager.run(cmd,input='not sent',stdin=subprocess.DEVNULL)

    def test_timeout_kills_and_waits(self):
        cmd,mark=self.command('ignore')
        with self.assertRaises(subprocess.TimeoutExpired):
            self.manager.run(cmd,timeout=.3,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=ENV,cwd=self.area)
        self.assertTrue(mark.exists());self.assertEqual(self.saved[-1][0].returncode,-9)
        self.assertEqual(self.manager.children,set())

    def test_close_reaps_stream_and_rejects_spawn(self):
        p,mark=self.start('ignore');self.manager.close()
        self.assertEqual(p.returncode,-9)
        self.assertEqual(self.manager.children,set())
        with self.assertRaises(ft.TransportStopped):self.manager.popen(self.command()[0])
        self.assertEqual(len(self.saved),1)

    def test_close_cannot_miss_spawn_registration(self):
        argv,mark=self.command();real=ft.subprocess.Popen;spawned=threading.Event();release=threading.Event();errors=[]
        def delayed(*a,**kw):
            p=real(*a,**kw);spawned.set();release.wait(2);return p
        def create():
            try:self.manager.popen(argv,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=ENV,cwd=self.area)
            except BaseException as e:errors.append(repr(e))
        with patch.object(ft.subprocess,'Popen',side_effect=delayed):
            t=threading.Thread(target=create);t.start();self.assertTrue(spawned.wait(2))
            c=threading.Thread(target=self.manager.close);c.start();time.sleep(.03)
            self.assertTrue(c.is_alive());release.set();t.join(3);c.join(3)
        self.assertFalse(t.is_alive() or c.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.manager.children,set());self.assertIsNotNone(self.saved[-1][0].returncode)

    def test_default_and_foreground_ssh_options(self):
        y=self.y;self.assertEqual(y.ssh_options(),y.SSH_TIMEOUT_OPTS)
        self.assertIn('ControlPersist=60',y.ssh_options())
        y._FOREGROUND=self.manager
        opts=y.ssh_options()
        for option in ('ControlMaster=no','ControlPath=none','ControlPersist=no','ForkAfterAuthentication=no','PermitLocalCommand=no'):
            self.assertIn(option,opts)
        self.assertNotIn('ControlMaster=auto',opts)
        self.assertLess(opts.index('IgnoreUnknown=ForkAfterAuthentication'), opts.index('ForkAfterAuthentication=no'))
        import shlex
        e=shlex.split(y._watch_ssh_e('/not read/config with spaces')[1])
        self.assertEqual(e[:3],['ssh','-F','/not read/config with spaces'])
        self.assertEqual(e[3:],opts)

    def test_all_call_sites_use_transport_and_options(self):
        tree=ast.parse((REPO/'ssh-sync.py').read_text())
        for func in [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]:
            if func.name not in ('ssh_options',):
                self.assertFalse(any(isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load) and n.id=='SSH_TIMEOUT_OPTS' for n in ast.walk(func)),func.name)
            calls=[n for n in ast.walk(func) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and isinstance(n.func.value,ast.Name) and n.func.value.id=='subprocess' and n.func.attr in ('run','Popen')]
            if calls:self.assertIn(func.name,('_transport_run','_transport_popen'))
        ftree=ast.parse((REPO/'foreground_transport.py').read_text())
        self.assertFalse(any(isinstance(n,ast.Attribute) and n.attr in ('killpg','setsid','fork') for n in ast.walk(ftree)))

    def main_patches(self):
        import contextlib
        stack=contextlib.ExitStack()
        stack.enter_context(patch.object(sys,'argv',['ssh-sync.py','watch',str(self.area/'not-created'),'fixture:/not-used','--foreground',
                                                     '--probe-root',str(self.area/'not-created-scratch'),
                                                     '--remote-probe-root','/not-used/scratch']))
        stack.enter_context(patch.object(self.y.shutil,'which',return_value='/not-used'))
        stack.enter_context(patch.object(self.y,'ssh_config_path',return_value='/not-read/config'))
        return stack

    def test_main_signal_during_preflight_reaps_before_return(self):
        y=self.y;errors=[];sent=[];cmd,mark=self.command()
        def preflight(*unused):
            self.assertEqual(signal.getsignal(signal.SIGTERM),y._signal_handler)
            y._transport_run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=3,env=ENV,cwd=self.area)
        def send():
            try:
                wait(mark.exists)
                if signal.getsignal(signal.SIGTERM)!=y._signal_handler:raise AssertionError('handler missing')
                os.kill(os.getpid(),signal.SIGTERM);sent.append(True)
            except BaseException as e:errors.append(repr(e));self.stop.set()
        t=threading.Thread(target=send)
        with self.main_patches(),patch.object(y,'preflight',side_effect=preflight),patch.object(y,'watch') as watch:
            t.start();y.main();t.join(3);watch.assert_not_called()
        self.assertEqual((errors,sent),([],[True]));self.assertIsNone(y._FOREGROUND)
        self.assertEqual(self.saved[-1][0].returncode,0)
        self.assertEqual(signal.getsignal(signal.SIGTERM),self.signals[signal.SIGTERM])

    def test_main_exception_joins_real_stream(self):
        y=self.y;cmd,mark=self.command('ignore');threads=[]
        def body(*unused):
            def popen(*a,**kw):return y._FOREGROUND.popen(cmd,**kw,env=ENV,cwd=self.area)
            with patch.object(y,'_transport_popen',side_effect=popen):
                t=threading.Thread(target=y.remote_stream_thread,args=('/not-read','fixture','/not-used'))
                y._WATCH_THREADS=[t];threads.append(t);t.start();wait(mark.exists)
            raise RuntimeError('synthetic watch failure')
        with self.main_patches(),patch.object(y,'preflight'),patch.object(y,'watch',side_effect=body):
            with self.assertRaisesRegex(RuntimeError,'synthetic watch failure'):y.main()
        self.assertFalse(threads[0].is_alive());self.assertEqual(self.saved[-1][0].returncode,-9)

    def test_stream_reconnect_delays_and_actual_wait(self):
        y=self.y;y._FOREGROUND=self.manager;delays=[]
        def popen(*a,**kw):return self.manager.popen(self.command('stream')[0],**kw,env=ENV,cwd=self.area)
        def backoff(seconds):
            delays.append(seconds)
            if len(delays)==4:self.stop.set();return True
            return False
        with patch.object(y,'_transport_popen',side_effect=popen),patch.object(self.stop,'wait',side_effect=backoff):
            y.remote_stream_thread('/not-read','fixture','/not-used')
        self.assertEqual(delays,[5.,10.,20.,30.])
        self.assertEqual([p.returncode for p,_,_ in self.saved],[0,0,0,0])
        self.assertEqual(self.manager.children,set())

    def test_stream_stop_during_admission_is_normal(self):
        y=self.y;y._FOREGROUND=self.manager
        def closed(*a,**kw):
            self.stop.set()
            raise ft.TransportStopped()
        with patch.object(y,'_transport_popen',side_effect=closed) as spawn:
            y.remote_stream_thread('/not-read','fixture','/not-used')
        self.assertEqual(spawn.call_count,1);self.assertEqual(self.saved,[])

    def test_cycle_and_checksum_policies_unchanged(self):
        y=self.y;local=y.ChangeSignal('local')
        with patch.object(y.time,'time',return_value=10):local.note()
        with patch.object(y.time,'time',return_value=11):local.note()
        self.assertEqual(local.pending_since(),10)
        self.assertEqual(y.cycle_trigger(12,12,.5,(local,)),'interval deadline')
        self.assertEqual(y.cycle_trigger(11,20,.5,(local,)),'local change')
        self.assertEqual(y.WATCH_RSYNC_FLAGS,'-rlptDvc');self.assertEqual(y.WATCH_EXCLUDES,[])

    def test_unreadable_or_unprotectable_original_aborts(self):
        from types import SimpleNamespace
        y=self.y;root=self.area/'non-agent-tree';root.mkdir();(root/'original').write_text('mine')
        gid=os.getgid()+1000000
        self.assertEqual(y.unmarked_files(root,gid),(['original'],True))
        with patch.object(y.os,'lstat',side_effect=PermissionError('synthetic ownership read failure')):
            self.assertEqual(y.unmarked_files(root,gid),([],False))
        with patch.object(y,'gc_exclude_and_targets',return_value=([],[],[])),patch.object(y,'unmarked_files',return_value=(['unsafe\nname'],True)),patch.object(y,'_transport_run',side_effect=AssertionError('no transfer allowed')):
            self.assertEqual(y.watch_rsync('/not-read','fixture',str(root),'/not-used',SimpleNamespace(verbose=False),'pull',gid),(False,0))

    def test_watch_still_refuses_delete_and_oneshot_foreground(self):
        for argv in (['watch',str(self.area),'fixture:/not-used','--delete'],['push',str(self.area),'fixture:/not-used','--foreground']):
            with self.subTest(argv=argv),patch.object(sys,'argv',['ssh-sync.py']+argv):
                with self.assertRaises(SystemExit) as stopped:self.y.main()
                self.assertEqual(stopped.exception.code,2)
        self.assertEqual(self.saved,[])


if __name__=='__main__':unittest.main(verbosity=2)
