# Local simulation of an architecture build

From the repository root:

```sh
python3 local/frontend/simulate_handoff.py \
  /tmp/capstone-live-sjSE8O/build/verification_handoff.json
```

The standalone runner validates an RTL-phase version-1 handoff, freezes its
authenticated input files into a fresh directory, and compiles the exact RTL
inventory with Verilator. It generates a SystemVerilog driver and reads declared
top/module ports through the handoff's event/observation bindings. It neither
changes the original build nor calls LLMs, the legacy simulation flow, orchestrator,
remote services or backend. No third-party Python packages beyond the existing
frontend requirements are added. Verilator and a working C++ build toolchain are
required.

The default output is `BUILD/simulations/<timestamp>_<id>/`. Use `--output-dir`
for a fresh explicit directory, `--seed` for reproducible randomized traffic,
`--max-cycles` for each scenario's clock-edge bound (default 200,000), and
`--timeout` for each compiler/simulator process's wall-clock bound (default 120
seconds). Existing directories are rejected, preserving earlier evidence.

## Executable profile and scenarios

The first profile, `--profile ddr4-fifo-v1`, is an explicit independent oracle for
the compact 4-bit-address/32-bit-data controller with FIFO service, 16 locations
per bank, one active slot, configurable waiting capacity, 1/2/4 banks, open-page
or close-page policy, enabled refresh/tRRD/tFAW, and synchronous active-low reset.
Row/valid observation arrays must use ascending indices from the LSB, and NOP
must be encoded as zero. Selecting the profile
asserts these behavioral semantics; it does not infer algorithms from natural
language. An explicitly requested non-FIFO scheduler, unsupported interface,
configuration, reset convention, missing required bindings, or review-phase
handoff fails before simulation. Other architectures need another executable
profile; structural handoff validity alone does not establish compatibility.

The driver holds payload and valid through backpressure and samples all traces
before the rising edge. Reset is asserted before that sampling phase while
valid/write/payload remain nonzero, allowing immediate gating failures to show.
Raw event predicates remain visible during reset; the driver does not hide
incorrect DUT gating.

Five bounded scenarios run independently, resetting the DUT for each:

| Scenario | Stimulus |
| --- | --- |
| `reset_zero` | Read all 16 locations in each bank after reset. |
| `fifo_memory` | Interleaved writes/reads, repeated addresses, row hits/conflicts and bank isolation. |
| `queue_pressure` | 160 reproducible mixed transactions; require host stalls and full-queue simultaneous dispatch/admission. |
| `refresh_traffic` | Traffic across periodic refresh, active drain under pending refresh, at least two REF services, subsequent admissions/reads. |
| `reset_inflight` | Reset with accepted work outstanding, require cancellation, then check reset-zero reads. |

The Python oracle uses accepted host requests, its own waiting FIFO, active and
cleanup ownership, banked memory, row state, and command timestamps. Expected
data is never read from DUT memory. Packed field layouts, opcode encodings and
numeric timing values come from the authenticated shared definitions. Checks
include complete FIFO payload order, capacity/admission behavior, ACT/PRE/access
legality, write payloads, exact READ-to-response latency and data, candidate hold
and dispatch/commit bubbles, idle/cleanup/refresh readiness gating, row updates,
tRCD/tRAS/tRP/tRRD/tFAW/tRFC minima, predicted periodic pending, ordered refresh
closure, and refresh service duration. Unbound deadline/end events are inferred
from reset epochs and REF timestamps rather than invented observation pulses.

## Evidence and limits

Each run contains a frozen `inputs/` tree, generated `testbench.sv`, simulator
build/logs, per-scenario `.vectors`/`.trace`/`.log` files and
`simulation_report.json`. The report binds the original package, manifest, RTL
revision and handoff fingerprint, records commands, simulator version, seed,
runner-source hashes, evidence hashes, executed check counters and the first
checker failure in each scenario. It reports `passed`, `failed`, `tool_error`
or `invalid_input`; CLI exit codes are 0, 1, or 2 for pass, behavioral failure,
or input/tool failure respectively. Cycle-limit exhaustion is a bounded-completion
failure, not a successful process run. Missing/empty/malformed traces cannot pass.

All source requirement and obligation keys are retained with `unverified`
status. Passed scenario/check records are separate: limited directed execution
does not discharge the complete prose, configuration matrix or verification plan.
This version has no isolated-module permission tests, exhaustive timing-boundary
tests, coverage closure, UVM execution or on-chip BIST circuitry. Verilator's
two-state initialization cannot establish X-propagation correctness. The legacy
consumer's `adapter_required` status and original build's `not_run` metadata are
not rewritten; local simulation has its own evidence report.

Offline regressions, including an actual Verilator driver test and injected
scoreboard failures:

```sh
cd local/frontend
python3 -m unittest test_simulate_handoff test_verification_handoff -v
```

## Single-request open-page profile

For the newly generated 8-bit-address design, select the additional profile:

```sh
python3 local/frontend/simulate_handoff.py \
  local/frontend/microarch/builds/text_runs/20261027T182419Z_27739196/build/verification_handoff.json \
  --profile ddr4-single-request-v1 --seed 1
```

This profile requires four banks, 256 32-bit words per bank, one outstanding
request, 4-bit row/column address fields, synchronous active-low reset, and the
ACT/PRE/READ/WRITE/NOP command vocabulary. It checks the declared tRCD, tRAS,
tRRD and tFAW cycles. It does not assume a waiting FIFO, refresh or tRP, and it
rejects incompatible interfaces/layouts, refresh requests and timing sets. The
original `ddr4-fifo-v1` profile remains the default; profiles are explicit semantic
contracts, not automatic interpretations of arbitrary natural-language specs.

The driver uses authenticated declared-port event/observation bindings and
pre-edge sampling. Its independent Python model captures host admissions,
tracks one outstanding request, maintains its own banked memory and open rows,
and derives candidate legality from committed history. It checks exact command
mapping/sequence, blocked-candidate stability, backpressure, reset gating,
READ response latency/data retention, and completion. Expected read data never
comes from DUT memory.

Five scenarios execute:

- `reset_zero`: read every address in every bank (1,024 locations).
- `memory_row_paths`: distinct writes to all banks before readback, including
  row hits, row conflicts, row/column mapping and bank isolation.
- `backpressure_capture`: 160 seeded mixed transactions plus a final read;
  changing subsequent host requests must not change the active captured payload.
- `timing_row_conflict`: require executed tRCD/tRAS blocked edges and exact
  E+T boundary eligibility, with PRE/ACT/access sequencing.
- `reset_inflight`: cancel queued/active work and a pending read response, then
  confirm reset-zero behavior and resumed service.

The saved generated RTL passed all five scenarios for seeds 1, 2 and 17:
**15 scenario passes, 39,488 sampled cycles**. Final reports are under the saved
build's `simulations/20261027T195551Z_7385a91c`,
`simulations/20261027T195559Z_9c73f343`, and
`simulations/20261027T195606Z_7f01d786` directories. Earlier runs remain as evidence,
including a correctly rejected attempt to use the incompatible FIFO profile.
Original RTL, reviewed caches and authenticated handoff contents are unchanged.
No LLM or network access is used.

These are bounded system scenarios. tRRD/tFAW are checked on encountered
candidates, but isolated DUT timing probes and exhaustive boundaries are not
covered. In particular, this single-request system's long tRCD serializes ACTs
so normal traffic does not challenge the four-ACT tFAW limit. Unit fault tests
validate the oracle's timing boundaries; that is separate from exercising those
boundaries in DUT simulation. Source requirements remain `unverified`, and the
immutable frontend build metadata is not rewritten; the simulation report owns
local executed evidence. The existing two-state/X-propagation and coverage limits
still apply.

```sh
python3 -m unittest discover -s local/frontend -p test_single_request_simulation.py -v
```

Validation of the added profile: all **585 frontend tests passed** with network
connections blocked, including 11 new independent-trace/profile tests. The 44
focused simulation, handoff and text-intake regressions passed. Python compilation
and whitespace checks passed. Deliberate wrong-data, response, timing, candidate,
reset and capacity mutations were rejected by the checker.

## Isolated timing guard

`--profile ddr4-timing-guard-v1` additionally instantiates the authenticated
handoff's timing-role module in isolation. Its nine scenarios directly exercise
tRCD/tRAS/tRRD T-1/T/T+1 boundaries, the four-ACT tFAW limit and exact expiration
with a simultaneous new ACT, reset/invalid gating, noncommitting probes,
per-bank history, long idle and seeded inputs. It uses bank-state/commit stubs;
component probes need not be reachable through a complete controller. See
[campaign groundwork](../campaigns/README.md) for contracts, preparation, commands
and the distinction between new design trials and testing existing generated RTL.
The existing FIFO and single-request simulation defaults are unchanged.
