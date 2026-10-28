"""Versioned, explicitly reviewed module execution plans and offline acceptance.

This protocol is separate from the deployed v1 all-source simulation collector.
It never grants system or backend readiness and never infers scope from prose.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile

import yaml

from .module_probe import expected_source_catalog
from .specification_mailbox import (read_json, relative_file, verify_specification,
                                    prepare_module, verify_module_probe)

PLAN_SCHEMA = 'capstone.module_execution_plan'
RESULT_SCHEMA = 'capstone.module_execution_results'
METHODS = {'metadata': 'metadata', 'structural': 'structural', 'module': 'simulation'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def source_inputs(mailbox, module):
    mailbox = Path(mailbox).resolve()
    metadata = verify_specification(mailbox)
    rows = read_json(mailbox / 'verification_inventory.json')['modules']
    selected = [row for row in rows if row['kind'] == 'functional' and row['module_name'] == module]
    if len(selected) != 1:
        raise ValueError('Execution plan requires exactly one functional module')
    spec_path = relative_file(mailbox, selected[0]['specification'])
    spec = yaml.safe_load(spec_path.read_text())
    sources = expected_source_catalog(spec)
    native = read_json(mailbox / 'handoff/verification_handoff.json')['verification_intent']
    composed = {key for key, row in sources.items()
                if len(row.get('source_requirement', {}).get('owners', [])) > 1}
    composed.update(key for key, row in native.get('obligations', {}).items()
                    if key in sources and len(row.get('owners', [])) > 1)
    # Retain legacy normative records as well as rich source objects. Their
    # duplication is explicit; a consumer must not silently invent extra work.
    for field in ('invariants', 'implementation_constraints', 'forbidden_patterns'):
        for index, value in enumerate(spec.get(field, [])):
            key = 'yaml.' + field + '.' + str(index)
            if key in sources:
                raise ValueError('Reserved auxiliary source ID collision')
            sources[key] = {'source_yaml_field': {'path': '/' + field + '/' + str(index), 'value': value}}
    identity = {'snapshot_sha256': hashlib.sha256((mailbox / 'snapshot.json').read_bytes()).hexdigest(),
                'rtl_revision': metadata['rtl_revision'], 'module_name': module,
                'specification_sha256': hashlib.sha256(spec_path.read_bytes()).hexdigest()}
    facets = ['/behavior/contract/' + key for key in spec['behavior']['contract']]
    return identity, sources, sorted(facets), sorted(composed)


def draft_routing(mailbox, module):
    identity, sources, facets, composed = source_inputs(mailbox, module)
    return {'schema': 'capstone.module_scope_routing', 'schema_version': 1,
            'identity': identity, 'review': {'status': 'draft', 'reviewer': '', 'rationale': ''},
            'required_contract_facets': facets,
            'composition_sources': composed,
            'sources': {key: {'scope': 'unresolved', 'checks': [], 'reason': ''} for key in sources},
            'checks': []}


def build_plan(mailbox, routing):
    identity, sources, facets, composed = source_inputs(mailbox, routing['identity']['module_name'])
    if (routing.get('schema') != 'capstone.module_scope_routing' or routing.get('schema_version') != 1
            or routing.get('identity') != identity or routing.get('required_contract_facets') != facets
            or routing.get('composition_sources') != composed):
        raise ValueError('Routing identity/facets differ from the authenticated module')
    review = routing.get('review', {})
    if (review.get('status') != 'approved' or not isinstance(review.get('reviewer'), str)
            or not review['reviewer'].strip() or not isinstance(review.get('rationale'), str)
            or not review['rationale'].strip()):
        raise ValueError('Routing requires explicit scope review; drafts cannot establish acceptance')
    assignments = routing.get('sources', {})
    if set(assignments) != set(sources):
        raise ValueError('Routing drops or invents source records')
    checks = {}
    for row in routing.get('checks', []):
        key, scope = row.get('id'), row.get('scope')
        if not isinstance(key, str) or not key.strip() or key in checks:
            raise ValueError('Missing/duplicate execution check ID')
        if scope not in METHODS or row.get('method') != METHODS[scope]:
            raise ValueError('Check scope/method mismatch')
        if (not isinstance(row.get('description'), str) or not row['description'].strip()
                or not isinstance(row.get('assumptions'), list)
                or any(not isinstance(a, str) or not a.strip() for a in row['assumptions'])):
            raise ValueError('Checks need descriptions and explicit environment assumptions')
        refs = row.get('source_ids', [])
        if not refs or len(set(refs)) != len(refs) or set(refs) - set(sources):
            raise ValueError('Check has missing/duplicate/unknown source references')
        covered = row.get('contract_facets', [])
        if len(set(covered)) != len(covered) or set(covered) - set(facets):
            raise ValueError('Check has unknown/duplicate contract facets')
        checks[key] = deepcopy(row)
    if not checks or not any(row['scope'] == 'module' for row in checks.values()):
        raise ValueError('Module plan requires actual local simulation checks')
    if {facet for row in checks.values() for facet in row.get('contract_facets', [])} != set(facets):
        raise ValueError('Local contract facets lack explicit check coverage')
    for key, assignment in assignments.items():
        scope, refs = assignment.get('scope'), assignment.get('checks')
        if scope not in (*METHODS, 'system') or not isinstance(refs, list):
            raise ValueError('Unresolved/unsupported source scope: ' + key)
        if key in composed and scope == 'module':
            raise ValueError('Multi-owner source cannot be certified by isolated module simulation: ' + key)
        if not isinstance(assignment.get('reason'), str) or not assignment['reason'].strip():
            raise ValueError('Source scope needs a reviewable reason: ' + key)
        if len(set(refs)) != len(refs) or set(refs) - set(checks):
            raise ValueError('Source assignment references unknown/duplicate checks')
        if scope == 'system':
            if refs:
                raise ValueError('Local checks cannot certify a full system source')
        elif not refs:
            raise ValueError('Locally assigned source has no required checks')
        for ref in refs:
            if key not in checks[ref]['source_ids']:
                raise ValueError('Source/check references disagree')
            if scope in ('metadata', 'structural') and checks[ref]['scope'] != scope:
                raise ValueError('Metadata/structural sources require matching evidence methods')
        if key == 'behavior.contract' and (scope != 'module' or
                {f for ref in refs for f in checks[ref].get('contract_facets', [])} != set(facets)):
            raise ValueError('Whole local contract requires every declared facet; a narrow check is insufficient')
        if key == 'behavior.contract':
            structural_facets = {'status', 'role', 'interfaces', 'owned_state', 'invariants', 'verification'}
            behavioral = {f for f in facets if f.rsplit('/', 1)[1] not in structural_facets}
            simulated = {f for ref in refs if checks[ref]['scope'] == 'module'
                         for f in checks[ref].get('contract_facets', [])}
            if behavioral - simulated:
                raise ValueError('Behavioral contract facets require simulation, not metadata/structural checks')
    plan = {'schema': PLAN_SCHEMA, 'schema_version': 1, 'identity': identity,
            'review': deepcopy(review), 'sources': sources, 'assignments': deepcopy(assignments),
            'checks': list(checks.values()), 'required_contract_facets': facets,
            'composition_sources': composed,
            'deferred_system_sources': sorted(key for key, row in assignments.items() if row['scope'] == 'system'),
            'consumer_capability': 'capstone.module_execution_plan.v1',
            'unrouted_legacy_checks': 'reject_before_agent_execution',
            'system_verification': 'not_accepted', 'backend_ready': False}
    plan['plan_sha256'] = fingerprint(plan)
    return plan


def validate_plan(mailbox, plan):
    routing = {'schema': 'capstone.module_scope_routing', 'schema_version': 1,
               'identity': plan['identity'], 'review': plan['review'], 'sources': plan['assignments'],
               'composition_sources': plan['composition_sources'],
               'checks': plan['checks'], 'required_contract_facets': plan['required_contract_facets']}
    if build_plan(mailbox, routing) != plan:
        raise ValueError('Execution plan differs from authenticated inputs/reviewed routing')
    return plan


def export_bundle(mailbox, plan, destination):
    """Portable request for the new consumer; preserve the native isolated pair."""
    validate_plan(mailbox, plan)
    mailbox, destination = Path(mailbox).resolve(), Path(destination).absolute()
    if destination.exists():
        raise FileExistsError(destination)
    if destination.resolve().is_relative_to(mailbox):
        raise ValueError('Scope bundle must be outside the immutable source')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.module-scope-', dir=destination.parent) as temporary:
        bundle = Path(temporary) / 'bundle'
        bundle.mkdir()
        pair = prepare_module(mailbox, plan['identity']['module_name'], bundle / 'input')
        (bundle / 'execution_plan.json').write_text(json.dumps(plan, indent=2) + '\n')
        request = execution_request(plan, bundle / 'execution_plan.json', pair)
        (bundle / 'scope_request.json').write_text(json.dumps(request, indent=2) + '\n')
        validate_plan(mailbox, plan)
        verify_module_probe(pair, mailbox)
        if destination.exists():
            raise FileExistsError(destination)
        bundle.rename(destination)
    return request


def execution_request(plan, plan_path, inputs):
    return {'schema': 'capstone.module_execution_request', 'schema_version': 1,
            'identity': plan['identity'], 'consumer_capability': plan['consumer_capability'],
            'plan_sha256': plan['plan_sha256'],
            'input_snapshot_sha256': hashlib.sha256((Path(inputs) / 'snapshot.json').read_bytes()).hexdigest(),
            'plan_file_sha256': hashlib.sha256(Path(plan_path).read_bytes()).hexdigest(),
            'input_directory': 'input', 'plan_file': 'execution_plan.json',
            'backend_ready': False, 'system_verification': 'not_accepted'}


def accept_results(mailbox, plan, result, evidence_root):
    """Offline result-body validation, not remote invocation/system acceptance."""
    validate_plan(mailbox, plan)
    if (result.get('schema') != RESULT_SCHEMA or result.get('schema_version') != 1
            or result.get('identity') != plan['identity'] or result.get('plan_sha256') != plan['plan_sha256']
            or result.get('consumer_capability') != plan['consumer_capability']
            or result.get('scope') != 'module_checks_only' or result.get('backend_ready') is not False):
        raise ValueError('Result identity, capability or scope differs from the execution plan')
    rows = result.get('checks', [])
    actual = {row['id']: row for row in rows}
    expected = {row['id']: row for row in plan['checks']}
    if len(actual) != len(rows) or set(actual) != set(expected):
        raise ValueError('Result drops, duplicates or invents execution checks')
    tests = result.get('executed_tests', [])
    test_records = {row['id']: row for row in tests}
    if len(test_records) != len(tests):
        raise ValueError('Duplicate executed test ID')
    for row in test_records.values():
        if (row.get('method') != 'simulation' or row.get('status') != 'passed'
                or row.get('execution_completed') is not True or row.get('execution_succeeded') is not True):
            raise ValueError('Test manifest lacks completed successful simulation')
    for key, row in actual.items():
        if row.get('status') != 'passed' or row.get('method') != expected[key]['method']:
            raise ValueError('Assigned check lacks passed evidence with the correct method: ' + key)
        tests = row.get('test_ids')
        if (not isinstance(tests, list) or any(not isinstance(t, str) or not t.strip() for t in tests)
                or (expected[key]['method'] == 'simulation' and not tests)):
            raise ValueError('Simulation check needs executed test IDs')
        if len(set(tests)) != len(tests) or any(t not in test_records for t in tests):
            raise ValueError('Check test IDs do not resolve to unique executed simulation records')
        evidence = row.get('evidence', [])
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('Check has no evidence artifacts')
        for item in evidence:
            value = item.get('path', '')
            path = PurePosixPath(value)
            if not value or '\\' in value or path.is_absolute() or '..' in path.parts:
                raise ValueError('Evidence path must be a relative contained file')
            root = Path(evidence_root).resolve()
            target = root.joinpath(*path.parts)
            if any(p.is_symlink() for p in (target, *target.parents) if p.is_relative_to(root)):
                raise ValueError('Symlinked evidence artifact')
            if (not target.resolve().is_relative_to(root) or not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != item.get('sha256')):
                raise ValueError('Missing/changed evidence artifact')
        for test in tests:
            if (not isinstance(test_records[test].get('evidence'), list)
                    or not test_records[test]['evidence']
                    or any(item not in evidence for item in test_records[test]['evidence'])):
                raise ValueError('Executed test evidence is not bound to checked artifacts')
    records = result.get('source_results', {})
    if set(records) != set(plan['sources']):
        raise ValueError('Result drops or invents source dispositions')
    for key, assigned in plan['assignments'].items():
        expected_status = 'unverified' if assigned['scope'] == 'system' else 'passed'
        if records[key] != {'status': expected_status, 'check_ids': assigned['checks']}:
            raise ValueError('Source disposition changes scope or claims deferred coverage: ' + key)
    return {'schema': 'capstone.module_scope_acceptance', 'schema_version': 1,
            'identity': plan['identity'], 'plan_sha256': plan['plan_sha256'],
            'status': 'module_checks_passed', 'checks_passed': sorted(actual),
            'deferred_system_sources': plan['deferred_system_sources'],
            'system_verification': 'not_accepted', 'backend_ready': False,
            'remote_invocation_authentication': 'not_evaluated_by_offline_result_validator'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mailbox', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    draft = commands.add_parser('draft')
    draft.add_argument('--module', required=True)
    build = commands.add_parser('build')
    build.add_argument('--routing', type=Path, required=True)
    validate = commands.add_parser('validate')
    validate.add_argument('--plan-file', type=Path, required=True)
    bundle = commands.add_parser('bundle')
    bundle.add_argument('--plan-file', type=Path, required=True)
    bundle.add_argument('--output', type=Path, required=True)
    accept = commands.add_parser('accept')
    accept.add_argument('--plan-file', type=Path, required=True)
    accept.add_argument('--results', type=Path, required=True)
    accept.add_argument('--evidence-root', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'draft':
        value = draft_routing(args.mailbox, args.module)
    elif args.command == 'build':
        value = build_plan(args.mailbox, read_json(args.routing))
    elif args.command == 'validate':
        value = validate_plan(args.mailbox, read_json(args.plan_file))
    elif args.command == 'bundle':
        value = export_bundle(args.mailbox, read_json(args.plan_file), args.output)
    else:
        value = accept_results(args.mailbox, read_json(args.plan_file), read_json(args.results), args.evidence_root)
    print(json.dumps(value, indent=2))


if __name__ == '__main__':
    main()
