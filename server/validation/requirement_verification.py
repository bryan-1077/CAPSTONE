"""Shared requirement IDs and conservative, evidence-based verification (Python 3.6)."""
import re

def append_requirement(
    requirements,
    seen,
    category,
    source,
    text
):
    if not isinstance(
        text,
        str
    ):

        return

    text = " ".join(
        text.split()
    ).strip()

    if not text:

        return

    key = (
        category,
        text.lower()
    )

    if key in seen:

        return

    seen.add(
        key
    )

    requirements.append(
        {
            "id":
                "REQ-{:03d}".format(
                    len(
                        requirements
                    )
                    +
                    1
                ),

            "category":
                category,

            "source":
                source,

            "text":
                text,
        }
    )


def append_requirement_list(
    requirements,
    seen,
    category,
    source,
    value
):
    if not isinstance(
        value,
        list
    ):

        return

    for item in value:

        append_requirement(
            requirements,
            seen,
            category,
            source,
            item,
        )


def extract_implementation_constraints(
    spec,
    requirements,
    seen
):
    value = spec.get(
        "implementation_constraints"
    )

    # ========================================================
    # LIST FORMAT
    # ========================================================

    if isinstance(
        value,
        list
    ):

        append_requirement_list(
            requirements,
            seen,
            "implementation_constraint",
            "implementation_constraints",
            value,
        )

        return

    # ========================================================
    # OBJECT FORMAT
    # ========================================================

    if isinstance(
        value,
        dict
    ):

        append_requirement_list(
            requirements,
            seen,
            "implementation_required",
            "implementation_constraints.required",
            value.get(
                "required"
            ),
        )

        append_requirement_list(
            requirements,
            seen,
            "implementation_forbidden",
            "implementation_constraints.forbidden",
            value.get(
                "forbidden"
            ),
        )


def extract_request_struct_requirements(
    behavior,
    requirements,
    seen
):
    request_struct = behavior.get(
        "request_struct"
    )

    if not isinstance(
        request_struct,
        dict
    ):

        return

    typedef_name = request_struct.get(
        "typedef_name"
    )

    if (
        isinstance(
            typedef_name,
            str
        )
        and
        typedef_name.strip()
    ):

        append_requirement(
            requirements,
            seen,
            "structure",
            "behavior.request_struct.typedef_name",
            "Request structure typedef name is {}".format(
                typedef_name.strip()
            ),
        )

    packed = request_struct.get(
        "packed"
    )

    if isinstance(
        packed,
        bool
    ):

        append_requirement(
            requirements,
            seen,
            "structure",
            "behavior.request_struct.packed",
            "Request structure packed is {}".format(
                packed
            ),
        )

    fields = request_struct.get(
        "fields"
    )

    if isinstance(
        fields,
        list
    ):

        descriptions = []

        for field in fields:

            if not isinstance(
                field,
                dict
            ):

                continue

            name = field.get(
                "name"
            )

            width = field.get(
                "width"
            )

            if (
                name is not None
                and
                width is not None
            ):

                descriptions.append(
                    "{}:{}".format(
                        name,
                        width
                    )
                )

        if descriptions:

            append_requirement(
                requirements,
                seen,
                "structure",
                "behavior.request_struct.fields",
                "Request structure fields are {}".format(
                    ", ".join(
                        descriptions
                    )
                ),
            )


def extract_fsm_requirements(spec, requirements, seen):
    """Translate the approved FSM graph into traceable runtime obligations.

    State encodings and signal paths come from RTL only for observation. The
    expected graph, guard expressions and reset target come from the YAML.
    """
    machine = spec.get('state_machine')
    if not isinstance(machine, dict):
        return
    reset = spec.get('reset') if isinstance(spec.get('reset'), dict) else {}
    target = machine.get('reset_state')
    if isinstance(target, str) and target.strip():
        polarity = ('active-low' if reset['active_low'] else 'active-high') if 'active_low' in reset else 'specified polarity'
        timing = ('synchronous' if reset['synchronous'] else 'asynchronous') if 'synchronous' in reset else 'specified timing'
        append_requirement(requirements, seen, 'fsm_reset', 'state_machine.reset_state',
                           'Reset {} ({}, {}) must place the observed FSM state in {}.'.format(
                               reset.get('name', 'as specified'), timing, polarity, target))
    states = machine.get('states', [])
    names = [s if isinstance(s, str) else s.get('name') if isinstance(s, dict) else None for s in states]
    names = [s for s in names if isinstance(s, str) and s.strip()]
    if names:
        append_requirement(requirements, seen, 'fsm_state', 'state_machine.states',
                           'After reset, the observed FSM state must be a known member of: {}.'.format(', '.join(names)))
    for index, transition in enumerate(machine.get('transitions', [])):
        if not isinstance(transition, dict):
            continue
        source, destination, guard = (transition.get(key) for key in ('from', 'to', 'condition'))
        if not all(isinstance(value, str) and value.strip() for value in (source, destination, guard)):
            continue
        append_requirement(requirements, seen, 'fsm_transition', 'state_machine.transitions[{}]'.format(index),
                           'Outside reset, when the pre-edge state is {} and ({}) is true, '
                           'the observed state after the consuming edge must be {}. '
                           'Compare DUT state, not the predictor alone; preserve the YAML guard exactly.'.format(
                               source, guard, destination))
    if names and machine.get('transitions'):
        append_requirement(requirements, seen, 'fsm_hold', 'state_machine.transitions',
                           'Outside reset, when none of the YAML outgoing transition conditions for the '
                           'current state are true, the observed FSM state must remain unchanged.')
    # Port prose and explicit output equations are part of an FSM's specification
    # even when the optional behavior.rules section is absent.
    for index, port in enumerate(spec.get('ports', [])):
        if (isinstance(port, dict) and port.get('direction', port.get('dir')) == 'output' and
                isinstance(port.get('description'), str) and port['description'].strip()):
            append_requirement(requirements, seen, 'fsm_output', 'ports[{}].description'.format(index),
                               'Output {}: {}'.format(port.get('name'), port['description']))
    overrides = spec.get('output_overrides', {})
    if isinstance(overrides, dict):
        for name, expression in sorted(overrides.items()):
            if isinstance(expression, (str, int, bool)):
                append_requirement(requirements, seen, 'fsm_output', 'output_overrides.' + str(name),
                                   'Observed output {} must equal ({}).'.format(name, expression))


def build_requirement_catalog(spec):
    requirements = []

    seen = set()

    behavior = spec.get(
        "behavior",
        {}
    )

    if not isinstance(
        behavior,
        dict
    ):

        behavior = {}

    # ========================================================
    # CORRECTNESS CRITERIA
    # ========================================================

    append_requirement_list(
        requirements,
        seen,
        "correctness_criterion",
        "behavior.correctness_criteria",
        behavior.get(
            "correctness_criteria"
        ),
    )

    # ========================================================
    # BEHAVIOR RULES
    # ========================================================

    append_requirement_list(
        requirements,
        seen,
        "behavior_rule",
        "behavior.rules",
        behavior.get(
            "rules"
        ),
    )

    # ========================================================
    # COMBINATIONAL BEHAVIOR
    # ========================================================

    append_requirement(
        requirements,
        seen,
        "combinational_behavior",
        "behavior.combinational_behavior",
        behavior.get(
            "combinational_behavior"
        ),
    )

    # ========================================================
    # SEQUENTIAL BEHAVIOR
    # ========================================================

    append_requirement(
        requirements,
        seen,
        "sequential_behavior",
        "behavior.sequential_behavior",
        behavior.get(
            "sequential_behavior"
        ),
    )

    # ========================================================
    # INVARIANTS
    # ========================================================

    append_requirement_list(
        requirements,
        seen,
        "invariant",
        "invariants",
        spec.get(
            "invariants"
        ),
    )

    # ========================================================
    # IMPLEMENTATION CONSTRAINTS
    # ========================================================

    extract_implementation_constraints(
        spec,
        requirements,
        seen,
    )

    # ========================================================
    # FORBIDDEN PATTERNS
    # ========================================================

    append_requirement_list(
        requirements,
        seen,
        "forbidden_pattern",
        "forbidden_patterns",
        spec.get(
            "forbidden_patterns"
        ),
    )

    # ========================================================
    # DEPTH
    # ========================================================

    if "depth_default" in behavior:

        append_requirement(
            requirements,
            seen,
            "structure",
            "behavior.depth_default",
            "Default depth is {}".format(
                behavior.get(
                    "depth_default"
                )
            ),
        )

    # ========================================================
    # REQUEST STRUCT
    # ========================================================

    extract_request_struct_requirements(
        behavior,
        requirements,
        seen,
    )

    # Append new schema coverage so existing behavior-based IDs stay stable.
    extract_fsm_requirements(spec, requirements, seen)

    split_rows = []
    for row in requirements:
        parts = row["text"].split(";", 1)
        if len(parts) == 2 and "does not contain a valid field" in parts[0]:
            for part in parts:
                child = dict(row)
                child["text"] = part.strip()
                split_rows.append(child)
        else:
            split_rows.append(row)
    for index, row in enumerate(split_rows, 1):
        row["id"] = "REQ-{:03d}".format(index)
    return split_rows



def attach_static_results(requirements, rtl_text):
    """Only prove simple declarations; unsupported syntax remains unverified."""
    clean = re.sub(r'/\*.*?\*/|//[^\n]*', '', rtl_text, flags=re.S)
    # Preprocessing can select inactive declarations. Never infer proof from them.
    ambiguous = bool(re.search(r'`(?:ifdef|ifndef|include|define)', clean)) or len(re.findall(r'\bmodule\s+', clean)) != 1
    structs = {}
    for m in re.finditer(r'typedef\s+struct\s+(packed\s*)?\{([^{}]*)\}\s*(\w+)\s*;', clean):
        fields = []
        valid = True
        for declaration in m.group(2).split(';'):
            if not declaration.strip():
                continue
            field = re.fullmatch(r'\s*(?:logic|bit)\s*(?:\[(\d+)\s*:\s*(\d+)\])?\s*(\w+)\s*', declaration)
            if not field:
                valid = False
                break
            fields.append((field.group(3), abs(int(field.group(1))-int(field.group(2)))+1 if field.group(1) else 1))
        structs.setdefault(m.group(3), []).append((bool(m.group(1)), fields if valid else None, m.group(0)))

    for req in requirements:
        text = req['text']
        normalized = re.sub(r'[_\s-]+', ' ', text.lower()).strip()
        # These describe how the source is implemented, not port behavior.
        # An output trace cannot prove absence of an alternative implementation.
        source_strategy = normalized in {
            'timestamp tracking', 'shift register window', 'window tracking',
            'multi event counting', 'decrement outside counter',
            'use cooldown counter', 'cycle based model', 'parameterize delay',
        }
        status, evidence, method = 'NOT_TESTED', 'Requires executable behavioral checks.', 'simulation'
        match = re.fullmatch(r'(\w+) parameter defaults to (\d+)', text, re.I)
        if not match:
            depth = re.fullmatch(r'Default depth is (\d+)', text)
            if depth:
                match = re.fullmatch(r'(\w+) parameter defaults to (\d+)', 'DEPTH parameter defaults to '+depth.group(1))
        if match:
            method = 'static'
            values = re.findall(r'\bparameter\s+(?:(?:int|integer|unsigned|signed)\s+)*'+re.escape(match.group(1))+r'\s*=\s*(\d+)\s*(?=[,);])', clean)
            evidence = 'Unresolved or ambiguous parameter declaration.'
            if len(values) == 1:
                status = 'PASS' if int(values[0]) == int(match.group(2)) else 'FAIL'
                evidence = 'parameter {} = {}'.format(match.group(1), values[0])
        else:
            name_match = re.fullmatch(r'Request structure typedef name is (\w+)', text)
            no_valid = re.fullmatch(r'(\w+) does not contain a valid field', text)
            if name_match or no_valid:
                method = 'static'
                name = (name_match or no_valid).group(1)
                declarations = structs.get(name, [])
                evidence = 'No uniquely parsed typedef for {}.'.format(name)
                if len(declarations) == 1:
                    packed, fields, declaration = declarations[0]
                    if name_match or fields is not None:
                        status = 'PASS' if name_match or not any(n == 'valid' for n,w in fields) else 'FAIL'
                        evidence = declaration
            elif text.startswith('Request structure packed is ') or text.startswith('Request structure fields are '):
                method = 'static'
                evidence = 'No unique fully parsed struct declaration.'
                all_structs = [d for group in structs.values() for d in group]
                if len(all_structs) == 1:
                    packed, fields, declaration = all_structs[0]
                    if text.startswith('Request structure packed is '):
                        status = 'PASS' if str(packed) == text.rsplit(' ', 1)[-1] else 'FAIL'
                    elif fields is not None:
                        actual = ', '.join('{}:{}'.format(n,w) for n,w in fields)
                        status = 'PASS' if actual == text[len('Request structure fields are '):] else 'FAIL'
                    evidence = declaration
            elif re.fullmatch(r'do not include a valid field inside (\w+)', text):
                method = 'static'
                name = text.rsplit(' ', 1)[-1]
                declarations = structs.get(name, [])
                evidence = 'No unique fully parsed typedef for ' + name
                if len(declarations) == 1 and declarations[0][1] is not None:
                    status = 'FAIL' if any(n == 'valid' for n,w in declarations[0][1]) else 'PASS'
                    evidence = declarations[0][2]
            elif source_strategy or req['category'] == 'structure' or re.search(r'\b(?:typedef|packed struct|valid field inside|explicit.*signal|next-state ordering|use request_t)\b', text, re.I):
                method = 'static'
                evidence = ('Source/implementation constraint requires structural evidence; '
                            'no implemented static checker for this constraint.')
        if ambiguous and method == 'static':
            status, evidence = 'NOT_TESTED', 'Preprocessed RTL requires an elaboration-aware checker.'
        req['verification_method'] = method
        req['static_result'] = {'status': status, 'evidence': evidence, 'method': 'static'}
    return requirements


def combine_requirement_results(requirements, simulation_report):
    dynamic = {r['id']: r for r in simulation_report['requirements']}
    rows = []
    for req in requirements:
        static = req['static_result']
        sim = dynamic.get(req['id'], {'status': 'NOT_TESTED', 'evidence': 'Missing simulation result.'})
        selected = static if req['verification_method'] == 'static' else sim
        # A reported dynamic failure must never be erased by static evidence.
        status = 'FAIL' if sim['status'] == 'FAIL' or static['status'] == 'FAIL' else selected['status']
        rows.append({'id': req['id'], 'requirement': req['text'], 'source': req['source'],
                     'method': req['verification_method'], 'status': status,
                     'evidence': selected['evidence'], 'static': static, 'simulation': sim})
    failed = sum(r['status'] == 'FAIL' for r in rows)
    gaps = sum(r['status'] == 'NOT_TESTED' for r in rows)
    overall = 'FAIL' if failed or simulation_report['overall_status'] == 'FAIL' else ('INCOMPLETE' if gaps or not rows else 'PASS')
    return {'overall_status': overall, 'total_requirements': len(rows),
            'passed_requirements': len(rows)-failed-gaps, 'failed_requirements': failed,
            'not_tested_requirements': gaps, 'requirements': rows}
