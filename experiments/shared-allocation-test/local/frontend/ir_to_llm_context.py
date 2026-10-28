# ir_to_llm_context.py

import json
import re


def _append_unique(items, value):
    if value and value not in items:
        items.append(value)


def _string_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    text = str(value).strip()
    return [text] if text else []


def _constraint_lists(value):
    required = []
    forbidden = []

    if isinstance(value, dict):
        required = _string_list(value.get("required"))
        forbidden = _string_list(value.get("forbidden"))
    else:
        required = _string_list(value)

    return required, forbidden


REQUEST_PAYLOAD_COMPATIBILITY_RULES = [
    "request payload width is 51 bits: bank[1:0], row[9:0], col[5:0], is_write, wdata[31:0].",
    "Ports may use packed logic vectors for tool compatibility.",
    "If packed vector ports are used, internally unpack into request_t before applying policy logic.",
]

REQUEST_QUEUE_WIDTH_RULES = [
    "Define localparam int REQUEST_WIDTH = 51.",
    "Define localparam int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH).",
    "Never cast a packed request struct using logic'(some_request_struct).",
    "If a packed request struct must be assigned to a vector, use direct assignment when widths match or use REQUEST_WIDTH'(expr).",
    "Do not hardcode queue index width as 2.",
    "Use SEL_WIDTH for sel_idx, first_free_idx, insert_idx, and loop-derived index casts.",
    "Use block-local loop variables: for (int i = 0; i < DEPTH; i++) begin.",
    "Do not use localparams in ANSI port widths. Any symbol used in a port width must be a literal, package-visible typedef/constant, or a parameter declared in the module parameter list.",
]

LOOP_VARIABLE_GUIDANCE = [
    "Do not declare module-scope loop variables for procedural for-loops.",
    "Use block-local loop variables: for (int i = 0; i < N; i++) begin.",
    "Do not reuse the same loop variable across multiple always_comb/always_ff blocks.",
    "Loop variables used only inside procedural blocks are not architectural signals.",
]

SCHEDULER_BLOCKING_RULES = [
    "sel_idx must remain stable while blocked",
    "locked selection clears only after successful_issue",
    "queue entries are expected to persist until deq_en",
    "issue_valid may track req_valid[locked_idx]",
    "scheduler must not unlock or reselect merely because cmd_ready or timing_ok is low",
]

SCHEDULER_REFRESH_PRIORITY_RULES = [
    "issue_ref and issue_txn must be mutually exclusive",
    "when ref_req, issue_valid, cmd_ready, and timing_ok are all true, refresh has priority and issue_txn must be 0",
    "issue_ref == ref_req && cmd_ready && timing_ok",
    "issue_txn == issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref",
    "successful_issue == issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref",
    "successful_issue for the selected transaction must not assert in a refresh-priority cycle",
    "refresh priority cycles must not create a new transaction lock",
    "transaction lock acquisition requires !ref_req",
    "candidate_success == candidate_valid && cmd_ready && timing_ok && !ref_req",
    "candidate_blocked == candidate_valid && !ref_req && !candidate_success",
    "new transaction lock acquisition requires candidate_blocked",
    "do not create a transaction lock when the selected request issues immediately",
    "if already locked and refresh arrives, the lock may remain but no transaction issue occurs until refresh is no longer winning arbitration",
]


def _sanitize_request_payload_constraints(items):
    sanitized = []
    replaced_request_t_port_constraint = False

    for item in items:
        normalized = item.strip().lower()
        if normalized == "use request_t for enq_req and req_array entries":
            replaced_request_t_port_constraint = True
            continue
        sanitized.append(item)

    if replaced_request_t_port_constraint:
        for rule in REQUEST_PAYLOAD_COMPATIBILITY_RULES:
            _append_unique(sanitized, rule)

    return sanitized


def _sanitize_scheduler_blocking_rules(items):
    sanitized = []
    replaced = False

    for item in items:
        normalized = item.strip().lower()
        if normalized == "issue_valid remains asserted for the selected valid entry while blocked":
            replaced = True
            continue
        if normalized == "do not unlock or reselect merely because req_valid changes; lock clears only on successful_issue":
            replaced = True
            continue
        if normalized == "scheduler must not reselect or unlock merely because cmd_ready, timing_ok, or req_valid changes while blocked":
            replaced = True
            continue
        sanitized.append(item)

    if replaced:
        for rule in SCHEDULER_BLOCKING_RULES:
            _append_unique(sanitized, rule)

    return sanitized


def _add_loop_variable_guidance(items):
    for rule in LOOP_VARIABLE_GUIDANCE:
        _append_unique(items, rule)
    return items


def _add_request_queue_width_rules(items):
    for rule in REQUEST_QUEUE_WIDTH_RULES:
        _append_unique(items, rule)
    return items


def build_llm_context(design):
    """
    Convert validated Design IR into structured LLM-ready context.
    This is NOT a raw dump — it is a normalized, semantically enriched view.
    """

    ctx = {}

    # ------------------------------------------------------------------
    # 1. Module definition
    # ------------------------------------------------------------------
    ctx["module"] = {
        "name": design.design_name,
        "type": design.design_type,
        "ports": [
            {
                "name": p.name,
                "direction": p.direction,
                "width": p.width
            }
            for p in sorted(design.ports, key=lambda x: x.name)
        ]
    }

    # ------------------------------------------------------------------
    # 2. Clock / Reset
    # ------------------------------------------------------------------
    if design.clock:
        ctx["clock"] = {
            "name": design.clock.name,
            "frequency_mhz": design.clock.frequency_mhz
        }

    if design.reset:
        ctx["reset"] = {
            "name": design.reset.name,
            "active_low": design.reset.active_low,
            "synchronous": design.reset.synchronous
        }

    # ------------------------------------------------------------------
    # 3. FSM structure (CORE)
    # ------------------------------------------------------------------
    if design.fsm:
        ctx["fsm"] = {
            "states": sorted(design.fsm.states),
            "reset_state": design.fsm.reset_state,
            "transitions": sorted(
                [
                    {
                        "from": t.src,
                        "to": t.dst,
                        "condition": t.raw_condition
                    }
                    for t in design.fsm.transitions
                ],
                key=lambda x: (x["from"], x["to"], x["condition"])
            )
        }

    # ------------------------------------------------------------------
    # 4. Signals (from inferred widths)
    # ------------------------------------------------------------------
    inferred = design.metadata.get("inferred_signal_widths", {})

    ctx["signals"] = {
        name: {
            "width": width,
            "type": "logic"
        }
        for name, width in sorted(inferred.items())
    }

    supplemental_metadata = {
        name: design.metadata[name]
        for name in sorted(design.metadata)
        if name not in {
            "inferred_signal_widths",
            "warnings",
            "implementation_constraints",
            "forbidden_patterns",
            "invariants",
        }
    }
    if supplemental_metadata:
        ctx["metadata"] = supplemental_metadata

    behavior = {}
    if isinstance(design.metadata.get("behavior"), dict):
        behavior = design.metadata["behavior"]

    request_struct = design.metadata.get("request_struct")
    if request_struct is None and isinstance(behavior, dict):
        request_struct = behavior.get("request_struct")
    if isinstance(request_struct, dict):
        ctx["request_struct"] = request_struct

    implementation_constraints, constraint_forbidden = _constraint_lists(
        design.metadata.get("implementation_constraints")
    )
    implementation_constraints = _sanitize_request_payload_constraints(
        implementation_constraints
    )
    implementation_constraints = _sanitize_scheduler_blocking_rules(
        implementation_constraints
    )
    implementation_constraints = _add_loop_variable_guidance(
        implementation_constraints
    )
    if design.design_name == "ddr4_request_queue":
        implementation_constraints = _add_request_queue_width_rules(
            implementation_constraints
        )
    forbidden_patterns = _string_list(
        design.metadata.get("forbidden_patterns")
    )
    for pattern in constraint_forbidden:
        _append_unique(forbidden_patterns, pattern)
    declared_invariants = _string_list(
        design.metadata.get("invariants")
    )
    declared_invariants = _sanitize_scheduler_blocking_rules(
        declared_invariants
    )

    # ------------------------------------------------------------------
    # 5. Semantic enrichment
    # ------------------------------------------------------------------

    semantics = {}
    counter_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|<=|>=|<|>)")
    counters = set()
    counter_state_rules = set()

    if design.fsm:
        semantics["fsm_behavior"] = [
            "state transitions occur on clock edges only",
            "if no transition condition is true, state remains unchanged",
            "transitions are evaluated every cycle in a deterministic order",
        ]

        for t in design.fsm.transitions:
            cond = t.raw_condition or ""
            match = counter_re.match(cond)
            if match and "_counter" in match.group(1):
                counter_name = match.group(1)
                counters.add(counter_name)
                counter_state_rules.add(
                    "{} is only valid in state {}".format(counter_name, t.src)
                )

    if counters:
        semantics["counters"] = [
            {
                "name": c,
                "rules": [
                    "{} increments by exactly 1 on each clock cycle when active".format(c),
                    "{} does not increment when inactive".format(c),
                    "{} resets to 0 when leaving its counting phase".format(c),
                    "{} must not skip or jump values".format(c),
                    "{} must be implemented as sequential logic driven by the clock".format(c),
                ]
            }
            for c in sorted(counters)
        ]

    if counter_state_rules:
        semantics["counter_state_binding"] = sorted(counter_state_rules)

    semantics["sequential_rules"] = [
        "all state-holding behavior must be implemented in sequential logic",
        "state, counters, and other stored values update only on clock edges",
        "combinational logic may compute next values but must not store state",
        "reset behavior must deterministically initialize every sequential register",
    ]

    ctx["semantics"] = semantics

    # ------------------------------------------------------------------
    # 6. Invariants / spec lock
    # ------------------------------------------------------------------

    invariants = []

    def add_invariant(text):
        if text not in invariants:
            invariants.append(text)

    add_invariant("no signals may be used unless declared in ports or inferred signals")
    add_invariant("no additional states or signals may be introduced")
    add_invariant("all signals must have a single driver")
    add_invariant("all sequential registers must have explicit reset behavior")
    add_invariant("signals representing stored values must not be driven by combinational logic")
    add_invariant("all state-holding variables must be updated in clocked always_ff blocks")
    add_invariant("combinational blocks must not store state")
    add_invariant("outputs must be fully defined for all possible input combinations")
    add_invariant("outputs must not depend on undefined or uninitialized signals")

    if counters:
        add_invariant("counters must reset to a defined value on reset")
        add_invariant("if a transition depends on a counter reaching a value, the counter must reach that value through sequential increments")

    if design.fsm:
        add_invariant("current_state must always be one of the defined states")
        add_invariant("next_state defaults to current_state unless a transition condition is true")
        add_invariant("only one state is active at a time")
        add_invariant("all transitions must originate from valid states")
        add_invariant("all state transitions occur only on clock edges")
        add_invariant("state updates must use sequential logic (clocked always_ff)")
        add_invariant("for any given state, transition conditions must be mutually exclusive and deterministic")
        add_invariant("reset must initialize the system into the defined reset_state")
        add_invariant("outputs must be fully defined for all possible states")
        add_invariant("outputs derived from state must match state conditions exactly")
        add_invariant("if no transition condition is met, the state must remain unchanged")

    if design.reset:
        add_invariant(
            "reset signal '{}' is active_{} and {}".format(
                design.reset.name,
                "low" if design.reset.active_low else "high",
                "synchronous" if design.reset.synchronous else "asynchronous"
            )
        )

    for invariant in declared_invariants:
        add_invariant(invariant)

    for criterion in _string_list(behavior.get("correctness_criteria")):
        add_invariant(criterion)

    if isinstance(request_struct, dict):
        fields = request_struct.get("fields")
        if isinstance(fields, list):
            field_text = []
            for field in fields:
                if not isinstance(field, dict):
                    continue
                name = field.get("name")
                width = field.get("width")
                if name is not None and width is not None:
                    field_text.append("{}[{}]".format(name, width))
            if field_text:
                add_invariant("request_t fields == {}".format(", ".join(field_text)))
                add_invariant(REQUEST_PAYLOAD_COMPATIBILITY_RULES[0])
                add_invariant(REQUEST_PAYLOAD_COMPATIBILITY_RULES[1])
                add_invariant(REQUEST_PAYLOAD_COMPATIBILITY_RULES[2])
                add_invariant("request_t must not include a valid field; req_valid is the only validity source")
                add_invariant("queue consumers must ignore request payload contents when req_valid for that entry is 0")
                ctx["request_payload"] = {
                    "width_bits": 51,
                    "required_localparams": [
                        "localparam int REQUEST_WIDTH = 51",
                        "localparam int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH)",
                    ],
                    "fields": field_text,
                    "port_policy": [
                        "packed vector ports are acceptable",
                        "internal typedef request_t is encouraged",
                        "unpack packed vector ports into request_t before policy logic",
                        "pack request_t entries back into packed vector outputs when needed",
                    ],
                    "casting_policy": [
                        "never use logic'(some_request_struct)",
                        "use direct assignment when packed struct/vector widths match",
                        "use REQUEST_WIDTH'(expr) when an explicit packed request-width cast is needed",
                    ],
                }

    if behavior.get("blocking_policy"):
        add_invariant("blocking_policy == {}".format(behavior["blocking_policy"]))
        add_invariant("scheduler selected index must remain locked until successful_issue")
        for rule in SCHEDULER_BLOCKING_RULES:
            add_invariant(rule)

    if behavior.get("policy") == "row_hit_then_first_valid_lowest_index":
        ctx["scheduler_policy"] = {
            "row_hit_condition": "row_hit[i] = req_valid[i] && bank_active[req_array[i].bank] && (bank_open_row[req_array[i].bank] == req_array[i].row)",
            "priority_rule": [
                "scan req_array entries in ascending index order",
                "select the lowest-index row-hit request if any row-hit request exists",
                "otherwise select the lowest-index valid request",
            ],
            "required_inputs": [
                "req_array[i].bank",
                "req_array[i].row",
                "req_valid[i]",
                "bank_active[req_array[i].bank]",
                "bank_open_row[req_array[i].bank]",
            ],
            "locking_policy": "once selected, hold sel_idx stable until successful_issue; issue_valid may track req_valid[locked_idx]",
            "refresh_priority": [
                "issue_ref and issue_txn are mutually exclusive",
                "refresh wins when ref_req and a transaction are both issuable",
                "a refresh-priority cycle is not a successful transaction issue",
                "a refresh-priority cycle must not acquire a new transaction lock",
                "new transaction lock acquisition requires !ref_req",
                "new transaction lock acquisition requires a blocked candidate, not an immediately issued candidate",
            ],
            "timing_responsibility": "scheduler does not enforce DDR timing; timing_ok is provided externally",
        }
        add_invariant("scheduler policy == row_hit_then_first_valid_lowest_index")
        add_invariant("row_hit[i] == req_valid[i] && bank_active[req_array[i].bank] && (bank_open_row[req_array[i].bank] == req_array[i].row)")
        add_invariant("scheduler must compute row_hit for queue entries before selecting a new unlocked request")
        add_invariant("scheduler selects the lowest-index row-hit request when any row-hit request exists")
        add_invariant("scheduler falls back to the lowest-index valid request when no row-hit request exists")
        add_invariant("scheduler must not modify req_array, req_valid, bank_active, or bank_open_row")
        for rule in SCHEDULER_BLOCKING_RULES:
            add_invariant(rule)
        for rule in SCHEDULER_REFRESH_PRIORITY_RULES:
            add_invariant(rule)

    for rule in _string_list(behavior.get("rules")):
        if "enqueue" in rule or "dequeue" in rule or "same" in rule:
            add_invariant(rule)
    if design.design_name == "ddr4_request_queue":
        for rule in REQUEST_QUEUE_WIDTH_RULES:
            add_invariant(rule)
        add_invariant("same-cycle queue enqueue/dequeue must use explicit reuse_slot detection")
        add_invariant("reuse_slot == deq_en && enq_valid && (insert_idx == sel_idx)")
        add_invariant("dequeue clear must be guarded as deq_en && !reuse_slot")
        add_invariant("when reuse_slot is true, the selected slot remains valid and stores enq_req")

    if "window_size" in behavior:
        add_invariant("window_size == {}".format(behavior["window_size"]))
        if "act_count" in behavior.get("internal_signals", {}):
            add_invariant("act_count <= {}".format(behavior["window_size"]))

    if "timestamp_count" in behavior:
        add_invariant("timestamp_count == {}".format(behavior["timestamp_count"]))

    if "tFAW_cycles" in behavior:
        add_invariant("tFAW_cycles == {}".format(behavior["tFAW_cycles"]))

    if "count_source" in behavior:
        add_invariant("count_source == {}".format(behavior["count_source"]))

    if "updated_window_expression" in behavior:
        add_invariant(
            "updated_window_expression == {}".format(
                behavior["updated_window_expression"]
            )
        )

    internal_signals = behavior.get("internal_signals", {})
    if isinstance(internal_signals, dict):
        for signal_name in sorted(internal_signals):
            signal_spec = internal_signals[signal_name]
            if not isinstance(signal_spec, dict):
                continue
            if "width" in signal_spec:
                add_invariant(
                    "{} width == {}".format(signal_name, signal_spec["width"])
                )
            if "count" in signal_spec:
                add_invariant(
                    "{} count == {}".format(signal_name, signal_spec["count"])
                )

    ctx["invariants"] = invariants
    ctx["declared_invariants"] = declared_invariants
    ctx["implementation_constraints"] = implementation_constraints
    ctx["forbidden_patterns"] = forbidden_patterns
    ctx["validator_alignment"] = {
        "source_of_truth": "STRUCTURED_LLM_CONTEXT only",
        "must_not_assume": [
            "decrement logic unless explicitly specified in structured context",
            "background aging mechanisms unless explicitly specified in structured context",
            "wraparound handling beyond what structured context explicitly states",
            "alternate architectures not present in structured context",
        ]
    }
    ctx["review_protocol"] = {
        "status_values": ["VALID", "INVALID", "AMBIGUOUS"],
        "issue_types": ["SPEC_VIOLATION", "NON_ISSUE", "AMBIGUITY"],
        "merge_policy": "apply only SPEC_VIOLATION fixes grounded in structured context"
    }

    # ------------------------------------------------------------------
    # 7. Warnings (propagate validator insights)
    # ------------------------------------------------------------------
    if "warnings" in design.metadata:
        ctx["warnings"] = design.metadata["warnings"]

    return ctx


def llm_context_to_string(ctx):
    """
    Convert context dict to a stable JSON string for prompt injection.
    """
    return json.dumps(ctx, indent=2, sort_keys=True)
