# Host-adapter verification failure investigation

Run: `shared/top_orchestrator/runs/20261028T015818Z_c4983cb9`.

Finding: the observed failure is a verification scope/method mismatch. No
host-adapter functional RTL defect was demonstrated. The integration's source
catalog policy contributes to this failure; it is not solely an agent bug.

## Evidence from the deployed run

Agent 1 passed, including compilation and YAML/RTL consistency after the reset
checker fix. Agent 2's report is `ERROR`, stopped during testbench generation.
The generated-testbench audit lists 11 missing executable checks. No behavioral
simulation result established a DUT mismatch. Agent 3 was skipped.

The audit is at
`reports/verification_reports/2026-10-27_20-58-59/agent2/generation_audit_iter_0.json`
within the run directory. Its blocked checks include:

| Audit IDs | Source meaning | Appropriate verification boundary |
| --- | --- | --- |
| REQ-003/004/014/015/023/024 | Requested package design name/type and their metadata-inspection obligations | Authenticate/check package metadata; do not demand a DUT waveform comparison |
| REQ-027 | Reset waiting/active requests, memory, rows, timing history and refresh state | System/composed-module simulation; local host reset gating is only a contributing check |
| REQ-029 | Waiting occupancy under simultaneous admission/dispatch, full/empty behavior | Queue and system simulation, not the isolated stateless host adapter |
| REQ-010/012/013 | Ownership boundaries, concrete parameter use, absent-port behavior | Structural/configuration checks plus relevant local behavior; not an unconditional generic simulation call |

Package design name `ddr4_controller_top` and type `memory_controller` are not
the same namespace as the lowered host module name/type. Do not rename the
host module or turn its `datapath` classification into `memory_controller` to
satisfy those package metadata checks.

The generation audit asked for unsupported markers when observation was missing;
subsequent repair attempts tried to introduce markers that the audit-edit guard
rejected. All five attempts failed before usable testbench acceptance. Routing
the obligations correctly before generation is needed; disabling the audit or
accepting fabricated checks would not fix coverage.

## Where scope was lost

1. The source package associates the host contract with full system/execution
   rules and obligations, many shared with other module owners.
2. `local/frontend/specification_lowering.py` copies the complete referenced
   invariant text and obligation object into the lowered module YAML. This
   preserves traceability, but does not express which local portion is testable.
3. `server/validation/requirement_verification.py` extracts those full source
   objects into the agent catalog, alongside overlapping legacy invariant and
   implementation-constraint records.
4. `shared/top_orchestrator/module_probe.py` currently requires every selected
   source row to have `PASS`, `method=simulation` and evidence. This protects
   against silent requirement loss but incorrectly equates complete source
   preservation with complete local simulation responsibility.

This explains why a reader capability preflight passed while real generation
failed: catalog preservation did not establish executable scope compatibility.

## Independent local RTL check

The published host adapter is stateless. Its local contract requires payload
packing, ready/valid gating, response forwarding, and reset suppression. A
standalone Verilator simulation exercised all 16 combinations of reset,
queue-ready, transaction-valid and response-valid, with 256 varied payload/data
vectors per combination. It checked these five outputs/properties each time:

- `txn_ready = rst_n && queue_ready`.
- `enq_valid = rst_n && txn_valid`.
- `enq_request = {bank, row, column, is_write, wdata}`.
- `rsp_valid = rst_n && rsp_valid_in`.
- `rsp_rdata = rst_n ? rsp_data : 0`.

Result: 4,096 vectors and 20,480 assertions passed. A temporary mutation that
inverted the ready reset gate failed the same checker, confirming it detects
that error. Original RTL SHA-256:
`96729c8d540a9ab319730224353706128276b1b80176256da86e8d7ea4e7a776`.
Artifacts: `/tmp/capstone-host-adapter-investigation-20261028/` contains the
testbench, compile/simulation logs and `audit.json`.

This check covers two-state local combinational behavior on sampled vectors,
not exhaustive payload space, X/Z behavior, full-controller timing/state or the
complete agent acceptance protocol. System response retention/latency depends
on the memory module driving the response inputs. The local adapter passing
through those inputs does not independently prove the composed response rule.

The generated Agent 2 candidate also labels some narrow signal comparisons with
broad source/whole-contract IDs. For example, its `REQ-030` whole-contract check
includes a payload-packing comparison. Check presence alone cannot establish the
entire contract; coverage must identify the actual tested local facets.

## Coordinated fix, preserving requirements

Introduce an explicit, versioned verification execution plan separating metadata,
structural checks, module-local behavioral facets and system obligations. Preserve
all original IDs/objects and map local checks to their source rules. Multi-owner
rules require composition coverage; each owner must not claim the whole rule.
Classification must be explicit and validated, not guessed from prose keywords.

The frontend/integration owner should express local scope and required environment
assumptions, retain global obligations for system coverage, and update module
acceptance to require the appropriate evidence for each scope/method. A deferred
system obligation remains unverified, not dropped, not marked passed and not
silently waived. Any new catalog capability/version must be coordinated with the
deployed consumer and backwards-compatible Standard behavior.

The verification owner should consume that plan, check metadata against the
authenticated package, generate local tests for the isolated module, and retain
integration obligations for a composed system DUT. Do not spend repeated model
repairs attempting to simulate unavailable system state. Keep the audit guard
against unsupported substitutions and fake checks; improve routing/diagnostics.

Regression acceptance should show that metadata checks do not require fake HDL
comparisons, host-local behavior passes real simulation, broken reset/packing
fails, shared system obligations stay pending until system execution, and all
module passes still cannot enable backend readiness on incomplete system coverage.

No generated RTL, published mailbox, deployed agent or acceptance policy was
changed during this investigation. No remote agents or model calls were run.

Subsequent implementation: [module scope protocol](VERIFICATION_SCOPE_PROTOCOL.md)
provides the frontend-side reviewed plan, request bundle and offline result-body
validator. It preserves deferred coverage and auxiliary YAML constraints. Live
invocation/acceptance still needs a compatible consumer and coordinated wiring;
the original all-source simulation policy has not been silently relaxed.
