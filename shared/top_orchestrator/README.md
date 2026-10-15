# Top-level orchestrator

Run from the repository root using Python 3.10+. Frequency selection uses PyYAML;
frontend execution and optional frequency advice need the existing frontend dependencies.

Generation accepts these stage controls:

| Flag | Behavior |
| --- | --- |
| `--no-verif` | Skip remote verification; alias for `--skip-verification`. Continues to backend unless `--no-backend` is also set. |
| `--no-backend` | Disable backend execution. Combine with `--no-verif` to stop after frontend checks and snapshot publication. |
| `--no-rtlgen` | Check existing RTL without spec selection or generation; alias for `--checks-only`. Cannot be combined with `--input` or `--cache`. |
| `--no-bist` | Skip BIST simulation while retaining RTL lint. Records BIST as `not_run`. |

For frontend generation, lint, BIST, and automatic repair only:

```bash
python3 -m shared.top_orchestrator generate \
  --input local/frontend/configs/user_input.yaml --no-verif --no-backend
```

To check existing RTL instead, omit `--input` and add `--no-rtlgen`.
Add `--no-bist` to run only RTL lint. Frontend-only runs need no remote
configuration or backend-frequency selection. Automatic repair preserves the
selected stage controls and rechecks existing RTL after a successful repair.
These controls apply to `generate` entry. Default routing continues from remote
validation into backend when verification reports `READY_FOR_BACKEND`;
`--no-backend` stops after verification and `--no-verif` goes directly to backend.

```bash
# Start at interactive spec selection, then run generation and downstream stages.
python -m shared.top_orchestrator
# Equivalent: python -m shared.top_orchestrator generate

# Inspect the generation command and route without writing files or calling models.
python -m shared.top_orchestrator generate --input local/frontend/configs/user_input.yaml --cache --plan

# Execute generation, remote verification, and physical design (configure SSH first).
python -m shared.top_orchestrator generate --input local/frontend/configs/user_input.yaml --cache

# Bypass the frequency prompt with an explicit backend target.
python -m shared.top_orchestrator --input local/frontend/configs/user_input.yaml --target-mhz 230

# Generate fresh RTL, run frontend checks, then go straight to backend.
python -m shared.top_orchestrator generate --skip-verification --target-mhz 200 --ask-password

# Inspect an existing frontend failure workspace without model calls.
python -m shared.top_orchestrator debug --failure-dir /absolute/path/to/failure_workspace --offline

# Run the existing debug repair graph, then require validation.
python -m shared.top_orchestrator debug --failure-dir /absolute/path/to/failure_workspace --repair

# Verify an existing snapshot, or inspect the blocked backend entry.
python -m shared.top_orchestrator validation --mailbox shared/mailbox/<run-id>/revision_001 --ask-password
python -m shared.top_orchestrator backend
```

By default, generation opens the existing natural-language spec selection and
confirmation flow, asks for a backend target in MHz, then runs generation. Use `--input` to skip spec
selection and reuse a YAML configuration. `--cache` caches RTL generation only;
spec selection can still call a model. Use `--input ... --cache` to bypass both. Debug currently accepts an existing
failure workspace created by `debug_agent.py`; new report intake is future work.
Debug retains its existing attempt limit and patch checks.

## Current structure

| File | Responsibility |
| --- | --- |
| `contracts.py` | Configuration, stage results, and run state |
| `nodes.py` | Frontend/backend adapters and stage registration |
| `validation.py` | Snapshot transfer, remote invocation, report retrieval, and evidence checks |
| `validation_runner.py` | Standalone uploaded wrapper for the unchanged deployed pipeline |
| `mailbox.py` | Atomic RTL/spec snapshots and content verification |
| `remote.py` | SSH/Slurm configuration, job command builder, and remote preflight |
| `orchestrator.py` | Explicit routing and atomic state-file updates |
| `frequency.py` | Spec handoff, deterministic frequency checks, and opt-in advice |
| `__main__.py` | CLI and plan-only inspection |

Spec selection wraps `local/frontend/configure_from_text.py --output <run-dir>/approved_specs.yaml`
without `--run-flow`. Supplied `--input` specs are copied to that same run-local snapshot.
After frequency selection, generation wraps `local/frontend/run_flow.py` with the snapshot.
Debug wraps
`local/frontend/debug_agent.py graph`. These workflows keep their internal steps.
After generation, the adapter runs `run_sim.sh --no-wave-prompt` automatically.
BIST output streams to the terminal and is saved as `bist.log` in the run directory.
BIST requires exit code zero, a TEST PASS marker, and no TEST FAIL marker; otherwise
the failure routes to debug. Successful generation plus BIST routes to validation.
After repair, system lint and BIST rerun on the patched RTL before validation. A successful process exit
alone does not count as a debug repair: its structured status must also report
success. BIST and system-lint failures automatically invoke `debug_agent.py auto --repair`
with their evidence and rerun command. Other generation errors stop. The debug
agent retains responsibility for classifying evidence and deciding whether to patch.

By default, generation continues through remote system validation into backend.
Validation succeeds when verification reports `Readiness: READY_FOR_BACKEND` for
the controller RTL snapshot. Backend checks that accepted validation evidence
belongs to the same immutable mailbox revision, then runs at the selected frequency
using the same SSH connection settings and password. `--no-backend` stops after
verification. Direct `validation` entry runs verification only; direct backend
testing still requires `--allow-unvalidated`.

Each execution creates `runs/<id>/state.json` plus frontend subprocess logs. Later stages and retries get numbered
subdirectories so earlier evidence is preserved.
Use `--run-dir` to select a new output directory. Existing directories are rejected.
Exit codes: 0 = complete (or plan printed), 2 = incomplete/stopped or CLI misuse,
1 = run-artifact filesystem error. A specs-to-verification run completes when the
required verification evidence passes. A direct unvalidated backend test can complete;
its result explicitly records `validation: not_run`.

For frontend-to-backend execution, use `generate --skip-verification`. This starts
with the usual interactive spec selection and frequency selection, runs RTL
generation and frontend lint/BIST, publishes a fresh mailbox revision in the
current checkout, and passes that exact snapshot to the local backend app. It
skips remote verification and records `validation: not_run`. No `--mailbox` or
`--allow-unvalidated` argument is needed. Frontend failures still follow the
existing bounded repair/recheck flow; failed checks or publication stop before
backend execution. Backend failure stops with its logs and structured result.

Use `--input local/frontend/configs/user_input.yaml` to reuse approved YAML;
add `--cache` to reuse frontend RTL generation. For existing RTL, use
`--checks-only --skip-verification --target-mhz 200`. The checks-only route
requires an explicit frequency because it skips selection. Backend SSH settings
and `--ask-password` apply to these routes, and `--backend-python` can select the
backend environment. Validation's `--remote-config` is not used by this route.

State files are diagnostic records, not resumable checkpoints. The frontend still
writes into its existing shared output directories; run one workflow at a time.
An interrupted process may leave state marked `running`. Frontend prompts and output appear live in the terminal and are also saved to
the stage log. Terminal input is passed directly to the frontend.

## Backend PDK prerequisite

The server backend launchers must resolve an installed SKY130 PDK readable by
the configured SSH account on the Slurm compute node. A PDK installed under a
teammate's home directory is not automatically available under your account.
For group use, place the agreed PDK version in a shared directory and configure
the server's Genus and Innovus launchers to use it consistently, or install the
same version for each account. Check the server launchers' supported arguments
and environment settings before configuring a path; the canonical Genus scripts
are maintained on the server and are not included in this checkout.

`Could not find volare sky130 root: ...` indicates that the netlist launcher
failed before Genus started. The backend preserves the launcher logs and stops
with `failure_class: netlist_environment_error`, skipping synthesis review,
edits, and retries. Correct the PDK installation/path before starting a new run.

## Planned integration contracts

Run configuration now carries an explicit mailbox path. Snapshots contain the
RTL tree, matching expanded YAMLs, the approved configuration when available,
and content hashes. Validation evidence now records the exact snapshot digest,
remote invocation, original report archive, and required stage results. Failure
intake normalization remains future work.

**Validation node:** consume the design bundle and validation configuration;
run the required structural and behavioral checks; return a structured report
with per-check pass/fail/not-tested status and evidence. System acceptance follows
verification's `READY_FOR_BACKEND` decision for that revision. Normalize repairable failures into the
debug agent's intake contract (`lint`, `bist`, or `verif`). Static checks alone
must not count as behavioral verification.

**Backend node:** consume that same validated revision, timing/physical constraints,
and tool configuration. Return output artifact paths, tool reports, and whether
the required implementation checks passed. Classify failures as RTL defects,
constraint/configuration issues, tool/environment failures, or unmet implementation
targets. Only supported RTL defects should feed frontend debug (`pd` intake).

**Routing and lifecycle:** validation/backend -> debug edges are implemented for
failed results carrying a `FailureReport`. System Agent 2/3 failures after Agent 1
PASS now supply verification run intake; backend currently retains native triage
in its JSON result and stops on failure. After any RTL repair, invalidate earlier
validation/backend results and run validation again before backend. Reject stale
or absent validation evidence, including for direct backend entry. Keep debug's
internal attempt budget separate from a bounded top-level repair-cycle budget.
Persist revision fingerprints before adding resume or caching. Tool/environment
errors should have bounded retries; ambiguous requirements should stop for input.

Remaining integration: failure normalization, live server
verification, then resume.

## Target frequency selection

After approving specs, enter a positive MHz value, press Enter for the displayed
default, or type `discuss`. Only `discuss` calls the frequency advisor; numeric,
default, and `--target-mhz` selection do not. Existing spec selection and RTL
generation still have their own model calls. During discussion, enter follow-up
questions, a final numeric target, or Enter to accept the original displayed default.
Advice never automatically changes the target or approved specs. An unavailable
advisor leaves manual selection available. EOF or interruption stops before generation;
unattended runs should supply `--input` and `--target-mhz`.

The deterministic clock fields supported in the input YAML are
`controller_clock.frequency_mhz`, `controller_clock.period_ns`, and
`controller_clock_mhz`. These are optional explicit controller-clock annotations.
If multiple fields disagree, the highest frequency supplies the suggestion and
the other values appear as mismatch warnings. With none present, the configured
fallback is **200 MHz**, independent of backend environment defaults. `memory.speed`
is a DDR transfer-rate setting and never supplies a controller-clock target.
Mismatches with an explicit operating clock are reported and saved as warnings;
the chosen backend target does not rewrite operating requirements.

`state.json` stores the resolved `target_mhz`, selection source, and approved spec
snapshot path. `frequency_selection.json` stores the suggestion, warnings, and
discussion transcript. Repair cycles preserve the target without prompting again.
Checks-only and direct debug/validation/backend entries do not run spec or frequency
selection. `--plan` never prompts or calls a model.

The advisor reuses `configure_from_text.call_llm` and its default model, with
`TAMUS_AI_CHAT_API_KEY`. It receives the approved YAML and discussion history;
past-run evidence is not connected yet. It is explicitly told not to invent
measurements or guarantee timing closure.

Discussion context includes the project's SKY130 PDK and the user's observation
that 250 MHz has not yet been reliably reached. The advisor favors 150–200 MHz,
usually starting at 200 MHz, and treats 220–230 MHz as stretch targets. It must
not recommend above 250 MHz and must describe 250 MHz itself as unproven. This is
project-specific guidance, not a universal PDK limit or verified timing data.
Conflicting approved clock requirements must be explained, not silently changed.
These are discussion guidelines; manual numeric input and the 200 MHz fallback
are unchanged. Actual reports from previous runs can be connected later.

The backend adapter uses `backend_command(config, mailbox_revision)` to pass the
saved value as `app.py --mailbox <revision> --target-mhz <value>`. It rejects absent
targets. Direct backend testing requires an explicit target; normal generation
passes the selected target into backend after accepted verification.

## Checks

```bash
python -m unittest discover -s shared/top_orchestrator -p 'test_*.py'
```

Tests use fake frontend processes and temporary artifacts; they make no model or
EDA tool calls and do not modify generated RTL.

## Automatic debug handoff

To test existing RTL without spec selection or regeneration:

```bash
python -m shared.top_orchestrator --checks-only
```

This runs system lint, then BIST. A failure enters automatic debug repair; a
successful repair repeats both checks before validation. Do not combine this with
`--input` or `--cache`. Add `--plan` to inspect routing without executing anything.

`StageResult.failure` accepts `FailureReport(source, log, target_command)`.
Sources are `lint`, `bist`, `verif` (validation), and `pd` (backend). The log/report
path must be absolute and point to existing evidence in a format the debug agent
understands. `target_command` is optional, but should reproduce the failed check;
it runs from `local/frontend`, so include absolute paths or an explicit working
directory for external tools. Adapters must not use raw untrusted report text as
shell commands. Without a target command, debug may only perform its local checks;
the orchestrator still reruns frontend checks and validation after repair.

Remote system validation hands an Agent 2 or Agent 3 FAIL to automatic debug when
Agent 1 passed and the current frontend RTL still matches the verified snapshot.
Debug imports the downloaded verification run directory. Transport failures,
incomplete evidence, and a changed frontend stop without automatic repair.
Backend failures also stop without automatic PD repair. Neither adapter supplies
an automatic repair command from raw remote report text.

`--max-repair-cycles` defaults to 2 across the whole run; set it to 0 to stop at
failures. Each debug call separately retains its internal repair-attempt budget.
Unsuccessful debug stops the run; exhausted top-level cycles report
`needs_attention`. After successful debug the generation node runs in checks-only
mode: it does not rerun spec selection or regenerate RTL.

## Mailbox snapshots and backend testing

After successful generation/checks and BIST, the orchestrator publishes
`shared/mailbox/<run-directory-name>/revision_001/`. Repair cycles publish a new
numbered revision after rechecks. Existing destinations are rejected. Publication
uses a temporary directory and a final rename so incomplete snapshots are not
exposed as revisions. Run-directory names must be unique across mailbox runs.

Each snapshot contains:

- `rtl/`: the entire frontend RTL output tree, including manifest, filelist, and headers.
- `specs/`: expanded YAMLs from `local/frontend/microarch/gen_exp/` whose
  `design_name` matches a module in the RTL manifest,
  preserving their relative directories. Aggregate `master.yaml` and `*_master.yaml`
  files are excluded. Older workspaces without `microarch/` use `expanded/`;
  migrated workspaces never fall back to that legacy directory.
- `approved_specs.yaml`: the approved input configuration, when available.
- `snapshot.json`: file hashes, the backend-compatible RTL digest, and modules
  without matching expanded specs (for example generated wrappers).

Snapshots do not claim verification coverage. They are never updated by the
orchestrator; input hash verification detects later edits before backend launch.
The frontend remains a shared workspace, so run one workflow at a time.

Default generation runs verification and backend on the newly published snapshot.
`--skip-verification` runs backend without remote verification, while `--no-backend`
stops after verification. To test only backend execution on a
published snapshot without functional validation:

```bash
python -m shared.top_orchestrator backend \
  --mailbox shared/mailbox/<run-id>/revision_001 \
  --target-mhz 200 --allow-unvalidated
```

Add `--plan` to inspect without execution. Use `--backend-python /path/to/venv/bin/python`
if the backend dependencies (including Paramiko) are in a separate environment.
The adapter starts `local/backend/app.py` from `local/backend`, streams output,
and saves `backend.log`, `backend_result.json`, and `backend_reports/` beneath
this invocation's run directory. Backend retains its existing SSH configuration,
remote build handling, internal retries, and timing-closure logic.
`backend.log` contains progress and failure messages. Full backend state, including
tool logs and AI responses, is saved in `backend_result.json` and
`backend_reports/full_session.log.state.json` instead of being printed into the
terminal transcript. Stage-specific diagnostic files remain in `backend_reports/`.

Backend SSH settings can be overridden with `CAPSTONE_SSH_HOST`,
`CAPSTONE_SSH_USER`, `CAPSTONE_SSH_PORT`, and `CAPSTONE_SSH_KEY`. They otherwise
retain the backend defaults. Set `CAPSTONE_SSH_KEY` to an empty string to omit
the explicit default key (SSH agent/discovered keys remain available). Backend
does not consume `--remote-config` or the validation project-directory setting.
For password authentication, add `--ask-password` to the command above. The local
backend prompts securely and reuses the password for staging and execution;
the password is not put in command arguments or saved state. A terminal is required.
Authentication errors occur before RTL staging or EDA execution.

A backend pass requires exit zero, a JSON state reporting `done`, success for all
four implementation stages, the expected RTL digest and target period, and
successful timing closure when enabled. Failed or missing physical-verification
status when that gate is enabled also prevents completion. The backend's separate
physical agent checks DRC and process
antenna counts and rechecks setup/hold timing after its repairs; see
[physical verification](../../local/backend/physical_verification/README.md).
Early stage stops, missing results, and mismatched evidence fail. This is backend
completion, not functional validation.
Native failure triage is retained in the JSON result; PD debug intake translation
is not yet connected. Remote output paths remain in backend state; this adapter
does not download every physical-design artifact.

The backend CLI also supports `--result-json <new-file>` and `--log-dir <new-directory>`
for standalone invocations. The latter redirects session, parsed-stage, and timing
closure logs. Existing result files and explicit log directories are rejected to
preserve earlier evidence. Other legacy helpers can still use their existing
shared output locations. Omitting these options preserves standalone logging behavior.

## SSH and Slurm preflight

The `remote-check` entry connects over SSH, checks the remote directory and `srun`,
then allocates a short Slurm job. Inside that job it sources the remote environment,
checks Python, and records the compute hostname, working directory, and Slurm job ID.
This tests access and environment setup only; it does not run validation or backend.

Install the transport dependency into the Python environment used by the orchestrator:

```bash
python -m pip install -r shared/remote/requirements.txt
```

Set these exports in your **local** `.bashrc`, replacing the account and directory:

```bash
export CAPSTONE_SSH_HOST=olympus.ece.tamu.edu
export CAPSTONE_SSH_USER=YOUR_USERNAME
export CAPSTONE_REMOTE_PROJECT_DIR=/absolute/server/path/to/your/project
export CAPSTONE_SLURM_PARTITION=adademic
export CAPSTONE_SLURM_QOS=olympus-academic
# Optional: omit to use SSH-agent/default-key discovery.
export CAPSTONE_SSH_KEY="$HOME/.ssh/id_ed25519"
# Python and VCS setup are built into the remote defaults; no exports needed.
```

The partition spelling above is copied literally from backend; use the actual
partition on your server. The remote project path is independent of backend's
hardcoded directory. The SSH key setting is a local file path, not key contents.
Open a new terminal or source your local `.bashrc` before starting the orchestrator;
an already-running IDE may need its environment refreshed. Python reads exported
variables from its environment and does not parse or execute your local `.bashrc`.

```bash
python -m shared.top_orchestrator remote-check --plan
python -m shared.top_orchestrator remote-check
```

If your SSH account also requires a password, run:

```bash
python -m shared.top_orchestrator remote-check --ask-password
```

Runs that include verification or physical design require a hidden SSH password
prompt after spec and frequency selection, before frontend execution:
`Enter SSH password for 'user' for Verification and Physical Design portions of the flow:`
The password is reused for remote stages during that run and is not saved in
configuration or run artifacts. Frontend-only runs and `--plan` do not prompt.
The backend receives it through an anonymous inherited descriptor.

The hidden terminal prompt passes the password to the SSH connection alongside
key/agent authentication. If the initial login rejects authentication, validation
and remote-check re-prompt up to three times (four attempts total). Other failures
are not retried. Remote-check retries require `--ask-password`; flow validation
retries use the password collected before execution. It is held in memory for this check (including any
reconnection) and is not saved in configuration or run artifacts. `--plan` never
prompts. This option handles an account password; it does not implement custom
Duo/MFA challenge handling. A terminal capable of disabling echo is required.

By default the compute job runs `source ~/.bashrc`, then
`source /opt/coe/synopsys/vcs/W-2024.09-SP2-4/setup.vcs.sh` in its remote login Bash
shell, and uses `/usr/bin/python3` (the server's existing Python 3.6 environment).
`load-ecen-454` is an alias for allocating an interactive `srun` session; do not
include it in setup because the orchestrator already allocates the job. Its
`--pty` and `--x11=first` options are unnecessary for this command-line probe.
Both setup and the probe run in the same shell, and setup
errors stop execution. Override setup with the trusted shell snippet
`CAPSTONE_REMOTE_SETUP` when the server requires a different environment.
The job enables Bash alias expansion so setup aliases defined by `.bashrc` work
in this noninteractive shell. If `.bashrc` defines tools only for interactive
sessions, configure the underlying environment setup script explicitly instead.

Additional exported settings are `CAPSTONE_SSH_PORT` (22), `CAPSTONE_SLURM_CPUS` (1),
`CAPSTONE_SLURM_WAIT_SECONDS` (30), `CAPSTONE_SLURM_JOB_SECONDS` (120), and
`CAPSTONE_REMOTE_TIMEOUT_SECONDS` (210). The local timeout must allow the allocation
wait, job time, and at least 30 seconds for connection/setup overhead.

For optional per-run overrides, copy `remote.example.json` to `remote.local.json`,
edit it, and pass `--remote-config shared/top_orchestrator/remote.local.json`.
Explicit JSON values override exported values; remaining settings use defaults.
The JSON `required_tools` list can check executables such as `vcs` inside the job;
it is empty by default, so the base preflight does not claim simulator readiness.

Each check preserves `state.json`, `remote.log`, and `remote_result.json` in a new
run directory. Output streams locally while both SSH streams are drained.
Failed/interrupted compute checks attempt `scancel` using their unique job name
and save the cancellation result. Slurm also receives a job time limit. A failed
cancellation is recorded; closing SSH alone is not proof that the job stopped.

Transport lives in `shared/remote/ssh_executor.py`; the backend import forwards to
it. Backend retains its existing connection configuration. The shared transport
retains backend's host-key behavior (load known hosts, automatically accept unknown
keys), SFTP helpers, and structured command results. Validation uses the same
transport and Slurm wrapper, as described below.


## Specs to remote verification

Configure the remote connection using the exports above or your own JSON file.
These options now work on generation, validation, and debug repair entries as well
as `remote-check`. The default deployed pipeline path is
`<CAPSTONE_REMOTE_PROJECT_DIR>/server/validation/run_pipeline.py`. The server must
already have the validation scripts, Python dependencies, simulator, and model
configuration installed. This adapter does not deploy or edit that source folder.

System verification is now the default remote mode. The uploaded wrapper runs:

```text
python3 server/validation/run_pipeline.py <invocation>/input/rtl --system
```

The RTL directory comes from the immutable mailbox revision. Matching YAMLs remain
in `<invocation>/input/specs` as snapshot evidence, but system mode passes only the
RTL directory to the pipeline. Reports are collected from
`<invocation>/input/rtl/verification_reports`, preserving their paths in the archive.
Snapshot hashes are checked before and after execution; generated reports are
excluded from that comparison, while changes to RTL or other inputs are rejected.

The adapter requires an explicit `mode: system` summary for the same RTL input
directory and controller manifest, successful process execution, and a published
`final_system_report.json` for the same controller top. It uses verification's
`Readiness` decision (`verification_readiness: READY_FOR_BACKEND`) in the summary
and final report to accept system validation. `PASS_WITH_GAPS`, coverage limitations,
and review flags are retained as evidence and do not override that decision.
Blocked or missing readiness stops with `needs_attention`; process exit zero alone
never establishes backend readiness.

The frontend writes controller manifest v2 using the shared discovery contract.
After lint/BIST, including checks of existing RTL, the orchestrator refreshes
metadata with `generate_wrapper.py --manifest-only` before publishing a new
revision. That refresh writes the filelist, feature indexes, and manifest without
rewriting RTL or the testbench. The mailbox includes every referenced config,
expanded spec, IR, JEDEC dictionary, and testbench; discovery must still pass after
relocation. Earlier published revisions remain unchanged.

For the older module/YAML batch pipeline, set `CAPSTONE_VALIDATION_MODE=modules`
or `validation_mode: "modules"` in the remote JSON configuration. That mode retains
the RTL-plus-specs invocation, parallel `--jobs` option, and per-design evidence
checks. System mode verifies the controller rather than claiming independent
verification of every module YAML.

Use the server interpreter that already runs the validation agents successfully.
`CAPSTONE_REMOTE_PYTHON` (or the JSON `python` setting) defaults to `/usr/bin/python3`;
the uploaded wrapper supports Python 3.6. The local orchestrator still requires
Python 3.10+. Existing Slurm and environment setup applies to this interpreter.
Environment and JSON overrides still take precedence. Remove old
`CAPSTONE_REMOTE_PYTHON` and `CAPSTONE_REMOTE_SETUP` exports to use the built-in
defaults (`unset CAPSTONE_REMOTE_PYTHON CAPSTONE_REMOTE_SETUP` in the current
terminal). Remove corresponding JSON overrides if using `--remote-config`.

For a new server environment only, install the validation dependencies there,
from the remote project directory, using the selected interpreter:

```bash
python3 -m pip install --user -r server/validation/requirements.txt
python3 -c 'import sys, yaml, requests, dotenv; print(sys.executable)'
```

For a virtual environment, omit `--user` and set the remote `python` setting to
the absolute path of its `bin/python`. Installing into your local frontend Python
does not install packages into the remote Slurm interpreter. `yaml` is provided
by **PyYAML**; `dotenv` is provided by **python-dotenv**. A missing import can stop
the pipeline before any summary exists. The wrapper records the interpreter and
the end of pipeline stderr in `remote_validation_result.json` and reports that
startup error when a failed pipeline has no summary.

Check imports under the **same SSH account** the orchestrator uses. Packages
installed with `pip --user` belong to that account; an import under another login
does not verify the orchestrator's environment, even with `/usr/bin/python3` on
the same compute node. Use `dotenv.__file__` to print the module path
(`dotenv.file` is not a module attribute):

```bash
whoami
/usr/bin/python3 -c 'import sys, site, dotenv; from dotenv import load_dotenv; print(sys.executable); print(dotenv.__file__); print(site.getusersitepackages())'
```

Each validation result records the configured SSH username, and the remote
wrapper logs and saves `python_environment` with the actual compute account,
interpreter, import paths, user site settings, and imports of `yaml`, `requests`,
and `dotenv` after Slurm/tool setup. It does not include API keys or `.env`
contents. Compare these diagnostics with your manual session before installing
packages. Generated simulator symlinks inside `verification_reports` are excluded
from snapshot checks and report archives; symlinks in published inputs remain
an error.

```bash
# Interactive spec selection -> RTL -> lint/BIST -> snapshot -> verification.
python -m shared.top_orchestrator --ask-password

# Use existing approved input specs.
python -m shared.top_orchestrator generate \
  --input local/frontend/configs/user_input.yaml --target-mhz 200 \
  --remote-config shared/top_orchestrator/remote.local.json --ask-password

# Rerun verification without repeating frontend generation.
python -m shared.top_orchestrator validation \
  --mailbox shared/mailbox/<run-id>/revision_001 \
  --remote-config shared/top_orchestrator/remote.local.json --ask-password
```

JSON overrides specific to validation:

| Setting | Default | Environment override |
| --- | --- | --- |
| `validation_script` | `server/validation/run_pipeline.py` relative to remote project directory | `CAPSTONE_VALIDATION_SCRIPT` |
| `validation_mode` | `system` (`system` or `modules`) | `CAPSTONE_VALIDATION_MODE` |
| `validation_jobs` | `1` | `CAPSTONE_VALIDATION_JOBS` |
| `validation_job_seconds` | `14400` (4 hours) | `CAPSTONE_VALIDATION_JOB_SECONDS` |
| `validation_timeout_seconds` | `14490` | `CAPSTONE_VALIDATION_TIMEOUT_SECONDS` |

`validation_script` also accepts an absolute server path. Ensure allocated CPUs
are appropriate if increasing parallel validation jobs. The timeout must exceed
the allocation wait plus validation job time by at least 30 seconds. Preflight
keeps its separate short time limits. Remote setup must provide validation tools
(for example VCS), rather than only backend tools.

The orchestrator automatically creates a unique workspace under
`<remote-project>/shared/validation_runs/run_<invocation-id>/`, including
`input/rtl/`, `input/specs/`, `work/`, and nested snapshot directories. Previous
workspaces and results are preserved; no manual directory preparation is required.
The SSH account must be able to create and write these directories.

`validation_workspace` (or `CAPSTONE_VALIDATION_WORKSPACE`) optionally selects an
explicit absolute path or a path relative to the remote project. Missing paths
are created automatically. Existing prepared workspaces are accepted only when
input/work contain no files and no previous runner, request, or reports exist.
Use a fresh path for each invocation, or leave this setting unset/null for automatic
unique paths.

`allow_validation_output_dirs` defaults to `true`, allowing the deployed pipeline
to create report and scratch directories. Setting it to `false` stops validation
before SSH. The deployed validation source folder is not modified.

The adapter freezes and verifies the local snapshot, uploads it and a small shared
wrapper into the run workspace,
then launches the deployed pipeline under SSH/Slurm. RTL and module-spec directories
are passed separately: the approved user configuration is preserved as provenance
but is not incorrectly treated as a module-level verification spec. Pipeline scratch
files stay in invocation-specific work directories. Snapshot hashes are checked
before and after execution; Python bytecode writing in the deployed source tree is disabled.

The local validation stage directory contains `validation.log`,
`validation_result.json`, `remote_validation_result.json`, `validation_request.json`,
`validation_input/`, `reports.zip`, and extracted `reports/verification_reports/`.
The adapter collects reports on pipeline failure too, if the remote wrapper was
able to finish. Setup/transfer failures or a killed job may have only the local log
and result. Incomplete remote jobs receive a best-effort cancellation request;
remote inputs and reports are retained for diagnosis.

Success requires matching invocation/snapshot provenance, unchanged input hashes,
a complete report archive, and `PASS` evidence for agents 1, 2, and 3 across every
published design. Exit zero alone, `PASS_WITH_GAPS`, skipped stages, missing designs,
and untested requirements cannot produce success. Module verification failures
stop with reports. System Agent 2/3 failures after Agent 1 PASS enter debug when
the current frontend RTL matches the verified snapshot; repaired RTL is verified again.

**Scope:** verification covers the published expanded YAML designs. Generated
wrappers without matching YAMLs are listed in `modules_without_specs` and the final
message; a module-level pass does not establish independent wrapper/system coverage.
The existing frontend system lint and BIST still run before publication.
