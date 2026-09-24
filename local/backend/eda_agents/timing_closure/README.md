# Timing Closure Overview

This directory holds the tools that run backend setup timing closure, analyze timing reports, and suggest improvements.

- `closure_runner.py`: Measures up to three backend presets, then invokes AI recovery from the best eligible routed checkpoint when timing still fails.
- `../agents/openai_timing_closure.py`: Chooses specific gate/buffer drive-strength changes using timing evidence, actual available cells, and past experiment results.
- `constraints.py`: Enforces the fixed 4 mm² die-footprint and 2 W reported-total-power limits.
- `timing_analyzer.py`: Extracts timing measurements, classifies critical paths using pattern-matching rules, and generates recommendations in Markdown or JSON.
- `remote_analyze.py`: Downloads timing reports from the SSH server and analyzes local copies.
- `resume.py`: Loads a saved flow state and starts closure at the GDSII stage using an already successful mapped design.
- `timing_closure_report.md`: Generated summary of the latest closure status, timing results, and recommendations.
- `tests/`: Checks report parsing, closure decisions, retries, build naming, and backend setup.
- `fixtures/`: Sample timing reports, a minimal mapped design, and technology files used for testing.

Notes:

- AI recovery is enabled by default. The analyzer and final acceptance checks are deterministic; the AI chooses the resize experiment, not its pass/fail result.
- The normal `app.py` flow enables closure by default at a 4.762 ns clock period (about 210 MHz).
- Success requires fresh reports confirming setup timing at the requested period, die footprint <= **4 mm²**, and total reported power <= **2 W**. Missing, non-finite, zero/placeholder, or unrecognized power/area measurements cannot pass. AI resize candidates must also pass a fresh hold-timing check.
- Area means the die bounding-box footprint, not the sum of standard-cell areas. Power means Innovus's total-power estimate under the design's existing activity and analysis conditions; it is not a measured-silicon or worst-workload guarantee. This flow does not provide full multi-corner signoff.
- RTL recommendations remain advisory. AI recovery changes only drive strengths of observed SKY130 HD combinational gates and buffers within the same Boolean-function family; it does not edit RTL or timing/power constraints.
- Logs, status, and attempt details are saved under `logs/timing_closure/`. Existing backend build directories are preserved; a naming collision stops the run.

To run the full flow at a chosen frequency, run from the project root:

```bash
python3 app.py --target-mhz 230
```

This calculates the period as `1000 / MHz` (230 MHz is about 4.348 ns) and disables inherited overconstraint. CLI options override environment settings; running without options keeps the existing environment/default behavior. Use `--overconstraint-mhz 240` to explicitly optimize at a higher frequency, or `--no-overconstraint` to disable it when using an environment target. Run `python3 app.py --help` for options.

### AI recovery after timing failure

After the preset attempts fail, the controller selects the highest-WNS checkpoint that meets the area/power caps. The AI reads its critical paths, the loaded library's available drive strengths, measured limits, and experiment history, then proposes up to 16 gate/buffer resizes. Each experiment restores that checkpoint into a new output directory, checks the instance/master identities, applies `ecoChangeCell`, legalizes placement, reroutes, and reruns optimization and reporting. Unsuccessful or over-budget experiments never replace the best eligible checkpoint. An improved candidate becomes the source of the next experiment.

Default recovery budget: **3 AI experiments** after the preset attempts. `EDA_TIMING_CLOSURE_AI_MAX_ATTEMPTS` accepts 1–10. `EDA_TIMING_CLOSURE_AI_ENABLED=0` disables AI recovery, but does not disable the area/power acceptance limits. AI uses the existing `OPENAI_API_KEY`, `OPENAI_MODEL`, and optional `OPENAI_BASE_URL` configuration. API failure, an invalid plan, repeated experiments, or no legal resize choices stop recovery explicitly.

Plans and their input evidence are saved as `ai_plan_N.json` beside the attempt states. Status JSON records the source checkpoint, actions, measured WNS, hold slack for AI candidates, area, power, and best eligible attempt. The plan API uses [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs); execution still validates every proposed action locally.

To recover an **existing failed run**, supply its full flow state (for max clocking, use the frequency's `.state.json`, not the sweep summary):

```bash
python3 timing_closure/resume.py --state /path/to/230MHz.state.json --recover-best
```

This keeps the saved clock period, chooses the best measured saved attempt at that period, and restores it into a fresh directory to collect current timing, area, power, and available-cell evidence. It then starts AI resizing if needed. Existing snapshots and builds are preserved; recovery uses unique output and log names. Use `--ai-attempts 5` to allow five experiments. The saved checkpoint must still exist on the server. No remote Innovus or model call is made until you run the flow.

To find the highest passing frequency in 10 MHz steps:

```bash
python3 app.py max clocking
# Equivalent flag form:
python3 app.py --max-clocking
```

Max clocking starts a fresh full flow at **230 MHz**, waits for completion, and then tries **240, 250, 260 MHz, ...** until the first unsuccessful run. Each target gets the existing stage retries, backend presets, and enabled AI recovery attempts before it is declared unsuccessful. This mode forces timing closure on and disables inherited overconstraint, so success requires passing timing and area/power checks at that target. It cannot be combined with `--target-mhz` or `--overconstraint-mhz`.

The terminal reports the highest passing frequency and the first failed target. If 230 MHz fails, it reports that no passing frequency was found. Each sweep saves `summary.json`, `report.md`, and per-frequency logs and flow states in a unique folder under `logs/max_clocking/`. The summary records the best state's path and each run's GDSII directory. Exit status is zero when at least one target passed, otherwise one.

Any flow error, missing timing validation, or existing build-name collision stops the sweep; the report includes the reason. Existing remote builds are preserved using the same naming rules as `--target-mhz`. The reported best is the highest successfully tested target; a tool or connection error does not establish a physical timing limit.

To start closure from the saved state in `logs/full_session.log.state.json`, run from the project root:

```bash
python3 timing_closure/resume.py --target-period 4.762 --attempts 3
```
