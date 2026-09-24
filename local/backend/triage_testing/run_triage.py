"""Review either test archive directly, with optional real tool diagnostics."""
import argparse
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from agents.openai_rtl_failure_triage import run_openai_rtl_failure_triage
from app import _load_service_exports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", action="store_true", help="Review the unchanged archive.")
    parser.add_argument("--log", type=Path, help="Optional real simulation/synthesis report.")
    args = parser.parse_args()
    _load_service_exports()
    archive = ROOT / ("MemoryController_legacy.zip" if args.legacy else "MemoryController.zip")
    with zipfile.ZipFile(archive) as bundle:
        sources = {name: bundle.read(name).decode() for name in bundle.namelist() if name.endswith(".sv")}
    # Exclude the mutation manifest and answer key from the review evidence.
    result = run_openai_rtl_failure_triage(
        execution_context={"top_module": "ddr4_controller_top"},
        rtl_sources=sources,
        logs={"log_text": args.log.read_text()} if args.log else {},
    )
    output = ROOT / ("legacy_triage_result.json" if args.legacy else "triage_result.json")
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({
        "handoff_owner": result.get("handoff_owner"),
        "failure_category": result.get("failure_category"),
        "summary": result.get("summary"),
        "ai_available": result.get("available"),
        "api_error": result.get("api_error"),
        "output": str(output),
    }, indent=2))
    return 0 if result.get("available") else 1


if __name__ == "__main__":
    raise SystemExit(main())
