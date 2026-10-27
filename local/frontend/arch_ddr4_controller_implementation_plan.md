# DDR4 Controller — Implementation Plan

Build a specification-driven generator for a functional DDR4-like controller.
New capabilities should enter through design requests and specifications, without
feature-specific branches in Python expansion or wrapper generation.

## Current handoff — 2026-10-26 (America/Chicago)

**Start here in the next session.** This section supersedes older "next step"
statements below. The detailed sections retain historical implementation and
validation evidence; their test counts are selected milestone suites, not one
current full-repository test result.

### Goal and present capability

The goal is a repeatable, dynamic frontend for varied specifications, not a
manually repaired DDR4 RTL example. The intended path is:

```text
request → system specification → architecture/contracts/verification bindings
        → compiler checks and bounded edits → semantic review/readiness
        → approved RTL generation → structural integration lint → handoff
```

Automated semantic review may authorize RTL when all readiness checks pass;
the user explicitly authorized that policy. Downstream UVM/vector execution,
BIST and backend execution are outside this frontend run. Keep work in small,
verified sections. The user runs paid API commands; do not start paid retries
merely to inspect a failure. Do not add agents, more rounds, RTL chunking or a
large JEDEC rule dictionary to address the current failure.

**Infrastructure exists, but reliable live completion remains unproven.** Some
earlier artifacts reached RTL generation for a few modules. This repaired dynamic
controller path has not completed semantic approval, RTL generation and integration
lint. Offline passing checks are not target behavioral verification or proof of
repeatability across specifications.

### Implemented and verified in the recent sections

- [x] Revision-bound source/export validation, approval/readiness enforcement,
      existing RTL generators and typed downstream handoff infrastructure.
- [x] Module/flow token and estimated cost accounting, stage banners, conservative
      TPM pacing, reviewed-RTL cache/resume and saved failure/attempt diagnostics.
- [x] Lossless context sharing and stage-specific prompt/schema guidance.
- [x] Aggregate independent ownership/dependency, functional wiring and explicit
      verification-output declaration errors where prerequisites permit it.
      Invalid partial graphs do not produce misleading coverage/cycle conclusions.
- [x] Derive omitted contract invariant/verification references from explicit
      rule owners and locked rules. Derive omitted obligation reverse references
      from explicit rule-to-verification selections. Explicit inconsistent lists
      remain failures; published/hand-authored source validation stays strict.
- [x] Semantic review excludes historical compiler repair diagnostics from current
      evidence, while preserving full source provenance and fingerprints.
- [x] Review classification separates design_repair from external_agreement.
      External-only cases stop before generation; only repairable system findings
      select system regeneration. Normal draft/RTL approval is not an external
      prerequisite; the review prompt states this and lists eligible blocker paths.
- [x] Bounded explicit architecture edits and system semantic edits. System edits
      preserve unmentioned fields, reject bulk replacement of existing nonempty
      objects, protect request text/origin and external agreement, and journal
      exact applied changes. Fresh system generation still uses complete responses.
- [x] Repair-focused final review uses the existing single final call. Every input
      design finding requires an explicit assessment; preservation regressions and
      unresolved findings must link current design findings and block approval.
      Target-bound repair_review_context.json is saved and rechecked for approval.
      Standalone --repair-context retains those obligations on explicit re-review.

Current identities: generator revision **18**, semantic review protocol **4**,
repair-review context version **1**. No new protocol bump is needed merely to
continue testing these already implemented mechanisms.

Recent validation milestones: 74 selected generation/edit/audit/prompt checks
passed for system edits; 65 selected repair-review/repair/system-edit/standard
review checks passed for repair-focused review. Compilation and whitespace checks
passed. Neither milestone demonstrates live model adherence or behavioral proof.

### Latest live run: structural check diagnostics exhausted the repair budget

The run directory is named in UTC (20261027T023137Z); this occurred on October 26
in Central Time. Define the following paths for inspection, not as a paid command:

```bash
accepted_parent="local/frontend/microarch/proposals/clarity_20261024T055520Z/repair_20261025T214055Z/frontend_20261025T232303Z/repair"
latest_attempt="$accepted_parent/repair_20261027T023137Z"
```

- Parent export/review: `$accepted_parent/architecture.yaml` and
  `$accepted_parent/review/report.json`. This parent is **structurally valid but
  semantically rejected**, with the known refresh-drain deadlock. Do not mistake
  it for an approved baseline.
- Latest evidence: `$latest_attempt/source.attempts.json`,
  `$latest_attempt/source.failure.json`, `$latest_attempt/repair_status.json`
  and `${latest_attempt}.log`.
- Usage: `microarch/usage/20261027T023138Z_2ccdbb28d556/usage.json`.
  Four calls, input 77,948, output 8,696, total 86,644; estimated cost $0.260248.

Observed sequence:

1. System semantic repair passed on attempt 1 using **11 explicit edits**. It
   removed the pending-low restriction from active/cleanup creation and clarified
   corresponding drain/priority statements and expected results. This is compiler
   acceptance of the edited system, **not final semantic approval**.
2. Architecture attempt 1 included two invalid input-dependency references:
   waiting_queue.enqueue_ready named output head_valid, and
   command_engine.commit_valid named output candidate_valid. Both were repaired
   on attempt 2. Internal registered state is not a module input dependency;
   express internal-state gating in behavioral contracts instead.
3. The first architecture response already omitted `events` in four check objects:
   V_CONFIGURATION, V_EXPORT, V_IDENTITY and V_ISOLATION. The binding validator
   reported only the first omission. Attempt 2 fixed V_CONFIGURATION; attempt 3
   fixed V_EXPORT, then failed on V_IDENTITY. V_ISOLATION remained hidden behind it.
4. Three architecture attempts were exhausted. No new source/package.yaml or
   architecture export was published for this attempt. No repair_review_context.json
   was produced, no final repair-focused semantic review ran, and no RTL ran.

The latest failure is an **incomplete diagnostics bottleneck**, not evidence of
another system-edit regression, TPM exhaustion or a provider timeout. It does
not establish that the updated system is behaviorally correct either.

### Next bounded implementation task — do this before another paid run

- [ ] In [verification_bindings.py](verification_bindings.py), aggregate independent
      verification-check shape errors across all entries instead of stopping at
      the first `fields(check, ...)` failure. Preserve prerequisites; do not run
      relationship checks on malformed entries or coerce invalid data.
- [ ] Add a saved regression projection from the latest first architecture
      response: expose all four missing-events errors together, alongside the
      independently checkable dependency errors. Verify stable diagnostics, no
      input mutation, unchanged rejection and no extra attempts/model calls.
- [ ] Clarify generation guidance: `events` is a required field but `events: []`
      is already allowed when observations supply the check's evidence. Do not
      invent unrelated event triggers to satisfy configuration/identity checks,
      silently fill missing fields, or relax reference/coverage validation.
- [ ] Run the focused binding/diagnostic/edit/generation compatibility checks.
      Only then perform one bounded live repair test and inspect its artifacts.

The previous live run added refresh_due as an event anchor to V_CONFIGURATION;
this satisfied shape validation but should not become a default compiler rule.
The existing validator requires at least events or observations, not a nonempty
events list for every check.

### Remaining milestones and unresolved facts

- [ ] Demonstrate a compiled repair reaching the new repair-focused review and
      resolving its input findings without preservation regressions.
- [ ] Explicitly disposition A_PROFILE/Q_PROFILE_PROVENANCE. They remain recorded
      external timing-library agreement blockers. Do not clear them by model
      repair, treat numeric values as proof of authoritative provenance, or confuse
      them with the ordinary Q_RTL_APPROVAL gate. This is not yet resolved.
- [ ] Complete a frontend-only run through RTL, integration lint and handoff;
      retain strict admission checks and existing retry limits.
- [ ] Establish repeated success on different specifications, not only DDR4.
- [ ] Consider specification-stage checkpoints/resume after the current convergence
      work. RTL resume exists; accepted intermediate system/failed architecture
      attempts are **not** automatically resumed by the current specification flow.
- [ ] Decide how to consolidate independently confirmed findings across separate
      reviews. Current carry-forward covers only the selected input review for a
      repair, not a global history. Earlier commit-payload observability concerns
      on the 214055Z source were confirmed but have no recorded explicit resolution;
      recheck them on any new architecture rather than infer resolution from silence.
- [ ] Keep sourced/versioned timing/common-rule additions deferred until the first
      complete frontend run, as recorded below.

Known repair-preservation evidence: the 232303Z full system rewrite fixed dispatch
eligibility but wrongly gated active/cleanup creation by pending refresh, causing
deadlock. The offline audit recorded 24 changed fields and three unrelated removed
type-description fields. See [the audit](microarch/reviews/repair_20261025T232303Z_change_audit.md).
Explicit system edits and repair-focused review were added afterward; the latest
live run exercised the edits but stopped before exercising that final review.

Key implementation entry points: [generate_specification.py](generate_specification.py),
[specification_edits.py](specification_edits.py),
[verification_bindings.py](verification_bindings.py),
[review_specification.py](review_specification.py),
[repair_specification.py](repair_specification.py),
[repair_review.py](repair_review.py), and
[specification_change_audit.py](specification_change_audit.py).
Detailed history and usage are in [architecture_flow.md](microarch/architecture_flow.md)
and [automated_review.md](microarch/automated_review.md).

## Deferred until a full frontend run completes

- [ ] Audit the existing timing/command libraries, then incrementally add sourced,
      versioned standard-derived facts and reusable common behavioral rules that
      the compiler can resolve/check. Keep standard facts, project conventions and
      user requirements distinct. Require evidence that each rule prevents a real
      failure; preserve dynamic decomposition. Do not build a comprehensive JEDEC
      dictionary before completing the current request-to-RTL frontend flow.

## 1. Specification and Generation System

- [x] Add simple terminal stage headers for system specification, architecture
      decomposition, semantic review, compiler lowering, integration lint and
      verification handoff. Preserve existing per-module dual-LLM/FSM banners.

### Starting point (historical)

The original flow selects predefined features in `run_flow.py`, expands templates
in `expand_spec.py`, and adds controller behavior in `generate_wrapper.py`.
The opt-in LLM flow already supports custom module specs, graph validation,
module YAML export, structural integration, and isolated builds. Its missing
pieces are a coherent system specification, an enforced review boundary, and
controller behavioral verification. Existing artifacts do not establish success
for the target below.

Reuse `generate_microarch.py`, `architecture.py`, the existing IR/RTL routes, and
optional templates/timing profiles. See [microarch/architecture_flow.md](microarch/architecture_flow.md)
for the current implementation and commands.

Target flow:

```text
Design request + constraints + optional compatibility preset
    → elaborated system specification
    → module contracts + architecture graph
    → consistency checks + semantic review/readiness
    → approved specs → IR → RTL + structural integration
    → RTL + specification/verification-intent handoff
    → downstream UVM + vector verification → requirement results
```

Scope boundary: `local/frontend/` owns dynamic specification/architecture
generation, consistency checks, review, RTL generation, and the verification
intent exported with that RTL. After RTL generation, the existing downstream
verification stage performs UVM and vector testing outside `local/frontend/`.
Its implementation is outside this frontend task. Frontend compiler regressions
and generation lint remain local checks; they do not replace downstream testing.
Generate architecture-specific verification intent from system behavior and
contracts, using shared definitions and requirement IDs, without per-feature
Python test branches. Define the handoff before adding another executable
behavioral-verification engine to the frontend.

Frontend preparation validation on 2026-10-15: all 78 architecture, package,
lowering/approval, handoff and staged-generation regressions passed. The final
staged validator adjustment also passed its focused 14-test suite. The supplied
draft_005 review export (`a8d815ca95b23bc5`, manifest `cc709aaf0b96ac0c`)
passed handoff validation. No live target specification/RTL generation, target
approval, downstream UVM/vector execution or backend run is recorded by these
checks. The next frontend milestone is a live specification proposal, concrete
review/approval, and target RTL generation/lint with cache replay.

Live attempt on 2026-10-15 (`live_20261015T185021Z`): the initial system-specification
model call failed with ConnectionError under the network sandbox. The requested
network escalation was declined, so execution stopped before either specification
stage completed. No generated target package, approval, or RTL resulted. The log
is `microarch/proposals/system/live_20261015T185021Z/frontend.log`; live generation
and the first target RTL build remain pending.

The user's subsequent terminal run reached Terra, but the system stage exhausted
its attempts on contradictory format-example fields and an opaque reset-style
check. Its final model-reported unsupported result incorrectly treated optional
mixed-style reuse as mandatory. Fixes on 2026-10-16 preserve the request and
baseline requirements: stage-specific response examples, canonical `synchronous`
reset style with legacy alias support, field-specific repair diagnostics, explicit
conditional-reuse guidance, and full response journals for rejected attempts.
Generator revision 2 and lowering revision 3 invalidate stale proposal/export
caches. A completed live package and RTL build are still pending.

Follow-up validation on 2026-10-16: all 81 frontend regressions passed, including
canonical/legacy reset lowering, stage-specific prompt examples, controller
baseline preservation and rejected/unsupported response journals. Python
compilation and diff whitespace checks passed. The live command can be retried
unchanged; the corrected prompt and compiler context select a fresh source cache.
This offline result does not establish completion of the next live generation.

The next user-run live attempt completed both specification stages on their third
attempts and exported `/tmp/ddr4-frontend-review/architecture.yaml`. The generated
source is `microarch/proposals/system/48fdba3e96f7670e/package.yaml`; its manifest
fingerprint begins `4b1010039a077247`. It has eight modules, 43 rules and 32
obligations. Source reproduction and review-handoff checks passed. The initial
default remains four banks/four waiting/FIFO/open-page/DDR4-3200. The generated
package declares four capacity/page-policy configurations, separate from the
hand-authored example's 36-case matrix. Technical inspection is recorded beside
the export in `frontend_inspection.md`; no target approval or RTL is recorded.
The generated package's four declared configurations passed structural checks and
source compilation exactly reproduced the export. Ten focused approval/build/
handoff regressions passed after adding explicit review-question confirmation,
including version-1 compatibility and stale/unknown/incomplete approval rejection.

Latest user-run RTL build on 2026-10-16 (`4b1010039a077247`) records an
approved generated package, successful host-adapter and waiting-queue generation,
and failure at FIFO-scheduler review. Single-file lint passed; the reviewer found
that implicit clock/reset inputs were missing from the authoritative port list,
and that a blanket signal prohibition conflicted with internal implementation
freedom. The three rounds did not establish a clean scheduler review; integration
lint and verification handoff did not run. This supersedes the earlier no-approval
status for the generated `draft_003` export, not the separate supplied `draft_005`.

- [x] Normalize the generator/reviewer interface to include declared clock/reset
      inputs and permit internal helpers within owned behavior.
- [x] Keep the three-round bound; stop on specification ambiguities and skip a
      final merge that cannot receive another reviewer pass.
- [x] Save full per-round reviewer reports and candidate RTL for module CLI runs.
- [x] Print provider-reported input/output/total usage after each module and flow,
      including failed runs, retries, and specification generation; persist usage
      reports shared across module subprocesses. Missing usage stays unknown.
- [x] Add per-module and flow USD cost estimates using the user-provided Opus
      input/output rates ($5/$25 per million) and Terra rates ($2/$12 per million).
      Include retries and reviews, save pricing and cost totals in usage reports,
      and retain unknown totals with known subtotals when usage/rates are missing.
      All 12 focused usage/review regressions passed after this addition, including
      mixed-model pricing, subprocess/retry aggregation, failure subtotals and
      zero-cost cache reuse. No live model calls were made for this change.
- [x] Reduce repeated prompt context through a generic, lossless shared-definition
      serializer; preserve complete contracts and future metadata without feature
      branches, prose summarization or normative-field filtering.
- [x] Send constraints once, remove invariant echo from generation/merge responses,
      retain compiler invariants, and avoid repeated RTL snapshots in round history
      and merges. Keep complete behavioral context in compact-review fallback.
- [x] Measure prompt characters per API call and regression-check exact context
      reconstruction. The saved eight-module comparison reduced context characters
      by 45.86%; provider-token/cost savings await a live run.
      Validation: 50 offline checks passed (nine prompt-preservation/size tests,
      13 dual-LLM API tests, 12 usage/cost/review tests and 16 existing lowering/
      approval tests). Python compilation and diff whitespace checks passed;
      no live model calls or target RTL regeneration were performed.
- [x] Print a blank line, module tokens, module cost and then FINISHED, across all
      successful generation routes; retain failure costs without a success banner.
- [x] Start code-generation/merge calls at the existing 16,384-token ceiling,
      retaining 8,192 for review and explicit environment overrides. Save truncated
      responses and raw usage details without accepting partial RTL.
      The latest reduced-context live run still stopped at command-engine
      truncation followed by HTTP 524; the revised initial budget awaits live
      validation and does not establish that server timeouts are resolved.
      Validation: 32 offline checks passed (16 API boundary tests, 14 usage/cost/
      review tests and two actual frontend generation/cache/lint smoke tests).
      Compilation and whitespace checks passed; no live API calls were made.
- [x] Add simple shared TPM pacing (2026-10-18): one 60-second reservation window
      across frontend API calls/models/subprocesses, default 80k estimated TPM,
      full output reservations, bounded logged waits, no early refunds or new
      network retries. Fail oversized requests before sending and log provider
      rate-limit headers; keep billing based on actual provider usage.
      Validation: 101 offline checks passed across pacing (12), dual-LLM API (16),
      usage/cost (14), debug API (15), architecture (27), and staged generation (17).
      Fake clocks avoid real pacing sleeps; subprocess tests exercise file locking.
      Live pacing behavior and the cause of HTTP 524 remain unconfirmed.
- [x] Clarify specification timing bindings as selected-library path strings and
      report the full offending field/type/value for numeric literals. Verify
      that the same reference resolves correctly across project timing profiles.
- [x] Compact specification request/context/locked-system JSON and include each
      previous repair response once without double encoding; preserve raw journals
      and complete repair data. Generator revision 3 refreshes proposal caches.
      The actual oversized repair now reserves 74,059 estimated tokens instead of
      82,444, under the unchanged 80k target and 24k output allowance. Reference-only
      correction of its saved system response passes offline system validation.
      Historical failure reports remain unchanged; live proposal retry is pending.
      Validation: all 45 selected offline checks passed (21 staged-generation,
      12 package and 12 TPM tests), including numeric-binding repair through both
      generation stages, cross-profile resolution and lossless repair feedback.
      Compilation and whitespace checks passed; no live API calls were made.
- [x] Separate initial-generation examples from repair schema guidance. Keep the
      complete request, locked system, schema/library and rejected candidate;
      share repeated request/system facts losslessly using the existing serializer.
      Require full corrected stage responses and preserve raw journals.
- [x] Make each named owner's invariant and full verification coverage explicit
      in initial/repair instructions. Preserve compiler validation and approval gates.
- [x] Regression-check both stages' initial and repair budgets with large-controller
      fixtures. The saved architecture repair now reserves 76,492 estimated tokens
      instead of 99,467 under unchanged 80k/24k budgets. Generator revision 4
      refreshes proposal caches. All 37 selected offline checks passed; no live
      API calls were made. Live proposal retry remains pending.
- [x] Aggregate independent architecture ownership and dependency errors into one
      repair report, alongside safe graph/metadata diagnostics. Preserve early
      schema stops and require clean validation before export or handoff.
- [x] Save structured diagnostics in attempt journals and repair provenance;
      clarify that combinational dependencies list declared inputs only. Generator
      revision 5 refreshes proposal caches; the three-attempt limit is unchanged.
      Offline replay of the latest first architecture candidate reports 15 errors,
      including both invalid dependencies, with a 74,481-token repair reservation
      under the unchanged 80k target. All 72 selected offline diagnostic, package,
      generation, prompt, lowering/approval and handoff tests passed.
      Live proposal retry remains pending.
- [x] Make requested rule text and origins compiler-owned at system-stage ingestion.
      The model declares original IDs and verification links; missing/renamed IDs,
      duplicate rules and invalid obligations still fail. Preserve raw responses and
      retain strict request/provenance checks after elaboration. Generator revision 6.
      The October 22 first response now passes system validation offline, changing
      only its 13 incorrectly qualified origins on a copy. All 47 selected offline
      generation, prompt, diagnostic and package checks passed. Live retry is pending.
- [x] Recover one extra trailing closing brace in specification responses only,
      logging the recovery and preserving raw journals. Strict parsing must succeed
      after that single deletion, followed by all existing validation. Saved October
      22 attempt 2 now passes system validation offline. Generator revision 7;
      all 54 selected offline JSON, pacing, generation, diagnostic and prompt tests pass.
- [x] Verify that the existing TPM override supports a reported 1,000,000 limit
      and still paces cumulative requests. Keep the 80k default pending account
      confirmation; a confirmed 1M allowance can use an 800k local target.
- [ ] Investigate the October 22 server error separately from semantic repair.
- [x] Clarify generation guidance for first creation versus replacement, blocked
      holds, simultaneous-event priorities, and payload behavior when valid is low.
      Use existing contract fields and preserve the complete-module RTL path.
- [x] Append target R12/R13 for candidate lifecycle and valid/payload separation,
      preserving R1-R11 exactly. Generator revision 9 refreshes proposal caches.
      Verify exact requirement transport through both stages and lowering. All 36
      distinct selected offline generation, prompt, link and approval checks pass.
- [ ] Generate/review a fresh package from R12/R13, check dependency/ownership
      semantics, record its own approval, then attempt one complete-module RTL run.
      Existing approved exports and their caches remain untouched; new global
      package fingerprints can invalidate reuse across revisions.
- [x] Make the dual-LLM client timeout configurable with DUAL_LLM_TIMEOUT_SECONDS
      (default 180); add probe --timeout override and use it for the stream guard.
      Reject invalid/nonfinite values before network activity. All 44 selected
      offline API, diagnostic and pacing checks pass. Live 600-second test pending.
- [x] Add a diagnostic for the exact initial generator request with a small output
      cap, elapsed time, usage/cost and rate-limit header reporting. Verify the saved
      command-engine prompt remains 52,379 characters and make no RTL/cache writes.
      Initial six offline checks passed. The live 512-token request returned HTTP
      200 in 8.6s with zero visible characters; detailed reasoning attribution is pending.
- [x] Extend the diagnostic with provider usage details and message field summaries,
      plus opt-in SSE streaming and first-header/body/reasoning/content timings.
      Save metadata in probe.json without reasoning text, prompt text or RTL output.
      All 37 selected offline diagnostic, pacing and usage checks passed.
      Gateway streaming behavior and the paired 2,048-token live tests remain pending.
- [x] Add explicit RTL `--resume`: reuse reviewed module caches keyed by exact
      generated YAML in the same build directory, recheck local lint, generate
      missing/invalid entries, and rerun integration lint. Keep `--cache` cache-only
      and preserve specification approval. All 39 selected offline checks passed.
      The actual interrupted build reused its first three modules at zero new API
      calls, then stopped on sandbox DNS restrictions. Network retry was denied;
      target RTL completion remains pending.
- [x] Aggregate system-stage reciprocal verification-link errors across both
      system and execution slices after schema validation. Keep malformed-schema
      stops and block decomposition until every link passes. Generator revision 8.
      Saved run review_20261022T170628Z now reports all three first-response errors
      together; the second reports two and the third still passes offline.
      All 47 selected offline link, generation, JSON, diagnostic and prompt tests pass.
- [x] Collect independent system metadata, fixed configuration-axis and reciprocal
      link errors together, retaining schema prerequisites and strict rejection.
      Generator revision 10. Saved clarity_20261024T042800Z attempt 1 now reports
      all five errors together; an explicitly corrected copy passes offline.
      Raw responses and historical proposal artifacts remain unchanged.
      All 79 selected offline checks, compilation and whitespace checks pass.
- [x] Make the architecture root JSON shape explicit and add targeted nesting
      repair feedback. Keep modules limited to contracts; internal_types, graph,
      verification and rule_owners are root siblings. Generator revision 11.
      Saved clarity_20261024T050035Z responses receive closure guidance for attempts
      1/2 and an exact modules.internal_types location hint for attempt 3.
      Malformed JSON remains rejected; no fields/braces are repaired automatically.
      All 49 selected offline checks, compilation and whitespace checks pass.
- [x] Review clarity_20261024T055520Z across shared events, execution expectations,
      contracts, bindings and approval readiness. All four matrix configurations
      and the exported architecture pass structural checks. Record semantic gaps
      and the A_PROFILE approval blocker in microarch/reviews/clarity_20261024T055520Z.md;
      structural PASS does not approve this revision for RTL.
- [x] Add a bounded, persisted semantic review report/check before RTL admission,
      using snapshot JSON pointers and unrelated register-design fixtures. User
      authorized automated review to allow RTL. Opt-in --automated-spec-review
      issues distinct version-3 revision-bound approvals on a clean verdict plus
      compiler readiness; malformed verdicts/findings/provider failures stop.
      Existing explicit approval and proposal-only defaults remain available.
      All 76 selected offline checks, compilation and whitespace checks pass.
- [x] Clarify automated-review reference coordinates after the first live review:
      expand shared prompt definitions and reference snapshot roots, not /context
      or /package/timing_library. Review revision 2 aggregates invalid references
      and retains parsed findings in failed reports. All 19 gate tests pass;
      saved live verdict reports all 15 invalid references without mutation.
      Scope guidance now explicitly routes shared-event/expected-result changes
      back to system elaboration. No live retry or automatic path repair ran.
- [x] Confirm live review revision 2 produces a valid changes_required report:
      clarity_20261024T055520Z/automated_review_v2 identifies two system and two
      architecture findings with valid references. Approval correctly remains
      blocked. One call used 19,899 tokens, estimated $0.069988; no RTL ran.
- [x] Add one opt-in semantic repair cycle (--auto-spec-repair) and a standalone
      repair_specification.py entry point for an existing validated report. Any
      system finding regenerates system/decomposition; architecture-only findings
      preserve the locked system. Recompile/review the new revision, with existing
      stage limits and no second semantic cycle. Preserve request/configuration,
      parent timing library and unresolved external agreement. No target-specific
      text patches, new dictionaries, agent framework or RTL fragment generation.
      All 103 selected offline checks, compilation and whitespace checks pass;
      saved controller findings select system routing. Live repair remains untested.
- [x] Fix fresh architecture generation after system repair: keep the stage-format
      example even with semantic feedback, distinguish fresh decomposition from
      candidate repair, and specify contract mapping/list and flat clock/reset
      field shapes. Generator revision 12. Collect independent contract-shape and
      graph clock/reset shape errors before relational validation. Saved repair
      20261024T072406Z now reports 17/9/1 errors across its three architecture replies.
      No coercion, additional rounds, source mutation or paid retry is introduced.
      All 126 selected offline checks, compilation and whitespace checks pass.
- [x] Support explicit graph.verification_outputs declarations backed by validated
      observations used in checks; reject input/top/absent/connected ports and
      preserve every functional sink/source coverage check. Lower to existing
      unused_outputs and retain typed handoff observations. Generator revision 13.
      Add repair guidance to preserve unrelated valid wiring, with no guessed
      connections or automatic controller patch. Existing exports still validate.
      176 distinct offline checks passed across focused/compatibility runs,
      including actual interface-fixture integration lint; no paid calls ran.
- [x] Use explicit object-field edits for parseable architecture compiler retries
      instead of regenerating the whole candidate. Bind edits to a candidate
      fingerprint, apply atomically, retain raw/applied evidence and rerun all
      existing compiler checks. Preserve full regeneration for malformed initial
      responses, existing attempt limits and semantic-review boundaries.
- [x] Diagnostic audit, first section: aggregate independent verification-output
      declaration errors after endpoint/connection/binding prerequisites pass.
      Explain that connected outputs remain observable without a verification-only
      declaration. Replay a committed graph projection from the saved 232854Z
      attempt: report all 13 conflicts together without changing the candidate.
      All 39 focused offline tests, compilation and whitespace checks pass.
      Broader graph diagnostics and bookkeeping changes remain separate work.
- [x] Diagnostic audit, second section: collect independent connection endpoint,
      sink-list, conditional-presence, direction, type/width and duplicate-driver
      errors. Stop before coverage/output-use/cycle checks on a partial invalid
      graph. Preserve prerequisite checks and valid-graph behavior. Seven new
      regressions include the saved duplicate dispatch-driver edge; all 46 focused
      offline checks, compilation and whitespace checks pass. No paid retry ran.
- [x] Ownership bookkeeping, first section: generated contracts may omit
      invariants/verification reference lists; derive them deterministically from
      explicit rule_owners and locked rule-to-obligation links. Keep supplied
      lists strict and published/hand-authored package validation unchanged.
      Keep behavioral intent, checks, observations and semantic approval explicit.
      Generator revision 15; all 113 selected offline tests pass. A read-only
      controller replay reproduces all eight original contract reference sets.
      System-stage reciprocal references and specification resume remain separate.
- [x] System verification bookkeeping, second section: rule verification IDs stay
      authoritative; derive omitted obligation.rules reverse references locally
      within each slice. Keep explicit lists, coverage, behavioral intent and
      published source validation strict. Generator revision 16. All 124 selected
      offline checks pass; a read-only controller replay matches 26 reverse-link
      sets without changing scenarios, expected results or status. No paid run.
- [x] Review context boundary: exclude historical generation validation_repairs
      from semantic-review input and reference resolution, retaining full saved
      provenance and artifact fingerprints. Review protocol revision 3 requires
      fresh review. The saved repaired response-wiring regression preserves
      current connections and external blockers while omitting stale diagnostics.
      All 37 selected offline review/repair tests, compilation and diff checks pass.
- [x] Separate repairable design findings from external agreement. Review protocol
      4 requires explicit resolution classification and validated blocker refs.
      External-only cases stop before generation; mixed architecture/external
      reports preserve the system and retain blockers. Real design system issues
      still select system repair. Preserve unresolved decisions as well as
      assumptions/questions during regeneration (generator revision 17).
      All 53 focused offline review/repair/prompt tests pass; no paid calls ran.
- [x] Clarify automated approval policy in review input: draft status and normal
      RTL approval questions are not external prerequisites. Render eligible
      blocker paths from validator metadata checks; preserve strict validation.
      Saved 223610Z classification regression retains the legitimate profile
      finding and rejects circular approval. All 47 offline review/repair tests,
      compilation and whitespace checks pass. No paid calls or source edits ran.
- [x] Repair preservation, first segment: audit the saved 232303Z system rewrite
      without changing live repair behavior. Record 24 exact system-projection
      differences and a focused regression fixture, including broadened active/
      cleanup guards and three removed type-description fields. Add a read-only
      JSON change utility with no behavioral inference or automatic repair.
      All 16 selected offline audit/edit tests, compilation and diff checks pass.
- [x] Constrain system semantic repair to fingerprint-bound explicit edits from
      its accepted parent. Preserve unmentioned fields, reject bulk object
      replacement and request/external-agreement mutations, record applied
      changes, and retain existing compiler/final-review checks and attempt limits.
      Saved preservation cases and generic real-compiler repair regressions cover
      the new path. Fresh system generation remains complete-response based.
      All 74 selected offline tests, compilation and whitespace checks pass;
      no paid calls or target source/export edits ran.
- [x] Repair-focused final review: carry every input design finding and exact
      change evidence into the existing single review call. Require explicit
      per-finding disposition and preservation assessment, current references and
      links to open/regression findings. Persist target-bound context and recheck
      it during approval/next repair. Add --repair-context for explicit re-review.
      All 65 focused offline tests, compilation and whitespace checks pass.
      No paid calls; merging unrelated historical reviews remains separate work.
- [ ] Aggregate independent verification-check shape errors and clarify required
      but possibly empty events lists, using the 20261027T023137Z saved failure.
      This is the immediate next task; see Current handoff above. Do not increase
      rounds or retry the paid flow before the diagnostic fix is verified.
- [ ] Complete the live target RTL build and integration lint with these repairs;
      downstream UVM/vector testing remains outside frontend scope.

Validation of these repairs: 106 offline frontend tests passed (67 architecture,
package, lowering/approval and handoff regressions; 17 specification-generation,
13 dual-LLM API and nine usage/review regressions). Python compilation and diff
whitespace checks passed. The actual failed scheduler YAML now yields eight
public ports including `clk` and `rst_n`. No live model calls or target RTL rerun
were performed during this repair.

### A. Define and review the specification format

Reviewed for implementation: [specification format](microarch/specification/README.md)
and [example package](microarch/specification/ddr4_fifo/package.yaml), current revision
`draft_005`, cover the first target's shared definitions, system behavior, all
eight module contracts, and typed connection graph. The user accepted the defaults
on 2026-10-15; [review findings](microarch/specification/ddr4_fifo/review.md) record
lifecycle, ownership, and intentional compatibility changes. Executable package
checks cover all 36 declared configurations. Contract-to-module-spec lowering and
revision-bound approval enforcement are now connected to the architecture build
route. Approval of the concrete first-target export and live RTL generation remain
pending; no behavioral obligations have been executed.
`draft_005` preserves accepted behavioral defaults, includes verification bindings,
and clarifies clock-driven candidate formation and refresh-pending observation;
the expanded export awaits
its own concrete review. Existing downstream system verification still requires
an adapter for the new architecture/semantics.

- [x] Draft an example package for the first controller target in Section 2.
  - [x] Draft the request/FIFO slice with package metadata, decisions, assumptions,
        and explicit unresolved questions.
  - [x] Complete the example with execution contracts and the connection graph.
- [x] Define shared types, widths, capacities, command encodings, events, and timing
      values once; reference them from module contracts.
  - [x] Define shared request types, widths, capacities, lifecycle events, and
        timing-profile references.
  - [x] Define command payloads and encodings; complete execution-event semantics.
- [x] Describe system behavior: request lifecycle, ordering, scheduling policy,
      command semantics, bank state, timing, refresh, and responses.
  - [x] Describe admission, dispatch, active ownership, retirement, FIFO ordering,
        backpressure, simultaneous queue events, and reset cancellation.
  - [x] Complete command, bank-state, timing, refresh, and response-edge behavior.
- [x] Define module contracts: owned state, interfaces, handshakes, reset, update
      events, stalls, simultaneous events, invariants, and verification obligations.
  - [x] Draft waiting-queue and FIFO-scheduler contracts with verification obligations.
  - [x] Complete host-adapter, command-engine, row-state, timing, memory, and refresh contracts.
- [x] Use structured fields for shared facts and rules; precise prose for algorithms.
      Do not introduce a complete hardware language as part of this milestone.
- [x] Review the example and schema before implementing the generation stages.

### B. Establish baseline and ownership rules

- [x] Turn `current_controller` into an explicit compatibility preset rather than
      a universal restriction. Initially preserve host ports, address mapping,
      acceptance behavior, data correctness, and response handling. Internal
      structure and command latency may change.
- [x] Default the preset to four waiting requests plus one separately stored active
      request. Allow explicit waiting-capacity changes in design requests.
- [x] Require separate owners for request storage, scheduling, command sequencing,
      timing enforcement, and functional memory/data handling.
- [x] Permit further splitting; require review before merging major responsibilities.
      Trivial helper modules must not disguise a monolithic core.
- [x] Keep top-level integration structural. Give functional glue an explicit module
      contract, and make major modules independently testable through their ports.

Implemented by `current_controller_v2`, configurable
`microarchitecture.waiting_capacity`, and the existing architecture role/wiring
validator and structural wrapper. Generic requests may select or omit the preset;
legacy configurations still attach it automatically. Named policies are request
constraints, not a Python policy allowlist. Proposals must be regenerated against
the new contract. These checks establish boundaries, not behavioral correctness.

### C. Generate and validate specifications

- [x] Elaborate the system spec before selecting module boundaries and connections.
  - [x] Add a two-stage request route (`run_flow.py --specification`): validate and
        lock system behavior first, then derive contracts, owner assignments,
        wiring and verification bindings. Keep the existing direct architecture
        proposal route available separately; neither replaces legacy defaults yet.
  - [x] Correct the first live run's prompt/schema mismatch and reset-style
        diagnostic loop; preserve failed responses for inspection and replay.
  - [x] Exercise live system/decomposition generation through the complete review
        export, with schema/ownership repair evidence preserved.
- [x] Distinguish requested constraints, model-selected decisions, assumptions, and
      unresolved questions. Preserve requirement IDs and traceability.
  - [x] Preserve these categories, rule IDs/origins and verification references in
        portable supplied-package exports and module contexts.
  - [x] Generate architecture-specific verification intent: properties, scenarios,
        observable events, expected results, assumptions, and coverage obligations
        for the downstream verification stage.
    - [x] Validate and export supplied intent and typed event/observation bindings
          from each package; retain explicit unbound events without RTL-name heuristics.
- [x] Derive module contracts from the system spec; assign each rule an owner and
      verification obligation. Resolve cross-module agreements before RTL.
  - [x] Preserve every request ID/text and explicit configuration constraint;
        require reciprocal owner/obligation links and exact system-stage identity.
        Allow additive internal interface types without replacing system types.
        Structural agreement is checked; behavioral prose still requires review.
  - [x] Lower supplied reviewed contracts into concrete module YAMLs and the
        existing architecture manifest, without inventing behavior or boundaries.
- [x] Validate shared references, types, widths, ownership, contract consistency,
      graph connections, drivers, clocks/resets, and declared dependencies.
  - [x] Add a read-only package checker with parameter/type elaboration, timing
        binding resolution, reciprocal rule/verification links, typed graph and
        declared dependency validation; test the full configuration matrix.
  - [x] Integrate package checks into generated-spec elaboration and the build path.
- [x] Keep templates optional. Extend generic compiler constructs when needed;
      do not add another predefined feature registration for each capability.
- [x] Report unsupported requests and required extensions. Never silently omit or
      weaken a requested capability, or label unverified behavior as passed.
  - [x] Require affected request IDs, reasons and extensions in unsupported reports;
        reject incomplete supported packages and omitted/rewritten requirements.
        Bounded repair, response replay and request/context-bound cache are available.
        These checks preserve traceability; they do not prove semantic satisfaction.

### D. Enforce review before RTL

- [x] Emit the system spec, module contracts, connection graph, decisions,
      assumptions, unresolved questions, and verification plan as one package.
- [x] Require user review and approval before RTL generation; resolve open decisions.
  - [x] Enforce an explicit approval artifact before any specification-derived
        build writes RTL; reject unresolved design decisions and stale approvals.
  - [x] Support explicit review-question confirmation in an exact-export approval
        without rewriting locked generated source. Keep other blockers active.
  - [x] Review and approve the concrete exported first-target package before its
        first live RTL build (generated `draft_003`; approval recorded in the build).
    - [x] Inspect lifecycle, ownership, timing, refresh, response and binding
          agreements; clarify engine progress between handshake events in draft_005.
    - [x] Supply the initial request/configuration: four banks, four waiting entries,
          FIFO, open-page, DDR4-3200; retain broader supplied-package matrix coverage.
- [x] Tie approval to the reviewed package revision. Contract changes require review
      again; routine implementation repairs within approved contracts may proceed.
- [ ] Let the LLM choose internal representations, helper modules, names, and
      algorithms within approved contracts. Bring changes to observable behavior,
      ownership boundaries, or requested guarantees back for review.

### E. Generate, verify, and adopt

- [ ] Feed the approved contracts into existing IR/RTL routes and manifest-driven
      integration. Preserve isolated outputs, provenance, and replay/cache support.
  - [x] Connect package/manifest CLI inputs, existing module IR/datapath routing,
        structural integration and the approval gate; exercise orchestration with
        offline fixtures and retain source/approval evidence in isolated builds.
  - [x] Exercise the real frontend-only cache/IR/RTL-file/lint/handoff path and replay
        with a small generated-specification fixture; test failed-rebuild handoff
        invalidation. This is transport/compiler evidence, not target behavior.
  - [ ] Exercise the approved first-target contracts with live RTL generation and
        reviewed module-cache replay.
- [x] Define and export a versioned frontend handoff for downstream UVM/vector verification:
      RTL/filelist, module/top interfaces, resolved configuration/types/timing,
      reset/handshake/event bindings, requirement IDs, and verification intent.
- [x] Export supplied module isolation scenarios and system acceptance scenarios for that
      handoff. Executable UVM/vector test generation and execution belong to the
      downstream stage outside this frontend task.
- [x] Base exported expected behavior on approved system contracts, independently
      of the generated RTL, so downstream monitors/scoreboards can check it.
- [x] Define requirement-result correlation with downstream passed, failed, or
      unverified evidence, separately from frontend specification validation, RTL
      generation, lint, and compilation. Downstream result production is external.
  - [x] Emit per-rule and per-obligation unverified records; never turn structural
        validation or generation success into behavioral verification success.
- [ ] Confirm downstream adoption of the new handoff. Existing system consumer
      v2 expects legacy configuration/TB/private wrapper observations and permits
      pre-dispatch reordering. Its new-schema/behavior/requirement-ID adapter and
      executable UVM/vector support are outside `local/frontend/` scope. Export
      `adapter_required` until that work is completed; do not claim integration PASS.
      The orchestrator also needs explicit isolated-build/approval selection and
      transport support; its current publisher selects the legacy global output.
- [ ] Demonstrate the first target through specifications and generic generation,
      without command-engine or refresh branches in Python expansion/wrapper logic.
- [ ] Adopt the new flow as the default after review and passing system verification.
- [ ] After initial success, review support for broader architectural freedom,
      including replacement of the compatibility preset and host interface.

## 2. Controller Features

Verification items below describe system acceptance requirements to carry into
the downstream UVM/vector stage; they do not assign that stage's implementation
to `local/frontend/`.

### A. First target — requests and FIFO scheduling

Use the existing simple host interface and functional banked memory. Support the
existing bank-count configurations and configurable waiting capacity, with one
active request. FIFO establishes the flow; scheduling experiments follow later.

- [ ] Admit on `txn_valid && txn_ready` at a rising edge. Each qualifying cycle is
      a distinct request; retain payload while backpressured.
- [ ] Dispatch by capturing the complete request in active storage and releasing
      its waiting slot. Define simultaneous enqueue/dequeue and full-queue reuse.
- [ ] Hold active ownership through PRE, ACT, and timing stalls. Setup commands
      must not retire the request.
- [ ] Retire on final READ/WRITE commit. Capture read-response state before releasing
      active storage; deliver the response as a separate event.
- [ ] Preserve acceptance order for the same bank/address under every policy.
      Independent requests may reorder under later policies; FIFO initially preserves
      global acceptance order. Track age independently of reusable slot index.
- [ ] Verify backpressure, field preservation, reset, simultaneous events, FIFO order,
      and exactly-once execution/response behavior.

### B. First target — commands, bank state, and timing

The engine proposes commands; timing decides legality; committed commands update
architectural state. Preserve existing timing protection through this migration.

- [x] Define command payload and encoding for NOP, ACT, PRE, READ, WRITE, and REF.
      Define one commit event, with at most one command committed per cycle.
      Defined in shared types/events and reviewed execution contracts; execution
      tests remain pending.
- [ ] Generate ACT → READ/WRITE for closed banks, READ/WRITE for row hits, and
      PRE → ACT → READ/WRITE for row conflicts. Hold candidates stable while blocked.
- [ ] Update open-row state only on committed ACT/PRE and defined refresh events.
- [ ] Enforce tRCD, tRAS, tRP, tRRD, and tFAW in one timing layer using selected
      profile values. Update timing history from commits, never admissions or stalls.
- [ ] Support open-page and close-page. Close-page cleanup uses a legal PRE and
      retains bank ownership until commit; initially block new dispatch during cleanup.
- [ ] Write memory only on WRITE commit; start the existing read-response pipeline
      on READ commit. Keep response delivery separate from request retirement.
- [ ] Verify sequences, blocked-state stability, timing boundaries, bank isolation,
      read-after-write data, responses, and both page policies.

### C. First target — refresh and system acceptance

Refresh must cause a real service interval and preserve queued traffic. The system
spec must resolve active-service drain, row-state effects, and timing interactions.

- [ ] Generate periodic pending refresh from tREFI. Give it priority after active
      service and required cleanup; prevent indefinite starvation.
- [x] Specify preparation/drain and row-state handling before committing REF.
- [ ] Start tRFC blocking on REF commit; prohibit normal commands during the interval,
      acknowledge once, and resume queued traffic afterward.
- [ ] Verify idle and loaded refresh, active-service interaction, interval boundaries,
      no traffic loss, and post-refresh progress.
- [ ] Pass lint/compilation and a system test covering host acceptance through command
      execution and response, queue stress, ordering, timing, refresh, data, reset,
      and close-page cleanup. Record configuration and requirement results.

### D. Later — scheduling policies

Policies choose among requests eligible under the system ordering contract.
Introduce them through design requests/specs, without Python feature branches.

- [ ] Add row-hit-first selection and verify priority without same-address reordering.
- [ ] Add age-based fallback and configurable bounded-starvation policies; define
      progress bounds and assumptions about timing, refresh, and downstream stalls.
- [ ] Experiment with other policies and per-candidate progress eligibility.
- [ ] Compare request latency, throughput, row-hit rate, queue utilization, and stalls.

### E. Later — AXI-Lite, responses, and bursts

Keep host protocol handling separate from DDR control and timing.

- [ ] Add an AXI-Lite adapter with independent write address/data capture, channel
      pairing, read/write responses, and backpressure handling.
- [ ] Extend response contracts for buffering and configurable CAS-like latency.
- [ ] Add simplified fixed-length bursts, beat tracking, data intervals, and explicit
      burst completion/ownership. Full AXI4 bursts and a DDR PHY remain outside scope.
- [ ] Add module and system tests for channel stalls, response preservation, burst
      data/order, and interaction with queueing and refresh.

### F. Later — additional timing and execution overlap

Extend the same timing-rule and ownership structures as capabilities grow.

- [ ] Add tRC, tCCD, tWTR, and tWR; consider tRTP/tRTW and bank-group rules afterward.
- [ ] For each rule, define scope, triggering commit, blocked commands, profile value,
      and directed boundary tests.
- [ ] Explore overlapping bank execution after single-active behavior is verified;
      specify command arbitration, request ownership, and response ordering first.

Defer PHY signaling, training/calibration, ECC, mode-register programming, power
management, and production initialization until the functional architecture and
generation flow are established.
