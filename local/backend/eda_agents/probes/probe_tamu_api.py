#!/usr/bin/env python3
"""Probe a TAMU or OpenAI-compatible API endpoint.

This script checks whether a provider exposes endpoints that are usable for
an agent loop such as:

- list models
- create a chat completion
- create a responses API call

Environment variables:
- TAMU_CHAT_API_KEY or TAMU_API_KEY or OPENAI_API_KEY
- TAMU_CHAT_BASE_URL or TAMU_BASE_URL or OPENAI_BASE_URL
- TAMU_MODEL or OPENAI_MODEL (optional)

Example:
    export TAMU_CHAT_API_KEY="sk-5313881325b54fafbd4cd59417bedf56"
    export TAMU_CHAT_BASE_URL="https://chat-api.tamu.ai/openai"
    export TAMU_MODEL="protected.o3"
    python3 probe_tamu_api.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import requests


DEFAULT_TIMEOUT = 30


@dataclass
class ProbeResult:
    name: str
    ok: bool
    status: int | None
    detail: str
    payload: dict[str, Any] | None = None


def _env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _request(
    *,
    method: str,
    url: str,
    api_key: str,
    timeout: int,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    if json_body is not None:
        headers["Content-Type"] = "application/json"

    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=json_body,
            timeout=timeout,
        )
        raw = response.text
        try:
            parsed = response.json() if raw else None
        except (json.JSONDecodeError, ValueError):
            parsed = None
        return response.status_code, parsed, raw
    except requests.RequestException as exc:
        return 0, None, str(exc)


def _truncate(text: str, limit: int = 300) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _extract_sse_json_objects(raw: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue

        data_part = line[5:].strip()
        if not data_part or data_part == "[DONE]":
            continue

        try:
            parsed = json.loads(data_part)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, dict):
            objects.append(parsed)

    return objects


def probe_models(base_url: str, api_key: str, timeout: int) -> ProbeResult:
    status, payload, raw = _request(
        method="GET",
        url=f"{base_url}/models",
        api_key=api_key,
        timeout=timeout,
    )

    if 200 <= status < 300 and isinstance(payload, dict):
        models = payload.get("data", [])
        model_ids = [
            item.get("id")
            for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        preview = ", ".join(model_ids[:10]) if model_ids else "no model ids returned"
        return ProbeResult(
            name="models",
            ok=True,
            status=status,
            detail=f"Listed models successfully: {preview}",
            payload=payload,
        )

    return ProbeResult(
        name="models",
        ok=False,
        status=status,
        detail=f"Model listing failed: {_truncate(raw)}",
        payload=payload,
    )


def probe_chat_completions(
    base_url: str,
    api_key: str,
    model: str,
    timeout: int,
) -> ProbeResult:
    status, payload, raw = _request(
        method="POST",
        url=f"{base_url}/chat/completions",
        api_key=api_key,
        timeout=timeout,
        json_body={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "Reply with the single word: ok",
                },
                {
                    "role": "user",
                    "content": "Return exactly one word.",
                },
            ],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
        },
    )

    if 200 <= status < 300 and isinstance(payload, dict):
        choices = payload.get("choices", [])
        if choices:
            first = choices[0]
            message = first.get("message", {}) if isinstance(first, dict) else {}
            content = message.get("content", "") if isinstance(message, dict) else ""
            return ProbeResult(
                name="chat_completions",
                ok=True,
                status=status,
                detail=f"Chat completions worked. Sample response: {_truncate(str(content), 80)}",
                payload=payload,
            )
        return ProbeResult(
            name="chat_completions",
            ok=True,
            status=status,
            detail="Chat completions endpoint responded, but no choices were returned.",
            payload=payload,
        )

    if 200 <= status < 300:
        sse_objects = _extract_sse_json_objects(raw)
        if sse_objects:
            sample_text_parts: list[str] = []

            for obj in sse_objects:
                choices = obj.get("choices", [])
                if not isinstance(choices, list):
                    continue
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta", {})
                    if isinstance(delta, dict):
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            sample_text_parts.append(content)

            sample_text = "".join(sample_text_parts).strip()
            detail = "Chat completions worked via SSE streaming response."
            if sample_text:
                detail += f" Sample response: {_truncate(sample_text, 80)}"

            return ProbeResult(
                name="chat_completions",
                ok=True,
                status=status,
                detail=detail,
                payload=sse_objects[0],
            )

    return ProbeResult(
        name="chat_completions",
        ok=False,
        status=status,
        detail=f"Chat completions failed: {_truncate(raw)}",
        payload=payload,
    )


def _extract_responses_text(payload: dict[str, Any]) -> str:
    output = payload.get("output", [])
    fragments: list[str] = []

    if not isinstance(output, list):
        return ""

    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                fragments.append(text)

    return " ".join(fragments).strip()


def probe_responses(
    base_url: str,
    api_key: str,
    model: str,
    timeout: int,
) -> ProbeResult:
    status, payload, raw = _request(
        method="POST",
        url=f"{base_url}/responses",
        api_key=api_key,
        timeout=timeout,
        json_body={
            "model": model,
            "input": "Reply with the single word: ok",
            "max_output_tokens": 8,
        },
    )

    if 200 <= status < 300 and isinstance(payload, dict):
        response_id = payload.get("id")
        sample_text = _extract_responses_text(payload)
        detail_parts = [f"Responses API worked. id={response_id}"]
        if sample_text:
            detail_parts.append(f"Sample response: {_truncate(sample_text, 80)}")
        return ProbeResult(
            name="responses",
            ok=True,
            status=status,
            detail=" ".join(detail_parts),
            payload=payload,
        )

    return ProbeResult(
        name="responses",
        ok=False,
        status=status,
        detail=f"Responses API failed: {_truncate(raw)}",
        payload=payload,
    )


def choose_model(models_result: ProbeResult, requested_model: str | None) -> str | None:
    if requested_model:
        return requested_model

    payload = models_result.payload or {}
    models = payload.get("data", [])
    preferred_prefixes = (
        "protected.o3",
        "protected.gpt-5",
        "protected.gpt-4.1",
        "protected.gpt-4o",
        "gpt-5",
        "gpt-4.1",
    )

    if isinstance(models, list):
        model_ids = [
            item.get("id")
            for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        for prefix in preferred_prefixes:
            for model_id in model_ids:
                if model_id.startswith(prefix):
                    return model_id
        if model_ids:
            return model_ids[0]

    return None


def print_result(result: ProbeResult) -> None:
    status = "PASS" if result.ok else "FAIL"
    code = result.status if result.status is not None else "n/a"
    print(f"[{status}] {result.name} (HTTP {code})")
    print(f"  {result.detail}")


def _explicit_model_flag_present(argv: list[str]) -> bool:
    for arg in argv:
        if arg == "--model" or arg.startswith("--model="):
            return True
    return False


def _looks_like_cloudflare_1010(result: ProbeResult) -> bool:
    if result.status != 403:
        return False

    detail = result.detail.lower()
    raw = ""
    if result.payload is not None:
        raw = json.dumps(result.payload).lower()
    combined = f"{detail} {raw}"
    return "cloudflare" in combined or "error 1010" in combined or "access denied" in combined


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe a TAMU/OpenAI-compatible API.")
    parser.add_argument(
        "--base-url",
        default=_env("TAMU_CHAT_BASE_URL", "TAMU_BASE_URL", "OPENAI_BASE_URL")
        or "https://chat-api.tamu.ai/openai",
        help="Provider base URL. Example: https://chat-api.tamu.ai/openai",
    )
    parser.add_argument(
        "--api-key",
        default=_env("TAMU_CHAT_API_KEY", "TAMU_API_KEY", "OPENAI_API_KEY"),
        help="Bearer API key.",
    )
    parser.add_argument(
        "--model",
        default=_env("TAMU_MODEL", "OPENAI_MODEL"),
        help="Model name to probe. If omitted, the script tries to infer one from /models.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="HTTP timeout in seconds.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    explicit_model = _explicit_model_flag_present(sys.argv[1:])

    if not args.base_url:
        print("Missing base URL. Set TAMU_CHAT_BASE_URL or pass --base-url.", file=sys.stderr)
        return 2

    if not args.api_key:
        print("Missing API key. Set TAMU_CHAT_API_KEY or pass --api-key.", file=sys.stderr)
        return 2

    base_url = _normalize_base_url(args.base_url)

    print("Probing provider")
    print(f"  base_url: {base_url}")
    print(f"  requested_model: {args.model or '<auto>'}")

    models_result = probe_models(base_url, args.api_key, args.timeout)
    print_result(models_result)

    model: str | None = None
    if explicit_model:
        model = args.model
    elif models_result.ok:
        model = choose_model(models_result, args.model)

    if not model:
        print("[FAIL] model_selection (HTTP n/a)")
        print("  Could not determine a model to test. Pass --model explicitly.")
        if _looks_like_cloudflare_1010(models_result):
            print("  Note: /models appears blocked before normal API processing.")
            print("  Chat may still work when tested with an explicit model.")
            print("  Next recommended test: python3 probe_tamu_api.py --model protected.o3")
        return 1

    print(f"  selected_model: {model}")

    chat_result = probe_chat_completions(base_url, args.api_key, model, args.timeout)
    print_result(chat_result)

    responses_result = probe_responses(base_url, args.api_key, model, args.timeout)
    print_result(responses_result)

    print("\nSummary")
    if (models_result.ok or explicit_model) and (chat_result.ok or responses_result.ok):
        print("  This provider looks usable for your agent loop.")
        if chat_result.ok:
            print("  Chat Completions is available, which is the main TAMU compatibility check.")
        elif responses_result.ok:
            print("  Responses API is available even though Chat Completions did not succeed.")
        if responses_result.ok:
            print("  Responses API is also available, so separate review/prep sessions are straightforward.")
        else:
            print("  Responses may be unavailable on TAMU-compatible providers, and that is not necessarily a blocker.")
        return 0

    print("  This provider does not yet look ready for the full workflow.")
    print("  Chat Completions is the main compatibility signal; Responses may be unavailable on TAMU-compatible providers.")
    if _looks_like_cloudflare_1010(models_result):
        print("  Note: /models appears blocked before normal API processing.")
        print("  Chat may still work when tested with an explicit model.")
        print("  Next recommended test: python3 probe_tamu_api.py --model protected.o3")
    print("  Check the base URL, model name, key scope, and whether the provider exposes OpenAI-compatible endpoints.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())