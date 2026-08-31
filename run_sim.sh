#!/usr/bin/env bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GTK_PRESET_DIR="$SCRIPT_DIR/gtk_presets"
GTKWAVE_BIN="${GTKWAVE_BIN:-gtkwave}"

detect_scheduler_mode() {
    local expanded_yaml="$SCRIPT_DIR/expanded/ddr4_scheduler/ddr4_scheduler_scheduler.yaml"
    local generated_yaml="$SCRIPT_DIR/inputs/generated/ddr4_scheduler.yaml"
    local config_yaml="$SCRIPT_DIR/configs/user_input.yaml"
    local fallback_config="$SCRIPT_DIR/configs/simple.yaml"
    local mode=""

    if [ -f "$expanded_yaml" ]; then
        mode=$(awk -F': *' '/^[[:space:]]*scheduler_mode:[[:space:]]*/ {gsub(/"/, "", $2); print $2; exit}' "$expanded_yaml")
    fi

    if [ -z "$mode" ] && [ -f "$generated_yaml" ]; then
        if grep -q 'scheduler_round_robin' "$generated_yaml"; then
            mode="round_robin"
        elif grep -Eq '^[[:space:]]*-[[:space:]]*scheduler[[:space:]]*$' "$generated_yaml"; then
            mode="simple"
        fi
    fi

    if [ -z "$mode" ] && [ -f "$config_yaml" ]; then
        mode=$(awk -F': *' '/^[[:space:]]*scheduler:[[:space:]]*/ {gsub(/"/, "", $2); print $2; exit}' "$config_yaml")
    fi

    if [ -z "$mode" ] && [ -f "$fallback_config" ]; then
        mode=$(awk -F': *' '/^[[:space:]]*scheduler:[[:space:]]*/ {gsub(/"/, "", $2); print $2; exit}' "$fallback_config")
    fi

    if [ "$mode" = "round_robin" ] || [ "$mode" = "simple" ]; then
        printf '%s\n' "$mode"
    fi
}

detect_bank_count() {
    local config_yaml="$SCRIPT_DIR/configs/user_input.yaml"
    local fallback_config="$SCRIPT_DIR/configs/simple.yaml"
    local wrapper_sv="$SCRIPT_DIR/rtl_output/ddr4_controller_top/ddr4_controller_top.sv"
    local banks=""

    # Prefer validated user config if present
    if [ -f "$config_yaml" ]; then
        banks=$(awk -F': *' '/^[[:space:]]*banks:[[:space:]]*/ {gsub(/"/, "", $2); print $2; exit}' "$config_yaml")
    fi

    if [ -z "$banks" ] && [ -f "$fallback_config" ]; then
        banks=$(awk -F': *' '/^[[:space:]]*banks:[[:space:]]*/ {gsub(/"/, "", $2); print $2; exit}' "$fallback_config")
    fi

    # Fall back to generated wrapper metadata comment
    if [ -z "$banks" ] && [ -f "$wrapper_sv" ]; then
        banks=$(awk -F': *' '/^[[:space:]]*\/\/[[:space:]]*Bank count[[:space:]]*:[[:space:]]*/ {gsub(/"/, "", $2); print $2; exit}' "$wrapper_sv")
    fi

    case "$banks" in
        1|2|4)
            printf '%s\n' "$banks"
            ;;
        *)
            printf '1\n'
            ;;
    esac
}

select_gtkwave_preset() {
    local scheduler_mode="$1"
    local bank_count="$2"
    local candidate=""

    case "$scheduler_mode" in
        round_robin)
            candidate="$GTK_PRESET_DIR/round_robin_controller_demo.gtkw"
            if [ -f "$candidate" ]; then
                printf '%s\n' "$candidate"
                return 0
            fi

            candidate="$GTK_PRESET_DIR/simple_controller_demo.gtkw"
            if [ -f "$candidate" ]; then
                printf '%s\n' "$candidate"
                return 0
            fi
            ;;
        simple)
            if [ "$bank_count" = "4" ]; then
                candidate="$GTK_PRESET_DIR/memsys_demo_4bank_simple.gtkw"
                if [ -f "$candidate" ]; then
                    printf '%s\n' "$candidate"
                    return 0
                fi
            fi

            candidate="$GTK_PRESET_DIR/simple_controller_demo.gtkw"
            if [ -f "$candidate" ]; then
                printf '%s\n' "$candidate"
                return 0
            fi
            ;;
    esac

    return 1
}

open_waveform() {
    local vcd_file="$1"
    local scheduler_mode=""
    local bank_count=""
    local preset=""
    local open_wave=""
    local preset_label=""

    if ! command -v "$GTKWAVE_BIN" >/dev/null 2>&1 || [ ! -f "$vcd_file" ]; then
        return 0
    fi

    read -r -p "[SIM] Open waveform in GTKWave? (y/n): " open_wave
    if [ "$open_wave" != "y" ]; then
        return 0
    fi

    scheduler_mode=$(detect_scheduler_mode)
    bank_count=$(detect_bank_count)

    if preset=$(select_gtkwave_preset "$scheduler_mode" "$bank_count"); then
        preset_label="${preset#$SCRIPT_DIR/}"
        echo "[SIM] Scheduler: ${scheduler_mode:-unknown} | Banks: ${bank_count:-unknown}"
        echo "[SIM] Using GTKWave preset: $preset_label"
        "$GTKWAVE_BIN" "$vcd_file" "$preset" &
        return 0
    fi

    if [ -n "$scheduler_mode" ]; then
        echo "[SIM] Scheduler: ${scheduler_mode} | Banks: ${bank_count:-unknown}"
        echo "[SIM] Preset not found for scheduler '$scheduler_mode' banks '$bank_count', falling back to raw VCD"
    else
        echo "[SIM] Scheduler mode unavailable, falling back to raw VCD"
    fi
    "$GTKWAVE_BIN" "$vcd_file" &
}

main() {
    local rtl_files=""
    local tb_file="$SCRIPT_DIR/tb/tb_ddr4_controller_top.sv"
    local vcd_file="$SCRIPT_DIR/tb_ddr4_controller_top.vcd"

    cd "$SCRIPT_DIR"

    echo "[SIM] Collecting RTL files..."
    rtl_files=$(find rtl_output -name "*.sv")

    if [ ! -f "$tb_file" ]; then
        echo "[ERROR] Testbench not found: $tb_file"
        exit 1
    fi

    echo "[SIM] Compiling with Verilator..."
    verilator --binary \
        $rtl_files \
        "$tb_file" \
        --top-module tb_ddr4_controller_top \
        --trace \
        -Wno-fatal \
        --trace-structs

    echo "[SIM] Running simulation..."
    ./obj_dir/Vtb_ddr4_controller_top

    if [ -f "$vcd_file" ]; then
        echo "[SIM] Waveform generated: ${vcd_file#$SCRIPT_DIR/}"
    else
        echo "[WARNING] No waveform file found."
    fi

    open_waveform "$vcd_file"

    echo "[SIM] Done."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi