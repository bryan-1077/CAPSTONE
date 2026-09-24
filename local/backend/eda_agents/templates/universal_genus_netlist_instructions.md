# Universal Genus Netlist Instructions

This file explains the repository's universal Genus netlist flow in a way that an agent or LLM can follow directly.

It is designed to be the netlist-stage companion to the universal RTL prep instructions.

The corresponding script is typically:

- `run_genus_netlist_universal.py`

If a project has a different wrapper name, agents should still follow the same operating rules described here.

## Goal

Run Cadence Genus on a prepared RTL bundle and produce a clean first-pass synthesis netlist bring-up result.

This flow is intentionally focused on:

- validating the prepared RTL bundle
- selecting the intended top module
- launching Genus in a repeatable way
- collecting logs and synthesis outputs
- classifying failures into prep-owned versus netlist-owned issues
- enabling bounded deterministic and optional AI-assisted fixes in the netlist loop
- preserving separation between manual flows and agent-generated flows

This flow is not supposed to silently redesign the RTL or hide synthesis problems.

## What This Netlist Flow Is Good For

Use the universal netlist flow when:

- a prep directory has already been created successfully
- the design is ready for first Genus elaboration / synthesis bring-up
- you want a reproducible agent-driven netlist run
- you want outputs placed in a dedicated agent-owned build directory

Examples:

- `build_prep_universal_mc1` -> `build_netlist_mc_lang`
- `build_prep_universal_mc2` -> `build_netlist_mc2_lang`
- other prepared RTL bundles that contain the standard prep artifacts

## Required Inputs

The netlist flow expects a prepared build directory containing, at minimum:

- `filelist_genus.f`
- `constraints/mc_genus.sdc`
- `run_genus_mc.tcl`
- `README_prepared.txt`
- `top_module.txt`
- the prepared RTL source files listed by `filelist_genus.f`

Agents must treat these files as the contract between prep and netlist.

If these artifacts are missing or inconsistent, the correct action is to fail clearly and route the issue back to prep or the fixer agent.

## What This Netlist Flow Does

The universal netlist flow should:

1. Accept a prepared RTL build directory.
2. Validate that the required prep artifacts exist and are non-empty.
3. Determine the intended top module.
4. Create a dedicated netlist output directory.
5. Launch Cadence Genus in batch mode using the prepared Tcl and filelist.
6. Capture stdout, stderr, and the exact command used.
7. Save synthesis logs, reports, and generated netlist artifacts.
8. Return a machine-readable result that the orchestrator can use.
9. Review the run with deterministic checks first and merge optional AI review feedback if available.
10. If the review keeps ownership in netlist, allow only bounded netlist-stage fixes and rerun.

## What It Must Validate Before Running Genus

Before invoking Genus, the flow should verify:

- the input prep directory exists
- `filelist_genus.f` exists and contains at least one RTL source entry
- each filelist entry resolves to a real file
- `top_module.txt` exists and is non-empty, unless an explicit top is passed
- `run_genus_mc.tcl` exists and references the intended flow inputs
- `constraints/mc_genus.sdc` exists
- the requested Genus executable exists, if a path is supplied explicitly

These preflight checks should fail fast and produce actionable error messages.

## Top Module Rules

The preferred top-selection order is:

1. explicit `--top <module_name>` argument
2. `top_module.txt` from the prep directory
3. a clearly documented fallback only if the script is explicitly designed to support one

Agents must not silently invent a different top module when `top_module.txt` already exists.

If the top looks wrong, the correct fix is to update prep inputs or rerun with an explicit top.

## Directory Ownership Rules

This is very important.

Agent-driven netlist runs must not overwrite manual flow directories.

Agents should:

- write to a dedicated agent-owned output directory such as `build_netlist_*_lang`
- keep manual reference directories untouched
- avoid destructive cleanup outside the selected agent-owned output directory
- log exactly which directory is being used for output

Agents must not:

- delete unrelated build directories
- overwrite manual netlist results
- repurpose an existing manual flow directory without an explicit instruction

## What This Flow Produces

A successful netlist run should produce some combination of:

- synthesis log(s)
- QoR / timing / area / power reports if the Tcl emits them
- an elaborated or synthesized RTL netlist
- a generic gate-level or Genus-produced netlist suitable for the next stage
- command logs that show exactly how Genus was launched

The exact filenames may vary by project, but the flow should record the important output paths in the orchestrator state.

The current flow also writes or captures:

- a completion marker `EDA_NETLIST_RUN_COMPLETE:<rc>` after the script exits
- heartbeat and tee logs during long netlist runs, when emitted by the runtime wrapper
- the primary log chosen for local deterministic classification

## What It Does Not Do

The universal netlist flow does not:

- repair broken architecture automatically
- guarantee timing closure
- guarantee mapped or physically aware synthesis quality
- replace proper constraints engineering
- make a design backend-ready by itself
- hide Genus errors by pretending the run succeeded

Agents should treat this stage as a synthesis bring-up stage, not a signoff stage.

## Expected Failure Classes

Agents should expect failures such as:

- missing prep artifacts
- wrong top module
- missing package / interface files in the filelist
- unsupported SystemVerilog constructs for the target tool setup
- constraint issues
- missing macro definitions
- Genus executable path problems
- license or environment setup problems
- Tcl script issues
- AI backend quota, availability, or JSON-format failures during optional review/edit calls

The netlist flow should report these clearly and preserve the original error text when possible.

## Success Criteria

A netlist stage should only be marked successful when the following are true:

- the Genus command completed successfully
- the expected output netlist artifact exists and is non-empty
- the run produced usable logs
- the orchestrator can identify the output directory and key result files

A stage must not be marked successful solely because:

- the command launched
- the tool banner appeared
- a partial log file exists

## Failure Handling Rules

If Genus fails, the flow should:

1. preserve stdout and stderr
2. preserve the exact command used
3. mark the stage as failed
4. store a concise but actionable `last_error`
5. return enough detail for a reviewer or fixer agent to diagnose the issue

If the failure is due to bad prep content, the flow should route feedback back toward prep/fix rather than silently forcing a workaround.

If the optional AI reviewer/editor fails, the flow should not treat that as the root synthesis failure. It should preserve the AI error in logs and continue with deterministic review/edit behavior when possible.

## Logging Rules

The netlist flow should log at least:

- input build directory
- output build directory
- resolved top module
- resolved Genus executable path, if applicable
- launched command
- stdout
- stderr
- detected output files
- final pass/fail result

Agents should make debugging easy from logs alone.

For deterministic local review, agents should prefer structured Genus diagnostics over arbitrary wrapped text. Continuation lines, report headers, and unrelated table rows should not be promoted to blocking semantic errors by themselves.

## Environment Rules

If the repository uses a remote or scheduled environment, the flow may need to:

- source the user shell environment
- load the course or lab tool setup
- run under `srun` or another scheduler wrapper
- execute `/bin/bash -lc ...` so the tool environment is initialized correctly

Agents must not assume the local shell environment is already enough.

They should preserve any known-good environment bootstrap logic already present in the repository.

## Macro and Define Handling

If a design requires synthesis defines, the netlist flow should only pass them when:

- they were explicitly requested by the user
- they were recorded during prep
- the project has a documented requirement

Agents should not guess defines casually because that can change architecture and synthesis behavior.

## Interaction With Prep

Prep owns source sanitization and bundle construction.

Netlist owns synthesis bring-up.

That means:

- if `filelist_genus.f` is wrong, that is usually a prep or fix issue
- if the top is wrong, that is usually a prep selection issue unless the user explicitly overrides it
- if Genus cannot elaborate because needed files are absent, the fix should usually happen upstream

Agents should avoid burying prep mistakes inside netlist-stage hacks.

## Interaction With Mapped Netlist and GDSII Stages

The universal netlist flow is upstream of:

- mapped netlist generation
- downstream physical design / GDSII work

Agents must not claim those later stages are safe just because the first netlist stage passed.

A successful netlist run means the prepared RTL made it through the intended Genus bring-up path.

It does not mean the design is timing-clean, placement-clean, antenna-clean, or signoff-ready.

## Typical Usage

Basic:

```bash
python3 run_genus_netlist_universal.py --builddir build_prep_universal_mc1 --outdir build_netlist_mc_lang
```

With explicit Genus path:

```bash
python3 run_genus_netlist_universal.py --builddir build_prep_universal_mc1 --genus /opt/coe/cadence/DDI231/bin/genus --outdir build_netlist_mc_lang
```

With explicit top override:

```bash
python3 run_genus_netlist_universal.py --builddir build_prep_universal_mc2 --top mem_ctrl_top --outdir build_netlist_mc2_lang
```

If the environment requires a scheduler wrapper, preserve the repository's existing launch pattern instead of replacing it with a guessed shortcut.

## LLM Operating Rules

If you are an agent using the universal netlist flow:

1. Treat the prep directory as the input contract.
2. Validate required prep artifacts before invoking Genus.
3. Prefer explicit top selection over hidden inference at this stage.
4. Keep agent-generated results in agent-owned output directories.
5. Do not overwrite manual build directories.
6. Preserve exact command logs and tool output.
7. Fail clearly when the flow is broken.
8. Do not fabricate success from partial evidence.
9. Route prep-related issues back to prep or the fixer agent.
10. Treat QoR and timing reports as diagnostic outputs, not automatic proof of correctness.
11. Treat optional AI review/edit as additive only. Deterministic review must still stand on its own.
12. When deterministic netlist edits are available, keep them bounded to proven synthesis-compatibility fixes in existing prepared RTL rather than architectural rewrites.

## Practical Interpretation

The universal netlist flow is best thought of as:

- a synthesis bring-up runner
- a validation checkpoint between prep and mapped netlist
- a reproducible way to capture first-pass Genus results

It is not:

- a replacement for RTL design review
- a replacement for proper constraints engineering
- a replacement for mapped synthesis review
- a replacement for backend signoff analysis

Use it to get prepared RTL through a clean, diagnosable Genus netlist stage while preserving strict separation between manual directories and agent-generated directories.
