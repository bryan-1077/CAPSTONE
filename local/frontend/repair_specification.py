#!/usr/bin/env python3
"""Bounded semantic repair cycles: route, compile, export, then review again.

Uses the existing bounded compiler-repair loops. Never edits the parent revision,
changes request requirements, or resolves external agreement automatically.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from architecture import ArchitectureError, ROOT, fingerprint, validate_architecture
from generate_microarch import DEFAULT_MODEL, call_llm
from generate_specification import propose_specification
from prompt_context import compact_json
from review_specification import (REVIEW_REVISION, DesignReviewRequired, ReviewChangesRequired,
                                  review_specification, validate_verdict,
                                  validate_reference_protocol, report_history)
from finding_ledger import validate_ledger, source_from_report
from specification import SpecificationError, fields, read_document, text_value
from specification_lowering import compile_package, emit_review
from specification_request import system_projection
from repair_review import build_repair_context
from specification_recovery import RecoveryLimits, RepairBudget, RepairBudgetExceeded, positive_setting


def plan_repair(manifest, report, *, limits=None):
    limits = limits or RecoveryLimits.from_environment()
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
    findings = [f for f in report['verdict']['findings'] if f['resolution'] == 'design_repair']
    external = [f for f in report['verdict']['findings'] if f['resolution'] == 'external_agreement']
    if not findings and not external and report['readiness_error'] is None:
        raise SpecificationError('No semantic findings or unresolved agreement/readiness to repair')
    scopes = sorted({f['scope'] for f in findings})
    return {'repair_version': 1, 'max_semantic_cycles': limits.semantic_cycles,
            'compiler_attempts_per_stage': limits.stage_attempts,
            'stage': ('system' if 'system' in scopes else 'architecture') if findings else None, 'scopes': scopes,
            'source_manifest_fingerprint': fingerprint(manifest), 'report_fingerprint': fingerprint(report),
            'findings': copy.deepcopy(findings), 'external_findings': copy.deepcopy(external),
            'finding_history': [source_from_report(report)],
            'readiness_error': report['readiness_error']}


def _repair_seed(manifest, plan):
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
    return {'stage': plan['stage'], 'system': system, 'architecture': architecture,
            'feedback': feedback, 'library': snapshot['timing_library'],
            'source_manifest_fingerprint': plan['source_manifest_fingerprint'],
            'report_fingerprint': plan['report_fingerprint']}


def _behavior_fingerprint(manifest):
    snapshot = copy.deepcopy(manifest['provenance']['specification']['snapshot'])
    snapshot['package'].pop('generation', None)
    return fingerprint(snapshot)


def repair_and_review(manifest, report, *, output_dir, model=DEFAULT_MODEL,
                      generation_caller=None, review_caller=None, limits=None):
    limits = limits or RecoveryLimits.from_environment()
    plan = plan_repair(manifest, report, limits=limits)
    generation_tokens = positive_setting('SPECIFICATION_MAX_TOKENS', 24000)
    output = Path(output_dir)
    if output.exists():
        raise SpecificationError(f'Repair output already exists: {output}; use a fresh directory')
    output.mkdir(parents=True)
    budget = RepairBudget(limits)
    generate = budget.wrap(generation_caller or (
        lambda text, selected: call_llm(text, selected, max_tokens=generation_tokens,
                                        label='Specification')), generation_tokens)
    review = budget.wrap(review_caller or (
        lambda text, selected: call_llm(text, selected, max_tokens=6000,
                                        label='Specification-Review')), 6000)
    reference = budget.wrap(review_caller or (
        lambda text, selected: call_llm(text, selected, max_tokens=4000,
                                        label='Specification-Reference-Correction')), 4000)
    status = {'status': 'in_progress', 'stage': plan['stage'], 'semantic_cycles': 0,
              'max_semantic_cycles': limits.semantic_cycles, 'rtl_generation': 'not_run'}
    seen = {_behavior_fingerprint(manifest)}

    def needs_design_review(current, current_report, report_path, export):
        package = current['provenance']['specification']['snapshot']['package']
        pending = copy.deepcopy({
            'open_questions': {name: value for name, value in package['open_questions'].items()
                               if value['blocks'] != 'rtl_generation'},
            'assumptions': {name: value for name, value in package['assumptions'].items()
                            if value['status'] == 'requires_review'},
            'decisions': {name: value for name, value in package['decisions'].items()
                          if value['status'] not in ('accepted', 'recorded')}})
        ids = [name for entries in pending.values() for name in entries]
        status.update(status='design_review_required', stop_reason='external_agreement',
                      pending_decisions=pending, readiness_error=current_report['readiness_error'],
                      review_report=str(report_path), architecture=str(export))
        raise DesignReviewRequired(
            'Design review required; unresolved agreement/readiness requires explicit disposition. '
            f'Pending choices: {ids}. {current_report["readiness_error"] or ""} See {report_path}', report_path)

    try:
        for cycle in range(1, limits.semantic_cycles + 1):
            directory = output if cycle == 1 else output / f'cycle_{cycle:03d}'
            directory.mkdir(exist_ok=True)
            (directory / 'repair_plan.json').write_text(json.dumps(plan, indent=2) + '\n')
            input_review = directory / 'input_review.json'
            input_review.write_text(json.dumps(report, indent=2) + '\n')
            status.update(stage=plan['stage'], cycle_directory=str(directory), review_report=str(input_review))
            if not plan['findings']:
                export = directory / 'architecture.yaml'
                emit_review(manifest, export)
                needs_design_review(manifest, report, input_review, export)
            status['semantic_cycles'] = cycle
            print('\n' + '=' * 80, flush=True)
            print(f'--- STARTING --- SEMANTIC REPAIR {cycle}/{limits.semantic_cycles} ------ '
                  f'{plan["stage"].upper()} ROUTE', flush=True)
            print('=' * 80 + '\n', flush=True)
            snapshot = manifest['provenance']['specification']['snapshot']
            path = propose_specification(snapshot['package']['generation']['request'], output_dir=directory / 'source',
                                         model=model, caller=generate, repair_seed=_repair_seed(manifest, plan), limits=limits)
            repaired = compile_package(path, snapshot['overrides'], snapshot['profile'])
            export = directory / 'architecture.yaml'
            emit_review(repaired, export)
            context = build_repair_context(manifest, repaired, plan)
            (directory / 'repair_review_context.json').write_text(json.dumps(context, indent=2) + '\n')
            status.update(architecture=str(export), manifest_fingerprint=fingerprint(repaired),
                          review_report=str(directory / 'review/report.json'))
            try:
                approval = review_specification(repaired, output_dir=directory / 'review', model=model, caller=review,
                                                reference_caller=reference, repair_context=context)
            except ReviewChangesRequired as exc:
                report = read_document(exc.report_path)
                status['review_report'] = str(exc.report_path)
                plan = plan_repair(repaired, report, limits=limits)
                if not plan['findings']:
                    needs_design_review(repaired, report, exc.report_path, export)
                behavior = _behavior_fingerprint(repaired)
                if not context['changes'] or behavior in seen:
                    status['stop_reason'] = 'no_semantic_progress'
                    raise ReviewChangesRequired(f'Semantic repair made no progress; {exc}', exc.report_path) from exc
                seen.add(behavior)
                if cycle == limits.semantic_cycles:
                    status['stop_reason'] = 'semantic_cycle_limit'
                    raise
                manifest = repaired
                continue
            status.update(status='approved', approval=str(approval), review_report=str(directory / 'review/report.json'))
            return repaired, approval, export
    except Exception as exc:
        if not isinstance(exc, DesignReviewRequired):
            status['status'] = 'stopped'
        if isinstance(exc, RepairBudgetExceeded) or budget.exhausted:
            status['stop_reason'] = 'repair_budget_exhausted'
        status['error'] = str(exc)
        raise  # Never approve a rejected revision or generate RTL here.
    finally:
        status['budget'] = budget.snapshot()
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
        except DesignReviewRequired as exc:
            usage.status = 'needs_attention'
            print(f'[SEMANTIC-REPAIR] DESIGN REVIEW REQUIRED: {exc}')
            return 2
        except (SpecificationError, ArchitectureError, OSError) as exc:
            usage.status = 'failed'
            print(f'[SEMANTIC-REPAIR] ERROR: {exc}')
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
