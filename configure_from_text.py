#!/usr/bin/env python3
"""Create configs/user_input.yaml from a plain-text controller description."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

from run_flow import (
    SCRIPT_DIR,
    ValidatedUserConfig,
    print_validated_config_summary,
    validate_user_config,
    write_yaml,
)


CONFIG_OUTPUT_PATH = SCRIPT_DIR / "configs" / "user_input.yaml"
TAMU_API_URL = "https://chat-api.tamu.ai/openai/chat/completions"
DEFAULT_LLM_MODEL = "protected.gpt-5.4-nano"
MAX_JSON_ATTEMPTS = 2
MAX_CLARIFICATION_WARNINGS = 3
MAX_CONFIRMATION_REJECTIONS = 2


class ConfigTextError(Exception):
    """Raised for recoverable plain-text configuration errors."""


class FatalConfigTextError(Exception):
    """Raised when retrying the user's wording will not fix the failure."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a plain-text DDR4 controller request into configs/user_input.yaml."
    )
    parser.add_argument(
        "--output",
        default=str(CONFIG_OUTPUT_PATH),
        help="YAML config path to write after confirmation.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_LLM_MODEL,
        help="TAMU chat model name to use for extraction.",
    )
    parser.add_argument(
        "--run-flow",
        action="store_true",
        help="Run run_flow.py with the written config after confirmation.",
    )
    return parser.parse_args()


def collect_user_description(prompt: str) -> str:
    """Collect one plain-text description from stdin."""
    print(prompt)
    print("Press Enter when done.")
    try:
        return input("> ").strip()
    except EOFError:
        print()
        return ""


def call_llm(prompt: str, model: str) -> str:
    """Call the TAMU OpenAI-compatible chat endpoint."""
    api_key = os.environ.get("TAMUS_AI_CHAT_API_KEY")
    if not api_key:
        raise FatalConfigTextError("TAMUS_AI_CHAT_API_KEY is not set.")

    try:
        response = requests.post(
            TAMU_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1200,
                "temperature": 0.0,
                "stream": False,
            },
            timeout=45,
        )
    except requests.exceptions.RequestException as exc:
        raise FatalConfigTextError(f"LLM request failed: {exc}") from exc

    if response.status_code != 200:
        detail = response.text.strip()
        if len(detail) > 300:
            detail = detail[:300] + "..."
        raise FatalConfigTextError(
            f"LLM request failed with HTTP {response.status_code}: {detail}"
        )

    try:
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise FatalConfigTextError(
            "LLM response did not match the expected chat schema."
        ) from exc


def strip_json_fences(text: str) -> str:
    """Remove common markdown wrappers around JSON."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text


def parse_llm_json(text: str) -> dict[str, Any]:
    """Parse the LLM response as a JSON object."""
    try:
        parsed = json.loads(strip_json_fences(text))
    except json.JSONDecodeError as exc:
        raise ConfigTextError(f"Malformed JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigTextError("LLM JSON must be an object.")
    return parsed


def build_extraction_prompt(user_text: str, retry_note: str = "") -> str:
    """Build a strict extraction prompt for one user description."""
    return f"""You extract a DDR4 controller configuration from plain text.

Return ONLY valid JSON. Do not include markdown, comments, or explanation.

Allowed response shapes:
1. Successful extraction:
{{
  "status": "ok",
  "preset_used": "none|slowest|medium|fastest",
  "config": {{
    "design_name": "ddr4_controller",
    "profile": {{"type": "basic"}},
    "memory": {{"protocol": "DDR4", "speed": "2400|3200"}},
    "topology": {{"banks": 1|2|4}},
    "features": {{
      "scheduler": "simple|round_robin",
      "page_policy": "open_page|close_page",
      "refresh": true|false,
      "tFAW": true|false,
      "tRRD": true|false
    }}
  }},
  "assumptions": ["short assumption text"]
}}

2. Needs clarification:
{{
  "status": "needs_clarification",
  "question": "One concise question asking for the missing or unclear DDR4 controller details."
}}

Rules:
- Supported protocol is DDR4 only.
- Supported speeds are 2400 and 3200.
- Supported bank counts are 1, 2, and 4.
- Supported schedulers are simple and round_robin.
- Supported page policies are open_page and close_page.
- Optional boolean features are refresh, tFAW, and tRRD.
- topology.banks greater than 1 currently requires features.scheduler to be simple.
- Treat the user's text as an answer to an existing DDR4 controller configuration prompt.
- A short adjective, rating, vibe, or metaphor can be enough to choose a preset if it has a clear performance, quality, caution, or intensity meaning.
- If the text is random, unrelated, or has no recognizable controller setting or preset intent, return needs_clarification.
- If the text is recognizable but omits fields, use defaults and list them as assumptions.
- Defaults: speed=3200, banks=1, scheduler=simple, page_policy=open_page, refresh=true, tFAW=true, tRRD=true.
- Presets may be inferred from semantic intent, not only exact keywords.
- Categorize expressive or metaphorical requests into the closest preset when the intent is recognizable:
  - slowest: low capability, low complexity, conservative, minimal, cautious, or intentionally slow intent
  - medium: balanced, ordinary, moderate, default-like, or middle-ground intent
  - fastest: high capability, maximum performance, aggressive, premium, powerful, or top-tier intent
- Positive intensity, superiority, heat, power, dominance, or top-quality language should generally map to fastest.
- Slowness, caution, weakness, tiny scale, or intentionally limited language should generally map to slowest.
- Neutral, ordinary, acceptable, or middle-quality language should generally map to medium.
- Use judgment for figurative descriptions, but do not force a preset for words with no clear configuration implication.
- Preset values:
  - slowest: speed=2400, banks=1, scheduler=simple, page_policy=close_page, refresh=true, tFAW=false, tRRD=false
  - medium: speed=3200, banks=2, scheduler=simple, page_policy=open_page, refresh=true, tFAW=true, tRRD=false
  - fastest: speed=3200, banks=4, scheduler=simple, page_policy=open_page, refresh=true, tFAW=true, tRRD=true
- If a preset is used, set preset_used to slowest, medium, or fastest and list that preset inference in assumptions.
- If no preset is used, set preset_used to none.
- Explicit user settings override preset values when valid.
- Do not invent unsupported fields or unsupported values.
{retry_note}

User text:
{user_text}
"""


def extract_config_with_llm(user_text: str, model: str) -> dict[str, Any]:
    """Ask the LLM for config JSON, giving it two chances to produce valid JSON."""
    retry_note = ""
    last_error = ""
    for attempt in range(1, MAX_JSON_ATTEMPTS + 1):
        raw = call_llm(build_extraction_prompt(user_text, retry_note), model)
        try:
            return parse_llm_json(raw)
        except ConfigTextError as exc:
            last_error = str(exc)
            print(f"[WARN] LLM returned malformed JSON on attempt {attempt}.")
            retry_note = (
                "\nPrevious response was not valid JSON. Return exactly one valid JSON "
                "object using one of the allowed response shapes."
            )

    raise FatalConfigTextError(
        f"LLM did not return valid JSON after {MAX_JSON_ATTEMPTS} attempts. {last_error}"
    )


def require_string_list(value: Any, field_name: str) -> list[str]:
    """Return a list of strings or raise a clear error."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigTextError(f"Expected {field_name} to be a list of strings.")
    return value


def normalize_result(
    result: dict[str, Any],
) -> tuple[dict[str, Any], list[str], str, ValidatedUserConfig]:
    """Convert the LLM result into a validated user config."""
    status = result.get("status")
    if status == "needs_clarification":
        question = result.get("question")
        if not isinstance(question, str) or not question.strip():
            question = "I could not identify a valid DDR4 controller configuration. Please revise your request."
        raise ConfigTextError(question.strip())

    if status != "ok":
        raise ConfigTextError("LLM result must use status='ok' or status='needs_clarification'.")

    config = result.get("config")
    if not isinstance(config, dict):
        raise ConfigTextError("LLM status was ok, but config was missing or invalid.")

    preset_used = result.get("preset_used", "none")
    if preset_used not in {"none", "slowest", "medium", "fastest"}:
        raise ConfigTextError("Expected preset_used to be none, slowest, medium, or fastest.")

    assumptions = require_string_list(result.get("assumptions"), "assumptions")
    try:
        validated = validate_user_config(config)
    except ValueError as exc:
        raise ConfigTextError(str(exc)) from exc

    return config, assumptions, preset_used, validated


def prompt_yes_no(message: str) -> bool:
    """Prompt until the user answers yes or no."""
    while True:
        reply = input(message).strip().lower()
        if reply in {"y", "yes"}:
            return True
        if reply in {"n", "no"}:
            return False
        print("Please enter y or n.")


def print_assumptions(assumptions: list[str]) -> None:
    """Print LLM assumptions when present."""
    if not assumptions:
        return
    print("[ASSUMPTIONS]")
    for assumption in assumptions:
        print(f"- {assumption}")


def run_flow(config_path: Path) -> int:
    """Run the existing generation flow with the confirmed config."""
    command = [sys.executable, "run_flow.py", str(config_path)]
    completed = subprocess.run(command, cwd=SCRIPT_DIR, check=False)
    return completed.returncode


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser()
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()

    warning_count = 0
    rejection_count = 0
    prompt = """Describe the DDR4 controller you want.

Supported options:
- Speed: 2400 or 3200
- Banks: 1, 2, or 4
- Scheduler: simple or round_robin
- Page policy: open_page or close_page
- Optional features: refresh, tFAW, tRRD
"""

    while warning_count < MAX_CLARIFICATION_WARNINGS:
        user_text = collect_user_description(prompt)
        if not user_text:
            print("[ERROR] No description provided.")
            return 1

        print("[INFO] Loading...")
        try:
            result = extract_config_with_llm(user_text, args.model)
            config, assumptions, preset_used, validated = normalize_result(result)
        except FatalConfigTextError as exc:
            print(f"[ERROR] {exc}")
            print("[ERROR] No file written.")
            return 1
        except ConfigTextError as exc:
            warning_count += 1
            remaining = MAX_CLARIFICATION_WARNINGS - warning_count
            print(f"[WARN] {exc}")
            if remaining == 0:
                print("[ERROR] Too many unclear or invalid configuration attempts. No file written.")
                return 1
            print(f"[INFO] Please try again. Attempts remaining: {remaining}")
            prompt = "Revise the DDR4 controller request with the missing or corrected details."
            continue

        print()
        print("[INTERPRETED CONFIG]")
        print_validated_config_summary(validated)
        print_assumptions(assumptions)
        print()
        if preset_used != "none":
            print("Trying to interpret your request, is this something you're happy with?")

        if not prompt_yes_no(f"Write this configuration to {output_path}? (y/n): "):
            rejection_count += 1
            remaining = MAX_CONFIRMATION_REJECTIONS - rejection_count
            if remaining == 0:
                print(
                    "[ERROR] Unable to satisfy your request, exiting program. "
                    "Please run again if you make up your mind."
                )
                print("[ERROR] No file written.")
                return 1
            print(f"[INFO] No file written. Please revise your request. Attempts remaining: {remaining}")
            prompt = "Describe what you want changed in the DDR4 controller configuration."
            continue

        write_yaml(output_path, config)
        print(f"[CONFIG] Wrote {output_path}")

        if args.run_flow:
            return run_flow(output_path)
        return 0

    print("[ERROR] Too many unclear or invalid configuration attempts. No file written.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
