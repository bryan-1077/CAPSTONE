# Phase 1: interfaces and execution boundaries

Status: design defined; execution changes are not implemented. Scope: Linux,
Standard (`expand-spec`) flow. Experimental flow remains outside initial execution.

## Source map

| Boundary | Existing source | Required GUI integration |
| --- | --- | --- |
| Configuration and outcomes | `shared/top_orchestrator/contracts.py`: `RunConfig`, `StageResult`, `RunState` | Validate inputs through shared code; retain original stage results |
| Routing and persistence | `shared/top_orchestrator/orchestrator.py`: `run`, `_run`, `save_state` | Emit events around existing routing; keep atomic `state.json` writes |
| Specification preparation | `shared/top_orchestrator/frequency.py`: `prepare_generation` | Supplied YAML is snapshotted; text selection currently launches a subprocess |
| Description, clarification, approval | `local/frontend/configure_from_text.py`: `collect_user_description`, `prompt_yes_no`, `main` | Extract reusable interaction callbacks; preserve normalization, assumptions, retry limits, and approval before writing |
| Frequency and optional advice | `shared/top_orchestrator/frequency.py`: `select_frequency`, `ask_advisor` | Replace direct `input` with an interaction adapter; retain finite-positive checks, clock warnings, and explicit selection |
| Initial credentials and retries | `shared/top_orchestrator/auth.py`: `prepare_password`, `prompt_password`, `password_session` | Supply secret responses through the interaction adapter; retain bounded authentication retries |
| Remote preflight credentials | `shared/top_orchestrator/remote.py`: remote-check path | Route its separate `getpass` call through the same adapter before exposing remote-check |
| Child process output | `shared/top_orchestrator/nodes.py`: `stream_command` | Preserve file logs and stream bounded output; inherited terminal stdin is unsuitable for GUI decisions |
| Backend handoff | `shared/top_orchestrator/nodes.py`: backend adapter; `local/backend/app.py` | Preserve password-FD handoff and structured backend outcomes; audit fallback prompts before enabling GUI launch |
| Remote verification and GDSII checks | `shared/top_orchestrator/validation.py`, `gdsii_validation.py` | Surface invocation, evidence, and cleanup records without parsing console prose for acceptance |
| LEC | `shared/top_orchestrator/lec.py` and backend integration | Display as a backend substep; it is not a top-level `Stage` |
| Shared Slurm allocation | `shared/top_orchestrator/allocation.py`: `AllocationSession`, `cancellation_command` | Register owned allocation and steps; retain release evidence |
| Backend remote launch | `local/backend/workers/workers.py`, `local/backend/services/ssh_executor.py` | Add owned job/step registration; current launch includes the shared name `ecen-454` |
| Timing and summary | `shared/top_orchestrator/runtime.py`, `summary.py` | Reuse measured durations and final evidence; target MHz is not achieved frequency |

## Process and transport decisions

- [x] Use PySide6 Widgets for the window; keep Qt imports out of orchestration code.
- [x] Launch an independent Python worker with a new session and redirected file logs.
- [x] Let the worker own orchestration and child processes; window closure only disconnects.
- [x] Use a local Unix-domain socket with newline-delimited JSON for commands and events.
- [x] Place the socket in a user-owned directory with mode `0700`; socket mode `0600`.
- [x] Bound message sizes and reject unknown protocol versions and malformed payloads.
- [x] Separate ordinary subprocess output from structured protocol traffic.
- [x] Persist nonsecret events as append-only JSONL; tolerate a partial final line after a crash.
- [x] Keep current orchestrator state and evidence authoritative for stage acceptance.
- [x] Use a worker UUID as stable identity; run directories can be renamed during frequency selection.
- [x] Publish the current run path when it changes; never use a mutable path as identity.

Proposed modules under `shared/gui/`: `app.py` (entry), `window.py` (views),
`run_reader.py` (historical artifacts), `controller.py` (client), `worker.py`
(execution owner), `protocol.py` (messages), and `registry.py` (identity/locking).
Names are implementation targets, not existing modules.

## Message contract v1

Every message contains `version: 1`, `type`, `worker_id`, and `payload`.
Worker events additionally contain increasing `seq` and an ISO-8601 UTC `time`.
Display timestamps in America/Chicago. Client commands contain `command_id`;
acknowledgments refer to it. Duplicate command IDs must not repeat side effects.

| Direction | Type | Payload |
| --- | --- | --- |
| Client → worker | `hello` | Supported version and last received event sequence |
| Worker → client | `snapshot` | Current run path, lifecycle, stage results, pending requests, latest sequence |
| Worker → client | `stage_started` | Stage, attempt identifier, artifact directory |
| Worker → client | `stage_finished` | Original structured `StageResult`, attempt identifier |
| Worker → client | `activity` | Informational substep and message; never acceptance evidence |
| Worker → client | `log_available` | Log path and byte offset; client reads incrementally |
| Worker → client | `artifact_available` | Artifact kind, path, owning stage/attempt |
| Worker → client | `run_path_changed` | Current run directory |
| Worker → client | `input_requested` | Request ID, kind, revision, prompt, validated public context |
| Client → worker | `input_response` | Matching request ID/revision, action, response value |
| Client → worker | `secret_response` | Matching credential request ID and password; never persisted |
| Client → worker | `cancel` | Request cooperative stop for this worker's run |
| Worker → client | `ack` / `command_rejected` | Command ID and result or reason |
| Worker → client | `remote_job_updated` | Owned host/account, job or step ID, unique submission token, lifecycle |
| Worker → client | `run_finished` | Original final outcome plus cancellation/cleanup disposition |
| Worker → client | `heartbeat` | Worker liveness; not workflow progress |

- [x] Subscribe before taking the snapshot; replay events after its sequence without gaps.
- [x] On reconnect, return unresolved public requests; require resubmission of secrets if still needed.
- [x] Reject stale responses, duplicate approvals, and responses after cancellation.
- [x] Disconnecting a window does not answer or decline pending input; worker waits.
- [x] Cancel must remain processable while execution waits for input or a blocking SSH call.
- [x] Use an I/O/control thread and cancellation token; run orchestration on the worker main thread.

## Interaction adapter

- [x] Define a Qt-independent interface: `emit(event)`, `request(kind, context)`, `check_cancelled()`.
- [x] Provide terminal and worker-backed implementations; default remains terminal behavior.
- [x] Inject it at orchestration and frontend entry points; do not monkeypatch `input` or `getpass`.
- [x] For the specification subprocess, add explicit structured interaction transport or extract a callable service before GUI natural-language support.
- [x] Keep generation/checking subprocesses noninteractive in the approved-YAML milestone.

| Request kind | Public context | Allowed responses |
| --- | --- | --- |
| `description` | Supported options, clarification reason, remaining attempts | Submit description or cancel |
| `spec_approval` | Candidate config, assumptions, preset, candidate digest, revision | Approve exact candidate, request revision, or cancel |
| `frequency` | Suggested MHz/source, explicit clock constraints, warnings, advisor conversation | Select finite positive MHz, request advice, or cancel |
| `credential` | Host, username, retry number, reason | Secret response or cancel |

Approval records bind to the candidate digest and revision. A revision invalidates
earlier approvals. Frequency advice never selects a target automatically.

## Credentials and local configuration

- [x] Use masked credential fields and private local IPC; exclude secrets from command arguments and environment variables.
- [x] Retain credentials only in worker memory for the active password session.
- [x] Preserve the existing backend password-FD handoff; do not serialize secrets into backend state.
- [x] Exclude secret responses from event journals, diagnostic dumps, and command replay caches.
- [x] Clear GUI input fields after submission and release cached references at completion; do not promise secure memory erasure in Python.
- [x] Store machine-specific paths and connection selections in ignored local settings.
- [x] Never guess a new connection identity from GUI defaults; display the resolved account before authentication.

## Ownership, reconnect, and cancellation

- [x] Acquire a Linux advisory lock scoped to the canonical checkout before worker launch; worker holds it through cleanup.
- [x] Route future CLI launches through the same lock before claiming GUI/CLI mutual exclusion; legacy CLI cannot currently enforce this.
- [x] Keep a private registry entry containing worker UUID, PID, process start identity, socket path, and current run path.
- [x] Verify protocol handshake and process start identity; a matching PID alone is insufficient.
- [x] Treat failed heartbeat/connection as disconnected first; establish worker death before marking interrupted.
- [x] Never restart work automatically from stale state; preserve interrupted evidence.
- [x] Leave Run Executing & Close keeps the worker and lock alive and disconnects the client.
- [x] Stop Run first disables new stage launches and invalidates outstanding decisions.
- [x] Register child process groups and remote submissions before waiting for completion.
- [x] Give each remote submission a unique run/attempt token; record exact job or step IDs when known.
- [x] Cancel only owned jobs/steps; never cancel by the generic backend name `ecen-454`.
- [x] Reuse adapter cleanup and shared allocation release; make repeated cancellation safe.
- [x] Attempt cooperative cleanup before bounded termination of owned local process groups.
- [x] Query remote status after cancellation; record confirmed termination or `cleanup_unconfirmed` with identifiers.
- [x] Persist worker lifecycle separately from existing `StageResult` status literals; cancellation must not fabricate verification failure or success.

Existing adapters already attempt cleanup, but they do not yet provide one complete
ownership registry or a uniform GUI cancellation API. Backend job registration and
blocked-call interruption require implementation before enabling Stop Run.

## Dependency and packaging assessment

- [x] Local inspection: Python 3.14.7 and importable PySide6 6.11.1; no dependency installation performed.
- [x] Choose Widgets with QtCore/QtGui/QtWidgets; no web engine or JavaScript toolchain required.
- [x] Qt documents open-source LGPLv3/GPLv3 and commercial licensing: [Qt for Python](https://doc.qt.io/qtforpython-6/).
- [x] Before distributing binaries, review included module licenses and ship required notices and license materials.
- [x] Start with a virtual environment and source launch; choose a tested dependency pin during GUI implementation.
- [x] Qt recommends `pyside6-deploy` for deployment: [deployment documentation](https://doc.qt.io/qtforpython-6/deployment/index.html).
- [x] Packaging does not bundle the remote EDA environment, SSH access, PDK, or frontend provider credentials.
- [x] Verify Qt Widgets startup using the offscreen display plugin in the Phase 2 smoke check; native desktop interaction remains a manual check.
- [ ] Validate a clean environment and second Linux machine during Phase 6.

## Implementation handoff

- [ ] Phase 2: implement historical artifact reading and a read-only window first; no execution changes required.
- [ ] Phase 3: implement protocol, worker, shared lock, credential adapter, and approved-YAML execution.
- [ ] Phase 4: refactor specification and frequency conversations behind the interaction adapter.
- [ ] Phase 5: enable close/reconnect and Stop Run only after their lifecycle checks pass.
- [ ] Keep screenshot and GDSII integration in the later phase from `PLAN.md`.
