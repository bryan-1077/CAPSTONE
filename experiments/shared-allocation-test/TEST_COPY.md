# Shared-allocation test copy

This directory is an independent source snapshot of the current CAPSTONE workspace,
including the uncommitted LEC integration, queue handling and resume command.
The original working version remains at `/home/antgamez1203/CAPSTONE`.
Files are ordinary copies, not links, so changes here do not modify the original.
`SNAPSHOT_MANIFEST.json` records the original file hashes and Git revision.
Shared-allocation mode is implemented only in this experimental copy.

## Experimental Agent 4 copies

The experimental orchestrator defaults to
`server/validation/agent4_lec_experimental.py` on the existing server. The original
`agent4_lec.py` remains unchanged. Matching local copies are in this test tree's
`server/validation/`, together with:

- `experimental_test_agent4_preparation.py`
- `experimental_test_system_agent4.py`
- `run_agent4_experimental_tests.py`
- `AGENT4_EXPERIMENTAL_MANIFEST.json` with original and copied-file hashes

The runtime and test copies began as byte-for-byte copies. The experimental
runtime now fixes system-mode Slang file/include tokens and repair JSON parsing.
Slang paths use validated unquoted tokens; the native Verilog path keeps its
existing quoting. Repair parsing accepts leading `<think>` reasoning blocks and
JSON code fences, while rejecting extra prose, multiple objects, duplicate keys
and non-JSON constants. All existing settings, input-integrity and proof gates
remain in force. The test launcher binds `agent4_lec` to the experimental module in its own process so the
copied tests and their fixture imports can target the copy without editing
original modules. Run it on the server, where the supporting modules exist:

```bash
source tools/oss-cad-suite/environment
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 server/validation/run_agent4_experimental_tests.py
```

An explicit `CAPSTONE_LEC_SCRIPT` or remote JSON `lec_script` override takes
precedence over the experimental default; select the experimental filename in
such overrides when testing the copy. The installed Slang/Yosys tools successfully
prepared the exact 11 saved reference files from the failed controller run after
the fix; their hashes stayed unchanged. This is preparation validation, not a
completed controller equivalence proof. No full-system rerun was started.

## Run the six-CPU experiment

Use the same configured Python environment and exported API/SSH settings as the
normal full-system command. Run locally, not inside a manually created Slurm shell:

```bash
cd /home/antgamez1203/CAPSTONE/experiments/shared-allocation-test
python3 -m shared.top_orchestrator --shared-allocation --shared-allocation-cpus 6
```

This retains the usual interactive spec and frequency selection. To specify a
frequency directly, add `--target-mhz 200` (or your intended target). Add `--plan`
to preview without generating RTL, submitting jobs or writing run artifacts.

The frontend runs locally. Just before remote verification, the controller queues
one `salloc --no-shell` reservation for six CPUs. Verification, backend synthesis,
mapping, LEC, GDSII generation and GDSII validation execute sequentially using
`srun --jobid=<shared-id>` against that reservation. Backend steps omit the nested
`load-ecen-454` allocation alias in this mode. Supporting processes receive
`MAKEFLAGS=-j6` and `OMP_NUM_THREADS=6`; tool-specific serial phases remain serial.
LEC retains the authenticated system proof settings from the copied integration.

Defaults are four hours of total allocation lifetime and four hours of initial
queue allowance. `--shared-allocation-hours N` and
`--shared-allocation-queue-hours N` adjust those limits. The allocation lifetime
covers all remote stages combined, not four hours per stage. The controller
releases its owned allocation on completion, failure or interruption. A hard
process kill or network outage can prevent cleanup; the Slurm wall-time limit
still expires it, and its job ID is retained in `shared_allocation.json`.

This mode is enabled only by `--shared-allocation` on a verified full `generate`
flow. Do not add `--no-verif` or `--no-backend`. Existing per-stage launch behavior
remains available when the flag is omitted. The separate resume CLI does not yet
accept shared-allocation options.

Before queuing, the experimental controller checks old `ecen-454` allocations
owned by the same user in the exact CAPSTONE project directory. It automatically
releases only one-CPU `bash` allocations at least ten minutes old after two
process observations show a single sleeping shell without children or other
workload steps. It rechecks job ownership/project identity before cancellation.
Unknown process state, active work, recent allocations and unrelated jobs are
retained. Cleanup evidence is saved in `shared_allocation.json`.

The initial six-CPU request can still wait if an active allocation consumes
your per-user quota. The experiment does not cancel unrelated jobs. Once granted,
stages reuse its CPUs instead of requesting more allocations between stages.

Inspect `<local-run-dir>/shared_allocation.log` and `shared_allocation.json` for
the reservation and release history. Remote validation/LEC/GDSII reports use
`<remote-project>/shared/experiments/shared-allocation/<job-id>/`. Backend build
and script names include `sa<job-id>`, for example `build_mapped_sa777_200MHz_01`,
and timing-closure builds preserve that namespace.

## Local tests

```bash
cd /home/antgamez1203/CAPSTONE/experiments/shared-allocation-test
python3 -m unittest discover -s shared/top_orchestrator -t .
```

Backend gate and recovery tests:

```bash
cd /home/antgamez1203/CAPSTONE/experiments/shared-allocation-test/local/backend
python3 -m unittest timing_closure.tests.test_lec_gate timing_closure.tests.test_ai_closure timing_closure.tests.test_closure_runner
```

The existing Python environment can run these tests; a virtual environment was
not copied. The small current RTL/testbench inputs were retained for fixture tests.
Historical runs, backend logs, mailboxes, simulator outputs, large GDS files and
legacy archived agent flows were omitted. Service export settings and `.env`
files were not copied.

## Remote execution

The experiment reads the existing remote validator/tool sources and functional
libraries. It creates new namespaced build copies and runtime workspaces; it does
not replace the original local or remote source files. Service export settings
and `.env` files were not copied, so use your configured shell or an explicit
remote JSON profile. Explicit `validation_workspace` overrides should point at
a fresh test workspace; the automatic default is namespaced as described above.

Tests mock scheduler and EDA execution. They validate allocation reuse, stage
handoffs, environment propagation, report collection and cleanup; they do not
establish real controller equivalence or measured speedups. No live experiment
was launched during implementation.

This directory has no independent Git repository. Git commands here still find
the parent CAPSTONE repository; avoid repository-wide checkout/reset operations
from this directory. No original source or Git history was changed to create it.
