"""Opt-in ownership of this link's direct transport children, not a supervisor.

No daemon, process-group signals, payload restart or persisted runtime facts.
The caller owns the foreground root; helpers must stay in its group/session.
"""
import locale
import subprocess
import tempfile
import threading
import time


class TransportStopped(Exception):
    """Stop was requested; no new transport command may be started."""


class ForegroundTransport:
    def __init__(self, stop):
        self.stop = stop
        self.lock = threading.Lock()
        self.children = set()
        self.closed = False

    def popen(self, argv, **kwargs):
        # Admission and registration are one critical section. close cannot
        # snapshot between a successful spawn and storing its real Popen.
        with self.lock:
            if self.closed or self.stop.is_set():
                raise TransportStopped()
            proc = subprocess.Popen(argv, **kwargs)
            self.children.add(proc)
            return proc

    def forget(self, proc):
        if proc.returncode is None:
            raise RuntimeError('cannot forget an unobserved transport child')
        with self.lock:
            self.children.discard(proc)

    @staticmethod
    def finish(proc, grace=1):
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            return proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            # Never report settled merely because KILL was sent. If wait is
            # stuck, keep this root alive for its external supervisor to stop.
            return proc.wait()

    def run(self, argv, *, input=None, timeout=None, **kwargs):
        if input is not None:
            if kwargs.pop('stdin', None) is not None:
                raise ValueError('stdin and input may not both be supplied')
            # Retrying communicate(input=...) cannot portably resume unsent
            # stdin. Use an anonymous, private stdin buffer, not another thread
            # or protocol file. Only fd 0 is inherited; close_fds stays default.
            text = any(kwargs.get(k) for k in ('text','universal_newlines','encoding','errors'))
            data = input.encode(kwargs.get('encoding') or locale.getpreferredencoding(False),
                                kwargs.get('errors') or 'strict') if text else input
            with tempfile.TemporaryFile() as source:
                source.write(data)
                source.seek(0)
                return self.run(argv, stdin=source, timeout=timeout, **kwargs)
        proc = self.popen(argv, **kwargs)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                if self.stop.is_set():
                    self.finish(proc)
                    proc.communicate()
                    raise TransportStopped()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self.finish(proc)
                    out, err = proc.communicate()
                    raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
                try:
                    out, err = proc.communicate(timeout=.1 if remaining is None else min(.1, remaining))
                    if self.stop.is_set():
                        raise TransportStopped()
                    return subprocess.CompletedProcess(argv, proc.returncode, out, err)
                except subprocess.TimeoutExpired:
                    # communicate retains output buffers; stdin is already a
                    # complete anonymous file and never needs to be resent.
                    continue
        finally:
            self.finish(proc)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            self.forget(proc)

    def close(self):
        with self.lock:
            self.closed = True
            self.stop.set()
            children = list(self.children)
        # Wake all stream/communicate readers before waiting on any one child.
        for proc in children:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        for proc in children:
            self.finish(proc)
            self.forget(proc)
