"""Rebuild contracts and lint externally repaired RTL without model calls."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import shutil

from architecture import lower_architecture, validate_architecture
from specification import read_document
from verification_handoff import emit_handoff, validate_handoff


def recheck(candidate, output):
    candidate, output = Path(candidate).resolve(), Path(output).absolute()
    if output.exists() or output.resolve().is_relative_to(candidate):
        raise ValueError('Recheck requires a new directory outside the candidate')
    manifest = read_document(candidate / 'architecture.yaml')
    validate_architecture(manifest)
    specs = lower_architecture(manifest, output,
                               specification_approval=candidate / 'specification_approval.json')
    from validator import validate_spec
    ir = output / 'microarch/ir'
    ir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        (ir / (spec.stem + '_ir.json')).write_text(json.dumps(asdict(validate_spec(str(spec))), indent=2) + '\n')
    # Regenerate all contracts/IR/wiring from the exact approved architecture.
    # The structural wrapper cannot be patched independently of that architecture.
    rtl = output / 'rtl_output'
    expected = (rtl / 'filelist.f').read_text().splitlines()
    supplied = {p.name for p in (candidate / 'rtl_output').iterdir() if p.suffix in ('.sv', '.v')}
    if supplied != set(expected):
        raise ValueError('Candidate RTL differs from the approved compile inventory')
    top = manifest['design_name'] + '.sv'
    if (candidate / 'rtl_output' / top).read_bytes() != (rtl / top).read_bytes():
        raise ValueError('Structural wrapper differs from approved architecture')
    for name in expected:
        source = candidate / 'rtl_output' / name
        if source.is_symlink():
            raise ValueError('Repaired RTL cannot be symlinked')
        shutil.copyfile(source, rtl / name)
    # Preserve approved serialization after comparing regenerated contracts/IR.
    for source in candidate.rglob('*'):
        if source.is_file() and source.suffix in ('.json', '.yaml', '.yml'):
            target = output / source.relative_to(candidate)
            if not target.is_file() or read_document(source) != read_document(target):
                raise ValueError('Candidate contract/IR differs from deterministic lowering: ' + str(source))
            shutil.copyfile(source, target)
    from dual_llm_rtlGen import _run_verilator_lint_file_set, LINT_MODE_INTEGRATION
    result = _run_verilator_lint_file_set([str(rtl / name) for name in expected],
                                         LINT_MODE_INTEGRATION, manifest['design_name'])
    (output / 'integration_lint.json').write_text(json.dumps(result, indent=2) + '\n')
    if not result or result.get('ok') is not True:
        raise ValueError('Repaired integration lint failed')
    emit_handoff(manifest, output / 'architecture.yaml', specs, phase='rtl',
                 output=output / 'verification_handoff.json')
    validate_handoff(output / 'verification_handoff.json')
    status = {'structural_validation': 'passed', 'rtl_generation': 'passed',
              'specification_approval': 'approved', 'integration_lint': 'passed',
              'verification_handoff': 'exported', 'behavioral_verification': 'not_run',
              'rtl_origin': 'externally_repaired', 'backend_ready': False}
    (output / 'build_status.json').write_text(json.dumps(status, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    recheck(args.candidate, args.output)
