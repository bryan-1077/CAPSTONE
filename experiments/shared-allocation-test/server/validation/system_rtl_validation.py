"""Agent 1 controller integration gate (Python 3.6 compatible).

The scanner handles generated ANSI modules and common instantiation syntax.
It is deliberately conservative about expression/type resolution: uncertain
checks belong to the mandatory HDL compile/elaboration, never a guessed width.
Reachability is syntactic; conditional generate/preprocessor elaboration belongs
to the compiler. No simulation or functional verification runs here.
"""
import ast
import operator
import re
import sys
from collections import deque
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared'))
from controller_manifest import discover_system, find_manifest, read_mapping, resolve_path


IDENT = r'[A-Za-z_][A-Za-z0-9_$]*'
PRIMITIVES = set(('and nand or nor xor xnor buf not bufif0 bufif1 notif0 notif1 '
                  'nmos pmos cmos rnmos rpmos rcmos tran rtran tranif0 tranif1 '
                  'rtranif0 rtranif1 pullup pulldown').split())
KEYWORDS = set(('module endmodule input output inout wire reg logic bit int integer '
                'parameter localparam typedef struct enum function task automatic '
                'if else for foreach while case casex casez return assign begin end '
                'always always_ff always_comb always_latch initial generate endgenerate '
                'assert assume cover property sequence repeat wait unique priority').split())
FEATURE_KEYS = {'refresh_controller': 'refresh_enabled', 'trrd': 'trrd_enabled',
                'tfaw_tracker': 'tfaw_enabled'}


def mask_source(source):
    return re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                  lambda m: ''.join('\n' if c == '\n' else ' ' for c in m.group()), source)


def integer_value(expression, environment):
    """Evaluate a small constant-expression subset without eval or unknown defaults."""
    if expression is None:
        return None
    expression = str(expression).strip()
    def literal(match):
        size, base, digits = match.groups()
        value = int(digits.replace('_', ''), {'b': 2, 'o': 8, 'd': 10, 'h': 16}[base.lower()])
        return str(value & ((1 << int(size)) - 1)) if size else str(value)
    try:
        # Signed literals, casts, x/z and unsized fill literals are left to HDL.
        expression = re.sub(r"(\d+)?'([bBoOdDhH])([0-9a-fA-F_]+)", literal, expression)
        expression = expression.replace('$clog2', 'clog2')
        tree = ast.parse(expression, mode='eval')
        binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.Div: lambda a, b: int(a / b), ast.FloorDiv: operator.floordiv,
                  ast.Mod: operator.mod, ast.LShift: operator.lshift, ast.RShift: operator.rshift,
                  ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.BitXor: operator.xor}
        unary = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert}
        def evaluate(node):
            if isinstance(node, ast.Num) and isinstance(node.n, int):
                return node.n
            if isinstance(node, ast.Name) and isinstance(environment.get(node.id), int):
                return environment[node.id]
            if isinstance(node, ast.BinOp) and type(node.op) in binary:
                left, right = evaluate(node.left), evaluate(node.right)
                if isinstance(node.op, (ast.LShift, ast.RShift)) and not 0 <= right <= 4096:
                    raise ValueError('Unbounded shift')
                return binary[type(node.op)](left, right)
            if isinstance(node, ast.UnaryOp) and type(node.op) in unary:
                return unary[type(node.op)](evaluate(node.operand))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'clog2' and len(node.args) == 1):
                value = evaluate(node.args[0])
                if value >= 0:
                    return (value - 1).bit_length() if value else 0
            raise ValueError('Expression deferred to HDL')
        return evaluate(tree.body)
    except (ValueError, SyntaxError, KeyError, TypeError, ZeroDivisionError, OverflowError):
        return None


def parameters_from(blob, local, validator):
    rows = []
    for entry in validator.split_top_level_commas(blob):
        match = re.search(r'(' + IDENT + r')\s*=\s*(.+)$', entry.strip(), re.S)
        if match:
            rows.append({'name': match.group(1), 'expression': match.group(2).strip(),
                         'local': local or bool(re.search(r'\blocalparam\b', entry)),
                         'type_parameter': bool(re.search(r'\btype\b', entry))})
    return rows


def parse_instances(body, validator):
    # Function/task signatures and local declarations are not hierarchy edges.
    body = re.sub(r'\b(function|task)\b[\s\S]*?\bend(?:function|task)\b', '', body)
    pattern = re.compile(r'\b(' + IDENT + r')\s+(?:#\s*\(|(' + IDENT + r')\s*(?:\[[^\]]+\]\s*)*\()')
    instances, deferred = [], []
    cursor = 0
    while True:
        match = pattern.search(body, cursor)
        if not match:
            break
        cursor = match.end()
        kind = match.group(1)
        if kind in KEYWORDS or kind in PRIMITIVES:
            continue
        overrides = ''
        if match.group(2) is None:
            end = validator.find_matching_paren(body, match.end() - 1)
            if end is None:
                deferred.append('Unbalanced parameter override for ' + kind)
                continue
            overrides = body[match.end():end]
            position = end + 1
        else:
            position = match.start(2)
        pending = []
        while True:
            instance = re.match(r'\s*(' + IDENT + r')\s*((?:\[[^\]]+\]\s*)*)\(', body[position:])
            if not instance:
                deferred.append('Cannot parse instance of ' + kind)
                break
            opening = position + instance.end() - 1
            end = validator.find_matching_paren(body, opening)
            if end is None:
                deferred.append('Unbalanced connections for ' + kind + '.' + instance.group(1))
                break
            pending.append({'module': kind, 'name': instance.group(1), 'parameters': overrides,
                            'connections': body[opening + 1:end], 'array': bool(instance.group(2).strip())})
            following = re.match(r'\s*([,;])', body[end + 1:])
            if not following:
                deferred.append('Malformed instantiation of ' + kind + '.' + instance.group(1))
                break
            position = end + 1 + following.end()
            cursor = position
            if following.group(1) == ';':
                instances.extend(pending)
                break
    return instances, deferred


def parse_modules(path, validator):
    source = mask_source(validator.read_text(str(path)))
    rows = []
    pattern = re.compile(r'\bmodule\s+(?:(?:automatic|static)\s+)?(' + IDENT + r')\b([\s\S]*?)\bendmodule\b')
    for match in pattern.finditer(source):
        name, block = match.group(1), match.group()
        position = match.start(2) - match.start()
        tail = re.match(r'\s*#\s*\(', block[position:])
        params = []
        if tail:
            opening = position + tail.end() - 1
            closing = validator.find_matching_paren(block, opening)
            if closing is not None:
                params += parameters_from(block[opening + 1:closing], False, validator)
                position = closing + 1
        port_header = re.match(r'\s*\(', block[position:])
        if port_header:
            closing = validator.find_matching_paren(block, position + port_header.end() - 1)
            if closing is not None:
                position = closing + 1
        header_end = block.find(';', position)
        body = block[header_end + 1:] if header_end >= 0 else ''
        for param in re.finditer(r'\b(parameter|localparam)\b([^;]+);', body):
            params += parameters_from(param.group(2), param.group(1) == 'localparam', validator)
        ports = validator.extract_ports(block, name)
        instances, deferred = parse_instances(body, validator)
        rows.append({'name': name, 'source_file': str(path), 'parameters': params,
                     'ports': ports, 'instantiated_modules': instances,
                     'deferred_checks': deferred, '_block': block, '_body': body})
    return rows


def parameter_environment(module, overrides=None):
    overrides = overrides or {}
    environment = dict(overrides)
    pending = list(module['parameters'])
    for _ in range(len(pending) + 1):
        changed = False
        for row in pending:
            name = row['name']
            if name not in environment and not row['type_parameter']:
                value = integer_value(row['expression'], environment)
                if value is not None:
                    environment[name], changed = value, True
        if not changed:
            break
    return environment


def declaration_width(port, environment):
    if port.get('custom_type') or ('direction' in port and port['direction'] is None):
        return None
    dimensions = re.findall(r'\[([^\]]+)\]', port.get('width_text') or '')
    # Unpacked port arrays require shape/type checks, not a guessed packed width.
    if re.search(r'\b' + re.escape(port['name']) + r'\s*\[', port.get('declaration_text', '')):
        return None
    if not dimensions:
        text = port.get('declaration_text', '')
        if re.search(r'\b(?:int|integer|shortint|longint|byte|time|real)\b', text):
            return None
        return 1
    width = 1
    for dimension in dimensions:
        halves = dimension.split(':')
        if len(halves) != 2:
            return None
        left, right = [integer_value(x, environment) for x in halves]
        if left is None or right is None:
            return None
        width *= abs(left - right) + 1
    return width


def signal_widths(module, environment, validator):
    widths = {p['name']: declaration_width(p, environment) for p in module['ports']}
    body = re.sub(r'\btypedef\b[\s\S]*?;', '', module['_body'])
    # Only primitive packed logic declarations are reliable here. Custom types
    # and unpacked arrays stay unknown even if they contain primitive members.
    for match in re.finditer(r'\b(?:wire|reg|logic|bit)\s+((?:(?:signed|unsigned)\s+)?(?:\[[^\]]+\]\s*)*)([^;{}]+);', body):
        for entry in validator.split_top_level_commas(match.group(2)):
            name = re.match(r'\s*(' + IDENT + r')\s*(.*)', entry, re.S)
            if name:
                widths[name.group(1)] = None if name.group(2).lstrip().startswith('[') else declaration_width(
                    {'name': name.group(1), 'width_text': match.group(1)}, environment)
    return widths


def expression_width(expression, signals, environment, validator):
    expression = expression.strip()
    if expression in signals:
        return signals[expression]
    constant = re.fullmatch(r"(\d+)'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+", expression)
    if constant:
        return int(constant.group(1))
    select = re.fullmatch(r'(' + IDENT + r')\s*\[([^\]]+)\]', expression)
    if select and signals.get(select.group(1)) is not None:
        halves = select.group(2).split(':')
        if len(halves) == 1:
            return 1
        if len(halves) == 2:
            left, right = [integer_value(x, environment) for x in halves]
            if left is not None and right is not None:
                return abs(left - right) + 1
    if expression.startswith('{') and expression.endswith('}'):
        terms = validator.split_top_level_commas(expression[1:-1])
        widths = [expression_width(t, signals, environment, validator) for t in terms]
        if widths and all(w is not None for w in widths):
            return sum(widths)
    return None


def bindings(blob, names, validator, allow_wildcard=False):
    entries = validator.split_top_level_commas(blob)
    result, problems, wildcard = {}, [], False
    named = any(e.strip().startswith('.') for e in entries)
    for index, entry in enumerate(entries):
        entry = entry.strip()
        if not entry and not blob.strip():
            continue
        if entry == '.*' and allow_wildcard:
            wildcard = True
            continue
        if named:
            match = re.fullmatch(r'\.(' + IDENT + r')\s*(?:\(([\s\S]*)\))?', entry)
            if not match:
                problems.append('Mixed or malformed named/positional binding: ' + entry)
                continue
            name, expression = match.group(1), match.group(2)
            expression = name if expression is None else expression.strip()
        elif index < len(names):
            name, expression = names[index], entry
        else:
            problems.append('Too many positional bindings')
            continue
        if name not in names:
            problems.append('Unknown binding name: ' + name)
        if name in result:
            problems.append('Duplicate binding: ' + name)
        result[name] = expression
    return result, problems, wildcard


def clock_reset_ports(module, manifest, spec=None):
    names = {p['name'] for p in module['ports']}
    if module['name'] == manifest['top_module']:
        return manifest['clock']['name'], manifest['reset']['name']
    if spec:
        return spec.get('clock', {}).get('name'), spec.get('reset', {}).get('name')
    events = re.findall(r'\balways(?:_ff)?\s*@\s*\(([^)]*)\)', module['_block'])
    clocks = {m.group(1) for event in events
              for m in [re.search(r'\b(?:posedge|negedge)\s+(' + IDENT + ')', event)] if m}
    clock = next(iter(clocks)) if len(clocks) == 1 else None
    # Integration wrappers often have no local flops. Propagate identifiable
    # manifest signals without imposing a reset on purely combinational leaves.
    if clock is None and manifest['clock']['name'] in names:
        clock = manifest['clock']['name']
    resets = {m.group(1) for m in re.finditer(r'\bif\s*\(\s*[!~]?\s*(' + IDENT + r')\s*\)', module['_block'])
              if re.search(r'rst|reset', m.group(1), re.I)}
    reset = next(iter(resets)) if len(resets) == 1 else None
    if reset is None and clock and manifest['reset']['name'] in names:
        reset = manifest['reset']['name']
    return clock, reset


def validate_system(validator, manifest_path, output_json, work_dir=None):
    """Return/save a structural result. The existing compiler runs once on all RTL."""
    manifest_path = Path(manifest_path).resolve()
    result = {'mode': 'system', 'verification_scope': 'structural_integration_only',
              'functional_verification_passed': False, 'ready_for_backend': False,
              'manifest_path': str(manifest_path), 'top_module': 'ddr4_controller_top',
              'rtl_files': [], 'module_definitions': [], 'generated_modules': [],
              'generated_module_count': 0, 'reachable_modules': [], 'unused_generated_modules': [],
              'missing_modules': [], 'duplicate_modules': [], 'module_spec_status': {},
              'module_spec_results': {}, 'failure_details': [], 'warnings': [],
              'deferred_checks': [], 'errors': [], 'compile_tool': None, 'compile_passed': False,
              'elaboration_passed': False, 'hierarchy_passed': False, 'integration_passed': False,
              'is_valid': False, 'verification_readiness': 'BLOCKED_SYSTEM_AGENT_1'}
    def fail(category, message, **context):
        result['errors'].append(message)
        result['failure_details'].append(dict(category=category, message=message, **context))
    def save():
        result['failure_categories'] = sorted({d['category'] for d in result['failure_details']})
        validator.write_json(str(output_json), result)
        return result
    try:
        manifest = read_mapping(manifest_path)
        result.update(top_module=manifest['top_module'], configuration=manifest['configuration'],
                      configuration_sources=manifest['configuration_sources'],
                      configuration_contract='manifest.configuration (normalized system configuration)',
                      primary_component_config=manifest.get('primary_component_config', manifest['controller_config']),
                      clock=manifest['clock'], reset=manifest['reset'])
        rtl_files = [str(resolve_path(manifest_path, manifest, p)) for p in manifest['rtl_files']]
        if not rtl_files:
            raise ValueError('The manifest supplies no RTL files.')
        result['rtl_files'] = rtl_files
        definitions = [row for path in rtl_files for row in parse_modules(Path(path), validator)]
        inventory = {}
        for row in definitions:
            inventory.setdefault(row['name'], []).append(row)
        result['module_definitions'] = [{k: v for k, v in row.items() if not k.startswith('_')}
                                        for row in definitions]
        result['generated_modules'] = sorted(inventory)
        result['generated_module_count'] = len(definitions)
        modules = {name: rows[0] for name, rows in inventory.items()}
        for name, rows in sorted(inventory.items()):
            if len(rows) > 1:
                files = [r['source_file'] for r in rows]
                result['duplicate_modules'].append({'name': name, 'files': files})
                fail('SYSTEM_DUPLICATE_MODULE', 'Duplicate module {}: {}'.format(name, ', '.join(files)), module=name, files=files)
        top = manifest['top_module']
        if top != 'ddr4_controller_top' or top not in modules:
            fail('SYSTEM_TOP_NOT_FOUND', 'Expected controller top ddr4_controller_top was not found.')
        # Run Phase 1 checks as well; structural diagnostics above survive stale
        # inventories and missing tops instead of collapsing to generic RTL_FAILED.
        try:
            discover_system(manifest_path)
            result['discovery_passed'] = True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result['discovery_passed'] = False
            fail('SYSTEM_CONFIG_INTEGRATION_FAILED', 'Manifest discovery: ' + str(exc))
        external = set(PRIMITIVES)
        library_modules = manifest.get('external_modules', [])
        if not isinstance(library_modules, list) or any(not isinstance(n, str) for n in library_modules):
            raise ValueError('external_modules must be a list of library module names.')
        external.update(library_modules)
        result['external_modules'] = library_modules
        for path in rtl_files:
            external.update(re.findall(r'\bprimitive\s+(' + IDENT + ')', mask_source(validator.read_text(path))))
        reachable, visiting, completed = set(), set(), set()
        stack = [(top, False)] if top in modules else []
        while stack:
            name, leaving = stack.pop()
            if leaving:
                visiting.discard(name)
                completed.add(name)
                continue
            if name in visiting:
                fail('SYSTEM_HIERARCHY_FAILED', 'Recursive module hierarchy involving ' + name)
                continue
            if name in completed:
                continue
            visiting.add(name)
            reachable.add(name)
            stack.append((name, True))
            parent = modules[name]
            for instance in reversed(parent['instantiated_modules']):
                child = instance['module']
                if child in external and child not in modules:
                    continue
                if child not in modules:
                    detail = {'parent_module': name, 'child_module': child,
                              'source_file': parent['source_file'], 'instance_name': instance['name']}
                    result['missing_modules'].append(detail)
                    fail('SYSTEM_MISSING_DEPENDENCY', '{}.{} instantiates missing {}'.format(name, instance['name'], child), **detail)
                else:
                    stack.append((child, False))
        result['reachable_modules'] = sorted(reachable)
        result['unused_generated_modules'] = sorted(set(modules) - reachable)
        result['reachable_rtl_files'] = sorted({modules[n]['source_file'] for n in reachable})
        if result['unused_generated_modules']:
            result['warnings'].append('Unused generated modules: ' + ', '.join(result['unused_generated_modules']))
        specs = {}
        for row in manifest['module_specs']:
            name = row['module_name']
            spec_path = resolve_path(manifest_path, manifest, row['path'])
            spec = validator.load_yaml(str(spec_path))
            specs[name] = spec
            ok, errors, warnings, checks = validator.validate_yaml(spec)
            if name in modules and ok:
                match, match_errors, match_warnings, match_checks = validator.compare_yaml_rtl(
                    name, modules[name]['_block'], spec, structural_only=True)
                ok = ok and match
                errors += match_errors
                warnings += match_warnings
                checks += match_checks
            elif name not in modules:
                ok = False
                errors.append('Spec has no RTL module definition.')
            result['module_spec_status'][name] = 'PASS' if ok else 'FAIL'
            result['module_spec_results'][name] = {'path': str(spec_path), 'status': 'PASS' if ok else 'FAIL',
                                                  'errors': errors, 'warnings': warnings, 'checks': checks}
            result['warnings'].extend('{}: {}'.format(name, w) for w in warnings)
            for error in errors:
                fail('SYSTEM_MODULE_SPEC_FAILED', name + ': ' + error, module=name, spec_path=str(spec_path))
        for name in modules:
            result['module_spec_status'].setdefault(name, 'NO_MODULE_SPEC')
        # Feature identity comes from expanded master metadata, never module names
        # or filenames. Resolve each master's child spec to its actual design_name.
        requirements = []
        features_seen = set()
        for relative in manifest['spec_groups']:
            path = resolve_path(manifest_path, manifest, relative)
            master = read_mapping(path)
            feature = str(master.get('source_feature', '')).lower()
            key = FEATURE_KEYS.get(feature)
            features_seen.add(feature)
            enabled = manifest['configuration'].get(key) if key else feature in ('basic_commands', 'request_queue', 'scheduler')
            if not enabled:
                continue
            for child in master.get('submodules', []):
                child_spec = read_mapping(path.parent / child['spec'])
                name = child_spec['design_name']
                requirements.append({'feature': feature, 'module': name, 'reachable': name in reachable})
                if name not in reachable:
                    fail('SYSTEM_CONFIG_INTEGRATION_FAILED', 'Enabled feature {} requires reachable module {}'.format(feature, name), feature=feature, module=name)
        for feature, key in FEATURE_KEYS.items():
            if manifest['configuration'].get(key) and feature not in features_seen:
                fail('SYSTEM_CONFIG_INTEGRATION_FAILED', 'Enabled {} has no feature/module metadata.'.format(key), feature=feature)
        result['configuration_requirements'] = requirements
        if top in modules:
            top_module = modules[top]
            top_ports = {p['name']: p for p in top_module['ports']}
            for kind in ('clock', 'reset'):
                expected = manifest[kind]['name']
                if expected not in top_ports or top_ports[expected].get('direction') in ('output', 'inout'):
                    fail('SYSTEM_CLOCK_RESET_FAILED', 'Controller {} {} must be an input port.'.format(kind, expected))
                elif top_ports[expected].get('direction') is None:
                    result['deferred_checks'].append('Controller {} direction/type deferred to compiler.'.format(kind))
            reset_style = validator.detect_reset_style(top_module['_block'], manifest['reset']['name'])
            if reset_style['found_reset_logic']:
                if reset_style['asynchronous'] == manifest['reset']['synchronous']:
                    fail('SYSTEM_CLOCK_RESET_FAILED', 'Controller reset style differs from manifest.')
                if reset_style['active_low_detected'] is not None and reset_style['active_low_detected'] != manifest['reset']['active_low']:
                    fail('SYSTEM_CLOCK_RESET_FAILED', 'Controller reset polarity differs from manifest.')
            top_env = parameter_environment(top_module)
            for constant, expected in (('BANK_COUNT', manifest['configuration']['bank_count']),
                                       ('KEEP_ROWS_OPEN', int(manifest['configuration']['page_policy'] == 'open_page'))):
                if constant in top_env and top_env[constant] != expected:
                    fail('SYSTEM_CONFIG_INTEGRATION_FAILED', '{}={} differs from configured {}'.format(constant, top_env[constant], expected))
            result['dut_spec'] = {'top_module': top, 'clock': manifest['clock'], 'reset': manifest['reset'],
                                  'ports': [dict(p, width=declaration_width(p, top_env)) for p in top_module['ports']],
                                  'configuration': manifest['configuration'], 'rtl_files': rtl_files,
                                  'verification_scope': 'structural_integration_only'}
            # Check instantiated contexts with evaluated overrides, rather than
            # comparing every parametrized module against its default widths.
            queue = deque([(top, top_env, ())])
            seen_contexts = set()
            while queue:
                name, environment, ancestors = queue.popleft()
                context = (name, tuple(sorted(environment.items())))
                if context in seen_contexts or name in ancestors:
                    continue
                seen_contexts.add(context)
                if len(seen_contexts) > 10000:
                    result['deferred_checks'].append('Large parameter-context expansion deferred to compiler.')
                    break
                parent = modules[name]
                signals = signal_widths(parent, environment, validator)
                parent_ports = {p['name']: p for p in parent['ports']}
                parent_clock, parent_reset = clock_reset_ports(parent, manifest, specs.get(name))
                result['deferred_checks'].extend('{}: {}'.format(name, d) for d in parent['deferred_checks'])
                for instance in parent['instantiated_modules']:
                    if instance['module'] not in modules:
                        continue
                    child = modules[instance['module']]
                    location = '{}.{}'.format(name, instance['name'])
                    parameter_names = [p['name'] for p in child['parameters'] if not p['local']]
                    overrides, problems, _ = bindings(instance['parameters'], parameter_names, validator)
                    for problem in problems:
                        fail('SYSTEM_INTERFACE_FAILED', location + ': parameter ' + problem)
                    override_values = {key: integer_value(value, environment) for key, value in overrides.items()}
                    child_env = parameter_environment(child, override_values)
                    child_ports = {p['name']: p for p in child['ports']}
                    connections, problems, wildcard = bindings(instance['connections'], list(child_ports), validator, True)
                    for problem in problems:
                        fail('SYSTEM_INTERFACE_FAILED', location + ': ' + problem)
                    if wildcard:
                        for port in child_ports:
                            if port not in connections and port in signals:
                                connections[port] = port
                            elif port not in connections:
                                result['deferred_checks'].append(location + ': wildcard binding for ' + port)
                    child_clock, child_reset = clock_reset_ports(child, manifest, specs.get(child['name']))
                    for kind, port, upstream in (('clock', child_clock, parent_clock), ('reset', child_reset, parent_reset)):
                        if not port or port not in child_ports:
                            continue
                        expression = connections.get(port, '').strip()
                        if not expression or integer_value(expression, environment) is not None or expression in ("'0", "'1"):
                            fail('SYSTEM_CLOCK_RESET_FAILED', location + ': unconnected/constant ' + kind + ' port ' + port)
                        elif upstream and expression != upstream:
                            # Derived clocks/resets, inversions and local synchronization
                            # can be intentional; compiler plus later verification applies.
                            result['deferred_checks'].append(location + ': derived {} {} (parent {})'.format(kind, expression, upstream))
                    for port, expression in connections.items():
                        if port not in child_ports or not expression:
                            continue
                        info = child_ports[port]
                        root_signal = re.match(r'^(' + IDENT + r')(?:\s*\[|$)', expression)
                        root = root_signal.group(1) if root_signal else None
                        if info.get('direction') in ('output', 'inout'):
                            if root in parent_ports and parent_ports[root].get('direction') == 'input':
                                fail('SYSTEM_INTERFACE_FAILED', location + ': child ' + port + ' drives parent input ' + root)
                            elif integer_value(expression, environment) is not None or expression in ("'0", "'1"):
                                fail('SYSTEM_INTERFACE_FAILED', location + ': output/inout ' + port + ' connected to constant')
                        actual = expression_width(expression, signals, environment, validator)
                        expected = declaration_width(info, child_env)
                        if instance['array']:
                            actual = None
                        if actual is not None and expected is not None and actual != expected:
                            fail('SYSTEM_INTERFACE_FAILED', '{}: port {} width {} connected to width {}'.format(location, port, expected, actual))
                        elif actual is None or expected is None:
                            result['deferred_checks'].append(location + ': width/type of port ' + port + ' deferred to compiler')
                    queue.append((child['name'], child_env, ancestors + (name,)))
        result['deferred_checks'] = sorted(set(result['deferred_checks']))
        result['warnings'].extend(result['deferred_checks'])
        compile_dir = Path(work_dir or (str(Path(output_json).resolve().parent / 'system_compile'))).resolve()
        compile_dir.mkdir(parents=True, exist_ok=True)
        tool, passed, output = validator.compile_rtl(rtl_files, top, work_dir=str(compile_dir), working_directory=str(compile_dir))
        log = compile_dir / 'compile.log'
        log.write_text(output)
        result.update(compile_tool=tool, compile_passed=passed, elaboration_passed=passed,
                      compile_log=str(log), compile_output=output, compile_top=top,
                      compile_rtl_files=rtl_files)
        if not passed:
            fail('SYSTEM_RTL_COMPILE_FAILED', 'Full controller compile/elaboration failed: ' + output, compile_log=str(log))
            if re.search(r'unresolved|unknown module|cannot find|not found|elaborat|undefined', output, re.I):
                fail('SYSTEM_ELABORATION_FAILED', 'Compiler reported unresolved hierarchy/elaboration; see compile.log.')
        categories = {d['category'] for d in result['failure_details']}
        result['hierarchy_passed'] = not categories.intersection({
            'SYSTEM_TOP_NOT_FOUND', 'SYSTEM_DUPLICATE_MODULE', 'SYSTEM_MISSING_DEPENDENCY', 'SYSTEM_HIERARCHY_FAILED'})
        result['integration_passed'] = result['hierarchy_passed'] and not categories.intersection({
            'SYSTEM_INTERFACE_FAILED', 'SYSTEM_CLOCK_RESET_FAILED', 'SYSTEM_CONFIG_INTEGRATION_FAILED', 'SYSTEM_MODULE_SPEC_FAILED'})
        result['is_valid'] = result['integration_passed'] and passed and not result['errors']
        result['checks'] = {label: not categories.intersection(failures) for label, failures in (
            ('top_module', {'SYSTEM_TOP_NOT_FOUND'}), ('hierarchy', {'SYSTEM_TOP_NOT_FOUND', 'SYSTEM_HIERARCHY_FAILED', 'SYSTEM_DUPLICATE_MODULE', 'SYSTEM_MISSING_DEPENDENCY'}),
            ('dependencies', {'SYSTEM_MISSING_DEPENDENCY'}), ('interfaces', {'SYSTEM_INTERFACE_FAILED'}),
            ('clock_reset', {'SYSTEM_CLOCK_RESET_FAILED'}), ('configuration', {'SYSTEM_CONFIG_INTEGRATION_FAILED'}))}
        if result['is_valid']:
            result['verification_readiness'] = 'READY_FOR_SYSTEM_AGENT_2_AND_AGENT_3'
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        fail('SYSTEM_CONFIG_INTEGRATION_FAILED', 'System input validation failed: ' + str(exc))
    return save()


def print_system_result(result):
    print('\n' + '=' * 60 + '\nAGENT 1 — FULL SYSTEM RTL VALIDATION\n' + '=' * 60)
    for label, value in (('Mode', 'SYSTEM'), ('Top module', result['top_module']),
                         ('RTL files discovered', len(result['rtl_files'])), ('Module definitions', result['generated_module_count'])):
        print('{:<23}: {}'.format(label, value))
    print('\nHierarchy:')
    for label, key in (('Reachable modules', 'reachable_modules'), ('Unused modules', 'unused_generated_modules'),
                       ('Missing modules', 'missing_modules'), ('Duplicate modules', 'duplicate_modules')):
        print('  {:<21}: {}'.format(label, len(result[key])))
    print('\nConfiguration (normalized manifest.configuration):')
    for label, key in (('DDR rate', 'memory_speed'), ('Banks', 'bank_count'), ('Scheduler', 'scheduler'),
                       ('Page policy', 'page_policy'), ('Refresh', 'refresh_enabled'), ('tRRD', 'trrd_enabled'), ('tFAW', 'tfaw_enabled')):
        value = result.get('configuration', {}).get(key, 'UNKNOWN')
        if type(value) is bool:
            value = 'enabled' if value else 'disabled'
        print('  {:<21}: {}'.format(label, value))
    print('\nSpecifications:')
    print('  Specs found          : {}'.format(len(result['module_spec_results'])))
    print('  Specs valid          : {}'.format(sum(r['status'] == 'PASS' for r in result['module_spec_results'].values())))
    for name, status in sorted(result['module_spec_status'].items()):
        print('  {}: {}'.format(name, status))
    print('  Wrapper specs        : not required in system mode')
    print('\nIntegration:')
    for name, passed in result.get('checks', {}).items():
        print('  {:<21}: {}'.format(name.replace('_', ' ').title(), 'PASS' if passed else 'FAIL'))
    print('\nCompilation:')
    print('  Tool                 : {}'.format((result['compile_tool'] or 'UNAVAILABLE').upper()))
    print('  Full hierarchy       : {}'.format('PASS' if result['compile_passed'] else 'FAIL'))
    print('  Elaboration          : {}'.format('PASS' if result['elaboration_passed'] else 'FAIL'))
    if result.get('compile_log'):
        print('  Compile log          : ' + result['compile_log'])
    print('  Deferred static checks: {}'.format(len(result['deferred_checks'])))
    for failure in result['failure_details']:
        print('  {}: {}'.format(failure['category'], failure['message']))
    print('\nAGENT 1 SYSTEM RESULT : ' + ('PASS' if result['is_valid'] else 'FAIL'))
    print('Verification readiness: ' + result['verification_readiness'])
    print('Structural/integration validation only. No functional controller behavior has been proven.')
    print('=' * 60)


def run_system_agent(validator, input_path, output_json):
    from run_pipeline import read_report_json, run_reported_agent, write_report_json
    def entrypoint(report):
        metadata_path = str(Path(report.run_dir) / 'run.json')
        metadata = read_report_json(metadata_path)
        metadata.update(mode='system', verification_scope='structural_integration_only', ready_for_backend=False)
        write_report_json(metadata_path, metadata)
        report.update(verification_scope='structural_integration_only', ready_for_backend=False)
        manifest_path = find_manifest([input_path])
        if manifest_path is None:
            raise ValueError('No controller manifest found at ' + input_path)
        report.step('system_structural_validation')
        output = Path(output_json).resolve()
        result = validate_system(validator, manifest_path, output, Path(report.directory) / 'work' / 'compile')
        report.artifact('validation_result.json', data=result)
        if result.get('compile_log'):
            report.artifact('compile.log', source=result['compile_log'])
        report.update(verification_status='PASS' if result['is_valid'] else 'FAIL',
                      verification_readiness=result['verification_readiness'],
                      failure_categories=result['failure_categories'],
                      failure_detail='\n'.join(result['errors']) or None,
                      checks=result.get('checks', {}))
        print_system_result(result)
        if not result['is_valid']:
            raise SystemExit(1)
    run_reported_agent('agent1', entrypoint, [input_path])
