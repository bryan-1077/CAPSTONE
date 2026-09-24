#!/usr/bin/env python3
"""RTL debug agent: failure intake, analysis, and controlled patch proposals."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, TypedDict

import debug_investigation as investigation


SCRIPT_DIR = Path(__file__).resolve().parent
DEBUG_DIR = SCRIPT_DIR / "debug"
FAILURES_DIR = DEBUG_DIR / "failures"
TEMPLATES_DIR = DEBUG_DIR / "templates"
SCHEMAS_DIR = DEBUG_DIR / "schemas"
FIXTURES_DIR = DEBUG_DIR / "fixtures"
RTL_OUTPUT_DIR = SCRIPT_DIR / "rtl_output"
MANIFEST_PATH = RTL_OUTPUT_DIR / "manifest.json"
SUPPORTED_SOURCES = ("lint", "bist", "verif", "pd")
FAILURE_SUBDIRS = ("intake", "analysis", "attempts", "result")
DEFAULT_PATCH_MODEL = "protected.Claude Opus 4.8"
TAMU_API_URL = "https://chat-api.tamu.ai/openai/chat/completions"
MAX_PATCH_CONTEXT_CHARS = 12000
MAX_ATTEMPT_CONTEXT_CHARS = 4000
MAX_REPAIR_ATTEMPTS = 3
MAX_PROMPT_FAILURE_ITEMS = 12
MAX_LOG_EXCERPT_RADIUS = 8
MAX_FOCUSED_SNIPPETS_PER_FILE = 8
FOCUSED_SNIPPET_RADIUS = 5
GRAPH_CHECKPOINT_VERSION = 1
DEFAULT_LINT_COMMAND = (
    "verilator --lint-only --Wall -Wno-fatal --top-module ddr4_controller_top "
    "$(find rtl_output -name '*.sv' | sort)"
)
AUTO_LINT_COMMAND = "auto"
PATCH_SUCCESS_STATUSES = {
    "fixed",
    "patch_applied",
    "patch_applied_lint_passed",
    "patch_applied_target_passed",
}
LOCALIZATION_HINTS = {
    "backpressure": [
        "ddr4_controller_top",
        "ddr4_scheduler_scheduler",
        "ddr4_request_queue",
    ],
    "controller": ["ddr4_controller_top"],
    "refresh": [
        "ddr4_refresh_refresh_controller",
        "ddr4_scheduler_scheduler",
        "ddr4_controller_top",
    ],
    "scheduler": ["ddr4_scheduler_scheduler"],
    "tfaw": ["ddr4_tFAW_tFAW_tracker", "ddr4_controller_top"],
    "throttling": [
        "ddr4_scheduler_scheduler",
        "ddr4_controller_top",
        "ddr4_tRRD_simple_tRRD",
        "ddr4_tFAW_tFAW_tracker",
    ],
    "timing": [
        "ddr4_scheduler_scheduler",
        "ddr4_controller_top",
        "ddr4_tRRD_simple_tRRD",
        "ddr4_tFAW_tFAW_tracker",
    ],
    "trrd": [
        "ddr4_tRRD_simple_tRRD",
        "ddr4_scheduler_scheduler",
        "ddr4_controller_top",
    ],
}
READ_DATA_MISMATCH_SIGNALS = (
    "rsp_rdata",
    "rsp_valid",
    "bank_mem",
    "service_bank",
    "service_addr",
    "service_wdata",
    "service_is_write",
    "selected_req",
)


class DebugGraphState(TypedDict, total=False):
    failure_id: str
    failure_dir: str
    source: str
    command: str | None
    log: str | None
    name: str | None
    evidence_refs: dict[str, object]
    artifact_refs: dict[str, object]
    intake: dict[str, object]
    parsed_failure: dict[str, object]
    classification: dict[str, object]
    localization: dict[str, object]
    suspects: list[dict[str, object]]
    hypotheses: list[dict[str, object]]
    investigation: dict[str, object]
    inspected_files: list[dict[str, object]]
    attempt_dir: str | None
    attempts: list[dict[str, object]]
    validation: dict[str, object]
    patch_context: dict[str, object]
    check_results: list[dict[str, object]]
    status: dict[str, object]
    steps: list[dict[str, object]]
    budgets: dict[str, object]
    rtl_revision: dict[str, object]
    input_hashes: dict[str, object]
    checkpoint: dict[str, object]
    stale_inputs: dict[str, object]
    repair: bool
    investigate: bool
    resume: bool
    force_stale: bool
    model: str
    offline: bool
    max_attempts: int
    lint_command: str | None
    target_command: str | None
    keep_failed_patch: bool
    demo_mode: bool
    graph_backend: str
    stop_reason: str


@dataclass(frozen=True)
class IntakeResult:
    source: str
    mode: str
    command: str | None
    log_path: str | None
    returncode: int | None
    started_at: str
    finished_at: str
    raw_log_bytes: int


@dataclass(frozen=True)
class ParsedFailure:
    source: str
    returncode: int | None
    command_passed: bool | None
    summary: str
    counts: dict[str, int]
    verilator_diagnostics: list[dict[str, object]]
    testbench_errors: list[dict[str, object]]
    assert_failures: list[dict[str, object]]
    flow_errors: list[dict[str, object]]
    status_markers: dict[str, object]


@dataclass(frozen=True)
class FailureClassification:
    label: str
    confidence: str
    reasons: list[str]


@dataclass(frozen=True)
class RTLLocalization:
    suspects: list[dict[str, object]]
    extracted_terms: list[str]
    rtl_files_considered: list[str]
    notes: list[str]


def parse_args() -> argparse.Namespace:
    if len(sys.argv) > 1 and sys.argv[1] in {"propose", "validate", "apply", "repair", "graph", "auto"}:
        mode = sys.argv[1]
        if mode == "auto":
            parser = argparse.ArgumentParser(
                description="Run a failure check and immediately send it through the LangGraph debug flow."
            )
            parser.add_argument("mode", choices=("auto",))
            parser.add_argument("source", choices=SUPPORTED_SOURCES)
            intake = parser.add_mutually_exclusive_group(required=True)
            intake.add_argument("--command", help="Failure command to run and capture.")
            intake.add_argument("--log", help="Existing failure log to ingest.")
            parser.add_argument("--name", help="Optional short name for the failure workspace.")
            parser.add_argument("--repair", action="store_true", help="Continue through proposal, validation, and apply.")
            parser.add_argument("--investigate", action="store_true", help="Stop after investigation and evidence assessment.")
            parser.add_argument("--model", default=DEFAULT_PATCH_MODEL)
            parser.add_argument("--offline", action="store_true", help="Collect evidence without model calls and stop for review.")
            parser.add_argument("--max-attempts", type=int, default=MAX_REPAIR_ATTEMPTS)
            parser.add_argument("--lint-command", default=AUTO_LINT_COMMAND)
            parser.add_argument("--target-command", help="Command to rerun after a patch; defaults to --command.")
            parser.add_argument("--keep-failed-patch", action="store_true")
            parser.add_argument("--demo-mode", action="store_true")
            parser.add_argument("--require-langgraph", action="store_true")
            parser.add_argument("--investigation-steps", type=int, default=8)
            parser.add_argument("--investigation-context-chars", type=int, default=64000)
            return parser.parse_args()
        if mode == "graph":
            parser = argparse.ArgumentParser(
                description=(
                    "Run the RTL debug flow through LangGraph orchestration when "
                    "available, with a local node-runner fallback."
                )
            )
            parser.add_argument("mode", choices=("graph",))
            parser.add_argument(
                "source",
                nargs="?",
                choices=SUPPORTED_SOURCES,
                help="Failure source for a new graph-run intake.",
            )
            parser.add_argument(
                "--failure-dir",
                help="Existing debug/failures/<id> directory to continue through the graph.",
            )
            intake = parser.add_mutually_exclusive_group()
            intake.add_argument(
                "--command",
                help="Failing command to run and capture for a new graph-run intake.",
            )
            intake.add_argument(
                "--log",
                help="Existing failure log to ingest for a new graph-run intake.",
            )
            parser.add_argument(
                "--name",
                help="Optional short name for the failure directory when creating one.",
            )
            parser.add_argument(
                "--repair",
                action="store_true",
                help="Continue past analysis into one propose -> validate -> apply repair attempt.",
            )
            parser.add_argument(
                "--resume",
                action="store_true",
                help="Resume from the saved graph checkpoint for --failure-dir when inputs are unchanged.",
            )
            parser.add_argument(
                "--force-stale",
                action="store_true",
                help="Allow graph repair to continue even when saved checkpoint inputs are stale.",
            )
            parser.add_argument(
                "--model",
                default=DEFAULT_PATCH_MODEL,
                help=f"LLM model for patch proposal generation. Default: {DEFAULT_PATCH_MODEL}",
            )
            parser.add_argument(
                "--offline",
                action="store_true",
                help="Collect investigation evidence without model calls; stop for human assessment.",
            )
            parser.add_argument(
                "--max-attempts",
                type=int,
                default=MAX_REPAIR_ATTEMPTS,
                help=f"Maximum numbered repair attempts before marking needs_human. Default: {MAX_REPAIR_ATTEMPTS}",
            )
            parser.add_argument(
                "--lint-command",
                default=AUTO_LINT_COMMAND,
                help=(
                    "Lint/structural check to run after applying the patch. "
                    "Default: auto-select single-file lint for standalone verif reports, "
                    "otherwise full-RTL Verilator lint."
                ),
            )
            parser.add_argument(
                "--target-command",
                help="Original failing check to rerun after lint passes.",
            )
            parser.add_argument(
                "--keep-failed-patch",
                action="store_true",
                help="Leave an applied patch in the RTL tree even if lint or target checks fail.",
            )
            parser.add_argument(
                "--demo-mode",
                action="store_true",
                help="Alias for --keep-failed-patch during iterative demo repair.",
            )
            parser.add_argument(
                "--require-langgraph",
                action="store_true",
                help="Fail instead of using the local fallback when LangGraph is not installed.",
            )
            parser.add_argument("--investigation-steps", type=int, default=8,
                                help="Maximum read/search actions per investigation (default: 8).")
            parser.add_argument("--investigation-context-chars", type=int, default=64000,
                                help="Maximum collected evidence characters (default: 64000).")
            parser.add_argument("--investigate", action="store_true",
                                help="Investigate and assess evidence without proposing or applying a patch.")
            return parser.parse_args()

        if mode == "propose":
            parser = argparse.ArgumentParser(
                description="Generate a proposal-only RTL patch attempt for an existing failure."
            )
            parser.add_argument("mode", choices=("propose",))
            parser.add_argument("failure_dir", help="Path to a debug/failures/<id> directory.")
            parser.add_argument(
                "--model",
                default=DEFAULT_PATCH_MODEL,
                help=f"LLM model for patch proposal generation. Default: {DEFAULT_PATCH_MODEL}",
            )
            parser.add_argument(
                "--offline",
                action="store_true",
                help="Write prompt and placeholder proposal without calling the LLM API.",
            )
            parser.add_argument(
                "--max-attempts",
                type=int,
                default=MAX_REPAIR_ATTEMPTS,
                help=f"Maximum numbered repair attempts before marking needs_human. Default: {MAX_REPAIR_ATTEMPTS}",
            )
            return parser.parse_args()

        if mode == "validate":
            parser = argparse.ArgumentParser(
                description="Validate a proposed RTL patch attempt without applying it."
            )
            parser.add_argument("mode", choices=("validate",))
            parser.add_argument("attempt_dir", help="Path to an attempts/attempt_XX directory.")
            return parser.parse_args()

        if mode == "repair":
            parser = argparse.ArgumentParser(
                description="Run one controlled RTL repair attempt: propose, validate, apply, then check."
            )
            parser.add_argument("mode", choices=("repair",))
            parser.add_argument("failure_dir", help="Path to a debug/failures/<id> directory.")
            parser.add_argument(
                "--model",
                default=DEFAULT_PATCH_MODEL,
                help=f"LLM model for patch proposal generation. Default: {DEFAULT_PATCH_MODEL}",
            )
            parser.add_argument(
                "--offline",
                action="store_true",
                help="Write prompt and placeholder proposal without calling the LLM API.",
            )
            parser.add_argument(
                "--max-attempts",
                type=int,
                default=MAX_REPAIR_ATTEMPTS,
                help=f"Maximum numbered repair attempts before marking needs_human. Default: {MAX_REPAIR_ATTEMPTS}",
            )
            parser.add_argument(
                "--lint-command",
                default=AUTO_LINT_COMMAND,
                help=(
                    "Lint/structural check to run after applying the patch. "
                    "Default: auto-select single-file lint for standalone verif reports, "
                    "otherwise full-RTL Verilator lint."
                ),
            )
            parser.add_argument(
                "--target-command",
                help="Original failing check to rerun after lint passes.",
            )
            parser.add_argument(
                "--keep-failed-patch",
                action="store_true",
                help="Leave an applied patch in the RTL tree even if lint or target checks fail.",
            )
            parser.add_argument(
                "--demo-mode",
                action="store_true",
                help="Alias for --keep-failed-patch during iterative demo repair.",
            )
            return parser.parse_args()

        parser = argparse.ArgumentParser(
            description="Apply a validated RTL patch attempt and optionally run checks."
        )
        parser.add_argument("mode", choices=("apply",))
        parser.add_argument("attempt_dir", help="Path to an attempts/attempt_XX directory.")
        parser.add_argument(
            "--lint-command",
            default=AUTO_LINT_COMMAND,
            help=(
                "Lint/structural check to run after applying the patch. "
                "Default: auto-select single-file lint for standalone verif reports, "
                "otherwise full-RTL Verilator lint."
            ),
        )
        parser.add_argument(
            "--target-command",
            help="Original failing check to rerun after lint passes.",
        )
        parser.add_argument(
            "--keep-failed-patch",
            action="store_true",
            help="Leave an applied patch in the RTL tree even if lint or target checks fail.",
        )
        parser.add_argument(
            "--demo-mode",
            action="store_true",
            help="Alias for --keep-failed-patch during iterative demo repair.",
        )
        return parser.parse_args()

    parser = argparse.ArgumentParser(
        description="Capture RTL debug failures into debug/failures artifacts."
    )
    parser.add_argument(
        "source",
        choices=SUPPORTED_SOURCES,
        help="Failure source to record.",
    )

    intake = parser.add_mutually_exclusive_group(required=True)
    intake.add_argument(
        "--command",
        help="Failing command to run and capture, for example './run_sim.sh'.",
    )
    intake.add_argument(
        "--log",
        help="Existing failure log to ingest.",
    )

    parser.add_argument(
        "--name",
        help="Optional short name for the failure directory.",
    )
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text.strip().lower())
    slug = slug.strip("_")
    return slug[:48] or "failure"


def next_failure_index() -> int:
    ensure_debug_layout()
    highest = 0
    for path in FAILURES_DIR.iterdir():
        if not path.is_dir():
            continue
        match = re.match(r"^(\d{4})_", path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def ensure_debug_layout() -> None:
    for path in (FAILURES_DIR, TEMPLATES_DIR, SCHEMAS_DIR, FIXTURES_DIR):
        path.mkdir(parents=True, exist_ok=True)


def ensure_failure_layout(failure_dir: Path) -> None:
    for dirname in FAILURE_SUBDIRS:
        (failure_dir / dirname).mkdir(parents=True, exist_ok=True)


def intake_dir(failure_dir: Path) -> Path:
    return failure_dir / "intake"


def analysis_dir(failure_dir: Path) -> Path:
    return failure_dir / "analysis"


def result_dir(failure_dir: Path) -> Path:
    return failure_dir / "result"


def attempts_dir(failure_dir: Path) -> Path:
    return failure_dir / "attempts"


def make_failure_dir(source: str, name: str | None, command: str | None, log: str | None) -> Path:
    if name:
        base = name
    elif command:
        base = command
    elif log:
        base = Path(log).stem
    else:
        base = source

    failure_dir = FAILURES_DIR / f"{next_failure_index():04d}_{slugify(base)}"
    failure_dir.mkdir(parents=False, exist_ok=False)
    ensure_failure_layout(failure_dir)
    return failure_dir


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(SCRIPT_DIR).as_posix()
    except ValueError:
        return str(path.resolve())


def debug_log_label(message: str) -> str:
    lowered = message.lower()
    if any(word in lowered for word in ("created failure workspace", "running bist command", "ingesting", "captured raw log")):
        return "intake"
    if any(word in lowered for word in ("parsing failure", "failure detected", "failure summary", "evidence counts", "classifying", "classification", "localizing", "suspect:", "writing analysis", "patch plan")):
        return "analyze"
    if any(word in lowered for word in ("starting patch proposal", "created patch attempt", "collecting patch context", "wrote prompt", "calling patch llm", "offline mode", "patch proposal", "patch diff bytes", "patch normalization")):
        return "propose"
    if any(word in lowered for word in ("validating patch", "patch validation", "validation issue", "validation warning", "attempt:")):
        return "validate"
    if any(word in lowered for word in ("applying patch", "validated touched files", "applying unified diff", "patch apply", "rtl modified")):
        return "apply"
    if any(word in lowered for word in ("running lint", "lint result", "running target", "target result", "skipping target", "checks failed", "rollback")):
        return "check"
    if any(word in lowered for word in ("starting repair", "repair flow", "repair failed", "repair stopped", "validation passed")):
        return "repair"
    if any(word in lowered for word in ("status:", "status json", "debug log", "final status")):
        return "result"
    if "error" in lowered:
        return "error"
    return "debug"


def log_debug(failure_dir: Path | None, message: str, *, section: bool = False) -> None:
    line = f"===== {message} =====" if section else f"[debug:{debug_log_label(message)}] {message}"
    print(("\n" if section else "") + line, flush=True)
    if failure_dir is None:
        return
    log_path = failure_dir / "debug.log"
    timestamped = ("\n" if section else "") + f"{utc_now()} {line}\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(timestamped)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_rtl_state() -> dict[str, object]:
    rtl_files = sorted(RTL_OUTPUT_DIR.rglob("*.sv")) if RTL_OUTPUT_DIR.is_dir() else []
    entries = []

    for path in rtl_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        modules = re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)", text)
        entries.append(
            {
                "path": display_path(path),
                "sha256": file_sha256(path),
                "bytes": path.stat().st_size,
                "modules": modules or [path.stem],
            }
        )

    manifest = None
    if MANIFEST_PATH.is_file():
        manifest = {
            "path": display_path(MANIFEST_PATH),
            "sha256": file_sha256(MANIFEST_PATH),
            "bytes": MANIFEST_PATH.stat().st_size,
        }

    return {
        "captured_at": utc_now(),
        "rtl_root": display_path(RTL_OUTPUT_DIR),
        "file_count": len(entries),
        "files": entries,
        "manifest": manifest,
    }


def run_command(command: str) -> tuple[int, str, str, str]:
    started_at = utc_now()
    proc = subprocess.run(
        command,
        cwd=SCRIPT_DIR,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    finished_at = utc_now()
    return proc.returncode, proc.stdout or "", started_at, finished_at


def ingest_log(log_path: str) -> tuple[str, str, str]:
    started_at = utc_now()
    path = Path(log_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    raw_log = path.read_text(encoding="utf-8", errors="replace")
    finished_at = utc_now()
    return raw_log, started_at, finished_at


def collect_sibling_verif_evidence(log_path: str | None, raw_log: str, intake_path: Path) -> None:
    if not log_path:
        return

    source_path = Path(log_path).expanduser()
    if not source_path.is_absolute():
        source_path = (Path.cwd() / source_path).resolve()
    if not source_path.is_file():
        return

    report = parse_verif_json_report(raw_log)
    if not report:
        return

    evidence_dir = intake_path / "evidence"
    copied: list[dict[str, object]] = []
    candidates: list[Path] = []
    top_module = str(report.get("top_module_name") or "").strip()
    if top_module:
        candidates.extend([
            source_path.parent / f"{top_module}.sv",
            source_path.parent / f"{top_module}.yaml",
            source_path.parent / f"{top_module}.yml",
        ])

    for field in ("top_module_file", "spec_file"):
        field_path = Path(str(report.get(field) or "")).name
        if field_path:
            candidates.append(source_path.parent / field_path)

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        destination = evidence_dir / candidate.name
        shutil.copy2(candidate, destination)
        copied.append(
            {
                "source": str(candidate),
                "captured_as": display_path(destination),
                "sha256": file_sha256(destination),
                "bytes": destination.stat().st_size,
            }
        )

    if copied:
        write_json(intake_path / "evidence_files.json", copied)


def capture_intake(args: argparse.Namespace, failure_dir: Path) -> IntakeResult:
    intake_path = intake_dir(failure_dir)
    if args.command:
        returncode, raw_log, started_at, finished_at = run_command(args.command)
        write_text(intake_path / "command.txt", args.command + "\n")
        write_text(intake_path / "raw.log", raw_log)
        return IntakeResult(
            source=args.source,
            mode="command",
            command=args.command,
            log_path=None,
            returncode=returncode,
            started_at=started_at,
            finished_at=finished_at,
            raw_log_bytes=len(raw_log.encode("utf-8")),
        )

    raw_log, started_at, finished_at = ingest_log(args.log)
    write_text(intake_path / "command.txt", f"# ingested log\n{args.log}\n")
    write_text(intake_path / "raw.log", raw_log)
    if args.source == "verif":
        collect_sibling_verif_evidence(args.log, raw_log, intake_path)
    return IntakeResult(
        source=args.source,
        mode="log",
        command=None,
        log_path=args.log,
        returncode=None,
        started_at=started_at,
        finished_at=finished_at,
        raw_log_bytes=len(raw_log.encode("utf-8")),
    )


def parse_verilator_diagnostic(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if re.match(r"^%Error:\s*Exiting due to\b", stripped):
        return None

    pattern = re.compile(
        r"^%(?P<severity>Error|Warning)(?:-(?P<code>[A-Za-z0-9_]+))?:\s*"
        r"(?:(?P<file>[^:]+\.s?v):(?P<line>\d+):(?:(?P<column>\d+):)?\s*)?"
        r"(?P<message>.*)$"
    )
    match = pattern.match(stripped)
    if not match:
        return None

    line_no = match.group("line")
    column = match.group("column")
    return {
        "severity": match.group("severity").lower(),
        "code": match.group("code"),
        "file": match.group("file"),
        "line": int(line_no) if line_no else None,
        "column": int(column) if column else None,
        "message": match.group("message").strip(),
        "raw": stripped,
    }


def parse_testbench_error(line: str) -> dict[str, object] | None:
    pattern = re.compile(
        r"^\[ERROR\]\s*(?P<test>.*?)\s*\|\s*time=(?P<time>\d+)\s*\|\s*(?P<message>.*)$"
    )
    match = pattern.match(line.strip())
    if not match:
        return None

    return {
        "test": match.group("test").strip(),
        "time": int(match.group("time")),
        "message": match.group("message").strip(),
        "raw": line.strip(),
    }


def parse_assert_failure(line: str) -> dict[str, object] | None:
    pattern = re.compile(r"^\[ASSERT FAIL\]\s*(?P<message>.*?)(?:\s+at\s+(?P<time>\d+))?$")
    match = pattern.match(line.strip())
    if not match:
        return None

    time_text = match.group("time")
    return {
        "time": int(time_text) if time_text else None,
        "message": match.group("message").strip(),
        "raw": line.strip(),
    }


def parse_flow_error(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped.startswith("[FLOW] ERROR:") and not stripped.startswith("[ERROR] "):
        return None
    if stripped.startswith("[ERROR] ") and "|" in stripped:
        return None
    return {
        "message": stripped,
        "raw": stripped,
    }


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def parse_verif_json_report(raw_log: str) -> dict[str, object] | None:
    try:
        data = json.loads(raw_log)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    known_keys = {
        "is_valid",
        "error_code",
        "failure_stage",
        "issues",
        "spec_issues",
        "validation_stages",
        "verification_readiness",
    }
    if not any(key in data for key in known_keys):
        return None

    issues = string_list(data.get("issues"))
    spec_issues = string_list(data.get("spec_issues"))
    warnings = string_list(data.get("warnings"))
    stage_errors: list[dict[str, str]] = []
    validation_stages = data.get("validation_stages")
    if isinstance(validation_stages, dict):
        for stage_name, stage_data in validation_stages.items():
            if not isinstance(stage_data, dict):
                continue
            for error in string_list(stage_data.get("errors")):
                stage_errors.append({"stage": str(stage_name), "message": error})

    merged_issues: list[dict[str, str]] = []
    seen: set[str] = set()
    for source_name, values in (
        ("issues", issues),
        ("spec_issues", spec_issues),
        ("validation_stages", [item["message"] for item in stage_errors]),
    ):
        for message in values:
            if message in seen:
                continue
            seen.add(message)
            stage = str(data.get("failure_stage") or "")
            if source_name == "validation_stages":
                stage = next(
                    (item["stage"] for item in stage_errors if item["message"] == message),
                    stage,
                )
            merged_issues.append(
                {
                    "source": source_name,
                    "stage": stage,
                    "message": message,
                }
            )

    return {
        "is_valid": data.get("is_valid"),
        "severity": data.get("severity"),
        "error_code": data.get("error_code"),
        "failure_stage": data.get("failure_stage"),
        "summary": data.get("summary"),
        "issues": merged_issues,
        "warnings": warnings,
        "rtl_files": string_list(data.get("rtl_files")),
        "top_module_name": data.get("top_module_name"),
        "top_module_file": data.get("top_module_file"),
        "spec_file": data.get("spec_file"),
        "compile_tool": data.get("compile_tool"),
        "compile_passed": data.get("compile_passed"),
        "yaml_sane": data.get("yaml_sane"),
        "spec_consistent": data.get("spec_consistent"),
        "rtl_sane": data.get("rtl_sane"),
    }


def summarize_failure(
    intake: IntakeResult,
    verilator_diagnostics: list[dict[str, object]],
    testbench_errors: list[dict[str, object]],
    assert_failures: list[dict[str, object]],
    flow_errors: list[dict[str, object]],
    status_markers: dict[str, object],
) -> str:
    for diagnostic in verilator_diagnostics:
        if diagnostic["severity"] == "error":
            location = diagnostic.get("file") or "unknown file"
            line = diagnostic.get("line")
            suffix = f":{line}" if line is not None else ""
            return f"Verilator error at {location}{suffix}: {diagnostic['message']}"

    if assert_failures:
        return f"Assertion failure: {assert_failures[0]['message']}"

    if testbench_errors:
        first = testbench_errors[0]
        return f"Testbench error in {first['test']} at time {first['time']}: {first['message']}"

    if flow_errors:
        return str(flow_errors[0]["message"])

    if status_markers.get("test_fail"):
        return "Simulation reported TEST FAIL."

    if intake.mode == "command" and intake.returncode not in (None, 0):
        return f"Command exited with status {intake.returncode}, but no known failure pattern was parsed."

    if intake.mode == "command" and intake.returncode == 0:
        return "Command completed successfully; no failure pattern was parsed."

    return "Log ingested; no known failure pattern was parsed."


def parse_failure(raw_log: str, intake: IntakeResult) -> ParsedFailure:
    verilator_diagnostics: list[dict[str, object]] = []
    testbench_errors: list[dict[str, object]] = []
    assert_failures: list[dict[str, object]] = []
    flow_errors: list[dict[str, object]] = []
    status_markers: dict[str, object] = {
        "test_pass": False,
        "test_fail": False,
        "sim_done": False,
        "errors_reported": None,
    }

    for index, line in enumerate(raw_log.splitlines(), start=1):
        diagnostic = parse_verilator_diagnostic(line)
        if diagnostic:
            diagnostic["log_line"] = index
            verilator_diagnostics.append(diagnostic)
            continue

        testbench_error = parse_testbench_error(line)
        if testbench_error:
            testbench_error["log_line"] = index
            testbench_errors.append(testbench_error)
            continue

        assert_failure = parse_assert_failure(line)
        if assert_failure:
            assert_failure["log_line"] = index
            assert_failures.append(assert_failure)
            continue

        flow_error = parse_flow_error(line)
        if flow_error:
            flow_error["log_line"] = index
            flow_errors.append(flow_error)

        stripped = line.strip()
        if "TEST PASS" in stripped:
            status_markers["test_pass"] = True
        if "TEST FAIL" in stripped:
            status_markers["test_fail"] = True
        if stripped == "[SIM] Done.":
            status_markers["sim_done"] = True

        errors_match = re.search(r"\bErrors:\s*(\d+)\b", stripped)
        if errors_match:
            status_markers["errors_reported"] = int(errors_match.group(1))

    verif_report = parse_verif_json_report(raw_log) if intake.source == "verif" else None
    if verif_report:
        status_markers["verif_report"] = {
            "is_valid": verif_report.get("is_valid"),
            "severity": verif_report.get("severity"),
            "error_code": verif_report.get("error_code"),
            "failure_stage": verif_report.get("failure_stage"),
            "top_module_name": verif_report.get("top_module_name"),
            "top_module_file": verif_report.get("top_module_file"),
            "spec_file": verif_report.get("spec_file"),
            "compile_tool": verif_report.get("compile_tool"),
            "compile_passed": verif_report.get("compile_passed"),
            "yaml_sane": verif_report.get("yaml_sane"),
            "spec_consistent": verif_report.get("spec_consistent"),
            "rtl_sane": verif_report.get("rtl_sane"),
        }
        issues = verif_report.get("issues")
        if isinstance(issues, list):
            for issue_index, issue in enumerate(issues, start=1):
                if not isinstance(issue, dict):
                    continue
                message = str(issue.get("message") or "").strip()
                if not message:
                    continue
                stage = str(issue.get("stage") or verif_report.get("failure_stage") or "verif")
                top_module = str(verif_report.get("top_module_name") or "")
                flow_errors.append(
                    {
                        "message": f"[VERIF] {stage}: {message}",
                        "raw": message,
                        "log_line": issue_index,
                        "source": issue.get("source"),
                        "stage": stage,
                        "code": verif_report.get("error_code"),
                        "severity": verif_report.get("severity"),
                        "file": verif_report.get("top_module_file"),
                        "rtl_files": verif_report.get("rtl_files"),
                        "module": top_module,
                    }
                )
        if verif_report.get("is_valid") is False:
            status_markers["verif_fail"] = True

    counts = {
        "verilator_errors": sum(1 for item in verilator_diagnostics if item["severity"] == "error"),
        "verilator_warnings": sum(1 for item in verilator_diagnostics if item["severity"] == "warning"),
        "testbench_errors": len(testbench_errors),
        "assert_failures": len(assert_failures),
        "flow_errors": len(flow_errors),
    }
    if verif_report:
        issues = verif_report.get("issues")
        counts["verif_issues"] = len(issues) if isinstance(issues, list) else 0

    summary = summarize_failure(
        intake,
        verilator_diagnostics,
        testbench_errors,
        assert_failures,
        flow_errors,
        status_markers,
    )

    command_passed = None
    if intake.returncode is not None:
        command_passed = intake.returncode == 0

    return ParsedFailure(
        source=intake.source,
        returncode=intake.returncode,
        command_passed=command_passed,
        summary=summary,
        counts=counts,
        verilator_diagnostics=verilator_diagnostics,
        testbench_errors=testbench_errors,
        assert_failures=assert_failures,
        flow_errors=flow_errors,
        status_markers=status_markers,
    )


def classify_failure(parsed: ParsedFailure) -> FailureClassification:
    reasons: list[str] = []

    verilator_errors = [
        item for item in parsed.verilator_diagnostics
        if item["severity"] == "error"
    ]
    if verilator_errors:
        first = verilator_errors[0]
        message = str(first.get("message") or "").lower()
        code = str(first.get("code") or "").lower()
        raw = str(first.get("raw") or "").lower()

        reasons.append(f"Found {len(verilator_errors)} Verilator error diagnostic(s).")
        if "syntax" in code or "syntax" in message or "unexpected" in message:
            return FailureClassification("syntax", "high", reasons)
        if (
            "can't find definition" in message
            or "can't resolve" in message
            or "unknown module" in message
            or "pin not found" in message
            or "port" in message
            or "elab" in raw
        ):
            return FailureClassification("elaboration", "high", reasons)
        return FailureClassification("syntax", "medium", reasons)

    if parsed.assert_failures:
        reasons.append(f"Found {len(parsed.assert_failures)} assertion failure(s).")
        return FailureClassification("BIST assertion", "high", reasons)

    if parsed.testbench_errors:
        messages = " ".join(str(item.get("message") or "") for item in parsed.testbench_errors)
        lowered = messages.lower()
        reasons.append(f"Found {len(parsed.testbench_errors)} testbench error(s).")

        if any(word in lowered for word in ("mismatch", "expected", "actual", "compare")):
            return FailureClassification("data mismatch", "high", reasons)
        if any(word in lowered for word in ("protocol", "ready", "valid", "handshake", "timing")):
            return FailureClassification("protocol violation", "medium", reasons)
        if any(word in lowered for word in ("coverage", "covered", "unreached", "missing")):
            return FailureClassification("coverage missing", "medium", reasons)
        return FailureClassification("BIST assertion", "medium", reasons)

    if parsed.source == "pd":
        raw_markers = " ".join(str(item.get("message") or "") for item in parsed.flow_errors).lower()
        if any(word in raw_markers for word in ("setup", "hold", "slack", "timing")):
            reasons.append("PD source contains timing-related marker(s).")
            return FailureClassification("PD timing logic issue", "medium", reasons)
        if any(word in raw_markers for word in ("drc", "structural", "floating", "undriven", "unconnected")):
            reasons.append("PD source contains structural marker(s).")
            return FailureClassification("PD structural logic issue", "medium", reasons)
        reasons.append("PD source did not match a known timing or structural pattern.")
        return FailureClassification("unknown", "low", reasons)

    if parsed.source == "verif":
        report = parsed.status_markers.get("verif_report")
        report = report if isinstance(report, dict) else {}
        error_code = str(report.get("error_code") or "").lower()
        failure_stage = str(report.get("failure_stage") or "").lower()
        messages = " ".join(str(item.get("message") or "") for item in parsed.flow_errors).lower()

        if parsed.flow_errors:
            reasons.append(f"Found {len(parsed.flow_errors)} external verification issue(s).")
        if error_code:
            reasons.append(f"Verifier error_code={report.get('error_code')}.")
        if failure_stage:
            reasons.append(f"Verifier failure_stage={report.get('failure_stage')}.")

        if "yaml_rtl_mismatch" in error_code or "yaml_vs_rtl" in failure_stage:
            return FailureClassification("YAML/RTL mismatch", "high", reasons)
        if any(word in messages for word in ("protocol", "timing", "handshake", "ready", "valid")):
            return FailureClassification("protocol violation", "medium", reasons)
        if any(word in messages for word in ("coverage", "unverified", "not_tested")):
            return FailureClassification("coverage missing", "medium", reasons)
        if parsed.flow_errors:
            return FailureClassification("unknown", "medium", reasons)

    if parsed.flow_errors:
        reasons.append(f"Found {len(parsed.flow_errors)} flow-level error(s).")
        return FailureClassification("unknown", "low", reasons)

    if parsed.command_passed is True:
        reasons.append("Command passed and no failure pattern was parsed.")
        return FailureClassification("unknown", "low", reasons)

    if parsed.returncode not in (None, 0):
        reasons.append(f"Command exited with status {parsed.returncode}.")
        return FailureClassification("unknown", "low", reasons)

    reasons.append("No known failure evidence was found.")
    return FailureClassification("unknown", "low", reasons)


def load_manifest_modules() -> list[str]:
    if not MANIFEST_PATH.is_file():
        return []
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    modules = data.get("modules", [])
    if not isinstance(modules, list):
        return []
    return [str(module) for module in modules if str(module).strip()]


def collect_rtl_index() -> dict[str, object]:
    rtl_files = sorted(RTL_OUTPUT_DIR.rglob("*.sv")) if RTL_OUTPUT_DIR.is_dir() else []
    file_text: dict[Path, str] = {}
    module_to_file: dict[str, Path] = {}
    file_to_modules: dict[Path, list[str]] = {}

    for path in rtl_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        file_text[path] = text

        modules = re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)", text)
        if not modules:
            modules = [path.stem]
        file_to_modules[path] = modules

        for module in modules:
            module_to_file[module] = path

    for module in load_manifest_modules():
        module_to_file.setdefault(module, RTL_OUTPUT_DIR / f"{module}.sv")

    return {
        "rtl_files": rtl_files,
        "file_text": file_text,
        "module_to_file": module_to_file,
        "file_to_modules": file_to_modules,
    }


def is_under_rtl_output(path: Path) -> bool:
    try:
        path.resolve().relative_to(RTL_OUTPUT_DIR.resolve())
        return True
    except ValueError:
        return False


def resolve_rtl_path(path_text: object, rtl_files: list[Path]) -> Path | None:
    if not path_text:
        return None

    raw_path = Path(str(path_text))
    candidates = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.append((SCRIPT_DIR / raw_path).resolve())

    for candidate in candidates:
        if candidate.is_file() and is_under_rtl_output(candidate):
            return candidate

    basename = raw_path.name
    for rtl_file in rtl_files:
        if rtl_file.name == basename:
            return rtl_file

    return None


def add_suspect(
    suspects: dict[str, dict[str, object]],
    path: Path,
    score: int,
    reason: str,
    module: str | None = None,
    term: str | None = None,
) -> None:
    key = display_path(path)
    entry = suspects.setdefault(
        key,
        {
            "file": key,
            "module": module or path.stem,
            "score": 0,
            "reasons": [],
            "matched_terms": [],
        },
    )
    entry["score"] = int(entry["score"]) + score
    if reason not in entry["reasons"]:
        entry["reasons"].append(reason)
    if term and term not in entry["matched_terms"]:
        entry["matched_terms"].append(term)


def extract_tokens(text: str) -> list[str]:
    tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", text)
    stopwords = {
        "error", "warning", "assert", "fail", "test", "pass",
        "time", "expected", "actual", "mismatch", "unknown", "module",
        "signal", "while", "with", "from", "this", "that", "the", "and",
        "or", "at", "in", "to", "of", "is", "was", "were", "verilator",
        "simulation", "command", "exited", "status", "summary", "phase",
        "info", "sim", "rtl", "file", "files", "line", "column",
        "begin", "end", "always", "always_ff", "always_comb", "logic",
        "input", "output", "wire", "reg", "assign", "case", "endcase",
        "if", "else", "for", "generate", "endgenerate", "posedge",
        "negedge", "should", "never", "observed", "exercise", "pattern",
        "patterns", "stress", "default", "reset", "only", "still",
        "first", "last", "low", "high", "nothing", "done", "running",
        "collecting", "compiling", "built", "sources", "threads",
        "allocated", "walltime", "cpu", "home", "bryan", "desktop",
        "code", "capstone", "obj_dir", "directory", "entering",
        "leaving", "make", "into", "needing", "rev", "fedora",
        "testbench", "tb_ddr4_controller_top",
        "yaml", "verif", "but", "does", "not", "describes", "clearly",
        "drive", "driven", "assign", "directly",
    }

    terms: list[str] = []
    for token in tokens:
        if token.lower() in stopwords:
            continue
        if len(token) < 3:
            continue
        if token not in terms:
            terms.append(token)
    return terms


def extract_failure_terms(raw_log: str, parsed: ParsedFailure) -> list[str]:
    evidence_parts = [parsed.summary]
    for collection in (
        parsed.verilator_diagnostics,
        parsed.testbench_errors,
        parsed.assert_failures,
        parsed.flow_errors,
    ):
        for item in collection:
            evidence_parts.append(str(item.get("message") or ""))
            evidence_parts.append(str(item.get("raw") or ""))

    terms = extract_tokens("\n".join(evidence_parts))
    if terms:
        return terms[:80]

    fallback_lines = raw_log.splitlines()[-80:]
    return extract_tokens("\n".join(fallback_lines))[:80]


def find_module_path(term: str, module_to_file: dict[str, Path]) -> tuple[str, Path] | None:
    for module, path in module_to_file.items():
        if module.lower() == term.lower():
            return module, path
    return None


def add_hint_suspects(
    suspects: dict[str, dict[str, object]],
    term: str,
    module_to_file: dict[str, Path],
) -> None:
    for module in LOCALIZATION_HINTS.get(term.lower(), []):
        path = module_to_file.get(module)
        if path and path.is_file() and is_under_rtl_output(path):
            add_suspect(
                suspects,
                path,
                hint_score(term, module),
                f"Failure term '{term}' maps to likely module '{module}'.",
                module=module,
                term=term,
            )


def hint_score(term: str, module: str) -> int:
    lowered = term.lower()
    if lowered == "controller":
        return 15
    if module == "ddr4_scheduler_scheduler" and lowered in {
        "backpressure",
        "timing",
        "throttling",
        "trrd",
    }:
        return 60
    if module == "ddr4_controller_top" and lowered in {
        "backpressure",
        "timing",
        "throttling",
        "trrd",
    }:
        return 35
    if module == "ddr4_request_queue" and lowered == "backpressure":
        return 35
    return 45


def add_partial_module_suspects(
    suspects: dict[str, dict[str, object]],
    term: str,
    module_to_file: dict[str, Path],
) -> None:
    if "_" not in term and not term.lower().startswith("ddr4"):
        return

    lowered = term.lower()
    for module, path in module_to_file.items():
        if lowered not in module.lower():
            continue
        if path.is_file() and is_under_rtl_output(path):
            add_suspect(
                suspects,
                path,
                25,
                f"Failure term '{term}' appears in module name '{module}'.",
                module=module,
                term=term,
            )


def has_functional_failure_evidence(parsed: ParsedFailure) -> bool:
    return bool(
        parsed.testbench_errors
        or parsed.assert_failures
        or parsed.flow_errors
        or parsed.status_markers.get("test_fail")
    )


def is_read_data_mismatch(parsed: ParsedFailure) -> bool:
    evidence = [parsed.summary]
    for item in parsed.testbench_errors:
        evidence.append(str(item.get("message") or ""))
        evidence.append(str(item.get("raw") or ""))
    lowered = " ".join(evidence).lower()
    return (
        any(word in lowered for word in ("read", "rsp", "response"))
        and "data" in lowered
        and any(word in lowered for word in ("unexpected", "mismatch", "expected", "returned"))
    )


def add_read_data_mismatch_suspects(
    suspects: dict[str, dict[str, object]],
    file_text: dict[Path, str],
    file_to_modules: dict[Path, list[str]],
) -> None:
    for path, text in file_text.items():
        lowered = text.lower()
        matched = [
            signal
            for signal in READ_DATA_MISMATCH_SIGNALS
            if signal.lower() in lowered
        ]
        if not matched:
            continue

        score = 25 + (20 * len(matched))
        if "rsp_rdata" in matched and "bank_mem" in matched:
            score += 80
        if "service_bank" in matched and "service_addr" in matched:
            score += 40
        if path.name == "ddr4_controller_top.sv":
            score += 40

        modules = file_to_modules.get(path, [path.stem])
        for signal in matched:
            add_suspect(
                suspects,
                path,
                score if signal == matched[0] else 0,
                "BIST read-data mismatch matches response/storage datapath signal(s).",
                module=modules[0],
                term=signal,
            )


def localize_rtl(raw_log: str, parsed: ParsedFailure) -> RTLLocalization:
    index = collect_rtl_index()
    rtl_files = index["rtl_files"]
    file_text = index["file_text"]
    module_to_file = index["module_to_file"]
    file_to_modules = index["file_to_modules"]

    suspects: dict[str, dict[str, object]] = {}
    notes: list[str] = []

    for diagnostic in parsed.verilator_diagnostics:
        severity = diagnostic.get("severity")
        if severity == "warning" and has_functional_failure_evidence(parsed):
            continue
        path = resolve_rtl_path(diagnostic.get("file"), rtl_files)
        if not path:
            continue
        modules = file_to_modules.get(path, [path.stem])
        add_suspect(
            suspects,
            path,
            100 if severity == "error" else 25,
            "Verilator diagnostic points at this RTL file.",
            module=modules[0],
        )

    for error in parsed.flow_errors:
        raw_paths: list[object] = []
        if error.get("file"):
            raw_paths.append(error.get("file"))
        rtl_files_from_error = error.get("rtl_files")
        if isinstance(rtl_files_from_error, list):
            raw_paths.extend(rtl_files_from_error)

        for raw_path in raw_paths:
            path = resolve_rtl_path(raw_path, rtl_files)
            if not path:
                continue
            modules = file_to_modules.get(path, [path.stem])
            module = str(error.get("module") or modules[0])
            add_suspect(
                suspects,
                path,
                95,
                "External verification report points at this RTL file.",
                module=module,
            )

    terms = extract_failure_terms(raw_log, parsed)
    verif_report = parsed.status_markers.get("verif_report")
    if isinstance(verif_report, dict):
        for field in ("top_module_name", "error_code", "failure_stage"):
            value = str(verif_report.get(field) or "").strip()
            if value and value not in terms:
                terms.append(value)
    if is_read_data_mismatch(parsed):
        add_read_data_mismatch_suspects(suspects, file_text, file_to_modules)
        for term in READ_DATA_MISMATCH_SIGNALS:
            if term not in terms:
                terms.append(term)

    for term in terms:
        add_hint_suspects(suspects, term, module_to_file)
        add_partial_module_suspects(suspects, term, module_to_file)

        module_match = find_module_path(term, module_to_file)
        module_path = module_match[1] if module_match else None
        if module_path and module_path.is_file() and is_under_rtl_output(module_path):
            add_suspect(
                suspects,
                module_path,
                70,
                f"Failure text references module '{term}'.",
                module=module_match[0],
                term=term,
            )
            continue

        pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        for path, text in file_text.items():
            if pattern.search(text):
                modules = file_to_modules.get(path, [path.stem])
                add_suspect(
                    suspects,
                    path,
                    15,
                    "Failure text term(s) appear in this RTL file.",
                    module=modules[0],
                    term=term,
                )

    ranked = sorted(
        suspects.values(),
        key=lambda item: (-int(item["score"]), str(item["file"])),
    )

    for item in ranked:
        item["matched_terms"] = sorted(item["matched_terms"])

    if not ranked:
        notes.append("No RTL file matched parsed diagnostics or extracted failure terms.")

    return RTLLocalization(
        suspects=ranked,
        extracted_terms=terms,
        rtl_files_considered=[display_path(path) for path in rtl_files],
        notes=notes,
    )


def format_count_lines(counts: dict[str, int]) -> list[str]:
    lines = []
    for key in sorted(counts):
        lines.append(f"- {key}: {counts[key]}")
    return lines


def build_repair_hypothesis(classification: FailureClassification) -> str:
    label = classification.label
    if label == "syntax":
        return "Inspect the first Verilator error location and make the smallest syntax-preserving RTL correction."
    if label == "elaboration":
        return "Check module names, port names, widths, and instance connections around the suspected RTL files."
    if label == "BIST assertion":
        return "Trace the failing assertion back to the control or data path signal named in the failure text."
    if label == "data mismatch":
        return "Compare expected vs actual behavior around the named signals and update the suspected RTL logic if the test expectation is valid."
    if label == "coverage missing":
        return "Inspect whether the generated RTL can reach the missing scenario before changing behavior."
    if label == "protocol violation":
        return "Review ready/valid, timing, and command sequencing behavior around the suspected modules."
    if label == "YAML/RTL mismatch":
        return "Compare the YAML-described intent against the suspected RTL and make the smallest RTL change that satisfies the verifier report."
    if label == "PD timing logic issue":
        return "Review logic depth and timing-owned control behavior before considering structural changes."
    if label == "PD structural logic issue":
        return "Inspect undriven, floating, unconnected, or structurally invalid signals in the suspected RTL."
    return "No specific repair hypothesis is available yet; inspect the parsed evidence and suspected RTL manually."


def build_risk_note(classification: FailureClassification) -> str:
    if classification.label in {"syntax", "elaboration"}:
        return "Low functional-risk if the patch only fixes malformed syntax or invalid connections."
    if classification.label in {"data mismatch", "protocol violation", "BIST assertion"}:
        return "Moderate functional-risk; a passing local test may still hide protocol or sequencing regressions."
    if classification.label == "YAML/RTL mismatch":
        return "Moderate spec-alignment risk; confirm the YAML is authoritative before changing RTL behavior."
    if classification.label.startswith("PD "):
        return "Moderate implementation-risk; confirm the issue is logic-owned before changing RTL behavior."
    return "Unknown risk until the failure is localized more precisely."


def write_patch_plan(
    failure_dir: Path,
    parsed: ParsedFailure,
    classification: FailureClassification,
    localization: RTLLocalization,
) -> None:
    lines = [
        "# Patch Plan",
        "",
        "## Summary",
        "",
        parsed.summary,
        "",
        "## Classification",
        "",
        f"- label: {classification.label}",
        f"- confidence: {classification.confidence}",
    ]

    for reason in classification.reasons:
        lines.append(f"- reason: {reason}")

    lines.extend([
        "",
        "## Evidence Counts",
        "",
        *format_count_lines(parsed.counts),
        "",
        "## Suspected RTL",
        "",
    ])

    if localization.suspects:
        for suspect in localization.suspects[:5]:
            reasons = "; ".join(str(reason) for reason in suspect.get("reasons", []))
            terms = ", ".join(str(term) for term in suspect.get("matched_terms", []))
            lines.append(
                f"- {suspect['file']} ({suspect['module']}), score={suspect['score']}"
            )
            if reasons:
                lines.append(f"  evidence: {reasons}")
            if terms:
                lines.append(f"  matched terms: {terms}")
    else:
        lines.append("- No suspected RTL files identified.")

    lines.extend([
        "",
        "## Repair Hypothesis",
        "",
        build_repair_hypothesis(classification),
        "",
        "## Risks",
        "",
        build_risk_note(classification),
        "",
        "## Phase 1 Limit",
        "",
        "This plan is analyze-only. No RTL edits were made.",
        "",
    ])

    write_text(analysis_dir(failure_dir) / "patch_plan.md", "\n".join(lines))


def parsed_failure_detected(parsed: ParsedFailure) -> bool:
    if parsed.status_markers.get("test_fail"):
        return True
    if parsed.status_markers.get("errors_reported"):
        return True
    if parsed.status_markers.get("test_pass"):
        return False
    failure_count_keys = (
        "verilator_errors",
        "testbench_errors",
        "assert_failures",
        "flow_errors",
    )
    return any(parsed.counts.get(key, 0) > 0 for key in failure_count_keys)


def record_status(
    failure_dir: Path,
    intake: IntakeResult,
    parsed: ParsedFailure,
    classification: FailureClassification,
) -> None:
    failure_detected = parsed_failure_detected(parsed)
    if intake.mode == "command" and intake.returncode == 0 and not failure_detected:
        status = "captured_pass"
    else:
        status = "captured_failure"

    write_json(
        result_dir(failure_dir) / "status.json",
        {
            "classification": classification.label,
            "failure_detected": failure_detected,
            "summary": parsed.summary,
            "status": status,
            "phase": "write_patch_plan",
            "next_step": "phase_1_complete",
        },
    )


def resolve_failure_dir(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()

    if not path.is_dir():
        raise FileNotFoundError(f"Failure directory does not exist: {path}")

    missing = [dirname for dirname in ("intake", "analysis", "attempts", "result") if not (path / dirname).is_dir()]
    if missing:
        raise FileNotFoundError(f"Failure directory is missing subdir(s): {', '.join(missing)}")

    return path


def next_attempt_dir(failure_dir: Path) -> Path:
    root = attempts_dir(failure_dir)
    root.mkdir(parents=True, exist_ok=True)
    highest = 0
    for path in root.iterdir():
        if not path.is_dir():
            continue
        match = re.match(r"^attempt_(\d{2})$", path.name)
        if match:
            highest = max(highest, int(match.group(1)))

    attempt_dir = root / f"attempt_{highest + 1:02d}"
    attempt_dir.mkdir(parents=False, exist_ok=False)
    return attempt_dir


def numbered_attempt_dirs(failure_dir: Path) -> list[Path]:
    root = attempts_dir(failure_dir)
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.iterdir()
        if path.is_dir() and re.match(r"^attempt_\d{2}$", path.name)
    )


def attempt_limit_reached(failure_dir: Path, max_attempts: int) -> bool:
    return len(numbered_attempt_dirs(failure_dir)) >= max_attempts


def record_needs_human(failure_dir: Path, max_attempts: int, reason: str) -> None:
    status_path = result_dir(failure_dir) / "status.json"
    previous_status = read_json(status_path) if status_path.is_file() else None
    write_json(
        status_path,
        {
            "status": "needs_human",
            "phase": "attempt_limit",
            "attempts": [path.name for path in numbered_attempt_dirs(failure_dir)],
            "max_attempts": max_attempts,
            "reason": reason,
            "previous_status": previous_status,
            "rtl_modified": False,
        },
    )


def read_limited_text(path: Path, limit: int = MAX_PATCH_CONTEXT_CHARS) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def resolve_rtl_context_path(path_text: str) -> Path | None:
    path = Path(path_text)
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    path = path.resolve()
    if not path.is_file() or not is_under_rtl_output(path):
        return None
    return path


def read_optional_json(path: Path) -> object | None:
    if not path.is_file():
        return None
    return read_json(path)


def compact_items(items: object, limit: int = MAX_PROMPT_FAILURE_ITEMS) -> tuple[list[object], int]:
    if not isinstance(items, list):
        return [], 0
    return items[:limit], max(0, len(items) - limit)


def compact_parsed_failure(parsed: object) -> dict[str, object]:
    if not isinstance(parsed, dict):
        return {}

    compact: dict[str, object] = {
        "source": parsed.get("source"),
        "returncode": parsed.get("returncode"),
        "command_passed": parsed.get("command_passed"),
        "summary": parsed.get("summary"),
        "counts": parsed.get("counts", {}),
        "status_markers": parsed.get("status_markers", {}),
    }

    omitted: dict[str, int] = {}
    for key in ("verilator_diagnostics", "testbench_errors", "assert_failures", "flow_errors"):
        items, omitted_count = compact_items(parsed.get(key))
        compact[key] = items
        if omitted_count:
            omitted[key] = omitted_count

    if omitted:
        compact["omitted_repeated_items"] = omitted

    return compact


def first_failure_log_line(parsed: object) -> int | None:
    if not isinstance(parsed, dict):
        return None

    candidates: list[int] = []
    for key in ("verilator_diagnostics", "testbench_errors", "assert_failures", "flow_errors"):
        items = parsed.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            line = item.get("log_line")
            if isinstance(line, int):
                candidates.append(line)

    return min(candidates) if candidates else None


def collect_log_excerpt(failure_dir: Path, parsed: object) -> dict[str, object]:
    raw_path = intake_dir(failure_dir) / "raw.log"
    if not raw_path.is_file():
        return {"available": False, "reason": "raw.log not found"}

    lines = raw_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return {"available": False, "reason": "raw.log is empty"}

    center = first_failure_log_line(parsed)
    if center is None:
        center = max(1, len(lines) - (MAX_LOG_EXCERPT_RADIUS * 2))

    start = max(1, center - MAX_LOG_EXCERPT_RADIUS)
    end = min(len(lines), center + MAX_LOG_EXCERPT_RADIUS)
    excerpt = [
        {"line": index, "text": lines[index - 1]}
        for index in range(start, end + 1)
    ]

    return {
        "available": True,
        "center_line": center,
        "start_line": start,
        "end_line": end,
        "excerpt": excerpt,
    }


def parsed_failure_terms(parsed: object) -> list[str]:
    if not isinstance(parsed, dict):
        return []

    parts = [str(parsed.get("summary") or "")]
    for key in ("verilator_diagnostics", "testbench_errors", "assert_failures", "flow_errors"):
        items = parsed.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items[:MAX_PROMPT_FAILURE_ITEMS]:
            if isinstance(item, dict):
                parts.append(str(item.get("message") or ""))
                parts.append(str(item.get("raw") or ""))

    return extract_tokens("\n".join(parts))


def normalize_search_terms(terms: object) -> list[str]:
    normalized: list[str] = []
    if not isinstance(terms, list):
        return normalized

    skipped = {
        "access", "accepted", "actual", "classify", "expected", "latency",
        "not", "service", "unexpected", "value",
        "row", "bank", "valid", "ready", "data", "read", "write",
    }
    for term in terms:
        text = str(term).strip()
        if len(text) < 3:
            continue
        if text.lower() in skipped:
            continue
        if text not in normalized:
            normalized.append(text)

    return normalized


def line_matches_terms(line: str, terms: list[str]) -> bool:
    for term in terms:
        if re.search(rf"\b{re.escape(term)}\b", line, flags=re.IGNORECASE):
            return True
    return False


def score_focused_line(line: str, terms: list[str]) -> int:
    if not line_matches_terms(line, terms):
        return -1

    stripped = line.strip()
    score = 10
    if "<=" in line:
        score += 80
    elif re.search(r"\bassign\b", line):
        score += 30
    if re.search(r"\bif\s*\(", line) or stripped in {"end", "end else begin"}:
        score += 15
    if any(token in line for token in ("service_", "open_row", "row_open_valid")):
        score += 15
    if re.match(r"^(logic|localparam|input|output|module)\b", stripped):
        score -= 35
    if stripped.startswith("//"):
        score -= 50
    return score


def merge_line_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not windows:
        return []

    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def build_focused_rtl_snippets(text: str, terms: list[str]) -> list[dict[str, object]]:
    if not text or not terms:
        return []

    lines = text.splitlines()
    scored_lines: list[tuple[int, int]] = []
    for index, line in enumerate(lines, start=1):
        score = score_focused_line(line, terms)
        if score >= 0:
            scored_lines.append((score, index))

    windows: list[tuple[int, int]] = []
    for _score, index in sorted(scored_lines, key=lambda item: (-item[0], item[1]))[:MAX_FOCUSED_SNIPPETS_PER_FILE]:
        start = max(1, index - FOCUSED_SNIPPET_RADIUS)
        end = min(len(lines), index + FOCUSED_SNIPPET_RADIUS)
        windows.append((start, end))

    snippets = []
    for start, end in merge_line_windows(windows):
        snippet_lines = [
            f"{line_no:5d}: {lines[line_no - 1]}"
            for line_no in range(start, end + 1)
        ]
        snippets.append(
            {
                "start_line": start,
                "end_line": end,
                "text": "\n".join(snippet_lines),
            }
        )

    return snippets


def collect_previous_attempts_context(failure_dir: Path, max_attempts: int = 3) -> list[dict[str, object]]:
    attempts: list[dict[str, object]] = []
    for attempt_dir in numbered_attempt_dirs(failure_dir)[-max_attempts:]:
        patch_path = attempt_dir / "patch.diff"
        if not patch_path.is_file():
            continue

        status = read_optional_json(attempt_dir / "status.json")
        validation = read_optional_json(attempt_dir / "validation.json")
        checks = read_optional_json(attempt_dir / "checks.json")
        patch_excerpt, patch_truncated = read_limited_text(patch_path, MAX_ATTEMPT_CONTEXT_CHARS)

        attempt: dict[str, object] = {
            "attempt": attempt_dir.name,
            "status": status if isinstance(status, dict) else None,
            "validation": validation if isinstance(validation, dict) else None,
            "patch_excerpt": patch_excerpt,
            "patch_truncated": patch_truncated,
        }
        if isinstance(checks, dict):
            attempt["checks"] = checks.get("checks", [])
        attempts.append(attempt)

    return attempts


def collect_patch_context(failure_dir: Path, top_n: int = 3) -> dict[str, object]:
    analysis_path = analysis_dir(failure_dir)
    parsed = read_json(analysis_path / "parsed_failure.json")
    classification = read_json(analysis_path / "classification.json")
    localization = read_json(analysis_path / "suspected_modules.json")
    patch_plan = (analysis_path / "patch_plan.md").read_text(encoding="utf-8", errors="replace")

    rtl_files = []
    if isinstance(localization, dict):
        suspects = localization.get("suspects", [])
    else:
        suspects = []

    failure_terms = parsed_failure_terms(parsed)
    for suspect in suspects[:top_n] if isinstance(suspects, list) else []:
        if not isinstance(suspect, dict):
            continue
        path_text = str(suspect.get("file") or "")
        path = resolve_rtl_context_path(path_text)
        if not path:
            continue
        full_text = path.read_text(encoding="utf-8", errors="replace")
        text, truncated = read_limited_text(path)
        focus_terms = normalize_search_terms(
            list(suspect.get("matched_terms", [])) + failure_terms
        )
        rtl_files.append(
            {
                "path": display_path(path),
                "module": suspect.get("module"),
                "score": suspect.get("score"),
                "matched_terms": suspect.get("matched_terms", []),
                "truncated": truncated,
                "focused_snippets": build_focused_rtl_snippets(full_text, focus_terms),
                "text": text,
            }
        )

    return {
        "failure_dir": display_path(failure_dir),
        "parsed_failure": parsed,
        "failure_summary": compact_parsed_failure(parsed),
        "failure_log_excerpt": collect_log_excerpt(failure_dir, parsed),
        "classification": classification,
        "localization": localization,
        "patch_plan": patch_plan,
        "previous_attempts": collect_previous_attempts_context(failure_dir),
        "investigation": read_optional_json(analysis_path / "investigation.json"),
        "rtl_files": rtl_files,
    }


def build_patch_prompt(context: dict[str, object]) -> str:
    rtl_sections = []
    focused_sections = []
    rtl_files = context.get("rtl_files", [])
    if isinstance(rtl_files, list):
        for item in rtl_files:
            if not isinstance(item, dict):
                continue
            path = item.get("path", "unknown")
            module = item.get("module", "unknown")
            score = item.get("score", "unknown")
            truncated = " yes" if item.get("truncated") else " no"
            snippets = item.get("focused_snippets", [])
            if isinstance(snippets, list) and snippets:
                focused_sections.extend(
                    [
                        f"### {path}",
                        "",
                        f"- module: {module}",
                        f"- localization score: {score}",
                        "",
                    ]
                )
                for snippet in snippets:
                    if not isinstance(snippet, dict):
                        continue
                    focused_sections.extend(
                        [
                            f"Lines {snippet.get('start_line')}..{snippet.get('end_line')}:",
                            "",
                            "```systemverilog",
                            str(snippet.get("text") or ""),
                            "```",
                            "",
                        ]
                    )
            rtl_sections.extend(
                [
                    f"### {path}",
                    "",
                    f"- module: {module}",
                    f"- localization score: {score}",
                    f"- truncated:{truncated}",
                    "",
                    "```systemverilog",
                    str(item.get("text") or ""),
                    "```",
                    "",
                ]
            )

    if not rtl_sections:
        rtl_sections = ["No RTL context files were available.", ""]

    if not focused_sections:
        focused_sections = ["No focused snippets were available.", ""]

    log_excerpt = context.get("failure_log_excerpt", {})
    if isinstance(log_excerpt, dict) and log_excerpt.get("available"):
        log_lines = [
            f"{item.get('line')}: {item.get('text')}"
            for item in log_excerpt.get("excerpt", [])
            if isinstance(item, dict)
        ]
    else:
        log_lines = [str(log_excerpt.get("reason") or "No raw-log excerpt available.")]

    previous_attempts = context.get("previous_attempts", [])
    if not isinstance(previous_attempts, list) or not previous_attempts:
        previous_attempts = []

    return "\n".join(
        [
            "# RTL Patch Proposal Request",
            "",
            "Generate a minimal proposal patch for this captured RTL failure.",
            "",
            "## Output Rules",
            "",
            "- Output a unified diff only.",
            "- Prefer `diff --git a/path b/path` file headers and correct hunk line counts.",
            "- Patch only files under `rtl_output/*.sv`.",
            "- Do not edit testbenches, YAML, IR, generator scripts, manifests, or debug artifacts.",
            "- The RTL may contain generated-file comments; for this debug loop, edits under `rtl_output/*.sv` are explicitly allowed.",
            "- Do not change module ports unless the failure cannot be fixed otherwise.",
            "- Keep the change small, behavior-focused, and synthesizable SystemVerilog.",
            "- Account for previous attempts; do not repeat a patch that was rejected or failed checks.",
            "- Do not return `NO_PATCH` only because the captured command had return code 0; `TEST FAIL`, reported errors, and parsed testbench errors are failing evidence.",
            "- If there is not enough context for a credible RTL patch, output `NO_PATCH` followed by a brief reason.",
            "",
            "## Failure Summary",
            "",
            "```json",
            json.dumps(context.get("failure_summary"), indent=2, sort_keys=True),
            "```",
            "",
            "## Investigation Evidence and Uncertainty",
            "",
            json.dumps(context.get("investigation"), indent=2, sort_keys=True),
            "",
            "## Failure Log Excerpt",
            "",
            "```text",
            "\n".join(log_lines),
            "```",
            "",
            "## Classification",
            "",
            "```json",
            json.dumps(context.get("classification"), indent=2, sort_keys=True),
            "```",
            "",
            "## Suspected Modules",
            "",
            "```json",
            json.dumps(context.get("localization"), indent=2, sort_keys=True),
            "```",
            "",
            "## Patch Plan",
            "",
            str(context.get("patch_plan") or ""),
            "",
            "## Focused RTL Snippets",
            "",
            *focused_sections,
            "",
            "## Previous Attempts",
            "",
            "```json",
            json.dumps(previous_attempts, indent=2, sort_keys=True),
            "```",
            "",
            "## RTL Context",
            "",
            *rtl_sections,
        ]
    )


def extract_response_text(data: dict[str, object]) -> str:
    choices = data.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message", {})
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    return ""


def call_patch_llm(prompt: str, model: str, *, system_prompt: str | None = None,
                   diagnostic_path: Path | None = None) -> str:
    api_key = os.environ.get("TAMUS_AI_CHAT_API_KEY", "")
    if not api_key:
        raise RuntimeError("TAMUS_AI_CHAT_API_KEY is not set; use --offline or export it before proposing online.")

    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("The requests package is required for online patch proposals.") from exc

    response = requests.post(
        TAMU_API_URL,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 4096,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt or "You are an expert ASIC RTL debug engineer. Return only a minimal unified diff or NO_PATCH.",
                },
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
    )
    if diagnostic_path:
        write_json(diagnostic_path, {"model": model, "http_status": response.status_code})
    if response.status_code != 200:
        text = (response.text or "").strip()
        if len(text) > 800:
            text = text[:800] + "..."
        raise RuntimeError(f"Patch proposal API failed with HTTP {response.status_code}: {text}")

    data = response.json()
    choices = data.get("choices", [])
    metadata = {"model": model, "http_status": response.status_code,
                "usage": data.get("usage"), "finish_reasons": [
                    item.get("finish_reason") for item in choices if isinstance(item, dict)
                ] if isinstance(choices, list) else []}
    text = extract_response_text(data)
    metadata["content_characters"] = len(text)
    if diagnostic_path:
        write_json(diagnostic_path, metadata)
    if not text:
        raise RuntimeError(f"Model API returned no message content; finish_reasons={metadata['finish_reasons']}; "
                           f"usage={metadata['usage']}")
    return text


def extract_diff(response_text: str) -> str:
    fenced = re.search(r"```(?:diff|patch)?\s*(.*?)```", response_text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip() + "\n"

    stripped = response_text.strip()
    if stripped.startswith("diff --git") or stripped.startswith("--- ") or stripped.startswith("NO_PATCH"):
        return stripped + "\n"
    return ""


def split_hunks(file_lines: list[str]) -> list[tuple[int, int]]:
    hunks = []
    starts = [
        index for index, line in enumerate(file_lines)
        if line == "@@" or line.startswith("@@ ")
    ]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(file_lines)
        hunks.append((start, end))
    return hunks


def normalize_hunk_header(header: str, body: list[str]) -> tuple[str, str | None]:
    match = re.match(
        r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
        r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<section>.*)$",
        header,
    )
    if not match:
        return header, f"Unable to parse hunk header: {header}"

    old_count = 0
    new_count = 0
    malformed_lines = []
    for line in body:
        if not line:
            malformed_lines.append("<empty line>")
            continue
        marker = line[0]
        if marker == "\\":
            continue
        if marker not in {" ", "+", "-"}:
            malformed_lines.append(line[:80])
            continue
        if marker in {" ", "-"}:
            old_count += 1
        if marker in {" ", "+"}:
            new_count += 1

    if malformed_lines:
        return header, f"Hunk contains malformed diff body line(s): {', '.join(malformed_lines[:3])}"

    old_start = match.group("old_start")
    new_start = match.group("new_start")
    section = match.group("section") or ""
    normalized = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{section}"
    if normalized == header:
        return header, None
    return normalized, f"Normalized hunk counts: `{header}` -> `{normalized}`"


def restore_missing_hunk_locations(file_lines: list[str], old_path: str, new_path: str) -> tuple[list[str], str | None]:
    hunks = split_hunks(file_lines)
    if not any(file_lines[start] == "@@" for start, _ in hunks):
        return file_lines, None
    name = normalize_diff_path(old_path)
    path = (SCRIPT_DIR / name).resolve()
    if (name != normalize_diff_path(new_path) or not name.startswith("rtl_output/")
            or not path.is_relative_to(RTL_OUTPUT_DIR.resolve()) or not path.is_file()):
        return file_lines, "Cannot reconstruct bare hunk headers outside an existing RTL file."
    try:
        raw = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return file_lines, f"Cannot read source for bare hunk headers: {exc}"
    if not raw.endswith("\n") or "\r" in raw:
        return file_lines, "Cannot reconstruct bare hunk headers for non-LF or unterminated source files."
    source = raw.splitlines()
    repaired = []
    previous_end = 0
    for start, end in hunks:
        body = file_lines[start + 1:end]
        if any(not line or line[0] not in " +-" for line in body):
            return file_lines, "Cannot reconstruct bare hunk headers with malformed or newline-marker bodies."
        original = [line[1:] for line in body if line[0] in " -"]
        replacement = [line[1:] for line in body if line[0] in " +"]
        matches = [i for i in range(len(source) - len(original) + 1)
                   if original and source[i:i + len(original)] == original]
        if len(matches) != 1 or matches[0] < previous_end:
            return file_lines, "Cannot reconstruct bare hunk headers: source context is missing, ambiguous, or overlapping."
        position = matches[0]
        repaired.extend(source[previous_end:position])
        repaired.extend(replacement)
        previous_end = position + len(original)
    repaired.extend(source[previous_end:])
    # Generate counts and surrounding context together; header-only recovery can
    # leave a trailing-context-free hunk that Git interprets as an EOF change.
    result = list(difflib.unified_diff(source, repaired, fromfile=old_path,
                                     tofile=new_path, lineterm=""))
    return result, f"Reconstructed unified diff from unique exact source matches in {name}."


def normalize_patch_text(patch_text: str) -> tuple[str, list[str]]:
    stripped = patch_text.strip()
    if not stripped or stripped.startswith("NO_PATCH"):
        return patch_text, []

    original_has_trailing_newline = patch_text.endswith("\n")
    lines = patch_text.splitlines()
    normalized: list[str] = []
    warnings: list[str] = []
    index = 0
    pending_git_header = False

    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            if pending_git_header:
                warnings.append(f"Removed duplicate git diff header: {line}")
                index += 1
                continue
            normalized.append(line)
            pending_git_header = True
            index += 1
            continue

        if line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            old_path = diff_header_path(line)
            new_path = diff_header_path(lines[index + 1])
            if not pending_git_header and old_path != "/dev/null" and new_path != "/dev/null":
                normalized.append(f"diff --git {old_path} {new_path}")
                warnings.append(f"Added missing git diff header for {normalize_diff_path(new_path)}.")

            file_start = index
            index += 2
            while (
                index < len(lines)
                and not lines[index].startswith("diff --git ")
                and not (
                    lines[index].startswith("--- ")
                    and index + 1 < len(lines)
                    and lines[index + 1].startswith("+++ ")
                )
            ):
                index += 1

            file_lines = lines[file_start:index]
            file_lines, location_warning = restore_missing_hunk_locations(file_lines, old_path, new_path)
            if location_warning:
                warnings.append(location_warning)
            for hunk_start, hunk_end in reversed(split_hunks(file_lines)):
                header, warning = normalize_hunk_header(
                    file_lines[hunk_start],
                    file_lines[hunk_start + 1:hunk_end],
                )
                file_lines[hunk_start] = header
                if warning:
                    warnings.append(warning)
            normalized.extend(file_lines)
            pending_git_header = False
            continue

        normalized.append(line)
        index += 1

    normalized_text = "\n".join(normalized)
    if original_has_trailing_newline or normalized_text:
        normalized_text += "\n"
    return normalized_text, warnings


def normalize_patch_file(patch_path: Path) -> tuple[str, list[str], bool]:
    patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    normalized_text, warnings = normalize_patch_text(patch_text)
    changed = normalized_text != patch_text
    if changed:
        write_text(patch_path, normalized_text)
    return normalized_text, warnings, changed


def resolve_attempt_dir(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()

    if not path.is_dir():
        raise FileNotFoundError(f"Attempt directory does not exist: {path}")
    if not (path / "patch.diff").is_file():
        raise FileNotFoundError(f"Attempt directory is missing patch.diff: {path}")
    if path.parent.name != "attempts":
        raise FileNotFoundError("Attempt directory must be under an attempts/ directory.")

    failure_dir = path.parent.parent
    missing = [dirname for dirname in ("intake", "analysis", "attempts", "result") if not (failure_dir / dirname).is_dir()]
    if missing:
        raise FileNotFoundError(f"Parent failure directory is missing subdir(s): {', '.join(missing)}")

    return path


def normalize_diff_path(path_text: str) -> str:
    path = path_text.strip()
    if path == "/dev/null":
        return path
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    if path.startswith("./"):
        path = path[2:]
    return path


def diff_header_path(line: str) -> str:
    return line.split(maxsplit=1)[1].split("\t", maxsplit=1)[0].strip()


def extract_patch_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                paths.extend([parts[2], parts[3]])
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            paths.append(diff_header_path(line))
    return sorted(set(normalize_diff_path(path) for path in paths if path.strip()))


def explain_git_apply_failure(output: str) -> list[dict[str, object]]:
    details: list[dict[str, object]] = []
    if not output:
        return details

    for line in output.splitlines():
        failed_match = re.search(r"patch failed:\s*(?P<file>[^:]+):(?P<line>\d+)", line)
        if failed_match:
            details.append(
                {
                    "kind": "context_mismatch",
                    "file": normalize_diff_path(failed_match.group("file")),
                    "line": int(failed_match.group("line")),
                    "message": (
                        "Patch hunk context does not match the current RTL at this location. "
                        "The proposal may be based on stale line numbers, missing context, or an incorrect code shape."
                    ),
                    "raw": line,
                }
            )
            continue

        if "patch does not apply" in line:
            details.append(
                {
                    "kind": "context_mismatch",
                    "message": "Git found a valid diff, but at least one hunk could not be matched to the current RTL.",
                    "raw": line,
                }
            )
            continue

        if "patch with only garbage" in line:
            details.append(
                {
                    "kind": "malformed_diff",
                    "message": "Git could not parse the patch as a valid unified diff.",
                    "raw": line,
                }
            )
            continue

        if "patch fragment without header" in line:
            details.append(
                {
                    "kind": "malformed_hunk",
                    "message": "A hunk appeared without a valid file header, often caused by incorrect hunk counts.",
                    "raw": line,
                }
            )

    return details


def validate_patch_text(patch_text: str, patch_path: Path) -> dict[str, object]:
    stripped = patch_text.strip()
    if not stripped:
        return {
            "status": "patch_rejected",
            "can_apply": False,
            "issues": ["patch.diff is empty."],
            "warnings": [],
            "touched_files": [],
            "git_apply_check": None,
            "rejection_details": [],
        }

    if stripped.startswith("NO_PATCH"):
        return {
            "status": "no_patch",
            "can_apply": False,
            "issues": [],
            "warnings": ["Patch response explicitly returned NO_PATCH."],
            "touched_files": [],
            "git_apply_check": None,
            "rejection_details": [],
        }

    issues: list[str] = []
    warnings: list[str] = []
    rejection_details: list[dict[str, object]] = []
    touched_paths = extract_patch_paths(patch_text)
    touched_files = sorted(path for path in touched_paths if path != "/dev/null")

    if not touched_paths:
        issues.append("No unified diff file headers were found.")

    if "/dev/null" in touched_paths:
        issues.append("Patch creates or deletes a file; only modifications to existing RTL files are allowed.")

    for path in touched_files:
        if not path.startswith("rtl_output/") or not path.endswith(".sv"):
            issues.append(f"Patch touches non-RTL output path: {path}")
            continue
        resolved = (SCRIPT_DIR / path).resolve()
        if not resolved.is_file() or not is_under_rtl_output(resolved):
            issues.append(f"Patch target is not an existing rtl_output file: {path}")

    forbidden_headers = (
        "new file mode ",
        "deleted file mode ",
        "rename from ",
        "rename to ",
        "copy from ",
        "copy to ",
    )
    for line in patch_text.splitlines():
        if line.startswith(forbidden_headers):
            issues.append(f"Patch uses unsupported file operation: {line}")

    for line in patch_text.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        if re.search(r"\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*\s*#", line) or re.search(
            r"\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*\s*\(", line
        ):
            warnings.append("Patch appears to touch a module declaration; review for unintended port changes.")
            break

    git_apply_check = None
    git_apply_mode = None
    if not issues:
        strict_proc = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", str(patch_path)],
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        strict_output = (strict_proc.stdout or "").strip()
        git_apply_check = {
            "mode": "strict",
            "passed": strict_proc.returncode == 0,
            "returncode": strict_proc.returncode,
            "output": strict_output,
        }
        if strict_proc.returncode == 0:
            git_apply_mode = "strict"
        else:
            fallback_proc = subprocess.run(
                ["git", "apply", "--check", "-C0", "--whitespace=nowarn", str(patch_path)],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            fallback_output = (fallback_proc.stdout or "").strip()
            git_apply_check = {
                "mode": "zero_context",
                "passed": fallback_proc.returncode == 0,
                "returncode": fallback_proc.returncode,
                "output": fallback_output,
                "strict_check": {
                    "passed": False,
                    "returncode": strict_proc.returncode,
                    "output": strict_output,
                },
            }
            if fallback_proc.returncode == 0:
                git_apply_mode = "zero_context"
                warnings.append(
                    "Strict git apply check failed, but reduced-context apply check passed."
                )
            else:
                issues.append(f"git apply --check failed: {strict_output}")
                rejection_details.extend(explain_git_apply_failure(strict_output))

    return {
        "status": "patch_valid" if not issues else "patch_rejected",
        "can_apply": not issues,
        "issues": issues,
        "warnings": warnings,
        "touched_files": touched_files,
        "git_apply_check": git_apply_check,
        "git_apply_mode": git_apply_mode,
        "rejection_details": rejection_details,
    }


def validate_attempt(args: argparse.Namespace) -> int:
    attempt_dir = resolve_attempt_dir(args.attempt_dir)
    failure_dir = attempt_dir.parent.parent
    log_debug(failure_dir, f"VALIDATE PATCH | {attempt_dir.name}", section=True)
    log_debug(failure_dir, f"Validating patch attempt: {attempt_dir.name}")
    patch_path = attempt_dir / "patch.diff"
    patch_text, normalization_warnings, normalized = normalize_patch_file(patch_path)
    if normalized:
        log_debug(failure_dir, "Normalized patch diff formatting before validation")
    validation = validate_patch_text(patch_text, patch_path)
    validation["warnings"] = normalization_warnings + list(validation["warnings"])

    write_json(attempt_dir / "validation.json", validation)
    previous_attempt_status = None
    attempt_status_path = attempt_dir / "status.json"
    if attempt_status_path.is_file():
        previous_attempt_status = read_json(attempt_status_path)

    write_json(
        attempt_status_path,
        {
            "status": validation["status"],
            "phase": "validate_patch",
            "can_apply": validation["can_apply"],
            "issues": validation["issues"],
            "warnings": validation["warnings"],
            "touched_files": validation["touched_files"],
            "previous_status": previous_attempt_status,
            "rtl_modified": False,
        },
    )
    write_json(
        result_dir(failure_dir) / "status.json",
        {
            "status": validation["status"],
            "phase": "validate_patch",
            "attempt": attempt_dir.name,
            "can_apply": validation["can_apply"],
            "issues": validation["issues"],
            "warnings": validation["warnings"],
            "touched_files": validation["touched_files"],
            "rtl_modified": False,
        },
    )

    log_debug(failure_dir, f"Patch validation: {validation['status']}")
    if validation["issues"]:
        for issue in validation["issues"]:
            log_debug(failure_dir, f"Validation issue: {issue}")
    if validation["warnings"]:
        for warning in validation["warnings"]:
            log_debug(failure_dir, f"Validation warning: {warning}")
    log_debug(failure_dir, f"Attempt: {display_path(attempt_dir)}")
    log_debug(failure_dir, "RTL modified: no")
    return 0 if validation["status"] in {"patch_valid", "no_patch"} else 1


def count_verilator_diagnostics(output: str) -> tuple[int, int]:
    error_count = 0
    warning_count = 0

    for line in output.splitlines():
        text = line.strip()
        if text.startswith("%Warning-"):
            warning_count += 1
        elif text.startswith("%Error-"):
            error_count += 1
        elif text.startswith("%Error:") and not text.startswith("%Error: Exiting due to"):
            error_count += 1

    if error_count == 0:
        match = re.search(r"Exiting due to\s+(\d+)\s+error", output)
        if match:
            error_count = int(match.group(1))

    if warning_count == 0:
        match = re.search(r"Exiting due to\s+(\d+)\s+warning", output)
        if match:
            warning_count = int(match.group(1))

    return error_count, warning_count


def run_named_check(attempt_dir: Path, name: str, command: str) -> dict[str, object]:
    returncode, output, started_at, finished_at = run_command(command)
    write_text(attempt_dir / f"{name}.log", output)
    passed = returncode == 0
    error_count = None
    warning_count = None
    if name == "lint" and "verilator" in command:
        error_count, warning_count = count_verilator_diagnostics(output)
        passed = error_count == 0
    else:
        parsed = parse_failure(
            output,
            IntakeResult(
                source="bist" if name == "target" else name,
                mode="command",
                command=command,
                log_path=None,
                returncode=returncode,
                started_at=started_at,
                finished_at=finished_at,
                raw_log_bytes=len(output.encode("utf-8", errors="replace")),
            ),
        )
        if parsed_failure_detected(parsed):
            passed = False
    return {
        "name": name,
        "command": command,
        "returncode": returncode,
        "passed": passed,
        "errors": error_count,
        "warnings": warning_count,
        "started_at": started_at,
        "finished_at": finished_at,
        "log": display_path(attempt_dir / f"{name}.log"),
    }


def single_file_lint_command(path_text: str) -> str:
    return "verilator --lint-only --Wall -Wno-fatal " + shlex.quote(path_text)


def existing_rtl_file(path_text: object) -> str | None:
    if not path_text:
        return None
    resolved = resolve_rtl_path(path_text, sorted(RTL_OUTPUT_DIR.rglob("*.sv")) if RTL_OUTPUT_DIR.is_dir() else [])
    if not resolved:
        return None
    return display_path(resolved)


def first_suspect_rtl_file(failure_dir: Path) -> str | None:
    localization = read_optional_json(analysis_dir(failure_dir) / "suspected_modules.json")
    if not isinstance(localization, dict):
        return None
    suspects = localization.get("suspects")
    if not isinstance(suspects, list):
        return None
    for suspect in suspects:
        if not isinstance(suspect, dict):
            continue
        path = existing_rtl_file(suspect.get("file"))
        if path:
            return path
    return None


def verif_report_rtl_file(failure_dir: Path) -> str | None:
    parsed = read_optional_json(analysis_dir(failure_dir) / "parsed_failure.json")
    if not isinstance(parsed, dict):
        return None

    markers = parsed.get("status_markers")
    report = markers.get("verif_report") if isinstance(markers, dict) else None
    if isinstance(report, dict):
        for field in ("top_module_file",):
            path = existing_rtl_file(report.get(field))
            if path:
                return path
        top_module = str(report.get("top_module_name") or "").strip()
        if top_module:
            path = existing_rtl_file(RTL_OUTPUT_DIR / f"{top_module}.sv")
            if path:
                return path

    for error in parsed.get("flow_errors", []) if isinstance(parsed.get("flow_errors"), list) else []:
        if not isinstance(error, dict):
            continue
        path = existing_rtl_file(error.get("file"))
        if path:
            return path
        rtl_files = error.get("rtl_files")
        if isinstance(rtl_files, list):
            for rtl_file in rtl_files:
                path = existing_rtl_file(rtl_file)
                if path:
                    return path

    return None


def failure_is_standalone_verif(failure_dir: Path) -> bool:
    parsed = read_optional_json(analysis_dir(failure_dir) / "parsed_failure.json")
    classification = read_optional_json(analysis_dir(failure_dir) / "classification.json")
    if not isinstance(parsed, dict) or not isinstance(classification, dict):
        return False
    if parsed.get("source") != "verif":
        return False
    if classification.get("label") == "YAML/RTL mismatch":
        return True
    markers = parsed.get("status_markers")
    report = markers.get("verif_report") if isinstance(markers, dict) else None
    return isinstance(report, dict) and bool(report.get("top_module_name"))


def derive_lint_plan(
    failure_dir: Path,
    touched_files: list[str],
    requested_lint_command: str | None,
) -> dict[str, object]:
    if requested_lint_command and requested_lint_command != AUTO_LINT_COMMAND:
        return {
            "scope": "custom",
            "reason": "User supplied --lint-command.",
            "command": requested_lint_command,
            "requested": requested_lint_command,
        }

    if failure_is_standalone_verif(failure_dir):
        lint_file = None
        if len(touched_files) == 1:
            lint_file = existing_rtl_file(touched_files[0])
        if not lint_file:
            lint_file = verif_report_rtl_file(failure_dir)
        if not lint_file:
            lint_file = first_suspect_rtl_file(failure_dir)
        if lint_file:
            return {
                "scope": "single_file",
                "reason": "Standalone verifier report targets one RTL module.",
                "command": single_file_lint_command(lint_file),
                "requested": requested_lint_command or AUTO_LINT_COMMAND,
                "rtl_file": lint_file,
            }

    return {
        "scope": "full_rtl",
        "reason": "Integrated or ambiguous failure; using full RTL lint.",
        "command": DEFAULT_LINT_COMMAND,
        "requested": requested_lint_command or AUTO_LINT_COMMAND,
    }


def collect_file_hashes(paths: list[str]) -> dict[str, str]:
    hashes = {}
    for path_text in paths:
        path = (SCRIPT_DIR / path_text).resolve()
        hashes[path_text] = file_sha256(path)
    return hashes


def rollback_applied_patch(
    attempt_dir: Path,
    patch_path: Path,
    touched_files: list[str],
    before_hashes: dict[str, str],
) -> dict[str, object]:
    started_at = utc_now()
    proc = subprocess.run(
        ["git", "apply", "-R", "--whitespace=nowarn", str(patch_path)],
        cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    finished_at = utc_now()
    output = (proc.stdout or "").strip()
    write_text(attempt_dir / "rollback.log", output + ("\n" if output else ""))

    final_hashes = collect_file_hashes(touched_files)
    hashes_restored = all(
        final_hashes.get(path) == expected_hash
        for path, expected_hash in before_hashes.items()
    )
    return {
        "attempted": True,
        "passed": proc.returncode == 0 and hashes_restored,
        "returncode": proc.returncode,
        "output": output,
        "started_at": started_at,
        "finished_at": finished_at,
        "log": display_path(attempt_dir / "rollback.log"),
        "hashes_restored": hashes_restored,
        "final_hashes": final_hashes,
    }


def derive_apply_status(checks: list[dict[str, object]]) -> str:
    if not checks:
        return "patch_applied"

    lint = next((check for check in checks if check.get("name") == "lint"), None)
    target = next((check for check in checks if check.get("name") == "target"), None)

    if lint and lint.get("passed") is False:
        return "lint_failed"
    if target and target.get("passed") is False:
        return "target_failed"
    if lint and target:
        return "fixed"
    if lint:
        return "patch_applied_lint_passed"
    if target:
        return "patch_applied_target_passed"
    return "patch_applied"


def check_summary_line(check: dict[str, object]) -> str:
    name = str(check.get("name", "check"))
    command = str(check.get("command", ""))
    if check.get("skipped"):
        reason = check.get("reason", "unknown")
        return f"- {name}: skipped ({reason}) | `{command}`"
    result = "PASS" if check.get("passed") else "FAIL"
    log = check.get("log")
    suffix = f" | log: `{log}`" if log else ""
    return f"- {name}: {result} | `{command}`{suffix}"


def generator_backport_note(status: str) -> str:
    if status == "fixed":
        return "Candidate for later generator-flow mining if this RTL pattern recurs."
    if status == "lint_failed":
        return "Do not backport; the patch failed the lint/structural gate."
    if status == "target_failed":
        return "Do not backport yet; the original failing check still fails."
    return "Not ready for generator-flow mining until target and lint checks both pass."


def should_keep_failed_patch(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "keep_failed_patch", False)
        or getattr(args, "demo_mode", False)
    )


def build_fix_report(
    failure_dir: Path,
    attempt_dir: Path,
    apply_record: dict[str, object],
    validation: dict[str, object],
) -> str:
    parsed_path = analysis_dir(failure_dir) / "parsed_failure.json"
    classification_path = analysis_dir(failure_dir) / "classification.json"
    parsed = read_json(parsed_path) if parsed_path.is_file() else {}
    classification = read_json(classification_path) if classification_path.is_file() else {}
    if not isinstance(parsed, dict):
        parsed = {}
    if not isinstance(classification, dict):
        classification = {}

    checks = apply_record.get("checks", [])
    if not isinstance(checks, list):
        checks = []

    touched_files = apply_record.get("touched_files", [])
    if not isinstance(touched_files, list):
        touched_files = []

    warnings = validation.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []

    lines = [
        "# Fix Record",
        "",
        "## Status",
        "",
        f"- status: {apply_record.get('status')}",
        f"- attempt: {attempt_dir.name}",
        f"- classification: {classification.get('label', 'unknown')}",
        "",
        "## Original Failure",
        "",
        str(parsed.get("summary") or "No parsed failure summary available."),
        "",
        "## Touched RTL",
        "",
    ]

    if touched_files:
        lines.extend(f"- {path}" for path in touched_files)
    else:
        lines.append("- No touched RTL files recorded.")

    lines.extend([
        "",
        "## Checks",
        "",
    ])
    if checks:
        lines.extend(check_summary_line(check) for check in checks if isinstance(check, dict))
    else:
        lines.append("- No post-apply checks were recorded.")

    rollback = apply_record.get("rollback")
    if isinstance(rollback, dict):
        lines.extend([
            "",
            "## Rollback",
            "",
            f"- attempted: {rollback.get('attempted')}",
            f"- passed: {rollback.get('passed')}",
            f"- log: `{rollback.get('log')}`",
        ])

    lines.extend([
        "",
        "## Artifacts",
        "",
        f"- patch: `{display_path(attempt_dir / 'patch.diff')}`",
        f"- validation: `{display_path(attempt_dir / 'validation.json')}`",
        f"- apply record: `{display_path(attempt_dir / 'apply.json')}`",
        "",
        "## Review Notes",
        "",
        generator_backport_note(str(apply_record.get("status") or "")),
    ])

    if warnings:
        lines.extend([
            "",
            "## Validation Warnings",
            "",
            *[f"- {warning}" for warning in warnings],
        ])

    lines.append("")
    return "\n".join(lines)


def apply_attempt(args: argparse.Namespace) -> int:
    attempt_dir = resolve_attempt_dir(args.attempt_dir)
    failure_dir = attempt_dir.parent.parent
    log_debug(failure_dir, f"APPLY AND CHECK | {attempt_dir.name}", section=True)
    log_debug(failure_dir, f"Applying patch attempt: {attempt_dir.name}")
    patch_path = attempt_dir / "patch.diff"
    patch_text, normalization_warnings, normalized = normalize_patch_file(patch_path)
    if normalized:
        log_debug(failure_dir, "Normalized patch diff formatting before apply")

    previous_attempt_status = None
    attempt_status_path = attempt_dir / "status.json"
    if attempt_status_path.is_file():
        previous_attempt_status = read_json(attempt_status_path)

    validation = validate_patch_text(patch_text, patch_path)
    validation["warnings"] = normalization_warnings + list(validation["warnings"])
    write_json(attempt_dir / "validation.json", validation)

    if not validation["can_apply"]:
        status = {
            "status": "apply_rejected",
            "phase": "apply_patch",
            "reason": f"Patch validation status is {validation['status']}.",
            "validation": validation,
            "previous_status": previous_attempt_status,
            "rtl_modified": False,
        }
        write_json(attempt_status_path, status)
        write_json(
            result_dir(failure_dir) / "status.json",
            {
                "status": "apply_rejected",
                "phase": "apply_patch",
                "attempt": attempt_dir.name,
                "reason": status["reason"],
                "rtl_modified": False,
            },
        )
        log_debug(failure_dir, f"Patch apply rejected: {display_path(attempt_dir)}")
        log_debug(failure_dir, f"Reason: {status['reason']}")
        log_debug(failure_dir, "RTL modified: no")
        return 1

    touched_files = [str(path) for path in validation.get("touched_files", [])]
    log_debug(failure_dir, "Validated touched files: " + ", ".join(touched_files))
    before_hashes = collect_file_hashes(touched_files)
    apply_started_at = utc_now()
    apply_command = ["git", "apply", "--whitespace=nowarn", str(patch_path)]
    if validation.get("git_apply_mode") == "zero_context":
        apply_command = ["git", "apply", "-C0", "--whitespace=nowarn", str(patch_path)]
    log_debug(failure_dir, f"Applying unified diff with {validation.get('git_apply_mode') or 'strict'} context")
    proc = subprocess.run(
        apply_command,
        cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    apply_finished_at = utc_now()
    apply_output = (proc.stdout or "").strip()
    write_text(attempt_dir / "apply.log", apply_output + ("\n" if apply_output else ""))

    if proc.returncode != 0:
        status = {
            "status": "apply_failed",
            "phase": "apply_patch",
            "returncode": proc.returncode,
            "output": apply_output,
            "validation": validation,
            "previous_status": previous_attempt_status,
            "rtl_modified": False,
        }
        write_json(attempt_status_path, status)
        write_json(
            result_dir(failure_dir) / "status.json",
            {
                "status": "apply_failed",
                "phase": "apply_patch",
                "attempt": attempt_dir.name,
                "returncode": proc.returncode,
                "rtl_modified": False,
            },
        )
        log_debug(failure_dir, f"Patch apply failed: {display_path(attempt_dir)}")
        log_debug(failure_dir, f"ERROR: {apply_output}")
        return 1

    after_hashes = collect_file_hashes(touched_files)
    lint_plan = derive_lint_plan(
        failure_dir,
        touched_files,
        getattr(args, "lint_command", AUTO_LINT_COMMAND),
    )
    lint_command = str(lint_plan["command"])
    log_debug(
        failure_dir,
        f"Running lint check ({lint_plan['scope']}): {lint_command}",
    )
    lint_check = run_named_check(attempt_dir, "lint", lint_command)
    lint_check["scope"] = lint_plan["scope"]
    lint_check["reason"] = lint_plan["reason"]
    if lint_plan.get("rtl_file"):
        lint_check["rtl_file"] = lint_plan["rtl_file"]
    checks: list[dict[str, object]] = [lint_check]
    log_debug(failure_dir, f"Lint result: {'PASS' if checks[-1].get('passed') else 'FAIL'}")
    if args.target_command and (not checks or checks[-1].get("passed") is True):
        log_debug(failure_dir, f"Running target check: {args.target_command}")
        checks.append(run_named_check(attempt_dir, "target", args.target_command))
        log_debug(failure_dir, f"Target result: {'PASS' if checks[-1].get('passed') else 'FAIL'}")
    elif args.target_command:
        log_debug(failure_dir, "Skipping target check because lint failed")
        checks.append(
            {
                "name": "target",
                "command": args.target_command,
                "skipped": True,
                "reason": "lint_failed",
            }
        )

    final_status = derive_apply_status(checks)
    keep_failed_patch = should_keep_failed_patch(args)
    rollback = {
        "attempted": False,
        "reason": "patch_kept" if keep_failed_patch else "checks_passed",
    }
    rtl_modified = True
    if final_status not in PATCH_SUCCESS_STATUSES and not keep_failed_patch:
        log_debug(failure_dir, "Checks failed; rolling back applied patch")
        rollback = rollback_applied_patch(attempt_dir, patch_path, touched_files, before_hashes)
        rtl_modified = not bool(rollback.get("passed"))
        if not rollback.get("passed"):
            final_status = f"{final_status}_rollback_failed"

    apply_record = {
        "status": final_status,
        "phase": "post_apply_checks" if checks else "apply_patch",
        "attempt": attempt_dir.name,
        "applied_at": {
            "started_at": apply_started_at,
            "finished_at": apply_finished_at,
        },
        "touched_files": touched_files,
        "before_hashes": before_hashes,
        "after_hashes": after_hashes,
        "keep_failed_patch": keep_failed_patch,
        "rollback": rollback,
        "lint_plan": lint_plan,
        "checks": checks,
        "rtl_modified": rtl_modified,
    }
    write_json(attempt_dir / "apply.json", apply_record)
    write_json(attempt_dir / "checks.json", {"checks": checks})
    write_json(
        attempt_status_path,
        {
            **apply_record,
            "validation": validation,
            "previous_status": previous_attempt_status,
        },
    )
    write_json(result_dir(failure_dir) / "status.json", apply_record)
    write_text(result_dir(failure_dir) / "fix.md", build_fix_report(
        failure_dir,
        attempt_dir,
        apply_record,
        validation,
    ))

    log_debug(failure_dir, f"Patch application path completed: {display_path(attempt_dir)}")
    log_debug(failure_dir, f"Final status: {final_status}")
    if rollback.get("attempted"):
        result = "passed" if rollback.get("passed") else "failed"
        log_debug(failure_dir, f"Rollback: {result}")
    log_debug(failure_dir, f"RTL modified: {'yes' if rtl_modified else 'no'}")
    return 0 if final_status in PATCH_SUCCESS_STATUSES else 1


def propose_patch(args: argparse.Namespace) -> int:
    ensure_debug_layout()
    failure_dir = resolve_failure_dir(args.failure_dir)
    log_debug(failure_dir, "Starting patch proposal")
    if args.max_attempts < 1:
        log_debug(failure_dir, "ERROR: --max-attempts must be at least 1.")
        return 1
    if attempt_limit_reached(failure_dir, args.max_attempts):
        reason = f"Reached {args.max_attempts} numbered repair attempt(s)."
        record_needs_human(failure_dir, args.max_attempts, reason)
        log_debug(failure_dir, f"Repair attempt limit reached: {display_path(failure_dir)}")
        log_debug(failure_dir, f"Reason: {reason}")
        return 1

    attempt_dir = next_attempt_dir(failure_dir)
    log_debug(failure_dir, f"PATCH ATTEMPT {int(attempt_dir.name.split('_')[-1])} | PROPOSE", section=True)
    log_debug(failure_dir, f"Created patch attempt: {attempt_dir.name}")
    started_at = utc_now()
    previous_status_path = result_dir(failure_dir) / "status.json"
    previous_status = None
    if previous_status_path.is_file():
        previous_status = read_json(previous_status_path)

    try:
        log_debug(failure_dir, "Collecting patch context")
        context = collect_patch_context(failure_dir)
        prompt = build_patch_prompt(context)
        write_text(attempt_dir / "prompt.md", prompt)
        log_debug(failure_dir, f"Wrote prompt: {display_path(attempt_dir / 'prompt.md')}")

        if args.offline:
            log_debug(failure_dir, "Offline mode: writing NO_PATCH placeholder instead of calling LLM")
            response_text = "NO_PATCH\nOffline mode: prompt generated but the LLM API was not called.\n"
        else:
            log_debug(failure_dir, f"Calling patch LLM model: {args.model}")
            response_text = call_patch_llm(prompt, args.model)

        patch_text = extract_diff(response_text)
        patch_text, normalization_warnings = normalize_patch_text(patch_text)
        finished_at = utc_now()
        parsed = context.get("parsed_failure") if isinstance(context.get("parsed_failure"), dict) else {}
        classification = context.get("classification") if isinstance(context.get("classification"), dict) else {}
        write_text(attempt_dir / "response.md", response_text)
        write_text(attempt_dir / "patch.diff", patch_text)
        write_json(
            attempt_dir / "metadata.json",
            {
                "attempt": attempt_dir.name,
                "failure_dir": display_path(failure_dir),
                "model": args.model,
                "offline": args.offline,
                "started_at": started_at,
                "finished_at": finished_at,
                "rtl_modified": False,
                "context_files": [
                    item.get("path")
                    for item in context.get("rtl_files", [])
                    if isinstance(item, dict)
                ],
                "previous_attempts": [
                    item.get("attempt")
                    for item in context.get("previous_attempts", [])
                    if isinstance(item, dict)
                ],
            },
        )
        write_json(
            attempt_dir / "status.json",
            {
                "classification": classification.get("label"),
                "failure_summary": parsed.get("summary"),
                "status": "proposal_generated",
                "phase": "plan_patch",
                "patch_diff_bytes": len(patch_text.encode("utf-8")),
                "normalization_warnings": normalization_warnings,
                "rtl_modified": False,
            },
        )
        write_json(
            result_dir(failure_dir) / "status.json",
            {
                "classification": classification.get("label"),
                "failure_summary": parsed.get("summary"),
                "status": "proposal_generated",
                "phase": "plan_patch",
                "attempt": attempt_dir.name,
                "patch_diff_bytes": len(patch_text.encode("utf-8")),
                "normalization_warnings": normalization_warnings,
                "previous_status": previous_status,
                "rtl_modified": False,
            },
        )
    except Exception as exc:
        finished_at = utc_now()
        write_json(
            attempt_dir / "status.json",
            {
                "status": "proposal_failed",
                "phase": "plan_patch",
                "error": str(exc),
                "started_at": started_at,
                "finished_at": finished_at,
                "rtl_modified": False,
            },
        )
        write_json(
            result_dir(failure_dir) / "status.json",
            {
                "status": "proposal_failed",
                "phase": "plan_patch",
                "attempt": attempt_dir.name,
                "error": str(exc),
                "previous_status": previous_status,
                "rtl_modified": False,
            },
        )
        log_debug(failure_dir, f"Patch proposal failed: {display_path(attempt_dir)}")
        log_debug(failure_dir, f"ERROR: {exc}")
        return 1

    log_debug(failure_dir, f"Patch proposal artifacts: {display_path(attempt_dir)}")
    log_debug(failure_dir, f"Patch diff bytes: {len(patch_text.encode('utf-8'))}")
    for warning in normalization_warnings:
        log_debug(failure_dir, f"Patch normalization: {warning}")
    log_debug(failure_dir, "RTL modified: no")
    return 0


def current_result_status(failure_dir: Path) -> object | None:
    status_path = result_dir(failure_dir) / "status.json"
    if not status_path.is_file():
        return None
    return read_json(status_path)


def write_repair_record(
    failure_dir: Path,
    attempt_dir: Path | None,
    record: dict[str, object],
) -> None:
    write_json(result_dir(failure_dir) / "repair.json", record)
    if attempt_dir is not None:
        write_json(attempt_dir / "repair.json", record)


def repair_failure(args: argparse.Namespace) -> int:
    failure_dir = resolve_failure_dir(args.failure_dir)
    repair_started_at = utc_now()
    keep_failed_patch = should_keep_failed_patch(args)
    before_attempts = {path.name for path in numbered_attempt_dirs(failure_dir)}
    steps: list[dict[str, object]] = []
    log_debug(failure_dir, f"Starting repair flow: {display_path(failure_dir)}")
    if keep_failed_patch:
        log_debug(failure_dir, "Failed patches will be kept for this repair flow")

    propose_rc = propose_patch(args)
    steps.append({"name": "propose", "returncode": propose_rc})
    if propose_rc != 0:
        result_status = current_result_status(failure_dir)
        status = result_status.get("status") if isinstance(result_status, dict) else "repair_failed"
        write_repair_record(
            failure_dir,
            None,
            {
                "status": status,
                "phase": "repair",
                "started_at": repair_started_at,
                "finished_at": utc_now(),
                "steps": steps,
                "keep_failed_patch": keep_failed_patch,
                "demo_mode": bool(getattr(args, "demo_mode", False)),
                "result_status": result_status,
                "rtl_modified": False,
            },
        )
        log_debug(failure_dir, f"Repair flow stopped after propose: returncode={propose_rc}")
        return propose_rc

    new_attempts = [
        path for path in numbered_attempt_dirs(failure_dir)
        if path.name not in before_attempts
    ]
    if not new_attempts:
        failure_status = {
            "status": "repair_failed",
            "phase": "repair",
            "reason": "Proposal step completed but no new attempt directory was found.",
            "rtl_modified": False,
        }
        write_json(
            result_dir(failure_dir) / "status.json",
            failure_status,
        )
        write_repair_record(
            failure_dir,
            None,
            {
                **failure_status,
                "started_at": repair_started_at,
                "finished_at": utc_now(),
                "steps": steps,
                "keep_failed_patch": keep_failed_patch,
                "demo_mode": bool(getattr(args, "demo_mode", False)),
                "result_status": failure_status,
            },
        )
        log_debug(failure_dir, "Repair failed: no new attempt directory was found.")
        return 1

    attempt_dir = new_attempts[-1]
    steps[-1]["attempt"] = attempt_dir.name
    validation_rc = validate_attempt(
        argparse.Namespace(mode="validate", attempt_dir=str(attempt_dir))
    )
    steps.append({"name": "validate", "returncode": validation_rc})
    if validation_rc != 0:
        result_status = current_result_status(failure_dir)
        status = result_status.get("status") if isinstance(result_status, dict) else "repair_failed"
        write_repair_record(
            failure_dir,
            attempt_dir,
            {
                "status": status,
                "phase": "repair",
                "attempt": attempt_dir.name,
                "started_at": repair_started_at,
                "finished_at": utc_now(),
                "steps": steps,
                "keep_failed_patch": keep_failed_patch,
                "demo_mode": bool(getattr(args, "demo_mode", False)),
                "result_status": result_status,
                "rtl_modified": False,
            },
        )
        log_debug(failure_dir, f"Repair flow stopped after validate: returncode={validation_rc}")
        return validation_rc

    validation_path = attempt_dir / "validation.json"
    validation = read_json(validation_path) if validation_path.is_file() else {}
    if not isinstance(validation, dict) or not validation.get("can_apply"):
        status = validation.get("status") if isinstance(validation, dict) else "unknown"
        write_repair_record(
            failure_dir,
            attempt_dir,
            {
                "status": status,
                "phase": "repair",
                "attempt": attempt_dir.name,
                "started_at": repair_started_at,
                "finished_at": utc_now(),
                "steps": steps,
                "keep_failed_patch": keep_failed_patch,
                "demo_mode": bool(getattr(args, "demo_mode", False)),
                "result_status": current_result_status(failure_dir),
                "rtl_modified": False,
            },
        )
        log_debug(failure_dir, f"Repair stopped before apply: validation status is {status}.")
        return 1

    log_debug(failure_dir, f"Validation passed; applying attempt: {attempt_dir.name}")
    apply_rc = apply_attempt(
        argparse.Namespace(
            mode="apply",
            attempt_dir=str(attempt_dir),
            lint_command=args.lint_command,
            target_command=args.target_command,
            keep_failed_patch=keep_failed_patch,
            demo_mode=bool(getattr(args, "demo_mode", False)),
        )
    )
    steps.append({"name": "apply", "returncode": apply_rc})
    result_status = current_result_status(failure_dir)
    final_status = result_status.get("status") if isinstance(result_status, dict) else "unknown"
    write_repair_record(
        failure_dir,
        attempt_dir,
        {
            "status": final_status,
            "phase": "repair",
            "attempt": attempt_dir.name,
            "started_at": repair_started_at,
            "finished_at": utc_now(),
            "steps": steps,
            "lint_command": args.lint_command,
            "target_command": args.target_command,
            "keep_failed_patch": keep_failed_patch,
            "demo_mode": bool(getattr(args, "demo_mode", False)),
            "result_status": result_status,
            "rtl_modified": bool(
                isinstance(result_status, dict) and result_status.get("rtl_modified")
            ),
        },
    )
    log_debug(failure_dir, f"Repair flow complete: status={final_status}, returncode={apply_rc}")
    return apply_rc


def graph_checkpoint_path(failure_dir: Path) -> Path:
    return result_dir(failure_dir) / "graph_checkpoint.json"


def graph_state_path(failure_dir: Path) -> Path:
    return result_dir(failure_dir) / "graph_state.json"


def git_head_revision() -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def hash_records(records: object) -> str:
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_rtl_revision() -> dict[str, object]:
    files = []
    if RTL_OUTPUT_DIR.is_dir():
        for path in sorted(RTL_OUTPUT_DIR.rglob("*.sv")):
            files.append(
                {
                    "path": display_path(path),
                    "sha256": file_sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
    return {
        "git_head": git_head_revision(),
        "rtl_root": display_path(RTL_OUTPUT_DIR),
        "file_count": len(files),
        "files": files,
        "signature": hash_records(files),
    }


def graph_artifact_refs(failure_dir: Path) -> dict[str, object]:
    refs: dict[str, object] = {
        "debug_log": display_path(failure_dir / "debug.log"),
        "raw_log": display_path(intake_dir(failure_dir) / "raw.log"),
        "intake_metadata": display_path(intake_dir(failure_dir) / "metadata.json"),
        "rtl_state": display_path(intake_dir(failure_dir) / "rtl_state.json"),
        "parsed_failure": display_path(analysis_dir(failure_dir) / "parsed_failure.json"),
        "classification": display_path(analysis_dir(failure_dir) / "classification.json"),
        "localization": display_path(analysis_dir(failure_dir) / "suspected_modules.json"),
        "patch_plan": display_path(analysis_dir(failure_dir) / "patch_plan.md"),
        "investigation": display_path(analysis_dir(failure_dir) / "investigation.json"),
        "patch_context": display_path(result_dir(failure_dir) / "patch_context.json"),
        "status": display_path(result_dir(failure_dir) / "status.json"),
        "graph": display_path(result_dir(failure_dir) / "graph.json"),
        "graph_state": display_path(graph_state_path(failure_dir)),
        "graph_checkpoint": display_path(graph_checkpoint_path(failure_dir)),
    }
    evidence_dir = intake_dir(failure_dir) / "evidence"
    if evidence_dir.is_dir():
        refs["evidence_files"] = [
            display_path(path)
            for path in sorted(evidence_dir.iterdir())
            if path.is_file()
        ]
    return refs


def existing_attempt_refs(failure_dir: Path) -> list[dict[str, object]]:
    refs = []
    for attempt_dir in numbered_attempt_dirs(failure_dir):
        checks = read_optional_json(attempt_dir / "checks.json")
        check_results = checks.get("checks", []) if isinstance(checks, dict) else []
        refs.append(
            {
                "attempt": attempt_dir.name,
                "path": display_path(attempt_dir),
                "status": read_optional_json(attempt_dir / "status.json"),
                "metadata": read_optional_json(attempt_dir / "metadata.json"),
                "validation": read_optional_json(attempt_dir / "validation.json"),
                "apply": read_optional_json(attempt_dir / "apply.json"),
                "checks": check_results if isinstance(check_results, list) else [],
                "artifacts": {
                    "prompt": display_path(attempt_dir / "prompt.md"),
                    "response": display_path(attempt_dir / "response.md"),
                    "patch": display_path(attempt_dir / "patch.diff"),
                    "validation": display_path(attempt_dir / "validation.json"),
                    "apply": display_path(attempt_dir / "apply.json"),
                    "checks": display_path(attempt_dir / "checks.json"),
                },
            }
        )
    return refs


def graph_input_hashes(failure_dir: Path) -> dict[str, object]:
    watched = [
        intake_dir(failure_dir) / "metadata.json",
        intake_dir(failure_dir) / "raw.log",
        analysis_dir(failure_dir) / "parsed_failure.json",
        analysis_dir(failure_dir) / "classification.json",
        analysis_dir(failure_dir) / "suspected_modules.json",
        analysis_dir(failure_dir) / "patch_plan.md",
    ]
    record = read_optional_json(analysis_dir(failure_dir) / "investigation.json")
    if isinstance(record, dict):
        for item in record.get("evidence", []):
            path = (SCRIPT_DIR / item["path"]).resolve()
            if path.is_relative_to(SCRIPT_DIR) and path not in watched:
                watched.append(path)
    files = []
    for path in watched:
        if path.is_file():
            files.append(
                {
                    "path": display_path(path),
                    "sha256": file_sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
    rtl_revision = current_rtl_revision()
    return {
        "files": files,
        "files_signature": hash_records(files),
        "rtl_signature": rtl_revision["signature"],
        "signature": hash_records(
            {
                "files": files,
                "rtl_signature": rtl_revision["signature"],
            }
        ),
    }


def compare_graph_inputs(saved: object, current: dict[str, object]) -> dict[str, object]:
    if not isinstance(saved, dict):
        return {
            "stale": True,
            "reason": "No saved checkpoint input hashes were available.",
            "changed": [],
        }
    changed = []
    for key in ("files_signature", "rtl_signature", "signature"):
        if saved.get(key) != current.get(key):
            changed.append(key)
    return {
        "stale": bool(changed),
        "changed": changed,
        "saved_signature": saved.get("signature"),
        "current_signature": current.get("signature"),
    }


def graph_state_snapshot(state: DebugGraphState, failure_dir: Path) -> DebugGraphState:
    snapshot = dict(state)
    snapshot["failure_id"] = failure_dir.name
    snapshot["failure_dir"] = display_path(failure_dir)
    snapshot["artifact_refs"] = graph_artifact_refs(failure_dir)
    snapshot["evidence_refs"] = {
        key: value
        for key, value in graph_artifact_refs(failure_dir).items()
        if key in {"raw_log", "rtl_state", "evidence_files"}
    }
    snapshot["attempts"] = existing_attempt_refs(failure_dir)
    snapshot["rtl_revision"] = current_rtl_revision()
    snapshot["input_hashes"] = graph_input_hashes(failure_dir)
    localization = read_optional_json(analysis_dir(failure_dir) / "suspected_modules.json")
    if isinstance(localization, dict):
        suspects = localization.get("suspects")
        if isinstance(suspects, list):
            snapshot["suspects"] = suspects
            snapshot["inspected_files"] = [
                {
                    "path": item.get("file"),
                    "module": item.get("module"),
                    "source": "localization",
                    "score": item.get("score"),
                }
                for item in suspects
                if isinstance(item, dict)
            ]
    if state.get("investigation"):
        investigation_data = read_optional_json(analysis_dir(failure_dir) / "investigation.json")
        if isinstance(investigation_data, dict):
            snapshot["inspected_files"] = [
                {key: item[key] for key in ("path", "start_line", "end_line", "sha256")}
                for item in investigation_data.get("evidence", [])
            ]
    classification = read_optional_json(analysis_dir(failure_dir) / "classification.json")
    if isinstance(classification, dict) and not state.get("investigation"):
        snapshot["hypotheses"] = [
            {
                "kind": "repair_hypothesis",
                "classification": classification.get("label"),
                "summary": build_repair_hypothesis(FailureClassification(**classification)),
                "evidence": display_path(analysis_dir(failure_dir) / "patch_plan.md"),
            }
        ]
    checks = []
    for attempt in snapshot.get("attempts", []):
        if isinstance(attempt, dict):
            attempt_checks = attempt.get("checks")
            if isinstance(attempt_checks, list):
                checks.extend(attempt_checks)
    snapshot["check_results"] = checks
    return snapshot


def write_graph_checkpoint(failure_dir: Path, state: DebugGraphState, node: str) -> DebugGraphState:
    snapshot = graph_state_snapshot(state, failure_dir)
    checkpoint = {
        "version": GRAPH_CHECKPOINT_VERSION,
        "node": node,
        "saved_at": utc_now(),
        "failure_id": failure_dir.name,
        "input_hashes": snapshot.get("input_hashes", {}),
        "rtl_revision": snapshot.get("rtl_revision", {}),
        "state": snapshot,
    }
    snapshot["checkpoint"] = {
        key: value
        for key, value in checkpoint.items()
        if key != "state"
    }
    write_json(graph_state_path(failure_dir), snapshot)
    write_json(graph_checkpoint_path(failure_dir), checkpoint)
    return snapshot


def load_graph_checkpoint(failure_dir: Path) -> dict[str, object] | None:
    path = graph_checkpoint_path(failure_dir)
    if not path.is_file():
        return None
    data = read_json(path)
    return data if isinstance(data, dict) else None


def merge_resume_checkpoint(args: argparse.Namespace, initial_state: DebugGraphState) -> DebugGraphState:
    if not args.resume or not args.failure_dir:
        return initial_state
    failure_dir = resolve_failure_dir(args.failure_dir)
    checkpoint = load_graph_checkpoint(failure_dir)
    if not checkpoint:
        return initial_state
    saved_state = checkpoint.get("state")
    if isinstance(saved_state, dict):
        merged = dict(saved_state)
        merged.update(initial_state)
        merged["checkpoint"] = {
            key: value
            for key, value in checkpoint.items()
            if key != "state"
        }
        current_hashes = graph_input_hashes(failure_dir)
        stale = compare_graph_inputs(checkpoint.get("input_hashes"), current_hashes)
        merged["input_hashes"] = current_hashes
        merged["stale_inputs"] = stale
        return merged
    return initial_state


def graph_inputs_are_stale(state: DebugGraphState) -> bool:
    stale = state.get("stale_inputs")
    return isinstance(stale, dict) and bool(stale.get("stale"))


def graph_node_update(state: DebugGraphState, update: DebugGraphState, node: str) -> DebugGraphState:
    merged = dict(state)
    merged.update(update)
    failure_dir_text = merged.get("failure_dir")
    if not failure_dir_text:
        return update
    failure_dir = resolve_failure_dir(str(failure_dir_text))
    snapshot = write_graph_checkpoint(failure_dir, merged, node)
    return {
        key: snapshot[key]
        for key in (
            "failure_id",
            "failure_dir",
            "evidence_refs",
            "artifact_refs",
            "suspects",
            "hypotheses",
            "investigation",
            "inspected_files",
            "attempts",
            "check_results",
            "budgets",
            "rtl_revision",
            "input_hashes",
            "checkpoint",
            "stale_inputs",
            "steps",
            "status",
            "attempt_dir",
            "validation",
            "patch_context",
            "stop_reason",
            "graph_backend",
        )
        if key in snapshot
    } | update


def graph_step(
    state: DebugGraphState,
    name: str,
    returncode: int = 0,
    **extra: object,
) -> list[dict[str, object]]:
    steps = list(state.get("steps", []))
    step: dict[str, object] = {
        "name": name,
        "returncode": returncode,
        "finished_at": utc_now(),
    }
    step.update(extra)
    steps.append(step)
    return steps


def graph_failure_dir(state: DebugGraphState) -> Path:
    failure_dir_text = state.get("failure_dir")
    if not failure_dir_text:
        raise FileNotFoundError("Graph state does not contain a failure_dir.")
    return resolve_failure_dir(str(failure_dir_text))


def read_intake_record(failure_dir: Path) -> IntakeResult:
    metadata_path = intake_dir(failure_dir) / "metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"Missing intake metadata: {metadata_path}")
    metadata = read_json(metadata_path)
    if not isinstance(metadata, dict):
        raise ValueError(f"Invalid intake metadata: {metadata_path}")
    return IntakeResult(**metadata)


def read_parsed_record(failure_dir: Path) -> ParsedFailure:
    parsed_path = analysis_dir(failure_dir) / "parsed_failure.json"
    parsed = read_json(parsed_path)
    if not isinstance(parsed, dict):
        raise ValueError(f"Invalid parsed failure JSON: {parsed_path}")
    return ParsedFailure(**parsed)


def read_classification_record(failure_dir: Path) -> FailureClassification:
    classification_path = analysis_dir(failure_dir) / "classification.json"
    classification = read_json(classification_path)
    if not isinstance(classification, dict):
        raise ValueError(f"Invalid classification JSON: {classification_path}")
    return FailureClassification(**classification)


def read_localization_record(failure_dir: Path) -> RTLLocalization:
    localization_path = analysis_dir(failure_dir) / "suspected_modules.json"
    localization = read_json(localization_path)
    if not isinstance(localization, dict):
        raise ValueError(f"Invalid localization JSON: {localization_path}")
    return RTLLocalization(**localization)


def graph_run_or_ingest_node(state: DebugGraphState) -> DebugGraphState:
    existing_failure_dir = state.get("failure_dir")
    if existing_failure_dir:
        failure_dir = resolve_failure_dir(str(existing_failure_dir))
        log_debug(failure_dir, f"Graph continuing existing failure workspace: {display_path(failure_dir)}")
        intake = read_intake_record(failure_dir)
        return graph_node_update(state, {
            "failure_dir": display_path(failure_dir),
            "intake": asdict(intake),
            "steps": graph_step(state, "run_or_ingest", 0, reused=True),
        }, "run_or_ingest")

    source = state.get("source")
    command = state.get("command")
    log = state.get("log")
    if source not in SUPPORTED_SOURCES:
        raise ValueError("Graph intake requires a supported source when --failure-dir is not provided.")
    if not command and not log:
        raise ValueError("Graph intake requires --command or --log when --failure-dir is not provided.")

    failure_dir = make_failure_dir(
        str(source),
        str(state.get("name") or "") or None,
        str(command) if command else None,
        str(log) if log else None,
    )
    log_debug(failure_dir, f"Graph created failure workspace: {display_path(failure_dir)}")
    if command:
        log_debug(failure_dir, f"Graph running {source} command: {command}")
    else:
        log_debug(failure_dir, f"Graph ingesting {source} log: {log}")

    intake_args = argparse.Namespace(
        source=source,
        command=command,
        log=log,
        name=state.get("name"),
    )
    intake = capture_intake(intake_args, failure_dir)
    write_json(intake_dir(failure_dir) / "metadata.json", asdict(intake))
    write_json(intake_dir(failure_dir) / "rtl_state.json", collect_rtl_state())
    log_debug(failure_dir, f"Graph captured raw log: {display_path(intake_dir(failure_dir) / 'raw.log')}")
    return graph_node_update(state, {
        "failure_dir": display_path(failure_dir),
        "intake": asdict(intake),
        "steps": graph_step(state, "run_or_ingest", 0, created=True),
    }, "run_or_ingest")


def graph_parse_failure_node(state: DebugGraphState) -> DebugGraphState:
    failure_dir = graph_failure_dir(state)
    log_debug(failure_dir, "FAILURE ANALYSIS", section=True)
    intake_data = state.get("intake")
    intake = IntakeResult(**intake_data) if isinstance(intake_data, dict) else read_intake_record(failure_dir)
    raw_log = (intake_dir(failure_dir) / "raw.log").read_text(encoding="utf-8", errors="replace")
    parsed = parse_failure(raw_log, intake)
    write_json(analysis_dir(failure_dir) / "parsed_failure.json", asdict(parsed))
    failure_detected = parsed_failure_detected(parsed)
    log_debug(failure_dir, f"Graph parsed failure evidence: {'failure' if failure_detected else 'no failure'}")
    log_debug(failure_dir, f"Graph failure summary: {parsed.summary}")
    return graph_node_update(state, {
        "parsed_failure": asdict(parsed),
        "steps": graph_step(state, "parse_failure", 0, failure_detected=failure_detected),
    }, "parse_failure")


def graph_classify_failure_node(state: DebugGraphState) -> DebugGraphState:
    failure_dir = graph_failure_dir(state)
    parsed_data = state.get("parsed_failure")
    parsed = ParsedFailure(**parsed_data) if isinstance(parsed_data, dict) else read_parsed_record(failure_dir)
    classification = classify_failure(parsed)
    write_json(analysis_dir(failure_dir) / "classification.json", asdict(classification))
    log_debug(
        failure_dir,
        f"Graph classification: {classification.label} ({classification.confidence} confidence)",
    )
    return graph_node_update(state, {
        "classification": asdict(classification),
        "steps": graph_step(state, "classify_failure", 0, label=classification.label),
    }, "classify_failure")


def graph_localize_rtl_node(state: DebugGraphState) -> DebugGraphState:
    failure_dir = graph_failure_dir(state)
    parsed_data = state.get("parsed_failure")
    parsed = ParsedFailure(**parsed_data) if isinstance(parsed_data, dict) else read_parsed_record(failure_dir)
    raw_log = (intake_dir(failure_dir) / "raw.log").read_text(encoding="utf-8", errors="replace")
    localization = localize_rtl(raw_log, parsed)
    write_json(analysis_dir(failure_dir) / "suspected_modules.json", asdict(localization))
    log_debug(failure_dir, f"Graph localized {len(localization.suspects)} RTL suspect(s)")
    return graph_node_update(state, {
        "localization": asdict(localization),
        "suspects": localization.suspects,
        "steps": graph_step(state, "localize_rtl", 0, suspects=len(localization.suspects)),
    }, "localize_rtl")


def graph_write_patch_plan_node(state: DebugGraphState) -> DebugGraphState:
    failure_dir = graph_failure_dir(state)
    parsed = read_parsed_record(failure_dir)
    classification = read_classification_record(failure_dir)
    localization = read_localization_record(failure_dir)
    write_patch_plan(failure_dir, parsed, classification, localization)
    intake_data = state.get("intake")
    intake = IntakeResult(**intake_data) if isinstance(intake_data, dict) else read_intake_record(failure_dir)
    record_status(failure_dir, intake, parsed, classification)
    status = current_result_status(failure_dir)
    log_debug(failure_dir, "Graph wrote patch plan and phase-1 status")
    return graph_node_update(state, {
        "status": status if isinstance(status, dict) else {},
        "steps": graph_step(state, "write_patch_plan", 0),
    }, "write_patch_plan")


def route_after_patch_plan(state: DebugGraphState) -> Literal["investigate", "record_result"]:
    return "investigate" if state.get("repair") or state.get("investigate") else "record_result"


def investigation_record(state: DebugGraphState) -> dict:
    failure_dir = graph_failure_dir(state)
    parsed = read_json(analysis_dir(failure_dir) / "parsed_failure.json")
    if state.get("investigation"):
        record = read_optional_json(analysis_dir(failure_dir) / "investigation.json")
        if isinstance(record, dict):
            investigation.configure_evidence_policy(record, parsed)
            return record
    budgets = state.get("budgets", {})
    record = investigation.new_investigation(
        SCRIPT_DIR, failure_dir, state.get("suspects", []),
        parsed_failure_terms(parsed),
        int(budgets.get("investigation_steps_max", 8)),
        int(budgets.get("investigation_context_chars_max", 64000)),
        parsed=parsed,
    )
    investigation.configure_evidence_policy(record, parsed)
    return record


def save_investigation(state: DebugGraphState, record: dict, node: str) -> DebugGraphState:
    failure_dir = graph_failure_dir(state)
    path = analysis_dir(failure_dir) / "investigation.json"
    write_json(path, record)
    budgets = dict(state.get("budgets", {}))
    budgets.update(investigation_steps_used=len(record["actions"]),
                   investigation_context_chars_used=record["chars_used"],
                   investigation_assessments_used=record["assessments"],
                   investigation_assessments_max=record["max_actions"] + 1,
                   investigation_reruns_max=0, investigation_reruns_used=0)
    update = {
        "investigation": {"artifact": display_path(path), "decision": record["decision"]},
        "hypotheses": record["hypotheses"], "budgets": budgets,
        "stop_reason": record.get("stop_reason", ""),
        "steps": graph_step(state, node, 0, decision=record["decision"]),
    }
    if record.get("stop_reason"):
        status = {"status": "needs_human", "phase": "investigation",
                  "reason": record["stop_reason"], "rtl_modified": False,
                  "unresolved_questions": record["unresolved_questions"],
                  "investigation": display_path(path)}
        write_json(result_dir(failure_dir) / "status.json", status)
        update["status"] = status
    log_debug(failure_dir, f"Graph {node}: {record['decision']} "
              f"({len(record['actions'])}/{record['max_actions']} inspections; "
              f"{record['chars_used']}/{record['max_chars']} evidence characters)")
    if node == "investigate" and record["actions"]:
        action = record["actions"][-1]
        log_debug(failure_dir, f"Inspection request: {json.dumps(action['request'], sort_keys=True)}")
        added = [item for item in record["evidence"] if item["id"] in action["evidence_ids"]]
        log_debug(failure_dir, f"Inspection result: {action['status']}; "
                  f"added {sum(len(item['text']) for item in added)} characters")
        for item in added:
            log_debug(failure_dir, f"Evidence {item['id']}: {item['path']}:{item['start_line']}-{item['end_line']} "
                      f"({item['kind']}, {len(item['text'])} characters)")
        if action.get("error"):
            log_debug(failure_dir, f"Inspection error: {action['error']}")
        for search in action.get("search_results", []):
            log_debug(failure_dir, f"Search coverage: {json.dumps(search, sort_keys=True)}")
        for selection in action.get("source_selections", []):
            log_debug(failure_dir, f"Context selection: {json.dumps(selection, sort_keys=True)}")
    elif node == "assess_evidence":
        for hypothesis in record["hypotheses"]:
            log_debug(failure_dir, f"Hypothesis: {hypothesis['summary']}; uncertainty: {hypothesis['uncertainty']}")
        for question in record["unresolved_questions"]:
            log_debug(failure_dir, f"Open question: {question}")
        log_debug(failure_dir, f"Next queued actions: {json.dumps(record['pending'][:4], sort_keys=True)}")
    if record.get("stop_reason"):
        log_debug(failure_dir, f"Investigation stopped: {record['stop_reason']}; "
                  + "; ".join(record["unresolved_questions"]))
    return graph_node_update(state, update, node)


def graph_investigate_node(state: DebugGraphState) -> DebugGraphState:
    record = investigation_record(state)
    stale = graph_inputs_are_stale(state) or not investigation.evidence_unchanged(record, SCRIPT_DIR)
    if stale:
        if state.get("force_stale"):
            fresh = dict(state)
            fresh["investigation"] = {}
            record = investigation_record(fresh)
            state = dict(state)
            state["stale_inputs"] = {"stale": False, "reason": "Forced fresh investigation"}
        else:
            record.update(decision="evidence_unavailable", stop_reason="stale_inputs")
            record["unresolved_questions"] = ["Inputs changed; start a fresh investigation against the current sources."]
            return save_investigation(state, record, "investigate")
    if not record.get("stop_reason") and record["decision"] != "sufficient_evidence":
        if investigation.can_inspect(record) and record["pending"]:
            log_debug(graph_failure_dir(state),
                      f"INVESTIGATION {len(record['actions']) + 1}/{record['max_actions']}", section=True)
        investigation.inspect(record, SCRIPT_DIR)
    return save_investigation(state, record, "investigate")


def graph_assess_evidence_node(state: DebugGraphState) -> DebugGraphState:
    record = investigation_record(state)
    if record.get("stop_reason"):
        return save_investigation(state, record, "assess_evidence")
    if not investigation.evidence_unchanged(record, SCRIPT_DIR):
        record.update(decision="evidence_unavailable", stop_reason="stale_inputs")
        return save_investigation(state, record, "assess_evidence")
    if record["decision"] != "sufficient_evidence" and not state.get("offline"):
        if record["assessments"] >= record["max_actions"] + 1:
            record.update(decision="evidence_unavailable", stop_reason="assessment_budget_exhausted")
            return save_investigation(state, record, "assess_evidence")
        failure_dir = graph_failure_dir(state)
        summary = compact_parsed_failure(read_json(analysis_dir(failure_dir) / "parsed_failure.json"))
        prompt = investigation.assessment_prompt(record, summary)
        record["assessments"] += 1
        prefix = analysis_dir(failure_dir) / f"assessment_{record['assessments']:03d}"
        prefix.with_suffix(".prompt.md").write_text(prompt, encoding="utf-8")
        try:
            response = call_patch_llm(prompt, str(state.get("model", DEFAULT_PATCH_MODEL)),
                                      system_prompt="You are an RTL investigator. Return only the requested JSON evidence assessment.",
                                      diagnostic_path=prefix.with_suffix(".api.json"))
            prefix.with_suffix(".response.md").write_text(response, encoding="utf-8")
            investigation.accept_assessment(record, response)
        except Exception as exc:
            write_json(prefix.with_suffix(".error.json"), {"error_type": type(exc).__name__, "message": str(exc)})
            record.update(decision="evidence_unavailable", stop_reason="assessment_failed")
            record["unresolved_questions"] = [f"Evidence assessment failed: {exc}"]
    elif state.get("offline"):
        record["unresolved_questions"] = ["Offline inspection cannot establish a causal explanation; online evidence assessment is required."]
    if record["decision"] != "sufficient_evidence" and not record.get("stop_reason"):
        if record["decision"] == "evidence_unavailable" or not record["pending"]:
            record["stop_reason"] = "evidence_unavailable"
        elif not investigation.can_inspect(record):
            record["stop_reason"] = ("investigation_context_exhausted" if investigation.context_exhausted(record)
                                     else "investigation_steps_exhausted")
    return save_investigation(state, record, "assess_evidence")


def route_after_assessment(state: DebugGraphState) -> Literal["investigate", "build_patch_context", "record_result"]:
    if state.get("stop_reason"):
        return "record_result"
    if state.get("investigation", {}).get("decision") == "sufficient_evidence":
        return "build_patch_context" if state.get("repair") else "record_result"
    return "investigate"


def graph_build_patch_context_node(state: DebugGraphState) -> DebugGraphState:
    failure_dir = graph_failure_dir(state)
    record = investigation_record(state)
    if record["decision"] != "sufficient_evidence" or not investigation.evidence_unchanged(record, SCRIPT_DIR):
        record.update(decision="evidence_unavailable", stop_reason="evidence_not_current_or_sufficient")
        return save_investigation(state, record, "build_patch_context")
    if graph_inputs_are_stale(state) and not state.get("force_stale"):
        stale = state.get("stale_inputs", {})
        status = {
            "status": "needs_human",
            "phase": "graph_stale_inputs",
            "reason": "Saved graph checkpoint inputs differ from current failure artifacts or RTL.",
            "stale_inputs": stale,
            "rtl_modified": False,
        }
        write_json(result_dir(failure_dir) / "status.json", status)
        log_debug(failure_dir, "Graph context build stopped: checkpoint inputs are stale")
        return graph_node_update(state, {
            "status": status,
            "stop_reason": "stale_inputs",
            "steps": graph_step(state, "build_patch_context", 1, stale_inputs=stale),
        }, "build_patch_context")

    context = collect_patch_context(failure_dir)
    context_path = result_dir(failure_dir) / "patch_context.json"
    write_json(context_path, context)
    log_debug(failure_dir, f"Graph patch context: {display_path(context_path)}")
    return graph_node_update(state, {
        "patch_context": {
            "artifact": display_path(context_path),
            "rtl_files": [
                item.get("path")
                for item in context.get("rtl_files", [])
                if isinstance(item, dict)
            ],
        },
        "steps": graph_step(
            state,
            "build_patch_context",
            0,
            artifact=display_path(context_path),
        ),
    }, "build_patch_context")


def route_after_build_patch_context(state: DebugGraphState) -> Literal["propose_patch", "record_result"]:
    return "record_result" if state.get("stop_reason") else "propose_patch"


def graph_propose_patch_node(state: DebugGraphState) -> DebugGraphState:
    failure_dir = graph_failure_dir(state)
    if graph_inputs_are_stale(state) and not state.get("force_stale"):
        stale = state.get("stale_inputs", {})
        status = {
            "status": "needs_human",
            "phase": "graph_stale_inputs",
            "reason": "Saved graph checkpoint inputs differ from current failure artifacts or RTL.",
            "stale_inputs": stale,
            "rtl_modified": False,
        }
        write_json(result_dir(failure_dir) / "status.json", status)
        log_debug(failure_dir, "Graph repair stopped: checkpoint inputs are stale")
        return graph_node_update(state, {
            "status": status,
            "stop_reason": "stale_inputs",
            "steps": graph_step(state, "propose_patch", 1, stale_inputs=stale),
        }, "propose_patch")
    before_attempts = {path.name for path in numbered_attempt_dirs(failure_dir)}
    propose_args = argparse.Namespace(
        mode="propose",
        failure_dir=str(failure_dir),
        model=state.get("model", DEFAULT_PATCH_MODEL),
        offline=bool(state.get("offline", False)),
        max_attempts=int(state.get("max_attempts", MAX_REPAIR_ATTEMPTS)),
    )
    returncode = propose_patch(propose_args)
    new_attempts = [
        path for path in numbered_attempt_dirs(failure_dir)
        if path.name not in before_attempts
    ]
    attempt_dir = new_attempts[-1] if new_attempts else None
    stop_reason = "" if returncode == 0 and attempt_dir else "proposal_failed"
    return graph_node_update(state, {
        "attempt_dir": display_path(attempt_dir) if attempt_dir else None,
        "status": current_result_status(failure_dir) or {},
        "stop_reason": stop_reason,
        "steps": graph_step(
            state,
            "propose_patch",
            returncode,
            attempt=attempt_dir.name if attempt_dir else None,
        ),
    }, "propose_patch")


def route_after_propose(state: DebugGraphState) -> Literal["validate_patch", "record_result"]:
    return "validate_patch" if state.get("attempt_dir") and not state.get("stop_reason") else "record_result"


def graph_validate_patch_node(state: DebugGraphState) -> DebugGraphState:
    attempt_dir_text = state.get("attempt_dir")
    if not attempt_dir_text:
        raise FileNotFoundError("Graph validate step has no attempt_dir.")
    returncode = validate_attempt(
        argparse.Namespace(mode="validate", attempt_dir=str(SCRIPT_DIR / str(attempt_dir_text)))
    )
    attempt_dir = resolve_attempt_dir(str(SCRIPT_DIR / str(attempt_dir_text)))
    validation = read_json(attempt_dir / "validation.json") if (attempt_dir / "validation.json").is_file() else {}
    stop_reason = ""
    if returncode != 0:
        stop_reason = "validation_failed"
    elif not (isinstance(validation, dict) and validation.get("can_apply")):
        stop_reason = "no_applicable_patch"
    return graph_node_update(state, {
        "validation": validation if isinstance(validation, dict) else {},
        "status": current_result_status(attempt_dir.parent.parent) or {},
        "stop_reason": stop_reason,
        "steps": graph_step(state, "validate_patch", returncode, attempt=attempt_dir.name),
    }, "validate_patch")


def route_after_validate(state: DebugGraphState) -> Literal["apply_patch", "record_result"]:
    validation = state.get("validation", {})
    can_apply = isinstance(validation, dict) and bool(validation.get("can_apply"))
    return "apply_patch" if can_apply and not state.get("stop_reason") else "record_result"


def graph_apply_patch_node(state: DebugGraphState) -> DebugGraphState:
    record = investigation_record(state)
    if record["decision"] != "sufficient_evidence" or not investigation.evidence_unchanged(record, SCRIPT_DIR):
        record.update(decision="evidence_unavailable", stop_reason="evidence_not_current_or_sufficient")
        return save_investigation(state, record, "apply_patch")
    attempt_dir_text = state.get("attempt_dir")
    if not attempt_dir_text:
        raise FileNotFoundError("Graph apply step has no attempt_dir.")
    attempt_dir = resolve_attempt_dir(str(SCRIPT_DIR / str(attempt_dir_text)))
    if graph_inputs_are_stale(state) and not state.get("force_stale"):
        failure_dir = attempt_dir.parent.parent
        stale = state.get("stale_inputs", {})
        status = {
            "status": "needs_human",
            "phase": "graph_stale_inputs",
            "reason": "Saved graph checkpoint inputs changed before patch application.",
            "stale_inputs": stale,
            "rtl_modified": False,
        }
        write_json(result_dir(failure_dir) / "status.json", status)
        log_debug(failure_dir, "Graph apply stopped: checkpoint inputs are stale")
        return graph_node_update(state, {
            "status": status,
            "stop_reason": "stale_inputs",
            "steps": graph_step(state, "apply_patch", 1, attempt=attempt_dir.name, stale_inputs=stale),
        }, "apply_patch")
    apply_args = argparse.Namespace(
        mode="apply",
        attempt_dir=str(attempt_dir),
        lint_command=state.get("lint_command") or AUTO_LINT_COMMAND,
        target_command=state.get("target_command"),
        keep_failed_patch=bool(state.get("keep_failed_patch", False)),
        demo_mode=bool(state.get("demo_mode", False)),
    )
    returncode = apply_attempt(apply_args)
    status = current_result_status(attempt_dir.parent.parent)
    return graph_node_update(state, {
        "status": status if isinstance(status, dict) else {},
        "stop_reason": "" if returncode == 0 else "checks_failed",
        "steps": graph_step(state, "apply_patch", returncode, attempt=attempt_dir.name),
    }, "apply_patch")


def graph_record_result_node(state: DebugGraphState) -> DebugGraphState:
    failure_dir = graph_failure_dir(state)
    status = current_result_status(failure_dir)
    record = {
        "backend": state.get("graph_backend", "unknown"),
        "failure_dir": display_path(failure_dir),
        "repair_requested": bool(state.get("repair", False)),
        "investigation": state.get("investigation", {}),
        "status": status if isinstance(status, dict) else {},
        "artifact_refs": state.get("artifact_refs", {}),
        "evidence_refs": state.get("evidence_refs", {}),
        "attempts": state.get("attempts", []),
        "check_results": state.get("check_results", []),
        "budgets": state.get("budgets", {}),
        "rtl_revision": state.get("rtl_revision", {}),
        "input_hashes": state.get("input_hashes", {}),
        "stale_inputs": state.get("stale_inputs", {}),
        "steps": state.get("steps", []),
        "stop_reason": state.get("stop_reason") or None,
        "finished_at": utc_now(),
    }
    write_json(result_dir(failure_dir) / "graph.json", record)
    log_debug(failure_dir, f"Graph result recorded: {display_path(result_dir(failure_dir) / 'graph.json')}")
    return graph_node_update(state, {
        "status": status if isinstance(status, dict) else {},
        "steps": graph_step(state, "record_result", 0),
    }, "record_result")


def build_langgraph_runner() -> Callable[[DebugGraphState], DebugGraphState]:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(DebugGraphState)
    graph.add_node("run_or_ingest", graph_run_or_ingest_node)
    graph.add_node("parse_failure", graph_parse_failure_node)
    graph.add_node("classify_failure", graph_classify_failure_node)
    graph.add_node("localize_rtl", graph_localize_rtl_node)
    graph.add_node("write_patch_plan", graph_write_patch_plan_node)
    graph.add_node("investigate", graph_investigate_node)
    graph.add_node("assess_evidence", graph_assess_evidence_node)
    graph.add_node("build_patch_context", graph_build_patch_context_node)
    graph.add_node("propose_patch", graph_propose_patch_node)
    graph.add_node("validate_patch", graph_validate_patch_node)
    graph.add_node("apply_patch", graph_apply_patch_node)
    graph.add_node("record_result", graph_record_result_node)
    graph.add_edge(START, "run_or_ingest")
    graph.add_edge("run_or_ingest", "parse_failure")
    graph.add_edge("parse_failure", "classify_failure")
    graph.add_edge("classify_failure", "localize_rtl")
    graph.add_edge("localize_rtl", "write_patch_plan")
    graph.add_conditional_edges("write_patch_plan", route_after_patch_plan)
    graph.add_edge("investigate", "assess_evidence")
    graph.add_conditional_edges("assess_evidence", route_after_assessment)
    graph.add_conditional_edges("build_patch_context", route_after_build_patch_context)
    graph.add_conditional_edges("propose_patch", route_after_propose)
    graph.add_conditional_edges("validate_patch", route_after_validate)
    graph.add_edge("apply_patch", "record_result")
    graph.add_edge("record_result", END)
    compiled = graph.compile(checkpointer=MemorySaver())

    def invoke(state: DebugGraphState) -> DebugGraphState:
        thread_id = state.get("failure_dir") or state.get("name") or utc_now()
        return compiled.invoke(
            state,
            config={"configurable": {"thread_id": str(thread_id)},
                    "recursion_limit": 30 + 2 * int(state.get("budgets", {}).get("investigation_steps_max", 8))},
        )

    return invoke


def run_fallback_graph(state: DebugGraphState) -> DebugGraphState:
    current = dict(state)
    for node in (
        graph_run_or_ingest_node,
        graph_parse_failure_node,
        graph_classify_failure_node,
        graph_localize_rtl_node,
        graph_write_patch_plan_node,
    ):
        current.update(node(current))

    if route_after_patch_plan(current) == "record_result":
        current.update(graph_record_result_node(current))
        return current

    while True:
        current.update(graph_investigate_node(current))
        current.update(graph_assess_evidence_node(current))
        route = route_after_assessment(current)
        if route == "record_result":
            current.update(graph_record_result_node(current))
            return current
        if route == "build_patch_context":
            break

    current.update(graph_build_patch_context_node(current))
    if route_after_build_patch_context(current) == "record_result":
        current.update(graph_record_result_node(current))
        return current

    current.update(graph_propose_patch_node(current))
    if route_after_propose(current) == "record_result":
        current.update(graph_record_result_node(current))
        return current

    current.update(graph_validate_patch_node(current))
    if route_after_validate(current) == "record_result":
        current.update(graph_record_result_node(current))
        return current

    current.update(graph_apply_patch_node(current))
    current.update(graph_record_result_node(current))
    return current


def validate_graph_args(args: argparse.Namespace) -> str | None:
    if args.investigate and args.repair:
        return "Choose --investigate for inspection only, or --repair for investigation followed by repair."
    if not 1 <= args.investigation_steps <= 100:
        return "--investigation-steps must be between 1 and 100."
    if not 1000 <= args.investigation_context_chars <= 200000:
        return "--investigation-context-chars must be between 1000 and 200000."
    if args.failure_dir:
        return None
    if not args.source:
        return "graph requires SOURCE unless --failure-dir is provided."
    if not args.command and not args.log:
        return "graph requires --command or --log unless --failure-dir is provided."
    return None


def run_graph_flow(args: argparse.Namespace) -> int:
    error = validate_graph_args(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    initial_state: DebugGraphState = {
        "failure_dir": args.failure_dir,
        "source": args.source,
        "command": args.command,
        "log": args.log,
        "name": args.name,
        "repair": bool(args.repair),
        "investigate": bool(args.investigate),
        "resume": bool(args.resume),
        "force_stale": bool(args.force_stale),
        "model": args.model,
        "offline": bool(args.offline),
        "max_attempts": args.max_attempts,
        "lint_command": args.lint_command,
        "target_command": args.target_command,
        "keep_failed_patch": bool(args.keep_failed_patch or args.demo_mode),
        "demo_mode": bool(args.demo_mode),
        "budgets": {
            "repair_attempts_max": args.max_attempts,
            "repair_attempts_used": 0,
            "investigation_steps_max": args.investigation_steps,
            "investigation_steps_used": 0,
            "investigation_context_chars_max": args.investigation_context_chars,
        },
        "steps": [],
    }
    initial_state = merge_resume_checkpoint(args, initial_state)
    if not args.resume:
        initial_state["investigation"] = {}
        initial_state["stop_reason"] = ""
    elif initial_state.get("investigation"):
        record = investigation_record(initial_state)
        # A resumed run may reassess saved evidence, but never replenish tool budgets.
        record.pop("stop_reason", None)
        if record["decision"] == "evidence_unavailable":
            record["decision"] = "insufficient_evidence"
        write_json(analysis_dir(graph_failure_dir(initial_state)) / "investigation.json", record)
        initial_state["stop_reason"] = ""

    try:
        runner = build_langgraph_runner()
        initial_state["graph_backend"] = "langgraph"
        final_state = runner(initial_state)
    except ImportError as exc:
        if args.require_langgraph:
            print(f"error: LangGraph is not installed: {exc}", file=sys.stderr)
            return 1
        initial_state["graph_backend"] = "local_fallback"
        final_state = run_fallback_graph(initial_state)

    failure_dir_text = final_state.get("failure_dir")
    if failure_dir_text:
        failure_dir = resolve_failure_dir(str(SCRIPT_DIR / str(failure_dir_text)))
        status = current_result_status(failure_dir)
        final_status = status.get("status") if isinstance(status, dict) else "unknown"
        log_debug(failure_dir, "DEBUG RESULT", section=True)
        log_debug(failure_dir, f"Graph final status: {final_status}")
        if args.investigate and final_status == "needs_human":
            return 1
        if args.repair:
            return 0 if final_status in PATCH_SUCCESS_STATUSES else 1
    return 0


def run_auto_flow(args: argparse.Namespace) -> int:
    if not args.repair and not args.investigate:
        print("error: auto requires --repair or --investigate.", file=sys.stderr)
        return 2

    ensure_debug_layout()
    failure_dir = make_failure_dir(args.source, args.name, args.command, args.log)
    log_debug(failure_dir, f"DEBUG RUN | {args.source.upper()} | {failure_dir.name}", section=True)
    log_debug(failure_dir, f"Auto flow created failure workspace: {display_path(failure_dir)}")
    intake_args = argparse.Namespace(
        source=args.source,
        command=args.command,
        log=args.log,
        name=args.name,
    )
    try:
        intake = capture_intake(intake_args, failure_dir)
        write_json(intake_dir(failure_dir) / "metadata.json", asdict(intake))
        write_json(intake_dir(failure_dir) / "rtl_state.json", collect_rtl_state())
        log_debug(failure_dir, f"Auto flow captured raw log: {display_path(intake_dir(failure_dir) / 'raw.log')}")
    except Exception as exc:
        log_debug(failure_dir, f"Auto intake failed: {exc}")
        return 1

    graph_args = argparse.Namespace(
        mode="graph",
        source=args.source,
        failure_dir=display_path(failure_dir),
        command=None,
        log=None,
        name=args.name,
        repair=bool(args.repair),
        investigate=bool(args.investigate),
        resume=False,
        force_stale=False,
        model=args.model,
        offline=bool(args.offline),
        max_attempts=args.max_attempts,
        lint_command=args.lint_command,
        target_command=args.target_command or args.command,
        keep_failed_patch=bool(args.keep_failed_patch or args.demo_mode),
        demo_mode=bool(args.demo_mode),
        require_langgraph=bool(args.require_langgraph),
        investigation_steps=args.investigation_steps,
        investigation_context_chars=args.investigation_context_chars,
    )
    return run_graph_flow(graph_args)


def run_phase1(args: argparse.Namespace) -> int:
    ensure_debug_layout()
    failure_dir = make_failure_dir(args.source, args.name, args.command, args.log)
    log_debug(failure_dir, f"Created failure workspace: {display_path(failure_dir)}")

    try:
        if args.command:
            log_debug(failure_dir, f"Running {args.source} command: {args.command}")
        else:
            log_debug(failure_dir, f"Ingesting {args.source} log: {args.log}")
        intake = capture_intake(args, failure_dir)
        if intake.returncode is not None:
            log_debug(failure_dir, f"Command exit code: {intake.returncode}")
        log_debug(failure_dir, f"Captured raw log: {display_path(intake_dir(failure_dir) / 'raw.log')} ({intake.raw_log_bytes} bytes)")

        log_debug(failure_dir, "Parsing failure evidence")
        raw_log = (intake_dir(failure_dir) / "raw.log").read_text(encoding="utf-8", errors="replace")
        parsed_failure = parse_failure(raw_log, intake)
        failure_detected = parsed_failure_detected(parsed_failure)
        log_debug(failure_dir, f"Failure detected: {'yes' if failure_detected else 'no'}")
        if intake.returncode == 0 and failure_detected:
            log_debug(failure_dir, "Command exited 0, but parsed failure markers/errors indicate a failing run.")
        log_debug(failure_dir, f"Failure summary: {parsed_failure.summary}")
        log_debug(
            failure_dir,
            "Evidence counts: "
            + ", ".join(f"{key}={value}" for key, value in sorted(parsed_failure.counts.items())),
        )

        log_debug(failure_dir, "Classifying failure")
        classification = classify_failure(parsed_failure)
        log_debug(
            failure_dir,
            f"Classification: {classification.label} ({classification.confidence} confidence)",
        )
        for reason in classification.reasons:
            log_debug(failure_dir, f"Classification reason: {reason}")

        log_debug(failure_dir, "Localizing suspected RTL files")
        localization = localize_rtl(raw_log, parsed_failure)
        if localization.suspects:
            for suspect in localization.suspects[:3]:
                log_debug(
                    failure_dir,
                    "Suspect: "
                    f"{suspect.get('file')} module={suspect.get('module')} score={suspect.get('score')}",
                )
        else:
            log_debug(failure_dir, "No suspected RTL files identified.")

        log_debug(failure_dir, "Writing analysis artifacts")
        write_patch_plan(failure_dir, parsed_failure, classification, localization)
        write_json(intake_dir(failure_dir) / "metadata.json", asdict(intake))
        write_json(intake_dir(failure_dir) / "rtl_state.json", collect_rtl_state())
        write_json(analysis_dir(failure_dir) / "parsed_failure.json", asdict(parsed_failure))
        write_json(analysis_dir(failure_dir) / "classification.json", asdict(classification))
        write_json(analysis_dir(failure_dir) / "suspected_modules.json", asdict(localization))
        record_status(failure_dir, intake, parsed_failure, classification)
    except Exception as exc:
        write_json(
            result_dir(failure_dir) / "status.json",
            {
                "status": "intake_error",
                "phase": "intake",
                "error": str(exc),
            },
        )
        log_debug(failure_dir, f"Intake failed; partial artifacts: {display_path(failure_dir)}")
        log_debug(failure_dir, f"ERROR: {exc}")
        return 1

    status = read_json(result_dir(failure_dir) / "status.json")
    final_status = status.get("status") if isinstance(status, dict) else "unknown"
    log_debug(failure_dir, f"Status: {final_status}")
    log_debug(failure_dir, f"Patch plan: {display_path(analysis_dir(failure_dir) / 'patch_plan.md')}")
    log_debug(failure_dir, f"Status JSON: {display_path(result_dir(failure_dir) / 'status.json')}")
    log_debug(failure_dir, f"Debug log: {display_path(failure_dir / 'debug.log')}")
    return 0


def main() -> int:
    args = parse_args()
    if getattr(args, "mode", None) == "auto":
        return run_auto_flow(args)
    if getattr(args, "mode", None) == "graph":
        return run_graph_flow(args)
    if getattr(args, "mode", None) == "propose":
        return propose_patch(args)
    if getattr(args, "mode", None) == "validate":
        return validate_attempt(args)
    if getattr(args, "mode", None) == "apply":
        return apply_attempt(args)
    if getattr(args, "mode", None) == "repair":
        return repair_failure(args)
    return run_phase1(args)


if __name__ == "__main__":
    raise SystemExit(main())
