"""Official-manager reconciliation, separate from Runtime workload identities.

An effect is journaled before invoking the manager. Interrupted, timed out or
unconfirmed effects block replay, including after an Owner restart. A manager
wait is never reported as a wait for the daemon it controls.
"""
import copy
import os
from pathlib import Path
import subprocess

from .contract import require
from .environment import execution_environment
from .kernel import now, publish, read_json
from .prepare import directory, prepare


def binding(svc):
    value = copy.deepcopy(svc)
    value['desired'] = 'online'
    value['_declaration']['spec']['desired'] = 'online'
    return value


def location(root, name):
    return Path(root) / 'external' / name


def state(root, name):
    path = location(root, name) / 'head.json'
    return read_json(path) if path.exists() else None


def probe(svc, config, env=None):
    from .probes import online
    context = svc['_declaration']['spec'].get('context', {})
    effective = dict(config)
    for key in ('home', 'path', 'inheritEnv'):
        if key in context: effective[key] = context[key]
    if 'cwd' in context: effective['workspace'] = context['cwd']
    return online(svc, effective, env=env)


def reconcile(owner, svc):
    """Return observed official-manager facts or raise a replay-block diagnostic."""
    root = owner.runtime.root
    base = directory(location(root, svc['name']))
    head = state(root, svc['name']) or dict(sequence=0, binding=None, online=False)
    if head.get('request'):
        record = read_json(base / (head['request'] + '.json'))
        require(record['status'] == 'done',
                'external outcome unknown; inspect exact request ' + head['request'])
        require(record['confirmed'],
                'external action unconfirmed; inspect exact request ' + head['request'])
        if head.get('appliedSequence') != head['sequence']:
            head.update(binding=record['binding'], online=record['action'] == 'start',
                        appliedSequence=head['sequence'], adopted=False, observedAt=now())
            publish(base / 'head.json', head)
    wanted = binding(svc)
    current, note = probe(head['binding'] or svc, owner.config)
    # Existing official daemons remain under their manager. This initial
    # baseline is a probe observation, not a fabricated daemon process/wait.
    if head['binding'] is None:
        head.update(binding=wanted, online=current, observedAt=now(), adopted=True)
        publish(base / 'head.json', head)
    changed = head['binding'] != wanted
    desired = svc['desired'] == 'online'
    if changed and desired:
        # A bad replacement source must leave the existing daemon available.
        prepare(wanted['_declaration'], owner.runtime.root)
    if current and (not desired or changed):
        action(owner, base, head, head['binding'], 'stop')
        current = False
    if desired and not current:
        action(owner, base, head, wanted, 'start')
        current = True
    if not current and not desired and changed:
        head.update(binding=wanted, observedAt=now())
        publish(base / 'head.json', head)
    return dict(name=svc['name'], online=current,
                version=head['binding']['version'], sequence=head['sequence'],
                request=head.get('request'), adopted=head.get('adopted',False), observedAt=now(), note=note)


def action(owner, base, head, svc, verb):
    d = copy.deepcopy(svc['_declaration'])
    d['spec']['desired'] = 'online'
    prepared = prepare(d, owner.runtime.root)
    work = directory(base / 'work')
    replacements = {'${work}': str(work)}
    replacements.update({'${code.' + s['alias'] + '}': str(Path(prepared['code']) / s['alias'])
                         for s in d['spec']['sources']})
    def expand(value):
        for key, replacement in replacements.items():
            value = value.replace(key, replacement)
        return value
    spec = dict(d['spec'], env={k: expand(v) for k, v in d['spec']['env'].items()})
    env = execution_environment(spec, dict(PATH=os.defpath, HOME=str(work), LC_ALL='C.UTF-8'))
    argv = svc['stop_cmd'] if verb == 'stop' else d['spec']['argv']
    argv = [expand(owner.config['home'] + a[1:] if a == '~' or a.startswith('~/') else a) for a in argv]
    cwd = spec.get('context', {}).get('cwd', expand(spec['cwd']))
    # Preparing sources/credentials is effect-free and can retry. Once pending
    # is durable, any interruption must retain this exact identity forever.
    sequence = head['sequence'] + 1
    request = 'manager-' + str(sequence)
    record = dict(requestId=request, action=verb, status='pending', acceptedAt=now(),
                  version=svc['version'], declaration=d, prepareKey=prepared['key'],
                  binding=binding(svc), confirmed=False, managerWaitObserved=False)
    publish(base / (request + '.json'), record)
    head.update(sequence=sequence, request=request)
    publish(base / 'head.json', head)
    owner.hook('external-pending', record)
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)
        record.update(managerExitCode=result.returncode, managerWaitObserved=True)
        observed, note = probe(svc, owner.config, env=env)
        record.update(confirmed=result.returncode == 0 and observed == (verb == 'start'),
                      observedOnline=observed, probe=note, status='done', endedAt=now())
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        # No argv/output/environment is included in error diagnostics.
        record.update(status='unknown', reason=type(error).__name__, endedAt=now())
    owner.hook('external-effect', record)
    publish(base / (request + '.json'), record)
    require(record['status'] == 'done' and record['confirmed'],
            'external action unconfirmed; inspect exact request ' + request)
    head.update(binding=binding(svc), online=verb == 'start', adopted=False,
                appliedSequence=sequence, observedAt=now())
    publish(base / 'head.json', head)
