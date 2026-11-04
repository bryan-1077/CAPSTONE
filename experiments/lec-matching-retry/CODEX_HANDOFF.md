# Codex handoff: investigate DDR4 controller LEC partition failures

## Request and current outcome

Continue investigating LEC for run `internal_200MHz_20261103_172729_848939`
(200 MHz controller target). The user authorized trying these two changes:

1. Check internal signal matching and exclude suspect matches.
2. Retry with larger comparisons that retain the surrounding logic.

Both experiments are complete. **Neither established equivalence.** The first
left 53 failing internal partitions; the second timed out without a proof or
counterexample. No RTL, mapped netlist, Liberty model, reset assumption, or
production Agent 4 checker was changed. Do not report this investigation as a
successful fix or permit physical design based on these diagnostic results.

This is the failing **17:27 run**, not the earlier successful run
`internal_200MHz_20261103_152847_705134`, which skipped LEC.

## Environment and source identities

- Local repository: `/home/antgamez1203/CAPSTONE`.
- Remote login host: `olympus.ece.tamu.edu`.
- Remote account: `antgamez1203`.
- Remote repository: `/mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE`.
- Top module: `ddr4_controller_top`.
- Original shared allocation: `733265`, six CPUs on `n02-hera`; released.
- Experiment Slurm job: `733316`; finished.
- Diagnostics retrieval job: `733326`; finished.
- Tools: remote repository `tools/oss-cad-suite/bin/{eqy,yosys}`.
- Production checker: remote repository `server/validation/agent4_lec.py`.
  This file was not present in the local `server/validation` directory during
  investigation. A temporary downloaded copy was `/tmp/capstone_agent4_original.py`;
  temporary files are not a durable dependency. Retrieve the deployed source if needed.

Original mapped netlist:

```text
/mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE/server/backend/build_mapped_sa733265_200MHz_01/results/ddr4_controller_top_mapped.v
```

Original remote Agent 4 evidence directory, called `SOURCE` below:

```text
/mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE/shared/allocations/733265/lec_runs/356dc9917c74489c94a97b3ca5358e24/verification_reports/agent4_runs/2026-11-03_23-50-08_ts9t2jq0/agent4
```

Remote experiment directory:

```text
/mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE/shared/lec_matching_20261103_01
```

Local original evidence mirrors `SOURCE` under:

```text
shared/top_orchestrator/runs/internal_200MHz_20261103_172729_848939/002_backend/backend_reports/lec/356dc9917c74489c94a97b3ca5358e24/reports/verification_reports/agent4_runs/2026-11-03_23-50-08_ts9t2jq0/agent4
```

## Original failure: established facts

- Agent 4 compared the previously verified RTL against the mapped netlist.
- EQY returned code 2 and `DONE (FAIL, rc=2)`.
- Eight of 65 partitions passed; 57 failed.
- EQY execution took 287.322 seconds. The original report records no execution
  timeout and no tool error.
- Passing partitions were:
  - `ref_req`, `rsp_valid`, `row_open_valid`;
  - `u_refresh_controller.clk`, `.counter`, `.ref_ack`, `.ref_req`, `.rst_n`.
  All names have the prefix `ddr4_controller_top.`.
- Failing comparisons included command/transaction readiness, request queue
  data and valid bits, service registers, open-row data, tFAW count bits, and all
  32 read-response data bits. These are related obligations, not evidence of 57
  independent RTL defects.
- The `cmd_ready` partition has a step-0 counterexample: gold/reference output
  is 0 while gate/mapped output is 1. Native traces exist under:
  `SOURCE/eqy_work/strategies/ddr4_controller_top.cmd_ready/prove/ddr4_controller_top.cmd_ready/engine_0/`.
- Other failing partitions also have later counterexamples. Do not generalize
  the step-0 observation to all 57 failures.

The production checker intentionally reports internal-cutpoint counterexamples
as **INCONCLUSIVE** unless it can establish a counterexample in a closed,
observable output cone. Its empty `formal.counterexamples` array does not mean
that native EQY/SBY trace files are absent. This classification is intentional,
not a demonstrated report-parser defect.

A passing local partition proves its logic under matching proof inputs; it does
not prove that all upstream partitions supplying those inputs also match.

## Matching concern found

Comparing the saved `gold.json` and `gate.json` revealed one common internal net
with different width/index metadata:

| Net | Reference width | Mapped width |
| --- | ---: | ---: |
| `req_array_packed` | 204 | 4 |

`SOURCE/eqy_work/matched.ids` matches these names. In the mapped JSON, the four
bits alias queue entry 0's `wdata[0:3]` register outputs.

This is a **suspect correspondence**, not proof that the name match is wrong:
synthesis may preserve a shortened bus with valid surviving bit meanings.
The bit-level semantic correspondence still needs investigation. Do not claim
that this width discrepancy caused all original failures.

## Experiment inputs and controls

Both trials read the exact original `SOURCE/eqy_work/gold.il` and `gate.il` using
`read_rtlil`. These already contain the original elaboration, memory mapping,
and `clk2fflogic` conversion. This isolates matching/partitioning changes from
frontend and synthesis changes; it is not a fresh authenticated pipeline run.

Recorded SHA-256 hashes:

```text
gold.il  5809aa6299c01a39e43c1b3a01cce447cbdc798cbb3e5b381255297d410d7cd9
gate.il  4d228a5efe91cf0e674a74cc689e198111f13e6accf9df0fe55d3f3f9c65a9d7
```

Both trials used six EQY workers, native SBY `abc pdr`, depth setting 20, and a
600-second strategy limit. The runner had a separate 720-second overall limit
per trial. The original proof config used a 3600-second strategy limit; the
experiment limits were deliberately bounded. Depth 20 is the configuration
setting, not a claim of exhaustive proof through exactly 20 cycles.

### Trial 1: exclude the suspect internal bus match

Configuration change, leaving other native matching intact:

```ini
[match ddr4_controller_top]
gold-nomatch /^req_array_packed$/
gate-nomatch /^req_array_packed$/
```

Result: **8 PASS, 53 FAIL**, no timeout; overall INCONCLUSIVE.
Elapsed time: **589.386 seconds**, approximately 9 min 49 sec.

The four fewer failures are the excluded `req_array_packed` obligations. They
were not newly proved. None of the remaining partitions changed to PASS.

### Trial 2: top-level matching and one closed output comparison

Explicitly match these top-level ports by name, then disable remaining matches:

```text
Inputs: clk, rst_n, txn_valid, txn_is_write, txn_addr[3:0],
        txn_wdata[31:0], txn_bank[1:0]
Outputs: txn_ready, cmd_ready, rsp_valid, rsp_rdata[31:0]
```

The exact config is saved in `results/closed_outputs/proof.eqy`. Its collection
rules are:

```ini
[collect ddr4_controller_top]
bind *
group *
```

Coverage was verified against the generated partition JSON:

- One partition, named `ddr4_controller_top.cmd_ready` by EQY.
- Despite that name, it covers **all 35 output bits** listed above.
- Its only proof inputs are the seven top-level input ports.
- No internal `crossbits`.

Result: **TIMEOUT / INCONCLUSIVE**; no counterexample trace and no proof.
Elapsed time: **616.764 seconds**, approximately 10 min 17 sec including setup
and finalization. The native SBY log says:

```text
Reached TIMEOUT (600 seconds). Terminating all subprocesses.
engine_0 did not produce any traces
DONE (TIMEOUT, rc=0)
```

The outer EQY process still returned 2 and wrote FAIL; interpret the underlying
strategy TIMEOUT correctly. The runner's `timed_out: false` describes only its
separate 720-second wall guard. `summary.json` separately records
`strategy_timeout: true`.

## Conclusions and limits

1. Excluding the width-mismatched match alone did not resolve the remaining
   internal failures.
2. Eliminating all internal cuts did not reproduce a counterexample within the
   experiment limit, but the larger proof timed out. This does not prove the
   original counterexamples were unreachable.
3. Neither experiment proves a reachable functional mismatch or establishes
   equivalence. The LEC gate remains blocked.
4. Reset/initial-state modeling and RTL preparation/synthesis changes remain
   possible causes. Neither was modified or ruled out by these trials.

## Suggested next investigation

1. Inspect the actual bit meanings of shortened/split/renamed internal buses,
   including the queue packed bus. Validate candidate state correspondences
   against both elaborated models rather than accepting names or widths alone.
2. Try an intermediate decomposition: a few output/subsystem groups with
   surrounding producer logic retained, rather than the original small free
   internal-input partitions or one controller-wide output cone. Audit the
   generated partitions to ensure every observable output remains covered.
3. Evaluate native EQY binding/amendment rules for retaining producer context.
   Producer obligations must still be proved; do not assume internal equality
   merely to make consumer proofs pass.
4. Preserve original models and reset behavior for the next matching experiment.
   If reset assumptions are later considered, first establish the intended
   operating contract; do not add assumptions solely to suppress a failure.
5. Report counterexamples, strategy timeouts, tool errors, and proven partitions
   separately. A larger-cone timeout must remain unresolved.

Reference for native rules:
https://yosyshq.readthedocs.io/projects/eqy/en/latest/config.html

## Files included with this handoff

Paths below are relative to `experiments/lec-matching-retry/` in the repository,
or to the root of the handoff ZIP:

- `CODEX_HANDOFF.md`: this self-contained handoff.
- `runner.py`: isolated remote trial generator and Slurm runner. Its source
  identity is intentionally fixed to this run. `--submit` requires a **new,
  exclusive remote directory**; do not resubmit over the saved experiment.
- `report.py`: local diagnostic summarizer and output-coverage assertions.
- `results/summary.json`: verified machine-readable results and model hashes.
- `results/manifest.json`: source identity, ports, and detected width mismatch.
- `results/{width_checked,closed_outputs}/proof.eqy`: exact trial configs.
- `results/{width_checked,closed_outputs}/result.json`: raw statuses, timings,
  and partition boundary descriptions.
- `results/{width_checked,closed_outputs}/eqy.log`: native EQY logs.
- `results/closed_outputs/work/strategies/`: native strategy status and solver
  logs, including the timeout evidence.

The bundle contains diagnostics, not the original RTLIL models, mapped netlist,
Liberty file, or complete original run. Reproduction needs repository/remote
access to the source paths above. No credentials are included.

Regenerate and validate the downloaded summary from the handoff directory:

```bash
python3 report.py results
python3 -m py_compile runner.py report.py
```

These checks passed. The two actual EDA trials ran remotely; they did not pass
LEC. Login-node reads showed stale/delayed files during collection; final
diagnostics were retrieved through a short compute-node Slurm step. Check actual
job state before resubmitting when logs appear stale.

The repository already contains unrelated uncommitted changes. Preserve them.
Work from new experiment directories and do not replace the original run's
receipts with these diagnostic results.
