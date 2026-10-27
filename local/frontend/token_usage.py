"""Provider-reported token accounting shared by frontend subprocesses.

Cached/reasoning details are subsets of input/output totals, never added twice.
Unavailable provider usage remains unknown; no character-based estimates.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone
import uuid


LEDGER_ENV = 'CAPSTONE_TOKEN_USAGE_FILE'
MODULE_ENV = 'CAPSTONE_TOKEN_MODULE'
SCOPE_ENV = 'CAPSTONE_TOKEN_MODULE_SCOPE'
# User-supplied rates; standard input/output estimates, not billing receipts.
MODEL_PRICING_USD_PER_MILLION = {
    'protected.Claude-Opus-5': {'input': 5, 'output': 25},
    'protected.gpt-5.6-terra': {'input': 2, 'output': 12},
}


def estimate_cost(calls):
    # At these integer dollars-per-million rates, arithmetic in microdollars is exact.
    known = {'input': 0, 'output': 0}
    missing = {'input': 0, 'output': 0}
    unpriced = set()
    for call in calls:
        rates = MODEL_PRICING_USD_PER_MILLION.get(call['model'])
        if rates is None:
            unpriced.add(call['model'])
        for direction in known:
            count = call[direction + '_tokens']
            if rates is None or count is None:
                missing[direction] += 1
            else:
                known[direction] += count * rates[direction]
    complete = not any(missing.values())
    result = {'currency': 'USD', 'complete': complete, 'unpriced_models': sorted(unpriced)}
    for direction in known:
        result['known_' + direction + '_usd'] = known[direction] / 1_000_000
        result['estimated_' + direction + '_usd'] = None if missing[direction] else known[direction] / 1_000_000
    result['known_total_usd'] = sum(known.values()) / 1_000_000
    result['estimated_total_usd'] = result['known_total_usd'] if complete else None
    return result


def _integer(value):
    return value if type(value) is int and value >= 0 else None


def _time():
    return datetime.now(timezone.utc).isoformat()


def _append(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # One append write per small event also supports inherited subprocesses.
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(value, separators=(',', ':')) + '\n')


def _read(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def record_usage(model, label, usage=None, *, outcome='response', finish_reason=None,
                 request_characters=None, rate_limit_headers=None):
    """Count every provider response, including truncation retries and errors."""
    target = os.environ.get(LEDGER_ENV)
    if not target:
        return
    usage = usage if isinstance(usage, dict) else {}
    incoming = _integer(usage.get('prompt_tokens', usage.get('input_tokens')))
    outgoing = _integer(usage.get('completion_tokens', usage.get('output_tokens')))
    reported_total = _integer(usage.get('total_tokens'))
    total = incoming + outgoing if incoming is not None and outgoing is not None else reported_total
    _append(target, {'call_id': uuid.uuid4().hex, 'at': _time(), 'model': model, 'label': label,
                     'module': os.environ.get(MODULE_ENV), 'module_scope': os.environ.get(SCOPE_ENV),
                     'input_tokens': incoming, 'output_tokens': outgoing, 'total_tokens': total,
                     'provider_total_tokens': reported_total, 'outcome': outcome, 'finish_reason': finish_reason,
                     'request_characters': request_characters, 'rate_limit_headers': rate_limit_headers})


def record_response(model, label, response, *, request_characters=None):
    """Record once before callers validate the response or raise HTTP errors."""
    from tpm_pacing import rate_limit_headers
    try:
        data = response.json()
    except (ValueError, TypeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    choices = data.get('choices') or []
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    record_usage(model, label, data.get('usage'),
                 outcome='response' if response.status_code == 200 else 'http_error',
                 finish_reason=choice.get('finish_reason'), request_characters=request_characters,
                 rate_limit_headers=rate_limit_headers(response))


def summarize(calls):
    result = {'calls': len(calls)}
    for key in ('input_tokens', 'output_tokens', 'total_tokens'):
        known = sum(call[key] for call in calls if call[key] is not None)
        missing = sum(call[key] is None for call in calls)
        result[key] = None if missing else known
        result['known_' + key] = known
        result['calls_missing_' + key] = missing
    result['complete'] = all(result[key] is not None for key in ('input_tokens', 'output_tokens', 'total_tokens'))
    result['cost'] = estimate_cost(calls)
    return result


def _display(totals):
    def value(key):
        count = totals[key]
        return f'{count:,}' if count is not None else f'unknown (known subtotal {totals["known_" + key]:,})'
    return f'input={value("input_tokens")} output={value("output_tokens")} total={value("total_tokens")} calls={totals["calls"]}'


def _display_cost(totals):
    cost = totals['cost']
    value = cost['estimated_total_usd']
    text = f'${value:.6f}' if value is not None else f'unknown (known subtotal ${cost["known_total_usd"]:.6f})'
    suffix = f'; no rates for {", ".join(cost["unpriced_models"])}' if cost['unpriced_models'] else ''
    return f'estimated USD cost={text}{suffix}'


def current_module_directory():
    ledger, scope = os.environ.get(LEDGER_ENV), os.environ.get(SCOPE_ENV)
    if not ledger or not scope:
        return None
    path = Path(ledger).parent / 'modules' / scope
    path.mkdir(parents=True, exist_ok=True)
    return path


def set_module_name(name):
    if os.environ.get(SCOPE_ENV):
        os.environ[MODULE_ENV] = name


def report_path():
    ledger = os.environ.get(LEDGER_ENV)
    return str(Path(ledger).parent / 'usage.json') if ledger else None


class UsageRun:
    def __init__(self, root, label='frontend'):
        self.root, self.label = Path(root), label
        self.status = 'failed'

    def __enter__(self):
        self.previous = {key: os.environ.get(key) for key in (LEDGER_ENV, MODULE_ENV, SCOPE_ENV)}
        self.run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:12]
        self.directory = self.root / self.run_id
        self.directory.mkdir(parents=True)
        self.started_at = _time()
        os.environ[LEDGER_ENV] = str((self.directory / 'calls.jsonl').resolve())
        os.environ.pop(MODULE_ENV, None)
        os.environ.pop(SCOPE_ENV, None)
        return self

    def __exit__(self, kind, error, traceback):
        try:
            calls = _read(self.directory / 'calls.jsonl')
            totals = summarize(calls)
            result = {'usage_version': 2, 'run_id': self.run_id, 'label': self.label,
                      'pricing': {'source': 'user_supplied', 'currency': 'USD',
                                  'usd_per_million_tokens': MODEL_PRICING_USD_PER_MILLION},
                      'status': 'failed' if kind else self.status, 'started_at': self.started_at,
                      'finished_at': _time(), 'totals': totals,
                      'by_model': {model: summarize([c for c in calls if c['model'] == model])
                                   for model in sorted({c['model'] for c in calls})},
                      'modules': _read(self.directory / 'modules.jsonl')}
            path = self.directory / 'usage.json'
            path.write_text(json.dumps(result, indent=2) + '\n')
            print(f'[TOKENS] Flow {result["status"]}: {_display(totals)}', flush=True)
            print(f'[COST] Flow {result["status"]}: {_display_cost(totals)}', flush=True)
            print(f'[TOKENS] Report: {path}', flush=True)
        finally:
            for key, value in self.previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class ModuleUsage:
    def __init__(self, name, output_root, *, on_completed=None):
        self.name, self.output_root = name, Path(output_root)
        self.on_completed = on_completed

    def __enter__(self):
        self.own_run = None
        if not os.environ.get(LEDGER_ENV):
            self.own_run = UsageRun(self.output_root / 'microarch/usage', 'standalone_module')
            self.own_run.__enter__()
        self.previous = {key: os.environ.get(key) for key in (MODULE_ENV, SCOPE_ENV)}
        self.scope = uuid.uuid4().hex
        os.environ[MODULE_ENV] = self.name
        os.environ[SCOPE_ENV] = self.scope
        return self

    def __exit__(self, kind, error, traceback):
        try:
            name = os.environ.get(MODULE_ENV, self.name)
            ledger = Path(os.environ[LEDGER_ENV])
            totals = summarize([c for c in _read(ledger) if c['module_scope'] == self.scope])
            result = {'module': name, 'scope': self.scope, 'status': 'failed' if kind else 'completed',
                      'totals': totals}
            directory = current_module_directory()
            (directory / 'usage.json').write_text(json.dumps(result, indent=2) + '\n')
            _append(ledger.parent / 'modules.jsonl', result)
            print(f'\n[TOKENS] Module {name} {result["status"]}: {_display(totals)}', flush=True)
            print(f'[COST] Module {name} {result["status"]}: {_display_cost(totals)}', flush=True)
            if kind is None and self.on_completed is not None:
                self.on_completed(name)
        finally:
            for key, value in self.previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if self.own_run:
                self.own_run.status = 'failed' if kind else 'completed'
                self.own_run.__exit__(kind, error, traceback)
