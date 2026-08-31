"""
fsm_generator.py

Deterministic SystemVerilog FSM generator from validated Design IR.

Takes a Design object (from ir_validator.validate_spec) and produces
synthesizable SystemVerilog -- no LLM involved.

Usage (library):
    from fsm_generator import generate_fsm_rtl
    from ir_validator import validate_spec

    design = validate_spec("traffic_light.yaml")
    rtl    = generate_fsm_rtl(design)
    with open("traffic_light.sv", "w") as f:
        f.write(rtl)

Usage (CLI):
    python3 fsm_generator.py traffic_light.yaml
    python3 fsm_generator.py traffic_light.yaml traffic_light.sv

Python 3.6 compatible (no walrus operator, no 3.7+ dataclass features).
"""

import math
import sys

from validator import (
    Design, FSM, Transition, Port, Clock, Reset,
    validate_spec,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _state_bits(n):
    """Minimum bits to encode n states in binary."""
    if n <= 1:
        return 1
    return int(math.ceil(math.log2(n)))


def _counter_bits(max_val):
    """Minimum bits to represent the value max_val (unsigned)."""
    if max_val <= 0:
        return 1
    return int(math.ceil(math.log2(max_val + 1)))


def _expr_to_sv(expr):
    if hasattr(expr, "left") and hasattr(expr, "op") and hasattr(expr, "right"):
        left  = _expr_to_sv(expr.left)
        right = _expr_to_sv(expr.right)
        return "{} {} {}".format(left, expr.op, right)

    elif hasattr(expr, "name"):
        return expr.name

    elif hasattr(expr, "value"):
        return str(expr.value)

    raise ValueError("Unsupported expression: {}".format(expr))


def _sized_literal(width, value):
    """Return an explicitly sized SystemVerilog literal."""
    return "{}'d{}".format(width, value)


def _condition_to_sv(expr, inferred_widths):
    """Convert a transition condition to width-safe SystemVerilog.
    FAILS HARD on unsupported expressions (no silent fallback).
    """

    # Compare: signal OP constant
    if hasattr(expr, "left") and hasattr(expr, "op") and hasattr(expr, "right"):
        # Left must be a signal
        if not hasattr(expr.left, "name"):
            raise ValueError("Unsupported condition: left side is not a signal: {}".format(expr))

        left = expr.left.name

        # Right must be constant
        if hasattr(expr.right, "value"):
            width = inferred_widths.get(left, 1)
            right = "{}'d{}".format(width, expr.right.value)
        else:
            raise ValueError("Unsupported condition: right side is not constant: {}".format(expr))

        return "{} {} {}".format(left, expr.op, right)

    # Logical conditions (future-proofing, safe recursion)
    if hasattr(expr, "conditions") and hasattr(expr, "op"):
        joiner = " {} ".format(expr.op)
        return joiner.join(_condition_to_sv(c, inferred_widths) for c in expr.conditions)

    # Constant condition (rare but valid)
    if hasattr(expr, "value"):
        return "1'd{}".format(expr.value)

    #  HARD FAIL (THIS IS THE FIX)
    raise ValueError("Unsupported condition AST: {}".format(expr))


def _collect_timers(design):
    """
    Walk all transitions and collect counter/timer signal widths and the FSM
    state each counter belongs to (i.e. the state whose outgoing transition
    checks that counter).

    Returns:
        timers       -- dict mapping signal_name -> bit_width (int)
        timer_states -- dict mapping signal_name -> src_state (str)

    Prefers widths already inferred by the validator
    (design.metadata['inferred_signal_widths']); falls back to computing
    from the literal comparison value.

    Excludes signals that are declared ports -- those are inputs/outputs,
    not internal counters (e.g. start, tRCD_done, cmd_valid).
    """
    import re as _re
    inferred   = design.metadata.get("inferred_signal_widths", {})
    port_names = {p.name for p in design.ports}
    timers       = {}
    timer_states = {}   # NEW: counter -> the FSM state it counts in

    _COND_RE = _re.compile(
        r'^([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|<|<=|>|>=)\s*(\d+)$'
    )

    for tr in design.fsm.transitions:
        sig   = None
        width = None

        cond = tr.condition
        if hasattr(cond, "left") and hasattr(cond, "op") and hasattr(cond, "right"):
            if hasattr(cond.left, "name") and hasattr(cond.right, "value"):
                sig   = cond.left.name
                width = inferred.get(sig, _counter_bits(cond.right.value))
        else:
            # Fallback: parse raw_condition string directly.
            # Handles cases where isinstance checks fail due to module
            # import identity issues.
            m = _COND_RE.match(tr.raw_condition.strip())
            if m:
                sig   = m.group(1)
                val   = int(m.group(3))
                width = inferred.get(sig, _counter_bits(val))

        if sig is None or width is None:
            continue

        # Skip signals that are declared ports -- they are inputs or outputs,
        # not internal counters (e.g. start == 1, tRCD_done == 1).
        if sig in port_names:
            continue

        if sig not in timers or width > timers[sig]:
            timers[sig] = width

        # Record which state this counter is checked in.
        # If the same counter appears in multiple transitions (unusual but
        # possible), the first recorded state wins; all should be the same
        # state in well-formed templates.
        if sig not in timer_states:
            timer_states[sig] = tr.src  # NEW

    return timers, timer_states   # NEW: return both dicts


def _output_state_map(design):
    """
    Heuristic: try to match each output port to a state by name.

    The heuristic strips common suffixes (_led, _out, _o, _sig, _n) from
    the port name, upper-cases it, and compares to the state list.

    Returns:
        dict mapping port_name -> matched_state_name  (or None if no match)
    """
    STRIP_SUFFIXES = ["_LED", "_OUT", "_OUTPUT", "_O", "_SIG", "_N"]
    state_upper = {s.upper(): s for s in design.fsm.states}

    result = {}
    for port in design.ports:
        if port.direction != "output":
            continue
        candidate = port.name.upper()

        # Pass 1: strip known RTL suffixes and check full name
        stripped = candidate
        for suffix in STRIP_SUFFIXES:
            if candidate.endswith(suffix):
                stripped = candidate[: -len(suffix)]
                break
        if stripped in state_upper:
            result[port.name] = state_upper[stripped]
            continue

        # Pass 2: check if any state name matches the last word after final '_'
        # Handles: tRCD_done -> DONE, bank_idle -> IDLE, bank_active -> ACTIVE
        if "_" in candidate:
            last_word = candidate.rsplit("_", 1)[-1]
            if last_word in state_upper:
                result[port.name] = state_upper[last_word]
                continue

        # Pass 3: check if any state name is a substring of the port name
        # Handles: bank_active -> ACTIVE (redundant with pass 2 but catches edge cases)
        matched_state = None
        for state_key, state_val in state_upper.items():
            if state_key in candidate:
                matched_state = state_val
                break
        result[port.name] = matched_state   # None if still unmatched
    return result


# ---------------------------------------------------------------------------
# Section generators -- each returns a list of strings (lines, no newlines)
# ---------------------------------------------------------------------------

def _gen_file_header(design):
    return [
        "// ============================================================",
        "// Auto-generated by fsm_generator.py -- DO NOT EDIT",
        "// Design : {}".format(design.design_name),
        "// States : {}".format(", ".join(design.fsm.states)),
        "// Reset  : {} ({})".format(
            design.reset.name if design.reset else "rst_n",
            "sync"  if (design.reset and design.reset.synchronous)  else "async",
        ),
        "// Regenerate: python3 fsm_generator.py <spec>.yaml",
        "// ============================================================",
        "",
    ]


def _gen_module_header(design):
    """
    Build the module declaration and port list.

    Port order: clk, reset, then user ports (inputs before outputs).
    """
    clk_name = design.clock.name if design.clock else "clk"
    rst_name = design.reset.name if design.reset else "rst_n"

    # Separate user ports by direction for ordered output
    inputs  = [p for p in design.ports if p.direction == "input"]
    outputs = [p for p in design.ports if p.direction == "output"]
    ordered_ports = inputs + outputs

    # Build each port declaration string (without trailing comma yet)
    def port_decl(direction, width, name):
        dir_str   = "input " if direction == "input" else "output"
        width_str = "       " if width == 1 else "[{:2d}:0] ".format(width - 1)
        return "    {} logic {}{}".format(dir_str, width_str, name)

    decls = []
    decls.append(port_decl("input", 1, clk_name))
    decls.append(port_decl("input", 1, rst_name))
    for p in ordered_ports:
        decls.append(port_decl(p.direction, p.width, p.name))

    lines = ["module {} (".format(design.design_name)]
    for i, d in enumerate(decls):
        comma = "," if i < len(decls) - 1 else ""
        lines.append(d + comma)
    lines.append(");")
    lines.append("")
    return lines


def _gen_state_typedef(design):
    """Generate typedef enum for state encoding."""
    states = design.fsm.states
    nbits  = _state_bits(len(states))

    lines = [
        "    // ---------------------------------------------------------",
        "    // State type",
        "    // ---------------------------------------------------------",
        "    typedef enum logic [{:d}:0] {{".format(nbits - 1),
    ]
    for i, state in enumerate(states):
        comma = "," if i < len(states) - 1 else ""
        lines.append("        {:<20s} = {}'d{}{}".format(state, nbits, i, comma))
    lines.append("    } state_t;")
    lines.append("")
    lines.append("    state_t current_state, next_state;")
    lines.append("")
    return lines


def _gen_counter_decls(timers):
    """Declare counter/timer signals inferred from transition conditions."""
    if not timers:
        return []
    lines = [
        "    // ---------------------------------------------------------",
        "    // Counters (auto-sized from transition conditions)",
        "    // ---------------------------------------------------------",
    ]
    for sig in sorted(timers):
        width = timers[sig]
        lines.append("    logic [{:d}:0] {};  // {} bit(s), holds up to {}".format(
            width - 1, sig, width, (1 << width) - 1))
        lines.append("    localparam int {}_WIDTH = $bits({});".format(sig.upper(), sig))
        lines.append("    localparam logic [{}_WIDTH-1:0] {}_ZERO_L = {}_WIDTH'(0);".format(
            sig.upper(), sig.upper(), sig.upper()))
        lines.append("    localparam logic [{}_WIDTH-1:0] {}_ONE_L = {}_WIDTH'(1);".format(
            sig.upper(), sig.upper(), sig.upper()))
    lines.append("")
    return lines


def _gen_state_register(design, timers, timer_states):
    """
    Generate the synchronous/asynchronous state register (always_ff).

    Behaviour:
      - On reset: go to reset_state, clear all counters.
      - On state transition: clear all counters (timers measure time-in-state).
      - Otherwise: increment each counter ONLY while in the state it belongs to.
        Counters are held (not incremented) in states where they are not used,
        preventing meaningless accumulation in IDLE or DONE states.
    """
    clk  = design.clock.name  if design.clock  else "clk"
    rst  = design.reset.name  if design.reset  else "rst_n"
    sync = design.reset.synchronous if design.reset else True
    alow = design.reset.active_low  if design.reset else True

    # Sensitivity list
    if sync:
        sens = "posedge {}".format(clk)
    else:
        edge = "negedge" if alow else "posedge"
        sens = "posedge {} or {} {}".format(clk, edge, rst)

    reset_cond = "!{}".format(rst) if alow else rst

    lines = [
        "    // ---------------------------------------------------------",
        "    // State register  ({} reset)".format("sync" if sync else "async"),
        "    // ---------------------------------------------------------",
        "    always_ff @({}) begin".format(sens),
        "        if ({}) begin".format(reset_cond),
        "            current_state <= {};".format(design.fsm.reset_state),
    ]
    for sig in sorted(timers):
        lines.append("            {} <= {}_ZERO_L;".format(sig, sig.upper()))
    lines.append("        end else begin")
    lines.append("            current_state <= next_state;")

    if timers:
        lines.append("            // Reset counters on any state change;")
        lines.append("            // increment only in the state the counter belongs to.")
        lines.append("            if (current_state != next_state) begin")
        for sig in sorted(timers):
            lines.append("                {} <= {}_ZERO_L;".format(sig, sig.upper()))
        lines.append("            end else begin")
        for sig in sorted(timers):
            counting_state = timer_states.get(sig)
            if counting_state:
                # Gate: only increment while in the state that uses this counter.
                lines.append("                if (current_state == {}) begin".format(counting_state))
                lines.append("                    {} <= {} + {}_ONE_L;".format(
                    sig, sig, sig.upper()))
                lines.append("                end")
            else:
                # No state info -- fall back to always-increment (safe but imprecise).
                lines.append("                {} <= {} + {}_ONE_L;".format(
                    sig, sig, sig.upper()))
        lines.append("            end")

    lines.append("        end")
    lines.append("    end")
    lines.append("")
    return lines


def _gen_next_state_logic(design):
    """
    Generate always_comb next-state logic (case statement).

    Multiple transitions from the same state become priority-encoded
    if-else chains within that case branch.
    """
    fsm    = design.fsm
    by_src = {s: [] for s in fsm.states}
    for tr in fsm.transitions:
        by_src[tr.src].append(tr)

    inferred = design.metadata.get("inferred_signal_widths", {})

    lines = [
        "    // ---------------------------------------------------------",
        "    // Next-state logic",
        "    // ---------------------------------------------------------",
        "    always_comb begin",
        "        next_state = current_state;  // default: hold current state",
        "        case (current_state)",
    ]

    for state in fsm.states:
        trans = by_src[state]
        if not trans:
            lines.append("            {}:  // no outgoing transitions (terminal state)".format(state))
            lines.append("                next_state = {};".format(state))
            continue

        if len(trans) == 1:
            tr   = trans[0]
            cond = _condition_to_sv(tr.condition, inferred)
            if cond.strip() == "1":
                lines.append("            {}: next_state = {};".format(
                    state, tr.dst))
            else:
                lines.append("            {}: if ({}) next_state = {};".format(
                    state, cond, tr.dst))
        else:
            lines.append("            {}: begin".format(state))
            for i, tr in enumerate(trans):
                cond = _condition_to_sv(tr.condition, inferred)
                if cond.strip() == "1":
                    lines.append("                else next_state = {};".format(tr.dst))
                elif i == 0:
                    lines.append("                if ({}) next_state = {};".format(
                        cond, tr.dst))
                else:
                    lines.append("                else if ({}) next_state = {};".format(
                        cond, tr.dst))
            lines.append("            end")

    # Safe recovery for any illegal/unused state encoding (e.g. state 2'd3
    # in a 3-state FSM with 2-bit encoding).  Without this, a stuck FSM
    # would hold forever because the always_comb default assigns
    # next_state = current_state.
    lines.append("            default: next_state = {};  // illegal encoding -- recover to reset state".format(
        fsm.reset_state))
    lines.append("        endcase")
    lines.append("    end")
    lines.append("")
    return lines


def _gen_output_logic(design):
    """
    Generate output assignments (Moore machine style).

    Priority order per output port:
      1. Explicit override from design.metadata["output_overrides"] -- used when
         the name-matching heuristic cannot express the correct logic (e.g.
         multi-state outputs like cmd_ready, or state-entry pulses like tRCD_start).
      2. Name-matching heuristic (_output_state_map).
      3. Default 1'b0 with a TODO comment.

    output_overrides is a {port_name: sv_expression_string} dict, e.g.:
        {"cmd_ready":  "(current_state == IDLE) || (current_state == ACTIVE)",
         "tRCD_start": "(current_state == ACTIVATING)"}
    It is populated by expand_spec.py via the submodule YAML -> validator metadata
    path, so the generator itself stays data-driven and never needs hardcoded
    per-design special cases.
    """
    outputs = [p for p in design.ports if p.direction == "output"]
    if not outputs:
        return []

    # Explicit overrides take priority over the heuristic.
    output_overrides = design.metadata.get("output_overrides", {})
    state_map = _output_state_map(design)

    lines = [
        "    // ---------------------------------------------------------",
        "    // Output logic (Moore)",
        "    // ---------------------------------------------------------",
        "    always_comb begin",
    ]
    for port in outputs:
        if port.name in output_overrides:
            # Explicit SV expression from YAML annotation -- no heuristic needed.
            lines.append("        {} = {};".format(
                port.name, output_overrides[port.name]))
        else:
            matched = state_map.get(port.name)
            if matched:
                if port.width == 1:
                    lines.append("        {} = (current_state == {});".format(
                        port.name, matched))
                else:
                    # Multi-bit: replicate the comparison across all bits
                    lines.append("        {} = {{{}'{{(current_state == {})}}}};".format(
                        port.name, port.width, matched))
            else:
                # Default zero with correct SV literal syntax: 1'b0 or N'b000...
                zero_literal = "{}'b{}".format(port.width, "0" * port.width)
                lines.append("        {} = {};  // TODO: specify output logic in YAML".format(
                    port.name, zero_literal
                ))
    lines.append("    end")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def generate_fsm_rtl(design):
    """
    Generate synthesizable SystemVerilog from a validated Design IR.

    Args:
        design  -- Design object returned by ir_validator.validate_spec()

    Returns:
        str     -- complete SystemVerilog source for the module

    Raises:
        ValueError  if design.fsm is None
    """
    if design.fsm is None:
        raise ValueError(
            "Design '{}' has no FSM. "
            "Only design_type='fsm' is supported by this generator.".format(
                design.design_name))

    timers, timer_states = _collect_timers(design)   # unpack both dicts
    warnings = design.metadata.get("warnings", [])

    sections = []
    sections.append(_gen_file_header(design))
    sections.append(_gen_module_header(design))
    sections.append(_gen_state_typedef(design))
    sections.append(_gen_counter_decls(timers))
    sections.append(_gen_state_register(design, timers, timer_states))  # pass timer_states
    sections.append(_gen_next_state_logic(design))
    sections.append(_gen_output_logic(design))

    # Attach validator warnings as comments before endmodule
    if warnings:
        sections.append(["    // --- Validator warnings ---"])
        for w in warnings:
            sections.append(["    // WARNING: {}".format(w)])
        sections.append([""])

    sections.append(["endmodule  // {}".format(design.design_name)])

    all_lines = []
    for section in sections:
        all_lines.extend(section)

    return "\n".join(all_lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 fsm_generator.py <spec>.yaml [output.sv]")
        sys.exit(1)

    yaml_path = sys.argv[1]
    out_path  = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        design = validate_spec(yaml_path)
    except Exception as e:
        print("Validation error: {}".format(e))
        sys.exit(2)

    try:
        rtl = generate_fsm_rtl(design)
    except Exception as e:
        print("Generation error: {}".format(e))
        sys.exit(3)

    if out_path:
        with open(out_path, "w") as fh:
            fh.write(rtl)
        print("Written: {}".format(out_path))
    else:
        print(rtl)
