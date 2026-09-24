# Workers Flow

This folder contains the stage orchestrator and command builders used by the EDA flow.

## Real Loop

At a high level, the flow is:

1. `app.py` or one of the stage-specific runner scripts loads service exports and calls `run_flow(...)`.
2. The orchestrator runs stages in order:
   - `rtl_prep`
   - `netlist`
   - `mapped_netlist`
   - `gdsii`
3. Each major reviewed stage has an inner generate/review/edit loop controlled by the stage-specific `*_max_iterations` state fields:
   - generate
   - review
   - edit
   - re-review
4. If a stage passes, the flow advances.
5. If a reviewer reroutes ownership to an earlier stage, the orchestrator jumps back to that stage.
6. The four main stages currently get a single top-level orchestrator attempt each; retries inside a stage happen through the per-stage iteration loops instead.
7. If a stage still fails after its allowed stage iterations or a routing loop exceeds its limit, the flow stops.

## Where Agents Fit

The optional OpenAI-backed agents live in `agents/`. They are not separate stages.

They plug into the `review` and `edit` steps inside a stage when the configured AI backend is available:

1. The local deterministic reviewer runs first.
2. The optional agent reviewer runs on the same stage result.
3. The flow merges the local review and any AI review output that was successfully parsed.
4. If the merged review fails, the deterministic editor and optional agent editor each propose bounded actions.
5. The flow merges those edit actions and applies them.
6. The reviewer runs again on the updated result.

So the practical loop is:

- generate
- local review
- optional agent review
- merge review result
- if needed: deterministic edit + optional agent edit
- apply merged edits
- re-review

## Stage Ownership

- Prep gets three review/edit attempts by default. Each applied edit is re-reviewed; passing review ends prep successfully without triage. A no-change edit still consumes an attempt and does not stop the remaining attempts. If all attempts fail, RTL triage runs once using the final review evidence, then prep is marked failed. Review-command and JSON-output failures follow the same budget. A generation failure is terminal and triggers triage immediately because there is no generated bundle to review/edit. Results are stored in `rtl_failure_triage_payload`, validation evidence, and `rtl_prep_rtl_failure_triage_*` logs; the terminal reports triage start and completion.
- `rtl_prep` owns bundle construction, filelist/Tcl/SDC consistency, stray files, and prep-contract completeness.
- `netlist` owns actual Genus bring-up, log classification, elaboration/synthesis failures, and stage routing after Genus runs.
- `mapped_netlist` owns mapped-stage bring-up, mapped log classification, mapped synthesis/export failures, and stage routing after mapped runs.
- `gdsii` owns export-stage bring-up, GDSII/log classification, physical-design export failures, and stage routing after GDSII runs.
- If netlist review decides the root cause is really a prep issue, the orchestrator routes feedback back to `rtl_prep`.
- If mapped review decides the root cause is really a broken generic handoff, the orchestrator routes feedback back to `netlist`.
- If GDSII review decides the root cause is really a broken mapped handoff, the orchestrator routes feedback back to `mapped_netlist`.

## Timing Closure Options

Timing-closure knobs are opt-in state fields passed to the GDSII/Innovus runner when set:

- `timing_target_clock_period_ns`: explicit create_clock period, for example `4.762` for 210 MHz.
- `timing_overconstraint_clock_period_ns` plus `timing_use_overconstraint`: use an overconstrained period such as `4.650`.
- `innovus_utilization`: forwarded as `--utilization`.
- `innovus_ccopt_target_skew`: forwarded as `--ccopt-target-skew`.
- `innovus_ccopt_target_max_transition`: forwarded as `--ccopt-target-max-transition`.
- `innovus_final_postroute_setup_opt`: enables final `optDesign -postRoute -setup` before final timing/export.

Existing generated-flow behavior is preserved when these fields are unset.

## Orchestrator And Workers

`orchestrator.py` is the control plane for the flow. It owns stage ordering, per-stage iteration loops, pass/fail decisions, routing failures back to earlier stages, merging state updates, and deciding when review or edit should run again.

`workers.py` is the command-construction layer. It turns the current flow state into the concrete remote commands used to generate artifacts, run deterministic reviews, and apply bounded edit actions for each stage.

In practice, the orchestrator decides what should happen next, and the workers decide exactly how to invoke the remote tooling for that step.

## Main Files

- `orchestrator.py`: Runs the ordered stage flow, per-stage generate/review/edit iterations, routing back to earlier stages, and live progress emission.
- `workers.py`: Builds the remote commands used for generate, review, and edit actions, including prep review payload assembly and the stage command lines executed over SSH.

## Important Detail

The agent layer is additive, not replacing the deterministic checks.

That means:

- deterministic review always runs even without Codex/OpenAI
- prep edit has a deterministic fallback plan and can still apply bounded fixes without AI
- netlist edit now also has a deterministic fallback plan for known safe synthesis-compatibility fixes
- mapped edit now also has a deterministic fallback plan for known safe mapped-stage compatibility fixes
- gdsii edit now also has a deterministic planning path, though the current built-in fallback action library is effectively empty unless AI contributes edits
- when the AI backend is enabled and healthy, its review/edit results are merged into the same loop
- if the AI backend is unavailable, over quota, returns malformed output, or the endpoint rejects a request, the flow falls back to deterministic behavior instead of treating AI as mandatory
