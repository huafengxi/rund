"""Native process facts for S; Darwin ABI adapted from pi-host/local_process.py.

Linux runtime facts keep their original /proc representation. Darwin uses
libproc start time in microseconds plus boot identity; errors remain unknown.
"""
import ctypes
import errno
from functools import lru_cache
import os
from pathlib import Path
import sys


class BSDInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'reserved')] + [
        ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + [
        (n, ctypes.c_uint32) for n in ('nfiles', 'pgid', 'pjobc', 'e_tdev', 'e_tpgid')] + [
        ('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
        ('start_usec', ctypes.c_uint64)]


@lru_cache(maxsize=1)
def _darwin():
    if sys.platform != 'darwin' or ctypes.sizeof(BSDInfo) != 136:
        raise RuntimeError('unsupported Darwin proc_bsdinfo ABI')
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                 ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_int]
    lib.proc_listpids.restype = ctypes.c_int
    return lib


@lru_cache(maxsize=1)
def boot_id():
    if sys.platform.startswith('linux'):
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if sys.platform != 'darwin':
        raise RuntimeError('unsupported local process platform')
    class Timeval(ctypes.Structure):
        _fields_ = [('sec', ctypes.c_long), ('usec', ctypes.c_int)]
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.sysctlbyname
    fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                   ctypes.c_void_p, ctypes.c_size_t]
    fn.restype = ctypes.c_int
    value = Timeval()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if fn(b'kern.boottime', ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), 'kern.boottime')
    if size.value != ctypes.sizeof(value) or value.sec <= 0:
        raise RuntimeError('invalid kern.boottime')
    return 'darwin:%d:%d' % (value.sec, value.usec)


def _bsd_info(pid):
    value = BSDInfo()
    ctypes.set_errno(0)
    n = _darwin().proc_pidinfo(pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value))
    if n != ctypes.sizeof(value):
        err = ctypes.get_errno()
        if n == 0 and err == errno.ESRCH:
            return None
        raise OSError(err or errno.EIO, 'proc_pidinfo incomplete/denied')
    if value.pid != pid or value.start_sec <= 0 or value.start_usec >= 1000000:
        raise RuntimeError('invalid proc_bsdinfo identity')
    return value


def identity(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError('positive integer pid required')
    if sys.platform.startswith('linux'):
        try:
            raw = Path('/proc/%d/stat' % pid).read_text()
            uid = os.stat('/proc/%d' % pid).st_uid
        except FileNotFoundError:
            return None
        fields = raw[raw.rfind(')') + 2:].split()
        return dict(pid=pid, start=fields[19], boot=boot_id(), uid=uid,
                    pgid=int(fields[2]), sid=int(fields[3]),
                    ppid=int(fields[1]), state=fields[0])
    if sys.platform != 'darwin':
        raise RuntimeError('unsupported local process platform')
    b = _bsd_info(pid)
    if b is None:
        return None
    # getsid is a separate observation, not an atomic handle. Recheck start so
    # a detected reuse during observation is an error rather than mixed facts.
    try:
        sid = os.getsid(pid)
    except ProcessLookupError:
        return None
    after = _bsd_info(pid)
    if after is None:
        return None
    if (b.start_sec, b.start_usec, b.pgid, b.uid) != (
            after.start_sec, after.start_usec, after.pgid, after.uid):
        raise RuntimeError('process changed during identity read')
    states = {1: 'I', 2: 'R', 3: 'S', 4: 'T', 5: 'Z'}
    if after.status not in states:
        raise RuntimeError('unknown Darwin process state')
    return dict(pid=pid, start=str(after.start_sec * 1000000 + after.start_usec),
                boot=boot_id(), uid=after.uid, pgid=after.pgid, sid=sid,
                ppid=after.ppid, state=states[after.status])



def group_pids(pgid):
    """Darwin PROC_PGRP_ONLY: enumerate this group, including foreign members."""
    lib = _darwin()
    ctypes.set_errno(0)
    size = lib.proc_listpids(2, pgid, None, 0)
    if size == 0 and ctypes.get_errno() in (0, errno.ESRCH):
        return []
    if size <= 0:
        raise OSError(ctypes.get_errno() or errno.EIO, 'proc_listpids size')
    for _ in range(4):
        values = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 128))()
        capacity = ctypes.sizeof(values)
        ctypes.set_errno(0)
        n = lib.proc_listpids(2, pgid, values, capacity)
        if n == 0 and ctypes.get_errno() in (0, errno.ESRCH):
            return []
        if n <= 0 or n % ctypes.sizeof(ctypes.c_int):
            raise OSError(ctypes.get_errno() or errno.EIO, 'proc_listpids data')
        if n < capacity:
            return [pid for pid in list(values)[:n // ctypes.sizeof(ctypes.c_int)] if pid > 0]
        size = capacity * 2
    raise RuntimeError('process group enumeration did not converge')
