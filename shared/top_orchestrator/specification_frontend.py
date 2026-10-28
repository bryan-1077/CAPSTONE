"""Run-owned Experimental frontend; no legacy checks, publication or repair loop."""
import json
import math
import sys
from pathlib import Path

import yaml

from .contracts import StageResult
from .frequency import positive_mhz
from .nodes import FRONTEND, stream_command
from .timing_model import apply_timing_model, SUMMARY


def read_intake(path, flow=None, target=None):
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("schema_version") != 1
            or record.get("status") != "confirmed_request"
            or record.get("frontend_flow") not in ("expand-spec", "specification")
            or not isinstance(record.get("original_text"), str)
            or not record["original_text"].strip()):
        raise ValueError("Expected a confirmed shared intake record")
    frequency = positive_mhz(str(record.get("target_mhz")))
    if flow is not None and flow != record["frontend_flow"]:
        raise ValueError("Frontend override conflicts with confirmed intake")
    if target is not None and not math.isclose(target, frequency, rel_tol=1e-9):
        raise ValueError("Frequency override conflicts with confirmed intake; prepare a new request")
    return record


def specification_command(request, root, cache=False, *, mode="request", approval=None, resume=False):
    if cache and mode != "approved":
        raise ValueError("Fresh Experimental review/repair cannot use --cache; use approved architecture input")
    if cache and resume:
        raise ValueError("Choose --cache or --resume-build")
    command = [sys.executable, "-u", str(FRONTEND / "run_flow.py"), str(request),
               "--architecture-output", str(root / "architecture.yaml"),
               "--build-dir", str(root / "build")]
    if mode == "request":
        command += ["--specification", "--specification-output", str(root / "source")]
    if mode == "approved":
        command += ["--spec-approval", str(approval)]
        if cache:
            command.append("--cache")
        if resume:
            command.append("--resume")
    else:
        command += ["--automated-spec-review", "--spec-review-output", str(root / "review"),
                    "--auto-spec-repair", "--spec-repair-output", str(root / "repair")]
    return command


def run_specification(config, run_dir):
    from .specification_inputs import inspect_input, snapshot_input, copy_resume_build
    root = run_dir / "frontend"
    root.mkdir()
    frequency = positive_mhz(str(config.target_mhz))
    mode, approval = "request", None
    if config.intake:
        record = read_intake(config.intake, config.frontend_flow, config.target_mhz)
        (root / "input").mkdir()
        (root / "input/intake.json").write_text(json.dumps(record, indent=2) + "\n")
        (root / "input/description.txt").write_text(record["original_text"], encoding="utf-8")
        request = root / "input/request.yaml"
        data = {
            "design_name": "ddr4_controller_top", "design_type": "memory_controller",
            "requirements": [record["original_text"]],
            "controller_clock": {"frequency_mhz": frequency},
        }
        if "timing_model" in record:
            data["timing_model"] = record["timing_model"]
        request.write_text(yaml.safe_dump(apply_timing_model(data), sort_keys=False), encoding="utf-8")
    else:
        info = inspect_input(config.input_yaml, config.spec_approval, frequency)
        mode = info["mode"]
        request, approval = snapshot_input(info, root / "input")
        # Revalidate the owned copy before any paid calls.
        inspect_input(request, approval, frequency)
        if mode == "request":
            data = yaml.safe_load(request.read_text())
            data["controller_clock"] = {"frequency_mhz": frequency}
            data = apply_timing_model(data)
            request = root / "request.yaml"
            request.write_text(yaml.safe_dump(data, sort_keys=False))
        if config.resume_build:
            if mode != "approved":
                raise ValueError("--resume-build requires an approved architecture")
            copy_resume_build(config.resume_build, root / "build", request)
    selected_model = yaml.safe_load(request.read_text()).get("timing_model")
    if mode == "request" and isinstance(selected_model, dict) and selected_model.get("id") == "project-functional-controller-v1":
        print(SUMMARY, flush=True)
    log = root / "frontend.log"
    artifacts = {"frontend_root": str(root), "request": str(request), "log": str(log),
                 "behavioral_verification": "not_run", "backend": "not_run",
                 "publication": "not_run"}
    code = stream_command(specification_command(request, root, config.cache, mode=mode, approval=approval,
                                               resume=bool(config.resume_build)), log)
    if code:
        return StageResult("generate", "failed", "Experimental frontend stopped; bounded frontend recovery exhausted or generation failed. Inspect frontend.log.", artifacts, code)
    build = root / "build"
    status = json.loads((build / "build_status.json").read_text())
    if any(status.get(key) != value for key, value in {
            "rtl_generation": "passed", "integration_lint": "passed",
            "specification_approval": "approved", "verification_handoff": "exported"}.items()):
        raise ValueError("Experimental build lacks required generation, approval, lint or handoff evidence")
    handoff = build / "verification_handoff.json"
    validation_log = root / "handoff_validation.log"
    code = stream_command([sys.executable, "-u", str(FRONTEND / "verification_handoff.py"),
                           str(handoff)], validation_log)
    if code:
        return StageResult("generate", "failed", "Experimental handoff authentication failed.", artifacts, code)
    document = json.loads(handoff.read_text())
    if document.get("phase") != "rtl":
        raise ValueError("Expected RTL-phase verification handoff")
    artifacts.update(handoff=str(handoff), build=str(build), handoff_validation_log=str(validation_log))
    from .frontend_result import write_frontend_result
    built_architecture = build / "architecture.yaml"
    timing_model = None
    if built_architecture.is_file():
        architecture = yaml.safe_load(built_architecture.read_text())
        timing_model = architecture.get("provenance", {}).get("specification", {}).get("snapshot", {}).get(
            "package", {}).get("generation", {}).get("request", {}).get("constraints", {}).get("timing_model")
    result_path = write_frontend_result(root / "frontend_result.json", flow="specification",
        build=build, top=document["design"]["top_module"], target_mhz=frequency,
        revision=document["identity"]["rtl_revision"],
        rtl_files=[build / name for name in document["rtl_files"]],
        contracts=[build / row["path"] for row in document["inputs"]
                   if row["role"] in ("module_spec", "architecture")],
        requirements=document["verification_intent"],
        timing_model=timing_model,
        checks={"integration_lint": "passed", "handoff_authentication": "passed",
                "behavioral_verification": "not_run"},
        evidence={"handoff": str(handoff), "build_status": str(build / "build_status.json"),
                  "compile_order": document["rtl_files"]})
    artifacts["frontend_result"] = str(result_path)
    return StageResult("generate", "passed", "Experimental frontend and authenticated handoff completed; downstream integration remains pending.", artifacts, 0)
