"""Typed observation/event bindings supplied by an architecture specification.

These describe what a downstream monitor can sample. They neither implement a
checker nor infer event semantics from RTL names or behavioral prose.
"""
from specification import (SpecificationError, elaborate_type_layouts, expression,
                           fields, names, text_value)


def elaborate_bindings(documents, parameters, widths):
    shared = documents['shared']
    if 'verification' not in documents:
        return {'status': 'missing', 'events': {}, 'observations': {}, 'checks': {},
                'limitations': ['No verification binding document was supplied.']}
    document = fields(documents['verification'], ('binding_version', 'sampling', 'events', 'observations', 'checks'), (), 'verification bindings')
    if type(document['binding_version']) is not int or document['binding_version'] != 1:
        raise SpecificationError('Unsupported verification binding_version')
    sampling = fields(document['sampling'], ('phase', 'reset_gates_events'), (), 'verification sampling')
    if sampling != {'phase': 'pre_edge', 'reset_gates_events': True} or type(sampling['reset_gates_events']) is not bool:
        raise SpecificationError('Verification bindings require pre-edge sampling and reset-gated events')
    layouts = elaborate_type_layouts(shared['types'], parameters, widths)
    ports = {'top.' + name: port for name, port in fields(shared['host_interface'], (), None, 'host_interface').items()}
    for instance, contract in documents['modules']['contracts'].items():
        ports.update({instance + '.' + name: port for name, port in contract['interfaces'].items()})

    def reference(value):
        fields(value, ('endpoint',), ('field',), 'observation reference')
        endpoint = value['endpoint']
        if not isinstance(endpoint, str) or endpoint not in ports:
            raise SpecificationError(f'Unknown verification endpoint: {endpoint!r}')
        port = fields(ports[endpoint], ('direction', 'type'), ('when',), 'verification port ' + endpoint)
        if 'when' in port and not expression(port['when'], parameters):
            raise SpecificationError(f'Verification endpoint is absent in this configuration: {endpoint}')
        type_ref = port['type']
        if not isinstance(type_ref, str) or not type_ref.startswith('shared.types.') or type_ref.removeprefix('shared.types.') not in layouts:
            raise SpecificationError(f'Unknown verification port type on {endpoint}: {type_ref!r}')
        layout = layouts[type_ref.removeprefix('shared.types.')]
        lsb, width = 0, layout['width']
        if 'field' in value:
            text_value(value['field'], 'observation field')
            for part in value['field'].split('.'):
                member = next((f for f in layout.get('fields', []) if f['name'] == part), None)
                if layout['kind'] != 'record' or member is None:
                    raise SpecificationError(f'Unknown packed field {value["field"]!r} on {endpoint}')
                lsb += member['lsb']
                width, type_ref = member['width'], member['type']
                layout = layouts[type_ref.removeprefix('shared.types.')]
        instance, port_name = endpoint.split('.')
        # The consumer selects its DUT root. All suffixes refer only to declared
        # instance ports; private implementation register names never appear.
        suffix = port_name if instance == 'top' else endpoint
        return dict(value, type=type_ref, width=width, lsb=lsb, msb=lsb + width - 1,
                    dut_path_suffix=suffix, visibility='top_port' if instance == 'top' else 'instance_port')

    def predicate(value):
        fields(value, (), ('all', 'any', 'endpoint', 'field', 'equals'), 'event predicate')
        for operator in ('all', 'any'):
            if operator in value:
                if set(value) != {operator} or not isinstance(value[operator], list) or not value[operator]:
                    raise SpecificationError(f'Event {operator} requires a nonempty list of predicates')
                return {operator: [predicate(p) for p in value[operator]]}
        fields(value, ('endpoint', 'equals'), ('field',), 'event comparison')
        result = reference({key: value[key] for key in ('endpoint', 'field') if key in value})
        expected = value['equals']
        layout = layouts[result['type'].removeprefix('shared.types.')]
        if isinstance(expected, str) and layout['kind'] == 'enum':
            if expected not in layout['encodings']:
                raise SpecificationError(f'Unknown event enum value: {expected}')
            result['symbol'] = expected
            expected = layout['encodings'][expected]
        if type(expected) is not int or expected < 0 or expected.bit_length() > result['width']:
            raise SpecificationError('Event comparison value does not fit its declared type')
        return dict(result, equals=expected)

    raw_events = fields(document['events'], (), None, 'verification events')
    if set(raw_events) != set(shared['events']):
        raise SpecificationError('Every shared event needs a binding or an explicit unbound reason')
    events, limitations = {}, []
    for name, event in raw_events.items():
        fields(event, ('status',), ('when', 'payload', 'reason'), 'event.' + name)
        if event['status'] == 'unbound':
            fields(event, ('status', 'reason'), (), 'event.' + name)
            text_value(event['reason'], name + '.reason')
            events[name] = dict(event, source='shared.events.' + name)
            limitations.append(f'{name}: {event["reason"]}')
        elif event['status'] == 'bound':
            fields(event, ('status', 'when', 'payload'), (), 'event.' + name)
            fields(event['payload'], (), None, name + '.payload')
            events[name] = {'status': 'bound', 'source': 'shared.events.' + name,
                            'when': predicate(event['when']),
                            'payload': {key: reference(ref) for key, ref in event['payload'].items()}}
        else:
            raise SpecificationError(f'{name}: unsupported binding status')
    observations = {name: reference(ref) for name, ref in fields(document['observations'], (), None, 'observations').items()}
    obligations = set()
    for section in ('system', 'execution'):
        source = fields(documents[section], ('verification',), None, section)
        obligations.update(f'{section}.verification.{name}' for name in
                           fields(source['verification'], (), None, section + '.verification'))
    checks = fields(document['checks'], (), None, 'verification checks')
    if set(checks) != obligations:
        raise SpecificationError('Verification checks must cover every obligation exactly once: '
                                 f'missing {sorted(obligations - checks.keys())}; '
                                 f'unknown {sorted(checks.keys() - obligations)}')
    for ref, check in checks.items():
        fields(check, ('scope', 'events', 'observations', 'note'), (), ref)
        if check['scope'] not in ('module', 'system', 'both'):
            raise SpecificationError(f'{ref}: invalid verification scope')
        text_value(check['note'], ref + '.note')
        if set(names(check['events'], ref)) - events.keys() or set(names(check['observations'], ref)) - observations.keys():
            raise SpecificationError(f'{ref}: unknown event/observation binding')
        if not check['events'] and not check['observations']:
            raise SpecificationError(f'{ref}: check has no declared observations')
    return {'status': 'partial' if limitations else 'complete', 'sampling': dict(sampling, clock_reset=shared['clock_reset']),
            'events': events, 'observations': observations, 'checks': checks, 'limitations': limitations}
