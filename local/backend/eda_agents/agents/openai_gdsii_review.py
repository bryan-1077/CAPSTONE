from __future__ import annotations

import json
import os
from typing import Any, Mapping

from agents.openai_prep_review import (
    AIReviewParseError,
    AI_REVIEW_SCHEMA,
    DEFAULT_OPENAI_MODEL,
    _build_client,
    _extract_chat_output_text,
    _extract_json_text,
    _extract_output_text,
    _extract_streamed_chat_text,
    _is_not_found_error,
    _load_rules_excerpt,
    _ordered_unique,
    _serialize_response,
)


def _normalize_ai_result(result: Mapping[str, Any], *, model: str) -> dict[str, Any]:
    errors = _ordered_unique(result.get("errors", []))
    warnings = _ordered_unique(result.get("warnings", []))
    suggested_fixes = _ordered_unique(result.get("suggested_fixes", []))

    return {
        "enabled": True,
        "available": True,
        "model": model,
        "pass": not errors,
        "errors": errors,
        "warnings": warnings,
        "suggested_fixes": suggested_fixes,
        "summary": str(result.get("summary", "")).strip()
        or "AI GDSII-stage review completed.",
    }


def _call_responses_api(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    review_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(review_payload)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "gdsii_ai_review",
                "strict": True,
                "schema": AI_REVIEW_SCHEMA,
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
    review_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(review_payload)},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "gdsii_ai_review",
                "strict": True,
                "schema": AI_REVIEW_SCHEMA,
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
    review_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    f"{system_prompt} "
                    "Return only a single JSON object matching this schema: "
                    f"{json.dumps(AI_REVIEW_SCHEMA)} "
                    "Do not wrap the JSON in markdown fences."
                ),
            },
            {"role": "user", "content": json.dumps(review_payload)},
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


def run_openai_gdsii_review(
    *,
    local_review: Mapping[str, Any],
    universal_rules_path: str | None,
    execution_context: Mapping[str, Any],
) -> dict[str, Any]:
    model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    base_url = os.getenv("OPENAI_BASE_URL")
    request_metadata = {
        "model": model,
        "base_url": base_url or "https://api.openai.com/v1",
        "candidate_path": local_review.get("candidate_path"),
        "mappeddir": execution_context.get("mappeddir"),
        "local_error_count": len(local_review.get("errors", [])),
        "local_warning_count": len(local_review.get("warnings", [])),
        "execution_ok": bool(execution_context.get("ok")),
    }

    base_result = {
        "enabled": False,
        "available": False,
        "model": model,
        "pass": True,
        "errors": [],
        "warnings": [],
        "suggested_fixes": [],
        "summary": "AI GDSII-stage review was skipped.",
        "request_metadata": request_metadata,
        "raw_response": "",
        "api_error": None,
    }

    if not os.getenv("OPENAI_API_KEY"):
        base_result["warnings"] = [
            "OpenAI GDSII-stage review skipped because OPENAI_API_KEY is not set."
        ]
        base_result["summary"] = (
            "AI GDSII-stage review was skipped because no API key was found."
        )
        return base_result

    try:
        from openai import OpenAI  # noqa: F401
    except ImportError as exc:
        base_result["warnings"] = [
            "OpenAI GDSII-stage review skipped because the openai Python package is not installed."
        ]
        base_result["summary"] = (
            "AI GDSII-stage review was skipped because the OpenAI SDK is unavailable."
        )
        base_result["api_error"] = str(exc)
        return base_result

    rules_excerpt = _load_rules_excerpt(universal_rules_path)
    review_payload = {
        "candidate_path": local_review.get("candidate_path"),
        "local_review": {
            "pass": bool(local_review.get("pass")),
            "failure_stage_owner": local_review.get("failure_stage_owner"),
            "failure_class": local_review.get("failure_class"),
            "route_back_to_stage": local_review.get("route_back_to_stage"),
            "blocking_errors": local_review.get(
                "blocking_errors", local_review.get("errors", [])
            ),
            "errors": local_review.get("errors", []),
            "warnings": local_review.get("warnings", []),
            "suggested_fixes": local_review.get("suggested_fixes", []),
            "summary": local_review.get("summary", ""),
        },
        "review_context": local_review.get("context", {}),
        "execution_context": execution_context,
        "rules_excerpt": rules_excerpt,
    }

    system_prompt = (
        "You are reviewing a GDSII/export stage bring-up run that starts from a successful mapped implementation bundle. "
        "Inspect the incoming mapped contract, detected GDSII outputs, command execution context, and stage logs. "
        "If the GDSII stage is failing because the mapped handoff bundle is missing required artifacts or metadata, "
        "route the issue back to the mapped_netlist stage instead of keeping it in gdsii. "
        "If the stage launched and failed on Innovus import, physical-design execution, stream-out, or other GDSII-stage work, "
        "keep ownership in the gdsii stage. "
        "Return only structured JSON that matches the requested schema. "
        "Use warnings for soft issues and errors only for issues that should block the GDSII stage."
    )

    try:
        client = _build_client()
        request_metadata["api_style"] = "responses"
        raw_response = ""
        try:
            normalized, raw_response = _call_responses_api(
                client,
                model=model,
                system_prompt=system_prompt,
                review_payload=review_payload,
            )
        except Exception as exc:
            if not _is_not_found_error(exc):
                raise
            request_metadata["responses_api_error"] = str(exc)
            request_metadata["api_style"] = "chat_completions_fallback"
            try:
                normalized, raw_response = _call_chat_completions_api(
                    client,
                    model=model,
                    system_prompt=system_prompt,
                    review_payload=review_payload,
                )
            except AIReviewParseError as chat_exc:
                raw_response = chat_exc.raw_response
                request_metadata["chat_completions_fallback_error"] = str(chat_exc)
                request_metadata["chat_completions_extracted_text"] = (
                    chat_exc.extracted_text[:4000]
                )
                request_metadata["api_style"] = "chat_completions_plain_json_fallback"
                normalized, raw_response = _call_chat_completions_plain_json_api(
                    client,
                    model=model,
                    system_prompt=system_prompt,
                    review_payload=review_payload,
                )
        normalized["request_metadata"] = request_metadata
        normalized["raw_response"] = raw_response
        normalized["api_error"] = None
        return normalized
    except AIReviewParseError as exc:
        base_result["warnings"] = [
            f"OpenAI GDSII-stage review failed and local validation was used instead: {exc}"
        ]
        base_result["summary"] = (
            "AI GDSII-stage review failed; using deterministic local review only."
        )
        base_result["api_error"] = str(exc)
        base_result["raw_response"] = exc.raw_response
        base_result["request_metadata"] = {
            **request_metadata,
            "last_extracted_text": exc.extracted_text[:4000],
        }
        return base_result
    except Exception as exc:
        base_result["warnings"] = [
            f"OpenAI GDSII-stage review failed and local validation was used instead: {exc}"
        ]
        base_result["summary"] = (
            "AI GDSII-stage review failed; using deterministic local review only."
        )
        base_result["api_error"] = str(exc)
        base_result["raw_response"] = locals().get("raw_response", "")
        return base_result


def merge_gdsii_review_results(
    *,
    local_review: Mapping[str, Any],
    ai_review: Mapping[str, Any],
) -> dict[str, Any]:
    local_errors = _ordered_unique(
        local_review.get("blocking_errors", local_review.get("errors", []))
    )
    local_warnings = _ordered_unique(local_review.get("warnings", []))
    local_fixes = _ordered_unique(local_review.get("suggested_fixes", []))

    ai_errors = _ordered_unique(ai_review.get("errors", []))
    ai_warnings = _ordered_unique(ai_review.get("warnings", []))
    ai_fixes = _ordered_unique(ai_review.get("suggested_fixes", []))

    merged_errors = _ordered_unique([*local_errors, *ai_errors])
    merged_warnings = _ordered_unique([*local_warnings, *ai_warnings])
    merged_fixes = _ordered_unique([*local_fixes, *ai_fixes])

    local_summary = str(local_review.get("summary", "")).strip()
    ai_summary = str(ai_review.get("summary", "")).strip()
    summary_parts = [part for part in [local_summary, ai_summary] if part]

    return {
        "pass": not merged_errors,
        "failure_stage_owner": local_review.get("failure_stage_owner"),
        "failure_class": local_review.get("failure_class"),
        "route_back_to_stage": local_review.get("route_back_to_stage"),
        "blocking_errors": merged_errors,
        "errors": merged_errors,
        "warnings": merged_warnings,
        "suggested_fixes": merged_fixes,
        "summary": " ".join(summary_parts).strip()
        or "GDSII-stage review completed.",
        "candidate_path": local_review.get("candidate_path"),
        "rules_path": local_review.get("rules_path"),
        "context": local_review.get("context", {}),
        "local_review": {
            "pass": not local_errors,
            "failure_stage_owner": local_review.get("failure_stage_owner"),
            "failure_class": local_review.get("failure_class"),
            "route_back_to_stage": local_review.get("route_back_to_stage"),
            "blocking_errors": local_errors,
            "errors": local_errors,
            "warnings": local_warnings,
            "suggested_fixes": local_fixes,
            "summary": local_summary,
        },
        "ai_review": {
            "enabled": bool(ai_review.get("enabled")),
            "available": bool(ai_review.get("available")),
            "model": ai_review.get("model"),
            "pass": not ai_errors,
            "errors": ai_errors,
            "warnings": ai_warnings,
            "suggested_fixes": ai_fixes,
            "summary": ai_summary,
            "request_metadata": ai_review.get("request_metadata", {}),
            "api_error": ai_review.get("api_error"),
        },
    }
