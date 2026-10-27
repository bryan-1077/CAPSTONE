#!/usr/bin/env python3
"""Elaborate system behavior, then derive a reviewable architecture package.

Two bounded model stages produce specification data only. Deterministic checks
and the existing exact-export approval boundary precede all RTL generation.
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import os
from pathlib import Path
import tempfile

import yaml

from architecture import ArchitectureError, ROOT, fingerprint, identifier
from generate_microarch import DEFAULT_MODEL, call_llm, parse_response
from specification import (SpecificationError, SpecificationErrors, _package_header, configuration_axes, fields, load_package,
                           names, read_document, text_value, validate_metadata,
                           validate_matrix, validate_shared)
from specification_request import build_specification_request, validate_request_rules
from prompt_context import compact_json, serialize_context
from specification_edits import apply_architecture_edits, apply_system_edits
from specification_change_audit import audit_changes


GENERATOR_REVISION = 19
DOCUMENTS = {name: name + '.yaml' for name in
             ('shared', 'system', 'execution', 'modules', 'graph', 'verification')}
REFERENCE = ROOT / 'microarch/specification/ddr4_fifo/package.yaml'
GUIDE = ROOT / 'microarch/specification/generation_contract.md'
ARCHITECTURE_SHAPE = ('{"status":"supported","modules":{"contracts":{}},'
                      '"internal_types":{},"graph":{},"verification":{},"rule_owners":{}}')


class UnsupportedSpecification(SpecificationError):
    pass


def generation_context(catalog=None):
    from specification_lowering import COMPILER_REVISION
    from planning_library import planning_catalog, planning_index
    package, documents, library = load_package(REFERENCE)
    # This example teaches the data format; none of its behavioral choices are
    # implicitly imposed on requests. No feature-to-template registry is used.
    package.pop('review', None)
    package.pop('generation', None)
    return {'generator_revision': GENERATOR_REVISION, 'lowering_revision': COMPILER_REVISION,
            'contract': GUIDE.read_text(),
            'format_example': dict(package=package, **documents), 'timing_library': library,
            'local_library_catalog': planning_index(planning_catalog() if catalog is None else catalog)}


def build_prompt(stage, request, context, system=None, feedback='', *, fresh_decomposition=False,
                 repair_base=None, narrow_architecture=False):
    # Examples must match the stage's accepted shape, rather than contradict
    # its instructions with compiler-owned fields and already-assigned owners.
    prompt_context = copy.deepcopy(context)
    if feedback and not fresh_decomposition:
        # An existing candidate supplies structure; fresh decomposition still needs an example.
        prompt_context.pop('format_example')
        # Discovery is for initial/fresh planning. A repair keeps the locked
        # design and timing library; unused template names are not repair facts.
        prompt_context.pop('local_library_catalog', None)
    else:
        example = prompt_context['format_example']
        if stage == 'system':
            example = {key: example[key] for key in ('package', 'shared', 'system', 'execution')}
            for key in ('documents', 'design_kind', 'generation', 'review'):
                example['package'].pop(key, None)
            example['package']['scope']['pending'] = []
            for decision in example['package']['decisions'].values():
                if decision['status'] == 'accepted':
                    decision['status'] = 'recorded'
            example['shared']['clock_reset']['assertion_style'] = 'synchronous'
            for section in ('system', 'execution'):
                for rule in example[section]['rules'].values():
                    rule.pop('owners', None)
                for obligation in example[section]['verification'].values():
                    obligation.pop('rules', None)
        else:
            example = {'modules': example['modules'], 'graph': example['graph'],
                       'verification': example['verification'],
                       'rule_owners': {f'{section}.rules.{rid}': rule['owners']
                                      for section in ('system', 'execution')
                                      for rid, rule in example[section]['rules'].items()}}
            for contract in example['modules']['contracts'].values():
                contract.pop('invariants', None)
                contract.pop('verification', None)
        prompt_context['format_example'] = dict(status='supported', **example)
    instruction = (
        'Elaborate SYSTEM BEHAVIOR FIRST. Do not choose modules, module names, owners or wiring. '
        'Return package metadata, shared definitions, system rules and execution rules with '
        'independent verification scenarios/expected outcomes. Each rule excludes owners. '
        'Include EVERY request ID as a rule with verification refs. Omit its statement/origin; '
        'the compiler supplies exact request text and request.ID. Echoed fields are ignored. '
        'Add refinement rules with statement/origin as needed. Constraints are '
        'requirements too. Derive parameters and interfaces from THIS request. '
        'Omit obligation rules lists; the compiler derives them from rule verification refs. Resolve simultaneous '
        'events, reset, timing edge conventions, storage ownership requirements, ordering, '
        'backpressure and responses before decomposition. '
        'Return {"status":"supported","package":{...},"shared":{...},"system":{...},"execution":{...}}.'
        if stage == 'system' else
        'Derive contracts, typed graph and RTL-independent verification bindings from the '
        'LOCKED system; keep metadata, shared definitions, rules, scenarios and expected results. '
        'Return rule_owners for EVERY system.rules.ID and execution.rules.ID with nonempty '
        'contract-ID lists. Omit contract invariants/verification lists; the compiler derives them '
        'from rule_owners and locked rules. For EACH owner individually, it includes the rule and '
        'every fully qualified verification obligation. Explicit lists stay strictly validated. Declare accurate '
        'combinational dependencies. Optional internal_types adds types without replacing shared types. '
        'ARCHITECTURE RESPONSE SHAPE (populate the empty objects): '
        + ARCHITECTURE_SHAPE + '. '
        'modules contains ONLY contracts. Close contracts/modules before root siblings. '
        'internal_types is optional, a root sibling of modules, graph, verification and rule_owners.')
    if feedback:
        # Share repeated facts across request and locked system, without omitting
        # either document or maintaining a second schema-specific projection.
        context_text = ('REPAIR CONTEXT (request, schema and LOCKED SYSTEM):\n' + serialize_context({
            'request': request, 'capabilities': prompt_context, 'locked_system': system}))
    else:
        context_text = ('REQUEST:\n' + compact_json(request) + '\nFORMAT AND CAPABILITIES:\n' +
                        compact_json(prompt_context) + '\nLOCKED SYSTEM (null in first stage):\n' +
                        compact_json(system))
    mode = ''
    if feedback or fresh_decomposition:
        mode = ('FRESH DECOMPOSITION: Use the schema guide and format example. Generate a '
                'complete new architecture from the locked system; no architecture candidate exists yet. '
                if fresh_decomposition else
                'REPAIR MODE: Use schema and candidate below; worked example omitted. '
                'Return a COMPLETE corrected stage response, not a patch. Preserve request '
                'behavior and the locked system. ')
        mode += ('For context_format shared_definitions_v1, each '
                 '{"$ref":"#/definitions/C..."} substitutes its exact local definition recursively.\n')
    if repair_base is not None:
        instruction = ('Repair the architecture candidate using explicit object-field edits. '
                       'Return ONLY {"base_fingerprint":"' + fingerprint(repair_base) +
                       '","edits":[{"op":"set","path":"/graph/connections/source.port",'
                       '"value":["sink.port"]}]}. Each edit is set (with value) or remove '
                       '(without value). Paths are RFC 6901 pointers into the candidate. '
                       'Parents must exist; lists are replaced as a whole, not indexed. '
                       'Edit only modules, internal_types, graph, verification or rule_owners. '
                       'Keep edits narrowly focused on diagnostics and necessary dependent changes. '
                       'Do not regenerate the full architecture. Full compiler validation still applies.')
        mode = ('ARCHITECTURE EDIT MODE: Previous response below is the complete base candidate. '
                'Return edits, overriding any full-response instructions in the schema or feedback. '
                'Preserve unrelated connections and verification declarations. '
                'Expand shared_definitions_v1 references recursively when reading context.\n')
        if stage == 'architecture' and narrow_architecture:
            instruction += (
                ' SEMANTIC ARCHITECTURE REPAIR: Edit only the reported findings and necessary '
                'dependent fields. Paths must address nested fields, not entire stage roots. '
                'Do not replace or remove existing nonempty objects; edit fields inside them. '
                'Add missing fields when needed; replace list-valued fields as a whole. '
                'Preserve unrelated contract prose, module identities, graph connections, '
                'types, rule owners and verification bindings exactly. The complete current '
                'architecture is the base, not a format example or invitation to decompose again. '
                'The locked system, original request and configuration remain binding.')
        if stage == 'system':
            instruction = ('Repair the accepted system using explicit edits, never a complete rewrite. '
                           'Return ONLY {"base_fingerprint":"' + fingerprint(repair_base) +
                           '","edits":[{"op":"set","path":"/shared/events/EVENT/condition","value":"..."}]}. '
                           'Use set with value or remove without value; 1..100 edits. '
                           'RFC 6901 paths address nested package/shared/system/execution fields. '
                           'Parents must exist; replace lists as a whole. To change an existing nonempty '
                           'object, edit its fields rather than replace it. Preserve unrelated behavior '
                           'and definitions; edit only what findings and necessary consistency require. '
                           'Fix the reported case while preserving progress and priority for other cases. '
                           'Original request text, external blockers and all compiler checks remain binding.')
            mode = ('SYSTEM EDIT MODE: Previous response below is the complete base candidate. '
                    'Return edits, overriding full-response instructions in schema or feedback. '
                    'Expand shared_definitions_v1 references recursively when reading context.\n')
    return ('Generate a specification package, never RTL. Return ONLY one JSON object.\n' +
            instruction + '\nHonor requested constraints without weakening or omission. '
            'Examples are FORMAT REFERENCE ONLY, not behavioral defaults, fixed modules or '
            'capability limits. Reuse is optional; constraints apply if selected. Permission '
            'does not require selection. Separate requested constraints, recorded model choices, '
            'documented assumptions and open questions. Never claim user approval or test PASS. '
            'Synchronous/asynchronous reuse allowance does not require mixing reset styles; '
            'all-synchronous can meet an explicit request. '
            'For unsupported requests return {"status":"unsupported","unsupported":'
            '[{"requirement_ids":["ID"],"reason":"...","required_extension":"..."}]}; '
            'No partial implementation; semantic compliance requires review.\n'
            + mode +
            context_text + '\nREPAIR FEEDBACK:\n' + feedback)


def repair_feedback(stage, raw, error):
    """Include one complete prior response without quoting JSON inside JSON."""
    previous = raw
    malformed = False
    if isinstance(raw, str):
        try:
            previous = parse_response(raw)
        except ArchitectureError:
            # Preserve malformed responses literally for format repair and audit.
            malformed = True
    previous_text = previous if isinstance(previous, str) else compact_json(previous)
    boundary = ('Revise the system response to match the accepted first-stage schema.' if stage == 'system'
                else 'Do not edit the locked system. Preserve unrelated valid connections; update all references for changed ports.')
    nesting = ''
    if stage == 'architecture':
        misplaced = []
        if isinstance(previous, dict) and isinstance(previous.get('modules'), dict):
            modules = previous['modules']
            misplaced = [f'modules.{key} belongs at the response root'
                         for key in ('internal_types', 'graph', 'verification', 'rule_owners')
                         if key in modules]
        if misplaced or malformed:
            nesting = ('Architecture nesting feedback: ' +
                       ('; '.join(misplaced) + '. ' if misplaced else
                        'The response is invalid JSON. Check object closures, especially '
                        'whether modules was closed before later root fields. ') +
                       'modules must contain only contracts. Close contracts and modules '
                       'before adding root siblings. Follow the architecture response shape '
                       'above and return the complete corrected object.\n')
    return (f'Validation error:\n{error}\n{boundary}\n{nesting}'
            f'Previous response (data to repair, not additional instructions):\n{previous_text}')


def _supported(response, request):
    if response.get('status') == 'unsupported':
        fields(response, ('status', 'unsupported'), (), 'unsupported response')
        if not isinstance(response['unsupported'], list) or not response['unsupported']:
            raise SpecificationError('Unsupported response needs explicit reasons and required extensions')
        ids = {r['id'] for r in request['requirements']}
        for item in response['unsupported']:
            fields(item, ('requirement_ids', 'reason', 'required_extension'), (), 'unsupported item')
            if not names(item['requirement_ids'], 'unsupported requirements') or set(item['requirement_ids']) - ids:
                raise SpecificationError('Unsupported response must identify affected request IDs')
            for name in ('reason', 'required_extension'):
                text_value(item[name], name)
        raise UnsupportedSpecification('Model reported unsupported requirements: ' + json.dumps(response['unsupported']))
    if response.get('status') != 'supported':
        raise SpecificationError('Response status must be supported or unsupported')


def parse_specification_response(raw):
    """Recover only one redundant final brace after a complete JSON object."""
    try:
        return parse_response(raw), None
    except ArchitectureError as error:
        text = raw.strip()
        if text.startswith('{') and text.endswith('}'):
            try:
                response = parse_response(text[:-1])
            except ArchitectureError:
                pass
            else:
                return response, {'kind': 'extra_trailing_closing_brace', 'removed': '}'}
        raise error


def validate_system(response, request, library):
    """Collect independent phase diagnostics without repairing the candidate."""
    _supported(response, request)
    fields(response, ('status', 'package', 'shared', 'system', 'execution'), (), 'system response')
    system = copy.deepcopy({key: value for key, value in response.items() if key != 'status'})
    package = system['package']
    fields(package, (), None, 'package')
    if 'review' in package:
        raise SpecificationError('package.review: omit this field; no review may be fabricated by the model')
    forbidden = {'generation', 'design_kind', 'documents'} & package.keys()
    if forbidden:
        raise SpecificationError(f'Omit compiler-owned package fields {sorted(forbidden)}; the compiler supplies them after validation')
    package['documents'] = dict(DOCUMENTS)
    if request.get('design_kind'):
        package['design_kind'] = request['design_kind']
    _package_header(package)
    errors = []

    def check(function):
        try:
            return function()
        except SpecificationError as exc:
            errors.extend(getattr(exc, 'errors', [str(exc)]))
            return None

    def metadata():
        validate_metadata(package)
        for name in ('package_id', 'design_name'):
            identifier(package[name], name)
        if package['status'] != 'draft_for_review':
            raise SpecificationError('Generated package must remain draft_for_review')
        if package['verification_strategy']['status'] != 'planned_not_executed':
            raise SpecificationError('Generated verification strategy must remain planned_not_executed')
        if package['scope']['pending']:
            raise SpecificationError('Incomplete scope cannot proceed; report unsupported requests explicitly or use blocking questions')
        if any(d['status'] == 'accepted' for d in package['decisions'].values()):
            raise SpecificationError('The model cannot record user acceptance of decisions')

    check(metadata)
    from specification import expression

    def shared_definitions():
        shared = system['shared']
        parameters, widths, _, _ = validate_shared(shared, library)
        reset = shared['clock_reset']  # validate_shared checks the common convention.
        for key in ('clock', 'reset'):
            identifier(reset[key], key)
        for name, port in fields(shared['host_interface'], (), None, 'host_interface').items():
            identifier(name, 'host port')
            fields(port, ('direction', 'type'), ('when',), 'host.' + name)
            if port['direction'] not in ('input', 'output') or port['type'] not in {'shared.types.' + t for t in widths}:
                raise SpecificationError(f'{name}: invalid host port direction/type')
            if 'when' in port and type(expression(port['when'], parameters)) is not bool:
                raise SpecificationError(f'{name}: invalid presence condition')
            if ('when' not in port or expression(port['when'], parameters)) and widths[port['type'].removeprefix('shared.types.')] < 1:
                raise SpecificationError(f'{name}: present host port has zero width')
        for name in (reset['clock'], reset['reset']):
            port = shared['host_interface'].get(name, {})
            if port.get('direction') != 'input' or port.get('when') or widths.get(port.get('type', '').removeprefix('shared.types.')) != 1:
                raise SpecificationError(f'{name}: clock/reset must be unconditional one-bit host inputs')
        return parameters, widths

    shared_result = check(shared_definitions)

    def configurations():
        shared = system['shared']
        reset = shared['clock_reset']
        strategy = fields(package['verification_strategy'],
                          ('status', 'configurations', 'module_checks', 'system_scenario', 'system_evidence'),
                          (), 'verification_strategy')
        axes, profiles = configuration_axes(strategy['configurations'])
        fixed = [name for name in axes if name in shared['parameters']
                 and (shared['parameters'][name].get('configurable') is False
                      or not (shared['parameters'][name].get('configurable')
                              or 'allowed' in shared['parameters'][name]))]
        if fixed:
            raise SpecificationErrors([
                f'configurations.parameters.{name}: parameter is not configurable; '
                'omit this axis and retain its declared value in shared.parameters'
                for name in fixed])
        for values in itertools.product(*axes.values(), profiles):
            settings = dict(zip(axes, values[:-1]))
            variant_parameters, variant_widths, _, _ = validate_shared(shared, library, settings, values[-1])
            for name, port in shared['host_interface'].items():
                if 'when' in port and not expression(port['when'], variant_parameters):
                    continue
                width = variant_widths[port['type'].removeprefix('shared.types.')]
                if width < 1 or (name in (reset['clock'], reset['reset']) and width != 1):
                    raise SpecificationError(f'{name}: invalid host width in configuration matrix')

    # Matrix elaboration depends on usable shared types/parameters, not on
    # descriptive metadata or verification links being correct.
    if shared_result is not None:
        parameters, widths = shared_result
        check(configurations)

    requested = {r['id']: r['text'] for r in request['requirements']}
    for section in ('system', 'execution'):
        doc = fields(system[section], ('slice_id', 'rules', 'verification'), (), section)
        for rid, rule in fields(doc['rules'], (), None, section + '.rules').items():
            identifier(rid, 'rule ID')
            if rid in requested:
                fields(rule, ('verification',), ('origin', 'statement'), rid)
                # Canonical request text is compiler-owned. Keep raw model
                # responses unchanged in journals; normalize only this copy.
                rule.update(origin='request.' + rid, statement=requested[rid])
            else:
                fields(rule, ('origin', 'statement', 'verification'), (), rid)
            text_value(rule['statement'], rid)
            text_value(rule['origin'], rid + '.origin')
            if not names(rule['verification'], rid):
                raise SpecificationError(f'{rid}: verification obligation required')
        for vid, obligation in fields(doc['verification'], (), None, section + '.verification').items():
            fields(obligation, ('scenario', 'expected', 'status'), ('rules',), vid)
            for key in ('scenario', 'expected'):
                text_value(obligation[key], vid + '.' + key)
            if obligation['status'] != 'planned_not_executed':
                raise SpecificationError('Generated obligations must remain planned_not_executed')
            if 'rules' not in obligation:
                # The model explicitly chooses coverage on each rule. Reverse
                # references add bookkeeping only, never a coverage relationship.
                obligation['rules'] = sorted(rid for rid, rule in doc['rules'].items()
                                             if vid in rule['verification'])
            if not names(obligation['rules'], vid):
                raise SpecificationError(f'{vid}: rule references required')
    # Check relationships only after both slices have usable schema. Independent
    # link errors belong in one repair; malformed fields still stop above.
    for section in ('system', 'execution'):
        doc = system[section]
        for rid, rule in doc['rules'].items():
            for vid in rule['verification']:
                if vid not in doc['verification'] or rid not in doc['verification'][vid]['rules']:
                    errors.append(f'{section}.rules.{rid}: nonreciprocal verification reference {vid}')
        for vid, obligation in doc['verification'].items():
            for rid in obligation['rules']:
                if rid not in doc['rules'] or vid not in doc['rules'][rid]['verification']:
                    errors.append(f'{section}.verification.{vid}: nonreciprocal rule reference {rid}')
    check(lambda: validate_request_rules(request, system))
    if errors:
        raise SpecificationErrors(errors)
    if request.get('baseline_contract'):
        shared = system['shared']
        from controller_baseline import validate_baseline
        validate_baseline({'design_name': package['design_name'], 'profile': shared['timing_profile']['selection'],
            'top_ports': [{'name': name, 'direction': port['direction'],
                           'width': widths[port['type'].removeprefix('shared.types.')]}
                          for name, port in shared['host_interface'].items()
                          if 'when' not in port or expression(port['when'], parameters)],
            'requirements': [{'id': rid, 'text': rule['statement']} for section in ('system', 'execution')
                             for rid, rule in system[section]['rules'].items()]}, request['baseline_contract'])
    return system


def assemble_package(system, response, request, model, context_hash, repairs):
    _supported(response, request)
    fields(response, ('status', 'modules', 'graph', 'verification', 'rule_owners'), ('internal_types',), 'architecture response')
    package = copy.deepcopy(system['package'])
    documents = {key: copy.deepcopy(system[key]) for key in ('shared', 'system', 'execution')}
    documents.update({key: copy.deepcopy(response[key]) for key in ('modules', 'graph', 'verification')})
    internal_types = fields(response.get('internal_types', {}), (), None, 'internal_types')
    if internal_types.keys() & documents['shared']['types'].keys():
        raise SpecificationError('Internal types cannot replace locked shared types')
    documents['shared']['types'].update(copy.deepcopy(internal_types))
    rules = {f'{section}.rules.{rid}': rule for section in ('system', 'execution')
             for rid, rule in documents[section]['rules'].items()}
    owners = fields(response['rule_owners'], tuple(rules), (), 'rule_owners')
    for ref, rule in rules.items():
        rule['owners'] = names(owners[ref], ref + '.owners')
    # rule_owners is the architectural choice. These lists repeat that choice
    # and the locked rule-to-obligation links, so generated responses may omit
    # them. Never overwrite explicit lists or infer an owner/verification intent.
    contracts = fields(documents['modules'], ('contracts',), (), 'modules')['contracts']
    fields(contracts, (), None, 'contracts')
    derived = {name: {'invariants': set(), 'verification': set()} for name in contracts}
    for ref, rule in rules.items():
        prefix = ref.split('.')[0]
        for owner in rule['owners']:
            if owner in derived:
                derived[owner]['invariants'].add(ref)
                derived[owner]['verification'].update(
                    f'{prefix}.verification.{vid}' for vid in rule['verification'])
    for name, contract in contracts.items():
        if isinstance(contract, dict):
            for key, refs in derived[name].items():
                if key not in contract:
                    contract[key] = sorted(refs)
    package['generation'] = {
        'version': 1, 'model': model, 'request': copy.deepcopy(request),
        'input_fingerprint': fingerprint(request), 'context_fingerprint': context_hash,
        'system_fingerprint': fingerprint(system), 'internal_types': copy.deepcopy(internal_types),
        'validation_repairs': copy.deepcopy(repairs),
    }
    return package, documents


def _write_package(directory, package, documents, library, catalog=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'package.yaml').write_text(yaml.safe_dump(package, sort_keys=False))
    for name, document in documents.items():
        (directory / DOCUMENTS[name]).write_text(yaml.safe_dump(document, sort_keys=False))
    (directory / 'timing.yaml').write_text(yaml.safe_dump(library, sort_keys=False))
    if catalog is not None:
        (directory / 'library_catalog.json').write_text(json.dumps(catalog, indent=2) + '\n')


def propose_specification(request, *, output_dir=None, model=DEFAULT_MODEL, cache=False,
                          response_path=None, caller=None, repair_seed=None):
    """Return a validated package path. No approval or RTL is ever produced."""
    from specification_lowering import compile_package
    from planning_library import planning_catalog
    catalog = planning_catalog()
    context = generation_context(catalog=catalog)
    if repair_seed is not None:
        if cache or response_path:
            raise SpecificationError('Semantic repair cannot use a proposal cache or stage replay file')
        fields(repair_seed, ('stage', 'system', 'architecture', 'feedback', 'library',
                             'source_manifest_fingerprint', 'report_fingerprint'), (), 'repair seed')
        if repair_seed['stage'] not in ('system', 'architecture'):
            raise SpecificationError('Unknown semantic repair stage')
        context['timing_library'] = copy.deepcopy(repair_seed['library'])
        context['semantic_repair'] = {key: repair_seed[key] for key in
            ('stage', 'source_manifest_fingerprint', 'report_fingerprint')}
    context_hash = fingerprint(context)
    digest = fingerprint({'request': request, 'context': context_hash, 'model': model})
    output = Path(output_dir) if output_dir else ROOT / 'microarch/proposals/system' / digest[:16]
    package_path = output / 'package.yaml'
    if cache:
        if response_path:
            raise SpecificationError('--cache and --specification-response are mutually exclusive')
        manifest = compile_package(package_path)
        proof = manifest['provenance']['specification']['snapshot']['package'].get('generation', {})
        if proof.get('request') != request or proof.get('model') != model or proof.get('context_fingerprint') != context_hash:
            raise SpecificationError('Generated specification cache is stale: request, model, context or compiler changed')
        return package_path
    if output.exists():
        raise SpecificationError(f'Specification output already exists: {output}; use --cache or a fresh --specification-output')
    replay = read_document(response_path) if response_path else None
    if replay is not None:
        fields(replay, ('system', 'architecture'), (), 'specification replay')
    if caller is None:
        try:
            token_budget = int(os.environ.get('SPECIFICATION_MAX_TOKENS', '24000'))
        except ValueError as exc:
            raise SpecificationError('SPECIFICATION_MAX_TOKENS must be a positive integer') from exc
        if token_budget < 1:
            raise SpecificationError('SPECIFICATION_MAX_TOKENS must be a positive integer')
        caller = lambda prompt, selected: call_llm(prompt, selected, max_tokens=token_budget, label='Specification')
    repairs, accepted, attempts = [], {}, []
    journal = output.with_name(output.name + '.attempts.json')

    def record_attempts():
        journal.parent.mkdir(parents=True, exist_ok=True)
        temporary = journal.with_suffix(journal.suffix + '.tmp')
        temporary.write_text(json.dumps({'request_fingerprint': fingerprint(request),
                            'context_fingerprint': context_hash, 'model': model,
                            'attempts': attempts}, indent=2) + '\n')
        temporary.replace(journal)

    system = copy.deepcopy(repair_seed['system']) if repair_seed and repair_seed['stage'] == 'architecture' else None
    stages = ('architecture',) if repair_seed and repair_seed['stage'] == 'architecture' else ('system', 'architecture')
    if repair_seed:
        repairs.append({'stage': 'semantic_review', 'attempt': 1, 'error': repair_seed['feedback'],
                        'diagnostics': [repair_seed['feedback']]})
        if stages == ('architecture',):
            accepted['system'] = {'status': 'supported', **copy.deepcopy(system)}
            for key in ('documents', 'design_kind', 'generation', 'review'):
                accepted['system']['package'].pop(key, None)
    record_attempts()
    try:
        for stage in stages:
            repair_base = None
            narrow_architecture = bool(repair_seed and repair_seed['stage'] == 'architecture')
            title = 'SYSTEM SPECIFICATION' if stage == 'system' else 'ARCHITECTURE DECOMPOSITION'
            route = 'OFFLINE REPLAY' if replay is not None else 'LLM BASED'
            print('\n' + '=' * 80, flush=True)
            print(f'--- STARTING --- {title} ------ {route}', flush=True)
            print('=' * 80 + '\n', flush=True)
            semantic_feedback = repair_seed['feedback'] if repair_seed else ''
            feedback = ''
            if repair_seed:
                if stage == repair_seed['stage']:
                    candidate = copy.deepcopy(repair_seed[stage])
                    if stage == 'system':
                        candidate = {'status': 'supported', **candidate}
                        for key in ('documents', 'design_kind', 'generation', 'review'):
                            candidate['package'].pop(key, None)
                    repair_base = copy.deepcopy(candidate)
                    feedback = repair_feedback(stage, candidate, semantic_feedback)
                else:
                    feedback = semantic_feedback + '\nThe old architecture is invalidated. Generate a fresh complete decomposition of the new locked system.'
            for attempt in range(1 if replay is not None else 3):
                print(f'[SPECIFICATION] {stage}: {"replay" if replay is not None else model} (attempt {attempt + 1})', flush=True)
                if repair_base is not None:
                    print(f'[SPECIFICATION] Repairing {stage} with explicit edits.', flush=True)
                raw = replay[stage] if replay is not None else caller(build_prompt(stage, request, context, system if stage == 'architecture' else None, feedback,
                    fresh_decomposition=bool(repair_seed and repair_seed['stage'] == 'system'
                                             and stage == 'architecture' and attempt == 0),
                    repair_base=repair_base, narrow_architecture=narrow_architecture), model)
                entry = {'stage': stage, 'attempt': attempt + 1, 'response': copy.deepcopy(raw), 'status': 'received'}
                if repair_base is not None:
                    entry['repair_base_fingerprint'] = fingerprint(repair_base)
                attempts.append(entry)
                record_attempts()
                response = None
                candidate = None
                try:
                    if isinstance(raw, str):
                        response, recovery = parse_specification_response(raw)
                        if recovery:
                            entry['parse_recovery'] = recovery
                            record_attempts()
                            print('[SPECIFICATION] Removed one extra trailing closing brace; '
                                  'full validation still required.', flush=True)
                    else:
                        response = copy.deepcopy(raw)
                    if not isinstance(response, dict):
                        raise SpecificationError('Specification response must be an object')
                    if repair_base is not None:
                        response = (apply_system_edits(repair_base, response) if stage == 'system'
                                    else apply_architecture_edits(repair_base, response, narrow=narrow_architecture))
                        entry['applied_response'] = copy.deepcopy(response)
                        entry['applied_changes'] = audit_changes(repair_base, response)
                    candidate = copy.deepcopy(response)
                    if stage == 'system':
                        system = validate_system(response, request, context['timing_library'])
                        if repair_seed:
                            requested_ids = {r['id'] for r in request['requirements']}
                            for section in ('system', 'execution'):
                                for rid, rule in repair_seed['system'][section]['rules'].items():
                                    if rid in requested_ids:
                                        raw_rule = response[section]['rules'].get(rid, {})
                                        if any(raw_rule.get(key) != rule[key] for key in ('origin', 'statement')):
                                            candidate = None
                                            raise SpecificationError(f'{rid}: system repair cannot edit authoritative request text/origin')
                            for name, decision in repair_seed['system']['package']['decisions'].items():
                                if decision['status'] not in ('accepted', 'recorded') and system['package']['decisions'].get(name) != decision:
                                    candidate = None
                                    raise SpecificationError(f'{name}: automatic repair cannot remove or resolve a decision requiring external review')
                            for name, assumption in repair_seed['system']['package']['assumptions'].items():
                                if assumption['status'] == 'requires_review' and system['package']['assumptions'].get(name) != assumption:
                                    candidate = None
                                    raise SpecificationError(f'{name}: automatic repair cannot remove or resolve an assumption requiring external review')
                            for name, question in repair_seed['system']['package']['open_questions'].items():
                                if question['blocks'] != 'rtl_generation' and system['package']['open_questions'].get(name) != question:
                                    candidate = None
                                    raise SpecificationError(f'{name}: automatic repair cannot remove or resolve a question requiring agreement')
                        # Files always refer to the portable, compiler-owned library.
                        system['shared']['timing_profile']['source'] = 'timing.yaml'
                    else:
                        package, documents = assemble_package(system, response, request, model, context_hash, repairs)
                        # Run the real compiler, including module IR, graph and role
                        # validation, before publishing any usable package.
                        with tempfile.TemporaryDirectory(prefix='capstone-spec-') as temporary:
                            _write_package(Path(temporary), package, documents, context['timing_library'], catalog)
                            validate_matrix(Path(temporary) / 'package.yaml')
                            compile_package(Path(temporary) / 'package.yaml')
                    accepted[stage] = response
                    entry['status'] = 'accepted'
                    record_attempts()
                    break
                except UnsupportedSpecification as exc:
                    entry.update(status='unsupported', error=str(exc))
                    record_attempts()
                    raise
                except (SpecificationError, ArchitectureError, KeyError, TypeError, ValueError) as exc:
                    diagnostics = getattr(exc, 'errors', [str(exc)])
                    entry.update(status='rejected', error=str(exc), diagnostics=diagnostics)
                    record_attempts()
                    repairs.append({'stage': stage, 'attempt': attempt + 1, 'error': str(exc),
                                    'diagnostics': diagnostics})
                    if (stage == 'architecture' or repair_base is not None) and replay is None:
                        if candidate is not None and candidate.get('status') == 'supported':
                            repair_base = candidate
                        if repair_base is not None:
                            # Invalid edits leave the last complete candidate intact.
                            feedback = repair_feedback(stage, repair_base, exc)
                        else:
                            feedback = repair_feedback(stage, raw, exc)
                    else:
                        feedback = repair_feedback(stage, raw, exc)
                    if semantic_feedback:
                        feedback = semantic_feedback + '\n' + feedback
                    print(f'[SPECIFICATION] Needs repair: {exc}', flush=True)
            else:
                raise SpecificationError(f'{stage} specification failed validation: {repairs[-1]["error"]}')
    except (SpecificationError, ArchitectureError) as exc:
        report = output.with_name(output.name + '.failure.json')
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({'status': 'unsupported' if isinstance(exc, UnsupportedSpecification) else 'failed',
                         'request': request, 'model': model, 'context_fingerprint': context_hash,
                         'error': str(exc), 'validation_repairs': repairs,
                         'attempts_file': journal.name if journal.exists() else None,
                         'accepted_stages': list(accepted), 'rtl_generation': 'not_run'}, indent=2) + '\n')
        print(f'[SPECIFICATION] Failure report: {report}', flush=True)
        print(f'[SPECIFICATION] Stage responses: {journal}', flush=True)
        raise
    # Stage outputs and source files are immutable inputs to the review export.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.' + output.name + '-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'package'
        _write_package(staging, package, documents, context['timing_library'], catalog)
        (staging / 'responses.json').write_text(json.dumps(accepted, indent=2) + '\n')
        (staging / 'system_elaboration.json').write_text(json.dumps(system, indent=2) + '\n')
        (staging / 'attempts.json').write_text(journal.read_text())
        staging.rename(output)
    print(f'[SPECIFICATION] Package ready for export and review: {package_path}', flush=True)
    return package_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_yaml', type=Path)
    parser.add_argument('--requirements-file', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--response', type=Path)
    parser.add_argument('--cache', action='store_true')
    args = parser.parse_args()
    try:
        request = build_specification_request(read_document(args.input_yaml),
                    args.requirements_file.read_text() if args.requirements_file else '')
        propose_specification(request, output_dir=args.output_dir, model=args.model,
                              cache=args.cache, response_path=args.response)
        return 0
    except (SpecificationError, ArchitectureError, OSError, yaml.YAMLError) as exc:
        parser.exit(1, f'Specification generation failed: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
