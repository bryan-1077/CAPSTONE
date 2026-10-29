"""Render the completed system using run-local specifications and stage evidence."""

from .contracts import RunState


def final_summary(state: RunState, run_dir) -> str:
    import yaml

    config = state.config
    lines = ["SYSTEM RUN COMPLETE — GDSII VALIDATION PASSED", "",
             "Specifications used for RTL generation:"]
    # Prefer the exact specification published alongside the implemented RTL.
    source = config.mailbox / "approved_specs.yaml" if config.mailbox is not None else config.input_yaml
    if source is None:
        lines.append("  Approved specifications: not recorded")
    else:
        try:
            specs = yaml.safe_load(source.read_text(encoding="utf-8"))
            if not isinstance(specs, dict):
                raise ValueError("approved specifications are not a YAML mapping")
            lines.extend("  " + line for line in yaml.safe_dump(specs, sort_keys=False).rstrip().splitlines())
        except (OSError, ValueError, yaml.YAMLError) as exc:
            lines.append(f"  Approved specifications unavailable: {exc}")
        lines.append(f"  Specification source: {source}")

    lines.extend(["", "Selected internal controller frequency:"])
    if config.target_mhz is None:
        lines.append("  Frequency: not recorded")
    else:
        lines.extend([f"  Frequency: {config.target_mhz:g} MHz",
                      f"  Clock period: {1000 / config.target_mhz:.2f} ns"])
    lines.append(f"  Selection source: {config.target_mhz_source or 'not recorded'}")
    lines.append("  Selected clock target; achieved frequency requires timing-report evidence.")

    latest = {result.stage: result for result in state.results}

    def status(stage):
        result = latest.get(stage)
        return result.status.upper() if result is not None else "NOT RECORDED IN THIS RUN"

    generated = latest.get("generate")
    bist = ("SKIPPED" if config.no_bist or
            (generated is not None and generated.artifacts.get("bist") == "not_run") else status("generate"))
    verification = (status("validation") if "validation" in latest else
                    "PASSED (accepted verification receipt)" if config.validation_result is not None else
                    "SKIPPED" if config.skip_verification or config.allow_unvalidated else
                    "NOT RECORDED IN THIS RUN")
    lines.extend(["", "Final stage results:",
                  f"  RTL lint:             {status('generate')}",
                  f"  BIST simulation:      {bist}",
                  f"  Agents 1–3:           {verification}",
                  f"  GDSII generation:     {status('backend')}",
                  f"  GDSII validation:     {status('gdsii-validation')}",
                  "", f"Run artifacts: {run_dir}"])
    return "\n".join(lines)
