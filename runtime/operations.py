"""Finite explicit CLI operations. No socket, watching, background loop or scheduler."""
import json
from pathlib import Path

from .contract import fields, load, name, no_duplicates, require, text
from .kernel import read_regular
from .lifecycle import Runtime


def absolute_file(path):
    text(path)
    require(Path(path).is_absolute(), 'input file must be absolute')
    return path


def execute(root, node, path):
    absolute_file(path)
    operations = json.loads(read_regular(Path(path)), object_pairs_hook=no_duplicates,
                            parse_constant=lambda _: require(False, 'nonfinite JSON number'))
    require(type(operations) is list and 0 < len(operations) <= 1000, 'finite nonempty operations list required')
    # Preflight batch syntax/declaration files before executing operations.
    # Prepare/tool/runtime-state checks are per operation, not a transaction:
    # a later failed operation does NOT undo an earlier explicit launch.
    prepared = []
    for op in operations:
        require(type(op) is dict, 'operation must be object')
        action = op.get('action')
        if action in ('release', 'accept', 'service'):
            fields(op, ('action', 'declaration') + (('requestId',) if action == 'service' else ()))
            d = load(absolute_file(op['declaration']))
            if action == 'service':
                name(op['requestId'])
            require(d['kind'] == ('Service' if action == 'service' else 'Task'), 'operation kind mismatch')
            require(d['spec']['node'] == node, 'declaration belongs to another node')
            require(d['kind'] != 'Service' or d['spec']['restart']['policy'] != 'always', 'always execution unsupported')
            prepared.append((op, d))
        elif action == 'rerun':
            fields(op, ('action', 'id', 'oldAttempt', 'declaration'))
            name(op['id'])
            name(op['oldAttempt'])
            d = load(absolute_file(op['declaration']))
            require(d['kind'] == 'Task' and d['metadata']['id'] == op['id'] and d['spec']['node'] == node, 'rerun declaration mismatch')
            prepared.append((op, d))
        elif action in ('start', 'observe', 'wait'):
            fields(op, ('action', 'id', 'attemptId'), ('seconds',) if action == 'wait' else ())
            name(op['id'])
            name(op['attemptId'])
            if action == 'wait':
                seconds = op.get('seconds', 10)
                require(type(seconds) in (int, float) and 0 <= seconds <= 3600, 'bounded wait required')
            prepared.append((op, None))
        elif action == 'tick':
            fields(op, ('action', 'id'))
            name(op['id'])
            prepared.append((op, None))
        elif action == 'control':
            fields(op, ('action', 'control'))
            Runtime.validate_control(op['control'])
            prepared.append((op, None))
        else:
            raise ValueError('unknown operation')
    results = []
    with Runtime(root, node) as runtime:
        for op, d in prepared:
            action = op['action']
            if action in ('release', 'accept'):
                value = runtime.accept_task(d, start=action == 'release')
            elif action == 'rerun':
                value = runtime.rerun_task(op['id'], op['oldAttempt'], d)
            elif action == 'service':
                value = runtime.submit_service(d, op['requestId'])
            elif action == 'tick':
                value = runtime.tick_service(op['id'])
            elif action == 'control':
                value = runtime.control(op['control'])
            elif action == 'start':
                value = runtime.start_task(op['id'], op['attemptId'])
            elif action == 'observe':
                value = runtime.observe_task(op['id'], op['attemptId'])
            else:
                value = runtime.wait_task(op['id'], op['attemptId'], op.get('seconds', 10))
            results.append(value)
    return results
