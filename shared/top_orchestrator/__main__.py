"""Run from the repository root: python -m shared.top_orchestrator --help."""

import argparse
import shlex
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .contracts import RunConfig
from .nodes import frontend_command
from .orchestrator import run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entry", choices=("generate", "debug", "validation", "backend"),
                        nargs="?", default="generate")
    parser.add_argument("--input", type=Path, help="Use existing YAML instead of interactive spec selection.")
    parser.add_argument("--failure-dir", type=Path, help="Existing frontend debug workspace.")
    parser.add_argument("--cache", action="store_true")
    parser.add_argument("--checks-only", action="store_true",
                        help="Skip spec selection and generation; check existing RTL with system lint and BIST.")
    parser.add_argument("--repair", action="store_true", help="Allow debug to propose and apply repairs.")
    parser.add_argument("--offline", action="store_true", help="Debug evidence collection without model calls.")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-repair-cycles", type=int, default=2,
                        help="Maximum automatic debug cycles across stages; 0 disables routing.")
    parser.add_argument("--plan", action="store_true", help="Print routing and commands without executing or writing files.")
    parser.add_argument("--run-dir", type=Path, help="New directory for logs and state.json.")
    args = parser.parse_args()
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
                       recheck_only=args.checks_only)
    if args.plan:
        if args.checks_only:
            print("Check existing RTL: system lint -> BIST; no spec selection or regeneration")
        elif args.entry in {"generate", "debug"}:
            print(shlex.join(frontend_command(args.entry, config)))
        if args.entry == "generate":
            print("Then: bash local/frontend/run_sim.sh --no-wave-prompt (BIST)")
        routes = {"generate": "generate -> validation (not implemented; stop)",
                  "debug": "debug -> system lint + BIST -> validation after repair",
                  "validation": "validation (not implemented; stop)",
                  "backend": "backend (not implemented; stop)"}
        print(routes[args.entry])
        print("Failures with evidence -> debug -> system lint + BIST -> validation (bounded retries)")
        print("Planned continuation: validation passes -> backend -> complete")
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
