# Universal Genus Prep Instruction

This file explains the repository's universal RTL prep flow in a way that an agent or LLM can follow directly.

It is designed to work better across multiple RTL codebases than the older project-specific prep flow.

The corresponding script is:

- `prepare_rtl_for_genus_universal.py`

## Goal

Prepare an arbitrary RTL zip or RTL directory for a smoother Cadence Genus bring-up run without hardcoding one project's wrapper structure.

This prep flow is intentionally generic. It does not try to rewrite architecture, flatten interfaces into custom wrappers, or silently change behavior.

It focuses on:

- discovering RTL files automatically
- removing common simulation-only noise
- excluding obvious unit-test or testbench files
- inferring a likely synthesis top
- generating a usable filelist, starter SDC, and Genus Tcl

In the current agent flow, prep review is always deterministic first. Optional AI review/edit feedback may be merged in when the backend is available, but the prep stage can still apply deterministic bounded fixes without AI.

## What This Prep Flow Is Good For

Use the universal prep script when:

- the design is not the original `MemoryController.zip` project
- the RTL hierarchy is different from the old controller-specific wrapper flow
- the design already has reasonable synthesizable module structure
- you want a broad "get me to first Genus elaboration" prep step

Examples:

- `MemoryController.zip`
- `MemoryController_2.zip`
- other moderately clean SV/V RTL trees with modules, packages, and interfaces

## What This Prep Flow Does

The universal prep script:

1. Accepts either a `.zip` file or an extracted RTL directory.
2. Extracts or copies the source into a staging area.
3. Recursively discovers `.sv`, `.v`, `.svh`, and `.vh` files.
4. Copies RTL into a prep directory while sanitizing common synthesis-hostile constructs.
5. Preserves packages, interfaces, and module hierarchy.
6. Excludes obvious unit-test / testbench files when they are clearly named that way.
7. Infers a likely top module using heuristics.
8. Detects likely clock and reset ports from the inferred top.
9. Generates:
   - `filelist_genus.f`
   - `constraints/mc_genus.sdc`
   - `run_genus_mc.tcl`
   - `README_prepared.txt`
   - `top_module.txt`

## What It Removes

The universal prep script strips or suppresses:

- `sequence ... endsequence`
- `property ... endproperty`
- `assert property`
- `cover property`
- `assume property`
- debug macros like `` `DPRINT ``
- `$display`, `$strobe`, `$monitor`

This is meant to reduce Genus noise and avoid requiring verification-only constructs in the synthesis filelist.

## What It Preserves

The universal prep script intentionally preserves:

- ordinary modules
- packages
- SystemVerilog interfaces
- modports
- hierarchy
- parameterization
- project-specific file naming

This is important because the script is supposed to be broadly reusable, not silently rewrite one project into another project's structure.

## What It Does Not Do

The universal prep script does not:

- generate project-specific `_impl` wrappers
- flatten interfaces into hand-authored port bundles
- rewrite DDR behavior into a custom approximation
- pipeline logic
- optimize timing behavior
- guarantee the inferred top is correct in every project
- generate signoff-quality constraints

Agents should treat the universal prep as a bring-up tool, not a full architectural translation layer.

One important nuance for the repository flow:

- the prep script itself stays generic
- bounded synthesis-only adaptation, when allowed, is handled by the prep review/edit loop rather than by silently changing the generic prep script into a project-specific transformer

## Top Inference Rules

The script tries to choose a likely top automatically.

It prefers modules that:

- are not instantiated by other design modules
- are only referenced by obvious testbench-like wrappers
- instantiate other modules
- look like integration-level blocks
- have clock/reset-like ports

It penalizes modules that:

- look like `tb`, `test`, `bench`, or `unit_test`
- contain `initial` blocks
- look like obvious verification wrappers

Important:

- auto top inference is helpful, not perfect
- if the chosen top is wrong, rerun with `--top <module_name>`

The detected top is written to:

- `top_module.txt`
- `README_prepared.txt`

## Clock and Reset Detection

The script tries to detect:

- clock-like input ports such as names containing `clk`, `clock`, or `aclk`
- reset-like input ports such as names containing `rst`, `reset`, or `areset`

It then creates a starter SDC with:

- `create_clock`
- `set_clock_uncertainty`
- `set_false_path -from` resets
- generic `set_input_delay` / `set_output_delay` on normal timed ports

This SDC is intentionally conservative and incomplete.

Agents must understand:

- this is only a bring-up constraint file
- interface timing, PHY timing, DDR timing, board timing, and multi-clock constraints still need manual review

## Interface Handling

Unlike the old controller-specific prep flow, the universal prep flow does not assume that interfaces must be flattened away.

It allows designs that use:

- `interface`
- `modport`
- package imports

This makes it more compatible with newer codebases like `MemoryController_2.zip`.

However:

- some interface-heavy projects may still require custom wrapper work for synthesis or backend use
- preserving interfaces is not the same as guaranteeing downstream tool compatibility

## Testbench Exclusion Rules

The universal prep script excludes files that are clearly named like testbench sources, such as:

- `*_tb.sv`
- `*_unit_test.sv`
- names containing `testbench`
- names containing `svunit`

It also treats `svunit_pkg` usage as a strong sign that a file is verification-only.

It does not aggressively exclude generic names like `Top.sv`, because in some projects that may be a real top.

## Defines and Macros

The script detects preprocessor macros used in the source and lists them in `README_prepared.txt`.

It does not automatically enable all detected macros because that can change architecture.

If a project needs a macro, pass it explicitly:

```bash
python3 prepare_rtl_for_genus_universal.py --input my_design.zip --define DDR4 --define FPGA
```

Agents should prefer explicit macro enabling over guessing.

## Typical Usage

Basic:

```bash
python3 prepare_rtl_for_genus_universal.py --input MemoryController.zip --outdir build_prep_universal_mc1
```

With explicit top:

```bash
python3 prepare_rtl_for_genus_universal.py --input MemoryController_2.zip --outdir build_prep_universal_mc2 --top mem_ctrl_top
```

With defines:

```bash
python3 prepare_rtl_for_genus_universal.py --input MemoryController_2.zip --outdir build_prep_universal_mc2 --top mem_ctrl_top --define DDR4
```

Then run Genus:

```bash
python3 run_genus_netlist_universal.py --builddir build_prep_universal_mc2
python3 run_genus_mapped_universal.py --netlistdir build_netlist_mc
```

The downstream Genus wrappers now read `top_module.txt` automatically when `--top` is not given.

## How This Differs From The Old Prep Script

The old prep flow in `prepare_rtl_for_genus.py` is specialized for the original memory controller project.

It assumes:

- a specific file list
- specific original module names
- specific generated `_impl` wrappers
- specific DDR modeling replacements

The universal prep flow avoids those assumptions.

That means:

- it is more reusable
- it is less invasive
- it is less likely to preserve a broken project by accident through hidden rewrites
- it may require more manual review on complex designs

## LLM Operating Rules

If you are an agent using the universal prep flow:

1. Prefer the universal prep script for unfamiliar RTL trees.
2. Read `README_prepared.txt` after prep and confirm the inferred top makes sense.
3. Check `top_module.txt` before launching Genus.
4. Review detected macros and decide whether any `--define` flags are required.
5. Treat the generated SDC as a starting point only.
6. Do not assume interface-heavy designs are automatically backend-ready.
7. Do not silently rewrite user RTL unless there is a strong, explicit reason.
8. If timing or elaboration fails, inspect whether the issue is:
   - architectural
   - constraint-related
   - interface-related
   - top-selection-related

## Practical Interpretation

The universal prep flow is best thought of as:

- a generic synthesis hygiene pass
- a project discovery pass
- a Genus bring-up accelerator

It is not:

- a complete synthesis adaptation layer
- a replacement for project-specific wrapper engineering
- a signoff constraint generator

Use it to get unknown RTL into a cleaner, more diagnosable state first.
