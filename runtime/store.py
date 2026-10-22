"""Atomic object files; no execution, transport or source preparation."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from datetime import datetime, timezone


def now(): return datetime.now(timezone.utc).isoformat()
def canonical(v): return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
def digest(v): return hashlib.sha256(canonical(v)).hexdigest()
def require(ok,message):
    if not ok: raise ValueError(message)
def directory(p):
    p=Path(p);p.mkdir(mode=0o700,parents=True,exist_ok=True);return p

def read(p):
    p=Path(p)
    def pairs(items):
        d={}
        for k,v in items:
            require(k not in d,'duplicate JSON key');d[k]=v
        return d
    st=p.lstat();require(stat.S_ISREG(st.st_mode) and st.st_nlink==1,'regular single-link JSON required')
    with p.open('rb') as f:return json.load(f,object_pairs_hook=pairs,parse_constant=lambda _:require(False,'nonfinite JSON'))

def write(p,value):
    p=Path(p);directory(p.parent)
    data=canonical(value)
    if p.exists():
        require(not p.is_symlink(),'symlink output refused')
        if p.read_bytes()==data:return
    t=p.with_name('.write-'+uuid.uuid4().hex)
    with t.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    t.chmod(0o600);os.replace(t,p)
    fd=os.open(p.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def lock(p):
    directory(Path(p).parent)
    fd=os.open(p,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BaseException:os.close(fd);raise
    return fd
