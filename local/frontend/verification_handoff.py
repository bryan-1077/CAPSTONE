"""Portable frontend inputs for a downstream verification consumer.

Exports contracts/intent and authenticates files; it never executes UVM/vector
tests or treats successful generation as behavioral verification.
"""
from __future__ import annotations

import argparse
import copy
from functools import lru_cache
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath

from specification import SpecificationError, fields, read_document, validate_contents
from verification_bindings import elaborate_bindings


SCHEMA = 'capstone.verification_handoff'
VERSION = 1
CONSUMER = {
    'name': 'existing_controller_system_verifier', 'status': 'adapter_required',
    'accepted_manifest_schema': 'legacy controller manifest v2',
    'blocking_reasons': [
        {'code': 'NEW_HANDOFF_SCHEMA', 'detail': 'The existing discovery entry point does not read this handoff schema.'},
        {'code': 'ISOLATED_BUILD_TRANSPORT', 'detail': 'The current orchestrator publisher reads frontend-root rtl_output/manifest.json; it does not select an isolated architecture build or this handoff automatically.'},
        {'code': 'LEGACY_DISCOVERY', 'detail': 'System discovery expects legacy component configuration/master specs, a frontend testbench, and clock/reset inferred from sequential logic in the top.'},
        {'code': 'PRIVATE_WRAPPER_OBSERVATIONS', 'detail': 'The existing UVM adapter expects legacy top-level private signals; this export binds declared module ports.'},
        {'code': 'FIXED_BEHAVIOR_AND_REQUIREMENTS', 'detail': 'Existing system reference semantics permit pre-dispatch reordering and build their own SYS-REQ catalog. New requirement IDs and semantics need explicit consumer support.'},
    ],
    'inspected_sources': ['shared/controller_manifest.py', 'shared/top_orchestrator/mailbox.py',
                          'shared/top_orchestrator/nodes.py', 'server/validation/controller_verification.py',
                          'server/validation/requirement_verification.py'],
}


def _digest(value):
    from architecture import fingerprint
    return fingerprint(value)


def _path(root, value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise SpecificationError('Handoff paths must be relative POSIX file paths')
    relative = PurePosixPath(value)
    if relative.is_absolute() or '..' in relative.parts:
        raise SpecificationError('Handoff path escapes the export directory')
    path = root.joinpath(*relative.parts)
    if any(p.is_symlink() for p in (path, *path.parents) if p != root and p.is_relative_to(root)):
        raise SpecificationError('Handoff inputs must not contain symlinks')
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise SpecificationError(f'Missing or nonportable handoff input: {value}')
    return path


@lru_cache(maxsize=1)
def _declaration_reader():
    # Reuse the existing inventory scanner without changing its legacy consumer.
    source = Path(__file__).resolve().parents[2] / 'shared/controller_manifest.py'
    spec = importlib.util.spec_from_file_location('_handoff_controller_inventory', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.module_declarations


def build_handoff(manifest, architecture_path, spec_paths, *, phase='review'):
    from architecture import validate_architecture
    from specification_lowering import is_specification_manifest, require_approval
    validate_architecture(manifest)
    if not is_specification_manifest(manifest) or phase not in ('review', 'rtl'):
        raise SpecificationError('Handoff export needs a specification-derived manifest and review/rtl phase')
    architecture_path = Path(architecture_path).absolute()
    root = architecture_path.parent.resolve()
    if read_document(_path(root, architecture_path.name)) != manifest:
        raise SpecificationError('Exported architecture differs from the reviewed manifest')
    proof = manifest['provenance']['specification']
    snapshot = proof['snapshot']
    package, documents = snapshot['package'], snapshot['documents']
    evidence = validate_contents(package, documents, snapshot['timing_library'], snapshot['overrides'], snapshot['profile'])
    bindings = elaborate_bindings(documents, evidence['parameters'], evidence['type_widths'])
    context = manifest['modules'][0]['spec']['behavior']['specification_context']
    inventory = []

    def add(path, role):
        path = Path(path).absolute()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise SpecificationError('Handoff input is outside its export directory') from exc
        actual = _path(root, relative)
        if any(row['path'] == relative for row in inventory):
            raise SpecificationError('Duplicate handoff input: ' + relative)
        data = actual.read_bytes()
        inventory.append({'path': relative, 'role': role, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
        return relative

    add(architecture_path, 'architecture')
    expected_specs = {m['spec']['design_name']: m for m in manifest['modules']}
    actual_specs = {}
    for path in spec_paths:
        relative = add(path, 'module_spec')
        spec = read_document(root / relative)
        name = spec.get('design_name')
        if name not in expected_specs or name in actual_specs:
            raise SpecificationError('Unexpected or duplicate handoff module specification')
        module = expected_specs[name]
        expected = copy.deepcopy(module['spec'])
        expected['verification_requirements'] = [r for r in manifest['requirements'] if module['instance'] in r['owners']]
        if spec != expected:
            raise SpecificationError('Handoff module specification differs from its approved contract: ' + name)
        actual_specs[name] = relative
    if set(actual_specs) != set(expected_specs):
        raise SpecificationError('Handoff is missing module specifications')
    rtl_revision, rtl_files, declarations = None, [], []
    if phase == 'rtl':
        from dataclasses import asdict
        from validator import validate_spec
        approval = root / 'specification_approval.json'
        require_approval(manifest, approval)
        add(approval, 'approval')
        add(root / 'verification_plan.json', 'verification_plan')
        filelist = root / 'rtl_output/filelist.f'
        add(filelist, 'filelist')
        files = [line.strip() for line in filelist.read_text().splitlines() if line.strip()]
        expected_files = {name + '.sv' for name in [*expected_specs, manifest['design_name']]}
        if set(files) != expected_files or len(files) != len(expected_files):
            raise SpecificationError('Handoff filelist differs from the architecture RTL inventory')
        for filename in files:
            relative = add(root / 'rtl_output' / filename, 'rtl')
            rtl_files.append(relative)
            declarations.extend({'name': name, 'file': relative} for name in _declaration_reader()((root / relative).read_text()))
            expected_name = Path(filename).stem
            if sum(row['name'] == expected_name and row['file'] == relative for row in declarations) != 1:
                raise SpecificationError('Missing primary RTL module declaration: ' + expected_name)
        declared_names = [row['name'] for row in declarations]
        if len(declared_names) != len(set(declared_names)):
            raise SpecificationError('Duplicate RTL module definitions in handoff')
        for name in expected_specs:
            ir = root / 'microarch/ir' / (name + '_ir.json')
            relative = add(ir, 'module_ir')
            expected_ir = asdict(validate_spec(str(root / actual_specs[name])))
            if read_document(root / relative) != expected_ir:
                raise SpecificationError('Handoff IR differs from its current module specification')
        rtl_revision = _digest({row['path']: row['sha256'] for row in inventory if row['role'] == 'rtl'})
    requirements, obligations = {}, {}
    for section in ('system', 'execution'):
        for name, rule in documents[section]['rules'].items():
            ref = f'{section}.rules.{name}'
            requirements[ref] = dict(rule, id=name, result='unverified')
        for name, obligation in documents[section]['verification'].items():
            ref = f'{section}.verification.{name}'
            obligation_rules = [f'{section}.rules.{rid}' for rid in obligation['rules']]
            owners = sorted({owner for rule in obligation_rules for owner in requirements[rule]['owners']})
            obligations[ref] = dict(obligation, id=name, rules=obligation_rules, owners=owners,
                                    bindings=bindings['checks'].get(ref), result='unverified')
    result = {
        'schema': SCHEMA, 'schema_version': VERSION, 'phase': phase, 'path_base': 'handoff_directory',
        'identity': {'package_id': package['package_id'], 'revision': package['revision'],
                     'package_fingerprint': proof['package_fingerprint'], 'manifest_fingerprint': _digest(manifest),
                     'rtl_revision': rtl_revision},
        'design': {'top_module': manifest['design_name'], 'top_ports': manifest['top_ports'],
                   'modules': [{'instance': m['instance'], 'module_name': m['spec']['design_name'], 'roles': m['roles'],
                                'ports': m['spec']['ports'], 'clock': m['spec']['clock'], 'reset': m['spec']['reset'],
                                'specification': actual_specs[m['spec']['design_name']]} for m in manifest['modules']],
                   'connections': manifest['connections'], 'module_definitions': declarations},
        'shared': {key: context[key] for key in ('parameters', 'types', 'clock_reset', 'timing_profile', 'timing_rules')},
        'verification_intent': {'requirements': requirements, 'obligations': obligations,
                                'events': documents['shared']['events'], 'bindings': bindings,
                                'decisions': package['decisions'], 'assumptions': package['assumptions'],
                                'strategy': package['verification_strategy']},
        'inputs': sorted(inventory, key=lambda row: row['path']), 'rtl_files': rtl_files,
        'execution': {'rtl_generation': 'not_run' if phase == 'review' else 'artifacts_present',
                      'uvm': 'not_run', 'vectors': 'not_run', 'behavioral_verification': 'not_run'},
        'consumer_compatibility': copy.deepcopy(CONSUMER),
        'result_contract': {
            'schema': 'capstone.verification_results', 'schema_version': 1,
            'identity': {'handoff_fingerprint': 'copy from this handoff',
                         'required_fields': ['package_fingerprint', 'manifest_fingerprint', 'rtl_revision']},
            'requirement_keys': list(requirements), 'obligation_keys': list(obligations),
            'record_fields': ['status', 'stage', 'test_ids', 'evidence'],
            'allowed_status': ['passed', 'failed', 'unverified', 'not_applicable'],
            'rules': ['Preserve source IDs; any consumer-ID translation must be explicit.',
                      'Only an rtl-phase handoff with an RTL revision can identify a DUT for runtime verification.',
                      'Missing records remain unverified; successful generation/compilation is not behavioral evidence.',
                      'Passed requires executed checks and evidence; not_applicable requires a justified configuration-specific reason.',
                      'Identity mismatch, duplicate/unknown IDs, or missing evidence must not be accepted as a pass.'],
        },
    }
    result['handoff_fingerprint'] = _digest(result)
    return result


def emit_handoff(manifest, architecture_path, spec_paths, *, phase='review', output=None):
    architecture_path = Path(architecture_path).absolute()
    output = Path(output).absolute() if output is not None else architecture_path.with_suffix('.handoff.json')
    if output.parent != architecture_path.parent or output == architecture_path:
        raise SpecificationError('Handoff output must be a distinct file beside its architecture')
    result = build_handoff(manifest, architecture_path, spec_paths, phase=phase)
    if any(output.name == row['path'] for row in result['inputs']):
        raise SpecificationError('Handoff output would overwrite an input')
    output.write_text(json.dumps(result, indent=2) + '\n')
    return output


def validate_handoff(path):
    """Check portable identity and contract fidelity; never run verification."""
    path = Path(path).absolute()
    root, document = path.parent.resolve(), read_document(path)
    fields(document, ('schema', 'schema_version', 'phase', 'path_base', 'identity', 'design', 'shared',
                      'verification_intent', 'inputs', 'rtl_files', 'execution', 'consumer_compatibility',
                      'result_contract', 'handoff_fingerprint'), (), 'verification handoff')
    if document.get('schema') != SCHEMA or type(document.get('schema_version')) is not int or document['schema_version'] != VERSION:
        raise SpecificationError('Unsupported verification handoff schema')
    if document.get('handoff_fingerprint') != _digest({k: v for k, v in document.items() if k != 'handoff_fingerprint'}):
        raise SpecificationError('Verification handoff content changed')
    if not isinstance(document['inputs'], list):
        raise SpecificationError('Handoff inputs must be a list')
    architecture, specs = None, []
    for row in document['inputs']:
        fields(row, ('path', 'role', 'sha256', 'bytes'), (), 'handoff input')
        source = _path(root, row['path'])
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != row['sha256'] or len(data) != row['bytes']:
            raise SpecificationError('Handoff input hash changed: ' + row['path'])
        if row['role'] == 'architecture':
            if architecture is not None:
                raise SpecificationError('Duplicate architecture input')
            architecture = source
        elif row['role'] == 'module_spec':
            specs.append(source)
    if architecture is None:
        raise SpecificationError('Handoff is missing its architecture snapshot')
    expected = build_handoff(read_document(architecture), architecture, specs, phase=document['phase'])
    if document != expected:
        raise SpecificationError('Handoff differs from the reviewed architecture/verification intent')
    return {'handoff_validation': 'passed', 'phase': document['phase'],
            'consumer_compatibility': document['consumer_compatibility']['status'],
            'behavioral_verification': 'not_run', 'identity': document['identity']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('handoff', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(validate_handoff(args.handoff), indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Handoff validation failed: {exc}\n')


if __name__ == '__main__':
    main()
