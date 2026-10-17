"""Strict, deliberately small rund/v1alpha1 JSON experiment (no execution)."""
import json
import re
from urllib.parse import urlsplit, unquote
from pathlib import Path


class ContractError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ContractError(message)


def fields(value, required, optional=()):
    require(type(value) is dict, 'expected object')
    require(set(required) <= value.keys(), 'missing fields: ' + str(set(required) - value.keys()))
    require(value.keys() <= set(required) | set(optional), 'unknown fields: ' + str(value.keys() - set(required) - set(optional)))


def text(value):
    require(type(value) is str and bool(value) and '\x00' not in value, 'expected nonempty NUL-free string')


def name(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value) is not None, 'invalid identifier')


def relative(value):
    text(value)
    require(not value.startswith('/') and all(p not in ('', '.', '..', '.git') for p in value.split('/')) and '\\' not in value, 'unsafe relative path')
    return value


def repository(value):
    text(value)
    u = urlsplit(value)
    require(u.scheme == 'file' and not u.netloc and not u.query and not u.fragment, 'only local file:/// repositories supported')
    path = Path(unquote(u.path))
    require(path.is_absolute() and path.as_uri() == value and '..' not in path.parts, 'repository must be canonical absolute file URI')
    return path


def no_duplicates(pairs):
    result = {}
    for k, v in pairs:
        require(k not in result, 'duplicate JSON field: ' + k)
        result[k] = v
    return result


def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=no_duplicates,
                          parse_constant=lambda _: require(False, 'nonfinite JSON number'))
    except (UnicodeError, json.JSONDecodeError) as e:
        raise ContractError('invalid JSON: ' + str(e)) from e
    return validate(data)


def validate(d):
    fields(d, ('apiVersion', 'kind', 'metadata', 'spec'))
    require(d['apiVersion'] == 'rund/v1alpha1', 'unsupported apiVersion')
    require(d['kind'] in ('Service', 'Task'), 'unsupported kind')
    fields(d['metadata'], ('id', 'version'))
    name(d['metadata']['id'])
    require(type(d['metadata']['version']) is int and d['metadata']['version'] > 0, 'version must be positive integer')
    common = ('node', 'sources', 'prepare', 'toolchain', 'argv', 'cwd', 'env', 'credentialRefs')
    special = ('desired', 'restart') if d['kind'] == 'Service' else ('attemptId', 'requestId')
    s = d['spec']
    fields(s, common + special)
    name(s['node'])
    require(s['prepare'] == [] and type(s['prepare']) is list, 'nonempty prepare unsupported')
    require(s['credentialRefs'] == [] and type(s['credentialRefs']) is list, 'credential loading unsupported')
    fields(s['toolchain'], ('python',))
    text(s['toolchain']['python'])
    require(s['cwd'] == '${work}', 'only independent ${work} cwd supported')
    require(type(s['argv']) is list and bool(s['argv']), 'argv must be nonempty list')
    for item in s['argv']:
        text(item)
    require(type(s['env']) is dict, 'env must be object')
    for k, v in s['env'].items():
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) is not None, 'invalid env key')
        require(type(v) is str and '\x00' not in v, 'invalid env value')
    require(type(s['sources']) is list and 0 < len(s['sources']) <= 64, 'sources must contain 1..64 entries')
    aliases = {}
    for src in s['sources']:
        fields(src, ('alias', 'repository', 'revision', 'requires'), ('lockFiles',))
        name(src['alias'])
        require(src['alias'].casefold() not in aliases, 'conflicting source alias')
        aliases[src['alias'].casefold()] = src
        repository(src['repository'])
        require(type(src['revision']) is str and re.fullmatch('[0-9a-f]{40}', src['revision']) is not None, 'revision must be full lowercase 40hex commit pin')
        require(type(src['requires']) is list, 'requires must be list')
        for dep in src['requires']:
            name(dep)
        require(len(set(src['requires'])) == len(src['requires']), 'duplicate requires')
        locks = src.get('lockFiles', [])
        require(type(locks) is list, 'lockFiles must be list')
        for lock in locks:
            relative(lock)
        require(len(set(locks)) == len(locks), 'duplicate lockFiles')
    by_name = {v['alias']: v for v in aliases.values()}
    visited, visiting = set(), set()
    def visit(alias):
        require(alias in by_name, 'unknown dependency alias')
        require(alias not in visiting, 'source dependency cycle')
        if alias in visited:
            return
        visiting.add(alias)
        for dep in by_name[alias]['requires']:
            visit(dep)
        visiting.remove(alias)
        visited.add(alias)
    for alias in by_name:
        visit(alias)
    # Placeholders are validated but never expanded/executed by prepare.
    for item in s['argv'] + list(s['env'].values()):
        for token in re.findall(r'\$\{([^}]*)\}', item):
            require(token == 'work' or token.startswith('code.') and token[5:] in by_name, 'unknown placeholder')
        require('${' not in re.sub(r'\$\{[^}]*\}', '', item), 'unclosed placeholder')
    if d['kind'] == 'Task':
        name(s['attemptId'])
        name(s['requestId'])
    else:
        require(s['desired'] in ('online', 'offline'), 'invalid desired')
        r = s['restart']
        fields(r, ('policy', 'initialSeconds', 'maxSeconds'))
        require(r['policy'] in ('manual', 'on-failure', 'always'), 'invalid restart policy')
        require(all(type(r[k]) in (int, float) and 0 <= r[k] <= 86400 for k in ('initialSeconds', 'maxSeconds')) and r['initialSeconds'] <= r['maxSeconds'], 'invalid bounded backoff')
    return d
