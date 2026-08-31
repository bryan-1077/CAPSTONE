#!/usr/bin/env python3
"""
check_interfaces.py
===================
Validate interface declarations inside a master YAML produced by expand_spec.py.

Reads:
    <master.yaml>                         -- top-level design manifest
    submodules[].spec                     -- concrete expanded submodule YAMLs

Checks:
    Pass 1: master.yaml interface structure
    Pass 2: referenced port existence, direction, and width
    Pass 3: per-submodule port coverage (unconnected ports are warnings)

Exit codes:
    0 on success (or warnings-only without --strict)
    1 on any errors, or on warnings when --strict is used

Usage:
    python3 check_interfaces.py expanded/ddr4_bank/master.yaml
    python3 check_interfaces.py expanded/ddr4_bank/master.yaml --strict

Python 3.6 compatible - no f-strings, no argparse dependency.
"""

import sys
import os
import json
import yaml


WRAPPER_INTERFACE_REQUIREMENTS = {
    "ddr4_scheduler_scheduler": {
        "clk": ("input", 1),
        "rst_n": ("input", 1),
        "ref_req": ("input", 1),
        "bank_active": ("input", 4),
        "bank_open_row": ("input", 40),
        "req_array": ("input", 204),
        "req_valid": ("input", 4),
        "cmd_ready": ("input", 1),
        "timing_ok": ("input", 1),
        "sel_idx": ("output", 2),
        "issue_valid": ("output", 1),
        "issue_ref": ("output", 1),
        "issue_txn": ("output", 1),
    },
    "ddr4_request_queue": {
        "clk": ("input", 1),
        "rst_n": ("input", 1),
        "enq_valid": ("input", 1),
        "enq_req": ("input", 51),
        "enq_ready": ("output", 1),
        "req_array": ("output", 204),
        "req_valid": ("output", 4),
        "sel_idx": ("input", 2),
        "deq_en": ("input", 1),
    },
    "ddr4_refresh_refresh_controller": {
        "clk": ("input", 1),
        "rst_n": ("input", 1),
        "ref_ack": ("input", 1),
        "ref_req": ("output", 1),
    },
    "ddr4_bank_bank_sequencer": {
        "clk": ("input", 1),
        "rst_n": ("input", 1),
        "cmd_valid": ("input", 1),
        "cmd_type": ("input", 2),
        "tRCD_done": ("input", 1),
        "tRAS_done": ("input", 1),
        "tRP_done": ("input", 1),
        "bank_idle": ("output", 1),
        "bank_active": ("output", 1),
        "cmd_ready": ("output", 1),
        "tRCD_start": ("output", 1),
        "tRAS_start": ("output", 1),
        "tRP_start": ("output", 1),
        "tRCD_ack": ("output", 1),
        "tRAS_ack": ("output", 1),
        "tRP_ack": ("output", 1),
    },
    "ddr4_bank_activate_fsm": {
        "clk": ("input", 1),
        "rst_n": ("input", 1),
        "start": ("input", 1),
        "ack": ("input", 1),
        "tRCD_done": ("output", 1),
    },
    "ddr4_bank_tRAS_fsm": {
        "clk": ("input", 1),
        "rst_n": ("input", 1),
        "start": ("input", 1),
        "ack": ("input", 1),
        "tRAS_done": ("output", 1),
    },
    "ddr4_bank_precharge_fsm": {
        "clk": ("input", 1),
        "rst_n": ("input", 1),
        "start": ("input", 1),
        "ack": ("input", 1),
        "tRP_done": ("output", 1),
    },
    "ddr4_tFAW_tFAW_tracker": {
        "clk": ("input", 1),
        "rst_n": ("input", 1),
        "act_pulse": ("input", 1),
        "act_count": ("output", 3),
        "tFAW_block": ("output", 1),
        "tFAW_ok": ("output", 1),
    },
    "ddr4_tRRD_simple_tRRD": {
        "clk": ("input", 1),
        "rst_n": ("input", 1),
        "act_pulse": ("input", 1),
        "tRRD_block": ("output", 1),
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_yaml(path):
    """Load a YAML file and return the parsed object. Raises on missing file."""
    if not os.path.isfile(path):
        raise IOError("File not found: {}".format(path))
    with open(path, "r") as fh:
        return yaml.safe_load(fh)


def resolve_submodule_path(spec_path, master_dir):
    """Resolve a submodule spec path relative to the master YAML directory."""
    if os.path.isabs(spec_path):
        return spec_path
    return os.path.normpath(os.path.join(master_dir, spec_path))


def load_submodule_ports(submodules, master_dir):
    """
    Load each submodule YAML once and return its port map.

    Returns:
        ({submodule_name: {port_name: {direction, width}}}, [load_error_strings])
    """
    port_map = {}
    load_errors = []
    yaml_cache = {}

    for submodule in submodules:
        name = submodule.get("name")
        spec_path = submodule.get("spec")

        if not name:
            load_errors.append("submodule entry missing required field: name")
            continue

        if not spec_path:
            load_errors.append(
                "submodule '{}' missing required field: spec".format(name)
            )
            continue

        resolved_path = resolve_submodule_path(spec_path, master_dir)

        if resolved_path in yaml_cache:
            submodule_yaml = yaml_cache[resolved_path]
        else:
            try:
                submodule_yaml = load_yaml(resolved_path)
                yaml_cache[resolved_path] = submodule_yaml
            except IOError as e:
                load_errors.append(
                    "submodule '{}' spec load failed: {}".format(name, e)
                )
                continue

        if not isinstance(submodule_yaml, dict):
            load_errors.append(
                "submodule '{}' YAML is not a mapping: {}".format(
                    name, resolved_path
                )
            )
            continue

        ports = submodule_yaml.get("ports")
        if not isinstance(ports, list):
            load_errors.append(
                "submodule '{}' missing valid ports list: {}".format(
                    name, resolved_path
                )
            )
            continue

        module_ports = {}
        port_errors = []

        for port in ports:
            if not isinstance(port, dict):
                port_errors.append("contains a non-mapping port entry")
                continue

            port_name = port.get("name")
            direction = port.get("direction")
            width = port.get("width")

            if port_name is None:
                port_errors.append("contains a port missing field: name")
                continue
            if direction is None:
                port_errors.append(
                    "port '{}' missing field: direction".format(port_name)
                )
                continue
            if width is None:
                port_errors.append(
                    "port '{}' missing field: width".format(port_name)
                )
                continue

            module_ports[port_name] = {
                "direction": direction,
                "width": width
            }

        if port_errors:
            for issue in port_errors:
                load_errors.append(
                    "submodule '{}' {}".format(name, issue)
                )
            continue

        port_map[name] = module_ports

    return port_map, load_errors


def format_interface_ref(interface):
    """Return a readable interface reference for logs."""
    signal = interface.get("signal", "<unnamed>")
    from_module = interface.get("from_module", "<missing>")
    from_port = interface.get("from_port", "<missing>")
    to_module = interface.get("to_module", "<missing>")
    to_port = interface.get("to_port", "<missing>")
    return "{} : {}.{} -> {}.{}".format(
        signal, from_module, from_port, to_module, to_port
    )


def is_positive_integer(value):
    """Return True when value is a positive integer (bool is not accepted)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def normalize_ports(ports):
    """Convert a port list into {port_name: {direction, width}}."""
    port_map = {}
    if not isinstance(ports, list):
        raise ValueError("ports must be a list")

    for port in ports:
        if not isinstance(port, dict):
            raise ValueError("ports contains a non-mapping entry")

        name = port.get("name")
        direction = port.get("direction")
        width = port.get("width")

        if name is None:
            raise ValueError("port missing field: name")
        if direction is None:
            raise ValueError("port '{}' missing field: direction".format(name))
        if width is None:
            raise ValueError("port '{}' missing field: width".format(name))

        port_map[name] = {
            "direction": direction,
            "width": width,
        }

    return port_map


def add_global_ports(port_map, spec_data):
    """Add clock/reset definitions from top-level spec fields when present."""
    clock = spec_data.get("clock")
    if isinstance(clock, dict):
        clock_name = clock.get("name")
        if clock_name:
            port_map[clock_name] = {
                "direction": "input",
                "width": 1,
            }

    reset = spec_data.get("reset")
    if isinstance(reset, dict):
        reset_name = reset.get("name")
        if reset_name:
            port_map[reset_name] = {
                "direction": "input",
                "width": 1,
            }

    return port_map


def load_module_metadata(module_name, root_dir):
    """Load module metadata from IR JSON or expanded YAML."""
    ir_path = os.path.join(root_dir, "ir", "{}_ir.json".format(module_name))
    if os.path.isfile(ir_path):
        with open(ir_path, "r") as fh:
            ir_data = json.load(fh)
        ports = normalize_ports(ir_data.get("ports"))
        ports = add_global_ports(ports, ir_data)
        return {
            "name": ir_data.get("design_name", module_name),
            "ports": ports,
            "source": ir_path,
        }

    expanded_root = os.path.join(root_dir, "expanded")
    for current_root, _, files in os.walk(expanded_root):
        yaml_name = "{}.yaml".format(module_name)
        if yaml_name in files:
            spec_path = os.path.join(current_root, yaml_name)
            spec_data = load_yaml(spec_path)
            ports = normalize_ports(spec_data.get("ports"))
            ports = add_global_ports(ports, spec_data)
            return {
                "name": spec_data.get("design_name", module_name),
                "ports": ports,
                "source": spec_path,
            }

    raise IOError(
        "module '{}' metadata not found under '{}'".format(module_name, root_dir)
    )


def validate_wrapper_modules(modules, root_dir):
    """Validate required wrapper-facing ports for the supplied modules."""
    errors = []
    normalized_modules = []

    for module in modules:
        if isinstance(module, dict):
            module_name = module.get("name")
            ports = module.get("ports")
            source = module.get("source", "<provided>")
            if not module_name:
                errors.append("module entry missing required field: name")
                continue
            try:
                port_map = normalize_ports(ports)
            except ValueError as exc:
                errors.append(
                    "module '{}' has invalid provided ports: {}".format(
                        module_name, exc
                    )
                )
                continue
            metadata = {
                "name": module_name,
                "ports": port_map,
                "source": source,
            }
        else:
            module_name = module
            try:
                metadata = load_module_metadata(module_name, root_dir)
            except (IOError, ValueError, json.JSONDecodeError) as exc:
                errors.append(str(exc))
                continue

        normalized_modules.append(metadata)

        expected_ports = WRAPPER_INTERFACE_REQUIREMENTS.get(module_name)
        if expected_ports is None:
            errors.append(
                "no wrapper interface requirements defined for module '{}'".format(
                    module_name
                )
            )
            continue

        actual_ports = metadata["ports"]
        for port_name, expectation in expected_ports.items():
            expected_direction, expected_width = expectation
            if port_name not in actual_ports:
                errors.append(
                    "module '{}' missing required port '{}' ({})".format(
                        module_name, port_name, metadata["source"]
                    )
                )
                continue

            actual = actual_ports[port_name]
            if actual["direction"] != expected_direction:
                errors.append(
                    "module '{}.{}' direction mismatch: expected '{}', found '{}'".format(
                        module_name,
                        port_name,
                        expected_direction,
                        actual["direction"],
                    )
                )
            if actual["width"] != expected_width:
                errors.append(
                    "module '{}.{}' width mismatch: expected {}, found {}".format(
                        module_name,
                        port_name,
                        expected_width,
                        actual["width"],
                    )
                )

    return normalized_modules, errors


def check_interfaces(modules, root_dir=None):
    """
    Validate wrapper module interfaces and raise on any mismatch.

    Args:
        modules: list of module names or metadata dicts
        root_dir: repo-local directory containing ir/ and expanded/

    Returns:
        Normalized module metadata list.

    Raises:
        RuntimeError on any interface validation failure.
    """
    if root_dir is None:
        root_dir = os.path.dirname(os.path.abspath(__file__))

    normalized_modules, errors = validate_wrapper_modules(modules, root_dir)

    if errors:
        raise RuntimeError(
            "Interface validation failed:\n- " + "\n- ".join(errors)
        )

    return normalized_modules


# ---------------------------------------------------------------------------
# Pass 1
# ---------------------------------------------------------------------------

def check_structure(interfaces, submodule_names):
    """Validate required interface fields and submodule references."""
    errors = []
    required_fields = [
        "signal", "from_module", "from_port", "to_module", "to_port", "width"
    ]

    if interfaces is None:
        return ["master.yaml missing required section: interfaces"]

    if not isinstance(interfaces, list):
        return ["master.yaml field 'interfaces' must be a list"]

    for index, interface in enumerate(interfaces):
        if not isinstance(interface, dict):
            errors.append(
                "interface[{}] must be a mapping".format(index)
            )
            continue

        label = interface.get("signal", "interface[{}]".format(index))

        for field in required_fields:
            if field not in interface:
                errors.append(
                    "{} missing required field: {}".format(label, field)
                )

        if "from_module" in interface and interface["from_module"] not in submodule_names:
            errors.append(
                "{} references unknown from_module '{}'".format(
                    label, interface["from_module"]
                )
            )

        if "to_module" in interface and interface["to_module"] not in submodule_names:
            errors.append(
                "{} references unknown to_module '{}'".format(
                    label, interface["to_module"]
                )
            )

        if "width" in interface and not is_positive_integer(interface["width"]):
            errors.append(
                "{} has invalid width '{}': expected positive integer".format(
                    label, interface["width"]
                )
            )

    return errors


# ---------------------------------------------------------------------------
# Pass 2
# ---------------------------------------------------------------------------

def check_ports(interfaces, port_map):
    """Validate port existence, direction, and width for each interface."""
    errors = []
    warnings = []

    for interface in interfaces:
        label = interface.get("signal", "<unnamed>")
        from_module = interface.get("from_module")
        from_port = interface.get("from_port")
        to_module = interface.get("to_module")
        to_port = interface.get("to_port")
        width = interface.get("width")

        if from_module not in port_map or to_module not in port_map:
            errors.append({
                "interface": interface,
                "messages": [
                    "cannot validate ports because one or both submodules failed to load"
                ]
            })
            continue

        if not is_positive_integer(width):
            errors.append({
                "interface": interface,
                "messages": [
                    "cannot validate ports because interface width is invalid"
                ]
            })
            continue

        issue_list = []
        from_ports = port_map[from_module]
        to_ports = port_map[to_module]

        if from_port not in from_ports:
            issue_list.append(
                "port '{}' not found on module '{}'".format(from_port, from_module)
            )
        else:
            from_def = from_ports[from_port]
            if from_def["direction"] != "output":
                issue_list.append(
                    "from_port '{}.{}' must be direction 'output', found '{}'".format(
                        from_module, from_port, from_def["direction"]
                    )
                )
            if from_def["width"] != width:
                issue_list.append(
                    "width mismatch on '{}.{}' -- interface declares {}, port defines {}".format(
                        from_module, from_port, width, from_def["width"]
                    )
                )

        if to_port not in to_ports:
            issue_list.append(
                "port '{}' not found on module '{}'".format(to_port, to_module)
            )
        else:
            to_def = to_ports[to_port]
            if to_def["direction"] != "input":
                issue_list.append(
                    "to_port '{}.{}' must be direction 'input', found '{}'".format(
                        to_module, to_port, to_def["direction"]
                    )
                )
            if to_def["width"] != width:
                issue_list.append(
                    "width mismatch on '{}.{}' -- interface declares {}, port defines {}".format(
                        to_module, to_port, width, to_def["width"]
                    )
                )

        if issue_list:
            errors.append({
                "interface": interface,
                "messages": issue_list
            })

    return errors, warnings


# ---------------------------------------------------------------------------
# Pass 3
# ---------------------------------------------------------------------------

def check_coverage(interfaces, port_map):
    """Warn on ports that never appear in any interface entry."""
    warnings = []
    seen_ports = set()

    for interface in interfaces:
        from_module = interface.get("from_module")
        from_port = interface.get("from_port")
        to_module = interface.get("to_module")
        to_port = interface.get("to_port")

        if from_module is not None and from_port is not None:
            seen_ports.add((from_module, from_port))
        if to_module is not None and to_port is not None:
            seen_ports.add((to_module, to_port))

    for module_name in sorted(port_map.keys()):
        ports = port_map[module_name]
        for port_name in sorted(ports.keys()):
            if port_name in ("clk", "rst_n"):
                continue
            if (module_name, port_name) not in seen_ports:
                warnings.append(
                    "{}.{} not connected in any interface".format(
                        module_name, port_name
                    )
                )

    return warnings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv):
    """
    Minimal argument parser (no argparse dependency).
    Returns (master_path, strict_mode).
    """
    if len(argv) < 2:
        print("Usage: python3 check_interfaces.py <master.yaml> [--strict]")
        print("")
        print("  master.yaml   Path to the master YAML produced by expand_spec.py.")
        print("")
        print("  --strict      Treat warnings as errors (affects exit code).")
        print("                Optional flag; default is warnings-only.")
        print("")
        print("Examples:")
        print("  python3 check_interfaces.py expanded/ddr4_bank/master.yaml")
        print("  python3 check_interfaces.py expanded/ddr4_bank/master.yaml --strict")
        sys.exit(1)

    master_path = argv[1]
    strict_mode = False

    i = 2
    while i < len(argv):
        if argv[i] == "--strict":
            strict_mode = True
            i += 1
        else:
            print("Unknown argument: {}".format(argv[i]))
            sys.exit(1)

    return master_path, strict_mode


def main():
    master_path, strict_mode = parse_args(sys.argv)

    print("[check_interfaces] Loading master: {}".format(master_path))

    try:
        master = load_yaml(master_path)
    except IOError as e:
        print("")
        print("ERROR: {}".format(e))
        sys.exit(1)

    if not isinstance(master, dict):
        print("")
        print("ERROR: master.yaml must contain a top-level mapping")
        sys.exit(1)

    master_dir = os.path.dirname(os.path.abspath(master_path))
    submodules = master.get("submodules", [])

    if not isinstance(submodules, list):
        print("")
        print("ERROR: master.yaml field 'submodules' must be a list")
        sys.exit(1)

    submodule_names = []
    for submodule in submodules:
        if isinstance(submodule, dict) and "name" in submodule:
            submodule_names.append(submodule["name"])

    print("[check_interfaces] Submodules found: {}".format(
        ", ".join(submodule_names) if submodule_names else "(none)"
    ))

    print("[check_interfaces] Loading submodule YAMLs...")
    port_map, load_errors = load_submodule_ports(submodules, master_dir)
    for submodule_name in submodule_names:
        if submodule_name in port_map:
            print("[check_interfaces]   Loaded: {} ({} ports)".format(
                submodule_name, len(port_map[submodule_name])
            ))

    interfaces = master.get("interfaces", [])
    errors = []
    warnings = []

    if load_errors:
        for issue in load_errors:
            errors.append(issue)
            print("[check_interfaces]   ERROR: {}".format(issue))

    print("[check_interfaces] Pass 1: Checking master.yaml structural integrity...")
    structure_errors = check_structure(interfaces, set(submodule_names))
    if structure_errors:
        for issue in structure_errors:
            errors.append(issue)
            print("[check_interfaces]   ERROR: {}".format(issue))
    else:
        print("[check_interfaces]   {} interface entries -- structure OK".format(
            len(interfaces)
        ))

    print("[check_interfaces] Pass 2: Checking port existence, direction, and width...")
    port_errors, port_warnings = check_ports(interfaces, port_map)
    warnings.extend(port_warnings)

    if port_errors:
        for issue in port_errors:
            print("[check_interfaces]   {}".format(
                format_interface_ref(issue["interface"])
            ))
            for detail in issue["messages"]:
                errors.append(detail)
                print("[check_interfaces]     ERROR: {}".format(detail))
    else:
        signal_width = 0
        for interface in interfaces:
            signal_width = max(signal_width, len(interface.get("signal", "")))

        for interface in interfaces:
            signal = interface.get("signal", "<unnamed>")
            ref = "{}.{} -> {}.{}".format(
                interface.get("from_module", "<missing>"),
                interface.get("from_port", "<missing>"),
                interface.get("to_module", "<missing>"),
                interface.get("to_port", "<missing>")
            )
            print("[check_interfaces]   {signal:<{width}} : {ref}  OK".format(
                signal=signal,
                width=signal_width,
                ref=ref
            ))

    print("[check_interfaces] Pass 3: Checking port coverage...")
    coverage_warnings = check_coverage(interfaces, port_map)
    warnings.extend(coverage_warnings)
    if not port_map:
        print("[check_interfaces]   No loaded submodule ports available for coverage check")
    elif coverage_warnings:
        for issue in coverage_warnings:
            print("[check_interfaces]   WARNING: {}".format(issue))
    else:
        print("[check_interfaces]   All non-global ports appear in at least one interface")

    effective_errors = len(errors)
    if strict_mode:
        effective_errors += len(warnings)

    print("")
    print("=" * 60)
    if effective_errors == 0:
        print("check_interfaces PASSED  ({} errors, {} warnings)".format(
            len(errors), len(warnings)
        ))
    else:
        print("check_interfaces FAILED  ({} errors, {} warnings)".format(
            len(errors), len(warnings)
        ))
    print("=" * 60)

    if effective_errors == 0:
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
