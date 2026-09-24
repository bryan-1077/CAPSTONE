from typing import TypedDict, Literal, Dict, Optional, List


StageName = Literal["rtl_prep", "netlist", "mapped_netlist", "gdsii", "done", "failed"]
StageStatus = Literal["queued", "running", "success", "failed", "skipped"]


class FlowState(TypedDict):
    project_name: str
    top_module: str

    ssh_host: str
    ssh_user: str
    ssh_port: int
    ssh_key_file: Optional[str]
    remote_project_root: str
    remote_prepare_dir: str
    remote_mapped_dir: str

    current_stage: StageName
    stage_status: Dict[str, StageStatus]
    retries: Dict[str, int]

    inputs: Dict[str, str]
    outputs: Dict[str, str]
    logs: Dict[str, str]

    last_error: Optional[str]
    history: List[str]
