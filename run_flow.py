#!/usr/bin/env python3
"""Orchestrate config expansion, RTL generation, and wrapper generation."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from generate_testbench import generate_testbench


SCRIPT_DIR = Path(__file__).resolve().parent
EXPANDED_DIR = SCRIPT_DIR / "expanded"
GENERATED_INPUT_DIR = SCRIPT_DIR / "inputs" / "generated"
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
        "expanded_dir": "ddr4_bank",
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
        "expanded_dir": "ddr4_tFAW",
        "ir_modules": ["ddr4_tFAW_tFAW_tracker"],
        "rtl_modules": ["ddr4_tFAW_tFAW_tracker"],
    },
    "ddr4_request_queue": {
        "expanded_dir": "ddr4_request_queue",
        "ir_modules": ["ddr4_request_queue"],
        "rtl_modules": ["ddr4_request_queue"],
    },
    "ddr4_tRRD": {
        "expanded_dir": "ddr4_tRRD",
        "ir_modules": ["ddr4_tRRD_simple_tRRD"],
        "rtl_modules": ["ddr4_tRRD_simple_tRRD"],
    },
    "ddr4_scheduler": {
        "expanded_dir": "ddr4_scheduler",
        "ir_modules": ["ddr4_scheduler_scheduler"],
        "rtl_modules": ["ddr4_scheduler_scheduler"],
    },
    "ddr4_refresh": {
        "expanded_dir": "ddr4_refresh",
        "ir_modules": ["ddr4_refresh_refresh_controller"],
        "rtl_modules": ["ddr4_refresh_refresh_controller"],
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run config expansion, expand_spec, design generation, and wrapper generation."
    )
    parser.add_argument("input_yaml", help="Path to a legacy input YAML or user config YAML.")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt before each flow step.",
    )
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
    subprocess.run(command, check=True, cwd=SCRIPT_DIR)


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

        expanded_dir = EXPANDED_DIR / artifacts["expanded_dir"]
        remove_path(expanded_dir)

        for module_name in artifacts["ir_modules"]:
            remove_path(SCRIPT_DIR / "ir" / f"{module_name}_ir.json")

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
    """Collect all non-master YAML files under selected expanded/ design directories."""
    yaml_paths: list[Path] = []
    for design_name in sorted(set(design_names)):
        design_dir = expanded_dir / design_name
        if not design_dir.is_dir():
            continue

        for root, _, files in os.walk(design_dir):
            for filename in files:
                if not filename.endswith(".yaml"):
                    continue
                if filename == "master.yaml":
                    continue
                yaml_paths.append((Path(root) / filename).resolve())

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


def should_run_expand_spec(interactive: bool, source_count: int, source_path: Path) -> bool:
    """Prompt for expand_spec when requested, otherwise run automatically."""
    if not interactive:
        return True
    if source_count == 1:
        return prompt_yes_no("Run expand_spec? (y/n): ")
    return prompt_yes_no(f"Run expand_spec for {display_path(source_path)}? (y/n): ")


def main() -> int:
    args = parse_args()
    start_time = time.time()
    validated_user_config: ValidatedUserConfig | None = None
    input_path = Path(args.input_yaml).expanduser()
    if not input_path.is_absolute():
        input_path = (Path.cwd() / input_path).resolve()
    else:
        input_path = input_path.resolve()

    try:
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

            run_command([sys.executable, "design.py", str(yaml_path)])

        if not args.interactive or prompt_yes_no("Run generate_wrapper? (y/n): "):
            print("[FLOW] Generating wrapper...")
            run_command([sys.executable, "generate_wrapper.py"])
            generate_testbench()

        print("[FLOW] Enforcing timescale directives...")
        enforce_timescale()
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
