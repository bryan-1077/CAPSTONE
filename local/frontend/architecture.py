"""Validated architecture graphs, reusable blocks, and structural integration.

This compiler does not select a controller decomposition. Its input can be authored
by a human or an LLM; feature templates are optional sources of module specs.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile

import yaml

from expand_spec import build_submodule_yaml, build_substitution_map
from validator import validate_spec, ValidationError
from fsm_generator import generate_fsm_rtl
from architecture_dependencies import REUSABLE_DEPENDENCIES, dependency_cycle
from controller_baseline import controller_baseline, validate_baseline

ROOT = Path(__file__).resolve().parent
LIBRARY = ROOT / "microarch/jedec"
VERSION = 1
COMPILER_REVISION = 5


class ArchitectureError(ValueError):
    """An architecture cannot be lowered without changing its meaning."""


def read_mapping(path):
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ArchitectureError(f"Expected an object in {path}")
    return data


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def library_context():
    return {"templates": read_mapping(LIBRARY / "feature_templates.yaml"),
            "profiles": read_mapping(LIBRARY / "jedec_dictionary.yaml"),
            "combinational_dependencies": copy.deepcopy(REUSABLE_DEPENDENCIES)}


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ArchitectureError(f"{label}: expected a simple HDL identifier, got {value!r}")
    # Keep emitted HDL identifiers away from keywords and compiler-generated nets.
    from architecture_keywords import KEYWORDS
    if value in KEYWORDS or value.startswith("arch_net_"):
        raise ArchitectureError(f"{label}: reserved HDL identifier {value!r}")
    return value


def nonempty_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ArchitectureError(f"{label}: expected nonempty text")


def only_fields(value, allowed, label):
    unknown = set(value) - set(allowed)
    if unknown:
        raise ArchitectureError(f"{label}: unsupported fields {sorted(unknown)}")


def ports_table(ports, label):
    if not isinstance(ports, list):
        raise ArchitectureError(f"{label}: ports must be a list")
    result = {}
    for port in ports:
        if not isinstance(port, dict):
            raise ArchitectureError(f"{label}: port must be an object")
        only_fields(port, {"name", "direction", "width", "description"}, label)
        name = identifier(port.get("name"), label)
        width = port.get("width", 1)
        if type(width) is not int or width <= 0:
            raise ArchitectureError(f"{label}.{name}: width must be a positive integer")
        if not isinstance(port.get("direction"), str) or port["direction"] not in {"input", "output"}:
            raise ArchitectureError(f"{label}.{name}: only input/output ports are supported")
        if name in result:
            raise ArchitectureError(f"{label}: duplicate port {name}")
        result[name] = dict(port, width=width)
    return result


def resolve_modules(manifest, library):
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not modules:
        raise ArchitectureError("modules must be a nonempty list")
    resolved = {}
    definitions = {}
    for module in modules:
        if not isinstance(module, dict):
            raise ArchitectureError("Each module must be an object")
        instance = identifier(module.get("instance"), "module.instance")
        if instance == "top" or instance in resolved:
            raise ArchitectureError(f"Duplicate or reserved instance {instance}")
        nonempty_text(module.get("responsibility"), f"{instance}.responsibility")
        if ("reuse" in module) == ("spec" in module):
            raise ArchitectureError(f"{instance}: specify exactly one of reuse or spec")
        if "reuse" in module:
            reuse = module["reuse"]
            if not isinstance(reuse, dict):
                raise ArchitectureError(f"{instance}.reuse must be an object")
            only_fields(reuse, {"feature", "submodule", "design_name"}, instance)
            try:
                template = library["templates"][reuse["feature"]]
                block = next(b for b in template["submodules"] if b["name"] == reuse["submodule"])
                profile = library["profiles"][manifest["profile"]]
                spec = build_submodule_yaml(block, "library", build_substitution_map(profile, template.get("timer_parameters", {})))
            except (KeyError, StopIteration, TypeError) as exc:
                raise ArchitectureError(f"{instance}: unknown reusable feature/submodule/profile") from exc
            # A reusable block retains its behavioral and port contract.
            spec["design_name"] = reuse.get("design_name", spec["design_name"])
        else:
            spec = copy.deepcopy(module["spec"])
        if not isinstance(spec, dict):
            raise ArchitectureError(f"{instance}.spec must be an object")
        only_fields(spec, {"design_name", "design_type", "clock", "reset", "ports", "behavior",
                           "state_machine", "output_overrides", "implementation_constraints",
                           "invariants", "forbidden_patterns", "scheduler_mode", "description",
                           "combinational_dependencies", "verification_requirements"}, instance)
        name = identifier(spec.get("design_name"), f"{instance}.design_name")
        if name == manifest["design_name"]:
            raise ArchitectureError("Top and child module names must differ")
        if not isinstance(spec.get("design_type"), str) or spec["design_type"] not in {"fsm", "datapath"}:
            raise ArchitectureError(f"{instance}: supported generation routes are fsm and datapath")
        if spec["design_type"] == "datapath" and not spec.get("behavior"):
            raise ArchitectureError(f"{instance}: datapath requires behavior")
        if spec["design_type"] == "fsm":
            overrides = spec.get("output_overrides", {})
            if not isinstance(overrides, dict) or any(not isinstance(expr, str) or not expr.strip() for expr in overrides.values()):
                raise ArchitectureError(f"{instance}: output_overrides must map ports to SV expression strings")
            sm = spec.get("state_machine", {})
            if isinstance(sm, dict) and isinstance(sm.get("states"), list):
                for state in sm["states"]:
                    identifier(state, f"{instance}.state")
        # Those names invoke specialized emitters; custom behavior would be ignored.
        specialized = {"ddr4_tFAW_tFAW_tracker": ("tFAW_tracker", "tFAW_tracker"),
                       "ddr4_tRRD_simple_tRRD": ("tRRD", "simple_tRRD"),
                       "ddr4_scheduler_scheduler": ("scheduler", "scheduler")}
        if name in specialized and ("spec" in module or
                (module["reuse"].get("feature"), module["reuse"].get("submodule")) != specialized[name]):
            raise ArchitectureError(f"{instance}: specialized timing/scheduler names require matching reuse")
        table = ports_table(spec.get("ports"), instance)
        for field in ("clock", "reset"):
            if not isinstance(spec.get(field), dict):
                raise ArchitectureError(f"{instance}: explicit {field} required")
            port_name = identifier(spec[field].get("name"), f"{instance}.{field}")
            only_fields(spec[field], {"name", "target_frequency_mhz"} if field == "clock" else
                        {"name", "active_low", "synchronous"}, f"{instance}.{field}")
            # Existing module IR treats clock/reset as implicit ports.
            if port_name in table:
                if "spec" in module or table[port_name]["direction"] != "input" or table[port_name]["width"] != 1:
                    raise ArchitectureError(f"{instance}: clock/reset must not be repeated in ports")
                spec["ports"] = [p for p in spec["ports"] if p["name"] != port_name]
            table[port_name] = {"name": port_name, "direction": "input", "width": 1}
        if spec["clock"]["name"] == spec["reset"]["name"]:
            raise ArchitectureError(f"{instance}: clock and reset must differ")
        for field in ("active_low", "synchronous"):
            if field in spec["reset"] and type(spec["reset"][field]) is not bool:
                raise ArchitectureError(f"{instance}.reset.{field} must be boolean")
        outputs = {name for name, p in table.items() if p['direction'] == 'output'}
        inputs = {name for name, p in table.items() if p['direction'] == 'input'}
        if spec['design_type'] == 'fsm':
            # Moore-state outputs are registered paths; override expressions may
            # expose combinational dependencies on explicit input ports.
            overrides = spec.get('output_overrides', {})
            dependencies = {out: sorted(inputs & set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', overrides.get(out, '')))) for out in outputs}
        elif 'reuse' in module:
            dependencies = copy.deepcopy(library['combinational_dependencies'].get(module['reuse']['feature']))
        else:
            dependencies = spec.get('combinational_dependencies')
        if not isinstance(dependencies, dict) or set(dependencies) != outputs:
            raise ArchitectureError(f'{instance}: combinational_dependencies must describe every output port')
        for out, names in dependencies.items():
            if not isinstance(names, list) or any(not isinstance(p, str) or p not in inputs for p in names):
                raise ArchitectureError(f'{instance}.{out}: combinational dependencies must reference input ports')
        spec['combinational_dependencies'] = dependencies
        if name in definitions and definitions[name] != spec:
            raise ArchitectureError(f"Conflicting definitions for {name}")
        definitions[name] = spec
        # Use the existing IR validator before any generated output is touched.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.yaml"
            path.write_text(yaml.safe_dump(spec))
            try:
                ir = validate_spec(str(path))
                if ir.design_type == "fsm":
                    preview = generate_fsm_rtl(ir)
                    if "TODO: specify output logic" in preview:
                        raise ArchitectureError("FSM output behavior is unspecified; provide output_overrides")
            except (ValidationError, TypeError, KeyError, ValueError) as exc:
                raise ArchitectureError(f"{instance}: module IR validation failed: {exc}") from exc
        resolved[instance] = {"spec": spec, "ports": table, "dependencies": dependencies}
    return resolved


def validate_architecture(manifest, library=None):
    if not isinstance(manifest, dict) or type(manifest.get("architecture_version")) is not int or manifest["architecture_version"] != VERSION:
        raise ArchitectureError("Expected architecture_version: 1")
    identifier(manifest.get("design_name"), "design_name")
    nonempty_text(manifest.get("rationale"), "rationale")
    library = library or library_context()
    if not isinstance(manifest.get("profile"), str) or manifest["profile"] not in library["profiles"]:
        raise ArchitectureError("Unknown timing profile")
    provenance = manifest.get('provenance', {})
    if not isinstance(provenance, dict):
        raise ArchitectureError('provenance must be an object')
    request = provenance.get('request', {})
    if isinstance(request, dict) and isinstance(request.get('constraints'), dict):
        try:
            expected = controller_baseline(request['constraints'])
        except ValueError as exc:
            raise ArchitectureError(str(exc)) from exc
        if expected != manifest.get('baseline_contract'):
            raise ArchitectureError('Saved controller proposal lacks the current baseline contract; regenerate it')
        if request.get('design_kind') != manifest.get('design_kind'):
            raise ArchitectureError('Saved proposal design_kind differs from its request')
    top = ports_table(manifest.get("top_ports"), "top")
    if not top:
        raise ArchitectureError("top_ports must not be empty")
    modules = resolve_modules(manifest, library)
    if set(modules) & set(top):
        raise ArchitectureError("Top ports and instance names share the HDL namespace")
    endpoints = {f"top.{name}": dict(p, source=p["direction"] == "input") for name, p in top.items()}
    for instance, module in modules.items():
        endpoints.update({f"{instance}.{name}": dict(p, source=p["direction"] == "output") for name, p in module["ports"].items()})
    connections = manifest.get("connections")
    if not isinstance(connections, list):
        raise ArchitectureError("connections must be a list")
    driven, used = set(), set()
    for connection in connections:
        if not isinstance(connection, dict):
            raise ArchitectureError("connection must be an object")
        only_fields(connection, {"from", "to"}, "connection")
        source, target = connection.get("from"), connection.get("to")
        if not isinstance(source, str) or not isinstance(target, str) or source not in endpoints or target not in endpoints:
            raise ArchitectureError(f"Unknown connection endpoint: {source!r} -> {target!r}")
        src, dst = endpoints[source], endpoints[target]
        if not src["source"] or dst["source"]:
            raise ArchitectureError(f"Wrong connection direction: {source} -> {target}")
        if src["width"] != dst["width"]:
            raise ArchitectureError(f"Width mismatch: {source} -> {target}")
        if target in driven:
            raise ArchitectureError(f"Multiple drivers for {target}")
        driven.add(target)
        used.add(source)
    missing = {name for name, p in endpoints.items() if not p["source"]} - driven
    if missing:
        raise ArchitectureError(f"Undriven inputs/top outputs: {sorted(missing)}")
    unused = {name for name, p in endpoints.items() if p['source']} - used
    unused_inputs = {name for name in unused if name.startswith('top.')}
    if unused_inputs:
        raise ArchitectureError(f'Unused top inputs: {sorted(unused_inputs)}')
    declared = manifest.get('unused_outputs', [])
    if not isinstance(declared, list):
        raise ArchitectureError('unused_outputs must be a list of endpoint/reason objects')
    intentionally_unused = set()
    for item in declared:
        if not isinstance(item, dict):
            raise ArchitectureError('unused_outputs entries must be objects')
        only_fields(item, {'endpoint', 'reason'}, 'unused_outputs')
        endpoint = item.get('endpoint')
        if not isinstance(endpoint, str) or endpoint not in unused or endpoint.startswith('top.') or endpoint in intentionally_unused:
            raise ArchitectureError(f'Invalid, connected or duplicate unused output: {endpoint!r}')
        nonempty_text(item.get('reason'), f'{endpoint}.unused reason')
        intentionally_unused.add(endpoint)
    if unused - intentionally_unused:
        raise ArchitectureError(f'Unused child outputs need explicit unused_outputs reasons: {sorted(unused - intentionally_unused)}')
    incoming = {c["to"]: c["from"] for c in connections}
    clocks, resets, polarities = set(), set(), set()
    for instance, module in modules.items():
        spec = module["spec"]
        clocks.add(incoming[f'{instance}.{spec["clock"]["name"]}'])
        resets.add(incoming[f'{instance}.{spec["reset"]["name"]}'])
        polarities.add(spec['reset'].get('active_low', True))
    if len(clocks) != 1 or len(resets) != 1 or len(polarities) != 1 or not all(s.startswith("top.") for s in clocks | resets) or clocks == resets:
        raise ArchitectureError('Current integration requires one top-level clock/reset source with consistent polarity; assertion styles may differ')
    cycle = dependency_cycle(connections, modules)
    if cycle:
        raise ArchitectureError('Declared combinational feedback cycle: ' + ' -> '.join(cycle))
    requirements = manifest.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ArchitectureError("requirements must be a nonempty list")
    ids = set()
    for req in requirements:
        if not isinstance(req, dict):
            raise ArchitectureError("requirement must be an object")
        only_fields(req, {"id", "text", "owners", "verification"}, "requirement")
        rid = identifier(req.get("id"), "requirement.id")
        if rid in ids:
            raise ArchitectureError(f"Duplicate requirement {rid}")
        ids.add(rid)
        nonempty_text(req.get("text"), rid)
        owners = req.get("owners")
        if not isinstance(owners, list) or not owners or any(not isinstance(owner, str) or owner not in modules for owner in owners):
            raise ArchitectureError(f"{rid}: owners must reference module instances")
        nonempty_text(req.get("verification"), f"{rid}.verification")
    for field in ("assumptions", "limitations"):
        if not isinstance(manifest.get(field, []), list) or any(not isinstance(s, str) for s in manifest.get(field, [])):
            raise ArchitectureError(f"{field} must be a list of strings")
    # Prevent silently ignored architectural constructs.
    allowed = {"architecture_version", "design_name", "profile", "rationale", "top_ports", "modules", "connections", "requirements", "assumptions", "limitations", "provenance", "unused_outputs", "baseline_contract", "design_kind"}
    unknown = set(manifest) - allowed
    if unknown:
        raise ArchitectureError(f"Unsupported architecture fields: {sorted(unknown)}")
    for module in manifest["modules"]:
        unknown = set(module) - {"instance", "responsibility", "reuse", "spec", "roles"}
        if unknown:
            raise ArchitectureError(f"Unsupported module fields: {sorted(unknown)}")
    if manifest.get('design_kind') not in (None, 'memory_controller'):
        raise ArchitectureError('Unsupported design_kind')
    if manifest.get('design_kind') == 'memory_controller' or 'baseline_contract' in manifest:
        validate_controller_decomposition(manifest, modules)
    if 'baseline_contract' in manifest:
        try:
            validate_baseline(manifest, manifest['baseline_contract'])
        except ValueError as exc:
            raise ArchitectureError(str(exc)) from exc
    from specification_lowering import validate_specification_manifest
    try:
        validate_specification_manifest(manifest)
    except ValueError as exc:
        raise ArchitectureError(str(exc)) from exc
    return modules


CONTROLLER_ROLES = {'request_queue', 'scheduler', 'command_engine', 'timing', 'memory'}


def validate_controller_decomposition(manifest, modules):
    """Require independently verifiable controller responsibilities and wiring."""
    owners = {role: set() for role in CONTROLLER_ROLES}
    optional = {'host_adapter', 'row_state', 'refresh'}
    for module in manifest['modules']:
        roles = module.get('roles')
        if not isinstance(roles, list) or not roles or any(not isinstance(r, str) or r not in CONTROLLER_ROLES | optional for r in roles):
            raise ArchitectureError(f'{module["instance"]}: controller modules require explicit supported roles')
        for role in roles:
            if role in owners:
                owners[role].add(module['instance'])
    missing = [role for role, instances in owners.items() if not instances]
    if missing:
        raise ArchitectureError(f'Controller decomposition requires separate modules for roles: {sorted(missing)}')
    assigned = set()
    definitions = set()
    for role, instances in owners.items():
        role_definitions = {modules[i]['spec']['design_name'] for i in instances}
        if assigned & instances or definitions & role_definitions:
            raise ArchitectureError('Controller queue, scheduler, command engine, timing and memory must have distinct module owners; monolithic cores are prohibited')
        assigned |= instances
        definitions |= role_definitions
    # Clock/reset wiring alone cannot make a dummy block a functional participant.
    connected = set()
    for connection in manifest['connections']:
        source, target = connection['from'].split('.'), connection['to'].split('.')
        if source[0] in modules and target[0] in modules and source[0] != target[0]:
            dst_spec = modules[target[0]]['spec']
            if target[1] not in {dst_spec['clock']['name'], dst_spec['reset']['name']}:
                connected.update((source[0], target[0]))
    if assigned - connected:
        raise ArchitectureError(f'Controller role modules need functional inter-module connections: {sorted(assigned - connected)}')
    obligations = {'BASE_QUEUE':'request_queue', 'BASE_POLICY':'scheduler', 'BASE_SEQUENCE':'command_engine',
                   'BASE_TIMING':'timing', 'BASE_DATA':'memory'}
    for req in manifest['requirements']:
        role = obligations.get(req['id'])
        if role and not owners[role] & set(req['owners']):
            raise ArchitectureError(f'{req["id"]} must identify a {role} module owner')


def emit_wrapper(manifest, modules):
    def declaration(port):
        width = port.get("width", 1)
        return f'{port["direction"]} logic ' + (f"[{width - 1}:0] " if width > 1 else "") + port["name"]
    lines = ["`timescale 1ns/1ps", "// Structural integration from validated architecture manifest.",
             f'module {manifest["design_name"]}(',
             ",\n".join("    " + declaration(p) for p in manifest["top_ports"]), ");"]
    nets = {}
    port_nets = {}
    for conn in manifest["connections"]:
        source = conn["from"]
        if source not in nets:
            if source.startswith("top."):
                nets[source] = source.split(".")[1]
            else:
                instance, port = source.split(".")
                net = f"arch_net_{len(nets)}"
                width = modules[instance]["ports"][port]["width"]
                lines.append("    logic " + (f"[{width-1}:0] " if width > 1 else "") + net + ";")
                nets[source] = net
        net = nets[source]
        port_nets[source] = net
        port_nets[conn["to"]] = net
        if conn["to"].startswith("top."):
            lines.append(f'    assign {conn["to"].split(".")[1]} = {net};')
    for instance, module in modules.items():
        lines.append(f'    {module["spec"]["design_name"]} {instance} (')
        lines.append(",\n".join(f"        .{p}({port_nets.get(instance + '.' + p, '')})" for p in module["ports"]))
        lines.append("    );")
    return "\n".join(lines + ["endmodule", ""])


def emit_module_specs(manifest, spec_dir):
    """Resolve templates/custom specs for verification without emitting RTL."""
    modules = validate_architecture(manifest)
    spec_dir = Path(spec_dir)
    spec_dir.mkdir(parents=True, exist_ok=True)
    specs = {}
    for module in modules.values():
        spec = module['spec']
        if spec['design_name'] in specs:
            continue
        spec = copy.deepcopy(spec)
        instances = {i for i, block in modules.items() if block['spec']['design_name'] == spec['design_name']}
        spec['verification_requirements'] = [r for r in manifest['requirements'] if instances & set(r['owners'])]
        path = spec_dir / (spec['design_name'] + '.yaml')
        path.write_text(yaml.safe_dump(spec, sort_keys=False))
        specs[spec['design_name']] = path
    return list(specs.values())


def emit_verification_specs(manifest, proposal_path):
    """Emit an immutable spec snapshot and a machine-readable verification index."""
    proposal_path = Path(proposal_path)
    folder = proposal_path.parent / 'module_specs' / fingerprint(manifest)[:16]
    paths = emit_module_specs(manifest, folder)
    resolved = validate_architecture(manifest)
    entries = []
    for instance, module in resolved.items():
        name = module['spec']['design_name']
        entries.append({'instance': instance, 'design_name': name,
                        'spec_path': (folder / (name + '.yaml')).relative_to(proposal_path.parent).as_posix(),
                        'roles': next(m.get('roles', []) for m in manifest['modules'] if m['instance'] == instance),
                        'requirements': [r for r in manifest['requirements'] if instance in r['owners']]})
    index = {'architecture_fingerprint': fingerprint(manifest), 'status': 'specs_ready_verification_not_run',
             'modules': entries}
    proposal_path.with_suffix('.modules.json').write_text(json.dumps(index, indent=2) + '\n')
    return paths


def lower_architecture(manifest, output_dir, *, specification_approval=None):
    modules = validate_architecture(manifest)
    from specification_lowering import require_approval
    approval = require_approval(manifest, specification_approval)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    spec_dir = output / "microarch/gen_exp"
    rtl_dir = output / "rtl_output"
    spec_dir.mkdir(parents=True, exist_ok=True)
    rtl_dir.mkdir(parents=True, exist_ok=True)
    spec_paths = emit_module_specs(manifest, spec_dir)
    (output / "architecture.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (rtl_dir / (manifest["design_name"] + ".sv")).write_text(emit_wrapper(manifest, modules))
    (rtl_dir / "filelist.f").write_text("\n".join([path.stem + ".sv" for path in spec_paths] + [manifest["design_name"] + ".sv"]) + "\n")
    verification = {"status": "planned_not_executed", "requirements": manifest["requirements"]}
    if approval is not None:
        (output / 'specification_approval.json').write_text(json.dumps(approval, indent=2) + '\n')
        verification['requirement_results'] = [{'id': r['id'], 'result': 'unverified'} for r in manifest['requirements']]
        verification['obligations'] = {ref: dict(obligation, result='unverified')
            for module in manifest['modules']
            for ref, obligation in module['spec']['behavior']['verification_obligations'].items()}
    (output / "verification_plan.json").write_text(json.dumps(verification, indent=2) + "\n")
    return spec_paths
