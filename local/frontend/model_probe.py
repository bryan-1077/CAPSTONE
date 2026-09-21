#!/usr/bin/env python3
"""
model_probe.py

Standalone TAMU model-name probe utility for the dual-LLM flow.

What it does:
- Sends a tiny test request to the TAMU chat proxy for one or more model names
- Reports whether each model name appears accepted by the API
- Prints useful response metadata when present
- Avoids relying on the model to self-identify in natural language

Usage examples:
  python3 model_probe.py --model "protected.gpt-5.1"
  python3 model_probe.py --model "protected.Claude Sonnet 4.5"
  python3 model_probe.py --file model_names.txt
  python3 model_probe.py --default-candidates

Environment:
  export TAMUS_AI_CHAT_API_KEY=...

Notes:
- This is intentionally independent of the main generation flow.
- A 200 response usually means the proxy accepted the model string.
- The most trustworthy signal is API success/failure plus any returned metadata.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Tuple

import requests

API_BASE = "https://chat-api.tamu.ai"
ENDPOINT = API_BASE + "/openai/chat/completions"
DEFAULT_TIMEOUT = 60

DEFAULT_CANDIDATES = [
    "protected.gpt-4o",
    "protected.gpt-4.1",
    "protected.gpt-5",
    "protected.gpt-5.1",
    "protected.gpt-5.2",
    "protected.gpt-5.4",
    "protected.o3",
    "protected.o3-mini",
    "protected.o4-mini",
    "protected.Claude Sonnet 4",
    "protected.Claude Sonnet 4.5",
    "protected.Claude Sonnet 4.6",
    "protected.Claude Sonnet 3.7",
    "protected.Claude Opus 4.1",
    "protected.Claude Opus 4.5",
    "protected.Claude Opus 4.6",
]

SYSTEM_PROMPT = "Reply with exactly: OK"
USER_PROMPT = "OK"


def extract_response_text(data: Dict[str, Any]) -> str:
    """Best-effort extraction of chat text from OpenAI-compatible or similar payloads."""
    try:
        choices = data.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
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
    except Exception:
        pass
    return ""


def extract_model_metadata(data: Dict[str, Any]) -> Dict[str, Any]:
    """Grab a few common metadata fields if present."""
    meta = {}
    for key in ("model", "id", "object", "created", "usage", "system_fingerprint"):
        if key in data:
            meta[key] = data[key]

    try:
        choices = data.get("choices", [])
        if choices and isinstance(choices[0], dict):
            choice0 = choices[0]
            meta["choice_keys"] = list(choice0.keys())
            message = choice0.get("message")
            if isinstance(message, dict):
                meta["message_keys"] = list(message.keys())
    except Exception:
        pass

    return meta


def probe_model(model_name: str, timeout: int = DEFAULT_TIMEOUT) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Probe one model name.
    Returns:
      (success, summary, metadata)
    """
    api_key = os.environ.get("TAMUS_AI_CHAT_API_KEY", "")
    if not api_key:
        raise RuntimeError("TAMUS_AI_CHAT_API_KEY not set. Export it before running this script.")

    payload = {
        "model": model_name,
        "max_tokens": 8,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
    }

    try:
        resp = requests.post(
            ENDPOINT,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        return False, f"Network error: {e}", {}

    if resp.status_code != 200:
        text = (resp.text or "").strip()
        if len(text) > 500:
            text = text[:500] + "..."
        return False, f"HTTP {resp.status_code}: {text}", {}

    try:
        data = resp.json()
    except ValueError as e:
        text = (resp.text or "").strip()
        if len(text) > 500:
            text = text[:500] + "..."
        return False, f"Response JSON parse error: {e}; raw={text}", {}

    text = extract_response_text(data)
    metadata = extract_model_metadata(data)

    summary = "Accepted by API"
    if text:
        summary += f" | text={text!r}"

    return True, summary, metadata


def load_models_from_file(path: str) -> List[str]:
    models = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            models.append(s)
    return models


def unique_preserve_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Standalone probe for TAMU proxy model names.")
    p.add_argument("--model", action="append", default=[], help="Model name to probe. Repeat for multiple.")
    p.add_argument("--file", help="Text file containing one model name per line.")
    p.add_argument("--default-candidates", action="store_true", help="Probe a built-in list of likely model names.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds.")
    p.add_argument("--json", action="store_true", help="Print full results as JSON at the end.")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    models: List[str] = []
    models.extend(args.model)

    if args.file:
        models.extend(load_models_from_file(args.file))

    if args.default_candidates:
        models.extend(DEFAULT_CANDIDATES)

    models = unique_preserve_order([m.strip() for m in models if m.strip()])

    if not models:
        parser.error("No models provided. Use --model, --file, or --default-candidates.")

    print("=" * 72)
    print("TAMU MODEL PROBE")
    print("=" * 72)
    print(f"Endpoint: {ENDPOINT}")
    print(f"Models to test: {len(models)}")
    print()

    all_results = []

    for model in models:
        print("-" * 72)
        print(f"Probing model: {model}")
        ok, summary, metadata = probe_model(model, timeout=args.timeout)

        result = {
            "model": model,
            "ok": ok,
            "summary": summary,
            "metadata": metadata,
        }
        all_results.append(result)

        status = "OK" if ok else "FAIL"
        print(f"Status : {status}")
        print(f"Result : {summary}")
        if metadata:
            print("Metadata:")
            print(json.dumps(metadata, indent=2, sort_keys=True))
        print()

    print("=" * 72)
    passed = sum(1 for r in all_results if r["ok"])
    failed = len(all_results) - passed
    print(f"Done. Passed: {passed} | Failed: {failed}")
    print("=" * 72)

    if args.json:
        print(json.dumps(all_results, indent=2, sort_keys=True))

    return 0 if passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
