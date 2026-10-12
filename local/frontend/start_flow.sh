#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/flow.log"
export PYTHONUNBUFFERED=1
exec > >(tee "$LOG_FILE") 2>&1

usage() {
    cat <<'EOF'
Usage: ./start_flow.sh [FLAGS]

Flags:
  --interactive   Prompt before each generation-flow step.
  --no-lint       Skip both dual-LLM single-file lint and full-system lint.
  --cache         Use existing configs/user_input.yaml and cached dual-LLM RTL; do not call LLMs.
  --microarch     Use the architecture proposal/graph flow with the existing config.
  --microarch-only Save/validate a proposal and stop before RTL generation.
  --input PATH    Use a requirements/config YAML or architecture manifest (implies --microarch).
  --requirements-file PATH  Add free-form requirements to the proposal.
  --architecture-output PATH  Proposal output path.
  --microarch-response PATH  Replay a saved JSON proposal without an LLM call.
  --microarch-model NAME     Architecture proposal model.
  --build-dir PATH           Isolated architecture build directory.
  -h, --help      Show this help message.
EOF
}

interactive=0
no_lint=0
cache=0
microarch=0
architecture_input="configs/user_input.yaml"
architecture_args=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --interactive)
            interactive=1
            ;;
        --no-lint)
            no_lint=1
            ;;
        --cache)
            cache=1
            ;;
        --microarch)
            microarch=1
            ;;
        --microarch-only)
            microarch=1
            architecture_args+=(--microarch-only)
            ;;
        --input)
            if [ "$#" -lt 2 ]; then echo "--input requires a path" >&2; exit 2; fi
            shift
            architecture_input="$1"
            microarch=1
            ;;
        --requirements-file|--architecture-output|--microarch-response|--microarch-model|--build-dir)
            if [ "$#" -lt 2 ]; then echo "$1 requires a value" >&2; exit 2; fi
            architecture_args+=("$1" "$2")
            shift
            microarch=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown flag: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

flow_args=()
if [ "$interactive" -eq 1 ]; then
    flow_args+=(--interactive)
fi
if [ "$no_lint" -eq 1 ]; then
    flow_args+=(--no-lint)
fi
if [ "$cache" -eq 1 ]; then
    flow_args+=(--cache)
fi

if [ "$microarch" -eq 1 ]; then
    python3 -u run_flow.py "$architecture_input" --microarch "${architecture_args[@]}" "${flow_args[@]}"
elif [ "$cache" -eq 1 ]; then
    python3 -u run_flow.py configs/user_input.yaml "${flow_args[@]}"
else
    python3 -u configure_from_text.py --run-flow "${flow_args[@]}"
fi
