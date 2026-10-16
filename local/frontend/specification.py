"""Check a system-specification package without generating IR or RTL.

Structured references, widths, ownership and wiring are checked mechanically.
Event conditions and behavioral prose remain review/test obligations.
"""
from __future__ import annotations

import argparse
import ast
import copy
import itertools
import json
from pathlib import Path

import yaml

from architecture_dependencies import dependency_cycle


class SpecificationError(ValueError):
    pass


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, (str, int)) or key in result:
            raise SpecificationError(f'Duplicate or invalid YAML key: {key!r}')
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueLoader.add_constructor('tag:yaml.org,2002:map', _unique_mapping)


def read_document(path):
    try:
        data = yaml.load(Path(path).read_text(), Loader=_UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise SpecificationError(f'{path}: {exc}') from exc
    return fields(data, (), None, str(path))


def fields(value, required, optional, label):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise SpecificationError(f'{label}: expected a mapping with string keys')
    missing = set(required) - value.keys()
    unknown = set() if optional is None else value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise SpecificationError(f'{label}: missing fields {sorted(missing)}, unknown fields {sorted(unknown)}')
    return value


def text_value(value, label):
    if not isinstance(value, str) or not value.strip():
        raise SpecificationError(f'{label}: expected nonempty text')


def names(value, label):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value) or len(set(value)) != len(value):
        raise SpecificationError(f'{label}: expected a list of unique names')
    return value


def validate_clock_reset(reset):
    """One shared reset contract, with a canonical style and legacy alias."""
    fields(reset, ('clock', 'edge', 'reset', 'active_level', 'priority',
                  'assertion_style', 'handshake_gating'), (), 'clock_reset')
    expected = {'edge': 'rising', 'active_level': 0, 'priority': 'reset_before_all_events'}
    for name, value in expected.items():
        if reset[name] != value or type(reset[name]) is not type(value):
            raise SpecificationError(f'clock_reset.{name}: expected {value!r}, received {reset[name]!r}')
    styles = ('synchronous', 'synchronous_for_all_stateful_modules_in_this_example')
    if reset['assertion_style'] not in styles:
        raise SpecificationError(
            f'clock_reset.assertion_style: expected "synchronous" '
            f'(legacy alias {styles[1]!r} is also accepted), received {reset["assertion_style"]!r}. '
            'Use a supported style if the request permits it; otherwise identify the actual unsupported requirement.')
    for name in ('clock', 'reset', 'handshake_gating'):
        text_value(reset[name], 'clock_reset.' + name)
    if reset['clock'] == reset['reset']:
        raise SpecificationError('Clock and reset names must differ')


def expression(source, parameters):
    """Only the small parameter/presence vocabulary, never Python eval."""
    def visit(node):
        if isinstance(node, ast.Name) and node.id in parameters:
            return parameters[node.id]
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return node.value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'ceil_log2' and len(node.args) == 1 and not node.keywords:
            value = visit(node.args[0])
            if type(value) is int and value > 0:
                return (value - 1).bit_length()
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            base, power = visit(node.left), visit(node.right)
            if type(base) is int and type(power) is int and base == 2 and 0 <= power <= 20:
                return base ** power
        if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1 and isinstance(node.ops[0], ast.Gt):
            left, right = visit(node.left), visit(node.comparators[0])
            if type(left) is type(right) is int:
                return left > right
        raise SpecificationError(f'Unsupported or unresolved parameter expression: {source!r}')
    if not isinstance(source, str):
        raise SpecificationError('Parameter expressions must be strings')
    try:
        return visit(ast.parse(source, mode='eval').body)
    except (SyntaxError, RecursionError) as exc:
        raise SpecificationError(f'Invalid parameter expression: {source!r}') from exc


def elaborate_parameters(definitions, overrides):
    fields(definitions, (), None, 'parameters')
    if set(overrides) - definitions.keys():
        raise SpecificationError(f'Unknown parameter overrides: {sorted(set(overrides) - definitions.keys())}')
    values, pending = {}, {}
    for name, spec in definitions.items():
        fields(spec, (), ('value', 'derive', 'allowed', 'minimum', 'configurable', 'unit', 'note'), name)
        if ('value' in spec) == ('derive' in spec):
            raise SpecificationError(f'{name}: specify exactly one of value or derive')
        if 'configurable' in spec and type(spec['configurable']) is not bool:
            raise SpecificationError(f'{name}: configurable must be boolean')
        if 'allowed' in spec and (not isinstance(spec['allowed'], list) or not spec['allowed']):
            raise SpecificationError(f'{name}: allowed must be a nonempty list')
        if 'minimum' in spec and type(spec['minimum']) is not int:
            raise SpecificationError(f'{name}: minimum must be an integer')
        if name in overrides and (spec.get('configurable') is False or not (spec.get('configurable') or 'allowed' in spec)):
            raise SpecificationError(f'{name}: parameter is not configurable')
        if 'derive' in spec:
            if name in overrides:
                raise SpecificationError(f'{name}: derived parameter cannot be overridden')
            pending[name] = spec['derive']
        else:
            value = overrides.get(name, spec['value'])
            if type(value) not in (int, str) or ('allowed' in spec and value not in spec['allowed']):
                raise SpecificationError(f'{name}: invalid parameter value {value!r}')
            if 'minimum' in spec and (type(value) is not int or value < spec['minimum']):
                raise SpecificationError(f'{name}: value is below minimum {spec["minimum"]}')
            values[name] = value
    while pending:
        resolved = {}
        for name, source in pending.items():
            # Defer only missing parameter names; unsupported syntax fails now.
            try:
                tree = ast.parse(source, mode='eval')
            except (SyntaxError, TypeError) as exc:
                raise SpecificationError(f'{name}: invalid derivation') from exc
            refs = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - {'ceil_log2'}
            if refs <= values.keys():
                resolved[name] = expression(source, values)
        if not resolved:
            raise SpecificationError(f'Unresolved or cyclic parameter derivations: {sorted(pending)}')
        for name, value in resolved.items():
            spec = definitions[name]
            if type(value) is not int or value < 0 or ('minimum' in spec and value < spec['minimum']) or ('allowed' in spec and value not in spec['allowed']):
                raise SpecificationError(f'{name}: invalid derived parameter value {value!r}')
        values.update(resolved)
        pending = {k: v for k, v in pending.items() if k not in resolved}
    return values


def type_widths(types, parameters):
    widths, active = {}, set()

    def size(value):
        if isinstance(value, str) and value.startswith('shared.parameters.'):
            name = value.removeprefix('shared.parameters.')
            if name not in parameters:
                raise SpecificationError(f'Unknown parameter reference: {value}')
            value = parameters[name]
        if type(value) is not int or value < 0:
            raise SpecificationError(f'Invalid width/count: {value!r}')
        return value

    def resolve(ref):
        if not isinstance(ref, str) or not ref.startswith('shared.types.') or ref.removeprefix('shared.types.') not in types:
            raise SpecificationError(f'Unknown type reference: {ref!r}')
        name = ref.removeprefix('shared.types.')
        if name in widths:
            return widths[name]
        if name in active:
            raise SpecificationError(f'Cyclic type reference: {ref}')
        active.add(name)
        spec = fields(types[name], ('kind',), ('width', 'encodings', 'reserved', 'element_type', 'count', 'packed_order', 'fields', 'mapping', 'semantics', 'legal_values'), ref)
        kind = spec['kind']
        required = {'unsigned': ('width',), 'enum': ('width', 'encodings', 'reserved'),
                    'record': ('fields', 'packed_order'), 'array': ('element_type', 'count', 'packed_order')}
        if not isinstance(kind, str) or kind not in required:
            raise SpecificationError(f'{ref}: unsupported type kind {kind!r}')
        fields(spec, ('kind',) + required[kind], ('mapping', 'semantics', 'legal_values'), ref)
        if kind in ('unsigned', 'enum'):
            width = size(spec['width'])
            if kind == 'enum':
                encodings = fields(spec['encodings'], (), None, ref + '.encodings')
                reserved = spec['reserved']
                if not isinstance(reserved, list):
                    raise SpecificationError(f'{ref}: reserved must be a list')
                codes = list(encodings.values()) + reserved
                if width < 1 or any(type(c) is not int or c < 0 or c.bit_length() > width for c in codes) or len(set(codes)) != len(codes):
                    raise SpecificationError(f'{ref}: duplicate or out-of-range enum encoding')
        elif kind == 'array':
            if spec['packed_order'] != 'ascending_index_from_lsb' or size(spec['count']) < 1:
                raise SpecificationError(f'{ref}: unsupported array packing or empty array')
            width = resolve(spec['element_type']) * size(spec['count'])
        else:
            if spec['packed_order'] != 'msb_to_lsb' or not isinstance(spec['fields'], list) or not spec['fields']:
                raise SpecificationError(f'{ref}: unsupported record packing or empty record')
            seen, width = set(), 0
            for field in spec['fields']:
                fields(field, ('name', 'type'), (), ref)
                text_value(field['name'], ref + '.field.name')
                if field['name'] in seen:
                    raise SpecificationError(f'{ref}: duplicate record field')
                seen.add(field['name'])
                field_width = resolve(field['type'])
                if field_width < 1:
                    raise SpecificationError(f'{ref}: zero-width record field')
                width += field_width
        active.remove(name)
        widths[name] = width
        return width

    for name in types:
        resolve('shared.types.' + name)
    return widths


def elaborate_type_layouts(types, parameters, widths):
    """Resolve shared packed layouts once for RTL generation and verification."""
    result = copy.deepcopy(types)
    for name, spec in result.items():
        spec['width'] = widths[name]
        if spec['kind'] == 'record':
            remaining = widths[name]
            for field in spec['fields']:
                width = widths[field['type'].removeprefix('shared.types.')]
                field.update(width=width, msb=remaining - 1, lsb=remaining - width)
                remaining -= width
        elif spec['kind'] == 'array':
            count = spec['count']
            spec['count'] = parameters[count.removeprefix('shared.parameters.')] if isinstance(count, str) else count
            spec['element_width'] = widths[spec['element_type'].removeprefix('shared.types.')]
            spec['index_lsb'] = 'index * element_width'
    return result


def validate_ownership(documents):
    contracts, rules, obligations = documents['modules']['contracts'], {}, {}
    contract_fields = ('status', 'role', 'owned_state', 'interfaces', 'handshakes', 'reset', 'update_events', 'stalls', 'simultaneous_events', 'invariants', 'verification', 'combinational_dependencies')
    for name, contract in contracts.items():
        fields(contract, contract_fields, ('dependency_note',), name)
        for field in ('owned_state', 'handshakes', 'update_events', 'simultaneous_events'):
            fields(contract[field], (), None, name + '.' + field)
        for field in ('status', 'role', 'reset', 'stalls'):
            text_value(contract[field], name + '.' + field)
        names(contract['invariants'], name + '.invariants')
        names(contract['verification'], name + '.verification')
        for event in contract['update_events']:
            if event != 'every_clock' and event not in {'shared.events.' + e for e in documents['shared']['events']}:
                raise SpecificationError(f'{name}: unknown update event {event}')
    for slice_name in ('system', 'execution'):
        doc = documents[slice_name]
        fields(doc, ('slice_id', 'rules', 'verification'), (), slice_name)
        for rid, rule in fields(doc['rules'], (), None, slice_name + '.rules').items():
            ref = f'{slice_name}.rules.{rid}'
            fields(rule, ('origin', 'owners', 'statement', 'verification'), (), ref)
            text_value(rule['origin'], ref + '.origin')
            text_value(rule['statement'], ref + '.statement')
            if not names(rule['owners'], ref) or set(rule['owners']) - contracts.keys():
                raise SpecificationError(f'{ref}: missing or unknown rule owners')
            if not names(rule['verification'], ref):
                raise SpecificationError(f'{ref}: missing verification obligations')
            rules[ref] = rule
        for vid, obligation in fields(doc['verification'], (), None, slice_name + '.verification').items():
            ref = f'{slice_name}.verification.{vid}'
            fields(obligation, ('rules', 'scenario', 'expected', 'status'), (), ref)
            for field in ('scenario', 'expected', 'status'):
                text_value(obligation[field], ref + '.' + field)
            if not names(obligation['rules'], ref):
                raise SpecificationError(f'{ref}: missing rule references')
            obligations[ref] = obligation
    for ref, rule in rules.items():
        prefix, _, rid = ref.split('.')
        for vid in rule['verification']:
            verification_ref = f'{prefix}.verification.{vid}'
            if verification_ref not in obligations or rid not in obligations[verification_ref]['rules']:
                raise SpecificationError(f'{ref}: nonreciprocal verification reference {vid}')
            for owner in rule['owners']:
                if ref not in contracts[owner]['invariants'] or verification_ref not in contracts[owner]['verification']:
                    raise SpecificationError(f'{ref}: owner {owner} lacks invariant/verification obligation')
    for ref, obligation in obligations.items():
        prefix, _, vid = ref.split('.')
        for rid in obligation['rules']:
            rule = rules.get(f'{prefix}.rules.{rid}')
            if rule is None or vid not in rule['verification']:
                raise SpecificationError(f'{ref}: nonreciprocal rule reference {rid}')
    for name, contract in contracts.items():
        for ref in contract['invariants']:
            if ref not in rules or name not in rules[ref]['owners']:
                raise SpecificationError(f'{name}: unknown or unowned invariant {ref}')
        for ref in contract['verification']:
            if ref not in obligations:
                raise SpecificationError(f'{name}: unknown verification reference {ref}')
            prefix = ref.split('.')[0]
            if not any(name in rules[f'{prefix}.rules.{rid}']['owners'] for rid in obligations[ref]['rules']):
                raise SpecificationError(f'{name}: unowned verification obligation {ref}')
    return len(rules), len(obligations)


def validate_graph(documents, parameters, widths):
    graph, shared = documents['graph'], documents['shared']
    contracts = documents['modules']['contracts']
    fields(graph, ('graph_id', 'clock_reset', 'connections', 'presence', 'integration'), (), 'graph')
    reset = shared['clock_reset']
    validate_clock_reset(reset)
    wiring = fields(graph['clock_reset'], ('definition', 'clock_source', 'reset_source', 'consumers'), (), 'graph.clock_reset')
    if wiring['definition'] != 'shared.clock_reset' or wiring['clock_source'] != 'top.' + reset['clock'] or wiring['reset_source'] != 'top.' + reset['reset']:
        raise SpecificationError('Clock/reset source differs from shared definition')
    if set(names(wiring['consumers'], 'clock/reset consumers')) != set(contracts):
        raise SpecificationError('Clock/reset consumers must cover every contract')
    endpoints, absent, modules = {}, {}, {}
    tables = {'top': shared['host_interface'], **{n: c['interfaces'] for n, c in contracts.items()}}
    for instance, ports in tables.items():
        fields(ports, (), None, instance + '.interfaces')
        for name, port in ports.items():
            endpoint = instance + '.' + name
            fields(port, ('direction', 'type'), ('when',), endpoint)
            if port['direction'] not in ('input', 'output'):
                raise SpecificationError(f'{endpoint}: invalid direction')
            ref = port['type']
            if not isinstance(ref, str) or not ref.startswith('shared.types.') or ref.removeprefix('shared.types.') not in widths:
                raise SpecificationError(f'{endpoint}: unknown type reference {ref!r}')
            if 'when' in port:
                absent[endpoint] = port['when']
                presence = expression(port['when'], parameters)
                if type(presence) is not bool:
                    raise SpecificationError(f'{endpoint}: presence must be a comparison')
                if not presence:
                    continue
            width = widths[ref.removeprefix('shared.types.')]
            if width < 1:
                raise SpecificationError(f'{endpoint}: present port has zero width')
            endpoints[endpoint] = dict(port, width=width, source=(port['direction'] == ('input' if instance == 'top' else 'output')))
    if graph['presence'] != absent:
        raise SpecificationError('Graph presence must match conditional port declarations')
    for source in (wiring['clock_source'], wiring['reset_source']):
        if source not in endpoints or not endpoints[source]['source'] or endpoints[source]['width'] != 1:
            raise SpecificationError(f'Invalid clock/reset source {source}')
    # Implicit clock/reset ports are supplied only by the clock/reset fanout.
    if any(reset['clock'] in c['interfaces'] or reset['reset'] in c['interfaces'] for c in contracts.values()):
        raise SpecificationError('Implicit clock/reset ports must not be redeclared')
    edges, driven, used = [], set(), {wiring['clock_source'], wiring['reset_source']}
    for source, targets in fields(graph['connections'], (), None, 'graph.connections').items():
        if source not in endpoints and source not in absent:
            raise SpecificationError(f'Unknown graph endpoint {source}')
        if not names(targets, source):
            raise SpecificationError(f'{source}: connection needs at least one sink')
        for target in names(targets, source):
            for endpoint in (source, target):
                if endpoint not in endpoints and endpoint not in absent:
                    raise SpecificationError(f'Unknown graph endpoint {endpoint}')
            if source not in endpoints or target not in endpoints:
                if source not in endpoints and target not in endpoints:
                    continue
                raise SpecificationError(f'Conditional connection must omit both ends: {source} -> {target}')
            src, dst = endpoints[source], endpoints[target]
            if not src['source'] or dst['source']:
                raise SpecificationError(f'Wrong graph direction: {source} -> {target}')
            if src['type'] != dst['type'] or src['width'] != dst['width']:
                raise SpecificationError(f'Type/width mismatch: {source} -> {target}')
            if target in driven:
                raise SpecificationError(f'Multiple drivers for {target}')
            driven.add(target)
            used.add(source)
            edges.append({'from': source, 'to': target})
    missing = {n for n, p in endpoints.items() if not p['source']} - driven
    unused = {n for n, p in endpoints.items() if p['source']} - used
    if missing or unused:
        raise SpecificationError(f'Undriven graph sinks {sorted(missing)}; unused sources {sorted(unused)}')
    for name, contract in contracts.items():
        dependencies = fields(contract['combinational_dependencies'], (), None, name + '.dependencies')
        outputs = {p for p, v in contract['interfaces'].items() if v['direction'] == 'output'}
        if set(dependencies) != outputs:
            raise SpecificationError(f'{name}: dependencies must describe every output')
        for out, inputs in dependencies.items():
            for port in names(inputs, name + '.' + out):
                if port not in contract['interfaces'] or contract['interfaces'][port]['direction'] != 'input':
                    raise SpecificationError(f'{name}.{out}: dependency must name an input')
        modules[name] = {'dependencies': {out: [p for p in inputs if name + '.' + p in endpoints]
                                           for out, inputs in dependencies.items()}}
    cycle = dependency_cycle(edges, modules)
    if cycle:
        raise SpecificationError('Declared combinational feedback cycle: ' + ' -> '.join(cycle))
    return len(edges)


def load_package(path):
    """Load the package, documents and timing library as one portable snapshot."""
    path = Path(path)
    package = read_document(path)
    _package_header(package)
    documents = {}
    for name, relative in package['documents'].items():
        text_value(relative, 'documents.' + name)
        document_path = (path.parent / relative).resolve()
        if not document_path.is_relative_to(path.parent.resolve()):
            raise SpecificationError(f'{name}: package documents must stay inside the package directory')
        documents[name] = read_document(document_path)
    shared = documents['shared']
    fields(shared, ('timing_profile',), None, 'shared')
    fields(shared['timing_profile'], ('source',), None, 'timing_profile')
    text_value(shared['timing_profile']['source'], 'timing_profile.source')
    library = read_document((path.parent / package['documents']['shared']).parent / shared['timing_profile']['source'])
    return package, documents, library


def _package_header(package):
    fields(package, ('specification_version', 'package_id', 'design_name', 'revision', 'status', 'scope', 'documents', 'verification_strategy', 'decisions', 'assumptions', 'open_questions'), ('review', 'design_kind', 'generation'), 'package')
    if type(package['specification_version']) is not int or package['specification_version'] != 1:
        raise SpecificationError('Unsupported specification_version')
    if package.get('design_kind') not in (None, 'memory_controller'):
        raise SpecificationError('Unsupported specification design_kind')
    for field in ('package_id', 'design_name', 'revision', 'status'):
        text_value(package[field], field)
    fields(package['scope'], ('plan', 'milestone', 'covered', 'pending'), (), 'scope')
    fields(package['documents'], ('shared', 'system', 'modules', 'execution', 'graph'), ('verification',), 'documents')


def configuration_axes(matrix):
    """Support parameter-named matrices as well as the first example's vocabulary."""
    if isinstance(matrix, dict) and 'parameters' in matrix:
        fields(matrix, ('parameters', 'profiles'), (), 'configurations')
        parameters = fields(matrix['parameters'], (), None, 'configuration parameters')
    else:
        fields(matrix, ('banks', 'waiting_capacity', 'profiles', 'page_policies'), (), 'configurations')
        parameters = dict(zip(('BANK_COUNT', 'WAITING_CAPACITY', 'PAGE_POLICY'),
                              (matrix[k] for k in ('banks', 'waiting_capacity', 'page_policies'))))
    for name, values in dict(parameters, profiles=matrix['profiles']).items():
        if not isinstance(values, list) or not values:
            raise SpecificationError(f'configurations.{name}: expected a nonempty list')
    return parameters, matrix['profiles']


def validate_shared(shared, library, overrides=None, profile=None):
    """Validate shared facts before architectural decomposition is proposed."""
    fields(shared, ('parameters', 'clock_reset', 'types', 'host_interface', 'events', 'timing_profile', 'timing_rules'), (), 'shared')
    validate_clock_reset(shared['clock_reset'])
    parameters = elaborate_parameters(shared['parameters'], overrides or {})
    widths = type_widths(fields(shared['types'], (), None, 'types'), parameters)
    for name, event in fields(shared['events'], (), None, 'events').items():
        fields(event, ('condition', 'effect'), (), 'events.' + name)
        text_value(event['condition'], name + '.condition')
        text_value(event['effect'], name + '.effect')
    timing = fields(shared['timing_profile'], ('source', 'selection', 'supported_profiles', 'unit', 'bindings', 'note'), (), 'timing_profile')
    profile = timing['selection'] if profile is None else profile
    if timing['unit'] != 'cycles' or profile not in names(timing['supported_profiles'], 'supported_profiles'):
        raise SpecificationError(f'Unsupported timing profile/unit: {profile!r}')
    fields(library, (), None, 'timing library')
    if profile not in library:
        raise SpecificationError(f'Unknown timing profile: {profile}')
    timing_values = {}
    for name, ref in fields(timing['bindings'], (), None, 'timing bindings').items():
        value = library[profile]
        if not isinstance(ref, str):
            raise SpecificationError(f'{name}: timing reference must be a string')
        for part in ref.split('.'):
            if not isinstance(value, dict) or part not in value:
                raise SpecificationError(f'{name}: unresolved timing binding {ref}')
            value = value[part]
        if type(value) is not int or value < 1:
            raise SpecificationError(f'{name}: timing value must be positive cycles')
        timing_values[name] = value
    for name, rule in fields(shared['timing_rules'], (), None, 'timing_rules').items():
        if name == 'edge_convention':
            text_value(rule, name)
            continue
        fields(rule, ('scope', 'trigger', 'blocked'), ('minimum', 'window', 'limit'), 'timing_rules.' + name)
        if ('minimum' in rule) == ('window' in rule):
            raise SpecificationError(f'{name}: specify minimum or window')
        text_value(rule['scope'], name + '.scope')
        text_value(rule['trigger'], name + '.trigger')
        opcodes = shared['types'].get('opcode', {}).get('encodings', {})
        if rule['trigger'] not in opcodes or set(names(rule['blocked'], name)) - opcodes.keys():
            raise SpecificationError(f'{name}: unknown timing opcode')
        for key in ('minimum', 'window', 'limit'):
            if key in rule:
                ref = rule[key]
                prefix, values = ('shared.parameters.', parameters) if key == 'limit' else ('shared.timing_profile.bindings.', timing_values)
                if not isinstance(ref, str) or not ref.startswith(prefix) or ref.removeprefix(prefix) not in values:
                    raise SpecificationError(f'{name}: unresolved timing reference {ref!r}')
    return parameters, widths, profile, timing_values


def validate_metadata(package):
    _package_header(package)
    for name in ('plan', 'milestone'):
        text_value(package['scope'][name], 'scope.' + name)
    for name in ('covered', 'pending'):
        names(package['scope'][name], 'scope.' + name)
    for group, required in (('decisions', ('origin', 'status', 'value', 'reason')),
                            ('assumptions', ('statement', 'status')),
                            ('open_questions', ('blocks', 'question'))):
        for name, value in fields(package[group], (), None, group).items():
            fields(value, required, (), group + '.' + name)
            for field in required:
                text_value(value[field], group + '.' + name + '.' + field)
    strategy = fields(package['verification_strategy'], ('status', 'configurations', 'module_checks', 'system_scenario', 'system_evidence'), (), 'verification_strategy')
    configuration_axes(strategy['configurations'])
    if 'review' in package:
        review = fields(package['review'], ('date', 'outcome', 'source', 'accepted_decisions', 'rtl_approval'), (), 'review')
        for name in ('date', 'outcome', 'source', 'rtl_approval'):
            text_value(review[name], 'review.' + name)
        for name in names(review['accepted_decisions'], 'review.accepted_decisions'):
            if name not in package['decisions'] or package['decisions'][name]['status'] != 'accepted':
                raise SpecificationError(f'review: unknown or unaccepted decision {name}')


def validate_contents(package, documents, library, overrides=None, profile=None):
    """Validate a loaded snapshot, including when replaying a saved manifest."""
    _package_header(package)
    fields(documents, ('shared', 'system', 'modules', 'execution', 'graph'), ('verification',), 'documents snapshot')
    shared = fields(documents['shared'], ('parameters', 'clock_reset', 'types', 'host_interface', 'events', 'timing_profile', 'timing_rules'), (), 'shared')
    fields(documents['modules'], ('contracts',), (), 'modules')
    fields(documents['modules']['contracts'], (), None, 'contracts')
    parameters, widths, profile, timing_values = validate_shared(shared, library, overrides, profile)
    rules, obligations = validate_ownership(documents)
    edges = validate_graph(documents, parameters, widths)
    if 'verification' in documents:
        from verification_bindings import elaborate_bindings
        elaborate_bindings(documents, parameters, widths)
    validate_metadata(package)
    if 'generation' in package:
        from specification_request import validate_generated_package
        validate_generated_package(package, documents)
    return {'package_id': package['package_id'], 'revision': package['revision'],
            'specification_validation': 'passed', 'behavioral_verification': 'not_run',
            'parameters': parameters, 'type_widths': widths, 'profile': profile, 'timing_cycles': timing_values,
            'modules': len(documents['modules']['contracts']), 'rules': rules, 'verification_obligations': obligations,
            'connections': edges, 'blocking_questions': list(package['open_questions'])}


def validate_package(path, overrides=None, profile=None):
    """Return structural evidence; this never approves a package or runs behavior."""
    return validate_contents(*load_package(path), overrides, profile)


def validate_matrix(path):
    # Validate the package shape before reading its configuration matrix.
    validate_package(path)
    package = read_document(path)
    matrix = package['verification_strategy']['configurations']
    results = []
    axes, profiles = configuration_axes(matrix)
    for values in itertools.product(*axes.values(), profiles):
        results.append(validate_package(path, dict(zip(axes, values[:-1])), values[-1]))
    if not results:
        raise SpecificationError('Configuration matrix must not be empty')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--matrix', action='store_true', help='Check every declared configuration')
    args = parser.parse_args()
    try:
        results = validate_matrix(args.package) if args.matrix else [validate_package(args.package)]
    except SpecificationError as exc:
        parser.exit(1, f'Specification validation failed: {exc}\n')
    print(json.dumps({'configurations_checked': len(results), 'results': results}, indent=2))


if __name__ == '__main__':
    main()
