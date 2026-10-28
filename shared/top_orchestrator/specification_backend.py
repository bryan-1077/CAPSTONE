"""Prepare an offline Experimental backend handoff; never execute backend tools."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from local.backend.services.mailbox_snapshot import freeze_mailbox
from shared.controller_manifest import module_declarations
from .frequency import positive_mhz
from .mailbox import file_hashes
from .specification_mailbox import read_json, relative_file, verify_specification


def handoff_contract(mailbox, target_mhz=None):
    mailbox = Path(mailbox).resolve(strict=True)
    metadata = verify_specification(mailbox)
    inventory = read_json(mailbox / 'verification_inventory.json')
    native = read_json(mailbox / 'handoff/verification_handoff.json')
    target = positive_mhz(str(inventory['target_mhz']))
    if target_mhz is not None and positive_mhz(str(target_mhz)) != target:
        raise ValueError('Backend target conflicts with the published controller target')
    clock_reset = native['shared']['clock_reset']
    ports = {row['name']: row for row in native['design']['top_ports']}
    for role in ('clock', 'reset'):
        port = ports.get(clock_reset[role], {})
        if port.get('direction') != 'input' or port.get('width') != 1:
            raise ValueError('Backend requires an explicit scalar top-level ' + role + ' input')
    if clock_reset['edge'] != 'rising':
        raise ValueError('Unsupported backend clock edge')
    top = inventory['top_module']
    order = inventory['compile_order']
    normalized = [Path(name).relative_to('rtl').as_posix() for name in order]
    if not order or len(set(normalized)) != len(order):
        raise ValueError('Backend compile order is empty or duplicated')
    declarations = {}
    for name in order:
        for module in module_declarations(relative_file(mailbox, name).read_text()):
            if module in declarations:
                raise ValueError('Duplicate RTL module declaration: ' + module)
            declarations[module] = name
    if top not in declarations:
        raise ValueError('Backend top module is missing from the compile inventory')
    filelist = (mailbox / 'rtl/filelist.f').read_text().splitlines()
    if filelist != normalized:
        raise ValueError('Backend filelist differs from approved compile order')
    sources = {p.relative_to(mailbox / 'rtl').as_posix()
               for p in (mailbox / 'rtl').rglob('*') if p.suffix.lower() in ('.sv', '.v')}
    if sources != set(normalized):
        raise ValueError('Backend RTL tree differs from the compile inventory')
    return {'schema': 'capstone.specification_backend_handoff', 'schema_version': 1,
            'frontend_flow': 'specification',
            'snapshot_sha256': hashlib.sha256((mailbox / 'snapshot.json').read_bytes()).hexdigest(),
            'rtl_revision': metadata['rtl_revision'], 'top_module': top,
            'target_mhz': target, 'clock_period_ns': 1000.0 / target,
            'clock_reset': clock_reset,
            'source_compile_order': order, 'staged_compile_order': normalized,
            'filelist': 'rtl/filelist.f', 'staged_rtl_directory': 'rtl',
            'manifest_path_base': 'source_mailbox_root',
            'checks': {'snapshot_authentication': 'passed', 'top_inventory': 'passed',
                       'clock_reset_ports': 'passed', 'compile_order': 'passed'},
            'remote_prepare_compatibility': 'not_established',
            'system_verification': 'not_accepted', 'physical_design': 'not_run',
            'backend_ready': False}


def prepare_offline_handoff(mailbox, destination, target_mhz=None):
    mailbox, destination = Path(mailbox).resolve(), Path(destination).absolute()
    if destination.resolve().is_relative_to(mailbox):
        raise ValueError('Offline handoff must be outside the immutable mailbox')
    if destination.exists():
        raise FileExistsError(destination)
    contract = handoff_contract(mailbox, target_mhz)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.offline-backend-', dir=destination.parent) as directory:
        stage = Path(directory) / 'handoff'
        stage.mkdir()
        frozen = freeze_mailbox(mailbox, stage / 'rtl')
        # Reauthenticate after freezing, then bind the bytes to the same publication.
        if handoff_contract(mailbox, target_mhz) != contract:
            raise ValueError('Source publication changed while preparing backend handoff')
        if any(digest != hashlib.sha256((mailbox / 'rtl' / name).read_bytes()).hexdigest()
               for name, digest in frozen['mailbox_file_hashes'].items()):
            raise ValueError('Frozen backend bytes differ from selected publication')
        contract.update(mailbox_revision=frozen['mailbox_revision'],
                        mailbox_file_hashes=frozen['mailbox_file_hashes'])
        (stage / 'backend_handoff.json').write_text(json.dumps(contract, indent=2) + '\n')
        if destination.exists():
            raise FileExistsError(destination)
        stage.rename(destination)
    return contract


def verify_offline_handoff(mailbox, destination):
    destination = Path(destination)
    expected = handoff_contract(mailbox)
    with tempfile.TemporaryDirectory(prefix='backend-handoff-check-') as directory:
        frozen = freeze_mailbox(mailbox, Path(directory) / 'rtl')
    expected.update(mailbox_revision=frozen['mailbox_revision'],
                    mailbox_file_hashes=frozen['mailbox_file_hashes'])
    if read_json(destination / 'backend_handoff.json') != expected:
        raise ValueError('Offline handoff differs from the authenticated source contract')
    actual = file_hashes(destination)
    actual.pop('backend_handoff.json', None)
    if actual != {'rtl/' + name: digest for name, digest in frozen['mailbox_file_hashes'].items()}:
        raise ValueError('Offline backend transport contents changed')
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mailbox', type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output', type=Path)
    mode.add_argument('--verify', type=Path, help='Recheck an existing offline handoff against its source mailbox')
    parser.add_argument('--target-mhz', type=positive_mhz)
    args = parser.parse_args()
    if args.verify:
        if args.target_mhz is not None:
            parser.error('--target-mhz applies only when preparing --output')
        result = verify_offline_handoff(args.mailbox, args.verify)
    else:
        result = prepare_offline_handoff(args.mailbox, args.output, args.target_mhz)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
