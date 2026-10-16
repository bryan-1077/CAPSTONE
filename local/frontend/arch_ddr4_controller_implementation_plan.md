# DDR4 Controller — Implementation Plan

Build a specification-driven generator for a functional DDR4-like controller.
New capabilities should enter through design requests and specifications, without
feature-specific branches in Python expansion or wrapper generation.

## 1. Specification and Generation System

### Starting point

The original flow selects predefined features in `run_flow.py`, expands templates
in `expand_spec.py`, and adds controller behavior in `generate_wrapper.py`.
The opt-in LLM flow already supports custom module specs, graph validation,
module YAML export, structural integration, and isolated builds. Its missing
pieces are a coherent system specification, an enforced review boundary, and
controller behavioral verification. Existing artifacts do not establish success
for the target below.

Reuse `generate_microarch.py`, `architecture.py`, the existing IR/RTL routes, and
optional templates/timing profiles. See [microarch/architecture_flow.md](microarch/architecture_flow.md)
for the current implementation and commands.

Target flow:

```text
Design request + constraints + optional compatibility preset
    → elaborated system specification
    → module contracts + architecture graph
    → consistency checks + user review
    → approved specs → IR → RTL + structural integration
    → RTL + specification/verification-intent handoff
    → downstream UVM + vector verification → requirement results
```

Scope boundary: `local/frontend/` owns dynamic specification/architecture
generation, consistency checks, review, RTL generation, and the verification
intent exported with that RTL. After RTL generation, the existing downstream
verification stage performs UVM and vector testing outside `local/frontend/`.
Its implementation is outside this frontend task. Frontend compiler regressions
and generation lint remain local checks; they do not replace downstream testing.
Generate architecture-specific verification intent from system behavior and
contracts, using shared definitions and requirement IDs, without per-feature
Python test branches. Define the handoff before adding another executable
behavioral-verification engine to the frontend.

Frontend preparation validation on 2026-10-15: all 78 architecture, package,
lowering/approval, handoff and staged-generation regressions passed. The final
staged validator adjustment also passed its focused 14-test suite. The supplied
draft_005 review export (`a8d815ca95b23bc5`, manifest `cc709aaf0b96ac0c`)
passed handoff validation. No live target specification/RTL generation, target
approval, downstream UVM/vector execution or backend run is recorded by these
checks. The next frontend milestone is a live specification proposal, concrete
review/approval, and target RTL generation/lint with cache replay.

Live attempt on 2026-10-15 (`live_20261015T185021Z`): the initial system-specification
model call failed with ConnectionError under the network sandbox. The requested
network escalation was declined, so execution stopped before either specification
stage completed. No generated target package, approval, or RTL resulted. The log
is `microarch/proposals/system/live_20261015T185021Z/frontend.log`; live generation
and the first target RTL build remain pending.

The user's subsequent terminal run reached Terra, but the system stage exhausted
its attempts on contradictory format-example fields and an opaque reset-style
check. Its final model-reported unsupported result incorrectly treated optional
mixed-style reuse as mandatory. Fixes on 2026-10-16 preserve the request and
baseline requirements: stage-specific response examples, canonical `synchronous`
reset style with legacy alias support, field-specific repair diagnostics, explicit
conditional-reuse guidance, and full response journals for rejected attempts.
Generator revision 2 and lowering revision 3 invalidate stale proposal/export
caches. A completed live package and RTL build are still pending.

Follow-up validation on 2026-10-16: all 81 frontend regressions passed, including
canonical/legacy reset lowering, stage-specific prompt examples, controller
baseline preservation and rejected/unsupported response journals. Python
compilation and diff whitespace checks passed. The live command can be retried
unchanged; the corrected prompt and compiler context select a fresh source cache.
This offline result does not establish completion of the next live generation.

The next user-run live attempt completed both specification stages on their third
attempts and exported `/tmp/ddr4-frontend-review/architecture.yaml`. The generated
source is `microarch/proposals/system/48fdba3e96f7670e/package.yaml`; its manifest
fingerprint begins `4b1010039a077247`. It has eight modules, 43 rules and 32
obligations. Source reproduction and review-handoff checks passed. The initial
default remains four banks/four waiting/FIFO/open-page/DDR4-3200. The generated
package declares four capacity/page-policy configurations, separate from the
hand-authored example's 36-case matrix. Technical inspection is recorded beside
the export in `frontend_inspection.md`; no target approval or RTL is recorded.
The generated package's four declared configurations passed structural checks and
source compilation exactly reproduced the export. Ten focused approval/build/
handoff regressions passed after adding explicit review-question confirmation,
including version-1 compatibility and stale/unknown/incomplete approval rejection.

Latest user-run RTL build on 2026-10-16 (`4b1010039a077247`) records an
approved generated package, successful host-adapter and waiting-queue generation,
and failure at FIFO-scheduler review. Single-file lint passed; the reviewer found
that implicit clock/reset inputs were missing from the authoritative port list,
and that a blanket signal prohibition conflicted with internal implementation
freedom. The three rounds did not establish a clean scheduler review; integration
lint and verification handoff did not run. This supersedes the earlier no-approval
status for the generated `draft_003` export, not the separate supplied `draft_005`.

- [x] Normalize the generator/reviewer interface to include declared clock/reset
      inputs and permit internal helpers within owned behavior.
- [x] Keep the three-round bound; stop on specification ambiguities and skip a
      final merge that cannot receive another reviewer pass.
- [x] Save full per-round reviewer reports and candidate RTL for module CLI runs.
- [x] Print provider-reported input/output/total usage after each module and flow,
      including failed runs, retries, and specification generation; persist usage
      reports shared across module subprocesses. Missing usage stays unknown.
- [x] Add per-module and flow USD cost estimates using the user-provided Opus
      input/output rates ($5/$25 per million) and Terra rates ($2/$12 per million).
      Include retries and reviews, save pricing and cost totals in usage reports,
      and retain unknown totals with known subtotals when usage/rates are missing.
      All 12 focused usage/review regressions passed after this addition, including
      mixed-model pricing, subprocess/retry aggregation, failure subtotals and
      zero-cost cache reuse. No live model calls were made for this change.
- [x] Reduce repeated prompt context through a generic, lossless shared-definition
      serializer; preserve complete contracts and future metadata without feature
      branches, prose summarization or normative-field filtering.
- [x] Send constraints once, remove invariant echo from generation/merge responses,
      retain compiler invariants, and avoid repeated RTL snapshots in round history
      and merges. Keep complete behavioral context in compact-review fallback.
- [x] Measure prompt characters per API call and regression-check exact context
      reconstruction. The saved eight-module comparison reduced context characters
      by 45.86%; provider-token/cost savings await a live run.
      Validation: 50 offline checks passed (nine prompt-preservation/size tests,
      13 dual-LLM API tests, 12 usage/cost/review tests and 16 existing lowering/
      approval tests). Python compilation and diff whitespace checks passed;
      no live model calls or target RTL regeneration were performed.
- [x] Print a blank line, module tokens, module cost and then FINISHED, across all
      successful generation routes; retain failure costs without a success banner.
- [x] Start code-generation/merge calls at the existing 16,384-token ceiling,
      retaining 8,192 for review and explicit environment overrides. Save truncated
      responses and raw usage details without accepting partial RTL.
      The latest reduced-context live run still stopped at command-engine
      truncation followed by HTTP 524; the revised initial budget awaits live
      validation and does not establish that server timeouts are resolved.
      Validation: 32 offline checks passed (16 API boundary tests, 14 usage/cost/
      review tests and two actual frontend generation/cache/lint smoke tests).
      Compilation and whitespace checks passed; no live API calls were made.
- [ ] Complete the live target RTL build and integration lint with these repairs;
      downstream UVM/vector testing remains outside frontend scope.

Validation of these repairs: 106 offline frontend tests passed (67 architecture,
package, lowering/approval and handoff regressions; 17 specification-generation,
13 dual-LLM API and nine usage/review regressions). Python compilation and diff
whitespace checks passed. The actual failed scheduler YAML now yields eight
public ports including `clk` and `rst_n`. No live model calls or target RTL rerun
were performed during this repair.

### A. Define and review the specification format

Reviewed for implementation: [specification format](microarch/specification/README.md)
and [example package](microarch/specification/ddr4_fifo/package.yaml), current revision
`draft_005`, cover the first target's shared definitions, system behavior, all
eight module contracts, and typed connection graph. The user accepted the defaults
on 2026-10-15; [review findings](microarch/specification/ddr4_fifo/review.md) record
lifecycle, ownership, and intentional compatibility changes. Executable package
checks cover all 36 declared configurations. Contract-to-module-spec lowering and
revision-bound approval enforcement are now connected to the architecture build
route. Approval of the concrete first-target export and live RTL generation remain
pending; no behavioral obligations have been executed.
`draft_005` preserves accepted behavioral defaults, includes verification bindings,
and clarifies clock-driven candidate formation and refresh-pending observation;
the expanded export awaits
its own concrete review. Existing downstream system verification still requires
an adapter for the new architecture/semantics.

- [x] Draft an example package for the first controller target in Section 2.
  - [x] Draft the request/FIFO slice with package metadata, decisions, assumptions,
        and explicit unresolved questions.
  - [x] Complete the example with execution contracts and the connection graph.
- [x] Define shared types, widths, capacities, command encodings, events, and timing
      values once; reference them from module contracts.
  - [x] Define shared request types, widths, capacities, lifecycle events, and
        timing-profile references.
  - [x] Define command payloads and encodings; complete execution-event semantics.
- [x] Describe system behavior: request lifecycle, ordering, scheduling policy,
      command semantics, bank state, timing, refresh, and responses.
  - [x] Describe admission, dispatch, active ownership, retirement, FIFO ordering,
        backpressure, simultaneous queue events, and reset cancellation.
  - [x] Complete command, bank-state, timing, refresh, and response-edge behavior.
- [x] Define module contracts: owned state, interfaces, handshakes, reset, update
      events, stalls, simultaneous events, invariants, and verification obligations.
  - [x] Draft waiting-queue and FIFO-scheduler contracts with verification obligations.
  - [x] Complete host-adapter, command-engine, row-state, timing, memory, and refresh contracts.
- [x] Use structured fields for shared facts and rules; precise prose for algorithms.
      Do not introduce a complete hardware language as part of this milestone.
- [x] Review the example and schema before implementing the generation stages.

### B. Establish baseline and ownership rules

- [x] Turn `current_controller` into an explicit compatibility preset rather than
      a universal restriction. Initially preserve host ports, address mapping,
      acceptance behavior, data correctness, and response handling. Internal
      structure and command latency may change.
- [x] Default the preset to four waiting requests plus one separately stored active
      request. Allow explicit waiting-capacity changes in design requests.
- [x] Require separate owners for request storage, scheduling, command sequencing,
      timing enforcement, and functional memory/data handling.
- [x] Permit further splitting; require review before merging major responsibilities.
      Trivial helper modules must not disguise a monolithic core.
- [x] Keep top-level integration structural. Give functional glue an explicit module
      contract, and make major modules independently testable through their ports.

Implemented by `current_controller_v2`, configurable
`microarchitecture.waiting_capacity`, and the existing architecture role/wiring
validator and structural wrapper. Generic requests may select or omit the preset;
legacy configurations still attach it automatically. Named policies are request
constraints, not a Python policy allowlist. Proposals must be regenerated against
the new contract. These checks establish boundaries, not behavioral correctness.

### C. Generate and validate specifications

- [x] Elaborate the system spec before selecting module boundaries and connections.
  - [x] Add a two-stage request route (`run_flow.py --specification`): validate and
        lock system behavior first, then derive contracts, owner assignments,
        wiring and verification bindings. Keep the existing direct architecture
        proposal route available separately; neither replaces legacy defaults yet.
  - [x] Correct the first live run's prompt/schema mismatch and reset-style
        diagnostic loop; preserve failed responses for inspection and replay.
  - [x] Exercise live system/decomposition generation through the complete review
        export, with schema/ownership repair evidence preserved.
- [x] Distinguish requested constraints, model-selected decisions, assumptions, and
      unresolved questions. Preserve requirement IDs and traceability.
  - [x] Preserve these categories, rule IDs/origins and verification references in
        portable supplied-package exports and module contexts.
  - [x] Generate architecture-specific verification intent: properties, scenarios,
        observable events, expected results, assumptions, and coverage obligations
        for the downstream verification stage.
    - [x] Validate and export supplied intent and typed event/observation bindings
          from each package; retain explicit unbound events without RTL-name heuristics.
- [x] Derive module contracts from the system spec; assign each rule an owner and
      verification obligation. Resolve cross-module agreements before RTL.
  - [x] Preserve every request ID/text and explicit configuration constraint;
        require reciprocal owner/obligation links and exact system-stage identity.
        Allow additive internal interface types without replacing system types.
        Structural agreement is checked; behavioral prose still requires review.
  - [x] Lower supplied reviewed contracts into concrete module YAMLs and the
        existing architecture manifest, without inventing behavior or boundaries.
- [x] Validate shared references, types, widths, ownership, contract consistency,
      graph connections, drivers, clocks/resets, and declared dependencies.
  - [x] Add a read-only package checker with parameter/type elaboration, timing
        binding resolution, reciprocal rule/verification links, typed graph and
        declared dependency validation; test the full configuration matrix.
  - [x] Integrate package checks into generated-spec elaboration and the build path.
- [x] Keep templates optional. Extend generic compiler constructs when needed;
      do not add another predefined feature registration for each capability.
- [x] Report unsupported requests and required extensions. Never silently omit or
      weaken a requested capability, or label unverified behavior as passed.
  - [x] Require affected request IDs, reasons and extensions in unsupported reports;
        reject incomplete supported packages and omitted/rewritten requirements.
        Bounded repair, response replay and request/context-bound cache are available.
        These checks preserve traceability; they do not prove semantic satisfaction.

### D. Enforce review before RTL

- [x] Emit the system spec, module contracts, connection graph, decisions,
      assumptions, unresolved questions, and verification plan as one package.
- [x] Require user review and approval before RTL generation; resolve open decisions.
  - [x] Enforce an explicit approval artifact before any specification-derived
        build writes RTL; reject unresolved design decisions and stale approvals.
  - [x] Support explicit review-question confirmation in an exact-export approval
        without rewriting locked generated source. Keep other blockers active.
  - [x] Review and approve the concrete exported first-target package before its
        first live RTL build (generated `draft_003`; approval recorded in the build).
    - [x] Inspect lifecycle, ownership, timing, refresh, response and binding
          agreements; clarify engine progress between handshake events in draft_005.
    - [x] Supply the initial request/configuration: four banks, four waiting entries,
          FIFO, open-page, DDR4-3200; retain broader supplied-package matrix coverage.
- [x] Tie approval to the reviewed package revision. Contract changes require review
      again; routine implementation repairs within approved contracts may proceed.
- [ ] Let the LLM choose internal representations, helper modules, names, and
      algorithms within approved contracts. Bring changes to observable behavior,
      ownership boundaries, or requested guarantees back for review.

### E. Generate, verify, and adopt

- [ ] Feed the approved contracts into existing IR/RTL routes and manifest-driven
      integration. Preserve isolated outputs, provenance, and replay/cache support.
  - [x] Connect package/manifest CLI inputs, existing module IR/datapath routing,
        structural integration and the approval gate; exercise orchestration with
        offline fixtures and retain source/approval evidence in isolated builds.
  - [x] Exercise the real frontend-only cache/IR/RTL-file/lint/handoff path and replay
        with a small generated-specification fixture; test failed-rebuild handoff
        invalidation. This is transport/compiler evidence, not target behavior.
  - [ ] Exercise the approved first-target contracts with live RTL generation and
        reviewed module-cache replay.
- [x] Define and export a versioned frontend handoff for downstream UVM/vector verification:
      RTL/filelist, module/top interfaces, resolved configuration/types/timing,
      reset/handshake/event bindings, requirement IDs, and verification intent.
- [x] Export supplied module isolation scenarios and system acceptance scenarios for that
      handoff. Executable UVM/vector test generation and execution belong to the
      downstream stage outside this frontend task.
- [x] Base exported expected behavior on approved system contracts, independently
      of the generated RTL, so downstream monitors/scoreboards can check it.
- [x] Define requirement-result correlation with downstream passed, failed, or
      unverified evidence, separately from frontend specification validation, RTL
      generation, lint, and compilation. Downstream result production is external.
  - [x] Emit per-rule and per-obligation unverified records; never turn structural
        validation or generation success into behavioral verification success.
- [ ] Confirm downstream adoption of the new handoff. Existing system consumer
      v2 expects legacy configuration/TB/private wrapper observations and permits
      pre-dispatch reordering. Its new-schema/behavior/requirement-ID adapter and
      executable UVM/vector support are outside `local/frontend/` scope. Export
      `adapter_required` until that work is completed; do not claim integration PASS.
      The orchestrator also needs explicit isolated-build/approval selection and
      transport support; its current publisher selects the legacy global output.
- [ ] Demonstrate the first target through specifications and generic generation,
      without command-engine or refresh branches in Python expansion/wrapper logic.
- [ ] Adopt the new flow as the default after review and passing system verification.
- [ ] After initial success, review support for broader architectural freedom,
      including replacement of the compatibility preset and host interface.

## 2. Controller Features

Verification items below describe system acceptance requirements to carry into
the downstream UVM/vector stage; they do not assign that stage's implementation
to `local/frontend/`.

### A. First target — requests and FIFO scheduling

Use the existing simple host interface and functional banked memory. Support the
existing bank-count configurations and configurable waiting capacity, with one
active request. FIFO establishes the flow; scheduling experiments follow later.

- [ ] Admit on `txn_valid && txn_ready` at a rising edge. Each qualifying cycle is
      a distinct request; retain payload while backpressured.
- [ ] Dispatch by capturing the complete request in active storage and releasing
      its waiting slot. Define simultaneous enqueue/dequeue and full-queue reuse.
- [ ] Hold active ownership through PRE, ACT, and timing stalls. Setup commands
      must not retire the request.
- [ ] Retire on final READ/WRITE commit. Capture read-response state before releasing
      active storage; deliver the response as a separate event.
- [ ] Preserve acceptance order for the same bank/address under every policy.
      Independent requests may reorder under later policies; FIFO initially preserves
      global acceptance order. Track age independently of reusable slot index.
- [ ] Verify backpressure, field preservation, reset, simultaneous events, FIFO order,
      and exactly-once execution/response behavior.

### B. First target — commands, bank state, and timing

The engine proposes commands; timing decides legality; committed commands update
architectural state. Preserve existing timing protection through this migration.

- [x] Define command payload and encoding for NOP, ACT, PRE, READ, WRITE, and REF.
      Define one commit event, with at most one command committed per cycle.
      Defined in shared types/events and reviewed execution contracts; execution
      tests remain pending.
- [ ] Generate ACT → READ/WRITE for closed banks, READ/WRITE for row hits, and
      PRE → ACT → READ/WRITE for row conflicts. Hold candidates stable while blocked.
- [ ] Update open-row state only on committed ACT/PRE and defined refresh events.
- [ ] Enforce tRCD, tRAS, tRP, tRRD, and tFAW in one timing layer using selected
      profile values. Update timing history from commits, never admissions or stalls.
- [ ] Support open-page and close-page. Close-page cleanup uses a legal PRE and
      retains bank ownership until commit; initially block new dispatch during cleanup.
- [ ] Write memory only on WRITE commit; start the existing read-response pipeline
      on READ commit. Keep response delivery separate from request retirement.
- [ ] Verify sequences, blocked-state stability, timing boundaries, bank isolation,
      read-after-write data, responses, and both page policies.

### C. First target — refresh and system acceptance

Refresh must cause a real service interval and preserve queued traffic. The system
spec must resolve active-service drain, row-state effects, and timing interactions.

- [ ] Generate periodic pending refresh from tREFI. Give it priority after active
      service and required cleanup; prevent indefinite starvation.
- [x] Specify preparation/drain and row-state handling before committing REF.
- [ ] Start tRFC blocking on REF commit; prohibit normal commands during the interval,
      acknowledge once, and resume queued traffic afterward.
- [ ] Verify idle and loaded refresh, active-service interaction, interval boundaries,
      no traffic loss, and post-refresh progress.
- [ ] Pass lint/compilation and a system test covering host acceptance through command
      execution and response, queue stress, ordering, timing, refresh, data, reset,
      and close-page cleanup. Record configuration and requirement results.

### D. Later — scheduling policies

Policies choose among requests eligible under the system ordering contract.
Introduce them through design requests/specs, without Python feature branches.

- [ ] Add row-hit-first selection and verify priority without same-address reordering.
- [ ] Add age-based fallback and configurable bounded-starvation policies; define
      progress bounds and assumptions about timing, refresh, and downstream stalls.
- [ ] Experiment with other policies and per-candidate progress eligibility.
- [ ] Compare request latency, throughput, row-hit rate, queue utilization, and stalls.

### E. Later — AXI-Lite, responses, and bursts

Keep host protocol handling separate from DDR control and timing.

- [ ] Add an AXI-Lite adapter with independent write address/data capture, channel
      pairing, read/write responses, and backpressure handling.
- [ ] Extend response contracts for buffering and configurable CAS-like latency.
- [ ] Add simplified fixed-length bursts, beat tracking, data intervals, and explicit
      burst completion/ownership. Full AXI4 bursts and a DDR PHY remain outside scope.
- [ ] Add module and system tests for channel stalls, response preservation, burst
      data/order, and interaction with queueing and refresh.

### F. Later — additional timing and execution overlap

Extend the same timing-rule and ownership structures as capabilities grow.

- [ ] Add tRC, tCCD, tWTR, and tWR; consider tRTP/tRTW and bank-group rules afterward.
- [ ] For each rule, define scope, triggering commit, blocked commands, profile value,
      and directed boundary tests.
- [ ] Explore overlapping bank execution after single-active behavior is verified;
      specify command arbitration, request ownership, and response ordering first.

Defer PHY signaling, training/calibration, ECC, mode-register programming, power
management, and production initialization until the functional architecture and
generation flow are established.
