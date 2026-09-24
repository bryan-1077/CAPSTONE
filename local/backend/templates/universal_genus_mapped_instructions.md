# Universal Genus Mapped Instructions

This file explains the repository's universal Genus mapped-stage flow in a way that an agent or LLM can follow directly.

It is designed to be the mapped-stage companion to the universal netlist instructions.

The corresponding script is typically:

- `run_genus_mapped_universal.py`

If a project has a different wrapper name, agents should still follow the same operating rules described here.

## Goal

Run Cadence Genus mapped synthesis starting from a successful generic netlist bundle and produce a clean mapped output bundle.

This flow is intentionally focused on:

- validating the generic netlist handoff from the netlist stage
- launching mapped synthesis in a repeatable way
- collecting mapped logs and exported artifacts
- classifying failures into netlist-owned versus mapped-owned issues
- enabling bounded deterministic and optional AI-assisted fixes in the mapped loop
- preserving separation between manual flows and agent-generated flows

This flow is not supposed to redesign the circuit, fabricate outputs, or hide mapped-stage problems.

## Required Inputs

The mapped flow expects a netlist output directory containing, at minimum, a usable generic netlist bundle such as:

- `*_generic.v`
- `*_generic.sdc`
- any supporting synthesis database files the mapped script depends on

Agents must treat this bundle as the contract between netlist and mapped.

If these artifacts are missing or inconsistent, the correct action is to fail clearly and route the issue back to netlist.

## What This Mapped Flow Does

The universal mapped flow should:

1. Accept a generic netlist build directory.
2. Validate that the expected generic handoff artifacts exist.
3. Determine the intended mapped top module.
4. Create a dedicated mapped output directory.
5. Launch Cadence Genus mapped synthesis in batch mode.
6. Capture stdout, stderr, completion markers, and the exact command used.
7. Save mapped logs, reports, and generated mapped artifacts.
8. Return a machine-readable result that the orchestrator can use.
9. Review the run with deterministic checks first and merge optional AI review feedback if available.
10. If the review keeps ownership in mapped, allow only bounded mapped-stage fixes and rerun.

## Ownership Rules

This stage is responsible for:

- mapped synthesis failures after launch
- mapped optimization failures
- mapped report/export issues
- bounded mapped-stage Tcl or metadata fixes

This stage is not responsible for:

- missing generic netlist handoff files
- wrong or incomplete netlist-stage outputs
- prep-stage bundle completeness problems

If the mapped reviewer determines the input contract is broken, it should route the issue back to `netlist`.

## Directory Ownership Rules

Agent-driven mapped runs must not overwrite manual flow directories.

Agents should:

- write to a dedicated agent-owned output directory such as `build_mapped_*_lang`
- keep manual reference directories untouched
- avoid destructive cleanup outside the selected agent-owned output directory
- log exactly which input and output directories were used

Agents must not:

- delete unrelated build directories
- overwrite manual mapped results
- repurpose an existing manual flow directory without an explicit instruction

## What This Flow Produces

A successful mapped run should produce some combination of:

- mapped netlist artifacts such as `*_mapped.v` or `*.vg`
- mapped constraints such as `*_mapped.sdc`
- synthesis database artifacts such as `*.db` when supported by the environment
- mapped logs and reports
- command logs that show exactly how Genus was launched

The current runtime wrapper also records:

- a completion marker `EDA_MAPPED_RUN_COMPLETE:<rc>`
- heartbeat and tee logs during long mapped runs
- the primary log chosen for deterministic local classification

## Deterministic Review Rules

The local mapped reviewer should:

- verify the generic netlist input contract
- verify the mapped output directory exists
- look for expected mapped artifacts
- inspect the primary mapped log for structured Genus diagnostics
- classify missing input handoff artifacts as `netlist` owned
- classify mapped synthesis or export failures as `mapped_netlist` owned

It should prefer structured Genus diagnostics over arbitrary wrapped text. Continuation lines, report headers, and unrelated table rows should not be promoted to blocking errors by themselves.

## Deterministic Edit Rules

The mapped editor may apply only safe, bounded changes inside the mapped-stage input bundle.

Allowed deterministic edits include:

- small Tcl fixes for known mapped-stage export incompatibilities
- bounded metadata adjustments tied directly to reviewer findings

The mapped editor must not:

- invent new logic
- fabricate missing mapped outputs
- patch unrelated source RTL
- hide environment or licensing failures

If a problem cannot be safely repaired automatically, the edit plan should return no action for that issue.

## Expected Failure Classes

Agents should expect failures such as:

- missing generic netlist inputs
- wrong mapped top module
- mapped Tcl script issues
- library or database export issues
- tool environment or executable path problems
- mapped-stage Genus errors after launch
- AI backend quota, availability, or JSON-format failures during optional review/edit calls

The mapped flow should report these clearly and preserve the original error text when possible.

## Success Criteria

A mapped stage should only be marked successful when the following are true:

- the mapped command completed successfully
- the expected mapped output artifact exists and is non-empty
- the run produced usable logs
- the orchestrator can identify the output directory and key result files

A stage must not be marked successful solely because:

- the command launched
- the tool banner appeared
- a partial log file exists

## Failure Handling Rules

If mapped synthesis fails, the flow should:

1. preserve stdout and stderr
2. preserve the exact command used
3. mark the stage as failed
4. store a concise but actionable `last_error`
5. return enough detail for a reviewer or fixer agent to diagnose the issue

If the failure is due to a broken generic handoff from netlist, the flow should route feedback back toward `netlist`.

If the optional AI reviewer/editor fails, the flow should preserve that error in logs and continue with deterministic review/edit behavior when possible.
