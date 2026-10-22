"""Native foreground process execution. All Popen handles stay in one Owner."""
import os
import signal
import subprocess
from pathlib import Path
from .native import identity,boot_id,group_pids
from .store import require,directory

KEYS=('pid','start','boot','uid','pgid','sid')
def same(a,b):return a is not None and all(a.get(k)==b.get(k) for k in KEYS)
def alive(p):return p is not None and p.get('state') not in ('Z','X')
def members(p):
    ids=group_pids(p['pgid']) if __import__('sys').platform=='darwin' else [int(x.name) for x in Path('/proc').iterdir() if x.name.isdecimal()]
    return [v for pid in ids if (v:=identity(pid)) and v['pgid']==p['pgid']]

def expand(value,c,task):
    mapping={'shared':c['sharedRoot'],'private':c['privateRoot'],'artifacts':str(Path(c['privateRoot'])/'artifacts'),'work':str(Path(c['privateRoot'])/'tasks'/task/'work'),'runtime':c.get('runtimeSource',str(Path(__file__).resolve().parents[1]))}
    for k,v in mapping.items():value=value.replace('${'+k+'}',v)
    require('${' not in value,'unknown execution placeholder')
    return c['home']+value[1:] if value=='~' or value.startswith('~/') else value

class Processes:
    def __init__(self,c):self.c=c;self.handles={}
    def spawn(self,d):
        c=self.c;tid=d['id'];work=directory(Path(c['privateRoot'])/'tasks'/tid/'work');logs=directory(Path(c['privateRoot'])/'tasks'/tid)
        env=dict(HOME=c['home'],PATH=c['path'],LC_ALL='C.UTF-8',TMPDIR=str(directory(work/'tmp')),PYTHONDONTWRITEBYTECODE='1')
        env.update({k:expand(v,c,tid) for k,v in d.get('env',{}).items()})
        argv=[expand(x,c,tid) for x in d['argv']];cwd=expand(d.get('cwd','${work}'),c,tid)
        with (logs/'stdout').open('xb',buffering=0) as out,(logs/'stderr').open('xb',buffering=0) as err:
            p=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
        self.handles[tid]=p
        fact=identity(p.pid);require(fact is not None and fact['pgid']==p.pid and fact['sid']==p.pid,'spawn identity unavailable')
        return {k:fact[k] for k in KEYS}
    def observe(self,tid,p):
        handle=self.handles.get(tid);rc=handle.poll() if handle else None
        if p is None:return dict(status='unknown',reason='spawn identity unavailable',rc=rc,waitObserved=rc is not None)
        try:
            require(p['boot']==boot_id(),'boot identity changed')
            actual=identity(p['pid']);group=members(p)
            require(actual is None or same(actual,p),'process identity changed')
            require(all(m['uid']==p['uid'] and m['sid']==p['sid'] for m in group),'process group identity changed')
            if alive(actual):return dict(status='running',rc=rc,waitObserved=rc is not None)
            if any(alive(m) for m in group):return dict(status='unknown',reason='leader exited but group remains',rc=rc,waitObserved=rc is not None)
            if handle is not None:
                rc=handle.poll()
                if rc is None:return dict(status='unknown',reason='owned wait unavailable',rc=None,waitObserved=False)
                return dict(status='exited',rc=rc,waitObserved=True)
            return dict(status='unknown',reason='prior Owner wait unavailable',rc=None,waitObserved=False)
        except (OSError,ValueError,RuntimeError) as e:return dict(status='unknown',reason=str(e),rc=rc,waitObserved=rc is not None)
    def signal(self,p,kill=False):
        require(p and p['boot']==boot_id(),'unknown process boot identity')
        actual=identity(p['pid']);require(same(actual,p) and alive(actual),'signal requires live exact leader')
        require(all(m['uid']==p['uid'] and m['sid']==p['sid'] for m in members(p)),'ambiguous group')
        os.killpg(p['pgid'],signal.SIGKILL if kill else signal.SIGTERM)
