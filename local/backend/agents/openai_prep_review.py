from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_OPENAI_MODEL = "gpt-5.2"
MAX_RULES_CHARS = 12000
DEFAULT_OPENAI_TIMEOUT_SEC = 120.0

AI_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "errors": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "false_positive_dependencies": {
            "type": "array",
            "items": {"type": "string"},
        },
        "suggested_fixes": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "pass",
        "errors",
        "warnings",
        "false_positive_dependencies",
        "suggested_fixes",
        "summary",
    ],
    "additionalProperties": False,
}


class AIReviewParseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        raw_response: str = "",
        extracted_text: str = "",
    ) -> None:
        super().__init__(message)
        self.raw_response = raw_response
        self.extracted_text = extracted_text


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _serialize_response(response: Any) -> str:
    if hasattr(response, "model_dump_json"):
        return response.model_dump_json(indent=2)
    if hasattr(response, "model_dump"):
        return json.dumps(response.model_dump(), indent=2, default=str)
    if hasattr(response, "to_dict"):
        return json.dumps(response.to_dict(), indent=2, default=str)
    return str(response)


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_text(text: str) -> str:
    cleaned = _strip_code_fences(text).strip()
    if not cleaned:
        raise ValueError("AI review returned empty text.")

    try:
        json.loads(cleaned)
        return cleaned
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError("AI review did not contain a JSON object.")

    candidate = match.group(0).strip()
    json.loads(candidate)
    return candidate


def _extract_streamed_chat_text(raw_response: str) -> str:
    if not raw_response.strip():
        return ""

    chunks: list[str] = []
    for line in raw_response.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue

        payload = stripped[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue

        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue

        for choice in event.get("choices", []):
            delta = choice.get("delta", {})
            content = delta.get("content")
            if isinstance(content, str):
                chunks.append(content)
                continue

            if isinstance(content, list):
                for part in content:
                    if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])

    return "".join(chunks).strip()


def _openai_timeout_seconds() -> float:
    raw_value = str(os.getenv("OPENAI_TIMEOUT_SEC", "")).strip()
    if not raw_value:
        return DEFAULT_OPENAI_TIMEOUT_SEC
    try:
        parsed = float(raw_value)
    except ValueError:
        return DEFAULT_OPENAI_TIMEOUT_SEC
    return parsed if parsed > 0 else DEFAULT_OPENAI_TIMEOUT_SEC


def _build_client() -> Any:
    from openai import OpenAI

    base_url = os.getenv("OPENAI_BASE_URL")
    timeout = _openai_timeout_seconds()
    if base_url:
        return OpenAI(base_url=base_url, timeout=timeout)
    return OpenAI(timeout=timeout)


def _extract_output_text(response: Any) -> str:
    output_text = getattr(response, "output_text", "")
    if output_text:
        return str(output_text)

    output = getattr(response, "output", None)
    if not output:
        return ""

    chunks: list[str] = []
    for item in output:
        content = getattr(item, "content", None)
        if not content:
            continue
        for part in content:
            text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))
    return "\n".join(chunks)


def _extract_chat_output_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        return ""

    message = getattr(choices[0], "message", None)
    if message is None:
        return ""

    parsed = getattr(message, "parsed", None)
    if isinstance(parsed, Mapping):
        return json.dumps(parsed)

    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content

    if not content:
        return ""

    chunks: list[str] = []
    for part in content:
        text = getattr(part, "text", None)
        if text:
            chunks.append(str(text))
            continue
        if isinstance(part, Mapping) and "text" in part:
            chunks.append(str(part["text"]))
            continue
        if isinstance(part, Mapping):
            inner_text = part.get("text")
            if isinstance(inner_text, Mapping) and "value" in inner_text:
                chunks.append(str(inner_text["value"]))
    return "\n".join(chunks)


def _load_rules_excerpt(rules_path: str | None) -> str:
    if not rules_path:
        return ""

    try:
        text = Path(rules_path).read_text(encoding="utf-8")
    except OSError:
        return ""

    return text[:MAX_RULES_CHARS]


def _normalize_ai_result(result: Mapping[str, Any], *, model: str) -> dict[str, Any]:
    errors = _ordered_unique(result.get("errors", []))
    warnings = _ordered_unique(result.get("warnings", []))
    false_positive_dependencies = _ordered_unique(
        result.get("false_positive_dependencies", [])
    )
    suggested_fixes = _ordered_unique(result.get("suggested_fixes", []))

    return {
        "enabled": True,
        "available": True,
        "model": model,
        "pass": not errors,
        "errors": errors,
        "warnings": warnings,
        "false_positive_dependencies": false_positive_dependencies,
        "suggested_fixes": suggested_fixes,
        "summary": str(result.get("summary", "")).strip() or "AI review completed.",
    }


PREP_BLOCKING_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bfilelist_genus\.f\b",
        r"\brun_genus_mc\.tcl\b",
        r"\btop_module\.txt\b",
        r"\bconstraints/mc_genus\.sdc\b",
        r"\bmissing required prep artifact\b",
        r"\bprep artifact is empty\b",
        r"\bmissing the filelist dependency\b",
        r"\bmissing definitions?\b",
        r"\bno module definition\b",
        r"\btestbench-like\b",
        r"\bunit[_\-. ]?test\b",
        r"\bsvunit\b",
        r"\bnon-rtl entry\b",
        r"\bfpga-only\b",
        r"\btop module\b",
        r"\bmacro definitions?\b",
        r"\bdefine(?:s)?\b",
        r"\bmissing package\b",
        r"\bmissing interface\b",
        r"\bbad prep content\b",
        r"\bgeneric interface\b",
        r"\bunsupported synthesis top\b",
        r"\bsynthesis-safe wrapper\b",
    ]
]

NETLIST_OWNED_SYNTHESIS_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bnon-synthesizable\b",
        r"\bunsupported systemverilog\b",
        r"\btri-state\b",
        r"'z\b",
        r"\balways_ff\b",
        r"\bdual-edge\b",
        r"\bdual-clock\b",
        r"\blatch inference\b",
        r"\balways_latch\b",
        r"\bqor\b",
        r"\btiming\b",
        r"\bclocking/constraints mismatch\b",
        r"\bclock groups?\b",
        r"\bgenerated_clock\b",
        r"\basync clock\b",
        r"\binout-style\b",
        r"\bbidirectional\b",
        r"\bpad-ring\b",
        r"\bi/o-cell\b",
        r"\bassociative array\b",
        r"\bbehavioral\b",
        r"\bbfm\b",
        r"\bphy wrapper\b",
        r"\bwrapper top\b",
        r"\bstandard-cell\b",
        r"\bgenus may\b",
        r"\bgenus (?:elaboration|compile|synthesis)\b",
        r"\belaboration may\b",
        r"\belaboration/compile\b",
        r"\bcompile/elaboration\b",
        r"\bcompile errors?\b",
        r"\bmultiple drivers?\b",
        r"\bconflicting continuous assignments?\b",
        r"\bundriven\b",
        r"\bsynthesis readiness\b",
    ]
]

SPECULATIVE_AI_PREP_BLOCKING_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bblocking risk\b",
        r"\bhigh-risk\b",
        r"\bcan become\b",
        r"\bmay\b",
        r"\blikely to\b",
        r"\bcommonly hard-stop\b",
        r"\bconfirm\b",
        r"\bre-verify\b",
        r"\bif\b.*\botherwise\b",
        r"\bif\b.*\bis fine\b",
        r"\bdepending on the exact code\b",
    ]
]


def _is_prep_blocking_issue(text: str) -> bool:
    message = str(text).strip()
    if not message:
        return False
    return any(pattern.search(message) for pattern in PREP_BLOCKING_PATTERNS)


def _is_generic_interface_top_issue(text: str) -> bool:
    message = str(text).strip()
    if not message:
        return False
    patterns = [
        r"\bgeneric interface\b",
        r"\braw interface ports?\b",
        r"\bselected top\b.*\binterface\b",
        r"\bunsupported synthesis top\b",
    ]
    return any(re.search(pattern, message, re.IGNORECASE) for pattern in patterns)


def _is_netlist_owned_synthesis_issue(text: str) -> bool:
    message = str(text).strip()
    if not message:
        return False
    if _is_prep_blocking_issue(message):
        return False
    return any(pattern.search(message) for pattern in NETLIST_OWNED_SYNTHESIS_PATTERNS)


def _is_speculative_ai_prep_blocker(text: str) -> bool:
    message = str(text).strip()
    if not message:
        return False
    return any(pattern.search(message) for pattern in SPECULATIVE_AI_PREP_BLOCKING_PATTERNS)


def _downgrade_prep_ai_errors(
    ai_errors: Iterable[str],
    ai_warnings: Iterable[str],
    ai_fixes: Iterable[str],
    *,
    local_review_passed: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    kept_errors: list[str] = []
    warnings = _ordered_unique(ai_warnings)
    original_warning_count = len(warnings)
    fixes = _ordered_unique(ai_fixes)

    for error in ai_errors:
        message = str(error).strip()
        if not message:
            continue
        if _is_generic_interface_top_issue(message):
            kept_errors.append(message)
            continue
        if _is_netlist_owned_synthesis_issue(message):
            warnings.append(
                "Prep AI review noted a downstream synthesis concern "
                f"(non-blocking at prep): {message}"
            )
            continue
        if local_review_passed and _is_speculative_ai_prep_blocker(message):
            warnings.append(
                "Prep AI review raised a speculative prep concern after deterministic "
                f"local validation passed (non-blocking at prep): {message}"
            )
            continue
        kept_errors.append(message)

    if len(warnings) > original_warning_count:
        fixes = _ordered_unique(
            [
                *fixes,
                "Let netlist/Genus review own synthesis-semantic issues after prep contract checks pass.",
            ]
        )

    return _ordered_unique(kept_errors), _ordered_unique(warnings), fixes


LOCAL_PREP_DEPENDENCY_RE = re.compile(
    r"Prepared RTL instantiates module '([^']+)' in '([^']+)'",
    re.IGNORECASE,
)


def _extract_local_dependency_token(message: str) -> str | None:
    match = LOCAL_PREP_DEPENDENCY_RE.search(str(message))
    if not match:
        return None
    token = match.group(1).strip()
    return token or None


def _downgrade_local_false_positive_dependencies(
    local_errors: Iterable[str],
    prep_dependency_errors: Iterable[str],
    local_warnings: Iterable[str],
    ai_false_positive_dependencies: Iterable[str],
) -> tuple[list[str], list[str], list[str]]:
    false_positive_names = {
        str(item).strip()
        for item in ai_false_positive_dependencies
        if str(item).strip()
    }
    kept_errors: list[str] = []
    kept_dependency_errors: list[str] = []
    warnings = _ordered_unique(local_warnings)

    for message in local_errors:
        token = _extract_local_dependency_token(str(message))
        if token and token in false_positive_names:
            warnings.append(
                "Prep review downgraded a likely parser false-positive dependency "
                f"for token '{token}'."
            )
            continue
        kept_errors.append(str(message).strip())

    for message in prep_dependency_errors:
        token = _extract_local_dependency_token(str(message))
        if token and token in false_positive_names:
            continue
        kept_dependency_errors.append(str(message).strip())

    return (
        _ordered_unique(kept_errors),
        _ordered_unique(kept_dependency_errors),
        _ordered_unique(warnings),
    )


def _downgrade_non_selected_top_interface_issues(
    errors: Iterable[str],
    warnings: Iterable[str],
    synthesis_top_checks: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    if bool(synthesis_top_checks.get("selected_top_has_generic_interface_ports")):
        return _ordered_unique(errors), _ordered_unique(warnings)

    selected_top = str(synthesis_top_checks.get("selected_top_module", "")).strip()
    kept_errors: list[str] = []
    merged_warnings = _ordered_unique(warnings)

    for error in errors:
        message = str(error).strip()
        if not message:
            continue
        if _is_generic_interface_top_issue(message):
            merged_warnings.append(
                "Prep review noted raw interface usage on non-selected modules, but the current "
                f"selected top '{selected_top or 'unknown'}' does not expose raw generic interface ports. "
                "Treating this as context only, not a prep blocker."
            )
            merged_warnings.append(message)
            continue
        kept_errors.append(message)

    return _ordered_unique(kept_errors), _ordered_unique(merged_warnings)


def _is_not_found_error(exc: Exception) -> bool:
    text = str(exc)
    return "404" in text or "Not Found" in text


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
                "name": "rtl_prep_ai_review",
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
                "name": "rtl_prep_ai_review",
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


def run_openai_prep_review(
    *,
    local_review: Mapping[str, Any],
    universal_rules_path: str | None,
) -> dict[str, Any]:
    model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    base_url = os.getenv("OPENAI_BASE_URL")
    request_metadata = {
        "model": model,
        "base_url": base_url or "https://api.openai.com/v1",
        "candidate_path": local_review.get("candidate_path"),
        "local_error_count": len(local_review.get("errors", [])),
        "local_warning_count": len(local_review.get("warnings", [])),
        "inventory_file_count": local_review.get("context", {})
        .get("inventory", {})
        .get("total_files", 0),
        "rtl_sample_count": len(local_review.get("context", {}).get("rtl_samples", [])),
    }

    base_result = {
        "enabled": False,
        "available": False,
        "model": model,
        "pass": True,
        "errors": [],
        "warnings": [],
        "false_positive_dependencies": [],
        "suggested_fixes": [],
        "summary": "AI prep review was skipped.",
        "request_metadata": request_metadata,
        "raw_response": "",
        "api_error": None,
    }

    if not os.getenv("OPENAI_API_KEY"):
        base_result["warnings"] = [
            "OpenAI prep review skipped because OPENAI_API_KEY is not set."
        ]
        base_result["summary"] = "AI prep review was skipped because no API key was found."
        return base_result

    try:
        from openai import OpenAI
    except ImportError as exc:
        base_result["warnings"] = [
            "OpenAI prep review skipped because the openai Python package is not installed."
        ]
        base_result["summary"] = "AI prep review was skipped because the OpenAI SDK is unavailable."
        base_result["api_error"] = str(exc)
        return base_result

    rules_excerpt = _load_rules_excerpt(universal_rules_path)
    review_payload = {
        "candidate_path": local_review.get("candidate_path"),
        "local_review": {
            "pass": bool(local_review.get("pass")),
            "errors": local_review.get("errors", []),
            "warnings": local_review.get("warnings", []),
            "suggested_fixes": local_review.get("suggested_fixes", []),
            "summary": local_review.get("summary", ""),
        },
        "review_context": local_review.get("context", {}),
        "rules_excerpt": rules_excerpt,
    }

    system_prompt = (
        "You are reviewing a prepared RTL synthesis bundle for Cadence Genus bring-up. "
        "Inspect the provided artifacts and snippets for prep-contract completeness, internal "
        "consistency, and likely missing prep edits. "
        "Prep stage owns bundle completeness, filelist/Tcl/SDC/top consistency, missing "
        "dependencies, stray verification files, and explicit prep artifacts. "
        "A selected top that looks like a testbench or simulation harness based on its contents "
        "(for example: no ports, initial blocks, delay-based clock generation, BFM harness behavior, "
        "or $finish/$display-heavy flow control) is a hard prep-stage blocker. "
        "In that case, recommend the likelier RTL design top or require an explicit top override; "
        "do not treat the harness module as the synthesis top just because it sits at the outer boundary. "
        "Generic interface ports on the selected synthesis top are a hard prep-stage blocker: "
        "if the chosen top exposes raw 'interface ...' ports at the module boundary, report that "
        "as a blocking prep error and recommend a synthesis-safe wrapper or explicit top override. "
        "If review_context.synthesis_top_checks shows that deterministic modport adaptation is safely inferable "
        "from trusted instantiation evidence, prefer a generated synthesis-only top copy inside the prepared build "
        "over rewriting the original RTL file. "
        "If review_context.synthesis_top_checks lists applicable_recipe_ids or a chosen_recipe_id, "
        "treat those approved recipes as the preferred bounded adaptation path. "
        "Do not invent a new synthesis rewrite when no approved recipe exists. "
        "Inspect the selected top name, its port declarations, and any nearby interface usage context "
        "before deciding whether this generic-interface-top condition applies. "
        "If raw interface ports are only present on internal non-selected modules while the selected "
        "top is a wrapper that avoids them at the boundary, report that as context or a warning, not "
        "as a prep-blocking generic_interface_top failure. "
        "If review_context.iteration_context is present, use it to distinguish persistent blockers "
        "from newly introduced issues and to avoid re-raising already-resolved prep problems without evidence. "
        "Do not recommend rewriting architecture to flatten or fake those interfaces. "
        "Prep stage does not own downstream ASIC/Genus synthesis-semantic issues such as "
        "unsupported RTL constructs, tri-state behavior, dual-edge flop modeling, latch-heavy "
        "logic, QoR concerns, or broader synthesis readiness judgments when the prep contract is "
        "otherwise intact. "
        "Known DDR behavioral transport patterns, tri-state-heavy bus models, and interface-heavy communication wrappers "
        "should only be treated as prep-stage blockers when the review context indicates an approved build-only recipe "
        "or when no approved recipe exists and the flow must stop cleanly for manual recipe creation. "
        "If a reported missing-module or unresolved dependency item looks like parser confusion "
        "rather than a real module instantiation, do not report it as an error. "
        "Instead, add the exact suspicious token name to false_positive_dependencies and explain "
        "the suspicion in warnings. Examples include built-in SystemVerilog types, typedef names, "
        "enum/state aliases, packed-struct field types, and task/function return types. "
        "Report those downstream synthesis concerns as warnings and suggested fixes, not prep "
        "errors. "
        "Return only structured JSON that matches the requested schema. "
        "Use warnings for soft issues and errors only for issues that should block the prep stage."
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
            f"OpenAI prep review failed and local validation was used instead: {exc}"
        ]
        base_result["summary"] = "AI prep review failed; using deterministic local review only."
        base_result["api_error"] = str(exc)
        base_result["raw_response"] = exc.raw_response
        base_result["request_metadata"] = {
            **request_metadata,
            "last_extracted_text": exc.extracted_text[:4000],
        }
        return base_result
    except Exception as exc:
        base_result["warnings"] = [
            f"OpenAI prep review failed and local validation was used instead: {exc}"
        ]
        base_result["summary"] = "AI prep review failed; using deterministic local review only."
        base_result["api_error"] = str(exc)
        base_result["raw_response"] = locals().get("raw_response", "")
        return base_result


def merge_prep_review_results(
    *,
    local_review: Mapping[str, Any],
    ai_review: Mapping[str, Any],
) -> dict[str, Any]:
    local_errors = _ordered_unique(local_review.get("errors", []))
    local_warnings = _ordered_unique(local_review.get("warnings", []))
    local_fixes = _ordered_unique(local_review.get("suggested_fixes", []))
    prep_dependency_errors = _ordered_unique(local_review.get("prep_dependency_errors", []))

    ai_false_positive_dependencies = _ordered_unique(
        ai_review.get("false_positive_dependencies", [])
    )
    local_errors, prep_dependency_errors, local_warnings = (
        _downgrade_local_false_positive_dependencies(
            local_errors,
            prep_dependency_errors,
            local_warnings,
            ai_false_positive_dependencies,
        )
    )

    synthesis_top_checks = (
        local_review.get("context", {}).get("synthesis_top_checks", {})
        if isinstance(local_review.get("context", {}), Mapping)
        else {}
    )
    local_errors, local_warnings = _downgrade_non_selected_top_interface_issues(
        local_errors,
        local_warnings,
        synthesis_top_checks,
    )

    ai_errors, ai_warnings, ai_fixes = _downgrade_prep_ai_errors(
        ai_review.get("errors", []),
        ai_review.get("warnings", []),
        ai_review.get("suggested_fixes", []),
        local_review_passed=not local_errors and not prep_dependency_errors,
    )
    ai_errors, ai_warnings = _downgrade_non_selected_top_interface_issues(
        ai_errors,
        ai_warnings,
        synthesis_top_checks,
    )

    merged_errors = _ordered_unique([*local_errors, *ai_errors])
    merged_warnings = _ordered_unique([*local_warnings, *ai_warnings])
    merged_fixes = _ordered_unique([*local_fixes, *ai_fixes])
    selected_top_has_generic_interfaces = bool(
        synthesis_top_checks.get("selected_top_has_generic_interface_ports")
    )
    selected_top_is_testbench = bool(
        synthesis_top_checks.get("selected_top_likely_testbench_or_harness")
    )
    detected_patterns = synthesis_top_checks.get("detected_patterns", [])
    applicable_recipe_ids = synthesis_top_checks.get("applicable_recipe_ids", [])
    chosen_recipe_id = synthesis_top_checks.get("chosen_recipe_id")
    ambiguity_reason = synthesis_top_checks.get("ambiguity_reason")
    if selected_top_has_generic_interfaces or any(
        _is_generic_interface_top_issue(item) for item in merged_errors
    ):
        failure_stage_owner = "prep"
        failure_class = "generic_interface_top"
        route_back_to_stage = "rtl_prep"
    elif selected_top_is_testbench:
        failure_stage_owner = "prep"
        failure_class = "unsupported_synthesis_top"
        route_back_to_stage = "rtl_prep"
    elif str(local_review.get("failure_class", "")).strip() == "unsupported_synthesis_recipe_needed":
        failure_stage_owner = "prep"
        failure_class = "unsupported_synthesis_recipe_needed"
        route_back_to_stage = "rtl_prep"
    elif prep_dependency_errors or any(_is_prep_blocking_issue(item) for item in merged_errors):
        failure_stage_owner = "prep"
        failure_class = "prep_bundle_completeness_problem"
        route_back_to_stage = "rtl_prep"
    else:
        failure_stage_owner = None
        failure_class = None
        route_back_to_stage = None

    local_summary = str(local_review.get("summary", "")).strip()
    ai_summary = str(ai_review.get("summary", "")).strip()
    summary_parts = [part for part in [local_summary, ai_summary] if part]

    return {
        "pass": not merged_errors,
        "errors": merged_errors,
        "warnings": merged_warnings,
        "suggested_fixes": merged_fixes,
        "prep_dependency_errors": prep_dependency_errors,
        "failure_stage_owner": failure_stage_owner,
        "failure_class": failure_class,
        "route_back_to_stage": route_back_to_stage,
        "subclass": (
            "raw_generic_interface_top"
            if failure_class == "generic_interface_top"
            else None
        ),
        "detected_patterns": detected_patterns if isinstance(detected_patterns, list) else [],
        "applicable_recipe_ids": [
            str(item) for item in applicable_recipe_ids if str(item).strip()
        ]
        if isinstance(applicable_recipe_ids, list)
        else [],
        "chosen_recipe_id": (
            str(chosen_recipe_id).strip() if str(chosen_recipe_id).strip() else None
        ),
        "ambiguity_reason": (
            str(ambiguity_reason).strip() if str(ambiguity_reason).strip() else None
        ),
        "summary": " ".join(summary_parts).strip()
        or "RTL prep review completed.",
        "candidate_path": local_review.get("candidate_path"),
        "rules_path": local_review.get("rules_path"),
        "context": local_review.get("context", {}),
        "local_review": {
            "pass": not local_errors,
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
            "false_positive_dependencies": ai_false_positive_dependencies,
            "suggested_fixes": ai_fixes,
            "summary": ai_summary,
            "request_metadata": ai_review.get("request_metadata", {}),
            "api_error": ai_review.get("api_error"),
        },
    }
