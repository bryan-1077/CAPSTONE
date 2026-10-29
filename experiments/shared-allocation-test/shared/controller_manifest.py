"""Portable controller input inventory, discovery and snapshot transport (Python 3.6).

This module never compiles RTL or runs verification. Paths in manifest v2 are
relative to the manifest directory and must stay inside its declared run root.
"""
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import yaml


SYSTEM_TOP = 'ddr4_controller_top'


def read_mapping(path):
    try:
        with Path(path).open() as handle:
            value = json.load(handle) if str(path).endswith('.json') else yaml.safe_load(handle)
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError('Invalid metadata {}: {}'.format(path, exc))
    if not isinstance(value, dict):
        raise ValueError('Expected a mapping: ' + str(path))
    return value


def module_declarations(source):
    # Preserve newlines while removing comments and strings. In particular,
    # documentation examples must not become module definitions.
    clean = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                   lambda m: '\n' * m.group().count('\n'), source)
    return re.findall(r'\bmodule\s+(?:(?:automatic|static)\s+)?([A-Za-z_][A-Za-z0-9_$]*)(?=[\s#(;])', clean)


def relative_path(path, directory):
    return Path(os.path.relpath(str(Path(path).resolve()), str(Path(directory).resolve()))).as_posix()


def top_clock_reset(source):
    clean = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/', '', source)
    events = re.findall(r'\balways(?:_ff)?\s*@\s*\(([^)]*)\)', clean)
    clocks = {m.group(1) for event in events
              for m in [re.search(r'\bposedge\s+(\w+)', event)] if m}
    resets = re.findall(r'\bif\s*\(\s*(!|~)?\s*(\w*(?:rst|reset)\w*)\s*\)', clean, re.I)
    if len(clocks) != 1 or len(set(resets)) != 1:
        raise ValueError('Cannot identify a unique controller clock/reset from the generated top.')
    clock = next(iter(clocks))
    negation, reset = resets[0]
    asynchronous = any(re.search(r'\b(?:posedge|negedge)\s+' + re.escape(reset) + r'\b', e)
                       for e in events)
    return {'name': clock, 'edge': 'posedge'}, {
        'name': reset, 'active_low': bool(negation), 'synchronous': not asynchronous}


def build_manifest(frontend_root, rtl_directory, rtl_files, filelist, top_module,
                   controller_config, configuration):
    root, directory = Path(frontend_root).resolve(), Path(rtl_directory).resolve()
    paths = [directory / p for p in rtl_files]
    if not paths:
        raise ValueError('No generated RTL files found.')
    definitions = [{'name': name, 'file': relative_path(path, directory)}
                   for path in paths for name in module_declarations(path.read_text())]
    names = sorted({row['name'] for row in definitions})
    tops = [row for row in definitions if row['name'] == top_module]
    if len(tops) != 1:
        raise ValueError('Expected one generated definition of ' + top_module)
    top_path = directory / tops[0]['file']
    clock, reset = top_clock_reset(top_path.read_text())

    microarch = root / 'microarch'
    expanded = microarch / 'gen_exp' if microarch.exists() else root / 'expanded'
    generated = microarch / 'gen_basic' if microarch.exists() else root / 'inputs' / 'generated'
    ir_directory = microarch / 'ir' if microarch.exists() else root / 'ir'
    jedec_directory = microarch / 'jedec' if microarch.exists() else root / 'jedec'

    specs, groups, parameters = [], set(), {}
    for path in sorted(expanded.rglob('*.yaml')):
        if path.name == 'master.yaml' or path.name.endswith('_master.yaml'):
            continue
        data = read_mapping(path)
        name = data.get('design_name')
        if name in names:
            specs.append({'module_name': name, 'path': relative_path(path, directory)})
            groups.add(path.parent)
            values = data.get('behavior', {}).get('rtl_parameters', {})
            if values:
                parameters[name] = values
    masters = [p for p in sorted(expanded.rglob('*.yaml'))
               if (p.name == 'master.yaml' or p.name.endswith('_master.yaml'))
               and p.parent in groups]
    group_names = {read_mapping(p)['design_name'] for p in masters}
    config_sources = [p for p in sorted(generated.glob('*.yaml'))
                      if read_mapping(p).get('design_name') in group_names]
    ir_files = [p for p in sorted(ir_directory.glob('*.json'))
                if read_mapping(p).get('design_name') in names]
    config = read_mapping(controller_config).get('controller_config', {})
    profile_name = config.get('jedec_profile')
    dictionary_path = jedec_directory / 'jedec_dictionary.yaml'
    profile = read_mapping(dictionary_path).get(profile_name)
    if not isinstance(profile, dict):
        raise ValueError('Missing generated JEDEC profile: ' + str(profile_name))
    protocol = config.get('protocol')
    prefix = str(protocol) + '-'
    if not isinstance(profile_name, str) or not profile_name.startswith(prefix):
        raise ValueError('Generated protocol and JEDEC profile disagree.')
    configuration = dict(configuration, protocol=protocol,
                         memory_speed=profile_name[len(prefix):])
    clock['profile_frequency_mhz'] = profile['clock_frequency_mhz']
    return {
        'schema_version': 2, 'design_name': top_module, 'top_module': top_module,
        'path_base': 'manifest_directory', 'run_root': relative_path(root, directory),
        'filelist': relative_path(filelist, directory),
        'rtl_files': [relative_path(p, directory) for p in paths],
        'modules': names, 'generated_modules': names, 'module_definitions': definitions,
        'hierarchy_analysis': 'deferred_to_agent1',
        # Preserve the v2 legacy key for existing consumers. This is one
        # component's input, not the complete normalized controller contract.
        'controller_config': relative_path(controller_config, directory),
        'primary_component_config': relative_path(controller_config, directory),
        'configuration_sources': [relative_path(p, directory) for p in config_sources],
        'module_specs': specs,
        'modules_without_specs': sorted(set(names) - {s['module_name'] for s in specs}),
        'spec_groups': [relative_path(p, directory) for p in masters],
        'ir_files': [relative_path(p, directory) for p in ir_files],
        'system_testbench': relative_path(root / 'tb' / ('tb_' + top_module + '.sv'), directory),
        'clock': clock, 'reset': reset, 'configuration': configuration,
        'timing': {'jedec_profile': profile_name,
                   'dictionary': relative_path(dictionary_path, directory),
                   'profile': profile, 'module_parameters': parameters},
    }


def find_manifest(inputs):
    candidates = set()
    for value in inputs:
        path = Path(value).resolve()
        possible = [path] if path.is_file() and path.name == 'manifest.json' else []
        if path.is_dir():
            possible += [path / 'manifest.json', path / 'rtl_output' / 'manifest.json',
                         path / 'rtl' / 'manifest.json']
        for candidate in possible:
            if candidate.is_file() and read_mapping(candidate).get('top_module') == SYSTEM_TOP:
                candidates.add(candidate)
    if len(candidates) > 1:
        raise ValueError('Multiple controller manifests found; select one frontend run.')
    return next(iter(candidates)) if candidates else None


def resolve_path(manifest_path, manifest, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute() or '\\' in value:
        raise ValueError('Manifest paths must be nonempty relative POSIX paths: ' + str(value))
    directory = Path(manifest_path).resolve().parent
    root = (directory / manifest['run_root']).resolve()
    path = (directory / value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError('Manifest path escapes the frontend run root: ' + value)
    if not path.is_file():
        raise ValueError('Missing system input: ' + str(path))
    return path


def manifest_paths(manifest):
    values = [manifest['filelist'], manifest['controller_config'], manifest['system_testbench'],
              manifest['timing']['dictionary']]
    values += manifest['rtl_files'] + manifest['configuration_sources']
    values += manifest['spec_groups'] + manifest['ir_files']
    values += [row['path'] for row in manifest['module_specs']]
    return sorted(set(values))


def discover_system(manifest_path):
    path = Path(manifest_path).resolve()
    manifest = read_mapping(path)
    if manifest.get('schema_version') != 2 or manifest.get('path_base') != 'manifest_directory':
        raise ValueError('Controller manifest v2 is required. Refresh the frontend handoff with '
                         'generate_wrapper.py --manifest-only.')
    if manifest.get('top_module') != SYSTEM_TOP:
        raise ValueError('Expected system top ' + SYSTEM_TOP)
    if not isinstance(manifest.get('run_root'), str) or Path(manifest['run_root']).is_absolute():
        raise ValueError('run_root must be a relative path.')
    required = ('rtl_files', 'generated_modules', 'modules', 'module_definitions',
                'module_specs', 'configuration_sources', 'spec_groups', 'ir_files',
                'modules_without_specs')
    for field in required:
        if not isinstance(manifest.get(field), list):
            raise ValueError('Manifest needs a list: ' + field)
    if not manifest['rtl_files'] or len(set(manifest['rtl_files'])) != len(manifest['rtl_files']):
        raise ValueError('Manifest must list a nonempty, unique RTL file set.')
    if not manifest['module_specs']:
        raise ValueError('Manifest has no module specifications.')
    if manifest.get('primary_component_config', manifest['controller_config']) != manifest['controller_config']:
        raise ValueError('primary_component_config must alias the legacy controller_config path.')
    resolved = {value: resolve_path(path, manifest, value) for value in manifest_paths(manifest)}
    actual = [{'name': name, 'file': value} for value in manifest['rtl_files']
              for name in module_declarations(resolved[value].read_text())]
    if actual != manifest['module_definitions']:
        raise ValueError('Module declaration inventory changed; refresh the frontend manifest.')
    names = sorted({row['name'] for row in actual})
    if names != manifest['generated_modules'] or names != manifest['modules'] or SYSTEM_TOP not in names:
        raise ValueError('Manifest module names do not match actual RTL declarations.')
    listed = [line.strip() for line in resolved[manifest['filelist']].read_text().splitlines() if line.strip()]
    if listed != manifest['rtl_files']:
        raise ValueError('Manifest RTL files and filelist disagree.')
    for spec in manifest['module_specs']:
        if read_mapping(resolved[spec['path']]).get('design_name') != spec['module_name']:
            raise ValueError('Module specification identity mismatch: ' + spec['path'])
        if spec['module_name'] not in names:
            raise ValueError('Specification names a module absent from RTL: ' + spec['module_name'])
    spec_names = [s['module_name'] for s in manifest['module_specs']]
    if len(spec_names) != len(set(spec_names)):
        raise ValueError('Multiple specifications found for the same generated module.')
    if sorted(set(names) - set(spec_names)) != manifest['modules_without_specs']:
        raise ValueError('modules_without_specs does not match the specification inventory.')
    # Master specs form a portable dependency index; do not silently repair old
    # machine-specific paths while consuming a handoff.
    for value in manifest['spec_groups']:
        master = resolved[value]
        for child in read_mapping(master).get('submodules', []):
            child_path = child['spec']
            if Path(child_path).is_absolute():
                raise ValueError('Nonportable master YAML spec path: ' + child_path)
            target = (master.parent / child_path).resolve()
            if target not in resolved.values():
                raise ValueError('Master YAML dependency absent from handoff: ' + child_path)
    cfg = manifest['configuration']
    for key in ('protocol', 'memory_speed', 'scheduler', 'page_policy'):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            raise ValueError('Missing system configuration: ' + key)
    if type(cfg.get('bank_count')) is not int or cfg['bank_count'] < 1:
        raise ValueError('Missing or invalid bank_count.')
    for key in ('refresh_enabled', 'trrd_enabled', 'tfaw_enabled'):
        if type(cfg.get(key)) is not bool:
            raise ValueError('Missing boolean system configuration: ' + key)
    for field in ('clock', 'reset'):
        if not isinstance(manifest.get(field), dict) or not manifest[field].get('name'):
            raise ValueError('Missing controller ' + field + ' metadata.')
    for key in ('active_low', 'synchronous'):
        if type(manifest['reset'].get(key)) is not bool:
            raise ValueError('Missing reset metadata: ' + key)
    config = read_mapping(resolved[manifest['controller_config']])['controller_config']
    expected = {'protocol': config['protocol'],
                'memory_speed': config['jedec_profile'].split('-', 1)[1],
                'bank_count': config['topology']['banks'], 'page_policy': config['page_policy']}
    if any(cfg[key] != value for key, value in expected.items()):
        raise ValueError('Manifest configuration differs from the generated controller configuration.')
    features = set()
    for value in manifest['configuration_sources']:
        source = read_mapping(resolved[value])['controller_config']
        if any(source.get(key) != config.get(key) for key in ('protocol', 'jedec_profile', 'topology', 'page_policy')):
            raise ValueError('Generated configuration sources disagree: ' + value)
        features.update(source.get('features', []))
    expected_scheduler = ('round_robin' if 'scheduler_round_robin' in features else
                          'simple' if 'scheduler' in features else None)
    if cfg['scheduler'] != expected_scheduler:
        raise ValueError('Scheduler policy differs from the generated configuration sources.')
    for key, feature in (('refresh_enabled', 'refresh_controller'), ('trrd_enabled', 'tRRD'),
                         ('tfaw_enabled', 'tFAW_tracker')):
        if cfg[key] != (feature in features):
            raise ValueError('Optional feature differs from generated configuration sources: ' + key)
    timing = manifest['timing']
    profile = read_mapping(resolved[timing['dictionary']]).get(timing['jedec_profile'])
    if timing['jedec_profile'] != config['jedec_profile'] or profile != timing['profile']:
        raise ValueError('Manifest timing metadata differs from its source profile.')
    if manifest['clock'].get('profile_frequency_mhz') != profile['clock_frequency_mhz']:
        raise ValueError('Controller profile clock frequency differs from the source profile.')
    top_file = next(row['file'] for row in actual if row['name'] == SYSTEM_TOP)
    clock, reset = top_clock_reset(resolved[top_file].read_text())
    if any(manifest['clock'].get(key) != value for key, value in clock.items()) or manifest['reset'] != reset:
        raise ValueError('Manifest clock/reset metadata differs from the generated top.')
    return {'mode': 'system', 'discovery_result': 'PASS',
            'verification_scope': 'input_discovery_only', 'manifest_path': str(path),
            'rtl_file_count': len(manifest['rtl_files']), 'module_definition_count': len(actual),
            'module_spec_count': len(manifest['module_specs']), 'manifest': manifest}


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def publish_snapshot(manifest_path, destination):
    """Copy each referenced input once, retaining the frontend layout and hashes.

    Historical snapshots are immutable. The external publisher is not part of
    this repository; this explicit export uses its existing snapshot.json fields.
    """
    discovery = discover_system(manifest_path)
    manifest = discovery['manifest']
    path = Path(manifest_path).resolve()
    root = (path.parent / manifest['run_root']).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Snapshot destination already exists: ' + str(destination))
    if root == destination or root in destination.parents:
        raise ValueError('Snapshot destination must be outside the frontend run root.')
    sources = {resolve_path(path, manifest, p) for p in manifest_paths(manifest)} | {path}
    destination.mkdir(parents=True)
    try:
        hashes = {}
        for source in sorted(sources):
            relative = source.relative_to(root)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(source), str(target))
            hashes[relative.as_posix()] = file_digest(target)
        rtl_hashes = {relative_path(resolve_path(path, manifest, p), root):
                      file_digest(resolve_path(path, manifest, p)) for p in manifest['rtl_files']}
        revision = hashlib.sha256(json.dumps(rtl_hashes, sort_keys=True).encode()).hexdigest()
        snapshot = {'schema_version': 1, 'files': hashes, 'rtl_revision': revision,
                    'modules_without_specs': manifest['modules_without_specs'],
                    'system_manifest': path.relative_to(root).as_posix()}
        (destination / 'snapshot.json').write_text(json.dumps(snapshot, indent=2) + '\n')
        discover_system(destination / snapshot['system_manifest'])
    except Exception:
        shutil.rmtree(str(destination))
        raise
    return destination / 'snapshot.json'
