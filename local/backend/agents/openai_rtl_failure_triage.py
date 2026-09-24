from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Mapping

from agents.openai_prep_review import (
    AIReviewParseError,
    DEFAULT_OPENAI_MODEL,
    _build_client,
    _extract_chat_output_text,
    _extract_json_text,
    _extract_output_text,
    _extract_streamed_chat_text,
    _is_not_found_error,
    _ordered_unique,
    _serialize_response,
)


FRONTEND_OWNER = "frontend"
BACKEND_OWNER = "backend"
UNKNOWN_OWNER = "unknown"

# Keep frontend handoffs focused on RTL design quality, without error subtypes.
FRONTEND_CATEGORIES = {"poor_logic_design"}
BACKEND_CATEGORIES = {
    "timing_violation",
    "congestion",
    "routing_error",
    "sdc_constraint",
}
VALID_CATEGORIES = FRONTEND_CATEGORIES | BACKEND_CATEGORIES | {"unknown"}

RTL_FAILURE_TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handoff_owner": {
            "type": "string",
            "enum": [FRONTEND_OWNER, BACKEND_OWNER, UNKNOWN_OWNER],
        },
        "failure_category": {
            "type": "string",
            "enum": sorted(VALID_CATEGORIES),
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "suggested_next_steps": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "handoff_owner",
        "failure_category",
        "confidence",
        "evidence",
        "warnings",
        "suggested_next_steps",
        "summary",
    ],
    "additionalProperties": False,
}


def _normalized_log_text(
    *,
    logs: Mapping[str, Any] | None = None,
    execution_context: Mapping[str, Any] | None = None,
    local_review: Mapping[str, Any] | None = None,
) -> str:
    parts: list[str] = []
    for source in (logs or {}, execution_context or {}, local_review or {}):
        for key, value in source.items():
            if isinstance(value, str) and key.lower() in {
                "stdout",
                "stderr",
                "log",
                "log_text",
                "summary",
                "last_error",
            }:
                parts.append(value)
            elif isinstance(value, list) and key.lower() in {
                "errors",
                "blocking_errors",
                "warnings",
                "suggested_fixes",
            }:
                parts.extend(str(item) for item in value)

    return "\n".join(part for part in parts if str(part).strip())


def _ordered_unique_strings(values: Iterable[Any]) -> list[str]:
    return _ordered_unique(str(item) for item in values)


def _matching_lines(text: str, pattern: str, *, limit: int = 12) -> list[str]:
    regex = re.compile(pattern, re.IGNORECASE)
    matches: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and regex.search(line):
            matches.append(line)
        if len(matches) >= limit:
            break
    return _ordered_unique_strings(matches)


def _score_signals(text: str, rtl_sources: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    rtl_text = "\n".join((rtl_sources or {}).values())
    combined = "\n".join(part for part in [text, rtl_text] if part)
    signals: list[dict[str, Any]] = []

    checks = [
        {
            "owner": FRONTEND_OWNER,
            "category": "poor_logic_design",
            "weight": 100,
            "pattern": (
                r"\b(?:poor|inefficient|low[-\s]quality)\s+(?:rtl\s+)?logic\s+(?:design|structure)\b"
                r"|\b(?:poor|inefficient|low[-\s]quality)\s+rtl\s+(?:design|architecture)\b"
                r"|\bexcessive\s+(?:combinational|rtl\s+logic)\s+depth\b"
                r"|\b(?:rtl|logic|datapath)\s+(?:redesign|restructuring)\s+(?:is\s+)?required\b"
            ),
            "label": "RTL logic design quality diagnostic",
        },
        {
            "owner": BACKEND_OWNER,
            "category": "timing_violation",
            "weight": 100,
            "pattern": r"(?:setup|hold|recovery|removal)\s+(?:violation|slack)|\bwns\b|\btns\b|negative\s+slack|timing\s+(?:not\s+met|violat)",
            "label": "Timing violation diagnostic",
        },
        {
            "owner": BACKEND_OWNER,
            "category": "congestion",
            "weight": 95,
            "pattern": r"\bcongestion\b|overflow|global\s+route.*congest|gcell.*overflow|utili[sz]ation.*(?:high|exceed)",
            "label": "Placement/routing congestion diagnostic",
        },
        {
            "owner": BACKEND_OWNER,
            "category": "routing_error",
            "weight": 100,
            "pattern": r"route\s+(?:fail|error)|unroute[sd]?|short\s+(?:circuit|net)|drc.*(?:route|metal|via)|antenna|open\s+net|nanoroute",
            "label": "Backend routing/DRC diagnostic",
        },
        {
            "owner": BACKEND_OWNER,
            "category": "sdc_constraint",
            "weight": 90,
            "pattern": r"\bsdc\b|create_clock|set_(?:input|output)_delay|false_path|multicycle|clock\s+(?:not\s+found|undefined)|constraint.*(?:missing|invalid|error)",
            "label": "SDC/constraint diagnostic",
        },
    ]

    for check in checks:
        # RTL keywords alone do not establish poor design; require a diagnostic.
        evidence = _matching_lines(
            text if check["owner"] == FRONTEND_OWNER else combined,
            check["pattern"],
        )
        if evidence:
            signals.append({**check, "evidence": evidence})

    return signals


def _normalize_owner_category(owner: Any, category: Any) -> tuple[str, str]:
    owner_text = str(owner or UNKNOWN_OWNER).strip().lower()
    category_text = str(category or "unknown").strip().lower()
    if owner_text not in {FRONTEND_OWNER, BACKEND_OWNER, UNKNOWN_OWNER}:
        owner_text = UNKNOWN_OWNER
    if category_text not in VALID_CATEGORIES:
        category_text = "unknown"
    if category_text == "unknown":
        owner_text = UNKNOWN_OWNER
    if category_text in FRONTEND_CATEGORIES:
        owner_text = FRONTEND_OWNER
    if category_text in BACKEND_CATEGORIES:
        owner_text = BACKEND_OWNER
    return owner_text, category_text


def run_local_rtl_failure_triage(
    *,
    local_review: Mapping[str, Any] | None = None,
    execution_context: Mapping[str, Any] | None = None,
    logs: Mapping[str, Any] | None = None,
    rtl_sources: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    text = _normalized_log_text(
        logs=logs,
        execution_context=execution_context,
        local_review=local_review,
    )
    signals = _score_signals(text, rtl_sources)
    ranked = sorted(signals, key=lambda item: int(item["weight"]), reverse=True)
    best = ranked[0] if ranked else None

    if best:
        owner = str(best["owner"])
        category = str(best["category"])
        confidence = 0.85 if int(best["weight"]) >= 95 else 0.75
        evidence = _ordered_unique_strings(best.get("evidence", []))
        summary = (
            f"Failure triaged to {owner} as {category} based on "
            f"{best['label'].lower()}."
        )
    else:
        owner = UNKNOWN_OWNER
        category = "unknown"
        confidence = 0.2
        evidence = []
        summary = "No known RTL/front-end or implementation/back-end failure pattern was detected."

    frontend_hits = [item for item in ranked if item["owner"] == FRONTEND_OWNER]
    backend_hits = [item for item in ranked if item["owner"] == BACKEND_OWNER]
    warnings = []
    if frontend_hits and backend_hits:
        warnings.append(
            "Both frontend and backend signals were detected; fix the highest-confidence blocking diagnostic first and rerun."
        )

    return {
        "enabled": True,
        "available": True,
        "model": "deterministic",
        "handoff_owner": owner,
        "failure_category": category,
        "confidence": confidence,
        "evidence": evidence,
        "warnings": warnings,
        "suggested_next_steps": _suggested_next_steps(owner, category),
        "summary": summary,
        "signals": ranked,
    }


def _suggested_next_steps(owner: str, category: str) -> list[str]:
    if owner == FRONTEND_OWNER:
        return [
            "Send the RTL back to frontend designers with the supporting evidence "
            "to improve the logic design or architecture."
        ]
    if owner == BACKEND_OWNER:
        by_category = {
            "timing_violation": "Keep in backend and tune constraints, synthesis/place optimization, buffering, or floorplan.",
            "congestion": "Keep in backend and address placement density, macro placement, floorplan, or routing resources.",
            "routing_error": "Keep in backend and repair route setup, DRC blockers, floorplan, or physical implementation settings.",
            "sdc_constraint": "Keep in backend and repair SDC clocks, exceptions, generated clocks, or IO delay constraints.",
        }
        return [by_category.get(category, "Keep in backend implementation for physical/synthesis-flow investigation.")]
    return ["Collect richer RTL, tool log, and report evidence before assigning ownership."]


def _normalize_ai_result(result: Mapping[str, Any], *, model: str) -> dict[str, Any]:
    owner, category = _normalize_owner_category(
        result.get("handoff_owner"),
        result.get("failure_category"),
    )
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    return {
        "enabled": True,
        "available": True,
        "model": model,
        "handoff_owner": owner,
        "failure_category": category,
        "confidence": confidence,
        "evidence": _ordered_unique_strings(result.get("evidence", [])),
        "warnings": _ordered_unique_strings(result.get("warnings", [])),
        "suggested_next_steps": _ordered_unique_strings(
            result.get("suggested_next_steps", [])
        )
        or _suggested_next_steps(owner, category),
        "summary": str(result.get("summary", "")).strip()
        or "RTL failure triage completed.",
    }


def _call_responses_api(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    triage_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(triage_payload)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "rtl_failure_triage",
                "strict": True,
                "schema": RTL_FAILURE_TRIAGE_SCHEMA,
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
    return _normalize_ai_result(parsed, model=model), raw_response


def _call_chat_completions_api(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    triage_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(triage_payload)},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "rtl_failure_triage",
                "strict": True,
                "schema": RTL_FAILURE_TRIAGE_SCHEMA,
            },
        },
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
    return _normalize_ai_result(parsed, model=model), raw_response


def _call_chat_completions_plain_json_api(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    triage_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    f"{system_prompt} Return only a single JSON object matching "
                    f"this schema: {json.dumps(RTL_FAILURE_TRIAGE_SCHEMA)} "
                    "Do not wrap the JSON in markdown fences."
                ),
            },
            {"role": "user", "content": json.dumps(triage_payload)},
        ],
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
    return _normalize_ai_result(parsed, model=model), raw_response


def run_openai_rtl_failure_triage(
    *,
    local_review: Mapping[str, Any] | None = None,
    execution_context: Mapping[str, Any] | None = None,
    logs: Mapping[str, Any] | None = None,
    rtl_sources: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    local_triage = run_local_rtl_failure_triage(
        local_review=local_review,
        execution_context=execution_context,
        logs=logs,
        rtl_sources=rtl_sources,
    )
    model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    base_url = os.getenv("OPENAI_BASE_URL")
    request_metadata = {
        "model": model,
        "base_url": base_url or "https://api.openai.com/v1",
        "local_review_failure_class": (local_review or {}).get("failure_class"),
        "local_handoff_owner": local_triage.get("handoff_owner"),
        "local_failure_category": local_triage.get("failure_category"),
        "log_chars": len(_normalized_log_text(
            logs=logs,
            execution_context=execution_context,
            local_review=local_review,
        )),
        "rtl_file_count": len(rtl_sources or {}),
    }
    base_result = {
        **local_triage,
        "enabled": False,
        "available": False,
        "model": model,
        "request_metadata": request_metadata,
        "raw_response": "",
        "api_error": None,
        "ai_triage": None,
    }

    if not os.getenv("OPENAI_API_KEY"):
        base_result["warnings"] = _ordered_unique_strings(
            [
                *base_result.get("warnings", []),
                "OpenAI RTL failure triage skipped because OPENAI_API_KEY is not set.",
            ]
        )
        return base_result

    try:
        from openai import OpenAI  # noqa: F401
    except ImportError as exc:
        base_result["warnings"] = _ordered_unique_strings(
            [
                *base_result.get("warnings", []),
                "OpenAI RTL failure triage skipped because the openai Python package is not installed.",
            ]
        )
        base_result["api_error"] = str(exc)
        return base_result

    triage_payload = {
        "local_triage": {
            "handoff_owner": local_triage.get("handoff_owner"),
            "failure_category": local_triage.get("failure_category"),
            "confidence": local_triage.get("confidence"),
            "evidence": local_triage.get("evidence", []),
            "signals": local_triage.get("signals", []),
            "summary": local_triage.get("summary", ""),
        },
        "local_review": local_review or {},
        "execution_context": execution_context or {},
        "logs": logs or {},
        "rtl_sources": rtl_sources or {},
    }
    system_prompt = (
        "You are an RTL failure triage agent for an ASIC/EDA flow. "
        "Classify a failing RTL or implementation run into exactly one owner and category. "
        "Use frontend with category poor_logic_design only when evidence shows poor or inefficient RTL logic design "
        "that requires source-level redesign, such as excessive combinational depth, inefficient mux or arithmetic "
        "structure, or unsuitable datapath architecture. Cite the evidence; do not infer poor design from RTL keywords alone. "
        #"Do not classify linting, syntax errors, generic code errors, or FSM state lockups as frontend handoffs. "
        #"If those are the only diagnostics, return unknown owner and category. "
        "Use backend when implementation flow, timing closure, physical design, routing, or constraints should change: "
        "timing_violation, congestion, routing_error, or sdc_constraint. "
        "Frontend means send the RTL back to designers to improve logic quality, without finer frontend error categories. "
        "Timing or congestion alone does not establish poor RTL design; keep it in backend unless evidence "
        "identifies an RTL design weakness as the root cause. "
        "Backend means keep the issue in synthesis/place-route/signoff implementation for tool, constraint, timing, or physical changes. "
        "If evidence is mixed, choose the earliest blocking root cause and mention the secondary evidence in warnings. "
        "Return only structured JSON that matches the requested schema."
    )

    try:
        client = _build_client()
        request_metadata["api_style"] = "responses"
        raw_response = ""
        try:
            ai_triage, raw_response = _call_responses_api(
                client,
                model=model,
                system_prompt=system_prompt,
                triage_payload=triage_payload,
            )
        except Exception as exc:
            if not _is_not_found_error(exc):
                raise
            request_metadata["responses_api_error"] = str(exc)
            request_metadata["api_style"] = "chat_completions_fallback"
            try:
                ai_triage, raw_response = _call_chat_completions_api(
                    client,
                    model=model,
                    system_prompt=system_prompt,
                    triage_payload=triage_payload,
                )
            except AIReviewParseError as chat_exc:
                raw_response = chat_exc.raw_response
                request_metadata["chat_completions_fallback_error"] = str(chat_exc)
                request_metadata["chat_completions_extracted_text"] = (
                    chat_exc.extracted_text[:4000]
                )
                request_metadata["api_style"] = "chat_completions_plain_json_fallback"
                ai_triage, raw_response = _call_chat_completions_plain_json_api(
                    client,
                    model=model,
                    system_prompt=system_prompt,
                    triage_payload=triage_payload,
                )
        return merge_rtl_failure_triage_results(
            local_triage=local_triage,
            ai_triage={
                **ai_triage,
                "request_metadata": request_metadata,
                "raw_response": raw_response,
                "api_error": None,
            },
        )
    except AIReviewParseError as exc:
        base_result["warnings"] = _ordered_unique_strings(
            [
                *base_result.get("warnings", []),
                f"OpenAI RTL failure triage failed and local triage was used instead: {exc}",
            ]
        )
        base_result["api_error"] = str(exc)
        base_result["raw_response"] = exc.raw_response
        base_result["request_metadata"] = {
            **request_metadata,
            "last_extracted_text": exc.extracted_text[:4000],
        }
        return base_result
    except Exception as exc:
        base_result["warnings"] = _ordered_unique_strings(
            [
                *base_result.get("warnings", []),
                f"OpenAI RTL failure triage failed and local triage was used instead: {exc}",
            ]
        )
        base_result["api_error"] = str(exc)
        base_result["raw_response"] = locals().get("raw_response", "")
        return base_result


def merge_rtl_failure_triage_results(
    *,
    local_triage: Mapping[str, Any],
    ai_triage: Mapping[str, Any],
) -> dict[str, Any]:
    local_owner, local_category = _normalize_owner_category(
        local_triage.get("handoff_owner"),
        local_triage.get("failure_category"),
    )
    ai_owner, ai_category = _normalize_owner_category(
        ai_triage.get("handoff_owner"),
        ai_triage.get("failure_category"),
    )
    local_confidence = float(local_triage.get("confidence", 0.0) or 0.0)
    ai_confidence = float(ai_triage.get("confidence", 0.0) or 0.0)

    use_ai = bool(ai_triage.get("available")) and ai_confidence >= max(
        0.6,
        local_confidence + 0.1,
    )
    owner = ai_owner if use_ai else local_owner
    category = ai_category if use_ai else local_category
    confidence = ai_confidence if use_ai else local_confidence

    warnings = _ordered_unique_strings(
        [
            *local_triage.get("warnings", []),
            *ai_triage.get("warnings", []),
        ]
    )
    if ai_triage.get("available") and (local_owner, local_category) != (
        ai_owner,
        ai_category,
    ):
        warnings.append(
            f"AI triage disagreed with deterministic triage: local={local_owner}/{local_category}, ai={ai_owner}/{ai_category}."
        )

    return {
        "enabled": True,
        "available": bool(ai_triage.get("available")),
        "model": ai_triage.get("model") if use_ai else local_triage.get("model"),
        "handoff_owner": owner,
        "failure_category": category,
        "route_to": owner,
        "confidence": confidence,
        "evidence": _ordered_unique_strings(
            [
                *local_triage.get("evidence", []),
                *ai_triage.get("evidence", []),
            ]
        ),
        "warnings": warnings,
        "suggested_next_steps": _ordered_unique_strings(
            [
                *_suggested_next_steps(owner, category),
                *local_triage.get("suggested_next_steps", []),
                *ai_triage.get("suggested_next_steps", []),
            ]
        ),
        "summary": str(
            ai_triage.get("summary") if use_ai else local_triage.get("summary", "")
        ).strip()
        or "RTL failure triage completed.",
        "request_metadata": ai_triage.get("request_metadata", {}),
        "raw_response": ai_triage.get("raw_response", ""),
        "api_error": ai_triage.get("api_error"),
        "local_triage": local_triage,
        "ai_triage": {
            "enabled": bool(ai_triage.get("enabled")),
            "available": bool(ai_triage.get("available")),
            "model": ai_triage.get("model"),
            "handoff_owner": ai_owner,
            "failure_category": ai_category,
            "confidence": ai_confidence,
            "summary": ai_triage.get("summary", ""),
            "request_metadata": ai_triage.get("request_metadata", {}),
            "api_error": ai_triage.get("api_error"),
        },
    }
