from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path
from uuid import uuid4


def collect_legacy_logs(backend_root: Path) -> list[Path]:
    """Archive old local run output without replacing current logs or state.

    Preserve relative paths and sidecar names together in a unique batch.
    Remote build outputs and explicitly configured output paths are untouched.
    """
    root = Path(backend_root)
    sources = []
    for directory in (root, root / "timing_closure"):
        if directory.is_dir():
            sources.extend(
                path for path in sorted(directory.iterdir())
                if path.is_file() and not path.is_symlink() and (
                    ".log" in path.suffixes
                    or path.name in {"timing_closure_status.json", "timing_closure_report.md"}
                )
            )
    staging = root / ".timing_closure_remote"
    if staging.is_dir() and not staging.is_symlink():
        sources.append(staging)
    if not sources:
        return []
    archive = root / "logs" / "legacy" / uuid4().hex
    destinations = []
    for source in sources:
        destination = archive / source.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        destinations.append(destination)
    return destinations


class TeeStream:
    def __init__(self, primary, mirror):
        self._primary = primary
        self._mirror = mirror
        self.encoding = getattr(primary, "encoding", "utf-8")

    def write(self, data):
        self._primary.write(data)
        self._mirror.write(data)
        return len(data)

    def flush(self):
        self._primary.flush()
        self._mirror.flush()

    def isatty(self):
        return bool(getattr(self._primary, "isatty", lambda: False)())


class SessionLogCapture:
    def __init__(self, log_path: Path):
        self.log_path = Path(log_path)
        self._log_handle = None
        self._stdout = None
        self._stderr = None

    def __enter__(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("w", encoding="utf-8", buffering=1)
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = TeeStream(self._stdout, self._log_handle)
        sys.stderr = TeeStream(self._stderr, self._log_handle)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            sys.stdout = self._stdout
            sys.stderr = self._stderr
            if self._log_handle is not None:
                self._log_handle.flush()
                self._log_handle.close()
                self._log_handle = None


def run_with_session_logging(
    run_callable,
    *,
    log_path: Path,
    parser_script_path: Path,
    parser_outdir: Path,
    result_printer=None,
):
    backend_root = Path(parser_script_path).resolve().parent
    collect_legacy_logs(backend_root)
    try:
        return _run_with_session_logging(
            run_callable,
            log_path=Path(parser_outdir) / Path(log_path).name,
            parser_script_path=parser_script_path,
            parser_outdir=parser_outdir,
            result_printer=result_printer,
        )
    finally:
        # Also collect files emitted by older helpers on failed/interrupted runs.
        collect_legacy_logs(backend_root)


def _run_with_session_logging(
    run_callable,
    *,
    log_path: Path,
    parser_script_path: Path,
    parser_outdir: Path,
    result_printer=None,
):
    flow_error = None
    result = None
    state_path = Path(f"{log_path}.state.json")
    wrote_state = False

    with SessionLogCapture(log_path):
        try:
            result = run_callable()
        except (Exception, KeyboardInterrupt, SystemExit) as exc:
            flow_error = exc
            traceback.print_exc()

    if result is not None:
        try:
            state_path.write_text(
                json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            wrote_state = True
        except TypeError:
            state_path.write_text(repr(result) + "\n", encoding="utf-8")
            wrote_state = True

    if result_printer is not None and result is not None:
        result_printer(result)

    if log_path.exists():
        parser_command = [
            sys.executable,
            str(parser_script_path),
            str(log_path),
            "--outdir",
            str(parser_outdir),
            "--pretty",
            "--overwrite",
        ]
        if wrote_state:
            parser_command.extend(["--state-file", str(state_path)])
        parser_result = subprocess.run(
            parser_command,
            cwd=str(parser_script_path.parent),
            capture_output=True,
            text=True,
        )
        if parser_result.stdout:
            print(parser_result.stdout, end="")
        if parser_result.stderr:
            print(parser_result.stderr, end="", file=sys.stderr)
        if parser_result.returncode != 0:
            print(
                f"Parser warning: failed to parse {log_path} into {parser_outdir}.",
                file=sys.stderr,
            )
    else:
        print(
            f"Parser warning: session log not found at {log_path}; parsed logs were not created.",
            file=sys.stderr,
        )

    if flow_error is not None:
        if isinstance(flow_error, (SystemExit, KeyboardInterrupt)):
            raise flow_error
        raise SystemExit(1)

    return result


if __name__ == "__main__":
    for archived in collect_legacy_logs(Path(__file__).resolve().parent):
        print(f"Archived: {archived}")
