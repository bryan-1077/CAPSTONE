"""Request identity and traceability checks shared by generation and replay.

Preserving a requirement's text establishes traceability, not satisfaction of
its meaning. Contract review and downstream behavioral tests remain necessary.
"""
from __future__ import annotations

import copy
import json

from specification import SpecificationError, fields, text_value


def build_specification_request(config, requirements_text=''):
    from generate_microarch import build_request
    request = build_request(config, requirements_text)

    def leaves(value, path=''):
        if isinstance(value, dict) and value:
            for key in sorted(value):
                escaped = str(key).replace('~', '~0').replace('/', '~1')
                yield from leaves(value[key], path + '/' + escaped)
        elif isinstance(value, list) and value:
            for index, item in enumerate(value):
                yield from leaves(item, path + '/' + str(index))
        else:
            yield path, value

    # Constraints remain explicit even when the caller supplies prose as well.
    # This is generic tree traversal, with no feature or scheduling-policy cases.
    request['requirements'].extend(
        {'id': f'CONSTRAINT_{index}',
         'text': f'Requested constraint {path} = {json.dumps(value, sort_keys=True)}.'}
        for index, (path, value) in enumerate(leaves(request['constraints']), 1))
    return request


def system_projection(package, documents):
    """The immutable result of system elaboration, before owners are assigned."""
    metadata = copy.deepcopy(package)
    metadata.pop('generation', None)
    projected = {section: copy.deepcopy(documents[section])
                 for section in ('shared', 'system', 'execution')}
    internal_types = package.get('generation', {}).get('internal_types', {})
    for name, definition in internal_types.items():
        if projected['shared']['types'].get(name) != definition:
            raise SpecificationError('Generated internal type differs from decomposition: ' + name)
        del projected['shared']['types'][name]
    for section in ('system', 'execution'):
        for rule in projected[section]['rules'].values():
            rule.pop('owners', None)
    return dict(package=metadata, **projected)


def validate_request_rules(request, documents):
    fields(request, ('constraints', 'requirements'), ('baseline_contract', 'design_kind'), 'request')
    fields(request['constraints'], (), None, 'request constraints')
    if not isinstance(request['requirements'], list) or not request['requirements']:
        raise SpecificationError('Request needs requirements')
    requested = {}
    for requirement in request['requirements']:
        fields(requirement, ('id', 'text'), (), 'request requirement')
        text_value(requirement['id'], 'request requirement ID')
        text_value(requirement['text'], 'request requirement text')
        if requirement['id'] in requested:
            raise SpecificationError('Duplicate request requirement ID')
        requested[requirement['id']] = requirement['text']
    supplied = {}
    for section in ('system', 'execution'):
        for rid, rule in documents[section]['rules'].items():
            if rid in supplied:
                raise SpecificationError(f'Rule IDs must be globally unique: {rid}')
            supplied[rid] = rule
    for rid, text in requested.items():
        rule = supplied.get(rid, {})
        if rule.get('statement') != text or rule.get('origin') != 'request.' + rid:
            raise SpecificationError(f'Request requirement {rid} missing or rewritten; preserve ID, exact text and origin')


def validate_generated_package(package, documents):
    from architecture import fingerprint
    proof = fields(package['generation'], ('version', 'model', 'request', 'input_fingerprint',
                   'system_fingerprint', 'context_fingerprint', 'validation_repairs', 'internal_types'), (), 'generation')
    if type(proof['version']) is not int or proof['version'] != 1:
        raise SpecificationError('Unsupported specification generation version')
    text_value(proof['model'], 'generation model')
    fields(proof['internal_types'], (), None, 'generation.internal_types')
    if not isinstance(proof['validation_repairs'], list):
        raise SpecificationError('generation.validation_repairs must be a list')
    for key in ('input_fingerprint', 'system_fingerprint', 'context_fingerprint'):
        text_value(proof[key], 'generation.' + key)
    if fingerprint(proof['request']) != proof['input_fingerprint']:
        raise SpecificationError('Generated package request fingerprint differs')
    validate_request_rules(proof['request'], documents)
    if fingerprint(system_projection(package, documents)) != proof['system_fingerprint']:
        raise SpecificationError('System specification changed after elaboration; regenerate the decomposition')
    if package.get('design_kind') != proof['request'].get('design_kind'):
        raise SpecificationError('Generated package design_kind differs from request')
    if 'verification' not in documents:
        raise SpecificationError('Generated packages require verification intent bindings')
    for section in ('system', 'execution'):
        for obligation in documents[section]['verification'].values():
            if obligation['status'] != 'planned_not_executed':
                raise SpecificationError('Generated verification obligations must remain planned_not_executed')
    baseline = proof['request'].get('baseline_contract')
    if baseline:
        from controller_baseline import controller_baseline
        if baseline != controller_baseline(proof['request']['constraints']):
            raise SpecificationError('Generated package baseline differs from request')

