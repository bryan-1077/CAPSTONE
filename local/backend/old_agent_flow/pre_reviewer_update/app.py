from pprint import pprint

from agents.orchestrator import run_flow
from schemas.state import FlowState


def main():
    initial_state: FlowState = {
        "current_stage": "idle",
        "history": [],
        "last_error": None,
        "logs": {},
        "outputs": {},
        "inputs": {},
        "validation": {},
        "stage_status": {
            "rtl_prep": "pending",
            "netlist": "pending",
            "mapped_netlist": "pending",
            "gdsii": "pending",
        },
        "max_stage_retries": 3,
        "retry_counts":{},
        "ssh_host": "olympus.ece.tamu.edu",
        "ssh_user": "antgamez1203",
        "ssh_port": 22,
        "ssh_key_file": "~/.ssh/id_ed25519",
        "remote_project_root": "/home/ugrads/a/antgamez1203/capstone",
    }

    ssh_config = {
        "host": initial_state["ssh_host"],
        "username": initial_state["ssh_user"],
        "port": initial_state["ssh_port"],
        "key_filename": initial_state["ssh_key_file"],
    }

    result = run_flow(
        initial_state=initial_state,
        ssh_config=ssh_config,
        thread_id="eda-flow-run",
    )

    pprint(result)


if __name__ == "__main__":
    main()
