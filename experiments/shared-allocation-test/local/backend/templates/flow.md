# EDA Flow

This file describes the runtime flow in plain language so an LLM or agent can follow the current code behavior reliably.

## Purpose

The flow starts in `app.py`, loads service exports, opens SSH, runs the orchestrator, completes RTL prep first, and only moves forward after each reviewed stage passes its review loop. `rtl_prep`, `netlist`, `mapped_netlist`, and `gdsii` all use multi-step reviewer/editor loops instead of simple single-pass verification.

## Canonical Flow

1. `app.py` is launched.
2. `app.py` loads `services/exports.txt` into the process environment using `os.environ.setdefault(...)`, so values already exported by the parent shell win over the file.
3. `app.py` builds the initial flow state, including:
   - remote script names
   - remote build directories
   - prep review state
   - netlist review state
   - mapped review state
   - GDSII review state
4. `app.py` creates the SSH config.
5. `app.py` calls `run_flow(...)` in `agents/orchestrator.py`.
6. `run_flow(...)` opens the SSH session through `SSHExecutor`.
7. The orchestrator runs stages in order:
   - `rtl_prep`
   - `netlist`
   - `mapped_netlist`
   - `gdsii`
8. Each stage still has outer stage retries through `_run_stage_with_retries(...)`.
9. The `rtl_prep`, `netlist`, `mapped_netlist`, and `gdsii` stages also have their own inner iteration loops for review and edit handling.

## RTL Prep Agent Flow

The `rtl_prep` stage is no longer just:

- run prepare
- check that files exist

It now behaves like this:

1. Run the remote prep generator command.
   Default script: `prepare_rtl_for_genus_universal.py`
2. Write the prepared bundle to the configured prep directory.
   Current default directory: `build_prep_mc`
3. Run a deterministic local-style review on the remote bundle.
4. Merge that review with optional AI prep review feedback when the backend is available and returns valid structured output.
5. If review passes:
   - mark `rtl_prep` successful
   - store the remote prep bundle path in outputs
6. If review fails:
   - call the prep editor service
   - build deterministic bounded prep actions first
   - merge optional AI prep edit actions when available
   - apply any safe bounded edits to the prepared bundle
   - rerun review after edits
7. If the post-edit review still fails, feed the review findings back into the next prep iteration and regenerate the prep bundle.
8. If all prep iterations fail, mark `rtl_prep` failed.

In practice, the prep stage is:

- generator
- reviewer
- editor
- optional re-review
- retry loop

## Netlist Agent Flow

The `netlist` stage is also no longer a placeholder server-access check.

It now behaves like this:

1. Use the successful prep bundle as the netlist input contract.
   Current default prep directory: `build_prep_mc`
2. Run the remote netlist command through the scheduler/bootstrap path.
   Default script: `run_genus_netlist_universal.py`
3. Launch Genus through the existing `srun` environment wrapper and resolve the binary from:
   - configured `remote_genus_bin`
   - `EDA_GENUS_BIN` or `GENUS_BIN` if already exported remotely
   - `PATH` via `command -v genus`
   If no executable is found, the stage fails before review with a specific environment error.
4. Wait for the netlist script to fully exit before any review begins.
   The runtime now emits an explicit `EDA_NETLIST_RUN_COMPLETE:<rc>` marker after the script returns.
   During long runs it also writes remote heartbeat and tee logs in the netlist output directory:
   - `agent_netlist_progress.log`
   - `agent_netlist_stdout.log`
   - `agent_netlist_stderr.log`
   If that marker is missing, the orchestrator treats the run as incomplete and skips review.
5. Write outputs to the configured agent-owned netlist directory.
   If `remote_netlist_dir` is set, that explicit value is used.
   Otherwise the default is derived from the active prep directory, for example:
   - `build_prep_mc` -> `build_netlist_mc_lang`
   - `build_prep_mc_2` -> `build_netlist_mc_2_lang`
6. Run a deterministic netlist review that checks:
   - prep artifacts still exist and are non-empty
   - `filelist_genus.f` entries resolve correctly
   - `top_module.txt` is usable
   - `run_genus_mc.tcl` references expected inputs
   - the netlist output directory exists
   - expected synthesized outputs/logs were produced
   - structured Genus diagnostics are extracted from the primary netlist log instead of treating arbitrary wrapped text or report-table rows as blocking errors
7. Merge that review with optional AI netlist review feedback when the backend is available and returns valid structured output.
8. If the netlist run and review both pass:
   - mark `netlist` successful
   - store the remote netlist directory in outputs
9. If review fails:
   - call the netlist editor service
   - build deterministic bounded netlist edit actions first
   - merge optional AI netlist edit actions when available
   - apply any safe bounded edits to the prep-side inputs used by netlist
   - rerun the netlist command
   - rerun review
10. If the stage still does not pass, feed the findings into the next netlist iteration.
11. If all netlist iterations fail, mark `netlist` failed.

In practice, the netlist stage is:

- generator
- reviewer
- editor
- rerun
- re-review
- retry loop

## Mapped Agent Flow

The `mapped_netlist` stage is also no longer a placeholder server-access check.

It now behaves like this:

1. Use the successful netlist bundle as the mapped-stage input contract.
2. Run the remote mapped command through the scheduler/bootstrap path.
   Default script: `run_genus_mapped_universal.py`
3. Launch Genus through the existing `srun` environment wrapper and resolve the binary from:
   - configured `remote_genus_bin`
   - `EDA_GENUS_BIN` or `GENUS_BIN` if already exported remotely
   - `PATH` via `command -v genus`
   If no executable is found, the stage fails before review with a specific environment error.
4. Wait for the mapped script to fully exit before any review begins.
   The runtime now emits an explicit `EDA_MAPPED_RUN_COMPLETE:<rc>` marker after the script returns.
   During long runs it also writes remote heartbeat and tee logs in the mapped output directory:
   - `agent_mapped_progress.log`
   - `agent_mapped_stdout.log`
   - `agent_mapped_stderr.log`
   If that marker is missing, the orchestrator treats the run as incomplete and skips review.
5. Write outputs to the configured agent-owned mapped directory.
   If `remote_mapped_dir` is set, that explicit value is used.
   Otherwise the default is derived from the active netlist directory and prep directory.
6. Run a deterministic mapped review that checks:
   - the netlist handoff directory exists
   - expected generic input artifacts such as `*_generic.v` or `*_generic.sdc` exist
   - the mapped output directory exists
   - expected mapped outputs/logs were produced
   - structured Genus diagnostics are extracted from the primary mapped log
7. Merge that review with optional AI mapped review feedback when the backend is available and returns valid structured output.
8. If the mapped run and review both pass:
   - mark `mapped_netlist` successful
   - store the remote mapped directory in outputs
9. If review fails:
   - if the reviewer classifies the failure as a broken netlist handoff, route back to `netlist`
   - otherwise call the mapped editor service
   - build deterministic bounded mapped edit actions first
   - merge optional AI mapped edit actions when available
   - apply any safe bounded edits to the mapped-stage input bundle
   - rerun the mapped command
   - rerun review
10. If the stage still does not pass, feed the findings into the next mapped iteration.
11. If all mapped iterations fail, mark `mapped_netlist` failed.

In practice, the mapped stage is:

- generator
- reviewer
- editor
- rerun
- re-review
- retry loop

## GDSII Agent Flow

The `gdsii` stage is also no longer a placeholder server-access check.

It now behaves like this:

1. Use the successful mapped bundle as the GDSII-stage input contract.
2. Run the remote GDSII/export command through the scheduler/bootstrap path.
   Default script: `run_innovus_GDSII_universal.py`
3. Launch the physical-design export flow and resolve the binary from:
   - `EDA_INNOVUS_BIN` if already exported remotely
   - `INNOVUS_BIN` if already exported remotely
   - `PATH` via `command -v innovus`
   If no executable is found, the stage fails before review with a specific environment error.
4. Wait for the GDSII script to fully exit before any review begins.
   The runtime now emits an explicit `EDA_GDSII_RUN_COMPLETE:<rc>` marker after the script returns.
   During long runs it also writes remote heartbeat and tee logs in the GDSII output directory:
   - `agent_gdsii_progress.log`
   - `agent_gdsii_stdout.log`
   - `agent_gdsii_stderr.log`
   If that marker is missing, the orchestrator treats the run as incomplete and skips review.
5. Write outputs to the configured agent-owned GDSII directory.
   If `remote_gdsii_dir` is set, that explicit value is used.
   Otherwise the default is derived from the active mapped directory.
6. Run a deterministic GDSII review that checks:
   - the mapped handoff directory exists
   - expected mapped input artifacts such as `*_mapped.v`, `*_mapped.sdc`, `*.db`, or `*.def` exist
   - the GDSII output directory exists
   - expected layout outputs such as `*.gds` or `*.gdsii` were produced
   - structured tool diagnostics are extracted from the primary GDSII log
7. Merge that review with optional AI GDSII review feedback when the backend is available and returns valid structured output.
8. If the GDSII run and review both pass:
   - mark `gdsii` successful
   - store the remote GDSII directory in outputs
9. If review fails:
   - if the reviewer classifies the failure as a broken mapped handoff, route back to `mapped_netlist`
   - otherwise call the GDSII editor service
   - build deterministic bounded GDSII edit actions first
   - merge optional AI GDSII edit actions when available
   - apply any safe bounded edits in the workspace
   - rerun the GDSII command
   - rerun review
10. If the stage still does not pass, feed the findings into the next GDSII iteration.
11. If all GDSII iterations fail, mark `gdsii` failed.

In practice, the GDSII stage is:

- generator
- reviewer
- editor
- rerun
- re-review
- retry loop

## Stage Logic For Agents

- Entry point: `app.py`
- SSH opens before stage execution begins.
- First required stage: `rtl_prep`
- `rtl_prep` must succeed before `netlist` can start.
- `netlist` must succeed before `mapped_netlist` can start.
- `mapped_netlist` is now a reviewed stage with the same generate/review/edit/rerun shape as prep and netlist.
- `gdsii` is now a reviewed stage with the same generate/review/edit/rerun shape as the other downstream stages.
- AI review/edit are optional helpers, not prerequisites for stage progress.
- Prep success means:
  - the prep command completed successfully
  - the prep review passed
  - the prepared bundle path was recorded
- Netlist success means:
  - the Genus/netlist command completed successfully
  - the netlist review passed
  - expected output artifacts were detected
  - the output directory was recorded
- Mapped success means:
  - the Genus/mapped command completed successfully
  - the mapped review passed
  - expected mapped output artifacts were detected
  - the output directory was recorded
- GDSII success means:
  - the export command completed successfully
  - the GDSII review passed
  - expected layout output artifacts were detected
  - the output directory was recorded
- Failures produce:
  - captured stdout
  - captured stderr
  - exact command logs
  - reviewer summaries
  - suggested fixes
- AI errors may also be recorded in logs, but they do not by themselves decide stage ownership or pass/fail.

## Decision Tree

```text
Start app.py
  -> Open SSH
  -> Start orchestrator

  -> Run rtl_prep stage
     -> Generate prep bundle
     -> Review prep bundle
     -> If review passes: accept rtl_prep
     -> If review fails:
        -> Ask prep editor for bounded edits
        -> Use deterministic prep edits even if AI is unavailable
        -> Apply edits
        -> Re-review
        -> If still failing: regenerate and retry prep iteration

  -> If rtl_prep succeeds, run netlist stage
     -> Launch remote netlist/Genus flow
     -> Wait for script completion marker
     -> Review netlist outputs and prep contract
     -> If review passes: accept netlist
     -> If review fails:
        -> Ask netlist editor for bounded edits
        -> Use deterministic netlist edits even if AI is unavailable
        -> Apply edits to prep-side inputs
        -> Rerun netlist
        -> Re-review
        -> If still failing: retry netlist iteration

  -> If netlist succeeds:
     -> Run mapped_netlist stage
     -> Launch remote mapped/Genus flow
     -> Wait for script completion marker
     -> Review mapped outputs and netlist handoff contract
     -> If review passes: accept mapped_netlist
     -> If review routes to netlist: send feedback back to netlist
     -> If mapped-owned review fails:
        -> Ask mapped editor for bounded edits
        -> Use deterministic mapped edits even if AI is unavailable
        -> Apply edits to mapped-side inputs
        -> Rerun mapped
        -> Re-review
        -> If still failing: retry mapped iteration

  -> If mapped_netlist succeeds:
     -> Run gdsii stage
     -> Launch remote export / GDSII flow
     -> Wait for script completion marker
     -> Review GDSII outputs and mapped handoff contract
     -> If review passes: accept gdsii
     -> If review routes to mapped_netlist: send feedback back to mapped
     -> If gdsii-owned review fails:
        -> Ask gdsii editor for bounded edits
        -> Use deterministic gdsii edits even if AI is unavailable
        -> Apply edits in the workspace
        -> Rerun gdsii
        -> Re-review
        -> If still failing: retry gdsii iteration

  -> If any required stage exhausts retries:
     -> Stop the flow
```

## Naming Notes

- Your requested `preprtl` step maps to the code stage named `rtl_prep`.
- The prep stage now includes a reviewer/editor loop, not just generation.
- The netlist stage now includes a reviewer/editor loop, not just a launch command.
- The mapped stage now includes a reviewer/editor loop, not just a launch command.
- The GDSII stage now includes a reviewer/editor loop, not just a launch command.
- Current defaults in `app.py` are:
  - prep script: `prepare_rtl_for_genus_universal.py`
  - prep dir: `build_prep_mc`
  - netlist script: `run_genus_netlist_universal.py`
  - netlist dir: derived from the active prep dir unless `remote_netlist_dir` is explicitly set
  - mapped script: `run_genus_mapped_universal.py`
  - mapped dir: derived from the active netlist dir unless `remote_mapped_dir` is explicitly set
  - gdsii script: `run_innovus_GDSII_universal.py`
  - gdsii dir: derived from the active mapped dir unless `remote_gdsii_dir` is explicitly set
- Current AI defaults come from `services/exports.txt` unless the parent shell already exported them first.
