"""Strict model JSON and stage-specific payload contracts; no model calls."""
import json
import re


ALLOWED_STATUS = {'VALID', 'INVALID', 'AMBIGUOUS'}
ALLOWED_ISSUE_TYPES = {'SPEC_VIOLATION', 'NON_ISSUE', 'AMBIGUITY'}
STATUS_ALIASES = {'CLEAN': 'VALID'}


class PayloadValidationError(ValueError):
    def __init__(self, message, path='', *, kind='invalid_payload'):
        self.path, self.failure_kind = path, kind
        super().__init__((path or '/') + ': ' + message)


def json_input(text):
    if not isinstance(text, str):
        raise PayloadValidationError('response must be text')
    value = text.strip()
    match = re.fullmatch(r'```(?:json)?\s*(.*?)```', value, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else value


def decode_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PayloadValidationError('duplicate JSON member: ' + key)
            result[key] = value
        return result
    def constant(value):
        raise PayloadValidationError('nonstandard JSON constant: ' + value)
    return json.loads(text, object_pairs_hook=unique, parse_constant=constant)


def _fields(value, required, optional, path):
    if not isinstance(value, dict):
        raise PayloadValidationError('expected an object', path)
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise PayloadValidationError(f'missing fields {sorted(missing)}, unknown fields {sorted(unknown)}', path)


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise PayloadValidationError('expected nonempty text', path)
    return value.strip()


def _rtl(value, *, required):
    if not isinstance(value, str):
        raise PayloadValidationError('corrected_rtl must be text', '/corrected_rtl')
    value = value.strip()
    match = re.fullmatch(r'```(?:systemverilog|verilog|sv)?\s*(.*?)```', value, re.DOTALL | re.IGNORECASE)
    value = match.group(1).strip() if match else value
    if required and not value:
        raise PayloadValidationError('generator/merger requires nonempty RTL', '/corrected_rtl', kind='missing_rtl')
    return value


def validate_payload(data, stage):
    """Return validated copies; compatibility is explicit, never type coercion."""
    if stage not in ('generator', 'merger', 'reviewer', 'compact_reviewer'):
        raise ValueError('Unknown response stage: ' + stage)
    required = ('status', 'issues')
    optional = () if stage == 'compact_reviewer' else ('invariants', 'corrected_rtl')
    _fields(data, required, optional, '')
    status = _text(data['status'], '/status').upper()
    status = STATUS_ALIASES.get(status, status)
    if status not in ALLOWED_STATUS:
        raise PayloadValidationError('unknown status', '/status')
    if not isinstance(data['issues'], list):
        raise PayloadValidationError('issues must be an array', '/issues')
    issues = []
    for index, issue in enumerate(data['issues']):
        path = f'/issues/{index}'
        _fields(issue, ('type', 'description', 'location'), (), path)
        kind = _text(issue['type'], path + '/type').upper()
        if kind not in ALLOWED_ISSUE_TYPES:
            raise PayloadValidationError('unknown issue type', path + '/type')
        issues.append({'type': kind, 'description': _text(issue['description'], path + '/description'),
                       'location': _text(issue['location'], path + '/location')})
    kinds = {issue['type'] for issue in issues}
    if status == 'VALID' and kinds & {'SPEC_VIOLATION', 'AMBIGUITY'}:
        raise PayloadValidationError('VALID contradicts blocking issues', '/status')
    if status == 'INVALID' and 'SPEC_VIOLATION' not in kinds:
        raise PayloadValidationError('INVALID requires a SPEC_VIOLATION issue', '/issues')
    if status == 'AMBIGUOUS' and 'AMBIGUITY' not in kinds:
        raise PayloadValidationError('AMBIGUOUS requires an AMBIGUITY issue', '/issues')
    result = {'status': status, 'issues': issues}
    if 'invariants' in data:
        value = data['invariants']
        if not isinstance(value, list):
            raise PayloadValidationError('invariants must be an array of text', '/invariants')
        result['invariants'] = [_text(v, f'/invariants/{i}') for i, v in enumerate(value)]
    if 'corrected_rtl' in data:
        result['corrected_rtl'] = _rtl(data['corrected_rtl'], required=stage in ('generator', 'merger'))
    elif stage in ('generator', 'merger'):
        raise PayloadValidationError('generator/merger requires corrected_rtl', '/corrected_rtl', kind='missing_rtl')
    return result
