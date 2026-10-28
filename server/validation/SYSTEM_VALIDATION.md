Full-system controller validation (Phases 2, 3 and 4)
================================================

For the Experimental specification route, see the proposed
[verification integration agreement](../../shared/top_orchestrator/EXPERIMENTAL_VERIFICATION.md).
The Standard controller system interface described below does not establish
Experimental system compatibility.

Run from the repository root, with the HDL tool environment loaded:

```sh
python3 server/validation/run_pipeline.py local/frontend/rtl_output --system
```

A frontend run directory or its `manifest.json` also works. A controller
manifest selects system mode automatically. Discovery must pass first. The
pipeline requests **Agent 1**, then **Agent 2** only if structural/elaboration
validation passes and all frontend input fingerprints still match. Agent 2
compiles every manifest RTL file with the controller UVM environment. No
individual module UVM runs precede it. A sufficiently successful Agent 2 hands
its exact saved checker to **Agent 3** for directed/random/timing stress. The
pipeline stops after Agent 3 and never launches backend/LEC/GDSII work.
A structural PASS alone produces
`READY_FOR_SYSTEM_AGENT_2_AND_AGENT_3`; it does not prove controller behavior.
Functional readiness requires independent accounting plus passing core
requirements, and produces `READY_FOR_SYSTEM_AGENT_3`.
Only all three passing gates and mandatory Agent 3 evidence can produce
`READY_FOR_BACKEND`. Optional limitations remain `PASS_WITH_GAPS` and keep
`verification_complete` false. Readiness never claims physical timing closure.
See [SYSTEM_AGENT3.md](SYSTEM_AGENT3.md) for checker authentication, timing scopes,
stress groups, failure feedback and Phase 4 limitations.

Reports are saved under the manifest directory's
`verification_reports/<run>/`: `summary.json`, `agent1/report.json`,
`agent1/validation_result.json`, `agent1/console.log`, `agent1/compile.log`,
and compiler work files. Agent 2 adds `agent2/system_report.json`,
`controller_verification_contract.json`, `requirement_catalog.json`,
`requirement_traceability.json`, `monitor_events.json`, `failure_evidence.json`,
`compile.log` when VCS was invoked, `simulation.log`, preserved `tb_auto/` sources, and
`uvm_environment.json`. A `controller.vcd` is retained when simulation produces
it. `verification_reports/latest.json` points to the
latest run. Existing RTL and testbench sources are not changed by validation.

Other entry points:

```sh
# Phase 1 inspection/portable snapshots, without agents:
python3 server/validation/run_pipeline.py local/frontend/rtl_output --discovery-only

# Phase 2 only, preserving structural validation behavior:
python3 server/validation/run_pipeline.py local/frontend/rtl_output --system --stop-after-agent1

# Stop after Agent 2 for checker review or reuse debugging:
python3 server/validation/run_pipeline.py local/frontend/rtl_output --system --stop-after-agent2

# Prefer a compatible saved checker; incompatible/absent checkers use the LLM:
python3 server/validation/run_pipeline.py local/frontend/rtl_output --system --reuse-tb

# Direct Agent 1 controller validation:
python3 server/validation/agent1_rtl_validator.py local/frontend/rtl_output --system --out /tmp/controller_result.json

# Existing targeted module diagnostic flow:
python3 server/validation/run_pipeline.py local/frontend/rtl_output --module ddr4_request_queue

# Regression tests (compiler responses are mocked; these do not prove HDL correctness):
python3 -B -m unittest discover -s server/validation -p 'test_system*.py' -v
```

How the gate works
------------------

The manifest supplies the RTL files; file stems never determine module names.
Every supplied file is scanned for actual module declarations, including
multiple declarations per file. Modules retain their source file, parameters,
ports and instances. An iterative graph traversal starts at
`ddr4_controller_top`; its edges come from parsed module instantiations.
Duplicate definitions, recursive hierarchy and missing reachable dependencies
block readiness. Generated, reachable and unused modules are reported separately.
All list sizes are computed from the inputs, with no fixed module/file count.

Unused generated modules are warnings. Enabled feature requirements come from
expanded masters' `source_feature` and their child YAML specifications, which
identify actual modules. Required feature modules outside the reachable graph
cause `SYSTEM_CONFIG_INTEGRATION_FAILED`. Bank count and page policy are checked
against resolvable controller constants when available. The normalized
`manifest.configuration` plus `configuration_sources` is the system contract.
`controller_config` remains a compatibility key for the primary component YAML;
`primary_component_config` clarifies that meaning without breaking v2 consumers.

Every available YAML runs through Agent 1's existing `validate_yaml`. Its
`compare_yaml_rtl(..., structural_only=True)` checks module identity, ports,
directions, known numeric widths and reset contract. Existing module mode keeps
its full comparison, including behavior heuristics. Wrappers without YAML are
recorded as `NO_MODULE_SPEC` and checked through hierarchy/interfaces/configuration
and compilation. No synthetic controller or wrapper YAML is created.

Named/positional connections and common parameter overrides are checked in
instantiated parameter contexts. Statically known port-name, direction, width,
and unconnected/constant clock/reset errors block readiness. Unknown typedefs,
casts, complex expressions, unpacked arrays and derived clocks/resets are listed
as deferred checks; they are never silently assigned a scalar width.

Finally the existing `compile_rtl` compiles **all manifest RTL together once**,
with controller top selected. VCS is preferred; the existing Icarus fallback
remains available. The compiler's work directory is isolated per report. Compile
output is retained, and failures or missing tools block readiness. Verilog gate
primitives and supplied UDP definitions are recognized. Optional manifest
`external_modules` can declare library cells; successful compiler resolution is
still mandatory for those cells.

Limits
------

The scanner covers frontend-generated ANSI modules and common instantiation
syntax, not the whole SystemVerilog language. Reachability is syntactic: it
includes references in conditional generate/preprocessor branches before
elaboration. Non-ANSI interfaces, escaped identifiers, interfaces/modports,
macros, derived clock/reset correctness and complex type/parameter expressions
need HDL elaboration or later verification. Deferred checks appear in the JSON
and terminal count. The compiler retains its existing 60-second timeout, and
Icarus may reject constructs supported by VCS. Functional correctness requires
system simulation. `--stop-after-agent1` deliberately stops before simulation.

Agent 2 transaction contract
----------------------------

The contract is extracted from the approved controller interface/constants,
manifest configuration/timing, component specifications, IR, and inspected
frontend testbench semantics. It is an Agent 2 validation artifact, not a new
frontend manifest or synthetic controller YAML. Unsupported interface or event
semantics produce `NOT_TESTED` and block readiness.

1. **Acceptance:** `txn_valid && txn_ready` at the consuming rising clock edge
   enqueues a host request. Presenting valid alone never counts as acceptance.
2. **Dispatch:** `dispatch_fire` (`accept_txn_fire`) removes `selected_index`
   from the queue and enters the single active service slot.
3. **Write completion:** `completion_fire` (`service_done`) for an active write
   commits `service_wdata_q` at its bank/address. There is no external write ack.
4. **Read completion:** service completion registers memory data; the monitor
   observes `rsp_valid/rsp_rdata` at the next rising edge. A read remains
   outstanding until its actual response is checked.
5. **Request ordering:** acceptance order does not guarantee dispatch order.
6. **Read ordering:** reads can reorder before dispatch; responses follow the
   serialized service order.
7. **Write ordering:** writes can reorder before dispatch; commits follow
   serialized service order.
8. **Same address:** no FIFO/dependency ordering guarantee. Ordered write then
   readback scenarios use drain barriers; overlapping traffic uses observed
   service order for expected memory values.
9. **Same bank:** no FIFO guarantee. The queue uses the lowest free slot and
   can reuse a just-dequeued slot when full; scheduling can prefer row hits or
   retain a locked selection. The checker does not require an acceptance FIFO.
10. **Responses:** at most one externally observable response at a time, no
    external transaction ID, and one active service slot. Dispatch and service
    completion cannot occur together; completion and new acceptance can.
11. **Reset:** queued work, active service, and waiting responses are cancelled;
    controller memory and row state clear. Cancelled work is accounted for,
    rather than falsely counted as completed or lost.

Independent checking and coverage
--------------------------------

The driver holds all request fields while ready is low and drives on falling
edges. The monitor samples the consuming rising edge with clocking input
`#1step`. Only the adapter in `dut_tb_top.sv` contains hierarchical references.
It exposes dispatch/selection, active service/completion, queue state,
row-buffer state, bank command routing, logical ACT requests, and refresh.
No DUT memory or DUT traffic counters supply expected values.

Every real controller port is connected from Agent 1's resolved port metadata;
the complete mapping is saved separately in `dut_port_connectivity.json`.
The transaction contract still selects only the behavioral host signals.
Additional outputs get four-state `dut_port_<name>` interface nets, clocking
monitor inputs and waveform visibility. The current `cmd_ready` output is
connected this way for debug; acceptance continues to use `txn_ready`.
New inputs/inouts require a reviewed drive/tie policy, and unresolved extra
output widths require review; the renderer never guesses a tie or omits a port.

For VCS, waveform dumping names explicit nets/variables rather than passing
the interface instance to `$dumpvars`. The TB assigns `vif = bus` to an explicit
`virtual dut_if vif` handle before handing that value to `uvm_config_db`.

The scoreboard reconstructs queue-slot identity from observed host handshakes
and the queue allocation rule. It associates a selected slot with its accepted
bank/address/read-write/data tuple and permits arbitrary dispatch order. An
associative reference memory keyed by bank and complete address stores only
stimulus-derived data, committed at write service completion. Read expectations
come from this memory at read service completion. Private checker IDs do not
add production RTL ports. A separate Python model replays monitor events and
rejects response/data/identity/accounting inconsistencies even if runtime
markers claim PASS.

Presented, accepted, dispatched, completed, responses, reset cancellations and
outstanding requests are recorded independently. At final drain:
`accepted == completed + cancelled_by_reset` and `outstanding == 0`.
Unexpected/repeated dispatches, completions or responses fail; pending requests
at a declared drain are lost. Without a completed proof, zero loss/duplication
is not claimed: JSON counts use `null` where unknown. Identical request tuples
cannot have externally distinguishable identities; the proof checks their
multiplicity and queue-slot/payload preservation under the documented contract.

Directed scenarios retain frontend reset/readback/locality/timing behavior and
add consecutive bursts, backpressure, queue saturation, multiple outstanding
requests, completion plus new acceptance, repeated/mixed traffic, same/different
banks, bank isolation, same/different rows, row replacement, reset with work
outstanding, refresh with accepted work, alternating-bank timing, long mixed
traffic, and final readback/drain. Bank counts, widths, queue depth, page policy,
enabled features and timing cycles come from current inputs. A single-bank
frontend may omit `txn_bank`; the adapter handles that interface. Memory tests
cover arbitrary tested addresses without allocating an exhaustive expected-data
array. Runtime coverage counts determine whether interface saturation or timing
boundary conditions actually occurred.

Stable IDs cover reset (`SYS-REQ-001`), acceptance (002), backpressure (003),
preservation (004), uniqueness (005), readback (006), routing (007), bank isolation
(008), row hit (009), row miss (010), page policy (011), refresh (012), tRRD (013),
tFAW (014), and completion (015). Optional bank/row/refresh/timing requirements
are instantiated only when applicable. Evidence preserves each executable
comparison's scenario, cycle, expected and observed values. Positive counts
and actual comparisons are required for PASS; violations remain FAIL, and
unexercised/unobservable requirements remain NOT_TESTED. System IDs and existing
module `REQ-*` IDs remain distinct. No LLM decides correctness or repairs a
functional failure by weakening the checker.

Reuse and Phase 3 limits
-----------------------

Agent 2 reuses the existing VCS compiler/runner, UVM evidence parser,
requirement reporting, source hashing and saved-environment machinery. Module
mode retains its schema-1 handoff and approved YAML. System mode saves a
schema-2 handoff containing all RTL hashes, original manifest/hash, configuration,
module specs, metadata hashes, interface/package/TB sources, contract/catalog,
compile flags, checker identity, and logs. Normal system runs automatically scan
prior environments for an eligible compatible checker and copy its exact source
bytes without an LLM call. `--reuse-tb` prefers the saved environment; absent or
incompatible sources enter generation/adaptation rather than unsafe reuse.
Pre-correction environments without compilation/eligibility metadata are not
eligible. Module-mode reuse behavior remains unchanged.
Agent 3's existing module reuse path remains supported. System mode authenticates
this schema-2 handoff, compiles separate stimulus/observation sources, and uses
the saved checker unchanged; it never invokes the LLM to revise expectations.

System LLM generation and integrity policy
-----------------------------------------

For a new or incompatible system, Agent 2 calls its existing `call_llm()` client
with the approved top interface, clock/reset, complete dynamic RTL inventory and
module metadata, manifest/configuration, enabled features, controller contract,
SYS-REQ catalog, VCS flags and rendered scaffolding. It asks for exact JSON edits
using the existing Agent 2 edit parser. Raw responses, response metadata, prompts,
input/prompt hashes and every candidate are retained in the run's `generation/`.
The existing file-block response format is also accepted.

The model authors/adapts `dut_sequencer`, `dut_agent`, `dut_env`, and `dut_test`.
The assembled environment also contains trusted `dut_item`, `dut_sample`,
`dut_sequence`, `dut_driver`, `dut_monitor`, and `dut_scoreboard`. The hierarchy is
test → environment → agent → sequencer/driver/monitor; the monitor's analysis
port connects to the environment scoreboard. All previous stimulus, reference
memory, dispatch/completion association, reset cancellation, timing checks and
accounting are preserved.

The deterministic integrity audit protects the interface/sampling adapter, DUT
connectivity, reference model and traffic; it also protects UVM connections and
the test's objection/sequence/finalization/fatal lifecycle. Interface/top and
package text outside the infrastructure markers are immutable byte for byte.
A balanced-token structural parser reads class declarations and methods, resolves
typed handles, and inlines explicit helpers. It accepts renamed handles/phase
arguments, factory or typed new construction, split declarations/assignments,
independent build ordering, super.connect_phase, explicit helper methods, this
receivers, and parenthesized connection arguments. Creation/connection ownership
and the sequence/objection/finalize/fatal/drop ordering remain mandatory.
Unknown constructs fail closed with a specific diagnostic. This parser covers
the permitted infrastructure subset; VCS remains the language/type elaborator.
It cannot authorize arbitrary SV behavior or new runtime
hooks, suppress reports, alter expected data, remove checks or change coverage
classification. This is intentionally constrained generation around a trusted
checker, not arbitrary model-authorized behavioral verification code.

Generation and compile repair share a maximum of five model requests, including
integrity retries (also respecting a smaller MAX_ITERS setting). A repair uses
the exact previous candidate and full named audit or compiler diagnostics.
Every attempt has a terminal section and a one-based `generation/attempt_N/`
directory containing sources, prompt, raw reply/metadata, audit.json and
diagnostics.txt. compile.log appears there only when that attempt invoked VCS.
`iteration_history.json` records actions, model, files, checks, failures,
invocation/results and repair decisions. Attempt 1 acceptance is LLM_GENERATED;
acceptance on attempts 2–5 is LLM_REPAIRED; saved checker reuse is REUSED.

After five rejected candidates/failed TB compilations, classification is
TESTBENCH_GENERATION_FAILURE, disposition AGENT2_TB_REPAIR_EXHAUSTED and readiness
BLOCKED_SYSTEM_AGENT_2. Integrity failure before compilation reports
SYSTEM_UVM_GENERATION_FAILED with VCS compile/simulation NOT_RUN, never a compiler
failure. Unobserved counts remain null/NOT_OBSERVED. Only
compiler errors positively localized to TB sources permit `AGENT2_TB_REPAIR`.
RTL-localized, mixed or unlocated/tool errors stop that loop. Reused checker
compile failures also stop without changing saved sources. Simulation runs once
after successful compilation and never enters the LLM repair loop.

`environment_saved`, `checker_compile_valid`, `checker_reuse_eligible`,
`simulation_completed`, and `agent2_result` are separate. Reuse requires a
complete source set, source/input hashes, matching top/RTL inventory, unchanged
manifest/configuration, contract/catalog, successful VCS compilation and passing
integrity audit. The trusted scaffolding revision is checked too. A valid checker
may remain reuse-eligible after a functional DUT failure; that never implies DUT
PASS. Compile-failed sources are preserved for debugging with eligibility false.

The terminal and `system_report.json.simulation` expose compile/simulation status,
`uvm_error`, `uvm_fatal`, `scoreboard_pass`, and `scoreboard_fail`. No simulation
uses unknown/null counts rather than presenting an unexecuted test as clean.
PASS/PASS_WITH_GAPS require successful compile and simulation, zero UVM errors/
fatals, positive scoreboard passes, zero scoreboard failures, no requirement
failures, proven accounting and no mismatches/loss/duplication. PASS_WITH_GAPS
only describes unexercised coverage, never a failure waiver.

Frontend RTL failure feedback
-----------------------------

Proven checker/replayed DUT failures are `RTL_FUNCTIONAL_FAILURE` with disposition
`RETURN_TO_FRONTEND_RTL_FIX` only after integrity passes, VCS compiles and valid
executable startup is observed in trusted monitor/accounting markers. A compiler
RTL diagnostic produces REVALIDATE_AGENT1_RTL, without functional frontend
feedback. Testbench generation/compilation failures remain inside Agent 2.
Missing verification evidence or interface/tool
failures do not establish an RTL fault. The run stops and writes
`agent2/frontend_feedback.json`; no production RTL is edited and no additional
module diagnostics, Agent 3 or backend runs start.

The package contains failing SYS-REQ/scenario and expected/actual evidence,
monitor history, known bank/address/row/column/operation/data, acceptance,
dispatch/service/completion/response cycles, pending/outstanding work, reset and
available timing/row/refresh comparisons, UVM/scoreboard/assertion log evidence,
and paths to logs, requirements, report and waveform when present. Unobserved
times/state remain unknown; internal subsystem attribution stays empty unless
supported by actual evidence. Dispatch is the documented entry into service.

The frontend/operator discovers the disposition and feedback path in
`verification_reports/latest.json`, `summary.json`, `agent2/report.json` or
`agent2/system_report.json`. Artifact mappings are relative to each run for
transport. This is an evidence handoff; automatic frontend RTL regeneration is
not implemented here. Frontend changes RTL and rebuilds its manifest, then runs
the same pipeline command again, starting at discovery and Agent 1. Changed RTL
hashes invalidate checker reuse. Each attempt receives a new report directory;
prior failure logs, checker sources, RTL snapshots and feedback are preserved.

For a direct Agent 2 invocation, use artifacts from a fresh structural pipeline
run (the pipeline command is preferred):

```sh
python3 server/validation/agent2_uvm_tb.py local/frontend/rtl_output/manifest.json \
    --system \
    --agent1-result <run>/agent1/validation_result.json \
    --agent1-inputs <run>/agent1/system_inputs.json
```

Direct reuse additionally requires `--reuse-tb --uvm-environment <saved-manifest>`.
Fingerprints cover every manifest input, checked around structural validation,
before simulation, and before saving the handoff. Missing VCS or UVM compilation
failure preserves sources/logs, blocks readiness, and never starts simulation.
Regression tests mock compiler responses and exercise the Python checking and
orchestration; they do not establish real SystemVerilog compilation or DUT PASS.

Timing checks observe the wrapper's logical `act_pulse` (request/admission), as
does the frontend TB. Row checks prove logical row-buffer replacement/page policy.
The interface does not expose physical DRAM PRE/ACT/REF pins, so physical command
sequencing is an explicit observability limitation. Changed internal signal names
or service architecture require an adapter/contract review. A missed tRRD boundary,
unreached saturation or disabled/unobserved condition is not silently passed.
Results can be `PASS`, `PASS_WITH_GAPS`, `FAIL` or `NOT_TESTED`; core functional
requirements and complete independent accounting must pass for Agent 3 readiness.
Agent 2 alone establishes neither backend readiness nor completed Agent 3 verification.
