"""Strict local communication only; no Runtime, locks or execution authority."""
import ctypes
import errno
import json
import os
from pathlib import Path
import stat
import uuid

from runtime.contract import fields, name, no_duplicates, require, validate
from runtime.prepare import canonical, digest, directory, no_links, sync_dir, write_new

API = 'rund-file/v0alpha1'
LIMIT = 65536


def private(path, *, folder=False):
    path = no_links(Path(path))
    st = path.stat()
    require(st.st_uid == os.getuid() and not st.st_mode & 0o077, 'private same-UID path required')
    require(stat.S_ISDIR(st.st_mode) if folder else stat.S_ISREG(st.st_mode) and st.st_nlink == 1,
            'private directory or single-link regular file required')
    return path


def read(path, limit=LIMIT):
    path = private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        require(stat.S_ISREG(st.st_mode) and st.st_nlink <= 1 and st.st_size <= limit,
                'non-regular, hardlinked or oversized input')
        data = f.read(limit + 1)
    require(len(data) <= limit, 'oversized input')
    return data


def decode(data):
    return json.loads(data, object_pairs_hook=no_duplicates,
                      parse_constant=lambda _: require(False, 'nonfinite JSON number'))


def outer_key(rid):
    name(rid)
    return 'fo-' + digest(rid.encode())[:48]


def inner_key(rid):
    return 'fi-' + outer_key(rid)[3:]


def target(e):
    if 'declaration' in e:
        d = e['declaration']
        return d['kind'], d['metadata']['id']
    return e['kind'], e['id']


def envelope(e, node):
    common = ('apiVersion', 'requestId', 'node', 'action')
    require(type(e) is dict, 'envelope object required')
    action = e.get('action')
    if action in ('accept', 'release', 'submit', 'rerun'):
        fields(e, common + ('declaration', 'declarationDigest') + (('id', 'oldAttempt') if action == 'rerun' else ()))
        d = validate(e['declaration'])
        require(d['kind'] == ('Service' if action == 'submit' else 'Task'), 'action kind mismatch')
        require(e['declarationDigest'] == digest(canonical(d)), 'declaration digest mismatch')
        require(d['spec']['node'] == node, 'declaration node mismatch')
        if d['kind'] == 'Task':
            require(not d['spec']['requestId'].startswith(('fo-', 'fi-')), 'reserved request namespace')
        if action == 'rerun':
            name(e['oldAttempt'])
            require(e['id'] == d['metadata']['id'], 'rerun id mismatch')
    elif action in ('start', 'observe', 'status', 'cancel', 'stop', 'restart'):
        fields(e, common + ('kind', 'id', 'attemptId') +
               (('expectedGeneration',) if action in ('cancel', 'stop', 'restart') else ()))
        require(e['kind'] in ('Task', 'Service'), 'invalid kind')
        name(e['id'])
        name(e['attemptId'])
        require(e['kind'] != 'Service' or e['attemptId'] == 'service', 'invalid service attempt')
        require(action not in ('start', 'observe', 'cancel') or e['kind'] == 'Task', 'Task action required')
        require(action not in ('stop', 'restart') or e['kind'] == 'Service', 'Service action required')
        if action in ('cancel', 'stop', 'restart'):
            require(type(e['expectedGeneration']) is int and e['expectedGeneration'] > 0, 'positive generation required')
    else:
        require(False, 'unknown action')
    require(e['apiVersion'] == API, 'unsupported file API')
    name(e['requestId'])
    name(e['node'])
    require(e['node'] == node, 'non-local node')
    return e


def disjoint(a, b):
    a, b = no_links(Path(a)), no_links(Path(b))
    require(a != b and a not in b.parents and b not in a.parents, 'protocol/runtime/source roots must be disjoint')


def binding(protocol):
    protocol = private(protocol, folder=True)
    value = decode(read(protocol / 'binding.json'))
    fields(value, ('apiVersion', 'node', 'runtimeRoot'))
    require(value['apiVersion'] == API, 'unknown binding')
    name(value['node'])
    disjoint(protocol, Path(value['runtimeRoot']))
    return value


def send(protocol, e):
    protocol = Path(protocol)
    b = binding(protocol)
    envelope(e, b['node'])
    data = canonical(e)
    require(len(data) <= LIMIT, 'oversized request')
    requests = private(protocol / 'requests', folder=True)
    destination = requests / (e['requestId'] + '.json')
    temp = requests / ('.tmp-' + uuid.uuid4().hex)
    write_new(temp, data)
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    rc = rename(-100, os.fsencode(temp), -100, os.fsencode(destination), 1)
    if rc:
        error = ctypes.get_errno()
        if error != errno.EEXIST:
            raise OSError(error, os.strerror(error))
        require(read(destination) == data, 'requestId file already published with different body')
        # Retain uncommitted temp, ignored forever; no deletion/GC in alpha.
    sync_dir(requests)
    return {'published': e['requestId'], 'digest': digest(data)}


def receipt(protocol, rid):
    b = binding(Path(protocol))
    path = Path(b['runtimeRoot']) / 'state/requests' / (outer_key(rid) + '.json')
    if path.exists():
        record = decode(read(path, 4 * 1024 * 1024))
        require(type(record) is dict and record.get('status') in ('pending', 'done'), 'damaged receipt status')
        body = record['body']
        require(body['operation'] == 'file-owner' and body['envelope']['requestId'] == rid, 'request key collision')
        envelope(body['envelope'], b['node'])
        require(record['digest'] == digest(canonical(body)), 'damaged receipt digest')
        require(record['status'] != 'done' or 'result' in record, 'done without result')
        return record
    rejected = Path(protocol) / 'rejections' / (rid + '.json')
    return decode(read(rejected)) if rejected.exists() else None


def snapshot(protocol):
    binding(Path(protocol))
    return decode(read(Path(protocol) / 'snapshot.json', 16 * 1024 * 1024))
