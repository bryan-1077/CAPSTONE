# Specification generation contract

The request is authoritative. The example demonstrates schema and reference
syntax; its functionality, decisions, module names and decomposition are not
implicit constraints. Do not invent unrequested features or label unsupported
behavior as a simplification. No RTL, user approval, or executed-test claim may
appear in either stage.

## Stage 1: system elaboration

Return `status: supported`, `package`, `shared`, `system`, and `execution`.
The compiler supplies package `documents`, optional request `design_kind`, and
generation provenance. Do not return those fields, or a `review` field.

Package fields: `specification_version: 1`, identifier `package_id`, identifier
`design_name`, revision string, `status: draft_for_review`, `scope`,
`verification_strategy`, `decisions`, `assumptions`, and `open_questions`.
`scope` has `plan`, `milestone`, `covered` and `pending`; covered/pending are
lists of names. Use pending `[]` for a complete supported request; questions
may explicitly block design agreement. Scope.plan can name the request rather
than a filesystem document. A missing capability requires an unsupported report.

Decisions map IDs to `{origin,status,value,reason}`. Distinguish origins
`requested_constraint` and `model_selected`; status `recorded` means documented,
not approved. Never fabricate `accepted` decisions. Assumptions map IDs to
`{statement,status}`; use `documented` or `requires_review`. Questions map IDs to
`{blocks,question}`; a design question uses `blocks: design_review`. Explicit
exact-export approval is enforced separately by the compiler.
Do not add a generic design_review question that merely restates the approval
gate. If the only remaining action is approval of the exact export, use
`blocks: rtl_generation`; reserve design_review for actual missing agreement.

Verification strategy has `status: planned_not_executed`, `configurations`,
`module_checks`, `system_scenario`, and `system_evidence`. These describe future
downstream checks, not frontend UVM execution. Prefer the generic matrix shape:

```json
{"parameters":{"DEPTH":[1,4]},"profiles":["DDR4-3200"]}
```

An empty parameters mapping selects only profiles. Matrix axes must name
configurable parameters; choose values supported by the parameter declarations.
The example's legacy bank/capacity/page-policy matrix is also accepted.

Shared fields: `parameters`, `clock_reset`, `types`, `host_interface`, `events`,
`timing_profile`, `timing_rules`. Use the exact example's schema. Parameters
specify either `value` or `derive`, optional `configurable`, `allowed`, `minimum`,
`unit`, `note`. Derivations support names, integers, `ceil_log2(NAME)`, bounded
`2**NAME`; presence conditions support `NAME > integer`. Additional expression
operators require compiler extensions; do not hide calculations in strings.

Supported types: unsigned(width), enum(width, encodings, reserved), record(fields,
packed_order=msb_to_lsb), array(element_type, count,
packed_order=ascending_index_from_lsb). Width/count values are integers or
`shared.parameters.NAME`. Ports map names to `{direction,type}` with optional
`when`. Clock/reset are included in the host interface and implicit child inputs.
The current lowering supports one rising-edge clock, active-low synchronous
reset, reset before all events, and immediate reset gating. Use clock_reset
fields below. Use the canonical assertion_style `synchronous`; the older
`synchronous_for_all_stateful_modules_in_this_example` spelling remains a
compatible alias. The accepted values are:

```json
{"clock":"clk","edge":"rising","reset":"rst_n","active_level":0,"priority":"reset_before_all_events","assertion_style":"synchronous","handshake_gating":"Suppress all valid/ready/commit events while reset is asserted."}
```

Clock/reset signal names may follow the request. A baseline permitting reuse
of synchronous or asynchronous blocks does not require selecting both styles,
or selecting reusable blocks at all. Choosing all-synchronous custom modules
honors an explicit synchronous design request and that conditional allowance.
Do not report mixed reset support as required unless the request actually
requires a mixed design. A schema spelling error should be repaired to a
supported value when that value satisfies the request.

Events map names to `{condition,effect}` with precise pre-edge sampling prose.
Timing profiles use `{source,selection,supported_profiles,unit,bindings,note}`;
source is compiler-rewritten to local `timing.yaml`; unit is cycles. Each binding
value MUST be a string containing a dotted path relative to the SELECTED profile
in the supplied timing library. Keep that path in the response; the compiler
resolves its numeric cycle value separately for each selected profile. Numeric
cycle literals are invalid binding values, even when they equal today's library
value. For the supplied example:

```json
{"bindings":{"tRCD":"timing_cycles.tRCD","tRFC":"timing_cycles.tRFC1"}}
```

Use paths that actually exist in the supplied library; never invent profile
values or paths. Do not prefix binding paths with `shared` or the profile name.
References FROM timing rules use the shared vocabulary instead, for example
`"minimum":"shared.timing_profile.bindings.tRCD"`. These are different reference
locations: library paths define bindings; shared paths consume those bindings.
An empty timing binding map is allowed for designs with no timing requirements.
Timing rules use the example's opcode type and minimum/window vocabulary, or
an empty mapping when no such timing constraints are requested.

Both system and execution have `{slice_id,rules,verification}`; execution may
have empty rules/verification when appropriate. In this stage each rule is
`{verification}` for requested IDs (compiler sets exact text and `request.ID`
origins); `{origin,statement,verification}` for refinements. No owners.
Refinements cannot weaken requests. Rule IDs must
be globally unique HDL identifiers. Verification IDs are local to each slice.
Every rule lists nonempty local verification IDs. Generated obligations have
`{scenario,expected,status}`; OMIT `rules`, derived by the compiler from rule
references. Explicit `rules` lists remain strictly validated. Published source
packages require both directions. Use independent expected results and
`status: planned_not_executed`. Define reset, stalls, simultaneous events,
capacities, order and exact response/timing edges. Avoid implementation-private
names or copying implementation algorithms into expected-result models.

For stateful proposals, distinguish first creation, blocked hold, commit and later
replacement, with edge timing and simultaneous-event priorities. Define payload
values when each valid signal is low, separately from reset. Preserve any requested
latency choices; otherwise record the choice without fixing an FSM encoding.

## Stage 2: decomposition and bindings

Return `status: supported`, `modules`, `graph`, `verification`, and `rule_owners`.
The complete first-stage result is locked. Do not return edited package/shared
definitions/rules/expected results. `rule_owners` maps EVERY `system.rules.ID`
and `execution.rules.ID` to a nonempty list of module contract IDs. Assign each
rule's behavior to those owners. OMIT generated contract `invariants` and
`verification`: the compiler derives each owner's qualified rule/obligation
references from `rule_owners` and locked rules. Explicit lists remain strictly
validated, never corrected. This derives bookkeeping, not behavioral compliance.
Top-level bindings, checks, scenarios and expected results remain required.
`{"status":"supported","modules":{"contracts":{}},"internal_types":{},"graph":{},"verification":{},"rule_owners":{}}`.
`modules` contains only `contracts`; close both objects before the next root field.
Optional `internal_types`, `graph`, `verification` and `rule_owners` are root fields,
never children of `modules` or `contracts`.

Optional `internal_types` may add shared type definitions needed by internal
interfaces. It cannot replace existing types or change system semantics. The
compiler merges these additions into the single shared vocabulary and records
their identity separately from the locked first-stage projection.

Modules have `contracts`, mapping instance names to contracts. Required contract
fields in generated responses are `status`, `role`, `owned_state`, `interfaces`,
`handshakes`, `reset`, `update_events`, `stalls`, `simultaneous_events`, and
`combinational_dependencies`; optional `dependency_note`. Published source requires
both reference lists; hand-authored packages must supply them explicitly.
`owned_state`, `handshakes`, `update_events`, `simultaneous_events`: objects
`{name:description}`, NEVER strings or lists (empty: `{}`). `interfaces` and
`combinational_dependencies`: objects; `invariants`, `verification`: lists.
Use detailed behavioral
prose with explicit event-driven updates, independently testable interfaces,
owned state, absent-port semantics, valid/ready behavior and invalid outputs.
Express lifecycle/priority decisions in the existing update_events, stalls and
simultaneous_events fields. Initial creation must not require a previous commit.
Any valid-dependent payload masking must appear in its input dependencies; a
payload held independently of valid must say so. Reset gating remains implicit.
Update event keys are `shared.events.NAME` or `every_clock`. Invariants reference
qualified rules; verification lists qualified obligations. Dependencies map EVERY
output to current-cycle input names; [] means state-only/constant. Dependency
entries may name only declared INPUT ports, never another output or an internal
signal. Flatten transitive combinational paths to their input-port dependencies;
terms that depend only on registered state add no current-cycle input dependency.
Shared reset
gating is implicit. Declare transitive dependencies; never hide feedback.

Supported role vocabulary: request_storage, request_selection, command_sequencing,
timing_enforcement, functional_memory_and_responses, host_protocol, bank_state,
refresh_control (or their existing architecture role equivalents). Memory
controllers require distinct functional owners for request_queue, scheduler,
command_engine, timing and memory. Further splitting is allowed; do not create
trivial helpers while hiding all behavior in one core. Generic designs need not
use every controller role. Unsupported roles require an explicit extension.

Graph has `graph_id`, `clock_reset`, `connections`, `presence`, `integration`;
optional `verification_outputs` maps child output endpoints to nonempty reasons.
Clock_reset has definition shared.clock_reset, top clock/reset source endpoints,
and every contract in consumers. Connections map source endpoints to sink lists.
Use typed whole-port wiring only; one driver per sink, matching types and widths,
sources wired or explicitly verification-only, conditional ends omitted together.
Clock/reset wiring shape:
`{"definition":"shared.clock_reset","clock_source":"top.clk","reset_source":"top.rst_n","consumers":["instance"]}`.
Use declared names; no nested `source` object.
No clock/reset duplication
in the ordinary connection map. Presence maps EVERY conditional endpoint to its
declared condition. Verification-only outputs need a present child output, no
functional sink, and an observation used by a validated check. Inputs/top ports
cannot qualify. Never add unrelated consumers to silence unused-source errors.
Preserve unrelated valid wiring. Top wiring is structural; glue needs a contract.

Verification bindings have binding_version 1, sampling
`{phase:pre_edge,reset_gates_events:true}`, events, observations and checks.
Each shared event is either `{status:bound,when:predicate,payload:{alias:ref}}`
or `{status:unbound,reason:precise limitation and independent observation plan}`.
Refs are `{endpoint:top.port}` or `{endpoint:instance.port,field:packed.field}`.
Predicates support nested nonempty all/any lists and endpoint/optional field
`equals` (integer or enum symbol). Observations map aliases to refs. Checks map
EVERY qualified obligation to `{scope,events,observations,note}`, with scope
module/system/both and at least one declared event or observation. Never guess
private registers. Bindings must work throughout the declared configuration matrix.

## Unsupported requests

Either stage may return only:

```json
{"status":"unsupported","unsupported":[{"requirement_ids":["R1"],"reason":"Specific missing capability","required_extension":"Concrete compiler/schema change needed"}]}
```

Identify affected original request IDs. No partial RTL-capable package is exported.
