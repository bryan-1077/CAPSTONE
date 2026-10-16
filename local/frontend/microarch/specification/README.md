# System specification — first controller target

[ddr4_fifo/package.yaml](ddr4_fifo/package.yaml) is a hand-authored example for
implementation, covering the Section 2 first target: requests/FIFO, commands,
bank state, timing, both page policies, functional memory/responses, and refresh.
Revision `draft_005` preserves the defaults accepted in `reviewed_003`, includes
typed verification-event bindings, and clarifies clock-driven engine progress
and observation of refresh pending. It includes all
eight module contracts and a typed connection graph. It is not an executable architecture
manifest or an approved RTL-generation input. `specification.py` checks the
package, and `specification_lowering.py` now exports executable module YAMLs and
an architecture manifest. The package route in `run_flow.py` requires recorded
approval before passing those specifications to existing RTL generators.

Review the shared definitions first, then the lifecycle rules and module
contracts, then [execution rules](ddr4_fifo/execution.yaml) and
[connections](ddr4_fifo/connections.yaml). The package records proposed decisions
and the remaining RTL-approval question. All behavioral verification obligations
are planned and unexecuted.

The draft preserves the current host port shape and compact address mapping.
FIFO deliberately replaces the existing `simple` row-hit/slot-index policy;
the former `current_controller_v1` baseline rejected that policy change.
The `current_controller_v2` compatibility preset preserves host/data/timing
contracts while accepting a requested scheduling policy and waiting capacity.
Existing proposal files are reference material, not evidence of correctness for
this target. See [review.md](ddr4_fifo/review.md) for the compatibility findings.

## Draft format/schema

This format uses a small field vocabulary and an example, with a Python package
checker rather than a hardware language. YAML mappings use stable IDs;
references are strings, not copied numeric widths or repeated behavioral rules.
Unknown fields and unresolved structured references fail the checker. References
inside behavioral prose are reviewed rather than parsed as a language.

| Document | Required fields | Purpose |
| --- | --- | --- |
| Package | `specification_version`, `package_id`, `design_name`, `revision`, `status`, `scope`, `documents`, `verification_strategy`, `decisions`, `assumptions`, `open_questions`; optional `design_kind`, `review`, `generation` | Revision and review boundary; paths relative to the package file; configuration matrix and system verification strategy |
| Shared | `parameters`, `clock_reset`, `types`, `host_interface`, `events`, `timing_profile` | Single source of shared facts |
| System slice | `slice_id`, `rules`, `verification` | Observable rules and their owners/tests |
| Contracts | `contracts` | Mapping from module ID to its contract |
| Graph | `graph_id`, `clock_reset`, `connections`, `presence`, `integration` | Source endpoint to sink-list mapping, shared clock/reset fanout, conditional ports |
| Verification bindings (optional document) | `binding_version`, `sampling`, `events`, `observations`, `checks` | Declared port/event bindings and obligation observation needs; referenced as `documents.verification` |
| Module contract | `status`, `role`, `owned_state`, `interfaces`, `handshakes`, `reset`, `update_events`, `stalls`, `simultaneous_events`, `invariants`, `verification`, `combinational_dependencies` | Independent behavioral boundary |
| Rule | `origin`, `owners`, `statement`, `verification` | Requirement traceability; origin is a plan section in this example |
| Verification obligation | `rules`, `scenario`, `expected`, `status` | Observable check, with `planned_not_executed` initially |
| Decision | `origin`, `status`, `value`, `reason` | Distinguish requested constraints from proposed design choices |
| Open question | `blocks`, `question` | Missing agreement; never silently supply a default |
| Review (optional) | `date`, `outcome`, `source`, `accepted_decisions`, `rtl_approval` | Design-choice review record; does not itself authorize RTL generation |

References use `shared.parameters.NAME`, `shared.types.NAME`,
`shared.events.NAME`, `shared.timing_profile.bindings.NAME`, `system.rules.ID`,
`system.verification.ID`, `execution.rules.ID`, and `execution.verification.ID`.
Clock/reset references may name `shared.clock_reset` as a whole.
Interface entries have `direction` and `type`; optional `when` defines a
presence condition. Record types list fields in packed MSB-to-LSB order;
unsigned types use either an integer width or a parameter reference. An enum
lists its explicit encoding. Arrays declare `element_type`, `count`, and packed
element order. The checker computes packed widths and elaborates symbolic
parameters. Lowering converts them into fixed-width whole ports, with explicit
record bit offsets and array element order in each module's context. Derivations
support `ceil_log2(NAME)` and bounded `2**NAME`;
presence conditions support integer comparisons such as `BANK_COUNT > 1`.
Other expression forms fail explicitly.

Event `condition` and rule `statement` are precise review prose, not executable
expressions. Port dependencies refer only to current-cycle input signals;
registered state and the shared reset gating are implicit. A rule's `owners`
must name actual contracts with the corresponding invariant and obligation.
Rule/verification references must be reciprocal. A partial package cannot be
approved for RTL while any required contract or blocking question is pending.

## Edge convention and accepted choices

All handshakes sample values immediately before a rising `clk` edge. State after
the edge reflects those events. Reset wins over every event. The queue offers
only previously stored requests: an admission into an empty queue cannot dispatch
on that same edge. A dispatch captures the old head before a simultaneous enqueue
replaces storage. Full-queue admission is allowed when that edge also dispatches.

The engine's dispatch readiness is based on registered idle state, cleanup state,
and refresh exclusion. A retiring active request cannot be replaced on the same
edge for this target; the following edge may dispatch. This trades a cycle for a
simple ownership boundary and leaves timing legality outside the dispatch path.
Queue occupancy therefore changes by admissions minus dispatches, never by
retirement. A captured read response may remain pending after retirement and is
not counted as waiting or active storage.

FIFO age is represented by acceptance order, independent of physical slot index.
The draft does not mandate timestamps, a finite age counter, or ring pointers.
Tests use an independent acceptance sequence number, reset with the scoreboard.
Later reordering policies will need an explicit age/ordering contract of their own.

READ commit at edge E captures response data and raises registered response valid
after E. The host samples the response just before E+1, then the response slot
clears or captures a new read. This is one edge from commit to delivery; adding a
second response register in the host adapter would violate the accepted contract.

Timing minima are measured between committed-command edges. A trigger at E
permits a blocked command at E+T. REF requires explicit legal per-bank PRE and
all-bank tRP completion, then blocks commands before E+tRFC. Refresh pending is
sticky and periodic deadlines coalesce while awaiting service. The engine drains
active service and close-page cleanup before preparing refresh, with queued
traffic retained. The example intentionally does not implement physical refresh
debt, PHY signaling, or additional Section 2F timing rules.

`cmd_ready` queries timing for the incoming request's first needed command while
the engine is idle and refresh/cleanup permit it. A separate probe prevents host
visibility from interfering with FIFO selection or mutating timing history.
This intentionally refines the old row-hit/activation-gate diagnostic. The user
accepted this change and the response sampling convention on 2026-10-15; source
review does not establish behavioral equivalence.

The user accepted the defaults and requested continued implementation on
2026-10-15. This authorizes specification/compiler work. Revision-bound approval
is now enforced by the package build route; no approval for a concrete exported
first-target manifest has been recorded yet.

## Generate a package from a request

`generate_specification.py` implements an opt-in two-stage route. First it
elaborates shared definitions, system/execution rules, decisions, assumptions,
questions and independent verification scenarios. It validates and locks that
result before requesting module contracts, rule owners, typed wiring and event/
observation bindings. The second stage can add internal-interface types without
replacing existing definitions. No RTL is involved in either model stage.
See [generation_contract.md](generation_contract.md) for the model-facing format.

The first-target request is [ddr4_fifo/request.yaml](ddr4_fifo/request.yaml): four
banks, four waiting entries, FIFO, open-page, DDR4-3200. It includes the accepted
edge conventions and the existing controller compatibility preset. It is an
input to generation, not approval of a generated architecture. The hand-authored
`package.yaml` remains an independent supplied-package entry point.

```sh
python3 local/frontend/run_flow.py \
  local/frontend/microarch/specification/ddr4_fifo/request.yaml \
  --specification --microarch-only \
  --specification-output /tmp/ddr4-generated-source \
  --architecture-output /tmp/ddr4-generated-review/architecture.yaml
```

Use fresh output directories for generation. Generated source includes the
portable timing library, raw accepted responses and locked system elaboration.
Generation provenance records the exact request, model, context/system hashes,
internal type additions and repair history. Every original requirement and
configuration leaf becomes a rule with its exact ID/text and request origin;
cross-module owners and obligation links must be reciprocal. A generated
package's declared configuration matrix is checked before publication. These
checks establish traceability and consistency, not semantic satisfaction.

Each model stage gets up to three validation attempts. Unsupported responses
must identify affected requirements, reasons and required extensions. Failures
write a sibling `.failure.json` report without publishing a partial package.
Every returned response, including rejected and unsupported responses, is saved
in a sibling `.attempts.json` journal with its stage, attempt and validation result;
successful packages also contain `attempts.json`. Failure output prints both
diagnostic paths. An unsupported report records the model's assessment, not a
proof that the requested behavior is impossible to implement.
The model cannot fabricate an accepted decision, executed test or review record.
Recorded model decisions are documented choices presented for explicit approval.
Unresolved design questions and assumptions requiring review still block approval.

For offline response replay, supply `--specification-response path/to/responses.json`
with `system` and `architecture` response objects. Generated `responses.json`
uses that format. `--cache` reuses an existing package only when request, model,
generator context and lowering revision match; it makes no proposal calls.
The existing `--microarch`/`--microarch-response` route remains available for
direct architecture experiments and is separate from `--specification`.

Live generation needs `TAMUS_AI_CHAT_API_KEY`. `--microarch-model` selects both
specification-stage calls; RTL keeps its existing generator/reviewer models.
`SPECIFICATION_MAX_TOKENS` controls the specification output budget (default
24000). The user's initial live attempt reached the specification model but did
not complete system validation; a complete live target package and RTL build
remain pending. Prompt examples now use each stage's actual response shape.
The canonical reset style is `synchronous`; the original verbose example spelling
is still accepted. Baseline permission to reuse either reset style does not
require selecting asynchronous blocks or mixing styles. Full mixed-reset package
lowering remains unsupported; the direct architecture route retains its existing
mixed-style reuse support.

Review the exported snapshot/contracts/intent, resolve any design questions,
then record approval and run only the frontend:

```sh
python3 local/frontend/specification_lowering.py /tmp/ddr4-generated-review/architecture.yaml \
  --record-approval /tmp/ddr4-generated-review/approval.json --reviewer bryan
python3 local/frontend/run_flow.py /tmp/ddr4-generated-review/architecture.yaml \
  --spec-approval /tmp/ddr4-generated-review/approval.json \
  --build-dir /tmp/ddr4-frontend-build
# Replay the same module cache after a successful build:
python3 local/frontend/run_flow.py /tmp/ddr4-generated-review/architecture.yaml \
  --spec-approval /tmp/ddr4-generated-review/approval.json \
  --build-dir /tmp/ddr4-frontend-build --cache
```

These commands run module IR/RTL generation, structural integration, lint and
handoff export. They launch no orchestrator, UVM/vector execution or backend.
A rebuild removes the previous runtime handoff before generation; a failed
attempt records failure instead of leaving that prior handoff looking current.
Approval applies only to the exact exported configuration. Supplied-package
`--spec-set`/`--spec-profile` options do not override a generated request; change
the request and regenerate it instead.

`start_flow.sh` forwards these options too. Frontend-only regression coverage:

```sh
python3 -m unittest discover -s local/frontend -p test_generate_specification.py -v
```

## Run the package checks

From the repository root:

```sh
python3 local/frontend/specification.py \
  local/frontend/microarch/specification/ddr4_fifo/package.yaml --matrix
python3 -m unittest discover -s local/frontend -p test_specification.py -v
```

Omit `--matrix` to check the package defaults only. The matrix checks 1/2/4 banks,
waiting capacities 1/3/4, both profiles, and both page policies: 36 configurations.
The API also accepts explicit parameter overrides. Only parameters marked
configurable or declaring allowed values may change; fixed and derived
parameters cannot be overridden. A zero-width host-bank type is allowed only
where its port is omitted in the single-bank configuration.

Checks include duplicate/unknown fields, structured references, packed widths,
enum encodings, rule owners and reciprocal verification links, typed whole-port
connections, conditional presence, drivers, clock/reset fanout, declared
combinational dependencies, and timing-profile bindings. The tool emits JSON
evidence on stdout with behavioral verification explicitly `not_run`. It neither
executes behavioral prose nor proves actual RTL matches declared dependencies.
The opt-in staged generator now synthesizes system specifications before
decomposition. Supplied and generated packages use these checks during export,
saved-manifest replay, and approved builds. Live first-target generation remains
pending; structural validation does not establish semantic request satisfaction.

## Downstream verification boundary

After RTL generation, the existing verification stage performs UVM and vector
testing outside `local/frontend/`; implementing that stage is outside this task.
The frontend should dynamically generate verification intent from the reviewed
system behavior and architecture, then export it alongside the RTL. That intent
includes requirement IDs, properties/scenarios, expected observable behavior,
assumptions, coverage goals, and bindings to interfaces/events in the chosen
architecture. Downstream tools can use this independent contract information
when producing and running executable verification.

Exports now include a versioned handoff with rules/scenarios, expected results,
typed event/observation bindings and a requirement-result correlation contract.
The existing system consumer still needs an adapter. It currently expects legacy
controller manifest v2, sequential wrapper clock/reset discovery, private wrapper
signals and a reference model allowing pre-dispatch reordering. Its requirement
catalog uses its own `SYS-REQ-*` IDs. Those assumptions do not match this structural
FIFO architecture. The frontend does not fabricate legacy configurations, private
signals, a frontend testbench, or equivalent requirement mappings to bypass that
gap. The downstream implementation and its result production remain external.

The exporter writes `consumer_compatibility.status: adapter_required` and retains
the inspected consumer contract gaps. A valid handoff means its contracts and
files are consistent; it does not mean the current consumer accepts it or that
UVM/vector verification passed. Local compiler tests and RTL lint remain separate
from downstream behavioral evidence.

The current orchestrator publisher also selects the frontend's legacy global
`rtl_output/manifest.json`, rather than an isolated architecture build. Its stage
adapter does not yet pass package approval/build-selection options. Explicit
transport selection and consumption of the new handoff must be added outside
this frontend task; exporting a file does not automatically publish or launch it.

### Handoff inputs and validation

Review exports contain `architecture.handoff.json`. After an approved package
build finishes module generation and the selected lint checks, `run_flow.py`
writes `verification_handoff.json` beside the build's `architecture.yaml`.

```sh
python3 local/frontend/verification_handoff.py /tmp/ddr4-review/architecture.handoff.json
# After an approved RTL build:
python3 local/frontend/verification_handoff.py path/to/build/verification_handoff.json
python3 -m unittest discover -s local/frontend -p test_verification_handoff.py -v
```

Schema `capstone.verification_handoff`, version 1, contains:

- Package/revision, source/manifest fingerprints, and an RTL revision when files exist.
- Typed top/module interfaces, roles, structural connections, packed layouts,
  resolved parameters and timing, and the source module specifications.
- Every source requirement/obligation with its stable qualified reference, origin,
  owners, scenario, expected result, declared observation needs and unverified status.
- Explicit event predicates, payload extraction, sampling/reset semantics and
  observation limitations, supplied by the architecture package.
- Relative paths and SHA-256 inventories. RTL-phase exports require the recorded
  approval, exact filelist, primary module definitions, current module YAMLs and
  matching module IR. Helpers remain visible in the declaration inventory.
- A result contract identifying source requirement/obligation keys, stage/test/
  evidence fields, identity matching and allowed result states. This defines the
  interface; consuming actual downstream results is not implemented here.

Validation checks hashes, then reconstructs the handoff from its portable reviewed
snapshot and compares the contracts. A changed input or edited oracle cannot be
accepted merely by updating its hash. The archive can move without original
source-package paths. Review exports have no RTL revision and cannot establish
runtime verification evidence. RTL inventory/declaration checks do not replace
HDL compilation or behavioral verification.

### Declarative observation bindings

`ddr4_fifo/verification_bindings.yaml` supplies data for this architecture. The
staged architecture generator supplies corresponding data for its own modules
and policies through the staged route. The compiler has no FIFO/refresh-specific checker algorithm.
Bindings select declared `top.port` or `instance.port` endpoints and optional
packed record fields. They cannot access private implementation registers.
The handoff gives a DUT-relative path suffix; the downstream environment chooses
its DUT root and attaches observations.

Event predicates support nested `all`/`any` and typed endpoint/field equality.
Enum symbols are resolved from shared encodings; packed field offsets use the
same layout elaboration as RTL-generation inputs. Predicates sample pre-edge
values and are gated by inactive reset according to shared clock/reset metadata.
Temporal algorithms remain in the reviewed rules/scenarios, not this expression
vocabulary. Every shared event must be bound or carry an explicit unbound reason,
and every obligation must name its event/observation needs and module/system scope.

The refresh deadline and end have no dedicated port pulses in this example.
They remain explicitly unbound; downstream checks can predict deadlines from
reset/profile values and inspect pending/busy behavior. The frontend never calls
an observed pending transition equivalent to every refresh deadline. Bindings to
conditionally absent ports fail explicitly; this example instead uses the always
present packed request observation for bank-zero single-bank operation.

## Export module specifications and review before RTL

```sh
python3 local/frontend/run_flow.py \
  local/frontend/microarch/specification/ddr4_fifo/package.yaml \
  --microarch-only --architecture-output /tmp/ddr4-review/architecture.yaml
```

This makes no LLM calls and emits no RTL. The export contains `architecture.yaml`,
eight module YAMLs and a module index, a Markdown review, the complete portable
source snapshot, structural-validation evidence, and per-rule `unverified`
results. Without an explicit output path, exports go under
`microarch/proposals/specification/<package-fingerprint>/`. Nothing is approved
by exporting or validating it.

Use repeated `--spec-set NAME=VALUE` flags and `--spec-profile DDR4-2400` to select
an allowed configuration. For example, `--spec-set BANK_COUNT=1 --spec-set
WAITING_CAPACITY=3` omits both single-bank host ports and removes their dependency
references. A saved manifest already contains its configuration; overrides require
regenerating it from the package.

The compiler preserves the source rule IDs, owner assignments, origins, decisions,
assumptions and verification obligations. Generated specs carry the complete
contract, resolved shared definitions/timing and packed layouts into existing IR
metadata and the dual-LLM context. All eight current contracts use the datapath
route because their algorithms are prose rather than executable FSM transition
tables. Internal state/algorithm choices remain with the RTL implementation.
There are no command-engine or refresh branches in expansion or wrapper code.

After reviewing the exported artifacts, an explicit approval action records the
reviewer and fingerprints of both source/configuration and compiled manifest:

```sh
python3 local/frontend/specification_lowering.py /tmp/ddr4-review/architecture.yaml \
  --record-approval /tmp/ddr4-review/approval.json --reviewer bryan
python3 local/frontend/run_flow.py /tmp/ddr4-review/architecture.yaml \
  --spec-approval /tmp/ddr4-review/approval.json
```

These commands are instructions for a subsequent reviewed build; they have not
been run for the first target. A design-blocking question, proposed decision, or
assumption still marked `requires_review` prevents recording approval. Questions
whose only block is RTL approval are resolved by that explicit approval action.
The historical `review.rtl_approval` field in the source is not an approval token.

If an exported package includes a `design_review` question that is resolved by
accepting the existing contracts unchanged, explicitly name it when recording
approval, for example `--confirm-review-question Q_REVIEW`. This records the
human confirmation in a version-2 approval tied to the same source/manifest
fingerprints; it does not edit the model's locked system specification. The flag
is repeatable. It must name an existing design_review question and cannot clear
other question categories, proposed decisions or assumptions requiring review.
If resolving a question changes behavior, interfaces or ownership, revise and
export the contracts before approval instead. Version-1 approvals remain supported
when no explicit review-question confirmation is needed. No real target approval
is created by compiler tests or readiness inspection.

Builds revalidate the embedded snapshot and require the exact compiled manifest.
An edited contract, port, connection, configuration, or compiler result requires
re-export and review. Implementation repairs to RTL within unchanged specs can
reuse approval and the existing module cache. Approval and unverified requirement
and obligation records are copied into isolated builds. Direct saved-manifest
builds enforce the same gate; they need no live access to the original package.
The manifest and embedded snapshot are authoritative; loose exported module
YAMLs are review copies regenerated for builds. `design_kind: memory_controller`
keeps the mandatory controller-role checks active even if a role is removed.
Legacy inputs and ordinary architecture manifests retain their existing routes.

The bridge currently supports the declared role vocabulary, the package's
synchronous active-low reset convention, and globally unique rule IDs. Unsupported
roles/reset conventions or duplicate rule IDs fail explicitly. Lowering consumes
supplied contracts and wiring; the preceding opt-in generator selects boundaries
from requests. Neither proves the generated RTL satisfies the contracts.

```sh
python3 -m unittest discover -s local/frontend -p test_specification_lowering.py -v
```
