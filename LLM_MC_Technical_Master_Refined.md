# LLM MC Code-Adjacent Technical Master Document

This document is a repository-grounded technical reference for the `d05_multiBank` demo under the LLM memory-controller project. It is intended to serve as a handoff document, demo-prep reference, design-review baseline, and new-chat context starter.

The document is intentionally code-adjacent:

- It describes the actual current files in this repository snapshot.
- It prioritizes the currently generated 4-bank DDR4 simple-scheduler memory-system demo configured by `configs/user_input.yaml`.
- It distinguishes implemented behavior from planned behavior, support-only utilities, and legacy paths.
- It avoids claiming full DDR4 controller functionality where the code implements a simpler educational/demo memory system.

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [End-to-End Flow](#2-end-to-end-flow)
3. [Repository and File Structure](#3-repository-and-file-structure)
4. [Configuration System](#4-configuration-system)
5. [JEDEC and Timing Expansion](#5-jedec-and-timing-expansion)
6. [Intermediate Representation](#6-intermediate-representation)
7. [File-by-File Python Breakdown](#7-file-by-file-python-breakdown)
8. [Deterministic FSM Generation](#8-deterministic-fsm-generation)
9. [Dual-LLM RTL Generation](#9-dual-llm-rtl-generation)
10. [Wrapper Generation and System Integration](#10-wrapper-generation-and-system-integration)
11. [Bank Architecture](#11-bank-architecture)
12. [Scheduler Architecture](#12-scheduler-architecture)
13. [Global Timing Architecture](#13-global-timing-architecture)
14. [Refresh Architecture](#14-refresh-architecture)
15. [Read and Write Support](#15-read-and-write-support)
16. [Minimal Memory-System v2](#16-minimal-memory-system-v2)
17. [Testbench Generation and Verification](#17-testbench-generation-and-verification)
18. [Corner Cases](#18-corner-cases)
19. [Simulation and Waveform Flow](#19-simulation-and-waveform-flow)
20. [Validation Plan](#20-validation-plan)
21. [Completed Work](#21-completed-work)
22. [Limitations](#22-limitations)
23. [Future Work](#23-future-work)
24. [Demo Guide](#24-demo-guide)

## 1. Project Overview

### What This Is / What This Is Not

This system is **not a production DDR4 controller**. It does not implement full JEDEC command protocols, PHY behavior, or high-performance scheduling.

It **is** a generated control-plane plus a simplified memory-system backend that preserves timing structure, arbitration, bank behavior, and read/write semantics in a way that is observable, testable, and extensible.


This project generates a simplified DDR4 controller-oriented hardware subsystem from YAML specifications. The flow combines:

- deterministic compilation passes for config expansion and FSM generation,
- a structured IR layer used as the internal source of truth,
- LLM-assisted generation for non-FSM datapath-style blocks,
- a wrapper generator that integrates the generated modules into a runnable top-level system,
- a generated directed self-checking testbench,
- a Verilator plus GTKWave simulation flow.

What it generates today, in the current checked-in demo configuration:

- reusable timing/state modules such as `ddr4_bank_activate_fsm`, `ddr4_bank_tRAS_fsm`, `ddr4_bank_precharge_fsm`, `ddr4_scheduler_scheduler`, and `ddr4_refresh_refresh_controller`,
- optional global timing blocks `ddr4_tFAW_tFAW_tracker` and `ddr4_tRRD_simple_tRRD`,
- a reusable per-bank integration shell `ddr4_bank_top`,
- a top-level integrated system `ddr4_controller_top`,
- a self-checking `tb_ddr4_controller_top.sv`,
- a filelist, manifest, README, and GTKWave preset support for demo use.

The project exists because there are two related but different goals:

- controller generation: generating legal control-path RTL such as FSMs, refresh request logic, timing trackers, and scheduling/arbitration,
- memory-system behavior: making the generated controller observable as a functioning system that can accept transactions, preserve read/write intent, store data, and return responses in simulation.

That distinction matters. Earlier or narrower versions of this flow could stop at "controller-like control logic." The current `d05_multiBank` demo goes further by integrating:

- bank selection,
- global activation gating,
- typed transaction propagation (`READ` vs `WRITE`),
- bank-local storage,
- read response generation.

It is still not a full DDR4 memory controller in the production sense. It is best understood as a generated DDR4-inspired control-and-memory-system demo platform with real timing/state structure but intentionally simplified data and protocol behavior.

## 2. End-to-End Flow

### End-to-End Transaction Story (Concrete)

A single transaction flows through the system as:

txn_valid → scheduler → issue_txn → bank select (txn_bank) → bank_cmd_valid  
→ bank FSM (activate → active → precharge) → act_pulse  
→ global timing gates (tRRD / tFAW) → memory array  
→ (if READ) rsp_valid + rsp_rdata (1-cycle later)

Blocked case example:
txn_valid → scheduler issues → but tRRD_block=1 → no bank_cmd_valid → no state advance → txn stalls.


The real current flow is orchestrated by `run_flow.py`.

Current high-level flow:

```text
configs/user_input.yaml
  -> run_flow.py
  -> validate user config (fail fast)
  -> generate internal inputs/generated/*.yaml
  -> expand_spec.py
  -> expanded/<design>/*.yaml + master.yaml
  -> design.py
  -> validator.py -> ir/*.json
  -> fsm_generator.py or dual_llm_rtlGen.py
  -> width_safety.py post-processing
  -> rtl_output/<module>/<module>.sv
  -> generate_wrapper.py
  -> rtl_output/ddr4_bank_top.sv
  -> rtl_output/ddr4_controller_top.sv
  -> generate_testbench.py
  -> tb/tb_ddr4_controller_top.sv
  -> run_sim.sh
  -> Verilator build + run
  -> tb_ddr4_controller_top.vcd
  -> optional GTKWave preset
```

More specifically:

1. User config entry point

   `configs/user_input.yaml` is the current user-facing configuration. In the checked-in demo it selects:

   - `DDR4`
   - speed `3200`
   - `4` banks
   - scheduler `simple`
   - `refresh`, `tFAW`, and `tRRD` enabled

2. Validation and config expansion

   `run_flow.py` treats any YAML containing a `profile` block as the user-facing config schema. It validates it before generation side effects occur. If valid, it expands that single config into internal generated inputs:

   - `inputs/generated/ddr4_bank.yaml`
   - `inputs/generated/ddr4_scheduler.yaml`
   - `inputs/generated/ddr4_tFAW.yaml`
   - `inputs/generated/ddr4_tRRD.yaml`
   - `inputs/generated/ddr4_refresh.yaml`

   This is important because the rest of the older flow is still organized around per-feature input YAMLs rather than a single monolithic user config.

3. JEDEC expansion and template concretization

   For each generated input YAML, `expand_spec.py`:

   - reads `jedec/jedec_dictionary.yaml`,
   - reads `jedec/feature_templates.yaml`,
   - substitutes symbolic timing placeholders with integer cycle counts,
   - emits concrete submodule YAMLs in `expanded/<design>/`,
   - emits `master.yaml` manifests for those expanded designs.

4. IR generation and RTL generation

   `design.py` validates each concrete YAML through `validator.py`, writes JSON IR into `ir/`, then routes generation by `design_type`:

   - `fsm` -> `fsm_generator.py`
   - non-FSM, currently `datapath` -> `dual_llm_rtlGen.py`

5. Post-generation width hardening

   `design.py` always passes generated RTL through `width_safety.py`. In practice this matters most for:

   - `ddr4_tRRD_simple_tRRD`
   - `ddr4_tFAW_tFAW_tracker`

   Those modules are effectively normalized to width-safe known implementations after the main generator step.

6. Top-level integration

   `generate_wrapper.py`:

   - checks module interface compatibility,
   - generates `ddr4_bank_top`,
   - generates `ddr4_controller_top`,
   - writes the RTL package README, manifest, and filelist.

7. Testbench generation

   `run_flow.py` calls `generate_testbench.generate_testbench()`, which emits `tb/tb_ddr4_controller_top.sv`.

8. Simulation

   `run_sim.sh`:

   - collects all `rtl_output/**/*.sv`,
   - compiles with Verilator,
   - runs the generated testbench,
   - writes `tb_ddr4_controller_top.vcd`,
   - optionally launches GTKWave with a preset based on bank count and scheduler mode.

9. Waveform viewing

   For the current 4-bank simple-scheduler memory-system demo, `run_sim.sh` prefers:

   - `gtk_presets/memsys_demo_4bank_simple.gtkw`

## 3. Repository and File Structure

Practical map of the current directory:

- `configs/`
  User-facing configuration. `user_input.yaml` is the main current entry point.

- `inputs/generated/`
  Internal per-feature YAMLs created by `run_flow.py` from the user config. These are not hand-authored in normal use.

- `jedec/`
  Timing dictionary and feature templates. This is the declarative source of supported timing profiles and feature skeletons.

- `expanded/`
  Concrete submodule YAMLs created by `expand_spec.py`. Each directory corresponds to one design family such as `ddr4_bank`, `ddr4_scheduler`, `ddr4_tFAW`, `ddr4_tRRD`, and `ddr4_refresh`.

- `ir/`
  JSON IR emitted by `design.py` after validation. These files are the best machine-readable snapshot of what the validators and templates actually resolved to.

- `rtl_output/`
  Generated SystemVerilog modules plus package metadata. In the current checked-in build this includes:
  - `ddr4_controller_top`
  - `ddr4_bank_top`
  - the three bank timing FSMs
  - the bank sequencer
  - scheduler
  - refresh controller
  - tFAW tracker
  - tRRD tracker
  - `README.md`
  - `manifest.json`
  - `filelist.f`

- `tb/`
  Generated top-level testbench. Currently contains `tb_ddr4_controller_top.sv`.

- `gtk_presets/`
  GTKWave save files for different demo variants. Presets are curated by architecture mode rather than autogenerated from signal discovery.

- `obj_dir/`
  Verilator build outputs. Derived artifacts.

- Top-level Python flow files
  These implement the generation pipeline.

- Top-level helper/docs
  `run_sim.sh`, `clear.sh`, `MODULES_EXPLAINED.md`, and the generated VCD.

## 4. Configuration System

The user-facing config schema is defined in practice by `run_flow.py`, not by a separate schema file.

Current user config shape:

```yaml
design_name: "ddr4_controller"
profile:
  type: "basic"
memory:
  protocol: "DDR4"
  speed: "3200"
topology:
  banks: 4
features:
  scheduler: "simple"
  refresh: true
  tFAW: true
  tRRD: true
```

Supported options enforced by `run_flow.py`:

- protocols: `DDR4`
- speeds: `2400`, `3200`
- banks: `1`, `2`, `4`
- scheduler modes: `simple`, `round_robin`
- boolean features: `refresh`, `tFAW`, `tRRD`

Important fail-fast behavior:

- missing required fields raise `ValueError` before generation begins,
- wrong types raise clear errors,
- unsupported protocols/speeds/bank counts/scheduler modes are rejected,
- `banks > 1` with `scheduler != simple` is explicitly rejected.

That last point is one of the most important architectural restrictions in the repo today. The round-robin scheduler exists, but multi-bank generation is intentionally restricted to the simple scheduler.

Generated internal input YAMLs:

- The user config is expanded into one input YAML per managed design family.
- Disabled optional features are omitted.
- stale generated input files and stale derived artifacts for disabled designs are removed.

How config drives architecture:

- `topology.banks` changes wrapper shape, port width, number of bank instances, and banked storage dimensions.
- `features.scheduler` selects `scheduler` or `scheduler_round_robin` feature expansion.
- `refresh`, `tFAW`, and `tRRD` control whether the corresponding modules are generated and instantiated.
- `memory.speed` selects the JEDEC timing profile and therefore the concrete cycle counts injected into the design.

## 5. JEDEC and Timing Expansion

JEDEC timing data enters through `jedec/jedec_dictionary.yaml`.

What the dictionary contains:

- `DDR4-2400`
- `DDR4-3200`
- clock frequency per profile
- timing in ns
- timing precomputed in cycles
- comments documenting the cycle formula and JEDEC source assumptions

For the current demo configuration, `DDR4-3200` yields:

- `tRCD = 22`
- `tRP = 22`
- `tRAS = 52`
- `tRRD = 6`
- `tFAW = 40`
- `tRFC1 = 560`
- `tREFI = 12480`

How this becomes cycle-accurate behavior:

- `expand_spec.py` builds a substitution map from template placeholder names to integer timing cycle values.
- It replaces placeholders such as `{tRCD_cycles}`, `{tRRD_cycles}`, `{tFAW_cycles}`, and `{tREFI_cycles}`.
- The expanded YAMLs then contain literal integer comparisons like `tRCD_counter >= 22` or `counter >= 12480`.
- `validator.py` infers widths from those literal thresholds.
- `fsm_generator.py` and LLM/datapath generation then implement logic using those concrete values.

`expand_spec.py` also cross-checks stored cycle counts against the formula:

```text
cycles = ceil(timing_ns * clock_frequency_mhz / 1000)
```

It treats the YAML-stored cycle count as authoritative but prints warnings if the recomputed value differs.

## 6. Intermediate Representation

The IR is a lightweight normalized design model built by `validator.py`.

IR dataclasses:

- `Port`
- `Clock`
- `Reset`
- `Transition`
- `FSM`
- `Design`
- expression nodes such as `SignalRef`, `Const`, `Compare`, `BoolOp`

What `validator.py` does:

- loads a concrete expanded YAML,
- validates top-level fields,
- normalizes port definitions,
- parses FSM transitions into a structured condition AST,
- checks for invalid states and duplicate transitions,
- chooses reset state,
- infers signal widths from literal comparisons,
- stores additional metadata from unrecognized YAML keys.

Why the IR matters:

- it decouples expansion from generation,
- it is the source for deterministic FSM RTL generation,
- it feeds structured context into the LLM flow,
- it preserves metadata such as invariants, forbidden patterns, scheduler mode, output overrides, and datapath behavior descriptions.

Kinds of metadata carried today:

- `implementation_constraints`
- `invariants`
- `forbidden_patterns`
- `scheduler_mode`
- `output_overrides`
- `behavior`
- `inferred_signal_widths`
- validator warnings

In practice, the IR JSON files in `ir/` are one of the most useful artifacts for understanding what the system currently thinks a module is supposed to do.

## 7. File-by-File Python Breakdown

### `run_flow.py`

Purpose:

- Real top-level orchestrator for the current flow.

Inputs:

- a user config YAML or a directory/file of legacy per-design YAMLs.

Outputs:

- generated `inputs/generated/*.yaml`,
- expanded YAMLs,
- IR JSON,
- generated RTL,
- wrapper RTL,
- generated testbench.

Key responsibilities:

- validate the user-facing config,
- expand it into internal design-family inputs,
- clean stale artifacts for disabled features,
- call `expand_spec.py`,
- call `design.py` on each expanded YAML,
- call `generate_wrapper.py`,
- call `generate_testbench()`,
- enforce a common `` `timescale 1ns/1ps `` line on every generated `.sv`.

Current/legacy status:

- current and authoritative for end-to-end flow.

Notable implementation choices:

- treats user config detection heuristically via presence of `profile`,
- enforces the multi-bank plus simple-scheduler restriction centrally,
- manages a fixed set of design families with `MANAGED_DESIGN_ARTIFACTS`.

### `expand_spec.py`

Purpose:

- Compile high-level per-design feature YAML into concrete submodule YAMLs.

Inputs:

- internal design input YAML,
- `jedec/jedec_dictionary.yaml`,
- `jedec/feature_templates.yaml`.

Outputs:

- `expanded/<design>/*.yaml`,
- `expanded/<design>/master.yaml`.

Key responsibilities:

- validate high-level input,
- resolve JEDEC profile,
- substitute placeholders in FSM conditions and datapath behavior blocks,
- emit concrete YAMLs for validator consumption,
- emit master manifests.

Current/legacy status:

- current, but some comments still refer to earlier single-feature or Week-10 scope.

Notable implementation choices:

- FSM and datapath submodules are both supported,
- datapath modules bypass `state_machine` and carry a `behavior` block instead,
- `master.yaml` is still emitted even though current wrapper generation does not depend on it.

### `validator.py`

Purpose:

- Validate concrete YAML and build the internal IR.

Inputs:

- expanded module YAML.

Outputs:

- in-memory `Design` IR object,
- indirectly, JSON IR written by `design.py`.

Key responsibilities:

- schema checks,
- simple expression parsing for integer comparisons and `&&`/`||`,
- semantic FSM checks,
- inferred signal width derivation,
- propagation of metadata.

Current/legacy status:

- current and central.

Notable implementation choices:

- parser is intentionally small,
- width inference is driven from integer thresholds in conditions,
- unknown top-level YAML fields are preserved as metadata rather than dropped.

### `design.py`

Purpose:

- One-module generation driver.

Inputs:

- one expanded module YAML.

Outputs:

- one IR JSON file in `ir/`,
- one SystemVerilog module under `rtl_output/<module>/`.

Key responsibilities:

- load `validator.py`,
- validate the spec,
- serialize IR to JSON,
- choose deterministic FSM or dual-LLM generation,
- apply `width_safety.py`,
- write final RTL.

Current/legacy status:

- current.

Notable implementation choices:

- generation routing is purely by `design_type`,
- error messages and comments still contain a few stale naming references from older versions.

### `fsm_generator.py`

Purpose:

- Deterministically emit synthesizable SystemVerilog for FSM-style modules.

Inputs:

- validated `Design` IR with `fsm`.

Outputs:

- SystemVerilog module text.

Key responsibilities:

- encode states,
- infer/register counters,
- generate state register, next-state logic, and Moore-style outputs,
- respect output overrides from metadata,
- hard-fail on unsupported condition AST forms.

Current/legacy status:

- current and used for most controller modules.

Notable implementation choices:

- counters are incremented only in the state that uses them,
- counters reset on state transition,
- illegal state encodings recover to reset state,
- outputs can be heuristic-mapped by name unless explicitly overridden.

### `dual_llm_rtlGen.py`

Purpose:

- Generate datapath-style modules through a dual-model generate-review-merge loop.

Inputs:

- validated `Design` IR,
- raw YAML text.

Outputs:

- SystemVerilog text for datapath modules.

Key responsibilities:

- build structured prompt context through `ir_to_llm_context.py`,
- call generator model,
- syntax-check the result,
- call reviewer model,
- merge spec-grounded reviewer fixes,
- keep a short sliding history,
- retry syntax repair,
- detect repeated issues and escalate merge instructions.

Current/legacy status:

- current for datapath modules.

Notable implementation choices:

- JSON-only model protocol,
- reviewer findings are typed as `SPEC_VIOLATION`, `NON_ISSUE`, or `AMBIGUITY`,
- syntax checking prefers Verilator lint and falls back to heuristics,
- width safety is enforced again after model output.

### `ir_to_llm_context.py`

Purpose:

- Convert IR plus metadata into a structured, semantically enriched prompt context.

Inputs:

- validated `Design` IR.

Outputs:

- normalized context dict,
- stable JSON string form for prompt injection.

Key responsibilities:

- expose ports, reset, metadata, behavior, invariants, and constraints,
- turn datapath behavior into structured constraints,
- explicitly encode "must not assume" rules for the models.

Current/legacy status:

- current and important for the LLM path.

### `width_safety.py`

Purpose:

- Replace or normalize certain generated modules with width-safe known-good RTL.

Inputs:

- generated RTL text,
- module name.

Outputs:

- possibly replaced RTL text.

Key responsibilities:

- emit canonical `ddr4_tRRD_simple_tRRD`,
- emit canonical `ddr4_tFAW_tFAW_tracker`,
- preserve other modules unchanged.

Current/legacy status:

- current and architecturally significant.

Important implication:

- for `tRRD` and `tFAW`, the checked-in final RTL is not just "whatever the LLM produced"; the post-pass can fully replace it.

### `generate_wrapper.py`

Purpose:

- Assemble the generated modules into the reusable per-bank wrapper and the top-level controller wrapper.

Inputs:

- generated module availability,
- generated/user config for bank count and scheduler mode,
- module interface metadata from IR or expanded YAML.

Outputs:

- `rtl_output/ddr4_bank_top.sv`,
- `rtl_output/ddr4_controller_top.sv`,
- package `README.md`,
- `manifest.json`,
- `filelist.f`.

Key responsibilities:

- validate wrapper-facing interfaces,
- create bank routing,
- instantiate banks and optional global modules,
- implement banked storage,
- implement read response path,
- package the generated handoff bundle.

Current/legacy status:

- current and central to the memory-system integration layer.

### `generate_testbench.py`

Purpose:

- Emit a deterministic self-checking top-level testbench tailored to the generated design shape.

Inputs:

- generated wrapper RTL,
- manifest,
- IR JSON,
- generated/user config.

Outputs:

- `tb/tb_ddr4_controller_top.sv`.

Key responsibilities:

- inspect the current wrapper and enabled features,
- build directed stimulus and assertions,
- vary checks based on bank count and feature presence,
- emit coverage-style flags and final PASS/FAIL reporting.

Current/legacy status:

- current.

### `check_interfaces.py`

Purpose:

- Validate interface compatibility.

Inputs:

- currently: module names plus repo root from `generate_wrapper.py`,
- historically/optionally: `master.yaml` plus submodule specs through its CLI path.

Outputs:

- raises on mismatch, or prints structured diagnostics in CLI mode.

Key responsibilities:

- enforce `WRAPPER_INTERFACE_REQUIREMENTS`,
- load metadata from IR or expanded YAML,
- validate required wrapper-facing ports,
- support older master-YAML interface checking flows.

Current/legacy status:

- current for wrapper validation,
- the CLI/master-YAML path remains support tooling.

Important caveat:

- some `master.yaml` files contain stale absolute paths from a different machine, so the CLI path is not the best representation of the currently exercised flow.

### `rtlGen.py`

Purpose:

- older single-model LLM RTL generation agent.

Inputs:

- prompt text only.

Outputs:

- generated RTL file in an older agent-style loop.

Current/legacy status:

- legacy/support-only.

Why it matters:

- `design.py` still checks that it exists and imports it,
- but the current non-FSM path uses `dual_llm_rtlGen.py` instead.

### `run_sim.sh`

Purpose:

- build and run the current generated design under Verilator and optionally open a waveform preset.

Current/legacy status:

- current.

Notable implementation choices:

- detects scheduler mode and bank count from generated YAML/config/wrapper comments,
- chooses a GTKWave preset based on architecture shape,
- uses all generated `.sv` under `rtl_output`.

## 8. Deterministic FSM Generation

FSM generation is the project's fully deterministic and tool-controlled generation path.

How it works:

- `validator.py` parses the FSM and conditions into an AST.
- `fsm_generator.py` encodes the states in a compact enum.
- It emits:
  - module header,
  - state typedef,
  - inferred counters if needed,
  - state register,
  - next-state combinational logic,
  - output combinational logic.

Guarantees it provides:

- no LLM dependency for FSM modules,
- explicit reset behavior,
- explicit width-safe comparison literals for inferred counters,
- deterministic transition ordering,
- default illegal-state recovery.

Assumptions it makes:

- transitions are expressible in the validator's restricted expression language,
- counters are inferable from literal threshold comparisons,
- timer ownership maps cleanly to the state that checks the timer,
- Moore-style output logic is acceptable unless overridden.

Resets, widths, and transitions:

- sync or async reset is taken from IR reset metadata,
- widths come from inferred thresholds or explicit port widths,
- transitions from the same state are emitted as priority-ordered `if` / `else if` chains,
- missing transitions default to state hold,
- counters reset on state change and only increment in the bound state.

In the current repo, these deterministic FSMs cover the bulk of the control-plane logic:

- bank sequencer
- tRCD/tRAS/tRP timer FSMs
- simple scheduler
- refresh controller

## 9. Dual-LLM RTL Generation

The LLM path exists because not every useful hardware block in this repo fits the narrow deterministic FSM generator model.

Current use cases:

- `ddr4_tFAW_tFAW_tracker`
- `ddr4_tRRD_simple_tRRD`

Both are tagged `design_type: datapath` in the expanded YAML and carry structured behavior descriptions instead of `state_machine` definitions.

How `dual_llm_rtlGen.py` works:

1. Build authoritative structured context from IR and metadata.
2. LLM #1 generates candidate RTL.
3. Run syntax checking.
4. If syntax fails, give syntax diagnostics back to LLM #1 for repair.
5. LLM #2 reviews the candidate strictly against the structured context.
6. If LLM #2 finds spec-grounded violations, LLM #1 merges/fixes using the typed review.
7. Keep a short sliding history to improve convergence and detect repeated failures.
8. Return the best RTL seen, preferably a syntax-clean one.

Generator/reviewer loop:

- Generator model: intended to produce compliant RTL.
- Reviewer model: intended to act as an adversarial correctness check.
- Merger: same generator-side model, but forced to reconcile reviewer feedback.

Syntax gating:

- First choice is Verilator lint.
- If Verilator is unavailable, it falls back to heuristic checks.
- There are bounded syntax-fix retries per round.

Review and merge loop:

- reviewer status and issues are parsed from strict JSON,
- only `SPEC_VIOLATION` findings are supposed to drive corrective merges,
- repeated identical issue signatures trigger an escalation mode,
- the process caps at `MAX_ROUNDS = 4`.

Historically relevant issue classes, as reflected in the code and prompts:

- markdown-fenced output instead of raw RTL,
- malformed literals,
- missing `endmodule`,
- serializing Python dataclass representations instead of RTL,
- implicit width mismatch problems,
- introducing logic not grounded in the structured context,
- using the wrong datapath structure such as timestamp tracking instead of shift-register or cooldown-counter designs.

Important constraint on LLM generation (very important):

- Final RTL is bounded by IR + invariants + explicit constraints.
- The LLM is not free-form; it operates as a constrained generator guided by structured context.
- width_safety.py enforces correctness for critical modules after generation.

Current limitation that must be stated clearly:

- the final checked-in `tFAW` and `tRRD` RTL is post-processed by `width_safety.py`.
- That means the LLM path is real and important, but for those specific modules the emitted final artifact is additionally constrained by a deterministic replacement pass.

How this differs from `rtlGen.py`:

- `rtlGen.py` is a single-model legacy loop with optional disabled syntax checking.
- `dual_llm_rtlGen.py` is structured, typed, review-driven, and more constrained by repository metadata.

## 10. Wrapper Generation and System Integration

`generate_wrapper.py` is where the project crosses from "generated control blocks" into "functioning system."

What the wrapper does:

- instantiates the scheduler,
- instantiates one `ddr4_bank_top` per configured bank,
- optionally instantiates refresh, tFAW, and tRRD modules,
- routes a single transaction stream to one selected bank,
- mirrors only the selected bank's readiness to `cmd_ready`,
- keeps global activation gating at the controller level,
- stores write data in a controller-level banked memory array,
- returns read data and response pulses.

Why logic lives in the wrapper instead of inside generated modules:

- bank FSMs are reused unchanged per bank,
- global constraints like `tFAW` and `tRRD` span banks,
- memory storage is a top-level integration concern, not a bank-control-FSM concern,
- the single incoming transaction stream and bank selection are system policy rather than bank-local timing behavior,
- scheduler arbitration is global and intentionally separate from bank internals.

Scheduler integration:

- scheduler decides between refresh and transaction issue,
- bank selection is not done by scheduler,
- wrapper passes `txn_valid` to scheduler and then distributes `issue_txn` to the selected bank only.

Timing gating:

- `act_allowed = tFAW_ok & ~tRRD_block` when both modules exist,
- bank command valid is `issue_txn & act_allowed` plus bank-selection routing,
- `act_pulse` is the OR of all bank activation pulses.

Top-level IO in current wrapper:

- `clk`, `rst_n`
- `txn_valid`
- `txn_is_write`
- `txn_addr[3:0]`
- `txn_wdata[31:0]`
- `txn_bank[1:0]` for 4-bank mode
- `cmd_ready`
- `rsp_valid`
- `rsp_rdata[31:0]`

Multi-bank scaling:

- bank count is used to size the bank vector signals and `txn_bank`,
- one `ddr4_bank_top` instance is emitted per bank,
- one bank-local `cmd_type` signal is emitted per bank,
- one banked storage slice exists per bank.

Read/write mapping:

- `txn_is_write = 0` maps to `txn_cmd_type = 2'b01`
- `txn_is_write = 1` maps to `txn_cmd_type = 2'b10`
- only the selected bank sees a nonzero `cmd_type`
- non-selected banks see `2'b00`

## 11. Bank Architecture

The bank path has two layers:

- bank-local control submodules generated from templates,
- `ddr4_bank_top` as a reusable integration shell.

`ddr4_bank_top` contains:

- `ddr4_bank_bank_sequencer`
- `ddr4_bank_activate_fsm`
- `ddr4_bank_tRAS_fsm`
- `ddr4_bank_precharge_fsm`

What `ddr4_bank_top` exports:

- `cmd_valid`
- `cmd_type`
- `bank_idle`
- `bank_active`
- `cmd_ready`
- `activating`

Why `bank_top` exists:

- it packages the internal FSM chain behind a stable per-bank interface,
- wrapper logic can scale banks by replication without re-wiring each timing FSM separately,
- it makes the multi-bank top-level structurally cleaner.

Bank sequencer behavior:

- `IDLE` -> `ACTIVATING` when `cmd_valid && cmd_ready`
- `ACTIVATING` -> `ACTIVE` when `tRCD_done`
- `ACTIVE` -> `PRECHARGING` when `tRAS_done`
- `PRECHARGING` -> `IDLE` when `tRP_done`

Important simplification:

- `cmd_type` is carried into the bank but does not change bank-sequencer state transitions.
- The bank flow is still a generic activate/open/precharge cycle, with read/write meaning preserved mainly for system/data-path behavior and observability.

Scaling from 1 to 2 to 4 banks:

- single-bank mode directly connects the one bank path,
- 2-bank and 4-bank modes replicate `ddr4_bank_top` and add `txn_bank` routing,
- bank control logic is otherwise the same per instance.

## 12. Scheduler Architecture

There are two scheduler architectures in the repo:

- `simple`
- `round_robin`

Current checked-in generated demo uses `simple`.

Simple scheduler:

- 3 states: `IDLE`, `ISSUE_REF`, `ISSUE_TXN`
- refresh has priority by transition ordering
- issues are one-cycle Moore outputs
- `cmd_ready` gates entry into either issue state

Round-robin scheduler:

- exists in templates,
- uses more states to encode preference and post-issue turn flipping,
- fairness applies only under contention,
- turn flips only after successful contested issue.

Why the scheduler is intentionally "dumb":

- it arbitrates refresh vs transaction only,
- it does not queue,
- it does not reorder,
- it does not choose which bank should be serviced next,
- it does not inspect timing per bank beyond the global `cmd_ready`/issue gating interaction.

Current restriction with multi-bank:

- the repo explicitly rejects multi-bank generation with `round_robin`,
- current multi-bank demo requires `simple`.

## 13. Global Timing Architecture

The current top-level timing policy treats `tFAW` and `tRRD` as shared controller-global gates.

`tRRD`:

- implemented as a cooldown counter,
- reloaded to `TRRD_CYCLES - 1` on `act_pulse`,
- blocks while counter is nonzero,
- currently `TRRD_CYCLES = 6` for DDR4-3200.

`tFAW`:

- implemented as a shift-register sliding window,
- window length `TFAW_CYCLES = 40`,
- block when the next window would contain 4 or more ACTs,
- exports `tFAW_ok`, `tFAW_block`, and `act_count`.

Why they are global/shared:

- wrapper computes `act_pulse` as OR of all bank activation pulses,
- both modules are instantiated once at top level,
- they gate transaction issue before bank routing completes.

How they gate issue behavior:

- `act_allowed = tFAW_ok & ~tRRD_block`
- bank command valid requires both scheduler transaction issue and `act_allowed`

Interaction with banks:

- banks are locally independent for `cmd_ready` and timing FSM sequencing,
- but activation issue is globally throttled,
- this lets cross-bank timing effects show up without implementing a fuller scheduler/reorder layer.

## 14. Refresh Architecture

Refresh in the current design is simplified.

Actual current behavior:

- `ddr4_refresh_refresh_controller` is a 2-state FSM with a counter.
- It counts in `IDLE`.
- When `counter >= tREFI_cycles`, it raises `ref_req` by entering `REQUEST`.
- It waits for `ref_ack`.
- Wrapper ties `ref_ack = issue_ref`.

What that means architecturally:

- refresh is modeled as a periodic request source,
- scheduler can choose to issue refresh,
- issuing refresh immediately acknowledges the request,
- there is no detailed per-bank refresh sequence,
- there is no explicit data-array refresh side effect in the memory model.

This is enough for demoing refresh request generation and arbitration, but not for modeling a real DDR refresh protocol.

## 15. Read and Write Support

The current repo preserves read/write intent explicitly through the control path.

Key signals:

- `txn_is_write`
- `txn_cmd_type`
- `bankN_cmd_type`

Mapping:

- read -> `2'b01`
- write -> `2'b10`

What changed relative to earlier generic-access versions:

- transactions are no longer just undifferentiated accesses,
- the wrapper and testbench both verify that read vs write intent survives routing,
- blocked transactions still preserve intent in `txn_cmd_type`, even if no bank actually observes a nonzero `cmd_type` because global gating prevented issue.

The bank FSMs still do not implement different control sequences for reads and writes. The current repo's read/write support is primarily:

- semantic preservation,
- routing visibility,
- interaction with the top-level memory model,
- response generation for reads.

## 16. Minimal Memory-System v2

This is the most important "controller-only to memory-system" evolution in the current demo.

Where storage lives:

- `ddr4_controller_top` declares:

```text
logic [DATA_WIDTH-1:0] bank_mem [0:BANK_COUNT-1][0:MEM_DEPTH-1];
```

So storage is top-level, banked, and address-indexed.

How writes store data:

- on `posedge clk`, if `bank_cmd_valid[bank_index] && txn_is_write`,
- wrapper writes `txn_wdata` into `bank_mem[bank_index][txn_addr]`.

How reads return data:

- `selected_bank_rdata` is combinationally read from `bank_mem[selected_bank][txn_addr]`,
- `accepted_read = selected_bank_cmd_valid & ~txn_is_write`,
- accepted read captures `selected_bank_rdata` into `read_rsp_data_q`,
- `rsp_valid_q` is driven from `read_rsp_pending_q`,
- `rsp_rdata_q` is driven from the stored captured data.

Current response timing:

- one-cycle fixed latency after accepted read,
- `rsp_valid` pulses for one cycle,
- write produces no response payload.

Why the storage lives in the wrapper:

- the bank FSM chain does not carry address/data state,
- the same bank control shell can be reused independently of memory modeling,
- routing, bank select, and selected-bank response muxing are wrapper concerns.

How bank isolation works:

- each bank has its own memory slice,
- identical addresses in different banks map to different storage locations,
- current testbench explicitly writes different values to the same address in four banks and reads them back separately.

## 17. Testbench Generation and Verification

`generate_testbench.py` emits a deterministic, architecture-aware, self-checking testbench.

Generated testbench structure:

- common tasks for waiting, scheduler checks, bank selection checks, and memory operations,
- immediate assertions and runtime checks,
- structured test phases,
- final PASS/FAIL block,
- waveform dumping.

Why it is directed/deterministic:

- it does not randomize traffic,
- it uses specific sequences chosen to demonstrate routing, gating, read/write semantics, and refresh observation,
- it is generated based on current design shape and enabled features.

Assertions and checks used today:

- handshake violation check for `txn_valid && !cmd_ready`,
- scheduler mutual-exclusion check,
- tRRD immediate violation check,
- tFAW immediate violation check if that condition ever occurs,
- bank routing checks,
- `cmd_type` preservation checks,
- memory write/read correctness checks,
- refresh observation check,
- final coverage-style checks.

Coverage-style flags in the current checked-in 4-bank testbench:

- `saw_backpressure`
- `saw_tRRD_block`

Important nuance:

- current generated testbench does not require a `tFAW` limit event in this 4-bank DDR4-3200 configuration because the generated reachability logic considers it not practically reachable given the much larger bank timing delays.

How pass/fail is determined:

- checks increment `error_count`,
- the test prints `TEST PASS` when `error_count == 0`,
- otherwise `TEST FAIL`,
- there is also a global timeout guard.

What the current generated testbench validates well:

- scheduler simple-mode behavior,
- bank routing for 4 banks,
- selected-bank `cmd_ready` behavior,
- cross-bank shared `tRRD` blocking,
- command-type preservation,
- memory write/read correctness and bank isolation,
- refresh request eventually appearing,
- backpressure being observable.

## 18. Corner Cases

### Backpressure

Validated by waiting for `cmd_ready == 0` and setting `saw_backpressure`.

Meaning:

- the design can stall transaction acceptance while a selected bank is busy,
- the testbench observes that explicitly.

### Selected-bank routing

Validated directly:

- only the selected bank sees `bank_cmd_valid`,
- non-selected banks observe zero command activity,
- `cmd_ready` mirrors the selected bank's readiness.

### Cross-bank `tRRD` blocking

This is one of the most important multi-bank behaviors currently demonstrated.

- one bank activation creates a shared `tRRD_block`,
- another bank may still be locally ready,
- but the shared gate prevents issuance,
- the testbench explicitly checks this scenario on bank 3 after priming the gate from bank 0.

### Bank isolation

Validated through the memory-system phase:

- same address `3` in banks 0..3 stores four different values,
- subsequent reads return the bank-specific value.

### Read-after-write

Validated in a simple form:

- writes are issued and later reads from the same bank/address return the stored value.

What is not claimed:

- same-cycle or tightly back-to-back hazard modeling,
- realistic DDR burst semantics,
- queue-based RAW conflict resolution.

### Blocked transaction semantics

The testbench checks that:

- blocked transactions do not assert bank command valid,
- `txn_cmd_type` still reflects requested type,
- bank-local `cmd_type` remains zero when issue is blocked.

### Refresh observation

Validated by waiting for `ref_req` within a bounded observation window derived from `tREFI`.

This proves refresh request generation exists, not full refresh service realism.

## 19. Simulation and Waveform Flow

`run_sim.sh` is the practical demo entry point after generation.

What it does:

- gathers all generated RTL files under `rtl_output`,
- compiles them with Verilator,
- compiles the generated top-level testbench,
- enables tracing,
- runs the testbench binary,
- reports whether a VCD was generated,
- optionally opens GTKWave.

Preset selection logic:

- detect scheduler mode from expanded/generated/user config,
- detect bank count from config or wrapper comment,
- choose:
  - `memsys_demo_4bank_simple.gtkw` for 4-bank simple mode,
  - `simple_controller_demo.gtkw` for simpler/single-bank simple flows,
  - `round_robin_controller_demo.gtkw` for round-robin flows.

Why presets matter:

- the VCD contains many internal signals,
- the curated presets focus the demo on the story being told,
- different architecture variants expose different useful signal groups.

Current visibility strategy in the 4-bank memory-system preset:

- external transaction/response,
- scheduler and issue control,
- shared timing gates,
- bank routing summary,
- banked memory model summary variables,
- per-bank summaries.

## 20. Validation Plan

Validation in this repo should be described in three layers, because not every "implemented" behavior is asserted in the same way.


Note on verification scope:

- No formal verification is performed.
- No randomized stress testing is performed.
- Coverage is based on directed tests and observable behaviors, not exhaustive proof.

### Flow validation

Method:

- repository generation flow runs end-to-end,
- configs expand cleanly,
- IR and RTL artifacts are produced,
- wrapper and testbench generate successfully.

Evidence in current repo:

- checked-in generated artifacts exist,
- `run_flow.py` contains the authoritative orchestration,
- wrapper generation includes interface validation before assembly.

### Testbench validation

Method:

- generated directed self-checking testbench,
- immediate runtime checks,
- explicit PASS/FAIL outcome.

Current checked-in status:

- `run_sim.sh` was rerun against the current checked-in generated design,
- Verilator compile completed,
- simulation reached `TEST PASS`.

What "testbench validated" means here:

- behavior was explicitly checked by generated assertions/tasks and contributed to pass/fail outcome.

### Waveform/manual validation

Method:

- VCD inspection through curated GTKWave presets,
- engineer confirms control and timing narrative visually.

This is important because some implemented behaviors are better observed than asserted. Examples:

- detailed per-bank state evolution,
- refresh interaction timing,
- tFAW signal behavior in configurations where the limit is not reached by directed stimulus.

Important clarification:

- a feature can be implemented and visible in waveforms without being fully asserted in the current generated testbench.
- In the current 4-bank DDR4-3200 demo, `tFAW` is instantiated and part of the architecture, but the checked-in testbench does not require reaching the blocking threshold.

## 21. Completed Work

The current repo, as represented by the checked-in artifacts and source, has completed the following major capabilities:

- fail-fast user config validation in `run_flow.py`
- stable summary printing of validated config
- generation of per-feature internal inputs from one user config
- JEDEC timing/profile expansion for DDR4-2400 and DDR4-3200
- deterministic FSM generation for core control modules
- structured dual-LLM path for datapath blocks
- width-safe normalization for tFAW and tRRD generated outputs
- wrapper interface validation before top-level assembly
- reusable `ddr4_bank_top` integration block
- 1-bank, 2-bank, and 4-bank wrapper scaling logic
- explicit multi-bank external bank selection
- simple scheduler support
- round-robin scheduler template/support path for single-bank style flows
- global shared tRRD and tFAW gating architecture
- refresh request generation and scheduler integration
- typed read/write transaction semantics via `txn_is_write` and `cmd_type`
- minimal memory-system v2 with banked storage, writes, reads, `rsp_valid`, and `rsp_rdata`
- directed self-checking generated testbench
- GTKWave preset selection aligned to architecture mode
- runnable Verilator plus VCD demo flow

## 22. Limitations

The current design is intentionally simplified. Important limitations:

- no AXI, APB, TileLink, or other standard host interface
- no realistic DDR command bus encoding
- no burst transfers
- no row/column address decomposition beyond a tiny demo address
- no realistic DDR data-path timing pipeline
- no DQS/DQ/PHY modeling
- no bank scheduler with queues, reordering, or auto-bank selection
- no per-bank refresh execution model
- no realistic write response protocol
- no ECC, initialization/training, mode-register programming, or calibration
- read/write control semantics are preserved, but the bank FSM sequence is not direction-specific
- `tFAW` and `tRRD` are modeled as global/shared gating blocks, not as a full controller timing closure framework
- multi-bank mode currently requires `simple` scheduler
- LLM generation still depends on external API/network access when regenerating datapath modules
- some repo files and comments are legacy or stale relative to the current flow
- some generated `master.yaml` files contain old absolute paths from another machine

## 23. Future Work

Realistic next steps after the current demo:

- support multi-bank round-robin or richer scheduler policies
- add bank request queues and arbitration independent of external `txn_bank`
- differentiate read and write sequencing more explicitly in bank control
- add more realistic response timing and possibly write acknowledgements
- deepen refresh behavior from "request source" into a more explicit maintenance flow
- expand the memory model beyond a small wrapper-local banked array
- make `tFAW` coverage reachable in at least one default stress configuration
- reduce reliance on hard replacement in `width_safety.py` by making the LLM path consistently emit acceptable width-safe RTL
- remove or isolate legacy paths such as `rtlGen.py` if no longer needed
- clean stale path/comment drift in generated manifests and older docs
- add formal or property-based checks for core invariants

## 24. Demo Guide

Practical current demo flow:

1. Generate or reuse the current generated design.
2. Run `./run_sim.sh`.
3. If desired, open GTKWave and use the 4-bank simple preset.

Main demo scenario in the current checked-in build:

- 4-bank DDR4-3200
- simple scheduler
- refresh enabled
- tFAW enabled
- tRRD enabled
- memory-system behavior enabled through wrapper-local banked storage and read responses

What to show in waveform:

- external transaction stream: `txn_valid`, `txn_is_write`, `txn_bank`, `txn_addr`, `txn_wdata`
- scheduler decisions: `issue_ref`, `issue_txn`
- shared gates: `tRRD_block`, `tFAW_ok`, `act_pulse`
- bank routing: `bank_cmd_valid`, `bank_cmd_ready`, `bankN_cmd_type`
- bank state summaries: each `u_bankN.u_bank_sequencer.current_state`
- memory-response path: `accepted_read`, `read_rsp_pending_q`, `rsp_valid`, `rsp_rdata`

Main story of the system:

- a single external transaction stream is routed to one selected bank,
- each bank has reusable generated timing/control logic,
- global DDR-inspired timing gates can block activation across banks,
- read/write intent is preserved across the control path,
- accepted writes update bank-local storage,
- accepted reads produce a one-cycle-later response,
- the result is no longer just a controller skeleton; it is a runnable minimal memory system built from generated components.

## Closing Notes

Authoritative baseline for this document:

- current checked-in generated top-level is `rtl_output/ddr4_controller_top.sv`
- current checked-in demo bank wrapper is `rtl_output/ddr4_bank_top.sv`
- current checked-in self-checking testbench is `tb/tb_ddr4_controller_top.sv`
- current checked-in user demo config is `configs/user_input.yaml`

Repository honesty notes:

- `rtlGen.py` is present but not the main non-FSM generation path.
- `master.yaml` files are still emitted, but current wrapper generation validates interfaces from module metadata directly instead of consuming `master.yaml`.
- Some comments and emitted paths reflect earlier repository states and should not be treated as the current operational truth when they conflict with the generated RTL and active scripts.
