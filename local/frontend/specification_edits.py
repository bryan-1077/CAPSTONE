"""Atomic object-field edits for bounded specification repairs."""
import copy
import itertools
import re

from architecture import fingerprint
from specification import SpecificationError


ROOTS = {'modules', 'internal_types', 'graph', 'verification', 'rule_owners'}


class SpecificationEditError(SpecificationError):
    """Retain the exact rejected operation for feedback and repeat detection."""
    def __init__(self, message, edit):
        super().__init__(message)
        self.rejected_edit = copy.deepcopy(edit)


def _field_paths(value, path):
    for name in sorted(value):
        child = path + '/' + name.replace('~', '~0').replace('/', '~1')
        if isinstance(value[name], dict) and value[name]:
            yield from _field_paths(value[name], child)
        else:
            yield child


def apply_architecture_edits(base, response, *, narrow=False):
    """Semantic repairs edit fields inside the accepted architecture.

    Ordinary compiler repairs retain their existing broader shape-repair mode.
    """
    return _apply_edits(base, response, ROOTS, 'Architecture', narrow=narrow,
                        allow_object_removal=not narrow)


def apply_system_edits(base, response):
    return _apply_edits(base, response, {'package', 'shared', 'system', 'execution'}, 'System', narrow=True)


def _apply_edits(base, response, roots, label, *, narrow=False, allow_object_removal=True):
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
        if (narrow and isinstance(parent.get(key), dict) and parent[key]
                and (edit['op'] == 'set' or not allow_object_removal)):
            children = list(itertools.islice(_field_paths(parent[key], path), 8))
            raise SpecificationEditError(
                f'{label} edit must address fields within an existing object: {path}. '
                f'Edit individual fields instead, for example: {", ".join(children)}. '
                'For an endpoint binding correction, set its /endpoint field and preserve other fields.', edit)
        if edit['op'] == 'remove':
            if key not in parent:
                raise SpecificationError(f'{label} edit cannot remove absent field: {path}')
            del parent[key]
        else:
            parent[key] = copy.deepcopy(edit['value'])
    return result
