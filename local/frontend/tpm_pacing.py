"""Small, workspace-shared rolling token budget for frontend API calls.

Reservations include estimated input plus the full requested output allowance.
They expire after 60 seconds, even on errors; provider usage is never refunded.
This is local pacing, not an account-wide quota guarantee or a billing counter.
"""
import fcntl
import json
import math
import os
from pathlib import Path
import time

import requests


DEFAULT_LIMIT = 80_000
WINDOW_SECONDS = 60
DEFAULT_STATE = Path(__file__).resolve().parent / 'microarch/usage/tpm_reservations.json'


class PacingError(ValueError):
    pass


def estimate_input(messages):
    # Dependency-free heuristic: two UTF-8 bytes/token plus message overhead.
    # Deliberately more generous than the common four-character heuristic for RTL.
    size = len(json.dumps(messages, ensure_ascii=False).encode('utf-8'))
    return (size + 1) // 2 + 256


def reserve_request(messages, max_tokens, *, label='LLM', now=None, sleep=None):
    try:
        limit = int(os.environ.get('CAPSTONE_TPM_LIMIT', str(DEFAULT_LIMIT)))
    except ValueError as exc:
        raise PacingError('CAPSTONE_TPM_LIMIT must be a nonnegative integer') from exc
    if limit < 0:
        raise PacingError('CAPSTONE_TPM_LIMIT must be nonnegative (0 explicitly disables pacing)')
    if limit == 0:
        return
    if type(max_tokens) is not int or max_tokens <= 0:
        raise PacingError('TPM pacing requires a positive max_tokens output allowance')
    needed = estimate_input(messages) + max_tokens
    if needed > limit:
        raise PacingError(f'TPM reservation for {label} needs {needed:,} tokens, exceeding the '
                         f'{limit:,} local limit. Reduce context/output allowance or configure CAPSTONE_TPM_LIMIT.')
    state = Path(os.environ.get('CAPSTONE_TPM_STATE_FILE', str(DEFAULT_STATE))).expanduser()
    state.parent.mkdir(parents=True, exist_ok=True)
    now = now or time.time
    sleep = sleep or time.sleep
    while True:
        with state.with_name(state.name + '.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            current = now()
            try:
                entries = json.loads(state.read_text()) if state.exists() else []
                if not isinstance(entries, list) or any(
                    not isinstance(entry, dict) or
                    type(entry.get('at')) not in (int, float) or not math.isfinite(entry['at']) or
                    type(entry.get('tokens')) is not int or entry['tokens'] <= 0
                    for entry in entries
                ):
                    raise ValueError('invalid reservations')
            except (ValueError, TypeError) as exc:
                raise PacingError(f'Invalid TPM reservation state: {state}; refusing to discard active reservations') from exc
            entries = [entry for entry in entries if entry['at'] > current - WINDOW_SECONDS]
            used = sum(entry['tokens'] for entry in entries)
            if used + needed <= limit:
                entries.append({'at': current, 'tokens': needed})
                pending = state.with_name(state.name + '.tmp')
                pending.write_text(json.dumps(entries, separators=(',', ':')))
                pending.replace(state)
                print(f'[TPM] {label}: reserving {needed:,}; rolling reservations '
                      f'{used + needed:,}/{limit:,} tokens (estimated)', flush=True)
                return
            delay = min(30.0, max(0.01, min(entry['at'] for entry in entries) + WINDOW_SECONDS - current))
        # Release the file lock while waiting; another process may reserve first.
        print(f'[TPM] {label}: waiting {delay:.1f}s; {used:,} reserved, '
              f'next request needs {needed:,}, limit {limit:,}', flush=True)
        sleep(delay)


def rate_limit_headers(response):
    return {name: str(value)[:200] for name, value in response.headers.items()
            if name.lower().startswith(('x-ratelimit-', 'ratelimit-')) or name.lower() == 'retry-after'}


def paced_post(*args, pacing_label='LLM', **kwargs):
    payload = kwargs['json']
    label = f'{pacing_label}/{payload["model"]}'
    reserve_request(payload['messages'], payload['max_tokens'], label=label)
    response = requests.post(*args, **kwargs)
    headers = rate_limit_headers(response)
    if headers:
        print(f'[TPM] {label}: provider rate-limit headers {json.dumps(headers, sort_keys=True)}', flush=True)
    return response
