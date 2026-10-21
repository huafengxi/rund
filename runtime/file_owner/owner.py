"""One synchronous foreground adapter. Execution authority stays in S Runtime."""
import os
from pathlib import Path
import time

from runtime.contract import name, require, repository, validate
from runtime.kernel import now, publish, read_json
from runtime.lifecycle import Runtime
from runtime.prepare import canonical, digest, directory, no_links
from .protocol import (API, binding, decode, disjoint, envelope, inner_key,
                       outer_key, private, read, request_body, target)


def fact_path(path, *, folder=False):
    # S intermediate dirs inherit umask; the original private root fences them.
    path = no_links(path)
    st = path.stat()
    require(st.st_uid == os.getuid(), 'foreign runtime fact')
    require(path.is_dir() if folder else path.is_file() and st.st_nlink == 1,
            'invalid runtime fact type/link count')
    return path


class Owner:
    def __init__(self, root, protocol, node, *, hook=None, kernel_hook=None):
        self.protocol = Path(protocol)
        disjoint(self.protocol, Path(root))
        self.hook = hook or (lambda event, value: None)
        self.runtime = Runtime(root, node, hook=kernel_hook)
        self.node = node
        self.entries = None
        self.errors = []
        try:
            private(self.runtime.root, folder=True)
            if self.protocol.exists():
                private(self.protocol, folder=True)
                require(not list(self.protocol.iterdir()) or (self.protocol / 'binding.json').exists(),
                        'unmanaged protocol root')
            private(directory(self.protocol), folder=True)
            b = {'apiVersion': API, 'node': node, 'runtimeRoot': str(self.runtime.root)}
            if (self.protocol / 'binding.json').exists():
                require(binding(self.protocol) == b, 'protocol belongs to different node/runtime')
            else:
                publish(self.protocol / 'binding.json', b)
            for part in ('requests', 'rejections'):
                private(directory(self.protocol / part), folder=True)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.entries is not None:
            self.entries.close()
            self.entries = None
        self.runtime.kernel.close()

    def check_declaration(self, d):
        validate(d)
        require(d['spec']['node'] == self.node, 'wrong head node')
        for source in d['spec']['sources']:
            repo = repository(source['repository'])
            if isinstance(repo, Path):
                disjoint(self.protocol, repo)

    def discover(self):
        """Validate original heads and run indexing; never synthesize registration."""
        r = self.runtime
        tasks, services, indexed, errors = [], [], set(), []
        try:
            objects = r.root / 'state/objects'
            if objects.exists():
                fact_path(objects, folder=True)
                for kinds in objects.iterdir():
                    fact_path(kinds, folder=True)
                    require(kinds.name in ('Task', 'Service'), 'unknown object kind')
                    for obj in kinds.iterdir():
                        fact_path(obj, folder=True)
                        name(obj.name)
                        for head in obj.iterdir():
                            record = read_json(fact_path(head))
                            if kinds.name == 'Task':
                                require(head.suffix == '.json', 'unknown Task head')
                                attempt = head.stem
                                name(attempt)
                                d = record['declaration']
                                require(record['digest'] == digest(canonical(d)), 'Task head digest mismatch')
                                require(d['spec']['attemptId'] == attempt, 'attempt mismatch')
                                run = r.task(obj.name, attempt)
                                indexed.add(('Task', obj.name, attempt, 1))
                            else:
                                require(head.name == 'service.json', 'unknown Service head')
                                d = record['declaration']
                                generation = record['generation']
                                require(type(generation) is int and generation >= 0 and record['id'] == obj.name,
                                        'invalid Service head')
                                run = r.read_run('Service', obj.name, 'service', generation) if generation else None
                                require(generation <= 100000, 'alpha generation inventory limit exceeded')
                                for g in range(1, generation + 1):
                                    indexed.add(('Service', obj.name, 'service', g))
                            self.check_declaration(d)
                            require(d['kind'] == kinds.name and d['metadata']['id'] == obj.name, 'head binding mismatch')
                            if run:
                                self.check_declaration(run['declaration'])
                                require(run['node'] == self.node and
                                        run['declarationDigest'] == digest(canonical(run['declaration'])) and
                                        run['id'] == obj.name and run['kind'] == kinds.name and
                                        run['declaration']['metadata']['id'] == obj.name and
                                        run['declaration']['kind'] == kinds.name and
                                        run['attemptId'] == (attempt if kinds.name == 'Task' else 'service') and
                                        run['generation'] == (1 if kinds.name == 'Task' else generation),
                                        'run binding mismatch')
                                require(kinds.name != 'Task' or run['declaration'] == d, 'Task declaration mismatch')
                            if kinds.name == 'Task':
                                tasks.append((obj.name, attempt))
                            else:
                                services.append(obj.name)
                            require(len(tasks) + len(services) <= 4096, 'alpha head limit exceeded')
            runs = r.root / 'state/runs'
            if runs.exists():
                fact_path(runs, folder=True)
                for kinds in runs.iterdir():
                    fact_path(kinds, folder=True)
                    require(kinds.name in ('Task', 'Service'), 'unknown run kind')
                    for obj in kinds.iterdir():
                        fact_path(obj, folder=True)
                        name(obj.name)
                        for attempt in obj.iterdir():
                            fact_path(attempt, folder=True)
                            name(attempt.name)
                            for gen in attempt.iterdir():
                                fact_path(gen, folder=True)
                                require(gen.name.isdecimal() and str(int(gen.name)) == gen.name,
                                        'invalid generation directory')
                                require((kinds.name, obj.name, attempt.name, int(gen.name)) in indexed,
                                        'unindexed run: ' + str(gen))
        except (OSError, ValueError, KeyError, TypeError) as e:
            errors.append('discovery blocked: ' + str(e))
        return tasks, services, errors

    def pending(self):
        blocked, errors = set(), []
        base = self.runtime.root / 'state/requests'
        try:
            if base.exists():
                fact_path(base, folder=True)
                for path in base.iterdir():
                    if path.name.startswith('.publish-'):
                        continue  # incomplete atomic IO, never an accepted record
                    require(path.suffix == '.json', 'unknown request artifact')
                    rec = read_json(fact_path(path))
                    body = rec['body']
                    require(rec['digest'] == digest(canonical(body)), 'damaged request digest')
                    require(rec['status'] in ('pending', 'done'), 'unknown request status')
                    if rec['status'] == 'done':
                        require('result' in rec, 'done without result')
                        continue
                    op = body['operation']
                    if op == 'file-owner':
                        e = envelope(body['envelope'], self.node)
                        require(path.stem == outer_key(e['requestId']), 'outer record key mismatch')
                        blocked.add(target(e))
                    elif op in ('release', 'service'):
                        self.check_declaration(body['declaration'])
                        d = body['declaration']
                        blocked.add((d['kind'], d['metadata']['id']))
                    elif op == 'control':
                        Runtime.validate_control(body['control'])
                        blocked.add((body['control']['kind'], body['control']['id']))
                    else:
                        require(False, 'unknown pending operation')
        except (OSError, ValueError, KeyError, TypeError) as e:
            errors.append('request records blocked: ' + str(e))
        return blocked, errors

    def existing(self, e):
        # Runtime's convenience readers create directories. Admission must not
        # let an unknown status/start/control allocate an unindexed generation.
        action = e['action']
        if action in ('accept', 'release', 'submit'):
            return
        kind, id = target(e)
        attempt = e['oldAttempt'] if action == 'rerun' else e['attemptId']
        base = self.runtime.root / 'state/objects' / kind / id
        head = read_json(fact_path(base / (attempt + '.json' if kind == 'Task' else 'service.json')))
        gen = 1 if kind == 'Task' else head['generation']
        if gen:
            run = self.runtime.root / 'state/runs' / kind / id / attempt / str(gen)
            fact_path(run / ('result.json' if (run / 'result.json').exists() else 'run.json'))

    def execute(self, e):
        r, action = self.runtime, e['action']
        if action in ('accept', 'release'):
            return r.accept_task(e['declaration'], start=action == 'release')
        if action == 'rerun':
            return r.rerun_task(e['id'], e['oldAttempt'], e['declaration'])
        if action == 'submit':
            return r.submit_service(e['declaration'], inner_key(e['requestId']))
        if action == 'start':
            return r.start_task(e['id'], e['attemptId'])
        if action == 'observe':
            return r.observe_task(e['id'], e['attemptId'])
        if action == 'status':
            return r.task(e['id'], e['attemptId']) if e['kind'] == 'Task' else r.service(e['id'])
        control = {k: e[k] for k in ('action', 'kind', 'id', 'attemptId', 'expectedGeneration')}
        control.update(apiVersion='rund-control/v1alpha1', requestId=inner_key(e['requestId']))
        return r.control(control)

    def process(self, path):
        raw = None
        try:
            raw = read(path)
            e = envelope(decode(raw), self.node)
            require(path.name == e['requestId'] + '.json', 'filename/requestId mismatch')
            if 'declaration' in e:
                self.check_declaration(e['declaration'])
            key = outer_key(e['requestId'])
            record_path = self.runtime.root / 'state/requests' / (key + '.json')
            # Duplicate lookup precedes current blocking: exact done is immutable.
            if record_path.exists():
                record = read_json(fact_path(record_path))
                require(record['digest'] == digest(canonical(request_body(e))),
                        'requestId conflicts with prior body')
                require(record['body'] == request_body(e), 'record body mismatch')
                return  # done returns original record; pending NEVER replays
            self.existing(e)
            _, _, discovery_errors = self.discover()
            blocked, pending_errors = self.pending()
            p, rec, duplicate = self.runtime.request(key, request_body(e))
            require(not duplicate, 'unexpected duplicate')
            self.hook('pending', e)
            if (discovery_errors or pending_errors or target(e) in blocked) and e['action'] not in ('status', 'observe'):
                value = {'status': 'blocked', 'reason': 'unknown/pending facts; no effects',
                         'diagnostics': discovery_errors + pending_errors}
            else:
                value = self.execute(e)
                self.hook('effect', e)
            # A failure here leaves original pending. No exception handler can fill success.
            self.hook('before-receipt', e)
            self.runtime.receipt(p, rec, value)
            self.hook('done', e)
        except (OSError, ValueError, KeyError, TypeError) as error:
            message = {'status': 'rejected-or-blocked', 'reason': str(error), 'at': now(),
                       'file': path.name, 'rawDigest': digest(raw) if raw is not None else None}
            self.errors.append(message)
            try:
                name(path.stem)
                publish(self.protocol / 'rejections' / path.name, message)
            except (OSError, ValueError):
                pass  # diagnostic failure is not an operation receipt

    def round(self, batch=16):
        require(type(batch) is int and 1 <= batch <= 16, 'bounded batch required')
        self.errors = []
        private(self.protocol / 'requests', folder=True)
        if self.entries is None:
            self.entries = os.scandir(self.protocol / 'requests')
        for _ in range(batch):
            entry = next(self.entries, None)
            if entry is None:
                self.entries.close()
                self.entries = None
                break
            if entry.name.endswith('.json') and not entry.name.startswith('.'):
                self.process(Path(entry.path))
        tasks, services, errors = self.discover()
        blocked, pending_errors = self.pending()
        errors.extend(pending_errors)
        if not errors:
            for id, attempt in tasks:
                self.runtime.observe_task(id, attempt)
            for id in services:
                if ('Service', id) not in blocked:
                    self.runtime.tick_service(id)
        runs, heads = [], []
        # Projection after progression; read original facts, never complete receipts from these.
        for id, attempt in tasks:
            runs.append(self.runtime.task(id, attempt))
        for id in services:
            service = self.runtime.service(id)
            heads.append(service)
            if service['generation']:
                runs.append(self.runtime.read_run('Service', id, 'service', service['generation']))
        value = {'apiVersion': API, 'node': self.node, 'ownerEpoch': self.runtime.kernel.epoch,
                 'owner': read_json(self.runtime.root / 'state/owners/owner.json'),
                 'observedAt': now(), 'semantics': 'last-observed', 'runs': runs, 'services': heads,
                 'blockedTargets': sorted(blocked), 'diagnostics': errors + self.errors}
        publish(self.protocol / 'snapshot.json', value)
        self.hook('snapshot', value)
        return value

    def run(self, interval=0.05):
        require(type(interval) in (int, float) and 0.01 <= interval <= 5, 'bounded poll interval required')
        while True:
            self.round()
            time.sleep(interval)
