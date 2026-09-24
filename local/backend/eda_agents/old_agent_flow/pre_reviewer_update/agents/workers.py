import shlex

from schemas.state import FlowState
from services.ssh_executor import SSHExecutor


import shlex

DEFAULT_INNER_HOST = "n01-zeus"
DEFAULT_PROJECT_ROOT = "/home/ugrads/a/antgamez1203/capstone"


def _build_inner_ssh_command(state, inner_cmd: str) -> str:
    wrapped = (
        "source ~/.bashrc >/dev/null 2>&1 || true; "
        "type load-ecen-454 >/dev/null 2>&1 && load-ecen-454; "
        f"{inner_cmd}"
    )

    return (
        "srun --job-name=ecen-454 "
        "--cpus-per-task=1 "
        "--partition=adademic "
        "--qos=olympus-academic "
        f"/bin/bash -lc {shlex.quote(wrapped)}"
    )


def _build_prepare_command(state: FlowState) -> str:
    outdir = state.get("remote_prepare_dir", "build_prep_mc_lang")
    input_name = state.get("remote_prepare_input", "MemoryController.zip")

    return (
        f"rm -rf {shlex.quote(outdir)} && "
        f"python3 prepare_rtl_for_genus.py "
        f"--input {shlex.quote(input_name)} "
        f"--outdir {shlex.quote(outdir)}"
    )


def _build_netlist_command(state: FlowState) -> str:
    builddir = state.get("remote_prepare_dir", "build_prep_mc_lang")
    outdir = state.get("remote_netlist_dir", "build_netlist_mc_lang")
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)

    inner_cmd = (
        f"cd {shlex.quote(project_root)}; "
        "echo 'HOST:'; hostname; "
        "echo 'PATH:'; echo $PATH; "
        "echo 'GENUS_CHECK:'; "
        "GENUS_BIN=$(which genus); "
        "echo \"$GENUS_BIN\"; "
        "test -n \"$GENUS_BIN\"; "
        "test -x \"$GENUS_BIN\"; "
        "echo 'GENUS_OK'; "
        f"rm -rf {shlex.quote(outdir)}; "
        "python3 run_genus_netlist.py "
        f"--builddir {shlex.quote(builddir)} "
        "--genus \"$GENUS_BIN\" "
        f"--outdir {shlex.quote(outdir)}"
    )

    return _build_inner_ssh_command(state, inner_cmd)


def _build_mapped_command(state: FlowState) -> str:
    netlistdir = state.get("remote_netlist_dir", "build_netlist_mc_lang")
    outdir = state.get("remote_mapped_dir", "build_mapped_mc_lang")
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    mapped_top = state.get(
        "mapped_top_module",
        f"{state.get('top_module', 'MemoryController')}_impl",
    )

    inner_cmd = (
        f"cd {shlex.quote(project_root)}; "
        "echo 'HOST:'; hostname; "
        "echo 'PATH:'; echo $PATH; "
        "echo 'GENUS_CHECK:'; "
        "GENUS_BIN=$(which genus); "
        "echo \"$GENUS_BIN\"; "
        "test -n \"$GENUS_BIN\"; "
        "test -x \"$GENUS_BIN\"; "
        "echo 'GENUS_OK'; "
        f"rm -rf {shlex.quote(outdir)}; "
        "python3 run_genus_mapped.py "
        f"--netlistdir {shlex.quote(netlistdir)} "
        f"--top {shlex.quote(mapped_top)} "
        f"--outdir {shlex.quote(outdir)}"
    )

    return _build_inner_ssh_command(state, inner_cmd)


def _build_gdsii_command(state: FlowState) -> str:
    mappeddir = state.get("remote_mapped_dir", "build_mapped_mc_lang")
    outdir = state.get("remote_gdsii_dir", "build_GDSII_mc_lang")
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    gdsii_script = state.get("gdsii_script", "run_innovus_gdsii.py")

    inner_cmd = (
        f"cd {shlex.quote(project_root)}; "
        "echo 'HOST:'; hostname; "
        "echo 'PATH:'; echo $PATH; "
        "echo 'INNOVUS_CHECK:'; "
        "INNOVUS_BIN=$(which innovus); "
        "echo \"$INNOVUS_BIN\"; "
        "test -n \"$INNOVUS_BIN\"; "
        "test -x \"$INNOVUS_BIN\"; "
        "echo 'INNOVUS_OK'; "
        f"rm -rf {shlex.quote(outdir)}; "
        f"python3 {shlex.quote(gdsii_script)} "
        f"--mappeddir {shlex.quote(mappeddir)} "
        f"--outdir {shlex.quote(outdir)}"
    )

    return _build_inner_ssh_command(state, inner_cmd)
