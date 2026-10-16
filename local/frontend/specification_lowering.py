"""Lower reviewed system contracts to the existing module-spec/architecture IR route.

This compiler translates the supplied contracts and graph; it invents no module
behavior. Exporting a review never calls an LLM or emits RTL.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path

import yaml

from specification import (SpecificationError, elaborate_type_layouts, expression,
                           fields, load_package, names, read_document, text_value, validate_contents)


COMPILER_REVISION = 3
SOURCE = 'specification_package'
ROLE_NAMES = {
    'request_storage': 'request_queue', 'request_selection': 'scheduler',
    'command_sequencing': 'command_engine', 'timing_enforcement': 'timing',
    'functional_memory_and_responses': 'memory', 'host_protocol': 'host_adapter',
    'bank_state': 'row_state', 'refresh_control': 'refresh',
}


def _fingerprint(value):
    from architecture import fingerprint
    return fingerprint(value)


def _present(port, parameters):
    return 'when' not in port or expression(port['when'], parameters)


def _compile_snapshot(snapshot):
    """Pure lowering, also used to verify portable saved manifests before use."""
    from architecture import CONTROLLER_ROLES, identifier

    fields(snapshot, ('package', 'documents', 'timing_library', 'overrides', 'profile'), (), 'specification snapshot')
    package, documents = snapshot['package'], snapshot['documents']
    evidence = validate_contents(package, documents, snapshot['timing_library'], snapshot['overrides'], snapshot['profile'])
    shared, graph = documents['shared'], documents['graph']
    parameters, widths = evidence['parameters'], evidence['type_widths']
    reset = shared['clock_reset']
    package_hash = _fingerprint({'compiler_revision': COMPILER_REVISION, 'snapshot': snapshot})
    context = {
        'package_id': package['package_id'], 'revision': package['revision'],
        'package_fingerprint': package_hash,
        'parameters': parameters, 'types': elaborate_type_layouts(shared['types'], parameters, widths),
        'clock_reset': reset, 'events': shared['events'],
        'timing_profile': {'selection': evidence['profile'], 'unit': 'cycles', 'values': evidence['timing_cycles']},
        'timing_rules': copy.deepcopy(shared['timing_rules']),
        'system_rules': documents['system']['rules'], 'execution_rules': documents['execution']['rules'],
        'decisions': package['decisions'], 'assumptions': package['assumptions'],
    }
    if 'generation' in package:
        context['request'] = copy.deepcopy(package['generation']['request'])
    for rule in context['timing_rules'].values():
        if isinstance(rule, dict):
            for key in ('minimum', 'window', 'limit'):
                if key in rule:
                    ref = rule[key]
                    values = parameters if key == 'limit' else evidence['timing_cycles']
                    rule[key] = values[ref.rsplit('.', 1)[1]]
    modules, requirements, roles = [], [], set()

    def ports(table):
        return [{'name': identifier(name, 'port'), 'direction': port['direction'],
                 'width': widths[port['type'].removeprefix('shared.types.')],
                 'description': port['type']}
                for name, port in table.items() if _present(port, parameters)]

    for slice_name in ('system', 'execution'):
        for rid, rule in documents[slice_name]['rules'].items():
            obligations = {vid: documents[slice_name]['verification'][vid] for vid in rule['verification']}
            requirements.append({
                'id': identifier(rid, 'requirement'), 'text': rule['statement'], 'owners': rule['owners'],
                'verification': yaml.safe_dump({'source': f'{slice_name}.rules.{rid}', 'origin': rule['origin'],
                                                'obligations': obligations}, sort_keys=False),
            })
    for name, contract in documents['modules']['contracts'].items():
        identifier(name, 'contract')
        role = ROLE_NAMES.get(contract['role'], contract['role'])
        if role not in set(ROLE_NAMES.values()):
            raise SpecificationError(f'{name}: unsupported role {role!r}; extend the role vocabulary explicitly')
        roles.add(role)
        interfaces = ports(contract['interfaces'])
        active_ports = {p['name'] for p in interfaces}
        dependencies = {out: [p for p in inputs if p in active_ports]
                        for out, inputs in contract['combinational_dependencies'].items() if out in active_ports}
        rendered_contract = copy.deepcopy(contract)
        rendered_contract['interfaces'] = {p['name']: {'direction': p['direction'],
            'type': contract['interfaces'][p['name']]['type'], 'width': p['width']} for p in interfaces}
        rendered_contract['combinational_dependencies'] = dependencies
        obligations = {}
        invariant_text = []
        for ref in contract['invariants']:
            section, _, rid = ref.split('.')
            invariant_text.append(f'{ref}: {documents[section]["rules"][rid]["statement"]}')
        for ref in contract['verification']:
            section, _, vid = ref.split('.')
            obligations[ref] = documents[section]['verification'][vid]
        spec = {
            'design_name': identifier(f'{package["package_id"]}_{name}', 'module name'),
            # Prose algorithms are not a state-transition table. Let the existing
            # datapath route implement state/algorithms inside the fixed contract.
            'design_type': 'datapath',
            'clock': {'name': reset['clock']},
            'reset': {'name': reset['reset'], 'active_low': True, 'synchronous': True},
            'ports': interfaces,
            'behavior': {
                'description': f'Implement the complete {name} contract below. All structured contract and system context fields are normative. Internal representations and algorithms may vary within this boundary.',
                'contract': rendered_contract, 'specification_context': context,
                'active_ports': sorted(active_ports), 'verification_obligations': obligations,
            },
            'invariants': invariant_text,
            'implementation_constraints': [
                'Use the exact elaborated port widths and packed field offsets; no additional public ports.',
                'Honor every contract handshake, reset, update event, stall and simultaneous-event rule.',
                'Do not transfer owned state or responsibilities into another module or top-level wiring.',
                'Implement the declared combinational dependencies accurately; internal registers and helper logic remain implementation choices.',
                'Parameter values and timing values in specification_context are concrete for this build; do not substitute legacy defaults.',
                'A conditionally omitted input has no signal in this build; apply its specified absent-port behavior.',
            ],
            'combinational_dependencies': dependencies,
        }
        modules.append({'instance': name, 'roles': [role], 'responsibility': contract['role'], 'spec': spec})
    top_ports = ports(shared['host_interface'])
    active_endpoints = {f'top.{p["name"]}' for p in top_ports}
    for module in modules:
        active_endpoints.update(f'{module["instance"]}.{p["name"]}' for p in module['spec']['ports'])
    connections = [{'from': source, 'to': target} for source, targets in graph['connections'].items()
                   for target in targets if source in active_endpoints and target in active_endpoints]
    for name in graph['clock_reset']['consumers']:
        connections.extend({'from': f'top.{signal}', 'to': f'{name}.{signal}'} for signal in (reset['clock'], reset['reset']))
    manifest = {
        'architecture_version': 1, 'design_name': package['design_name'], 'profile': evidence['profile'],
        'rationale': f'Deterministic lowering of {package["package_id"]} revision {package["revision"]}; module boundaries, interfaces and behavior come from the supplied contracts.',
        'top_ports': top_ports, 'modules': modules, 'connections': connections, 'requirements': requirements,
        'assumptions': [f'{name}: {a["statement"]}' for name, a in package['assumptions'].items()],
        'limitations': ['Specification/IR validation does not execute behavioral obligations.',
                        'This stage lowers supplied contracts; request-to-system-spec synthesis remains separate.'],
        'provenance': {'source': SOURCE, 'specification': {'compiler_revision': COMPILER_REVISION,
                         'package_fingerprint': package_hash, 'snapshot': snapshot}},
    }
    if package.get('design_kind') == 'memory_controller' or CONTROLLER_ROLES <= roles:
        manifest['design_kind'] = 'memory_controller'
    baseline = package.get('generation', {}).get('request', {}).get('baseline_contract')
    if baseline:
        manifest['baseline_contract'] = copy.deepcopy(baseline)
    return manifest, evidence


def compile_package(path, overrides=None, profile=None):
    from architecture import validate_architecture
    package, documents, library = load_package(path)
    snapshot = {'package': package, 'documents': documents, 'timing_library': library,
                'overrides': copy.deepcopy(overrides or {}),
                'profile': profile if profile is not None else documents['shared']['timing_profile']['selection']}
    manifest, _ = _compile_snapshot(snapshot)
    validate_architecture(manifest)
    return manifest


def is_specification_manifest(manifest):
    provenance = manifest.get('provenance', {})
    return (isinstance(provenance, dict) and (provenance.get('source') == SOURCE or 'specification' in provenance)
            or any(isinstance(m.get('spec'), dict) and isinstance(m['spec'].get('behavior'), dict)
                   and 'specification_context' in m['spec']['behavior'] for m in manifest.get('modules', []) if isinstance(m, dict)))


def validate_specification_manifest(manifest):
    if not is_specification_manifest(manifest):
        return
    proof = manifest.get('provenance', {}).get('specification')
    if not isinstance(proof, dict) or 'snapshot' not in proof:
        raise SpecificationError('Specification-derived manifest lost its source snapshot; regenerate it')
    expected, _ = _compile_snapshot(proof['snapshot'])
    if manifest != expected:
        raise SpecificationError('Specification-derived manifest or compiler changed; regenerate and review the package again')


def _reviewable(manifest, review_confirmations=()):
    validate_specification_manifest(manifest)
    if not is_specification_manifest(manifest):
        raise SpecificationError('Expected a specification-derived architecture manifest')
    package = manifest['provenance']['specification']['snapshot']['package']
    if not isinstance(review_confirmations, (list, tuple)):
        raise SpecificationError('Review confirmations must be a list of question IDs')
    confirmations = names(list(review_confirmations), 'review confirmations')
    for name in confirmations:
        question = package['open_questions'].get(name)
        if question is None or question['blocks'] != 'design_review':
            raise SpecificationError(f'{name}: only an existing design_review question can receive explicit review confirmation')
    pending = [name for name, q in package['open_questions'].items()
               if q['blocks'] != 'rtl_generation' and name not in confirmations]
    pending += [name for name, d in package['decisions'].items() if d['status'] not in ('accepted', 'recorded')]
    pending += [name for name, a in package['assumptions'].items() if a['status'] == 'requires_review']
    if pending:
        raise SpecificationError(f'Resolve blocking decisions/questions before approving RTL: {pending}')
    return package


def create_approval(manifest, reviewer, *, review_confirmations=()):
    """Called only by the explicit record-approval action after user review."""
    from architecture import validate_architecture
    validate_architecture(manifest)
    package = _reviewable(manifest, review_confirmations)
    text_value(reviewer, 'reviewer')
    approval = {'approval_version': 2 if review_confirmations else 1, 'status': 'approved', 'reviewer': reviewer,
            'approved_at': datetime.now(timezone.utc).isoformat(),
            'package_id': package['package_id'], 'revision': package['revision'],
            'package_fingerprint': manifest['provenance']['specification']['package_fingerprint'],
            'manifest_fingerprint': _fingerprint(manifest),
            'resolved_questions': list(package['open_questions'])}
    if review_confirmations:
        approval['review_confirmations'] = sorted(review_confirmations)
    return approval


def require_approval(manifest, approval_path=None):
    if not is_specification_manifest(manifest):
        if approval_path is not None:
            raise SpecificationError('Specification approval supplied for a non-specification manifest')
        return
    if approval_path is None:
        _reviewable(manifest)
        raise SpecificationError('RTL generation requires revision-bound specification approval. Export with --microarch-only, review the artifacts, then supply --spec-approval.')
    approval = read_document(approval_path)
    version = approval.get('approval_version')
    if type(version) is not int or version not in (1, 2):
        raise SpecificationError('Unsupported specification approval_version')
    fields(approval, ('approval_version', 'status', 'reviewer', 'approved_at', 'package_id', 'revision',
                      'package_fingerprint', 'manifest_fingerprint', 'resolved_questions') +
                      (('review_confirmations',) if version == 2 else ()), (), 'approval')
    confirmations = approval.get('review_confirmations', [])
    package = _reviewable(manifest, confirmations)
    expected = {'approval_version': 2 if confirmations else 1, 'status': 'approved', 'package_id': package['package_id'],
                'revision': package['revision'], 'package_fingerprint': manifest['provenance']['specification']['package_fingerprint'],
                'manifest_fingerprint': _fingerprint(manifest), 'resolved_questions': list(package['open_questions'])}
    if type(approval['approval_version']) is not int or any(approval.get(k) != v for k, v in expected.items()):
        raise SpecificationError('Specification approval is stale or belongs to another package/configuration; review again')
    text_value(approval['reviewer'], 'approval.reviewer')
    text_value(approval['approved_at'], 'approval.approved_at')
    return approval


def emit_review(manifest, output):
    from architecture import emit_verification_specs, validate_architecture
    validate_architecture(manifest)
    output = Path(output)
    if output.suffix not in ('.yaml', '.yml'):
        raise SpecificationError('Architecture output must use .yaml or .yml')
    proof = manifest['provenance']['specification']
    package = proof['snapshot']['package']
    _, evidence = _compile_snapshot(proof['snapshot'])
    evidence.update(architecture_validation='passed', module_ir_validation='passed', rtl_generation='not_run')
    # Validation precedes replacing any previously usable export.
    specs = emit_verification_specs(manifest, output)
    temporary = output.with_suffix(output.suffix + '.tmp')
    temporary.write_text(yaml.safe_dump(manifest, sort_keys=False))
    temporary.replace(output)
    output.with_suffix('.specification.json').write_text(json.dumps(proof, indent=2) + '\n')
    output.with_suffix('.validation.json').write_text(json.dumps(evidence, indent=2) + '\n')
    output.with_suffix('.verification.json').write_text(json.dumps({
        'behavioral_verification': 'not_run', 'requirements': [dict(r, result='unverified') for r in manifest['requirements']],
        'obligations': {ref: dict(obligation, result='unverified') for module in manifest['modules']
                        for ref, obligation in module['spec']['behavior']['verification_obligations'].items()},
    }, indent=2) + '\n')
    from verification_handoff import emit_handoff
    handoff = emit_handoff(manifest, output, specs)
    report = [f'# {package["package_id"]}: {package["revision"]}', '',
              'Specification, architecture and module-IR checks passed. RTL generation and behavioral verification have not run.', '',
              f'[Downstream verification handoff]({handoff.name}): current system consumer requires an adapter; UVM/vector execution remains external.', '',
              f'Package fingerprint: `{proof["package_fingerprint"]}`',
              f'Manifest fingerprint: `{_fingerprint(manifest)}`', '', '## Modules', '']
    report.extend(f'- [{path.name}]({path.relative_to(output.parent).as_posix()})' for path in specs)
    report.extend(['', '## Review scope', '',
                   'Review the embedded specification snapshot, module contracts, numeric layouts/timing, graph, decisions, assumptions and all verification obligations together.',
                   'An approval covers this exact snapshot, compiler result and configuration. Changes require a new export and approval.', '',
                   '## Decisions', ''])
    report.extend(f'- {name} ({d["status"]}): {d["value"]}. {d["reason"]}' for name, d in package['decisions'].items())
    report.extend(['', '## Assumptions', ''])
    report.extend(f'- {name}: {a["statement"]}' for name, a in package['assumptions'].items())
    report.extend(['', '## Questions', ''])
    report.extend(f'- {name}: {q["question"]}' for name, q in package['open_questions'].items())
    output.with_suffix('.md').write_text('\n'.join(report) + '\n')
    return specs


def parse_overrides(items):
    values = {}
    for item in items:
        if '=' not in item:
            raise SpecificationError('--spec-set requires NAME=VALUE')
        name, value = item.split('=', 1)
        if not name or name in values:
            raise SpecificationError('Parameter overrides must have unique nonempty names')
        try:
            values[name] = yaml.safe_load(value)
        except yaml.YAMLError as exc:
            raise SpecificationError(f'Invalid parameter override {name}') from exc
    return values


def main():
    from architecture import ArchitectureError, ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='System package, or an exported architecture for recording approval')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--spec-set', action='append', default=[], metavar='NAME=VALUE')
    parser.add_argument('--spec-profile')
    parser.add_argument('--record-approval', type=Path, help='Explicitly approve an already exported architecture; never generates RTL')
    parser.add_argument('--reviewer')
    parser.add_argument('--confirm-review-question', action='append', default=[], metavar='ID',
                        help='With --record-approval, explicitly confirm a design_review question by accepting the unchanged exported contracts. Contract changes require a new export.')
    args = parser.parse_args()
    try:
        if args.record_approval:
            if args.output or args.spec_set or args.spec_profile or not args.reviewer:
                raise SpecificationError('Approval requires --reviewer and an exported manifest, without export/configuration options')
            if args.record_approval.resolve() == args.source.resolve():
                raise SpecificationError('Approval output must not overwrite the reviewed manifest')
            manifest = read_document(args.source)
            approval = create_approval(manifest, args.reviewer, review_confirmations=args.confirm_review_question)
            args.record_approval.parent.mkdir(parents=True, exist_ok=True)
            args.record_approval.write_text(json.dumps(approval, indent=2) + '\n')
            print(f'Recorded approval for {approval["package_id"]} {approval["revision"]}: {args.record_approval}')
        else:
            if args.reviewer or args.confirm_review_question:
                raise SpecificationError('--reviewer and --confirm-review-question are only used with --record-approval')
            manifest = compile_package(args.source, parse_overrides(args.spec_set), args.spec_profile)
            digest = manifest['provenance']['specification']['package_fingerprint']
            output = args.output or ROOT / 'microarch/proposals/specification' / digest[:16] / 'architecture.yaml'
            if output.resolve() == args.source.resolve():
                raise SpecificationError('Architecture export must not overwrite the source package')
            specs = emit_review(manifest, output)
            print(f'Exported {len(specs)} module specifications: {output}')
            print('RTL generation and behavioral verification have not run.')
        return 0
    except (SpecificationError, ArchitectureError, OSError, yaml.YAMLError) as exc:
        parser.exit(1, f'Specification lowering failed: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
