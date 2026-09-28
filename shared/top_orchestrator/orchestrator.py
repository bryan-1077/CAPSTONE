"""Deterministic routing. Automatic repair loops will follow the report contracts."""

import json
from dataclasses import asdict, replace
from pathlib import Path

from .contracts import RunConfig, RunState, Stage, StageResult
from .nodes import NODES


def next_stage(result: StageResult) -> Stage | None:
    if result.status == "failed" and result.failure is not None:
        return "debug"
    if result.status != "passed":
        return None
    return {"generate": "validation", "debug": "generate",
            "validation": "backend", "backend": None}[result.stage]


def save_state(state: RunState, run_dir: Path) -> None:
    temporary = run_dir / "state.json.tmp"
    temporary.write_text(json.dumps(asdict(state), indent=2, default=str) + "\n",
                         encoding="utf-8")
    temporary.replace(run_dir / "state.json")


def run(config: RunConfig, run_dir: Path) -> RunState:
    # Never overwrite an earlier run's evidence.
    run_dir.mkdir(parents=True, exist_ok=False)
    state = RunState(config, current_stage=config.entry, status="running")
    active_config = config
    while state.current_stage is not None:
        save_state(state, run_dir)
        stage = state.current_stage
        print(f"Running {stage}; artifacts: {run_dir}", flush=True)
        try:
            step_dir = run_dir if not state.results else run_dir / f"{len(state.results):03d}_{stage}"
            step_dir.mkdir(exist_ok=True)
            result = NODES[stage](active_config, step_dir)
        except Exception as exc:
            result = StageResult(stage, "failed", f"{type(exc).__name__}: {exc}")
        state.results.append(result)
        state.current_stage = next_stage(result)
        if state.current_stage == "debug":
            if state.repair_cycles >= config.max_repair_cycles:
                state.current_stage = None
                result.status = "needs_attention"
                result.message += " Repair cycle budget exhausted."
            else:
                state.repair_cycles += 1
                active_config = replace(config, failure=result.failure, repair=True)
        elif stage == "debug" and result.status == "passed":
            # Never regenerate after repair: doing so would overwrite the patch.
            active_config = replace(config, recheck_only=True)
        if state.current_stage is None:
            state.status = "complete" if result.status == "passed" else result.status
        save_state(state, run_dir)
        print(f"{stage}: {result.status} — {result.message}", flush=True)
    return state
