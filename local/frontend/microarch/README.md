# Microarchitecture flow

For plain-text user specifications, run `bash local/frontend/start_flow.sh
--specification --from-text` from the repository root. It prompts for a description,
shows request YAML for confirmation, and runs the staged specification/architecture
and RTL flow in an isolated directory. See [plain_text_input.md](plain_text_input.md)
for options and limits. The default legacy text/extraction/expand-spec route is
unchanged.

Completed specification-derived RTL builds can now run the standalone local
[simulation runner](../simulation/README.md). It consumes the isolated build's
`verification_handoff.json`, runs an explicit FIFO-controller profile with
Verilator, and preserves separate behavioral evidence without changing the
legacy `run_sim.sh` flow or marking whole source requirements verified.

The new **LLM architecture proposal + deterministic graph compiler** path is
available through `run_flow.py --microarch` or `--microarch-only`. It supports
reusable and custom module specs, generates integration from the proposed graph,
and uses isolated build outputs. See [architecture_flow.md](architecture_flow.md)
for usage, the manifest contract, verification status, and current limitations.
The input-expansion flow below remains the default during migration.

The [system specification draft](specification/README.md) starts implementation
plan 1A with shared definitions, system behavior, all controller module contracts,
and a typed connection graph. It is unapproved review material; the current
generator does not consume this format.

Run `./start_flow.sh` from `local/frontend/`, or pass a config, basic YAML, or basic YAML directory to `run_flow.py`.

1. `configure_from_text.py` writes `configs/user_input.yaml` (skipped by `start_flow.sh --cache`).
2. `run_flow.py` validates the user config and writes selected design specs to `microarch/gen_basic/*.yaml`. Disabled designs are removed from basic, expanded, IR, and RTL outputs.
3. `expand_spec.py` reads each basic spec plus `microarch/jedec/jedec_dictionary.yaml` and `feature_templates.yaml`. It substitutes timing values and writes module YAMLs directly into `microarch/gen_exp/`. A `<design>_master.yaml` manifest is emitted only for designs with multiple modules or interface connections (currently `ddr4_bank`).
4. `run_flow.py` discovers module YAMLs by the selected design filename prefixes, excluding masters. `design.py` validates each module, writes `microarch/ir/*_ir.json`, and generates SystemVerilog in `rtl_output/`.
5. `generate_wrapper.py` uses the generated RTL, basic specs, and IR to produce the top-level wrapper. `generate_testbench.py` generates the testbench in `tb/`.
6. `run_flow.py` enforces timescale directives and runs full-system lint unless `--no-lint` is set.

`gen_basic/` and `gen_exp/` are flat directories. The bank master retains the submodule list and interface map for inspection and `check_interfaces.py`; `ddr4_bank_top.sv` generation uses module metadata directly. Master module paths are relative to the manifest directory. `ir/` contains JSON representations; `jedec/` contains the timing profiles and feature templates used by expansion.

`clear.sh` removes generated basic/expanded specs and IR outputs while preserving `microarch/jedec/`. `run_sim.sh` reads scheduler configuration from the new microarchitecture paths.

Controller handoffs use manifest v2 for server system verification. Run
`python3 generate_wrapper.py --manifest-only` from `local/frontend` to refresh
metadata for existing RTL without rewriting RTL or the testbench. This adds
feature master indexes for single-module designs as well as the bank design;
the server uses those indexes to check enabled feature reachability. The top
orchestrator performs this refresh after successful frontend checks and publishes
all referenced configuration/spec/IR/timing/testbench inputs in the mailbox.

## Diagnosing dual-LLM generation failures

The dual-LLM API helper logs the model, `finish_reason`, completion token usage,
and requested output budget for each response. `finish_reason=length` means the
provider cut off its response: incomplete JSON is a generation/API failure.
The helper starts at 8,192 output tokens and retries a truncated response with
up to 16,384 tokens before stopping with a clear error. Override the budgets
with `DUAL_LLM_MAX_TOKENS` and `DUAL_LLM_MAX_RETRY_TOKENS`; both must be positive
integers, and the retry ceiling must be at least the initial budget. Provider
model limits still apply. Retries make additional API calls.

HTTP errors, network failures, unexpected/empty API responses, and JSON parsing
failures are distinct from Verilator diagnostics. Empty generated RTL stops
before lint/review. A parsed, nonempty module can still fail lint or specification
review; increasing the response budget does not fix RTL behavior errors.

Run the API handling regressions without network calls from `local/frontend`:

```bash
python3 -m unittest test_dual_llm_api -v
```
