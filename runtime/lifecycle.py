"""Experimental independent Task attempts and Service declarations.

Only explicit calls progress state. No daemon, scheduler, report/provider logic,
automatic Task retries or cross-node takeover. See README for crash boundaries.
"""
import copy
import os
import time

from .contract import fields, name, require, validate
from .prepare import canonical, digest, directory, prepare
from .kernel import Kernel, now, publish, read_json

TASK_FINAL = frozenset(('succeeded', 'failed', 'canceled', 'lost'))


class Runtime:
    def __init__(self, root, node, hook=None):
        self.kernel = Kernel(root, node, hook)
        self.root = self.kernel.root
        self.node = node

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.kernel.close()

    def declaration(self, d):
        self.kernel.owned()
        d = copy.deepcopy(validate(d))
        require(d['spec']['node'] == self.node, 'declaration belongs to another node')
        return d

    def object_dir(self, kind, id):
        self.kernel.owned()
        require(kind in ('Task', 'Service'), 'invalid kind')
        name(id)
        return directory(self.root / 'state/objects' / kind / id)

    def request(self, request_id, body):
        self.kernel.owned()
        name(request_id)
        path = directory(self.root / 'state/requests') / (request_id + '.json')
        fingerprint = digest(canonical(body))
        if path.exists():
            old = read_json(path)
            require(old['digest'] == fingerprint, 'requestId conflicts with prior operation/digest')
            return path, old, True
        record = {'digest': fingerprint, 'body': body, 'status': 'pending', 'acceptedAt': now()}
        publish(path, record)
        return path, record, False

    def receipt(self, path, record, value):
        record.update(status='done', result=value, endedAt=now())
        publish(path, record)
        return value

    def new_run(self, d, attempt, generation):
        run = {'apiVersion': 'rund-result/v1alpha1', 'kind': d['kind'], 'id': d['metadata']['id'],
               'node': self.node, 'attemptId': attempt, 'generation': generation,
               'requestId': d['spec'].get('requestId'), 'version': d['metadata']['version'],
               'declarationDigest': digest(canonical(d)), 'declaration': d,
               'ownerEpoch': self.kernel.epoch,
               'sourceCommits': {s['alias']: s['revision'] for s in d['spec']['sources']}, 'prepareKey': None,
               'pid': None, 'procStart': None, 'pgid': None, 'session': None, 'uid': None,
               'acceptedAt': now(), 'startedAt': None, 'endedAt': None,
               'exitCode': None, 'signal': None, 'waitObserved': False, 'history': []}
        path = self.kernel.run_dir(run)
        require(not (path / 'run.json').exists(), 'run identity already exists')
        return self.kernel.transition(run, 'accepted' if d['kind'] == 'Task' else 'declared', 'declaration-accepted')

    def read_run(self, kind, id, attempt, generation):
        probe = {'kind': kind, 'id': id, 'attemptId': attempt, 'generation': generation}
        path = self.kernel.run_dir(probe)
        return read_json(path / ('result.json' if (path / 'result.json').exists() else 'run.json'))

    def task(self, id, attempt):
        return self.read_run('Task', id, attempt, 1)

    def accept_task(self, d, *, start=True, _rerun=False):
        d = self.declaration(d)
        require(d['kind'] == 'Task', 'Task required')
        id, attempt = d['metadata']['id'], d['spec']['attemptId']
        base = self.object_dir('Task', id)
        old = base / (attempt + '.json')
        dg = digest(canonical(d))
        if old.exists():
            require(read_json(old)['digest'] == dg, 'attempt declaration digest conflict')
        else:
            require(_rerun or not list(base.glob('*.json')), 'new attempt requires explicit rerun')
        path, req, duplicate = self.request(d['spec']['requestId'], {'operation': 'release', 'declaration': d})
        if old.exists():
            # Even an accepted but not yet started duplicate release never starts.
            runpath = self.kernel.run_dir({'kind': 'Task', 'id': id, 'attemptId': attempt, 'generation': 1})
            if (runpath / 'run.json').exists():
                return self.task(id, attempt)
        else:
            publish(old, {'digest': dg, 'declaration': d})
        # Completing interrupted acceptance is safe: no launch occurs before run.json.
        run = self.new_run(d, attempt, 1)
        self.receipt(path, req, {'kind': 'Task', 'id': id, 'attemptId': attempt, 'generation': 1})
        return self.start_task(id, attempt) if start and not duplicate else run

    def rerun_task(self, id, old_attempt, d, *, start=True):
        require(self.task(id, old_attempt)['phase'] in TASK_FINAL, 'old attempt must be terminal')
        require(d['kind'] == 'Task' and d['metadata']['id'] == id and d['spec']['attemptId'] != old_attempt,
                'rerun requires same id and NEW attempt/request')
        require(d['spec']['requestId'] != self.task(id, old_attempt)['requestId'], 'rerun requires new request')
        return self.accept_task(d, start=start, _rerun=True)

    def _launch(self, run, prepared=None):
        d = run['declaration']
        self.kernel.transition(run, 'preparing', 'prepare-requested')
        try:
            prepared = prepared or prepare(d, self.root)
        except (ValueError, OSError, __import__('subprocess').SubprocessError) as e:
            return self.kernel.finish(run, 'failed', 'prepare: ' + str(e))
        try:
            return self.kernel.launch(run, d, prepared)
        except (ValueError, OSError, __import__('subprocess').SubprocessError) as e:
            key = self.kernel.key(run)
            if key in self.kernel.processes:
                # Never turn a possibly live payload into a terminal spawn failure.
                return self.kernel.transition(run, 'cleanupUnknown', 'spawn-record-unavailable: ' + str(e))
            fd = self.kernel.leases.pop(key, None)
            if fd is not None:
                os.close(fd)
            return self.kernel.finish(run, 'failed', 'start: ' + str(e))

    def start_task(self, id, attempt):
        run = self.task(id, attempt)
        # Explicit start is one-shot too; preparing/starting cannot replay spawn.
        if run['phase'] != 'accepted':
            return run
        return self._launch(run)

    def observe_task(self, id, attempt):
        run = self.task(id, attempt)
        if run['phase'] in TASK_FINAL or run['phase'] == 'accepted':
            return run
        if run['phase'] == 'preparing':
            return self.kernel.finish(run, 'lost', 'coordinator-lost-during-prepare')
        fact = self.kernel.observe(run)
        run['observation'] = fact
        if fact['status'] == 'exited':
            if run.get('cancelRequested'):
                phase = 'canceled'
            else:
                phase = ('succeeded' if fact['exitCode'] == 0 else 'failed') if fact['waitObserved'] else 'lost'
            return self.kernel.finish(run, phase, fact['reason'], fact)
        return self.kernel.transition(run, 'running' if fact['status'] == 'running' else 'cleanupUnknown', fact['reason'])

    def service(self, id):
        return read_json(self.object_dir('Service', id) / 'service.json')

    def save_service(self, service):
        publish(self.object_dir('Service', service['id']) / 'service.json', service)
        return service

    def _service_start(self, service, prepared=None):
        d = service['declaration']
        # Allocate by immutable run directories, including a possible interrupted head update.
        base = directory(self.root / 'state/runs/Service' / service['id'] / 'service')
        generations = [int(p.name) for p in base.iterdir() if p.name.isdecimal()]
        service['generation'] = max(generations, default=0) + 1
        run = self.new_run(d, 'service', service['generation'])
        run['requestId'] = service['requestId']
        self.kernel.save(run)
        service.update(phase='starting', nextStartAt=None)
        self.save_service(service)  # head published BEFORE any spawn
        run = self._launch(run, prepared)
        service.update(phase=run['phase'])
        return self.save_service(service)

    def submit_service(self, d, request_id):
        d = self.declaration(d)
        require(d['kind'] == 'Service', 'Service required')
        id = d['metadata']['id']
        path, req, duplicate = self.request(request_id, {'operation': 'service', 'declaration': d})
        current = self.object_dir('Service', id) / 'service.json'
        if duplicate:
            if req['status'] == 'pending':
                return dict(req, reason='request-outcome-unknown-no-replay', current=self.service(id) if current.exists() else None)
            return self.service(id) if current.exists() else req
        previous = self.service(id) if current.exists() else None
        if previous:
            old = previous['declaration']
            if old == d:
                return self.receipt(path, req, previous)
            require(d['metadata']['version'] > old['metadata']['version'], 'Service change requires newer version')
        # Preparation failure must NOT stop the current generation or change desired identity.
        try:
            prepared = prepare(d, self.root) if d['spec']['desired'] == 'online' else None
        except (ValueError, OSError, __import__('subprocess').SubprocessError) as e:
            return self.receipt(path, req, {'phase': 'failed', 'reason': 'prepare: ' + str(e), 'previousUnchanged': bool(previous)})
        if previous and previous['generation']:
            previous = self._stop_service(previous)
            if previous['phase'] == 'cleanupUnknown':
                return self.receipt(path, req, previous)
        service = {'id': id, 'declaration': d, 'generation': previous['generation'] if previous else 0,
                   'phase': 'stopped', 'hold': d['spec']['desired'] == 'offline', 'failures': 0,
                   'nextStartAt': None, 'ownerEpoch': self.kernel.epoch, 'requestId': request_id}
        self.save_service(service)
        if not service['hold']:
            service = self._service_start(service, prepared)
        return self.receipt(path, req, service)

    def _service_current(self, service):
        return self.read_run('Service', service['id'], 'service', service['generation'])

    def tick_service(self, id):
        service = self.service(id)
        # A crash after run allocation but before head publication must not orphan
        # an unindexed run and then allocate another generation.
        base = self.root / 'state/runs/Service' / id / 'service'
        generations = [int(p.name) for p in base.iterdir() if p.name.isdecimal()] if base.exists() else []
        if max(generations, default=0) > service['generation']:
            service.update(phase='cleanupUnknown', reason='unindexed-generation-requires-manual-inspection')
            return self.save_service(service)
        if service['phase'] == 'backoff':
            if not service['hold'] and time.time() >= service['nextStartAt']:
                return self._service_start(service)
            return service
        if not service['generation'] or service['phase'] in ('stopped', 'exited', 'failed'):
            return service
        run = self._service_current(service)
        path = self.kernel.run_dir(run)
        if (path / 'result.json').exists():
            # Crash after immutable result, before Service policy snapshot.
            failed = run['exitCode'] != 0
        else:
            fact = self.kernel.observe(run)
            run['observation'] = fact
            if fact['status'] != 'exited':
                phase = 'running' if fact['status'] == 'running' else 'cleanupUnknown'
                self.kernel.transition(run, phase, fact['reason'])
                service.update(phase=phase, ownerEpoch=self.kernel.epoch)
                return self.save_service(service)
            run = self.kernel.finish(run, 'stopped' if run.get('stopRequested') else 'exited', fact['reason'], fact)
            failed = fact['exitCode'] != 0
        if service['hold'] or run.get('stopRequested'):
            service.update(phase='stopped', nextStartAt=None, hold=True)
        elif (service['declaration']['spec']['restart']['policy'] == 'always' or
              (failed and service['declaration']['spec']['restart']['policy'] == 'on-failure')):
            r = service['declaration']['spec']['restart']
            delay = min(r['maxSeconds'], r['initialSeconds'] * (2 ** min(service['failures'], 32)))
            service.update(phase='backoff', failures=service['failures'] + 1,
                           nextStartAt=time.time() + delay, backoffSeconds=delay)
        else:
            service.update(phase='exited', nextStartAt=None)
        return self.save_service(service)

    def _stop_run(self, run, terminal_phase, term_seconds=0.5, kill_seconds=2):
        path = self.kernel.run_dir(run)
        if (path / 'result.json').exists():
            return read_json(path / 'result.json')
        if run['phase'] in ('accepted', 'declared', 'preparing'):
            return self.kernel.finish(run, terminal_phase, 'canceled-before-spawn' if run['kind'] == 'Task' else 'stopped-before-spawn')
        self.kernel.transition(run, 'canceling' if run['kind'] == 'Task' else 'stopping', 'explicit-control')
        fact, events, sent = self.kernel.stop(run, term_seconds, kill_seconds)
        run.setdefault('stopEvidence', []).extend(events)
        # Observe-before-signal wins an exit/cancel race. Once a signal was
        # actually sent while alive, the cancellation/stop intent is retained.
        if sent:
            run['cancelRequested' if run['kind'] == 'Task' else 'stopRequested'] = True
        if fact['status'] == 'exited':
            if run['kind'] == 'Service':
                phase = terminal_phase if sent or run.get('stopRequested') else 'exited'
            else:
                phase = 'canceled' if sent or run.get('cancelRequested') else (
                    ('succeeded' if fact['exitCode'] == 0 else 'failed') if fact['waitObserved'] else 'lost')
            return self.kernel.finish(run, phase, fact['reason'], fact)
        return self.kernel.transition(run, 'cleanupUnknown', fact['reason'])

    def _stop_service(self, service, term_seconds=0.5, kill_seconds=2):
        service.update(hold=True, nextStartAt=None)
        self.save_service(service)  # stop desired state durable before effects
        if service['generation']:
            run = self._stop_run(self._service_current(service), 'stopped', term_seconds, kill_seconds)
            service['phase'] = 'cleanupUnknown' if run['phase'] == 'cleanupUnknown' else 'stopped'
        else:
            service['phase'] = 'stopped'
        return self.save_service(service)

    @staticmethod
    def validate_control(control):
        fields(control, ('apiVersion', 'requestId', 'action', 'kind', 'id', 'attemptId', 'expectedGeneration'))
        name(control['requestId'])
        require(control['apiVersion'] == 'rund-control/v1alpha1', 'unsupported control API')
        kind, id = control['kind'], control['id']
        require(kind in ('Task', 'Service'), 'invalid control kind')
        name(id)
        name(control['attemptId'])
        require(type(control['expectedGeneration']) is int and control['expectedGeneration'] > 0, 'expectedGeneration required')
        require(control['action'] in (('cancel',) if kind == 'Task' else ('stop', 'restart')), 'unsupported action')
        require(kind == 'Task' or control['attemptId'] == 'service', 'Service attemptId must be service')
        return control

    def control(self, control, *, term_seconds=0.5, kill_seconds=2):
        self.validate_control(control)
        kind, id = control['kind'], control['id']
        path, req, duplicate = self.request(control['requestId'], {'operation': 'control', 'control': control})
        if duplicate:
            # A pending receipt means coordinator died mid-effect; never replay
            # an ambiguous restart or signal. Explicit observe/manual decision.
            return req.get('result', req)
        service = self.service(id) if kind == 'Service' else None
        run = self._service_current(service) if service and service['generation'] else (
            self.task(id, control['attemptId']) if kind == 'Task' else None)
        if run is None or run['generation'] != control['expectedGeneration']:
            return self.receipt(path, req, {'noop': True, 'reason': 'stale-generation'})
        if kind == 'Task' and run['phase'] in TASK_FINAL:
            return self.receipt(path, req, {'noop': True, 'reason': 'terminal', 'run': run})
        if kind == 'Task':
            value = self._stop_run(run, 'canceled', term_seconds, kill_seconds)
        else:
            value = self._stop_service(service, term_seconds, kill_seconds)
            if control['action'] == 'restart' and value['phase'] != 'cleanupUnknown':
                # A stopped offline declaration cannot restart implicitly.
                require(value['declaration']['spec']['desired'] == 'online', 'offline Service cannot restart')
                value.update(hold=False, requestId=control['requestId'])
                value = self._service_start(value)
        return self.receipt(path, req, value)

    def wait_task(self, id, attempt, seconds=10):
        require(type(seconds) in (int, float) and 0 <= seconds <= 3600, 'bounded wait required')
        deadline = time.monotonic() + seconds
        while True:
            run = self.observe_task(id, attempt)
            if run['phase'] in TASK_FINAL or run['phase'] == 'cleanupUnknown' or time.monotonic() >= deadline:
                return run
            time.sleep(0.01)
