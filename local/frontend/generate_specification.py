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
from specification import (SpecificationError, _package_header, configuration_axes, fields, load_package,
                           names, read_document, text_value, validate_metadata,
                           validate_matrix, validate_shared)
from specification_request import build_specification_request, validate_request_rules


GENERATOR_REVISION = 2
DOCUMENTS = {name: name + '.yaml' for name in
             ('shared', 'system', 'execution', 'modules', 'graph', 'verification')}
REFERENCE = ROOT / 'microarch/specification/ddr4_fifo/package.yaml'
GUIDE = ROOT / 'microarch/specification/generation_contract.md'


class UnsupportedSpecification(SpecificationError):
    pass


def generation_context():
    from specification_lowering import COMPILER_REVISION
    package, documents, library = load_package(REFERENCE)
    # This example teaches the data format; none of its behavioral choices are
    # implicitly imposed on requests. No feature-to-template registry is used.
    package.pop('review', None)
    package.pop('generation', None)
    return {'generator_revision': GENERATOR_REVISION, 'lowering_revision': COMPILER_REVISION,
            'contract': GUIDE.read_text(),
            'format_example': dict(package=package, **documents), 'timing_library': library}


def build_prompt(stage, request, context, system=None, feedback=''):
    # Examples must match the stage's accepted shape, rather than contradict
    # its instructions with compiler-owned fields and already-assigned owners.
    prompt_context = copy.deepcopy(context)
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
    else:
        example = {'modules': example['modules'], 'graph': example['graph'],
                   'verification': example['verification'],
                   'rule_owners': {f'{section}.rules.{rid}': rule['owners']
                                  for section in ('system', 'execution')
                                  for rid, rule in example[section]['rules'].items()}}
    prompt_context['format_example'] = dict(status='supported', **example)
    instruction = (
        'Elaborate SYSTEM BEHAVIOR FIRST. Do not choose modules, module names, owners or wiring. '
        'Return package metadata, shared definitions, system rules and execution rules with '
        'independent verification scenarios/expected outcomes. Each rule excludes owners. '
        'Preserve EVERY requested requirement as a rule with its original ID and EXACT text '
        'and origin request.ID; add detailed refinement rules as needed. Constraints are '
        'requirements too. Derive parameters and interfaces from THIS request. '
        'All rules need reciprocal verification obligations. Explicitly resolve simultaneous '
        'events, reset, timing edge conventions, storage ownership requirements, ordering, '
        'backpressure and responses before decomposition. '
        'Return {"status":"supported","package":{...},"shared":{...},"system":{...},"execution":{...}}.'
        if stage == 'system' else
        'Derive module contracts, typed graph and verification bindings from the LOCKED system '
        'specification. You cannot edit its package metadata, existing shared definitions, rules, '
        'scenarios or expected outcomes. Return rule_owners mapping EVERY fully qualified '
        'system.rules.ID and execution.rules.ID to a nonempty list of contract IDs. '
        'Assign reciprocal invariant/verification references to all owners. '
        'Generate architecture-specific verification intent bindings to declared ports, '
        'independently of any RTL. Declare accurate combinational dependencies. '
        'You may supply optional internal_types for additional internal-interface types; '
        'existing shared types cannot be replaced. Return {"status":"supported",'
        '"modules":{"contracts":{...}},"graph":{...},"verification":{...},"rule_owners":{...}}.')
    return ('Generate a specification package, never RTL. Return ONLY one JSON object.\n' +
            instruction + '\nHonor all requested constraints without weakening or omission. '
            'The example is FORMAT REFERENCE ONLY; its FIFO policy, eight modules, bank counts '
            'and names are not defaults or a capability menu. Templates are optional. '
            'Separate requested constraints, model-selected recorded decisions, documented '
            'assumptions and unresolved design questions. Never claim user approval or test PASS. '
            'A permitted implementation option is not an obligation to select it. Conditional '
            'reuse requirements apply only to blocks actually selected; reuse is optional. '
            'A baseline allowing synchronous or asynchronous reusable blocks does not require '
            'mixing reset styles. An explicit all-synchronous design can satisfy that allowance. '
            'For unsupported requests return {"status":"unsupported","unsupported":'
            '[{"requirement_ids":["ID"],"reason":"...","required_extension":"..."}]}; '
            'do not return a partial implementation. Semantic compliance remains subject to review.\n'
            'REQUEST:\n' + json.dumps(request, indent=2) + '\nFORMAT AND CAPABILITIES:\n' +
            json.dumps(prompt_context, indent=2) + '\nLOCKED SYSTEM (null in first stage):\n' +
            json.dumps(system, indent=2) + '\nREPAIR FEEDBACK:\n' + feedback)


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


def validate_system(response, request, library):
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
    shared = system['shared']
    parameters, widths, _, _ = validate_shared(shared, library)
    reset = shared['clock_reset']  # validate_shared checks the common convention.
    for key in ('clock', 'reset'):
        identifier(reset[key], key)
    from specification import expression
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
    axes, profiles = configuration_axes(package['verification_strategy']['configurations'])
    for values in itertools.product(*axes.values(), profiles):
        settings = dict(zip(axes, values[:-1]))
        variant_parameters, variant_widths, _, _ = validate_shared(shared, library, settings, values[-1])
        for name, port in shared['host_interface'].items():
            if 'when' in port and not expression(port['when'], variant_parameters):
                continue
            width = variant_widths[port['type'].removeprefix('shared.types.')]
            if width < 1 or (name in (reset['clock'], reset['reset']) and width != 1):
                raise SpecificationError(f'{name}: invalid host width in configuration matrix')
    for section in ('system', 'execution'):
        doc = fields(system[section], ('slice_id', 'rules', 'verification'), (), section)
        for rid, rule in fields(doc['rules'], (), None, section + '.rules').items():
            identifier(rid, 'rule ID')
            fields(rule, ('origin', 'statement', 'verification'), (), rid)
            text_value(rule['statement'], rid)
            text_value(rule['origin'], rid + '.origin')
            if not names(rule['verification'], rid):
                raise SpecificationError(f'{rid}: verification obligation required')
            for vid in rule['verification']:
                if vid not in doc['verification'] or rid not in doc['verification'][vid].get('rules', []):
                    raise SpecificationError(f'{rid}: nonreciprocal verification reference {vid}')
        for vid, obligation in fields(doc['verification'], (), None, section + '.verification').items():
            fields(obligation, ('rules', 'scenario', 'expected', 'status'), (), vid)
            for key in ('scenario', 'expected'):
                text_value(obligation[key], vid + '.' + key)
            if obligation['status'] != 'planned_not_executed':
                raise SpecificationError('Generated obligations must remain planned_not_executed')
            if not names(obligation['rules'], vid):
                raise SpecificationError(f'{vid}: rule references required')
            for rid in obligation['rules']:
                if rid not in doc['rules'] or vid not in doc['rules'][rid]['verification']:
                    raise SpecificationError(f'{vid}: nonreciprocal rule reference {rid}')
    validate_request_rules(request, system)
    if request.get('baseline_contract'):
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
    package['generation'] = {
        'version': 1, 'model': model, 'request': copy.deepcopy(request),
        'input_fingerprint': fingerprint(request), 'context_fingerprint': context_hash,
        'system_fingerprint': fingerprint(system), 'internal_types': copy.deepcopy(internal_types),
        'validation_repairs': copy.deepcopy(repairs),
    }
    return package, documents


def _write_package(directory, package, documents, library):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'package.yaml').write_text(yaml.safe_dump(package, sort_keys=False))
    for name, document in documents.items():
        (directory / DOCUMENTS[name]).write_text(yaml.safe_dump(document, sort_keys=False))
    (directory / 'timing.yaml').write_text(yaml.safe_dump(library, sort_keys=False))


def propose_specification(request, *, output_dir=None, model=DEFAULT_MODEL, cache=False,
                          response_path=None, caller=None):
    """Return a validated package path. No approval or RTL is ever produced."""
    from specification_lowering import compile_package
    context = generation_context()
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

    system = None
    record_attempts()
    try:
        for stage in ('system', 'architecture'):
            feedback = ''
            for attempt in range(1 if replay is not None else 3):
                print(f'[SPECIFICATION] {stage}: {"replay" if replay is not None else model} (attempt {attempt + 1})', flush=True)
                raw = replay[stage] if replay is not None else caller(build_prompt(stage, request, context, system, feedback), model)
                entry = {'stage': stage, 'attempt': attempt + 1, 'response': copy.deepcopy(raw), 'status': 'received'}
                attempts.append(entry)
                record_attempts()
                try:
                    response = parse_response(raw) if isinstance(raw, str) else copy.deepcopy(raw)
                    if not isinstance(response, dict):
                        raise SpecificationError('Specification response must be an object')
                    if stage == 'system':
                        system = validate_system(response, request, context['timing_library'])
                        # Files always refer to the portable, compiler-owned library.
                        system['shared']['timing_profile']['source'] = 'timing.yaml'
                    else:
                        package, documents = assemble_package(system, response, request, model, context_hash, repairs)
                        # Run the real compiler, including module IR, graph and role
                        # validation, before publishing any usable package.
                        with tempfile.TemporaryDirectory(prefix='capstone-spec-') as temporary:
                            _write_package(Path(temporary), package, documents, context['timing_library'])
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
                    entry.update(status='rejected', error=str(exc))
                    record_attempts()
                    repairs.append({'stage': stage, 'attempt': attempt + 1, 'error': str(exc)})
                    boundary = ('Revise the system response to match the accepted first-stage schema.' if stage == 'system'
                                else 'Do not edit the locked system; repair only decomposition, bindings or internal type additions.')
                    feedback = f'Previous response:\n{json.dumps(raw)}\nValidation error:\n{exc}\n{boundary}'
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
        _write_package(staging, package, documents, context['timing_library'])
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
