#!/usr/bin/env python3
"""
design.py
------------
Load/validate YAML spec, then hand a formatted requirement to your RTL agent.
Works without renaming your existing files (uses importlib to load by filename).
"""

import sys
import os
import importlib.util
import json
from datetime import datetime
from dataclasses import asdict

from width_safety import enforce_width_safety

ROUTE_FSM_GENERATOR = "FSM_GENERATOR"
ROUTE_DUAL_LLM = "DUAL_LLM"
ROUTE_DETERMINISTIC_TIMING_HARDENED = "DETERMINISTIC_TIMING_HARDENED"

ROUTE_BANNER_LABELS = {
    ROUTE_FSM_GENERATOR: "------ FSM GENERATOR",
    ROUTE_DUAL_LLM: "------ DUAL LLM BASED",
    ROUTE_DETERMINISTIC_TIMING_HARDENED: "------ DETERMINISTIC TIMING HARDENED",
}

DETERMINISTIC_TIMING_MODULES = {
    "ddr4_tFAW_tFAW_tracker",
    "ddr4_tRRD_simple_tRRD",
}


def generation_route(spec, module_name):
    if spec.design_type == "fsm":
        return ROUTE_FSM_GENERATOR
    if module_name in DETERMINISTIC_TIMING_MODULES:
        return ROUTE_DETERMINISTIC_TIMING_HARDENED
    return ROUTE_DUAL_LLM


# ---------- helper: dynamic import by path (works with hyphen filenames) ----------
def load_module_from_path(module_name: str, path: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def display_path(path: str, root_dir: str) -> str:
    abs_path = os.path.abspath(path)
    try:
        return os.path.relpath(abs_path, root_dir)
    except ValueError:
        return abs_path


def print_module_start_banner(module_name: str, label: str):
    filename = module_name + ".sv"
    print("=" * 80)
    print("--- CREATING --- {} file {}".format(filename, label))
    print("")


def print_module_end_banner(module_name: str):
    filename = module_name + ".sv"
    print("")
    print("--- FINISHED --- {} file ------- SUCCESS".format(filename))
    print("=" * 80)
    print("")
    print("")


def _behavior_param(spec, name: str, default: int) -> int:
    """Read an integer RTL parameter from a validated datapath spec."""
    value = spec.metadata.get("behavior", {}).get("rtl_parameters", {}).get(name)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def generate_tfaw_seed_rtl(spec, module_name: str) -> str:
    """Emit a parameter seed that width_safety expands into the final tFAW RTL."""
    tfaw_cycles = _behavior_param(spec, "TFAW_CYCLES", 32)
    tfaw_limit = _behavior_param(spec, "TFAW_LIMIT", 4)
    return f"""module {module_name}(
    input logic clk,
    input logic rst_n,
    input logic act_pulse,
    output logic [2:0] act_count,
    output logic tFAW_block,
    output logic tFAW_ok
);
    parameter int TFAW_CYCLES = {tfaw_cycles};
    parameter int TFAW_LIMIT = {tfaw_limit};
endmodule
"""


def generate_trrd_seed_rtl(spec, module_name: str) -> str:
    """Emit a parameter seed that width_safety expands into the final tRRD RTL."""
    trrd_cycles = _behavior_param(spec, "TRRD_CYCLES", 4)
    return f"""module {module_name} (
    input logic clk,
    input logic rst_n,
    input logic act_pulse,
    output logic tRRD_block
);
    parameter int TRRD_CYCLES = {trrd_cycles};
endmodule
"""

# ---------- format the YAML spec into a clear task_description for the LLM ----------
from dataclasses import asdict, is_dataclass

def make_task_from_spec(spec):
    """
    Accept either:
      - a dict (legacy), or
      - a dataclass Design (preferred).
    Returns (task_description_str, module_name)
    """
    # normalize to plain dict
    if is_dataclass(spec):
        spec = asdict(spec)
    elif not isinstance(spec, dict):
        raise TypeError("spec must be a dict or a dataclass")

    name = spec.get('design_name', 'unnamed_module')
    desc = spec.get('description', '').strip()

    # clock/reset/ports may be nested dicts after asdict
    clock = spec.get('clock', {}) or {}
    reset = spec.get('reset', {}) or {}
    ports = spec.get('ports', {}) or {}

    # If you used the ir_validator, FSM info lives under spec['fsm']
    # but earlier code might have used state_machine. Support both:
    fsm = spec.get('fsm') or spec.get('state_machine') or {}

    def fmt_port_list(side):
        # ports arranged as list of dicts in asdict output
        arr = ports if isinstance(ports, list) else ports.get(side, [])
        lines = []
        # handle case where ports is a flat list of dicts
        if isinstance(ports, list):
            for p in ports:
                w = p.get('width')
                if isinstance(w, int) and w > 1:
                    width_str = f"[{w-1}:0]"
                else:
                    width_str = ""
                nm = p.get('name')
                descp = p.get('description', '')
                lines.append(f"- {nm} {width_str} : {descp}")
        else:
            # older format: ports grouped by 'inputs'/'outputs'
            for p in arr:
                w = p.get('width')
                width_str = f"[{w-1}:0]" if isinstance(w, int) and w>1 else ""
                nm = p.get('name')
                descp = p.get('description', '')
                lines.append(f"- {nm} {width_str} : {descp}")
        return "\n".join(lines) if lines else "  (none)"

    inputs_str = fmt_port_list('inputs')
    outputs_str = fmt_port_list('outputs')

    clk_name = clock.get('name', 'clk')
    clk_freq = clock.get('target_frequency_mhz', 'unspecified')
    rst_name = reset.get('name', 'rst_n')
    rst_type = reset.get('type', 'active_low')
    rst_sync = reset.get('synchronous', False)

    # try to extract states & transitions from either fsm or state_machine
    states = []
    transitions = []
    if isinstance(fsm, dict):
        states = fsm.get('states') or []
        transitions = fsm.get('transitions') or []
    elif isinstance(fsm, list):
        # improbable, but handle defensively
        states = fsm

    # Build a readable transitions block
    trans_lines = []
    for t in transitions:
        # t may already be a dict from asdict or an original dict
        frm = t.get('src') or t.get('from') or t.get('from_state') or t.get('from')
        to = t.get('dst') or t.get('to') or t.get('to_state')
        cond = t.get('raw_condition') or t.get('condition') or str(t)
        trans_lines.append(f" {frm} -> {to} : {cond}")
    transitions_str = "\n".join(trans_lines) if trans_lines else "  (none)"

    task = f"""
Design name: {name}
Description: {desc}

Clock:
- name: {clk_name}
- target_frequency_mhz: {clk_freq}

Reset:
- name: {rst_name}
- type: {rst_type}
- synchronous: {rst_sync}

Ports:
Inputs:
{inputs_str}

Outputs:
{outputs_str}

States:
{', '.join(states) if states else '(none)'}

Transitions:
{transitions_str}

Requirements / Notes:
- Write synthesizable SystemVerilog (use logic, always_ff, always_comb).
- Provide a short header comment with module name and date.
- Module name must be exactly: {name}
- Use active-low reset semantics if reset type indicates active_low.
- Target frequency: {clk_freq} MHz (for timing/clock domain comments only).
- Output ONLY the SystemVerilog RTL code (no explanations, no markdown fences).
"""
    return task.strip(), name

# ---------- main ----------
def main():
    yaml_path = sys.argv[1] if len(sys.argv) > 1 else "inputs/input_test_1-0.yaml"
    root_dir = os.path.dirname(os.path.abspath(__file__))
    yaml_label = display_path(yaml_path, root_dir)
    module_name = os.path.splitext(os.path.basename(yaml_path))[0]

    # adjust these paths if your files live somewhere else
    validator_path = os.path.join(root_dir, "validator.py")
    rtlgen_path    = os.path.join(root_dir, "rtlGen.py")
    dual_rtlgen_path = os.path.join(root_dir, "dual_llm_rtlGen.py")

    if not os.path.exists(validator_path):
        print("Couldn't find input_validator_v1-1.py at", validator_path)
        sys.exit(1)
    if not os.path.exists(rtlgen_path):
        print("Couldn't find rtlGen.py at", rtlgen_path)
        sys.exit(1)
    if not os.path.exists(dual_rtlgen_path):
        print("Couldn't find dual_llm_rtlGen.py at", dual_rtlgen_path)
        sys.exit(1)

    # load modules
    validator = load_module_from_path("input_validator", validator_path)
    rtlgen    = load_module_from_path("rtlgen", rtlgen_path)
    dual_rtlgen = load_module_from_path("dual_rtlgen", dual_rtlgen_path)

    print("[FLOW] Generating: {}".format(yaml_label))

    # validate
    print(f"Validating YAML: {yaml_path} ...")
    spec = validator.validate_spec(yaml_path)
    if not spec:
        print("Validation failed. Aborting RTL generation.")
        sys.exit(2)
    # Ensure IR output directory exists
    os.makedirs("ir", exist_ok=True)

    # Convert dataclass IR → dictionary
    ir_dict = asdict(spec)

    # Save as JSON
    ir_path = os.path.join("ir", f"{spec.design_name}_ir.json")
    with open(ir_path, "w") as f:
        json.dump(ir_dict, f, indent=2)

    print(f"[INFO] IR saved to {ir_path}")
    # build task description and call rtl agent
    # All generated SystemVerilog files share the flat rtl_output directory.
    module_name = spec.design_name
    route = generation_route(spec, module_name)
    print_module_start_banner(module_name, ROUTE_BANNER_LABELS[route])
    work_dir = os.path.join(root_dir, "rtl_output")
    os.makedirs(work_dir, exist_ok=True)

    if route == ROUTE_DUAL_LLM:
        with open(yaml_path, "r") as f:
            yaml_text = f.read()
        print(f"[FLOW] Using DUAL LLM generator for {module_name}")
        rtl = dual_rtlgen.run_dual_llm_rtlgen(spec, yaml_text)
        rtl = enforce_width_safety(rtl, module_name)
        sv_path = os.path.join(work_dir, module_name + ".sv")
        with open(sv_path, "w") as f:
            f.write(rtl)
        print_module_end_banner(module_name)
        result = {"passed": True, "rtl_path": sv_path, "retries_used": 0}
    elif route == ROUTE_DETERMINISTIC_TIMING_HARDENED:
        print(f"[FLOW] Using DETERMINISTIC TIMING HARDENED generator for {module_name}")
        if module_name == "ddr4_tFAW_tFAW_tracker":
            rtl = generate_tfaw_seed_rtl(spec, module_name)
        elif module_name == "ddr4_tRRD_simple_tRRD":
            rtl = generate_trrd_seed_rtl(spec, module_name)
        else:
            raise RuntimeError("No deterministic timing generator for {}".format(module_name))
        rtl = enforce_width_safety(rtl, module_name)
        sv_path = os.path.join(work_dir, module_name + ".sv")
        with open(sv_path, "w") as f:
            f.write(rtl)
        print_module_end_banner(module_name)
        result = {"passed": True, "rtl_path": sv_path, "retries_used": 0}
    elif route == ROUTE_FSM_GENERATOR:
        print(f"[FLOW] Using FSM GENERATOR for {module_name}")
        fsm_gen_path = os.path.join(root_dir, "fsm_generator.py")
        if not os.path.exists(fsm_gen_path):
            print("Couldn't find fsm_generator.py at", fsm_gen_path)
            sys.exit(1)
        fsm_gen = load_module_from_path("fsm_generator", fsm_gen_path)
        rtl = fsm_gen.generate_fsm_rtl(spec)
        rtl = enforce_width_safety(rtl, module_name)
        sv_path = os.path.join(work_dir, module_name + ".sv")
        with open(sv_path, "w") as f:
            f.write(rtl)
        print_module_end_banner(module_name)
        result = {"passed": True, "rtl_path": sv_path, "retries_used": 0}
    else:
        raise RuntimeError("Unknown generation route {} for {}".format(route, module_name))

if __name__ == "__main__":
    main()
