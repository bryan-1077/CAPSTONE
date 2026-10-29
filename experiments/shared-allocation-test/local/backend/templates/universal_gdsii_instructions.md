# Universal GDSII Instructions

This file explains the repository's universal GDSII/export flow in a way that an agent or LLM can follow directly.

It is designed to be the GDSII-stage companion to the universal mapped instructions.

The corresponding script is typically:

- `run_innovus_GDSII_universal.py`

If a project has a different wrapper name, agents should still follow the same operating rules described here.

## Goal

Run the physical-design export stage starting from a successful mapped implementation bundle and produce a clean GDSII output bundle.

This flow is intentionally focused on:

- validating the mapped handoff from the mapped stage
- launching the GDSII/export script in a repeatable way
- collecting stage logs and layout artifacts
- classifying failures into mapped-owned versus GDSII-owned issues
- enabling bounded deterministic and optional AI-assisted fixes in the GDSII loop
- preserving separation between manual flows and agent-generated flows

This flow is not supposed to fabricate physical-design outputs or hide physical-design tool failures.

## Required Inputs

The GDSII flow expects a mapped output directory containing a usable mapped implementation bundle such as:

- `*_mapped.v`
- `*_mapped.sdc`
- implementation database or design handoff files such as `*.db` or `*.def`

Agents must treat this bundle as the contract between mapped and GDSII.

If these artifacts are missing or inconsistent, the correct action is to fail clearly and route the issue back to `mapped_netlist`.

## What This GDSII Flow Does

The universal GDSII flow should:

1. Accept a mapped implementation build directory.
2. Validate that the expected mapped handoff artifacts exist.
3. Create a dedicated GDSII output directory.
4. Launch the GDSII/export tool flow in batch mode.
5. Capture stdout, stderr, completion markers, and the exact command used.
6. Save export logs, reports, and generated layout artifacts.
7. Return a machine-readable result that the orchestrator can use.
8. Review the run with deterministic checks first and merge optional AI review feedback if available.
9. If the review keeps ownership in GDSII, allow only bounded GDSII-stage fixes and rerun.

## Ownership Rules

This stage is responsible for:

- import and physical-design export failures after launch
- stream-out or write-GDS issues
- bounded GDSII-stage script or metadata fixes

This stage is not responsible for:

- missing mapped handoff files
- wrong or incomplete mapped-stage outputs
- earlier synthesis-stage bundle completeness problems

If the GDSII reviewer determines the input contract is broken, it should route the issue back to `mapped_netlist`.

## What This Flow Produces

A successful GDSII run should produce some combination of:

- `*.gds`
- `*.gdsii`
- stage logs and reports
- command logs that show exactly how the export flow was launched

The current runtime wrapper also records:

- a completion marker `EDA_GDSII_RUN_COMPLETE:<rc>`
- heartbeat and tee logs during long GDSII runs
- the primary log chosen for deterministic local classification

## Deterministic Review Rules

The local GDSII reviewer should:

- verify the mapped input contract
- verify the GDSII output directory exists
- look for expected GDSII artifacts
- inspect the primary GDSII log for structured tool diagnostics
- classify missing mapped handoff artifacts as `mapped_netlist` owned
- classify export/import/stream-out failures as `gdsii` owned

## Deterministic Edit Rules

The GDSII editor may apply only safe, bounded changes tied directly to the current review findings.

Allowed edits include:

- small script or metadata fixes in the existing project workspace
- bounded updates that help the export flow run again without altering intended design content

The GDSII editor must not:

- fabricate layout results
- invent missing design data
- patch unrelated RTL architecture
- hide environment or licensing failures

If a problem cannot be safely repaired automatically, the edit plan should return no action for that issue.
