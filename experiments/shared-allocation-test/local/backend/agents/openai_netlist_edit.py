from __future__ import annotations

import json
import os
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
    _serialize_response,
)
from services.stage_routing import should_run_netlist_editor

FIX_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["remove_file", "remove_line", "replace_text", "append_text"],
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


def _sanitize_fix_actions(actions: list[dict[str, str]]) -> list[dict[str, str]]:
    sanitized: list[dict[str, str]] = []
    for action in actions:
        action_type = str(action.get("type", "")).strip()
        path = str(action.get("path", "")).strip()
        reason = str(action.get("reason", "")).strip()
        if not action_type or not path or not reason:
            continue
        sanitized.append(
            {
                "type": action_type,
                "path": path,
                "reason": reason,
                "search_text": str(action.get("search_text", "")),
                "replacement_text": str(action.get("replacement_text", "")),
                "line_text": str(action.get("line_text", "")),
            }
        )
    return sanitized


def _blocking_errors(review_payload: Mapping[str, Any]) -> list[str]:
    raw_errors = review_payload.get("blocking_errors", review_payload.get("errors", []))
    return [str(item).strip() for item in raw_errors if str(item).strip()]


def _has_error(errors: list[str], pattern: str) -> bool:
    regex = re.compile(pattern)
    return any(regex.search(item) for item in errors)


def _build_memcontfsm_width_actions(review_payload: Mapping[str, Any]) -> list[dict[str, str]]:
    errors = _blocking_errors(review_payload)
    actions: list[dict[str, str]] = []

    if _has_error(errors, r"MemContFSM_impl\.sv'.*pointer'.*line 66"):
        actions.append(
            {
                "type": "replace_text",
                "path": "generated/MemContFSM_impl.sv",
                "reason": "Use a self-sized zero literal for the vector reset assignment to avoid the Genus bitwidth mismatch on pointer.",
                "search_text": "        pointer <= 1'b0;\n",
                "replacement_text": "        pointer <= '0;\n",
                "line_text": "",
            }
        )

    if _has_error(errors, r"MemContFSM_impl\.sv'.*WaitRegRd'.*line 67"):
        actions.append(
            {
                "type": "replace_text",
                "path": "generated/MemContFSM_impl.sv",
                "reason": "Use a self-sized zero literal for the 5-bit wait register reset assignment.",
                "search_text": "        WaitRegRd <= 4'b0;\n",
                "replacement_text": "        WaitRegRd <= '0;\n",
                "line_text": "",
            }
        )

    if _has_error(errors, r"MemContFSM_impl\.sv'.*line 174"):
        actions.append(
            {
                "type": "replace_text",
                "path": "generated/MemContFSM_impl.sv",
                "reason": "Drive the DDR bus with a vector-sized high-impedance literal instead of a 1-bit 'z.",
                "search_text": "    DDR4Bus.dq_c = 'z;\n",
                "replacement_text": "    DDR4Bus.dq_c = {DATAWIDTH{1'bz}};\n",
                "line_text": "",
            }
        )

    return actions


def _build_scheduler_width_actions(review_payload: Mapping[str, Any]) -> list[dict[str, str]]:
    errors = _blocking_errors(review_payload)
    if not (
        _has_error(errors, r"Scheduler\.sv'.*\{PageHitT,PageMissT,PageEmptyT\}.*line 27[579]")
        or _has_error(errors, r"Scheduler\.sv'.*\{PageHit,PageMiss,PageEmpty\}.*line 317")
    ):
        return []

    return [
        {
            "type": "replace_text",
            "path": "rtl/Scheduler.sv",
            "reason": "Make PageHitT match the 2-bit PageHit output so the concatenation widths stay consistent across scheduler handoff logic.",
            "search_text": "logic [2:0] PageHitT;\n",
            "replacement_text": "logic [1:0] PageHitT;\n",
            "line_text": "",
        }
    ]


def _build_ddr4interface_width_actions(review_payload: Mapping[str, Any]) -> list[dict[str, str]]:
    errors = _blocking_errors(review_payload)
    if not _has_error(errors, r"DDR4Interface\.sv'.*line (83|93|110|128|140|152|164)"):
        return []

    return [
        {
            "type": "replace_text",
            "path": "rtl/DDR4Interface.sv",
            "reason": "Use an explicit 2-bit command literal instead of assigning a 1-bit LOW constant into a 2-bit concatenation.",
            "search_text": "\t{cs_n,act_n} = LOW;\n",
            "replacement_text": "\t{cs_n,act_n} = 2'b00;\n",
            "line_text": "",
        },
        {
            "type": "replace_text",
            "path": "rtl/DDR4Interface.sv",
            "reason": "Use an explicit 4-bit command literal for the MRS control concatenation.",
            "search_text": "\t{cs_n,`RAS,`CAS,`WE} = LOW;\n",
            "replacement_text": "\t{cs_n,`RAS,`CAS,`WE} = 4'b0000;\n",
            "line_text": "",
        },
        {
            "type": "replace_text",
            "path": "rtl/DDR4Interface.sv",
            "reason": "Use an explicit 4-bit command literal for PRE so the concatenation width matches exactly.",
            "search_text": "\t\t{cs_n,`RAS,`WE,`AP} = LOW;\n",
            "replacement_text": "\t\t{cs_n,`RAS,`WE,`AP} = 4'b0000;\n",
            "line_text": "",
        },
        {
            "type": "replace_text",
            "path": "rtl/DDR4Interface.sv",
            "reason": "Use an explicit 4-bit command literal for WR so the concatenation width matches exactly.",
            "search_text": "\t{cs_n,`CAS,`WE,`AP} = LOW;\n",
            "replacement_text": "\t{cs_n,`CAS,`WE,`AP} = 4'b0000;\n",
            "line_text": "",
        },
        {
            "type": "replace_text",
            "path": "rtl/DDR4Interface.sv",
            "reason": "Use an explicit 4-bit command literal for RD so the concatenation width matches exactly.",
            "search_text": "\t\t{cke,act_n,`RAS,`WE} = `HIGH;\n",
            "replacement_text": "\t\t{cke,act_n,`RAS,`WE} = 4'b1111;\n",
            "line_text": "",
        },
        {
            "type": "replace_text",
            "path": "rtl/DDR4Interface.sv",
            "reason": "Use an explicit 4-bit command literal for WRA so the concatenation width matches exactly.",
            "search_text": "\t{cke,act_n,`RAS,`AP} = `HIGH;\n",
            "replacement_text": "\t{cke,act_n,`RAS,`AP} = 4'b1111;\n",
            "line_text": "",
        },
        {
            "type": "replace_text",
            "path": "rtl/DDR4Interface.sv",
            "reason": "Use an explicit 5-bit command literal for RDA so the concatenation width matches exactly.",
            "search_text": "\t\t{cke,act_n,`RAS,`WE,`AP} = `HIGH;\n",
            "replacement_text": "\t\t{cke,act_n,`RAS,`WE,`AP} = 5'b11111;\n",
            "line_text": "",
        },
    ]


def _build_deterministic_netlist_edit_plan(
    review_payload: Mapping[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    actions = _ordered_unique_actions(
        _sanitize_fix_actions(
            [
                *_build_memcontfsm_width_actions(review_payload),
                *_build_scheduler_width_actions(review_payload),
                *_build_ddr4interface_width_actions(review_payload),
            ]
        )
    )
    summary = (
        "Deterministic netlist edits will apply bounded synthesis-compatibility fixes for proven width-mismatch patterns in prepared RTL."
        if actions
        else "No deterministic netlist edits matched the current review findings."
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
        "summary": summary or "Netlist edit planning completed.",
    }


def _normalize_edit_plan(result: Mapping[str, Any], *, model: str) -> dict[str, Any]:
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
        "actions": actions,
        "summary": str(result.get("summary", "")).strip() or "AI netlist edit planning completed.",
    }


def _call_edit_responses_api(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    edit_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(edit_payload)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "netlist_edit_plan",
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
    return _normalize_edit_plan(parsed, model=model), raw_response


def _call_edit_chat_api(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    edit_payload: Mapping[str, Any],
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
                    "name": "netlist_edit_plan",
                    "strict": True,
                    "schema": FIX_PLAN_SCHEMA,
                },
            }
        }

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": json.dumps(edit_payload)},
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
    return _normalize_edit_plan(parsed, model=model), raw_response


def run_openai_netlist_edit(
    *,
    review_payload: Mapping[str, Any],
    universal_rules_path: str | None,
) -> dict[str, Any]:
    model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    base_url = os.getenv("OPENAI_BASE_URL")
    deterministic_plan = _build_deterministic_netlist_edit_plan(
        review_payload,
        model=model,
    )
    review_context = review_payload.get("context", {})
    request_metadata = {
        "model": model,
        "base_url": base_url or "https://api.openai.com/v1",
        "candidate_path": review_payload.get("candidate_path"),
        "builddir": review_context.get("input_builddir"),
        "error_count": len(
            review_payload.get("blocking_errors", review_payload.get("errors", []))
        ),
        "warning_count": len(review_payload.get("warnings", [])),
        "failure_stage_owner": review_payload.get("failure_stage_owner"),
        "failure_class": review_payload.get("failure_class"),
        "route_back_to_stage": review_payload.get("route_back_to_stage"),
        "deterministic_action_count": len(deterministic_plan.get("actions", [])),
    }

    base_result = {
        "enabled": False,
        "available": False,
        "model": model,
        "actions": list(deterministic_plan.get("actions", [])),
        "summary": str(deterministic_plan.get("summary", "")).strip()
        or "AI netlist edit planning was skipped.",
        "warnings": [],
        "request_metadata": request_metadata,
        "raw_response": "",
        "api_error": None,
    }

    if str(review_payload.get("failure_class", "")).strip() == "generic_interface_top":
        base_result["summary"] = (
            "Netlist edit planning was skipped because raw generic interface ports on the selected synthesis top are a prep-owned top-selection/wrapper problem."
        )
        base_result["warnings"] = [
            "Netlist editor declined RTL/interface surgery; choose a synthesis-safe wrapper or explicit top override in prep."
        ]
        return base_result

    if not should_run_netlist_editor(review_payload):
        base_result["summary"] = (
            "Netlist edit planning was skipped because the reviewer did not classify the failure as netlist-owned."
        )
        base_result["warnings"] = [
            "Netlist editor skipped because the current failure is not routed to the netlist stage."
        ]
        return base_result

    if not os.getenv("OPENAI_API_KEY"):
        base_result["warnings"] = [
            "OpenAI netlist editor skipped because OPENAI_API_KEY is not set."
        ]
        base_result["summary"] = (
            f"{base_result['summary']} AI netlist edit planning was skipped because no API key was found."
        ).strip()
        return base_result

    rules_excerpt = _load_rules_excerpt(universal_rules_path)
    edit_payload = {
        "candidate_path": review_payload.get("candidate_path"),
        "builddir": review_context.get("input_builddir"),
        "failure_stage_owner": review_payload.get("failure_stage_owner"),
        "failure_class": review_payload.get("failure_class"),
        "route_back_to_stage": review_payload.get("route_back_to_stage"),
        "blocking_errors": review_payload.get(
            "blocking_errors",
            review_payload.get("errors", []),
        ),
        "errors": review_payload.get("errors", []),
        "warnings": review_payload.get("warnings", []),
        "suggested_fixes": review_payload.get("suggested_fixes", []),
        "summary": review_payload.get("summary", ""),
        "review_context": review_context,
        "rules_excerpt": rules_excerpt,
    }

    system_prompt = (
        "You are a netlist-editor agent for a Cadence Genus synthesis bring-up flow. "
        "You do not decide pass/fail; the reviewer does that. "
        "Your job is to propose only safe, bounded edits to the prepared build directory so the netlist run can be retried. "
        "Never invent missing RTL logic, fabricate synthesis outputs, or paper over environment, license, or tool installation failures. "
        "Allowed edits are limited to removing stray files, removing stray lines, replacing text in existing prepared RTL, metadata, Tcl, filelist, or constraint files, "
        "or appending clarifying text to existing files. "
        "Do not create missing module implementations or architectural behavior. "
        "Bounded synthesis-compatibility edits to existing RTL are allowed when the reviewer classifies the issue as netlist-owned, "
        "for example replacing an unsupported multi-clock always_ff form with a synthesis-safe equivalent in the prepared build only. "
        "If an issue cannot be safely fixed automatically, return no action for it and say so in the summary. "
        "Prefer the smallest edit set that directly addresses the current review findings."
    )

    try:
        client = _build_client()
        request_metadata["api_style"] = "responses"
        raw_response = ""
        try:
            normalized, raw_response = _call_edit_responses_api(
                client,
                model=model,
                system_prompt=system_prompt,
                edit_payload=edit_payload,
            )
        except Exception as exc:
            if not _is_not_found_error(exc):
                raise
            request_metadata["responses_api_error"] = str(exc)
            request_metadata["api_style"] = "chat_completions_fallback"
            try:
                normalized, raw_response = _call_edit_chat_api(
                    client,
                    model=model,
                    system_prompt=system_prompt,
                    edit_payload=edit_payload,
                    plain_json=False,
                )
            except AIReviewParseError as chat_exc:
                raw_response = chat_exc.raw_response
                request_metadata["chat_completions_fallback_error"] = str(chat_exc)
                request_metadata["chat_completions_extracted_text"] = (
                    chat_exc.extracted_text[:4000]
                )
                request_metadata["api_style"] = "chat_completions_plain_json_fallback"
                normalized, raw_response = _call_edit_chat_api(
                    client,
                    model=model,
                    system_prompt=system_prompt,
                    edit_payload=edit_payload,
                    plain_json=True,
                )
        normalized = _merge_edit_plans(deterministic_plan, normalized)
        normalized["request_metadata"] = request_metadata
        normalized["raw_response"] = raw_response
        normalized["api_error"] = None
        normalized["warnings"] = []
        return normalized
    except AIReviewParseError as exc:
        base_result["warnings"] = [
            f"OpenAI netlist editor failed and no automated edits were applied: {exc}"
        ]
        base_result["summary"] = (
            f"{str(deterministic_plan.get('summary', '')).strip()} "
            "AI netlist edit planning failed; using deterministic netlist edit actions only."
        ).strip()
        base_result["api_error"] = str(exc)
        base_result["raw_response"] = exc.raw_response
        base_result["request_metadata"] = {
            **request_metadata,
            "last_extracted_text": exc.extracted_text[:4000],
        }
        return base_result
    except Exception as exc:
        base_result["warnings"] = [
            f"OpenAI netlist editor failed and no automated edits were applied: {exc}"
        ]
        base_result["summary"] = (
            f"{str(deterministic_plan.get('summary', '')).strip()} "
            "AI netlist edit planning failed; using deterministic netlist edit actions only."
        ).strip()
        base_result["api_error"] = str(exc)
        return base_result
