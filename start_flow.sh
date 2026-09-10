#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./start_flow.sh [FLAGS]

Flags:
  --interactive   Prompt before each generation-flow step.
  --no-lint       Skip both dual-LLM single-file lint and full-system lint.
  --cache         Use existing configs/user_input.yaml and cached dual-LLM RTL; do not call LLMs.
  -h, --help      Show this help message.
EOF
}

interactive=0
no_lint=0
cache=0

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

if [ "$cache" -eq 1 ]; then
    python3 run_flow.py configs/user_input.yaml "${flow_args[@]}"
else
    python3 configure_from_text.py --run-flow "${flow_args[@]}"
fi
