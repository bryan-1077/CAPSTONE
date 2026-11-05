# CAPSTONE desktop GUI

The application provides saved-run history and approved-YAML execution. All GUI
implementation and tests live in this directory. Start Run launches the existing
Standard flow, including remote work when selected.

From the repository root:

```bash
./shared/gui/launch.sh
./shared/gui/launch.sh --runs /path/to/history
```

Requires PySide6 in the selected Python environment. The current development
machine has PySide6 6.11.1. An isolated GUI environment can be created with:

```bash
python3 -m venv shared/gui/.venv
shared/gui/.venv/bin/python -m pip install -r shared/gui/requirements.txt
shared/gui/.venv/bin/python -m shared.gui
```

The default history folder is `shared/top_orchestrator/runs`. Use History Folder
to choose another collection or Open Run to select an individual directory
containing `state.json`. Refresh reloads saved evidence manually.

Overview shows recorded outcomes, stage messages, skipped-stage configuration,
and artifact values. Stages preserves each recorded execution attempt.
Configuration shows the saved configuration. Logs & Artifacts previews the final
256 KiB of text files and opens selected files or report directories with the
system's associated application. Missing external paths remain visible in the
overview. Old absolute paths inside a moved run are rebased when the run folder
name is preserved; external mailbox/tool paths are not rewritten.

A saved `running` status does not prove a worker is alive. The history viewer does not
resume interrupted executions. The active-run panel reconnects only to a living worker. Layout viewing is deferred.

Offline checks, including Qt window startup:

```bash
python3 -m unittest shared.gui.test_viewer
```

See [PLAN.md](PLAN.md) for phases and [ARCHITECTURE.md](ARCHITECTURE.md) for the
future execution interface.

## Approved-YAML execution

Use Setup / Active Run to select approved YAML or enter a design description, a target controller frequency,
and an optional remote configuration. Stage controls map to existing orchestrator
flags. The worker retains existing acceptance gates, snapshot publication, bounded
repair, backend, LEC, and GDSII routing. No live end-to-end remote run has been
performed to validate this GUI milestone.

Remote credentials may be supplied upfront or requested through an inline masked
field. Initial credentials travel through an anonymous pipe; later responses use a
private Unix-domain socket in a user-owned temporary directory. Password responses
are never journaled. Orchestrator SSH retries use this channel and retain existing
retry bounds. Backend subprocess authentication failures are not yet interactive;
those failures still stop with their existing diagnostics.

All interaction adapters are worker-local bindings around existing frontend and
orchestrator functions. No `input` or `getpass` monkeypatch is used. Shared CLI
source is unchanged. The future common interaction interface remains a refactor.

One GUI run per checkout is enforced by an inherited advisory lock. Existing CLI
launches do not acquire this lock: do not run them alongside GUI workflows. Run
state and bounded worker log output refresh every second. Runs use stable `gui_`
UUID directory names. Worker console logs are sibling `.worker.log` files.

Closing during execution offers Keep Window Open, Leave Run Executing & Close,
or Stop Run. Keep Window Open is the default. Stop Run opens a separate confirmation
and leaves the window open while cleanup executes.
Transport uses a startup pipe, a private request/response socket, and saved state/log
polling. Persistent worker discovery and the full event/replay protocol in
ARCHITECTURE.md remain planned.

Additional offline checks:

```bash
python3 -m unittest shared.gui.test_execution shared.gui.test_viewer
```

## Specification and frequency interaction

A nonempty description takes precedence over the YAML selector. Start Run calls the
existing frontend extraction and normalization functions in the worker. This needs
the frontend Python dependencies and provider configuration in the selected Python
environment. Description interpretation and optional frequency advice make model
calls when used; offline tests use fake responses.

The pending-decision panel displays the complete proposed YAML, assumptions,
preset, and revision. Approve specification accepts that exact candidate digest.
To revise, enter a complete revised description and choose Revise. Clarification
and rejection bounds follow the existing frontend constants. Approved candidates
and their approval record are saved in the run directory before RTL generation.
Declining pending input stops the flow; this is not a general remote Stop action.

Enable frequency review to confirm a target or ask the advisor a question. Advice
never selects a frequency. Enter MHz and Continue to select explicitly; approved
clock constraints remain visible. Discussion is saved in `gui_frequency_review.json`.
The orchestrator retains its numeric validation, constraint warnings, and frequency
selection record.

## Worker lifecycle

Reopening the application discovers the current worker using its PID, Linux process
start identity, and boot ID. The socket handshake must also match the worker UUID
before the GUI displays pending requests or enables Stop Run. Disconnection does
not imply termination. Reconnection preserves pending decisions; responses and
credentials are not written to the discovery registry.

GUI remote runs now use the existing shared Slurm allocation path (default six
CPUs, four execution hours, four queue hours). This is required for cancellation
ownership, including backend execution. Allocation credentials and settings come
from the selected remote configuration. The backend inherits the same allocation.
CLI runs remain unchanged and must not run concurrently with GUI workflows.

Stop Run interrupts orchestration, prevents additional stage launches, attempts
existing adapter/allocation cleanup, and terminates processes in the worker's
owned Linux session with bounded TERM/KILL waits. Tools that deliberately detach
into another session are outside this local cleanup boundary. Remote release is
followed by an independent `squeue` query using the unique allocation name.
Failures or remaining queue entries are recorded as `cleanup_unconfirmed`; inspect
the allocation receipt and job identifiers before assuming remote work ended.

Worker outcomes and cleanup receipts are stored beside run directories in
`<run-name>.lifecycle.json`; original stage results are retained. A dead worker
without a final receipt is marked interrupted on discovery. There is no automatic
restart or checkpoint resume. Forced termination or machine failure may leave
remote jobs requiring manual cleanup.

Offline lifecycle tests use a fake worker and local sleeping child, with no model
calls or remote work:

```bash
python3 -m unittest shared.gui.test_lifecycle shared.gui.test_interaction shared.gui.test_execution shared.gui.test_viewer
```

Actual remote cancellation and desktop-session logout behavior remain unverified.


## Fresh Linux checkout

1. Clone the repository and enter its root.
2. Create `shared/gui/.venv` using a Python version supported by the pinned dependencies.
3. Install `shared/gui/requirements.txt` for the viewer. For execution, install
   `shared/gui/requirements-execution.txt` and complete the existing frontend/backend setup.
4. Run `./shared/gui/launch.sh --doctor` to inspect local packages and tools.
5. Run `./shared/gui/launch.sh --smoke-test` to check Qt Widgets without a visible window.
6. Launch `./shared/gui/launch.sh` and open an existing run before starting execution.
7. For remote work, copy `shared/top_orchestrator/remote.example.json` to
   `shared/gui/remote.local.json`, configure your account, and select that file in Setup.
   A blank selector uses the existing `CAPSTONE_*` environment configuration; it
   does not automatically load a local JSON file.
8. Configure frontend provider access using its existing local setup. Configure
   server-side Slurm, Python, EDA tools, and a readable SKY130 PDK for your SSH account.
9. Follow [VALIDATION.md](VALIDATION.md) for live and second-machine checks.

The launcher works from any directory and chooses `CAPSTONE_GUI_PYTHON`, then
`shared/gui/.venv/bin/python`, then `python3` from PATH. Explicit interpreter paths
must be absolute when launching from another working directory.

Direct dependency versions are pinned to this development environment. A clean
installation and complete transitive dependency lock are not yet verified.
`requirements.txt` is sufficient for viewing; it does not install EDA tools.
`iverilog` is reported for information; the current BIST shell flow uses Verilator.
No diagnostic command connects to a server or prints credential values.

Machine-specific GUI files (`.runtime/`, `.venv/`, `settings.local.json`, and
`remote.local.json`) are ignored by Git. There is no password persistence feature.

## Compact run status

The active-run panel shows RTL & checks, verification, backend / LEC, and GDSII
validation above the logs. Labels include symbols and status text as well as
colors. The activity bar animates only for a connected executing worker or cleanup;
it pauses for user input and disconnection. Elapsed time is shown without an
estimated completion percentage. RTL/checks and backend/LEC remain grouped until
finer structured events are available. The application uses Qt Fusion and its own
palette and status colors, independent of the KDE widget theme.

## Simplified main view

History is hidden on startup. Show history reveals previous runs and their report
and configuration tabs; Hide history returns to the design screen. The progress
panel remains at the top, outside the scrolling settings area, with a larger
activity bar. Its steps are Create design, Check design, Build layout, and Check
layout. Advanced settings and technical logs are collapsed by default and can be
expanded independently. Execution options and acceptance decisions are unchanged.

The hammer-and-anvil animation represents worker liveness: it continues throughout
input waits, temporary disconnection, and cancellation cleanup, stopping only when
the worker exits. The progress bar separately reflects active versus waiting work.
