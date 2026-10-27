"""Deterministic review reference transport and strictly reference-only recovery.

The index binds IDs to current snapshot locations, never to inferred semantics.
No pointer guessing, finding filtering or verdict changes occur here.
"""
import copy
import hashlib

from architecture import fingerprint
from specification import SpecificationError, fields


def escape(value):
    return str(value).replace('~', '~0').replace('/', '~1')


def reference_index(snapshot):
    paths = set()
    def add(path):
        paths.add(path)
    def children(value, path):
        if isinstance(value, dict):
            for key in value:
                add(path + '/' + escape(key))
    package = snapshot.get('package', {})
    if 'package' in snapshot:
        add('/package')
    for group in ('decisions', 'assumptions', 'open_questions'):
        children(package.get(group), '/package/' + group)
    if 'request' in package.get('generation', {}):
        add('/package/generation/request')
    documents = snapshot.get('documents', {})
    shared = documents.get('shared', {})
    children(shared, '/documents/shared')
    for group in ('events', 'parameters', 'types', 'timing_rules'):
        children(shared.get(group), '/documents/shared/' + group)
    for section in ('system', 'execution'):
        for group in ('rules', 'verification'):
            children(documents.get(section, {}).get(group), '/documents/' + section + '/' + group)
    children(documents.get('modules', {}).get('contracts'), '/documents/modules/contracts')
    graph = documents.get('graph')
    if graph is not None:
        add('/documents/graph')
        children(graph, '/documents/graph')
    for group in ('events', 'observations', 'checks'):
        children(documents.get('verification', {}).get(group), '/documents/verification/' + group)
    if 'timing_library' in snapshot:
        add('/timing_library')
        children(snapshot['timing_library'], '/timing_library')
    for key in ('overrides', 'profile'):
        if key in snapshot:
            add('/' + key)
    targets = {}
    for path in sorted(paths):
        reference = 'R' + hashlib.sha256(path.encode('utf-8')).hexdigest()[:12]
        if reference in targets:
            raise SpecificationError('Review reference ID collision')
        targets[reference] = path
    return {'version': 1, 'snapshot_fingerprint': fingerprint(snapshot), 'targets': targets}


def reference_view(snapshot, index):
    """Lossless inline [reference_id, value] labels at selected semantic nodes."""
    if index != reference_index(snapshot):
        raise SpecificationError('Reference index belongs to another snapshot')
    by_path = {path: reference for reference, path in index['targets'].items()}
    def visit(value, path=''):
        if isinstance(value, dict):
            value = {k: visit(v, path + '/' + escape(k)) for k, v in value.items()}
        elif isinstance(value, list):
            value = [visit(v, path + '/' + str(i)) for i, v in enumerate(value)]
        return [by_path[path], value] if path in by_path else value
    return visit(snapshot)


def expand_reference_view(view, index):
    """Reconstruct using catalog locations, so source lists cannot mimic labels."""
    by_path = {path: reference for reference, path in index['targets'].items()}
    seen = set()
    def visit(value, path=''):
        if path in by_path:
            if not isinstance(value, list) or len(value) != 2 or value[0] != by_path[path]:
                raise SpecificationError('Malformed reference label at ' + path)
            seen.add(path)
            value = value[1]
        if isinstance(value, dict):
            return {k: visit(v, path + '/' + escape(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v, path + '/' + str(i)) for i, v in enumerate(value)]
        return value
    result = visit(view)
    if seen != set(by_path) or fingerprint(result) != index['snapshot_fingerprint']:
        raise SpecificationError('Reference view differs from its snapshot')
    return result


def compact_change_locations(context, index):
    result = copy.deepcopy(context)
    prefixes = sorted(((path, ref) for ref, path in index['targets'].items()),
                      key=lambda pair: (-len(pair[0]), pair[0]))
    for row in result['changes']:
        if not isinstance(row, list) or len(row) < 2 or not isinstance(row[1], str):
            continue
        path = row[1]
        for prefix, ref in prefixes:
            if path == prefix or path.startswith(prefix + '/'):
                location = [ref, path[len(prefix):]]
                # Compression is optional; never enlarge short paths.
                if len(str(location)) < len(path):
                    row[1] = location
                break
    result['change_format'] = 'snapshot_after_refs_v1'
    return result


def reference_slots(verdict):
    """Yield only reference array entries; malformed shapes remain validator work."""
    if not isinstance(verdict, dict):
        return
    for group in ('findings', 'repair_assessments', 'finding_assessments'):
        entries = verdict.get(group)
        if isinstance(entries, list):
            for i, entry in enumerate(entries):
                if isinstance(entry, dict) and isinstance(entry.get('references'), list):
                    for j, value in enumerate(entry['references']):
                        yield f'/{group}/{i}/references/{j}', value
    preservation = verdict.get('preservation_assessment')
    if isinstance(preservation, dict) and isinstance(preservation.get('references'), list):
        for j, value in enumerate(preservation['references']):
            yield f'/preservation_assessment/references/{j}', value


def _set_reference(verdict, slot, value):
    parts = slot[1:].split('/')
    node = verdict
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    node[int(parts[-1])] = value


def canonicalize_references(verdict, index):
    result = copy.deepcopy(verdict)
    for slot, reference in reference_slots(result):
        if isinstance(reference, str) and reference in index['targets']:
            _set_reference(result, slot, index['targets'][reference])
    return result


def apply_reference_corrections(verdict, issues, response, index):
    fields(response, ('corrections',), (), 'reference correction')
    corrections = response['corrections']
    expected = {issue['slot'] for issue in issues}
    if not isinstance(corrections, dict) or set(corrections) != expected or not expected:
        raise SpecificationError('Correct exactly the invalid reference entries; no additions or omissions')
    result = copy.deepcopy(verdict)
    for slot, reference in corrections.items():
        if not isinstance(reference, str) or reference not in index['targets']:
            raise SpecificationError('Reference correction must select a supplied reference ID')
        _set_reference(result, slot, index['targets'][reference])
    # Verify that *only* the selected entries changed. This includes all verdict
    # statuses, findings, explanations, scope, links and reference-list lengths.
    restored = copy.deepcopy(result)
    original = dict(reference_slots(verdict))
    for slot in expected:
        _set_reference(restored, slot, original[slot])
    if restored != verdict:
        raise SpecificationError('Reference correction changed substantive review content')
    return result
