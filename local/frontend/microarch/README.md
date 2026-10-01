# Microarchitecture flow

Run `./start_flow.sh` from `local/frontend/`, or pass a config, basic YAML, or basic YAML directory to `run_flow.py`.

1. `configure_from_text.py` writes `configs/user_input.yaml` (skipped by `start_flow.sh --cache`).
2. `run_flow.py` validates the user config and writes selected design specs to `microarch/gen_basic/*.yaml`. Disabled designs are removed from basic, expanded, IR, and RTL outputs.
3. `expand_spec.py` reads each basic spec plus `microarch/jedec/jedec_dictionary.yaml` and `feature_templates.yaml`. It substitutes timing values and writes module YAMLs directly into `microarch/gen_exp/`. A `<design>_master.yaml` manifest is emitted only for designs with multiple modules or interface connections (currently `ddr4_bank`).
4. `run_flow.py` discovers module YAMLs by the selected design filename prefixes, excluding masters. `design.py` validates each module, writes `microarch/ir/*_ir.json`, and generates SystemVerilog in `rtl_output/`.
5. `generate_wrapper.py` uses the generated RTL, basic specs, and IR to produce the top-level wrapper. `generate_testbench.py` generates the testbench in `tb/`.
6. `run_flow.py` enforces timescale directives and runs full-system lint unless `--no-lint` is set.

`gen_basic/` and `gen_exp/` are flat directories. The bank master retains the submodule list and interface map for inspection and `check_interfaces.py`; `ddr4_bank_top.sv` generation uses module metadata directly. Master module paths are relative to the manifest directory. `ir/` contains JSON representations; `jedec/` contains the timing profiles and feature templates used by expansion.

`clear.sh` removes generated basic/expanded specs and IR outputs while preserving `microarch/jedec/`. `run_sim.sh` reads scheduler configuration from the new microarchitecture paths.
