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

The default history folder is `shared/top_orchestrator/runs`. The History
sidebar's options menu provides Change History Folder, Refresh, and Open Run
Folder. A run's context menu also opens its folder.

The workspace has three permanent tabs, all describing the selected run:

- **Run** contains design setup or the four stage nodes and selected stage details.
  RTL has module activity, verification has expandable agents, and physical design
  has expandable preparation, synthesis, mapping, equivalence, and implementation
  steps. Stage Notes preserves attempts and artifact references. The optional log
  shares a resizable splitter with stage details and follows output until scrolled.
- **Results** presents the design summary, outcome, failures and coverage gaps,
  stage summaries, recorded physical measurements, and published deliverables.
  Target speed is labeled separately from measured timing. Missing summaries never
  establish a pass. Open Stage Details navigates to the corresponding Run node;
  View Evidence opens the exact supporting file in Files.
- **Files** browses the actual run folder and recorded external artifacts. Text
  previews show at most the final 256 KiB; full reports open externally. Raw
  configuration remains available in state.json.

History selection preserves the current top tab. The selected run name and status
stay above the tabs. Browsing history never changes the independent worker.
A separate Active Run button returns to it and says Waiting for You when input is
pending. New Design clears the prior selection and setup; it is disabled while a
worker is alive. Startup shows a living active run or a fresh setup. No dialogs
are restored. Divider sizes and optional log visibility are saved locally in
`shared/gui/.runtime/ui_preferences.json`; read-only installations still work.

Validation reports under `001_validation/reports/` and backend evidence under
`002_backend/backend_reports/` remain in the orchestrator's stage folders. Files
loads folder contents on expansion, lists up to 1,000 entries per folder, and does
not traverse directory symlinks. Published mailbox snapshots and mutable frontend
working outputs are labeled separately. Existing absolute in-run paths are rebased
when moving a run without renaming its directory. The GUI does not fetch,
regenerate, or relocate evidence. Innovus PNG viewing remains deferred until image
export and delivery are ready.

A saved `running` status does not prove a worker is alive. The history viewer does not
resume interrupted executions. The active-run panel reconnects only to a living worker. Layout viewing is deferred.

Offline checks, including Qt window startup:

```bash
python3 -m unittest shared.gui.test_viewer
```

See [PLAN.md](PLAN.md) for phases and [ARCHITECTURE.md](ARCHITECTURE.md) for the
future execution interface.

## Approved-YAML execution

Use New Design in the Run tab to select approved YAML or enter a design description, a target controller frequency,
and an optional remote configuration. Stage controls map to existing orchestrator
flags. The worker retains existing acceptance gates, snapshot publication, bounded
repair, backend, LEC, and GDSII routing. No live end-to-end remote run has been
performed to validate this GUI milestone.

New designs default to Skip Equivalence Checking. Target Speed starts blank with
a short MHz hint and requires a positive value before launch. When Stop Before
Physical Design is selected, the LEC option is disabled and its flag is omitted
from the effective configuration; turning physical design back on preserves the
selected LEC preference. These defaults apply only to the GUI.

Remote credentials are requested through the same prominent pending-decision card
as specification approval, with the server account, a masked password field, and
Connect. There is no password field in the initial setup form.
Startup configuration travels through an anonymous pipe; credential responses use
a private Unix-domain socket in a user-owned temporary directory. Password responses
are never journaled. Orchestrator SSH retries use this channel and retain existing
retry bounds. Backend subprocess authentication failures are not yet interactive;
those failures still stop with their existing diagnostics.

All interaction adapters are worker-local bindings around existing frontend and
orchestrator functions. No `input` or `getpass` monkeypatch is used. Shared CLI
source is unchanged. The future common interaction interface remains a refactor.

One GUI run per checkout is enforced by an inherited advisory lock. Existing CLI
launches do not acquire this lock: do not run them alongside GUI workflows. Run
state and bounded worker log output refresh every second. Runs use stable `gui_`
UUID directory names. New workers keep console output in `<run-id>/worker.log`
and lifecycle/cleanup receipts in `<run-id>/gui_lifecycle.json`. The orchestrator
must create a fresh run directory, so startup evidence is briefly staged under
`shared/gui/.runtime/workers/<run-id>/` and moved into the run on its first output
after directory creation. Early startup failures still produce a contained folder
with log, receipt, and failure state. Older sibling `.worker.log` and
`.lifecycle.json` files remain readable; they are not moved while existing workers
may still use them. Completed legacy runs can be consolidated after worker exit;
the active-run monitor does this when it observes the old worker finishing.
Existing destination files are preserved. Unsupported hosts do not move receipts.

Closing during execution offers Stop Run and Close, Maintain Run and Close, or Cancel.
Cancel is the default and Escape action. Stop Run and Close requests cancellation
without another confirmation and closes automatically after the worker exits,
allowing its cleanup to finish. If cancellation cannot be confirmed, the window
stays open. Maintain Run and Close leaves the worker available for reconnection.
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
preset, and revision. Approve Design accepts that exact candidate digest.
The interactive editor hides design name and design profile metadata while
preserving those values in the approved configuration.
Adjust settings directly in the editor, then approve. The approval card has no
separate Revise action or description input. Clarification requests can still ask
for a revised description when interpretation needs more information. Approved candidates
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

Worker outcomes and cleanup receipts are stored in `<run-id>/gui_lifecycle.json`
for new workers; original stage results are retained. A dead worker
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

The Run Log follows the newest output by default. Scrolling upward with
the mouse, scrollbar, or keyboard pauses following and preserves the reading
position as output refreshes. Jump to Latest returns to the bottom and resumes
following. A new design or newly launched run starts with following enabled.

Recognized remote connection failures display an amber Server connection error
in history, stage badges, progress, and the problem dialog. Guidance suggests
checking VPN and server reachability; it does not claim to detect VPN state.
This interpretation uses saved transport errors, including the shared allocation
socket timeout. Model, simulation, queue-wait, and command-deadline failures are
not reclassified. Recorded outcomes and raw errors remain in technical details.
This does not add a network probe before generation or automatic retry/resume.

The Run view includes an RTL Generation module list, updated from this run's
worker log and its published RTL snapshot. Rows distinguish Generating, Generated,
Generation failed, and Generation unconfirmed. Generated confirms RTL production,
not independent module or system verification. The list includes observed modules;
it does not invent pending modules before they are reported. Log reads are bounded
to the first 2 MiB and snapshot manifests to 1 MiB. Missing or unsupported evidence
is omitted. The mutable frontend output directory is never used for these rows.
Saved runs show the same module evidence through their RTL Generation node.
The active module is listed first and brought into view when it changes. The
summary counts observed, generated, generating, failed, and unconfirmed modules.
Observed counts can grow as the worker reports additional modules; they are not
a predicted total. Module column widths adapt to the pane width.

During a run, stage details and the Run Log use a horizontal splitter. Drag its
vertical divider to change their widths, or hide the log to give details the full
width. The log sits directly in this workspace, outside the setup/approval scroll
area, so it has one scrollbar. It contains worker output, while outcome summaries,
attempts, settings, and artifact paths appear in Stage Notes and Files.
The log opens by default when a run first starts working; Show/Hide Run Log controls
its visibility afterward. Jump to Latest resumes following after scrolling away.

Each of the four flow nodes opens its stage's details with a click or keyboard
activation. Advancing to another stage closes the prior detail view and opens the
new one. Manually reopening an earlier stage persists across polls until the next
stage transition; Back to Active Stage returns immediately. The inspected node
has a gold border. RTL includes Modules and Stage Notes; the other stages include
recorded status, attempts, relevant configuration, and available artifact paths.
Unreached or skipped stages are identified explicitly. These views show existing
evidence; they do not fabricate per-check progress that the worker has not reported.
Pending approval hides the workspace and prioritizes the decision card.

Verification opens an Agents tab with expandable rows for Agent 1 (structural
validation), Agent 2 (functional verification), and Agent 3 (protocol and timing).
Each row includes its role, available status, scope, checks, simulation/traffic
counts, vector results, and coverage gaps as reported. Passed with gaps remains
an amber outcome. Expansion and the selected tab survive polling refreshes.
Live statuses come from explicit agent start/result lines in the latest attempt's
validation log; a missing result stays Not reported or Unconfirmed. Retrieved
final reports supply authoritative agent outcomes and additional details.
Reads are bounded: logs use the first and last 1 MiB, and individual JSON reports
are limited to 512 KiB. Large trace reports are not loaded into the UI. Only this
run's latest validation attempt is inspected, avoiding old results on retries.

The active-run panel draws the orchestrator's main route as connected nodes:
RTL generation, Verification, Physical design, and GDSII validation. The display
always has just these four nodes; active RTL repair is grouped under generation,
while the activity text still explains that repair is running.
BIST remains part of generation and LEC part of physical design; neither has an
independent structured stage status. Each node has a separate title and status
row, with a vector-drawn icon in a fixed-size slot and a visible gap before the
status text. Icons include a check for passed, play for running, clock for waiting,
warning triangle for needs attention, and crossed circle for failed. Labels use
colors. The activity bar uses an explicit animation clock throughout the worker's
lifetime, including disconnection and cleanup, but freezes while waiting for user
input, with a gray track and segment, and resumes in cyan from the same position
after a response. The status text still
identifies waits and unconfirmed progress. Elapsed time is shown without an
estimated completion percentage. Finished runs use their recorded finish time,
so reopening the GUI does not add idle time. Older runs without a finish timestamp
use the last saved state/log time as an estimate. Large bold Start/Stop
controls sit at the left of the progress panel, followed by the chip, with status
and elapsed time aligned to the right. View problem appears with the controls.
RTL/checks and backend/LEC remain grouped until
finer structured events are available. The application uses Qt Fusion and its own
palette and status colors, independent of the KDE widget theme.

## Simplified main view

Normal desktop launch opens maximized using the window manager's available screen
area. Restoring the window returns to a resizable window. History chooses its
initial width from the run-name font metrics and available workspace; manually
dragging the divider takes precedence afterward. Cards wrap to the sidebar width,
with no horizontal scrolling. Long run lists still scroll vertically.

The top-right controls are New Design and Saved Runs. New Design opens a clean setup,
clearing elapsed time, progress, errors, and draft inputs. Server configuration is
retained and saved results remain in history. History is hidden on startup and
uses a lighter selected color while open. All three workspace tabs remain visible.
Clicking a history card preserves the current tab and marks the chosen card with a
gold Selected label. An independent Active Run action remains available while a
worker is alive; pending approval never replaces the run being reviewed.

Approved Specification in Run or Results opens a read-only window with structured
fields and complete YAML. It prefers the exact GUI approval
(`gui_approved_specs.yaml`), then the run's `approved_specs.yaml` for older or
file-based runs. It does not substitute a mutable external input file. Missing
specifications disable the action; opening the viewer never modifies a run.

History cards retain the run directory names and add recorded protocol, bank
count, target clock, saved-state date, and a colored outcome badge. Missing
hardware details are omitted. Worker receipts take precedence over stale saved
outcomes. Running is shown only for a matching live process identity; stale
identified workers are Interrupted, and saved activity without verifiable worker
identity is Unconfirmed. These badges do not establish remote cleanup. Visible
history badges refresh every two seconds without changing selection.

While a worker is alive, the initial description, specification loader, target
speed, and advanced setup fields are hidden. They return after the worker exits,
preserving the entered values. Pending decisions appear first below progress and
scroll into view when a new request arrives. Their action buttons precede the
specification editor. An amber "Waiting for you" heading identifies required
input; gold emphasizes primary actions, cyan indicates active work, green indicates success, and
red marks stop actions and failures. Repeated polling preserves pending edits.

The chip animation pulses and sends brief blue electrical arcs around its pins.
The original hammer-and-anvil animation is preserved; launch with
`CAPSTONE_GUI_INDICATOR=anvil ./shared/gui/launch.sh` to use it again.
The animation represents worker liveness: it continues throughout
input waits, temporary disconnection, and cancellation cleanup, stopping only when
the worker exits. The progress bar pauses for user input and stops when the worker
exits; only a successful completion fills the bar.

## Visual theme

The desktop launches in Precision Console, the selected B design direction:
navy board (`#142638`), blue panels (`#1b3144`), inset fields (`#10202e`),
steel borders (`#456074`), and brass gold (`#dfbb67`). There is no theme selector;
older green preferences do not change startup. A gold header rule, instrument-style
contact marks, numbered flow nodes, restrained edge traces, and mostly 2 px corners
provide the engineering appearance. Navigation uses flat tabs with gold selected
underlines. Saved Runs retains its lighter selected button and readable cards.

Gold identifies primary actions and selections. Cyan identifies live work and the
moving progress segment, green identifies success, orange identifies input waits,
and red identifies failures. Approval uses an orange left rule and heading with a
gold primary action. The paused progress bar remains gray. Drawn chip pins use
brass while the electrical arcs remain blue; animation lifetime is unchanged.
Keyboard navigation has a visible focus indicator distinct from the inspected
node. Artifact filenames remain readable when selection loses focus.

Typography uses Red Hat Text and JetBrains Mono when installed, with platform and
Qt fallbacks. Branding is 24 logical px, selected-run details 19 px, section titles
18 px, body text 14 px, and technical text 13 px. Run identifiers and elapsed time
use monospace. Selected-run identity and hardware summary have separate lines;
requested MHz remains explicitly labeled as a target. All user data stays plain
text. Fonts are selected locally without downloads.

Results includes compact evidence-backed metric cards and reflows its four stage
summaries to two columns on wider viewports, returning to one column below 860
logical px. Missing counts are not treated as zero or used to invent totals.
Stage navigation and exact evidence links remain available in either layout.
Section badges fit their text; verification-agent and physical-step badges align
to the right of their evidence column. Files retains its adjustable Explorer and
preview layout. Native Qt layouts, splitters, wrapped labels, and vector drawing
allow the interface to resize without adopting the mockups' fixed coordinates.

Offscreen Qt checks cover 1150×740, 1366×768, and 1920×1080 logical windows at
100%, 150%, and 200% scale, including approval visibility and tab/header alignment.
These checks do not establish macOS or WSLg compatibility; real-machine checks
remain part of the compatibility work below.

## Compatibility groundwork

Use `python -m shared.gui` from the repository root with the chosen environment
activated; the Bash launcher is a Linux/WSL convenience. Platform checks live in
`platform_support.py`. Worker launching, discovery, and history liveness share
that boundary. Linux facilities are required for starting and reconnecting to
runs. On other hosts, Start Design is disabled with an explanation, saved runs
remain viewable, and active Linux receipts are left unchanged and shown as
Unconfirmed. A completed receipt still displays its recorded outcome.

`python -m shared.gui --doctor` reports the host, WSL detection, worker
capabilities, available font assets, and dependencies. These checks establish
local facilities, not readiness of the design tools or server. On macOS and
Windows, display availability is unknown until Qt is actually started; Linux
display environment variables are not used to judge those desktops.

Optional fonts in `assets/fonts/` are registered once per application. Include
redistribution licenses with any future binaries. Installed platform fonts and
Qt system fonts remain fallbacks. No font binaries are currently bundled.

Future GUI work should keep paths in `pathlib`, use Qt for opening files and
drawing icons, use flexible layouts and wrapped labels, and isolate any new
OS-specific worker behavior behind the platform boundary. Before claiming
support, test startup, history, dialogs, long paths, and 100/150/200% display
scaling on actual Linux, WSL2/WSLg, and macOS machines. Native Windows execution
and macOS execution need worker transport/process-lifecycle implementations;
mocked platform tests do not establish that support. Packaging and real-machine
validation remain future work.
