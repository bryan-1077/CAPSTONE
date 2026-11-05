# Desktop GUI plan

## Agreed direction

- [x] Build a local Python desktop GUI using PySide6.
- [x] Keep GUI source and setup instructions in GitHub alongside the orchestrator.
- [x] Target Linux first; assess other operating systems separately.
- [x] Keep `shared/top_orchestrator` responsible for execution, routing, repair, and acceptance.
- [x] Execute the orchestrator in a separate worker process so the window stays responsive.
- [x] Support one active workflow at a time while frontend output directories are shared.
- [x] Provide Setup, Active Run, and History views.
- [x] Defer layout screenshots and GDSII viewing until core execution works.

## Phase 1: Inspect and define interfaces

- [x] Map Standard-flow prompts, subprocesses, SSH authentication, remote jobs, and artifacts.
- [x] Define structured messages for stage transitions, activity, input requests, errors, and completion.
- [x] Define request/response interfaces for specification approval and frequency selection.
- [x] Define credential handling that excludes passwords from logs, saved state, and GitHub.
- [x] Preserve CLI behavior through the same execution interfaces.
- [x] Define worker ownership, persistent identity, reconnection, and stale-process detection.
- [x] Define cancellation behavior for local subprocesses and remote jobs.
- [x] Check PySide6 dependency, licensing, and packaging requirements before implementation.

Design and source map: [ARCHITECTURE.md](ARCHITECTURE.md). Interfaces are defined; implementation follows in later phases.

## Phase 2: Read-only run viewer

- [x] Create the application entry point and main window under `shared/gui/`.
- [x] Open existing run directories and discover run history.
- [x] Read saved configuration, stage results, logs, and artifact paths.
- [x] Display passed, failed, running, skipped, waiting, and needs-attention states.
- [x] Distinguish local verification results, pending system coverage, and backend readiness.
- [x] Show incomplete or interrupted runs without implying they can resume.
- [x] Provide report links and an Open Run Folder action.

## Phase 3: Execute from approved YAML

- [x] Add YAML file selection, target frequency, and remote configuration selection.
- [x] Expose supported stage controls and validate their combinations.
- [x] Launch the Standard flow through a separate worker process.
- [x] Enforce the single-active-run restriction across GUI instances.
- [x] Stream activity and logs while preserving existing log files.
- [x] Display current stage, elapsed time, repair attempts, and completion results.
- [x] Show progress by stage; avoid unsupported percentage estimates.
- [ ] Run the complete configured flow through backend, LEC, and GDSII validation.
- [x] Preserve existing acceptance gates and explicit skipped-stage reporting.

Phase 3 validation: six offline tests pass. Full remote execution remains unverified.
Current worker uses a startup pipe and state/log polling; reconnect protocol is deferred.

## Phase 4: Specification interaction

- [x] Add natural-language design input.
- [x] Display proposed specifications for review and approval.
- [x] Support revisions before approval.
- [x] Collect frequency and authentication inputs through the GUI.
- [x] Show pending decisions in the Active Run view with a Continue action.
- [x] Save approved specifications with their run artifacts.

Phase 4 uses worker-local adapters. Orchestrator credential retries are interactive; backend subprocess retries and the shared CLI interaction refactor remain follow-up work. No live model/remote end-to-end run has been performed.

## Phase 5: Window closure and process management

- [x] Show a close dialog when a run is active.
- [x] Offer Keep Window Open, Leave Run Executing & Close, and Stop Run.
- [x] Make Keep Window Open the default action.
- [x] Keep the worker alive when the user chooses to close only the window.
- [x] Reconnect to an executing worker when the application reopens.
- [x] Require a separate Stop Run confirmation describing affected local and remote work.
- [x] Record cancellation results and identify remote jobs whose termination is unconfirmed.
- [x] Handle worker crashes and disconnected remote sessions explicitly.
- [x] Keep reconnecting to an active run distinct from resuming an interrupted run.

Phase 5 uses owned shared Slurm allocations for GUI remote runs. Local lifecycle behavior is tested offline; live remote cleanup and desktop-session logout still require validation.

## Phase 6: Verification and portable setup

- [x] Verify historical-run rendering with passed, failed, skipped, and incomplete evidence.
- [x] Verify worker messaging, pending input, failures, closure, reconnection, and cancellation.
- [x] Verify GUI execution preserves CLI routing and acceptance decisions.
- [ ] Perform a configured end-to-end Standard-flow run.
- [x] Define reproducible dependencies and a single launch command.
- [x] Document fresh-clone setup, local tool prerequisites, SSH configuration, and credentials.
- [x] Keep machine-specific settings and credentials outside tracked files.
- [ ] Verify setup on a second Linux computer.
- [ ] Add desktop shortcuts or application packaging after the launch workflow is stable.

Phase 6 evidence: see [VALIDATION.md](VALIDATION.md). Offline routing uses fake stage adapters; live acceptance remains unverified. Direct dependencies are pinned; clean-install and transitive-lock verification remain pending.

## Later phase: Layout images and GDSII

- [ ] Coordinate an artifact manifest with the backend screenshot work.
- [ ] Record schema version, stage, attempt, relative image path, caption, and capture timestamp.
- [ ] Transfer remote images into the owning run directory before publishing manifest entries.
- [ ] Preserve images from earlier attempts and associate them with the exact run.
- [ ] Add a Layout tab for floorplan, placement, routing, and final GDSII previews.
- [ ] Add zoom, pan, fit-to-window, and Open Image actions.
- [ ] Show waiting or unavailable captures without blocking run monitoring.
- [ ] Keep image availability separate from timing, physical-check, and validation status.
- [ ] Add a rendered GDSII preview and configurable Open GDSII in Viewer action.
- [ ] Consider side-by-side comparisons, congestion maps, clock trees, and timing-path images.
- [ ] Assess interactive GDSII geometry and layer controls as a separate enhancement.

## First milestone acceptance

- [ ] Launch the desktop application from a documented command.
- [ ] Inspect an existing run and open its reports and artifacts.
- [ ] Start a full Standard flow from approved YAML and monitor live progress.
- [ ] Display final outcomes, skipped stages, failures, and verification gaps accurately.
