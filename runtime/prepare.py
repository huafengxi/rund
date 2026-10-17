"""File-only Git object preparation. No checkout, transport or payload execution.

The caller owns the node root and source repository; concurrent cooperating
preparers are supported, hostile same-uid writers racing filesystem checks are
not. Incomplete/unknown artifacts are retained, never automatically deleted.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import uuid

from .contract import require, repository, relative, validate

FORMAT = 'rund-prepare/v1alpha1-object-export-1'
LAYOUT = ('cache', 'code', 'manifests', 'work', 'state', 'protocol')
MAX_OBJECT = 32 * 1024 * 1024
MAX_FILES = 10000
MAX_BYTES = 128 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def no_links(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'absolute non-traversing path required')
    for p in reversed((path,) + tuple(path.parents)):
        if p.exists() or p.is_symlink():
            require(not p.is_symlink(), 'symlink path refused: ' + str(p))
    return path


def directory(path):
    no_links(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.is_dir(), 'directory required: ' + str(path))
    return path


@contextmanager
def lock(path):
    no_links(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_nlink == 1, 'private regular lock required')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_new(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, mode)


def read_regular(path):
    no_links(path)
    require(stat.S_ISREG(path.lstat().st_mode), 'regular file required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, 'regular non-hardlinked file required')
        return f.read()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def init_root(root):
    root = Path(root)
    require(root.is_absolute(), '--root must be absolute')
    no_links(root)
    # Only a new/empty root or our explicitly versioned root is accepted.
    if root.exists():
        require(root.is_dir(), 'root must be directory')
        entries = set(p.name for p in root.iterdir())
        require(not entries or '.rund-root.json' in entries or entries == {'.init.lock'}, 'refusing nonempty unmanaged root')
    directory(root)
    with lock(root / '.init.lock'):
        # A cooperating initializer may have created the marker but not written
        # it yet. Inspect its contents only under this stable initialization lock.
        marker = root / '.rund-root.json'
        if marker.exists() or marker.is_symlink():
            require(read_regular(marker) == canonical({'format': FORMAT}), 'unrecognized node root')
        else:
            require(set(p.name for p in root.iterdir()) == {'.init.lock'}, 'refusing nonempty unmanaged root')
            write_new(marker, canonical({'format': FORMAT}))
        for name in LAYOUT:
            directory(root / name)
        for name in ('locks', 'staging', 'git-home'):
            directory(root / 'state' / name)
    return root


def tool_identity(path):
    real = Path(path).resolve(strict=True)
    require(real.is_file() and os.access(real, os.X_OK), 'missing executable tool')
    return {'path': str(real), 'sha256': digest(real.read_bytes())}


class Git:
    def __init__(self, root):
        git = shutil.which('git')
        require(git is not None, 'missing git tool')
        self.identity = tool_identity(git)
        self.path = self.identity['path']
        self.root = root
        self.env = {'PATH': os.defpath, 'HOME': str(root / 'state/git-home'),
                    'XDG_CONFIG_HOME': str(root / 'state/git-home'), 'LC_ALL': 'C',
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_ATTR_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
                    'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_CONFIG_COUNT': '0'}
        self.identity['version'] = self.run(['--version']).decode().strip()

    def run(self, args, *, db=None, objects=None, data=None):
        env = dict(self.env)
        if db is not None:
            env['GIT_DIR'] = str(db)
        if objects is not None:
            env['GIT_OBJECT_DIRECTORY'] = str(objects)
        cmd = [self.path, '--no-replace-objects', '-c', 'core.hooksPath=/dev/null',
               '-c', 'core.attributesFile=/dev/null', '-c', 'protocol.allow=never'] + args
        p = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env, cwd=self.root, timeout=60)
        require(p.returncode == 0, 'git operation failed (' + args[0] + '): ' + p.stderr.decode(errors='replace')[:1000])
        return p.stdout

    def cache(self, source):
        key = digest(str(source).encode())
        db = self.root / 'cache' / key
        if not db.exists():
            staging = self.root / 'cache' / ('.' + key + '-' + uuid.uuid4().hex)
            directory(staging)
            self.run(['init', '--bare', '--template=', '--object-format=sha1', str(staging)])
            write_new(staging / 'rund-owner', canonical({'format': FORMAT, 'repository': source.as_uri()}))
            sync_dir(staging)
            os.rename(staging, db)
            sync_dir(db.parent)
        require(read_regular(db / 'rund-owner') == canonical({'format': FORMAT, 'repository': source.as_uri()}), 'unknown or incomplete cache (retained)')
        # Git is only run against a config we created, never a source config.
        expected = b'[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = true\n'
        require(read_regular(db / 'config') == expected, 'modified cache config refused')
        check_objects(db / 'objects')
        no_links(db / 'HEAD')
        return db

    def object(self, db, objects, oid, kind):
        require(len(oid) == 40 and all(c in '0123456789abcdef' for c in oid), 'invalid object id')
        actual = self.run(['cat-file', '-t', oid], db=db, objects=objects).strip().decode()
        require(actual == kind, 'pin/object is not a ' + kind)
        size = int(self.run(['cat-file', '-s', oid], db=db, objects=objects))
        require(0 <= size <= MAX_OBJECT, 'object too large')
        data = self.run(['cat-file', kind, oid], db=db, objects=objects)
        require(len(data) == size and hashlib.sha1(kind.encode() + b' ' + str(size).encode() + b'\0' + data).hexdigest() == oid, 'Git object identity mismatch')
        copied = self.run(['hash-object', '-w', '-t', kind, '--stdin'], db=db, data=data).strip().decode()
        require(copied == oid, 'cache object identity mismatch')
        # hash-object may reuse an already existing object. Do not let a
        # corrupt cache entry masquerade as a successfully imported object.
        cached = self.run(['cat-file', kind, oid], db=db)
        require(cached == data, 'cached object content mismatch (retained)')
        return data


def check_objects(objects):
    no_links(objects)
    require(objects.is_dir(), 'Git objects directory missing')
    for base, dirs, files in os.walk(objects, followlinks=False):
        for name in dirs + files:
            p = Path(base) / name
            require(not p.is_symlink(), 'symlink in Git object store refused')
            require(p.is_dir() or p.is_file(), 'special object-store entry refused')
    for name in ('alternates', 'http-alternates'):
        require(not (objects / 'info' / name).exists(), 'alternate object stores unsupported')


def source_objects(source, root):
    no_links(source)
    require(source.is_dir(), 'repository directory missing')
    require(root != source and root not in source.parents and source not in root.parents, 'source and node root must be disjoint')
    db = source / '.git' if (source / '.git').exists() or (source / '.git').is_symlink() else source
    no_links(db)
    require(db.is_dir(), 'gitdir indirection unsupported')
    objects = db / 'objects'
    check_objects(objects)
    return objects


def export(git, db, objects, revision, target):
    commit = git.object(db, objects, revision, 'commit')
    first = commit.split(b'\n', 1)[0]
    require(first.startswith(b'tree ') and len(first) == 45, 'invalid commit tree header')
    tree = first[5:].decode('ascii')
    inventory = {}
    total = 0

    def walk(oid, prefix, depth):
        nonlocal total
        require(depth <= 64, 'tree too deep')
        raw = git.object(db, objects, oid, 'tree')
        offset, names = 0, set()
        while offset < len(raw):
            space = raw.find(b' ', offset)
            nul = raw.find(b'\0', space + 1)
            require(space >= offset and nul > space and nul + 21 <= len(raw), 'invalid tree record')
            mode = raw[offset:space]
            name = raw[space+1:nul].decode('utf-8', errors='strict')
            require('/' not in name and name.casefold() not in names, 'conflicting tree entry')
            relative(name)
            require(name.casefold() != '.git', 'Git metadata path refused')
            names.add(name.casefold())
            child = raw[nul+1:nul+21].hex()
            offset = nul + 21
            rel = prefix + name
            require(len(inventory) < MAX_FILES, 'export entry budget exceeded')
            if mode == b'40000':
                directory(target / rel)
                inventory[rel] = {'object': child, 'gitMode': '40000'}
                walk(child, rel + '/', depth + 1)
            else:
                require(mode in (b'100644', b'100755'), 'unsupported tree mode (symlink/submodule/special)')
                require(name not in ('.gitmodules', '.lfsconfig'), 'submodule/LFS unsupported')
                content = git.object(db, objects, child, 'blob')
                require(not content.startswith(b'version https://git-lfs.github.com/spec/v1'), 'LFS pointer unsupported')
                total += len(content)
                require(total <= MAX_BYTES and len(inventory) < MAX_FILES, 'export budget exceeded')
                write_new(target / rel, content, 0o555 if mode == b'100755' else 0o444)
                inventory[rel] = {'object': child, 'gitMode': mode.decode(), 'sha256': digest(content)}
    walk(tree, '', 0)
    return inventory


def verify_tree(path, inventory, *, sealed=False):
    no_links(path)
    require(path.is_dir(), 'published source missing')
    seen = set()
    for base, dirs, files in os.walk(path, followlinks=False):
        if sealed:
            require(stat.S_IMODE(Path(base).stat().st_mode) == 0o555, 'published directory mode mismatch')
        for name in dirs:
            p = Path(base) / name
            require(not p.is_symlink(), 'published directory symlink refused')
            rel = p.relative_to(path).as_posix()
            require(rel in inventory and inventory[rel]['gitMode'] == '40000', 'unexpected published directory')
            seen.add(rel)
        for name in files:
            p = Path(base) / name
            rel = p.relative_to(path).as_posix()
            require(rel in inventory and inventory[rel]['gitMode'] in ('100644', '100755'), 'unexpected published file')
            record = inventory[rel]
            content = read_regular(p)
            require(digest(content) == record['sha256'] and hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest() == record['object'], 'published content mismatch')
            require(stat.S_IMODE(p.stat().st_mode) == (0o555 if record['gitMode'] == '100755' else 0o444), 'published mode mismatch')
            seen.add(rel)
    require(seen == set(inventory), 'published files missing')


def prepare(declaration, root):
    d = validate(declaration)
    root = init_root(root)
    git = Git(root)
    python = shutil.which(d['spec']['toolchain']['python'])
    require(python is not None, 'missing declared python tool')
    py_identity = tool_identity(python)
    require(Path(py_identity['path']) == Path(sys.executable).resolve(), 'only current Python interpreter supported')
    py_identity['version'] = sys.version
    inputs = {'format': FORMAT, 'declaration': d, 'tools': {'git': git.identity, 'python': py_identity},
              'platform': {'system': platform.system(), 'machine': platform.machine()}, 'locks': {}}
    # Repo locks in sorted canonical order avoid deadlocks across declarations.
    from contextlib import ExitStack
    with ExitStack() as stack:
        repos = {repository(s['repository']) for s in d['spec']['sources']}
        for repo in sorted(repos):
            stack.enter_context(lock(root / 'state/locks' / ('repo-' + digest(str(repo).encode()) + '.lock')))
        # Always verify the full pinned source objects before using a manifest.
        stage = root / 'state/staging' / uuid.uuid4().hex
        directory(stage)
        inventories = {}
        for src in d['spec']['sources']:
            repo = repository(src['repository'])
            objects = source_objects(repo, root)
            db = git.cache(repo)
            target = directory(stage / src['alias'])
            inv = export(git, db, objects, src['revision'], target)
            verify_tree(target, inv)
            inventories[src['alias']] = inv
            inputs['locks'][src['alias']] = {}
            for rel in src.get('lockFiles', []):
                require(rel in inv and inv[rel]['gitMode'] in ('100644', '100755'), 'lockFile must be a tracked regular file')
                inputs['locks'][src['alias']][rel] = inv[rel]['sha256']
        key = digest(canonical(inputs))
        manifest_path = root / 'manifests' / (key + '.json')
        with lock(root / 'state/locks' / ('prepare-' + key + '.lock')):
            if manifest_path.exists() or manifest_path.is_symlink():
                manifest = json.loads(read_regular(manifest_path))
                require(type(manifest) is dict, 'manifest must be a JSON object (retained)')
                require(manifest.get('format') == FORMAT and manifest.get('complete') is True and manifest.get('key') == key and manifest.get('inputs') == inputs and manifest.get('files') == inventories, 'incomplete or mismatched manifest (retained)')
                code_name = manifest.get('codeDirectory', '')
                require(type(code_name) is str and len(code_name) == 97 and code_name[:64] == key and code_name[64] == '-' and all(c in '0123456789abcdef' for c in code_name[65:]), 'unsafe code directory in manifest')
                code = root / 'code' / code_name
                no_links(code)
                require(stat.S_IMODE(code.stat().st_mode) == 0o555, 'published code directory mode mismatch')
                for alias, inv in inventories.items():
                    verify_tree(code / alias, inv, sealed=True)
                require(set(p.name for p in code.iterdir()) == set(inventories), 'unexpected published source alias')
                return {'key': key, 'cacheHit': True, 'manifest': str(manifest_path), 'code': str(code), 'work': str(root / 'work')}
            code_name = key + '-' + uuid.uuid4().hex
            code = root / 'code' / code_name
            for base, dirs, files in os.walk(stage, topdown=False):
                sync_dir(Path(base))
                if Path(base) != stage:
                    os.chmod(base, 0o555)
            # Moving a directory across parents may require write permission on
            # that directory to update '..'. Seal the top only after the move.
            os.rename(stage, code)
            os.chmod(code, 0o555)
            sync_dir(code)
            sync_dir(root / 'code')
            manifest = {'format': FORMAT, 'complete': True, 'key': key, 'inputs': inputs,
                        'files': inventories, 'codeDirectory': code_name}
            temp = root / 'manifests' / ('.' + uuid.uuid4().hex + '.tmp')
            write_new(temp, canonical(manifest))
            os.rename(temp, manifest_path)
            sync_dir(manifest_path.parent)
            return {'key': key, 'cacheHit': False, 'manifest': str(manifest_path), 'code': str(code), 'work': str(root / 'work')}
