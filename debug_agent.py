#!/usr/bin/env python3
"""RTL debug agent: failure intake, analysis, and controlled patch proposals."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


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
DEFAULT_PATCH_MODEL = "protected.gpt-5.4"
TAMU_API_URL = "https://chat-api.tamu.ai/openai/chat/completions"
MAX_PATCH_CONTEXT_CHARS = 12000
MAX_ATTEMPT_CONTEXT_CHARS = 4000
MAX_REPAIR_ATTEMPTS = 3
MAX_PROMPT_FAILURE_ITEMS = 12
MAX_LOG_EXCERPT_RADIUS = 8
MAX_FOCUSED_SNIPPETS_PER_FILE = 8
FOCUSED_SNIPPET_RADIUS = 5
DEFAULT_LINT_COMMAND = (
    "verilator --lint-only --Wall -Wno-fatal --top-module ddr4_controller_top "
    "$(find rtl_output -name '*.sv' | sort)"
)
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
    if len(sys.argv) > 1 and sys.argv[1] in {"propose", "validate", "apply", "repair"}:
        mode = sys.argv[1]
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
                default=DEFAULT_LINT_COMMAND,
                help="Lint/structural check to run after applying the patch. Defaults to full-RTL Verilator lint.",
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
            default=DEFAULT_LINT_COMMAND,
            help="Lint/structural check to run after applying the patch. Defaults to full-RTL Verilator lint.",
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


def log_debug(failure_dir: Path | None, message: str) -> None:
    line = f"[DEBUG] {message}"
    print(line)
    if failure_dir is None:
        return
    log_path = failure_dir / "debug.log"
    timestamped = f"{utc_now()} {line}\n"
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

    counts = {
        "verilator_errors": sum(1 for item in verilator_diagnostics if item["severity"] == "error"),
        "verilator_warnings": sum(1 for item in verilator_diagnostics if item["severity"] == "warning"),
        "testbench_errors": len(testbench_errors),
        "assert_failures": len(assert_failures),
        "flow_errors": len(flow_errors),
    }

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


def localize_rtl(raw_log: str, parsed: ParsedFailure) -> RTLLocalization:
    index = collect_rtl_index()
    rtl_files = index["rtl_files"]
    file_text = index["file_text"]
    module_to_file = index["module_to_file"]
    file_to_modules = index["file_to_modules"]

    suspects: dict[str, dict[str, object]] = {}
    notes: list[str] = []

    for diagnostic in parsed.verilator_diagnostics:
        path = resolve_rtl_path(diagnostic.get("file"), rtl_files)
        if not path:
            continue
        modules = file_to_modules.get(path, [path.stem])
        add_suspect(
            suspects,
            path,
            100 if diagnostic.get("severity") == "error" else 40,
            "Verilator diagnostic points at this RTL file.",
            module=modules[0],
        )

    terms = extract_failure_terms(raw_log, parsed)
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
    return any(value > 0 for value in parsed.counts.values())


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


def call_patch_llm(prompt: str, model: str) -> str:
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
                    "content": "You are an expert ASIC RTL debug engineer. Return only a minimal unified diff or NO_PATCH.",
                },
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
    )
    if response.status_code != 200:
        text = (response.text or "").strip()
        if len(text) > 800:
            text = text[:800] + "..."
        raise RuntimeError(f"Patch proposal API failed with HTTP {response.status_code}: {text}")

    data = response.json()
    text = extract_response_text(data)
    if not text:
        raise RuntimeError("Patch proposal API returned no message content.")
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
        if line.startswith("@@ ")
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
    lint_command = args.lint_command or DEFAULT_LINT_COMMAND
    log_debug(failure_dir, f"Running lint check: {lint_command}")
    checks: list[dict[str, object]] = [
        run_named_check(attempt_dir, "lint", lint_command)
    ]
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
