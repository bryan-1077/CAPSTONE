from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any, Mapping

from agents.openai_prep_review import (
    AIReviewParseError,
    DEFAULT_OPENAI_MODEL,
    _build_client,
    _extract_chat_output_text,
    _extract_json_text,
    _extract_output_text,
    _extract_streamed_chat_text,
    _is_not_found_error,
    _load_rules_excerpt,
    _openai_timeout_seconds,
    _serialize_response,
)

FIX_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["remove_file", "remove_line", "replace_text", "append_text", "adapt_synthesis_top", "apply_synthesis_recipe"],
        },
        "path": {"type": "string"},
        "reason": {"type": "string"},
        "search_text": {"type": "string"},
        "replacement_text": {"type": "string"},
        "line_text": {"type": "string"},
    },
    "required": [
        "type",
        "path",
        "reason",
        "search_text",
        "replacement_text",
        "line_text",
    ],
    "additionalProperties": False,
}

FIX_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {"type": "array", "items": FIX_ACTION_SCHEMA},
        "summary": {"type": "string"},
    },
    "required": ["actions", "summary"],
    "additionalProperties": False,
}


def _ordered_unique_actions(actions: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str, str, str]] = set()
    ordered: list[dict[str, str]] = []
    for action in actions:
        key = (
            str(action.get("type", "")),
            str(action.get("path", "")),
            str(action.get("search_text", "")),
            str(action.get("replacement_text", "")),
            str(action.get("line_text", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        ordered.append(action)
    return ordered


def _extract_context_text(review_payload: Mapping[str, Any], rel_path: str) -> str:
    context = review_payload.get("context", {})
    key_files = context.get("key_files", {})
    value = key_files.get(rel_path, "")
    return str(value) if value is not None else ""


def _extract_rtl_sample_text(
    review_payload: Mapping[str, Any],
    rel_path: str,
) -> str:
    context = review_payload.get("context", {})
    for item in context.get("rtl_samples", []):
        if not isinstance(item, Mapping):
            continue
        sample_path = str(item.get("path", "")).strip()
        if sample_path == rel_path:
            return str(item.get("content", "") or "")
    return ""


def _parse_module_definitions_from_text(text: str) -> list[str]:
    if not text:
        return []

    cleaned = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"//.*", "", cleaned)

    modules: list[str] = []
    for match in re.finditer(
        r"(?m)^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b",
        cleaned,
    ):
        module_name = match.group(1).strip()
        if module_name and module_name not in modules:
            modules.append(module_name)
    return modules


def _extract_filelist_source_paths(
    review_payload: Mapping[str, Any],
) -> list[str]:
    filelist_text = _extract_context_text(review_payload, "filelist_genus.f")
    paths: list[str] = []
    for line in filelist_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("+incdir+"):
            continue
        if stripped.startswith("+"):
            continue
        normalized = stripped.replace("\\", "/").lstrip("./")
        if not normalized.endswith((".sv", ".v", ".svh", ".vh")):
            continue
        if normalized not in paths:
            paths.append(normalized)
    return paths


def _looks_verification_only_assertion_file(rel_path: str) -> bool:
    name = pathlib.Path(rel_path).name.lower()
    verification_tokens = (
        "assert",
        "assertion",
        "property",
        "check",
        "checker",
        "monitor",
        "tb",
        "testbench",
        "svunit",
    )
    return any(token in name for token in verification_tokens)


def _looks_verification_filename_cleanup_candidate(rel_path: str) -> bool:
    name = pathlib.Path(rel_path).name.lower()
    verification_tokens = (
        "assert",
        "assertion",
        "property",
        "check",
        "checker",
        "monitor",
        "testbench",
        "svunit",
    )
    return any(token in name for token in verification_tokens)


def _is_required_assertion_file(
    review_payload: Mapping[str, Any],
    rel_path: str,
) -> bool:
    context = review_payload.get("context", {})
    dependency_analysis = context.get("dependency_analysis", {})

    instantiated_modules = {
        str(item).strip()
        for item in dependency_analysis.get("instantiated_modules", [])
        if str(item).strip()
    }
    if not instantiated_modules:
        return False

    sample_text = _extract_rtl_sample_text(review_payload, rel_path)
    defined_modules = _parse_module_definitions_from_text(sample_text)
    if not defined_modules:
        return True

    return any(module_name in instantiated_modules for module_name in defined_modules)


def _normalize_action_path(path: str) -> str:
    normalized = str(path).strip().replace("\\", "/").lstrip("./")
    if not normalized:
        return ""
    match = re.match(
        r"^(build_(?:prep|netlist|mapped|gdsii)[A-Za-z0-9_.-]*)/(.+)$",
        normalized,
    )
    if match:
        return match.group(2)
    return normalized


ASSERTION_BLOCKING_ERROR_RE = re.compile(
    r"^Prepared RTL file (?P<path>.+?) contains "
    r"(?P<kind>sequence blocks|property blocks|assert property|cover property|assume property)\.$"
)

def _build_assertion_artifact_removal_actions(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    findings = _extract_assertion_blocking_files(review_payload)
    if not findings:
        return []

    actions: list[dict[str, str]] = []
    filelist_text = _extract_context_text(review_payload, "filelist_genus.f")

    for item in findings:
        rel_path = str(item.get("path", "")).strip()
        kind = str(item.get("kind", "")).strip()
        if not rel_path:
            continue
        if not _looks_verification_only_assertion_file(rel_path):
            continue
        if _is_required_assertion_file(review_payload, rel_path):
            continue

        actions.append(
            {
                "type": "remove_file",
                "path": rel_path,
                "reason": (
                    f"Remove verification-only prepared RTL file '{rel_path}' "
                    "from the synthesis bundle only after confirming it is not "
                    f"required by instantiated design dependencies and prep review reported {kind}."
                ),
                "search_text": "",
                "replacement_text": "",
                "line_text": "",
            }
        )

        filelist_line = _find_filelist_line(filelist_text, rel_path)
        if filelist_line:
            actions.append(
                {
                    "type": "remove_line",
                    "path": "filelist_genus.f",
                    "reason": (
                        f"Remove stale filelist entry for assertion-bearing file '{rel_path}' "
                        "after excluding it from the prepared synthesis bundle."
                    ),
                    "search_text": "",
                    "replacement_text": "",
                    "line_text": filelist_line,
                }
            )

    return actions


def _build_verification_filename_cleanup_actions(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    filelist_paths = _extract_filelist_source_paths(review_payload)
    if not filelist_paths:
        return []

    actions: list[dict[str, str]] = []
    filelist_text = _extract_context_text(review_payload, "filelist_genus.f")
    for rel_path in filelist_paths:
        if not _looks_verification_filename_cleanup_candidate(rel_path):
            continue
        if _is_required_assertion_file(review_payload, rel_path):
            continue

        actions.append(
            {
                "type": "remove_file",
                "path": rel_path,
                "reason": (
                    f"Remove verification-only prepared RTL file '{rel_path}' "
                    "because its filename indicates assertion/checker or testbench intent "
                    "and it is not required by instantiated design dependencies."
                ),
                "search_text": "",
                "replacement_text": "",
                "line_text": "",
            }
        )

        filelist_line = _find_filelist_line(filelist_text, rel_path)
        if filelist_line:
            actions.append(
                {
                    "type": "remove_line",
                    "path": "filelist_genus.f",
                    "reason": (
                        f"Remove verification-only filelist entry for '{rel_path}' "
                        "after excluding it from the prepared synthesis bundle."
                    ),
                    "search_text": "",
                    "replacement_text": "",
                    "line_text": filelist_line,
                }
            )

    return actions

def _extract_assertion_blocking_files(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for item in review_payload.get("errors", []):
        text = str(item).strip()
        match = ASSERTION_BLOCKING_ERROR_RE.match(text)
        if not match:
            continue

        rel_path = match.group("path").strip()
        kind = match.group("kind").strip()
        key = (rel_path, kind)
        if not rel_path or key in seen:
            continue

        seen.add(key)
        findings.append({"path": rel_path, "kind": kind})

    return findings

def _extract_top_module(review_payload: Mapping[str, Any]) -> str:
    top_text = _extract_context_text(review_payload, "top_module.txt").strip()
    if top_text:
        return top_text.splitlines()[0].strip()

    run_tcl_text = _extract_context_text(review_payload, "run_genus_mc.tcl")
    match = re.search(r'^set TOP "([^"]+)"$', run_tcl_text, re.MULTILINE)
    if match:
        return match.group(1).strip()

    return "mem_ctrl_top"


def _extract_detected_macros(review_payload: Mapping[str, Any]) -> list[str]:
    readme_text = _extract_context_text(review_payload, "README_prepared.txt")
    if not readme_text:
        return []

    match = re.search(
        r"Preprocessor macros detected in source:\n(?P<body>(?:  - .+\n)+)",
        readme_text,
    )
    if not match:
        return []

    macros: list[str] = []
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        macro = stripped[2:].strip()
        if macro and macro not in macros:
            macros.append(macro)
    return macros


def _preferred_genus_macros(macros: list[str]) -> list[str]:
    preferred: list[str] = []
    seen: set[str] = set()
    excluded = {"FPGA"}
    for macro in macros:
        normalized = macro.strip()
        if not normalized or normalized in excluded or normalized in seen:
            continue
        seen.add(normalized)
        preferred.append(normalized)
    return preferred


def _extract_fpga_only_artifacts(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    context = review_payload.get("context", {})
    dependency_analysis = context.get("dependency_analysis", {})
    raw_items = dependency_analysis.get("fpga_only_artifacts", [])
    artifacts: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if not path or path in seen:
            continue
        seen.add(path)
        artifacts.append({"path": path, "reason": reason})
    return artifacts


def _extract_synthesis_top_checks(
    review_payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    context = review_payload.get("context", {})
    synthesis_top_checks = context.get("synthesis_top_checks", {})
    return synthesis_top_checks if isinstance(synthesis_top_checks, Mapping) else {}


def _find_filelist_line(filelist_text: str, rel_path: str) -> str:
    normalized_target = rel_path.strip().replace("\\", "/").lstrip("./")
    for line in filelist_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("+incdir+"):
            continue
        normalized_line = stripped.replace("\\", "/").lstrip("./")
        if normalized_line == normalized_target:
            return line
    return ""


def _extract_non_rtl_filelist_entries(
    review_payload: Mapping[str, Any],
) -> list[str]:
    entries: list[str] = []
    seen: set[str] = set()
    prefix = "filelist_genus.f contains a non-RTL entry: "
    for item in review_payload.get("errors", []):
        text = str(item).strip()
        if not text.startswith(prefix):
            continue
        entry = text[len(prefix) :].strip()
        if not entry or entry in seen:
            continue
        seen.add(entry)
        entries.append(entry)
    return entries


def _is_filelist_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/").lstrip("./")
    return normalized == "filelist_genus.f" or normalized.endswith("/filelist_genus.f")


def _is_allowed_filelist_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return True
    if stripped.startswith("+incdir+"):
        return True
    if stripped.startswith("+"):
        return False
    return True


def _sanitize_filelist_text(filelist_text: str) -> str:
    if not filelist_text:
        return ""

    seen_entries: set[str] = set()
    output_lines: list[str] = []
    changed = False
    for line in filelist_text.splitlines():
        stripped = line.strip()
        if not _is_allowed_filelist_line(line):
            changed = True
            continue

        if not stripped or stripped.startswith("#"):
            output_lines.append(line)
            continue

        normalized = stripped.replace("\\", "/")
        if normalized in seen_entries:
            changed = True
            continue
        seen_entries.add(normalized)
        output_lines.append(line)

    if not changed:
        return filelist_text

    deduped = "\n".join(output_lines)
    if filelist_text.endswith("\n"):
        deduped += "\n"
    return deduped


def _dedupe_filelist_text(filelist_text: str) -> str:
    return _sanitize_filelist_text(filelist_text)

def _ensure_filelist_line_before_sources(
    filelist_text: str,
    line: str,
) -> str:
    normalized_target = line.strip().replace("\\", "/")
    if not normalized_target:
        return filelist_text

    lines = filelist_text.splitlines()
    existing = [line.strip().replace("\\", "/") for line in lines if line.strip()]
    if normalized_target in existing:
        return filelist_text

    insert_at = 0
    while insert_at < len(lines):
        stripped = lines[insert_at].strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("+incdir+"):
            insert_at += 1
            continue
        break

    updated_lines = [*lines[:insert_at], line, *lines[insert_at:]]
    updated = "\n".join(updated_lines)
    if filelist_text.endswith("\n"):
        updated += "\n"
    return _sanitize_filelist_text(updated)


def _sanitize_fix_actions(actions: list[dict[str, str]]) -> list[dict[str, str]]:
    sanitized: list[dict[str, str]] = []
    for raw_action in actions:
        action = dict(raw_action)
        path = _normalize_action_path(str(action.get("path", "")).strip())
        action["path"] = path
        action_type = str(action.get("type", "")).strip()
        if action_type == "remove_line" and not str(action.get("line_text", "")).strip():
            search_text = str(action.get("search_text", ""))
            if search_text:
                action["line_text"] = search_text.rstrip("\n")
        if action_type in {"adapt_synthesis_top", "apply_synthesis_recipe"}:
            sanitized.append(action)
            continue
        if not _is_filelist_path(path):
            sanitized.append(action)
            continue

        if action_type in {"replace_text", "append_text"}:
            replacement_text = str(action.get("replacement_text", ""))
            sanitized_replacement = _sanitize_filelist_text(replacement_text)
            if action_type == "append_text" and replacement_text and not sanitized_replacement:
                continue
            action["replacement_text"] = sanitized_replacement
        if action_type == "remove_line":
            line_text = str(action.get("line_text", ""))
            if line_text and not _is_allowed_filelist_line(line_text):
                continue
        sanitized.append(action)
    return sanitized


def _plan_has_required_adaptation_fields(plan: Mapping[str, Any]) -> bool:
    port_rewrites = plan.get("port_rewrites", [])
    return bool(
        str(plan.get("source_top_file", "")).strip()
        and str(plan.get("source_top_module", "")).strip()
        and str(plan.get("generated_top_file", "")).strip()
        and str(plan.get("generated_top_module", "")).strip()
        and isinstance(port_rewrites, list)
        and port_rewrites
    )


def _coerce_supported_prep_actions(
    actions: list[dict[str, str]],
    review_payload: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[str]]:
    synthesis_top_checks = _extract_synthesis_top_checks(review_payload)
    chosen_recipe_id = str(synthesis_top_checks.get("chosen_recipe_id", "")).strip()
    adaptation_inferable = bool(synthesis_top_checks.get("adaptation_inferable"))
    adaptation_plan = synthesis_top_checks.get("adaptation_plan", {})
    warnings: list[str] = []
    coerced: list[dict[str, str]] = []

    for raw_action in actions:
        action = dict(raw_action)
        action_type = str(action.get("type", "")).strip()
        if action_type != "apply_synthesis_recipe":
            coerced.append(action)
            continue

        recipe_id = str(action.get("search_text", "")).strip()
        try:
            parsed_plan = json.loads(str(action.get("replacement_text", "") or "{}"))
        except json.JSONDecodeError:
            parsed_plan = {}
        if not recipe_id and isinstance(parsed_plan, Mapping):
            recipe_id = str(parsed_plan.get("recipe_id", "")).strip()

        if recipe_id == "memory_controller_impl_bundle":
            coerced.append(action)
            continue

        if (
            recipe_id in {"", "generic_interface_top_to_impl"}
            and chosen_recipe_id == "generic_interface_top_to_impl"
            and adaptation_inferable
            and isinstance(adaptation_plan, Mapping)
            and _plan_has_required_adaptation_fields(adaptation_plan)
        ):
            coerced.append(
                {
                    **action,
                    "type": "adapt_synthesis_top",
                    "path": str(adaptation_plan.get("source_top_file", "")).strip()
                    or str(action.get("path", "")).strip(),
                    "search_text": "",
                    "replacement_text": json.dumps(
                        dict(adaptation_plan),
                        separators=(",", ":"),
                    ),
                    "line_text": "",
                }
            )
            warnings.append(
                "Converted generic-interface apply_synthesis_recipe request into deterministic adapt_synthesis_top using the approved adaptation plan."
            )
            continue

        dropped_recipe = recipe_id or "<empty>"
        warnings.append(
            f"Dropped unsupported prep recipe action '{dropped_recipe}'. Generic-interface wrapper generation is only auto-applied through deterministic adapt_synthesis_top plans, and apply_synthesis_recipe currently supports only memory_controller_impl_bundle."
        )

    return _ordered_unique_actions(_sanitize_fix_actions(coerced)), warnings


def _build_filelist_dedupe_action(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    errors_text = "\n".join(str(item) for item in review_payload.get("errors", []))
    if "filelist_genus.f contains duplicate source entries" not in errors_text:
        return []

    filelist_text = _extract_context_text(review_payload, "filelist_genus.f")
    deduped_text = _dedupe_filelist_text(filelist_text)
    if not filelist_text or deduped_text == filelist_text:
        return []

    return [
        {
            "type": "replace_text",
            "path": "filelist_genus.f",
            "reason": "Remove duplicate RTL source entries from filelist_genus.f while preserving the first occurrence order for deterministic Genus parsing.",
            "search_text": filelist_text,
            "replacement_text": deduped_text,
            "line_text": "",
        }
    ]


def _extract_testbench_like_paths(
    review_payload: Mapping[str, Any],
) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for item in review_payload.get("errors", []):
        text = str(item).strip()
        prefix = "Prepared RTL still includes testbench-like source: "
        if not text.startswith(prefix):
            continue
        rel_path = text[len(prefix) :].strip()
        if not rel_path or rel_path in seen:
            continue
        seen.add(rel_path)
        paths.append(rel_path)
    return paths


def _build_top_rewrite_action(review_payload: Mapping[str, Any]) -> list[dict[str, str]]:
    errors_text = "\n".join(str(item) for item in review_payload.get("errors", []))
    if (
        "reading top_module.txt" not in errors_text
        and "run_genus_mc.tcl does not reference the inferred top module" not in errors_text
        and "run_genus_mc.tcl does not mention the resolved top module" not in errors_text
    ):
        return []

    run_tcl_text = _extract_context_text(review_payload, "run_genus_mc.tcl")
    if not run_tcl_text:
        return []

    match = re.search(r'^set TOP "([^"]+)"\n', run_tcl_text, re.MULTILINE)
    if not match:
        return []

    replacement = (
        'set TOP_FILE [file normalize "./top_module.txt"]\n'
        'set TOP ""\n'
        'if {[file exists $TOP_FILE]} {\n'
        "    set top_fp [open $TOP_FILE r]\n"
        "    set TOP [string trim [read $top_fp]]\n"
        "    close $top_fp\n"
        "}\n"
        'if {$TOP eq ""} {\n'
        f'    set TOP "{_extract_top_module(review_payload)}"\n'
        "}\n"
    )

    return [
        {
            "type": "replace_text",
            "path": "run_genus_mc.tcl",
            "reason": "Read the intended synthesis top from top_module.txt instead of hardcoding it in the generated Genus Tcl.",
            "search_text": match.group(0),
            "replacement_text": replacement,
            "line_text": "",
        }
    ]


def _build_define_rewrite_action(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    errors_text = "\n".join(str(item) for item in review_payload.get("errors", []))
    if "does not apply any `define settings" not in errors_text:
        return []

    run_tcl_text = _extract_context_text(review_payload, "run_genus_mc.tcl")
    if not run_tcl_text or "set_db hdl_define" in run_tcl_text:
        return []

    macros = _preferred_genus_macros(_extract_detected_macros(review_payload))
    if not macros:
        return []

    anchor = 'catch {set_db hdl_track_filename_row_col true}\n\n'
    if anchor not in run_tcl_text:
        return []

    replacement = (
        anchor
        + f"set ACTIVE_DEFINES [list {' '.join(macros)}]\n"
        + 'if {[info exists ::env(GENUS_DEFINES)] && [string trim $::env(GENUS_DEFINES)] ne ""} {\n'
        + "    set ACTIVE_DEFINES [split $::env(GENUS_DEFINES)]\n"
        + "}\n"
        + 'if {[llength $ACTIVE_DEFINES] > 0} {\n'
        + "    set_db hdl_define $ACTIVE_DEFINES\n"
        + "}\n"
        + 'puts "DEFINES  = $ACTIVE_DEFINES"\n\n'
    )

    return [
        {
            "type": "replace_text",
            "path": "run_genus_mc.tcl",
            "reason": "Apply explicit HDL defines in the generated Genus Tcl for the non-FPGA Genus flow, with GENUS_DEFINES as an override hook.",
            "search_text": anchor,
            "replacement_text": replacement,
            "line_text": "",
        }
    ]


def _build_sdc_guard_action(review_payload: Mapping[str, Any]) -> list[dict[str, str]]:
    errors_text = "\n".join(str(item) for item in review_payload.get("errors", []))
    if "unconditionally creates clocks on ports" not in errors_text:
        return []

    sdc_text = _extract_context_text(review_payload, "constraints/mc_genus.sdc")
    if not sdc_text:
        return []

    search_text = (
        "create_clock -name clk -period 10.000 [get_ports clk]\n"
        "set_clock_uncertainty 0.10 [get_clocks clk]\n"
        "create_clock -name clk_t -period 10.000 [get_ports clk_t]\n"
        "set_clock_uncertainty 0.10 [get_clocks clk_t]\n\n"
        "set_false_path -from [get_ports rst_n]\n"
    )
    if search_text not in sdc_text:
        return []

    replacement = (
        "if {[llength [get_ports -quiet clk]]} {\n"
        "    create_clock -name clk -period 10.000 [get_ports clk]\n"
        "    set_clock_uncertainty 0.10 [get_clocks clk]\n"
        "}\n"
        "if {[llength [get_ports -quiet clk_t]]} {\n"
        "    create_clock -name clk_t -period 10.000 [get_ports clk_t]\n"
        "    set_clock_uncertainty 0.10 [get_clocks clk_t]\n"
        "}\n\n"
        "if {[llength [get_ports -quiet rst_n]]} {\n"
        "    set_false_path -from [get_ports rst_n]\n"
        "}\n"
    )

    return [
        {
            "type": "replace_text",
            "path": "constraints/mc_genus.sdc",
            "reason": "Guard generated clock and reset constraints with -quiet port existence checks so bring-up does not fail on top-level naming mismatches.",
            "search_text": search_text,
            "replacement_text": replacement,
            "line_text": "",
        }
    ]


def _build_generic_interface_top_actions(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    synthesis_top_checks = _extract_synthesis_top_checks(review_payload)
    if str(synthesis_top_checks.get("chosen_recipe_id", "")).strip() == "memory_controller_impl_bundle":
        return []
    selected_top_has_generic_interfaces = bool(
        synthesis_top_checks.get("selected_top_has_generic_interface_ports")
    )
    selected_top_is_testbench = bool(
        synthesis_top_checks.get("selected_top_likely_testbench_or_harness")
    )
    if not selected_top_has_generic_interfaces and not selected_top_is_testbench:
        return []
    if synthesis_top_checks.get("adaptation_inferable"):
        return []

    actions: list[dict[str, str]] = []
    selected_top = str(synthesis_top_checks.get("selected_top_module", "")).strip()
    recommended_top = str(synthesis_top_checks.get("recommended_top_override", "")).strip()
    top_module_text = _extract_context_text(review_payload, "top_module.txt")

    if (
        recommended_top
        and recommended_top != selected_top
        and synthesis_top_checks.get("auto_apply_top_override")
        and top_module_text.strip()
    ):
        replacement = top_module_text
        if top_module_text.strip() == selected_top:
            replacement = top_module_text.replace(selected_top, recommended_top, 1)
        actions.append(
            {
                "type": "replace_text",
                "path": "top_module.txt",
                "reason": (
                    f"Switch the selected synthesis top from '{selected_top}' to the clearer "
                    f"RTL top candidate '{recommended_top}' because the current top "
                    + (
                        "looks like a testbench or simulation harness."
                        if selected_top_is_testbench
                        else "exposes raw generic interface ports."
                    )
                ),
                "search_text": top_module_text,
                "replacement_text": replacement,
                "line_text": "",
            }
        )

    recommended_text = (
        f"Recommended synthesis top override: {recommended_top}."
        if recommended_top
        else "No clearly safe synthesis wrapper candidate was auto-selected."
    )
    issue_text = (
        "Prep review detected that the selected top looks like a testbench or simulation harness and is not suitable for direct Genus synthesis."
        if selected_top_is_testbench
        else "Prep review detected raw generic interface ports on the selected top, which are not suitable for direct Genus elaboration."
    )
    actions.append(
        {
            "type": "append_text",
            "path": "README_prepared.txt",
            "reason": "Document that the currently selected synthesis top is not suitable for direct Genus use and requires a top override or wrapper decision rather than RTL surgery.",
            "search_text": "",
            "replacement_text": (
                "\nSynthesis top suitability note:\n"
                f"- Selected top: {selected_top or 'unknown'}\n"
                f"- {issue_text}\n"
                f"- {recommended_text}\n"
                "- No automatic RTL/interface flattening was performed.\n"
            ),
            "line_text": "",
        }
    )
    return actions


def _build_synthesis_recipe_actions(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    synthesis_top_checks = _extract_synthesis_top_checks(review_payload)
    chosen_recipe_id = str(synthesis_top_checks.get("chosen_recipe_id", "")).strip()
    plan = synthesis_top_checks.get("adaptation_plan", {})
    if chosen_recipe_id != "memory_controller_impl_bundle" or not isinstance(plan, Mapping):
        return []

    selected_top_file = str(synthesis_top_checks.get("selected_top_file", "")).strip()
    path = selected_top_file or str(plan.get("source_top_file", "")).strip() or "README_prepared.txt"
    reason = str(synthesis_top_checks.get("adaptation_reason", "")).strip() or (
        f"Apply approved synthesis adaptation recipe '{chosen_recipe_id}' in the prepared build."
    )
    return [
        {
            "type": "apply_synthesis_recipe",
            "path": path,
            "reason": reason,
            "search_text": "",
            "replacement_text": json.dumps(dict(plan), separators=(",", ":")),
            "line_text": "",
        }
    ]


def _build_synthesis_top_adaptation_actions(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    synthesis_top_checks = _extract_synthesis_top_checks(review_payload)
    if str(synthesis_top_checks.get("chosen_recipe_id", "")).strip() == "memory_controller_impl_bundle":
        return []
    if not bool(synthesis_top_checks.get("selected_top_has_generic_interface_ports")):
        return []
    if not bool(synthesis_top_checks.get("adaptation_inferable")):
        return []

    plan = synthesis_top_checks.get("adaptation_plan", {})
    if not isinstance(plan, Mapping):
        return []

    source_top_file = str(plan.get("source_top_file", "")).strip()
    source_top_module = str(plan.get("source_top_module", "")).strip()
    generated_top_file = str(plan.get("generated_top_file", "")).strip()
    generated_top_module = str(plan.get("generated_top_module", "")).strip()
    port_rewrites = plan.get("port_rewrites", [])
    inferred_modport_map = synthesis_top_checks.get("inferred_modport_map", {})
    if (
        not source_top_file
        or not source_top_module
        or not generated_top_file
        or not generated_top_module
        or not isinstance(port_rewrites, list)
        or not port_rewrites
    ):
        return []

    plan_payload = {
        "source_top_file": source_top_file,
        "source_top_module": source_top_module,
        "generated_top_file": generated_top_file,
        "generated_top_module": generated_top_module,
        "port_rewrites": port_rewrites,
        "metadata_updates": list(plan.get("metadata_updates", [])),
        "inferred_modport_map": inferred_modport_map if isinstance(inferred_modport_map, Mapping) else {},
        "adaptation_reason": str(synthesis_top_checks.get("adaptation_reason", "")).strip(),
        "selected_top_file": str(synthesis_top_checks.get("selected_top_file", "")).strip(),
    }

    return [
        {
            "type": "adapt_synthesis_top",
            "path": source_top_file,
            "reason": (
                f"Generate synthesis-only adapted top '{generated_top_module}' from "
                f"'{source_top_module}' by rewriting only the raw interface top ports "
                "to deterministically inferred typed modports."
            ),
            "search_text": "",
            "replacement_text": json.dumps(plan_payload, separators=(",", ":")),
            "line_text": "",
        }
    ]


def _build_fpga_only_cleanup_actions(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    artifacts = _extract_fpga_only_artifacts(review_payload)
    if not artifacts:
        return []

    actions: list[dict[str, str]] = []
    filelist_text = _extract_context_text(review_payload, "filelist_genus.f")
    for artifact in artifacts:
        rel_path = artifact["path"]
        reason = artifact["reason"] or "is FPGA-only in the Cadence flow"
        actions.append(
            {
                "type": "remove_file",
                "path": rel_path,
                "reason": f"Remove FPGA-only prepared RTL artifact '{rel_path}' because it {reason}.",
                "search_text": "",
                "replacement_text": "",
                "line_text": "",
            }
        )
        filelist_line = _find_filelist_line(filelist_text, rel_path)
        if filelist_line:
            actions.append(
                {
                    "type": "remove_line",
                    "path": "filelist_genus.f",
                    "reason": f"Remove FPGA-only filelist entry for '{rel_path}' from the non-FPGA Cadence flow.",
                    "search_text": "",
                    "replacement_text": "",
                    "line_text": filelist_line,
                }
            )
    return actions


def _build_testbench_cleanup_actions(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    paths = _extract_testbench_like_paths(review_payload)
    if not paths:
        return []

    actions: list[dict[str, str]] = []
    filelist_text = _extract_context_text(review_payload, "filelist_genus.f")
    for rel_path in paths:
        actions.append(
            {
                "type": "remove_file",
                "path": rel_path,
                "reason": f"Remove stray testbench-like prepared RTL file '{rel_path}' from the synthesis bundle.",
                "search_text": "",
                "replacement_text": "",
                "line_text": "",
            }
        )
        filelist_line = _find_filelist_line(filelist_text, rel_path)
        if filelist_line:
            actions.append(
                {
                    "type": "remove_line",
                    "path": "filelist_genus.f",
                    "reason": f"Remove stray testbench-like filelist entry for '{rel_path}'.",
                    "search_text": "",
                    "replacement_text": "",
                    "line_text": filelist_line,
                }
            )
    return actions


def _build_non_rtl_filelist_cleanup_actions(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    entries = _extract_non_rtl_filelist_entries(review_payload)
    if not entries:
        return []

    actions: list[dict[str, str]] = []
    filelist_text = _extract_context_text(review_payload, "filelist_genus.f")
    for entry in entries:
        filelist_line = _find_filelist_line(filelist_text, entry)
        if not filelist_line:
            normalized_entry = entry.strip().replace("\\", "/")
            for line in filelist_text.splitlines():
                if line.strip().replace("\\", "/") == normalized_entry:
                    filelist_line = line
                    break
        if not filelist_line:
            continue
        actions.append(
            {
                "type": "remove_line",
                "path": "filelist_genus.f",
                "reason": f"Remove non-RTL filelist entry '{entry}' so filelist_genus.f remains a path-only compile list for the Cadence prep flow.",
                "search_text": "",
                "replacement_text": "",
                "line_text": filelist_line,
            }
        )
    return actions

def _build_definitions_pkg_filelist_actions(
    review_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    errors_text = "\n".join(str(item) for item in review_payload.get("errors", []))
    if "Definitions.pkg" not in errors_text or "filelist_genus.f" not in errors_text:
        return []

    filelist_text = _extract_context_text(review_payload, "filelist_genus.f")
    if not filelist_text:
        return []

    updated_filelist = _ensure_filelist_line_before_sources(
        filelist_text,
        "+incdir+./generated",
    )
    for bad_entry in ("./rtl/Definitions.pkg", "rtl/Definitions.pkg"):
        filelist_line = _find_filelist_line(updated_filelist, bad_entry)
        if not filelist_line:
            continue
        if filelist_line + "\n" in updated_filelist:
            updated_filelist = updated_filelist.replace(filelist_line + "\n", "", 1)
        else:
            updated_filelist = updated_filelist.replace(filelist_line, "", 1)
        updated_filelist = _sanitize_filelist_text(updated_filelist)

    if updated_filelist == filelist_text:
        return []

    return [
        {
            "type": "replace_text",
            "path": "filelist_genus.f",
            "reason": "Remove standalone Definitions.pkg from filelist_genus.f and rely on +incdir search paths instead; also add +incdir+./generated for generated-source include resolution.",
            "search_text": filelist_text,
            "replacement_text": updated_filelist,
            "line_text": "",
        }
    ]


def _build_deterministic_prep_edit_plan(
    review_payload: Mapping[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    actions = _ordered_unique_actions(
        [
            *_build_filelist_dedupe_action(review_payload),
            *_build_non_rtl_filelist_cleanup_actions(review_payload),
            *_build_definitions_pkg_filelist_actions(review_payload),
            *_build_verification_filename_cleanup_actions(review_payload),
            *_build_assertion_artifact_removal_actions(review_payload),
            *_build_testbench_cleanup_actions(review_payload),
            *_build_fpga_only_cleanup_actions(review_payload),
            *_build_synthesis_recipe_actions(review_payload),
            *_build_synthesis_top_adaptation_actions(review_payload),
            *_build_generic_interface_top_actions(review_payload),
            *_build_top_rewrite_action(review_payload),
            *_build_define_rewrite_action(review_payload),
            *_build_sdc_guard_action(review_payload),
        ]
    )
    summary = (
        "Deterministic prep edits will deduplicate filelist_genus.f, remove verification-only assertion/checker and other stray verification/testbench-like files when they are not required by instantiated design dependencies, remove FPGA-only bundle artifacts for the non-FPGA Cadence flow, "
        "apply approved synthesis adaptation recipes or generate a synthesis-only adapted top when generic-interface mapping is deterministic, update the generated Genus Tcl to read top_module.txt, apply explicit defines, "
        "and harden the generated SDC clock/reset guards where needed."
        if actions
        else "No deterministic prep edits matched the current review findings."
    )
    return {
        "enabled": True,
        "available": True,
        "model": model,
        "actions": actions,
        "summary": summary,
    }


def _merge_edit_plans(
    deterministic_plan: Mapping[str, Any],
    ai_plan: Mapping[str, Any],
) -> dict[str, Any]:
    summary = " ".join(
        part
        for part in [
            str(deterministic_plan.get("summary", "")).strip(),
            str(ai_plan.get("summary", "")).strip(),
        ]
        if part
    ).strip()
    return {
        **ai_plan,
        "actions": _ordered_unique_actions(
            _sanitize_fix_actions(
                [
                    *[
                        dict(item)
                        for item in deterministic_plan.get("actions", [])
                        if isinstance(item, Mapping)
                    ],
                    *[
                        dict(item)
                        for item in ai_plan.get("actions", [])
                        if isinstance(item, Mapping)
                    ],
                ]
            )
        ),
        "summary": summary or "Prep edit planning completed.",
    }


def _normalize_fix_plan(result: Mapping[str, Any], *, model: str) -> dict[str, Any]:
    actions: list[dict[str, str]] = []
    for item in result.get("actions", []):
        if not isinstance(item, Mapping):
            continue
        action_type = str(item.get("type", "")).strip()
        path = str(item.get("path", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if not action_type or not path or not reason:
            continue
        actions.append(
            {
                "type": action_type,
                "path": path,
                "reason": reason,
                "search_text": str(item.get("search_text", "")),
                "replacement_text": str(item.get("replacement_text", "")),
                "line_text": str(item.get("line_text", "")),
            }
        )

    return {
        "enabled": True,
        "available": True,
        "model": model,
        "actions": _ordered_unique_actions(_sanitize_fix_actions(actions)),
        "summary": str(result.get("summary", "")).strip() or "AI prep edit planning completed.",
    }


def _call_fix_responses_api(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    fix_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(fix_payload)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "rtl_prep_edit_plan",
                "strict": True,
                "schema": FIX_PLAN_SCHEMA,
            }
        },
    )
    raw_response = _serialize_response(response)
    extracted_text = _extract_output_text(response)
    try:
        parsed = json.loads(_extract_json_text(extracted_text))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AIReviewParseError(
            str(exc),
            raw_response=raw_response,
            extracted_text=extracted_text,
        ) from exc
    return _normalize_fix_plan(parsed, model=model), raw_response


def _call_fix_chat_api(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    fix_payload: Mapping[str, Any],
    plain_json: bool,
) -> tuple[dict[str, Any], str]:
    if plain_json:
        system_content = (
            f"{system_prompt} "
            "Return only a single JSON object matching this schema: "
            f"{json.dumps(FIX_PLAN_SCHEMA)} "
            "Do not wrap the JSON in markdown fences."
        )
        kwargs: dict[str, Any] = {}
    else:
        system_content = system_prompt
        kwargs = {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "rtl_prep_edit_plan",
                    "strict": True,
                    "schema": FIX_PLAN_SCHEMA,
                },
            }
        }

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": json.dumps(fix_payload)},
        ],
        **kwargs,
    )
    raw_response = _serialize_response(response)
    extracted_text = _extract_chat_output_text(response)
    if not extracted_text:
        extracted_text = _extract_streamed_chat_text(raw_response)
    try:
        parsed = json.loads(_extract_json_text(extracted_text))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AIReviewParseError(
            str(exc),
            raw_response=raw_response,
            extracted_text=extracted_text,
        ) from exc
    return _normalize_fix_plan(parsed, model=model), raw_response


def run_openai_prep_edit(
    *,
    review_payload: Mapping[str, Any],
    universal_rules_path: str | None,
) -> dict[str, Any]:
    model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    base_url = os.getenv("OPENAI_BASE_URL")
    deterministic_plan = _build_deterministic_prep_edit_plan(
        review_payload,
        model=model,
    )
    request_metadata = {
        "model": model,
        "base_url": base_url or "https://api.openai.com/v1",
        "timeout_sec": _openai_timeout_seconds(),
        "candidate_path": review_payload.get("candidate_path"),
        "error_count": len(review_payload.get("errors", [])),
        "warning_count": len(review_payload.get("warnings", [])),
        "deterministic_action_count": len(deterministic_plan.get("actions", [])),
    }

    base_result = {
        "enabled": False,
        "available": False,
        "model": model,
        "actions": list(deterministic_plan.get("actions", [])),
        "summary": str(deterministic_plan.get("summary", "")).strip()
        or "AI prep edit planning was skipped.",
        "warnings": [],
        "request_metadata": request_metadata,
        "raw_response": "",
        "api_error": None,
    }

    if not os.getenv("OPENAI_API_KEY"):
        base_result["warnings"] = [
            "OpenAI prep editor skipped because OPENAI_API_KEY is not set."
        ]
        base_result["summary"] = (
            f"{base_result['summary']} AI prep edit planning was skipped because no API key was found."
        ).strip()
        return base_result

    rules_excerpt = _load_rules_excerpt(universal_rules_path)
    fix_payload = {
        "candidate_path": review_payload.get("candidate_path"),
        "errors": review_payload.get("errors", []),
        "warnings": review_payload.get("warnings", []),
        "suggested_fixes": review_payload.get("suggested_fixes", []),
        "summary": review_payload.get("summary", ""),
        "review_context": review_payload.get("context", {}),
        "rules_excerpt": rules_excerpt,
    }

    system_prompt = (
        "You are a prep-editor agent for a Cadence Genus RTL prep flow. "
        "You do not decide pass/fail; the reviewer does that. "
        "Your job is to propose only safe, bounded edits to the prepared bundle so the reviewer can re-check it. "
        "Never invent missing RTL logic or fabricate source files to satisfy unresolved dependencies. "
        "Allowed edits are limited to removing stray files, removing stray lines, replacing text in generated Tcl, SDC, filelist, "
        "or metadata files that already exist in the prepared bundle, or appending clarifying text to those generated files. "
        "If the selected synthesis top looks like a testbench or simulation harness based on file contents "
        "(for example: no ports, initial blocks, delay-based clock generation, or $finish/$display-heavy behavior), "
        "you may update top_module.txt only if the review context already identifies a likelier RTL design top candidate. "
        "If review_context.synthesis_top_checks.chosen_recipe_id is set to memory_controller_impl_bundle and a deterministic adaptation plan is present, "
        "prefer a single apply_synthesis_recipe action that uses that approved recipe rather than inventing new RTL edits. "
        "If review_context.synthesis_top_checks.chosen_recipe_id refers to generic_interface_top_to_impl, do not emit apply_synthesis_recipe. "
        "Instead, if review_context.synthesis_top_checks.adaptation_inferable is true and a deterministic adaptation plan is present, "
        "emit a single adapt_synthesis_top action that uses that existing plan to generate a synthesis-only copy of the top inside the prepared build. "
        "For selected synthesis tops that expose raw generic interface ports, you may update top_module.txt only if the review context already identifies a clearly safer wrapper/top candidate. "
        "If review_context.iteration_context or review_context.prior_fix_history is present, preserve previously resolved fixes, "
        "focus on persistent blockers, and do not reintroduce issues that earlier prep edits already removed. "
        "Do not rewrite RTL modules to remove interfaces, invent wrappers, or fabricate behavioral connections unless an approved deterministic recipe explicitly covers that build-only transform. "
        "When editing filelist_genus.f, keep it path-only: allow source paths, blank lines, comments, and +incdir+... lines only. "
        "Do not insert +sv, -sv, read_hdl options, language mode switches, or any other pseudo-directives into filelist_genus.f. "
        "Keep language selection in run_genus_mc.tcl instead. "
        "If duplicate filelist entries are present, prefer a bounded normalization that keeps only the first occurrence of each allowed source/directive line. "
        "If an issue cannot be safely fixed automatically, return no action for it and mention that in the summary. "
        "Prefer the smallest edit set that directly addresses the current review findings."
    )

    try:
        client = _build_client()
        request_metadata["api_style"] = "responses"
        raw_response = ""
        try:
            normalized, raw_response = _call_fix_responses_api(
                client,
                model=model,
                system_prompt=system_prompt,
                fix_payload=fix_payload,
            )
        except Exception as exc:
            if not _is_not_found_error(exc):
                raise
            request_metadata["responses_api_error"] = str(exc)
            request_metadata["api_style"] = "chat_completions_fallback"
            try:
                normalized, raw_response = _call_fix_chat_api(
                    client,
                    model=model,
                    system_prompt=system_prompt,
                    fix_payload=fix_payload,
                    plain_json=False,
                )
            except AIReviewParseError as chat_exc:
                raw_response = chat_exc.raw_response
                request_metadata["chat_completions_fallback_error"] = str(chat_exc)
                request_metadata["chat_completions_extracted_text"] = (
                    chat_exc.extracted_text[:4000]
                )
                request_metadata["api_style"] = "chat_completions_plain_json_fallback"
                normalized, raw_response = _call_fix_chat_api(
                    client,
                    model=model,
                    system_prompt=system_prompt,
                    fix_payload=fix_payload,
                    plain_json=True,
                )
        normalized = _merge_edit_plans(deterministic_plan, normalized)
        normalized_actions, coercion_warnings = _coerce_supported_prep_actions(
            [
                dict(item)
                for item in normalized.get("actions", [])
                if isinstance(item, Mapping)
            ],
            review_payload,
        )
        normalized["actions"] = normalized_actions
        normalized["request_metadata"] = request_metadata
        normalized["raw_response"] = raw_response
        normalized["api_error"] = None
        normalized["warnings"] = coercion_warnings
        return normalized
    except AIReviewParseError as exc:
        base_result["warnings"] = [
            f"OpenAI prep editor failed and no automated edits were applied: {exc}"
        ]
        base_result["summary"] = "AI prep edit planning failed; no automated edits were applied."
        base_result["api_error"] = str(exc)
        base_result["raw_response"] = exc.raw_response
        base_result["request_metadata"] = {
            **request_metadata,
            "last_extracted_text": exc.extracted_text[:4000],
        }
        return base_result
    except Exception as exc:
        base_result["warnings"] = [
            f"OpenAI prep editor failed and no automated edits were applied: {exc}"
        ]
        base_result["summary"] = "AI prep edit planning failed; no automated edits were applied."
        base_result["api_error"] = str(exc)
        return base_result
