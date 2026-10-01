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
