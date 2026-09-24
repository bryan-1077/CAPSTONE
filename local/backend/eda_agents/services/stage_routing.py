from __future__ import annotations

from typing import Any, Mapping


def _normalized_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _normalized_route_stage(value: Any) -> str | None:
    text = _normalized_text(value)
    if text == "prep":
        return "rtl_prep"
    return text


def extract_review_routing(review_payload: Mapping[str, Any]) -> dict[str, Any]:
    blocking_errors = review_payload.get("blocking_errors")
    if not isinstance(blocking_errors, list):
        blocking_errors = review_payload.get("errors", [])

    normalized_errors = [
        str(item).strip()
        for item in blocking_errors
        if str(item).strip()
    ]

    owner = _normalized_text(review_payload.get("failure_stage_owner"))
    failure_class = _normalized_text(review_payload.get("failure_class"))
    route_back_to_stage = _normalized_route_stage(review_payload.get("route_back_to_stage"))

    return {
        "failure_stage_owner": owner,
        "failure_class": failure_class,
        "route_back_to_stage": route_back_to_stage,
        "blocking_errors": normalized_errors,
        "is_prep_owned": owner == "prep" and route_back_to_stage == "rtl_prep",
        "is_netlist_owned": owner == "netlist" and route_back_to_stage in (None, "netlist"),
        "is_mapped_owned": owner == "mapped_netlist" and route_back_to_stage in (None, "mapped_netlist"),
        "is_gdsii_owned": owner == "gdsii" and route_back_to_stage in (None, "gdsii"),
    }


def should_run_netlist_editor(review_payload: Mapping[str, Any]) -> bool:
    routing = extract_review_routing(review_payload)
    return bool(routing["blocking_errors"]) and routing["is_netlist_owned"]


def should_run_mapped_editor(review_payload: Mapping[str, Any]) -> bool:
    routing = extract_review_routing(review_payload)
    return bool(routing["blocking_errors"]) and routing["is_mapped_owned"]


def should_run_gdsii_editor(review_payload: Mapping[str, Any]) -> bool:
    routing = extract_review_routing(review_payload)
    return bool(routing["blocking_errors"]) and routing["is_gdsii_owned"]
