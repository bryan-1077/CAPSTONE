# RTL Debug Automation Plan

## Goal

Build an automated post-generation RTL debug loop for failures from BIST, external verification, and PD-originated logic issues. For the near term, fixes are applied directly to `rtl_output/*.sv`. Later, recurring fixes can be analyzed and backported into the generation flow.

## Implementation Approach

Start with a small plain-Python `debug_agent.py` so the debug flow works before new workflow dependencies are installed. Keep each step as a standalone function that can later be wrapped as a LangGraph node.

LangGraph/LangChain can be added after Phase 1 if the graph abstraction is still useful. Do not make them required for the first working debug loop.

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

Later graph flow:

```text
run_check
  -> parse_failure
  -> classify_failure
  -> localize_rtl
  -> plan_patch
  -> apply_patch
  -> rerun_check
  -> record_result
```

Conditional flow:

```text
pass -> record_result
fail and attempts < max_attempts -> localize_rtl
fail and attempts >= max_attempts -> mark needs_human
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
