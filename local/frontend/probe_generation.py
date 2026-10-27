#!/usr/bin/env python3
"""Time one real generator prompt with a small output cap; never emit RTL."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

import requests

import dual_llm_rtlGen as generator
from token_usage import UsageRun, record_usage
from tpm_pacing import estimate_input, paced_post, rate_limit_headers
from validator import validate_spec


class _CapturedRequest(Exception):
    pass


def capture_request(spec_path):
    """Intercept the production generator's initial request without sending it."""
    design = validate_spec(str(spec_path))
    captured = {}
    def capture(model, system_prompt, user_message, label):
        captured.update(model=model, messages=[{'role': 'system', 'content': system_prompt},
                                             {'role': 'user', 'content': user_message}])
        raise _CapturedRequest()
    with patch.object(generator, '_call_llm', side_effect=capture):
        try:
            generator._llm1_generate(design, Path(spec_path).read_text(), [])
        except _CapturedRequest:
            pass
    if not captured:
        raise ValueError('Generator did not produce an initial request')
    return captured


def _observe_message(message, stats, started):
    if not isinstance(message, dict):
        raise ValueError('Provider message/delta must be an object')
    for name, value in message.items():
        field = stats['message_fields'].setdefault(name, {'types': [], 'characters': 0})
        kind = type(value).__name__
        if kind not in field['types']:
            field['types'].append(kind)
        if isinstance(value, str):
            field['characters'] += len(value)
            if value and name == 'content' and stats['first_content_seconds'] is None:
                stats['first_content_seconds'] = time.perf_counter() - started
                print(f'[PROBE] First content: {stats["first_content_seconds"]:.1f}s', flush=True)
        elif isinstance(value, (dict, list)):
            field['characters'] += len(json.dumps(value))
        if value and ('reason' in name or 'think' in name) and stats['first_reasoning_seconds'] is None:
            stats['first_reasoning_seconds'] = time.perf_counter() - started
            print(f'[PROBE] First reasoning field: {stats["first_reasoning_seconds"]:.1f}s', flush=True)


def _read_stream(response, stats, started, timeout=180):
    data_lines, finished = [], False
    def event():
        nonlocal finished
        if not data_lines:
            return
        text = '\n'.join(data_lines)
        data_lines.clear()
        if text.strip() == '[DONE]':
            finished = True
            return
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError('Provider SSE data must be an object')
        if data.get('error'):
            raise RuntimeError('Provider returned an error event in the stream')
        if isinstance(data.get('usage'), dict):
            stats['provider_usage'] = data['usage']
        for choice in data.get('choices', []):
            if choice.get('finish_reason') is not None:
                stats['finish_reason'] = choice['finish_reason']
            _observe_message(choice.get('delta') or {}, stats, started)
    for line in response.iter_lines(chunk_size=1, decode_unicode=True):
        if isinstance(line, bytes):
            line = line.decode('utf-8')
        if time.perf_counter() - started > timeout:
            raise RuntimeError(f'Streaming probe exceeded its {timeout:g}-second diagnostic window')
        if line and stats['first_body_seconds'] is None:
            stats['first_body_seconds'] = time.perf_counter() - started
            print(f'[PROBE] First stream line/heartbeat: {stats["first_body_seconds"]:.1f}s', flush=True)
        if line == '':
            event()
        elif line.startswith('data:'):
            data_lines.append(line[5:].lstrip(' '))
        if finished:
            break
    event()
    if not finished or stats['finish_reason'] is None:
        raise RuntimeError('Incomplete SSE stream: missing [DONE] or finish_reason')


def run_probe(spec_path, max_tokens=512, dry_run=False, stream=False, timeout=None):
    if not 1 <= max_tokens <= 2048:
        raise ValueError('Probe output allowance must be between 1 and 2048 tokens')
    timeout = generator.request_timeout_seconds(timeout)
    payload = dict(capture_request(spec_path), max_tokens=max_tokens, stream=stream)
    if stream:
        payload['stream_options'] = {'include_usage': True}
    messages = payload['messages']
    characters = sum(len(m['content']) for m in messages)
    digest = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()
    print(f'[PROBE] Model: {payload["model"]}; endpoint: {generator.ENDPOINT}', flush=True)
    print(f'[PROBE] Client timeout: {timeout:g}s', flush=True)
    print(f'[PROBE] Request characters: {characters}; messages SHA-256: {digest}', flush=True)
    print(f'[PROBE] Output cap: {max_tokens}; stream={stream}; estimated reservation: {estimate_input(messages) + max_tokens:,}', flush=True)
    if dry_run:
        print('[PROBE] Dry run: no API request sent.')
        return
    key = os.environ.get('TAMUS_AI_CHAT_API_KEY', '')
    if not key:
        raise ValueError('TAMUS_AI_CHAT_API_KEY is not set')
    stats = {'stream': stream, 'max_tokens': max_tokens, 'message_sha256': digest, 'client_timeout_seconds': timeout,
             'provider_usage': None, 'finish_reason': None, 'message_fields': {},
             'first_body_seconds': None, 'first_content_seconds': None, 'first_reasoning_seconds': None}
    with UsageRun(Path(__file__).resolve().parent / 'microarch/usage', label='generation_probe') as usage:
        started, response, outcome = time.perf_counter(), None, 'network_error'
        try:
            response = paced_post(generator.ENDPOINT, pacing_label='Generation-Probe',
                headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                json=payload, timeout=timeout, stream=stream)
            stats['http_status'] = response.status_code
            stats['headers_seconds'] = time.perf_counter() - started
            outcome = 'response' if response.status_code == 200 else 'http_error'
            print(f'[PROBE] HTTP {response.status_code}; headers received at {stats["headers_seconds"]:.1f}s', flush=True)
            if response.status_code != 200:
                raise RuntimeError(f'Probe received HTTP {response.status_code}; see usage report for rate-limit headers')
            if stream:
                if 'text/event-stream' not in response.headers.get('Content-Type', '').lower():
                    raise RuntimeError('Gateway did not return text/event-stream for the streaming request')
                _read_stream(response, stats, started, timeout=timeout)
            else:
                data = response.json()
                stats['provider_usage'] = data.get('usage')
                choice = data['choices'][0]
                stats['finish_reason'] = choice.get('finish_reason')
                _observe_message(choice['message'], stats, started)
            print(f'[PROBE] finish_reason={stats["finish_reason"]}; message fields={json.dumps(stats["message_fields"])}', flush=True)
            print('[PROBE] Provider usage: ' + json.dumps(stats['provider_usage']).replace(key, '[redacted]'), flush=True)
            print('[PROBE] Response received. Truncation is expected with this cap; no RTL accepted or written.', flush=True)
            usage.status = 'completed'
        except requests.RequestException:
            outcome = 'network_error'
            raise RuntimeError('Probe network request failed') from None
        finally:
            stats['elapsed_seconds'] = time.perf_counter() - started
            stats['status'] = usage.status
            headers = rate_limit_headers(response) if response is not None else {}
            record_usage(payload['model'], 'Generation-Probe', stats['provider_usage'], outcome=outcome,
                         finish_reason=stats['finish_reason'], request_characters=characters, rate_limit_headers=headers)
            report = usage.directory / 'probe.json'
            report.write_text(json.dumps(stats, indent=2).replace(key, '[redacted]') + '\n')
            if response is not None:
                response.close()
            print(f'[PROBE] Elapsed including any pacing wait: {stats["elapsed_seconds"]:.1f}s; details: {report}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec', type=Path, help='Expanded module YAML used by design.py')
    parser.add_argument('--max-tokens', type=int, default=512)
    parser.add_argument('--timeout', type=float, help='Client timeout seconds; overrides DUAL_LLM_TIMEOUT_SECONDS (default 180)')
    parser.add_argument('--stream', action='store_true', help='Test SSE delivery and first-line/content timings')
    parser.add_argument('--dry-run', action='store_true', help='Describe the exact request without contacting the provider')
    args = parser.parse_args()
    try:
        run_probe(args.spec, args.max_tokens, args.dry_run, args.stream, args.timeout)
    except (ValueError, RuntimeError, OSError, KeyError, IndexError, TypeError) as error:
        print(f'[PROBE] ERROR: {error}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
