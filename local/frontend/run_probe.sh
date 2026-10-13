#!/usr/bin/env bash
set -euo pipefail
probe_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
frontend_dir="$(pwd)"
if [[ ! -f "$frontend_dir/rtl_output/ddr4_scheduler_scheduler.sv" ]]; then
    echo 'Run this script from CAPSTONE/local/frontend.' >&2
    exit 2
fi
probe_run_dir="$(mktemp -d "${TMPDIR:-/tmp}/capstone-scheduler-probe.XXXXXX")"
echo "Probe evidence: $probe_run_dir"
verilator --version | tee "$probe_run_dir/version.txt"
sha256sum "$frontend_dir/rtl_output/ddr4_scheduler_scheduler.sv" "$frontend_dir/tb/tb_ddr4_controller_top.sv" | tee "$probe_run_dir/sources.sha256"
probe_failed=0
for mode in default no_optimization; do
    args=()
    if [[ "$mode" == no_optimization ]]; then args+=(-O0); fi
    echo "Testing $mode"
    if ! verilator --binary -j 2 --timing -Wno-fatal "${args[@]}" \
        --top-module scheduler_input_probe --Mdir "$probe_run_dir/$mode" \
        "$frontend_dir/rtl_output/ddr4_scheduler_scheduler.sv" \
        "$probe_dir/scheduler_input_probe.sv" > "$probe_run_dir/$mode.compile.log" 2>&1; then
        cat "$probe_run_dir/$mode.compile.log"
        probe_failed=1
        continue
    fi
    if ! "$probe_run_dir/$mode/Vscheduler_input_probe" > "$probe_run_dir/$mode.simulation.log" 2>&1; then
        probe_failed=1
    fi
    cat "$probe_run_dir/$mode.simulation.log"
done
exit "$probe_failed"
