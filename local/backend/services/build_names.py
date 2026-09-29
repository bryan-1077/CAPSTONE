"""Readable server build names and exclusive reservation of full-flow outputs."""
import json
import math
import shlex


def frequency_label(period_ns):
    period = float(period_ns)
    if not math.isfinite(period) or period <= 0:
        raise ValueError("Target period must be finite and positive")
    return f"{1000 / period:.0f}MHz"


def full_flow_names(period_ns):
    suffix = f"{frequency_label(period_ns)}_01"
    scripts = f"scripts_{suffix}"
    return {
        "remote_prepare_dir": f"build_prep_{suffix}",
        "remote_netlist_dir": f"build_netlist_{suffix}",
        "remote_mapped_dir": f"build_mapped_{suffix}",
        "remote_gdsii_dir": f"build_GDSII_{suffix}",
        "remote_prepare_script": f"{scripts}/prepare_rtl_for_genus_{suffix}.py",
        "remote_netlist_script": f"{scripts}/run_genus_netlist_{suffix}.py",
        "remote_mapped_script": f"{scripts}/run_genus_mapped_{suffix}.py",
        "gdsii_script": f"{scripts}/run_innovus_GDSII_{suffix}.py",
        "reserve_named_builds": True,
    }


def reserve_full_flow(ssh, state):
    # The scripts directory doubles as an exclusive reservation for this name set.
    # Keep the canonical server scripts as sources; each run executes its own copy.
    payload = {
        "names": {key: state[key] for key in (
            "remote_prepare_dir", "remote_netlist_dir", "remote_mapped_dir", "remote_gdsii_dir",
            "remote_prepare_script", "remote_netlist_script", "remote_mapped_script", "gdsii_script",
        )},
        "directories": [state[key] for key in ("remote_prepare_dir", "remote_netlist_dir", "remote_mapped_dir")],
        "gdsii": state["remote_gdsii_dir"],
        "scripts": {
            state["remote_prepare_script"]: "prepare_rtl_for_genus_universal.py",
            state["remote_netlist_script"]: "run_genus_netlist_universal.py",
            state["remote_mapped_script"]: "run_genus_mapped_universal.py",
            state["gdsii_script"]: "run_innovus_GDSII_universal.py",
        },
    }
    code = '''
import json
from pathlib import Path
config = json.loads(CONFIG)
original_scripts = Path(next(iter(config["scripts"]))).parent
original_suffix = original_scripts.name.removeprefix("scripts_")
label, first_index = original_suffix.rsplit("_", 1)
sources = {destination: Path(source).read_bytes() for destination, source in config["scripts"].items()}
for index in range(int(first_index), int(first_index) + 10000):
    suffix = f"{label}_{index:02d}"
    def renamed(name):
        return name.replace(original_suffix, suffix)
    scripts_dir = Path(renamed(str(original_scripts)))
    builds = [Path(renamed(name)) for name in [*config["directories"], config["gdsii"]]]
    if any(path.exists() or path.is_symlink() for path in [scripts_dir, *builds]):
        continue
    try:
        # Atomic reservation prevents concurrent runs from sharing names.
        scripts_dir.mkdir(exist_ok=False)
    except FileExistsError:
        continue
    try:
        for name in config["directories"]:
            Path(renamed(name)).mkdir(exist_ok=False)
    except FileExistsError:
        continue
    break
else:
    raise RuntimeError("No unused build number found after 10000 attempts")
for destination, data in sources.items():
    with open(renamed(destination), "xb") as output:
        output.write(data)
print("RESERVED_BUILD_NAMES=" + json.dumps({key: renamed(value) for key, value in config["names"].items()}))
'''.replace("CONFIG", repr(json.dumps(payload)))
    result = ssh.run(
        f"{shlex.quote(state.get('remote_python_cmd') or 'python3')} -c {shlex.quote(code)}",
        cwd=state["remote_project_root"],
    )
    if not result["ok"]:
        raise RuntimeError(f"Cannot reserve named builds/scripts; existing files are preserved: {result.get('stderr', '')}")
    for line in result.get("stdout", "").splitlines():
        if line.startswith("RESERVED_BUILD_NAMES="):
            state.update(json.loads(line.split("=", 1)[1]))
            print(f"Reserved build: {state['remote_prepare_dir']}", flush=True)
            return
    raise RuntimeError("Build reservation did not return the selected names; stopping to protect existing builds")
