# Experimental verification integration agreement

Status: **proposed interface for the verification owner**, October 27, 2026.
This documents implemented module integration and the remaining system interface.
It does not enable system verification or backend execution. Standard verification
must retain its existing behavior.

The subsequent [host-adapter investigation](HOST_ADAPTER_INVESTIGATION.md) found
that preserving every source record and requiring simulation for each one loses
the distinction between metadata, local behavior and system obligations. That
documents a required coordinated correction to the currently implemented module
acceptance policy below; the policy has not yet been changed.

Our new [module scope protocol](VERIFICATION_SCOPE_PROTOCOL.md) now implements
explicit reviewed plan construction, portable bundles and offline scoped-result
validation. The real host-adapter routing is included for consumer coordination.
The main CLI now offers explicit single-module `--scope-plan` transport, gated
by the matching deployed capability before agents. The old path remains
unchanged without that flag; scoped batch/repair collection and actual deployed
acceptance remain pending.

## Current boundary and ownership

| Area | Current state | Next responsibility |
| --- | --- | --- |
| Immutable RTL, approved contracts, compile inventory | Implemented and authenticated locally | Orchestrator retains exact snapshot identity during transport |
| Deployed requirement reader | Consumer preflight passed after reader update | Verification preserves all source metadata through executed results |
| Stateless host-adapter reset detection | Agent 1 rejected existing combinational reset gating | Verification owner confirms/fixes detection and adds regressions |
| Module execution and batch collection | Implemented; real module acceptance remains pending | Verification produces complete simulation evidence; user reruns after deployment |
| Experimental system consumer | Not established | Verification owner defines supported semantics and discovery/capability interface |
| System result transport/acceptance | Not implemented for Experimental | Orchestrator implements against the agreed consumer interface |
| Scoped RTL patch/recheck/publication | Implemented without model calls | Repairs use new publication and fresh verification |
| Backend | Offline staging only | Actual execution waits for accepted system evidence and backend compatibility |

Reference failure: `shared/top_orchestrator/runs/20261028T001145Z_9665a3ac`.
Agent 1 passed YAML syntax, RTL syntax and compilation, then reported that reset
logic was absent. The selected adapter has no registered state and explicitly
gates ready/valid/data with `rst_n` using ternary expressions. Agents 2/3 did not
run. This supports a checker-limitation diagnosis, not behavioral acceptance or
an instruction to add registers/change latency in the frontend.

## Inputs already available

The full mailbox contains these distinct contracts:

| Path | Meaning and path base |
| --- | --- |
| `snapshot.json` | Full publication file hashes; snapshot digest is SHA-256 of these exact bytes |
| `verification_inventory.json` | `capstone.specification_rtl_inventory`, version 1; module entries and compile-order paths relative to mailbox root |
| `rtl/manifest.json` | Same explicit Experimental inventory; **not** legacy controller manifest v2 |
| `rtl/filelist.f` | Exact compile order, with filenames relative to `rtl/` |
| `specs/*.yaml` | Functional module contracts projected byte-for-byte from native handoff inputs |
| `handoff/verification_handoff.json` | Native `capstone.verification_handoff`, version 1, phase `rtl`; paths relative to `handoff/` |
| `handoff/architecture.yaml` | Reviewed architecture, structural top, module interfaces and provenance |

The structural top has an architecture-backed contract rather than fabricated
module YAML. It must remain in the inventory, compilation and system checks.
Do not infer a clock/reset from sequential logic in this wiring-only top.
Use `design.top_ports`, module `clock`/`reset`, and `shared.clock_reset` instead.

The native handoff includes `identity`, hashed `inputs`, ordered `rtl_files`,
`design`, `shared`, and `verification_intent`. The last contains complete source
`requirements`, `obligations`, `events`, elaborated `bindings`, decisions,
assumptions and verification strategy. Bind observations to declared ports using
these bindings; do not assume Standard private wrapper signals exist.

Authenticate before execution and confirm inputs unchanged afterward. Reject
missing files, path escapes, symlinks, changed hashes, mismatched approvals,
duplicate module definitions and incomplete compile inventories. Do not rewrite
RTL/contracts during verification. A proposed repair is a separate revision.

Identity levels are distinct and must not be substituted for one another:

- Invocation ID and full mailbox `snapshot_sha256` identify the submitted run.
- Native `handoff_fingerprint`, `package_fingerprint`, `manifest_fingerprint`
  and `rtl_revision` identify its exact approved design and RTL.
- The backend transport's `mailbox_revision` is a different digest of its RTL
  transport tree; it is not verification identity.

## Timing and supported behavior

For `project-functional-controller-v1`, selected profile cycle counts are used
unchanged as controller-domain cycles, including refresh. The controller MHz
target is an implementation/backend target. Profile ns/frequency metadata does
not implicitly request physical DDR/PHY clocks, rate conversion or CDC. See the
canonical [timing model](timing_model.py).

The current real build selects DDR4-3200 and a 200 MHz controller implementation
target. Its functional model does not claim a physical DDR4-3200 link. Preserve
explicit physical requirements and report unsupported/conflicting scope rather
than silently weakening them.

Support must be explicit for the supplied architecture and obligations. Existing
Standard controller reference behavior, reorder policy, reset assumptions and
generated SYS-REQ catalog cannot automatically substitute for source semantics.
Unsupported profiles, observations or requirements remain unverified/incomplete.

## Existing module consumer contract (implemented)

The current module adapter supplies one YAML/SV pair and `source_context.json`,
including shared types/timing, full verification intent and parent identity.
`snapshot.json` binds the pair to its parent publication. Module source keys are:

- Each `verification_requirements[].id`, preserving the full object as
  `source_requirement`.
- Each `behavior.verification_obligations` key, preserving its full object as
  `source_obligation`.
- `behavior.contract`, preserving its full object as `source_contract`.

Agent-local `REQ-NNN` IDs may remain, but `source_id` and source objects must
survive catalog creation, agent execution and combined reporting. Across modules,
identify a record by **(module name, source ID)**; source IDs can repeat between
modules. Do not merge such records into one global module result.

The existing collector requires one retrieved report with the complete exact
source catalog, no duplicate source IDs, every selected row having uppercase
`status: PASS`, `method: simulation` and nonempty evidence. Static recognition
alone cannot pass these behavioral source records. It also requires the normal
complete pipeline summary and successful requested stages. This describes the
currently enforced adapter; evidence-path/hashing requirements for the proposed
system contract below are stronger and are not claimed to be implemented here.

`SPECIFICATION_CATALOG_VERSION=1` means reader capability, not agent execution.
`--preflight-only` proves deployed catalog/CLI compatibility with networking
disabled in the probe. Neither it nor compilation establishes behavioral PASS.
All module results, including a complete batch, leave system coverage pending
and backend readiness false. Current implementation references:
[reader](../../server/validation/requirement_verification.py),
[module probe](module_probe.py), [invocation](validation.py),
[batch/collection](specification_validation.py).

## Agent 1 reset fix and required regressions

Recognize combinational reset gating in a genuinely stateless module without
requiring a sequential reset branch or adding latency. Inspect signal use and
logic structure; a reset name appearing anywhere is insufficient evidence.
Continue checking sequential state against its declared clock, reset polarity
and assertion style. Do not relax all modules merely because one is stateless.

Cover the existing ternary-gated adapter, equivalent `if`-based combinational
logic, missing/inverted gating, a declared reset that is unused, and existing
Standard synchronous/asynchronous sequential reset cases. Agent 2/3 simulation
must still verify contract-defined outputs and event suppression throughout
asserted reset, release, and simultaneous reset/transaction conditions.

## Proposed system result contract (not yet implemented)

Start with the native handoff's existing `result_contract`:
`capstone.verification_results`, schema version 1. It advertises exact required
requirement/obligation keys, identity fields and record fields. Its statuses are
lowercase `passed`, `failed`, `unverified`, `not_applicable`; these are distinct
from module pipeline `PASS`/`NOT_TESTED` and require explicit translation.

For agreement with the consumer, the proposed document layout is:

```json
{
  "schema": "capstone.verification_results",
  "schema_version": 1,
  "identity": {
    "handoff_fingerprint": "COPY_EXACT_HANDOFF_VALUE",
    "package_fingerprint": "COPY_EXACT_HANDOFF_VALUE",
    "manifest_fingerprint": "COPY_EXACT_HANDOFF_VALUE",
    "rtl_revision": "COPY_EXACT_HANDOFF_VALUE"
  },
  "requirements": {},
  "obligations": {}
}
```

The empty maps above illustrate layout only and are **not acceptable coverage**.
Populate them using exactly `result_contract.requirement_keys` and
`result_contract.obligation_keys`. Each record must contain `status`, `stage`,
`test_ids`, and `evidence`. Proposed evidence entries should identify relative
report/artifact paths, their SHA-256 digests, and the executed assertion/check
that supports the result. Preserve source content or an explicit binding to the
authenticated source object. Test IDs must resolve to the executed test record.

The remote invocation envelope must additionally bind invocation ID, full
snapshot digest, complete compile inventory, top module, consumer/tool versions
and digests, input-unchanged outcome, exit status and report archive digest.
The orchestrator must independently validate downloaded evidence. Merely copying
identity fields into a report is not sufficient acceptance.

Missing, duplicate or unknown source IDs, unsupported checks, timeouts and absent
simulation remain incomplete. `passed` requires executed evidence. A
`not_applicable` disposition needs configuration-specific justification and must
not silently waive a mandatory obligation; its acceptance policy must be agreed
before enabling readiness. Module passes cannot replace integration checks.

System coverage must include structural connectivity and elaboration, declared
interfaces and handshake/event bindings, reset, simultaneous events, and the
source-defined end-to-end behavior and timing. A reference checker must account
for inputs and expected outcomes independently of DUT success/retirement signals.
Preserve failing test inputs and expected/observed evidence for scoped repair.

The existing Standard readiness token is `READY_FOR_BACKEND`. If retained for
Experimental, it must require complete accepted source coverage, successful
mandatory stages, no unresolved gaps and exact revision identity. A summary PASS
or this token alone must not authorize backend execution. The Experimental
backend gate currently remains closed regardless of reported readiness.

## Owner response and acceptance sequence

Before implementing the system adapter, agree on the consumer entry point and
arguments, capability/version discovery, supported timing/behavior profiles,
the proposed system result layout, evidence format and not-applicable policy.
No Experimental system invocation command is advertised until that interface is
implemented. Reader deployment alone does not supply this consumer.

1. Verification owner deploys the reset fix and preserves Standard regressions.
2. User reruns the single host-adapter probe; it must reach Agents 2/3 and produce
   full source-linked simulation coverage, not only Agent 1 acceptance.
3. Exercise all functional modules with `validation --module-batch`; unresolved
   module failures remain visible and keep module coverage incomplete.
4. Implement the agreed system consumer and orchestrator adapter; test structural
   top inclusion, complete source coverage, unsupported profiles, stale identity,
   mutated inputs/reports, duplicate/missing IDs and incomplete-evidence rejection.
5. Accept a real complete system run for the exact immutable publication. A repair
   must publish a new revision and obtain fresh module/system evidence.
6. Only then establish backend handoff compatibility and exact-revision physical
   acceptance. Offline backend staging is separate evidence.

Existing commands and implementation status are maintained in the
[architecture flow](../../local/frontend/microarch/architecture_flow.md).
This document is a handoff for review, not a notification sent to another owner.
