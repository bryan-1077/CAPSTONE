"""Bounded structural parser for generated UVM infrastructure.

Tokenizes elsewhere, parses balanced declarations/statements here, resolves
typed handles, and inlines explicit helpers. This is not an SV elaborator: VCS
checks language/type correctness. Unsupported constructs fail closed with a
specific diagnostic; trusted runtime code is never parsed/reinterpreted here.
"""
from collections import Counter


ROLES = {'dut_driver': 'driver', 'dut_sequencer': 'sequencer', 'dut_monitor': 'monitor',
         'dut_agent': 'agent', 'dut_scoreboard': 'scoreboard', 'dut_env': 'env',
         'dut_sequence': 'sequence_instance', 'virtual dut_if': 'vif', 'uvm_phase': 'phase'}
INFRA_CLASSES = ('dut_sequencer', 'dut_agent', 'dut_env', 'dut_test')


def balanced(words, start, opening='(', closing=')'):
    if start >= len(words) or words[start] != opening:
        raise ValueError('Expected ' + opening + ' in structural declaration/call')
    depth = 1
    stop = start + 1
    while stop < len(words):
        if words[stop] == opening:
            depth += 1
        elif words[stop] == closing:
            depth -= 1
        if depth == 0:
            return words[start + 1:stop], stop + 1
        stop += 1
    raise ValueError('Unclosed ' + opening + ' in generated infrastructure')


def split_arguments(words):
    result, start, depth = [], 0, 0
    for index, word in enumerate(words):
        if word in ('(', '[', '{'):
            depth += 1
        elif word in (')', ']', '}'):
            depth -= 1
        elif word == ',' and depth == 0:
            result.append(words[start:index])
            start = index + 1
    if words[start:]:
        result.append(words[start:])
    return result


def declaration(words):
    """Return a supported typed handle/parameter declaration and initializer."""
    words = list(words)
    while words and words[0] in ('input', 'output', 'inout', 'const', 'ref'):
        words.pop(0)
    if words[:2] == ['virtual', 'dut_if']:
        kind, offset = 'virtual dut_if', 2
    elif words:
        kind, offset = words[0], 1
    else:
        raise ValueError('Empty declaration')
    if len(words) <= offset:
        raise ValueError('Declaration needs a variable name: ' + ' '.join(words))
    name = words[offset]
    tail = words[offset + 1:]
    if tail and tail[0] != '=':
        raise ValueError('Unsupported structural declaration: ' + ' '.join(words))
    return kind, name, tail[1:] if tail else None


def parse_classes(words):
    classes, index = {}, 0
    while index < len(words):
        if words[index] == ';':
            index += 1
            continue
        if words[index] != 'class':
            raise ValueError('Unexpected class-level token: ' + words[index])
        index += 1
        name = words[index]
        if name in classes:
            raise ValueError('Duplicate generated class: ' + name)
        index += 1
        end_header = words.index(';', index)
        header = words[index:end_header]
        fields, methods, registrations = {}, {}, []
        index = end_header + 1
        while index < len(words) and words[index] != 'endclass':
            if words[index] == ';':
                index += 1
                continue
            if words[index:index + 2] == ['`', 'uvm_component_utils']:
                args, index = balanced(words, index + 2)
                registrations.append(args)
                continue
            start = index
            while index < len(words) and words[index] in ('virtual', 'protected', 'local', 'static'):
                index += 1
            if words[index] in ('function', 'task'):
                method_kind = words[index]
                index += 1
                opening = words.index('(', index)
                signature = words[index:opening]
                method_name = signature[-1]
                params, index = balanced(words, opening)
                if words[index] != ';':
                    raise ValueError(name + '.' + method_name + ': method header needs semicolon')
                index += 1
                terminator = 'endfunction' if method_kind == 'function' else 'endtask'
                end_method = words.index(terminator, index)
                if method_name in methods:
                    raise ValueError(name + ': duplicate method ' + method_name)
                methods[method_name] = {'body': words[index:end_method],
                                        'params': [declaration(p) for p in split_arguments(params)],
                                        'kind': method_kind, 'signature': signature[:-1],
                                        'qualifiers': words[start:opening - len(signature) - 1]}
                index = end_method + 1
                if words[index:index + 1] == [':']:
                    if words[index + 1] != method_name:
                        raise ValueError('Mismatched end label: ' + method_name)
                    index += 2
            else:
                index = start
                stop = words.index(';', index)
                kind, field, initializer = declaration(words[index:stop])
                if initializer is not None or field in fields:
                    raise ValueError(name + ': initialized/duplicate structural field ' + field)
                fields[field] = kind
                index = stop + 1
        if index >= len(words):
            raise ValueError('Missing endclass: ' + name)
        index += 1
        if words[index:index + 1] == [':']:
            if words[index + 1] != name:
                raise ValueError('Mismatched endclass label: ' + name)
            index += 2
        classes[name] = {'header': header, 'fields': fields, 'methods': methods,
                         'registrations': registrations}
    return classes


def canonical(words, owner, classes, locals_):
    """Resolve a typed receiver and each member in its actual class scope."""
    words = list(words)
    while words[:1] == ['(']:
        inner, stop = balanced(words, 0)
        if stop != len(words):
            break
        words = inner
    # Parenthesized receivers/port arguments are equivalent expressions. Keep
    # call parentheses and every operator-bearing expression intact.
    cursor = 0
    while cursor < len(words):
        if words[cursor] == '(' and (cursor == 0 or words[cursor - 1] in ('(', ',', '=', '.')):
            inner, stop = balanced(words, cursor)
            if inner and all(word == '.' or word.replace('_', '').isalnum() for word in inner):
                words[cursor:stop] = inner
                continue
        cursor += 1
    result, index = [], 0
    fields = classes[owner]['fields']
    scope = dict(fields, **locals_)
    while index < len(words):
        if words[index:index + 2] == ['this', '.']:
            index += 2
        word = words[index]
        kind = scope.get(word)
        if kind in ROLES:
            result.append(ROLES[kind])
            index += 1
            while words[index:index + 1] == ['.'] and index + 1 < len(words):
                member = words[index + 1]
                member_kind = classes.get(kind, {}).get('fields', {}).get(member)
                result.extend(['.', ROLES.get(member_kind, member)])
                kind = member_kind
                index += 2
        elif kind in ('string', 'uvm_component'):
            result.append('name' if kind == 'string' else 'parent')
            index += 1
        else:
            result.append(word)
            index += 1
    return tuple(result)


def creation(expr, kind):
    if expr and expr[0] == 'new':
        opening = 1
    elif list(expr[:5]) == [kind, '::', 'type_id', '::', 'create']:
        opening = 5
    else:
        return None
    args, stop = balanced(list(expr), opening)
    values = split_arguments(args)
    if stop != len(expr) or not values or len(values[0]) != 1 or not values[0][0].startswith('"'):
        raise ValueError('Factory/new construction needs a literal instance name')
    parents = values[1:]
    if kind != 'dut_sequence' and parents != [['this']]:
        raise ValueError(kind + ' must be constructed with its owning component as parent')
    if kind == 'dut_sequence' and parents not in ([], [['this']]):
        raise ValueError('Sequence construction has an unsupported parent/context')
    return ('create', ROLES[kind], kind)


def method_actions(owner, method, classes, stack=()):
    if method in stack:
        raise ValueError(owner + ': recursive structural helper ' + method)
    definition = classes[owner]['methods'][method]
    scope = {name: kind for kind, name, _ in definition['params']}
    words = definition['body']
    index = 0
    used = {method}

    def statement():
        nonlocal index
        if words[index] == ';':
            index += 1
            return []
        if words[index] == 'begin':
            index += 1
            label = None
            if words[index:index + 1] == [':']:
                label = words[index + 1]
                index += 2
            actions = []
            while index < len(words) and words[index] != 'end':
                actions.extend(statement())
            if index >= len(words):
                raise ValueError(owner + '.' + method + ': unclosed begin block')
            index += 1
            if words[index:index + 1] == [':']:
                if words[index + 1] != label:
                    raise ValueError('Mismatched begin/end label')
                index += 2
            return actions
        if words[index] == 'if':
            condition, index = balanced(words, index + 1)
            then = statement()
            otherwise = []
            if words[index:index + 1] == ['else']:
                index += 1
                otherwise = statement()
            return [('if', canonical(condition, owner, classes, scope), tuple(then), tuple(otherwise))]
        if words[index:index + 2] == ['`', 'uvm_fatal']:
            args, index = balanced(words, index + 2)
            values = split_arguments(args)
            if len(values) != 2 or any(len(value) != 1 or not value[0].startswith('"') for value in values):
                raise ValueError('Protected fatal requires its severity ID and literal diagnostic')
            # Message wording is diagnostic, not behavior; severity/ID are fixed.
            return [('fatal', values[0][0])]
        start, depth = index, 0
        while index < len(words):
            word = words[index]
            if word == ';' and depth == 0:
                break
            if word in ('(', '[', '{'):
                depth += 1
            elif word in (')', ']', '}'):
                depth -= 1
            index += 1
        if index >= len(words):
            raise ValueError(owner + '.' + method + ': statement needs semicolon')
        text = words[start:index]
        index += 1
        if text and (text[0] in ROLES or text[:2] == ['virtual', 'dut_if']):
            kind, name, initializer = declaration(text)
            if name in scope or name in classes[owner]['fields']:
                raise ValueError(owner + '.' + method + ': shadowed/duplicate handle ' + name)
            scope[name] = kind
            if initializer is None:
                return []
            text = [name, '='] + initializer
        normalized = canonical(text, owner, classes, scope)
        helper = list(normalized)
        if helper[:2] == ['this', '.']:
            helper = helper[2:]
        if helper and helper[0] in classes[owner]['methods'] and helper[1:2] == ['(']:
            arguments, stop = balanced(helper, 1)
            target = classes[owner]['methods'][helper[0]]
            if stop != len(helper) or target['params'] and (
                    [kind for kind, _, _ in target['params']] != ['uvm_phase'] or arguments != ['phase']) or (
                    not target['params'] and arguments):
                raise ValueError('Helper arguments must be empty or the current uvm_phase')
            actions, nested_used = method_actions(owner, helper[0], classes, stack + (method,))
            used.update(nested_used)
            return actions
        if len(normalized) > 2 and normalized[1] == '=':
            lhs = text[0] if text[:2] != ['this', '.'] else text[2]
            kind = scope.get(lhs, classes[owner]['fields'].get(lhs))
            if kind in ROLES and kind.startswith('dut_'):
                made = creation(normalized[2:], kind)
                if made:
                    return [made]
        return [('statement', normalized)]

    actions = []
    while index < len(words):
        actions.extend(statement())
    return actions, used


def compare_structure(words, expected_words):
    """Return named topology/lifecycle rule failures, not a token-equality verdict."""
    failures = {'topology': [], 'lifecycle': []}
    try:
        classes = parse_classes(words)
        expected = parse_classes(expected_words)
    except (ValueError, IndexError) as exc:
        failures['topology'].append('STRUCTURAL_PARSE: ' + str(exc))
        return failures
    for owner in INFRA_CLASSES:
        if owner not in classes:
            failures['topology'].append('REQUIRED_CLASS: missing ' + owner)
            continue
        actual, original = classes[owner], expected[owner]
        if actual['header'] != original['header']:
            failures['topology'].append(owner + ': must extend ' + ' '.join(original['header'][1:]))
        if actual['registrations'] != [[owner]]:
            failures['topology'].append(owner + ': requires one matching uvm_component_utils registration')
        if Counter(actual['fields'].values()) != Counter(original['fields'].values()):
            failures['topology'].append(owner + ': required typed handles are ' + ', '.join(original['fields'].values()))
        used = set()
        for method in original['methods']:
            category = 'lifecycle' if owner == 'dut_test' or method == 'new' else 'topology'
            if method not in actual['methods']:
                failures[category].append(owner + ': missing ' + method)
                continue
            try:
                adef, edef = actual['methods'][method], original['methods'][method]
                if (adef['kind'] != edef['kind'] or adef['signature'] != edef['signature'] or
                        [p[0] for p in adef['params']] != [p[0] for p in edef['params']] or
                        'static' in adef['qualifiers']):
                    raise ValueError('phase/constructor signature differs from its UVM contract')
                actions, reached = method_actions(owner, method, classes)
                required, _ = method_actions(owner, method, expected)
                used.update(reached)
                if method == 'connect_phase':
                    harmless = ('statement', ('super', '.', 'connect_phase', '(', 'phase', ')'))
                    actions = [a for a in actions if a != harmless]
                if method == 'build_phase':
                    matches = Counter(actions) == Counter(required)
                elif method == 'run_phase':
                    # Create, raise and vif propagation may be arranged before
                    # start, but finalization/failure/drop ordering is fixed.
                    matches = (len(actions) == len(required) and
                               Counter(actions[:3]) == Counter(required[:3]) and actions[3:] == required[3:])
                    if matches:
                        matches = actions.index(('create', 'sequence_instance', 'dut_sequence')) < actions.index(
                            ('statement', ('sequence_instance', '.', 'vif', '=', 'vif')))
                else:
                    matches = actions == required
                if not matches:
                    missing = list((Counter(required) - Counter(actions)).elements())
                    unexpected = list((Counter(actions) - Counter(required)).elements())
                    def describe(action):
                        if action[0] == 'create':
                            return 'instantiate ' + action[2] + ' through its owning component'
                        if action[0] == 'statement':
                            return 'execute ' + ' '.join(action[1])
                        return str(action)
                    detail = ('missing: ' + ', '.join(describe(a) for a in missing) if missing else '') + (
                        '; unexpected: ' + ', '.join(describe(a) for a in unexpected) if unexpected else '')
                    failures[category].append(owner + '.' + method + ': ' +
                        (detail or 'required lifecycle actions are out of order'))
            except (ValueError, IndexError, TypeError) as exc:
                failures[category].append(owner + '.' + method + ': ' + str(exc))
        unreviewed = set(actual['methods']) - used
        if unreviewed:
            failures['lifecycle'].append(owner + ': unreviewed callbacks/helpers: ' + ', '.join(sorted(unreviewed)))
    unexpected = set(classes) - set(INFRA_CLASSES)
    if unexpected:
        failures['topology'].append('Unexpected infrastructure classes: ' + ', '.join(sorted(unexpected)))
    return failures
