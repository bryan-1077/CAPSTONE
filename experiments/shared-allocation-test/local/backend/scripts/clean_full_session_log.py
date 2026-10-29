from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parse_full_session_log import load_flow_state, split_live_transcript_and_state


TIMESTAMPED_LINE_RE = re.compile(r"^\[(?P<timestamp>[^\]]+)\]\s*(?P<message>.*)$")


def _timestamp_lookup(live_lines: list[str]) -> dict[str, deque[str]]:
    lookup: dict[str, deque[str]] = defaultdict(deque)
    for line in live_lines:
        match = TIMESTAMPED_LINE_RE.match(line.strip())
        if not match:
            continue
        message = match.group("message").strip()
        timestamp = match.group("timestamp").strip()
        if message:
            lookup[message].append(timestamp)
    return lookup


def _dedupe_messages(messages: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for message in messages:
        item = str(message).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _clean_lines(live_lines: list[str], history: list[str]) -> list[str]:
    lookup = _timestamp_lookup(live_lines)
    messages = _dedupe_messages(history)
    if not messages:
        messages = _dedupe_messages(
            match.group("message").strip()
            for line in live_lines
            if (match := TIMESTAMPED_LINE_RE.match(line.strip()))
        )

    cleaned = []
    for message in messages:
        timestamps = lookup.get(message)
        timestamp = timestamps.popleft() if timestamps else None
        cleaned.append(f"[{timestamp}] {message}" if timestamp else message)
    return cleaned


def clean_full_session_log(log_path: Path, state_path: Path) -> None:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    live_lines, state_text = split_live_transcript_and_state(text)
    state = load_flow_state(state_path if state_path.exists() else None, state_text)
    history = state.get("history", []) if isinstance(state.get("history", []), list) else []

    if state:
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    log_path.write_text("\n".join(_clean_lines(live_lines, history)).rstrip() + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean full_session.log into a compact live transcript.")
    parser.add_argument("logfile", help="Path to full_session.log")
    parser.add_argument("--state-file", default=None, help="Sidecar state file to write or reuse")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    log_path = Path(args.logfile).expanduser().resolve()
    state_path = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else Path(f"{log_path}.state.json")
    )

    if not log_path.exists():
        raise SystemExit(f"Log file not found: {log_path}")

    clean_full_session_log(log_path, state_path)
    print(f"Cleaned session log: {log_path}")
    print(f"State sidecar: {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
