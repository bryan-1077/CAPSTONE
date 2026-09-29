# Top-level orchestrator skeleton

Run from the repository root using Python 3.10+. Frequency selection uses PyYAML;
frontend execution and optional frequency advice need the existing frontend dependencies.

```bash
# Start at interactive spec selection, then run generation and downstream stages.
python -m shared.top_orchestrator
# Equivalent: python -m shared.top_orchestrator generate

# Inspect the generation command and route without writing files or calling models.
python -m shared.top_orchestrator generate --input local/frontend/configs/user_input.yaml --cache --plan

# Execute generation, then stop at the validation placeholder.
python -m shared.top_orchestrator generate --input local/frontend/configs/user_input.yaml --cache

# Bypass the frequency prompt with an explicit backend target.
python -m shared.top_orchestrator --input local/frontend/configs/user_input.yaml --target-mhz 230

# Inspect an existing frontend failure workspace without model calls.
python -m shared.top_orchestrator debug --failure-dir /absolute/path/to/failure_workspace --offline

# Run the existing debug repair graph, then require validation.
python -m shared.top_orchestrator debug --failure-dir /absolute/path/to/failure_workspace --repair

# Exercise either placeholder without running frontend tools.
python -m shared.top_orchestrator validation
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
| `nodes.py` | Frontend subprocess adapters and empty validation/backend adapters |
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

Validation and backend return `not_implemented`; neither performs checks nor
claims success. The future success route is validation -> backend -> complete.
The direct backend entry currently only exposes the placeholder; connecting it
requires enforcing the validation gate described below first.

Each execution creates `runs/<id>/state.json` plus frontend subprocess logs. Later stages and retries get numbered
subdirectories so earlier evidence is preserved.
Use `--run-dir` to select a new output directory. Existing directories are rejected.
Exit codes: 0 = complete (or plan printed), 2 = incomplete/stopped or CLI misuse,
1 = run-artifact filesystem error. No real end-to-end run can complete yet.

State files are diagnostic records, not resumable checkpoints. The frontend still
writes into its existing shared output directories; run one workflow at a time.
An interrupted process may leave state marked `running`. Frontend prompts and output appear live in the terminal and are also saved to
the stage log. Terminal input is passed directly to the frontend.

## Planned integration contracts

Before connecting the remaining stages, extend the shared contracts with a design
bundle: RTL filelist/manifest, top module, configuration/specification paths,
testbench inputs, and a revision hash covering the relevant file contents. Stage
results should identify the input revision, report paths, failure category,
changed artifacts, and the original check command needed to reproduce a failure.

**Validation node:** consume the design bundle and validation configuration;
run the required structural and behavioral checks; return a structured report
with per-check pass/fail/not-tested status and evidence. A pass must mean all
required checks passed for that revision. Normalize repairable failures into the
debug agent's intake contract (`lint`, `bist`, or `verif`). Static checks alone
must not count as behavioral verification.

**Backend node:** consume that same validated revision, timing/physical constraints,
and tool configuration. Return output artifact paths, tool reports, and whether
the required implementation checks passed. Classify failures as RTL defects,
constraint/configuration issues, tool/environment failures, or unmet implementation
targets. Only supported RTL defects should feed frontend debug (`pd` intake).

**Routing and lifecycle:** validation/backend -> debug edges are implemented for
failed results carrying a `FailureReport`. The adapters still need to produce those
reports from real tools. After any RTL repair, invalidate earlier
validation/backend results and run validation again before backend. Reject stale
or absent validation evidence, including for direct backend entry. Keep debug's
internal attempt budget separate from a bounded top-level repair-cycle budget.
Persist revision fingerprints before adding resume or caching. Tool/environment
errors should have bounded retries; ambiguous requirements should stop for input.

Suggested implementation order: design bundle, validation adapter, revision gate,
backend adapter, then resume.

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

The future backend adapter can use `backend_command(config, mailbox_revision)` to pass the saved
value as `app.py --mailbox <revision> --target-mhz <value>`. That command builder rejects absent targets;
backend execution and its validation gate remain unconnected.

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

For validation/PD integration we still need the executable entry point, working
directory, required inputs, report format, and pass/fail criteria. Return a failed
result with this report to enter debug. Placeholders remain `not_implemented` and
never trigger debug. Failures without a report stop with their recorded evidence.

`--max-repair-cycles` defaults to 2 across the whole run; set it to 0 to stop at
failures. Each debug call separately retains its internal repair-attempt budget.
Unsuccessful debug stops the run; exhausted top-level cycles report
`needs_attention`. After successful debug the generation node runs in checks-only
mode: it does not rerun spec selection or regenerate RTL.
