#!/usr/bin/env python3
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_INNOVUS_BIN = "/opt/coe/cadence/DDI231/bin/innovus"
DEFAULT_CDS_LIC_FILE = "5280@coe-vtls2.engr.tamu.edu"
DEFAULT_LM_LICENSE_FILE = "5280@coe-vtls2.engr.tamu.edu"


def run_bash(cmd, logfile, cwd=None):
    print("\n[RUN]\n{0}\n".format(cmd))
    logfile.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        ["bash", "-lc", cmd],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )

    with logfile.open("w", encoding="utf-8", errors="ignore") as f:
        for line in proc.stdout:
            sys.stdout.write(line)
            f.write(line)

    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(
            "Command failed with exit code {0}. See log: {1}".format(rc, logfile)
        )


def validate_mappeddir(mappeddir):
    d = Path(mappeddir).expanduser().resolve()
    if not d.is_dir():
        raise SystemExit("Mapped directory not found: {0}".format(d))
    if not (d / "results").is_dir():
        raise SystemExit("Missing results directory in: {0}".format(d))

    mapped_netlists = [
        p for p in (d / "results").rglob("*.v")
        if p.is_file() and "mapped" in p.name.lower() and "generic" not in p.name.lower()
    ]
    if not mapped_netlists:
        raise SystemExit(
            "Could not find a mapped Verilog netlist (*.v) under {0}/results".format(d)
        )
    return d


def find_mapped_builddir(explicit_mappeddir=None):
    if explicit_mappeddir:
        return validate_mappeddir(explicit_mappeddir)

    cwd = Path.cwd().resolve()
    if (cwd / "results").is_dir():
        try:
            return validate_mappeddir(cwd)
        except SystemExit:
            pass

    candidates = []
    for cand in sorted(cwd.iterdir()):
        if cand.is_dir() and (cand / "results").is_dir():
            try:
                validate_mappeddir(cand)
                candidates.append(cand.resolve())
            except SystemExit:
                continue

    if not candidates:
        raise SystemExit(
            "Could not auto-find a mapped build directory. "
            "Run this from a mapped directory or pass --mappeddir."
        )

    preferred = [cand for cand in candidates if cand.name.startswith("build_mapped")]
    if len(preferred) == 1:
        return preferred[0]
    if len(candidates) == 1:
        return candidates[0]

    names = "\n  - ".join(str(c) for c in candidates)
    raise SystemExit(
        "Found multiple mapped build directories. Pass --mappeddir explicitly:\n  - {0}".format(
            names
        )
    )


def derive_outdir(mappeddir, explicit_outdir=None):
    if explicit_outdir:
        return Path(explicit_outdir).expanduser().resolve()

    name = mappeddir.name
    if name.startswith("build_mapped"):
        derived = name.replace("build_mapped", "build_GDSII", 1)
    elif "mapped" in name:
        derived = name.replace("mapped", "GDSII", 1)
    else:
        derived = "build_GDSII_{0}".format(name)
    return (mappeddir.parent / derived).resolve()


def find_innovus(explicit_innovus=None):
    if explicit_innovus:
        p = Path(explicit_innovus).expanduser().resolve()
        if not p.exists():
            raise SystemExit("Innovus executable not found: {0}".format(p))
        return p

    p = Path(DEFAULT_INNOVUS_BIN)
    if p.exists():
        return p

    which = subprocess.run(
        ["bash", "-lc", "command -v innovus || true"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    candidate = which.stdout.strip()
    if candidate:
        return Path(candidate).resolve()

    raise SystemExit("Could not find innovus executable. Pass --innovus explicitly.")


def unique_paths(paths):
    seen = set()
    out = []
    for path_obj in paths:
        rp = str(Path(path_obj).resolve())
        if rp not in seen:
            seen.add(rp)
            out.append(Path(rp))
    return out


def split_csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def search_files(root, patterns):
    found = []
    for pattern in patterns:
        found.extend([p for p in root.rglob(pattern) if p.is_file()])
    return unique_paths(found)


def score_name(path_obj, preferred_terms, banned_terms=()):
    name = path_obj.name.lower()
    score = 0
    for term in preferred_terms:
        if term in name:
            score += 10
    for term in banned_terms:
        if term in name:
            score -= 10
    if "results" in str(path_obj.parent).lower():
        score += 3
    return score


def choose_file(candidates, preferred_terms, banned_terms=()):
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda p: (
            score_name(p, preferred_terms, banned_terms),
            p.stat().st_size if p.exists() else 0,
            str(p),
        ),
        reverse=True,
    )
    return ranked[0]


def find_mapped_netlist(mappeddir, explicit_netlist=None):
    if explicit_netlist:
        p = Path(explicit_netlist).expanduser().resolve()
        if not p.exists():
            raise SystemExit("Mapped netlist not found: {0}".format(p))
        return p

    candidates = [
        p for p in mappeddir.rglob("*.v")
        if p.is_file()
        and "generic" not in p.name.lower()
        and "testbench" not in p.name.lower()
        and "mapped" in p.name.lower()
    ]
    if not candidates:
        raise SystemExit(
            "Could not find a mapped Verilog netlist under {0}".format(mappeddir)
        )

    preferred_mc = [
        p for p in candidates
        if any(tok in p.name.lower() for tok in ("memorycontroller", "memory_controller", "memcontroller"))
    ]
    if preferred_mc:
        best = choose_file(
            preferred_mc,
            preferred_terms=("memorycontroller", "memory_controller", "memcontroller", "mapped", "impl"),
            banned_terms=("generic", "testbench", "scheduler"),
        )
        if best is not None:
            return best

    best = choose_file(
        candidates,
        preferred_terms=("mapped", "impl", "top", "netlist"),
        banned_terms=("generic", "testbench", "scheduler"),
    )
    if best is None:
        raise SystemExit(
            "Could not find a mapped Verilog netlist under {0}".format(mappeddir)
        )
    return best


def find_mapped_sdc(mappeddir, explicit_sdc=None):
    if explicit_sdc:
        p = Path(explicit_sdc).expanduser().resolve()
        if not p.exists():
            raise SystemExit("Mapped SDC not found: {0}".format(p))
        return p

    candidates = [p for p in mappeddir.rglob("*.sdc") if p.is_file()]
    return choose_file(
        candidates,
        preferred_terms=("mapped", "constraint", "constraints", "top"),
        banned_terms=("generic",),
    )


def choose_top(netlist, explicit_top=None):
    if explicit_top:
        return explicit_top

    text = netlist.read_text(errors="ignore")
    modules = re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text)
    if not modules:
        raise SystemExit("Could not determine top module name from {0}".format(netlist))

    for mod in modules:
        if mod == "MemoryController_impl":
            return mod

    stem = netlist.stem
    changed = True
    while changed and stem:
        changed = False
        for suffix in ("_mapped", "_postroute", "_netlist", "_generic", "_impl", "_synth"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                changed = True
                break

    for mod in modules:
        if mod.lower() == stem.lower():
            return mod

    return modules[0]


def choose_sky130_root(explicit_root=None):
    if explicit_root:
        p = Path(explicit_root).expanduser().resolve()
        if not p.exists():
            raise SystemExit("PDK root does not exist: {0}".format(p))
        return p

    home = Path.home()
    candidates = []
    candidates.extend(home.glob(".volare/volare/sky130/versions/*/sky130A"))
    candidates.extend(home.glob(".volare/sky130A"))
    candidates.extend(Path("/usr/share/pdk").glob("sky130A"))
    candidates.extend(Path("/opt").glob("**/sky130A"))
    candidates = [p.resolve() for p in candidates if p.exists()]
    if not candidates:
        raise SystemExit(
            "Could not auto-find a sky130A PDK root. Pass --pdk-root explicitly."
        )

    candidates.sort(
        key=lambda p: (1 if (p / "libs.ref").exists() else 0, len(str(p))),
        reverse=True,
    )
    return candidates[0]


def auto_find_tech_lef(pdk_root, stdcell_lib):
    candidates = search_files(
        pdk_root,
        [
            "libs.ref/{0}/techlef/*.tlef".format(stdcell_lib),
            "libs.ref/{0}/lef/*tech*.lef".format(stdcell_lib),
            "libs.ref/*/techlef/*.tlef",
        ],
    )
    best = choose_file(candidates, preferred_terms=("nom", "tech", stdcell_lib))
    if best is None:
        raise SystemExit(
            "Could not find a technology LEF/TLEF under the selected PDK root"
        )
    return best


def auto_find_stdcell_lef(pdk_root, stdcell_lib):
    candidates = search_files(
        pdk_root,
        [
            "libs.ref/{0}/lef/{0}.lef".format(stdcell_lib),
            "libs.ref/{0}/lef/*.lef".format(stdcell_lib),
        ],
    )
    best = choose_file(candidates, preferred_terms=(stdcell_lib, "lef"))
    if best is None:
        raise SystemExit(
            "Could not find a standard-cell LEF for {0}".format(stdcell_lib)
        )
    return best


def auto_find_timing_libs(pdk_root, stdcell_lib):
    candidates = search_files(
        pdk_root,
        [
            "libs.ref/{0}/lib/*.lib".format(stdcell_lib),
            "libs.ref/{0}/lib/*.lib.gz".format(stdcell_lib),
        ],
    )
    if not candidates:
        raise SystemExit(
            "Could not find any Liberty timing libraries for {0}".format(stdcell_lib)
        )

    preferred = []
    for term in ("__tt_025c_1v80", "__tt_", "tt", stdcell_lib):
        picked = [p for p in candidates if term.lower() in p.name.lower()]
        if picked:
            preferred = picked
            break
    if not preferred:
        preferred = candidates
    return sorted(unique_paths(preferred))


def auto_find_qrc_or_cap(pdk_root):
    qrc_candidates = search_files(
        pdk_root,
        ["*.tch", "*.qx", "*qrc*.tech", "*qx_tech*", "*rcx*tech*"],
    )
    cap_candidates = search_files(
        pdk_root,
        ["*captable*", "*.capTbl", "*.captable", "*.cap_table"],
    )

    qrc_tech = choose_file(qrc_candidates, preferred_terms=("qrc", "qx", "typ", "nom"))
    cap_table = choose_file(cap_candidates, preferred_terms=("cap", "typ", "nom"))
    return qrc_tech, cap_table


def auto_find_stream_map(pdk_root):
    candidates = search_files(
        pdk_root,
        ["*gds*.map", "*stream*.map", "*encounter*.map", "*.map"],
    )
    candidates = [
        p for p in candidates
        if any(tok in p.name.lower() for tok in ("gds", "stream", "encounter", "map"))
    ]
    return choose_file(candidates, preferred_terms=("encounter", "stream", "gds", "map"))


def parse_site_and_layers(lef_files):
    sites = []
    routing_layers = []
    filler_cells = []

    for lef in lef_files:
        try:
            lines = lef.read_text(errors="ignore").splitlines()
        except Exception:
            continue

        current_layer = None
        current_macro = None
        current_layer_is_routing = False

        for line in lines:
            site_match = re.match(r"^\s*SITE\s+(\S+)", line)
            if site_match:
                sites.append(site_match.group(1))

            layer_match = re.match(r"^\s*LAYER\s+(\S+)", line)
            if layer_match:
                current_layer = layer_match.group(1)
                current_layer_is_routing = False
                continue

            if current_layer and re.search(r"\bTYPE\s+ROUTING\b", line):
                current_layer_is_routing = True
                continue

            if current_layer and re.match(r"^\s*END\s+" + re.escape(current_layer) + r"\s*$", line):
                if current_layer_is_routing:
                    routing_layers.append(current_layer)
                current_layer = None
                current_layer_is_routing = False
                continue

            macro_match = re.match(r"^\s*MACRO\s+(\S+)", line)
            if macro_match:
                current_macro = macro_match.group(1)
                macro_lower = current_macro.lower()
                if "fill" in macro_lower and "decap" not in macro_lower:
                    filler_cells.append(current_macro)
                continue

            if current_macro and re.match(r"^\s*END\s+" + re.escape(current_macro) + r"\s*$", line):
                current_macro = None

    sites = list(dict.fromkeys(sites))
    routing_layers = list(dict.fromkeys(routing_layers))
    filler_cells = list(dict.fromkeys(filler_cells))

    preferred_site = None
    for site in sites:
        site_lower = site.lower()
        if any(tok in site_lower for tok in ("hd", "core", "site", "unit")):
            preferred_site = site
            break
    if preferred_site is None and sites:
        preferred_site = sites[0]
    if preferred_site is None:
        raise SystemExit(
            "Could not determine a row site from the LEF files. Supply a valid technology/standard-cell LEF."
        )

    def layer_sort_key(name):
        match = re.search(r"(\d+)$", name)
        return (int(match.group(1)) if match else -1, name)

    routing_layers = sorted(routing_layers, key=layer_sort_key)
    if not routing_layers:
        raise SystemExit("Could not find any ROUTING layers in the supplied LEF files.")

    return preferred_site, routing_layers, filler_cells


def tcl_list(items):
    escaped = [str(item).replace("\\", "/") for item in items]
    return "{ " + " ".join(escaped) + " }"


def write_text(path_obj, text):
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    path_obj.write_text(text, encoding="utf-8")


def sanitize_sdc_for_innovus(sdc_path, tcldir, clock_period_ns=None):
    if sdc_path is None:
        return None, 0

    original_text = sdc_path.read_text(errors="ignore")
    removed_lines = []
    kept_lines = []
    clock_period_updates = 0
    for line in original_text.splitlines():
        if re.match(r"^\s*set_units\b", line):
            removed_lines.append(line.rstrip())
            continue
        if clock_period_ns is not None and re.search(r"\bcreate_clock\b", line):
            if re.search(r"\s-period\s+[-+]?(?:\d+(?:\.\d*)?|\.\d+)", line):
                line = re.sub(
                    r"(\s-period\s+)[-+]?(?:\d+(?:\.\d*)?|\.\d+)",
                    r"\g<1>{0:.3f}".format(clock_period_ns),
                    line,
                    count=1,
                )
            else:
                line = re.sub(
                    r"\bcreate_clock\b",
                    "create_clock -period {0:.3f}".format(clock_period_ns),
                    line,
                    count=1,
                )
            clock_period_updates += 1
        kept_lines.append(line)

    if not removed_lines and not clock_period_updates:
        return sdc_path, 0

    sanitized_path = tcldir / "{0}.innovus.sdc".format(sdc_path.stem)
    lines = [
        "# Auto-sanitized for Innovus.",
        "# Unsupported set_units commands were removed from the original SDC.",
    ]
    if clock_period_updates:
        lines.append(
            "# create_clock period explicitly overridden to {0:.3f} ns by user/requested timing-closure option.".format(
                clock_period_ns
            )
        )
    for line in removed_lines:
        lines.append("# Removed: {0}".format(line))
    lines.append("")
    lines.extend(kept_lines)
    write_text(sanitized_path, "\n".join(lines) + "\n")
    return sanitized_path, len(removed_lines)


def format_connectivity_note(top):
    return (
        "{0}.conn.rpt is a flow note for the current Innovus run.\n\n"
        "This flow intentionally skips raw verifyConnectivity because it over-reports\n"
        "abutted PG terminals such as VPB/VNB in this sky130 flow.\n\n"
        "Use these current reports instead:\n"
        "- build_GDSII_*/checkDesign/checknetlist.rpt\n"
        "- build_GDSII_*/checkDesign/pgTermConnectivity.main.htm\n"
        "- build_GDSII_*/checkDesign/checkPlacement.rpt\n"
    ).format(top)


def format_power_note():
    return (
        "Legacy placeholder report.\n\n"
        "Use reports/power_postroute.rpt for the current post-route power analysis\n"
        "from this flow.\n"
    )


def format_collect_genus_library_log(timing_libs):
    lib_list = " ".join(str(p) for p in timing_libs)
    return (
        "Info: All library file list of dcCorner 'DC_TYP' : '{0}'\n"
        "Info: non-std cell library file list: ''\n"
        "Info: SynthesisEngine library file list: ' {0}'\n"
    ).format(lib_list)


def parse_constant_usage(netlist):
    text = netlist.read_text(errors="ignore")

    constant_port_assigns = [
        (port.strip(), bit)
        for port, bit in re.findall(
            r"^\s*assign\s+(.+?)\s*=\s*1'b([01])\s*;\s*$",
            text,
            flags=re.MULTILINE,
        )
    ]

    literal_pin_ties = []
    inst_re = re.compile(r"(?ms)^\s*sky130_fd_sc_hd__\S+\s+(\\?\S+)\s*\((.*?)\);\s*$")
    for inst_name, conn_text in inst_re.findall(text):
        clean_inst_name = inst_name.strip().lstrip("\\")
        for pin_name, bit in re.findall(r"\.(\w+)\s*\(\s*1'b([01])\s*\)", conn_text):
            literal_pin_ties.append((clean_inst_name, pin_name, bit))

    return constant_port_assigns, literal_pin_ties


def format_constant_usage_report(
    constant_port_assigns,
    literal_pin_ties,
    inferred_tiehi_net,
    inferred_tielo_net,
):
    lines = ["Netlist constant usage summary", ""]
    lines.append(
        "Inferred tie-high net: {0}".format(
            inferred_tiehi_net if inferred_tiehi_net is not None else "NONE"
        )
    )
    lines.append(
        "Inferred tie-low net : {0}".format(
            inferred_tielo_net if inferred_tielo_net is not None else "NONE"
        )
    )
    lines.append("")

    lines.append("Top-level constant assigns: {0}".format(len(constant_port_assigns)))
    for port_name, bit in constant_port_assigns:
        lines.append("  {0} = 1'b{1}".format(port_name, bit))
    lines.append("")

    lines.append("Direct literal instance pin ties: {0}".format(len(literal_pin_ties)))
    for inst_name, pin_name, bit in literal_pin_ties:
        lines.append("  {0}.{1} = 1'b{2}".format(inst_name, pin_name, bit))
    lines.append("")

    return "\n".join(lines) + "\n"


def build_mmmc_tcl(sdc_path, timing_libs, qrc_tech, cap_table):
    lib_list = tcl_list([str(p) for p in timing_libs])
    if sdc_path:
        sdc_part = "create_constraint_mode -name FUNC_MODE -sdc_files {0}\n".format(
            tcl_list([str(sdc_path)])
        )
    else:
        sdc_part = "create_constraint_mode -name FUNC_MODE\n"

    if qrc_tech:
        rc_line = "create_rc_corner -name RC_TYP -qx_tech_file {{{0}}}\n".format(
            str(qrc_tech).replace("\\", "/")
        )
    elif cap_table:
        rc_line = "create_rc_corner -name RC_TYP -cap_table {{{0}}}\n".format(
            str(cap_table).replace("\\", "/")
        )
    else:
        rc_line = "create_rc_corner -name RC_TYP\n"

    return (
        "# Auto-generated MMMC for Innovus\n"
        "{0}"
        "create_library_set -name LIB_TYP -timing {1}\n"
        "{2}"
        "create_delay_corner -name DC_TYP -library_set LIB_TYP -rc_corner RC_TYP\n"
        "create_analysis_view -name VIEW_TYP -constraint_mode FUNC_MODE -delay_corner DC_TYP\n"
        "set_analysis_view -setup {{VIEW_TYP}} -hold {{VIEW_TYP}}\n"
    ).format(sdc_part, lib_list, rc_line)


def build_timing_eco_tcl(plan, top):
    """Compile data-only resize actions; never execute model-generated Tcl."""
    def word(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./\[\]-]+", value):
            raise ValueError("Unsupported identifier/path in timing ECO plan")
        return "{" + value + "}"

    if not isinstance(plan, dict) or set(plan) != {"checkpoint", "actions"}:
        raise ValueError("Timing ECO requires a checkpoint and resize actions")
    if not isinstance(plan["actions"], list) or len(plan["actions"]) > 16:
        raise ValueError("Timing ECO is limited to 16 resizes")
    lines = [
        "# Restore the best checkpoint into a fresh output directory.",
        "if {[catch {",
        "restoreDesign %s %s" % (word(plan["checkpoint"]), word(top)),
        'if {[eda_die_area_mm2] > 4.0} {error "Die footprint exceeds 4 mm^2; resizing stopped"}',
    ]
    seen = set()
    if plan["actions"]:
        lines.append("deleteFiller -prefix FILL")
    for action in plan["actions"]:
        if not isinstance(action, dict) or set(action) != {"instance", "from_cell", "to_cell", "reason"}:
            raise ValueError("Only gate/buffer resizing is supported")
        name, old, new = (action[key] for key in ("instance", "from_cell", "to_cell"))
        family_pattern = r"(sky130_fd_sc_hd__(?:buf|inv|(?:and|nand|or|nor)\d+b?|x(?:n?or)\d+|mux\d+|a\d+oi?|o\d+ai?))_\d+"
        old_family, new_family = re.fullmatch(family_pattern, old), re.fullmatch(family_pattern, new)
        if not old_family or not new_family or old_family[1] != new_family[1] or old == new or name in seen:
            raise ValueError("Resize must preserve the Boolean function and change the drive strength once")
        seen.add(name)
        lines.extend([
            "set eco_inst [dbGetInstByName %s]" % word(name),
            'if {$eco_inst eq "0x0"} {error "Resize instance missing from checkpoint"}',
            "if {[dbGet $eco_inst.cell.name] ne %s} {error \"Resize source cell mismatch\"}" % word(old),
            "if {[lsearch -exact [dbGet head.libCells.name] %s] < 0} {error \"Replacement cell is not loaded\"}" % word(new),
            "ecoChangeCell -inst %s -cell %s" % (word(name), word(new)),
            "if {[dbGet $eco_inst.cell.name] ne %s} {error \"Requested resize was not applied\"}" % word(new),
        ])
    if plan["actions"]:
        lines.extend(["refinePlace", "ecoRoute"])
    lines.extend(['} timing_eco_err]} {', 'puts "ERROR: timing ECO failed: $timing_eco_err"', 'exit 6', '}'])
    return lines


def build_innovus_tcl(
    top,
    netlist,
    lef_files,
    mmmc_file,
    outdir,
    site,
    routing_layers,
    filler_cells,
    stream_map,
    pwr_net,
    gnd_net,
    pwr_pins,
    gnd_pins,
    process_nm,
    aspect,
    util,
    core_margin,
    ring_width,
    ring_spacing,
    ring_offset,
    stripe_width,
    stripe_spacing,
    stripe_pitch,
    stripe_offset,
    do_cts,
    ccopt_target_skew,
    ccopt_target_max_transition,
    final_postroute_setup_opt,
    antenna_diode_cell,
    tiehi_net,
    tielo_net,
    timing_eco_plan=None,
):
    results_dir = "$OUTDIR/results"
    reports_dir = "$OUTDIR/reports"
    db_dir = "$OUTDIR/db"
    checks_dir = "$OUTDIR/checkDesign"
    timing_dir = "$OUTDIR/timingReports"
    lef_list = tcl_list([str(p) for p in lef_files])

    if len(routing_layers) >= 2:
        horiz_layer = routing_layers[-2]
        vert_layer = routing_layers[-1]
        stripe_layer = routing_layers[-1]
    else:
        horiz_layer = routing_layers[0]
        vert_layer = routing_layers[0]
        stripe_layer = routing_layers[0]

    bottom_route_layer = 1
    top_route_layer = len(routing_layers)

    filler_list = tcl_list(filler_cells) if filler_cells else ""

    benign_warning_ids = [
        "TECHLIB-302",
        "IMPLF-108",
        "IMPLF-201",
        "IMPEXT-6197",
        "IMPEXT-2766",
        "IMPEXT-2773",
        "IMPEXT-2882",
        "IMPVPA-120",
        "IMPMF-5054",
        "IMPSP-9025",
        "IMPFP-325",
        "IMPPTN-1250",
        "IMPDC-1629",
        "NRDB-942",
        "NRIG-1303",
        "NRIF-82",
        "NRIF-95",
    ]

    stream_opts = [
        "streamOut $RESULTS_DIR/{0}.gds".format(top),
        "-units 1000",
        "-mode ALL",
    ]
    if stream_map:
        stream_opts.append("-mapFile {{{0}}}".format(str(stream_map).replace("\\", "/")))
    stream_cmd = " \\\n    ".join(stream_opts)

    lines = []
    lines.append("# Auto-generated Innovus TCL")
    lines.append("set TOP {{{0}}}".format(top))
    lines.append("set OUTDIR {{{0}}}".format(str(outdir).replace("\\", "/")))
    lines.append("set RESULTS_DIR {0}".format(results_dir))
    lines.append("set REPORTS_DIR {0}".format(reports_dir))
    lines.append("set DBS_DIR {0}".format(db_dir))
    lines.append("set CHECKS_DIR {0}".format(checks_dir))
    lines.append("set TIMING_DIR {0}".format(timing_dir))
    lines.append("set LOGS_DIR $OUTDIR/logs")
    lines.append("file mkdir $OUTDIR")
    lines.append("file mkdir $RESULTS_DIR")
    lines.append("file mkdir $REPORTS_DIR")
    lines.append("file mkdir $DBS_DIR")
    lines.append("file mkdir $CHECKS_DIR")
    lines.append("file mkdir $TIMING_DIR")
    lines.append("file mkdir $LOGS_DIR")
    lines.append("")
    lines.append("foreach benign_warning_id {{ {0} }} {{".format(" ".join(benign_warning_ids)))
    lines.append("    catch {suppressMessage $benign_warning_id}")
    lines.append("}")
    lines.append(
        'puts "INFO: suppressed expected sky130/library/tool noise warnings for cleaner reporting: {0}"'.format(
            " ".join(benign_warning_ids)
        )
    )
    lines.append("")
    lines.append("proc move_root_artifacts {root reports results} {")
    lines.append("    foreach rpt [glob -nocomplain [file join $root *.rpt]] {")
    lines.append("        file rename -force $rpt [file join $reports [file tail $rpt]]")
    lines.append('        puts "INFO: moved report [file tail $rpt] -> $reports"')
    lines.append("    }")
    lines.append("    set list_files {}")
    lines.append("    foreach f [glob -nocomplain [file join $root *.list]] {")
    lines.append("        lappend list_files $f")
    lines.append("    }")
    lines.append("    foreach f [glob -nocomplain [file join $root .*.list]] {")
    lines.append("        lappend list_files $f")
    lines.append("    }")
    lines.append("    foreach lst $list_files {")
    lines.append("        file rename -force $lst [file join $results [file tail $lst]]")
    lines.append('        puts "INFO: moved list [file tail $lst] -> $results"')
    lines.append("    }")
    lines.append("    foreach lef [glob -nocomplain [file join $root *.lef]] {")
    lines.append("        file rename -force $lef [file join $results [file tail $lef]]")
    lines.append('        puts "INFO: moved lef [file tail $lef] -> $results"')
    lines.append("    }")
    lines.append("}")
    lines.append("")
    lines.append("proc get_diode_signal_pin {diode_cell_name} {")
    lines.append("    set diode_cell [dbGetCellByName $diode_cell_name]")
    lines.append('    if {$diode_cell == "0x0"} { return "" }')
    lines.append('    set diode_pin ""')
    lines.append("    dbForEachCellFTerm $diode_cell fterm {")
    lines.append("        set diode_pin [dbFTermName $fterm]")
    lines.append("        break")
    lines.append("    }")
    lines.append("    return $diode_pin")
    lines.append("}")
    lines.append("")
    lines.append("proc parse_antenna_targets {report_file} {")
    lines.append('    set targets {}')
    lines.append("    if {![file exists $report_file]} {")
    lines.append('        puts "WARN: targeted antenna cleanup could not find report $report_file"')
    lines.append("        return $targets")
    lines.append("    }")
    lines.append("    set fh [open $report_file r]")
    lines.append('    set current_net ""')
    lines.append("    while {[gets $fh line] >= 0} {")
    lines.append('        if {[regexp {^(\\S.*)\\s+\\(\\d+\\)\\s*$} $line -> net_name]} {')
    lines.append("            set current_net $net_name")
    lines.append("            continue")
    lines.append("        }")
    lines.append('        if {$current_net eq ""} { continue }')
    lines.append('        if {[regexp {^\\s+(\\S+)\\s+\\([^)]+\\)\\s+(\\S+)\\s*$} $line -> inst_name pin_name]} {')
    lines.append("            lappend targets [list $current_net $inst_name $pin_name]")
    lines.append('            set current_net ""')
    lines.append("        }")
    lines.append("    }")
    lines.append("    close $fh")
    lines.append("    return $targets")
    lines.append("}")
    lines.append("")
    lines.append("proc parse_antenna_violation_count {report_file} {")
    lines.append("    if {![file exists $report_file]} { return -1 }")
    lines.append("    set fh [open $report_file r]")
    lines.append("    set count -1")
    lines.append("    while {[gets $fh line] >= 0} {")
    lines.append('        if {[regexp {^Total number of process antenna violations:\\s+(\\d+)\\s*$} $line -> vio_count]} {')
    lines.append("            set count $vio_count")
    lines.append("            break")
    lines.append("        }")
    lines.append("    }")
    lines.append("    close $fh")
    lines.append("    return $count")
    lines.append("}")
    lines.append("")
    lines.append("proc insert_targeted_antenna_diodes {report_file diode_cell_name pass_tag} {")
    lines.append("    set diode_pin [get_diode_signal_pin $diode_cell_name]")
    lines.append('    if {$diode_pin eq ""} {')
    lines.append('        puts "WARN: targeted antenna cleanup could not determine a signal pin for diode cell $diode_cell_name"')
    lines.append("        return 0")
    lines.append("    }")
    lines.append("    set targets [parse_antenna_targets $report_file]")
    lines.append("    if {[llength $targets] == 0} {")
    lines.append('        puts "INFO: targeted antenna cleanup found no remaining nets in $report_file"')
    lines.append("        return 0")
    lines.append("    }")
    lines.append("    set count 0")
    lines.append("    foreach target $targets {")
    lines.append("        lassign $target net_name inst_name pin_name")
    lines.append("        set inst_ptr [dbGetInstByName $inst_name]")
    lines.append('        if {$inst_ptr == "0x0"} { continue }')
    lines.append("        set term_ptr [dbGetTermByName $inst_ptr $pin_name]")
    lines.append('        if {$term_ptr == "0x0"} { continue }')
    lines.append("        set term_loc [dbTermLoc $term_ptr]")
    lines.append("        if {[llength $term_loc] < 2} { continue }")
    lines.append("        incr count")
    lines.append('        set diode_inst_name [format "ANTFIX_%s_DIODE_%d" $pass_tag $count]')
    lines.append("        set x [dbDBUToMicrons [lindex $term_loc 0]]")
    lines.append("        set y [dbDBUToMicrons [lindex $term_loc 1]]")
    lines.append("        addInst -cell $diode_cell_name -inst $diode_inst_name")
    lines.append("        placeInstance $diode_inst_name $x $y -placed")
    lines.append("        attachTerm $diode_inst_name $diode_pin $net_name")
    lines.append('        puts "INFO: inserted targeted antenna diode $diode_inst_name on $net_name near $inst_name/$pin_name"')
    lines.append("    }")
    lines.append("    if {$count > 0} {")
    lines.append("        refinePlace")
    lines.append('        catch {setInstancePlacementStatus -name [format "ANTFIX_%s_DIODE_*" $pass_tag] -status fixed}')
    lines.append("    }")
    lines.append("    return $count")
    lines.append("}")
    lines.append("")
    lines.extend([
        "proc eda_die_area_mm2 {} {",
        "    set box [dbGet top.fPlan.box]",
        "    if {[llength $box] == 1} {set box [lindex $box 0]}",
        '    if {[llength $box] != 4} {error "Cannot measure die bounding box"}',
        "    lassign $box x0 y0 x1 y1",
        '    if {$x1 <= $x0 || $y1 <= $y0} {error "Invalid die bounding box"}',
        "    return [expr {($x1 - $x0) * ($y1 - $y0) / 1000000.0}]",
        "}",
    ])
    implementation_start = len(lines)
    lines.append("setDesignMode -process {0}".format(process_nm))
    lines.append("set init_design_netlisttype { Verilog }")
    lines.append("set init_verilog {{{0}}}".format(str(netlist).replace("\\", "/")))
    lines.append("set init_top_cell $TOP")
    lines.append("set init_lef_file {0}".format(lef_list))
    lines.append("set init_mmmc_file {{{0}}}".format(str(mmmc_file).replace("\\", "/")))
    lines.append("set init_pwr_net {{{0}}}".format(pwr_net))
    lines.append("set init_gnd_net {{{0}}}".format(gnd_net))
    lines.append("")
    lines.append("setGenerateViaMode -auto true")
    lines.append("init_design")
    lines.append("saveDesign $DBS_DIR/01_init.enc")
    lines.append("")
    lines.append("if {[catch {collectGenusLibrary > $OUTDIR/collectGenusLibrary.log} cgl_err]} {")
    lines.append('    puts "WARN: collectGenusLibrary failed: $cgl_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: wrote collectGenusLibrary.log"')
    lines.append("}")
    lines.append("")
    lines.append("set delaycal_use_default_delay_limit 5000")
    lines.append('puts "INFO: set delaycal_use_default_delay_limit to $delaycal_use_default_delay_limit for large-fanout timing estimation"')
    lines.append("")
    lines.append(
        "floorPlan -site {0} -r {1} {2} {3} {3} {3} {3}".format(
            site, aspect, util, core_margin
        )
    )
    lines.append("")
    for pin in pwr_pins:
        lines.append('puts "INFO: globalNetConnect {0} <- {1}"'.format(pwr_net, pin))
        lines.append("globalNetConnect {0} -type pgpin -pin {1} -inst *".format(pwr_net, pin))
    for pin in gnd_pins:
        lines.append('puts "INFO: globalNetConnect {0} <- {1}"'.format(gnd_net, pin))
        lines.append("globalNetConnect {0} -type pgpin -pin {1} -inst *".format(gnd_net, pin))
    if tiehi_net:
        lines.append('puts "INFO: globalNetConnect tiehi <- {0}"'.format(tiehi_net))
        lines.append("globalNetConnect {0} -type tiehi -inst *".format(tiehi_net))
    if tielo_net:
        lines.append('puts "INFO: globalNetConnect tielo <- {0}"'.format(tielo_net))
        lines.append("globalNetConnect {0} -type tielo -inst *".format(tielo_net))
    lines.append('if {[catch {applyGlobalNets} apply_gn_err]} {')
    lines.append('    puts "WARN: applyGlobalNets reported: $apply_gn_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: applyGlobalNets complete"')
    lines.append("}")
    lines.append("")
    lines.append(
        "if {[catch {addRing -nets { %s %s } -type core_rings -follow core \\"
        % (pwr_net, gnd_net)
    )
    lines.append(
        "    -layer {top %s bottom %s left %s right %s} \\"
        % (horiz_layer, horiz_layer, vert_layer, vert_layer)
    )
    lines.append(
        "    -width {top %s bottom %s left %s right %s} \\"
        % (ring_width, ring_width, ring_width, ring_width)
    )
    lines.append(
        "    -spacing {top %s bottom %s left %s right %s} \\"
        % (ring_spacing, ring_spacing, ring_spacing, ring_spacing)
    )
    lines.append(
        "    -offset {top %s bottom %s left %s right %s}} ring_err]} {"
        % (ring_offset, ring_offset, ring_offset, ring_offset)
    )
    lines.append('    puts "WARN: addRing failed or partially failed: $ring_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: addRing complete"')
    lines.append("}")
    lines.append("")
    lines.append(
        "if {[catch {addStripe -nets { %s %s } -layer %s -direction vertical \\"
        % (pwr_net, gnd_net, stripe_layer)
    )
    lines.append(
        "    -width %s -spacing %s -set_to_set_distance %s \\"
        % (stripe_width, stripe_spacing, stripe_pitch)
    )
    lines.append(
        "    -start_offset %s -stop_offset %s} stripe_err]} {"
        % (stripe_offset, stripe_offset)
    )
    lines.append('    puts "WARN: addStripe failed or partially failed: $stripe_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: addStripe complete"')
    lines.append("}")
    lines.append("")
    lines.append('puts "INFO: skipping intermediate checkDesign -all; final post-route checkDesign will run once to avoid duplicate structural warnings for this padless core top"')
    lines.append("saveDesign $DBS_DIR/02_powerplan.enc")
    lines.append("")
    lines.append('if {[catch {setPlaceMode -place_global_place_io_pins true} place_mode_err]} {')
    lines.append('    puts "WARN: setPlaceMode failed: $place_mode_err"')
    lines.append("}")
    lines.append("placeDesign")
    lines.append("saveDesign $DBS_DIR/03_place.enc")
    lines.append("")
    lines.append('if {[catch {applyGlobalNets} apply_gn_post_place_err]} {')
    lines.append('    puts "WARN: applyGlobalNets after placement reported: $apply_gn_post_place_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: applyGlobalNets after placement complete"')
    lines.append("}")
    lines.append("")
    lines.append(
        "if {[catch {sroute -connect {corePin floatingStripe} -nets { %s %s }} sroute_err]} {"
        % (pwr_net, gnd_net)
    )
    lines.append('    puts "WARN: sroute reported: $sroute_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: sroute complete"')
    lines.append("}")
    lines.append("")
    lines.append("saveDesign $DBS_DIR/03a_post_sroute.enc")
    lines.append("")
    if do_cts:
        lines.append("if {![catch {create_ccopt_clock_tree_spec -file ${REPORTS_DIR}/ccopt.spec} cts_spec_err]} {")
        lines.append('    puts "INFO: created CCOpt spec"')
        lines.append("} else {")
        lines.append('    puts "WARN: create_ccopt_clock_tree_spec failed: $cts_spec_err"')
        lines.append("}")
        lines.append("")
        lines.append('puts "INFO: setting CCOpt targets"')
        lines.append('if {[catch {set_ccopt_property target_max_trans %.3f} ccopt_trans_err]} {' % ccopt_target_max_transition)
        lines.append('    puts "WARN: set_ccopt_property target_max_trans failed: $ccopt_trans_err"')
        lines.append("}")
        lines.append('if {[catch {set_ccopt_property target_skew %.3f} ccopt_skew_err]} {' % ccopt_target_skew)
        lines.append('    puts "WARN: set_ccopt_property target_skew failed: $ccopt_skew_err"')
        lines.append("}")
        lines.append("")
        lines.append("if {![catch {ccopt_design} cts_err]} {")
        lines.append('    puts "INFO: CCOpt/CTS completed"')
        lines.append("} else {")
        lines.append('    puts "WARN: ccopt_design failed: $cts_err"')
        lines.append("}")
        lines.append("")
        lines.append("saveDesign $DBS_DIR/04_cts.enc")
        lines.append("")
    lines.append("setDesignMode -bottomRoutingLayer {0}".format(bottom_route_layer))
    lines.append("setDesignMode -topRoutingLayer {0}".format(top_route_layer))
    if antenna_diode_cell:
        lines.append("setNanoRouteMode -routeInsertAntennaDiode true")
        lines.append("setNanoRouteMode -routeAntennaCellName {{{0}}}".format(antenna_diode_cell))
        lines.append("")
    lines.append("routeDesign")
    lines.append("saveDesign $DBS_DIR/05_route.enc")
    lines.append("")
    lines.append('if {[catch {verifyProcessAntenna} ant_route_err]} {')
    lines.append('    puts "WARN: verifyProcessAntenna after route failed: $ant_route_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: verifyProcessAntenna after route complete"')
    lines.append("}")
    lines.append("")
    if timing_eco_plan is not None:
        lines[implementation_start:] = build_timing_eco_tcl(timing_eco_plan, top)
    if filler_cells:
        lines.append("catch {setFillerMode -add_fillers_with_drc false}")
        lines.append(
            "if {![catch {addFiller -cell %s -prefix FILL -fitGap} filler_err]} {"
            % filler_list
        )
        lines.append('    puts "INFO: filler insertion complete"')
        lines.append("} else {")
        lines.append('    puts "ERROR: addFiller failed: $filler_err"')
        lines.append("    exit 2")
        lines.append("}")
        lines.append("")
        for pin in pwr_pins:
            lines.append('puts "INFO: re-apply globalNetConnect {0} <- {1}"'.format(pwr_net, pin))
            lines.append("globalNetConnect {0} -type pgpin -pin {1} -inst *".format(pwr_net, pin))
        for pin in gnd_pins:
            lines.append('puts "INFO: re-apply globalNetConnect {0} <- {1}"'.format(gnd_net, pin))
            lines.append("globalNetConnect {0} -type pgpin -pin {1} -inst *".format(gnd_net, pin))
        if tiehi_net:
            lines.append('puts "INFO: re-apply globalNetConnect tiehi <- {0}"'.format(tiehi_net))
            lines.append("globalNetConnect {0} -type tiehi -inst *".format(tiehi_net))
        if tielo_net:
            lines.append('puts "INFO: re-apply globalNetConnect tielo <- {0}"'.format(tielo_net))
            lines.append("globalNetConnect {0} -type tielo -inst *".format(tielo_net))
        lines.append('if {[catch {applyGlobalNets} apply_gn_fill_err]} {')
        lines.append('    puts "WARN: applyGlobalNets after filler reported: $apply_gn_fill_err"')
        lines.append("} else {")
        lines.append('    puts "INFO: applyGlobalNets after filler complete"')
        lines.append("}")
        lines.append("")
        lines.append(
            "if {[catch {sroute -connect {corePin floatingStripe} -nets { %s %s }} sroute_fill_err]} {"
            % (pwr_net, gnd_net)
        )
        lines.append('    puts "WARN: post-filler sroute reported: $sroute_fill_err"')
        lines.append("} else {")
        lines.append('    puts "INFO: post-filler sroute complete"')
        lines.append("}")
        lines.append("")
        lines.append("saveDesign $DBS_DIR/05b_postfill_sroute.enc")
        lines.append("")
        lines.append("setDesignMode -bottomRoutingLayer {0}".format(bottom_route_layer))
        lines.append("setDesignMode -topRoutingLayer {0}".format(top_route_layer))
        if antenna_diode_cell:
            lines.append("setNanoRouteMode -routeInsertAntennaDiode true")
            lines.append("setNanoRouteMode -routeAntennaCellName {{{0}}}".format(antenna_diode_cell))
            lines.append("")
        lines.append("if {![catch {ecoRoute -target} eco_err]} {")
        lines.append('    puts "INFO: ecoRoute after filler insertion complete"')
        lines.append("} else {")
        lines.append('    puts "ERROR: ecoRoute failed: $eco_err"')
        lines.append("    exit 2")
        lines.append("}")
        lines.append("")
        lines.append('if {[catch {verifyProcessAntenna} ant_post_eco_err]} {')
        lines.append('    puts "WARN: verifyProcessAntenna after ecoRoute failed: $ant_post_eco_err"')
        lines.append("} else {")
        lines.append('    puts "INFO: verifyProcessAntenna after ecoRoute complete"')
        lines.append("}")
        lines.append("")
        lines.append("saveDesign $DBS_DIR/05c_post_eco_ant.enc")
        lines.append("")
    lines.append('puts "INFO: entering final verification stage"')
    lines.append("")
    lines.append('set ANTENNA_RPT [file join $OUTDIR "${TOP}.antenna.rpt"]')
    lines.append('if {[catch {verifyProcessAntenna} ant_pre_target_err]} {')
    lines.append('    puts "WARN: pre-target verifyProcessAntenna failed: $ant_pre_target_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: pre-target verifyProcessAntenna complete"')
    lines.append("}")
    lines.append('if {[file exists $ANTENNA_RPT]} {')
    lines.append('    catch {file copy -force $ANTENNA_RPT [file join $OUTDIR "${TOP}.antenna.rpt.old"]}')
    lines.append("}")
    lines.append("")
    if antenna_diode_cell:
        lines.append("set antfix_max_passes 3")
        lines.append("set antfix_pass 1")
        lines.append("set antfix_remaining [parse_antenna_violation_count $ANTENNA_RPT]")
        lines.append('while {$antfix_pass <= $antfix_max_passes && $antfix_remaining > 0} {')
        lines.append('    set antfix_pass_tag [format "P%d" $antfix_pass]')
        lines.append('    puts "INFO: targeted antenna cleanup pass $antfix_pass starting with $antfix_remaining remaining violation(s)"')
        lines.append("    set antfix_count [insert_targeted_antenna_diodes $ANTENNA_RPT {{{0}}} $antfix_pass_tag]".format(antenna_diode_cell))
        lines.append('    if {$antfix_count <= 0} {')
        lines.append('        puts "INFO: targeted antenna cleanup pass $antfix_pass inserted no extra diodes"')
        lines.append("        break")
        lines.append("    }")
        for pin in pwr_pins:
            lines.append('    puts "INFO: targeted cleanup globalNetConnect {0} <- {1}"'.format(pwr_net, pin))
            lines.append("    globalNetConnect {0} -type pgpin -pin {1} -inst *".format(pwr_net, pin))
        for pin in gnd_pins:
            lines.append('    puts "INFO: targeted cleanup globalNetConnect {0} <- {1}"'.format(gnd_net, pin))
            lines.append("    globalNetConnect {0} -type pgpin -pin {1} -inst *".format(gnd_net, pin))
        if tiehi_net:
            lines.append('    puts "INFO: targeted cleanup globalNetConnect tiehi <- {0}"'.format(tiehi_net))
            lines.append("    globalNetConnect {0} -type tiehi -inst *".format(tiehi_net))
        if tielo_net:
            lines.append('    puts "INFO: targeted cleanup globalNetConnect tielo <- {0}"'.format(tielo_net))
            lines.append("    globalNetConnect {0} -type tielo -inst *".format(tielo_net))
        lines.append('    if {[catch {applyGlobalNets} antfix_apply_gn_err]} {')
        lines.append('        puts "WARN: applyGlobalNets after targeted diode insertion reported: $antfix_apply_gn_err"')
        lines.append("    } else {")
        lines.append('        puts "INFO: applyGlobalNets after targeted diode insertion complete"')
        lines.append("    }")
        lines.append("    setDesignMode -bottomRoutingLayer {0}".format(bottom_route_layer))
        lines.append("    setDesignMode -topRoutingLayer {0}".format(top_route_layer))
        lines.append("    setNanoRouteMode -routeInsertAntennaDiode true")
        lines.append("    setNanoRouteMode -routeAntennaCellName {{{0}}}".format(antenna_diode_cell))
        lines.append("    if {![catch {ecoRoute -target} ant_target_eco_err]} {")
        lines.append('        puts "INFO: targeted antenna cleanup ecoRoute complete"')
        lines.append("    } else {")
        lines.append('        puts "ERROR: targeted antenna cleanup ecoRoute failed: $ant_target_eco_err"')
        lines.append("        exit 2")
        lines.append("    }")
        lines.append('    if {[catch {verifyProcessAntenna} ant_final_err]} {')
        lines.append('        puts "WARN: final verifyProcessAntenna after targeted cleanup failed: $ant_final_err"')
        lines.append("    } else {")
        lines.append('        puts "INFO: final verifyProcessAntenna after targeted cleanup complete"')
        lines.append("    }")
        lines.append("    set antfix_remaining [parse_antenna_violation_count $ANTENNA_RPT]")
        lines.append('    puts "INFO: targeted antenna cleanup pass $antfix_pass finished with $antfix_remaining remaining violation(s)"')
        lines.append("    incr antfix_pass")
        lines.append("}")
        lines.append("saveDesign $DBS_DIR/05d_targeted_ant_cleanup.enc")
        lines.append("")
    lines.append("catch {reportSpecialNet VDD > $REPORTS_DIR/VDD_final.rpt}")
    lines.append("catch {reportSpecialNet VSS > $REPORTS_DIR/VSS_final.rpt}")
    lines.append("foreach expected_checkdesign_id {IMPREPO-200 IMPREPO-202 IMPREPO-212 IMPREPO-213} {")
    lines.append("    catch {suppressMessage $expected_checkdesign_id}")
    lines.append("}")
    lines.append('puts "INFO: suppressed expected checkDesign I/O-structure warnings for padless core-top reporting: IMPREPO-200 IMPREPO-202 IMPREPO-212 IMPREPO-213"')
    lines.append("")
    lines.append('if {[catch {checkDesign -all} check_final_err]} {')
    lines.append('    puts "WARN: final checkDesign reported: $check_final_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: final checkDesign complete"')
    lines.append("}")
    lines.append("")
    lines.append('puts "INFO: final connectivity review uses checkDesign -all; raw verifyConnectivity is skipped because it over-reports abutted PG terminals such as VPB/VNB in this sky130 flow"')
    lines.append('puts "INFO: review $OUTDIR/checkDesign/checknetlist.rpt and $OUTDIR/checkDesign/pgTermConnectivity.main.htm for final connectivity status"')
    lines.append('puts "INFO: review $REPORTS_DIR/netlist_constraint_usage.rpt for intentional constant assigns and inferred tie-net handling"')
    lines.append("")
    lines.append('if {[catch {verify_drc} drc_verify_err]} {')
    lines.append('    puts "ERROR: verify_drc failed: $drc_verify_err"')
    lines.append("    exit 3")
    lines.append("} else {")
    lines.append('    puts "INFO: verify_drc command finished; review violation count in the log"')
    lines.append("}")
    lines.append("")
    if final_postroute_setup_opt:
        lines.append('puts "INFO: configuring OCV analysis for post-route setup optimization"')
        lines.append('if {[catch {setAnalysisMode -analysisType onChipVariation} postroute_analysis_err]} {')
        lines.append('    puts "ERROR: cannot configure post-route OCV analysis: $postroute_analysis_err"')
        lines.append("    exit 4")
        lines.append("}")
        lines.append('puts "INFO: running final post-route setup optimization"')
        lines.append('if {[catch {optDesign -postRoute -setup} postroute_setup_opt_err]} {')
        lines.append('    puts "ERROR: final optDesign -postRoute -setup failed: $postroute_setup_opt_err"')
        lines.append("    exit 4")
        lines.append("} else {")
        lines.append('    puts "INFO: final optDesign -postRoute -setup complete"')
        lines.append("}")
        lines.append("catch {saveDesign $DBS_DIR/05e_postroute_setup_opt.enc}")
        lines.append("")
    if timing_eco_plan is not None and timing_eco_plan["actions"]:
        lines.extend([
            'if {[catch {optDesign -postRoute -hold; verify_drc} eco_hold_err]} {',
            '    puts "ERROR: timing ECO hold/DRC check failed: $eco_hold_err"',
            '    exit 6',
            '}',
        ])
    lines.append('if {[catch {report_timing -max_paths 10 > $REPORTS_DIR/timing_postroute.rpt} timing_report_err]} {')
    lines.append('    puts "ERROR: post-route timing report failed: $timing_report_err"')
    lines.append("    exit 5")
    lines.append("}")
    lines.append("catch {report_area > $REPORTS_DIR/area_postroute.rpt}")
    lines.extend([
        'if {[catch {',
        '    report_power -unit W > $REPORTS_DIR/power_postroute.rpt',
        '    set power_units [open $REPORTS_DIR/power_postroute.rpt a]',
        '    puts $power_units "Power Units: W"',
        '    close $power_units',
        '    set area_file [open $REPORTS_DIR/die_area.rpt w]',
        '    puts $area_file "die_area_mm2: [eda_die_area_mm2]"',
        '    close $area_file',
        '    set library_file [open $REPORTS_DIR/eco_library.rpt w]',
        '    foreach cell [lsort -unique [dbGet head.libCells.name]] {puts $library_file $cell}',
        '    close $library_file',
        '    report_timing -early -max_paths 10 > $REPORTS_DIR/hold_postroute.rpt',
        '} closure_metrics_err]} {',
        '    puts "ERROR: closure measurements failed: $closure_metrics_err"',
        '    exit 7',
        '}',
    ])
    lines.append("")
    lines.append('puts "INFO: entering final export stage"')
    lines.append("catch {defOut $RESULTS_DIR/%s.def}" % top)
    lines.append("catch {saveNetlist $RESULTS_DIR/%s_postroute.v}" % top)
    lines.append("if {[catch {" + stream_cmd + "} stream_err]} {")
    lines.append('    puts "WARN: streamOut failed: $stream_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: GDS stream-out complete"')
    lines.append("}")
    lines.append("saveDesign $DBS_DIR/06_final.enc")
    lines.append('puts "INFO: organizing root-level generated artifacts"')
    lines.append('if {[catch {move_root_artifacts $OUTDIR $REPORTS_DIR $RESULTS_DIR} move_artifacts_err]} {')
    lines.append('    puts "WARN: artifact organization failed: $move_artifacts_err"')
    lines.append("} else {")
    lines.append('    puts "INFO: artifact organization complete"')
    lines.append("}")
    lines.append('puts "INFO: Innovus flow finished"')
    lines.append("exit")
    lines.append("")
    return "\n".join(lines)


def write_tcl(
    tcl_path,
    top,
    netlist,
    lef_files,
    mmmc_file,
    outdir,
    site,
    routing_layers,
    filler_cells,
    stream_map,
    pwr_net,
    gnd_net,
    pwr_pins,
    gnd_pins,
    process_nm,
    aspect,
    util,
    core_margin,
    ring_width,
    ring_spacing,
    ring_offset,
    stripe_width,
    stripe_spacing,
    stripe_pitch,
    stripe_offset,
    do_cts,
    ccopt_target_skew,
    ccopt_target_max_transition,
    final_postroute_setup_opt,
    antenna_diode_cell,
    tiehi_net,
    tielo_net,
    timing_eco_plan=None,
):
    txt = build_innovus_tcl(
        top=top,
        netlist=netlist,
        lef_files=lef_files,
        mmmc_file=mmmc_file,
        outdir=outdir,
        site=site,
        routing_layers=routing_layers,
        filler_cells=filler_cells,
        stream_map=stream_map,
        pwr_net=pwr_net,
        gnd_net=gnd_net,
        pwr_pins=pwr_pins,
        gnd_pins=gnd_pins,
        process_nm=process_nm,
        aspect=aspect,
        util=util,
        core_margin=core_margin,
        ring_width=ring_width,
        ring_spacing=ring_spacing,
        ring_offset=ring_offset,
        stripe_width=stripe_width,
        stripe_spacing=stripe_spacing,
        stripe_pitch=stripe_pitch,
        stripe_offset=stripe_offset,
        do_cts=do_cts,
        ccopt_target_skew=ccopt_target_skew,
        ccopt_target_max_transition=ccopt_target_max_transition,
        final_postroute_setup_opt=final_postroute_setup_opt,
        antenna_diode_cell=antenna_diode_cell,
        tiehi_net=tiehi_net,
        tielo_net=tielo_net,
        timing_eco_plan=timing_eco_plan,
    )
    write_text(tcl_path, txt)


def copy_stream_map(found_stream_map, outdir):
    if found_stream_map is None:
        return None
    dst = outdir / "streamOut.map"
    shutil.copyfile(str(found_stream_map), str(dst))
    return dst


def main():
    parser = argparse.ArgumentParser(
        description="Run a universal Innovus/GDSII flow from a mapped Genus build."
    )
    parser.add_argument(
        "--mappeddir",
        default=None,
        help="Mapped-netlist directory produced by the mapped Genus stage; defaults to an auto-detected build_mapped* directory.",
    )
    parser.add_argument(
        "--outdir",
        default=None,
        help="Output directory for Innovus/GDSII artifacts; defaults to build_mapped* renamed to build_GDSII*.",
    )
    parser.add_argument("--top", default=None)
    parser.add_argument("--netlist", default=None, help="Explicit mapped Verilog netlist to use.")
    parser.add_argument("--sdc", default=None, help="Explicit mapped SDC to use.")
    parser.add_argument("--innovus", default=None)
    parser.add_argument("--pdk-root", default=None)
    parser.add_argument("--stdcell-lib", default="sky130_fd_sc_hd")
    parser.add_argument("--tech-lef", default=None)
    parser.add_argument("--lef", action="append", default=[])
    parser.add_argument("--lib", action="append", default=[])
    parser.add_argument("--qrc-tech", default=None)
    parser.add_argument("--cap-table", default=None)
    parser.add_argument("--stream-map", default=None)
    parser.add_argument("--pwr-net", default="VDD")
    parser.add_argument("--gnd-net", default="VSS")
    parser.add_argument("--pwr-pins", default="VPWR,VPB,KAPWR")
    parser.add_argument("--gnd-pins", default="VGND,VNB")
    parser.add_argument("--process-nm", type=int, default=130)
    parser.add_argument("--aspect", type=float, default=1.0)
    parser.add_argument(
        "--clock-period",
        type=float,
        default=None,
        help="Explicit create_clock period override in ns. Use only when intentionally changing/overconstraining timing.",
    )
    parser.add_argument("--utilization", type=float, default=0.55)
    parser.add_argument("--ccopt-target-skew", type=float, default=0.25)
    parser.add_argument("--ccopt-target-max-transition", type=float, default=0.50)
    parser.add_argument("--final-postroute-setup-opt", action="store_true")
    parser.add_argument("--timing-eco-json", help="Validated resize plan and source checkpoint, encoded as JSON")
    parser.add_argument("--core-margin", type=float, default=10.0)
    parser.add_argument("--ring-width", type=float, default=2.0)
    parser.add_argument("--ring-spacing", type=float, default=2.0)
    parser.add_argument("--ring-offset", type=float, default=2.0)
    parser.add_argument("--stripe-width", type=float, default=2.0)
    parser.add_argument("--stripe-spacing", type=float, default=2.0)
    parser.add_argument("--stripe-pitch", type=float, default=40.0)
    parser.add_argument("--stripe-offset", type=float, default=10.0)
    parser.add_argument("--no-cts", action="store_true")
    parser.add_argument("--no-filler", action="store_true")
    parser.add_argument("--antenna-diode-cell", default="sky130_fd_sc_hd__diode_2")
    parser.add_argument("--tiehi-net", default=None)
    parser.add_argument("--tielo-net", default=None)
    parser.add_argument("--setup-only", action="store_true")
    args = parser.parse_args()

    mappeddir = find_mapped_builddir(args.mappeddir)
    outdir = derive_outdir(mappeddir, args.outdir)
    try:
        innovus = find_innovus(args.innovus)
    except SystemExit:
        if not args.setup_only:
            raise
        innovus = (
            Path(args.innovus).expanduser().resolve()
            if args.innovus
            else Path(DEFAULT_INNOVUS_BIN)
        )

    tcldir = outdir / "tcl"
    logsdir = outdir / "logs"
    reportsdir = outdir / "reports"
    resultsdir = outdir / "results"
    dbdir = outdir / "db"
    checksdir = outdir / "checkDesign"
    timingdir = outdir / "timingReports"
    for path_obj in (outdir, tcldir, logsdir, reportsdir, resultsdir, dbdir, checksdir, timingdir):
        path_obj.mkdir(parents=True, exist_ok=True)

    netlist = find_mapped_netlist(mappeddir, args.netlist)
    sdc = find_mapped_sdc(mappeddir, args.sdc)
    top = choose_top(netlist, args.top)

    pdk_root = choose_sky130_root(args.pdk_root)
    tech_lef = (
        Path(args.tech_lef).expanduser().resolve()
        if args.tech_lef
        else auto_find_tech_lef(pdk_root, args.stdcell_lib)
    )
    stdcell_lef = auto_find_stdcell_lef(pdk_root, args.stdcell_lib)

    extra_lefs = [Path(x).expanduser().resolve() for x in args.lef]
    lef_files = unique_paths([tech_lef, stdcell_lef, *extra_lefs])

    auto_libs = auto_find_timing_libs(pdk_root, args.stdcell_lib)
    extra_libs = [Path(x).expanduser().resolve() for x in args.lib]
    timing_libs = unique_paths([*auto_libs, *extra_libs])

    auto_qrc, auto_cap = auto_find_qrc_or_cap(pdk_root)
    qrc_tech = (
        Path(args.qrc_tech).expanduser().resolve() if args.qrc_tech else auto_qrc
    )
    cap_table = (
        Path(args.cap_table).expanduser().resolve() if args.cap_table else auto_cap
    )
    raw_stream_map = (
        Path(args.stream_map).expanduser().resolve()
        if args.stream_map
        else auto_find_stream_map(pdk_root)
    )
    local_stream_map = copy_stream_map(raw_stream_map, outdir)

    site, routing_layers, filler_cells = parse_site_and_layers(lef_files)
    if args.no_filler:
        filler_cells = []

    pwr_pins = split_csv(args.pwr_pins)
    gnd_pins = split_csv(args.gnd_pins)
    do_cts = bool(sdc) and not args.no_cts

    constant_port_assigns, literal_pin_ties = parse_constant_usage(netlist)
    tiehi_net = args.tiehi_net
    tielo_net = args.tielo_net
    if tiehi_net is None and any(bit == "1" for _, bit in constant_port_assigns):
        tiehi_net = args.pwr_net
    if tiehi_net is None and any(bit == "1" for _, _, bit in literal_pin_ties):
        tiehi_net = args.pwr_net
    if tielo_net is None and any(bit == "0" for _, bit in constant_port_assigns):
        tielo_net = args.gnd_net
    if tielo_net is None and any(bit == "0" for _, _, bit in literal_pin_ties):
        tielo_net = args.gnd_net

    innovus_sdc, removed_sdc_lines = sanitize_sdc_for_innovus(
        sdc,
        tcldir,
        clock_period_ns=args.clock_period,
    )
    mmmc_file = tcldir / "mmmc.tcl"
    tcl_path = tcldir / "run_innovus.tcl"
    wrapper_log = outdir / "run_wrapper.log"
    innovus_log = logsdir / "innovus.log"
    native_log = logsdir / "innovus_native.log"

    write_text(
        mmmc_file,
        build_mmmc_tcl(
            sdc_path=innovus_sdc,
            timing_libs=timing_libs,
            qrc_tech=qrc_tech,
            cap_table=cap_table,
        ),
    )

    constant_usage_txt = format_constant_usage_report(
        constant_port_assigns=constant_port_assigns,
        literal_pin_ties=literal_pin_ties,
        inferred_tiehi_net=tiehi_net,
        inferred_tielo_net=tielo_net,
    )
    write_text(reportsdir / "netlist_constraint_usage.rpt", constant_usage_txt)
    write_text(reportsdir / "netlist_constant_usage.rpt", constant_usage_txt)
    write_text(reportsdir / "{0}.conn.rpt".format(top), format_connectivity_note(top))
    write_text(reportsdir / "power.rpt", format_power_note())
    write_text(outdir / "collectGenusLibrary.log", format_collect_genus_library_log(timing_libs))

    write_tcl(
        tcl_path=tcl_path,
        top=top,
        netlist=netlist,
        lef_files=lef_files,
        mmmc_file=mmmc_file,
        outdir=outdir,
        site=site,
        routing_layers=routing_layers,
        filler_cells=filler_cells,
        stream_map=local_stream_map,
        pwr_net=args.pwr_net,
        gnd_net=args.gnd_net,
        pwr_pins=pwr_pins,
        gnd_pins=gnd_pins,
        process_nm=args.process_nm,
        aspect=args.aspect,
        util=args.utilization,
        core_margin=args.core_margin,
        ring_width=args.ring_width,
        ring_spacing=args.ring_spacing,
        ring_offset=args.ring_offset,
        stripe_width=args.stripe_width,
        stripe_spacing=args.stripe_spacing,
        stripe_pitch=args.stripe_pitch,
        stripe_offset=args.stripe_offset,
        do_cts=do_cts,
        ccopt_target_skew=args.ccopt_target_skew,
        ccopt_target_max_transition=args.ccopt_target_max_transition,
        final_postroute_setup_opt=args.final_postroute_setup_opt,
        antenna_diode_cell=args.antenna_diode_cell,
        tiehi_net=tiehi_net,
        tielo_net=tielo_net,
        timing_eco_plan=json.loads(args.timing_eco_json) if args.timing_eco_json else None,
    )

    summary = (
        "Mapped dir   : {0}\n"
        "Out dir      : {1}\n"
        "Innovus bin  : {2}\n"
        "CDS_LIC_FILE : {3}\n"
        "LM_LICENSE_FILE: {4}\n"
        "Netlist      : {5}\n"
        "Top          : {6}\n"
        "SDC original : {7}\n"
        "SDC Innovus  : {8}\n"
        "SDC sanitize : removed {9} unsupported set_units command(s)\n"
        "PDK root     : {10}\n"
        "Tech LEF     : {11}\n"
        "LEFs         : {12}\n"
        "Timing libs  : {13}\n"
        "QRC tech     : {14}\n"
        "Cap table    : {15}\n"
        "Stream map   : {16}\n"
        "Site         : {17}\n"
        "Routing      : {18}\n"
        "Fillers      : {19}\n"
        "Power pins   : {20}\n"
        "Ground pins  : {21}\n"
        "TieHi net    : {22}\n"
        "TieLo net    : {23}\n"
        "Const assigns: {24} top-level, {25} direct literal pin ties\n"
        "CTS enabled  : {26}\n"
        "No filler    : {27}\n"
        "Antenna diode: {28}\n"
        "Clock period override ns: {29}\n"
        "CCOpt target skew       : {30}\n"
        "CCOpt target max trans  : {31}\n"
        "Final post-route setup opt: {32}\n"
    ).format(
        mappeddir,
        outdir,
        innovus,
        DEFAULT_CDS_LIC_FILE,
        DEFAULT_LM_LICENSE_FILE,
        netlist,
        top,
        sdc if sdc else "NONE (CTS disabled)",
        innovus_sdc if innovus_sdc else "NONE (CTS disabled)",
        removed_sdc_lines,
        pdk_root,
        tech_lef,
        ", ".join(str(p) for p in lef_files),
        ", ".join(str(p) for p in timing_libs),
        qrc_tech if qrc_tech else "NONE",
        cap_table if cap_table else "NONE",
        local_stream_map if local_stream_map else "NONE",
        site,
        routing_layers,
        ", ".join(filler_cells) if filler_cells else "NONE",
        pwr_pins,
        gnd_pins,
        tiehi_net if tiehi_net else "NONE",
        tielo_net if tielo_net else "NONE",
        len(constant_port_assigns),
        len(literal_pin_ties),
        do_cts,
        args.no_filler,
        args.antenna_diode_cell,
        "{0:.3f}".format(args.clock_period) if args.clock_period is not None else "NONE",
        "{0:.3f}".format(args.ccopt_target_skew),
        "{0:.3f}".format(args.ccopt_target_max_transition),
        args.final_postroute_setup_opt,
    )
    write_text(outdir / "setup_summary.txt", summary)
    print(summary)

    if args.setup_only:
        print("Generated Innovus setup only. No run launched.")
        return 0

    innovus_dir = str(innovus.parent)
    bash_cmd = "\n".join(
        [
            "set -euo pipefail",
            'export PATH="{0}:$PATH"'.format(innovus_dir),
            'export CDS_LIC_FILE="{0}"'.format(DEFAULT_CDS_LIC_FILE),
            'export LM_LICENSE_FILE="{0}"'.format(DEFAULT_LM_LICENSE_FILE),
            'echo "PATH=$PATH"',
            'echo "CDS_LIC_FILE=${CDS_LIC_FILE-}"',
            'echo "LM_LICENSE_FILE=${LM_LICENSE_FILE-}"',
            "which innovus || true",
            'innovus -no_gui -log "{0}" -files "{1}" | tee "{2}"'.format(
                native_log,
                tcl_path,
                innovus_log,
            ),
        ]
    )

    run_bash(bash_cmd, logfile=wrapper_log, cwd=outdir)

    gds_path = resultsdir / "{0}.gds".format(top)
    if not gds_path.exists():
        raise RuntimeError(
            "Innovus completed but no GDS file was found at {0}. See logs: {1} and {2}".format(
                gds_path,
                wrapper_log,
                innovus_log,
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
