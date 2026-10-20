"""Complete controller-owned desired snapshots; nodes never write the input.

One checksummed JSON object is the activation unit. Transport may be delayed or
interrupted. Only the Owner persists acceptance in its PRIVATE root. A missing
entry means offline (retained tombstone), not permission to forget a live load.
"""
import copy
from pathlib import Path
from .contract import fields, name, require, validate
from .prepare import canonical, digest, read_regular
from .kernel import publish


def envelope(payload):
    return dict(apiVersion='rund-registry/v1', sha256=digest(canonical(payload)), payload=payload)


def check(value, node):
    fields(value, ('apiVersion', 'sha256', 'payload'))
    require(value['apiVersion'] == 'rund-registry/v1', 'unsupported registry snapshot')
    p = value['payload']
    require(value['sha256'] == digest(canonical(p)), 'registry snapshot digest mismatch')
    fields(p, ('node', 'generation', 'services'))
    require(p['node'] == node, 'registry snapshot belongs to another node')
    require(type(p['generation']) is int and p['generation'] > 0, 'positive registry generation required')
    require(type(p['services']) is list, 'registry services must be list')
    seen = set()
    for item in p['services']:
        fields(item, ('name', 'declaration'), ('status', 'external', 'stop_cmd'))
        name(item['name'])
        require(item['name'] not in seen, 'duplicate service in snapshot')
        seen.add(item['name'])
        d = validate(item['declaration'])
        require(d['kind'] == 'Service' and d['spec']['node'] == node and
                d['metadata']['id'] == 'svc-' + item['name'], 'service snapshot identity mismatch')
        require(type(item.get('external', False)) is bool, 'external must be bool')
        # Probe/official-manager fields reuse the existing registry validator.
        from .registry import validate_profile
        profile = dict(version=d['metadata']['version'], cmd=d['spec']['argv'])
        for key in ('status', 'stop_cmd'):
            if key in item: profile[key] = item[key]
        validate_profile(profile)
    return value


def load(path, node):
    import json
    from .contract import no_duplicates
    return check(json.loads(read_regular(Path(path)), object_pairs_hook=no_duplicates), node)


def descriptors(records):
    result = []
    for record in records:
        d = record['declaration']
        svc = dict(name=record['name'], version=d['metadata']['version'],
                   cmd=d['spec']['argv'], desired=d['spec']['desired'], _declaration=d,
                   external=record.get('external', False))
        for key in ('status', 'stop_cmd'):
            if key in record: svc[key] = record[key]
        result.append(svc)
    return result


def selected(c, *, accept=False):
    """Return effective records and input diagnostics, preserving last good input."""
    accepted_path = Path(c['root']) / 'registry-accepted.json'
    import json
    previous = json.loads(read_regular(accepted_path)) if accepted_path.exists() else None
    old = check(previous['snapshot'], c['node']) if previous else None
    records = {r['name']: copy.deepcopy(r) for r in c.get('seedServices', [])}
    error = None
    latest = old
    try:
        candidate = load(c['registryFile'], c['node'])
        if old:
            require(candidate['payload']['generation'] >= old['payload']['generation'], 'registry generation rollback refused')
            if candidate['payload']['generation'] == old['payload']['generation']:
                require(candidate['sha256'] == previous['inputDigest'], 'registry generation reused with different content')
        incoming = {r['name']: copy.deepcopy(r) for r in candidate['payload']['services']}
        baseline = dict(records)
        if old:baseline.update({r['name']: r for r in old['payload']['services']})
        for key, record in incoming.items():
            if key not in baseline:continue
            before = copy.deepcopy(baseline[key])
            after = copy.deepcopy(record)
            before['declaration']['spec']['desired'] = 'online'
            after['declaration']['spec']['desired'] = 'online'
            old_version = before['declaration']['metadata']['version']
            new_version = after['declaration']['metadata']['version']
            require(new_version >= old_version, 'service version rollback refused: ' + key)
            require(before == after or new_version > old_version, 'changed service requires version bump: ' + key)
        if old:
            for r in old['payload']['services']:
                if r['name'] not in incoming:
                    retained = copy.deepcopy(r)
                    if r['name'] not in records:
                        retained['declaration']['spec']['desired'] = 'offline'
                    incoming[r['name']] = retained
        latest = envelope(dict(candidate['payload'], services=list(incoming.values())))
        accepted = dict(inputDigest=candidate['sha256'], snapshot=latest)
        if accept and accepted != previous:
            # Persist input identity and effective tombstones in ONE atomic file.
            publish(accepted_path, accepted)
    except (OSError, ValueError, KeyError, TypeError) as e:
        error = 'desired registry: ' + str(e)
    if latest:
        records.update({r['name']: copy.deepcopy(r) for r in latest['payload']['services']})
    return descriptors(list(records.values())), error
