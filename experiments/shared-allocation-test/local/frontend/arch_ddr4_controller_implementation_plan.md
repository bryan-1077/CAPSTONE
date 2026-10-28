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
    → module tests + system test → requirement results
```

### A. Define and review the specification format

Draft ready for review: [specification format](microarch/specification/README.md)
and [example package](microarch/specification/ddr4_fifo/package.yaml), revision
`draft_002`, cover the first target's shared definitions, system behavior, all
eight module contracts, and typed connection graph. Document consistency checks
passed for 1/2/4 banks. User/compatibility review remains open; no generation
stages consume the draft and no behavioral obligations have been executed.

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
- [ ] Review the example and schema before implementing the generation stages.

### B. Establish baseline and ownership rules

- [ ] Turn `current_controller` into an explicit compatibility preset rather than
      a universal restriction. Initially preserve host ports, address mapping,
      acceptance behavior, data correctness, and response handling. Internal
      structure and command latency may change.
- [ ] Default the preset to four waiting requests plus one separately stored active
      request. Allow explicit waiting-capacity changes in design requests.
- [ ] Require separate owners for request storage, scheduling, command sequencing,
      timing enforcement, and functional memory/data handling.
- [ ] Permit further splitting; require review before merging major responsibilities.
      Trivial helper modules must not disguise a monolithic core.
- [ ] Keep top-level integration structural. Give functional glue an explicit module
      contract, and make major modules independently testable through their ports.

### C. Generate and validate specifications

- [ ] Elaborate the system spec before selecting module boundaries and connections.
- [ ] Distinguish requested constraints, model-selected decisions, assumptions, and
      unresolved questions. Preserve requirement IDs and traceability.
- [ ] Derive module contracts from the system spec; assign each rule an owner and
      verification obligation. Resolve cross-module agreements before RTL.
- [ ] Validate shared references, types, widths, ownership, contract consistency,
      graph connections, drivers, clocks/resets, and declared dependencies.
- [ ] Keep templates optional. Extend generic compiler constructs when needed;
      do not add another predefined feature registration for each capability.
- [ ] Report unsupported requests and required extensions. Never silently omit or
      weaken a requested capability, or label unverified behavior as passed.

### D. Enforce review before RTL

- [ ] Emit the system spec, module contracts, connection graph, decisions,
      assumptions, unresolved questions, and verification plan as one package.
- [ ] Require user review and approval before RTL generation; resolve open decisions.
- [ ] Tie approval to the reviewed package revision. Contract changes require review
      again; routine implementation repairs within approved contracts may proceed.
- [ ] Let the LLM choose internal representations, helper modules, names, and
      algorithms within approved contracts. Bring changes to observable behavior,
      ownership boundaries, or requested guarantees back for review.

### E. Generate, verify, and adopt

- [ ] Feed the approved contracts into existing IR/RTL routes and manifest-driven
      integration. Preserve isolated outputs, provenance, and replay/cache support.
- [ ] Add module tests for failure isolation and a runnable controller system test.
- [ ] Base expected behavior on approved system contracts; use independent protocol
      monitors and data/request scoreboards rather than copying implementation logic.
- [ ] Record per-requirement passed, failed, or unverified results, separately from
      specification validation, RTL generation, lint, and compilation.
- [ ] Demonstrate the first target through specifications and generic generation,
      without command-engine or refresh branches in Python expansion/wrapper logic.
- [ ] Adopt the new flow as the default after review and passing system verification.
- [ ] After initial success, review support for broader architectural freedom,
      including replacement of the compatibility preset and host interface.

## 2. Controller Features

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

- [ ] Define command payload and encoding for NOP, ACT, PRE, READ, WRITE, and REF.
      Define one commit event, with at most one command committed per cycle.
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
- [ ] Specify preparation/drain and row-state handling before committing REF.
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
