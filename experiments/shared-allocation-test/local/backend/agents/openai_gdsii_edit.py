from __future__ import annotations

import json
import os
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
from agents.openai_mapped_edit import (
    FIX_PLAN_SCHEMA,
    _merge_edit_plans,
    _normalize_edit_plan,
    _ordered_unique_actions,
    _sanitize_fix_actions,
)
from services.stage_routing import should_run_gdsii_editor


def _build_deterministic_gdsii_edit_plan(
    review_payload: Mapping[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    actions = _ordered_unique_actions(_sanitize_fix_actions([]))
    summary = (
        "No deterministic GDSII-stage edits matched the current review findings."
    )
    return {
        "enabled": True,
        "available": True,
        "model": model,
        "actions": actions,
        "summary": summary,
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
                "name": "gdsii_edit_plan",
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
                    "name": "gdsii_edit_plan",
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


def run_openai_gdsii_edit(
    *,
    review_payload: Mapping[str, Any],
    universal_rules_path: str | None,
) -> dict[str, Any]:
    model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    base_url = os.getenv("OPENAI_BASE_URL")
    deterministic_plan = _build_deterministic_gdsii_edit_plan(
        review_payload,
        model=model,
    )
    review_context = review_payload.get("context", {})
    request_metadata = {
        "model": model,
        "base_url": base_url or "https://api.openai.com/v1",
        "candidate_path": review_payload.get("candidate_path"),
        "mappeddir": review_context.get("input_mappeddir"),
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
        or "AI GDSII-stage edit planning was skipped.",
        "warnings": [],
        "request_metadata": request_metadata,
        "raw_response": "",
        "api_error": None,
    }

    if not should_run_gdsii_editor(review_payload):
        base_result["summary"] = (
            "GDSII-stage edit planning was skipped because the reviewer did not classify the failure as GDSII-owned."
        )
        base_result["warnings"] = [
            "GDSII-stage editor skipped because the current failure is not routed to the GDSII stage."
        ]
        return base_result

    if not os.getenv("OPENAI_API_KEY"):
        base_result["warnings"] = [
            "OpenAI GDSII-stage editor skipped because OPENAI_API_KEY is not set."
        ]
        base_result["summary"] = (
            f"{base_result['summary']} AI GDSII-stage edit planning was skipped because no API key was found."
        ).strip()
        return base_result

    rules_excerpt = _load_rules_excerpt(universal_rules_path)
    edit_payload = {
        "candidate_path": review_payload.get("candidate_path"),
        "mappeddir": review_context.get("input_mappeddir"),
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
        "You are a GDSII-stage editor agent for a physical-design export bring-up flow. "
        "You do not decide pass/fail; the reviewer does that. "
        "Your job is to propose only safe, bounded edits to the GDSII-stage inputs so the stage can be retried. "
        "Never fabricate a layout result, invent physical-design data, or hide tool/environment failures. "
        "Allowed edits are limited to bounded text changes in existing GDSII-stage Tcl, metadata, or supporting files already present in the project workspace. "
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
            f"OpenAI GDSII-stage editor failed and no automated edits were applied: {exc}"
        ]
        base_result["summary"] = (
            f"{str(deterministic_plan.get('summary', '')).strip()} "
            "AI GDSII-stage edit planning failed; using deterministic GDSII edit actions only."
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
            f"OpenAI GDSII-stage editor failed and no automated edits were applied: {exc}"
        ]
        base_result["summary"] = (
            f"{str(deterministic_plan.get('summary', '')).strip()} "
            "AI GDSII-stage edit planning failed; using deterministic GDSII edit actions only."
        ).strip()
        base_result["api_error"] = str(exc)
        return base_result
