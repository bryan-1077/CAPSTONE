# Workers Flow

This folder contains the stage orchestrator and command builders used by the EDA flow.

## Real Loop

If RTL prep exhausts its normal review/edit iterations, failure triage diagnoses
the remaining errors. A backend-owned diagnosis is handed back to the prep AI
reviewer and editor with its evidence and suggested next steps. They receive up
to `prep_max_iterations` additional repair attempts on the existing prepared
bundle, without regenerating it. Applied edits must pass a fresh review before
the flow advances to synthesis. If recovery fails, the latest review and the
original diagnosis remain available in the reports. Frontend-owned and unknown
diagnoses retain the failure handoff rather than triggering backend prep edits.

### Mailbox input

From the repository root, use an explicitly selected local revision:

```bash
python local/backend/app.py --mailbox shared/mailbox/<run-id>/revision_001 --target-mhz 200
```

The revision must contain `rtl/` with the complete source tree, including headers
and dependencies. Preserve the frontend's `rtl/manifest.json` to supply its
`top_module`; without it the existing configured top remains in use. Other mailbox
folders, such as `specs/` and verification reports, are not uploaded by backend.

Backend freezes a temporary local copy, records SHA-256 hashes, and packages the
files into one archive automatically. It streams the archive through its existing
SSH connection to a Slurm compute receiver. The receiver creates a unique
`mailbox_inputs/` directory under the remote project root, extracts the files with
exclusive creation, and verifies their sizes and SHA-256 hashes, including a
read-back check. Directory creation and scratch reads/writes happen on compute;
the login node relays the bytes. Prep receives that directory using `--input` and
the selected top using `--top` only after verification succeeds. Upload failure
stops before the flow starts. Input paths, hashes, and the compute transfer receipt
(`mailbox_transfer`, including host, job ID, bytes, and elapsed time) are retained
in backend state. Frequency sweeps reuse the same uploaded revision. Without
`--mailbox`, standalone invocation retains the legacy `MemoryController.zip` input.

Mailbox staging retries transient SSH/streaming transport failures up to three
total attempts, waiting 2 seconds and then 4 seconds between attempts. Each attempt
uses a fresh SSH connection and unique remote directory, reuploading the entire
frozen local snapshot with exclusive file creation. Authentication, permission,
input errors, failed remote checks, and interrupts stop immediately. Archive byte
count, destination, execution host/job, 300-second command deadline, elapsed time,
and failures are printed to the backend/session logs. Failed staging directories are left in place
and their paths are logged; they are never reused by retries.

This option transports an existing revision; it does not create the frontend
mailbox or prove that validation passed. Automatic publication, remote validation,
and the top-level gate for the exact validated revision still need integration.
Directory input and `--top` follow the documented remote prep interface; they must
also be available in the deployed `prepare_rtl_for_genus_universal.py`.

Mailbox receiving, prep generation, all four deterministic stage reviews, and
application of prep/netlist/mapped/GDSII edit actions run in Slurm compute steps.
They reuse the shared allocation when present and request a standalone compute
step otherwise. Output cleanup, project-directory changes, reviews, and edits
happen inside those steps. `[PREP]` identifies the generation host; `[BACKEND]`
review/edit host messages are recorded on stderr so stdout remains JSON. The
existing 300-second generation/review/edit deadlines include step startup and any
queue wait. AI requests and edit planning remain local; their bounded actions are
applied on compute. Other helpers, timing-closure copies, build-name reservation,
and report downloads retain their existing routing.

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
