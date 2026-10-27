#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Opt-in intake is dispatched before legacy logging/parsing. Its own parser
# rejects conflicting legacy options and keeps all artifacts in an isolated run.
from_text=0
for arg in "$@"; do
    if [ "$arg" = '--from-text' ]; then from_text=1; fi
done
if [ "$from_text" -eq 1 ]; then
    text_args=()
    for arg in "$@"; do
        case "$arg" in
            --from-text|--specification) ;;
            *) text_args+=("$arg") ;;
        esac
    done
    exec python3 -u "$SCRIPT_DIR/configure_microarch_from_text.py" "${text_args[@]}"
fi

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
  --specification Generate system specification, then architecture/contracts/intent for review.
  --from-text     New plain-text intake -> request YAML -> specification/architecture -> RTL.
                  Use --from-text --help for intake options, including --prepare-only and --simulate.
  --microarch-only Save/validate a proposal and stop before RTL generation.
  --input PATH    Use a requirements/config YAML or architecture manifest (implies --microarch).
  --requirements-file PATH  Add free-form requirements to the proposal.
  --architecture-output PATH  Proposal output path.
  --microarch-response PATH  Replay a saved JSON proposal without an LLM call.
  --microarch-model NAME     Architecture proposal model.
  --rtl-recovery             Enable bounded RTL recovery (default for architecture RTL builds with lint).
  --no-rtl-recovery          Disable bounded RTL response recovery.
  --rtl-generator-model NAME Architecture build generator/merger model; requires an architecture mode.
  --specification-output PATH   Fresh generated-package directory.
  --specification-response PATH Replay both specification stages from JSON.
  --spec-set NAME=VALUE      Override a supplied-package parameter (repeatable).
  --spec-profile NAME        Select a supplied-package timing profile.
  --spec-approval PATH       Approval of the exact exported specification manifest.
  --build-dir PATH           Isolated architecture build directory.
  -h, --help      Show this help message.
EOF
}

interactive=0
no_lint=0
cache=0
microarch=0
specification=0
rtl_generator_override=0
rtl_recovery=0
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
        --specification)
            specification=1
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
        --rtl-recovery|--no-rtl-recovery)
            if [ "$1" = --rtl-recovery ]; then rtl_recovery=1; else rtl_recovery=0; fi
            architecture_args+=("$1")
            ;;
        --rtl-generator-model)
            if [ "$#" -lt 2 ]; then echo "$1 requires a value" >&2; exit 2; fi
            architecture_args+=("$1" "$2")
            rtl_generator_override=1
            shift
            ;;
        --requirements-file|--architecture-output|--microarch-response|--microarch-model|--build-dir|--specification-output|--specification-response|--spec-set|--spec-profile|--spec-approval)
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

if [ "$rtl_recovery" -eq 1 ] && [ "$microarch" -eq 0 ]; then
    echo '--rtl-recovery requires --microarch, --specification or --input' >&2
    exit 2
fi

if [ "$rtl_generator_override" -eq 1 ] && [ "$microarch" -eq 0 ]; then
    echo '--rtl-generator-model requires --microarch, --specification or --input' >&2
    exit 2
fi

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

if [ "$specification" -eq 1 ]; then
    python3 -u "$SCRIPT_DIR/run_flow.py" "$architecture_input" --specification "${architecture_args[@]}" "${flow_args[@]}"
elif [ "$microarch" -eq 1 ]; then
    python3 -u "$SCRIPT_DIR/run_flow.py" "$architecture_input" --microarch "${architecture_args[@]}" "${flow_args[@]}"
elif [ "$cache" -eq 1 ]; then
    python3 -u run_flow.py configs/user_input.yaml "${flow_args[@]}"
else
    python3 -u configure_from_text.py --run-flow "${flow_args[@]}"
fi
