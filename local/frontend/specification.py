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


class SpecificationErrors(SpecificationError):
    """Independent diagnostics from one candidate, with stable deduplication."""
    def __init__(self, errors):
        self.errors = list(dict.fromkeys(errors))
        super().__init__(f'Validation failed with {len(self.errors)} error(s):\n' +
                         '\n'.join('- ' + error for error in self.errors))


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


def _contract_shapes(documents):
    """Collect independent shape errors before running any relational checks."""
    contracts = documents['modules']['contracts']
    contract_fields = ('status', 'role', 'owned_state', 'interfaces', 'handshakes', 'reset', 'update_events', 'stalls', 'simultaneous_events', 'invariants', 'verification', 'combinational_dependencies')
    errors = []

    def check(function):
        try:
            return function()
        except SpecificationError as exc:
            errors.extend(getattr(exc, 'errors', [str(exc)]))
            return None

    for name, contract in contracts.items():
        if check(lambda: fields(contract, contract_fields, ('dependency_note',), name)) is None:
            continue  # Do not dereference missing/malformed contract fields.
        for field in ('owned_state', 'handshakes', 'update_events', 'simultaneous_events'):
            check(lambda: fields(contract[field], (), None, name + '.' + field))
        for field in ('status', 'role', 'reset', 'stalls'):
            check(lambda: text_value(contract[field], name + '.' + field))
        check(lambda: names(contract['invariants'], name + '.invariants'))
        check(lambda: names(contract['verification'], name + '.verification'))
        interfaces = check(lambda: fields(contract['interfaces'], (), None, name + '.interfaces'))
        if interfaces is not None:
            for port, spec in interfaces.items():
                if check(lambda: fields(spec, ('direction', 'type'), ('when',), name + '.' + port)) is not None:
                    if spec['direction'] not in ('input', 'output'):
                        errors.append(f'{name}.{port}: invalid direction')
        dependencies = check(lambda: fields(contract['combinational_dependencies'], (), None, name + '.dependencies'))
        if dependencies is not None:
            for out, inputs in dependencies.items():
                check(lambda: names(inputs, name + '.' + out))
    if errors:
        raise SpecificationErrors(errors)
    return contracts


def _graph_shape(documents):
    """Graph shape is independent of contract shapes and endpoint relationships."""
    graph = fields(documents['graph'], ('graph_id', 'clock_reset', 'connections', 'presence', 'integration'), ('verification_outputs',), 'graph')
    declarations = fields(graph.get('verification_outputs', {}), (), None, 'graph.verification_outputs')
    for endpoint, reason in declarations.items():
        text_value(reason, endpoint + '.verification output reason')
    return fields(graph['clock_reset'], ('definition', 'clock_source', 'reset_source', 'consumers'), (), 'graph.clock_reset')


def validate_ownership(documents):
    contracts, rules, obligations = _contract_shapes(documents), {}, {}
    errors = []
    for name, contract in contracts.items():
        for event in contract['update_events']:
            if event != 'every_clock' and event not in {'shared.events.' + e for e in documents['shared']['events']}:
                errors.append(f'{name}: unknown update event {event}')
    for slice_name in ('system', 'execution'):
        doc = documents[slice_name]
        fields(doc, ('slice_id', 'rules', 'verification'), (), slice_name)
        for rid, rule in fields(doc['rules'], (), None, slice_name + '.rules').items():
            ref = f'{slice_name}.rules.{rid}'
            fields(rule, ('origin', 'owners', 'statement', 'verification'), (), ref)
            text_value(rule['origin'], ref + '.origin')
            text_value(rule['statement'], ref + '.statement')
            if not names(rule['owners'], ref) or set(rule['owners']) - contracts.keys():
                errors.append(f'{ref}: missing or unknown rule owners')
            if not names(rule['verification'], ref):
                errors.append(f'{ref}: missing verification obligations')
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
                errors.append(f'{ref}: nonreciprocal verification reference {vid}')
            for owner in rule['owners']:
                if owner not in contracts:
                    continue  # The unknown owner is already reported above.
                if ref not in contracts[owner]['invariants'] or verification_ref not in contracts[owner]['verification']:
                    errors.append(f'{ref}: owner {owner} lacks invariant/verification obligation '
                                  f'(requires invariant {ref} and verification {verification_ref})')
    for ref, obligation in obligations.items():
        prefix, _, vid = ref.split('.')
        for rid in obligation['rules']:
            rule = rules.get(f'{prefix}.rules.{rid}')
            if rule is None or vid not in rule['verification']:
                errors.append(f'{ref}: nonreciprocal rule reference {rid}')
    for name, contract in contracts.items():
        for ref in contract['invariants']:
            if ref not in rules or name not in rules[ref]['owners']:
                errors.append(f'{name}: unknown or unowned invariant {ref}')
        for ref in contract['verification']:
            if ref not in obligations:
                errors.append(f'{name}: unknown verification reference {ref}')
                continue
            prefix = ref.split('.')[0]
            if not any(name in rules.get(f'{prefix}.rules.{rid}', {}).get('owners', []) for rid in obligations[ref]['rules']):
                errors.append(f'{name}: unowned verification obligation {ref}')
    if errors:
        raise SpecificationErrors(errors)
    return len(rules), len(obligations)


def validate_dependencies(documents):
    """Check every module dependency declaration independently of graph wiring."""
    modules, errors = {}, []
    for name, contract in _contract_shapes(documents).items():
        dependencies = contract['combinational_dependencies']
        outputs = {p for p, spec in contract['interfaces'].items() if spec['direction'] == 'output'}
        if set(dependencies) != outputs:
            errors.append(f'{name}: dependencies must describe every output '
                          f'(missing {sorted(outputs - dependencies.keys())}, extra {sorted(dependencies.keys() - outputs)})')
        for out, inputs in dependencies.items():
            for port in inputs:
                if port not in contract['interfaces'] or contract['interfaces'][port]['direction'] != 'input':
                    errors.append(f'{name}.{out}: dependency must name an input; {port!r} is not an input')
        modules[name] = {'dependencies': dependencies}
    if errors:
        raise SpecificationErrors(errors)
    return modules


def validate_graph(documents, parameters, widths, bindings=None):
    graph, shared = documents['graph'], documents['shared']
    contracts = documents['modules']['contracts']
    wiring = _graph_shape(documents)
    reset = shared['clock_reset']
    validate_clock_reset(reset)
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
    connection_errors = []
    for source, targets in fields(graph['connections'], (), None, 'graph.connections').items():
        source_known = source in endpoints or source in absent
        if not source_known:
            connection_errors.append(f'Unknown graph endpoint {source}')
        try:
            targets = names(targets, source)
        except SpecificationError as exc:
            connection_errors.append(str(exc))
            continue
        if not targets:
            connection_errors.append(f'{source}: connection needs at least one sink')
        for target in targets:
            target_known = target in endpoints or target in absent
            if not target_known:
                connection_errors.append(f'Unknown graph endpoint {target}')
            if not source_known or not target_known:
                continue
            if source not in endpoints or target not in endpoints:
                if source not in endpoints and target not in endpoints:
                    continue
                connection_errors.append(f'Conditional connection must omit both ends: {source} -> {target}')
                continue
            src, dst = endpoints[source], endpoints[target]
            if not src['source'] or dst['source']:
                connection_errors.append(f'Wrong graph direction: {source} -> {target}')
                continue
            if src['type'] != dst['type'] or src['width'] != dst['width']:
                connection_errors.append(f'Type/width mismatch: {source} -> {target}')
                continue
            if target in driven:
                connection_errors.append(f'Multiple drivers for {target}')
                continue
            driven.add(target)
            used.add(source)
            edges.append({'from': source, 'to': target})
    if connection_errors:
        # Invalid edges make coverage, output-use and cycle conclusions unsafe.
        # Collect local wiring faults, but do not diagnose the partial graph.
        raise SpecificationErrors(connection_errors)
    declarations = graph.get('verification_outputs', {})
    if declarations and bindings is None:
        raise SpecificationError('graph.verification_outputs requires valid verification bindings')
    observed = set()
    if bindings is not None:
        for check in bindings['checks'].values():
            observed.update(bindings['observations'][alias]['endpoint'] for alias in check['observations'])
    verification_only, declaration_errors = set(), []
    for endpoint in sorted(declarations):
        if endpoint not in endpoints or endpoint.startswith('top.') or not endpoints[endpoint]['source']:
            declaration_errors.append(f'{endpoint}: verification-only declaration requires a present child output')
            continue
        if endpoint in used:
            declaration_errors.append(f'{endpoint}: verification-only output already has a functional connection; '
                                      'remove its graph.verification_outputs declaration, retaining the functional '
                                      'connection and verification observation')
            continue
        if endpoint not in observed:
            declaration_errors.append(f'{endpoint}: verification-only output must be an observation used by a verification check')
            continue
        verification_only.add(endpoint)
    if declaration_errors:
        # All endpoints and connections are valid here. Each declaration can be
        # checked independently; invalid declarations never grant an exemption.
        # Defer coverage/cycle checks until declarations themselves are usable.
        raise SpecificationErrors(declaration_errors)
    missing = {n for n, p in endpoints.items() if not p['source']} - driven
    unused = {n for n, p in endpoints.items() if p['source']} - used - verification_only
    if missing or unused:
        message = f'Undriven graph sinks {sorted(missing)}; unused sources {sorted(unused)}'
        monitors = sorted(endpoint for endpoint in unused & observed if not endpoint.startswith('top.'))
        if monitors:
            message += (f'; checked observations {monitors} may use explicit graph.verification_outputs '
                        'only if intended solely for verification; functional connections must still be repaired')
        raise SpecificationError(message)
    for name, spec in validate_dependencies(documents).items():
        dependencies = spec['dependencies']
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
            raise SpecificationError(
                f'shared.timing_profile.bindings.{name}: timing reference must be a string path '
                f'relative to timing_library[{profile!r}]; received {type(ref).__name__} {ref!r}. '
                'Keep the library path in bindings; the compiler resolves cycle values for each profile.')
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
    errors = []
    for group, required in (('decisions', ('origin', 'status', 'value', 'reason')),
                            ('assumptions', ('statement', 'status')),
                            ('open_questions', ('blocks', 'question'))):
        for name, value in fields(package[group], (), None, group).items():
            try:
                fields(value, required, (), group + '.' + name)
            except SpecificationError as exc:
                errors.append(str(exc))
                continue  # Field checks require a usable entry shape.
            for field in required:
                try:
                    text_value(value[field], group + '.' + name + '.' + field)
                except SpecificationError as exc:
                    errors.append(str(exc))
    if errors:
        raise SpecificationErrors(errors)
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
    errors = []

    def check(function):
        try:
            return function()
        except SpecificationError as exc:
            errors.extend(getattr(exc, 'errors', [str(exc)]))
            return None

    check(lambda: _contract_shapes(documents))
    check(lambda: _graph_shape(documents))
    if errors:
        raise SpecificationErrors(errors)  # No relational checks on malformed shapes.

    counts = check(lambda: validate_ownership(documents))
    check(lambda: validate_dependencies(documents))
    bindings = None
    if 'verification' in documents:
        from verification_bindings import elaborate_bindings
        bindings = check(lambda: elaborate_bindings(documents, parameters, widths))
    edges = check(lambda: validate_graph(documents, parameters, widths, bindings))
    check(lambda: validate_metadata(package))
    if not errors and 'generation' in package:
        from specification_request import validate_generated_package
        check(lambda: validate_generated_package(package, documents))
    if errors:
        raise SpecificationErrors(errors)
    rules, obligations = counts
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
