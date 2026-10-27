"""Response evidence and failure categories; no retry or acceptance policy.

Normal frontend entry points provide a ModuleUsage directory. Private helper
calls without that scope retain diagnostics in memory and create no global files.
"""
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

import token_usage


class FailureKind(str, Enum):
    CONFIGURATION = 'configuration'
    NETWORK_TIMEOUT = 'network_timeout'
    NETWORK_CONNECTION = 'network_connection'
    NETWORK_ERROR = 'network_error'
    HTTP_ERROR = 'http_error'
    PROVIDER_RESPONSE = 'provider_response'
    TRUNCATED_RESPONSE = 'truncated_response'
    FILTERED_RESPONSE = 'filtered_response'
    REFUSED_RESPONSE = 'refused_response'
    UNFINISHED_RESPONSE = 'unfinished_response'
    EMPTY_RESPONSE = 'empty_response'
    NON_TEXT_RESPONSE = 'non_text_response'
    MALFORMED_JSON = 'malformed_json'
    INVALID_PAYLOAD = 'invalid_payload'
    MISSING_RTL = 'missing_rtl'
    SPEC_CLARIFICATION = 'spec_clarification'
    REVIEW_UNAVAILABLE = 'review_unavailable'


_failure_events = ContextVar('rtl_llm_failure_events', default=None)


def redact(value):
    """Keep complete evidence except for occurrences of the configured API key."""
    secret = os.environ.get('TAMUS_AI_CHAT_API_KEY', '')
    if isinstance(value, str):
        if secret:
            variants = {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]}
            for variant in sorted(variants, key=len, reverse=True):
                value = value.replace(variant, '[redacted]')
        return value
    if isinstance(value, dict):
        return {redact(k): redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def write_json(path, value):
    """Private, atomic, best-effort evidence writes cannot replace the real error."""
    temporary = None
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(redact(value), stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
        return True
    except OSError as exc:
        print('[dual_llm] Diagnostic write failed: ' + redact(str(exc)), flush=True)
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def failure(kind, stage, message, *, diagnostic=None, **details):
    value = redact({'kind': FailureKind(kind).value, 'stage': stage, 'message': str(message),
                    'diagnostic': str(diagnostic) if diagnostic else None, **details})
    events = _failure_events.get()
    if events is not None:
        events.append(value)
    return value


class LlmFailure(RuntimeError):
    def __init__(self, kind, stage, message, *, event=None, **details):
        self.failure = event if event is not None else failure(kind, stage, message, **details)
        self.kind = self.failure['kind']
        super().__init__(redact(str(message)))


class DiagnosticText(str):
    """String-compatible response carrying its per-attempt evidence handle."""
    def __new__(cls, text, diagnostic):
        result = super().__new__(cls, text)
        result.diagnostic = diagnostic
        return result


class CallDiagnostics:
    def __init__(self, model, stage, system_prompt, user_message, timeout):
        self.call_id = uuid.uuid4().hex
        self.directory = None
        try:
            scope = token_usage.current_module_directory()
            if scope is not None:
                self.directory = scope / 'llm_calls' / self.call_id
        except OSError as exc:
            print('[dual_llm] Diagnostic directory unavailable: ' + redact(str(exc)), flush=True)
        messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_message}]
        self.request = {'model': model, 'stage': stage, 'timeout_seconds': timeout,
                        'request_characters': len(system_prompt) + len(user_message),
                        'messages_sha256': hashlib.sha256(json.dumps(messages, ensure_ascii=True).encode()).hexdigest()}

    def attempt(self, number, budget):
        path = self.directory / f'attempt_{number:03d}.json' if self.directory is not None else None
        return AttemptDiagnostic(path, {'diagnostic_version': 1, 'call_id': self.call_id,
            'attempt': number, 'started_at': datetime.now(timezone.utc).isoformat(),
            'request': {**self.request, 'max_tokens': budget}, 'response': None,
            'parser': None, 'failure': None})


class AttemptDiagnostic:
    def __init__(self, path, record):
        self.path, self.record = path, record
        self.save()

    def save(self):
        if self.path is not None:
            clean = redact(self.record)
            clean['credentials_redacted'] = clean != self.record
            write_json(self.path, clean)

    def response(self, response):
        # Store the complete decoded HTTP body before the caller parses the
        # provider envelope or the model's inner JSON. Never store request headers.
        body = response.text
        data = None
        try:
            data = response.json()
        except (ValueError, TypeError):
            pass
        choice = None
        if isinstance(data, dict) and isinstance(data.get('choices'), list) and data['choices']:
            choice = data['choices'][0]
        choice = choice if isinstance(choice, dict) else {}
        message = choice.get('message')
        message = message if isinstance(message, dict) else {}
        self.record['response'] = {'received_at': datetime.now(timezone.utc).isoformat(),
            'http_status': response.status_code, 'body': body, 'content': message.get('content'),
            'finish_reason': choice.get('finish_reason'),
            'usage': data.get('usage') if isinstance(data, dict) else None}
        self.save()

    def fail(self, kind, message, **details):
        event = failure(kind, self.record['request']['stage'], message, diagnostic=self.path, **details)
        self.record['failure'] = event
        self.save()
        return event

    def parsed(self, text, error=None):
        location = ({'line': error.lineno, 'column': error.colno, 'position': error.pos}
                    if isinstance(error, json.JSONDecodeError) else None)
        self.record['parser'] = {'status': 'failed' if error else 'parsed',
            'input': text, 'coordinate_space': ('parser.input_before_credential_redaction'
                if redact(text) != text else 'parser.input'), 'location': location,
            'error_type': type(error).__name__ if error else None,
            'error': str(error) if error else None}
        self.save()


def record_parse(text, parsed_text, stage, error=None, *, missing_rtl=False):
    diagnostic = getattr(text, 'diagnostic', None)
    if diagnostic is not None:
        diagnostic.parsed(parsed_text, error)
    if error is not None:
        kind = (FailureKind.MALFORMED_JSON if isinstance(error, json.JSONDecodeError)
                else FailureKind(getattr(error, 'failure_kind', FailureKind.INVALID_PAYLOAD)))
        details = {'parser_error_type': type(error).__name__}
        if hasattr(error, 'path'):
            details['schema_path'] = error.path
        if isinstance(error, json.JSONDecodeError):
            details['parser_location'] = {'line': error.lineno, 'column': error.colno, 'position': error.pos}
        if diagnostic is not None:
            return diagnostic.fail(kind, str(error), **details)
        return failure(kind, stage, str(error), **details)
    if missing_rtl:
        message = stage + ' returned missing or empty corrected_rtl'
        if diagnostic is not None:
            return diagnostic.fail(FailureKind.MISSING_RTL, message)
        return failure(FailureKind.MISSING_RTL, stage, message)
    return None
