"""Runtime accounting, including timestamp fallback for older run artifacts."""

from datetime import datetime
import math
from pathlib import Path
import re


def seconds(artifacts, key):
    try:
        value = float(artifacts[key])
        return value if math.isfinite(value) and value >= 0 else None
    except (KeyError, ValueError, TypeError):
        return None


def log_events(path):
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        match = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) [^]]+\] (.*)", line)
        if match:
            events.append((datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S"), match[2]))
    return events


def span(events):
    if len(events) < 2:
        return None
    return max(0.0, (events[-1][0] - events[0][0]).total_seconds())


def stage_seconds(result):
    measured = seconds(result.artifacts, "elapsed_seconds")
    if measured is not None:
        return measured
    events = log_events(result.artifacts.get("log"))
    if result.stage == "generate":
        events += log_events(result.artifacts.get("bist_log"))
    elif result.stage == "validation":
        # Exclude the workspace setup message preceding the pipeline itself.
        events = [event for event in events if "CAPSTONE_WORKSPACE_" not in event[1]]
    elif result.stage == "backend":
        end = next((i for i, event in enumerate(events) if event[1] == "Flow completed"), None)
        if end is None:
            return None
        events = events[:end + 1]
    return span(events)


def add(values):
    return None if any(value is None for value in values) else sum(values)


def runtime_stats(state):
    frontend, verification, backend = [], [], []
    lec_ran = False
    for result in state.results:
        if result.stage == "generate":
            frontend.append(stage_seconds(result))
        elif result.stage in ("validation", "gdsii-validation"):
            verification.append(stage_seconds(result))
        elif result.stage == "backend":
            lec = result.artifacts.get("lec", "not_run") not in ("not_run", "skipped")
            lec_ran |= lec
            lec_seconds = seconds(result.artifacts, "lec_seconds") if lec else 0.0
            physical = seconds(result.artifacts, "physical_design_seconds")
            if physical is None:
                elapsed = stage_seconds(result)
                physical = None if elapsed is None or lec_seconds is None else max(0.0, elapsed - lec_seconds)
            backend.append(physical)
            if lec:
                verification.append(lec_seconds)
    # A receipt from another run does not establish runtime in this run.
    front = add(frontend) if frontend else None
    verify = add(verification) if verification else None
    physical = add(backend) if backend else None
    return front, verify, physical, add([front, verify, physical]), lec_ran


def format_seconds(value):
    if value is None:
        return "NOT RECORDED IN THIS RUN"
    total = int(value + 0.5)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return (f"{hours}h " if hours else "") + f"{minutes}m {secs:02d}s"
