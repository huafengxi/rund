"""Foreground-only bounded probe scratch; no GC, lease, scan or recursive delete.

The same stdlib-only implementation runs locally and as a remote Python program.
Roots are caller-provided, existing private real directories outside the synced
root, on its filesystem. Hostile same-UID mutation is outside this contract.
"""
import grp
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import uuid


class ProbeError(ValueError):
    pass


def validate_roots(root, data):
    root, data = Path(root), Path(data)
    if not root.is_absolute() or not data.is_absolute():
        raise ProbeError('absolute probe and data roots required')
    if str(root.resolve(strict=True)) != str(root):
        raise ProbeError('probe root must be a real path, not a symlink')
    actual_data = data.resolve(strict=True)
    if not root.is_dir() or not actual_data.is_dir():
        raise ProbeError('probe and data roots must be existing directories')
    if os.path.commonpath([str(root), str(actual_data)]) in (str(root), str(actual_data)):
        raise ProbeError('probe root and synced tree must be disjoint')
    st, target = root.stat(), actual_data.stat()
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise ProbeError('probe root must be owned by this uid with mode 0700')
    if st.st_dev != target.st_dev:
        raise ProbeError('probe and data roots must be on the same filesystem')
    return root


def anonymous_membership(data, gid):
    if not hasattr(os, 'O_TMPFILE'):
        raise OSError('foreground membership probe requires Linux O_TMPFILE')
    fd = os.open(data, os.O_TMPFILE | os.O_RDWR, 0o600)
    try:
        os.fchown(fd, -1, gid)
        st = os.fstat(fd)
        if (st.st_gid != gid or st.st_dev != os.stat(data).st_dev or
                st.st_nlink != 0 or not stat.S_ISREG(st.st_mode)):
            raise ProbeError('anonymous membership probe did not verify')
        return True
    finally:
        os.close(fd)


def membership(data, gid, scratch, report=None):
    """Check real chgrp without ever linking a probe in the synced tree.

    Darwin has no O_TMPFILE. Its same-filesystem private scratch verifies
    chgrp; effective write/search access to the data root is checked separately.
    The ordinary rsync pipeline still verifies receiver group marking.
    """
    if sys.platform != 'darwin':
        return anonymous_membership(data, gid)
    validate_roots(scratch, data)
    if not os.access(data, os.W_OK | os.X_OK, effective_ids=True):
        raise ProbeError('data root lacks effective write/search access')
    group = grp.getgrgid(gid).gr_name
    with Probe(scratch, data, report=report) as probe:
        result = probe.action('group', group)
        if result.get('group') != group:
            raise ProbeError('scratch membership group mismatch')
    return True


def _regular(path):
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
            st.st_uid != os.geteuid() or st.st_size != 32):
        raise ProbeError('refuse non-owned, linked, non-regular or unexpected-size probe file')
    return st


def perform(req):
    if set(req) != {'action','root','data','nonce','group'}:
        raise ProbeError('invalid probe request fields')
    action, nonce = req['action'], req['nonce']
    if action not in ('create','inspect','group','cleanup'):
        raise ProbeError('invalid probe action')
    if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
        raise ProbeError('invalid probe nonce')
    root = validate_roots(req['root'], req['data'])
    child = root / ('ssh-sync-' + nonce)
    if action == 'create':
        child.mkdir(mode=0o700)  # Exclusive creation, never adopt an old directory.
    st = child.lstat()
    if (not stat.S_ISDIR(st.st_mode) or child.resolve() != child or
            st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700):
        raise ProbeError('invalid private probe directory')
    probe = child / 'probe'
    result = {'ok':True, 'nonce':nonce, 'path':str(child)}
    if action == 'group':
        gid = grp.getgrnam(req['group']).gr_gid
        with probe.open('xb') as f:
            f.write(nonce.encode('ascii'))
            f.flush()
            os.fchown(f.fileno(), -1, gid)
        result['group'] = grp.getgrgid(_regular(probe).st_gid).gr_name
        if result['group'] != req['group']:
            raise ProbeError('group probe landed with the wrong group')
    elif action == 'inspect':
        result['group'] = grp.getgrgid(_regular(probe).st_gid).gr_name
        if probe.read_bytes() != nonce.encode('ascii'):
            raise ProbeError('transferred probe content mismatch')
    elif action == 'cleanup':
        # Validate ALL entries before unlinking any known file. Never recurse,
        # follow a symlink, unlink an unknown entry or delete a caller root.
        names = set(os.listdir(child))
        if names - {'probe'}:
            raise ProbeError('unknown scratch content retained')
        if 'probe' in names:
            _regular(probe)
            if probe.read_bytes() != nonce.encode('ascii'):
                raise ProbeError('unknown probe content retained')
            probe.unlink()
        child.rmdir()
    return result


class Probe:
    def __init__(self, root, data, call=None, report=None, nonce=None):
        self.req = {'root':str(Path(root)), 'data':str(Path(data)),
                    'nonce':nonce or uuid.uuid4().hex, 'group':None, 'action':'create'}
        self.path = Path(self.req['root']) / ('ssh-sync-' + self.req['nonce'])
        self.call = call or perform
        self.report = report or (lambda message: None)
        self.created = False

    def action(self, action, group=None):
        req = dict(self.req, action=action, group=group)
        reply = self.call(req)
        if (not isinstance(reply, dict) or reply.get('ok') is not True or
                reply.get('nonce') != req['nonce'] or reply.get('path') != str(self.path)):
            raise ProbeError('probe reply invalid/unconfirmed: ' + str(reply))
        return reply

    def __enter__(self):
        try:
            self.action('create')
        except BaseException:
            self.report('probe creation unconfirmed; no cleanup attempted: ' + str(self.path))
            raise
        self.created = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.created:
            try:
                self.action('cleanup')
                self.created = False
            except BaseException as cleanup:
                self.report('probe cleanup unconfirmed; may remain: ' + str(self.path) + ': ' + str(cleanup))
                if exc is None:
                    raise
        return False

    def write(self):
        if not self.created:
            raise ProbeError('probe directory not confirmed')
        with (self.path/'probe').open('xb') as f:
            f.write(self.req['nonce'].encode('ascii'))
        return self.path/'probe'


def remote_command(req):
    # Fixed, self-contained source. No deployment paths or arbitrary returned
    # stdout is turned into a delete command. The nonce path is caller-derived.
    return 'python3 -c ' + shlex.quote(Path(__file__).read_text()) + ' ' + shlex.quote(json.dumps(req))


if __name__ == '__main__':
    request = {}
    try:
        request = json.loads(sys.argv[1])
        print(json.dumps(perform(request)))
    except Exception as error:
        print(json.dumps({'ok':False, 'nonce':request.get('nonce') if isinstance(request,dict) else None,
                          'error':type(error).__name__ + ': ' + str(error)}))
        sys.exit(1)
