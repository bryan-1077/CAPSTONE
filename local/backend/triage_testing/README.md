# RTL Triage Testing

This folder contains the original controller and an intentionally faulty variant for testing RTL failure triage. Both archives retain all 11 source files and the `ddr4_controller_top` integration module.

- `MemoryController_legacy.zip`: Byte-for-byte backup of the original project-root archive. This is a baseline, not a certification that the original design is bug-free.
- `MemoryController.zip`: Modified test archive with logic faults and an undefined module instance that causes deterministic prep review to fail.
- `MemoryController_logic_faults.zip`: Preserved previous test archive with only the four logic/design faults, before adding the prep failure.
- `rtl/`: Extracted modified sources for inspection.
- `changes.diff`: Exact changes from the legacy sources.
- `mutations.json`: Fault descriptions and archive SHA-256 checksums.
- `prep_failure_check.json`: Local prep-review comparison showing the previous archive passes and the new archive fails on the missing dependency.
- `run_triage.py`: Calls the existing RTL triage function with the complete source bundle and optional real diagnostic logs.

## Injected faults

| Fault | Location | Expected effect |
| --- | --- | --- |
| Missing module dependency | `ddr4_controller_top.sv`, `u_request_queue` | Instantiates `ddr4_request_queue_missing`, which has no definition in the bundle. Prep reports `prep_bundle_completeness_problem`. |
| Timing gate bypass | `ddr4_controller_top.sv`, `slow_path_allowed` | Uses OR instead of AND, allowing an activation when one timing restriction still blocks it. |
| Bank address aliasing | `ddr4_controller_top.sv`, `service_bank_q` | Clears the upper bank bit, directing bank 2/3 operations to bank 0/1. |
| Serial activation count | `ddr4_tFAW_tFAW_tracker.sv`, `next_count` | Uses a dependent combinational addition loop over the 40-bit activation window. Synthesis may rebalance it; increased routed delay is not guaranteed. |
| Activation-limit off-by-one | `ddr4_tFAW_tFAW_tracker.sv`, `tFAW_block` | Changes `>=` to `>`, incorrectly allowing the boundary count. The top has an additional count-based guard, so test the tracker output directly too. |

Module definitions, ports, filenames, and reset structures are preserved. The request queue instance now refers to an undefined module, intentionally breaking dependency resolution during prep. The other four changes remain functional/design-quality faults.

For your current full-flow configuration (`remote_prepare_input=MemoryController_bad.zip`), upload this folder's updated `MemoryController.zip` to the remote project using the name `MemoryController_bad.zip`. The remote archive has not been updated automatically. Prep completes up to three review/edit attempts first. If the missing-module failure remains after all attempts, triage runs once with the final diagnostic; a successful repair skips triage. Triggering triage does not guarantee frontend ownership: a missing dependency can return unknown under the design-quality policy. Remote automated repair behavior after the initial rejection has not been tested.

## Run triage directly

From the project root (the runner loads `services/exports.txt` like `app.py`, preserving existing environment overrides):

```bash
python3 triage_testing/run_triage.py
python3 triage_testing/run_triage.py --legacy
```

To include actual tool evidence:

```bash
python3 triage_testing/run_triage.py --log /path/to/actual/tool_report.log
```

Results go into this folder as `triage_result.json` and `legacy_triage_result.json`. The runner excludes this README, the diff, and the fault manifest from the AI input. It returns nonzero when AI review is unavailable; a successful exit means review ran, not that a particular classification was obtained.

The desired design-quality finding is `frontend` / `poor_logic_design`, supported by source or tool evidence. Classification is not guaranteed: the local fallback only pattern-matches diagnostics and cannot discover these faults from RTL alone. The AI also is not a substitute for simulation or timing analysis.

## Use in the full flow

Upload the modified archive from this folder to the remote project and select it as the prep input. Use `ddr4_controller_top` as the intended/explicit top. Select only the modified archive or `rtl/`, not this whole folder containing multiple variants. The legacy archive remains unchanged.

The current orchestrator triage call supplies logs and review context, but not `rtl_sources`; the direct runner above supplies RTL explicitly. Full-flow detection requires diagnostic evidence to reach triage. The previously observed prep macro-check bug can still stop or distract that flow and is separate from this test bundle.

## Verification

The actual deterministic prep reviewer was run locally using the saved prep artifact context and each archive's RTL. The previous logic-fault archive passed; the updated archive failed with exactly the missing-module dependency error. Both ZIPs passed integrity checks, and the legacy checksum is unchanged. No remote flow, automated repair, HDL simulation, or live AI classification was run for this update.
