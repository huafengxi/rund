"""Linux-only shared execution/identity/stop kernel; explicitly invoked, no daemon.

Cooperating immutable foreground payloads and helpers may exec and close FDs.
They must preserve process identity/group/session/UID; daemonization and group
escape remain unsupported. Observation uses strong identity, group membership
and real owner wait, without per-run lease files or inherited private FDs.
This is an availability fence, not an exactly-once/security guarantee.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from .contract import name, require
from .prepare import (canonical, directory, digest, init_root, no_links,
                      read_regular, sync_dir, write_new)


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(read_regular(Path(path)))


def publish(path, value, *, final=False):
    """Atomic visibility under the synchronous, node-flock-fenced sole writer.

    Terminal destinations are checked before publishing and never reused. A
    rename avoids a link/unlink crash window leaving a hardlinked result that
    P2a's regular-single-link reader would correctly refuse. Hostile same-UID
    mutation/concurrent calls on one Runtime object are outside this contract.
    """
    path = Path(path)
    no_links(path)
    if path.exists():
        read_regular(path)
        require(not final, 'terminal result cannot be overwritten')
    directory(path.parent)
    temp = path.parent / ('.publish-' + uuid.uuid4().hex)
    write_new(temp, canonical(value))
    if final:
        require(not path.exists(), 'terminal result cannot be overwritten')
    os.replace(temp, path)
    sync_dir(path.parent)


def private_fd(path):
    no_links(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    st = os.fstat(fd)
    if not (stat.S_ISREG(st.st_mode) and st.st_nlink == 1):
        os.close(fd)
        raise ValueError('private stable regular lock required')
    return fd


def proc(pid):
    """None = absent; errors other than disappearance remain explicit unknown."""
    try:
        p = Path('/proc') / str(pid)
        raw = (p / 'stat').read_text()
        tail = raw[raw.rindex(')') + 2:].split()
        return {'pid': int(pid), 'procStart': int(tail[19]), 'pgid': int(tail[2]),
                'session': int(tail[3]), 'state': tail[0], 'uid': p.stat().st_uid}
    except (FileNotFoundError, ProcessLookupError):
        return None


def same_identity(actual, recorded):
    return actual is not None and all(actual[k] == recorded.get(k)
                                      for k in ('pid', 'procStart', 'pgid', 'session', 'uid'))


def group_members(recorded):
    """Read-only group enumeration. Never grants kill authority without leader.

    Zombies are recorded but have exited. All scans must succeed; unreadable
    identity is not evidence of an empty group.
    """
    members = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        item = proc(int(entry.name))
        if item and item['pgid'] == recorded['pgid']:
            members.append(item)
    return members


class Kernel:
    def __init__(self, root, node, hook=None):
        require(sys.platform == 'linux' and Path('/proc/self/stat').exists(), 'Linux /proc required')
        name(node)
        self.root = init_root(root)
        self.node = node
        self.owner_pid = os.getpid()
        self.epoch = uuid.uuid4().hex
        self.hook = hook or (lambda event, run: None)
        self.processes, self.waited = {}, {}
        owners = directory(self.root / 'state/owners')
        # One root is one local node; root lock also fences accidental node aliases.
        self.fd = private_fd(owners / 'node.lock')
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            identity = owners / 'node.json'
            if identity.exists():
                require(read_json(identity) == {'node': node}, 'root belongs to another node')
            else:
                publish(identity, {'node': node})
            self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            publish(owners / 'owner.json', {'node': node, 'ownerEpoch': self.epoch,
                    'pid': os.getpid(), 'procStart': proc(os.getpid())['procStart'], 'startedAt': now()})
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def owned(self):
        require(self.fd is not None and os.getpid() == self.owner_pid, 'closed or fork-inherited coordinator cannot operate')

    def run_dir(self, run):
        self.owned()
        for field in ('kind', 'id', 'attemptId'):
            name(run[field])
        require(type(run['generation']) is int and run['generation'] > 0, 'invalid generation')
        return directory(self.root / 'state/runs' / run['kind'] / run['id'] /
                         run['attemptId'] / str(run['generation']))

    def key(self, run):
        return str(self.run_dir(run))

    def save(self, run):
        path = self.run_dir(run)
        require(not (path / 'result.json').exists(), 'terminal result is immutable')
        publish(path / 'run.json', run)

    def transition(self, run, phase, reason):
        if run.get('phase') != phase:
            run['history'].append({'phase': phase, 'reason': reason, 'at': now(), 'ownerEpoch': self.epoch})
        run.update(phase=phase, reason=reason, ownerEpoch=self.epoch)
        self.save(run)
        return run

    def finish(self, run, phase, reason, observation=None):
        result = self.run_dir(run) / 'result.json'
        if result.exists():
            return read_json(result)
        if observation:
            run.update(exitCode=observation['exitCode'], signal=observation['signal'],
                       waitObserved=observation['waitObserved'])
        run.update(endedAt=now(), phase=phase, reason=reason, ownerEpoch=self.epoch)
        run['history'].append({'phase': phase, 'reason': reason, 'at': now(), 'ownerEpoch': self.epoch})
        # result.json is the single terminal commit point. No mutable snapshot
        # can claim a terminal phase before that fenced publication.
        publish(result, run, final=True)
        return run

    def launch(self, run, d, prepared):
        key = self.key(run)
        rel = Path(run['kind']) / run['id'] / run['attemptId'] / str(run['generation'])
        work = directory(self.root / 'work' / rel)
        logs = directory(self.root / 'logs' / rel)
        home = directory(work / 'home')
        tmp = directory(work / 'tmp')
        replacements = {'${work}': str(work)}
        replacements.update({'${code.' + s['alias'] + '}': str(Path(prepared['code']) / s['alias'])
                             for s in d['spec']['sources']})
        def expand(text):
            for token, value in replacements.items():
                text = text.replace(token, value)
            return text
        argv = [expand(a) for a in d['spec']['argv']]
        executable = shutil.which(argv[0], path=os.defpath)
        if argv[0] == d['spec']['toolchain']['python']:
            executable = read_json(prepared['manifest'])['inputs']['tools']['python']['path']
        require(executable is not None and Path(executable).is_absolute() and os.access(executable, os.X_OK),
                'payload executable unavailable')
        argv[0] = executable
        env = {'PATH': os.defpath, 'HOME': str(home), 'TMPDIR': str(tmp), 'LC_ALL': 'C.UTF-8'}
        # Runtime-owned keys cannot be overridden by a declaration.
        require(not (set(d['spec']['env']) & set(env)), 'reserved execution environment key')
        env.update({k: expand(v) for k, v in d['spec']['env'].items()})
        run.update(prepareKey=prepared['key'], sourceCommits={s['alias']: s['revision'] for s in d['spec']['sources']},
                   cwd=str(work), stdout=str(logs / 'stdout'), stderr=str(logs / 'stderr'),
                   argv=argv, bootId=self.boot)
        self.transition(run, 'starting', 'spawn-intent')
        self.hook('starting', run)
        # No shell. Logs cannot overwrite an earlier invocation.
        with open(logs / 'stdout', 'xb', buffering=0) as out, open(logs / 'stderr', 'xb', buffering=0) as err:
            p = subprocess.Popen(argv, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=err, start_new_session=True)
        self.processes[key] = p
        self.hook('spawned', run)  # intentional non-atomic spawn -> identity publication window
        identity = proc(p.pid)
        require(identity is not None and identity['pgid'] == p.pid and identity['session'] == p.pid,
                'spawn identity unavailable')
        run.update({k: identity[k] for k in ('pid', 'procStart', 'pgid', 'session', 'uid')})
        run['startedAt'] = now()
        self.transition(run, 'running', 'spawn-recorded')
        self.hook('recorded', run)
        return run

    def observe(self, run):
        key = self.key(run)
        # One retry only: a true poll(None) can precede a matching zombie sample.
        # Only the local Popen supplies wait status; a dead-state/empty-group
        # sample never substitutes for a still-owned, unobserved wait.
        for sample in range(2):
            p = self.processes.get(key)
            if p is not None:
                rc = p.poll()
                if rc is not None:
                    self.waited[key] = rc
                    self.processes.pop(key)
                    self.hook('waited', run)
            rc = self.waited.get(key)
            fact = {'exitCode': rc if rc is not None and rc >= 0 else None,
                    'signal': -rc if rc is not None and rc < 0 else None,
                    'waitObserved': rc is not None, 'observedAt': now()}
            try:
                if run.get('pid') is None:
                    # No identity cannot distinguish pre-spawn from post-spawn
                    # crash. Keep BOTH Task and Service blocked, even if quiet.
                    fact.update(status='unknown', reason='starting-identity-unavailable', members=[])
                    return fact
                if run.get('bootId') != self.boot:
                    fact.update(status='unknown', reason='boot-identity-mismatch', members=[])
                    return fact
                actual = proc(run['pid'])
                members = group_members(run)
                fact.update(leader=actual, members=members)
                if actual is not None and not same_identity(actual, run):
                    fact.update(status='unknown', reason='identity-mismatch')
                elif any(m['session'] != run['session'] or m['uid'] != run['uid'] for m in members):
                    fact.update(status='unknown', reason='group-identity-ambiguous')
                elif actual is not None and actual['state'] not in ('Z', 'X'):
                    fact.update(status='running', reason='identity-live')
                elif (sample == 0 and self.boot and p is not None and
                      self.processes.get(key) is p and p.pid == run['pid'] and
                      same_identity(actual, run)):
                    # Re-poll nonblocking and rebuild leader/group facts.
                    # None/unreadable/mismatch remain conservative.
                    continue
                elif any(m['state'] not in ('Z', 'X') for m in members):
                    fact.update(status='unknown', reason='leader-exited-group-remains')
                elif key in self.processes:
                    fact.update(status='unknown', reason='owned-wait-unavailable')
                else:
                    fact.update(status='exited', reason='group-converged' if fact['waitObserved']
                                else 'orphan-exit-status-unavailable-group-converged')
            except (OSError, ValueError, IndexError) as e:
                fact.update(status='unknown', reason='identity-observation-unavailable: ' + str(e))
            return fact

    def stop(self, run, term_seconds=0.5, kill_seconds=2):
        require(all(type(t) in (int, float) and 0 <= t <= 60 for t in (term_seconds, kill_seconds)), 'invalid stop bounds')
        events = []
        fact = self.observe(run)
        events.append({'event': 'before-stop', **fact})
        if fact['status'] != 'running':
            return fact, events, False
        sent = False
        for sig, bound in ((signal.SIGTERM, term_seconds), (signal.SIGKILL, kill_seconds)):
            # Recheck strong leader identity at EACH escalation, not just once.
            try:
                actual = proc(run['pid'])
            except (OSError, ValueError, IndexError) as e:
                fact.update(status='unknown', reason='identity-recheck-unavailable: ' + str(e))
                events.append({'event': 'signal-refused', 'at': now(), **fact})
                return fact, events, sent
            if run.get('bootId') != self.boot or not same_identity(actual, run) or actual['state'] in ('Z', 'X'):
                fact = self.observe(run)
                events.append({'event': 'signal-refused', 'at': now(), **fact})
                return fact, events, sent
            try:
                os.killpg(run['pgid'], sig)
            except ProcessLookupError:
                pass
            except OSError as e:
                fact.update(status='unknown', reason='signal-unavailable: ' + str(e))
                events.append({'event': 'signal-refused', 'at': now(), **fact})
                return fact, events, sent
            sent = True
            events.append({'event': 'signal', 'signal': int(sig), 'at': now(), 'identity': actual})
            deadline = time.monotonic() + bound
            while True:
                fact = self.observe(run)
                if fact['status'] == 'exited' or time.monotonic() >= deadline:
                    break
                # An absent leader never authorizes KILL to a possibly reused group.
                time.sleep(0.01)
            events.append({'event': 'after-signal', 'signal': int(sig), **fact})
            if fact['status'] == 'exited':
                return fact, events, sent
        return fact, events, sent
