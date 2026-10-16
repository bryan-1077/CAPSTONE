#!/usr/bin/env python3
"""Orchestrate config expansion, RTL generation, and wrapper generation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from generate_testbench import generate_testbench

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass


SCRIPT_DIR = Path(__file__).resolve().parent
EXPANDED_DIR = SCRIPT_DIR / "microarch" / "gen_exp"
GENERATED_INPUT_DIR = SCRIPT_DIR / "microarch" / "gen_basic"
FLOW_LINT_LOG = SCRIPT_DIR / "flow_lint.log"
GENERATED_INPUT_SPECS = [
    ("ddr4_bank", None, ["basic_commands"]),
    ("ddr4_request_queue", None, ["request_queue"]),
    ("ddr4_tFAW", "tFAW", ["tFAW_tracker"]),
    ("ddr4_tRRD", "tRRD", ["tRRD"]),
    ("ddr4_scheduler", "scheduler", ["scheduler"]),
    ("ddr4_refresh", "refresh", ["refresh_controller"]),
]
SCHEDULER_FEATURE_MAP = {
    "simple": ["scheduler"],
    "round_robin": ["scheduler_round_robin"],
}
SUPPORTED_MEMORY_PROTOCOLS = {"DDR4"}
SUPPORTED_MEMORY_SPEEDS = {"2400", "3200"}
SUPPORTED_BANK_COUNTS = {1, 2, 4}
BOOLEAN_FEATURE_KEYS = ("refresh", "tFAW", "tRRD")
SUPPORTED_PAGE_POLICIES = {"open_page", "close_page"}
DEFAULT_PAGE_POLICY = "open_page"
MANAGED_DESIGN_ARTIFACTS = {
    "ddr4_bank": {
        "ir_modules": [
            "ddr4_bank_activate_fsm",
            "ddr4_bank_bank_sequencer",
            "ddr4_bank_precharge_fsm",
            "ddr4_bank_tRAS_fsm",
        ],
        "rtl_modules": [
            "ddr4_bank_activate_fsm",
            "ddr4_bank_bank_sequencer",
            "ddr4_bank_precharge_fsm",
            "ddr4_bank_tRAS_fsm",
        ],
    },
    "ddr4_tFAW": {
        "ir_modules": ["ddr4_tFAW_tFAW_tracker"],
        "rtl_modules": ["ddr4_tFAW_tFAW_tracker"],
    },
    "ddr4_request_queue": {
        "ir_modules": ["ddr4_request_queue"],
        "rtl_modules": ["ddr4_request_queue"],
    },
    "ddr4_tRRD": {
        "ir_modules": ["ddr4_tRRD_simple_tRRD"],
        "rtl_modules": ["ddr4_tRRD_simple_tRRD"],
    },
    "ddr4_scheduler": {
        "ir_modules": ["ddr4_scheduler_scheduler"],
        "rtl_modules": ["ddr4_scheduler_scheduler"],
    },
    "ddr4_refresh": {
        "ir_modules": ["ddr4_refresh_refresh_controller"],
        "rtl_modules": ["ddr4_refresh_refresh_controller"],
    },
}


def parse_args() -> argparse.Namespace:
    from generate_microarch import DEFAULT_MODEL

    parser = argparse.ArgumentParser(
        description="Run config expansion, expand_spec, design generation, and wrapper generation."
    )
    parser.add_argument("input_yaml", help="Path to a user config, architecture manifest, or system-specification package.")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt before each flow step.",
    )
    parser.add_argument(
        "--no-lint",
        action="store_true",
        help="Skip dual-LLM single-file lint and full-system lint.",
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        help="Use cached dual-LLM RTL for cache-backed modules and avoid LLM calls.",
    )
    parser.add_argument("--microarch", action="store_true", help="Propose an LLM architecture before generation.")
    parser.add_argument("--specification", action="store_true",
                        help="Generate system behavior first, then contracts/graph/intent; requires review before RTL.")
    parser.add_argument("--specification-output", type=Path, help="Fresh directory for generated specification source files.")
    parser.add_argument("--specification-response", type=Path,
                        help="Replay system and architecture stage responses from one JSON file, without model calls.")
    parser.add_argument("--microarch-only", action="store_true", help="Save/validate the architecture and stop before RTL.")
    parser.add_argument("--requirements-file", type=Path, help="Additional free-form architecture requirements.")
    parser.add_argument("--architecture-output", type=Path, default=SCRIPT_DIR / "microarch/proposals/architecture.yaml")
    parser.add_argument("--microarch-model", default=DEFAULT_MODEL,
                        help="Architecture model (defaults to generate_microarch.DEFAULT_MODEL).")
    parser.add_argument("--microarch-response", type=Path, help="Replay a saved JSON architecture without calling the LLM.")
    parser.add_argument("--build-dir", type=Path, help="Architecture build directory (defaults to content-addressed microarch/builds/).")
    parser.add_argument("--spec-set", action="append", default=[], metavar="NAME=VALUE",
                        help="Override a configurable system-package parameter (repeatable).")
    parser.add_argument("--spec-profile", help="Select a system-package timing profile.")
    parser.add_argument("--spec-approval", type=Path, help="Recorded approval of this exact specification-derived architecture.")
    return parser.parse_args()


def display_path(path: Path) -> str:
    """Return a readable path for logs and prompts."""
    try:
        return path.resolve().relative_to(SCRIPT_DIR).as_posix()
    except ValueError:
        return str(path.resolve())


def prompt_yes_no(message: str) -> bool:
    """Return True for yes and False for no."""
    while True:
        reply = input(message).strip().lower()
        if reply in {"y", "yes"}:
            return True
        if reply in {"n", "no"}:
            return False
        print("Please enter y or n.")


def run_command(command: list[str]) -> None:
    """Run a subprocess in the flow directory and raise on failure."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    subprocess.run(command, check=True, cwd=SCRIPT_DIR, env=env)


def design_command(yaml_path: Path, *, no_lint: bool, cache: bool) -> list[str]:
    """Build the design.py command with generation-flow flags."""
    command = [sys.executable, "design.py", str(yaml_path)]
    if no_lint:
        command.append("--no-lint")
    if cache:
        command.append("--cache")
    return command


def load_yaml(path: Path) -> dict:
    """Load a YAML file into a dictionary."""
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def write_yaml(path: Path, data: dict) -> None:
    """Write a YAML dictionary to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)


def require_config_value(config: dict, *keys: str):
    """Return a nested config value or raise a clear error."""
    current = config
    key_path = ".".join(keys)
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise ValueError(f"Missing required config field: {key_path}")
        current = current[key]
    return current


@dataclass(frozen=True)
class ValidatedUserConfig:
    """Normalized user-config values after fail-fast validation."""

    protocol: str
    speed: str
    bank_count: int
    scheduler: str
    refresh: bool
    tfaw: bool
    trrd: bool
    page_policy: str


def _require_mapping(parent: dict, key: str, key_path: str) -> dict:
    """Return a nested mapping value or raise a clear validation error."""
    value = parent.get(key)
    if value is None:
        raise ValueError(f"Missing required config field: {key_path}")
    if not isinstance(value, dict):
        raise ValueError(f"Expected {key_path} to be a mapping")
    return value


def _require_string(parent: dict, key: str, key_path: str) -> str:
    """Return a string config value or raise a clear validation error."""
    value = parent.get(key)
    if value is None:
        raise ValueError(f"Missing required config field: {key_path}")
    if not isinstance(value, str):
        raise ValueError(f"Expected {key_path} to be a string")
    return value


def _require_bool(parent: dict, key: str, key_path: str) -> bool:
    """Return a boolean config value or raise a clear validation error."""
    value = parent.get(key)
    if value is None:
        raise ValueError(f"Missing required config field: {key_path}")
    if not isinstance(value, bool):
        raise ValueError(f"Expected {key_path} to be a boolean")
    return value


def _require_int(parent: dict, key: str, key_path: str) -> int:
    """Return an integer config value or raise a clear validation error."""
    value = parent.get(key)
    if value is None:
        raise ValueError(f"Missing required config field: {key_path}")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Expected {key_path} to be an integer")
    return value


def validate_user_config(config: dict) -> ValidatedUserConfig:
    """Validate the user-facing config before any generation side effects occur."""
    if "design_name" in config and not isinstance(config["design_name"], str):
        raise ValueError("Expected design_name to be a string")

    if "profile" in config and not isinstance(config["profile"], dict):
        raise ValueError("Expected profile to be a mapping")

    memory_cfg = _require_mapping(config, "memory", "memory")
    features_cfg = _require_mapping(config, "features", "features")
    topology_cfg = config.get("topology")
    if topology_cfg is not None and not isinstance(topology_cfg, dict):
        raise ValueError("Expected topology to be a mapping")

    protocol = _require_string(memory_cfg, "protocol", "memory.protocol")
    if protocol not in SUPPORTED_MEMORY_PROTOCOLS:
        supported = ", ".join(sorted(SUPPORTED_MEMORY_PROTOCOLS))
        raise ValueError(
            f"Unsupported protocol '{protocol}'. Supported protocols: {supported}"
        )

    speed = _require_string(memory_cfg, "speed", "memory.speed")
    if speed not in SUPPORTED_MEMORY_SPEEDS:
        supported = ", ".join(sorted(SUPPORTED_MEMORY_SPEEDS))
        raise ValueError(
            f"Unsupported speed '{speed}'. Supported speeds: {supported}"
        )

    scheduler = _require_string(features_cfg, "scheduler", "features.scheduler")
    if scheduler not in SCHEDULER_FEATURE_MAP:
        supported = ", ".join(sorted(SCHEDULER_FEATURE_MAP))
        raise ValueError(
            f"Unsupported scheduler mode '{scheduler}'. Supported modes: {supported}"
        )

    bank_count = 1
    if topology_cfg is not None:
        bank_count = _require_int(topology_cfg, "banks", "topology.banks")
        if bank_count not in SUPPORTED_BANK_COUNTS:
            supported = ", ".join(str(value) for value in sorted(SUPPORTED_BANK_COUNTS))
            raise ValueError(
                f"Unsupported bank count '{bank_count}'. Supported bank counts: {supported}"
            )

    if bank_count > 1 and scheduler != "simple":
        raise ValueError(
            "Unsupported configuration: topology.banks>1 currently requires features.scheduler='simple'"
        )

    feature_values: dict[str, bool] = {}
    for feature_key in BOOLEAN_FEATURE_KEYS:
        feature_values[feature_key] = _require_bool(
            features_cfg,
            feature_key,
            f"features.{feature_key}",
        )

    page_policy = features_cfg.get("page_policy", DEFAULT_PAGE_POLICY)
    if not isinstance(page_policy, str):
        raise ValueError("Expected features.page_policy to be a string")
    if page_policy not in SUPPORTED_PAGE_POLICIES:
        supported = ", ".join(sorted(SUPPORTED_PAGE_POLICIES))
        raise ValueError(
            f"Unsupported page policy '{page_policy}'. Supported policies: {supported}"
        )

    return ValidatedUserConfig(
        protocol=protocol,
        speed=speed,
        bank_count=bank_count,
        scheduler=scheduler,
        refresh=feature_values["refresh"],
        tfaw=feature_values["tFAW"],
        trrd=feature_values["tRRD"],
        page_policy=page_policy,
    )


def build_generated_input(
    protocol: str,
    speed: str,
    bank_count: int,
    page_policy: str,
    design_name: str,
    features: list[str],
) -> dict:
    """Build one internal input YAML payload."""
    return {
        "design_name": design_name,
        "design_type": "memory_controller",
        "controller_config": {
            "protocol": protocol,
            "jedec_profile": f"{protocol}-{speed}",
            "page_policy": page_policy,
            "topology": {
                "banks": bank_count,
            },
            "features": features,
        },
    }


def remove_path(path: Path) -> None:
    """Remove a file or directory when it exists."""
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def cleanup_disabled_artifacts(selected_design_names: set[str]) -> None:
    """Remove stale expanded/IR/RTL artifacts for managed designs disabled by config."""
    for design_name, artifacts in MANAGED_DESIGN_ARTIFACTS.items():
        if design_name in selected_design_names:
            continue

        remove_path(EXPANDED_DIR / f"{design_name}_master.yaml")
        for expanded_path in EXPANDED_DIR.glob(f"{design_name}_*.yaml"):
            remove_path(expanded_path)

        for module_name in artifacts["ir_modules"]:
            remove_path(SCRIPT_DIR / "microarch" / "ir" / f"{module_name}_ir.json")

        for module_name in artifacts["rtl_modules"]:
            remove_path(SCRIPT_DIR / "rtl_output" / f"{module_name}.sv")


def expand_user_config(config: ValidatedUserConfig) -> Path:
    """Generate internal input YAML files from a validated user config."""
    print("[CONFIG] Detected user config")
    print("[CONFIG] Generating internal input YAMLs...")

    GENERATED_INPUT_DIR.mkdir(parents=True, exist_ok=True)

    selected_files: set[Path] = set()
    managed_files = {
        GENERATED_INPUT_DIR / f"{design_name}.yaml"
        for design_name, _, _ in GENERATED_INPUT_SPECS
    }

    for design_name, feature_key, module_features in GENERATED_INPUT_SPECS:
        selected_features = module_features

        if feature_key == "scheduler":
            selected_features = SCHEDULER_FEATURE_MAP[config.scheduler]
        elif feature_key == "refresh" and not config.refresh:
            continue
        elif feature_key == "tFAW" and not config.tfaw:
            continue
        elif feature_key == "tRRD" and not config.trrd:
            continue

        output_path = GENERATED_INPUT_DIR / f"{design_name}.yaml"
        payload = build_generated_input(
            config.protocol,
            config.speed,
            config.bank_count,
            config.page_policy,
            design_name,
            selected_features,
        )
        write_yaml(output_path, payload)
        selected_files.add(output_path)
        print(f"[CONFIG] Generated: {display_path(output_path)}")

    for stale_path in managed_files - selected_files:
        if stale_path.exists():
            stale_path.unlink()

    cleanup_disabled_artifacts({path.stem for path in selected_files})

    return GENERATED_INPUT_DIR


def maybe_expand_user_config(input_path: Path) -> tuple[Path, ValidatedUserConfig | None]:
    """Validate a user config before generation, then expand it transactionally."""
    if input_path.is_dir():
        return input_path, None

    spec = load_yaml(input_path)
    if "profile" not in spec:
        return input_path, None

    validated_config = validate_user_config(spec)
    return expand_user_config(validated_config), validated_config


def print_validated_config_summary(config: ValidatedUserConfig) -> None:
    """Print a short stable summary of the validated user config."""
    print("[SUMMARY]")
    print(f"Protocol : {config.protocol}")
    print(f"Speed    : {config.speed}")
    print(f"Banks    : {config.bank_count}")
    print(f"Scheduler: {config.scheduler}")
    print(f"PagePol  : {config.page_policy}")
    print(f"Refresh  : {'enabled' if config.refresh else 'disabled'}")
    print(f"tFAW     : {'enabled' if config.tfaw else 'disabled'}")
    print(f"tRRD     : {'enabled' if config.trrd else 'disabled'}")


def collect_input_yamls(input_path: Path) -> list[Path]:
    """Return input YAML files from a file path or directory."""
    if input_path.is_file():
        return [input_path.resolve()]
    if not input_path.is_dir():
        raise ValueError(f"Input path does not exist: {input_path}")

    yaml_paths: list[Path] = []
    for root, _, files in os.walk(input_path):
        for filename in files:
            if filename.endswith(".yaml"):
                yaml_paths.append((Path(root) / filename).resolve())

    return sorted(yaml_paths)


def get_design_name(yaml_path: Path) -> str:
    """Extract the design_name from an input YAML file."""
    spec = load_yaml(yaml_path)
    design_name = spec.get("design_name")
    if not design_name:
        raise ValueError(f"Missing design_name in {yaml_path}")
    return str(design_name)


def discover_targets(input_path: Path) -> tuple[list[Path], list[str]]:
    """Collect source YAMLs and their design names for the current run."""
    source_yamls = collect_input_yamls(input_path)
    design_names = [get_design_name(path) for path in source_yamls]
    return source_yamls, design_names


def discover_expanded_yamls(expanded_dir: Path, design_names: list[str]) -> list[Path]:
    """Collect selected designs' module YAMLs without requiring manifests."""
    yaml_paths: set[Path] = set()
    for path in expanded_dir.glob("*.yaml"):
        if path.name.endswith("_master.yaml"):
            continue
        if any(path.name.startswith(f"{name}_") for name in design_names):
            yaml_paths.add(path.resolve())
    return sorted(yaml_paths)


def enforce_timescale(rtl_root: str = "rtl_output") -> None:
    """Ensure every generated SystemVerilog file begins with a shared timescale."""
    import os

    timescale_line = "`timescale 1ns/1ps\n"
    rtl_root_path = SCRIPT_DIR / rtl_root

    for root, _, files in os.walk(rtl_root_path):
        for filename in files:
            if not filename.endswith(".sv"):
                continue

            path = Path(root) / filename
            with path.open("r", encoding="utf-8") as handle:
                content = handle.readlines()

            if content and content[0].strip().startswith("`timescale"):
                continue

            content.insert(0, timescale_line)

            with path.open("w", encoding="utf-8") as handle:
                handle.writelines(content)


def write_system_lint_log(lint_result: dict) -> None:
    """Write detailed system-lint diagnostics, overwriting the previous log."""
    sections = [
        ("Blocking findings", lint_result.get("blocking", [])),
        ("Nonblocking warnings", lint_result.get("nonblocking", [])),
        ("Ignored", lint_result.get("ignored", [])),
    ]
    lines = [
        "Full-system RTL lint",
        "top_module: {}".format(lint_result.get("top_module", "unknown")),
        "rtl_dir: {}".format(lint_result.get("rtl_dir", "unknown")),
        "tool: {}".format(lint_result.get("tool", "unknown")),
        "errors: {}".format(
            lint_result.get("error_count", len(lint_result.get("blocking", [])))),
        "warnings: {}".format(
            lint_result.get("warning_count", len(lint_result.get("nonblocking", [])))),
        "",
    ]

    for title, values in sections:
        lines.append("{}:".format(title))
        if values:
            lines.extend(values)
        else:
            lines.append("(none)")
        lines.append("")

    FLOW_LINT_LOG.write_text("\n".join(lines), encoding="utf-8")


def run_full_system_lint(rtl_root: str = "rtl_output") -> None:
    """Run full generated-RTL integration lint and raise on blocking findings."""
    from dual_llm_rtlGen import lint_full_system

    print("[FLOW] Running full-system RTL lint...")
    lint_result = lint_full_system(rtl_dir=str(SCRIPT_DIR / rtl_root))
    write_system_lint_log(lint_result)
    print("[FLOW] Full-system RTL lint: {} error(s), {} warning(s), {} blocking finding(s).".format(
        lint_result.get("error_count", len(lint_result.get("blocking", []))),
        lint_result.get("warning_count", len(lint_result.get("nonblocking", []))),
        len(lint_result.get("blocking", [])),
    ))
    if lint_result["ok"]:
        print("[FLOW] Full-system RTL lint OK.")
        return

    print("[FLOW] Full-system RTL lint FAILED. See flow_lint.log.")
    raise ValueError("full-system RTL lint failed; see flow_lint.log")


def should_run_expand_spec(interactive: bool, source_count: int, source_path: Path) -> bool:
    """Prompt for expand_spec when requested, otherwise run automatically."""
    if not interactive:
        return True
    if source_count == 1:
        return prompt_yes_no("Run expand_spec? (y/n): ")
    return prompt_yes_no(f"Run expand_spec for {display_path(source_path)}? (y/n): ")


def run_architecture_flow(input_path: Path, args: argparse.Namespace) -> int:
    """Lower the proposed graph directly, bypassing fixed controller integration."""
    from architecture import fingerprint, lower_architecture, validate_architecture, emit_verification_specs
    from generate_microarch import build_request, propose
    from specification_lowering import (compile_package, emit_review, is_specification_manifest,
                                        parse_overrides)
    from specification import read_document

    source = read_document(input_path)
    if args.spec_approval and args.microarch_only:
        raise ValueError('--spec-approval is used for RTL builds, not proposal-only exports')
    package_input = 'specification_version' in source
    generated_input = args.specification
    if generated_input and (package_input or 'architecture_version' in source or args.microarch or args.microarch_response):
        raise ValueError('--specification takes a request, not a package, manifest or legacy architecture-proposal options')
    if not generated_input and (args.specification_output or args.specification_response):
        raise ValueError('--specification-output/--specification-response require --specification')
    if generated_input:
        if args.spec_set or args.spec_profile:
            raise ValueError('Configure a generated design in its request; --spec-set/--spec-profile apply to supplied packages')
        from generate_specification import propose_specification
        from specification_request import build_specification_request
        request = build_specification_request(source, args.requirements_file.read_text() if args.requirements_file else '')
        generated_path = propose_specification(request, output_dir=args.specification_output,
                            model=args.microarch_model, cache=args.cache, response_path=args.specification_response)
        manifest = compile_package(generated_path)
    elif package_input:
        if args.requirements_file or args.microarch_response or args.cache:
            raise ValueError('System packages use deterministic lowering; request/response proposal and proposal-cache options do not apply. Use --cache on the exported approved manifest for RTL caching.')
        manifest = compile_package(input_path, parse_overrides(args.spec_set), args.spec_profile)
    elif "architecture_version" in source:
        if args.spec_set or args.spec_profile:
            raise ValueError('Configuration overrides require a system package; regenerate the manifest instead')
        if args.requirements_file or args.microarch_response:
            raise ValueError("Proposal inputs cannot be combined with an already-authored architecture")
        manifest = source
        validate_architecture(manifest)
    else:
        if args.spec_set or args.spec_profile or args.spec_approval:
            raise ValueError('Specification options require a system package or a specification-derived manifest')
        request = build_request(source, args.requirements_file.read_text() if args.requirements_file else "")
        if request.get('baseline_contract'):
            print(f'[MICROARCH] Preserving baseline: {request["baseline_contract"]["id"]} (host, data, queue and timing contracts)')
        manifest = propose(request, output=args.architecture_output, model=args.microarch_model,
                           cache=args.cache, response_path=args.microarch_response)
    print(f'[MICROARCH] Validated architecture: {manifest["design_name"]}')
    if generated_input:
        proposal_path = args.architecture_output
        if proposal_path == SCRIPT_DIR / 'microarch/proposals/architecture.yaml':
            digest = manifest['provenance']['specification']['package_fingerprint']
            proposal_path = SCRIPT_DIR / 'microarch/proposals/specification' / digest[:16] / 'architecture.yaml'
        protected = {input_path.resolve(), *(p.resolve() for p in generated_path.parent.iterdir() if p.is_file())}
        if proposal_path.resolve() in protected:
            raise ValueError('Architecture export must not overwrite the request or generated specification source')
        emit_review(manifest, proposal_path)
        print(f'[MICROARCH] Generated specification review export: {proposal_path}')
        if args.microarch_only:
            print('[MICROARCH] Review required; RTL generation and behavioral verification have not run.')
            return 0
    if args.microarch_only:
        if is_specification_manifest(manifest):
            proposal_path = input_path if not package_input else args.architecture_output
            if package_input and proposal_path == SCRIPT_DIR / 'microarch/proposals/architecture.yaml':
                digest = manifest['provenance']['specification']['package_fingerprint']
                proposal_path = SCRIPT_DIR / 'microarch/proposals/specification' / digest[:16] / 'architecture.yaml'
            if package_input:
                snapshot = manifest['provenance']['specification']['snapshot']
                source_files = {input_path.resolve()}
                source_files.update((input_path.parent / p).resolve() for p in snapshot['package']['documents'].values())
                if proposal_path.resolve() in source_files:
                    raise ValueError('Architecture export must not overwrite source package documents')
            specs = emit_review(manifest, proposal_path)
            print(f'[MICROARCH] Specification review export: {proposal_path}')
            print(f'[MICROARCH] Module YAMLs for verification: {specs[0].parent} ({len(specs)} definitions)')
            print('[MICROARCH] Revision-bound approval is required before RTL generation. Behavioral verification has not run.')
            return 0
        proposal_path = input_path if 'architecture_version' in source else args.architecture_output
        specs = emit_verification_specs(manifest, proposal_path)
        print(f'[MICROARCH] Module YAMLs for verification: {specs[0].parent} ({len(specs)} definitions)')
        print(f'[MICROARCH] Verification index: {proposal_path.with_suffix(".modules.json")}')
        print("[MICROARCH] Proposal ready for review; RTL generation has not run.")
        return 0
    build_dir = (args.build_dir or SCRIPT_DIR / "microarch/builds" / fingerprint(manifest)[:16]).resolve()
    if build_dir == SCRIPT_DIR:
        raise ValueError("Architecture builds must use a separate directory")
    specs = lower_architecture(manifest, build_dir, specification_approval=args.spec_approval)
    status = {"structural_validation": "passed", "declared_dependency_validation": "passed", "rtl_generation": "in_progress",
              "integration_lint": "not_run", "behavioral_verification": "not_run"}
    if is_specification_manifest(manifest):
        status.update(specification_validation='passed', specification_approval='approved',
                      specification_fingerprint=manifest['provenance']['specification']['package_fingerprint'],
                      verification_handoff='not_run')
    from token_usage import report_path
    if report_path():
        status['token_usage_report'] = report_path()
    status_path = build_dir / "build_status.json"
    status_path.write_text(json.dumps(status, indent=2) + "\n")
    try:
        if is_specification_manifest(manifest):
            # A failed rebuild must not leave a prior handoff looking current.
            (build_dir / 'verification_handoff.json').unlink(missing_ok=True)
        for spec in specs:
            run_command(design_command(spec, no_lint=args.no_lint, cache=args.cache) +
                        ["--output-root", str(build_dir)])
        status["rtl_generation"] = "passed"
        if not args.no_lint:
            from dual_llm_rtlGen import _run_verilator_lint_file_set, LINT_MODE_INTEGRATION
            rtl_dir = build_dir / "rtl_output"
            files = [str(rtl_dir / name) for name in (rtl_dir / "filelist.f").read_text().splitlines()]
            result = _run_verilator_lint_file_set(files, LINT_MODE_INTEGRATION, manifest["design_name"])
            (build_dir / "integration_lint.json").write_text(json.dumps(result, indent=2) + "\n")
            if not result or not result["ok"]:
                raise ValueError(f"Architecture integration lint failed; see {build_dir}/integration_lint.json")
            status["integration_lint"] = "passed"
        else:
            status["integration_lint"] = "skipped"
        if is_specification_manifest(manifest):
            from verification_handoff import emit_handoff
            status['verification_handoff'] = 'in_progress'
            handoff = emit_handoff(manifest, build_dir / 'architecture.yaml', specs,
                                   phase='rtl', output=build_dir / 'verification_handoff.json')
            status['verification_handoff'] = 'exported'
            status['verification_handoff_file'] = handoff.name
            status['verification_consumer'] = 'adapter_required'
    except (subprocess.CalledProcessError, ValueError, OSError):
        if status["rtl_generation"] == "in_progress":
            status["rtl_generation"] = "failed"
        elif status["integration_lint"] == "not_run":
            status["integration_lint"] = "failed"
        if status.get('verification_handoff') == 'in_progress':
            status['verification_handoff'] = 'failed'
        status["result"] = "failed"
        status_path.write_text(json.dumps(status, indent=2) + "\n")
        raise
    status["result"] = "generated"
    status_path.write_text(json.dumps(status, indent=2) + "\n")
    (build_dir / "README.md").write_text(
        f'# {manifest["design_name"]}\n\n{manifest["rationale"]}\n\n'
        'Compile from rtl_output using filelist.f. architecture.yaml owns the graph; '
        'microarch/gen_exp contains resolved module specifications.\n\n'
        'Behavioral verification has not run. verification_plan.json records the '
        'requirements and scenarios; build_status.json records generation/lint results. '
        'The legacy controller testbench is not applicable to arbitrary architectures.\n')
    print(f"[MICROARCH] Generated build: {build_dir}")
    if is_specification_manifest(manifest):
        print('[MICROARCH] Verification handoff exported; the existing UVM/vector system consumer requires an adapter.')
    print("[MICROARCH] Behavioral verification remains pending; see verification_plan.json.")
    return 0


def main() -> int:
    from token_usage import UsageRun
    args = parse_args()
    with UsageRun(SCRIPT_DIR / 'microarch/usage') as usage:
        result = _run_flow(args)
        usage.status = 'completed' if result == 0 else 'failed'
        return result


def _run_flow(args) -> int:
    start_time = time.time()
    validated_user_config: ValidatedUserConfig | None = None
    input_path = Path(args.input_yaml).expanduser()
    if not input_path.is_absolute():
        input_path = (Path.cwd() / input_path).resolve()
    else:
        input_path = input_path.resolve()

    try:
        if args.specification or args.microarch or args.microarch_only or (input_path.is_file() and ({'architecture_version', 'specification_version'} & load_yaml(input_path).keys())):
            return run_architecture_flow(input_path, args)
        if args.requirements_file or args.microarch_response or args.build_dir or args.spec_set or args.spec_profile or args.spec_approval or args.specification_output or args.specification_response:
            raise ValueError("Architecture options require --microarch or an architecture manifest input")
        input_path, validated_user_config = maybe_expand_user_config(input_path)
        source_yamls, design_names = discover_targets(input_path)

        for source_yaml in source_yamls:
            if not should_run_expand_spec(args.interactive, len(source_yamls), source_yaml):
                continue

            print("[FLOW] Running expand_spec...")
            run_command([sys.executable, "expand_spec.py", str(source_yaml)])

        expanded_yamls = discover_expanded_yamls(EXPANDED_DIR, design_names)

        for yaml_path in expanded_yamls:
            yaml_label = display_path(yaml_path)
            if args.interactive and not prompt_yes_no(
                f"Generate RTL for {yaml_label}? (y/n): "
            ):
                continue

            run_command(design_command(yaml_path, no_lint=args.no_lint, cache=args.cache))

        if not args.interactive or prompt_yes_no("Run generate_wrapper? (y/n): "):
            print("[FLOW] Generating wrapper...")
            run_command([sys.executable, "generate_wrapper.py"])
            generate_testbench()

        print("[FLOW] Enforcing timescale directives...")
        enforce_timescale()

        if args.no_lint:
            print("[LINT] Full system lint skipped by --no-lint.")
        else:
            run_full_system_lint()
    except (subprocess.CalledProcessError, ValueError, OSError, yaml.YAMLError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print(f"[FLOW] ERROR: Command failed with exit code {exc.returncode}: {exc.cmd}")
            return exc.returncode or 1
        print(f"[FLOW] ERROR: {exc}")
        return 1

    print("[FLOW] COMPLETE")
    if validated_user_config is not None:
        print_validated_config_summary(validated_user_config)
    if not args.interactive:
        elapsed = time.time() - start_time
        print(f"[FLOW] Completed in {elapsed:.2f} seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
