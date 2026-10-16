# LLM microarchitecture flow

The architecture stage makes design decisions before module RTL generation. An
LLM proposes module decomposition, responsibilities, behavioral contracts and
connections. A deterministic compiler validates and lowers that proposal. Feature
templates are reusable blocks; custom module specs do not require registration in
`GENERATED_INPUT_SPECS` or `SUPPORTED_FEATURES`.

```text
Requirements + constraints + reusable library/timing profiles
    → LLM architecture proposal
    → graph/module-IR validation (up to three proposal attempts)
    → architecture.yaml + review rationale and verification obligations
    → resolved module specs → existing IR/RTL routes
    → structural wrapper + exact filelist → integration lint
```

The flow is opt-in while controller-level equivalence is established. The legacy
config expansion and specialized controller wrapper remain available. Architecture
builds bypass that wrapper: all functional glue must be described in a module,
and the manifest supplies all inter-module wiring.

The new `--specification` route adds a system-behavior stage before decomposition:

```text
Request + constraints + optional compatibility preset
    → system/shared behavior + independent verification intent
    → validation and locked system identity
    → module contracts + rule owners + graph + observation bindings
    → package/matrix/architecture/IR checks → exact export for review
    → explicit revision-bound approval → existing module RTL routes
    → structural integration + lint + handoff export
```

Use `run_flow.py request.yaml --specification --microarch-only` to prepare review
artifacts, then build the approved exported manifest. Both stages support bounded
validation repair, offline response replay and context-bound cache. The generator
preserves each requirement/configuration constraint and reports unsupported
requests without publishing partial packages. The example remains format data;
it does not impose FIFO or eight modules on every request. A small generic
register fixture exercises the staged route and real cached RTL/lint path.
See the [staged commands](specification/README.md#generate-a-package-from-a-request).
No orchestrator, downstream UVM/vector execution or backend is launched.

## Propose and review

From the repository root:

```sh
python3 local/frontend/run_flow.py local/frontend/configs/user_input.yaml \
  --microarch-only
```

This uses `TAMUS_AI_CHAT_API_KEY` and the project's existing TAMU chat endpoint.
`--microarch-model` selects the architecture model; its default comes from
`DEFAULT_MODEL` in `generate_microarch.py`, shared by both entry points.
The standalone `generate_microarch.py` also creates a proposal and module YAMLs without RTL calls;
its model flag is `--model`. Neither command requires a new SDK.

Default proposal: `local/frontend/microarch/proposals/architecture.yaml`, with a
Markdown review beside it. Proposal-only runs now also emit concrete module YAMLs
under `local/frontend/microarch/proposals/module_specs/<manifest-hash>/` and an
`architecture.modules.json` verification index beside the proposal. This does not
generate RTL, a wrapper, or module IR. Custom specs and reusable templates use the
same resolved module format as the RTL build path. Each YAML includes behavior,
ports, constraints/invariants, timing substitutions and `verification_requirements`.
The index maps each instance and role to its YAML path and owned requirements;
paths are relative to the index directory. Repeated instances share a definition.
The Markdown report links each YAML, and run_flow prints the directory/index. Inspect ownership, interfaces, assumptions, limitations,
and verification obligations before spending time on module RTL generation.

For architecture exploration, provide a YAML mapping containing constraints and
a `requirements` list of strings. Unlike the legacy text extractor, this entry
point does not restrict requests to existing schedulers, bank counts or feature
names. `--requirements-file` adds an entire text file as another requirement.
The proposal must preserve each requirement's ID and exact text, identify owners,
and describe a verification scenario. This checks traceability, not whether the
behavioral design actually satisfies the requirement. Config constraints are
included verbatim in the prompt/provenance; their semantic satisfaction still
needs review and behavioral tests.

Legacy controller configs (including `configs/user_input.yaml`) automatically
attach the versioned `current_controller_v2` compatibility preset from
`controller_baseline.py`. This preserves the existing top name, host ports/widths,
4-bit address mapping, queue admission/ownership, functional banked memory and
read responses, and existing tRCD/tRAS/tRP timing protection. It defaults to
four waiting requests plus one separately stored active request; an explicit
`microarchitecture.waiting_capacity` positive integer changes waiting capacity
without changing the single active slot. It requires ownership through any PRE/ACT setup, and separates candidate permission
from accepted ACT updates. These are behavioral requirements, not fixed module
names or template choices. Controller proposals MUST have separately owned
`request_queue`, `scheduler`, `command_engine`, `timing` and `memory` roles. Declare
`roles` on each module; optional roles are `host_adapter`, `row_state`, `refresh`.
Mandatory roles cannot share a module instance or definition. Each role must have
functional connections to other modules; clock/reset-only dummy modules do not
satisfy decomposition. Related baseline requirement owners must include their
functional role owner. The compiler attaches `design_kind: memory_controller` to
controller proposals; small standalone block examples may still use one module.
Profile/host-interface and exact requirement coverage are checked;
behavioral satisfaction still needs RTL tests.

The compiler attaches the baseline contract to saved proposals and rechecks it
when building directly from a manifest. Missing/changed baseline requirements and
renamed host ports fail validation. Proposals from before this contract must be
regenerated; `--cache` correctly rejects the old prompt/compiler fingerprint.
To explore an intentionally incompatible design, a request can explicitly set
`microarchitecture: {baseline: none}`. A generic requirements-only request has no
legacy baseline by default; `baseline: current_controller` opts into it.
The preset does not restrict scheduling to legacy policy names. A named
`features.scheduler` remains a requested constraint, so existing `simple` inputs
retain their row-hit/slot-index behavior while a FIFO request is allowed. Omitting
a policy leaves its selection to the design request/specification. Unspecified
refresh/tRRD/tFAW switches add no preset requirement; explicit switches remain
binding. Waiting-capacity options with a disabled/absent preset fail rather than
being silently ignored. These options apply to the architecture flow.

```yaml
microarchitecture:
  baseline: current_controller
  waiting_capacity: 3
features:
  scheduler: fifo
  page_policy: open_page
  refresh: true
  tRRD: true
  tFAW: true
```

The first-target system-specification package is now reviewed for implementation;
its accepted defaults and intentional compatibility changes are documented in
[specification/ddr4_fifo/review.md](specification/ddr4_fifo/review.md). Run
`python3 local/frontend/specification.py local/frontend/microarch/specification/ddr4_fifo/package.yaml --matrix`
from the repository root for read-only structural checks. Packages can now feed
the existing architecture/RTL route through deterministic contract lowering and
revision-bound approval. Automatic request-to-system-spec generation is available
through `--specification`; live first-target generation remains to be exercised.
See the [package commands](specification/README.md#export-module-specifications-and-review-before-rtl).

```sh
python3 local/frontend/run_flow.py \
  local/frontend/microarch/specification/ddr4_fifo/package.yaml --microarch-only
```

This exports module YAMLs, a portable source snapshot, architecture graph, review
report, and unverified requirement results under
`microarch/proposals/specification/<package-fingerprint>/`. No LLM or RTL call is
made. `--spec-set NAME=VALUE` and `--spec-profile` elaborate allowed configurations;
`--architecture-output` selects an explicit review-export path. All current
package modules use the existing datapath/dual-LLM route, with complete normative
contracts and numeric shared context, rather than invented FSM transition tables.
An approved exported manifest builds with `--spec-approval path/to/approval.json`;
`lower_architecture` enforces approval before creating RTL/build outputs. Edits to
derived specs require re-export from reviewed contracts. This gate applies to
specification-derived inputs, while ordinary architecture/legacy routes retain
their existing behavior.

Package reviews also export `architecture.handoff.json`; completed package builds
export `verification_handoff.json` with exact RTL/spec/IR identities and declared
event bindings. `verification_handoff.py` validates this portable version-1
contract without running verification. The current downstream system consumer
requires an adapter: legacy wrapper-private observations and non-FIFO ordering
semantics cannot be assumed for a new structural architecture. See the
[handoff contract](specification/README.md#handoff-inputs-and-validation).

```yaml
design_name: ddr4_controller
memory:
  protocol: DDR4
  speed: '3200'
topology:
  banks: 4
requirements:
  - Use queued requests and preserve the current host acceptance contract.
  - Explore shared versus per-bank control and explain the selected decomposition.
```

`start_flow.sh --microarch-only --input path/to/request.yaml` is the equivalent
entry point from `local/frontend/`. This bypasses the old constrained text
configuration extractor. `--microarch` without `--input` uses the existing config.
The existing `--interactive` prompts belong to the legacy flow; use
`--microarch-only` and then a separate manifest build for architecture review.

## Generate from a manifest

```sh
python3 local/frontend/run_flow.py \
  local/frontend/microarch/proposals/architecture.yaml
```

Builds default to `local/frontend/microarch/builds/<manifest-hash>/`; use
`--build-dir` to choose an isolated directory. Builds contain the resolved specs,
IR, RTL, `architecture.yaml`, `filelist.f`, `build_status.json`, lint evidence,
and `verification_plan.json`. The existing controller RTL is not regenerated by
this path. A new FSM follows the existing deterministic FSM emitter; a new
datapath follows dual-LLM generation/review. Known reusable timing blocks retain
their specialized deterministic emitters.

`--cache` on a requirements/config input requires an existing proposal matching
the request, library, prompt/compiler and model. It never falls back to a live
call. Architecture builds keep reviewed RTL caches separate from legacy caches,
keyed by resolved module spec content. `--cache` on a manifest skips proposal
creation and uses those module caches; deterministic modules still regenerate.

## Offline example

The checked-in timing example is a small compiler smoke test, not a controller:

```sh
python3 local/frontend/run_flow.py \
  local/frontend/microarch/examples/timing_request.yaml --microarch \
  --microarch-response local/frontend/microarch/examples/timing_proposal.json \
  --architecture-output /tmp/capstone-proposal.yaml \
  --build-dir /tmp/capstone-build

python3 -m unittest discover -s local/frontend -p test_microarchitecture.py -v
```

A replay file must contain JSON with the same manifest shape as an LLM response.
It passes the same validation and requirement-coverage checks. Replay proves the
compiler/generation path; it does not prove a live model produces good proposals.

## Manifest contract and current compiler limits

`architecture_version: 1` requires `design_name`, `profile`, `rationale`,
`top_ports`, `modules`, `connections`, and `requirements`. Optional `assumptions`
and `limitations` are string lists. The generator supplies `provenance` containing
the input snapshot, model, fingerprint and repair diagnostics.

Each module has an `instance`, a `responsibility`, controller `roles` when applicable, and exactly one source:

- `reuse`: `feature`, `submodule`, optional `design_name`; timing and behavior are
  resolved from the existing library/profile. Interface/behavior overrides need
  a custom spec instead.
- `spec`: a concrete existing module spec with `design_name`, `design_type`,
  `clock`, `reset`, `ports`, and FSM state/output definitions or datapath `behavior`.
  Explicit clock/reset names are implicit input ports. Do not list them again
  in custom `ports`. The compiler normalizes existing library clock/reset ports.

Connections use `from: instance.port` and `to: instance.port`, with `top.port`
for host ports. Top inputs and child outputs are sources; child inputs and top
outputs are sinks. Source fanout is supported. Every sink needs exactly one
source; widths must match. Top inputs must be used. Optional child outputs may
be left open with a top-level `unused_outputs` entry such as
`{endpoint: u_tfaw.act_count, reason: Debug count is not used}`. The wrapper emits
an explicit `.act_count()` connection; dummy consumers are not required. Repeated instances
may share an identical module spec. Different definitions cannot share a module
name. Special names triggering existing timing/scheduler rewrites are reserved
for their matching reusable templates.

Custom datapath specs require `combinational_dependencies`, mapping every output
to its current-cycle input dependencies. For example `ready: [valid]` declares a
combinational path; `data: []` declares no current-cycle input dependency. Include
transitive paths through internal expressions. FSM dependencies are inferred from
output expressions; reusable blocks use the dependency models in
`architecture_dependencies.py`. The compiler rejects cycles across wiring and
these declared paths. Incorrect model declarations can still hide bugs, so RTL
lint and behavioral tests remain necessary. The existing tFAW template's prospective
permission outputs depend on `act_pulse`; directly gating that accepted pulse with
those outputs is detected as feedback.

Current lowering supports one top-level clock/reset source, consistent reset
polarity, fixed positive integer port widths, input/output ports, and full-port
connections. Synchronous and asynchronous assertion styles can coexist on the
same reset source/polarity. Constants, slices, expressions on connections, parameter overrides,
instance arrays and inout ports are unsupported. Unknown architectural fields
fail rather than being ignored. Glue logic belongs in a module spec. New IR
constructs should be added explicitly when a useful proposal needs them.

Structural/dependency checks cannot establish same-address ordering, fairness,
command legality, refresh correctness, or absence of undeclared/internal
combinational loops. RTL
lint is a subsequent check. Arbitrary architectures do not use the specialized
legacy controller testbench. Verification obligations remain `planned_not_executed`
and build status records behavioral verification as `not_run`; runnable controller
scenario generation is a later integration task.

## Validation recorded — 2026-10-08

Offline regression covers graph errors, custom module lowering, reusable timing
resolution, implicit clock normalization, shared definitions/fanout, requirement
coverage, proposal repairs, stale-cache rejection, and failure without replacing
a valid proposal. End-to-end tests generate/lint reusable timing and custom FSM
builds, replay the architecture cache, and run a Verilator timing simulation.

The user subsequently ran a live TAMU proposal successfully: the saved provenance
shows `protected.gpt-5.4` and two validation repairs before success. Review found
missing baseline timing/data behavior and declared behavior implying feedback.
The strengthened baseline/dependency checks have offline regression coverage;
a new live proposal under those checks and controller-level behavioral equivalence
remain to establish before adopting this path as the default.

Regression coverage also includes baseline attachment and preservation, host-port
repair feedback, explicit unused outputs, mixed reset assertion styles, and
combinational-loop rejection with valid registered feedback allowed.

## Modular controller and specification-export checks — 2026-10-09

The reviewed Terra proposal grouped everything into one core; controller validation
now rejects that decomposition. Regenerate it with `--microarch-only` to obtain
separate module contracts. Baseline capacity is explicitly four WAITING requests
plus one separately stored active request: waiting dequeue at dispatch and active
retirement at final service are distinct events. The prior proposal is kept for
inspection and is no longer accepted by the strengthened contract.

Regressions cover role separation, failed proposal preservation, concrete module
YAML export, requirement/role index contents, and proposal-only CLI output without
RTL or module IR. Module verification itself remains a subsequent task.

## Usage and RTL review diagnostics — 2026-10-16

`run_flow.py` prints `[TOKENS] Module ... input=... output=... total=... calls=...`
after each module completes or fails, and `[TOKENS] Flow ...` on exit. Input means
provider prompt tokens; output means completion tokens. Totals cover the current
invocation, including specification/architecture calls, generation, lint repairs,
review, merge, and truncation retries. They include both models and module
subprocesses. Cache-only and deterministic modules consume zero new API tokens.
Missing provider usage is reported as unknown with known subtotals; cached-input
and reasoning details are already included in provider totals and are not added
again. Separate invocations have separate reports; historical runs without usage
cannot be reconstructed fully.

`[COST] Module ... estimated USD cost=$...` follows each module's token line,
and `[COST] Flow ...` reports the combined estimate on completion or failure.
Costs sum each call's input and output usage at its model's rate, including
reviews, repairs, and retries. Flow cost includes specification/architecture calls
when those stages run in the same invocation. Cache-only modules cost $0 for the
current invocation. Estimates use the user-provided rates below, without applying
unprovided cache discounts or other billing adjustments:

| Model | Input USD / 1M tokens | Output USD / 1M tokens |
| --- | ---: | ---: |
| `protected.Claude-Opus-5` | 5 | 25 |
| `protected.gpt-5.6-terra` | 2 | 12 |

Missing usage or an unpriced model makes the full cost unknown; the known cost
subtotal remains visible. Reports include `totals.cost` for each module, model
and flow, and the flow report snapshots the user-supplied rates under `pricing`
(`usage_version: 2`). Dollar values print to six decimal places.

Reports are saved under `microarch/usage/<timestamp_id>/`: `calls.jsonl` holds
individual usage events, `usage.json` contains flow totals, per-model totals and
module summaries, and `modules/<scope>/usage.json` contains module totals.
Each dual-LLM module also saves `rtl_review.json` in that module directory with
round candidates, lint findings, full reviewer issues and the failure reason.
The flow prints its report path and records it in isolated `build_status.json`.
Standalone `design.py` and dual-LLM CLI calls also create usage runs. These
reports contain no API credentials or raw request prompts.

The RTL review limit remains three rounds. An ambiguity now stops immediately
with a specification-clarification error, rather than paying for repeated reviews
of the same contradictory context. Spec violations can still receive bounded
repair; the final round does not pay for a merge that cannot be reviewed. The
normalized public module interface includes declared clock/reset ports, and
internal helpers are allowed within the specified owned behavior. These are
frontend diagnostics and implementation-context repairs; they do not establish
behavioral correctness or complete downstream verification.

## Reusable prompt-context reduction — 2026-10-16

The dual-LLM generator, reviewer, merge and compact-review fallback now receive
the complete normalized context once. `prompt_context.py` serializes compact
JSON and replaces sufficiently large exact repeated values with references to
shared definitions. `context_format: shared_definitions_v1` defines local
`{"$ref":"#/definitions/C..."}` substitution; definitions may reference other
definitions. This transformation preserves every field, value and list order.
It does not choose fields by design kind or summarize behavioral requirements.
Small contexts and contexts already using reference objects fall back to plain
compact JSON. The serializer retains the shorter representation, and inlines
single-use definitions. `expand_context()` reconstructs the original JSON for
regression checks. The validated IR, approved contracts and cache/approval source
fingerprints remain unchanged.

Constraints and invariants previously repeated after the structured JSON now
appear only in that context. Generation and merge responses return status,
issues and complete RTL; they no longer echo invariants. The parser retains the
compiler's invariant list independently of model-supplied echo fields and remains
compatible with older responses. The compact-review fallback now retains the
same complete facts as normal review, including behavioral metadata and FSMs.

Two rounds of diagnostic history remain available. Code already supplied as the
current candidate is omitted from history; regeneration without an explicit
candidate includes one latest RTL snapshot. Merge supplies a second version only
when reviewer RTL differs. Full round candidates remain in `rtl_review.json`.
No retry limits, generation budgets, cache-resume policy or approval rules changed.

Offline comparison against the previous context formatter on the eight saved
first-target module specs measured a combined 45.86% reduction in context text
characters (individual reductions 44.08–46.80%). The command engine dropped from
136,855 to about 73,665 characters. This measures context text, not billed tokens;
system prompts, candidate RTL and repair history also contribute to requests.
Each dual-LLM call now logs and records `request_characters` (system plus user text)
with provider-reported token usage, supporting comparison in subsequent live runs.
Round-trip preservation tests cover all eight supplied contracts, unknown metadata,
a synthetic new protocol/FSM, nested definitions and generated arbitrary contexts.
The first live run with these prompts remains pending.

Run the prompt regressions without provider calls:

```bash
python3 -m unittest discover -s local/frontend -p test_prompt_context.py
```

## Module completion order and output limits — 2026-10-16

For every successful `design.py` generation route, module reporting now ends with
one blank line, `[TOKENS] Module ...`, `[COST] Module ...`, then the `FINISHED`
success banner. The banner is emitted after usage reporting; failed modules keep
usage/cost reporting and do not emit a success banner. Standalone flow totals
follow the module banner, and parent-flow totals remain at flow completion.

The subsequent reduced-context live run (`20261016T234522Z_dd74d6f01665`) still
truncated command-engine generation at 8,192 completion tokens, then its 16,384
retry failed with HTTP 524. The known run cost was $1.228342, with usage/cost of
the failed HTTP request unavailable. This records a real limitation of context
reduction: a large response can still require a larger initial output budget.

Default `LLM1-Generate` and `LLM1-Merge` calls now start at 16,384 output tokens;
review calls retain the 8,192 initial budget. The default ceiling stays 16,384,
so code calls do not first discard an 8k attempt before requesting 16k. The
existing `DUAL_LLM_MAX_TOKENS` explicit setting overrides the initial budget for
all roles. `DUAL_LLM_MAX_RETRY_TOKENS` still sets the ceiling; absent an explicit
initial setting, role defaults are capped at that ceiling. Review truncation
can still trigger a bounded budget increase within the ceiling. Review rounds
and HTTP timeouts/retry policy have not increased.

On truncation, module CLI runs now save `truncated_<id>.json` beside
`rtl_review.json`, including partial text, raw usage details, requested budget
and finish reason. Partial responses remain unaccepted and do not reach lint or
RTL emission. These reports allow diagnosis of output verbosity versus reasoning
usage when the provider supplies those details. A larger initial budget avoids
the observed discarded attempt; it does not guarantee avoidance of server-side
HTTP 524 timeouts. Live validation of this budget change remains pending.
