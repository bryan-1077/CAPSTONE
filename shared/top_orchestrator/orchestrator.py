"""Deterministic routing. Automatic repair loops will follow the report contracts."""

import json
from dataclasses import asdict, replace
from pathlib import Path

from .contracts import RunConfig, RunState, Stage, StageResult
from .nodes import FRONTEND, NODES, REPO_ROOT
from .frequency import prepare_generation, select_frequency
from .mailbox import publish
from .display import print_node_header
from .auth import password_session, prepare_password


def next_stage(result: StageResult, config: RunConfig | None = None) -> Stage | None:
    if result.status == "failed" and result.failure is not None:
        return "debug"
    if result.status != "passed":
        return None
    if result.stage == "generate" and config is not None and config.skip_verification:
        return None if config.no_backend else "backend"
    if result.stage == "validation" and config is not None and config.entry in ("generate", "debug"):
        return None if config.no_backend else "backend"
    return {"generate": "validation", "debug": "generate",
            "validation": None, "backend": "gdsii-validation",
            "gdsii-validation": None, "remote-check": None}[result.stage]


def save_state(state: RunState, run_dir: Path) -> None:
    temporary = run_dir / "state.json.tmp"
    temporary.write_text(json.dumps(asdict(state), indent=2, default=str) + "\n",
                         encoding="utf-8")
    temporary.replace(run_dir / "state.json")


def run(config: RunConfig, run_dir: Path) -> RunState:
    with password_session():
        return _run(config, run_dir)


def _run(config: RunConfig, run_dir: Path) -> RunState:
    if config.frontend_flow != "expand-spec" and not (config.entry == 'validation' and config.module_probe):
        if not (config.frontend_flow == "specification" and (config.intake or config.input_yaml)
                and config.entry == "generate" and config.skip_verification and config.no_backend
                and not config.recheck_only):
            raise ValueError("Experimental currently requires confirmed intake and frontend-only generation")
        from .specification_frontend import read_intake, run_specification
        if config.intake:
            record = read_intake(config.intake, config.frontend_flow, config.target_mhz)
            config = replace(config, target_mhz=float(record["target_mhz"]),
                             target_mhz_source=record.get("target_mhz_source", "confirmed intake"))
        elif config.target_mhz is None:
            raise ValueError("Experimental YAML input requires an explicit target controller frequency")
        run_dir.mkdir(parents=True, exist_ok=False)
        state = RunState(config, current_stage="generate", status="running")
        save_state(state, run_dir)
        try:
            result = run_specification(config, run_dir)
        except (Exception, KeyboardInterrupt) as exc:
            result = StageResult("generate", "needs_attention", f"Experimental frontend stopped: {type(exc).__name__}: {exc}",
                                 {"frontend_root": str(run_dir / "frontend"), "log": str(run_dir / "frontend/frontend.log")})
        if result.status == "passed":
            try:
                from .specification_mailbox import publish_specification
                destination = REPO_ROOT / "shared/mailbox" / run_dir.name / "revision_001"
                mailbox = publish_specification(Path(result.artifacts["build"]), destination, config.target_mhz)
                result.artifacts.update(mailbox=str(mailbox), publication="published")
                result.message = "Experimental frontend and authenticated mailbox completed; downstream verification remains pending."
                state.config = replace(state.config, mailbox=mailbox)
            except (Exception, KeyboardInterrupt) as exc:
                result.status = "needs_attention"
                result.message = f"Frontend completed but Experimental publication stopped: {type(exc).__name__}: {exc}"
        owned_intake = run_dir / "frontend/input/intake.json"
        if config.intake and owned_intake.is_file():
            state.config = replace(state.config, intake=owned_intake)
        state.results.append(result)
        state.current_stage = None
        state.status = "complete" if result.status == "passed" else result.status
        save_state(state, run_dir)
        return state
    # Never overwrite an earlier run's evidence.
    run_dir.mkdir(parents=True, exist_ok=False)
    state = RunState(config, current_stage=config.entry, status="running")
    save_state(state, run_dir)
    if config.entry == "generate" and not config.recheck_only:
        print_node_header("specs")
        try:
            config = prepare_generation(config, run_dir)
        except (Exception, KeyboardInterrupt) as exc:
            state.status = "needs_attention"
            state.current_stage = None
            state.results.append(StageResult("generate", "needs_attention",
                f"Pre-generation selection stopped: {type(exc).__name__}: {exc}"))
            save_state(state, run_dir)
            print(state.results[-1].message, flush=True)
            return state
        state.config = config
        save_state(state, run_dir)
    try:
        if config.entry in ("generate", "debug") and not config.no_backend and config.target_mhz is None:
            config = select_frequency(config, run_dir)
            state.config = config
            save_state(state, run_dir)
        prepare_password(config)
    except (Exception, KeyboardInterrupt) as exc:
        state.status = "needs_attention"
        state.current_stage = None
        state.results.append(StageResult(config.entry, "needs_attention",
            f"SSH password entry stopped: {type(exc).__name__}"))
        save_state(state, run_dir)
        print(state.results[-1].message, flush=True)
        return state
    active_config = config
    while state.current_stage is not None:
        save_state(state, run_dir)
        stage = state.current_stage
        print_node_header(stage)
        print(f"Running {stage}; artifacts: {run_dir}", flush=True)
        try:
            step_dir = run_dir if not state.results else run_dir / f"{len(state.results):03d}_{stage}"
            step_dir.mkdir(exist_ok=True)
            result = NODES[stage](active_config, step_dir)
        except Exception as exc:
            result = StageResult(stage, "failed", f"{type(exc).__name__}: {exc}")
        if stage == "generate" and result.status == "passed":
            try:
                number = 1 + sum(r.stage == "generate" and "mailbox" in r.artifacts for r in state.results)
                revision = REPO_ROOT / "shared/mailbox" / run_dir.name / f"revision_{number:03d}"
                mailbox = publish(FRONTEND, revision, config.input_yaml)
                result.artifacts["mailbox"] = str(mailbox)
                if active_config.intake is not None:
                    from .frontend_result import standard_result
                    result.artifacts["frontend_result"] = str(standard_result(
                        mailbox, active_config, step_dir / "frontend_result.json", result.artifacts))
                active_config = replace(active_config, mailbox=mailbox,
                    allow_unvalidated=active_config.allow_unvalidated or config.skip_verification,
                    validation_result=None)
                if config.skip_verification:
                    result.message = ("Frontend completed; verification and backend skipped."
                                      if config.no_backend else
                                      "Frontend checks passed; remote verification skipped; continuing to backend.")
                    result.artifacts["validation"] = "not_run"
                if config.no_backend:
                    result.artifacts["backend"] = "not_run"
                state.config = active_config
            except Exception as exc:
                result.status = "failed"
                result.message = f"Mailbox publication failed: {type(exc).__name__}: {exc}"
                result.failure = None
        if stage == "validation" and result.status == "passed":
            receipt = result.artifacts.get("result")
            active_config = replace(active_config, validation_result=Path(receipt) if receipt else None)
            state.config = active_config
        if stage == "backend" and result.status == "passed":
            receipt = result.artifacts.get("result")
            active_config = replace(active_config, backend_result=Path(receipt) if receipt else None)
            state.config = active_config
        state.results.append(result)
        state.current_stage = next_stage(result, active_config)
        if state.current_stage == "debug":
            if state.repair_cycles >= config.max_repair_cycles:
                state.current_stage = None
                result.status = "needs_attention"
                result.message += " Repair cycle budget exhausted."
            else:
                state.repair_cycles += 1
                active_config = replace(active_config, failure=result.failure, repair=True)
        elif stage == "debug" and result.status == "passed":
            # Never regenerate after repair: doing so would overwrite the patch.
            active_config = replace(config, recheck_only=True)
        if state.current_stage is None:
            state.status = "complete" if result.status == "passed" else result.status
        save_state(state, run_dir)
        print(f"{stage}: {result.status} — {result.message}", flush=True)
    return state
