# RTL response recovery checkpoints

The recovery work is staged so each change can be inspected and tested before
the next. Checkpoint 1 records complete response evidence and failure categories.
It does **not** add recovery calls, stricter payload validation, different model
defaults or new RTL acceptance paths.

## Checkpoint 1: evidence and classification

Normal frontend entry points wrap modules in `ModuleUsage`. Dual-LLM calls now
save evidence beside the existing module usage and round reports:

```text
microarch/usage/<usage-run>/modules/<module-scope>/
├── usage.json
├── rtl_review.json
└── llm_calls/<call-id>/
    ├── attempt_001.json
    └── attempt_002.json     # Only if the existing truncation retry occurs
```

Each invocation of the API helper gets a distinct call directory; token-budget
retries remain numbered attempts within that call. Request metadata is saved
before HTTP execution. The complete decoded response body and model content are
saved before the runtime handles the provider envelope or parses the inner JSON.
The original model text, including outer whitespace, is retained separately from
the parser input. Successful responses are captured too. A saved response is
evidence, not reviewed/accepted RTL.

The version-1 attempt record includes:

- Model, stage, output budget, timeout, character count and message SHA-256.
- HTTP status, full decoded body, model content, finish reason and reported usage.
- Parser input, parse status and exception type/message.
- JSON error line/column (one-based) and character position (zero-based), referring
  to `parser.input`, not necessarily the unmodified response. Existing wrapper
  extraction can change those coordinates.
- A failure category and a link from the module round report to the evidence.

The configured `TAMUS_AI_CHAT_API_KEY` is redacted from persisted evidence,
response previews and typed error messages, including its JSON-escaped forms.
Request headers and full outbound prompts are not recorded. If credential
redaction changes parser input, the coordinate-space field identifies the
positions as referring to the input **before** credential redaction. Full evidence
therefore means complete content except for known-credential redaction.

Records use atomic replacement and private file permissions. A diagnostic-write
failure is reported without substituting it for the original model/parser error.
Evidence storage is best effort when the filesystem is unavailable. Private API
helper calls made outside `ModuleUsage` retain evidence in memory and create no
unexpected global output directories; both supported CLI paths establish a module
scope automatically.

## Categories recorded

| Category | Meaning |
| --- | --- |
| `configuration` | Invalid local API key/budget/timeout configuration before HTTP |
| `network_timeout`, `network_connection`, `network_error` | Request exception; no provider response/usage is invented |
| `http_error` | Non-200 status; exact status and full redacted body retained |
| `provider_response` | Invalid provider JSON/envelope shape, distinct from malformed model JSON |
| `truncated_response` | Provider reported `length` or `max_tokens` |
| `filtered_response`, `unfinished_response` | Provider did not report a completed text response |
| `empty_response`, `non_text_response` | Provider content cannot be consumed as text |
| `malformed_json` | Inner model JSON raised `JSONDecodeError` |
| `invalid_payload` | An already-rejected payload exception, such as a non-object root |
| `missing_rtl` | Generator/merger supplied missing or empty RTL; reviewer RTL remains optional |
| `spec_clarification` | The existing reviewer path explicitly requires specification clarification |
| `review_unavailable` | Existing reviewer fallback could not obtain reviewed cached RTL |

`LlmFailure` is a `RuntimeError` subclass so existing callers still catch failures.
Parser helpers retain their prior fallback/status behavior and add a structured
failure record. On a first-generation parse failure with no RTL fallback, the
module error now identifies `malformed_json` and points to its complete evidence.
It is not reclassified as a genuine specification ambiguity. Module reports retain
`failure_events` even when existing truncation/compact-review/cache behavior
subsequently succeeds; the report's final `outcome` still identifies the result.

Strict required-field/type/duplicate-key validation is checkpoint 2. Checkpoint 1
classifies the failures currently detected; it does not silently tighten the
accepted response schemas or remove existing normalization/fallback behavior.
RTL lint/review findings continue through their existing reports and gates.

Accounting still records each actual HTTP attempt once. Missing provider usage
remains unknown rather than zero. Models, token-budget growth/ceilings, compact
review fallback, convergence rounds, lint repairs, cache handling, resume, FSM
routing and final approval/lint requirements are unchanged.

## Offline verification

`test_llm_diagnostics.py` adds 18 mocked-provider tests covering full capture before
parsing, a long malformed response beyond the console preview, all parser stages,
provider/transport/content categories, credential redaction, missing usage,
truncation attempt counts, atomic-write failure, typed module reports, genuine
specification ambiguity and persistence after failed module/run cleanup. The
84 existing API, model-selection, resume, debug and probe checks also pass.
The complete frontend suite passes all **515 tests** with network connections
blocked in the runner. Python compilation and whitespace checks pass.

A synthetic offline example is saved under
`/tmp/capstone-rtl-evidence-checkpoint1-cwaufv8l/`. Its 7,610-character response
fails parsing at position 7,573; the stored parser input reproduces the same error
and retains the tail beyond the 1,000-character console preview. It makes one
mocked HTTP attempt, no recovery call, and writes no RTL or cache. This recreates
the incident's failure pattern; the original provider response beyond its logged
preview was lost and cannot be reconstructed.

```sh
python3 -m unittest discover -s local/frontend -p test_llm_diagnostics.py -v
```

## Checkpoint 2: strict stage-specific payloads

`rtl_response_payload.py` supplies one decoder and validator used by generation,
lint-repair generation, merge, full review and compact review. The response must
be one JSON object, optionally inside a single whole-response markdown fence.
Leading/trailing whitespace is allowed. Arbitrary prose, array wrappers and
multiple objects are rejected rather than extracting a plausible brace-delimited
substring. Duplicate members at any depth and nonstandard numeric constants
(`NaN`/`Infinity`) are rejected. RTL strings are decoded by the JSON parser, not
repaired by replacing quotes or backslashes.

| Stage | Required fields | Optional fields |
| --- | --- | --- |
| Generator / lint-repair generator / merger | `status`, `issues`, nonempty text `corrected_rtl` | `invariants` |
| Full reviewer | `status`, `issues` | Text `corrected_rtl`, `invariants` |
| Compact reviewer | `status`, `issues` | None |

Unknown fields are rejected. Status is nonempty text naming `VALID`, `INVALID`
or `AMBIGUOUS`; case/outer whitespace normalization and the legacy `CLEAN` alias
remain supported. Issues must be an array of objects containing exactly `type`,
`description` and `location`. Type must name `SPEC_VIOLATION`, `NON_ISSUE` or
`AMBIGUITY`, with nonempty text for description/location. No missing fields,
arbitrary values or typoed types are silently converted into ambiguity findings.

`VALID` cannot coexist with blocking violation/ambiguity issues. `INVALID`
requires at least one `SPEC_VIOLATION`; `AMBIGUOUS` requires an `AMBIGUITY`.
Prompts now state these rules consistently, removing the older reviewer
instruction that requested `VALID` whenever there were no specification
violations even if ambiguities remained. Every typed issue is preserved,
including a sixth issue; prompt guidance to keep reports short is not permission
to silently drop a later blocking finding. Compact review no longer turns
`NON_ISSUE` into `AMBIGUITY`.

When supplied, the legacy `invariants` field must be an array of nonempty text.
Caller/compiler invariants remain authoritative. Reviewer RTL is genuinely
optional; if absent or empty, the existing candidate is retained for the normal
review/merge decision. Wrong RTL types are rejected. Generator and merger
responses require nonempty RTL even if older candidate text exists. A single
outer RTL fence remains supported, while fence-like comments inside code are
retained rather than extracting an interior snippet. RTL syntax/behavior remains
the responsibility of HDL lint and review; nonempty text is not proof of valid RTL.

Schema errors are recorded as `invalid_payload`, with a `schema_path` where
available. An otherwise shaped generator/merger reply with absent/blank RTL is
`missing_rtl`; syntax decoding errors remain `malformed_json`. The checkpoint-1
raw content and parser input remain available. Parser helpers retain fallback
candidate text solely as context, but rejection is checked explicitly before
initial generation, lint-repair generation or merged output can proceed as a new
candidate. This intentionally closes the earlier path that could silently reuse
older RTL after a bad generator/merger reply. Genuine ambiguity remains a
separate validated review disposition.

This checkpoint adds no payload/transport retry and does not change model
selection, token-budget retries, existing compact-review fallback, reviewed-cache
fallback, convergence limits, FSM routing or resume selection. Valid legacy
responses remain supported; permissive acceptance of malformed/unknown shapes
is intentionally removed. Bounded recovery is still checkpoint 3.

`test_rtl_response_payload.py` adds 21 offline tests across the four payload
stages, including malformed wrappers, duplicates, field/type errors, status/issue
contradictions, escaped RTL, optional reviewer code, legacy aliases/invariants,
issue preservation and real-loop rejection at generation/lint-repair/merge
boundaries. All 123 focused payload, evidence, API, model, resume, debug and probe
tests pass. The older evidence fixture now uses a supported outer JSON fence
instead of arbitrary prose, preserving the long-error-coordinate check under
the stricter decoder.

An offline audit under `/tmp/capstone-rtl-payload-checkpoint2-egdjww79/` preserves
five rejected response cases and their full evidence. Each used one mocked HTTP
attempt and stopped before lint/review/cache. A valid fenced/CLEAN fixture used
the normal two mocked generation/review calls and passed actual HDL lint and
reviewed-cache handling. No recovery call, target-design execution, live model
call or modification of the original failed run occurred.

Final checkpoint-2 verification: all **536 frontend tests passed** with network
connections blocked in the runner, including the 21 new payload tests. The 123
focused regressions also passed, along with Python compilation and whitespace
checks. Live model consistency and bounded recovery remain later checkpoints;
no paid rerun is needed merely to verify these deterministic payload checks.

## Checkpoint 3: bounded recovery

Bounded recovery is enabled by default for microarchitecture RTL builds with
lint, including architecture-manifest resumes and the text intake route. Add
`--no-rtl-recovery` to `run_flow.py` or `start_flow.sh --specification --from-text`
to disable it. `--rtl-recovery` remains available as an explicit enable flag.
The two flags are mutually exclusive.

The default is resolved after architecture routing; the legacy expand-spec route
and FSM generator keep their existing defaults. Standalone `design.py` and the
Python generator API remain opt-in; architecture orchestration forwards the
resolved choice to `design.py` in its isolated build directory. No model defaults
change. Prepare-only/export-only and `--no-lint` runs do not automatically enable
recovery. Explicit `--rtl-recovery` still requires an RTL build with lint and an
isolated output root. An explicit disable preserves the prior transport,
truncation, compact-review and cache behavior.

The budget belongs to one module invocation, across generation, local lint
repairs, merge, full/compact review and all convergence rounds. It permits at
most **two extra HTTP attempts**, with at most **one per category**:

| Category | Recovery |
| --- | --- |
| Payload | Malformed JSON/schema: one complete response correction with original authoritative context, failed text and validation error. Missing/empty RTL or empty text: regenerate from original context. Full-review payload failure: use the existing compact review instead. |
| Transport | One same-request retry for timeout/connection exceptions (excluding SSL), or HTTP 408/429/500/502/503/504/524. |
| Truncation | One same-prompt retry with a larger output allowance, only below the configured ceiling. Partial text is never accepted. |

Nested failures share these allowances: payload correction followed by a 524
retry uses both extra attempts; subsequent truncation must stop. A second payload
failure in another stage/round cannot start another correction. Failed compact
review cannot recurse. These limits are additional to existing bounded semantic
rounds/lint repairs, which continue to handle actual RTL findings separately.

Transport delay defaults to two seconds. Numeric and HTTP-date `Retry-After`
values are honored subject to a 30-second retry-delay allowance. A larger delay
stops recovery rather than retrying sooner than requested. Existing TPM pacing
still applies independently. Authentication/configuration/client errors, SSL
errors, provider envelope/nontext failures, refusals (`refused_response`), filtering
and validated specification ambiguity stop without a format/transport retry.
A repaired response reporting genuine ambiguity also stops for clarification.

Every regenerated or corrected candidate still passes through the normal lint,
review and merge/re-review gates. A recovery is not semantic approval. In enabled
mode reviewer API/schema recovery failures propagate the typed cause rather than
silently returning cached RTL. Explicit reviewed-cache/resume hits remain valid
and consume no recovery attempts. The budget resets on the next module invocation;
it is not persisted as an enable setting or a lifetime retry count across resumes.

`rtl_review.json` records the limits, reserved extra attempts, per-category counts,
retry delays and denial reasons even on failure. Each HTTP attempt retains the
checkpoint-1 evidence and its current budget snapshot. Usage accounting includes
every actual attempt once; unavailable provider usage remains unknown. Corrections
have separate call directories; transport/truncation retries are numbered attempts
within their call directory. Full outbound prompts are not persisted.

`test_rtl_recovery.py` adds 19 tests covering recovery success/exhaustion, shared nested and
cross-stage/round limits, terminal errors, delay limits, real HDL lint followed by
mocked review, recovered merge re-review, cache/resume, unknown usage, redaction,
failed-run evidence, CLI forwarding and FSM isolation. All provider calls are
mocked; no live LLM calls or original saved-build mutations are part of this
checkpoint. Broader route/gate fault injection remains checkpoint 4, followed by
a user-run live continuation at checkpoint 5.

An additional synthetic audit is saved at
`/tmp/capstone-rtl-recovery-checkpoint3-nvnejn91/audit.json`, including full attempt
records and budget reports for malformed recovery, nested 524 recovery, payload
exhaustion, a denied third recovery and terminal authentication failure. The
successful cases used real Verilator lint and mocked semantic review.

Checkpoint-3 verification: all **555 frontend tests passed** with network
connections blocked in the runner, including the 19 recovery tests. All 166
focused regressions passed. Python compilation, shell syntax and whitespace
checks passed. No live provider call was made.

## Checkpoint 4: recovered-candidate gates and route isolation

`test_rtl_recovery_gates.py` adds 17 offline fault-injection tests. Provider
responses and semantic findings are mocked; candidate files, specification-keyed
cache writes, module output, specification lowering/validation, Verilator lint,
build status and verification handoff validation use the production code.
The architecture fixtures use synthetic offline approvals for a small byte
register, not approvals for the user's target design.

| Injected condition | Verified result |
| --- | --- |
| Format correction changes RTL behavior | Reviewer violation is retained; a merge and a later clean review are required before the corrected candidate is cached. Generator `VALID` alone cannot approve it. |
| Recovered RTL has syntax errors | Verilator rejects it; ordinary lint repair must produce a new lint-clean candidate before approval. Persistent lint errors remain blocking even if the mock reviewer says `VALID`. |
| Merger response requires payload recovery | Its result must pass lint and be reviewed again. A subsequent ambiguity/authentication failure stops without a new cache write. |
| Recovered merge fails lint | A lint-clean reviewer candidate can be reused, but it still gets another review before caching. |
| Compact reviewer returns a violation | The violation remains blocking and follows the normal merge/re-review path. |
| Lint-repair correction is malformed, or recovery reports genuine ambiguity | Failure cannot accept earlier candidate text as newly generated RTL. No new cache is written; clarification stops before further lint/review. |
| Reviewer fails after generation recovery | The typed failure propagates and an existing reviewed cache remains byte-for-byte unchanged. |
| Default architecture recovery succeeds | Real lowering, validation, module generation, module/integration lint and handoff export complete. Behavioral verification remains `not_run`. |
| Recovered module has the wrong public interface | Whole-system Verilator lint fails, build status reports failure, and no current verification handoff is exported. |
| Payload recovery is exhausted during an architecture build | Module generation fails, integration lint remains `not_run`, no new module RTL/cache is written, and a stale prior handoff is removed. |
| Architecture recovery is explicitly disabled | Malformed generation stops after one HTTP attempt; no recovery context leaks. |
| Recovery followed by legacy generation, FSM or deterministic timing generation | Legacy expansion commands keep their existing defaults; FSM/deterministic routes make no recovery/LLM call. Specification-keyed caches and prior output stay isolated. |

These checks exercise the existing acceptance gates without changing production
retry, acceptance or routing policy. Module cache entries represent module-level
review/lint approval and may already exist when integration lint later fails.
They are not whole-build approval. `build_status.json` and the presence/validation
of the current handoff determine whether the architecture build completed.
Prior module RTL may remain on disk after a failed regeneration; the tests ensure
it is not overwritten by rejected output and the failed build is not reported as
successful. Existing semantic-round exhaustion/cache fallback behavior is
preserved; recovery API/schema failures still propagate as established in
checkpoint 3.

The persistent audit is
`/tmp/capstone-rtl-recovery-checkpoint4-9rnsbc9l/audit.json`. It maps each test to
saved fixture files, full response evidence, module review reports and build
status. All 17 audit cases passed, including actual Verilator module/integration
checks. The 185 focused regressions also passed with network connections blocked.
No live provider call, target-design run or mutation of the user's saved build
was performed. Mocked semantic review verifies enforcement of findings; it does
not establish a live model's consistency or the target design's functional
correctness. Those remain distinct from this deterministic recovery validation.

Final checkpoint-4 validation passed all **574 frontend tests**, including the
17 new gate tests, with network connections blocked in the runner. All 185
focused regressions passed. Python compilation and whitespace checks passed.
Checkpoint 4 required no change to production acceptance or retry policy.

## Checkpoint 5: saved-build resume and live continuation (completed)

The original partial run is
`microarch/builds/text_runs/20261027T182419Z_27739196`. Its saved revision-bound
approval still validates with the current code. Relowering the approved manifest
into a temporary directory reproduces all seven module specification files
byte-for-byte, preserving their specification-keyed cache identities.

| Module | Verified resume state |
| --- | --- |
| `host_adapter` | Reviewed cache matches saved RTL and passes Verilator lint. |
| `request_queue` | Reviewed cache matches saved RTL and passes Verilator lint. |
| `scheduler` | Reviewed cache matches saved RTL and passes Verilator lint. |
| `command_engine` | No accepted RTL/cache; first module requiring generation. |
| `timing`, `bank_state`, `memory` | No accepted RTL/cache; generation remains pending. |

An actual resume through the production orchestration/module implementation was
then exercised on a copy of this build. The LLM helper was replaced with an
intentional stop before execution; HTTP and socket calls were separately blocked.
All three completed modules were cache hits, with no paid calls. The next call
boundary was `LLM1-Generate` for `command_engine` using the original run's
`protected.gpt-5.6-terra` override. Recovery was enabled by default and no recovery
allowance was consumed. The copied run reports failure only because the offline
stop deliberately prevents unfinished generation. This is not a live provider
failure or a completed frontend run.

The audit at
`/tmp/capstone-rtl-recovery-checkpoint5-p4smipdn/resume_audit.json` records those
checks; `preflight.json` contains exact specification/cache hashes and the saved
run snapshot. Original saved-run file contents and copied reviewed cache contents
were verified unchanged after the audit. No target RTL was newly generated and
no live LLM call was made.

For the user-run live continuation, execute from the repository root:

```bash
RUN_DIR="$PWD/local/frontend/microarch/builds/text_runs/20261027T182419Z_27739196"
set -o pipefail
python3 -u local/frontend/run_flow.py \
  "$RUN_DIR/architecture.yaml" \
  --spec-approval "$RUN_DIR/review/approval.json" \
  --build-dir "$RUN_DIR/build" \
  --rtl-generator-model protected.gpt-5.6-terra \
  --resume \
  2>&1 | tee "$RUN_DIR/resume-$(date -u +%Y%m%dT%H%M%SZ).log"
```

This reuses the approved specification and the three existing caches, then
continues the four unfinished modules. It does not repeat text intake or semantic
specification review. The explicit model keeps the original run's generator
choice; it is not a change to model defaults. `--rtl-recovery` need not be added.

The user subsequently completed this live continuation. The run's usage report
is `microarch/usage/20261027T193932Z_820095591eda/usage.json`:

- Three reviewed cache hits made zero provider calls; their saved cache hashes
  still match the offline preflight.
- `command_engine`, `timing` and `memory` each converged in one generation/review
  round. `bank_state` converged after three review rounds. Its first merge failed
  Verilator width lint, so a lint-clean reviewer candidate was reviewed again;
  the subsequent approved merge passed the gates before caching.
- All seven module reports have recovery enabled and zero recovery attempts or
  response/transport failure events. No malformed JSON, truncation or 524 occurred.
  Semantic repairs for `bank_state` were ordinary review/merge rounds, not payload
  or transport recovery.
- The 12 actual calls have 12 complete attempt evidence records. Reported usage
  is complete: 116,590 input + 14,540 output = 131,130 tokens. The continuation's
  estimated cost is $0.407660 using configured user-supplied rates. Including the
  original $0.491334 run gives $0.898994; this is an estimate, not a provider bill.
- `build/build_status.json` reports RTL generation and integration lint passed,
  recovery enabled, result `generated` and handoff `exported`. Local
  `validate_handoff` confirms `build/verification_handoff.json` passes validation.

Checkpoint 5 is complete for frontend generation, resume and handoff. This live
run confirms the integrated workflow succeeds; the recovery branches themselves
were exercised by offline fault injection rather than a live failure. Behavioral
simulation remains `not_run` and must not be inferred from generation/lint/handoff
completion. No additional live LLM call was made by the assistant during result
inspection.

## Following checkpoints

1. Complete response evidence and failure categories (implemented).
2. Consistent stage-specific strict payload validation (implemented).
3. Bounded recovery with shared limits (implemented; default for microarchitecture RTL builds with lint).
4. Broader recovered-candidate gate and route-isolation fault injection (validated).
5. Saved-build resume and user-run live continuation verified (completed; behavioral simulation not run).

### Default enablement verification

Recovery now defaults on only after architecture routing. Default, explicit
enable/disable, resume, export-only and no-lint choices are covered alongside
mutually exclusive flags and shell/text forwarding. Legacy direct-module/API
defaults and FSM routing remain covered by the existing regressions.

Default-enablement validation passed all **557 frontend tests** with network
connections blocked, including 168 focused regressions and 21 recovery tests.
Python compilation, shell syntax and whitespace checks passed. No live LLM calls
were made.
