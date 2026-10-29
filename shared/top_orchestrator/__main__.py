"""Run from the repository root: python -m shared.top_orchestrator --help."""

import argparse
import shlex
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .contracts import RunConfig
from .nodes import backend_command, frontend_command, spec_command
from .frequency import DEFAULT_TARGET_MHZ, positive_mhz
from .orchestrator import run


def main() -> int:
    with ExitStack() as resources:
        return _main(resources)


def _main(resources) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entry", choices=("generate", "debug", "validation", "backend", "remote-check"),
                        nargs="?", default="generate")
    parser.add_argument("--input", type=Path, help="Description text or route-compatible YAML (Experimental also accepts packages/approved architectures).")
    parser.add_argument("--frontend-flow", choices=("expand-spec", "specification"),
                        help="Standard (expand-spec, default) or Experimental (specification).")
    parser.add_argument("--prepare-intake", type=Path, metavar="JSON",
                        help="Confirm and save shared text intake only; optional 'discuss' clock advice calls a model.")
    parser.add_argument("--intake", type=Path, help="Use confirmed shared intake; Experimental currently supports frontend-only execution.")
    parser.add_argument("--spec-approval", type=Path, help="Approval for exact Experimental architecture input.")
    parser.add_argument("--resume-build", type=Path, help="Copy and resume an Experimental build without modifying its source.")
    parser.add_argument("--failure-dir", type=Path, help="Existing frontend debug workspace.")
    parser.add_argument("--mailbox", type=Path, help="Explicit published snapshot for validation or backend entry.")
    parser.add_argument("--module-probe", metavar="MODULE",
                        help="Validation only: check one Experimental module from a full published mailbox; no system/backend approval.")
    parser.add_argument("--scope-plan", type=Path,
                        help="With --module-probe: reviewed execution plan; requires scoped consumer capability before agents.")
    parser.add_argument("--module-batch", action="store_true",
                        help="Validation only: run Experimental functional modules in inventory order; stop on failure; no system/backend approval.")
    parser.add_argument("--offline-handoff", action="store_true",
                        help="Backend only: stage/check an Experimental mailbox locally; no SSH or physical design.")
    parser.add_argument("--preflight-only", action="store_true",
                        help="With --module-probe or --module-batch: check deployed consumers without running verification agents.")
    parser.add_argument("--allow-unvalidated", action="store_true",
                        help="Allow direct backend testing without validation; does not bypass normal generation routing.")
    parser.add_argument("--skip-verification", "--no-verif", dest="skip_verification", action="store_true",
                        help="Skip pre-backend RTL verification; backend and GDSII validation still run unless --no-backend is set.")
    parser.add_argument("--no-backend", action="store_true",
                        help="Disable backend execution; combine with --no-verif for frontend only.")
    parser.add_argument("--no-bist", action="store_true",
                        help="Skip BIST simulation; RTL lint still runs.")
    parser.add_argument("--skip-lec", "--no-lec", dest="skip_lec", action="store_true",
                        help="Skip Agent 4 LEC between mapping and GDSII; retain RTL and GDSII validation.")
    parser.add_argument("--backend-python", help="Python executable in the backend dependency environment.")
    parser.add_argument("--remote-config", type=Path, help="Optional JSON overrides for CAPSTONE_* SSH/Slurm environment variables.")
    parser.add_argument("--ask-password", action="store_true", help="Prompt securely for remote-check SSH authentication; remote flow runs always request a password after selection.")
    parser.add_argument("--cache", action="store_true")
    parser.add_argument("--target-mhz", type=positive_mhz,
                        help="Backend clock target; bypass the frequency prompt.")
    parser.add_argument("--checks-only", "--no-rtlgen", dest="checks_only", action="store_true",
                        help="Skip spec selection and generation; check existing RTL with system lint and BIST.")
    parser.add_argument("--repair", action="store_true", help="Allow debug to propose and apply repairs.")
    parser.add_argument("--offline", action="store_true", help="Debug evidence collection without model calls.")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-repair-cycles", type=int, default=2,
                        help="Maximum automatic debug cycles across stages; 0 disables routing.")
    parser.add_argument("--plan", action="store_true", help="Print routing and commands without executing or writing files.")
    parser.add_argument("--run-dir", type=Path, help="New directory for logs and state.json.")
    parser.add_argument('--shared-allocation', action='store_true',
                        help='Reserve CPUs once and reuse them for every remote stage')
    parser.add_argument('--shared-allocation-cpus', type=int, default=6)
    parser.add_argument('--shared-allocation-hours', type=int, default=4)
    parser.add_argument('--shared-allocation-queue-hours', type=int, default=4)
    args = parser.parse_args()
    if any(value < 1 for value in (args.shared_allocation_cpus, args.shared_allocation_hours,
                                   args.shared_allocation_queue_hours)):
        parser.error('Shared allocation CPUs and hours must be positive')
    if args.shared_allocation and (args.entry != 'generate' or args.skip_verification or args.no_backend
            or args.frontend_flow == 'specification' or args.prepare_intake
            or args.module_probe or args.module_batch or args.offline_handoff):
        parser.error('--shared-allocation requires a verified full generate flow with backend enabled')
    if args.skip_lec and (args.no_backend or args.prepare_intake or args.module_probe
                          or args.module_batch or args.offline_handoff
                          or args.frontend_flow == "specification"):
        parser.error("--skip-lec requires a flow that includes backend")
    if args.scope_plan and not args.module_probe:
        parser.error('--scope-plan requires validation --module-probe MODULE --mailbox PARENT')
    if args.module_batch or args.offline_handoff:
        from .cli_integration import integrated_entry
        return integrated_entry(args, parser)
    if args.module_probe and (args.entry != 'validation' or args.mailbox is None):
        parser.error('--module-probe requires validation --mailbox FULL_EXPERIMENTAL_SNAPSHOT')
    if args.scope_plan:
        from .verification_scope import validate_plan
        import json
        try:
            plan = json.loads(args.scope_plan.expanduser().read_text())
            validate_plan(args.mailbox.expanduser(), plan)
            if plan['identity']['module_name'] != args.module_probe:
                raise ValueError('Scope plan selects a different module')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(f'Invalid scope plan: {exc}')
    if args.preflight_only and not args.module_probe:
        parser.error('--preflight-only requires --module-probe')
    if args.prepare_intake is not None:
        if (args.entry != "generate" or args.checks_only or args.cache or args.failure_dir
                or args.repair or args.offline or args.mailbox or args.remote_config
                or args.ask_password or args.skip_verification or args.no_backend
                or args.no_bist or args.backend_python or args.allow_unvalidated
                or args.run_dir or args.intake or args.spec_approval or args.resume_build or args.module_probe or args.preflight_only or args.max_attempts != 3 or args.max_repair_cycles != 2):
            parser.error("--prepare-intake accepts only --frontend-flow, --input (text), --target-mhz and --plan")
        if args.plan:
            print("Prepare shared intake only: select Standard/Experimental, preserve text, "
                  "select controller frequency, confirm request, save JSON; no model calls or generation.")
            return 0
        from .intake import collect_intake
        try:
            result = collect_intake(args.frontend_flow, args.input, args.target_mhz,
                                    args.prepare_intake.expanduser().resolve())
        except (OSError, ValueError, EOFError, KeyboardInterrupt) as exc:
            parser.exit(2, f"Intake stopped: {exc}\n")
        print(f"Request saved: {result}")
        return 0
    # Only normal interactive generation enters shared intake. Existing scripted
    # YAML/checks/debug commands retain their established behavior.
    text_input = args.input is not None and args.input.suffix.lower() not in (".yaml", ".yml")
    if args.plan and text_input and not args.prepare_intake:
        if args.entry != "generate" or args.checks_only or args.intake:
            parser.error("Description text requires generate without --checks-only or --intake")
        if not args.input.expanduser().is_file():
            parser.error("--input must point to an existing description")
        if args.spec_approval or args.resume_build:
            parser.error("Approval/resume options require architecture YAML")
        print(f"Read full description from {args.input}; select "
              + (args.frontend_flow or "Standard (default) or Experimental")
              + "; select target controller frequency; confirm/edit request.")
        print("Standard interprets supported features and confirms YAML; Experimental generates "
              "system specification, reviewed architecture and isolated RTL/lint/handoff. "
              "Experimental currently requires --no-verif --no-backend. No execution or writes.")
        return 0
    if (args.entry == "generate" and not args.checks_only and not args.intake
            and not args.plan and (text_input or (args.input is None and sys.stdin.isatty()))):
        from .intake import collect_intake
        intake_workspace = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="capstone-intake-")))
        try:
            args.intake = collect_intake(args.frontend_flow, args.input if text_input else None,
                args.target_mhz, intake_workspace / "intake.json", allow_advisor=True)
        except (OSError, ValueError, EOFError, KeyboardInterrupt) as exc:
            parser.exit(2, f"Intake stopped: {exc}\n")
        args.input = None
    intake_record = None
    if args.intake is not None:
        from .specification_frontend import read_intake
        try:
            intake_record = read_intake(args.intake.expanduser(), args.frontend_flow, args.target_mhz)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        if args.entry != "generate" or args.input or args.checks_only:
            parser.error("--intake requires generate without --input or --checks-only")
        args.frontend_flow = intake_record["frontend_flow"]
        args.target_mhz = float(intake_record["target_mhz"])
    if args.frontend_flow == "specification" and not args.module_probe:
        from .specification_frontend import specification_command
        from .specification_inputs import inspect_input, planned_snapshot_path
        if (args.entry != "generate" or not args.skip_verification or not args.no_backend
                or args.checks_only or args.failure_dir or args.mailbox
                or args.remote_config or args.ask_password or args.backend_python
                or args.allow_unvalidated or args.repair or args.offline or args.no_bist
                or args.skip_lec or args.shared_allocation
                or args.max_attempts != 3 or args.max_repair_cycles != 2):
            parser.error("Experimental currently requires generate --no-verif --no-backend; legacy debug/check/BIST options are unsupported")
        info = None
        try:
            if args.intake is None:
                if args.input is None:
                    if args.plan:
                        print("Experimental: collect shared description, frequency and confirmation; "
                              "generate specification, review/repair, RTL, integration lint and authenticated handoff; frontend-only stop.")
                        return 0
                    parser.error("Supply --input YAML, --intake JSON, or use interactive intake")
                info = inspect_input(args.input, args.spec_approval, args.target_mhz)
                if args.target_mhz is None:
                    parser.error("Experimental YAML input requires --target-mhz; controller clock is never inferred from DDR speed")
                if args.resume_build and info["mode"] != "approved":
                    parser.error("--resume-build requires approved architecture input")
                if args.resume_build and not args.resume_build.expanduser().is_dir():
                    parser.error("--resume-build must be an existing directory")
            elif args.spec_approval or args.resume_build:
                parser.error("Approval/resume options require --input architecture.yaml")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        mode = info["mode"] if info else "request"
        if args.cache and (mode != "approved" or args.resume_build):
            parser.error("--cache requires approved architecture input without --resume-build")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
        run_dir = (args.run_dir or Path(__file__).parent / "runs" / run_id).expanduser().resolve()
        if args.plan:
            root = run_dir / "frontend"
            print(f"Experimental (specification): {mode} input; snapshot all compiler inputs; "
                  f"controller target {args.target_mhz:g} MHz.")
            if mode == "request":
                from .timing_model import SUMMARY
                print(SUMMARY + " Explicit YAML timing scopes are preserved; generic requests receive no DDR default.")
            if info:
                for path in info["files"]:
                    print(f"Snapshot: {path}")
            planned_input = (planned_snapshot_path(info, root / "input") if info else root / "input/request.yaml")
            if mode == "request" and info:
                planned_input = root / "request.yaml"
            print(shlex.join(specification_command(planned_input,
                root, args.cache, mode=mode, approval=root / "input/approval.json" if args.spec_approval else None,
                resume=bool(args.resume_build))))
            if args.resume_build:
                print(f"Copy and resume build: {args.resume_build}; source build remains unchanged.")
            print("Validate build approval, integration lint and authenticated RTL handoff; "
                  "publish authenticated Experimental mailbox, then stop. No legacy BIST/publication, "
                  "remote verification, backend or outer retry loop.")
            return 0
        config = RunConfig("generate", frontend_flow="specification",
                           intake=args.intake.expanduser().resolve() if args.intake else None,
                           input_yaml=args.input.expanduser().resolve() if args.input else None,
                           spec_approval=args.spec_approval.expanduser().resolve() if args.spec_approval else None,
                           resume_build=args.resume_build.expanduser().resolve() if args.resume_build else None,
                           target_mhz=args.target_mhz, cache=args.cache,
                           skip_verification=True, no_backend=True)
        try:
            state = run(config, run_dir)
        except (OSError, ValueError) as exc:
            parser.exit(2, f"Experimental stopped: {exc}\n")
        print(f"Experimental: {state.status}; artifacts: {run_dir}")
        return 0 if state.status == "complete" else 2
    if args.spec_approval or args.resume_build:
        parser.error("--spec-approval and --resume-build require Experimental")
    if args.skip_verification and args.entry != "generate":
        parser.error("--skip-verification applies only to generate")
    if (args.no_backend or args.no_bist) and args.entry != "generate":
        parser.error("--no-backend and --no-bist apply only to generate")
    if args.skip_verification and not args.no_backend and args.checks_only and args.target_mhz is None:
        parser.error("--checks-only with --skip-verification requires --target-mhz")
    direct_backend_route = args.entry == "backend" or (args.skip_verification and not args.no_backend)
    backend_route = args.entry == "backend" or (args.entry in ("generate", "debug") and not args.no_backend)
    if args.skip_lec and not backend_route:
        parser.error("--skip-lec requires a flow that includes backend")
    needs_remote = (args.entry in ("generate", "validation", "remote-check") and not args.skip_verification) or (args.entry == "debug" and args.repair)
    if not direct_backend_route and ((needs_remote and not args.plan) or args.entry == "remote-check" or args.remote_config):
        from .remote import RemoteConfig, probe_command
        try:
            remote = RemoteConfig.load(args.remote_config)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(str(exc))
    if (direct_backend_route or args.skip_verification) and args.remote_config is not None:
        parser.error("Backend entry uses CAPSTONE_SSH_* environment settings rather than --remote-config")
    if args.allow_unvalidated and args.entry != "backend":
        parser.error("--allow-unvalidated applies only to backend entry; use --skip-verification for generation")
    if args.backend_python and not backend_route:
        parser.error("--backend-python requires a flow that includes backend")
    if args.mailbox and args.entry not in ("validation", "backend"):
        parser.error("--mailbox applies to validation or backend entry")
    if args.entry == "validation" and args.mailbox is None:
        parser.error("validation requires --mailbox pointing to a published snapshot")
    if args.entry == "backend" and args.allow_unvalidated:
        if args.mailbox is None or args.target_mhz is None:
            parser.error("Unvalidated backend testing requires --mailbox and --target-mhz")
    if args.checks_only and (args.entry != "generate" or args.input or args.cache):
        parser.error("--checks-only requires generate mode without --input or --cache")
    if args.input is not None and not args.input.expanduser().is_file():
        parser.error("--input must point to an existing YAML file")
    if args.entry == "debug" and (args.failure_dir is None or not args.failure_dir.expanduser().is_dir()):
        parser.error("debug requires --failure-dir pointing to an existing debug workspace")
    if args.max_repair_cycles < 0:
        parser.error("--max-repair-cycles must be nonnegative")
    if args.max_attempts < 1:
        parser.error("--max-attempts must be positive")
    if args.entry != "generate" and (args.input or args.cache):
        parser.error("--input and --cache apply only to generate")
    if args.entry != "debug" and (args.failure_dir or args.repair or args.offline or args.max_attempts != 3):
        parser.error("debug options apply only to debug")
    config = RunConfig(args.entry,
                       args.input.expanduser().resolve() if args.input else None,
                       args.failure_dir.expanduser().resolve() if args.failure_dir else None,
                       args.cache, args.repair, args.offline, args.max_attempts, args.max_repair_cycles,
                       recheck_only=args.checks_only, target_mhz=args.target_mhz,
                       target_mhz_source="command line" if args.target_mhz is not None else None,
                       mailbox=args.mailbox.expanduser().resolve() if args.mailbox else None,
                       allow_unvalidated=args.allow_unvalidated, backend_python=args.backend_python,
                       remote_config=args.remote_config.expanduser().resolve() if args.remote_config else None,
                       ask_password=args.ask_password, skip_verification=args.skip_verification,
                       no_backend=args.no_backend, no_bist=args.no_bist,
                       frontend_flow="specification" if args.module_probe else args.frontend_flow or "expand-spec",
                       intake=args.intake.expanduser().resolve() if args.intake else None,
                       module_probe=args.module_probe, preflight_only=args.preflight_only,
                       scope_plan=args.scope_plan.expanduser().resolve() if args.scope_plan else None,
                       skip_lec=args.skip_lec,
                       shared_allocation=args.shared_allocation,
                       shared_allocation_cpus=args.shared_allocation_cpus,
                       shared_allocation_hours=args.shared_allocation_hours,
                       shared_allocation_queue_hours=args.shared_allocation_queue_hours)
    if args.plan:
        if args.module_probe:
            print(f'Experimental module: {args.module_probe}; parent mailbox: {config.mailbox}')
            if args.scope_plan:
                print('Freeze reviewed plan and unchanged module snapshot; SSH/Slurm network-disabled --scope-capabilities discovery.')
                print('Stop without agents; save scoped capability receipt.' if args.preflight_only else
                      'Require capstone.module_execution_plan.v1; run --execution-request/--execution-results; validate scoped results and archive locally.')
                print('No legacy fallback; deferred system sources and backend readiness remain pending.')
                return 0
            print('Authenticate parent and isolated YAML/SV/context; SSH/Slurm deployed-reader and CLI preflight (network-disabled probe).')
            print('Stop without agents; record a capability receipt.' if args.preflight_only else
                  'Run the deployed module pipeline; require normal stage PASS and complete source-linked requirement/obligation/contract evidence.')
            print('Receipt binds parent revision, module snapshot, consumer hashes and reports; system verification/backend readiness remain false.')
            return 0
        if args.shared_allocation:
            print(f'Reserve {args.shared_allocation_cpus} CPUs once for '
                  f'{args.shared_allocation_hours} hours after frontend; reuse one Slurm job ID '
                  'for verification, backend, '
                  + ('' if args.skip_lec else 'LEC and ')
                  + 'GDSII validation; release on exit')
        if args.entry == "remote-check":
            print(f"SSH {remote.username}@{remote.host}:{remote.port}; remote directory: {remote.project_dir}")
            print(probe_command(remote, "PLAN"))
            print("Connection/environment preflight only; no validation or backend execution.")
            return 0
        if args.checks_only:
            print("Check existing RTL: system lint" + ("; BIST skipped" if args.no_bist else " -> BIST")
                  + "; no spec selection or regeneration")
        elif args.entry == "generate":
            if config.intake is not None:
                print("Standard (expand-spec): preserve confirmed text, interpret supported features, "
                      "preview/confirm YAML, validate selected controller frequency.")
            elif config.input_yaml is None:
                print(shlex.join(spec_command(Path("<run-dir>/approved_specs.yaml"))))
            if not (args.skip_verification and args.no_backend):
                print(f"Select backend target: {args.target_mhz:g} MHz" if args.target_mhz is not None
                      else f"Prompt for target MHz: approved controller clock or {DEFAULT_TARGET_MHZ:g} MHz fallback; optional discuss")
            from dataclasses import replace
            print(shlex.join(frontend_command("generate", replace(config,
                input_yaml=config.input_yaml or Path("<run-dir>/approved_specs.yaml")))))
        elif args.entry == "debug":
            print(shlex.join(frontend_command(args.entry, config)))
        elif args.entry == "backend" and args.allow_unvalidated:
            print(shlex.join(backend_command(config, config.mailbox) + [
                "--result-json", "<run-dir>/backend_result.json", "--log-dir", "<run-dir>/backend_reports"]))
        if args.entry == "generate":
            print("BIST skipped" if args.no_bist else "Then: bash local/frontend/run_sim.sh --no-wave-prompt (BIST)")
            print("After checks pass: publish RTL and matching YAMLs to a new mailbox revision")
            if args.skip_verification and not args.no_backend:
                print(shlex.join(backend_command(config if args.target_mhz is not None else replace(
                    config, target_mhz=DEFAULT_TARGET_MHZ), Path("shared/mailbox/<run-id>/revision_001")) + [
                    "--result-json", "<backend-stage-dir>/backend_result.json",
                    "--log-dir", "<backend-stage-dir>/backend_reports"]))
                if args.target_mhz is None:
                    print("Backend command preview uses the fallback; execution uses the selected frequency.")
        routes = {"generate": ("frontend -> publish snapshot -> stop (verification and backend skipped)"
                               if args.skip_verification and args.no_backend else
                               "generate -> lint/BIST -> publish new snapshot -> backend (remote verification skipped)"
                               if args.skip_verification else
                               "generate -> publish snapshot -> remote validation -> stop (no backend)"
                               if args.no_backend else
                               "generate -> lint/BIST -> publish snapshot -> remote validation -> backend"),
                  "debug": "debug -> system lint + BIST -> publish snapshot -> remote validation -> backend",
                  "validation": "upload selected snapshot -> SSH/srun validation -> retrieve reports -> stop (no backend)",
                  "backend": ("backend on explicit UNVALIDATED snapshot -> inspect result" if args.allow_unvalidated
                              else "backend requires accepted validation evidence")}
        print(routes[args.entry])
        if backend_route:
            if not args.allow_unvalidated and not args.skip_verification:
                print("Backend stages: RTL prep -> generic netlist -> mapped netlist -> "
                      + ("" if args.skip_lec else "Agent 4 LEC -> ") + "GDSII generation")
            if args.skip_lec:
                print("Agent 4 LEC skipped; records LEC: not_run.")
            print("After backend success: SSH/srun GDSII validation -> retrieve reports; "
                  "PASS_WITH_GAPS completes the run with gaps recorded in the summary.")
        print("Frontend failures with supported evidence -> debug -> recheck -> publish -> "
              + ("stop" if args.skip_verification and args.no_backend else
                 "backend" if args.skip_verification else
                 "validation" if args.no_backend else "validation -> backend"))
        if args.skip_verification and not args.no_backend:
            print("Backend completion records validation: not_run.")
        elif not args.skip_verification:
            print("System Agent 2/3 failures after Agent 1 PASS -> debug -> recheck -> validation; "
                  + ("backend runs after READY_FOR_BACKEND." if backend_route else "no backend."))
        return 0
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    run_dir = (args.run_dir or Path(__file__).parent / "runs" / run_id).expanduser().resolve()
    try:
        state = run(config, run_dir)
    except OSError as exc:
        parser.exit(1, f"Cannot create/write run artifacts: {exc}\n")
    return 0 if state.status == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
