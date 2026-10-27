"""Atomic object-field edits for bounded specification repairs."""
import copy
import re

from architecture import fingerprint
from specification import SpecificationError


ROOTS = {'modules', 'internal_types', 'graph', 'verification', 'rule_owners'}


def apply_architecture_edits(base, response):
    return _apply_edits(base, response, ROOTS, 'Architecture')


def apply_system_edits(base, response):
    return _apply_edits(base, response, {'package', 'shared', 'system', 'execution'}, 'System', narrow=True)


def _apply_edits(base, response, roots, label, *, narrow=False):
    if not isinstance(response, dict) or set(response) != {'base_fingerprint', 'edits'}:
        raise SpecificationError(f'{label} repair requires base_fingerprint and edits only')
    if response['base_fingerprint'] != fingerprint(base):
        raise SpecificationError(f'{label} repair base_fingerprint does not match candidate')
    edits = response['edits']
    if not isinstance(edits, list) or not edits or len(edits) > 100:
        raise SpecificationError(f'{label} repair requires 1..100 explicit edits')
    result = copy.deepcopy(base)
    for edit in edits:
        if not isinstance(edit, dict) or edit.get('op') not in ('set', 'remove'):
            raise SpecificationError(f'{label} edit op must be set or remove')
        expected = {'op', 'path', 'value'} if edit['op'] == 'set' else {'op', 'path'}
        if set(edit) != expected:
            raise SpecificationError(f'{label} edit has missing or unknown fields')
        path = edit['path']
        if not isinstance(path, str) or not path.startswith('/') or re.search(r'~(?![01])', path):
            raise SpecificationError(f'{label} edit path must be an RFC 6901 pointer')
        parts = [p.replace('~1', '/').replace('~0', '~') for p in path[1:].split('/')]
        if parts[0] not in roots or (narrow and len(parts) < 2):
            raise SpecificationError(f'{label} edits may change only nested fields of its stage')
        parent = result
        for part in parts[:-1]:
            if not isinstance(parent, dict) or part not in parent:
                raise SpecificationError(f'{label} edit parent does not exist: {path}')
            parent = parent[part]
        if not isinstance(parent, dict):
            raise SpecificationError(f'{label} edits address object fields; replace lists as a whole')
        key = parts[-1]
        if edit['op'] == 'remove':
            if key not in parent:
                raise SpecificationError(f'{label} edit cannot remove absent field: {path}')
            del parent[key]
        else:
            if narrow and isinstance(parent.get(key), dict) and parent[key]:
                raise SpecificationError(f'{label} edit must address fields within an existing object: {path}')
            parent[key] = copy.deepcopy(edit['value'])
    return result
