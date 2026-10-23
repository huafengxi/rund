"""Validation of ordinary fixed Git source plans; no execution objects."""
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
    require(not u.query and not u.fragment, 'repository query/fragment unsupported')
    if u.scheme in ('ssh', 'https'):
        require(bool(u.hostname) and bool(u.path) and u.path.startswith('/') and
                u.password is None and (u.scheme != 'https' or u.username is None),
                'remote repository must be absolute ssh:// or credential-free https:// URL')
        require(not any(c.isspace() or ord(c) < 32 for c in value) and
                '..' not in unquote(u.path).split('/'), 'unsafe remote repository')
        return value
    require(u.scheme == 'file' and not u.netloc, 'repository must be file:///, ssh:// or https://')
    path = Path(unquote(u.path))
    require(path.is_absolute() and path.as_uri() == value and '..' not in path.parts, 'repository must be canonical absolute file URI')
    return path


def validate(plan):
    fields(plan, ('sources',), ('format',))
    require(plan.get('format', 'git-sources/v1') == 'git-sources/v1', 'unsupported source plan format')
    require(type(plan['sources']) is list and 0 < len(plan['sources']) <= 64, 'sources must contain 1..64 entries')
    aliases = {}
    for src in plan['sources']:
        fields(src, ('alias', 'repository', 'revision', 'requires'), ('lockFiles', 'sshConfig'))
        if 'sshConfig' in src:
            text(src['sshConfig'])
            require(Path(src['sshConfig']).expanduser().is_absolute(), 'absolute SSH config reference required')
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
    return plan
