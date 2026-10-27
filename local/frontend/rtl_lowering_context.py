"""RTL-stage interpretation of compiler-elaborated specification facts.

This adds context, never filters reviewer findings or grants RTL approval.
"""
import copy


RTL_STAGE_POLICY = '''
ELABORATED SPECIFICATION ROUTE — APPLY SOURCE RULES AT THEIR CORRECT STAGE:
The rtl_lowering facts describe this concrete build. Shared schema type names,
YAML reference paths and timing_cycles bindings belong to specification authoring
and elaboration. Check RTL against their resolved widths, bit offsets, enum
encodings, values and behavior. Numeric localparams/constants are valid ways to
implement resolved timing values; do not require YAML lookup syntax in RTL or an
extra array merely named timing_cycles. A wrong timing value remains a violation.
Shared type identities do not by themselves require matching SystemVerilog typedef
names. Flat packed vectors, packed structs or aliases may implement the same ABI.
Internal unpacked storage is allowed when public layout and behavior are preserved.
For a packed row_t, `typedef row_t [3:0] bank_rows_t;` is packed; a dimension after
the new typedef name (`typedef row_t bank_rows_t [3:0];`) is unpacked. Judge actual
width, packing and bank/field order; do not infer them from a type's spelling.
Use this build's elaborated widths; legacy 51-bit queue/request examples elsewhere
do not impose widths or scheduler behavior on a different declared contract.
Honor any EXPLICIT RTL-language/style/typedef requirement in the context; these
stage rules do not waive it. Preserve every behavioral, ownership, reset, timing,
handshake, public-interface and explicit implementation constraint. If a genuine
conflict remains between an explicit RTL directive and elaboration, report AMBIGUITY.
For SPEC_VIOLATION, identify the applicable context rule and concrete RTL mismatch
or observable failing case. A reviewer label alone is not proof; merger edits must
remain grounded in context. Do not redesign correct logic to satisfy a schema-only
or contradicted interpretation. Lint success alone is not behavioral approval.
'''.strip()


def lowering_facts(context, behavior):
    if not isinstance(behavior, dict):
        return None
    source = behavior.get('specification_context')
    contract = behavior.get('contract')
    if not isinstance(source, dict) or not isinstance(contract, dict) or not source.get('package_fingerprint'):
        return None
    return copy.deepcopy({
        'fact_version': 1,
        'scope': 'concrete_compiler_elaboration_not_candidate_verification',
        'package_fingerprint': source['package_fingerprint'],
        'public_ports': context['module']['ports'],
        'port_source_types': {name: value.get('type') for name, value in contract.get('interfaces', {}).items()},
        'source_type_layouts': source.get('types', {}),
        'resolved_parameters': source.get('parameters', {}),
        'resolved_timing_profile': source.get('timing_profile', {}),
        'schema_alone_requires_sv_typedef_spelling': False,
        'schema_reference_paths_apply_at': 'specification_authoring_and_elaboration',
        'rtl_must_preserve': ['public widths and bit/array ordering', 'resolved configured timing values',
                              'behavior and explicit RTL implementation constraints'],
    })


def scoped_system_prompt(system_prompt, bundle):
    """Legacy modules retain their original system prompts verbatim."""
    if bundle.get('ctx', {}).get('rtl_lowering') is None:
        return system_prompt
    return system_prompt + '\n\n' + RTL_STAGE_POLICY
