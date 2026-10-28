"""Main CLI entry adapters for the bounded Experimental integration tools."""
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from .contracts import RunConfig


def integrated_entry(args, parser):
    if args.module_batch and args.offline_handoff:
        parser.error('--module-batch and --offline-handoff select different operations')
    entry = 'validation' if args.module_batch else 'backend'
    if args.entry != entry or args.mailbox is None:
        parser.error(f'This operation requires {entry} --mailbox FULL_EXPERIMENTAL_SNAPSHOT')
    if args.frontend_flow not in (None, 'specification'):
        parser.error('This operation requires an Experimental mailbox')
    if (args.module_probe or args.input or args.intake or args.prepare_intake
            or args.spec_approval or args.resume_build or args.failure_dir or args.cache
            or args.checks_only or args.repair or args.offline or args.allow_unvalidated
            or args.backend_python or args.skip_verification or args.no_bist
            or args.max_attempts != 3 or args.max_repair_cycles != 2):
        parser.error('Generation, debug, individual module and gate-bypass options do not apply to this operation')
    if args.offline_handoff and (args.preflight_only or args.remote_config or args.ask_password):
        parser.error('Offline handoff does not accept remote verification options')
    if args.module_batch and args.target_mhz is not None:
        parser.error('Module batch uses the immutable published target; --target-mhz does not apply')
    mailbox = args.mailbox.expanduser().resolve()
    try:
        if args.offline_handoff:
            from .specification_backend import handoff_contract
            plan = handoff_contract(mailbox, args.target_mhz)
        else:
            from .specification_verification_plan import verification_plan
            plan = verification_plan(mailbox)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(f'Experimental snapshot check stopped: {exc}')
    if args.plan:
        if args.offline_handoff:
            print(f'Experimental offline backend handoff: {plan["top_module"]}; '
                  f'{plan["target_mhz"]:g} MHz ({plan["clock_period_ns"]:g} ns).')
            print('Authenticate source; freeze RTL bytes; check top/clock/compile order; save local handoff and state.')
            print('No SSH, model calls or physical design; backend readiness remains false.')
        else:
            modules = [row['module_name'] for row in plan['modules'] if row['kind'] == 'functional']
            print(f'Experimental module batch: {len(modules)} functional modules, in inventory order:')
            for name in modules:
                print('  ' + name)
            print('SSH/Slurm consumer preflight only; no verification agents.' if args.preflight_only else
                  'Run deployed verification agents (model calls); stop at first failure; recheck revision-bound evidence.')
            print('Structural/system verification remains pending; backend readiness remains false.')
        return 0
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid4().hex[:8]
    output = (args.run_dir or Path(__file__).parent / 'runs' / run_id).expanduser().resolve()
    if output.exists() or output.is_relative_to(mailbox):
        parser.error('Run directory must be new and outside the immutable mailbox')
    operation = 'module_batch' if args.module_batch else 'offline_backend_handoff'
    print(f'Experimental {operation}; artifacts: {output}', flush=True)
    try:
        if args.offline_handoff:
            from .specification_backend import prepare_offline_handoff
            output.mkdir(parents=True)
            result = prepare_offline_handoff(mailbox, output / 'backend_handoff', args.target_mhz)
            status, code = 'offline_checks_passed', 0
        else:
            from .auth import password_session, prepare_password
            from .specification_validation import run_modules
            config = RunConfig('validation', mailbox=mailbox, frontend_flow='specification',
                               remote_config=args.remote_config, ask_password=True,
                               preflight_only=args.preflight_only, no_backend=True)
            with password_session():
                prepare_password(config)
                result = run_modules(config, output)
            accepted = result['dispatch_status'] == 'passed' and (
                args.preflight_only or result['module_coverage'] == 'complete')
            code = 0 if accepted else 2
            status = ('consumer_preflights_passed' if args.preflight_only else 'module_checks_passed') if code == 0 else 'needs_attention'
        state = {'schema': 'capstone.experimental_integration_state', 'schema_version': 1,
                 'operation': operation, 'frontend_flow': 'specification', 'status': status,
                 'mailbox': str(mailbox), 'snapshot_sha256': result['snapshot_sha256'],
                 'rtl_revision': result['rtl_revision'], 'backend_ready': False,
                 'system_verification': 'not_accepted',
                 'evidence': 'collection.json' if args.module_batch else 'backend_handoff/backend_handoff.json'}
        (output / 'state.json').write_text(json.dumps(state, indent=2) + '\n')
        print(f'{status}; system verification pending; backend readiness false.', flush=True)
        return code
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if output.is_dir():
            (output / 'state.json').write_text(json.dumps({
                'schema': 'capstone.experimental_integration_state', 'schema_version': 1,
                'operation': operation, 'frontend_flow': 'specification', 'status': 'needs_attention',
                'mailbox': str(mailbox), 'snapshot_sha256': plan['snapshot_sha256'],
                'rtl_revision': plan['rtl_revision'], 'backend_ready': False,
                'system_verification': 'not_accepted', 'error': str(exc)}, indent=2) + '\n')
        parser.exit(2, f'Experimental {operation} stopped: {exc}\n')
