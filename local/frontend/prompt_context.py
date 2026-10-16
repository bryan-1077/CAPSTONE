"""Lossless, deterministic sharing of repeated JSON context values for LLM prompts.

No design-specific field selection, truncation, or semantic summarization is used.
The complete compiler context remains recoverable for regression checks.
"""
from collections import Counter
import json


FORMAT = 'shared_definitions_v1'


def compact_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def encode_context(context):
    counts = Counter()
    collision = False

    def key(value):
        if isinstance(value, str) and len(value) >= 160:
            return compact_json(value)
        if isinstance(value, (dict, list)):
            encoded = compact_json(value)
            return encoded if len(encoded) >= 256 else None
        return None

    def count(value):
        nonlocal collision
        if isinstance(value, dict):
            # Preserve arbitrary source metadata that already uses JSON references.
            collision |= set(value) == {'$ref'}
            for child in value.values():
                count(child)
        elif isinstance(value, list):
            for child in value:
                count(child)
        signature = key(value)
        if signature is not None:
            counts[signature] += 1

    count(context)
    if collision:
        return context
    names, definitions = {}, {}

    def encode(value, share=True):
        signature = key(value)
        if share and signature is not None and counts[signature] > 1:
            if signature not in names:
                name = f'C{len(names) + 1:03d}'
                names[signature] = name
                definitions[name] = encode(value, share=False)
            return {'$ref': '#/definitions/' + names[signature]}
        if isinstance(value, dict):
            return {name: encode(value[name]) for name in sorted(value)}
        if isinstance(value, list):
            return [encode(child) for child in value]
        return value

    encoded = encode(context)

    def reference_name(value):
        if isinstance(value, dict) and set(value) == {'$ref'}:
            return value['$ref'].removeprefix('#/definitions/')
        return None

    def references(value):
        name = reference_name(value)
        if name is not None:
            yield name
        elif isinstance(value, dict):
            for child in value.values():
                yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)

    # Sharing a parent can make a previously repeated child occur only once.
    # Inline such definitions to avoid adding reference overhead without savings.
    while definitions:
        uses = Counter(references(encoded))
        for value in definitions.values():
            uses.update(references(value))
        inline = {name for name in definitions if uses[name] < 2}
        if not inline:
            break

        def replace(value):
            name = reference_name(value)
            if name in inline:
                return replace(definitions[name])
            if isinstance(value, dict):
                return {k: replace(v) for k, v in value.items()}
            if isinstance(value, list):
                return [replace(v) for v in value]
            return value

        encoded = replace(encoded)
        definitions = {name: replace(value) for name, value in definitions.items() if name not in inline}

    document = {'context_format': FORMAT, 'definitions': definitions, 'context': encoded}
    return document if definitions and len(compact_json(document)) < len(compact_json(context)) else context


def expand_context(document):
    """Reconstruct the original context; also detects dangling/cyclic references."""
    if document.get('context_format') != FORMAT:
        return document
    definitions = document['definitions']

    def expand(value, active=()):
        if isinstance(value, dict):
            if set(value) == {'$ref'}:
                name = value['$ref'].removeprefix('#/definitions/')
                if name in active:
                    raise ValueError(f'Cyclic prompt definition: {name}')
                return expand(definitions[name], active + (name,))
            return {k: expand(v, active) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v, active) for v in value]
        return value

    return expand(document['context'])


def serialize_context(context):
    return compact_json(encode_context(context))
