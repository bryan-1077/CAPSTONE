"""Compact local planning evidence; a catalog entry is not verification approval."""
import copy
import hashlib

from architecture import LIBRARY, fingerprint, library_context


def planning_catalog():
    from specification_lowering import ROLE_NAMES
    library = library_context()
    sources = {name: hashlib.sha256((LIBRARY / name).read_bytes()).hexdigest()
               for name in ('feature_templates.yaml', 'jedec_dictionary.yaml')}
    entries = {}
    for feature, template in library['templates'].items():
        entries[feature] = {
            'description': template.get('description', ''),
            'protocol': template.get('protocol'),
            'timer_parameters': copy.deepcopy(template.get('timer_parameters', {})),
            'submodules': [{key: copy.deepcopy(block[key]) for key in
                           ('name', 'description', 'design_type', 'clock', 'reset', 'ports') if key in block}
                          for block in template.get('submodules', [])],
            'combinational_dependencies': copy.deepcopy(library['combinational_dependencies'].get(feature)),
            'template_fingerprint': fingerprint(template),
            'verification_status': 'not_established_by_catalog',
        }
    notes = [line.strip() for line in (LIBRARY/'jedec_dictionary.yaml').read_text().splitlines()
             if any(tag in line for tag in ('Project simplification:', 'Density assumption', 'Speed bin chosen'))]
    result = {'catalog_version': 1, 'timing_source_notes': notes,
              'accepted_contract_roles': sorted(set(ROLE_NAMES) | set(ROLE_NAMES.values())), 'source_sha256': sources,
              'dependency_fingerprint': fingerprint(library['combinational_dependencies']),
              'policy': {
                  'authority': 'Timing profiles are project data; inspect documented assumptions/simplifications. '
                               'Catalog membership is not JEDEC compliance or behavioral approval.',
                  'reuse': 'Optional implementation references, not a supported-feature menu. '
                           'Custom contracts remain allowed within compiler capabilities.',
                  'specification_route': 'Declare complete contracts in the normal specification schema; '
                                         'do not add direct-architecture reuse fields.',
                  'detail': 'Summaries omit implementation bodies. Do not assume omitted behavior; '
                            'require a complete selected contract before treating a block as equivalent.'},
              'entries': entries}
    result['catalog_fingerprint'] = fingerprint(result)
    return result


def planning_index(catalog):
    """Small discovery index; full interface/provenance catalog stays in artifacts."""
    return {'catalog_fingerprint': catalog['catalog_fingerprint'],
            'optional_unverified_features': list(catalog['entries'])}
