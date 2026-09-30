"""Small contracts shared by the orchestrator and its stage adapters."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Stage = Literal["generate", "debug", "validation", "backend", "remote-check"]
Status = Literal["passed", "failed", "needs_attention", "not_implemented"]


@dataclass(frozen=True)
class FailureReport:
    source: Literal["lint", "bist", "verif", "pd"]
    log: Path
    # Executed by debug from local/frontend; use absolute paths as needed.
    target_command: str | None = None


@dataclass(frozen=True)
class RunConfig:
    entry: Stage
    input_yaml: Path | None = None
    failure_dir: Path | None = None
    cache: bool = False
    repair: bool = False
    offline: bool = False
    max_attempts: int = 3
    max_repair_cycles: int = 2
    failure: FailureReport | None = None
    recheck_only: bool = False
    target_mhz: float | None = None
    target_mhz_source: str | None = None
    mailbox: Path | None = None
    allow_unvalidated: bool = False
    backend_python: str | None = None
    remote_config: Path | None = None
    ask_password: bool = False


@dataclass
class StageResult:
    stage: Stage
    status: Status
    message: str
    artifacts: dict[str, str] = field(default_factory=dict)
    returncode: int | None = None
    failure: FailureReport | None = None


@dataclass
class RunState:
    config: RunConfig
    results: list[StageResult] = field(default_factory=list)
    current_stage: Stage | None = None
    status: str = "pending"
    repair_cycles: int = 0
