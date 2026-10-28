"""Isolate verification failures and recheck scoped RTL patches without models."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .mailbox import file_hashes
from .module_probe import digest
from .nodes import FRONTEND
from .specification_mailbox import read_json, verify_specification, verify_module_probe, publish_specification
from .specification_validation import repair_recheck_plan

STALE = {'verification_handoff.json', 'build_status.json', 'integration_lint.json'}


def failure_context(mailbox, run):
    metadata = verify_specification(mailbox)
    run = Path(run)
    pair = verify_module_probe(run / 'validation_input', mailbox)
    receipt = read_json(run / 'validation_result.json')
    if receipt.get('scope_protocol'):
        raise ValueError('Scoped failure repair mapping is not integrated yet; preserve its run and plan evidence.')
    request = read_json(run / 'validation_request.json')
    remote = receipt.get('remote_result', {})
    if (receipt.get('status') not in ('failed', 'needs_attention')
            or receipt.get('preflight_only') is not False
            or remote.get('preflight_only') is not False
            or remote.get('input_unchanged') is not True
            or remote.get('scope') != 'one_module_only'):
        raise ValueError('Repair requires a failed module invocation with preflight_only=false')
    for row in (receipt, remote):
        if (row.get('module_name') != pair['module_name']
                or row.get('parent_rtl_revision') != metadata['rtl_revision']
                or row.get('parent_snapshot_digest') != digest(Path(mailbox) / 'snapshot.json')):
            raise ValueError('Failure belongs to a different parent revision/module')
    for key in ('invocation_id', 'snapshot_digest'):
        if not request.get(key) or request[key] != remote.get(key):
            raise ValueError('Failure receipt differs from its invocation')
    if (remote['snapshot_digest'] != digest(run / 'validation_input/snapshot.json')
            or remote.get('reports_digest') != digest(run / 'reports.zip')):
        raise ValueError('Failure inputs/reports differ from their receipt')
    inventory = read_json(Path(mailbox) / 'verification_inventory.json')
    module = next(row for row in inventory['modules'] if row['module_name'] == pair['module_name'])
    native = read_json(Path(mailbox) / 'handoff/verification_handoff.json')
    native_rtl = next(name for name in native['rtl_files'] if Path(name).name == Path(module['rtl']).name)
    return {'schema': 'capstone.specification_repair_workspace', 'schema_version': 1,
            'snapshot_sha256': digest(Path(mailbox) / 'snapshot.json'),
            'rtl_revision': metadata['rtl_revision'], 'target_mhz': inventory['target_mhz'],
            'module_name': pair['module_name'], 'editable_rtl': native_rtl,
            'failure_receipt_sha256': digest(run / 'validation_result.json'),
            'failure_archive_sha256': digest(run / 'reports.zip'),
            'triage': 'review_failure_before_editing_rtl',
            'contract_changes': 'require_new_approved_frontend_build',
            'verification': 'not_accepted', 'backend_ready': False}


def prepare_workspace(mailbox, run, destination):
    mailbox, run = Path(mailbox).resolve(), Path(run).resolve()
    destination = Path(destination).absolute()
    if destination.exists():
        raise FileExistsError(destination)
    if any(destination.resolve().is_relative_to(root) for root in (mailbox, run)):
        raise ValueError('Repair workspace must be outside source mailbox and failed run')
    context = failure_context(mailbox, run)
    if any(p.is_symlink() for p in run.rglob('*')):
        raise ValueError('Failure evidence cannot contain symlinks')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.repair-workspace-', dir=destination.parent) as temporary:
        work = Path(temporary) / 'workspace'
        work.mkdir()
        shutil.copytree(mailbox, work / 'baseline')
        shutil.copytree(mailbox / 'handoff', work / 'candidate')
        for name in STALE:
            (work / 'candidate' / name).unlink(missing_ok=True)
        evidence = work / 'evidence'
        evidence.mkdir()
        shutil.copytree(run / 'validation_input', evidence / 'validation_input')
        for name in ('validation_result.json', 'validation_request.json', 'reports.zip'):
            shutil.copyfile(run / name, evidence / name)
        # Reauthenticate frozen evidence and the original source before publication.
        if failure_context(work / 'baseline', evidence) != context or failure_context(mailbox, run) != context:
            raise ValueError('Repair source/evidence changed while copying')
        (work / 'repair_request.json').write_text(json.dumps(context, indent=2) + '\n')
        if destination.exists():
            raise FileExistsError(destination)
        work.rename(destination)
    return context


def check_workspace(workspace):
    work = Path(workspace).resolve()
    context = failure_context(work / 'baseline', work / 'evidence')
    if read_json(work / 'repair_request.json') != context:
        raise ValueError('Repair request differs from immutable baseline/failure evidence')
    before = file_hashes(work / 'baseline/handoff')
    before = {name: value for name, value in before.items() if name not in STALE}
    after = file_hashes(work / 'candidate')
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    outside = sorted(set(changed) - {context['editable_rtl']})
    if outside:
        raise ValueError('Changes exceed selected RTL scope; contract/architecture changes require a new approved frontend build: ' + ', '.join(outside))
    if context['editable_rtl'] not in after:
        raise ValueError('Selected RTL cannot be removed')
    return dict(context, changed_files=changed, candidate_files=after,
                status='patch_ready_for_recheck' if changed else 'no_patch')


def recheck_workspace(workspace, output):
    work, output = Path(workspace).resolve(), Path(output).absolute()
    context = check_workspace(work)
    if context['status'] != 'patch_ready_for_recheck':
        raise ValueError('No RTL patch to recheck')
    if output.exists() or output.resolve().is_relative_to(work):
        raise ValueError('Rechecked build must be new and outside the repair workspace')
    command = [sys.executable, str(FRONTEND / 'recheck_specification.py'), str(work / 'candidate'), str(output)]
    result = subprocess.run(command, cwd=FRONTEND, capture_output=True, text=True)
    log = work / 'recheck.log'
    log.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise ValueError('Native repaired-build recheck failed; see ' + str(log))
    if check_workspace(work) != context:
        raise ValueError('Candidate changed during recheck')
    expected = file_hashes(work / 'candidate')
    produced = file_hashes(output)
    # Contracts, IR and wrapper must remain exact; only fresh evidence may differ.
    if any(produced.get(name) != value for name, value in expected.items()):
        raise ValueError('Rechecked build changed scoped candidate inputs')
    from .specification_mailbox import validate_native
    native = validate_native(output / 'verification_handoff.json')
    if native['identity']['rtl_revision'] == context['rtl_revision']:
        raise ValueError('Repaired RTL did not produce a new revision')
    receipt = dict(context, status='local_recheck_passed', new_rtl_revision=native['identity']['rtl_revision'],
                   build_files=file_hashes(output), backend_ready=False)
    (work / 'recheck_receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def publish_repair(workspace, build, destination):
    work = Path(workspace)
    context = check_workspace(work)
    receipt = read_json(work / 'recheck_receipt.json')
    if (receipt.get('status') != 'local_recheck_passed'
            or any(receipt.get(key) != value for key, value in context.items() if key != 'status')
            or receipt.get('build_files') != file_hashes(Path(build))):
        raise ValueError('Repaired build/candidate differs from its local recheck receipt')
    destination = Path(destination).absolute()
    if any(destination.resolve().is_relative_to(root.resolve()) for root in (work, Path(build))):
        raise ValueError('Published repair must be outside the repair workspace and rechecked build')
    published = publish_specification(build, destination, context['target_mhz'])
    plan = repair_recheck_plan(work / 'baseline', published)
    (work / 'publication_receipt.json').write_text(json.dumps({
        'mailbox': str(published), 'recheck': plan, 'backend_ready': False}, indent=2) + '\n')
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--mailbox', type=Path, required=True)
    prepare.add_argument('--failed-run', type=Path, required=True)
    prepare.add_argument('--output', type=Path, required=True)
    for name in ('check', 'recheck', 'publish'):
        sub = commands.add_parser(name)
        sub.add_argument('workspace', type=Path)
        if name in ('recheck', 'publish'):
            sub.add_argument('--build', type=Path, required=True)
        if name == 'publish':
            sub.add_argument('--mailbox', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        result = prepare_workspace(args.mailbox, args.failed_run, args.output)
    elif args.command == 'check':
        result = check_workspace(args.workspace)
    elif args.command == 'recheck':
        result = recheck_workspace(args.workspace, args.build)
    else:
        result = publish_repair(args.workspace, args.build, args.mailbox)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
