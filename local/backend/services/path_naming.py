from __future__ import annotations


def resolve_prepare_dir(
    prep_candidate_path: str | None,
    remote_prepare_dir: str | None,
) -> tuple[str, str]:
    candidate_value = (prep_candidate_path or "").strip()
    if candidate_value:
        return candidate_value, "state.prep_candidate_path"

    configured_value = (remote_prepare_dir or "").strip()
    if configured_value:
        return configured_value, "state.remote_prepare_dir"

    return "build_prep_mc_lang", "fallback.default_prepare_dir"


def derive_netlist_dir_from_prep(prep_dir: str | None) -> str | None:
    raw_value = (prep_dir or "").strip()
    if not raw_value:
        return None

    suffix = raw_value
    if suffix.startswith("build_prep_"):
        suffix = suffix[len("build_prep_") :]
    if suffix.startswith("universal_"):
        suffix = suffix[len("universal_") :]

    if suffix.endswith("_lang"):
        return f"build_netlist_{suffix}"
    return f"build_netlist_{suffix}_lang"


def resolve_netlist_dir(
    remote_netlist_dir: str | None,
    prep_dir: str | None,
) -> str:
    return resolve_netlist_dir_choice(remote_netlist_dir, prep_dir)[0]


def resolve_netlist_dir_choice(
    remote_netlist_dir: str | None,
    prep_dir: str | None,
) -> tuple[str, str]:
    configured_value = (remote_netlist_dir or "").strip()
    if configured_value:
        return configured_value, "state.remote_netlist_dir"

    derived_value = derive_netlist_dir_from_prep(prep_dir)
    if derived_value:
        return derived_value, f"derived_from_prepare_dir:{(prep_dir or '').strip()}"

    return "build_netlist_lang", "fallback.default_netlist_dir"


def derive_mapped_dir_from_netlist(netlist_dir: str | None) -> str | None:
    raw_value = (netlist_dir or "").strip()
    if not raw_value:
        return None

    suffix = raw_value
    if suffix.startswith("build_netlist_"):
        suffix = suffix[len("build_netlist_") :]

    if suffix.endswith("_lang"):
        return f"build_mapped_{suffix}"
    return f"build_mapped_{suffix}_lang"


def resolve_mapped_dir(
    remote_mapped_dir: str | None,
    netlist_dir: str | None,
    prep_dir: str | None,
) -> str:
    return resolve_mapped_dir_choice(remote_mapped_dir, netlist_dir, prep_dir)[0]


def resolve_mapped_dir_choice(
    remote_mapped_dir: str | None,
    netlist_dir: str | None,
    prep_dir: str | None,
) -> tuple[str, str]:
    configured_value = (remote_mapped_dir or "").strip()
    if configured_value:
        return configured_value, "state.remote_mapped_dir"

    derived_netlist_dir = (
        resolve_netlist_dir(None, prep_dir) if not netlist_dir else netlist_dir
    )
    derived_value = derive_mapped_dir_from_netlist(derived_netlist_dir)
    if derived_value:
        return derived_value, f"derived_from_netlist_dir:{derived_netlist_dir}"

    return "build_mapped_lang", "fallback.default_mapped_dir"


def derive_gdsii_dir_from_mapped(mapped_dir: str | None) -> str | None:
    raw_value = (mapped_dir or "").strip()
    if not raw_value:
        return None

    suffix = raw_value
    if suffix.startswith("build_mapped_"):
        suffix = suffix[len("build_mapped_") :]

    if suffix.endswith("_lang"):
        return f"build_GDSII_{suffix}"
    return f"build_GDSII_{suffix}_lang"


def resolve_gdsii_dir(
    remote_gdsii_dir: str | None,
    mapped_dir: str | None,
    netlist_dir: str | None,
    prep_dir: str | None,
) -> str:
    return resolve_gdsii_dir_choice(
        remote_gdsii_dir,
        mapped_dir,
        netlist_dir,
        prep_dir,
    )[0]


def resolve_gdsii_dir_choice(
    remote_gdsii_dir: str | None,
    mapped_dir: str | None,
    netlist_dir: str | None,
    prep_dir: str | None,
) -> tuple[str, str]:
    configured_value = (remote_gdsii_dir or "").strip()
    if configured_value:
        return configured_value, "state.remote_gdsii_dir"

    derived_mapped_dir = (
        mapped_dir
        if mapped_dir
        else resolve_mapped_dir(None, netlist_dir, prep_dir)
    )
    derived_value = derive_gdsii_dir_from_mapped(derived_mapped_dir)
    if derived_value:
        return derived_value, f"derived_from_mapped_dir:{derived_mapped_dir}"

    return "build_GDSII_lang", "fallback.default_gdsii_dir"
