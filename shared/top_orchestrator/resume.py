"""Resume a verified full-system run at its mapped-netlist LEC gate."""

import argparse
from dataclasses import replace
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .contracts import RunConfig
from .mailbox import verify
from .orchestrator import run
from .validation import verify_backend_handoff


def resume_config(run_dir: Path, remote_config: Path | None = None) -> RunConfig:
    run_dir = run_dir.expanduser().resolve()
    saved = json.loads((run_dir / 'state.json').read_text())
    backends = [result for result in saved['results'] if result['stage'] == 'backend']
    if not backends or not backends[-1].get('artifacts', {}).get('result'):
        raise ValueError('Run has no saved backend result to resume')
    state_path = Path(backends[-1]['artifacts']['result']).resolve()
    state = json.loads(state_path.read_text())
    if not state.get('lec_enabled') or any(state.get('stage_status', {}).get(stage) != 'success'
            for stage in ('rtl_prep', 'netlist', 'mapped_netlist')):
        raise ValueError('Run has not completed the stages required to resume at LEC')
    if state.get('stage_status', {}).get('gdsii') not in (None, 'pending'):
        raise ValueError('This command resumes runs stopped before GDSII generation')
    mailbox = Path(state['mailbox_source']).resolve()
    if verify(mailbox)['rtl_revision'] != state.get('mailbox_revision'):
        raise ValueError('Mapped build and mailbox revision differ')
    receipt = Path(state['lec_validation_result']).resolve()
    verify_backend_handoff(receipt, mailbox)
    selected_remote = remote_config or state.get('lec_remote_config')
    return RunConfig('backend', mailbox=mailbox, validation_result=receipt,
                     target_mhz=1000 / state['timing_target_clock_period_ns'],
                     target_mhz_source='saved mapped build',
                     backend_python=saved.get('config', {}).get('backend_python'),
                     backend_resume_state=state_path,
                     remote_config=Path(selected_remote).expanduser().resolve() if selected_remote else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path, help='Original top-orchestrator run directory')
    parser.add_argument('--remote-config', type=Path, help='Override saved SSH/Slurm/LEC configuration')
    parser.add_argument('--plan', action='store_true', help='Validate local saved inputs and print the resume plan')
    parser.add_argument('--shared-allocation', action='store_true',
                        help='Reserve a fresh allocation for LEC and the remaining remote stages')
    parser.add_argument('--shared-allocation-cpus', type=int, default=6)
    parser.add_argument('--shared-allocation-hours', type=int, default=4)
    parser.add_argument('--shared-allocation-queue-hours', type=int, default=4)
    args = parser.parse_args(argv)
    if any(value < 1 for value in (args.shared_allocation_cpus, args.shared_allocation_hours,
                                   args.shared_allocation_queue_hours)):
        parser.error('Shared allocation CPUs and hours must be positive')
    try:
        config = resume_config(args.run, args.remote_config)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    config = replace(config, shared_allocation=args.shared_allocation,
                     shared_allocation_cpus=args.shared_allocation_cpus,
                     shared_allocation_hours=args.shared_allocation_hours,
                     shared_allocation_queue_hours=args.shared_allocation_queue_hours)
    if config.shared_allocation:
        print(f'Reserve {config.shared_allocation_cpus} CPUs in a fresh shared allocation; '
              'release after the resumed flow ends', flush=True)
    print(f'Resume mapped build at {config.target_mhz:g} MHz: LEC -> GDSII generation -> GDSII validator', flush=True)
    print(f'Saved backend state: {config.backend_resume_state}', flush=True)
    if args.plan:
        return 0
    destination = args.run.expanduser().resolve().parent / (
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_resume_' + uuid4().hex[:8])
    print(f'New reports: {destination}', flush=True)
    state = run(config, destination)
    print(f'Resume status: {state.status}; reports: {destination}', flush=True)
    return 0 if state.status == 'complete' else 2 if state.status == 'needs_attention' else 1


if __name__ == '__main__':
    raise SystemExit(main())
