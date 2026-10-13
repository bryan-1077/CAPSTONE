# System specification draft — 1A controller example

[ddr4_fifo/package.yaml](ddr4_fifo/package.yaml) is a hand-authored example for
review, covering the Section 2 first target: requests/FIFO, commands, bank state,
timing, both page policies, functional memory/responses, and refresh. Revision
`draft_002` includes all eight module contracts and a typed connection graph.
It is not an executable architecture manifest or an approved generation input.
No generation stage consumes this format yet.

Review the shared definitions first, then the lifecycle rules and module
contracts, then [execution rules](ddr4_fifo/execution.yaml) and
[connections](ddr4_fifo/connections.yaml). The package records proposed decisions
and review questions. All verification obligations are planned and unexecuted.

The draft preserves the current host port shape and compact address mapping.
FIFO deliberately replaces the existing `simple` row-hit/slot-index policy;
the current `current_controller_v1` baseline would reject that policy change.
Making compatibility selectable belongs to 1B. Existing proposal files are
reference material, not evidence of correctness for this target.

## Draft format/schema

This is a schema proposal expressed as a small field vocabulary and an example,
not a JSON Schema validator or a hardware language. YAML mappings use stable IDs;
references are strings, not copied numeric widths or repeated behavioral rules.
Unknown fields and unresolved references should fail a future validator.

| Document | Required fields | Purpose |
| --- | --- | --- |
| Package | `specification_version`, `package_id`, `design_name`, `revision`, `status`, `scope`, `documents`, `verification_strategy`, `decisions`, `assumptions`, `open_questions` | Revision and review boundary; paths relative to the package file; configuration matrix and system verification strategy |
| Shared | `parameters`, `clock_reset`, `types`, `host_interface`, `events`, `timing_profile` | Single source of shared facts |
| System slice | `slice_id`, `rules`, `verification` | Observable rules and their owners/tests |
| Contracts | `contracts` | Mapping from module ID to its contract |
| Graph | `graph_id`, `clock_reset`, `connections`, `presence`, `integration` | Source endpoint to sink-list mapping, shared clock/reset fanout, conditional ports |
| Module contract | `status`, `role`, `owned_state`, `interfaces`, `handshakes`, `reset`, `update_events`, `stalls`, `simultaneous_events`, `invariants`, `verification`, `combinational_dependencies` | Independent behavioral boundary |
| Rule | `origin`, `owners`, `statement`, `verification` | Requirement traceability; origin is a plan section in this example |
| Verification obligation | `rules`, `scenario`, `expected`, `status` | Observable check, with `planned_not_executed` initially |
| Decision | `origin`, `status`, `value`, `reason` | Distinguish requested constraints from proposed design choices |
| Open question | `blocks`, `question` | Missing agreement; never silently supply a default |

References use `shared.parameters.NAME`, `shared.types.NAME`,
`shared.events.NAME`, `shared.timing_profile.bindings.NAME`, `system.rules.ID`,
`system.verification.ID`, `execution.rules.ID`, and `execution.verification.ID`.
Clock/reset references may name `shared.clock_reset` as a whole.
Interface entries have `direction` and `type`; optional `when` defines a
presence condition. Record types list fields in packed MSB-to-LSB order;
unsigned types use either an integer width or a parameter reference. An enum
lists its explicit encoding. Arrays declare `element_type`, `count`, and packed
element order. Arrays, packing, and symbolic width elaboration
are future lowering work, not supported by today's architecture compiler.

Event `condition` and rule `statement` are precise review prose, not executable
expressions. Port dependencies refer only to current-cycle input signals;
registered state and the shared reset gating are implicit. A rule's `owners`
must name actual contracts with the corresponding invariant and obligation.
Rule/verification references must be reciprocal. A partial package cannot be
approved for RTL while any required contract or blocking question is pending.

## Edge convention and proposed choices

All handshakes sample values immediately before a rising `clk` edge. State after
the edge reflects those events. Reset wins over every event. The queue offers
only previously stored requests: an admission into an empty queue cannot dispatch
on that same edge. A dispatch captures the old head before a simultaneous enqueue
replaces storage. Full-queue admission is allowed when that edge also dispatches.

The engine's dispatch readiness is based on registered idle state, cleanup state,
and refresh exclusion. A retiring active request cannot be replaced on the same
edge in this draft; the following edge may dispatch. This trades a cycle for a
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
second response register in the host adapter would violate the proposed contract.

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
This refines the old row-hit/activation-gate diagnostic; its compatibility and
the response sampling convention require review, not a claim of equivalence.

The package is ready for specification review. Proposed choices and assumptions
still require user review; no generation approval is implied by document checks.
Only after reviewing this format should 1C/1D introduce elaboration, validation,
and revision-bound approval enforcement.
