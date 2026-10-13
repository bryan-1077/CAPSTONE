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
candidate testbench sources are available to investigation. For each stage,
the candidate matching the highest saved simulation iteration (or compile
iteration if no simulation exists) is exposed as checker evidence; other
candidate iterations remain archived. Hidden files, symlinks, and DUT source
files outside candidate directories are excluded.

The DUT remains the current `rtl_output/`; no Git history or previous DUT
version is loaded. Imported logs are not proof that the run used the current
RTL revision. `--investigate` stops after evidence assessment without repair.
Use `--offline` as well to collect evidence without model calls.

Initial verification inspection prioritizes the failure summary, latest simulation
log, generated checker logic, and approved specification. Rejected model
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
