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

## Shared TPM pacing — 2026-10-18

Frontend model requests now use `tpm_pacing.paced_post()`. One local rolling
60-second budget is shared across models, module subprocesses and consecutive
runs in this workspace. The default target is 80,000 estimated tokens per minute.
No additional command-line flags are needed. The same small wrapper covers
specification/architecture generation, dual-LLM generation/review/merge and their
truncation retries, legacy RTL calls, text configuration and online debug calls.
Cached RTL and deterministic generators make no API reservations.

Before every HTTP attempt, reserve estimated input plus the full requested output
allowance. The dependency-free input heuristic counts half the UTF-8 bytes of
serialized messages, rounded up, plus 256 tokens for overhead. This is deliberately
more generous than a four-character heuristic for RTL; it is not a provider
measurement or a guarantee for every tokenizer. Each reservation remains for
60 seconds from dispatch, including failures and short responses. There are no
early refunds, adaptive calibration or new network retries. Token/cost reports
continue to use only provider-reported usage.

If the next reservation does not fit, wait for older reservations to expire and
recheck the budget. The wait releases the file lock and logs progress in chunks
of at most 30 seconds. Each admitted request also logs its reservation and the
rolling total. A single request exceeding the whole local budget fails before
network activity with guidance to reduce context/output allowance or configure
the limit; it never waits indefinitely for impossible capacity.

Configuration:

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `CAPSTONE_TPM_LIMIT` | `80000` | Shared local estimated TPM target; `0` explicitly disables pacing, chiefly for offline tests. |
| `CAPSTONE_TPM_STATE_FILE` | `local/frontend/microarch/usage/tpm_reservations.json` | Shared reservation file; use the same path and target for cooperating processes. |

The state stores only dispatch timestamps and reserved counts. A local file lock
serializes reservations; atomic replacement protects state writes. Malformed
state fails with an explicit diagnostic instead of silently discarding active
reservations. State files live under the existing ignored usage directory.

Provider `x-ratelimit-*`, `ratelimit-*` and `Retry-After` headers, when present,
are logged. Existing usage-ledger responses also save them under
`rate_limit_headers`. This pass does not automatically parse reset/cooldown headers
or retry HTTP 429/524 responses, enforce the $20 daily allowance, or coordinate
with account activity outside cooperating frontend processes. The provider's
actual TPM accounting and the cause of the earlier 524 remain unconfirmed.

Offline pacing tests use fake clocks and mocked HTTP responses. Coverage includes
window expiry, bounded waits, oversized requests, failed attempts, truncation
retries, model/API-stage sharing, subprocess contention, header filtering and
unchanged provider-based billing totals:

```bash
python3 -m unittest discover -s local/frontend -p test_tpm_pacing.py
```

## Specification timing references and repair size — 2026-10-18

The fresh run in `/tmp/ddr4-frontend-fresh-20261018` received a complete system
response, but rejected numeric timing-profile bindings. The next repair request
was blocked locally: its estimated input plus 24,000 output allowance required
82,444 tokens, exceeding the 80,000 pacing target. No architecture or RTL was
produced, and no second API request was sent. This was a local oversized-request
check, not a provider rate-limit or timeout response.

The generation contract now explicitly distinguishes profile-relative library
path strings (for example, `timing_cycles.tRCD` or `timing_cycles.tRFC1`) from
shared references consuming those bindings (`shared.timing_profile.bindings.tRCD`).
Binding values remain strings; the compiler resolves cycle counts separately for
each selected profile. A numeric binding now reports the full offending field,
received type/value, and expected path convention. No implicit conversion or
schema relaxation was added.

Specification prompts use compact JSON for requests, stage examples/capabilities
and locked system data. Repair feedback includes one complete parsed previous
response as compact JSON, avoiding the former JSON-string-inside-JSON encoding.
Malformed responses remain literal repair data. Journals retain original raw
responses, all constraints remain present, and architecture repairs retain the
locked-system boundary. Retry limits, output budgets and the TPM target remain
unchanged. Generator revision 3 invalidates stale proposal caches; existing
approved exports are not rewritten by this prompt change.

Offline reconstruction of the actual failed request with the new diagnostic and
formatting reduced its reservation to 74,059 tokens, including the same 24,000
output allowance. An in-memory reference-only correction of the saved response
passed system validation; the historical reports were preserved. The comparison
is saved in `/tmp/ddr4-spec-repair-review.json`. This checks the reported failure
case, not arbitrary future prompt sizes or live model behavior. A new live
proposal and its subsequent review/RTL build remain pending.

## Architecture repair context — 2026-10-18

The next fresh run accepted its system stage but rejected decomposition because
an assigned rule owner lacked reciprocal invariant/verification references. Its
architecture repair reserved 99,467 estimated tokens and was blocked before the
API call. The previous repair-size check covered system repair, not the larger
combination of a locked system and a rejected architecture.

Initial generation retains the full stage-specific worked example. Repair mode
omits `format_example` and retains the complete schema guide and timing library.
A `REPAIR CONTEXT` envelope contains the exact request, capabilities and locked
system, serialized with the existing lossless shared-definition helper. This
shares repeated request/locked-rule text without filtering normative fields.
The full rejected candidate remains once in repair feedback, along with its
validation error. Repairs must return a complete corrected stage response.
Malformed candidates still retain their literal text, and raw attempt journals
are unchanged. No new partial-response/patch protocol is introduced.

The prompt and generation contract explicitly require EACH named rule owner to
list that rule in its invariants and ALL of the rule's qualified verification
obligations in its verification references. Other owners' coverage is insufficient.
Existing compiler checks remain authoritative; no automatic reference insertion
or relaxation is performed. Generator revision 4 refreshes stale proposal caches.

Reconstructing all four prompt modes from the latest saved candidate data gave
these reservations, including the unchanged 24,000 output allowance:

| Stage | Initial | Repair |
| --- | ---: | ---: |
| System | 54,826 | 58,399 |
| Architecture | 78,447 | 76,492 |

The actual saved architecture repair drops from 99,467 to 76,492, under the same
80,000 local target. Simply removing the example was insufficient; sharing exact
repeated request/system facts provided the remaining space. The comparison and
exact-reconstruction checks are recorded in
`/tmp/ddr4-architecture-repair-review.json`. Input estimation, pacing, output
allowances and round limits have not changed. These measurements cover the saved
case and regression fixtures; larger future contexts remain subject to the local
oversize guard.

Validation: 37 offline checks passed (four prompt-budget/preservation tests,
21 staged-generation/approval/handoff regressions and 12 pacing tests).
New self-contained large-controller fixtures exercise both stages in both modes,
check the default reservation budget, and reconstruct all request/schema/library/
locked-system fields plus the complete candidate. Compilation and whitespace
checks passed. No live model calls or target RTL generation ran.

### Combined architecture repair diagnostics (2026-10-18)

The next saved run reached three rejected architecture attempts. Validation had
reported only the first missing ownership link each time, hiding independent
dependency errors until the last attempt. Adding rounds would repeat expensive
repair requests without addressing that feedback limitation.

Package validation now collects ownership/reference errors and every invalid
module dependency declaration, together with independent graph and metadata
diagnostics. It deduplicates repeated diagnostics in a stable order. Shared-type
and contract-shape prerequisites still stop early when later checks would be
unreliable; binding elaboration requires clean ownership and graph checks.
Graph topology checks still stop at their first error. Invalid packages cannot
produce successful validation evidence, exports, approval or RTL handoffs.

The complete diagnostic list enters the existing repair feedback once. Structured
`diagnostics` lists are also saved alongside existing error strings in attempt
journals and repair provenance. Raw responses remain unchanged. Instructions now
explicitly require dependencies to name declared input ports, flattening paths
through outputs rather than listing those outputs as inputs. Generator revision
5 invalidates stale proposal caches; no automatic candidate edits are performed.

Offline replay of the three saved architecture candidates reports 15, four and
two errors respectively. The first report includes both invalid dependencies:
`waiting_queue.enq_ready` depends on output `head_valid`, and
`command_engine.commit_valid` depends on output `candidate_valid`. Those errors
can now be addressed together with missing ownership links in the first repair.
The corresponding repair reservations are 74,481, 72,918 and 71,935 estimated
tokens, including the unchanged 24,000 output allowance. All fit the unchanged
80,000 local target. The replay evidence is saved in
`/tmp/ddr4-aggregate-validation-review.json`; historical reports are untouched.
The three-attempt limit is unchanged. Live convergence remains untested.

Validation: 72 offline tests passed: seven diagnostic/repair tests, 12 package
tests, 21 staged-generation tests, four prompt-budget/preservation tests,
16 lowering/approval tests and 12 handoff tests. Python compilation and diff
whitespace checks passed. No live model calls or target RTL generation ran.

### Compiler-owned request rule text and origins (2026-10-22)

The fresh run `review_20261022T163557Z` first rejected 13 baseline rules whose
text was exact but whose origins were `request.current_controller_v2.ID` rather
than `request.ID`. Its second response corrected those references but included
an extra closing brace. The third call received an HTTP 500 server connection
error. Neither completed response was truncated, and TPM pacing admitted both.

This change addresses only the first failure. Requested rules are keyed by their
original IDs and may contain just `{verification}`. On its private response copy,
the compiler supplies exact request text and canonical `request.ID` origins;
legacy echoed text/origins are ignored. Model-added refinements still require
their own text/origin. Missing/renamed IDs, duplicate rules and invalid reciprocal
verification obligations remain errors. Raw response journals remain unchanged.
The normalized system is then locked and fingerprinted using the existing path;
later package edits still fail exact request and provenance validation. Generator
revision 6 refreshes proposal caches.

Offline replay of the actual first response passes system validation, changing
only its 13 origin values in memory. The saved second response still fails strict
JSON parsing. Reconstructing initial system and architecture prompts with the
first response gives reservations of 54,974 and 79,976 tokens including the
unchanged 24,000 output allowance. The architecture prompt has very little
headroom; this is evidence for that candidate, not a guarantee for larger ones.
No JSON recovery, server retries, budget changes or live API calls are included.

Validation: 47 offline tests passed (24 staged-generation, four prompt-budget,
seven diagnostic/repair and 12 package tests), including minimal requested rules,
raw-response preservation, canonical text/origins, missing/renamed rules, duplicate
IDs, invalid verification and rejection of later text edits. Python compilation
and diff whitespace checks passed. No behavioral correctness claim is made.

### One extra trailing JSON brace (2026-10-22)

Specification generation now tries the existing strict parser first. If a plain
object response fails, it may remove exactly one final `}` and try that same
parser again. It does not search for embedded objects, add missing syntax, remove
commas/comments, or accept duplicate keys. Multiple trailing braces, extra objects,
prose and truncated responses remain errors. The shared architecture parser is
unchanged. Both specification stages still run all existing semantic/compiler
checks after recovery; successful parsing cannot approve or publish an invalid
candidate. Generator revision 7 refreshes proposal caches.

Each recovered attempt logs the single deletion and records
`parse_recovery: {kind: extra_trailing_closing_brace, removed: '}'}` alongside its
original raw response in the attempt journal, even if semantic validation rejects
it. No additional model calls are made for this recovery. Offline replay of the
actual October 22 second response passes system validation after that deletion;
the historical journal's SHA-256 remains unchanged. This is system-stage evidence,
not architecture acceptance or behavioral verification. Server retries and output
budgets are unchanged, and no live API calls ran.

The user reports that the account now displays 1,000,000 TPM. This cannot be
confirmed from local files. The existing `CAPSTONE_TPM_LIMIT` override already
supports that value; a fake-clock regression exercises requests larger than 80k
and waiting when cumulative reservations exceed 1M. The default remains 80,000.
If the account's new allowance is confirmed, run `export CAPSTONE_TPM_LIMIT=800000`
in the same shell before starting the frontend for a 20% margin below 1M.
This changes local admission pacing, not model context/output limits or JSON
validity, and does not establish the cause of previous HTTP 500/524 errors.

Validation: all 54 selected offline tests passed (six JSON recovery, 13 pacing,
24 staged-generation, seven diagnostic/repair and four prompt-budget tests).
Python compilation and diff whitespace checks passed. Fixtures verify recovery
in both stages without extra calls, preservation of raw journals, rejection of
other malformed JSON, and semantic rejection after syntactic recovery.

### Combined system-stage verification-link diagnostics (2026-10-22)

Run `review_20261022T170628Z` used two system repair calls to discover three
reciprocal verification-link errors already present in its first response.
Architecture diagnostics were aggregated, but system validation still reported
only the first mismatched link. System validation now checks the schema of both
system and execution slices before collecting link errors in both directions.
Diagnostics name the slice and rule/obligation, are stably deduplicated using the
existing error type, and enter the existing repair feedback, attempt journal and
repair provenance. Malformed fields still stop early; the compiler does not infer
or insert verification links. No invalid system can advance to decomposition.

Offline replay reports all three first-response errors together: the missing
reverse link for `CONSTRAINT_9` / `V_CONFIGURATION_IDENTITY`, and the unmatched
`V_TFAW_WINDOW` references to `CONSTRAINT_6` and `CONSTRAINT_7`. The second saved
response reports its two remaining errors; the third still passes. Complete
repair reservations are 56,320 and 56,260 estimated tokens, including the same
24,000 output allowance, both below even the original 80k local target. The raw
historical journal is unchanged. Generator revision 8 refreshes proposal caches.
Three-attempt limits, JSON recovery, type handling, server retries, TPM settings
and architecture semantics are unchanged. No live API calls or RTL generation ran.

Validation: all 47 selected offline tests passed (six system-link, 24 staged-
generation, six JSON recovery, seven diagnostic/repair and four prompt-budget
tests). The new tests cover both slices, stable diagnostics, unknown/local-only
references, schema stops, complete first-repair feedback, raw journals/provenance
and unchanged bounded failure without publication. Python compilation and diff
whitespace checks passed. Live convergence remains untested.

## Resume an interrupted RTL build

Use the same approved architecture and build directory with `--resume`. The flow
still checks specification approval, lowers module specs, restores IR/RTL outputs
and runs integration lint and handoff generation. For dual-LLM modules, resume
looks in the existing build-local cache keyed by SHA-256 of the exact generated
YAML. A lint-clean reviewed cache entry is reused without model calls; missing or
lint-failing entries go through normal generation/review. Deterministic modules
are regenerated. Existing output files alone are never grounds for skipping work.
Each cache hit appears in the module review report as `cache_hit: true`, with zero
new usage. Changed specifications select different cache directories.

`--resume` requires an existing architecture manifest; it cannot start a fresh
proposal. Standalone `design.py --resume` requires `--output-root` so it uses a
specification-keyed cache. `--cache` remains strictly cache-only, and cannot be
combined with `--resume`. Ordinary runs still generate afresh. No server retry or
timeout behavior changes are included.

Example, after recording approval for the exact export:

```sh
python3 -u local/frontend/run_flow.py path/to/architecture.yaml \
  --spec-approval path/to/approval.json --build-dir path/to/existing/build --resume
```

Validation: 39 offline checks passed (seven resume, 16 dual-LLM API, 14 usage and
two approval/orchestration regressions). Tests cover cache hits without model
calls, misses and invalid caches with normal review, exact-spec cache identity,
restored outputs/IR, cache-only failures and conflicting options. Fixtures make
no behavioral correctness claims. Python compilation and whitespace checks pass.

The real interrupted October 22 build reused its first three reviewed modules
with zero API calls, then hit sandbox DNS restrictions at the command engine.
Network-enabled retry was denied, so RTL completion and integration lint remain
pending. The attempt is preserved in `review_20261022T171734Z/rtl_resume.log` and
usage run `20261022T173234Z_d224441d683a`. BIST and downstream verification/backend
were not run.

## Diagnose generation latency with a small output cap

`probe_generation.py EXPANDED_MODULE.yaml --max-tokens 512` captures the production
initial generator request before its API boundary and sends the same model,
endpoint, system message and user message with a small output allowance. It does
not run RTL parsing, lint, review, cache writes or RTL emission. There is one
HTTP attempt, existing TPM pacing, a 180-second client timeout and the normal
provider-based token/cost ledger under `microarch/usage/` with label
`generation_probe`. Existing rate-limit header capture is retained. The CLI caps
the diagnostic output allowance at 2,048 tokens. `--dry-run` captures and describes
the prompt without a key or network call, reporting characters and a message hash.

Unlike normal RTL generation, a token-limited response is an expected successful
transport result here, not accepted RTL. The probe reports HTTP status, elapsed
time including pacing, finish reason and response length without printing model
output. Malformed provider envelopes or HTTP/network failures remain failures;
no automatic retries occur. A fast limited response suggests full-output latency
contributes to the earlier timeout; it cannot conclusively isolate input
processing, reasoning or queueing effects.

The saved command-engine request reconstructs to 52,379 characters and reserves
28,887 estimated tokens with the 512-token cap. Six offline tests passed, covering
prompt capture, unchanged client restoration, dry runs without API/usage output,
one bounded request, expected truncation, HTTP/network failures, header retention
and invalid caps. Compilation and whitespace checks passed. No live probe ran
during implementation; run the diagnostic locally to obtain timing evidence.

The subsequent live 512-token probe returned HTTP 200 in 8.6 seconds, used 512
output tokens and returned zero visible characters. This demonstrates successful
handling of the complete input for that request, but does not identify where the
output allowance was consumed or establish a full-generation latency bound.

The diagnostic now prints provider usage (including any supplied reasoning token
details) and summarizes returned message fields by name, type and length, without
printing their contents. `probe.json` in that invocation's usage directory retains
these metadata and timings. Buffer-mode first-content/reasoning timestamps reflect
receipt of the whole buffered response, not when the provider first generated them.

`--stream` requests Chat Completions SSE with `stream_options.include_usage` and
enables streaming on the HTTP client. It timestamps response headers, the first
nonempty stream line (including a heartbeat), first reasoning field and first
nonempty content string. It consumes events incrementally and records the final
usage snapshot once, rather than summing usage across chunks. Absent usage remains
unknown. A completed stream requires both a finish reason and `[DONE]`; malformed
events, premature termination, error events and ignored/unsupported streaming fail
explicitly without retries or non-streaming fallback. An event-driven 180-second
diagnostic guard complements the existing 180-second network read timeout.
The production RTL generator remains buffered and unchanged.

Run the same expanded module with `--max-tokens 2048`, then repeat with
`--max-tokens 2048 --stream`, saving terminal logs separately. Compare identical
message hashes, field summaries and first-arrival timings. These short probes
check gateway behavior; they do not prove that a full RTL response will avoid a
proxy timeout, especially if thinking or heartbeat events are buffered upstream.

Validation: all 37 selected offline checks passed (10 diagnostic, 13 pacing and
14 usage tests). Coverage includes reasoning-field summaries without text leaks,
incremental heartbeat/reasoning/content delivery, final usage counted once,
unknown usage, incomplete/malformed streams and gateways ignoring streaming.
The actual command-engine dry run retains its original message hash and 52,379
characters. Compilation and whitespace checks passed; no live calls were made.

### Configurable local timeout

`DUAL_LLM_TIMEOUT_SECONDS` sets the HTTP client timeout for dual-LLM generation,
review, merge and diagnostic calls. The default remains 180 seconds. For a local
comparison, `export DUAL_LLM_TIMEOUT_SECONDS=600` before the existing `--resume`
command; subprocesses inherit the setting. Calls print the selected timeout.
No new retries, token budgets or model changes are introduced. This does not
configure the provider's proxy timeout or its reasoning policy.

The diagnostic also accepts `--timeout 600`, overriding the environment. Its
probe.json records client_timeout_seconds, and its event-driven streaming guard
uses the selected value rather than a fixed 180 seconds. The requests timeout
is a client connect/read timeout, not a guaranteed total duration; streaming
also checks elapsed time when lines arrive. A gateway 524 can still end the
request earlier than the local timeout.

All 44 selected offline tests passed (18 dual-LLM API, 13 diagnostic and 13 pacing).
Checks cover the unchanged default, HTTP forwarding, environment/CLI precedence,
positive finite validation, and a simulated stream lasting beyond 180 seconds
with a 600-second setting. Compilation and whitespace checks passed. No live
model calls ran during this change; the longer-timeout experiment is pending.

### Optional RTL generator model for architecture builds

`run_flow.py --rtl-generator-model MODEL` selects the generator and merger for
dual-LLM modules in an architecture build, including local lint repairs. The
reviewer, specification/proposal models, lint/review acceptance checks and attempt
limits retain their existing settings. Without the flag, generation still uses
`protected.Claude-Opus-5`; the reviewer remains `protected.gpt-5.6-terra`.
This is an explicit per-invocation argument, not a global/environment override.

Legacy config/basic expansion rejects the flag; the normal expand-spec route
retains its models and routing. The internal design CLI requires an isolated
`--output-root` for the override. Deterministic FSM/timing emitters ignore model
selection and continue through their existing routes. Proposal-only exports
reject the flag because they do not generate RTL. `start_flow.sh` forwards it
when an architecture mode or input is explicitly selected.

Resume keeps exact-specification reviewed cache entries regardless of the newly
requested generator. Only missing/unusable cache entries generate with that model.
Build status records `requested_rtl_generator_model` when supplied; module traces
record `requested_generator_model`, `reviewer_model` and cache-hit state. Existing
usage ledgers record the model actually called. A cache hit does not claim that
the newly selected model produced the cached RTL. Model selection does not edit
the approved manifest or change its approval/specification/cache identities.

For the saved interrupted build, test an alternate generator without regenerating
the five reviewed modules:

```sh
python3 -u local/frontend/run_flow.py \
  /tmp/capstone-live-sjSE8O/repair/architecture.yaml \
  --spec-approval /tmp/capstone-review-tig6mI/review/approval.json \
  --build-dir /tmp/capstone-live-sjSE8O/build --resume \
  --rtl-generator-model protected.gpt-5.6-terra
```

This experiment uses the same model for both generator and reviewer. It is not
evidence that the alternate provider path avoids HTTP 524. No live calls were
made while implementing the override. Offline tests exercise defaults, CLI
guards, explicit propagation through generation/lint repair/merge, unchanged
reviewer selection, FSM routing and cache reuse. A network-disabled cache-only
check on a copy of the saved build reused all five modules, then stopped at the
missing timing RTL with zero calls; the original build was not changed.

## Clarify control contracts before another complete RTL attempt

Following the repeated command-engine 524 responses, generation guidance now
requires distinct first-creation, blocked-hold, commit and later-replacement cases,
edge timing/priorities, and payload behavior when valid is low separately from
reset. These belong in existing update_events/stalls/simultaneous_events fields;
there is no new FSM schema, fragment protocol or generation stage. Valid-dependent
payload masking must be reflected in combinational input dependencies. These are
generation instructions, not a new deterministic semantic checker for prose.

The DDR4 request preserves R1-R11 exactly and appends R12/R13. Dispatch captures
ownership without forming a candidate on that edge. Empty slots may form an
initial candidate later; blocked candidates hold; commit clears the slot and
replacement uses updated bank state at a later edge. With no candidate, active
service precedes cleanup, then pending refresh preparation; reset wins and refresh
service blocks dispatch/new formation. Internal state encoding remains free.

Candidate payload is zero NOP when invalid. commit_command mirrors that registered
candidate payload even when candidate_ok is false, while permission gates only
commit_valid. Consumers sample on commit_valid. Probe payload remains a combinational
incoming-request/bank-state query even when its validity is low; eligibility gates
validity/readiness rather than payload. Reset forces command payloads to zero NOP.
This is consistent with the audited engine's declared commit/probe dependencies
without treating every low commit_valid as a request to mask its payload.

Generator revision 9 changes future proposal fingerprints. Historical source,
architecture, approval and caches under review_20261022T171734Z are not rewritten.
The new request requires a fresh proposal and semantic review before its own
revision-bound RTL approval. Current package-wide cache keys may require generating
previously completed modules again for the new revision. Original ownership
concerns (including page cleanup coverage) must also be checked in that review.
No live API calls, RTL generation, BIST or downstream verification ran here.

Validation: 36 distinct offline checks passed (24 staged-generation, four prompt
budget/preservation, six system-link and two approval-gate regressions). The
controller transport case was rechecked with explicit R12/R13 assertions in
lowered requirements and module contexts. Existing request IDs/text remain exact;
unapproved builds and stale approvals remain blocked. Initial system reservation
is 56,029 estimated tokens including the unchanged output allowance. Compilation
and whitespace checks passed. Live compliance and latency improvement are untested.


## Collect independent system-stage repair diagnostics

The clarity_20261024T042800Z proposal exhausted its three attempts while exposing
errors that were present in its first response. System validation now collects
metadata, usable shared-definition/configuration, request-coverage and reciprocal
verification-link diagnostics before requesting a repair. Metadata entries report
independent invalid text fields together. Configuration checks identify every
fixed parameter axis and explain that it must be omitted from the matrix while
retaining its declared shared value. Existing implicit configurability through
allowed values is preserved.

Malformed root/rule schemas still stop dependent checks. Matrix elaboration runs
only after shared definitions and host types are usable, and validates its own
strategy shape independently of descriptive metadata. Candidates remain rejected
until all checks pass; no values, parameter policies or verification links are
automatically repaired. The three-attempt limit and approval boundary are unchanged.
Generator revision 10 refreshes future proposal fingerprints.

Offline replay of the saved responses reports five, three and one diagnostics,
respectively. The first includes both numeric decision values, both fixed axes,
and the missing cleanup/refresh reciprocal link. Its input remains unchanged;
an explicitly corrected in-memory copy passes system validation. Regression
coverage checks combined repair prompts/journals, malformed shared/strategy data,
stable diagnostics, strict rejection and implicitly configurable parameters.
No live API calls or RTL generation ran during this change.

Validation: all 79 selected offline tests passed: ten system diagnostic/repair
checks and 69 generation, JSON, prompt, package, diagnostic and lowering/approval
regressions. Python compilation and whitespace checks passed. Live model repair
behavior remains to be confirmed by the next fresh specification run.


## Explicit architecture response nesting

The clarity_20261024T050035Z system stage passed on its second attempt after
receiving both decision-value errors together. Architecture attempts 1/2 omitted
a closing object boundary: graph, verification and rule_owners were nested in
modules, and the outer JSON was incomplete. Attempt 3 was parseable but retained
internal_types inside modules. All calls reported finish_reason=stop.

Generator revision 11 adds a valid root-shape skeleton to the architecture prompt
and schema guide, explicitly limiting modules to contracts and placing optional
internal_types alongside graph, verification and rule_owners at the root. Repair
feedback identifies misplaced known fields directly inside modules when parseable;
invalid JSON receives object-closure guidance without inferring a repaired tree.
The skeleton remains in the stage prompt rather than being duplicated in feedback.
Raw malformed candidates remain literal, parseable candidates retain equivalent
JSON content, and journals still preserve exact raw responses. No fields are
moved automatically and no missing braces are supplied. Contract instance names
are not treated as root-field placement errors.

Offline replay confirms targeted feedback for all three saved responses. A bounded
repair regression rejects malformed nesting, then misplaced internal_types, and
accepts an explicitly corrected third architecture response while preserving all
raw journal entries. The locked system, review boundary and three-attempt limit
remain unchanged. No live API calls, RTL generation or approval ran here.

Validation: all 49 selected offline tests passed (eight prompt, seven JSON/repair,
ten system diagnostics and 24 staged-generation regressions). Existing prompt
reservation checks, Python compilation and whitespace checks passed. Live model
compliance with the clarified shape remains to be tested on a fresh proposal.

## Opt-in automated semantic review and RTL admission

The user authorized successful automated review to allow RTL generation. Added
review_specification.py and --automated-spec-review to the existing flow. Review
uses one paced/accounted call over a losslessly shared full source snapshot,
including original generated request, selected configuration and verification
intent. Its strict verdict covers six general aspects; findings require existing
snapshot JSON pointers, known requirement IDs, concrete messages and
system/architecture scope. Malformed output, incomplete coverage, findings,
provider failure or compiler-readiness blockers persist a nonpassing report and
stop before RTL. There is no automatic retry, source rewrite or repair loop yet.

Only a clean verdict plus existing compiler/readiness checks creates an automated
version-3 approval, embedding its report and package/manifest fingerprints. Approval
validation rechecks verdict/schema, protocol revision, reviewer identity and exact
revision/configuration. Explicit version-1/2 approvals remain supported. Replay
runs are labeled separately from live model review. --microarch-only can exercise
the gate without RTL, and generated requests remain intact when exporting after
review. Cache-only mode cannot silently make a live review call. Existing RTL
resume can consume the saved automated approval through --spec-approval.

The full current controller review prompt has 78,429 characters, reserving 48,598
estimated tokens including the 6,000 output allowance under the existing pacing
estimator. No live reviewer or RTL model calls ran during implementation. An
unrelated register-design fixture covers a passing review entering the existing
frontend generation/handoff route without manual approval, while rejection never
starts RTL. Fixture RTL is interface-only and establishes orchestration, not
controller behavior. Automatic semantic repair routing remains the next bounded
step after observing the live review report. Usage and estimated costs use the
existing frontend ledger and user-supplied model rates.

Commands, artifact formats and remaining limits: [automated review](automated_review.md).

Validation: all 76 selected offline tests passed (17 new semantic-review/gate
checks and 59 lowering/approval, resume, handoff and generation regressions).
Python compilation, CLI help and whitespace checks passed. Live model semantic
review effectiveness and the controller RTL build remain untested by this change.


## Automated review reference-coordinate repair

The first live reviewer call returned changes_required with three substantive
findings (maintenance candidate creation, absent probe observations and the
A_PROFILE readiness blocker), but its verdict failed reference validation. The
compressed prompt contains a context transport wrapper; the initial review
instructions did not explicitly distinguish that wrapper from expanded snapshot
coordinates. Fourteen references used /context prefixes and one incorrectly
placed timing_library under package. The reviewer also scoped the missing
shared-event path as architecture-only; system changes cannot be made in that
locked stage. The saved call cost $0.067156 with 19,548 provider-reported tokens.

Review revision 2 explicitly explains shared-definition expansion and snapshot
root pointers, including the library/package distinction, and clarifies scope
when shared event conditions or expected results need correction. Independent
reference errors are now collected across findings. Failed reports retain the
parsed verdict as well as raw response.json, with all validation diagnostics in
failure.json; none of those findings can authorize RTL. No automatic prefix
stripping, path guessing, source change, verdict approval or paid retry occurs.

All 19 offline gate tests pass, including forced compressed context, lossless
expansion, aggregated reference errors, retained failed findings, stale approval
rejection and automatic admission on a valid unrelated register fixture. Replay
of the saved verdict reports all 15 bad references; an in-memory prefix-only
correction still exposes the invalid library path. Original live artifacts are
unchanged. Compilation and whitespace checks pass. Live compliance with revision
2 remains untested; a retry must use a fresh review output directory.


## Live semantic review revision 2 checkpoint

The automated_review_v2 call on clarity_20261024T055520Z returned a schema-valid
changes_required verdict with existing snapshot references and correct system
scope for the maintenance candidate-creation condition. Two system findings
cover candidate creation for maintenance and A_PROFILE review status; two
architecture findings cover probe observations and close-page cleanup evidence.
The gate correctly withheld approval and returned nonzero. This is a completed
review that rejected its input, not an API/format failure. Usage was 19,899 tokens
and estimated $0.069988 for one call. No RTL ran.

Automatic repair routing remains next. Because system findings are present,
repair must return to elaboration and regenerate decomposition before reviewing
the new revision. Verification evidence suggestions should prefer existing
ports/events and independent inference; an LLM request for visibility does not
by itself mandate exposing internal state. Profile-path resolution is already
compiler-checked; authoritative library provenance and unresolved review status
must be distinguished, rather than silently converting an external assumption
into accepted user agreement. Original request constraints remain authoritative.

## One deterministic semantic repair cycle

Added repair_specification.py to consume a validated changes_required report for
the exact generated manifest/configuration. Any system finding selects system
elaboration followed by fresh decomposition; architecture-only findings use the
original system projection without changing it. The original request, selected
configuration and parent timing library remain inputs. Semantic findings persist
through existing compiler-error repairs. The new generation context binds the
parent manifest/report identities, so its approval belongs to the new revision.
No existing source/export, request or approved cache is overwritten.

The standalone command avoids repeating the initial review when a valid report
already exists. In run_flow, --auto-spec-repair opts into one cycle with
--automated-spec-review. Only a validated rejection enters repair; transport,
format and reference failures stop. A fresh source/export must pass existing
compiler/matrix checks and another semantic review before the usual automated
approval can authorize RTL. Remaining findings stop with saved status/reports;
there is no second automatic semantic cycle. Existing compiler loops remain up
to three attempts per generated stage. No agent framework, tool-selection loop,
dictionary registry or RTL fragment generation was added.

Repair cannot remove, rewrite or resolve original requires_review assumptions or
non-RTL blocking questions. Readiness-only failures without semantic findings
stop for disposition rather than making a pointless generation call. This
preserves actual missing agreement; it does not classify library provenance or
convert compiler-checked facts into accepted external assumptions automatically.
The current controller's A_PROFILE therefore remains blocking until disposition.

Validation: all 103 selected offline tests passed: 12 new repair-route checks,
19 review-gate checks and 72 generation, links, prompt, JSON, lowering/approval and
resume regressions. Tests use an unrelated register pipeline and injected model
responses. They cover system-priority routing, a genuinely updated system reaching
fresh decomposition, unchanged system/library/request on architecture-only repair,
selected-configuration preservation, external agreement protection, stale reports,
compiler retry limits, semantic findings surviving retries, second-review rejection,
new revision approval and preservation of the original proposal-only export.
These are orchestration checks, not live LLM repair effectiveness or DDR4 behavior.
The saved controller report selects system routing with all four findings retained.
Compilation, CLI help and whitespace checks passed. No paid repair or RTL ran.


## Fresh decomposition formatting and prerequisite diagnostics

The repair_20261024T072406Z live system repair passed immediately and declared
separate active, cleanup and refresh candidate creation. Decomposition exhausted
three compiler attempts on formatting errors already present in the first reply:
eight string owned_state fields, eight string simultaneous_events fields, and a
nested graph.clock_reset.source object instead of clock_source/reset_source.
No final semantic review ran. Four calls cost an estimated $0.535350.

Generator revision 12 distinguishes fresh decomposition after system repair from
repairing an existing architecture candidate. The first fresh decomposition keeps
the stage's worked format example and receives a FRESH DECOMPOSITION instruction;
it does not claim a candidate exists. After a rejection, the candidate supplies
format and the repeated example is omitted as before. Both modes retain the full
request, locked system, schema, library, semantic findings and prompt-reference
expansion instructions. Guidance explicitly specifies mapping/list field shapes
and flat graph clock/reset source keys. Common instruction wording was shortened
without removing behavioral inputs or increasing the default token allowance.

Contract prerequisite validation now gathers independent malformed fields across
contracts; graph clock/reset shape is checked independently in that same phase.
Relational ownership/dependency/graph checks remain blocked while prerequisite
shapes are unusable. No string-to-mapping conversion, nested-source rewrite or
permissive acceptance is added. Offline replay of saved architecture replies now
reports 17, nine and one diagnostics respectively, without changing the inputs.

Regressions cover missing/malformed contracts without cascading exceptions,
combined contract/clock errors, stable nonmutating reports, and a fresh architecture
rejection transitioning to candidate repair with every diagnostic in its next
prompt and raw journal. The real repaired-system first decomposition prompt was
captured offline with its format example; its reservation remains below 80,000
estimated tokens including output. No live API retry or target RTL ran here.

Validation: all 126 selected offline checks passed: 31 focused prompt/schema/
semantic-repair checks and 95 generation, package, lowering/approval, system-link,
JSON, review-gate and resume regressions. Python compilation and whitespace checks
passed. The three-attempt compiler limit and one semantic-cycle limit are unchanged.
Live generation compliance with the clarified format still requires a new run.

## Explicit verification-only outputs and repair wiring preservation

Generator revision 13 adds optional graph.verification_outputs, a mapping from
child output endpoints to nonempty reasons. The port must be present, have no
functional consumer, and be referenced by an observation actually used by a
validated verification check. Observation aliases alone are insufficient. Top
ports, inputs, nonexistent/absent ports, connected outputs and invalid binding
references cannot receive this exemption. Ordinary undriven sinks, unused top
inputs and undeclared unused outputs remain errors.

For example, a design can declare:

```yaml
verification_outputs:
  stage.monitor: Observe registered validity without a functional consumer.
```

The existing contract must declare that output and its accurate dependencies;
the verification document must bind the actual instance port and use its alias
in a check. Lowering translates the explicit declaration into the architecture's
existing unused_outputs record. The structural wrapper keeps the child output
port unconnected; no dummy consumer, top-level output or functional connection is
invented. The verification handoff retains its typed instance-port observation.
Existing lint policy is unchanged.

Binding validation now runs independently of ownership-reference success, before
granting graph exemptions. Invalid observed types and malformed source obligation
maps report specification errors rather than runtime exceptions; missing check
coverage identifies the missing/unknown obligations. Old exports without the
optional declaration retain their prior lowered structure and remain valid.
Repair feedback explicitly preserves unrelated valid connections and updates
references when ports change. It does not infer or restore missing functional
edges automatically; those remain hard validation failures.

The saved repair_20261024T090357Z attempt 2 exposed monitor ports already named
in observations but no explicit observation-only declaration. An in-memory copy
with declarations reveals an additional missing V_IDENTITY check alongside its
three ownership errors. It remains rejected: incomplete verification cannot be
used to authorize exemptions. The saved source/export/caches were not changed,
and the current existing architecture still validates without migration.

Focused regressions use an unrelated register pipeline. They cover explicit
intent, checked observation use, bad/absent endpoint types and packed fields,
functional driver preservation, ownership-independent binding evidence, lowering,
wrapper generation and portable handoff. A constant-output interface fixture
also passes actual integration lint with an intentionally unconnected monitor;
this checks connectivity, not behavior. Prompt reservation checks still pass the
existing 80k test target after removing redundant instruction wording. No live
model calls, controller source repair or behavioral verification ran here.

Validation: 176 distinct selected offline checks passed across the focused and
compatibility runs. These include 18 new observation-only output checks, nine
prompt checks, nine diagnostic checks, ten link checks and 130 existing package,
generation, lowering, handoff, architecture, semantic repair/review and JSON
regressions. One wording regression in the locked-system repair instruction was
corrected; all 24 generation checks and the prompt suite were rechecked afterward.
The new behavior also has actual interface-fixture integration-lint coverage.
Compilation and whitespace checks pass. No paid calls ran; live generation may
still require ownership, verification-coverage and readiness repairs.

### Architecture compiler retries with explicit edits (generator revision 14)

The saved repair_20261024T173124Z fixed a duplicate driver, then a missing check
events field, but its last full rewrite dropped a previously correct readiness
connection and the explicit verification-only output declarations. Compiler
retries now edit the last parseable supported architecture candidate. The first
architecture response, semantic repair regeneration, and system-stage behavior
retain their existing response formats. Malformed initial architecture JSON
still requests a complete corrected response.

The edit envelope contains base_fingerprint and 1..100 edits. Each edit uses
set (path/value) or remove (path), with RFC 6901 object-field paths restricted to
architecture roots. Parents must exist; lists are replaced as a whole. This is
a small local protocol, not a full JSON Patch implementation. Application is
atomic on a private copy. Invalid edits retain the prior candidate; successfully
applied edits that fail compilation become the next candidate. No functional
wiring is inferred, no output exemption is automatic, and every result passes
the unchanged matrix/compiler validation before publication. The existing three
attempt limit remains. Journals retain the raw edit response, base fingerprint,
and complete applied response for audit. Existing source packages and exports
are not rewritten.

Offline regressions exercise exact field preservation, escaped pointer keys,
list replacement, atomic rejection, wrong fingerprints, locked root rejection,
invalid edited candidates, malformed initial JSON, attempt exhaustion, and the
real compiler/semantic-repair routes on the unrelated register fixture. No paid
API calls or controller-specific source edits are part of this change.

Validation: all 54 selected offline tests passed in one run (eight edit-protocol
regressions, 24 generation checks, nine prompt checks and 13 semantic-repair
checks). Python compilation and git diff --check also pass. Live model adherence
to the edit protocol remains to be tested with a fresh bounded repair run.

### Diagnostic audit, section 1: verification-output declarations

The live repair_20261024T232854Z used explicit edits successfully: its second
architecture reply cleared the eight reported ownership/dependency/driver
errors. However, the original candidate also declared 13 functionally connected
outputs as verification-only. The validator stopped on the first declaration;
the final edit removed that one and then encountered the next. These conflicts
were already present, rather than introduced by the edits.

Declaration validation now collects independent endpoint-kind, connected-output,
and missing-checked-observation errors in deterministic endpoint order. Connected
output feedback explicitly requests removal of the declaration while retaining
the functional connection and verification observation. No automatic repair or
exemption is added. Shape, endpoint/connection and binding prerequisites remain
unchanged; coverage/cycle checks still wait for usable declarations. Other graph
fail-fast checks have not been broadened in this section.

testdata/verification_output_conflicts.json is a graph-diagnostic projection of
the saved second architecture reply, with bindings elaborated before projection.
It is deliberately not a complete specification or approval fixture. Tests report
all 13 conflicts without mutation and show that explicitly removing those
declarations preserves connections/observations and passes graph validation
(41 edges), retaining the genuine observation-only candidate_source output.
This does not establish full compiler validity or semantic approval.

All 39 focused offline tests pass: 22 verification-output checks, nine diagnostic
checks and eight edit-protocol checks. Two older diagnostic retry fixtures were
updated to the existing explicit-edit protocol, retaining their multi-error and
bounded-failure assertions. Compilation and whitespace checks pass. No paid
calls, source-package changes, retry-limit changes or resume changes ran here.

### Diagnostic audit, section 2: functional connections

Once graph shapes, port definitions, presence and clock/reset prerequisites are
usable, connection validation now collects independent unknown-endpoint,
malformed/empty sink-list, partial conditional-presence, wrong-direction,
type/width and duplicate-driver errors across connections. Invalid endpoints do
not reach port lookup; invalid lists are not traversed; wrong-direction edges do
not produce type errors; only otherwise usable edges participate in duplicate
driver checks. Stable diagnostics are deduplicated without mutating the input.

Any connection errors stop graph validation before verification-output use,
missing/unused coverage or dependency-cycle analysis: the partial graph cannot
support reliable conclusions for those checks. Once connections are valid, the
existing coverage check reports all missing sinks and unused sources together.
No wire is inserted, removed or chosen automatically. Earlier port/clock/reset
prerequisites remain fail-fast; other bookkeeping and generation stages are
outside this section.

Seven new regressions cover simultaneous edge faults, malformed sink lists,
repeated unknown endpoints, fully absent versus partially present conditional
edges, coverage after valid edges, and a replay of the extra dispatch_request
driver from the saved 232854Z first architecture reply. That extra edge is
restored onto the existing second-reply graph projection; removing it exposes
the expected 13 declaration conflicts without altering other connections.
The mixed-error test also verifies that dependency/cycle analysis is deferred
and repeated validation preserves both diagnostics and the candidate.

All 46 focused offline tests pass (seven graph, nine diagnostic, 22
verification-output and eight edit-protocol checks), including the existing
interface-fixture integration lint. Python compilation and whitespace checks
pass. No paid calls, source package mutations, retry or resume changes ran.

### Ownership bookkeeping, section 1 (generator revision 15)

Architecture generation previously repeated the same relationships in
rule_owners, contract invariants, and contract verification reference lists.
rule_owners remains the model's explicit architectural choice. Generated
contracts may now omit either reference list; assemble_package derives missing
lists from the declared owners and locked rule-to-obligation links, with sorted,
deduplicated, fully qualified references for every named owner. The architecture
prompt/example requests omission. Generated source packages still contain both
lists for existing validators, lowering, review and handoff consumers.

Explicit lists, including empty or malformed lists, are never overwritten or
merged: existing strict checks reject inconsistent references. Missing/empty or
unknown owners remain failures. No owners, rules, obligations, scenarios,
expected results, observations or checks are invented. Deriving these lists
establishes reference consistency only; the model and semantic reviewer still
must establish that the selected owner's actual behavior fulfills its rules.
Raw responses/journals retain omitted fields as received. The accepted locked
system is unchanged; old generated responses and hand-authored source packages
retain their prior validation. Generator identity changes invalidate proposal
reuse against the old prompt. No schema field or manifest approval migration
is introduced.

All 113 selected offline checks passed in one consolidated run: 11 new ownership
derivation, 24 generation, nine prompt-budget/format, nine diagnostic, ten system
link, 13 semantic-repair, eight explicit-edit, seven graph diagnostic and 22
verification-output checks. An instruction-budget regression was caught during
development, wording shortened, and the consolidated suite rerun successfully.
Tests cover multiple owners, scoped references, de-duplication, changed owners,
strict legacy lists, missing checks, raw evidence and actual package compilation.
A read-only reconstruction of clarity_20261024T055520Z derives exactly the
original reference sets for all eight contracts and passes structural validation;
it does not approve the source or modify its files. Compilation and whitespace
checks pass. No paid API calls or target RTL generation ran.

This section does not change system-stage reciprocal rule/obligation references,
functional graph construction, retry limits, specification checkpoints, timing
facts or external readiness decisions. Those remain separate audit work.

### System verification bookkeeping, section 2 (generator revision 16)

Each system/execution rule still explicitly chooses its nonempty local
verification IDs. Generated obligations may now omit the reverse rules list;
validate_system derives that missing field from references in the same slice,
on its private canonical copy. The prompt and system-stage format example
request omission. Sorted reverse references are present in the locked system
and published source, so ownership derivation, existing strict validators,
semantic review, lowering and handoff retain their prior canonical format.

Explicit reverse lists are never replaced, including empty, malformed or
nonreciprocal lists. Missing obligations, orphan obligations, missing/empty rule
coverage and cross-slice references still fail. The compiler does not infer
coverage from prose or invent rules, obligations, scenarios, expected results,
checks or observations. Status remains planned_not_executed. Raw model responses
and journals preserve the omitted fields; hand-authored/published source still
requires both directions. The generator identity changes to bind new proposals
to this prompt/compiler behavior; old source validation remains unchanged.

New offline regressions cover many-to-many references, local verification-ID
scope, strict explicit lists, required coverage and behavioral intent, source
validation, raw evidence and both bookkeeping derivations together through real
package compilation. A read-only reconstruction of the saved controller matches
all 26 original reverse-reference sets and preserves scenarios, expected results
and status. This establishes bookkeeping equivalence, not behavioral approval.
No source package is edited and no paid API or target RTL run is performed.

All 124 selected offline tests passed in one run: 11 new system-link derivation,
11 ownership derivation, 24 generation, nine prompt, nine diagnostic, ten legacy
system-link, 13 semantic-repair, eight explicit-edit, seven graph diagnostic and
22 verification-output checks. A new many-to-many test initially overmatched
unrelated constraint obligations; its assertion was narrowed to the deliberately
shared obligations before the consolidated passing run. Python compilation and
whitespace checks pass. Retry limits, readiness/approval policy and resume
behavior are unchanged; live end-to-end repeatability remains to be established.

### Semantic review context: current accepted evidence (review revision 3)

repair_20261025T214055Z completed system generation, bounded architecture edits,
matrix validation and compilation. Its semantic reviewer then incorrectly
reported missing host response connections that were already present in the
accepted graph. The finding cited generation.validation_repairs/2, a historical
diagnostic from the rejected second architecture attempt. External timing-profile
provenance remained a separate legitimate readiness blocker.

review_snapshot now creates a private review view omitting only
package.generation.validation_repairs. The prompt states that review concerns
the current accepted artifact. Finding references resolve against this same
view, so audit-history paths cannot support current findings. All other content,
including current definitions, original request, configuration, assumptions,
questions and timing data, remains available at the original snapshot coordinates.
Complete saved snapshots and their full artifact/package fingerprints remain
unchanged. No findings are silently discarded, no current semantic claim is
automatically adjudicated, and readiness still blocks approval independently.

Review revision 3 binds the new evidence boundary. New reports/approvals use the
existing report schema; old review reports require a fresh review before use in
automated approval or repair. Existing manual approval behavior is unchanged.
The next paid check can review the saved accepted export directly, rather than
regenerate its system/architecture. This change does not resolve A_PROFILE or
Q_PROFILE_PROVENANCE or claim behavioral verification.

testdata/review_history_context.json preserves a review-context projection of the
actual current graph, assumptions/questions, rejected-attempt diagnostics and
stale finding. Tests confirm that response wires remain in the prompt, historical
errors disappear, historical references fail validation and source artifacts
remain unmodified. Complete unrelated register fixtures test full fingerprint
binding, stale approval rejection and external readiness blocking. All 37
selected offline tests pass (24 semantic review, 13 semantic repair), along with
Python compilation and whitespace checks. No paid calls or source-package edits
ran during this change.

### Resolution-aware repair routing (review revision 4, generator revision 17)

The saved 220801Z re-review contains two architecture defects and a system-scope
external profile-source blocker. Routing solely by scope would regenerate the
system even though regeneration cannot resolve that agreement. Findings now
require resolution=design_repair or external_agreement. The reviewer is asked to
separate mixed concerns. External findings require system scope and a pointer
to an existing blocking decision, requires_review assumption or non-RTL blocking
question, or its fields. References still resolve against the current review
view. No keyword heuristics or retroactive report rewriting classify findings.

plan_repair chooses scopes from design findings only. It records external_findings
and readiness_error separately, with the full input report retained. External-only
or readiness-only cases stop before repair output or generation calls. Mixed
architecture/external cases keep the accepted system; real design system defects
still select system regeneration. Repair feedback explicitly excludes external
findings from repair instructions. The new revision still goes through compiler,
semantic review and independent readiness checks; external findings never allow
approval. Blocking decisions now have the same exact-preservation guard already
used for original external assumptions/questions during system regeneration.

Reports require fresh protocol-4 review; protocol-3 artifacts are preserved and
cannot be silently relabeled or reused. Manual approval behavior and existing
attempt limits are unchanged. Generator identity advances for the added decision
preservation guard. Current profile agreement remains unresolved.

All 53 focused offline tests pass (27 review, 17 semantic repair, nine prompt).
New cases exercise invalid/missing classification, real blocker references,
external-only early stop, mixed architecture routing, design system precedence,
preserved blockers and denial of approval after a clean model re-review.
An explicitly synthetic, unsaved classification of the actual 220801Z findings
confirms architecture routing with two design findings and one external finding;
the original old-protocol report is correctly rejected. This audit is not new
review evidence or approval. Compilation and whitespace checks pass; no paid
calls, source/export mutations or target-specific architecture fixes ran.

### Approval-policy classification clarification (review protocol 4 unchanged)

The live 223610Z review correctly reported external timing-profile provenance,
but also classified normal draft/RTL approval as external_agreement. Validation
correctly rejected that circular prerequisite. The prompt now explicitly states
that this authorized automated review supplies the normal approval gate after a
clean verdict and compiler readiness checks. Draft status and rtl_generation
questions alone are neither design defects nor external-agreement findings.
Eligible external-blocker paths are rendered using the validator's existing
metadata eligibility helper, so classification instructions and checks agree.

No metadata is removed from the view to hide the approval question. No reviewer
findings are filtered or rewritten, no validation or readiness check is relaxed,
and no schema/protocol migration is added. The same standalone re-review command
can be rerun with a fresh output directory; true profile blockers still prevent
approval. Live model adherence remains to be tested.

testdata/review_approval_classification.json preserves the actual saved findings
and a projection of current package metadata and selected timing-library data.
New tests check prompt policy and eligible paths, retention of the normal gate,
acceptance of the profile finding, strict rejection of the approval-gate finding,
and approval of an unrelated clean draft whose only question is the normal RTL
gate. The prior history-context projection includes an empty decisions map to
support the metadata-based eligibility list; its historical findings are unchanged.
All 47 selected offline tests pass (30 review, 17 semantic repair), plus Python
compilation and whitespace checks. No paid API calls or source/export edits ran.

### Repair preservation, segment 1: offline change evidence

The saved 232303Z system repair fixed pending-refresh dispatch eligibility but
also required pending low for active and cleanup candidate creation. That
restriction contradicts the unchanged drain requirement and was caught by the
post-repair semantic review. An offline audit now records all 24 differences
between canonical parent/repaired system projections, including three removed
type-description fields. Those removals are collateral documentation changes;
the audit does not assert they cause an additional functional failure.

microarch/reviews/repair_20261025T232303Z_change_audit.md explains the evidence;
the adjacent JSON retains exact changes and source fingerprints. The focused
testdata/repair_preservation_audit.json fixture preserves twelve changed fields,
initial findings and the final deadlock finding. It is not a complete design or
an executable behavioral model. The manually reasoned active/cleanup deadlock
examples are documented separately from executed-test evidence.

specification_change_audit.audit_changes is a pure offline JSON comparison:
deterministic RFC 6901 object paths, explicit add/remove/replace records, atomic
list comparisons and copied evidence. It handles JSON type distinctions and
does not mutate inputs or parse arbitrary natural-language event semantics.
Tests cover the saved changes, removed fields, narrowed illustrative edit scope,
path escaping, list/type distinctions and evidence isolation. The illustration
is not an approved/generated repair. No live flow invokes this helper yet.

This segment changes no prompts, repair routing/application, review admission,
source packages, retry limits or RTL. Bounded system edits and repair-focused
review remain separate future segments; no paid calls ran.

All 16 selected offline tests pass (eight new audit, eight existing edit-protocol
regressions), along with Python compilation and whitespace checks. Parent and
repaired exports were inspected read-only; audit outputs grant no approval.

### Repair preservation, segment 2: bounded system semantic edits (generator 18)

System semantic repair now begins with a fingerprint-bound edit envelope against
the accepted parent instead of a regenerated full system. The shared atomic edit
implementation retains architecture behavior and adds a system entry point for
nested package/shared/system/execution fields. System set operations cannot
replace existing nonempty objects wholesale; edit their fields instead. Lists
remain atomic, parent paths must exist, and the existing 1..100 edit limit applies.
No DDR4-specific condition, automatic wire fix or natural-language rule is added.

Successful application is still followed by system validation and protected
request/agreement checks, then fresh architecture decomposition and complete
package/compiler/final semantic review. Applied candidates rejected by ordinary
compiler checks become the next base, as with architecture edits. Invalid edits
retain the prior base. Rejected request-text/origin and external-agreement
mutations also retain their prior base; the accepted parent remains the authority
for those checks. System prompts do not pass a rejected system as a locked
context. The three-attempt limit and one semantic cycle are unchanged. Complete
rewrites in system edit mode fail; fresh system generation and its compiler-repair
format remain unchanged. Malformed system-edit output does not trigger a rewrite
fallback.

Journals retain raw edit responses, base fingerprints, applied complete responses
and deterministic applied_changes from the offline audit helper. Unmentioned
definitions no longer disappear merely because a complete replacement omitted
them. This does not prevent incorrect explicit edits; semantic review and future
repair-focused preservation review remain necessary. Confirmed-finding carry-forward
is not implemented in this segment.

New offline tests exercise narrow dispatch edits on the saved fixture without
changing active/cleanup conditions or type descriptions, and drain-condition
edits without losing the repaired dispatch restriction. These are scope checks
on recorded text, not controller simulations. An unrelated register package
exercises real compilation, explicit source evidence, compiler rejection/retry,
stale bases, complete-rewrite rejection, request-text protection and bounded
failure. Existing external-blocker tests now supply actual edit envelopes via a
test-only translation of old fixture intentions; production does not translate
whole responses. No source/export artifacts or paid calls are changed here.

All 74 selected offline tests pass in one run: eight system-edit, 17 semantic
repair, eight architecture-edit, eight audit, nine prompt and 24 generation
checks. Python compilation and whitespace checks pass. Live model adherence and
end-to-end behavioral convergence have not yet been established by this change.

### Repair preservation, segment 3: explicit final-review obligations

repair_and_review now builds a target-bound repair_review_context.json from the
accepted parent, validated input review's design findings and compiled repaired
export. It records full parent/target fingerprints, parent report identity and
deterministic changes to current definitions/metadata. Generation provenance is
excluded from the comparison, avoiding historical compiler diagnostics and hash
churn as defect evidence. The current snapshot still includes its authoritative
request, assumptions and configuration. External agreement remains independently
blocking and is not a design-repair obligation.

The existing single final review call receives clearly separated parent findings
and before/after changes. It must assess every original finding exactly once and
check progress, ordering, priority, reset and other affected cases for preservation.
Original messages are not automatically treated as defects in the target. Each
resolved/unresolved assessment needs explanation and current snapshot references;
unresolved entries must link nonempty current design finding IDs. Preservation
is preserved or regressions_found with current evidence and explicit links to
reported regressions. Normal current findings remain required for any open
defect/regression, so a later repair receives them even if their IDs change.

The standard review contract is unchanged when there is no repair context. With
context, repair_assessments and preservation_assessment are required verdict
fields. Reports/automated approvals retain the context; pass-report validation
rechecks target binding and assessments. Missing/duplicate/unknown assessments,
bad evidence, stale contexts and unreported open links fail rather than silently
drop obligations. A standalone --repair-context option retains these obligations
for an explicit re-review of the same target. Existing input review reports remain
usable; the optional repair context has its own version-1 shape. Review protocol
4 and ordinary admission policy are unchanged.

This carries findings from the selected input review only, not a global registry
or automatic merge of independent earlier reviews. It does not certify the
model's semantic judgments. There is no extra LLM call, semantic cycle or retry.
Known external blockers still deny approval even if all design assessments pass.

All 65 selected offline tests pass: ten repair-review checks, 17 semantic repair,
eight system edits and 30 standard review checks. Coverage includes omitted/open
findings, explicit current links, preservation regressions, stale contexts,
approval revalidation, persistence into the next repair plan and standalone replay.
The saved dispatch-fix/drain-deadlock projection demonstrates that the requested
fix may be assessed resolved while a preservation regression still blocks the
verdict; this is protocol/evidence testing, not an executed controller model or a
live model-recognition test. Compilation and whitespace checks pass. No paid API
calls, source/export edits or target RTL generation ran during this segment.

### Repair-review prompt budget (October 27, 2026)

The live run `/tmp/capstone-live-sjSE8O` completed generation and one semantic
repair, but its final review was blocked locally: the estimated reservation was
97,133 tokens against the 80,000 limit. That final review made no API call.
The accepted repair and its 237-change audit remain available for standalone
review; restarting generation is unnecessary.

Repair prompts now use lossless `snapshot_after_v1` change rows. A replace/remove
row contains `[op,path,before]`; an add row contains `[add,path]`. For additions
and replacements, the exact after value is already at that path in the current
expanded snapshot. Whole before values, including lists, remain literal data.
Entries whose after value does not exactly match the snapshot, or whose shape
cannot be represented, remain complete objects. Instructions explain reconstruction
and independent expansion of the two shared-definition contexts. Saved audit
contexts, report/approval schemas, fingerprints and review obligations are unchanged.
No evidence is summarized, dropped or treated as behavioral verification.

The actual saved prompt now reserves 79,512 tokens, including the unchanged 6,000
output allowance. Offline reconstruction recovers every saved change and original
finding exactly. All 69 focused repair-review, standard review, semantic repair
and context-sharing tests pass. Future larger inputs may still exceed the limit;
the pacing gate remains active. No paid calls, approvals or controller RTL builds
were performed for this change.

Retry only the final review, retaining the original repair obligations:

```sh
RUN_DIR=/tmp/capstone-live-sjSE8O
REVIEW_DIR="$(mktemp -d /tmp/capstone-review-XXXXXX)/review"
python3 -u local/frontend/review_specification.py "$RUN_DIR/repair/architecture.yaml" \
  --repair-context "$RUN_DIR/repair/repair_review_context.json" \
  --output-dir "$REVIEW_DIR"
# Only after this produces approval.json:
python3 -u local/frontend/run_flow.py "$RUN_DIR/repair/architecture.yaml" \
  --spec-approval "$REVIEW_DIR/approval.json" --build-dir "$RUN_DIR/build"
```

### Live frontend completion with alternate RTL generator (October 27, 2026)

The user-run resume of `/tmp/capstone-live-sjSE8O/build` with
`--rtl-generator-model protected.gpt-5.6-terra` completed. Five earlier reviewed
modules were reused with zero calls. GPT generated timing, memory and refresh;
timing/memory passed the first review, and refresh passed after the existing merge
and second-review path corrected immediate reset gating of pending. The existing
reviewer remained GPT. This is a mixed-generator build, not regeneration of all
eight modules with GPT.

All eight modules now have reviewed RTL, integration Verilator lint passed, and
the RTL verification handoff was exported. A separate local invocation of
`verification_handoff.py` validated the exported handoff and its identities.
The user-run usage ledger is `20261027T060845Z_021d298c1a6f`: eight calls,
131,402 input tokens, 11,608 output tokens, estimated cost $0.402100 for that
resume only. Earlier generation/review/probe costs are additional.

No 524 occurred in this resume. This establishes a successful alternative for
this build, not a root-cause diagnosis or a guarantee against future gateway
timeouts. The final-review approval is at
`/tmp/capstone-review-tig6mI/review/approval.json`; build status records RTL and
integration lint passed, behavioral verification not_run and consumer
adapter_required. Behavioral/UVM/vector verification and backend execution did
not run. The assistant's follow-up validation made no LLM/network calls.

### Local executable simulation runner (October 27, 2026)

`simulate_handoff.py` now consumes authenticated RTL-phase handoffs in a
standalone local Verilator flow. See the [runner documentation](../simulation/README.md)
for its explicit `ddr4-fifo-v1` profile, supported configurations, commands,
evidence format and limits. It freezes validated inputs, generates a declared-port
testbench and evaluates pre-edge traces using an independent Python FIFO,
ownership, memory, row and timing scoreboard. It neither modifies the approved
RTL nor invokes legacy simulation/orchestration, LLMs, remote verification or
backend execution.

The saved completed build passed five scenarios: reset-zero reads, FIFO/memory,
queue pressure, traffic through refresh, and reset with outstanding work.
`/tmp/capstone-simulation-validated/simulation_report.json` records 70,843 total
cycles, 222 checked responses, three REF services, pending-refresh active drain,
post-refresh admissions, and five requests canceled by reset. Simulation evidence
is tied to the original handoff/manifest/RTL identities; original build status
and the legacy consumer's adapter-required metadata remain unchanged.

All 24 runner/handoff regressions pass, including a real compiled Verilator
driver test, hand-written independent traces, corrupted read/dispatch data,
early timing commands, row/candidate/reset faults, unsupported/private bindings,
input integrity rejection and nonvacuous scenario coverage. The executed smoke
scenarios are behavioral evidence for those checks, not exhaustive verification,
isolated module testing, all-configuration coverage or requirement closure.
Reports retain every source requirement/obligation as unverified and separate
scenario results. No on-chip BIST hardware is inserted and no network calls ran.

### Review reachability and regression attribution (October 27, 2026)

The first live plain-text run `20261027T065112Z_0766bc80` stopped after its one
semantic repair. Initial review requested broader declared observations and
readback instrumentation for the blocked-command check. Final review assessed
that finding resolved, but then alleged that missing response-slot feedback to
the engine permits a READ to retire while the previous response is valid.
Readiness passed; this was a schema-valid semantic rejection, not transport,
formatting or token-budget failure. No RTL was generated for that run.

The saved contracts forbid same-edge replacement dispatch, require candidate
formation on a later edge, and deliver/clear a READ response at E+1. A manual
edge analysis therefore places replacement dispatch no earlier than E+1,
candidate formation at E+2, and its commit at E+3. The missing wire alone does
not demonstrate an occupied-slot READ. The cited system response rule and engine
interfaces were already present before repair, so labeling that concern an
introduced preservation regression is also unsupported by those differences.
This is a contract interpretation, not executed target RTL or automatic approval.

The generic review prompt now requests reachable edge sequences accounting for
latency, ownership and bubbles, recognizes schedule-enforced safety, and asks for
the precise ambiguity when reachability is unspecified. Pre-existing current
defects still block approval but are distinguished from introduced regressions.
No findings are filtered, no guard wires are automatically added, no verdict or
approval is rewritten, and no schemas, protocol revision or retry limits change.
A fresh standalone review can consume the saved repaired manifest and original
repair-review context without regenerating the design.

`testdata/review_reachability_context.json` preserves the relevant parent/current
contracts and both findings as an explicitly incomplete projection. Tests retain
the current safety rule in the prompt, confirm the pre-existing rule/interface,
and exhaust a manually encoded small serialized-response abstraction with stalls
and progress. The abstraction does not parse arbitrary prose or certify the
actual controller. Existing review/repair gate regressions remain applicable.
The saved large repair prompt still fits at 79,719/80,000 estimated tokens;
this run's repaired review reserves 48,027. No paid calls or source-export edits
were performed while investigating this rejection. Architecture-only semantic
repair still starts with a complete decomposition: this run produced 69 audit
changes for its narrow observation finding. Reducing that rewrite churn is
separate from the reachability clarification above.

### Review reference reliability — change 1 (October 27, 2026)

Semantic review now labels current snapshot evidence with deterministic reference
IDs. IDs identify paths; the index fingerprint also binds the snapshot values.
The lossless prompt view includes labels at rules, events, contracts, verification
entries and package decisions. Repair change locations can use an ID plus a
path suffix to keep the complete before/after evidence within the token budget.
Source snapshots and saved repair contexts are not edited by this transport.

Reports retain canonical JSON pointers and review revision 4. Existing pointer
responses, approvals and handoffs remain supported. New reports record the raw
verdict, index fingerprint and any reference corrections. Approval validation and
repair admission replay that evidence and reject substantive disagreement with
the canonical verdict.

Validation collects invalid references across findings, repair assessments and
preservation assessments. An otherwise valid verdict may receive **one**
reference-only correction attempt. Its response must replace exactly the invalid
individual entries with supplied IDs. It cannot edit findings, scope, messages,
verdict, explanations, links or list lengths. The corrected verdict must pass all
normal checks, including external-blocker eligibility. Semantic/schema/transport
failures do not trigger this attempt; more than 100 invalid entries also stop.
Failed correction preserves the original verdict and all reference diagnostics.
There is no semantic reconsideration or approval override.

Review directories include `reference_index.json`; correction attempts also save
`reference_errors.json`, the correction prompt/raw response and its result.
Offline `--response` replay never silently makes a correction call. An explicit
offline correction can be supplied with `--reference-response corrections.json`,
whose exact format is `{"corrections":{"/findings/0/references/0":"R..."}}`.
Keys select invalid verdict entries; values must exist in the current reference
index. The correction is rejected if it changes valid entries or leaves any
reference unresolved. Initial review and correction have separate usage labels
and output caps of 6,000 and 4,000 tokens respectively.

Offline replay of the saved refresh-review failure collected all three invalid
paths, corrected them to the existing system/execution rules, and retained
`F_REFRESH_TIMEBASE_MAINTENANCE` with `changes_required`. No approval or RTL was
created. This demonstrates protocol recovery, not adjudication of that finding.
The previous completed controller's approval and RTL handoff still validate.
The saved large/current repaired prompts reserve 79,177/48,900 estimated tokens
respectively, including the 6,000-token review output cap. These are local
measurements, not guarantees about provider accounting or future prompt sizes.

`test_review_references.py` covers the saved failure, all reference sections,
stable IDs and lossless transport, escaped/Unicode/null/list values, bounded and
adversarial correction, offline replay, external blockers, approval/repair audit
tampering and token-call labels. Live reviewer convergence has not been tested
for this change. Narrow semantic architecture repairs and cross-review finding
carry-forward remain separate subsequent changes.

Final offline validation: all **440 frontend tests passed**, including 21 new
reference regressions, legacy FSM/dual-LLM routing, review/repair admission,
lowering, handoff and local simulation checks. Network connections were blocked
in the test runner. The first broad run exposed one stale JSON-recovery fixture
that supplied a full architecture response after compiler repair had entered
its existing edit mode. The fixture now supplies the required fingerprinted
remove edit and asserts the exact reconstructed response; generator behavior
was not changed. The complete suite then passed. The focused review suite also
passed all 53 tests. No live LLM calls ran during implementation or validation.

### Narrow semantic architecture repair — change 2 (October 27, 2026)

Architecture-scoped semantic repair now starts with the complete accepted
architecture as a fingerprint-bound edit base. Its first call and all compiler
retries must return `{"base_fingerprint":"...","edits":[...]}`. It reuses the
existing object-field edit engine rather than generating a new decomposition.
The prompt asks for only the reported corrections and necessary dependent fields,
preserving unrelated contract prose, module identities, types, wiring, rule
ownership and verification declarations. No worked decomposition example is
included when the accepted architecture already supplies the structure.

This route rejects entire stage-root edits and replacement/removal of existing
nonempty objects: change the fields inside those objects instead. New fields,
including structured fields, can be added; lists remain whole-field replacements.
The existing 1..100-edit limit, atomic application, exact base fingerprint and
full compiler/configuration-matrix validation still apply. Malformed responses
and stale fingerprints leave the base unchanged. A syntactically applied but
compiler-invalid candidate becomes the complete base for the next bounded
attempt, retaining prior edits for inspection and correction. There is no
fallback to full architecture regeneration or an additional semantic cycle.

Each applied architecture attempt now journals `repair_base_fingerprint`, raw
`response`, `applied_response` and exact before/after `applied_changes`, including
compiler-rejected candidates. The existing final parent/target repair context
still records the net changes and findings for semantic review. Edit validation
does not prove that a selected field is semantically related to a finding;
the change audit and independent review remain necessary. Unedited values stay
unchanged rather than being paraphrased by a complete replacement response.

System-scoped semantic repair still edits the system first, invalidates the old
architecture and generates a fresh decomposition against that new system.
Ordinary compiler architecture retries retain their broader shape-repair mode.
Initial proposals, legacy expand-spec/FSM/dual-LLM selection, RTL generation and
approval gates have not been rerouted by this change. Finding carry-forward is
still a later change.

The saved observability failure was exercised locally through real compilation
and export, using mocked generation for the three known check-binding edits.
Its net changes were exactly `events`, `note` and `observations` under
`execution.verification.V_EXEC_COMMAND_COMMIT`, compared with 69 changes in the
previous full replacement. Every other document value, locked system fact,
library, request and selected configuration stayed unchanged. A deliberately
rejecting mocked review retained the original finding, so no approval or RTL
was created. The first repair prompt reserves 65,001 estimated tokens including
the existing 24,000-token output cap. This is an offline scope/compilation audit,
not a live-model repair or behavioral verification result.

`test_semantic_architecture_edits.py` adds 14 regressions covering field scope,
escaped keys, list and structured additions, atomic rejection, whole-object and
locked-system edits, source preservation, exact journals, malformed/stale
responses, compiler rejection and subsequent correction, final review rejection,
transport failure, prompt budget and the recorded controller observation case.
The 62 focused existing edit, system-repair, semantic-repair/review, prompt and
JSON-recovery tests also pass.

A live check can revisit the original observability rejection in a fresh output
directory without regenerating its user/system specification:

```sh
RUN_DIR="$PWD/local/frontend/microarch/builds/text_runs/20261027T065112Z_0766bc80"
REPAIR_DIR="$(mktemp -d /tmp/capstone-step2-live-XXXXXX)/repair"
python3 -u local/frontend/repair_specification.py \
  "$RUN_DIR/architecture.yaml" \
  --review-report "$RUN_DIR/review/report.json" \
  --output-dir "$REPAIR_DIR" \
  --model protected.gpt-5.6-terra
```

Inspect `source/attempts.json` (or `source.attempts.json` if generation stopped),
`repair_review_context.json`, `repair_status.json` and `review/report.json`.
The question is whether the model returns focused edits, preserves unrelated
fields, passes the compiler, and receives a valid semantic disposition. A new
current finding can still legitimately stop the flow. This command has not been
run against the live model by the assistant.

Final validation: all **454 frontend tests passed** with network connections
blocked in the runner, including the 14 new narrow semantic architecture checks
and legacy FSM/dual-LLM routing regressions. The saved completed controller's
existing approval and RTL handoff also still validate. No live LLM calls or
behavioral verification of the newly repaired controller ran for this change.

### Finding carry-forward — change 3 (October 27, 2026)

New semantic review reports embed `finding_ledger` and write the same data to
`finding_ledger.json`. Each entry retains its original finding verbatim, an
identity derived from the originating manifest and complete finding, and a
history of open/resolved dispositions with explanations, evidence and links.
Display IDs alone do not identify a ledger entry: reused IDs or changed claims
cannot overwrite earlier allegations. Renamed current findings can be linked
while the original allegation remains open. A resolved finding reported again
on the same artifact is explicitly reopened. Closed entries remain in the audit.

Semantic repair automatically forwards its selected report's ledger through
the repair context. On the first repair, an original finding already covered
by `repair_assessments` uses that assessment to update the ledger; the reviewer
does not answer the same obligation twice. Other open entries require
`finding_assessments`, with exactly one `{ledger_key,status,explanation,references,
remaining_finding_ids}` for each outstanding key shown in the prompt. Unresolved
entries link nonempty current finding IDs of the same resolution kind and keep
the verdict blocking. Resolved entries require an explicit explanation, current
evidence and no remaining links. A reviewer can explain that a prior allegation
was unsupported or already prevented by the existing design, without demanding
a gratuitous architecture change. This is recorded review evidence, not a
deterministic proof that the explanation is correct. Independent external
agreement/readiness checks still gate approval.

Standalone rereview selects its history explicitly with repeatable
`review_specification.py --prior-review-report REPORT`. Pass the latest report
from the selected ledger chain; its entries include the earlier history. Multiple
earlier reports can seed a legacy chain. Reports must belong to the exact current
manifest/configuration. A repair-parent report is forwarded only through its
matching parent manifest/report fingerprints in the target-bound repair context.
Reports from unrelated revisions or configurations fail before an output
directory or model call is created. History is not inferred by searching `/tmp`
or other review directories. Omitting prior history starts a new review chain;
it cannot establish closure of allegations in an unselected chain.

`run_flow.py` exposes the same selection as repeatable
`--prior-spec-review-report REPORT`, requiring `--automated-spec-review`.
The legacy expand-spec/FSM route is unchanged. Existing canonical reports and
approvals without a ledger remain readable; this does not retroactively resolve
or revoke any historical findings/approvals. New approval validation and repair
admission recompute the ledger transition from the selected source entries and
the canonical verdict. Missing assessments, dropped entries, changed identities,
unexplained closure, inconsistent dispositions or omitted repair-parent history
are rejected. Compatible older/longer histories merge without resetting closure;
divergent dispositions for the same entry require selection of an explicit
review chain rather than silently choosing one branch.

An otherwise valid reference-only failed report can contribute its new findings,
including historically misspelled paths. Those references remain historical
data; dispositions must cite current evidence. A failed review cannot close its
incoming obligations. Provider/parse failures before a verdict retain the input
ledger and can be selected on retry solely to recover that history. Malformed
substantive verdicts are not accepted as new evidence. The one bounded reference
correction also covers `finding_assessments`, and cannot change their statuses,
messages or links. Raw source/report history is kept outside the prompt except
for the open claims the reviewer must assess, limiting repeated context growth.

Offline replay imported both saved allegations
`F_READ_RETIREMENT_RESPONSE_SLOT_GUARD` and
`F_REFRESH_TIMEBASE_MAINTENANCE`, alongside a later clean legacy report. A mocked
clean verdict omitting their dispositions was rejected. An explicitly unresolved
mocked verdict retained both, and a subsequent silent-drop attempt was rejected
again. Correcting the refresh finding's historical reference spelling produced
a separate current finding entry, preserving the earlier allegation and its
explicit link rather than silently coalescing claims. No semantic adjudication,
approval, RTL generation or live LLM call occurred in this audit. The prompt
reserves 50,715 estimated tokens including the existing review output cap, and
the previous completed controller's approval/handoff still validate.

`test_finding_ledger.py` adds 19 offline tests for multiple reviews and repairs,
aliases and reused IDs, explicit false-positive disposition, reopened findings,
missing/duplicate/unknown assessments, stale configuration, divergent histories,
reference correction/failure, transport recovery, external readiness, metadata
tampering and both CLI entry points.

For a live ledger check, revisit the original repaired artifact with both earlier
reports. This intentionally uses the old repaired manifest to which those reports
belong, rather than the different step-2 repair branch:

```sh
RUN_DIR="$PWD/local/frontend/microarch/builds/text_runs/20261027T065112Z_0766bc80"
REVIEW_DIR="$(mktemp -d /tmp/capstone-step3-live-XXXXXX)/review"
python3 -u local/frontend/review_specification.py \
  "$RUN_DIR/repair/architecture.yaml" \
  --repair-context "$RUN_DIR/repair/repair_review_context.json" \
  --prior-review-report "$RUN_DIR/repair/review/report.json" \
  --prior-review-report /tmp/capstone-rereview-0c2fNf/review/report.json \
  --model protected.gpt-5.6-terra \
  --output-dir "$REVIEW_DIR"
```

Both older allegations must receive explicit current dispositions, even if the
new findings list would otherwise be empty. A pass is not required for this
check: an unresolved finding with valid links is a legitimate blocking result.
Inspect the new report's `finding_assessments` and ledger history. Subsequent
rereview should select this new `report.json` with `--prior-review-report`.

Final validation: **473 frontend tests passed** with network connections blocked
in the runner. All 19 focused ledger tests also pass with transport-history
recovery enabled. Legacy FSM/dual-LLM routing, targeted architecture repair,
system repair, review reference recovery, lowering, handoff and local simulation
regressions remain covered. Python compilation and whitespace checks pass.
Live reviewer handling of the new ledger obligations remains for the user-run
command above; no live LLM call was made by the assistant.

### Blocking criteria and correction scope — change 4 (October 27, 2026)

The semantic review prompt now includes a generic blocking/completeness policy
and classifies repair scope by the necessary correction rather than the location
of cited evidence. See [review_policy.md](review_policy.md) for the criteria,
examples, saved refresh analysis and limits. A finding must identify a concrete
contradiction, impossible contract or missing behavioral decision; an ambiguity
must leave materially different behaviors permitted by all existing clauses.
A violating implementation is not evidence of an unspecified rule. Missing
duplicated prose or a preferred register, algorithm, wire or `every_clock` entry
alone does not justify a blocker when the required behavior is determinate and
implementable. Explicit update restrictions that prevent progress remain valid
blocking evidence. Essential reset, timing, ownership and priority choices still
must be specified.

Architecture corrections may cite locked system/execution rules as requirements
they must obey. System scope requires naming a locked behavior, event condition
or expected result that must change or be completed, and explaining why a local
architecture correction cannot suffice. The conflicting earlier instruction to
assign scope by evidence location was removed. No finding, scope, ledger entry
or approval is rewritten automatically. Review schemas, protocol revision,
repair limits and legacy generation routes are unchanged.

For the saved refresh allegation, the current evidence already fixes first and
repeated deadline edges and the tRFC release boundary. The absence of a separate
clock-update clause does not by itself demonstrate the alleged deadlock.
The reviewer must assess whether any other contradiction or missing essential
choice remains. A candidate explanatory `every_clock` contract addition was
compiled locally with exactly one document change and an unchanged locked system;
this demonstrates that a local clarification can be expressed, not that it is
necessary or semantically sufficient. Original source artifacts were unchanged,
and no approval or RTL was created. Both open refresh ledger entries remain
obligations; a mocked clean response omitting their assessments was rejected.

The new guidance is intentionally prompt-only: it does not attempt to decide
arbitrary prose semantics by keywords, infer a desired verdict, or bypass the
gate. Existing reachability and regression attribution checks remain in the
prompt. Reference guidance was shortened without removing evidence to accommodate
the new criteria. The saved large review reserves 79,731/80,000 estimated tokens
including its 6,000-token output allowance; the latest ledger review reserves
51,279. Both prompt snapshots reconstruct losslessly, and the earlier completed
controller's approval and handoff still validate.

`test_review_policy.py` adds ten checks using the saved refresh projection,
independently encoded absolute/elapsed time representations, reset and release
boundaries, a deliberately impossible update restriction, a genuinely unspecified
first phase, lossless evidence, repair routing and explicit ledger disposition.
These are manual abstractions and mocked review results, not automatic prose
classification or execution of target RTL. All 126 focused review/repair/ledger
tests pass. No live model call or semantic repair was run for this change.

For the next live check, rereview the unchanged repaired artifact with the latest
step-3 report so that both outstanding refresh entries require explicit answers:

```sh
RUN_DIR="$PWD/local/frontend/microarch/builds/text_runs/20261027T065112Z_0766bc80"
REVIEW_DIR="$(mktemp -d /tmp/capstone-step4-live-XXXXXX)/review"
python3 -u local/frontend/review_specification.py \
  "$RUN_DIR/repair/architecture.yaml" \
  --repair-context "$RUN_DIR/repair/repair_review_context.json" \
  --prior-review-report /tmp/capstone-step3-live-YWP0hz/review/report.json \
  --model protected.gpt-5.6-terra \
  --output-dir "$REVIEW_DIR"
```

Inspect the assessments and any remaining finding's concrete basis and correction
scope before starting a repair. A valid unresolved result is acceptable; a clean
verdict requires explicit dispositions and the normal readiness checks. The
assistant has not executed this command against the live model.

Final validation for change 4: **483 frontend tests passed**, including the ten
new policy/evidence checks, with network connections blocked in the runner.
The 126 focused review, repair and ledger checks also passed. Python compilation
and whitespace checks pass. These checks preserve the gate and evidence
transport; live semantic-review consistency remains to be evaluated with the
user-run command above.

### Local refresh preparation progress checks (October 27, 2026)

The live refresh-timebase repair added exactly one `refresh.update_events.every_clock`
entry and preserved the locked system. Its review explicitly resolved every carried
timebase entry but opened `F_REFRESH_PREPARATION_CANDIDATE_DEADLOCK`. The saved
command-engine clause forms candidates when "refresh is inactive"; dispatch is
separately forbidden while pending or in service. The phrase does not identify
whether pending itself blocks candidate formation, and initial PRE/REF selection
without an active request is not spelled out in that clock-update clause.

`testdata/refresh_preparation_progress.json` preserves the relevant contracts,
rules, finding, selected timing cycles and source manifest fingerprint from
`/tmp/capstone-refresh-repair-H1z1fV/repair/architecture.yaml`.
`test_refresh_progress.py` explicitly models three interpretations rather than
parsing the prose or simulating generated RTL:

| Initial path | Pending blocks formation | Busy-only gate, no idle preparation selection | Busy-only gate plus active-first drain and explicit idle preparation |
| --- | --- | --- | --- |
| Active request needs more commands | Stalls before its next candidate | Drains the request, then stalls | Drains, closes remaining banks in ascending order, commits REF |
| Idle with open banks | Stalls | Stalls | PRE for ascending open banks, then REF |
| Idle with all banks closed | Stalls | Stalls | Creates REF directly |

The strict model treats the source's "when" condition as an exclusive gate; the
source does not literally say "only when." Its stalled executions violate the
locked refresh progress requirement. They demonstrate the consequence of that
reading, not that every valid implementation of the complete specification must
deadlock. The progressing interpretation supplies its active-first and idle
PRE/REF selection explicitly. Consequently its success shows feasibility of the
proposed clarification, not that an algorithm was inferred or proven from prose.

Using the selected DDR4-3200 tRCD=22 and tRAS=52 cycle values, the first sample
progresses through PRE[0] at E1, ACT[0] at E3, READ[0] at E25, preparation PRE[0]
at E55, PRE[2] at E57, and REF at E59. The initially idle open-bank sample commits
PRE[0], PRE[2], PRE[3], REF at E1/E3/E5/E7. The all-closed sample commits REF at
E1. These edges are relative to the start of each manually chosen initial state,
not the reset epoch or an actual RTL trace.

Fourteen new checks also exercise a 256-state active READ/WRITE and bank-mask
matrix with finite stalls, one/four-bank cleanup masks, held candidate payload
and active ownership, an already outstanding setup candidate, due coinciding
with final retirement or REF, reset priority, service exclusion/release and
timeout under an environment that never grants a candidate. The tests distinguish
active-request setup PRE commands from the ascending cleanup order required only
after that request drains. All 70 focused progress, policy, ledger, repair-review
and targeted-architecture-edit tests pass with network connections blocked.

The model observes dispatch permission but does not execute a FIFO, functional
memory or response delivery. It uses selected timing guards, not complete DDR4
timing coverage or arbitrary-interleaving verification. Initial open-bank timing
histories are mature; no new host work is injected into the primary traces.
The earlier 483-test full frontend suite was not rerun for these test/document
additions; production flow code was unchanged.

The recommended correction is confined to
`command_engine.update_events.every_clock`: distinguish pending from service
busy, continue an already active request while pending, enter preparation directly
when idle and pending, choose the lowest open bank for PRE or choose REF when all
banks are closed, and retain existing candidate stability, timing permission,
registered-candidate bubbles and reset priority. Pending still blocks new dispatch.
A proposed one-field edit compiled locally with the locked system unchanged.
It was saved separately under `/tmp/capstone-refresh-progress-pzrgqzoi/`, with
`report.md`, `report.json`, edge traces, `proposed_architecture_edits.json`,
`proposed_clause.txt` and a deterministic candidate export under `proposal/`.
The original design was not edited, the ledger finding remains open, and no
approval, target RTL run or LLM/network call was made.

To reproduce the checked-in model regressions:

```sh
python3 -m unittest discover -s local/frontend -p test_refresh_progress.py -v
```

### RTL response recovery: checkpoint 1 (October 27, 2026)

The fresh text run `20261027T182419Z_27739196` passed semantic review and completed
three RTL modules before command-engine generation returned malformed inner JSON.
Its provider reported a normal stop, but parsing failed beyond the first
1,000-character logged preview. The original full malformed text was not retained.
The recorded estimate for the entire attempt was $0.491334 across 12 calls;
this was not a completed RTL/simulation flow.

Checkpoint 1 now captures full decoded provider responses and model content,
request metadata, usage and finish reason before runtime parsing. Parser input
and exact error coordinates are retained separately. Distinct response/parser
failure categories propagate to round reports, including the difference between
malformed JSON and genuine specification clarification. See
[rtl_response_recovery.md](rtl_response_recovery.md) for file locations, redaction,
category definitions, scope and verification details.

This is diagnostic groundwork only: no new retry, schema restriction, model
selection, cache fallback, FSM behavior, approval or RTL acceptance path was added.
Eighteen new evidence tests and 84 focused existing regressions pass. A synthetic
offline example preserves and reproduces a parser error at character 7,573 from
a 7,610-character response, with a single mocked HTTP attempt and no accepted RTL.
No LLM/network call or replay of the paid failed run was performed.

Final checkpoint-1 validation: all **515 frontend tests passed** with network
connections blocked in the runner. This includes the 18 new diagnostics tests,
legacy FSM/dual-LLM routing, resume/cache, review/repair gates, lowering, handoff
and local simulation regressions. Python compilation and whitespace checks pass.
Malformed generation still stops at this checkpoint, with its complete evidence
and category now retained; bounded recovery remains a later checkpoint.

### RTL response recovery: checkpoint 2 (October 27, 2026)

Generation, lint-repair generation, merger and reviewer parsers now share a strict
decoder and stage-specific payload validator. See
[rtl_response_recovery.md](rtl_response_recovery.md) for the exact required and
optional fields, explicit legacy compatibility, failure categories and scope.
Duplicate keys, unknown/missing fields, wrong types, status/issue contradictions,
array wrappers and arbitrary prose around responses no longer become plausible
normalized payloads. Compiler invariants remain authoritative. Reviewer RTL stays
optional; generator/merger RTL must be nonempty text. Whole-response/code fences
and the `CLEAN` alias remain supported, with internal RTL text preserved.

Rejected generator/merger replies cannot silently promote a retained older RTL
fallback into newly generated code. Schema paths and complete response evidence
are retained, and the ordinary lint/reviewer gate still checks valid candidates.
Reviewer prompts were aligned with the status/issue consistency rules. Compact
review preserves typed `NON_ISSUE` entries and does not drop a sixth issue.
No recovery calls or model/routing changes were introduced; existing compact
review, truncation, reviewed-cache fallback, resume and convergence bounds remain.

All 21 new payload tests and 123 focused regressions pass. An offline audit at
`/tmp/capstone-rtl-payload-checkpoint2-egdjww79/audit.json` confirms five invalid
reply classes stop after one mocked request before lint/review/cache, while a
valid fenced/CLEAN reply traverses real HDL lint and the ordinary mocked review
and cache path. No live LLM call or replay/modification of the user's failed
build was performed. Payload validity is not target RTL behavioral verification.

Final checkpoint-2 validation: **536 frontend tests passed** with network
connections blocked, including 21 new payload tests and the existing FSM/dual-LLM,
cache/resume, review/repair, handoff and local simulation checks. All 123 focused
regressions, Python compilation and whitespace checks pass. Bounded response
recovery remains checkpoint 3; no new paid run was started.

### RTL response recovery

Bounded RTL response recovery is on by default for architecture RTL builds with
lint, including the text intake and resume routes. Add `--no-rtl-recovery` to
disable it; `--rtl-recovery` explicitly enables it. Export-only and no-lint runs
do not automatically enable recovery. A module invocation shares a maximum of
two extra HTTP attempts across stages/rounds, with at most one per recovery
category. Recovered RTL still requires lint and semantic review. Legacy
expand-spec/FSM defaults, standalone generator defaults and model selection
remain unchanged. See
[RTL response recovery](rtl_response_recovery.md#checkpoint-3-bounded-recovery)
for exact limits, terminal failures and evidence paths.

Checkpoint 4 validates recovery through real isolated module/cache output,
Verilator module/integration lint, build status and handoff export using mocked
provider responses. See the
[gate audit](rtl_response_recovery.md#checkpoint-4-recovered-candidate-gates-and-route-isolation).
A module cache is module-level approval; an integration failure still leaves the
whole build failed and prevents a current verification handoff.

### Behavioral simulation of the resumed text build

The generated single-request controller uses an 8-bit address, four banks,
256 words per bank and no refresh. The compact FIFO simulation profile correctly
rejects this different contract. `simulate_handoff.py` now additionally supports
`--profile ddr4-single-request-v1`; the existing FIFO default and legacy generation
routes are unchanged. See the [simulation documentation](../simulation/README.md#single-request-open-page-profile)
for the matching command, scenario scope and coverage limits.

The saved build `text_runs/20261027T182419Z_27739196/build` passed the five bounded
scenarios for seeds 1, 2 and 17: 15 scenario passes and 39,488 cycles. The actual
Verilator runs used authenticated snapshots, independent admission-based memory
and command/timing scoreboards, and declared-port observations. Original RTL,
reviewed caches and handoff were verified unchanged. No LLM calls were made.
Simulation reports provide local executed evidence separately from the immutable
frontend status; complete requirement/configuration coverage is not claimed.

### Timing campaign groundwork

Initial/fresh text specification planning now receives a small optional-feature
discovery index alongside its schema/example/timing context. A richer local
catalog records source/template hashes, accepted roles, interface/reset/dependency
summaries and source assumptions in `library_catalog.json`. Entries are not marked
verified and custom modules are not restricted to a feature menu. Locked repairs
omit discovery-only metadata while retaining the original design/timing context.
Selected detailed contract injection remains future work; prompt-budget tests
continue to enforce the existing 80k reservation.
Generator revision 19 changes future specification-generation cache identity;
existing approved exports and exact-spec RTL resume remain valid.

`prepare_timing_campaign.py` creates seven offline request/acceptance cases and
fingerprinted context snapshots. The two DDR4 timing guards have an executable
`ddr4-timing-guard-v1` isolated permission profile; the five reloadable timer
width/priority cases explicitly retain planned checker status. No paid design
trial is performed by preparing the campaign. See [campaign documentation](../campaigns/README.md).

### RTL review interpretation after specification lowering

Specification-derived RTL prompts now include compiler-derived public interface,
source type layout and configured timing facts, plus a consistent stage policy
for generation, full/compact review and merge. YAML timing reference paths and
shared schema type identifiers are interpreted as elaboration constraints; the
RTL must preserve their resolved values and ABI. Numeric constants and equivalent
packed representations are valid unless an explicit RTL directive says otherwise.
The policy also distinguishes packed dimensions before a typedef name from
unpacked dimensions after it. This addresses the first timing campaign's review
nonconvergence without suppressing issues, extending round limits, approving the
saved candidate or changing legacy/FSM defaults. See the
[campaign stage-boundary notes](../campaigns/README.md#rtl-review-stage-boundaries-campaign-version-2).

## Planned top-orchestrator integration (October 27, 2026)

**Status: checkpoint 1 of 4 complete; checkpoint 2 publication, consumer preflight and module invocation/receipt adapters implemented and tested offline; deployed acceptance pending.** This records the integration plan for
[`shared/top_orchestrator`](../../../shared/top_orchestrator). The frontend can
already produce isolated microarchitecture builds; selecting and carrying those
builds through downstream verification/backend still requires the work below.
The implementation status below distinguishes available commands from planned behavior.

### Two frontend routes and shared CLI terminology

Add an orchestrator selector:

```text
--frontend-flow expand-spec     # default: existing expand_spec frontend
--frontend-flow specification   # full system-specification/microarchitecture frontend
```

These are the only two top-orchestrator frontend routes. Present **Standard**
(`expand-spec`) and **Experimental** (`specification`) to users. Keep the internal
values in CLI arguments and state; help and logs should show the friendly names
alongside those values so users can identify their artifacts.
`expand_spec` remains the existing implementation/script name; its CLI spelling
is `expand-spec`. Microarchitecture is an internal design stage of the
`specification` route, not a third orchestrator route or selector alias.

Keep the existing expand-spec scripts, generation nodes, publication path, BIST
and repair behavior unchanged. Introduce small dispatch points in the shared
orchestrator and separate specification-route adapters. Persist the selection and
build identity in run state; preserve them through retries, debug/recheck,
verification and backend transitions. Existing positional `RunConfig` construction
must remain compatible when adding configuration fields.

The `specification` route should start with text intake followed by
`run_flow.py --specification`, which generates the system specification and then
decomposes its architecture, performs the required review/approval stages, and
generates module RTL and structural integration. The standalone frontend's
`--microarch` direct-proposal flag is outside the orchestrator interface;
`--microarch-only` is an export/review stop control, not a frontend route.
Approved architecture resume should be a supported input mode within
`specification`, preserving its approval gates and revision identity.

Both routes should present the same normal user journey:

```text
Text description + constraints + target controller frequency
    → selected frontend → frontend checks → verification → backend → GDSII checks
```

Share text-description intake, target-frequency selection, run controls, progress
vocabulary and artifact discovery. Route-specific adapters translate these inputs
into the existing expansion or full specification/microarchitecture process.
Users should not need to supply internal frontend flags to obtain the full flow.
Both routes retain their supported design scope; an unsupported request must
produce a clear diagnostic rather than silently change routes or requirements.

#### Shared intake and implementation boundaries

Use one interaction shell with route-specific guidance and interpreters. Standard
retains its supported-feature list, existing slowest/medium/fastest presets and
interpreted configuration preview. Presets remain scoped to Standard and must
not silently select a controller frequency. Experimental accepts open-ended text:
"Describe what you want to build and any requirements that matter. Leave
implementation choices open; planning will propose them for review." Interfaces,
buffering, ordering, reset and timing are optional examples, not required fields.
Ask follow-up questions only when missing information prevents meaningful planning.

Preserve the original description intact. Experimental passes it to the existing
specification planner without another LLM summarization/extraction step. Capture
the route, explicit constraints, selected frequency and its source, and input
references in a shared intake record. Confirm the submitted request and allow
revision before continuing. This confirmation is separate from generated system
specification/architecture approval; proposed defaults and unspecified choices
must be visible at that later review. Normal execution should guide users through
the gates; manual export/resume commands are advanced input modes.

Interactive intake offers Standard/Experimental; Standard is the default and
existing noninteractive commands retain their meaning. Shared intake changes may
wrap legacy preparation while preserving its generation, checks and repair
semantics. Resume retains route and approved inputs; conflicting changes require
a new revision. Selected frequency is design intent, not achieved timing evidence.

Before downstream integration, define a common frontend result identifying the
selected build/revision, top, clock, ordered RTL inventory, contracts, requirements
and check evidence, with route-specific evidence attached. Resolve structural
wrapper representation and deployed verification capabilities at the downstream
compatibility checkpoint. Incomplete verification must block backend readiness.

**Checkpoint 1 implementation:** normal terminal generation now offers shared
Standard/Experimental intake. Enter a multiline description, select a controller
clock, then confirm or edit the request. Explicit `--input YAML`, checks-only and
scripted legacy commands retain their established entry behavior. Text files use
the same intake through `--input description.txt`. Standalone preparation remains
available through
`python -m shared.top_orchestrator --prepare-intake request.json`, optionally with
`--frontend-flow expand-spec|specification`, `--input description.txt` and
`--target-mhz MHZ`. It saves the original text and confirmed request metadata
without design approval. Numeric/default frequency selection makes no model
calls; typing `discuss` explicitly requests model advice and may incur cost. The
advisor never selects a frequency. `--plan` performs no writes or prompts.
Standard execution without shared intake remains on its existing path. Confirmed
Standard records now work with `--intake request.json`: the original description
is snapshotted and passed to the existing feature interpreter using `--text-file`,
which retains its interpreted YAML preview and confirmation. Declining or failing
interpretation stops rather than repeating paid extraction of unchanged text.
The intake clock is checked against interpreted clock constraints and added to the
confirmed configuration; it is also retained as the backend target. Shared
frequency advice and confirmation/edit are implemented.
Confirmed Experimental intake can feed the isolated frontend adapter:

```sh
python -m shared.top_orchestrator --intake request.json --no-verif --no-backend --plan
python -m shared.top_orchestrator --intake request.json --no-verif --no-backend
```

The second command starts paid frontend generation and remains user-operated.
The adapter snapshots the record and full description, adds the controller clock
as an explicit request constraint, and uses run-owned source/review/repair/build
directories. It invokes the existing automated specification review and bounded
semantic repair gates, then requires generation, approval, integration-lint and
authenticated RTL-phase handoff evidence. It does not execute local behavioral
simulation, legacy BIST, legacy publication, remote verification, backend or outer
repair retries. Route/frequency overrides conflicting with intake are rejected.
Both shared-intake routes now emit `frontend_result.json` after successful checks:
Standard indexes its authenticated published mailbox, while Experimental indexes
its authenticated handoff/build. The common index records route, top, target clock,
revision, hashed RTL/contracts, requirements references and check evidence without
claiming backend readiness. Experimental retains handoff compile order; Standard
references its native manifest without claiming sorted files are a compile order.
Existing Standard commands without intake retain their publication behavior.
Fresh Experimental `--cache` is rejected because the underlying frontend forbids
combining cache with automated review/semantic repair. Approved architecture input
can use native cache-only mode, or `--resume-build` to copy reviewed caches from a
prior build and generate only missing modules. Cache misses under resume can make
paid calls; resume is not an offline guarantee. Conflicting cache/resume flags are
rejected. The current text adapter uses the existing default
`ddr4_controller_top` memory-controller design category; YAML requests may supply
a different design name/category.

Experimental supports `--input` request YAML, specification package YAML, and
specification-derived architecture YAML. YAML mode requires `--target-mhz` so a
DDR data rate is never used as the controller clock. Request clocks are checked
before generation; the selected clock is injected into new requests. Package and
approved inputs preserve their contracts and reject conflicts with explicit
controller-clock constraints in their generation provenance; a target alone does
not prove cycle-based timing contracts or achieved physical timing. Package
snapshots retain all compiler-consumed documents plus the timing library with
relative paths intact and record file hashes. Documentation-only scope links are
metadata, not compiler inputs.

```sh
# Package: deterministic lowering, then paid automated review/repair and RTL.
python -m shared.top_orchestrator --frontend-flow specification \
  --input local/frontend/microarch/specification/ddr4_fifo/package.yaml \
  --target-mhz 200 --no-verif --no-backend --plan

# Approved architecture: exact revision-bound approval, optional copied build.
python -m shared.top_orchestrator --frontend-flow specification \
  --input /path/to/architecture.yaml --spec-approval /path/to/approval.json \
  --resume-build /path/to/prior/build --target-mhz 200 --no-verif --no-backend --plan
```

Approved inputs are validated by the native compiler before execution and again
from the owned snapshot. Resume requires a matching architecture and rejects
symlinked builds, altered structural tops and module RTL differing from its
reviewed cache. It leaves original builds untouched and stops for scoped recheck
rather than overwriting intentional patches. Usage evidence for explicit build
directories is stored beside the build, within the orchestrator-owned frontend.

Offline acceptance includes real package snapshot/lowering, exact approval and
resume, cached module RTL, Verilator integration lint, authenticated handoff and
common result export using a small register fixture with networking disabled.
It rejects stale approval and tampered RTL; this is integration evidence, not
controller behavior or system-verification coverage. Legacy manifest-refresh tests
now create their own controller fixture instead of depending on global generated
files. The three saved-controller handoff tests now explicitly declare their
generated-snapshot prerequisite; with the existing mailbox fixture test, four
tests skip when that legacy snapshot is absent. The full orchestrator suite runs
127 tests successfully (four skips), and 28 focused frontend intake/resume/handoff
regressions pass. The interactive cleanup/plan checks also pass after the final
CLI cleanup change. Python compilation and whitespace checks pass.
These implementation checks made no paid calls. Subsequent user-operated live
acceptance completed through resume; see the live acceptance update below.
Checkpoint 1 is complete, with behavioral verification reserved for later stages.

To start the normal shared interactive frontend-only experience:

```sh
python -m shared.top_orchestrator --no-verif --no-backend
```

This prompts for Standard/Experimental and then continues into generation after
confirmation; it can make paid calls. For the already saved Experimental request,
the earlier `--intake request.json --no-verif --no-backend` command remains valid.
Adding `--plan` previews without running. No additional preparation run is needed
for an already confirmed intake record.

Planned common CLI terms:

| Option | Shared meaning |
| --- | --- |
| `--frontend-flow expand-spec\|specification` | Choose the frontend implementation; default to `expand-spec`. |
| `--input PATH` | Supply a text description or route-compatible YAML; omission starts the shared interactive intake. |
| `--target-mhz MHZ` | Select the target controller clock for frontend design constraints and backend implementation; omission uses the shared frequency-selection interaction. |
| `--plan` | Show the selected route, input preparation, checks and downstream stages without execution or writes. |

Keep existing verification/backend skip controls and cache controls available
where supported, with consistent help text. A control whose existing meaning is
route-specific (for example `--no-bist`) must be documented and validated rather
than silently repurposed. Checks-only, debug and resume must resolve the selected
build without asking users to reconstruct internal output paths.

### Inputs, run ownership and local checks

#### Functional controller timing scope

**Live acceptance update:** run `20261027T233850Z_47d4ee60` completed the approved
design after two earlier provider HTTP 524 interruptions. It reused prior reviewed
module caches, completed the missing RTL, passed Verilator integration lint,
exported/authenticated the RTL-phase handoff and emitted `frontend_result.json`.
Its RTL revision is
`bbbfb0bd5b51f003f52fd74cdeeec0af9c9e3c3db41b4249c503309fa0d1fc2c`.
The successful invocation recorded six calls and estimated cost $0.987079;
this excludes earlier invocations and unknown usage on timed-out calls.
Behavioral verification remains `not_run`, consumer compatibility remains
`adapter_required`, and backend readiness is not established. Checkpoint 2 remains
mailbox publication and one-module verification compatibility.

**Follow-up: provider timeout resilience (planned).** Earlier runs
`20261027T232243Z_92d77b7f` and `20261027T233250Z_75a902cd` received HTTP 524 during
command-engine and timing RTL generation respectively, approximately 125–126
seconds into requests with a 180-second client timeout. Both logged a
`Retry-After: 120` header. These observations do not establish rate limiting as
the root cause; increasing the client timeout alone cannot prevent a returned
upstream HTTP error. Preserve status, elapsed time, response headers and provider
request IDs when available. Investigate request latency/prompt size and provider
streaming/async capabilities before selecting a mitigation. Any automatic retry
should be narrowly scoped to the failed request, use bounded attempts/wall time
and backoff informed by Retry-After, report uncertain/possibly duplicated billing,
and preserve reviewed caches and exact architecture identity. Keep transient
transport handling distinct from semantic repair and malformed-response recovery;
define one shared outer budget to avoid multiplying retry loops. Model fallback
requires an explicit policy; all recovered RTL must still pass normal lint/review.
Exercise transport recovery offline before another paid experiment. No automatic
524 retry or model fallback was added as part of checkpoint 1.

The first live orchestrator trial (`20261027T230928Z_884ecd0b`) stopped during
system planning: the model interpreted DDR4-3200 plus a 200 MHz controller target
as a request for a physical 1600 MHz DDR domain and an unspecified 8:1 PHY ratio.
One call was recorded (estimated $0.028248); no system stage was accepted and no
RTL was generated. Its evidence remains unchanged. The trial exposed an implicit
scope in Standard: `expand_spec.py` substitutes stored profile cycle counts while
backend frequency selection separately sets implementation timing.

Both orchestrator controller routes now explicitly use the versioned
`project-functional-controller-v1` timing contract defined in
[`timing_model.py`](../../../shared/top_orchestrator/timing_model.py):

| Quantity | Meaning in this project model |
| --- | --- |
| DDR speed/profile | Select the project timing dictionary profile. |
| Stored `timing_cycles` | Counts of controller clock edges, used unchanged for delays, windows, refresh intervals and refresh recovery. |
| Dictionary clock/ns fields | Provenance for the stored profile counts; they do not introduce another RTL clock domain. |
| Target controller MHz | Implementation/backend clock target; changing it does not rescale profile counts. |
| Physical DDR throughput/timing | Requires a separate explicit contract; this model supplies no PHY, CDC, rate conversion or physical JEDEC compliance claim. |

For example, DDR4-3200 `tRCD=22` remains 22 controller cycles, or 110 ns at
200 MHz. Refresh intervals follow the same cycle abstraction; treating minimum
delays conservatively does not establish valid physical refresh timing.

New shared intake displays and records this scope. Existing saved intake records
without it receive the same default when preparing a fresh controller request;
their original description and frequency remain unchanged. New Experimental
controller requests carry the complete versioned definition in `timing_model`,
so ordinary constraint traceability includes it in planning, review and generated
package/module context. Request fingerprints consequently change; previous failed
responses are not treated as results for the new request. No global generator
revision change is needed because the complete definition is part of request
identity. Generic YAML requests receive no implicit DDR model.

Standard shared-intake configuration records the same definition. Original
Standard YAML inputs remain byte-for-byte snapshots, with their established scope
recorded in a run-local `timing_model.json`. Both common frontend result indexes
carry declared timing scope; historical Experimental packages without a declaration
remain undeclared rather than being retroactively labeled. Existing package and
approved-architecture sources are never rewritten to adopt this default.

Explicit alternative timing scopes are preserved for Experimental planning.
Standard rejects an explicit incompatible scope. A modified definition claiming
the same version ID is rejected. Full original requirements remain authoritative:
physical signaling, real link throughput, additional clocks or absolute-time
requirements must be evaluated and reported as conflicting/unsupported when
necessary. The existing unsupported-response path remains terminal; this change
does not suppress the rejection or fabricate support for a PHY.

Offline tests cover the original 3200/200 request, scope traceability in both
planning prompts, unchanged Standard delay/refresh substitutions, input identity,
explicit physical requirements and terminal unsupported responses. The original failed
paid trial remains immutable. A later fresh request and resumed build completed
live acceptance as recorded above. The
orchestrator suite now runs 131 tests successfully (the same four missing-legacy-
snapshot skips). Seven focused timing/Standard tests pass after the final input
validation and prompt-budget assertions; compilation and whitespace checks pass.
The saved `request.json` can be reused for a new user-operated run with the same
command; the default timing contract is attached during fresh request preparation.

Use shared intake and frequency selection with route-specific input preparation
alongside the existing `frequency.py` preparation. Both routes accept a text
description and constraints; preserve existing expand-spec YAML support and add
specification request/package YAML support for the new route. Snapshot all referenced
inputs for package-based requests. Copying only one YAML file is insufficient
when it references other documents. Use a stable build directory owned by the
orchestrator run, for example:

```text
runs/<run-id>/frontend/
  input/
  source/
  architecture.yaml
  review/
  repair/
  build/
    microarch/gen_exp/           # generated module YAML contracts
    rtl_output/                 # functional modules and structural top
    verification_handoff.json
```

Artifacts must be located from the actual selected build and repair result;
repair can produce the final approved architecture under `repair/`. Do not copy
results into global legacy output directories to satisfy existing consumers.
Resolve the target controller frequency before generation and record its source.
Pass the same selected value into frontend constraints and backend configuration;
preserve existing expand-spec generation behavior while checking its declared
clock constraints. Use an explicit user choice or the shared selection interaction
informed by declared controller clocks. Surface conflicts with supplied or approved
specifications explicitly; do not silently override locked operating requirements.
DDR transfer rate is not controller frequency. Resume and direct downstream entry
must validate the selected target against the published design's clock contract.

The specification-route generation/check adapter should validate the selected
build's integration lint and authenticated handoff. Local simulation requires an
explicit compatible profile. Unsupported or unexecuted checks must be reported
truthfully and must not count as verification passes. The legacy BIST runner,
global lint helper and legacy controller-manifest refresh must not run on this
route.

Repairs and rechecks must address the selected build, preserve intentional RTL
patches, and publish a new revision when content changes. Frontend semantic repair
and RTL response recovery retain their own bounded budgets. The outer
orchestrator must not repeatedly restart the entire paid frontend flow after
those budgets are exhausted.

### Publication and downstream verification

#### Checkpoint 2 implementation status

`specification_mailbox.py` now publishes a separate immutable Experimental
snapshot. New successful orchestrator Experimental generations automatically use
this publisher and retain the mailbox in run state; Standard publication remains
unchanged. Already completed builds can be published without regeneration:

```sh
python -m shared.top_orchestrator.specification_mailbox \
  /path/to/completed/build /path/to/new/mailbox/revision_001 --target-mhz 200 \
  --module MODULE_NAME --module-output /path/to/new/module-input
```

Publication checks native handoff authentication, revision-bound approval,
generation/build status and actual integration-lint evidence. The portable native
handoff and every referenced input are copied under `handoff/`, including embedded
source/package snapshot, approval/review evidence, module YAML/IR/RTL, structural
top, filelist and verification plan. Original hashes and revision identity remain
intact. Explicit `rtl/` and `specs/` projections are checked against these native
inputs. `verification_inventory.json` and `rtl/manifest.json` use the distinct
`capstone.specification_rtl_inventory` schema, retain compile order and identify
the wrapper as an **architecture-backed structural contract**. No wrapper YAML is
invented and the wrapper is not omitted from inventory. This schema does not yet
claim compatibility with the legacy controller system verifier or backend.

The completed live build was published at
`shared/mailbox/20261027T233850Z_47d4ee60/revision_001` with the same RTL revision
`bbbfb0bd5b51f003f52fd74cdeeec0af9c9e3c3db41b4249c503309fa0d1fc2c`.
One real host-adapter YAML/SV pair was prepared under
`runs/20261027T233850Z_47d4ee60/module_compatibility/host_adapter/input/`.
Its `source_context.json` retains full shared context, system obligations and
parent identity; `snapshot.json` binds the pair to the full mailbox digest.

The local `server/validation/requirement_verification.py` catalog now consumes
structured source requirements, full verification obligations and behavioral
contracts. Existing agent-facing `REQ-NNN` IDs remain available, with original
source IDs and complete source objects retained through combined requirement
results. The new fields do not earn static passes: they require explicit
verification evidence. Legacy behavior/structure/FSM catalogs retain their prior
IDs and results. The real host-adapter local catalog audit retained all seven
source requirements and nine obligations; its report is
`module_compatibility/host_adapter/local_catalog_audit.json` beside the run.
This report explicitly records `agent_execution: not_run` and is not an agent pass.

The deployed `server/validation/run_pipeline.py` entry point is absent from this
checkout. A new explicit module-probe route checks its installed consumer before
running agents. Ordinary legacy validation and backend paths continue to reject
Experimental snapshots/module probes; module-only evidence never becomes system
or backend approval. No real remote upload, agent invocation, paid call or backend
run was performed while implementing these adapters.
The full orchestrator suite passes 135 tests with the same four legacy-fixture
skips; five focused publisher/consumer guard tests pass after the final guard
addition. The real saved build's publication and module preparation used native
handoff/approval validation. Python compilation and whitespace checks pass.

#### Deployed preflight and one-module verification

Use the full immutable mailbox as the source; the adapter prepares a fresh
module-only YAML/SV/context snapshot for each invocation:

```sh
python -m shared.top_orchestrator validation \
  --mailbox shared/mailbox/20261027T233850Z_47d4ee60/revision_001 \
  --module-probe ddr4_controller_functional_3200_host_adapter \
  --preflight-only
```

`--plan` previews with no writes/prompts/connections. `--preflight-only` uses the
existing SSH/Slurm settings (and optional `--remote-config`) and asks for SSH
credentials through the existing flow. It creates an isolated remote workspace
and uploads the module inputs plus the runner/preflight helper; it does not
deploy or overwrite the server's application code. The explicit probe selects
module mode regardless of the ordinary system-verification default.

The helper disables Python socket connections before importing the deployed
reader and executing conventional pipeline `--help` discovery. It requires
`SPECIFICATION_CATALOG_VERSION=1` and tests actual catalog/combined-result behavior
against every source requirement, obligation and the full behavioral contract.
Missing metadata or unexecuted obligations marked as passes stop before agents.
The deployed entry point must advertise the module `--jobs` interface; this is
CLI discovery, not proof of behavioral compatibility. Reader and pipeline hashes
are recorded and checked for changes. Preflight has a 45-second helper bound and
uses the shorter existing preflight Slurm/client budgets. A successful capability
receipt has phase `consumer_preflight_complete`, no pipeline return code/summary,
`system_verification: not_run` and `backend_ready: false`.

After deployed preflight succeeds, run the same command **without**
`--preflight-only` to execute the existing verification agents. That can make paid
calls. Every executed invocation repeats preflight, then requires the normal
stage/summary PASS gates plus one complete source-linked PASS requirement catalog
in the actual generated reports. The local adapter independently reconstructs
the expected catalog from the selected YAML and verifies the retrieved coverage
and report digest. Missing obligations, changed source objects, unknown/untested
statuses or a summary-only PASS do not establish module compatibility.
Source-linked PASS rows must identify the simulation method and contain nonempty
execution evidence; static-only or empty-evidence rows cannot satisfy this gate.

Receipts bind invocation ID, isolated snapshot digest, parent mailbox digest,
parent RTL revision, module name, reader/pipeline/helper hashes and reports.
Parent authentication and exact YAML/SV/context projection checks occur before
transfer and after collection. An executed PASS records `one_module_only` scope;
it does not unlock backend or satisfy multi-module/system requirements. Existing
module-agent report code may require deployment changes to retain source metadata;
the adapter reports that incompatibility instead of weakening the coverage gate.

Ten offline transport/receipt tests use real uploads/runner/report collection
against local fake agents, including successful preflight and module execution,
old-reader rejection, blocked network access during help, missing source evidence,
NOT_TESTED obligations, input/consumer mutations, replayed identity and dry-run
side effects. The full orchestrator suite passes 146 tests with the same four
legacy-fixture skips. A separate local check of the real saved host-adapter pair
retains all 17 source entries (seven requirements, nine obligations and one
behavioral contract); `local_consumer_preflight_v1.json` explicitly identifies its
CLI as an argparse-only fixture, not the deployed pipeline. Compilation and
whitespace checks pass. Eleven focused probe/evidence tests pass after the final
receipt-scope and executed-evidence checks. Step 2 live acceptance remains open until deployed
preflight and a real one-module verification invocation succeed.

The first user-operated deployed preflight, run
`20261028T000324Z_ca955527` (October 27, 2026, 19:03 CDT), reached a Python 3.6.8
Slurm worker and correctly rejected the installed reader for missing
`SPECIFICATION_CATALOG_VERSION=1`. No verification agents ran. Deployment must
include the complete updated `server/validation/requirement_verification.py`, not
just the version constant. Result collection also reported a missing remote
`result.json`; the reason is unconfirmed. Retrieval now retries only missing-file
errors up to three fetches with 0.25/0.75-second delays, to tolerate possible
cross-node filesystem visibility lag. It never restarts jobs/agents and does not
retry permission/authentication failures. No remote deployment or rerun was made
while adding this handling.

Add a separate specification-route mailbox publisher; retain the existing
`publish(...)` behavior for expand-spec. Publish the complete validated handoff
and its referenced artifacts: module YAML/SV pairs, structural top, approved
architecture/package, approval evidence, IR, compile order, requirements and event
bindings. Preserve hashes and revision identity. If existing consumers require
`rtl/`, `specs/` and `rtl/manifest.json`, create explicit authenticated projections
while retaining the original richer contracts.

Each functional module already has a lowered YAML contract and matching SV file.
The structural wrapper is currently described by the architecture, rather than a
separate expanded module YAML. Decide its downstream representation explicitly:
either support an architecture-backed structural entry, or add a wrapper contract
if the consumer requires YAML for every SV module. Never silently omit it from
the verification inventory.

First exercise one generated YAML/SV pair through the existing verification
agent's module mode. The lowered YAML shares the old base schema, but also has
structured contracts, specification context and verification obligations. The
current local requirement reader does not extract all of those fields. Extend
the consumer or add an adapter that retains source requirement IDs and full
obligations; schema similarity alone does not establish compatibility. The
deployed verification entry points must also be checked, since their versions and
capabilities are not established by the local helper code.

Multi-module verification needs explicit structural, interface and behavioral
coverage. Existing controller reference semantics cannot automatically verify an
arbitrary timing module, AXI interface or new scheduler. Module passes and local
simulation passes are separate evidence from complete system verification;
missing requirements or unsupported system checks must block backend readiness.
Verification repairs and frontend-revision checks must use the selected build,
never the legacy global frontend directory.

Backend mailbox transport appears reusable because it reads a manifest and
dynamic top name. Confirm compile order, top, clock and manifest compatibility
before relying on it. Verification receipts, backend inputs and GDSII evidence
must all refer to the exact same immutable published revision.

### Orchestrator change map

| Area | Required integration |
| --- | --- |
| `contracts.py`, `__main__.py` | Two route names (`expand-spec`, `specification`), shared CLI help/intake, persisted configuration, mode-specific argument validation and truthful `--plan` output. |
| `frequency.py` and new input adapter | Shared description/constraints/frequency journey, route-specific preparation, complete input snapshots and consistent frontend/backend clock constraints; retain existing expand-spec behavior. |
| `nodes.py` and new frontend adapter | Isolated generation, local checks, bounded failure handling and build-scoped repair/recheck. |
| `orchestrator.py` | Dispatch preparation, nodes and publication by route; retain build identity across transitions. |
| `mailbox.py` and new publisher | Authenticate and publish the full microarchitecture handoff with explicit compatibility projections. |
| `validation.py`, `validation_runner.py`, verification consumer | Validate module compatibility, consume all obligations, select supported system checks and bind results/repairs to the correct revision. |
| Backend mailbox input and GDSII validation | Confirm manifest/top/clock compatibility and exact-revision evidence through physical validation. |

### Implementation checkpoints and acceptance evidence

#### Explicit module verification scope protocol

The frontend/orchestrator side now implements
[module execution plans and scoped result validation](../../../shared/top_orchestrator/VERIFICATION_SCOPE_PROTOCOL.md)
in `shared/top_orchestrator/verification_scope.py`. An authenticated draft starts
unresolved; plan construction requires explicit routing review, full source
coverage, method-specific checks and complete local-contract facet coverage.
The plan retains rich requirement/obligation/contract records plus legacy YAML
invariants/constraints/forbidden patterns. It records environment assumptions
and local contributions separately from full-source dispositions. Multi-owner
sources cannot be certified by isolated module simulation. Metadata and structural
records no longer need fabricated simulation assertions under this new protocol.

The version-1 scoped-result validator requires exact plan/revision/capability,
every assigned check, resolved completed simulation test IDs and hashed contained
artifact evidence. Deferred system records must explicitly remain `unverified`.
It emits only local body-validation evidence, not remote invocation authentication
or system/backend acceptance. This is a separate protocol: the current deployed
all-source simulation collector remains unchanged until consumer support exists.

The frontend-reviewed example for the real host-adapter publication preserves
30 source records, assigns seven checks, and defers 17 full-source records to
system verification. The portable bundle at
`/tmp/capstone-host-scoped-consumer-bundle-20261028` keeps the original isolated
YAML/SV/context snapshot unchanged, with an identity-bound execution plan beside
it. The required consumer capability is `capstone.module_execution_plan.v1`, not
the earlier requirement-reader version marker. Other modules need their own
explicitly reviewed routing; no prose-classification heuristic was introduced.

Single-module capability negotiation and request/result transport are now
implemented behind `validation --module-probe MODULE --scope-plan PLAN.json`.
The wrapper probes `--scope-capabilities` with Python networking disabled and
requires `capstone.module_execution_plan.v1` plus the agreed
`--execution-request`/`--execution-results` flags. It freezes plan/request bytes,
binds helper and consumer-code hashes, and checks unchanged inputs/sidecars.
The client validates downloaded archive/body/artifacts and the normal successful
module summary/exit before saving authenticated `module_checks_only` evidence.
`--preflight-only` and read-only `--plan` are supported, without legacy fallback.
The uploaded helper/wrapper syntax is checked against Python 3.6 grammar.

Scoped batch/repair integration and real deployed/system acceptance remain
pending. The legacy batch and repair paths reject scoped receipts/configuration
until their new evidence mapping is implemented. No unsupported batch is launched
and no scoped failure loses its plan through legacy repair preparation.
No remote calls or paid verification runs were made while implementing this
protocol, and no published mailbox or RTL was rewritten.
Validation completed with 190 orchestrator tests and four existing fixture skips,
including 12 new scope-protocol tests. The real host bundle's plan/file/snapshot
digest bindings and local documentation links were checked independently.
The subsequent live-transport implementation passed the full 200-test suite
with four existing skips, including ten synthetic-consumer transport tests.
Those tests cover preflight without agents, absent capability/network attempts,
mutated inputs/sidecars/consumer code, failed or forged scoped evidence and
receipt/archive mismatch. This is local transport validation, not deployed
Olympus acceptance or real simulation of the scoped checks.

Live run `20261028T015818Z_c4983cb9` passed the deployed Agent 1 reset fix but
stopped in Agent 2 testbench generation. The
[host-adapter investigation](../../../shared/top_orchestrator/HOST_ADAPTER_INVESTIGATION.md)
traces 11 audit blockers to metadata/structural/system requirements being treated
as isolated module simulation obligations. Independent local RTL simulation
passed 4,096 vectors/20,480 assertions and detected an inverted-reset mutation;
this is limited local evidence, not complete system acceptance. The next fix must
coordinate explicit verification scope/method planning with the consumer and
module receipt policy, preserving global obligations as unverified until their
proper checks execute. No acceptance policy or published RTL was changed by
this investigation.

The proposed [Experimental verification integration agreement](../../../shared/top_orchestrator/EXPERIMENTAL_VERIFICATION.md)
is the handoff for the verification owner. It separates the implemented module
catalog/invocation contract from the unimplemented system result interface,
documents reset-checker regressions, native system source IDs and timing scope,
and assigns the remaining consumer/adapter acceptance work. Its proposed system
result envelope and evidence rules require agreement before implementation; they
do not enable readiness or replace deployed capability checks.

#### Build-scoped RTL repair workspace

`shared.top_orchestrator.specification_repair` now provides an offline, explicit
repair lifecycle for a failed Experimental module invocation with
`preflight_only=false`:

```bash
python -m shared.top_orchestrator.specification_repair prepare --mailbox PARENT_MAILBOX --failed-run FAILED_RUN --output NEW_WORKSPACE
python -m shared.top_orchestrator.specification_repair check WORKSPACE
# After an intentional RTL edit to the selected functional module:
python -m shared.top_orchestrator.specification_repair recheck WORKSPACE --build NEW_BUILD
python -m shared.top_orchestrator.specification_repair publish WORKSPACE --build NEW_BUILD --mailbox NEW_MAILBOX
```

Preparation reauthenticates the full source publication and failed invocation's
isolated inputs, parent/module identities, request, receipt and report archive.
Preflight-only evidence, stale/mismatched inputs, modified archives and existing
or nested destinations are rejected. It freezes the full publication in
`baseline/`, native handoff inputs in `candidate/`, and selected failure evidence
in `evidence/`, with a revision-bound `repair_request.json`. Candidate copies
exclude old build status, integration lint and verification handoff pass evidence.
The failure remains triage evidence: a failed consumer gate is not automatically
classified as an RTL bug and no RTL edit or model call is performed.

Only the failed functional module's RTL is editable in this workflow. Changes to
other modules, structural wiring, contracts, IR, architecture or approval are
blocked. Contract/architecture repairs must use a new approved frontend build;
this path does not manufacture approval or weaken the submitted requirements.
Workspace checks compare the entire candidate file/hash map with its baseline,
and reject symlinks and out-of-scope additions/removals as well as edits.

Native `local/frontend/recheck_specification.py` validates the exact approved
architecture, regenerates contracts/IR/structural wiring, compares them with the
candidate, and runs fresh integration lint on the candidate RTL without using
old generator caches or making model calls. Approved serialization is preserved
after semantic comparison. A new handoff and build status are emitted only after
lint and native authentication pass. The orchestrator rechecks candidate identity,
requires a changed RTL revision and stores candidate/build hashes in a new local
recheck receipt. Existing/partial output builds are never resumed or overwritten.

Publication requires that receipt and unchanged candidate/build hashes, uses the
existing native authenticated publisher, and writes a publication receipt plus
the old/new revision recheck plan. Old module results cannot transfer; all modules
and structural/system coverage require fresh verification. Backend readiness
remains false. This is a manual patch/recheck/publication path, not an autonomous
verification-driven model repair loop or a replacement for the verification
owner's consumer fix.

Offline acceptance used the saved host-adapter failure to prepare
`/tmp/capstone-repair-workspace-20261027`, with a comment-only candidate edit
explicitly labelled as a workflow fixture rather than an RTL bug fix. Fresh
lowering, Verilator lint, native handoff authentication and publication to
`/tmp/capstone-repaired-mailbox-test-20261027` succeeded. The resulting RTL revision
is `6e377dc02d48a4a99d828a3870e99fd48a5b024eac34dc210a4467688547a32a`;
the recheck plan identifies only host-adapter RTL as changed, requires all eight
functional module checks plus structural/system checks, and rejects old receipts.
The original repository mailbox and RTL were not edited. A real generic-register
fixture additionally exercises native repair/republication and stale-approval
rejection with networking disabled. No verification agents or model calls ran.
The full orchestrator regression suite passed: 178 tests, with four existing
fixture-dependent skips. Focused repair tests cover scope violations, stale
parent identity, changed evidence, symlinks, missing patches, failed native
recheck and edits after recheck preventing publication.

#### Offline backend handoff preparation

`shared.top_orchestrator.specification_backend` authenticates a full Experimental
mailbox and prepares a new local staging tree without SSH, model calls, synthesis
or physical design. The same `freeze_mailbox` implementation is used by the real
backend transport and this offline adapter; Standard upload behavior retains its
existing top, file tree and transport digest calculation.

```bash
python -m shared.top_orchestrator.specification_backend MAILBOX --output NEW_DIRECTORY
python -m shared.top_orchestrator.specification_backend MAILBOX --verify NEW_DIRECTORY
```

The output contains `rtl/` with byte-identical transport inputs and
`backend_handoff.json`. Checks authenticate the source publication, unique top
declaration, explicit scalar top clock/reset ports from the approved contract,
complete RTL compile inventory and ordered `filelist.f`. An optional
`--target-mhz` must match the published controller target. The period is computed
from that controller target, independently of the functional DDR timing profile.
Existing destinations and destinations inside the immutable source are rejected.
Publication uses a temporary directory and reauthenticates the source after
freezing. Recheck rejects changed staged files, extra files or altered handoff
metadata, including an attempted `backend_ready=true` change.

The backend's `mailbox_revision` is a digest of its RTL transport file/hash map;
it is distinct from the native `rtl_revision` and full `snapshot_sha256`. All
three are retained. Compile-order paths in the source inventory are relative to
the mailbox root (`rtl/...`); the transported `rtl/` contents and its filelist use
paths relative to that RTL directory. The source manifest is not rewritten or
presented as a different authenticated revision.

The existing real publication `20261027T233850Z_47d4ee60/revision_001` passed
offline preparation: nine RTL sources, top `ddr4_controller_top`, target 200 MHz,
period 5 ns; transport digest
`c5521c5ab0668a120840d4879f05e1a9765a9748e3bb6813691c626a81578018`.
The initial review artifact is `/tmp/capstone-offline-backend-20261027`.
Remote prepare compatibility, use of the ordered filelist in the deployed
backend, resulting clock constraints and exact-revision GDSII evidence are still
unestablished. The report records physical design as `not_run` and backend
readiness as false; existing Experimental backend execution gates remain active.
All 17 focused offline-handoff/publication and existing backend transport tests
passed. The real build's staged handoff also passed the read-only recheck.

#### Main CLI integration for prepared operations

The main entry point now exposes the existing bounded adapters directly:

```bash
# Pure local preview, no artifact writes or credential prompts.
python -m shared.top_orchestrator backend --mailbox MAILBOX --offline-handoff --plan

# Create a new local run with state.json and backend_handoff/; no SSH/tools.
python -m shared.top_orchestrator backend --mailbox MAILBOX --offline-handoff --run-dir NEW_RUN

# Preview the module inventory and remote capability-check route, without SSH.
python -m shared.top_orchestrator validation --mailbox MAILBOX --module-batch --preflight-only --plan

# User-operated remote capability checks; no verification agents.
python -m shared.top_orchestrator validation --mailbox MAILBOX --module-batch --preflight-only

# User-operated paid verification agents, after deployed compatibility fixes.
python -m shared.top_orchestrator validation --mailbox MAILBOX --module-batch
```

These are operations on a published Experimental snapshot, not additional
frontend routes. Both support `--plan` and an optional new `--run-dir`. Batch
supports `--remote-config`; offline staging accepts an optional matching
`--target-mhz` and rejects remote options. Generation/debug controls and bypass
flags are rejected rather than silently ignored. Existing Standard entry routing
and the one-module probe remain unchanged.

`state.json` uses `capstone.experimental_integration_state` for these bounded
operations. Status explicitly distinguishes `offline_checks_passed`,
`consumer_preflights_passed`, `module_checks_passed` and `needs_attention`.
Execution requires complete collected module evidence in addition to successful
dispatch; a capability preflight is never module acceptance. All statuses retain
`system_verification=not_accepted` and `backend_ready=false`. This does not enable
automatic generation-to-system-verification/backend execution. The offline
handoff remains separately recheckable using its `backend_handoff/` directory.
Validation: the full orchestrator suite completed with 170 tests, four existing
fixture-dependent skips, and no failures. The real published build also passed
the main CLI offline-handoff `--plan` path without credentials or remote setup.

Checkpoint 3 offline preparation can proceed while checkpoint 2 live acceptance
waits on the verification owner's reset-checker investigation. The deployed
consumer preflight passed in `20261028T000836Z_1ce9b2a1`; the subsequent module run
`20261028T001145Z_9665a3ac` passed syntax and compilation but Agent 1 rejected
reset detection on the stateless host adapter. Combinational ternary reset gating
is present; the checker limitation remains a diagnosis to confirm with its owner.
Agents 2/3 did not run and no behavioral acceptance is claimed.

`python -m shared.top_orchestrator.specification_verification_plan MAILBOX`
now authenticates the immutable snapshot and prints an offline JSON coverage
plan. It retains every functional module's exact source requirements, obligations
and behavioral contract, includes the architecture-backed structural top, and
binds the inventory and compile order to the snapshot digest and RTL revision.
Every check starts as `not_run`, system consumer support is `not_established`, and
backend readiness remains false. This is preparation for a future receipt and
system-consumer adapter, not an executable multi-module verification workflow.
Repairs must publish a new revision and recheck affected coverage. No model calls,
SSH deployment or verification-agent modifications are part of this preparation.

#### Checkpoint 3 module dispatch and evidence collection

The `shared.top_orchestrator.specification_validation` adapter now supports:

```bash
# Read-only, authenticated inventory; no SSH or model calls.
python -m shared.top_orchestrator.specification_validation MAILBOX plan

# Read-only collection of explicitly selected previous module runs.
python -m shared.top_orchestrator.specification_validation MAILBOX collect RUN_DIR [RUN_DIR ...]

# Remote capability checks for all functional modules; no verification agents.
python -m shared.top_orchestrator.specification_validation MAILBOX run --output NEW_RUN_DIR --preflight-only

# Paid verification-agent execution; user-operated after consumer fixes deploy.
python -m shared.top_orchestrator.specification_validation MAILBOX run --output NEW_RUN_DIR

# Compare two authenticated publications, without executing repair.
python -m shared.top_orchestrator.specification_validation NEW_MAILBOX repair-plan --previous OLD_MAILBOX
```

Batch dispatch preserves inventory order, owns a new output directory, uses the
existing one-module invocation path, and stops on the first unsuccessful module.
It does not retry failures or publish/repair RTL. Each module has separate input,
request, report archive and receipt evidence. SSH credentials are held in the
existing in-memory password session. `collection.json` explicitly separates
dispatch outcome, module coverage, system coverage and backend readiness.

Collection reauthenticates module inputs against the selected parent, binds both
local/remote receipts to module name and parent revision/digest, checks invocation
and input snapshot identities, and reopens the hashed report archive. The full
source catalog, simulation evidence and normal pipeline summary must pass again.
Preflight-only, stale, duplicate, changed or incomplete evidence is rejected;
missing modules remain pending. All module passes still leave structural/system
verification pending and backend readiness false. The plan includes the native
handoff's full `verification_intent` and `shared` event/type context so integration
requirements are not reduced to a union of module checks.

Repair comparison identifies added/removed/changed module RTL and contracts and
records old/new snapshot identities. For now it conservatively requires all
functional modules and structural/system checks on the new publication; old
receipts cannot transfer. Automated scoped repair and narrower dependency-based
recheck are not implemented.

**Still required to finish checkpoint 3:** the verification owner must provide a
compatible system consumer, with explicit support for this functional timing
model and architecture-backed top. The acceptance interface must retain the
native handoff's system requirements, verification obligations and event
bindings, and return executed evidence for structural connectivity, interface
handshakes, reset, simultaneous events and end-to-end behavior. Results must bind
to the immutable snapshot digest, RTL revision and complete compile inventory;
unsupported or unexecuted obligations must remain incomplete. The orchestrator
system invocation/receipt adapter must then be implemented and exercised against
that interface. The manual build-scoped RTL patch/recheck/publication lifecycle
above is implemented; automated verification-driven model repair remains future
work. Existing Standard controller
reference semantics must not be silently reused for arbitrary Experimental
architectures. No claim of checkpoint-3 completion is made by module collection.

Offline validation: 51 module-probe, publication and existing validation tests
passed; an additional focused rerun verified multi-module stop-on-first-failure
behavior after strengthening that case. Local collection of the existing failed
`20261028T001145Z_9665a3ac` run correctly leaves all eight functional modules
pending and backend readiness false. It did not launch a remote job or paid call.

1. **Selectable isolated frontend.** Add the two-route selector, shared text and
   frequency intake, new adapter, owned output directory and accurate dry-run
   plan. Complete a frontend-only specification-route
   run. Regression tests must show that the default route retains its existing
   commands, paths, controls and repair behavior. Check equivalent text/frequency
   entry on both routes, explicit clock conflicts, consistent route names in state
   and plans, and absence of a third direct-microarchitecture orchestrator route.
2. **Mailbox and one-module verification.** Publish an authenticated build and run
   one real generated YAML/SV pair through the existing verification agent. Resolve
   requirement extraction and wrapper representation before claiming compatibility.
3. **Multi-module verification and repair.** Exercise complete module inventories,
   integration requirements, scoped repair/recheck and revision-bound receipts.
   Verify that incomplete coverage cannot become backend readiness.
4. **Backend and GDSII handoff.** Carry the exact accepted snapshot through backend
   staging and physical validation, confirming top, compile order and clock.

Across these checkpoints, test missing YAML/SV pairs, tampered files, stale
approvals and verification results, unsupported check profiles, and accidental
reads from global legacy outputs. Live paid LLM runs remain user-operated; offline
adapter and regression tests should not contact model endpoints. Initial
orchestrator implementation and subsequent user-operated live acceptance are described above.
