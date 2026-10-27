#!/usr/bin/env python3
"""One semantic repair cycle: route, compile, export, then review again.

Uses the existing bounded compiler-repair loops. Never edits the parent revision,
changes request requirements, or resolves external agreement automatically.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from architecture import ArchitectureError, ROOT, fingerprint, validate_architecture
from generate_microarch import DEFAULT_MODEL
from generate_specification import propose_specification
from prompt_context import compact_json
from review_specification import (REVIEW_REVISION, review_specification, validate_verdict,
                                  validate_reference_protocol, report_history)
from finding_ledger import validate_ledger, source_from_report
from specification import SpecificationError, fields, read_document, text_value
from specification_lowering import compile_package, emit_review
from specification_request import system_projection
from repair_review import build_repair_context


def plan_repair(manifest, report):
    validate_architecture(manifest)
    proof = manifest.get('provenance', {}).get('specification', {})
    if not proof or 'generation' not in proof['snapshot']['package']:
        raise SpecificationError('Automatic repair requires a generated package with its authoritative request')
    fields(report, ('review_version', 'review_revision', 'status', 'model', 'mode', 'reviewed_at',
                    'manifest_fingerprint', 'package_fingerprint', 'verdict', 'readiness_error'),
           ('repair_context', 'reference_protocol', 'finding_ledger'), 'repair review')
    if (type(report['review_version']) is not int or report['review_version'] != 1
            or type(report['review_revision']) is not int or report['review_revision'] != REVIEW_REVISION
            or report['status'] != 'changes_required' or report['mode'] not in ('llm', 'replay')):
        raise SpecificationError('Repair requires a validated changes_required review from the current protocol')
    for key in ('model', 'reviewed_at'):
        text_value(report[key], 'review.' + key)
    if (report['manifest_fingerprint'] != fingerprint(manifest)
            or report['package_fingerprint'] != proof['package_fingerprint']):
        raise SpecificationError('Repair report is stale or belongs to a different revision/configuration')
    if report['readiness_error'] is not None:
        text_value(report['readiness_error'], 'review.readiness_error')
    validate_verdict(report['verdict'], manifest, report.get('repair_context'), report_history(report))
    validate_reference_protocol(report, manifest)
    validate_ledger(report, fingerprint(manifest))
    if not report['verdict']['findings']:
        raise SpecificationError('No semantic findings to repair; unresolved agreement/readiness requires explicit disposition')
    findings = [f for f in report['verdict']['findings'] if f['resolution'] == 'design_repair']
    external = [f for f in report['verdict']['findings'] if f['resolution'] == 'external_agreement']
    if not findings:
        raise SpecificationError('No repairable design findings; external agreement requires explicit disposition: '
                                 + str([f['id'] for f in external]))
    scopes = sorted({f['scope'] for f in findings})
    return {'repair_version': 1, 'max_semantic_cycles': 1, 'compiler_attempts_per_stage': 3,
            'stage': 'system' if 'system' in scopes else 'architecture', 'scopes': scopes,
            'source_manifest_fingerprint': fingerprint(manifest), 'report_fingerprint': fingerprint(report),
            'findings': copy.deepcopy(findings), 'external_findings': copy.deepcopy(external),
            'finding_history': [source_from_report(report)],
            'readiness_error': report['readiness_error']}


def repair_and_review(manifest, report, *, output_dir, model=DEFAULT_MODEL,
                      generation_caller=None, review_caller=None):
    plan = plan_repair(manifest, report)
    output = Path(output_dir)
    if output.exists():
        raise SpecificationError(f'Repair output already exists: {output}; use a fresh directory')
    output.mkdir(parents=True)
    (output / 'repair_plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    (output / 'input_review.json').write_text(json.dumps(report, indent=2) + '\n')
    snapshot = manifest['provenance']['specification']['snapshot']
    package, documents = snapshot['package'], snapshot['documents']
    system = system_projection(package, documents)
    architecture = {'status': 'supported', **{key: copy.deepcopy(documents[key])
                                            for key in ('modules', 'graph', 'verification')},
                    'internal_types': copy.deepcopy(package['generation']['internal_types']),
                    'rule_owners': {f'{section}.rules.{rid}': copy.deepcopy(rule['owners'])
                                   for section in ('system', 'execution')
                                   for rid, rule in documents[section]['rules'].items()}}
    feedback = (
        'Repair only findings in the plan.findings list, not external_findings. External '
        'agreement stays blocking and cannot be resolved through regeneration. '
        'Repair the semantic findings from the previous revision below. They are review '
        'evidence, not permission to weaken the authoritative request. Only address fields '
        'owned by the current stage. Preserve the request IDs/text, constraints and selected '
        'configuration. System repair invalidates the old architecture; architecture-only '
        'repair must leave the locked system unchanged. Keep every original requires_review '
        'assumption and non-rtl_generation blocking question unchanged: do not remove, '
        'rename, rewrite or mark them resolved, and do '
        'not invent user acceptance, external evidence, test execution or new public ports '
        'when existing observations/inference suffice. Unresolved external agreement must '
        'remain blocking. References identify the previous snapshot.\n'
        + compact_json({k: v for k, v in plan.items() if k != 'finding_history'}))
    seed = {'stage': plan['stage'], 'system': system, 'architecture': architecture,
            'feedback': feedback, 'library': snapshot['timing_library'],
            'source_manifest_fingerprint': plan['source_manifest_fingerprint'],
            'report_fingerprint': plan['report_fingerprint']}
    status = {'status': 'in_progress', 'stage': plan['stage'], 'semantic_cycles': 1,
              'rtl_generation': 'not_run'}
    print('\n' + '=' * 80, flush=True)
    print(f'--- STARTING --- SEMANTIC REPAIR 1/1 ------ {plan["stage"].upper()} ROUTE', flush=True)
    print('=' * 80 + '\n', flush=True)
    try:
        path = propose_specification(package['generation']['request'], output_dir=output / 'source',
                                     model=model, caller=generation_caller, repair_seed=seed)
        repaired = compile_package(path, snapshot['overrides'], snapshot['profile'])
        export = output / 'architecture.yaml'
        emit_review(repaired, export)
        context = build_repair_context(manifest, repaired, plan)
        (output / 'repair_review_context.json').write_text(json.dumps(context, indent=2) + '\n')
        approval = review_specification(repaired, output_dir=output / 'review', model=model, caller=review_caller,
                                        repair_context=context)
        status.update(status='approved', manifest_fingerprint=fingerprint(repaired),
                      architecture=str(export), approval=str(approval))
        return repaired, approval, export
    except Exception as exc:
        status.update(status='stopped', error=str(exc))
        raise  # No second semantic cycle, fallback approval or RTL call.
    finally:
        (output / 'repair_status.json').write_text(json.dumps(status, indent=2) + '\n')


def main():
    from token_usage import UsageRun
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('architecture', type=Path)
    parser.add_argument('--review-report', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True, help='Fresh directory for the repaired revision and review')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    args = parser.parse_args()
    with UsageRun(ROOT / 'microarch/usage') as usage:
        try:
            _, approval, export = repair_and_review(read_document(args.architecture), read_document(args.review_report),
                                                    output_dir=args.output_dir, model=args.model)
            print(f'[SEMANTIC-REPAIR] Approved export: {export}; approval: {approval}')
            usage.status = 'completed'
        except (SpecificationError, ArchitectureError, OSError) as exc:
            usage.status = 'failed'
            print(f'[SEMANTIC-REPAIR] ERROR: {exc}')
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
