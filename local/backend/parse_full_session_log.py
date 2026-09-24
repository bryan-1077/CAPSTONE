from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


STAGE_CONFIG = {
    "prep": {
        "dir_name": "prep_logs",
        "state_prefix": "prep",
        "validation_review_key": "rtl_prep_review",
        "directory_validation_keys": [],
        "candidate_keys": ["prep_candidate_path", "remote_prepare_dir"],
        "output_file_keys": ["generated_files"],
        "transcript_patterns": [
            re.compile(r"\brtl_prep\b", re.IGNORECASE),
            re.compile(r"\bprep\b", re.IGNORECASE),
        ],
    },
    "netlist": {
        "dir_name": "netlist_logs",
        "state_prefix": "netlist",
        "validation_review_key": "netlist_review",
        "directory_validation_keys": ["netlist_directory_resolution"],
        "candidate_keys": ["netlist_candidate_path", "remote_netlist_dir", "netlist_resolved_top"],
        "output_file_keys": ["netlist_output_files"],
        "transcript_patterns": [
            re.compile(r"\bnetlist\b", re.IGNORECASE),
        ],
    },
    "mapped": {
        "dir_name": "mapped_logs",
        "state_prefix": "mapped",
        "validation_review_key": "mapped_review",
        "directory_validation_keys": ["mapped_directory_resolution"],
        "candidate_keys": [
            "mapped_candidate_path",
            "remote_mapped_dir",
            "mapped_resolved_top",
            "mapped_top_module",
        ],
        "output_file_keys": ["mapped_output_files"],
        "transcript_patterns": [
            re.compile(r"\bmapped_netlist\b", re.IGNORECASE),
            re.compile(r"\bmapped\b", re.IGNORECASE),
        ],
    },
    "gdsii": {
        "dir_name": "GDSII_logs",
        "state_prefix": "gdsii",
        "validation_review_key": "gdsii_review",
        "directory_validation_keys": ["gdsii_directory_resolution"],
        "candidate_keys": ["gdsii_candidate_path", "remote_gdsii_dir"],
        "output_file_keys": ["gdsii_output_files"],
        "transcript_patterns": [
            re.compile(r"\bgdsii\b", re.IGNORECASE),
            re.compile(r"\bGDSII\b"),
        ],
    },
}

GLOBAL_TRANSCRIPT_PATTERN = re.compile(r"\bFlow (?:completed|stopped)\b", re.IGNORECASE)
TIMESTAMPED_TRANSCRIPT_LINE = re.compile(r"^\[[^\]]+\]\s*(?P<message>.*)$")


def split_live_transcript_and_state(text: str) -> tuple[list[str], str]:
    lines = text.splitlines()
    state_start = None
    saw_transcript = False

    for index, line in enumerate(lines):
        if line.startswith("["):
            saw_transcript = True
        if saw_transcript and line.startswith("{"):
            state_start = index
            break

    if state_start is None:
        for index, line in enumerate(lines):
            if line.startswith("{"):
                state_start = index
                break

    if state_start is None:
        return lines, ""

    return lines[:state_start], "\n".join(lines[state_start:])


def parse_flow_state(state_text: str) -> dict:
    text = state_text.strip()
    if not text:
        return {}

    candidate = text
    for _ in range(20):
        try:
            parsed = ast.literal_eval(candidate)
            return parsed if isinstance(parsed, dict) else {}
        except SyntaxError as exc:
            if not exc.lineno:
                return {}
            lines = candidate.splitlines()
            cutoff = max(exc.lineno - 1, 0)
            if cutoff >= len(lines):
                return {}
            candidate = "\n".join(lines[:cutoff]).rstrip()
            if not candidate:
                return {}
        except Exception:
            return {}

    return {}


def load_flow_state(state_file: Path | None, state_text: str) -> dict:
    if state_file and state_file.exists():
        text = state_file.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return parse_flow_state(text)

    return parse_flow_state(state_text)


def stage_matches_key(stage: str, key: str) -> bool:
    lowered = str(key).strip().lower()
    if not lowered:
        return False

    if stage == "prep":
        return (
            lowered.startswith("rtl_prep")
            or lowered.startswith("prep_")
            or "stage_routing_decision_rtl_prep" in lowered
        )

    if stage == "netlist":
        return (
            lowered.startswith("netlist")
            or "stage_routing_decision_netlist" in lowered
        ) and "mapped_netlist" not in lowered

    if stage == "mapped":
        return (
            lowered.startswith("mapped")
            or lowered.startswith("mapped_netlist")
            or "stage_routing_decision_mapped" in lowered
        )

    if stage == "gdsii":
        return (
            lowered.startswith("gdsii")
            or lowered.startswith("timing_closure")
            or "stage_routing_decision_gdsii" in lowered
        )

    return False


def _line_matches_stage(stage: str, line: str) -> bool:
    text = str(line)
    if not text.strip():
        return False
    if GLOBAL_TRANSCRIPT_PATTERN.search(text):
        return True

    if stage == "netlist" and "mapped_netlist" in text.lower():
        return False

    for pattern in STAGE_CONFIG[stage]["transcript_patterns"]:
        if pattern.search(text):
            return True
    return False


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for value in values:
        item = str(value)
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _transcript_message(line: str) -> str:
    match = TIMESTAMPED_TRANSCRIPT_LINE.match(str(line).strip())
    if match:
        return match.group("message").strip()
    return str(line).strip()


def _dedupe_transcript_lines(values: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for value in values:
        message = _transcript_message(value)
        if not message or message in seen:
            continue
        seen.add(message)
        ordered.append(str(value))
    return ordered


def collect_live_transcript(stage: str, live_lines: list[str], history: list[str]) -> str:
    transcript_lines = [line for line in live_lines if _line_matches_stage(stage, line)]
    history_lines = [line for line in history if _line_matches_stage(stage, line)]
    live_messages = {_transcript_message(line) for line in transcript_lines}
    history_lines = [
        line for line in history_lines
        if _transcript_message(line) not in live_messages
    ]

    sections = []
    if transcript_lines:
        sections.append("=== Live Transcript ===")
        sections.extend(_dedupe_transcript_lines(transcript_lines))

    if history_lines:
        if sections:
            sections.append("")
        sections.append("=== History ===")
        sections.extend(_dedupe_preserve_order(history_lines))

    if not sections:
        return "No data found for this stage.\n"

    return "\n".join(sections).rstrip() + "\n"


def _stage_output_matches(stage: str, key: str) -> bool:
    lowered = str(key).strip().lower()
    if stage == "prep":
        return any(token in lowered for token in ("prep", "rtl_bundle", "bundle", "generated_top"))
    if stage == "netlist":
        return "netlist" in lowered and "mapped_netlist" not in lowered
    if stage == "mapped":
        return "mapped" in lowered
    if stage == "gdsii":
        return "gdsii" in lowered or "GDSII" in key
    return False


def _parse_possible_json(value):
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except Exception:
                try:
                    return ast.literal_eval(stripped)
                except Exception:
                    return value
    return value


def _truncate_string(value: str, max_chars: int | None) -> str:
    if max_chars is None or len(value) <= max_chars:
        return value
    remaining = len(value) - max_chars
    return value[:max_chars] + f"\n...<truncated {remaining} chars>..."


def _truncate_value(value, max_chars: int | None):
    if max_chars is None:
        return value
    if isinstance(value, str):
        return _truncate_string(value, max_chars)
    if isinstance(value, list):
        return [_truncate_value(item, max_chars) for item in value]
    if isinstance(value, tuple):
        return [_truncate_value(item, max_chars) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _truncate_value(item, max_chars)
            for key, item in value.items()
        }
    return value


def _stage_review_key(stage: str) -> str:
    return STAGE_CONFIG[stage]["validation_review_key"]


def _stage_prefix(stage: str) -> str:
    return STAGE_CONFIG[stage]["state_prefix"]


def _collect_matching_logs(
    stage: str,
    state: dict,
    *,
    suffix_contains: tuple[str, ...] = (),
    suffix_endswith: tuple[str, ...] = (),
) -> dict:
    logs = state.get("logs", {})
    if not isinstance(logs, dict):
        return {}

    collected = {}
    for key, value in logs.items():
        if not stage_matches_key(stage, key):
            continue
        lowered = str(key).lower()
        if suffix_contains and not any(token in lowered for token in suffix_contains):
            continue
        if suffix_endswith and not any(lowered.endswith(token) for token in suffix_endswith):
            continue
        collected[str(key)] = _parse_possible_json(value)
    return collected


def collect_stage_outputs(stage: str, state: dict) -> dict:
    prefix = _stage_prefix(stage)
    outputs = state.get("outputs", {})
    validation = state.get("validation", {})
    review_payload = validation.get(_stage_review_key(stage), {})

    result = {
        "outputs": {
            key: value
            for key, value in (outputs.items() if isinstance(outputs, dict) else [])
            if _stage_output_matches(stage, key)
        },
        "candidate_paths": {
            key: state.get(key)
            for key in STAGE_CONFIG[stage]["candidate_keys"]
            if key in state
        },
        "output_files": {
            key: state.get(key)
            for key in STAGE_CONFIG[stage]["output_file_keys"]
            if key in state
        },
        "directory_resolution": {
            key: validation.get(key)
            for key in STAGE_CONFIG[stage]["directory_validation_keys"]
            if key in validation
        },
        "directory_resolution_logs": _collect_matching_logs(
            stage,
            state,
            suffix_contains=("directory_resolution",),
        ),
        "review_context_inventory": {},
        "execution_context": {},
    }

    if isinstance(review_payload, dict):
        context = review_payload.get("context", {})
        if isinstance(context, dict):
            for key in (
                "inventory",
                "artifact_status",
                "dependency_analysis",
                "detected_outputs",
                "resolved_top",
            ):
                if key in context:
                    result["review_context_inventory"][key] = context.get(key)
        execution_context = review_payload.get("execution_context", {})
        if isinstance(execution_context, dict):
            result["execution_context"] = execution_context

    if stage == "prep":
        for key in (
            "generated_files",
            "generated_top_module",
            "generated_top_file",
            "selected_top_module",
            "selected_top_file",
        ):
            if key in state:
                result.setdefault("prep_outputs", {})[key] = state.get(key)

    return {key: value for key, value in result.items() if value not in ({}, [], None)}


def collect_review_results(stage: str, state: dict) -> dict:
    prefix = _stage_prefix(stage)
    validation = state.get("validation", {})
    review_key = _stage_review_key(stage)
    review_payload = state.get(f"{prefix}_review_payload", {})
    validation_review = validation.get(review_key, {})

    result = {
        f"{prefix}_review_passed": state.get(f"{prefix}_review_passed"),
        f"{prefix}_review_errors": state.get(f"{prefix}_review_errors", []),
        f"{prefix}_review_warnings": state.get(f"{prefix}_review_warnings", []),
        f"{prefix}_review_suggested_fixes": state.get(f"{prefix}_review_suggested_fixes", []),
        f"{prefix}_review_summary": state.get(f"{prefix}_review_summary"),
        f"{prefix}_review_payload": review_payload if isinstance(review_payload, dict) else review_payload,
        "failure_class": state.get("failure_class"),
        "failure_stage_owner": state.get("failure_stage_owner"),
        "route_back_to_stage": state.get("route_back_to_stage"),
        "rtl_failure_handoff_owner": state.get("rtl_failure_handoff_owner"),
        "rtl_failure_category": state.get("rtl_failure_category"),
        "rtl_failure_triage_payload": state.get("rtl_failure_triage_payload"),
    }

    if isinstance(validation_review, dict):
        if "rtl_failure_triage" in validation_review:
            result["rtl_failure_triage"] = validation_review.get("rtl_failure_triage")
        if "local_review" in validation_review:
            result["local_review"] = validation_review.get("local_review")
        if "ai_review" in validation_review:
            result["ai_review"] = validation_review.get("ai_review")
        result["validation_review"] = validation_review

    return {key: value for key, value in result.items() if value not in (None, {}, [])}


def collect_edit_results(stage: str, state: dict) -> dict:
    prefix = _stage_prefix(stage)
    result = {}

    if stage == "prep":
        for key in (
            "prep_fix_applied_actions",
            "prep_fix_summary",
            "prep_fix_history",
            "prep_iteration_context",
            "prep_new_errors",
            "prep_persistent_errors",
            "prep_resolved_errors",
        ):
            if key in state:
                result[key] = state.get(key)
    else:
        for key in (
            f"{prefix}_edit_applied_actions",
            f"{prefix}_edit_summary",
        ):
            if key in state:
                result[key] = state.get(key)

    log_entries = _collect_matching_logs(
        stage,
        state,
        suffix_contains=("plan", "result", "summary"),
    )
    parsed_action_logs = {}
    for key, value in log_entries.items():
        if isinstance(value, dict) and any(
            field in value
            for field in ("actions", "applied_actions", "skipped_actions", "summary")
        ):
            parsed_action_logs[key] = value

    if parsed_action_logs:
        result["log_action_entries"] = parsed_action_logs

    return {key: value for key, value in result.items() if value not in (None, {}, [])}


def collect_executed_commands(stage: str, state: dict) -> dict:
    return _collect_matching_logs(stage, state, suffix_contains=("_command",))


def collect_stdout(stage: str, state: dict) -> dict:
    return _collect_matching_logs(stage, state, suffix_contains=("_stdout",))


def collect_stderr(stage: str, state: dict) -> dict:
    return _collect_matching_logs(stage, state, suffix_contains=("_stderr",))


def collect_ai_payloads(stage: str, state: dict) -> dict:
    prefix = _stage_prefix(stage)
    validation = state.get("validation", {})
    review_key = _stage_review_key(stage)
    review_payload = state.get(f"{prefix}_review_payload", {})
    validation_review = validation.get(review_key, {})

    result = {
        "log_entries": _collect_matching_logs(
            stage,
            state,
            suffix_contains=("_ai_raw_response", "_ai_request_metadata", "_ai_error"),
        )
    }

    if isinstance(review_payload, dict) and "ai_review" in review_payload:
        result["review_payload_ai"] = review_payload.get("ai_review")

    if isinstance(validation_review, dict) and "ai_review" in validation_review:
        result["validation_ai_review"] = validation_review.get("ai_review")

    if isinstance(validation_review, dict) and "local_review" in validation_review:
        result["validation_local_review"] = validation_review.get("local_review")

    return {key: value for key, value in result.items() if value not in (None, {}, [])}


def collect_validation_metadata(stage: str, state: dict) -> dict:
    validation = state.get("validation", {})
    review_key = _stage_review_key(stage)

    validation_entries = {
        key: value
        for key, value in (validation.items() if isinstance(validation, dict) else [])
        if stage_matches_key(stage, key)
    }

    result = {
        "validation_entries": validation_entries,
        "routing": {
            "failure_stage_owner": state.get("failure_stage_owner"),
            "failure_class": state.get("failure_class"),
            "route_back_to_stage": state.get("route_back_to_stage"),
            "rtl_failure_handoff_owner": state.get("rtl_failure_handoff_owner"),
            "rtl_failure_category": state.get("rtl_failure_category"),
        },
        "timing_closure_analysis": (
            validation.get("timing_closure_analysis")
            if stage == "gdsii" and isinstance(validation, dict)
            else None
        ),
        "review_context": {},
        "directory_resolution": {
            key: validation.get(key)
            for key in STAGE_CONFIG[stage]["directory_validation_keys"]
            if key in validation
        },
        "log_entries": _collect_matching_logs(
            stage,
            state,
            suffix_contains=(
                "_result",
                "_local_result",
                "_routing",
                "_rtl_failure_triage",
                "directory_resolution",
                "_iteration_context",
                "_completed",
            ),
        ),
    }

    validation_review = validation.get(review_key, {})
    if isinstance(validation_review, dict):
        context = validation_review.get("context", {})
        if isinstance(context, dict):
            result["review_context"] = context

    if stage == "prep":
        result["top_selection_and_adaptation"] = {
            key: state.get(key)
            for key in (
                "selected_top_module",
                "selected_top_file",
                "generic_interface_ports",
                "detected_patterns",
                "applicable_recipe_ids",
                "chosen_recipe_id",
                "inferred_modport_map",
                "generated_files",
                "generated_top_module",
                "generated_top_file",
                "adaptation_applied",
                "adaptation_reason",
                "adaptation_deterministic",
                "original_files_preserved",
                "ambiguity_reason",
                "top_selection_reason",
                "top_selection_candidates",
                "synthesis_top_checks",
            )
            if key in state
        }

    return {
        key: value
        for key, value in result.items()
        if value not in (None, {}, [])
    }


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json_file(path: Path, obj, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    else:
        text = json.dumps(obj, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def _format_sectioned_text(sections: dict, max_chars: int | None = None) -> str:
    if not sections:
        return "No data found for this stage.\n"

    parts = []
    for key in sorted(sections):
        value = sections[key]
        text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
        text = _truncate_string(text, max_chars)
        parts.append(f"=== {key} ===\n{text}".rstrip())
    return "\n\n".join(parts).rstrip() + "\n"


def _build_logs_readme() -> str:
    return (
        "# Parsed Session Logs\n\n"
        "Root files:\n\n"
        "- `README.md`: description of the parsed output layout.\n"
        "- `full_session.log` and `full_session.log.state.json`: full-flow transcript and state.\n"
        "- `timing_closure/`: closure transcripts, status, and attempt state files.\n\n"
        "Stage files:\n\n"
        "Each stage directory contains:\n\n"
        "- `live_execution_transcript.log`: stage-specific live transcript and history lines.\n"
        "- `stage_outputs.json`: candidate/output paths, output files, and directory-resolution metadata.\n"
        "- `review_results.json`: reviewer pass/fail data, summaries, fixes, and routing fields.\n"
        "- `edit_results.json`: edit/fix action summaries and applied action metadata.\n"
        "- `executed_commands.log`: commands issued for that stage.\n"
        "- `stderr.log`: collected stderr entries for that stage.\n"
        "- `stdout.log`: collected stdout entries for that stage.\n"
        "- `ai_payloads.json`: AI raw responses, request metadata, and AI review payloads.\n"
        "- `structured_validation_metadata.json`: validation context, routing, review context, and parsed structured log metadata.\n"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse an EDA full_session.log into stage-specific logs.")
    parser.add_argument("logfile", help="Path to full_session.log")
    parser.add_argument("--outdir", default="logs", help="Output directory for parsed logs")
    parser.add_argument("--state-file", default=None, help="Optional sidecar final-state JSON or Python repr")
    parser.add_argument("--max-chars-per-field", type=int, default=None, help="Optional truncation limit per string field")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    log_path = Path(args.logfile).expanduser().resolve()
    state_file = Path(args.state_file).expanduser().resolve() if args.state_file else None
    outdir = Path(args.outdir).expanduser()
    if not outdir.is_absolute():
        outdir = (Path.cwd() / outdir).resolve()

    if not log_path.exists():
        raise SystemExit(f"Log file not found: {log_path}")

    if outdir.exists():
        if not args.overwrite:
            raise SystemExit(f"Output directory already exists: {outdir}. Use --overwrite to replace it.")
        # Replace the generated files below, not the containing directory: the
        # original session log/state and timing-closure logs now live here too.

    outdir.mkdir(parents=True, exist_ok=True)

    text = log_path.read_text(encoding="utf-8", errors="replace")
    live_lines, state_text = split_live_transcript_and_state(text)
    state = load_flow_state(state_file, state_text)
    history = state.get("history", []) if isinstance(state.get("history", []), list) else []

    write_text_file(outdir / "README.md", _build_logs_readme())
    files_written_by_stage = {}

    for stage, config in STAGE_CONFIG.items():
        stage_dir = outdir / config["dir_name"]
        stage_dir.mkdir(parents=True, exist_ok=True)

        live_transcript = collect_live_transcript(stage, live_lines, history)
        stage_outputs = _truncate_value(collect_stage_outputs(stage, state), args.max_chars_per_field)
        review_results = _truncate_value(collect_review_results(stage, state), args.max_chars_per_field)
        edit_results = _truncate_value(collect_edit_results(stage, state), args.max_chars_per_field)
        commands = collect_executed_commands(stage, state)
        stdout_entries = collect_stdout(stage, state)
        stderr_entries = collect_stderr(stage, state)
        ai_payloads = _truncate_value(collect_ai_payloads(stage, state), args.max_chars_per_field)
        validation_metadata = _truncate_value(
            collect_validation_metadata(stage, state),
            args.max_chars_per_field,
        )
        write_text_file(stage_dir / "live_execution_transcript.log", live_transcript)
        write_json_file(stage_dir / "stage_outputs.json", stage_outputs or {}, pretty=args.pretty)
        write_json_file(stage_dir / "review_results.json", review_results or {}, pretty=args.pretty)
        write_json_file(stage_dir / "edit_results.json", edit_results or {}, pretty=args.pretty)
        write_text_file(
            stage_dir / "executed_commands.log",
            _format_sectioned_text(commands, args.max_chars_per_field),
        )
        write_text_file(
            stage_dir / "stderr.log",
            _format_sectioned_text(stderr_entries, args.max_chars_per_field),
        )
        write_text_file(
            stage_dir / "stdout.log",
            _format_sectioned_text(stdout_entries, args.max_chars_per_field),
        )
        write_json_file(stage_dir / "ai_payloads.json", ai_payloads or {}, pretty=args.pretty)
        write_json_file(
            stage_dir / "structured_validation_metadata.json",
            validation_metadata or {},
            pretty=args.pretty,
        )

        files_written_by_stage[stage] = 9

    print(f"Parsed session log written to: {outdir}")
    for stage, count in files_written_by_stage.items():
        print(f"- {STAGE_CONFIG[stage]['dir_name']}: {count} files")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
