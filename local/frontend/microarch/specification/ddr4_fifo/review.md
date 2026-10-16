# First-target review — 2026-10-15

The user accepted the draft defaults and requested continued implementation.
`reviewed_003` recorded those choices. Current revision `draft_005` preserves
them, includes declarative event/observation bindings, and clarifies engine
progress between handshake events. Those additions and their concrete export
await user package review. Behavioral obligations remain unexecuted;
the review record is not a revision-bound RTL approval.

## Request lifecycle

| Event | Storage/state owner | Effect |
| --- | --- | --- |
| Admission | Waiting queue; host adapter maps fields | Capture every ready/valid edge as one distinct request |
| Dispatch | Queue removes old head; engine captures it | Transfer the complete payload atomically; free one waiting slot |
| PRE/ACT commit | Engine sequences; row state and timing update | Retain active ownership; do not retire |
| READ/WRITE commit | Engine retires; memory handles data | Release active request; capture a read response separately |
| Response delivery | Memory owns slot; adapter passes it through | Host samples registered valid/data at the edge after READ commit |
| Close-page cleanup | Engine owns a separate bank reservation | Block dispatch until legal PRE commit; do not retire twice |

Four waiting entries and one active entry are independent capacities. Pending
response storage consumes neither capacity. No empty-queue bypass is permitted.
Full-queue dispatch/admission captures the old head and appends the new tail on
the same edge, including capacity one. Retirement alone cannot free a waiting
slot, and cannot enable same-edge active replacement. FIFO order follows
acceptance order across physical slot reuse.

## Compatibility findings

Source review used `generate_wrapper.py`, especially its assignments of
`downstream_cmd_ready`, `incoming_timing_ok`, `txn_ready`, and its sequential
`service_pending_q` completion block. The archived RTL is reference material;
the generator is the current behavior source.

| Topic | Legacy behavior | Accepted first-target behavior |
| --- | --- | --- |
| Selection | `simple` prefers row hits, then lowest valid slot | Global FIFO, including same-address write/read order |
| Host admission | `txn_valid && txn_ready`; queue readiness controls admission | Preserve this handshake independently of timing/engine readiness |
| Active replacement | Pre-edge busy state prevents dispatch on completion | Preserve no replacement on retirement edge |
| Response | Completion registers data/valid; host can sample next edge | READ commit at E registers data/valid; delivery at E+1, no extra adapter stage |
| `cmd_ready` | Idle and coarse row-hit/activation gating | Idle, no cleanup/refresh exclusion, and legal incoming first command; never an admission or immediate-service promise |
| Refresh | Simplified periodic request/acknowledgment | Drain, legal per-bank PRE, REF, then tRFC exclusion |

The first-command probe uses ACT for a closed bank, access for a row hit, and PRE
for a conflict. It reads pre-edge row/timing history, independently of queue
selection and commit. Thus a conflict may report ready to PRE even though ACT
cannot yet occur; a row hit may still be blocked by tRCD. Queue fullness and
`txn_valid` do not change what the diagnostic means.

The compatibility preset is now `current_controller_v2`. Legacy configurations
still attach it automatically and retain explicitly requested `simple` policy.
Generic requests may explicitly select it or omit/disable it. Waiting capacity
defaults to four and can be set through `microarchitecture.waiting_capacity`.
The changed contract/compiler fingerprint requires regenerating old proposals.

## Ownership and timing

The eight module contracts keep host translation, waiting storage, selection,
sequencing, row state, timing, memory/responses, and refresh control separate.
The existing architecture validator requires distinct owners for the five major
roles and rejects clock/reset-only helper decomposition. The top is structural.
Refresh and timing both observe REF for their different responsibilities:
refresh owns pending/deadline/service control; timing owns command permission.
They must agree on the same tRFC boundary.

Timing uses committed-command edges: a trigger at E permits the affected command
at E+T. Probes and blocked candidates insert no history. A blocked registered
candidate survives newly pending refresh. Refresh waits for active work and
cleanup, closes remaining banks, and honors all-bank tRP before REF. Pending
deadlines coalesce; no physical refresh-debt guarantee is claimed. Reset requires
a sampled rising edge to cancel state, while event/valid gating is immediate.

## Evidence and remaining work

The package checker validates all 36 declared configurations. It found missing
`V_ACTIVE` links in the queue and scheduler contracts; these are now attached to
their existing FIFO rule ownership. Regression tests also reject broken
references, ownership links, drivers, types, clocks/resets and declared feedback.
These are structural checks, not execution of the 21 behavioral obligations.

Validation on 2026-10-15: 27 architecture regressions and 12 package-checker
regressions passed; the CLI matrix check passed all 36 configurations.
The subsequent lowering/approval suite adds 13 passing regressions, including all
36 configurations lowered through the architecture/IR validator. Approved build
orchestration is exercised with interface-only fixtures, not a controller RTL
implementation. The final package suites pass 25 tests alongside the 27 existing
architecture regressions.

Contract lowering now exports eight existing-route module specs, resolved packed
layouts/timing, and a portable architecture manifest. Package validation is
integrated into export and replay; explicit approval binds the source snapshot,
configuration and compiled manifest before a specification-derived RTL build can
write outputs. No approval of the concrete first-target export has been recorded.

System-spec generation before module selection is now implemented as an opt-in
staged route. Remaining first-target work is user review of the concrete export,
live model/RTL generation, and independent downstream module/system behavioral
tests. No adoption of this flow as the default is part of this review.

The new version-1 frontend handoff includes source rules/scenarios, typed port
bindings, explicit observation gaps and an exact file inventory after RTL exists.
Existing downstream UVM/vector system verification requires a consumer adapter:
it expects the legacy configuration/testbench/private-wrapper signals and permits
reordering before dispatch. Its fixed requirement catalog must not silently stand
in for this package's FIFO/command/refresh rules. That implementation is outside
the frontend task; handoff validation remains distinct from consumer readiness.

Frontend validation after adding the handoff: 64 regressions passed, including
12 new binding/inventory/portability tests. These cover renamed instances,
configuration elaboration, packed-field extraction, explicit observation gaps,
stale/modified inputs and the build export. Downstream UVM/vector tests were not
run. The orchestrator still needs to select and transport an isolated build.

## Frontend run preparation — draft_005

The contract inspection covered queue admission/dispatch and full reuse, active
ownership and candidate stability, response capture/delivery, both page policies,
commit-driven bank/timing state, refresh drain/precharge/exclusion and reset.
The graph and binding review used declared module ports rather than private RTL.

| Agreement | Inspection result |
| --- | --- |
| Waiting storage / scheduler / engine | Dispatch transfers the same pre-edge payload; no empty bypass or retirement-edge replacement; full simultaneous reuse retains FIFO order |
| Candidate / permission / commit | Timing queries are independent of commit; all consumers share one commit and payload; blocked candidates survive pending refresh |
| Engine progress between events | Clarified with an explicit every_clock update: form a new registered candidate only after dispatch/previous commit, and observe refresh_pending as an input level; the private refresh_due event is not an engine input |
| Refresh / timing | Both use REF commit at E and release exclusion for E+tRFC; preparation closes rows and waits all-bank tRP; same-edge due wins pending acknowledgment |
| Memory / host adapter | READ at E captures pre-edge data; host samples at E+1; no adapter register or response backpressure; response storage outlives active ownership |
| Observation gaps | refresh_due/end remain explicitly unbound; downstream checks must derive deadlines/intervals independently from reset/profile and declared observations |

No additional observable behavior changes were introduced by this clarification.
The initial configuration is four banks, four waiting entries, FIFO, open-page,
DDR4-3200. `request.yaml` now expresses this target for the staged generator;
the supplied package still supports its declared 36-configuration matrix.

The opt-in request route validates and locks system behavior before decomposition,
preserves exact request/constraint IDs and text, derives architecture-specific
intent/bindings, and retains unsupported/repair/replay evidence. Offline tests
cover both a generic one-module register and controller baseline transport.
The generic fixture exercises real IR generation, cache-only RTL emission,
Verilator lint, handoff validation, repeated cache replay and failed-rebuild
handoff invalidation. It does not establish DDR4 controller behavior or live
model/provider availability. The concrete target export still requires user
approval before RTL generation; no target approval has been fabricated.

Final preparation evidence: 78 frontend regressions passed; the focused staged
suite passed 14 tests after the last validator adjustment. The fresh supplied
draft_005 export is under `microarch/proposals/specification/a8d815ca95b23bc5/`
(manifest prefix `cc709aaf0b96ac0c`). Its review-phase handoff validates, with
behavioral verification `not_run`, no RTL revision and no recorded target
approval. Python compilation, shell syntax and diff whitespace checks passed.

## First live specification attempt — follow-up on 2026-10-16

The user's terminal reached the model API but did not complete system validation.
The prompt supplied a full package as an example while forbidding compiler-owned
fields in the first-stage response. Reset checking also required a verbose
example-specific style string and reported no actual/expected value. The final
model response inferred that the baseline mandated mixed reset styles; it permits
optional reuse and does not mandate asynchronous or mixed-style blocks.

The fix gives each stage an example matching its schema, accepts canonical
`synchronous` alongside the legacy alias, shares reset validation with lowering,
and makes repair errors identify the field and expected/received values. Rejected
and unsupported responses are now journaled, rather than discarded on failure.
The requested reset semantics and baseline requirement text are unchanged.
Generator/lowering revisions are now 2/3; older concrete exports need regeneration
before review. The earlier export hashes above are historical evidence. No live
target package has passed validation yet, and no target RTL approval is recorded.

Follow-up validation on 2026-10-16: all 81 frontend regressions passed. Canonical
synchronous reset is exercised with the unchanged first-target request and
baseline, while the legacy style remains accepted. Stage examples, actionable
repair diagnostics and full response journals have regression coverage. The
corrected live generation has not been retried from this session.
