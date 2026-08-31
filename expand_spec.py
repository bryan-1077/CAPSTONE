#!/usr/bin/env python3
"""
expand_spec.py
==============
Compiler pass: High-level DDR YAML -> concrete submodule YAMLs.

Reads:
    <input.yaml>               -- user's high-level DDR spec
    jedec_dictionary.yaml      -- JEDEC timing profiles (ns + cycle counts)
    feature_templates.yaml     -- FSM skeletons and datapath templates with
                                  symbolic placeholders

Writes:
    expanded/<design_name>/<submodule_name>.yaml  -- one per submodule
    expanded/<design_name>/master.yaml            -- submodule list + interfaces

Supports two submodule types:
    FSM      (design_type: "fsm" or absent in template)
             -> state_machine block written to output YAML
             -> routes to fsm_generator.py via design.py
    Datapath (design_type: "datapath" in template)
             -> behavior block substituted and written to output YAML
             -> routes to LLM generation path via design.py
             -> no state_machine block; validator.py FSM field will be None

Usage:
    python3 expand_spec.py inputs/simple_ddr.yaml
    python3 expand_spec.py inputs/simple_ddr.yaml --output-dir my_output/

Then run each submodule through the pipeline:
    python3 design.py expanded/ddr4_bank/ddr4_bank_activate_fsm.yaml
    python3 design.py expanded/ddr4_bank/ddr4_bank_tFAW_tracker.yaml
    ...

Python 3.6 compatible — no f-strings, no text=True in subprocess.
Do NOT modify validator.py or fsm_generator.py.
"""

import sys
import os
import re
import math
import yaml


# ---------------------------------------------------------------------------
# File paths — resolved relative to this script's directory so the script
# can be called from any working directory.
# ---------------------------------------------------------------------------
SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
JEDEC_DICT_PATH     = os.path.join(SCRIPT_DIR, "jedec/jedec_dictionary.yaml")
TEMPLATES_PATH      = os.path.join(SCRIPT_DIR, "jedec/feature_templates.yaml")
DEFAULT_OUTPUT_DIR  = os.path.join(SCRIPT_DIR, "expanded")


# ---------------------------------------------------------------------------
# Supported values — hard gate so unsupported input fails loudly.
# Extend these lists as new profiles / features are added.
# ---------------------------------------------------------------------------
SUPPORTED_PROTOCOLS = {"DDR4"}
SUPPORTED_PROFILES  = {"DDR4-2400", "DDR4-3200"}
SUPPORTED_FEATURES  = {
    "basic_commands",
    "request_queue",
    "tFAW_tracker",
    "tRRD",
    "refresh_controller",
    "scheduler",
    "scheduler_round_robin",
}


# ===========================================================================
# I/O helpers
# ===========================================================================

def load_yaml(path):
    """Load a YAML file and return the parsed object. Raises on missing file."""
    if not os.path.isfile(path):
        raise IOError("File not found: {}".format(path))
    with open(path, "r") as fh:
        return yaml.safe_load(fh)


def write_yaml(data, path):
    """Write a dict to a YAML file. Creates intermediate directories."""
    dirpath = os.path.dirname(path)
    if dirpath and not os.path.isdir(dirpath):
        os.makedirs(dirpath)
    with open(path, "w") as fh:
        yaml.dump(data, fh, default_flow_style=False)


# ===========================================================================
# Validation helpers
# ===========================================================================

def validate_high_level_yaml(spec):
    """
    Check that the high-level YAML has the required fields.
    Raises ValueError with a clear message on any problem.
    """
    if "design_name" not in spec or not spec["design_name"]:
        raise ValueError("High-level YAML missing required field: design_name")

    if "controller_config" not in spec:
        raise ValueError("High-level YAML missing required section: controller_config")

    cfg = spec["controller_config"]

    if "protocol" not in cfg:
        raise ValueError("controller_config missing: protocol")
    if cfg["protocol"] not in SUPPORTED_PROTOCOLS:
        raise ValueError(
            "Unsupported protocol '{}'. Supported: {}".format(
                cfg["protocol"], sorted(SUPPORTED_PROTOCOLS)
            )
        )

    if "jedec_profile" not in cfg:
        raise ValueError("controller_config missing: jedec_profile")
    if cfg["jedec_profile"] not in SUPPORTED_PROFILES:
        raise ValueError(
            "Unsupported JEDEC profile '{}'. Supported: {}".format(
                cfg["jedec_profile"], sorted(SUPPORTED_PROFILES)
            )
        )

    if "features" not in cfg or not cfg["features"]:
        raise ValueError("controller_config missing or empty: features")

    for feature in cfg["features"]:
        if feature not in SUPPORTED_FEATURES:
            raise ValueError(
                "Unsupported feature '{}'. Supported: {}".format(
                    feature, sorted(SUPPORTED_FEATURES)
                )
            )


# ===========================================================================
# Substitution logic
# ===========================================================================

def build_substitution_map(jedec_profile_data, timer_parameters):
    """
    Build a {placeholder_name: integer_value} map.

    timer_parameters (from feature_templates.yaml) looks like:
        tRCD_cycles: "timing_cycles.tRCD"
        tRAS_cycles: "timing_cycles.tRAS"
        tRP_cycles:  "timing_cycles.tRP"

    jedec_profile_data is the dict for one profile from jedec_dictionary.yaml.

    Returns e.g.: {"tRCD_cycles": 17, "tRAS_cycles": 39, "tRP_cycles": 17}
    """
    sub_map = {}
    for placeholder, key_path in timer_parameters.items():
        # Walk the dot-separated key path into jedec_profile_data.
        # e.g. "timing_cycles.tRCD" -> jedec_profile_data["timing_cycles"]["tRCD"]
        parts = key_path.split(".")
        value = jedec_profile_data
        for part in parts:
            if part not in value:
                raise KeyError(
                    "JEDEC dictionary missing key '{}' (path: '{}')".format(
                        part, key_path
                    )
                )
            value = value[part]

        if not isinstance(value, int):
            raise TypeError(
                "Expected integer for '{}', got {} ({})".format(
                    key_path, type(value).__name__, value
                )
            )
        sub_map[placeholder] = value

    return sub_map


def substitute_condition(condition_str, sub_map):
    """
    Replace {PARAM_NAME} placeholders in a condition string with integer values.

    Example:
        condition_str = "tRCD_counter >= {tRCD_cycles}"
        sub_map       = {"tRCD_cycles": 17}
        returns         "tRCD_counter >= 17"

    Raises ValueError if any placeholder is unresolved after substitution.
    """
    result = condition_str
    for placeholder, value in sub_map.items():
        result = result.replace("{" + placeholder + "}", str(value))

    # Check for leftover placeholders — signals a template/dictionary mismatch.
    remaining = re.findall(r'\{[A-Za-z_][A-Za-z0-9_]*\}', result)
    if remaining:
        raise ValueError(
            "Unresolved placeholder(s) in condition '{}': {}\n"
            "  Check that timer_parameters in feature_templates.yaml covers "
            "all placeholders used in transitions.".format(
                condition_str, remaining
            )
        )

    return result


# ===========================================================================
# Submodule YAML builder
# ===========================================================================

def substitute_behavior(behavior, sub_map):
    """
    Recursively walk a behavior dict (or any nested structure) and apply
    placeholder substitution to every string value found.

    This is necessary for datapath submodules where {PARAM} placeholders
    appear inside the behavior block rather than in FSM transition conditions.
    Lists, dicts, and scalar strings are all handled; non-string scalars
    (ints, bools, None) are passed through unchanged.

    Example:
        behavior = {"tFAW_cycles": "{tFAW_cycles}", "desc": "wait {tFAW_cycles}"}
        sub_map  = {"tFAW_cycles": 30}
        returns    {"tFAW_cycles": "30", "desc": "wait 30"}
    """
    if isinstance(behavior, dict):
        return {
            k: substitute_behavior(v, sub_map)
            for k, v in behavior.items()
        }
    elif isinstance(behavior, list):
        return [substitute_behavior(item, sub_map) for item in behavior]
    elif isinstance(behavior, str):
        substituted = substitute_condition(behavior, sub_map)
        if re.fullmatch(r"-?\d+", substituted):
            return int(substituted)
        return substituted
    else:
        # int, bool, None — no substitution needed
        return behavior


def build_submodule_yaml(submodule_template, design_name, sub_map):
    """
    Convert one submodule template entry into a dict that validator.py
    can consume directly.

    The generated design_name follows the convention:
        <top_design_name>_<submodule_name>
    e.g. "ddr4_bank_activate_fsm", "ddr4_bank_tFAW_tracker"

    Supports two submodule types, selected by the template's design_type field:

    FSM submodules (design_type absent or "fsm"):
        - Iterate state_machine.transitions and substitute condition placeholders.
        - Write state_machine block into the output YAML.
        - Pass through output_overrides if present.
        - Identical behaviour to the original function — DDR bank path unchanged.

    Datapath submodules (design_type == "datapath"):
        - No state_machine block — do not attempt to read transitions.
        - Write design_type: "datapath" so design.py routes to the LLM path.
        - Substitute all {PARAM} placeholders throughout the behavior block.
        - Include the substituted behavior block in the output YAML so the
          LLM receives full implementation guidance via the raw YAML path.
    """
    submodule_name = submodule_template["name"]

    # Fix 3: read design_type from the template; fall back to "fsm" when absent
    # so that existing DDR bank templates (which don't set this key) are unchanged.
    design_type = submodule_template.get("design_type", "fsm")

    # -----------------------------------------------------------------------
    # Common fields — identical for both submodule types.
    # -----------------------------------------------------------------------
    output_design_name = submodule_template.get(
        "module_design_name",
        "{}_{}".format(design_name, submodule_name),
    )

    output = {
        "design_name": output_design_name,
        "design_type": design_type,
        "clock":       submodule_template["clock"],
        "reset":       submodule_template["reset"],
        "ports":       submodule_template["ports"],
    }

    passthrough_keys = [
        "implementation_constraints",
        "invariants",
        "forbidden_patterns",
        "scheduler_mode",
    ]
    for key in passthrough_keys:
        if key in submodule_template:
            output[key] = submodule_template[key]

    # -----------------------------------------------------------------------
    # FSM path (design_type == "fsm" or absent)
    # Exactly the original logic — no changes for the DDR bank submodules.
    # -----------------------------------------------------------------------
    if design_type == "fsm":
        # Fix 2: state_machine access is now gated behind the FSM branch.
        # Datapath submodules never reach this block, so no KeyError.
        transitions = []
        for t in submodule_template["state_machine"]["transitions"]:
            raw_condition = t["condition"]
            concrete_condition = substitute_condition(raw_condition, sub_map)
            transitions.append({
                "from":      t["from"],
                "to":        t["to"],
                "condition": concrete_condition
            })

        output["state_machine"] = {
            "states":      submodule_template["state_machine"]["states"],
            "reset_state": submodule_template["state_machine"]["reset_state"],
            "transitions": transitions
        }

        # Pass through output_overrides when present (bank_sequencer uses this).
        if "output_overrides" in submodule_template:
            output["output_overrides"] = submodule_template["output_overrides"]

    # -----------------------------------------------------------------------
    # Datapath path (design_type == "datapath")
    # No state_machine. Substitute and forward the behavior block instead.
    # -----------------------------------------------------------------------
    else:
        # Fix 4: substitute {PARAM} placeholders throughout the behavior block
        # and include it in the output so the LLM receives full guidance.
        if "behavior" in submodule_template:
            output["behavior"] = substitute_behavior(
                submodule_template["behavior"], sub_map
            )

    return output


# ===========================================================================
# Master YAML builder
# ===========================================================================

def build_master_yaml(design_name, feature_name, jedec_profile_name,
                      submodule_paths, feature_template):
    """
    Build the master YAML that lists all submodules and their interface
    connections. This is consumed by a future check_interfaces.py and
    serves as the source of truth for the hand-written top-level wrapper.

    submodule_paths: {submodule_name: path_to_generated_yaml}
    """
    submodule_entries = []
    for name, path in submodule_paths.items():
        submodule_entries.append({
            "name":        name,
            "spec":        path,
            "design_name": "{}_{}".format(design_name, name)
        })

    master = {
        "design_name":    design_name,
        "design_type":    "top_level",
        "source_feature": feature_name,
        "jedec_profile":  jedec_profile_name,
        "submodules":     submodule_entries,
        "interfaces":     feature_template.get("interfaces", [])
    }

    return master


# ===========================================================================
# Cycle count verification
# ===========================================================================

def verify_cycle_counts(sub_map, jedec_profile_name):
    """
    Cross-check precomputed cycle counts against the formula
        cycles = ceil(timing_ns * freq_mhz / 1000)
    using the ns values from the JEDEC dictionary.

    Prints a warning (not a hard error) if a mismatch is found, since the
    YAML's pre-stored cycles are authoritative — this is just a sanity gate.
    """
    # Load the dictionary again to cross-check ns values.
    # (This function is called after the dictionary is already loaded,
    # but we access it via sub_map which only has final cycle values.
    # The check is done separately in expand_spec() where we have the full data.)
    pass   # Implemented inline in expand_spec() below for access to full data.


# ===========================================================================
# Main expansion logic
# ===========================================================================

def expand_spec(input_path, output_dir=None):
    """
    Full expansion pipeline. Returns the path to the master YAML.

    Steps:
        1. Load + validate high-level YAML
        2. Load JEDEC dictionary, look up requested profile
        3. Load feature templates, look up requested feature
        4. Build substitution map (placeholder -> integer)
        5. For each submodule: substitute -> write validator-compatible YAML
        6. Write master YAML
        7. Print summary
    """

    # -----------------------------------------------------------------------
    # Step 1: Load and validate the high-level input YAML
    # -----------------------------------------------------------------------
    print("[expand_spec] Loading: {}".format(input_path))
    spec = load_yaml(input_path)
    validate_high_level_yaml(spec)

    design_name    = spec["design_name"]
    cfg            = spec["controller_config"]
    profile_name   = cfg["jedec_profile"]
    feature_names  = cfg["features"]

    print("[expand_spec] Design:  {}".format(design_name))
    print("[expand_spec] Profile: {}".format(profile_name))
    print("[expand_spec] Features: {}".format(", ".join(feature_names)))

    # -----------------------------------------------------------------------
    # Step 2: Load JEDEC dictionary and look up the requested profile
    # -----------------------------------------------------------------------
    print("[expand_spec] Loading JEDEC dictionary: {}".format(JEDEC_DICT_PATH))
    jedec_dict = load_yaml(JEDEC_DICT_PATH)

    if profile_name not in jedec_dict:
        raise KeyError(
            "JEDEC profile '{}' not found in {}. Available: {}".format(
                profile_name, JEDEC_DICT_PATH, list(jedec_dict.keys())
            )
        )

    jedec_profile = jedec_dict[profile_name]
    freq_mhz      = jedec_profile["clock_frequency_mhz"]
    timing_ns     = jedec_profile["timing_ns"]
    timing_cycles = jedec_profile["timing_cycles"]

    # Sanity-check the pre-stored cycle counts against the formula.
    print("[expand_spec] Verifying cycle counts for {} ({} MHz)...".format(
        profile_name, freq_mhz
    ))
    warnings = []
    for param, ns_val in timing_ns.items():
        if param not in timing_cycles:
            continue
        expected = int(math.ceil(ns_val * freq_mhz / 1000.0))
        stored   = timing_cycles[param]
        if expected != stored:
            warnings.append(
                "  WARNING: {} cycles mismatch — stored={}, computed={}".format(
                    param, stored, expected
                )
            )
        else:
            print("[expand_spec]   {} = {} ns -> {} cycles  OK".format(
                param, ns_val, stored
            ))

    if warnings:
        for w in warnings:
            print(w)
        print("[expand_spec] WARNING: cycle count mismatches found above.")
        print("[expand_spec] Stored values in jedec_dictionary.yaml are used.")

    # -----------------------------------------------------------------------
    # Step 3: Load feature templates
    # -----------------------------------------------------------------------
    print("[expand_spec] Loading feature templates: {}".format(TEMPLATES_PATH))
    templates = load_yaml(TEMPLATES_PATH)

    # -----------------------------------------------------------------------
    # Step 4: Set up output directory
    # -----------------------------------------------------------------------
    if output_dir is None:
        output_dir = os.path.join(DEFAULT_OUTPUT_DIR, design_name)

    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    print("[expand_spec] Output directory: {}".format(output_dir))

    # -----------------------------------------------------------------------
    # Step 5: Process each requested feature
    # For Week 10 scope, features list has exactly one entry: "basic_commands".
    # The loop structure is in place for future multi-feature support.
    # -----------------------------------------------------------------------
    all_submodule_paths = {}   # {submodule_name: output_yaml_path}
    last_feature_template = None

    for feature_name in feature_names:
        print("[expand_spec] Processing feature: {}".format(feature_name))

        if feature_name not in templates:
            raise KeyError(
                "Feature '{}' not found in {}. Available: {}".format(
                    feature_name, TEMPLATES_PATH, list(templates.keys())
                )
            )

        feature_template = templates[feature_name]
        last_feature_template = feature_template

        # Build substitution map: {placeholder: integer_value}
        timer_params = feature_template.get("timer_parameters", {})
        sub_map = build_substitution_map(jedec_profile, timer_params)

        print("[expand_spec] Substitution map:")
        for placeholder, value in sub_map.items():
            print("[expand_spec]   {{{}}} -> {}".format(placeholder, value))

        # Process each submodule in this feature
        for submodule_template in feature_template["submodules"]:
            submodule_name = submodule_template["name"]
            print("[expand_spec] Expanding submodule: {}".format(submodule_name))

            # Build the concrete YAML dict
            submodule_yaml = build_submodule_yaml(
                submodule_template, design_name, sub_map
            )

            # Write to output directory
            filename    = "{}_{}.yaml".format(design_name, submodule_name)
            output_path = os.path.join(output_dir, filename)
            write_yaml(submodule_yaml, output_path)

            all_submodule_paths[submodule_name] = output_path
            print("[expand_spec]   Written: {}".format(output_path))

    # -----------------------------------------------------------------------
    # Step 6: Write master YAML
    # -----------------------------------------------------------------------
    master_yaml = build_master_yaml(
        design_name          = design_name,
        feature_name         = feature_names[0],    # single feature for now
        jedec_profile_name   = profile_name,
        submodule_paths      = all_submodule_paths,
        feature_template     = last_feature_template
    )

    master_path = os.path.join(output_dir, "master.yaml")
    write_yaml(master_yaml, master_path)
    print("[expand_spec] Master YAML written: {}".format(master_path))

    # -----------------------------------------------------------------------
    # Step 7: Print summary
    # -----------------------------------------------------------------------
    print("")
    print("=" * 60)
    print("Expansion complete: {} -> {} submodules".format(
        design_name, len(all_submodule_paths)
    ))
    print("Profile:  {}  ({} MHz)".format(profile_name, freq_mhz))
    print("Feature:  {}".format(", ".join(feature_names)))
    print("")
    print("Generated files:")
    for name, path in all_submodule_paths.items():
        print("  {}".format(path))
    print("  {}".format(master_path))
    print("=" * 60)

    return master_path


# ===========================================================================
# CLI entry point
# ===========================================================================

def parse_args(argv):
    """
    Minimal argument parser (no argparse dependency).
    Returns (input_path, output_dir_or_None).
    """
    if len(argv) < 2:
        print("Usage: python3 expand_spec.py <input.yaml> [--output-dir <dir>]")
        print("")
        print("Example:")
        print("  python3 expand_spec.py inputs/simple_ddr.yaml")
        print("  python3 expand_spec.py inputs/simple_ddr.yaml --output-dir expanded/")
        sys.exit(1)

    input_path = argv[1]
    output_dir = None

    i = 2
    while i < len(argv):
        if argv[i] == "--output-dir" and i + 1 < len(argv):
            output_dir = argv[i + 1]
            i += 2
        else:
            print("Unknown argument: {}".format(argv[i]))
            sys.exit(1)

    return input_path, output_dir


def main():
    input_path, output_dir = parse_args(sys.argv)

    try:
        master_path = expand_spec(input_path, output_dir)
        sys.exit(0)

    except (IOError, KeyError, ValueError, TypeError) as e:
        print("")
        print("ERROR: {}".format(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
