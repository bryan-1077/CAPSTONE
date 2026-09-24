#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.ssh_executor import SSHExecutor
from timing_closure.timing_analyzer import (
    analyze_reports,
    emit_advisory_patch_proposals,
    parse_timing_report,
    render_markdown,
)


DEFAULT_SSH_HOST = "olympus.ece.tamu.edu"
DEFAULT_SSH_PORT = 22
DEFAULT_REMOTE_PROJECT_ROOT = "/home/ugrads/a/antgamez1203/capstone"


def _remote_project_path(remote_root: str, report_path: str) -> str:
    path = PurePosixPath(report_path)
    if path.is_absolute():
        return str(path)
    return str(PurePosixPath(remote_root) / path)


def _safe_local_name(remote_path: str) -> str:
    return str(remote_path).strip("/").replace("/", "__") or "report.rpt"


def fetch_remote_reports(
    *,
    ssh: SSHExecutor,
    remote_project_root: str,
    report_paths: list[str],
    local_staging_dir: Path,
) -> tuple[list[Path], list[dict]]:
    local_paths: list[Path] = []
    fetch_results: list[dict] = []
    for report_path in report_paths:
        remote_path = _remote_project_path(remote_project_root, report_path)
        local_path = local_staging_dir / _safe_local_name(report_path)
        result = ssh.fetch_file(remote_path, local_path)
        fetch_results.append(result)
        if result["ok"]:
            local_paths.append(local_path)
    return local_paths, fetch_results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch remote Cadence timing reports over SSH and run timing closure analysis locally."
    )
    parser.add_argument("reports", nargs="+", help="Remote report paths, absolute or relative to --remote-root.")
    parser.add_argument("--ssh-host", default=os.getenv("EDA_SSH_HOST", DEFAULT_SSH_HOST))
    parser.add_argument("--ssh-user", default=os.getenv("EDA_SSH_USER") or os.getenv("USER"))
    parser.add_argument("--ssh-port", type=int, default=int(os.getenv("EDA_SSH_PORT", DEFAULT_SSH_PORT)))
    parser.add_argument("--ssh-key-file", default=os.getenv("EDA_SSH_KEY_FILE", "~/.ssh/id_ed25519"))
    parser.add_argument("--remote-root", default=os.getenv("EDA_REMOTE_PROJECT_ROOT", DEFAULT_REMOTE_PROJECT_ROOT))
    parser.add_argument("--local-staging-dir", type=Path, default=REPO_ROOT / "logs" / "timing_closure" / "remote")
    parser.add_argument("--target-period", type=float, default=None)
    parser.add_argument("--max-paths", type=int, default=10)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--clean-downloaded",
        action="store_true",
        help="Remove local staged report copies after analysis. Remote reports are never deleted.",
    )
    parser.add_argument("--advisory-patches-dir", type=Path, default=None)
    args = parser.parse_args()

    if not args.ssh_user:
        raise SystemExit("Missing SSH user. Pass --ssh-user or set EDA_SSH_USER.")

    staging = args.local_staging_dir
    staging.mkdir(parents=True, exist_ok=True)

    with SSHExecutor(
        host=args.ssh_host,
        username=args.ssh_user,
        port=args.ssh_port,
        key_filename=args.ssh_key_file,
    ) as ssh:
        local_paths, fetch_results = fetch_remote_reports(
            ssh=ssh,
            remote_project_root=args.remote_root,
            report_paths=args.reports,
            local_staging_dir=staging,
        )

    failures = [result for result in fetch_results if not result["ok"]]
    if failures:
        for failure in failures:
            print(
                "Failed to fetch {0}: {1}".format(
                    failure["remote_path"],
                    failure["error"],
                )
            )
        if not local_paths:
            return 1

    reports = [parse_timing_report(path, max_paths=max(1, args.max_paths)) for path in local_paths]
    analysis = analyze_reports(reports, target_period=args.target_period)
    analysis["remote_fetch"] = fetch_results
    if args.advisory_patches_dir:
        analysis["advisory_patch_proposals"] = emit_advisory_patch_proposals(
            analysis,
            args.advisory_patches_dir,
        )

    output = (
        json.dumps(analysis, indent=2)
        if args.format == "json"
        else render_markdown(analysis)
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
    else:
        print(output, end="" if output.endswith("\n") else "\n")

    if args.clean_downloaded:
        for path in local_paths:
            try:
                path.unlink()
            except OSError:
                pass

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
