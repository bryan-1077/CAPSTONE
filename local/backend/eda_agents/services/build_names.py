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
scripts_dir = Path(next(iter(config["scripts"]))).parent
scripts_dir.mkdir(exist_ok=False)
for name in [*config["directories"], config["gdsii"]]:
    if Path(name).exists():
        raise RuntimeError("Existing build will not be overwritten: " + name)
for name in config["directories"]:
    Path(name).mkdir(exist_ok=False)
for destination, source in config["scripts"].items():
    data = Path(source).read_bytes()
    with open(destination, "xb") as output:
        output.write(data)
'''.replace("CONFIG", repr(json.dumps(payload)))
    result = ssh.run(
        f"{shlex.quote(state.get('remote_python_cmd') or 'python3')} -c {shlex.quote(code)}",
        cwd=state["remote_project_root"],
    )
    if not result["ok"]:
        raise RuntimeError(f"Cannot reserve named builds/scripts; existing files are preserved: {result.get('stderr', '')}")
