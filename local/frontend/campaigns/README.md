# Frontend design campaigns

The initial timing campaign separates request preparation, generation/review/lint,
and independent behavioral evidence. Preparation never calls an LLM:

```sh
python3 local/frontend/prepare_timing_campaign.py
```

Use `--output-dir PATH` for a fresh explicit directory. The default is
`local/frontend/microarch/campaigns/timing/<timestamp>_<id>/`; existing directories
are rejected. Runtime campaign output is ignored by Git; definitions in this
source directory are versioned.

A prepared campaign contains `campaign.json`, the exact `planning_context.json`,
`library_catalog.json`, and per-case request YAML/description files. Fingerprints
identify the definitions, requests, library catalog and full planning context.
Preparation is `prepared_not_generated`; no case is marked behaviorally passed.
Each independent paid trial must use a fresh output directory and retain the
request, model/context identity, generation/repair/review usage, lint results and
separate checker evidence. The manifest is a preparation snapshot, not an
aggregator that automatically updates when a user runs a command elsewhere.

## First cases

| Case | Checker readiness |
| --- | --- |
| `timing_guard_ddr4_2400` | Executable isolated permission profile using local DDR4-2400 bindings. New generation trial has not run. |
| `timing_guard_ddr4_3200` | Same profile using DDR4-3200 bindings. New generation trial has not run. |
| `reloadable_timer_t1`, `t2`, `t31`, `t32`, `t33` | Requests/acceptance criteria prepared; dedicated reloadable-timer checker is planned. Do not treat these as checked designs. |

Guard requests specify exactly one datapath timing-role contract with the fixed
checker interface and generic design type, without controller queue/scheduler/
memory decomposition. Later composition cases can deliberately expand that ABI. Reloadable cases lock
reset/start/expiration priority, pause behavior, enabled-edge counting and done
pulse duration before generation. Width cases straddle a power-of-two boundary.

The next paid design trial should be the DDR4-2400 guard, using its `request.yaml`
as input to `run_flow.py --specification`, fresh specification/review/repair/build
paths and the normal review/lint gates. After handoff export, use the checker
below. Paid generation is deliberately not invoked by preparation or simulation.

## Local library context

`planning_library.py` provides a compact catalog of existing reusable templates:
interfaces, clocks/resets, symbolic timer substitutions, dependency models, source
hashes and template fingerprints. It also exposes the actual accepted role
vocabulary and timing-source notes, including documented project simplifications.
Catalog membership is not verification approval, a capability restriction or a
complete behavioral contract. New custom modules remain allowed within the
compiler's capabilities. Detailed contract behavior cannot be inferred from
omitted implementation bodies.

Initial/fresh text specification planning now receives a small discovery index
with optional/unverified feature names and the catalog fingerprint, alongside the
existing schema/example/full timing library. The richer interface/provenance
catalog remains in `library_catalog.json` in prepared and generated source
directories. Selected detailed template-contract injection is future work; no
automatic template transplantation or new reuse schema is introduced. Locked
repairs omit the discovery index, retaining the complete request, schema, timing
library and locked design. This keeps the large-controller prompt fixtures within
the existing 80k reservation without raising limits or dropping design behavior. Generator revision 19 and context hashes
invalidate stale specification-generation caches when this evidence changes.
Existing approved exports and exact-spec RTL resume caches remain valid; the old
expand-spec/FSM defaults and direct architecture route are unchanged. AXI-specific
contracts/checkers and broader role/schema changes are later work.

## Isolated timing permission simulation

This closes the timing-probe coverage gap in the single-request full-controller
simulation. Run against an authenticated RTL-phase handoff, including the
existing generated controller build:

```sh
python3 local/frontend/simulate_handoff.py \
  local/frontend/microarch/builds/text_runs/20261027T182419Z_27739196/build/verification_handoff.json \
  --profile ddr4-timing-guard-v1 --seed 1
```

The driver instantiates the one timing-role module, never the controller top.
The handoff must declare the matching four-bank, 45-bit command interface and
synchronous active-low reset. The shared timing values must be cycle minima for
tRCD/tRAS/tRRD/tFAW. Permission is checked against independent absolute commit
history; no internal counter/register is read.

Nine bounded scenarios cover reset/invalid/reserved inputs, per-bank tRCD and
tRAS, global tRRD, four-ACT tFAW blocking/expiration, simultaneous expiration and
new commit, probe/non-ACT noninterference, reset of live history, long idle and
seeded mixed inputs. Boundary scenarios require actual T-1/T/T+1 observations;
tFAW requires both exclusive window blocking and expiration with a new ACT.
Trace length must match the complete programmed scenario, preventing a truncated
trace from passing on a few probes.

Bank-state inputs and commit events are explicit unit-test stubs. In particular,
the fifth-ACT probe supplies a closed bank to exercise the tFAW logic independently
of the full controller's tRAS/open-row lifecycle. This is component input-space
verification; not every probe/commit sequence is claimed reachable in the full
controller. All source requirements remain unverified in these bounded reports.
Whole-controller simulation, reloadable-timer semantics, AXI, physical DDR/PHY and
exhaustive coverage are not established by this profile.

Preparation and simulation are offline. Tests include independent hand-written
histories and deliberate early/late/reset/window permission faults:

```sh
python3 -m unittest discover -s local/frontend -p test_timing_campaign.py -v
```

## First user-run generation trial

This command makes live model calls; preparation and local simulation do not.
Set `CAMPAIGN` to the directory printed by the preparer. Keep model choices
consistent between comparative trials; normal code defaults apply unless an
explicit model override is supplied.

```bash
CAMPAIGN=/path/to/prepared/campaign
TRIAL_DIR="$(mktemp -d /tmp/capstone-timing-2400-XXXXXX)"
set -o pipefail
python3 -u local/frontend/run_flow.py \
  "$CAMPAIGN/cases/timing_guard_ddr4_2400/request.yaml" \
  --specification --specification-output "$TRIAL_DIR/source" \
  --architecture-output "$TRIAL_DIR/architecture.yaml" \
  --automated-spec-review --spec-review-output "$TRIAL_DIR/review" \
  --auto-spec-repair --spec-repair-output "$TRIAL_DIR/repair" \
  --build-dir "$TRIAL_DIR/build" \
  2>&1 | tee "$TRIAL_DIR/frontend.log"
```

Only after a successful build, run:

```bash
python3 local/frontend/simulate_handoff.py \
  "$TRIAL_DIR/build/verification_handoff.json" --profile ddr4-timing-guard-v1
```

The baseline guard requests explicitly lock one timing contract and the checker
ABI, including shared type names/packed layouts. They do not test free module
partitioning; later composition cases will use separate acceptance profiles.

## Groundwork validation

The existing generated DDR4-3200 timing module passed all nine isolated scenarios
for seeds 1, 2 and 17: **27 scenario passes, 6,444 edges**. This validates the
reference component and checker; the seven newly prepared requests still have
no generated/verified design trial. Original RTL, caches and handoff contents
were unchanged, and the saved approval still validates.

All **599 frontend tests passed**, including 14 new groundwork tests. The 124
focused regressions passed, along with Python compilation and whitespace checks.
The four large-controller initial/repair prompt fixtures remain below the existing
80k reservation (architecture repair: 79,958). The rich catalog initially violated
that budget; keeping a small discovery index for planning and omitting it during
locked repairs fixed the regression without changing limits or design facts.
No paid model call was made.

## RTL review stage boundaries (campaign version 2)

The first user-run DDR4-2400 trial reached RTL generation but exhausted three
review rounds on representation objections. Specification bindings were already
valid and lowered to 17/39/4/30 cycles. The reviewer nevertheless requested YAML
binding behavior in SystemVerilog, then typedef spelling, then incorrectly called
`typedef row_t [3:0] bank_rows_t;` unpacked. Local Verilator probes confirmed the
16-bit/4-bit packed port widths and bank-zero-at-LSB ordering. The saved final
candidate separately passed all nine timing scenarios in an unapproved diagnostic
run; this did not create a production approval, accepted RTL/cache or handoff.
The failed live trial's estimated cost was $1.211980.

Campaign definition version 2 makes the distinction explicit: shared type names
and `timing_cycles.*` paths constrain the specification package; resolved values
and packed layout constrain RTL. Equivalent packed vectors/structs and numeric
constants are permitted without a mandatory matching SystemVerilog typedef name
or invented runtime YAML lookup. Original version-1 campaign/trial files remain
unchanged; running the preparer again creates a fresh version-2 snapshot.

`rtl_lowering_context.py` derives deterministic facts from lowered module metadata:
complete public ports, source type references/layouts, concrete parameters and
selected timing values. They are marked as elaboration facts, not candidate
verification. Generation, full/compact review, merge and payload recovery receive
the same scoped policy for specification-derived modules. The original request,
contract and implementation constraints remain in context. Explicit RTL naming/
style directives, public packing/order, timing/reset behavior and other real
constraints still apply; genuine conflicts still stop for clarification.

Legacy modules keep their existing prompts and FSM routing. No reviewer finding
is filtered, suppressed or automatically accepted. Lint, review/round limits,
recovery budgets, reviewed-cache policy and final integration gates are unchanged.
The additional context interprets constraints at the correct stage; it does not
replace semantic review with a compiler pass.

Offline regressions include exact policy forwarding through all four model
stages and payload recovery, legacy isolation, preservation of explicit SV style,
continued rejection of actual timing/width/order/style findings, ambiguity stops,
and actual Verilator probes distinguishing packed aliases, truly unpacked public
arrays, and correct-width but wrongly ordered bank data. The existing failed
trial's approval remains valid and can be resumed without regenerating or
re-reviewing its specification. A read-only request-capture audit is saved at
`/tmp/capstone-rtl-stage-boundary-m14glwjt/audit.json` with lowering facts, prompt
hashes and budget checks; no paid call was made. Live reviewer consistency remains
to be checked by the user's continuation.

Stage-boundary validation passed all **611 frontend tests**, including 12 new
regressions. All 132 focused tests passed; Python compilation and whitespace
checks passed. No paid model call or mutation of the original failed trial was
performed. These checks validate prompt routing, deterministic elaboration facts,
compiler packing behavior and unchanged acceptance gates; the next user-run live
review is still required to measure the effect on reviewer consistency.
