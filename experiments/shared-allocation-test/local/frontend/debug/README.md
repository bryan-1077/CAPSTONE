# Verification run intake

From `local/frontend`, investigate a saved verification run:

```bash
python debug_agent.py auto verif --verif-run /path/to/run --investigate
```

The path is the run directory containing `summary.json` and `agent1/`,
`agent2/`, `agent3/`, etc. Its name and location are arbitrary. The summary
must contain `overall_status` and `stages`. Saved `agentN/report.json` files
supply stage results, including failed requirements. Agent 1's optional
`validation_result.json` supplies the DUT name and source references.

`--verif-run` is also available in plain intake and `graph` mode, and is
mutually exclusive with `--log` and `--command`. `--log` also accepts a run
directory when the source is `verif`. Existing single-log intake is unchanged.

Artifacts are copied into the new failure workspace under
`intake/evidence/verif_run/`, preserving relative paths. The evidence manifest
records source paths and SHA-256 hashes. Reports, specs, logs, and generated
candidate testbench sources are available to investigation. System UVM runs also
import the exact compiled testbench files listed in `uvm_environment.json`, after
checking their SHA-256 hashes and keeping paths inside the saved report tree.
For each legacy stage,
the candidate matching the highest saved simulation iteration (or compile
iteration if no simulation exists) is exposed as checker evidence; other
candidate iterations remain archived. Hidden files, symlinks, and DUT source
files outside candidate directories are excluded.

The DUT remains the current `rtl_output/`; no Git history or previous DUT
version is loaded. When a failing system stage records a complete compiled RTL
hash inventory, intake compares it with every current manifest RTL file and
reports whether the revisions match. Without that inventory, imported logs do
not prove current RTL identity. A mismatch is retained as explicit uncertainty.
`--investigate` stops after evidence assessment without repair.
Use `--offline` as well to collect evidence without model calls.

Initial verification inspection prioritizes the failure summary, latest simulation
log, generated checker logic, and approved specification. Checker excerpts put
the reported requirement checks before general helper functions. Files larger
than 2 MB use streaming reads/searches with bounded excerpts, original line
numbers, and full-file hashes; individual line previews are limited to 8192 bytes.
Related model-requested inspections run in batches of up to three before the next
assessment. Requests remain FIFO across assessments, with duplicates ignored even
when their reason text changes. Initial speculative discovery is deferred behind
model-requested work. Reads return up to 600 lines and 16000 new characters; the
total evidence and per-action inspection budgets remain enforced. Fresh runs
default to 128000 evidence characters and 12 inspection actions, leaving room
for controller wiring, queue state updates, checker sampling, and reproductions.
Explicit CLI limits and budgets in existing checkpoints remain unchanged. Localization
uses explicit diagnostic locations rather than treating every compiled RTL file
as an implicated source.

Assessments use a focused working set of at most 22000 source characters, with
normally up to 2400 characters per excerpt, rather than resending the complete
archive. Small RTL/checker excerpts of up to 6000 characters stay intact so their
state updates are not lost to slicing.
The full evidence remains saved and indexed. Counterevidence, recent inspections,
and RTL/observation/original-checker sources receive priority. Partial excerpts are
labeled; focused reads can recall archived lines. `list` exposes additional
catalog files without broadening the source context automatically.

For a causal diagnosis, the model records one observed failure, a stable
hypothesis ID, a competing explanation, different predicted outcomes of a
discriminating check, and cited check results. Readiness requires a result that
supports the primary cause, in addition to the existing source/checker/observation
requirements. A refuted hypothesis needs new result evidence and an explanation
before it can return. Repeated assessments without new evidence require a
disconfirming inspection or a changed diagnosis. Failed repair feedback requires
an explicit review distinguishing an incomplete fix, another bug, and a wrong
diagnosis; a failed patch does not automatically refute its original cause.
Changing or refuting the primary diagnosis archives its pending confirmation
plan instead of continuing to execute it. Recalling new lines from the saved
archive counts as progress; the model need not collect duplicate evidence IDs.

The investigator may request up to two focused Verilator simulations using a
generated `debug_repro` testbench and explicitly selected current RTL sources.
Sources are copied unchanged into `analysis/experiments/`; compile/simulation
logs, exit codes, source hashes, and testbenches are preserved as diagnostic
evidence. Builds time out after 60 seconds, simulations after 10 seconds, with
2 GiB per-process address-space and 32 MiB per-file limits. Model testbenches cannot
use includes, foreign-function interfaces, file I/O, or arbitrary system tasks.
A model-written reproducer is not treated as the original trusted checker, and
a compile failure or timeout does not prove a DUT fault. These experiments do not
replace post-repair checks or remote verification.

Rejected model
assessments receive validation feedback and can be corrected within the existing
assessment budget (investigation step limit plus one). Evidence requirements
remain enforced; exhausted correction budgets are reported explicitly. Each
attempt saves its prompt, response, and any validation error.

For BIST, initial triage includes testbench declarations and instance connections,
plus checker blocks and log excerpts for up to eight distinct failures. An assessment
that requests additional inspections, or gives a next inspection for its primary
hypothesis, cannot authorize a patch even if it says `sufficient_evidence`.
The investigator must check signal ties, initialization, array packing, and checker
sampling before attributing failures to missing inputs or scheduler behavior.

After an empty/rejected patch or a failed check with successful rollback, both the
LangGraph and fallback runners reassess the cause before the next proposal. Feedback
includes the saved model response, proposed diff, and focused lint/BIST diagnostics.
Additional inspections and assessments use the existing investigation budgets;
numbered proposals still use `--max-attempts`. Failed patches remain rolled back.
This improves diagnosis and retries; a repair is accepted only after its checks pass.

Start a fresh repair against the existing BIST log, without regenerating RTL:

```bash
cd local/frontend
python3 debug_agent.py auto bist --log bist.log --repair --max-attempts 3 \
  --target-command 'bash run_sim.sh --no-wave-prompt'
```

Regression tests run without model calls:

```bash
cd local/frontend
python3 -m unittest test_debug_repair test_debug_llm_api -v
```

Assessment and patch API calls start with an 8,192-token output budget. Responses
marked `length` or `max_tokens` are rejected and retried with a larger budget,
up to 16,384 tokens. If that ceiling is reached, the debug flow reports a
truncation error before parsing an assessment or validating a patch. Each retry
makes another API call. Provider model limits still apply.

Set `DEBUG_LLM_MAX_TOKENS` and `DEBUG_LLM_MAX_RETRY_TOKENS` to override the initial
budget and retry ceiling. Both must be positive integers, with the ceiling at
least as large as the initial budget. Equal values disable truncation retries.

Investigation diagnostics are saved in `analysis/assessment_*.api.json`;
patch proposal diagnostics are saved in `attempts/attempt_*/api.json`. The existing
top-level completion metadata describes the final API attempt, and `attempts`
preserves each request's budget, completion status, usage, and error type.
