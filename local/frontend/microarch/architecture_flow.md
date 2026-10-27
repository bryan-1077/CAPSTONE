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
