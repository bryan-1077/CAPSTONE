"""Uploaded wrapper for the deployed GDSII agent; standard library only."""

import hashlib
import json
import os
import runpy
import sys
import zipfile
from pathlib import Path


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def within(path, folder):
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def one_file(folder, suffixes, explicit=None, excluded=()):
    folder = Path(folder).resolve()
    if explicit:
        path = Path(explicit)
        path = (path if path.is_absolute() else folder / path).resolve()
        if not within(path, folder):
            raise ValueError("Selected artifact must belong to its backend output directory")
        candidates = [path] if path.is_file() else []
    else:
        candidates = sorted(p for p in folder.rglob("*")
                            if p.is_file() and p.suffix.lower() in suffixes and p.resolve() not in excluded)
    if len(candidates) != 1:
        raise ValueError(f"Expected one {sorted(suffixes)} artifact in {folder}; found {len(candidates)}. "
                         "Configure an explicit artifact path if selection is ambiguous.")
    selected = candidates[0].resolve()
    if not within(selected, folder):
        raise ValueError("Selected artifact must belong to its backend output directory")
    return selected


def execute(invocation):
    invocation = Path(invocation).resolve()
    request = json.loads((invocation / "request.json").read_text())
    result = {"schema_version": 1, "invocation_id": request["invocation_id"],
              "backend_digest": request["backend_digest"], "input_unchanged": False,
              "report": None}
    reports = invocation / "verification_reports"
    reports.mkdir(exist_ok=False)
    code = 1
    try:
        gds_dir = Path(request["gds_dir"]).resolve()
        libraries = [Path(p).resolve() for p in request["libraries"]]
        gds = one_file(gds_dir, {".gds", ".gdsii"}, request.get("gds_file"), libraries)
        # Prefer the final implementation's netlist. Fall back only when it has none.
        netlist_dir = gds_dir
        if not request.get("netlist") and not any(
                p.is_file() and p.suffix.lower() in {".v", ".sv", ".vg"} for p in gds_dir.rglob("*")):
            netlist_dir = Path(request["mapped_dir"]).resolve()
        elif request.get("netlist"):
            selected = Path(request["netlist"])
            if selected.is_absolute() and not within(selected.resolve(), gds_dir):
                netlist_dir = Path(request["mapped_dir"]).resolve()
        netlist = one_file(netlist_dir, {".v", ".sv", ".vg"}, request.get("netlist"))
        report_dirs = [Path(p).resolve() for p in request["report_dirs"]]
        if not report_dirs and (gds_dir / "reports").is_dir():
            report_dirs = [gds_dir / "reports"]
        pipeline = Path(request["pipeline"])
        result["pipeline_digest"] = digest(pipeline)
        sys.path.insert(0, str(pipeline.parent))
        # Use the deployed validation logic with our own isolated report writer.
        # AgentReport defaults to directories beside the design; avoid that side effect.
        agent_type = runpy.run_path(str(pipeline))["GDSIIValidationAgent"]
        agent = agent_type(netlist_file=str(netlist), gds_file=str(gds),
                           top_module=request["top_module"], gds_top=request.get("gds_top"),
                           gds_libraries=[str(p) for p in libraries],
                           report_dirs=[str(p) for p in report_dirs])
        report_paths = [Path(p) for p in agent.collect_backend_reports()]
        inputs = list(dict.fromkeys([gds, netlist, *libraries, *report_paths]))
        before = {str(p): digest(p) for p in inputs}
        result.update(gds_file=str(gds), netlist_file=str(netlist), input_digests=before,
                      netlist_source="implementation" if netlist_dir == gds_dir else "mapped",
                      top_module=request["top_module"])
        print(f"[GDSII] Inspecting {gds} with netlist {netlist}", flush=True)
        report = agent.validate()
        print(f"[GDSII] Result: {report.get('status')}", flush=True)
        after = {str(p): digest(p) for p in inputs}
        if before != after:
            raise ValueError("Backend artifacts changed during GDSII validation")
        result["input_unchanged"] = True
        result["report"] = report
        (reports / "gdsii_validation_report.json").write_text(json.dumps(report, indent=2) + "\n")
        # Preserve the exact bytes hashed above, including unsupported reports.
        for index, path in enumerate(report_paths):
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != before[str(path)]:
                raise ValueError("Backend report changed during collection")
            target = reports / "backend_reports" / f"{index:04d}_{path.name}"
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
        code = 0 if report.get("status") in ("PASS", "PASS_WITH_GAPS") else 1
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(result["error"], file=sys.stderr, flush=True)
    finally:
        with zipfile.ZipFile(invocation / "reports.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(reports.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, path.relative_to(invocation).as_posix())
        result["reports_digest"] = digest(invocation / "reports.zip")
        (invocation / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return code


if __name__ == "__main__":
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    raise SystemExit(execute(sys.argv[1]))
