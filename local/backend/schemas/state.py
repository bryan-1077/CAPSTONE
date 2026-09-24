from typing import Any, Dict, List, Literal, Optional, TypedDict


StageName = Literal[
    "idle",
    "rtl_prep",
    "netlist",
    "mapped_netlist",
    "gdsii",
    "done",
    "failed",
]
StageStatus = str


class FlowState(TypedDict, total=False):
    project_name: str
    top_module: str
    stop_after_stage: Optional[StageName]

    ssh_host: str
    ssh_user: str
    ssh_port: int
    ssh_key_file: Optional[str]
    remote_project_root: str
    reserve_named_builds: bool
    remote_prepare_input: str
    remote_python_cmd: str
    remote_prepare_script: str
    remote_prepare_dir: str
    remote_netlist_script: str
    remote_mapped_script: str
    remote_genus_bin: Optional[str]
    remote_netlist_dir: Optional[str]
    netlist_run_timeout_sec: int
    netlist_heartbeat_sec: int
    remote_mapped_dir: Optional[str]
    mapped_run_timeout_sec: int
    mapped_heartbeat_sec: int
    remote_gdsii_dir: Optional[str]
    gdsii_run_timeout_sec: int
    gdsii_heartbeat_sec: int
    timing_target_clock_period_ns: Optional[float]
    timing_overconstraint_clock_period_ns: Optional[float]
    timing_use_overconstraint: bool
    innovus_utilization: Optional[float]
    innovus_ccopt_target_skew: Optional[float]
    innovus_ccopt_target_max_transition: Optional[float]
    innovus_final_postroute_setup_opt: bool
    timing_closure_enable_remote_analysis: bool
    timing_closure_report_paths: List[str]
    timing_closure_local_staging_dir: Optional[str]
    timing_closure_max_paths: int
    timing_closure_analysis: Dict[str, Any]
    timing_closure_enabled: bool
    timing_closure_max_attempts: int
    timing_closure_ai_enabled: bool
    timing_closure_ai_max_attempts: int
    timing_closure_eco_plan: Optional[Dict[str, Any]]
    timing_closure_best_attempt: Optional[Dict[str, Any]]
    timing_closure_limits: Dict[str, Any]
    timing_closure_recover_best: bool
    timing_closure_status: str
    timing_closure_run_name: str
    timing_closure_attempts: List[Dict[str, Any]]
    timing_closure_source_mapped_dir: str
    timing_closure_mapped_snapshot_dir: str
    timing_closure_base_outdir: str
    inner_compute_host: str
    mapped_top_module: str
    gdsii_script: str

    current_stage: StageName
    stage_status: Dict[str, StageStatus]
    max_stage_retries: int
    retry_counts: Dict[str, int]

    inputs: Dict[str, str]
    outputs: Dict[str, str]
    logs: Dict[str, str]
    validation: Dict[str, Any]

    last_error: Optional[str]
    history: List[str]

    prep_iteration: int
    prep_max_iterations: int
    prep_review_passed: bool
    prep_review_errors: List[str]
    prep_review_warnings: List[str]
    prep_review_suggested_fixes: List[str]
    prep_review_summary: Optional[str]
    prep_dependency_errors: List[str]
    prep_candidate_path: Optional[str]
    prep_fix_applied_actions: List[Dict[str, Any]]
    prep_fix_summary: Optional[str]
    prep_fix_history: List[str]
    prep_iteration_context: Dict[str, Any]
    prep_new_errors: List[str]
    prep_persistent_errors: List[str]
    prep_resolved_errors: List[str]
    selected_top_module: Optional[str]
    selected_top_file: Optional[str]
    generic_interface_ports: List[str]
    detected_patterns: List[Dict[str, Any]]
    applicable_recipe_ids: List[str]
    chosen_recipe_id: Optional[str]
    inferred_modport_map: Dict[str, str]
    generated_files: List[str]
    generated_top_module: Optional[str]
    generated_top_file: Optional[str]
    adaptation_applied: bool
    adaptation_reason: Optional[str]
    adaptation_deterministic: Optional[bool]
    original_files_preserved: bool
    ambiguity_reason: Optional[str]
    top_selection_reason: Optional[str]
    top_selection_candidates: List[Dict[str, Any]]
    synthesis_top_checks: Dict[str, Any]
    netlist_iteration: int
    netlist_max_iterations: int
    netlist_review_passed: bool
    netlist_review_errors: List[str]
    netlist_review_warnings: List[str]
    netlist_review_suggested_fixes: List[str]
    netlist_review_summary: Optional[str]
    netlist_candidate_path: Optional[str]
    netlist_edit_applied_actions: List[Dict[str, Any]]
    netlist_edit_summary: Optional[str]
    netlist_output_files: List[str]
    netlist_resolved_top: Optional[str]
    mapped_iteration: int
    mapped_max_iterations: int
    mapped_review_passed: bool
    mapped_review_errors: List[str]
    mapped_review_warnings: List[str]
    mapped_review_suggested_fixes: List[str]
    mapped_review_summary: Optional[str]
    mapped_candidate_path: Optional[str]
    mapped_edit_applied_actions: List[Dict[str, Any]]
    mapped_edit_summary: Optional[str]
    mapped_output_files: List[str]
    mapped_resolved_top: Optional[str]
    mapped_review_payload: Dict[str, Any]
    gdsii_iteration: int
    gdsii_max_iterations: int
    gdsii_review_passed: bool
    gdsii_review_errors: List[str]
    gdsii_review_warnings: List[str]
    gdsii_review_suggested_fixes: List[str]
    gdsii_review_summary: Optional[str]
    gdsii_candidate_path: Optional[str]
    gdsii_edit_applied_actions: List[Dict[str, Any]]
    gdsii_edit_summary: Optional[str]
    gdsii_output_files: List[str]
    gdsii_review_payload: Dict[str, Any]
    failure_stage_owner: Optional[str]
    failure_class: Optional[str]
    route_back_to_stage: Optional[str]
    rtl_failure_handoff_owner: Optional[str]
    rtl_failure_category: Optional[str]
    rtl_failure_triage_payload: Dict[str, Any]
    netlist_review_payload: Dict[str, Any]
    universal_rules_path: Optional[str]
    netlist_rules_path: Optional[str]
    mapped_rules_path: Optional[str]
    gdsii_rules_path: Optional[str]
