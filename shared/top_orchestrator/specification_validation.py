"""Experimental module batch execution and offline evidence collection.

Module acceptance never establishes structural/system or backend readiness.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import zipfile

from .contracts import RunConfig
from .module_probe import digest, verify_source_coverage
from .specification_mailbox import read_json, verify_module_probe
from .specification_verification_plan import verification_plan
from .validation import assess, expected_designs, unpack_reports, validation_node


def accepted_module(mailbox, plan, run):
    """Recheck saved inputs and archive rather than trusting a PASS label."""
    run = Path(run).resolve()
    receipt = read_json(run / 'validation_result.json')
    pair = verify_module_probe(run / 'validation_input', mailbox)
    module = pair['module_name']
    expected = next((row for row in plan['modules']
                     if row['module_name'] == module and row['kind'] == 'functional'), None)
    if expected is None:
        raise ValueError('Receipt is not for a functional module in this snapshot')
    payload = receipt.get('remote_result', {})
    request = read_json(run / 'validation_request.json')
    if (receipt.get('status') != 'passed' or receipt.get('phase') != 'complete'
            or receipt.get('verification_scope') != 'one_module_only'
            or receipt.get('preflight_only') is not False
            or payload.get('preflight_only') is not False
            or payload.get('scope') != 'one_module_only'
            or payload.get('backend_ready') is not False
            or payload.get('input_unchanged') is not True
            or payload.get('error') or payload.get('pipeline_returncode') != 0
            or payload.get('validation_mode') != 'modules'):
        raise ValueError('Receipt does not establish executed one-module acceptance')
    for record in (receipt, payload):
        if (record.get('module_name') != module
                or record.get('parent_rtl_revision') != plan['rtl_revision']
                or record.get('parent_snapshot_digest') != plan['snapshot_sha256']):
            raise ValueError('Receipt belongs to a different module/parent revision')
    for key in ('invocation_id', 'snapshot_digest'):
        if request.get(key) != payload.get(key) or not payload.get(key):
            raise ValueError('Receipt differs from its invocation request')
    if digest(run / 'validation_input/snapshot.json') != payload['snapshot_digest']:
        raise ValueError('Receipt belongs to different module inputs')
    consumer = payload.get('consumer_preflight', {})
    if (consumer.get('status') != 'passed' or consumer.get('module_name') != module
            or consumer.get('source_catalog') != expected['required_sources']):
        raise ValueError('Consumer catalog differs from selected module sources')
    if digest(run / 'reports.zip') != payload.get('reports_digest'):
        raise ValueError('Module report archive changed')
    with tempfile.TemporaryDirectory(prefix='capstone-module-evidence-') as directory:
        reports = Path(directory) / 'reports'
        unpack_reports(run / 'reports.zip', reports)
        coverage = verify_source_coverage(reports / 'verification_reports', expected['required_sources'])
        if coverage != payload.get('source_coverage'):
            raise ValueError('Module coverage differs from its receipt')
        from pathlib import PurePosixPath
        summary_path = PurePosixPath(payload.get('summary_file', ''))
        if (summary_path.is_absolute() or '..' in summary_path.parts
                or summary_path.parts[:1] != ('verification_reports',)):
            raise ValueError('Invalid module summary path')
        summary = read_json(reports.joinpath(*summary_path.parts))
        if summary != payload.get('summary'):
            raise ValueError('Saved module summary differs from receipt')
        assess(summary, expected_designs(run / 'validation_input'),
               receipt['remote_directory'] + '/input', 'modules', module)
    return module, {'status': 'passed', 'run': str(run),
                    'receipt_sha256': digest(run / 'validation_result.json'),
                    'source_coverage': coverage}


def collect_modules(mailbox, runs):
    plan = verification_plan(mailbox)
    accepted, rejected = {}, []
    for run in runs:
        try:
            module, evidence = accepted_module(mailbox, plan, run)
            if module in accepted:
                raise ValueError('Duplicate module receipt; select one run per module')
            accepted[module] = evidence
        except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as exc:
            rejected.append({'run': str(run), 'reason': str(exc)})
    for row in plan['modules']:
        if row['module_name'] in accepted:
            row.update(accepted[row['module_name']])
    pending = [row['module_name'] for row in plan['modules']
               if row['kind'] == 'functional' and row['status'] != 'passed']
    plan.update(schema='capstone.specification_module_collection',
                module_coverage='complete' if not pending and not rejected else 'incomplete',
                rejected_receipts=rejected, pending_modules=pending,
                recheck_after_repair={'policy': 'all_modules_and_system_on_new_revision',
                                      'receipt_reuse_across_revisions': False})
    return plan


def repair_recheck_plan(previous, repaired):
    """Compare authenticated publications; never reuse old behavioral receipts."""
    old, new = verification_plan(previous), verification_plan(repaired)
    if old['snapshot_sha256'] == new['snapshot_sha256']:
        raise ValueError('Repair recheck requires a newly published snapshot')
    before = {row['module_name']: row for row in old['modules']}
    after = {row['module_name']: row for row in new['modules']}
    changed = sorted(name for name in set(before) | set(after)
                     if name not in before or name not in after
                     or any(before[name][key] != after[name][key]
                            for key in ('rtl_sha256', 'contract_sha256', 'kind')))
    return {'schema': 'capstone.specification_repair_recheck', 'schema_version': 1,
            'previous_snapshot_sha256': old['snapshot_sha256'],
            'snapshot_sha256': new['snapshot_sha256'], 'rtl_revision': new['rtl_revision'],
            'changed_modules': changed,
            'required_module_rechecks': [row['module_name'] for row in new['modules']
                                        if row['kind'] == 'functional'],
            'structural_and_system_recheck': 'required',
            'old_receipts_accepted': False, 'backend_ready': False,
            'repair_execution': 'not_performed'}


def run_modules(config, output):
    """Sequential dispatch with an explicit stop at the first unsuccessful module."""
    if config.scope_plan:
        raise ValueError('Scoped batch execution requires per-module plans and scoped collection; use a single module probe.')
    plan = verification_plan(config.mailbox)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    runs, dispatch = [], []
    for index, row in enumerate(row for row in plan['modules'] if row['kind'] == 'functional'):
        run = output / ('module_%03d' % index)
        run.mkdir()
        result = validation_node(replace(config, entry='validation', module_probe=row['module_name'],
                                         module_probe_parent=None, repair=False, no_backend=True), run)
        runs.append(run)
        dispatch.append({'module_name': row['module_name'], 'status': result.status,
                         'message': result.message})
        if result.status != 'passed':
            break
    result = collect_modules(config.mailbox, runs)
    result['preflight_only'] = config.preflight_only
    result['dispatch'] = dispatch
    result['dispatch_status'] = ('passed' if all(row['status'] == 'passed' for row in dispatch)
                                 and len(dispatch) == len([row for row in plan['modules']
                                                          if row['kind'] == 'functional'])
                                 else 'needs_attention')
    (output / 'collection.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mailbox', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('plan')
    collect = commands.add_parser('collect')
    collect.add_argument('runs', nargs='+', type=Path)
    repair = commands.add_parser('repair-plan', help='Compare a repaired publication with its previous snapshot')
    repair.add_argument('--previous', type=Path, required=True)
    run = commands.add_parser('run', help='Run remote verification agents for each functional module')
    run.add_argument('--output', required=True, type=Path)
    run.add_argument('--remote-config', type=Path)
    run.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    if args.command == 'plan':
        result = verification_plan(args.mailbox)
    elif args.command == 'collect':
        result = collect_modules(args.mailbox, args.runs)
    elif args.command == 'repair-plan':
        result = repair_recheck_plan(args.previous, args.mailbox)
    else:
        config = RunConfig('validation', mailbox=args.mailbox, frontend_flow='specification',
                           remote_config=args.remote_config, ask_password=True,
                           preflight_only=args.preflight_only)
        from .auth import password_session, prepare_password
        with password_session():
            prepare_password(config)
            result = run_modules(config, args.output)
    print(json.dumps(result, indent=2))
    if args.command == 'run' and result['dispatch_status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
