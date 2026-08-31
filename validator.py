"""
ir_validator.py

Lightweight IR model + YAML->IR conversion + semantic checks for basic FSMs.

Features:
- Dataclass-based IR (Port, Clock, Reset, Expr AST, Transition, FSM, Design)
- validate_spec(path) -> Design (raises ValidationError on problems)
- Minimal condition parser supporting: name (signal) <op> integer
  where <op> in ==, !=, <, <=, >, >=
- Basic semantic checks: states non-empty, unique states, transitions reference states,
  no duplicate transitions (same src/dst/cond text), default reset state handling,
  counter width inference for literal comparisons.
- CLI test-run: `python ir_validator.py path/to/spec.yaml`

Limitations / Next steps:
- Expression parser is intentionally small; for complex conditions integrate lark or similar.
- No signal table / ports vs signals reconciliation yet (you can add ports to YAML later
  and the validator will check for referenced signals if present).
- No auto-generation of timer/counter modules yet — the IR has enough info for a generator
  to build a counter from the "timer" signal requirement.

Usage example:
    from ir_validator import validate_spec
    design = validate_spec('design_spec_fsm.yaml')
    print(design)

"""
from dataclasses import dataclass, field
from typing import List, Optional, Union, Dict, Any
import re
import yaml
import math
import sys

# -----------------
# Exceptions
# -----------------
class ValidationError(Exception):
    pass

# -----------------
# IR dataclasses
# -----------------
@dataclass
class Port:
    name: str
    direction: str = "input"  # 'input'|'output'|'inout'
    width: int = 1
    description: Optional[str] = None

@dataclass
class Clock:
    name: str = "clk"
    frequency_mhz: Optional[float] = None

@dataclass
class Reset:
    name: str = "rst_n"
    active_low: bool = True
    synchronous: bool = True

# Expression AST
class Expr:
    pass

@dataclass
class SignalRef(Expr):
    name: str

@dataclass
class Const(Expr):
    value: int

@dataclass
class Compare(Expr):
    left: Expr
    op: str
    right: Expr

@dataclass
class BoolOp(Expr):
    op: str
    conditions: List[Expr]

# Transition and FSM
@dataclass
class Transition:
    src: str
    dst: str
    condition: Expr
    raw_condition: str

@dataclass
class FSM:
    states: List[str]
    transitions: List[Transition]
    reset_state: str

@dataclass
class Design:
    design_name: str
    design_type: str
    clock: Optional[Clock] = None
    reset: Optional[Reset] = None
    ports: List[Port] = field(default_factory=list)
    fsm: Optional[FSM] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

# -----------------
# Simple condition parser
# -----------------
# Accept patterns like: SIGNAL == 30, timer>=5, or simple boolean chains
_COND_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|<=|>=|<|>)\s*([0-9]+)\s*$")


def _parse_single_condition(cond_str: str) -> Compare:
    m = _COND_RE.match(cond_str)
    if not m:
        raise ValidationError(f"Unsupported/invalid condition format: '{cond_str}'")

    sig, op, num = m.groups()
    left = SignalRef(sig)
    right = Const(int(num))
    return Compare(left=left, op=op, right=right)


def parse_condition(cond_str: str) -> Expr:
    """Parse a very small subset of expressions into Expr AST.
    Supports:
    - constant true: 1
    - <signal> <op> <integer> with op in ==,!=,<,<=,>,>=
    - simple chains joined by && or ||
    """
    if not isinstance(cond_str, str):
        raise ValidationError(f"Condition must be a string, got: {type(cond_str)}")

    if cond_str.strip() == "1":
        return Const(1)

    if "&&" in cond_str and "||" in cond_str:
        raise ValidationError(f"Mixed boolean operators are not supported: '{cond_str}'")

    if "&&" in cond_str:
        return BoolOp(op="&&", conditions=[_parse_single_condition(part.strip()) for part in cond_str.split("&&")])

    if "||" in cond_str:
        return BoolOp(op="||", conditions=[_parse_single_condition(part.strip()) for part in cond_str.split("||")])

    return _parse_single_condition(cond_str)


def _iter_compares(expr: Expr):
    if isinstance(expr, Compare):
        yield expr
    elif isinstance(expr, BoolOp):
        for condition in expr.conditions:
            yield from _iter_compares(condition)

# -----------------
# Helpers
# -----------------

def sanitize_name(name: str) -> str:
    # keep only alnum and underscore, collapse others to underscore
    return re.sub(r'[^A-Za-z0-9_]', '_', name)


def infer_min_width_for_const(val: int) -> int:
    if val < 0:
        # treat negative as signed; take abs for width estimate
        val = abs(val)
    if val == 0:
        return 1
    return math.ceil(math.log2(val + 1))

# -----------------
# Main validator
# -----------------

def _load_yaml(path: str) -> dict:
    with open(path, 'r') as f:
        data = yaml.safe_load(f)
    if data is None:
        raise ValidationError("YAML file is empty")
    if not isinstance(data, dict):
        raise ValidationError("Top-level YAML must be a mapping/object")
    return data



def validate_spec(path: str) -> Design:
    """Load YAML, validate minimal schema for FSM designs, and return a Design IR object.

    Raises ValidationError on problems.
    """
    raw = _load_yaml(path)

    # Top-level required fields
    if 'design_name' not in raw:
        raise ValidationError("Missing required field: design_name")
    design_name = str(raw['design_name'])

    design_type = raw.get('design_type', 'fsm')

    # Clock
    clock_raw = raw.get('clock', {}) or {}
    clock = Clock(
        name=clock_raw.get('name', 'clk'),
        frequency_mhz=clock_raw.get('target_frequency_mhz')
    )

    # Reset: optional
    reset_raw = raw.get('reset', {}) or {}
    reset = Reset(
        name=reset_raw.get('name', 'rst_n'),
        active_low=reset_raw.get('active_low', True) if 'active_low' in reset_raw else reset_raw.get('type', 'active_low') == 'active_low',
        synchronous=reset_raw.get('synchronous', True)
    )

    # Ports (optional)
    ports_list = []
    for p in raw.get('ports', []) or []:
        if not isinstance(p, dict):
            raise ValidationError("Each port must be a mapping/object")
        ports_list.append(Port(
            name=str(p.get('name')),
            direction=str(p.get('direction', 'input')),
            width=int(p.get('width', 1)),
            description=p.get('description')
        ))

    design = Design(
        design_name=sanitize_name(design_name),
        design_type=design_type,
        clock=clock,
        reset=reset,
        ports=ports_list,
        metadata={k: v for k, v in raw.items() if k not in ['design_name', 'clock', 'reset', 'ports', 'state_machine', 'functionality', 'design_type']}
    )

    # Section-specific: FSM
    if design_type == 'fsm' or 'state_machine' in raw:
        sm = raw.get('state_machine')
        if not sm or not isinstance(sm, dict):
            raise ValidationError("state_machine section must be a mapping/object")

        states = sm.get('states')
        if not states or not isinstance(states, list) or not all(isinstance(s, str) for s in states):
            raise ValidationError("state_machine.states must be a non-empty list of strings")
        # normalize states
        norm_states = [s.strip() for s in states]
        if len(set(norm_states)) != len(norm_states):
            raise ValidationError("Duplicate state names in state_machine.states")

        # transitions
        trans_raw = sm.get('transitions', []) or []
        if not isinstance(trans_raw, list):
            raise ValidationError("state_machine.transitions must be a list")

        transitions = []
        seen_transitions = set()
        for t in trans_raw:
            if not isinstance(t, dict):
                raise ValidationError("Each transition must be a mapping/object")
            src = t.get('from')
            dst = t.get('to')
            cond = t.get('condition')
            if src is None or dst is None or cond is None:
                raise ValidationError("Each transition must have 'from', 'to', and 'condition' fields")
            if not isinstance(src, str) or not isinstance(dst, str):
                raise ValidationError("transition 'from' and 'to' must be strings")
            src = src.strip(); dst = dst.strip()
            if src not in norm_states:
                raise ValidationError(f"Transition references unknown state: {src}")
            if dst not in norm_states:
                raise ValidationError(f"Transition references unknown state: {dst}")

            # parse condition
            try:
                cond_ast = parse_condition(cond)
            except ValidationError as e:
                raise ValidationError(f"In transition {src} -> {dst}: {e}")

            raw_cond_text = cond.strip() if isinstance(cond, str) else str(cond)
            trans_key = (src, dst, raw_cond_text)
            if trans_key in seen_transitions:
                raise ValidationError(f"Duplicate transition detected: {src} -> {dst} [{raw_cond_text}]")
            seen_transitions.add(trans_key)

            transitions.append(Transition(src=src, dst=dst, condition=cond_ast, raw_condition=raw_cond_text))

        # pick reset state: allow explicit or default to first state
        rst_state = sm.get('reset_state') or norm_states[0]
        if rst_state not in norm_states:
            raise ValidationError(f"reset_state '{rst_state}' is not one of the states")

        # Small semantic checks: ensure at least one outgoing transition per state (basic)
        outgoing = {s: 0 for s in norm_states}
        for tr in transitions:
            outgoing[tr.src] += 1
        missing = [s for s, cnt in outgoing.items() if cnt == 0]
        if missing:
            # do not fail hard; warn via metadata
            design.metadata['warnings'] = design.metadata.get('warnings', []) + [f"State(s) with no outgoing transitions: {missing}"]

        # infer simple widths for any Compare(Const), including inside simple BoolOp chains
        inferred_signal_widths: Dict[str, int] = {}
        for tr in transitions:
            for compare in _iter_compares(tr.condition):
                left = compare.left
                right = compare.right
                if isinstance(left, SignalRef) and isinstance(right, Const):
                    width = infer_min_width_for_const(right.value)
                    prev = inferred_signal_widths.get(left.name)
                    if prev is None or width > prev:
                        inferred_signal_widths[left.name] = width

        # attach widths as metadata for generator
        design.metadata['inferred_signal_widths'] = inferred_signal_widths

        design.fsm = FSM(states=norm_states, transitions=transitions, reset_state=rst_state)

    # other design_types can be added later

    return design

# -----------------
# CLI for quick testing
# -----------------
if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python ir_validator.py path/to/spec.yaml")
        sys.exit(1)
    path = sys.argv[1]
    try:
        d = validate_spec(path)
        print("Validation successful. Design IR summary:")
        print(f"  name: {d.design_name}")
        print(f"  type: {d.design_type}")
        if d.clock:
            print(f"  clock: {d.clock.name} @ {d.clock.frequency_mhz} MHz")
        if d.fsm:
            print(f"  fsm states: {d.fsm.states}")
            print(f"  reset state: {d.fsm.reset_state}")
            print(f"  transitions:")
            for tr in d.fsm.transitions:
                if isinstance(tr.condition, Compare):
                    left = tr.condition.left.name if isinstance(tr.condition.left, SignalRef) else str(tr.condition.left)
                    right = tr.condition.right.value if isinstance(tr.condition.right, Const) else tr.condition.right
                    print(f"    - {tr.src} -> {tr.dst} when {left} {tr.condition.op} {right}")
                elif isinstance(tr.condition, BoolOp):
                    print(f"    - {tr.src} -> {tr.dst} when {tr.raw_condition}")
        if d.metadata:
            print("  metadata:")
            for k, v in d.metadata.items():
                print(f"    {k}: {v}")

    except ValidationError as e:
        print("Validation error:", e)
        sys.exit(2)
