"""Run from the repository root: python -m shared.top_orchestrator --help."""

import argparse
import shlex
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .contracts import RunConfig
from .nodes import backend_command, frontend_command, spec_command
from .frequency import DEFAULT_TARGET_MHZ, positive_mhz
from .orchestrator import run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entry", choices=("generate", "debug", "validation", "backend", "remote-check"),
                        nargs="?", default="generate")
    parser.add_argument("--input", type=Path, help="Use existing YAML instead of interactive spec selection.")
    parser.add_argument("--failure-dir", type=Path, help="Existing frontend debug workspace.")
    parser.add_argument("--mailbox", type=Path, help="Explicit published snapshot for validation or backend entry.")
    parser.add_argument("--allow-unvalidated", action="store_true",
                        help="Allow direct backend testing without validation; does not bypass normal generation routing.")
    parser.add_argument("--skip-verification", "--no-verif", dest="skip_verification", action="store_true",
                        help="Skip remote verification; continue to backend unless --no-backend is set.")
    parser.add_argument("--no-backend", action="store_true",
                        help="Disable backend execution; combine with --no-verif for frontend only.")
    parser.add_argument("--no-bist", action="store_true",
                        help="Skip BIST simulation; RTL lint still runs.")
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
    args = parser.parse_args()
    if args.skip_verification and args.entry != "generate":
        parser.error("--skip-verification applies only to generate")
    if (args.no_backend or args.no_bist) and args.entry != "generate":
        parser.error("--no-backend and --no-bist apply only to generate")
    if args.skip_verification and not args.no_backend and args.checks_only and args.target_mhz is None:
        parser.error("--checks-only with --skip-verification requires --target-mhz")
    direct_backend_route = args.entry == "backend" or (args.skip_verification and not args.no_backend)
    backend_route = args.entry == "backend" or (args.entry in ("generate", "debug") and not args.no_backend)
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
                       no_backend=args.no_backend, no_bist=args.no_bist)
    if args.plan:
        if args.entry == "remote-check":
            print(f"SSH {remote.username}@{remote.host}:{remote.port}; remote directory: {remote.project_dir}")
            print(probe_command(remote, "PLAN"))
            print("Connection/environment preflight only; no validation or backend execution.")
            return 0
        if args.checks_only:
            print("Check existing RTL: system lint" + ("; BIST skipped" if args.no_bist else " -> BIST")
                  + "; no spec selection or regeneration")
        elif args.entry == "generate":
            if config.input_yaml is None:
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
