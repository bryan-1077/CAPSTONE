# RTL Debug Automation Plan

## Goal

Build an automated post-generation RTL debug loop for failures from BIST, external verification, and PD-originated logic issues. For the near term, fixes are applied directly to `rtl_output/*.sv`. Later, recurring fixes can be analyzed and backported into the generation flow.

## Implementation Approach

Start with a small plain-Python `debug_agent.py` so the debug flow works before new workflow dependencies are installed. Keep each step as a standalone function that can later be wrapped as a LangGraph node.

The plain-Python baseline is now in place. Next, introduce LangGraph orchestration,
then add investigation capabilities, then enable automated repair retries.
Reuse the existing functions and CLI. LangGraph manages state and routing;
RTL inspection and simulation tools must supply the evidence. Use LangChain
components only where needed for model/tool integration, without a wholesale rewrite.

## Target Architecture

Near-term function flow:

```text
run_or_ingest
  -> parse_failure
  -> classify_failure
  -> localize_rtl
  -> write_patch_plan
  -> record_status
```

Target graph flow (introduced incrementally):

```text
run_or_ingest
  -> parse_failure
  -> classify_failure
  -> localize_rtl
  -> investigate
  -> assess_evidence
  -> build_patch_context
  -> propose_patch
  -> validate_patch
  -> apply_patch
  -> run_checks
  -> record_result
```

Conditional flow:

```text
insufficient_evidence and investigation_budget_remaining -> investigate
sufficient_evidence -> build_patch_context
checks_failed and repair_budget_remaining -> investigate with check feedback
lint_and_target_passed -> record_result
lint_passed_without_target -> record_result as behavior_unverified
budget_exhausted or evidence_unavailable -> record_result as needs_human
```

## Failure Sources

- `lint`: lint failures from full-system lint
- `bist`: built-in self-test failures from the generated testbench flow.
- `verif`: failures from the external verification subsystem.
- `pd`: PD tool reports that were classified as logic-owned issues.

Example commands:

```sh
python debug_agent.py bist --command ./run_sim.sh
python debug_agent.py verif --command ./run_verif.sh
python debug_agent.py pd --log reports/pd_report.log
```

## Debug Artifacts

Create one directory per failure:

```text
debug/
  fixtures/
  schemas/
  templates/
  failures/
    0001_short_failure_name/
      debug.log
      intake/
        metadata.json
        rtl_state.json
        command.txt
        raw.log
      analysis/
        parsed_failure.json
        classification.json
        suspected_modules.json
        patch_plan.md
      attempts/
        attempt_01/
          prompt.md
          response.md
          patch.diff
          validation.json
          apply.json
          checks.json
          apply.log
          lint.log
          target.log
          metadata.json
          status.json
      result/
        fix.md
        status.json
```

Each failure record should preserve:

- failure source
- failing command or input log
- raw output
- parsed error/assertion information
- suspected RTL files/modules
- patch plan
- applied diff
- rerun results
- final status
- notes on whether the fix should eventually become a generator-flow improvement

## Agent Roles

### Failure Intake

Runs the failing command or ingests a provided log. Records exit code, raw logs, active RTL state, and failure metadata.

### Failure Classifier

Labels the failure type:

```text
syntax
elaboration
BIST assertion
data mismatch
coverage missing
protocol violation
PD timing logic issue
PD structural logic issue
unknown
```

### RTL Localizer

Maps symptoms to likely RTL files and modules. Uses logs, assertion text, known signal/module names, and RTL inspection.

### Patch Planner

Produces a concise repair hypothesis, target files, intended RTL change, expected behavior, and risks.

### RTL Patch Agent

Applies a small RTL patch to the suspected files.

### Regression Agent

Reruns the failing check first, then broader checks when available.

### Fix Recorder

Writes the root cause, final diff, checks run, pass/fail result, remaining risk, and future generator-improvement recommendation.

## Guardrails

- Only edit `rtl_output/*.sv` in the near-term automated repair loop.
- Do not edit `tb/` files unless explicitly allowed.
- Do not edit generated YAML, IR, or generator scripts during near-term repair.
- Do not change module ports unless required by the failure.
- Do not delete modules.
- Do not rewrite whole files without approval.
- Keep patches small and behavior-focused.
- Preserve synthesizable SystemVerilog.
- Save every log, plan, diff, and result.
- Stop after 3 failed repair attempts and mark the failure as `needs_human`.

## MVP Phases

### Phase 1: Analyze Only

Implement `debug_agent.py` with:

- command/log intake
- debug folder creation
- failure parsing
- failure classification
- suspected RTL file identification
- deterministic `patch_plan.md` generation
- no automatic RTL edits

Supported initial commands:

```sh
python debug_agent.py bist --command ./run_sim.sh
python debug_agent.py lint --command "python run_flow.py configs/user_input.yaml --cache"
python debug_agent.py verif --command ./run_verif.sh
python debug_agent.py pd --log reports/pd_report.log
```

### Phase 2: Controlled RTL Patching

Add:

- proposal-only RTL patch generation
- patch validation guardrail
- patch application
- one-step `repair` command for propose -> validate -> apply -> check
- default lint/structural check rerun after applying a patch
- target failing-check rerun after lint passes
- failed-patch rollback unless explicitly kept
- attempt tracking
- prior-attempt context included in retry prompts
- repair orchestration record
- `needs_human` status after the attempt limit is reached
- final `fix.md` and `status.json`

Current repair/autorepair loop:

Both graph backends now retry rejected patches and failed checks under
`--max-attempts`. Failed checks require verified rollback before retrying;
kept patches and rollback failures stop the flow. These retries reuse the
existing investigation and include prior attempt feedback in proposal prompts.
Re-investigation after a failed hypothesis remains future work.

Git validation, application, and rollback share a command builder that maps
frontend-relative paths to the repository root. Verbose Git output is retained,
skipped patches fail validation, and unchanged target hashes fail application
before lint or simulation. Mode changes are rejected along with file operations.

```text
repair
  -> propose attempt_N
  -> validate attempt_N
  -> apply attempt_N
  -> lint/check attempt_N
  -> if fixed: record fix and stop
  -> if checks failed and rollback passed: retry with previous attempt context
  -> if patch invalid and budget remains: retry with validation feedback
  -> if NO_PATCH, rollback failure, kept failed patch, or budget exhausted: stop
```

Near-term autorepair policy:

- Treat `--max-attempts` as the total attempt budget for a single repair run.
- Reuse previous attempt artifacts in the next prompt so the model sees rejected diffs, failed checks, and rollback outcomes.
- Retry only when the tree is clean enough to continue, which currently means failed checks were rolled back successfully.
- Stop immediately on `NO_PATCH`; repeating the same under-evidenced prompt is not useful.
- Stop immediately when `--keep-failed-patch` or `--demo-mode` leaves a failed patch in the RTL tree.
- Record `needs_human` after repeated invalid patches or failed checks consume the attempt budget.
- Keep the loop local to `rtl_output/*.sv`; generator backports remain a later mining step after a fix is proven.

### Phase 3: Richer Debug Context

Add:

- VCD parsing around failure timestamps
- signal-window summaries
- module dependency maps
- PD report schema support
- external verification report adapters

### Phase 4: Flow Improvement Mining

Analyze repeated RTL fixes and recommend changes to:

- RTL generators
- wrapper generation
- feature templates
- validation checks
- testbench generation

## Current Status

The BIST-centered debug loop has been proven end-to-end on controlled RTL
data-path bugs:

```text
capture failure
  -> classify as data mismatch
  -> localize likely RTL
  -> build focused prompt context
  -> propose RTL patch with LLM
  -> validate patch
  -> apply patch
  -> run lint
  -> rerun BIST
  -> record fixed status
```

Known working path:

```sh
python debug_agent.py bist --command './run_sim.sh --no-wave-prompt' --name <name>
python debug_agent.py repair debug/failures/<id> --target-command './run_sim.sh --no-wave-prompt' --demo-mode
```

Verifier JSON handoff path:

```text
debug/fixtures/verif_<name>/
  <module>.sv
  <module>.yaml
  validation_result.json
```

```sh
cp debug/fixtures/verif_<name>/<module>.sv rtl_output/<module>.sv
python debug_agent.py verif --log debug/fixtures/verif_<name>/validation_result.json --name verif_<name>
python debug_agent.py repair debug/failures/<id> --target-command '<optional verifier rerun command>'
```

For standalone verifier reports, `repair` now auto-selects a focused
single-file lint such as:

```sh
verilator --lint-only --Wall -Wno-fatal rtl_output/<module>.sv
```

Integrated BIST/lint failures still default to full generated-RTL lint.

The localizer now handles read-data mismatch failures by prioritizing
controller response/storage datapath signals such as `rsp_rdata`, `bank_mem`,
`service_bank`, `service_addr`, `service_wdata`, and `selected_req`.

## Near-Term TODOs

Implementation order: Stage 1 LangGraph foundation, Stage 2 investigation,
Stage 3 automated repair retries. The localization requirements below describe
Stage 2; they do not block wrapping the existing flow in Stage 1.

### Stabilize Automation Commands

- [x] Add a first-class noninteractive simulation mode to `run_sim.sh`, such as
  `--no-wave-prompt` or `NO_GTKWAVE_PROMPT=1`, so automated debug runs do not
  rely on overriding `GTKWAVE_BIN`.
- [x] Keep the existing plain-Python CLI stable as the baseline smoke-test path:
  `bist`, `lint`, `verif`, `pd`, `propose`, `validate`, `apply`, and `repair`.
- [x] Ensure target-check pass/fail handling continues to prefer explicit BIST
  markers (`TEST PASS`, `TEST FAIL`, `Errors: N`) over benign tool warnings.
- [x] Auto-select single-file Verilator lint for standalone external verifier
  YAML/RTL mismatch reports.

### Stage 2: Evidence-Based Investigation

#### Current Behavior and Limitations

A BIST heuristic is a rule that suggests where to investigate a symptom. It
does not establish the root cause. Today, `localize_rtl` combines diagnostic
file references, module-name matches, failure-term searches across RTL, and
hardcoded `LOCALIZATION_HINTS`. Read-data mismatch handling also boosts files
containing predefined signals and explicitly boosts `ddr4_controller_top.sv`.
These scores are ranking weights, not probabilities of a module being faulty.

There is some dynamic discovery through text matching, but no causal traversal
of signal drivers, port connections, or state dependencies. The patch context
defaults to the top three suspects, so an incorrect ranking can exclude the
actual faulty logic. A high-confidence failure classification does not imply
high-confidence root-cause localization. Controlled bug demonstrations prove
the exercised paths; they do not demonstrate coverage of unfamiliar bugs.

The previously proposed BIST heuristics should guide questions and evidence
collection, rather than assign ownership to a fixed module list:

| Symptom | Evidence to investigate | Why a fixed owner can mislead |
| --- | --- | --- |
| Ready/valid or stall | Producer valid, consumer ready, accepted transfers, and gates preventing progress | A downstream block or reset condition can stall an otherwise correct queue. |
| tRRD/tFAW timing | Accepted ACT events, bank/rank identity, counter/window updates, and issue gating | A correct tracker can receive incorrect events or have its output ignored. |
| Refresh | Request, acknowledgement, interval state, arbitration, and command issue | The timer can be correct while arbitration prevents refresh service. |
| Row hit/miss | Address decoding, stored open-row state, and ACT/PRE updates | A wrong address upstream can look like a bank-state failure. |
| Queue full/empty/order | Push/pop handshakes, occupancy, pointers, and transaction identity | Dropped or duplicated transfers outside the queue can resemble queue corruption. |
| Read-data mismatch | Expected/actual data, accepted write, address, bank selection, storage, and response timing | The response output is the observation point; corruption may originate much earlier. |

#### Investigation Flow to Build

1. Preserve the failing observation: assertion/check location, hierarchy,
   simulation time/cycle, expected/actual values, seed, and transaction identity
   where available. Keep raw evidence when a parser does not recognize a format.
2. Read the failing checker and resolve its observed signals into the design.
   Treat testbench and reference-model logic as possible causes too; read access
   to that evidence does not authorize weakening checks or changing expectations.
3. Build a structural index using a SystemVerilog parser or elaborator: instances,
   port bindings, signal drivers, assignments, and control/state dependencies.
   Record unsupported constructs and unresolved edges explicitly.
4. Trace backward from the observation through data and control dependencies.
   Sequential logic requires prior-cycle state and inputs; static connectivity
   provides candidates, not proof. Use bounded trace windows when available.
5. Rank candidates with evidence provenance. Separate direct observations,
   structural relationships, text matches, and heuristic hints. Cap hint boosts
   so repeated keywords cannot overwhelm stronger evidence.
6. Expand the inspected files and dependency depth when evidence is weak,
   candidates tie, or a repair fails. Budget each expansion and record what was
   omitted. An unknown failure class must still enter this investigation path.
7. Form competing hypotheses and gather distinguishing evidence through targeted
   traces or reruns. Request missing evidence or report `needs_human` when the
   budget is exhausted; do not force an unsupported patch.
8. Validate a repair with lint, the original failing check, and relevant regression
   tests. Lint alone cannot establish a behavioral fix. Preserve the original
   checker and report exactly which checks ran.

#### Implementation Priorities and Acceptance

- [x] Keep localization scoring explainable in `suspected_modules.json`.
- [ ] First separate evidence from hints, bound hint contributions, and expose
  localization uncertainty independently of failure classification.
- [ ] Add checker context and a structural RTL index; replace fixed symptom-to-file
  routing with driver and dependency traversal across module boundaries.
- [ ] Expand focused snippets to include relevant assignments, control conditions,
  and state updates discovered through that index.
- [ ] Add bounded context expansion and evidence collection before further repair
  attempts, including an explicit path for unrecognized failures.
- [ ] Evaluate with held-out injected faults across upstream logic, reset, timing,
  queue control, and testbench expectations. Keep these out of heuristic tuning.
- [ ] Include renamed-module/signal cases and bugs outside the initial top three
  suspects. Measure whether the faulty location reaches the inspected context,
  whether expansion recovers it, and whether the original check passes afterward.
- [ ] Compare localization with hints enabled and disabled to expose dependence
  on known names. Add real unforeseen failures as regression cases when available.

These are planned capabilities, not features already implemented. Prioritize
evidence quality and search expansion before adding more symptom-specific rules
or automating repeated patch attempts.

### Stage 1: LangGraph Foundation

- [x] Wrap the existing debug-agent functions in LangGraph while preserving CLI
  behavior and the standalone Python baseline.
- [x] Define shared state for failure identity, source, evidence references,
  classification, suspects, hypotheses, inspected files, attempts, check results,
  budgets, and stop reason. Store large logs and traces as artifact references.
- [x] Add resumable checkpoints tied to the failure workspace and RTL revision
  or content hashes. On resume, detect changed inputs and avoid blindly replaying
  patch application or treating stale checks as current results.
- [x] Start with one repair attempt using the existing nodes:

```text
run_or_ingest
  -> parse_failure
  -> classify_failure
  -> localize_rtl
  -> build_patch_context
  -> propose_patch
  -> validate_patch
  -> apply_patch
  -> run_checks
  -> record_result
```

- [x] Initially record validation/check failures and stop; automatic repair
  retries are enabled in Stage 3 after investigation can use the feedback.
- [x] Preserve all existing artifacts (`prompt.md`, `response.md`, `patch.diff`,
  `validation.json`, `checks.json`, `fix.md`, `status.json`) as graph outputs.
- [ ] Verify parity with the baseline on captured BIST and verifier fixtures,
  including interrupted/resumed runs and lint-only outcomes. Do not mark a repair
  behaviorally fixed without a successful target rerun.

### Stage 2: Investigation Graph Integration

- [x] Insert `investigate` and `assess_evidence` before patch-context construction.
  Start with checker inspection, RTL searches, and additional file reads.
- [ ] Extend inspection with parser/elaborator-backed connectivity and driver
  tracing, then waveform inspection and targeted simulation reruns.
- [x] Give each assessed hypothesis supporting evidence, contradictory evidence, and a
  next inspection that could distinguish it from alternatives.
- [x] Persist investigation actions, results, unresolved questions, and omitted
  context. Bound tool calls, context size, and reruns separately from patch attempts.
- [x] Define evidence assessment using source locations and an explanation of
  how the suspected logic causes the observation; a heuristic score alone is
  insufficient. Record uncertainty even when proceeding to a testable patch.
- [x] Route newly collected evidence back to assessment, never directly to patch
  application. Stop with missing-evidence details when progress is impossible.

The initial integration lives in `debug_investigation.py` and `debug_agent.py`.
Graph `--repair` now requires investigation; `--investigate` runs the investigation
without patching. Plain graph analysis and standalone CLI modes retain their
existing routing. `--investigation-steps` (default 8) bounds read/search calls;
`--investigation-context-chars` (default 64000) bounds collected source text.
The first action is a bounded triage bundle (up to six source reads): the complete
primary suspect RTL file when it fits half the evidence budget, compact excerpts
around distinct failures, and matching testbench tasks/functions with nearby
callers and helper definitions (up to 12000 characters per checker). Oversized
RTL files fall back to focused logic with explicit omissions. Checker selection
is lexical, not a full SystemVerilog parser; missing context remains available
through read/search. Subsequent actions are individual reads or searches.
Reads/searches add at most 4000 characters per action, deduplicate overlapping
source lines, and retain partial excerpts with original line numbers. Non-RTL
evidence can occupy at most 40% of the total budget. Searches can specify catalog
paths to avoid unrelated files. Raw logs and testbench sources remain unchanged.
Assessment prompts reconstruct collected lines in source order, marking gaps,
while preserving original evidence IDs/ranges for citation validation. One
supported causal diagnosis may authorize a testable proposal even when separate
symptoms remain unresolved. It does not relax validation or authorize speculative
changes for the other symptoms. Automated multi-attempt repair remains Stage 3;
this change does not implement a repair/reinvestigation loop.
Progress reports both actions and characters; context and action exhaustion have
separate stop reasons. Restart older exhausted investigations without `--resume`
to use the new first-pass collection strategy.

Search selection prioritizes unseen assignments and relevance before other
matches, rather than repeatedly taking the first declarations in a file.
Search results record selected and remaining match locations for follow-up reads.
The workspace `debug.log` records inspection requests, result status, evidence
IDs, source ranges, added character counts, search coverage, hypotheses,
uncertainty, open questions, and the next queued actions. Exact source excerpts
remain in `analysis/investigation.json`; each assessment saves its prompt and
response. `assessment_NNN.api.json` records HTTP status, finish reasons, token
usage, and returned content length without credentials. Failed assessments also
write `assessment_NNN.error.json`. Empty API responses are execution failures,
not evidence-sufficiency decisions.
Assessment calls are limited to the inspection budget plus one, and investigation
reruns are currently disabled (budget zero). Resume retains consumed budgets.

`analysis/investigation.json` stores actions, line-numbered evidence, content
hashes, hypotheses, uncertainty, omitted context, and unresolved questions.
Online assessments also save their prompts and responses. Evidence sufficiency
is a model judgment with validated citations to current RTL and observation.
Behavioral failures also require checker source. Structural compiler failures
may instead cite a parsed compiler error in the captured log; compiler source
is not required. Mixed compiler/behavioral failures retain the checker requirement.
This is not proof of correctness. Offline mode gathers evidence
and stops with `needs_human`. Changed evidence blocks patch context/application.

Verification: `python -m unittest -v test_debug_investigation`. A real LangGraph
offline run on the verifier fixture reaches `needs_human` without a patch attempt.
Live model investigation and a successful behavioral repair remain unverified.

Stage 2 routing:

```text
investigate -> assess_evidence
insufficient_evidence and budget_remaining -> investigate
sufficient_evidence -> build_patch_context -> propose_patch
evidence_unavailable or budget_exhausted -> record_result as needs_human
```

The end-to-end entry point is now:

```bash
python debug_agent.py auto bist \
  --command './run_sim.sh --no-wave-prompt' \
  --name injected_bist \
  --repair
```

`auto` captures the command, starts the graph, preserves the command as the
target rerun, and records the resulting failure workspace. Use `--investigate`
instead of `--repair` to stop after evidence assessment.

### Stage 3: Multi-Attempt Autorepair

- [ ] Extend `repair` or add `autorepair` so one command can loop through multiple
  attempts up to `--max-attempts`.
- [ ] Include prior patch diffs, validation failures, lint logs, and target logs in
  the next investigation. Reassess hypotheses and expand context before retrying
  a failed behavioral repair, rather than repeating the same file ranking.
- [ ] Route invalid patches back to proposal with validator feedback under a
  bounded retry count; route lint/target failures through investigation.
- [ ] Track the exact RTL state tested by each attempt and explicitly retain or
  roll back agent-owned edits before the next attempt, preserving user changes.
- [ ] Stop automatically when lint and the target check both pass.
- [ ] Mark `needs_human` when repeated attempts fail or the patch validator rejects
  all attempts.

### Defer Until Examples Are Available

- [ ] PD report schema support should wait for representative PD logs.
- [ ] External verification adapters should wait for representative verif command
  output or report files.
- [x] Avoid overfitting parser/localizer behavior to imagined PD or verif formats.
