from __future__ import annotations

import json
import shlex
import re
from pathlib import Path

from schemas.state import FlowState
from services.path_naming import (
    resolve_gdsii_dir_choice,
    resolve_mapped_dir_choice,
    resolve_netlist_dir_choice,
    resolve_prepare_dir,
)
from services.synthesis_recipe_library import build_synthesis_recipe_library_payload

DEFAULT_INNER_HOST = "n01-zeus"
DEFAULT_PROJECT_ROOT = "/home/ugrads/a/antgamez1203/capstone"
DEFAULT_REMOTE_INNOVUS_BIN = "/opt/coe/cadence/DDI231/bin/innovus"
DEFAULT_PREPARE_SCRIPT = "prepare_rtl_for_genus_universal.py"
DEFAULT_NETLIST_SCRIPT = "run_genus_netlist_universal.py"
DEFAULT_MAPPED_SCRIPT = "run_genus_mapped_universal.py"
DEFAULT_GDSII_SCRIPT = "run_innovus_GDSII_universal.py"


def _build_inner_ssh_command(state: FlowState, inner_cmd: str) -> str:
    wrapped = (
        "source ~/.bashrc >/dev/null 2>&1 || true; "
        "type load-ecen-454 >/dev/null 2>&1 && load-ecen-454; "
        f"{inner_cmd}"
    )

    return (
        "srun --job-name=ecen-454 "
        "--cpus-per-task=1 "
        "--partition=adademic "
        "--qos=olympus-academic "
        f"/bin/bash -lc {shlex.quote(wrapped)}"
    )


def _load_rules_text(
    rules_path: str | None,
    *,
    label: str,
) -> tuple[str | None, list[str]]:
    if not rules_path:
        return None, [f"{label} rules path is not configured."]

    try:
        rules_text = Path(rules_path).read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"Unable to read {label} rules file '{rules_path}': {exc}"]

    return rules_text, []


def _load_universal_rules(state: FlowState) -> tuple[str | None, list[str]]:
    return _load_rules_text(
        state.get("universal_rules_path"),
        label="Universal prep",
    )


def _remote_python(state: FlowState) -> str:
    return shlex.quote(str(state.get("remote_python_cmd", "python3.11")).strip() or "python3.11")


def _positive_float_state_arg(state: FlowState, key: str, flag: str) -> str:
    value = state.get(key)
    if value in (None, ""):
        return ""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return ""
    if parsed <= 0:
        return ""
    return f" {flag} {shlex.quote(f'{parsed:.3f}')}"


def _build_gdsii_timing_closure_args(state: FlowState) -> str:
    args = ""
    use_overconstraint = bool(state.get("timing_use_overconstraint"))
    overconstrained_period = state.get("timing_overconstraint_clock_period_ns")
    if use_overconstraint and overconstrained_period not in (None, ""):
        args += _positive_float_state_arg(
            state,
            "timing_overconstraint_clock_period_ns",
            "--clock-period",
        )
    else:
        args += _positive_float_state_arg(
            state,
            "timing_target_clock_period_ns",
            "--clock-period",
        )
    args += _positive_float_state_arg(state, "innovus_utilization", "--utilization")
    args += _positive_float_state_arg(state, "innovus_ccopt_target_skew", "--ccopt-target-skew")
    args += _positive_float_state_arg(
        state,
        "innovus_ccopt_target_max_transition",
        "--ccopt-target-max-transition",
    )
    if bool(state.get("innovus_final_postroute_setup_opt")):
        args += " --final-postroute-setup-opt"
    if state.get("timing_closure_eco_plan") is not None:
        args += " --timing-eco-json " + shlex.quote(json.dumps(state["timing_closure_eco_plan"]))
    return args


def _build_prepare_command(state: FlowState) -> str:
    outdir = state.get("remote_prepare_dir", "build_prep_mc_lang")
    input_name = state.get("remote_prepare_input", "MemoryController.zip")
    prepare_script = state.get("remote_prepare_script", DEFAULT_PREPARE_SCRIPT)
    remote_python = _remote_python(state)
    review_errors = state.get("prep_review_errors", [])
    review_feedback = "\n".join(review_errors)
    rules_path = state.get("universal_rules_path") or ""
    prep_iteration = str(state.get("prep_iteration", 0))

    exports = [
        f"export EDA_RTL_PREP_ITERATION={shlex.quote(prep_iteration)}",
        f"export EDA_RTL_PREP_RULES_PATH={shlex.quote(rules_path)}",
        f"export EDA_RTL_PREP_REVIEW_ERRORS={shlex.quote(review_feedback)}",
        (
            "export EDA_RTL_PREP_REVIEW_ERRORS_JSON="
            f"{shlex.quote(json.dumps(review_errors))}"
        ),
    ]

    return (
        f"{'; '.join(exports)}; "
        f"rm -rf {shlex.quote(outdir)} && "
        f"{remote_python} {shlex.quote(prepare_script)} "
        f"--input {shlex.quote(input_name)} "
        f"--outdir {shlex.quote(outdir)}"
    )


def _build_prepare_review_command(state: FlowState, candidate_path: str) -> str:
    rules_text, preflight_errors = _load_universal_rules(state)
    rules_path = state.get("universal_rules_path") or ""
    remote_python = _remote_python(state)
    recipe_library_payload = build_synthesis_recipe_library_payload()

    review_script = f"""
import json
import pathlib
import re

candidate = pathlib.Path({candidate_path!r})
rules_path = {rules_path!r}
rules_text = {rules_text!r}
preflight_errors = {preflight_errors!r}
recipe_library = json.loads({json.dumps(recipe_library_payload)!r})
MAX_CONTEXT_FILES = 400
MAX_CONTEXT_CHARS = 6000
MAX_RTL_SAMPLES = 12
MAX_RTL_SAMPLE_CHARS = 2500
MAX_DEPENDENCY_EXAMPLES = 16

result = {{
    "pass": False,
    "errors": list(preflight_errors),
    "warnings": [],
    "suggested_fixes": [],
    "summary": "",
    "rules_path": rules_path,
    "candidate_path": str(candidate),
    "prep_dependency_errors": [],
    "context": {{
        "inventory": {{}},
        "artifact_status": {{}},
        "key_files": {{}},
        "rtl_samples": [],
        "dependency_analysis": {{}},
    }},
}}

errors = result["errors"]
warnings = result["warnings"]
suggested_fixes = result["suggested_fixes"]
context = result["context"]
dependency_errors = result["prep_dependency_errors"]
required_files = [
    "filelist_genus.f",
    "run_genus_mc.tcl",
    "README_prepared.txt",
    "top_module.txt",
    "constraints/mc_genus.sdc",
]
rtl_suffixes = {{".sv", ".v", ".svh", ".vh"}}
tb_name_re = re.compile(r"(^|[_\\-.])(tb|testbench|unit[_\\-.]?test|svunit)([_\\-.]|$)", re.IGNORECASE)
prohibited_patterns = [
    (re.compile(r"(?m)^\\s*sequence\\b"), "contains sequence blocks"),
    (re.compile(r"(?m)^\\s*endsequence\\b"), "contains sequence blocks"),
    (re.compile(r"(?m)^\\s*property\\b"), "contains property blocks"),
    (re.compile(r"(?m)^\\s*endproperty\\b"), "contains property blocks"),
    (re.compile(r"\\bassert\\s+property\\b"), "contains assert property"),
    (re.compile(r"\\bcover\\s+property\\b"), "contains cover property"),
    (re.compile(r"\\bassume\\s+property\\b"), "contains assume property"),
    (re.compile(r"\\$display\\s*\\("), "contains $display"),
    (re.compile(r"\\$strobe\\s*\\("), "contains $strobe"),
    (re.compile(r"\\$monitor\\s*\\("), "contains $monitor"),
]
ignored_instance_heads = {{
    "if", "else", "for", "foreach", "while", "case", "casex", "casez", "randcase",
    "assign", "always", "always_comb", "always_ff", "always_latch", "initial",
    "begin", "end", "generate", "endgenerate", "module", "endmodule", "package",
    "endpackage", "interface", "endinterface", "program", "endprogram", "class",
    "endclass", "function", "endfunction", "task", "endtask", "property",
    "endproperty", "sequence", "endsequence", "assert", "assume", "cover", "bind",
    "typedef", "return", "wire", "logic", "reg", "input", "output", "inout",
    "localparam", "parameter", "genvar", "import", "export", "typedef", "struct",
    "union", "enum", "virtual", "const", "static", "signed", "unsigned", "default",
    "modport", "ifdef", "ifndef", "elsif", "else", "endif", "define", "include",
    "timescale", "celldefine", "endcelldefine", "pragma",
    "bit", "byte", "shortint", "int", "longint", "integer", "time", "realtime",
    "real", "shortreal", "string", "event", "chandle", "void", "var", "uwire",
    "tri", "tri0", "tri1", "triand", "trior", "trireg", "wand", "wor", "supply0",
    "supply1", "interconnect", "rand", "randc", "ref",
}}
primitive_modules = {{
    "and", "nand", "or", "nor", "xor", "xnor", "buf", "not", "bufif0", "bufif1",
    "notif0", "notif1", "nmos", "pmos", "rnmos", "rpmos", "cmos", "rcmos", "tran",
    "rtran", "tranif0", "tranif1", "rtranif0", "rtranif1", "pullup", "pulldown",
}}
ignored_instance_name_tokens = {{"if", "for", "while", "case", "assign", "modport"}}
compiler_directive_re = re.compile(r"(?m)^\\s*`.*$")
preprocessor_directive_re = re.compile(
    r"^\\s*`(?P<directive>ifdef|ifndef|elsif|else|endif)\\b(?:\\s+(?P<name>[A-Za-z_][A-Za-z0-9_$]*))?"
)
active_defines = set()
fpga_vendor_instance_re = re.compile(r"\\b(?:blk_mem_gen(?:_[A-Za-z0-9]+)?|ddr4_0)\\b")
recipe_by_id = {{
    item.get("recipe_id", ""): item
    for item in recipe_library.get("recipes", [])
    if isinstance(item, dict) and item.get("recipe_id")
}}

def deep_merge_dict(base, override):
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged

def load_adaptation_hints(candidate_root: pathlib.Path):
    hints = dict(recipe_library.get("default_mapping_hints", {{}}))
    for filename in recipe_library.get("hint_filenames", []):
        if not filename:
            continue
        for probe in [candidate_root / filename, candidate_root.parent / filename]:
            if not probe.exists() or not probe.is_file():
                continue
            try:
                loaded = json.loads(probe.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if isinstance(loaded, dict):
                hints = deep_merge_dict(hints, loaded)
    return hints

def read_text_snippet(path: pathlib.Path, *, limit: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit] + "\\n...<truncated>..."

def strip_comments(text: str) -> str:
    text = re.sub(r"/\\*.*?\\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)

def strip_non_syntax_noise(text: str) -> str:
    cleaned = strip_comments(text)
    return compiler_directive_re.sub("", cleaned)

def extract_detected_macros(readme_text: str):
    match = re.search(
        r"Preprocessor macros detected in source:\\n(?P<body>(?:  - .+\\n)+)",
        readme_text or "",
    )
    if not match:
        return []

    macros = []
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        macro = stripped[2:].strip()
        if macro and macro not in macros:
            macros.append(macro)
    return macros

def preferred_genus_macros(macros):
    preferred = []
    seen = set()
    excluded = {{"FPGA"}}
    for macro in macros:
        normalized = str(macro).strip()
        if not normalized or normalized in excluded or normalized in seen:
            continue
        seen.add(normalized)
        preferred.append(normalized)
    return preferred

def extract_active_defines(run_tcl_text: str, readme_text: str):
    match = re.search(
        r"set\\s+ACTIVE_DEFINES\\s+\\[list\\s+(?P<body>[^\\]]*)\\]",
        run_tcl_text or "",
    )
    if match:
        return preferred_genus_macros(match.group("body").split())
    return preferred_genus_macros(extract_detected_macros(readme_text))

def preprocess_for_active_defines(text: str, defines):
    active = []
    stack = []
    current_active = True
    for raw_line in text.splitlines(True):
        match = preprocessor_directive_re.match(raw_line)
        if match:
            directive = match.group("directive")
            name = (match.group("name") or "").strip()
            if directive == "ifdef":
                parent_active = current_active
                branch_active = parent_active and name in defines
                stack.append({{
                    "parent_active": parent_active,
                    "branch_taken": branch_active,
                    "active": branch_active,
                }})
                current_active = branch_active
            elif directive == "ifndef":
                parent_active = current_active
                branch_active = parent_active and name not in defines
                stack.append({{
                    "parent_active": parent_active,
                    "branch_taken": branch_active,
                    "active": branch_active,
                }})
                current_active = branch_active
            elif directive == "elsif":
                if not stack:
                    continue
                frame = stack[-1]
                branch_active = (
                    frame["parent_active"]
                    and not frame["branch_taken"]
                    and name in defines
                )
                frame["active"] = branch_active
                if branch_active:
                    frame["branch_taken"] = True
                current_active = branch_active
            elif directive == "else":
                if not stack:
                    continue
                frame = stack[-1]
                branch_active = frame["parent_active"] and not frame["branch_taken"]
                frame["active"] = branch_active
                if branch_active:
                    frame["branch_taken"] = True
                current_active = branch_active
            elif directive == "endif":
                if stack:
                    stack.pop()
                current_active = stack[-1]["active"] if stack else True
            continue
        if current_active:
            active.append(raw_line)
    return "".join(active)

def prepare_rtl_text(text: str):
    return preprocess_for_active_defines(text, active_defines)

def classify_fpga_only_artifact(rel_path: str, text: str, defines):
    if "FPGA" in defines:
        return ""
    prepared = prepare_rtl_text(text)
    if re.search(r"(?m)^\\s*module\\s+bram_fifo_[A-Za-z0-9_]+\\b", prepared):
        return "contains FPGA BRAM wrapper RTL for generated Xilinx memory IP"
    if fpga_vendor_instance_re.search(prepared):
        return "instantiates FPGA-only vendor IP"
    return ""

def parse_module_definitions(text):
    cleaned = strip_non_syntax_noise(prepare_rtl_text(text))
    return re.findall(r"(?m)^\\s*module\\s+([A-Za-z_][A-Za-z0-9_$]*)\\b", cleaned)

def parse_interface_definitions(text):
    cleaned = strip_non_syntax_noise(prepare_rtl_text(text))
    return re.findall(r"(?m)^\\s*interface\\s+([A-Za-z_][A-Za-z0-9_$]*)\\b", cleaned)

def parse_type_aliases(text):
    cleaned = strip_non_syntax_noise(prepare_rtl_text(text))
    aliases = []
    for match in re.finditer(r"(?s)\\btypedef\\b.*?\\b([A-Za-z_][A-Za-z0-9_$]*)\\s*;", cleaned):
        alias = (match.group(1) or "").strip()
        if alias and alias not in aliases:
            aliases.append(alias)
    return aliases

def find_matching_paren(text: str, start_index: int) -> int:
    depth = 0
    for index in range(start_index, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1

def extract_module_port_list(text: str, module_name: str) -> str:
    cleaned = strip_comments(text)
    match = re.search(
        rf"(?m)^\\s*module\\s+{{re.escape(module_name)}}\\b",
        cleaned,
    )
    if not match:
        return ""

    cursor = match.end()
    while cursor < len(cleaned) and cleaned[cursor].isspace():
        cursor += 1

    if cursor < len(cleaned) and cleaned[cursor] == "#":
        param_start = cleaned.find("(", cursor)
        if param_start == -1:
            return ""
        param_end = find_matching_paren(cleaned, param_start)
        if param_end == -1:
            return ""
        cursor = param_end + 1
        while cursor < len(cleaned) and cleaned[cursor].isspace():
            cursor += 1

    if cursor >= len(cleaned) or cleaned[cursor] != "(":
        return ""

    ports_end = find_matching_paren(cleaned, cursor)
    if ports_end == -1:
        return ""

    return cleaned[cursor + 1 : ports_end]

def extract_interface_port_details(port_text: str, known_interface_names=None):
    if not port_text:
        return []

    known_interface_names = sorted(
        {{
            str(item).strip()
            for item in (known_interface_names or [])
            if str(item).strip()
        }},
        key=len,
        reverse=True,
    )
    details = []
    seen = set()

    def append_detail(raw_snippet: str, normalized: str, ports, interface_type="", modport=""):
        ports = [port for port in ports if port]
        if not normalized or not ports:
            return
        key = (normalized, tuple(ports), interface_type, modport)
        if key in seen:
            return
        seen.add(key)
        details.append({{
            "raw_snippet": raw_snippet,
            "normalized_snippet": normalized,
            "ports": ports,
            "interface_type": interface_type,
            "modport": modport,
            "has_explicit_modport": bool(modport),
        }})

    for match in re.finditer(r"(?im)(?:^|,)(\\s*interface\\b[^\\n;]*)", port_text):
        raw_snippet = match.group(1)
        normalized = re.sub(r"\\s+", " ", raw_snippet).strip()
        body = re.sub(r"^interface\\b", "", normalized, flags=re.IGNORECASE).strip()
        ports = []
        for item in body.split(","):
            token_match = re.match(r"([A-Za-z_][A-Za-z0-9_$]*)", item.strip())
            if not token_match:
                continue
            token = token_match.group(1)
            if token not in ports:
                ports.append(token)
        append_detail(raw_snippet, normalized, ports, interface_type="interface", modport="")

    for interface_name in known_interface_names:
        pattern = re.compile(
            rf"(?im)(?:^|,)(\\s*{{re.escape(interface_name)}}(?:\\s*\\.\\s*[A-Za-z_][A-Za-z0-9_$]*)?\\s+[^,;\\n]+)"
        )
        for match in pattern.finditer(port_text):
            raw_snippet = match.group(1)
            normalized = re.sub(r"\\s+", " ", raw_snippet).strip()
            modport_match = re.match(
                rf"^{{re.escape(interface_name)}}(?:\\s*\\.\\s*([A-Za-z_][A-Za-z0-9_$]*))?",
                normalized,
                flags=re.IGNORECASE,
            )
            modport = (modport_match.group(1) or "").strip() if modport_match else ""
            body = re.sub(
                rf"^{{re.escape(interface_name)}}(?:\\s*\\.\\s*[A-Za-z_][A-Za-z0-9_$]*)?",
                "",
                normalized,
                flags=re.IGNORECASE,
            ).strip()
            ports = []
            for item in body.split(","):
                token_match = re.search(r"([A-Za-z_][A-Za-z0-9_$]*)", item.strip())
                if not token_match:
                    continue
                token = token_match.group(1)
                if token not in ports:
                    ports.append(token)
            append_detail(
                raw_snippet,
                normalized,
                ports,
                interface_type=interface_name,
                modport=modport,
            )

    return details

def extract_generic_interface_port_details(port_text: str, known_interface_names=None):
    return [
        item
        for item in extract_interface_port_details(port_text, known_interface_names)
        if not bool(item.get("has_explicit_modport"))
    ]

def extract_typed_interface_port_details(port_text: str, known_interface_names=None):
    return [
        item
        for item in extract_interface_port_details(port_text, known_interface_names)
        if bool(item.get("has_explicit_modport"))
    ]

def detect_generic_interface_ports(port_text: str, known_interface_names=None):
    return [
        str(item.get("normalized_snippet", "")).strip()
        for item in extract_generic_interface_port_details(port_text, known_interface_names)
        if str(item.get("normalized_snippet", "")).strip()
    ]

def detect_typed_interface_ports(port_text: str, known_interface_names=None):
    return [
        str(item.get("normalized_snippet", "")).strip()
        for item in extract_typed_interface_port_details(port_text, known_interface_names)
        if str(item.get("normalized_snippet", "")).strip()
    ]

def detect_testbench_markers(module_name: str, rel_path: str, port_text: str, text: str):
    prepared = prepare_rtl_text(text)
    marker_examples = []

    def add_marker(marker: str):
        if marker not in marker_examples:
            marker_examples.append(marker)

    has_no_ports = not port_text.strip()
    has_initial_blocks = bool(re.search(r"(?m)\binitial\b", prepared))
    has_testbench_tasks = bool(
        re.search(r"(?m)\$(?:display|finish|stop|monitor|strobe)\b", prepared)
    )
    has_clock_generator = bool(
        re.search(r"(?is)\bforever\s*#\s*\d+|\balways\s*#\s*\d+", prepared)
    )
    instantiated_names = parse_instantiated_modules(prepared)
    has_bfm_like_instantiations = any(
        re.search(r"(?i)\b(?:tb|bfm|memory|send_|receive_)", name)
        for name in instantiated_names
    )

    if has_no_ports:
        add_marker("declares no top-level ports")
    if has_initial_blocks:
        add_marker("contains initial blocks")
    if has_testbench_tasks:
        add_marker("uses $display/$finish/$stop/$monitor-style testbench tasks")
    if has_clock_generator:
        add_marker("contains delay-based clock generation")
    if has_bfm_like_instantiations:
        add_marker("instantiates testbench/BFM-like helper modules")
    if tb_name_re.search(module_name) or tb_name_re.search(rel_path):
        add_marker("name/path looks testbench-like")

    likely_testbench_or_harness = (
        (has_no_ports and (has_initial_blocks or has_clock_generator))
        or (has_initial_blocks and has_testbench_tasks)
        or (has_no_ports and has_bfm_like_instantiations)
        or bool(tb_name_re.search(module_name) or tb_name_re.search(rel_path))
    )

    return {{
        "has_no_ports": has_no_ports,
        "likely_testbench_or_harness": likely_testbench_or_harness,
        "marker_examples": marker_examples[:8],
    }}

def analyze_module_ports(module_name: str, rel_path: str, text: str, known_interface_names=None):
    port_text = extract_module_port_list(text, module_name)
    generic_interface_ports = detect_generic_interface_ports(port_text, known_interface_names)
    generic_interface_port_details = extract_generic_interface_port_details(port_text, known_interface_names)
    typed_interface_ports = detect_typed_interface_ports(port_text, known_interface_names)
    typed_interface_port_details = extract_typed_interface_port_details(port_text, known_interface_names)
    testbench_markers = detect_testbench_markers(module_name, rel_path, port_text, text)
    return {{
        "module": module_name,
        "path": rel_path,
        "has_generic_interface_ports": bool(generic_interface_ports),
        "generic_interface_ports": generic_interface_ports[:8],
        "generic_interface_port_details": generic_interface_port_details[:8],
        "has_typed_interface_modports": bool(typed_interface_ports),
        "typed_interface_ports": typed_interface_ports[:8],
        "typed_interface_port_details": typed_interface_port_details[:8],
        "has_any_interface_ports": bool(generic_interface_ports or typed_interface_ports),
        "has_no_ports": bool(testbench_markers.get("has_no_ports")),
        "likely_testbench_or_harness": bool(
            testbench_markers.get("likely_testbench_or_harness")
        ),
        "testbench_marker_examples": testbench_markers.get("marker_examples", [])[:8],
    }}

def parse_interface_instances(text, known_interface_names):
    cleaned = strip_comments(prepare_rtl_text(text))
    instances = {{}}
    pattern = re.compile(
        r"(?m)^\\s*([A-Za-z_][A-Za-z0-9_$]*)\\s*(?:#\\s*\\([^;]*?\\))?\\s+([A-Za-z_][A-Za-z0-9_$]*)\\s*\\(",
        re.DOTALL,
    )
    for match in pattern.finditer(cleaned):
        type_name = match.group(1).strip()
        instance_name = match.group(2).strip()
        if type_name not in known_interface_names:
            continue
        instances.setdefault(instance_name, type_name)
    return instances

def find_module_instantiations(text, module_name):
    cleaned = strip_comments(prepare_rtl_text(text))
    instantiations = []
    pattern = re.compile(rf"(?<![A-Za-z0-9_$]){{re.escape(module_name)}}\\b")
    cursor = 0
    while True:
        match = pattern.search(cleaned, cursor)
        if not match:
            break
        cursor = match.end()
        index = cursor
        while index < len(cleaned) and cleaned[index].isspace():
            index += 1

        if index < len(cleaned) and cleaned[index] == "#":
            param_start = cleaned.find("(", index)
            if param_start == -1:
                continue
            param_end = find_matching_paren(cleaned, param_start)
            if param_end == -1:
                continue
            index = param_end + 1
            while index < len(cleaned) and cleaned[index].isspace():
                index += 1

        instance_match = re.match(r"([A-Za-z_][A-Za-z0-9_$]*)", cleaned[index:])
        if not instance_match:
            continue
        instance_name = instance_match.group(1)
        index += instance_match.end()
        while index < len(cleaned) and cleaned[index].isspace():
            index += 1
        if index >= len(cleaned) or cleaned[index] != "(":
            continue
        body_end = find_matching_paren(cleaned, index)
        if body_end == -1:
            continue
        body = cleaned[index + 1 : body_end]
        instantiations.append({{
            "instance_name": instance_name,
            "connections": body,
        }})
        cursor = body_end + 1
    return instantiations

def parse_named_connections(connection_text):
    connections = {{}}
    for match in re.finditer(r"\\.([A-Za-z_][A-Za-z0-9_$]*)\\s*\\(\\s*([^()]+?)\\s*\\)", connection_text, re.DOTALL):
        port_name = match.group(1).strip()
        expression = re.sub(r"\\s+", " ", match.group(2)).strip()
        if port_name and expression and port_name not in connections:
            connections[port_name] = expression
    return connections

def normalize_interface_mapping(raw_mapping, interface_type_hint=""):
    mapping = str(raw_mapping).strip()
    hinted_type = str(interface_type_hint).strip()
    if not mapping:
        return ""
    if "." in mapping:
        interface_type, modport = mapping.split(".", 1)
        interface_type = interface_type.strip()
        modport = modport.strip()
        if not interface_type or not modport:
            return ""
        if hinted_type and interface_type != hinted_type:
            return ""
        return f"{{interface_type}}.{{modport}}"
    if not hinted_type:
        return ""
    return f"{{hinted_type}}.{{mapping}}"

def build_interface_top_adaptation(selected_top_analysis, all_interface_definitions, adaptation_hints):
    top_module = selected_top_analysis.get("module", "")
    top_path = selected_top_analysis.get("path", "")
    port_details = selected_top_analysis.get("generic_interface_port_details", [])
    if not top_module or not top_path or not port_details:
        return {{
            "inferable": False,
            "deterministic": False,
            "reason": "Selected top does not expose raw generic interface ports requiring adaptation.",
            "generic_interface_ports": [],
            "inferred_modport_map": {{}},
            "ambiguous_ports": [],
            "missing_ports": [],
            "generated_top_module": "",
            "generated_top_file": "",
            "plan": {{}},
            "applicable_recipe_ids": [],
            "chosen_recipe_id": "",
        }}

    raw_ports = []
    for detail in port_details:
        for port_name in detail.get("ports", []):
            if port_name not in raw_ports:
                raw_ports.append(port_name)

    known_interface_names = set(all_interface_definitions.keys())
    observations = {{}}
    for path in rtl_files:
        rel_path = str(path.relative_to(candidate))
        if rel_path == top_path or rel_path in fpga_only_artifacts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        interface_instances = parse_interface_instances(text, known_interface_names)
        if not interface_instances:
            continue
        for instantiation in find_module_instantiations(text, top_module):
            named_connections = parse_named_connections(instantiation.get("connections", ""))
            for port_name in raw_ports:
                expression = named_connections.get(port_name, "")
                expr_match = re.fullmatch(
                    r"([A-Za-z_][A-Za-z0-9_$]*)\\.([A-Za-z_][A-Za-z0-9_$]*)",
                    expression,
                )
                if not expr_match:
                    continue
                base_signal = expr_match.group(1)
                modport_name = expr_match.group(2)
                interface_type = interface_instances.get(base_signal, "")
                if not interface_type:
                    continue
                observations.setdefault(port_name, []).append({{
                    "mapping": f"{{interface_type}}.{{modport_name}}",
                    "interface_type": interface_type,
                    "modport": modport_name,
                    "evidence_file": rel_path,
                    "instance_name": instantiation.get("instance_name", ""),
                    "expression": expression,
                }})

    inferred_modport_map = {{}}
    evidence = {{}}
    ambiguous_ports = []
    missing_ports = []
    hint_backed_ports = []

    def resolve_explicit_mapping(port_name, interface_type):
        if not isinstance(adaptation_hints, dict):
            return "", ""

        explicit_root = adaptation_hints.get("interface_port_mappings", {{}})
        if isinstance(explicit_root, dict):
            for scope_key in (top_module, "*"):
                scoped_mappings = explicit_root.get(scope_key, {{}})
                if not isinstance(scoped_mappings, dict):
                    continue
                for mapping_key in (port_name, "*"):
                    normalized_mapping = normalize_interface_mapping(
                        scoped_mappings.get(mapping_key, ""),
                        interface_type,
                    )
                    if normalized_mapping:
                        return normalized_mapping, f"interface_port_mappings.{{scope_key}}.{{mapping_key}}"

        interface_type_root = adaptation_hints.get("interface_type_port_mappings", {{}})
        if isinstance(interface_type_root, dict) and interface_type:
            type_scoped_mappings = interface_type_root.get(interface_type, {{}})
            if isinstance(type_scoped_mappings, dict):
                for mapping_key in (
                    f"{{top_module}}.{{port_name}}",
                    port_name,
                    "*",
                ):
                    normalized_mapping = normalize_interface_mapping(
                        type_scoped_mappings.get(mapping_key, ""),
                        interface_type,
                    )
                    if normalized_mapping:
                        return normalized_mapping, f"interface_type_port_mappings.{{interface_type}}.{{mapping_key}}"

        interface_type_defaults = adaptation_hints.get("interface_type_default_modports", {{}})
        if isinstance(interface_type_defaults, dict) and interface_type:
            normalized_mapping = normalize_interface_mapping(
                interface_type_defaults.get(interface_type, ""),
                interface_type,
            )
            if normalized_mapping:
                return normalized_mapping, f"interface_type_default_modports.{{interface_type}}"

        return "", ""

    for port_name in raw_ports:
        port_interface_type = ""
        for detail in port_details:
            if port_name not in detail.get("ports", []):
                continue
            port_interface_type = str(detail.get("interface_type", "")).strip()
            if port_interface_type:
                break
        explicit_mapping, explicit_source = resolve_explicit_mapping(
            port_name,
            port_interface_type,
        )
        if explicit_mapping:
            inferred_modport_map[port_name] = explicit_mapping
            hint_backed_ports.append(port_name)
            evidence[port_name] = [{{
                "mapping": explicit_mapping,
                "interface_type": explicit_mapping.split(".", 1)[0],
                "modport": explicit_mapping.split(".", 1)[1] if "." in explicit_mapping else "",
                "evidence_file": explicit_source or "explicit_hint",
                "instance_name": "",
                "expression": port_name,
            }}]
            continue
        port_observations = observations.get(port_name, [])
        unique_mappings = []
        for item in port_observations:
            mapping = item.get("mapping", "")
            if mapping and mapping not in unique_mappings:
                unique_mappings.append(mapping)
        if len(unique_mappings) == 1:
            inferred_modport_map[port_name] = unique_mappings[0]
            evidence[port_name] = port_observations[:4]
        elif len(unique_mappings) > 1:
            ambiguous_ports.append({{
                "port": port_name,
                "mappings": unique_mappings,
                "evidence": port_observations[:6],
            }})
        else:
            missing_ports.append(port_name)

    inferable = len(inferred_modport_map) == len(raw_ports) and not ambiguous_ports and not missing_ports
    applicable_recipe_ids = ["generic_interface_top_to_impl"]
    if inferable:
        generated_top_module = f"{{top_module}}_impl"
        generated_top_file = f"generated/{{generated_top_module}}.sv"
        port_rewrites = []
        for detail in port_details:
            raw_snippet = detail.get("raw_snippet", "")
            replacement_items = []
            for port_name in detail.get("ports", []):
                replacement_items.append(f"{{inferred_modport_map[port_name]}} {{port_name}}")
            indent_match = re.match(r"\\s*", raw_snippet or "")
            indent = indent_match.group(0) if indent_match else ""
            replacement_text = (",\\n" + indent).join(replacement_items)
            port_rewrites.append({{
                "raw_snippet": raw_snippet,
                "replacement_text": replacement_text,
                "ports": detail.get("ports", []),
            }})
        plan = {{
            "recipe_id": "generic_interface_top_to_impl",
            "source_top_file": top_path,
            "source_top_module": top_module,
            "generated_top_file": generated_top_file,
            "generated_top_module": generated_top_module,
            "port_rewrites": port_rewrites,
            "metadata_updates": [
                "top_module.txt",
                "filelist_genus.f",
                "README_prepared.txt",
            ],
        }}
        if hint_backed_ports and len(hint_backed_ports) == len(raw_ports):
            reason = (
                "Deterministic modport adaptation is inferable from explicit mapping hints "
                "for every raw interface port."
            )
        elif hint_backed_ports:
            reason = (
                "Deterministic modport adaptation is inferable from a combination of explicit "
                "mapping hints and observed typed interface-modport instantiations."
            )
        else:
            reason = (
                "Deterministic modport adaptation is inferable from existing top instantiations "
                "that connect the selected top through typed interface modports."
            )
        chosen_recipe_id = "generic_interface_top_to_impl"
    else:
        generated_top_module = ""
        generated_top_file = ""
        plan = {{}}
        if ambiguous_ports:
            reason = "Generic interface port mapping is ambiguous across available instantiation evidence."
        elif missing_ports:
            reason = "Generic interface port mapping could not be inferred for every raw interface port."
        else:
            reason = "No trusted instantiation evidence was found to infer typed interface modports."
        chosen_recipe_id = ""

    return {{
        "inferable": inferable,
        "deterministic": inferable,
        "reason": reason,
        "generic_interface_ports": raw_ports,
        "inferred_modport_map": inferred_modport_map,
        "mapping_evidence": evidence,
        "ambiguous_ports": ambiguous_ports,
        "missing_ports": missing_ports,
        "hint_backed_ports": hint_backed_ports,
        "generated_top_module": generated_top_module,
        "generated_top_file": generated_top_file,
        "plan": plan,
        "applicable_recipe_ids": applicable_recipe_ids,
        "chosen_recipe_id": chosen_recipe_id,
    }}

def rel_path_exists(rel_path: str):
    target = candidate / rel_path
    return target.exists() and target.is_file()

def read_rel_path(rel_path: str):
    target = candidate / rel_path
    if not target.exists() or not target.is_file():
        return ""
    return target.read_text(encoding="utf-8", errors="replace")

def build_memory_controller_bundle_recipe(selected_top_analysis, adaptation_hints):
    top_module = str(selected_top_analysis.get("module", "")).strip()
    selected_top_path = str(selected_top_analysis.get("path", "")).strip()
    required_rel_paths = [
        "rtl/MemoryController.sv",
        "rtl/Controller_FSM.sv",
        "rtl/Receive_command.sv",
        "rtl/Read_data_comm.sv",
        "rtl/ReadWrite.sv",
        "rtl/DDR4Interface.sv",
        "rtl/C2MInterface.sv",
    ]
    missing_files = [item for item in required_rel_paths if not rel_path_exists(item)]
    readwrite_text = read_rel_path("rtl/ReadWrite.sv")
    has_dual_edge_dqs = bool(
        re.search(r"always_ff\\s*@\\(\\s*posedge\\s+DQS_t\\s*,\\s*posedge\\s+DQS_c\\s*\\)", readwrite_text)
    )
    has_tristate_behavior = bool(re.search(r"'z", readwrite_text))
    has_ddr_behavioral_model = has_dual_edge_dqs or has_tristate_behavior
    explicit_recipe = ""
    if isinstance(adaptation_hints, dict):
        preferred = adaptation_hints.get("preferred_recipe_by_top", {{}})
        if isinstance(preferred, dict):
            explicit_recipe = str(preferred.get(top_module, "")).strip()

    matches = (
        top_module == "MemoryController"
        and selected_top_path.endswith("MemoryController.sv")
        and not missing_files
        and bool(selected_top_analysis.get("has_any_interface_ports"))
        and has_ddr_behavioral_model
        and (not explicit_recipe or explicit_recipe == "memory_controller_impl_bundle")
    )
    generated_files = [
        "generated/MemoryController_impl.sv",
        "generated/MemContFSM_impl.sv",
        "generated/receive_command_impl.sv",
        "generated/send_read_data_impl.sv",
        "generated/ReadWrite_synth.sv",
    ]
    excluded_sources = [
        "./rtl/MemoryController.sv",
        "./rtl/Controller_FSM.sv",
        "./rtl/Receive_command.sv",
        "./rtl/Read_data_comm.sv",
        "./rtl/ReadWrite.sv",
        "./rtl/Memory.sv",
    ]
    if not matches:
        if missing_files:
            reason = "Known MemoryController synthesis recipe is missing required source files."
        elif top_module != "MemoryController":
            reason = "Selected top does not match the known MemoryController project recipe."
        elif not selected_top_analysis.get("has_any_interface_ports"):
            reason = "Selected top does not expose the interface-heavy top pattern expected by the MemoryController recipe."
        elif not has_ddr_behavioral_model:
            reason = "Known DDR behavioral ReadWrite pattern was not detected for the MemoryController recipe."
        else:
            reason = "MemoryController project recipe requirements were not met."
        return {{
            "matches": False,
            "recipe_id": "memory_controller_impl_bundle",
            "reason": reason,
            "generated_files": generated_files,
            "excluded_sources": excluded_sources,
            "missing_files": missing_files,
            "plan": {{}},
        }}

    plan = {{
        "recipe_id": "memory_controller_impl_bundle",
        "selected_top_module": top_module,
        "selected_top_file": selected_top_path,
        "generated_top_module": "MemoryController_impl",
        "generated_top_file": "generated/MemoryController_impl.sv",
        "generated_files": generated_files,
        "excluded_source_files": excluded_sources,
        "metadata_updates": ["top_module.txt", "filelist_genus.f", "README_prepared.txt"],
        "required_source_files": required_rel_paths,
        "interface_port_mappings": {{
            "DDR4Bus": "DDR4Interface.Controller",
            "MCTx": "C2MInterface.Mem_data",
            "MCRx": "C2MInterface.Mem_tran",
        }},
        "adaptation_reason": (
            "Known MemoryController project recipe matched: raw generic top interfaces plus "
            "DDR behavioral ReadWrite transport logic were detected, so generate the reviewed *_impl bundle."
        ),
    }}
    return {{
        "matches": True,
        "recipe_id": "memory_controller_impl_bundle",
        "reason": plan["adaptation_reason"],
        "generated_files": generated_files,
        "excluded_sources": excluded_sources,
        "missing_files": [],
        "plan": plan,
    }}

def top_candidate_score(module_name: str, rel_path: str, analysis: dict, instantiated_names):
    score = 0
    reasons = []
    lowered_name = module_name.lower()
    lowered_path = rel_path.lower()
    if module_name not in instantiated_names:
        score += 40
        reasons.append("not instantiated by another prepared module")
    if analysis.get("has_generic_interface_ports"):
        score -= 100
        reasons.append("exposes raw generic interface ports")
    if analysis.get("has_no_ports"):
        score -= 80
        reasons.append("declares no top-level ports")
    if analysis.get("likely_testbench_or_harness"):
        score -= 200
        reasons.append("looks like a testbench or simulation harness")
    if re.search(r"(wrapper|synth|genus|asic|integration|chip|system|top)$", lowered_name):
        score += 20
        reasons.append("name looks like a synthesis/integration wrapper")
    if any(token in lowered_path for token in ("wrapper", "synth", "genus", "asic", "integration", "top")):
        score += 10
        reasons.append("source path looks wrapper/top-oriented")
    if tb_name_re.search(module_name) or tb_name_re.search(rel_path):
        score -= 100
        reasons.append("looks testbench-like")
    return score, reasons

def parse_instantiated_modules(text):
    cleaned = strip_non_syntax_noise(prepare_rtl_text(text))
    matches = re.findall(
        r"(?s)(?<![A-Za-z0-9_$])([A-Za-z_][A-Za-z0-9_$]*)\\s*(?:#\\s*\\([^;]*?\\))?\\s+([A-Za-z_][A-Za-z0-9_$]*)\\s*\\(",
        cleaned,
    )
    modules = []
    for module_name, instance_name in matches:
        lowered = module_name.lower()
        if lowered in ignored_instance_heads or lowered in primitive_modules:
            continue
        if instance_name.lower() in ignored_instance_name_tokens:
            continue
        modules.append(module_name)
    return modules

def resolve_filelist_entry(entry: str) -> pathlib.Path:
    raw = entry.strip()
    if raw.startswith("+incdir+"):
        raw = raw[len("+incdir+"):]
    resolved = pathlib.Path(raw)
    if not resolved.is_absolute():
        resolved = candidate / raw
    return resolved.resolve()

def unique_list(values):
    seen = set()
    ordered = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered

def unique_messages(values):
    return unique_list(values)

if not candidate.exists():
    errors.append(f"Prepared RTL candidate does not exist: {{candidate}}")
else:
    if not candidate.is_dir():
        errors.append(f"Prepared RTL candidate is not a directory: {{candidate}}")

    all_files = sorted(
        str(path.relative_to(candidate))
        for path in candidate.rglob("*")
        if path.is_file()
    )
    context["inventory"] = {{
        "total_files": len(all_files),
        "files": all_files[:MAX_CONTEXT_FILES],
        "truncated": len(all_files) > MAX_CONTEXT_FILES,
    }}

    if rules_text:
        required_phrases = [
            "filelist_genus.f",
            "constraints/mc_genus.sdc",
            "run_genus_mc.tcl",
            "README_prepared.txt",
            "top_module.txt",
        ]
        missing_rule_phrases = [
            phrase for phrase in required_phrases if phrase not in rules_text
        ]
        if missing_rule_phrases:
            errors.append(
                "Universal rules file is missing expected references: "
                + ", ".join(missing_rule_phrases)
            )

    for rel_path in required_files:
        full_path = candidate / rel_path
        context["artifact_status"][rel_path] = full_path.exists()
        if not full_path.exists():
            errors.append(f"Missing required prep artifact: {{rel_path}}")
            suggested_fixes.append(f"Regenerate prep outputs so {{rel_path}} is emitted.")
        elif full_path.is_file() and not full_path.read_text(encoding="utf-8", errors="replace").strip():
            errors.append(f"Prep artifact is empty: {{rel_path}}")
            suggested_fixes.append(f"Populate {{rel_path}} with valid generated content.")
        elif full_path.is_file():
            context["key_files"][rel_path] = read_text_snippet(full_path, limit=MAX_CONTEXT_CHARS)

    readme_text = (candidate / "README_prepared.txt").read_text(encoding="utf-8", errors="replace") if (candidate / "README_prepared.txt").exists() else ""
    run_tcl_text = (candidate / "run_genus_mc.tcl").read_text(encoding="utf-8", errors="replace") if (candidate / "run_genus_mc.tcl").exists() else ""
    active_defines = set(extract_active_defines(run_tcl_text, readme_text))
    context["dependency_analysis"]["active_defines"] = sorted(active_defines)

    rtl_files = sorted(
        path for path in candidate.rglob("*")
        if path.is_file() and path.suffix.lower() in rtl_suffixes
    )
    if not rtl_files:
        errors.append("Prepared RTL directory does not contain any RTL source files.")

    fpga_only_artifacts = {{}}
    for path in rtl_files:
        rel_path = str(path.relative_to(candidate))
        text = path.read_text(encoding="utf-8", errors="replace")
        reason = classify_fpga_only_artifact(rel_path, text, active_defines)
        if reason:
            fpga_only_artifacts[rel_path] = reason

    all_module_definitions = {{}}
    all_interface_definitions = {{}}
    all_type_aliases = {{}}
    for path in rtl_files:
        rel_path = str(path.relative_to(candidate))
        if rel_path in fpga_only_artifacts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for module_name in parse_module_definitions(text):
            all_module_definitions.setdefault(module_name, []).append(rel_path)
        for interface_name in parse_interface_definitions(text):
            all_interface_definitions.setdefault(interface_name, []).append(rel_path)
        for alias_name in parse_type_aliases(text):
            all_type_aliases.setdefault(alias_name, []).append(rel_path)

    bad_rtl_names = [
        str(path.relative_to(candidate))
        for path in rtl_files
        if tb_name_re.search(path.name)
    ]
    for rel_path in bad_rtl_names[:10]:
        errors.append(f"Prepared RTL still includes testbench-like source: {{rel_path}}")
    if len(bad_rtl_names) > 10:
        errors.append(
            f"Prepared RTL contains {{len(bad_rtl_names)}} testbench-like sources; only the first 10 are listed."
        )

    for path in rtl_files[:200]:
        rel_path = str(path.relative_to(candidate))
        if rel_path in fpga_only_artifacts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        cleaned_text = strip_non_syntax_noise(prepare_rtl_text(text))
        for pattern, message in prohibited_patterns:
            if pattern.search(cleaned_text):
                errors.append(f"Prepared RTL file {{rel_path}} {{message}}.")
                suggested_fixes.append(
                    f"Remove or sanitize the synthesis-hostile construct in {{rel_path}}: {{message}}."
                )
                break

    for path in rtl_files[:MAX_RTL_SAMPLES]:
        rel_path = str(path.relative_to(candidate))
        if rel_path in fpga_only_artifacts:
            continue
        context["rtl_samples"].append(
            {{
                "path": rel_path,
                "content": read_text_snippet(path, limit=MAX_RTL_SAMPLE_CHARS),
            }}
        )

    filelist_path = candidate / "filelist_genus.f"
    filelist_entries = []
    source_entries = []
    resolved_source_entries = []
    filelist_module_definitions = {{}}
    filelist_interface_definitions = {{}}
    filelist_type_aliases = {{}}
    instantiated_modules = {{}}
    if filelist_path.exists():
        filelist_entries = [
            line.strip()
            for line in filelist_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not filelist_entries:
            errors.append("filelist_genus.f does not list any source files.")
        seen_filelist_entries = {{}}
        duplicate_filelist_entries = []
        for entry in filelist_entries:
            normalized_entry = entry.replace("\\\\", "/").strip()
            if normalized_entry in seen_filelist_entries:
                if normalized_entry not in duplicate_filelist_entries:
                    duplicate_filelist_entries.append(normalized_entry)
                continue
            seen_filelist_entries[normalized_entry] = True
        if duplicate_filelist_entries:
            duplicate_examples = ", ".join(duplicate_filelist_entries[:12])
            errors.append(
                f"filelist_genus.f contains duplicate source entries (e.g., {{duplicate_examples}}). "
                "This can cause double-definition/parse-order noise and violates prep bundle cleanliness expectations."
            )
            suggested_fixes.append(
                "Deduplicate filelist_genus.f so each RTL source appears exactly once and preserve deliberate package/interface ordering."
            )
        for entry in filelist_entries[:400]:
            entry_name = pathlib.Path(entry).name
            if tb_name_re.search(entry_name):
                errors.append(f"filelist_genus.f references testbench-like source: {{entry}}")
        source_entries = [
            entry for entry in filelist_entries
            if not entry.startswith("+incdir+")
        ]
        allowed_entry_errors = [
            entry for entry in source_entries
            if pathlib.Path(entry).suffix.lower() not in rtl_suffixes
        ]
        for entry in allowed_entry_errors[:10]:
            errors.append(f"filelist_genus.f contains a non-RTL entry: {{entry}}")
            suggested_fixes.append(
                f"Remove or rewrite the non-RTL filelist entry '{{entry}}' so Genus sees valid source directives only."
            )
        for entry in source_entries[:400]:
            resolved_entry = resolve_filelist_entry(entry)
            resolved_source_entries.append({{
                "entry": entry,
                "resolved": str(resolved_entry),
                "exists": resolved_entry.exists(),
            }})
            if not resolved_entry.exists():
                dependency_errors.append(
                    f"Prepared bundle is missing the filelist dependency '{{entry}}'."
                )
                suggested_fixes.append(
                    f"Regenerate the prep bundle or repair filelist_genus.f so '{{entry}}' resolves to an existing RTL file."
                )
                continue
            try:
                resolved_entry.relative_to(candidate.resolve())
            except ValueError:
                warnings.append(
                    f"filelist_genus.f entry resolves outside the prepared bundle: {{entry}}"
                )
            if resolved_entry.suffix.lower() not in rtl_suffixes:
                continue
            rel_path = str(resolved_entry.relative_to(candidate.resolve()))
            if rel_path in fpga_only_artifacts:
                continue
            entry_text = resolved_entry.read_text(encoding="utf-8", errors="replace")
            for module_name in parse_module_definitions(entry_text):
                filelist_module_definitions.setdefault(module_name, []).append(rel_path)
            for interface_name in parse_interface_definitions(entry_text):
                filelist_interface_definitions.setdefault(interface_name, []).append(rel_path)
            for alias_name in parse_type_aliases(entry_text):
                filelist_type_aliases.setdefault(alias_name, []).append(rel_path)
            for module_name in parse_instantiated_modules(entry_text):
                instantiated_modules.setdefault(module_name, set()).add(rel_path)

    if not instantiated_modules:
        for path in rtl_files[:400]:
            rel_path = str(path.relative_to(candidate))
            if rel_path in fpga_only_artifacts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for module_name in parse_instantiated_modules(text):
                instantiated_modules.setdefault(module_name, set()).add(rel_path)

    fpga_only_filelist_entries = []
    for entry_info in resolved_source_entries:
        rel_path = ""
        try:
            rel_path = str(pathlib.Path(entry_info["resolved"]).resolve().relative_to(candidate.resolve()))
        except Exception:
            rel_path = ""
        if rel_path and rel_path in fpga_only_artifacts:
            fpga_only_filelist_entries.append(entry_info["entry"])

    for rel_path, reason in sorted(fpga_only_artifacts.items())[:MAX_DEPENDENCY_EXAMPLES]:
        dependency_errors.append(
            f"Prepared bundle includes FPGA-only RTL artifact '{{rel_path}}' which should be excluded from the non-FPGA Cadence flow because it {{reason}}."
        )
    if len(fpga_only_artifacts) > MAX_DEPENDENCY_EXAMPLES:
        dependency_errors.append(
            f"Prepared bundle includes {{len(fpga_only_artifacts)}} FPGA-only RTL artifacts; only the first {{MAX_DEPENDENCY_EXAMPLES}} are listed."
        )
    if fpga_only_artifacts:
        suggested_fixes.append(
            "Remove FPGA-only RTL artifacts from the prepared bundle and filelist_genus.f when the Cadence flow is not using the FPGA define path."
        )

    missing_from_bundle = []
    missing_from_filelist = []
    interface_references = {{}}
    type_alias_references = {{}}
    for module_name, sources in sorted(instantiated_modules.items()):
        if module_name in all_interface_definitions:
            interface_references[module_name] = sorted(sources)
            continue
        if module_name in all_type_aliases:
            type_alias_references[module_name] = sorted(sources)
            continue
        if module_name in all_module_definitions:
            if module_name not in filelist_module_definitions:
                example_source = sorted(sources)[0]
                defining_paths = all_module_definitions.get(module_name, [])
                defining_rel = defining_paths[0] if defining_paths else "unknown"
                missing_from_filelist.append(
                    f"Prepared RTL instantiates module '{{module_name}}' in '{{example_source}}', "
                    f"but filelist_genus.f does not include its definition from '{{defining_rel}}'."
                )
            continue
        example_source = sorted(sources)[0]
        missing_from_bundle.append(
            f"Prepared RTL instantiates module '{{module_name}}' in '{{example_source}}', "
            "but no module definition for it exists in the prepared bundle."
        )

    dependency_errors.extend(missing_from_bundle[:MAX_DEPENDENCY_EXAMPLES])
    dependency_errors.extend(missing_from_filelist[:MAX_DEPENDENCY_EXAMPLES])
    if len(missing_from_bundle) > MAX_DEPENDENCY_EXAMPLES:
        dependency_errors.append(
            f"Prepared bundle is missing definitions for {{len(missing_from_bundle)}} instantiated modules; only the first {{MAX_DEPENDENCY_EXAMPLES}} are listed."
        )
    if len(missing_from_filelist) > MAX_DEPENDENCY_EXAMPLES:
        dependency_errors.append(
            f"filelist_genus.f omits definitions for {{len(missing_from_filelist)}} instantiated modules; only the first {{MAX_DEPENDENCY_EXAMPLES}} are listed."
        )

    if missing_from_bundle:
        suggested_fixes.append(
            "Regenerate the prep bundle so every instantiated helper RTL module is copied into the prepared directory."
        )
    if missing_from_filelist:
        suggested_fixes.append(
            "Repair filelist_genus.f so it includes the prepared RTL files that define the instantiated helper modules."
        )

    instantiated_module_names = set(instantiated_modules.keys())
    module_port_analysis = {{}}
    top_selection_candidates = []
    for module_name, defining_paths in sorted(all_module_definitions.items()):
        if not defining_paths:
            continue
        rel_path = defining_paths[0]
        source_path = candidate / rel_path
        if not source_path.exists():
            continue
        source_text = source_path.read_text(encoding="utf-8", errors="replace")
        analysis = analyze_module_ports(
            module_name,
            rel_path,
            source_text,
            set(all_interface_definitions),
        )
        module_port_analysis[module_name] = analysis
        score, reasons = top_candidate_score(
            module_name,
            rel_path,
            analysis,
            instantiated_module_names,
        )
        top_selection_candidates.append({{
            "module": module_name,
            "path": rel_path,
            "score": score,
            "reasons": reasons,
            "has_generic_interface_ports": analysis["has_generic_interface_ports"],
            "generic_interface_ports": analysis["generic_interface_ports"],
            "has_no_ports": analysis["has_no_ports"],
            "likely_testbench_or_harness": analysis["likely_testbench_or_harness"],
            "testbench_marker_examples": analysis["testbench_marker_examples"],
        }})

    top_selection_candidates.sort(
        key=lambda item: (
            -int(item.get("score", 0)),
            int(bool(item.get("has_generic_interface_ports"))),
            str(item.get("module", "")),
        )
    )

    context["dependency_analysis"] = {{
        "module_definitions_in_bundle": sorted(all_module_definitions)[:MAX_CONTEXT_FILES],
        "module_definitions_in_filelist": sorted(filelist_module_definitions)[:MAX_CONTEXT_FILES],
        "interface_definitions_in_bundle": sorted(all_interface_definitions)[:MAX_CONTEXT_FILES],
        "interface_definitions_in_filelist": sorted(filelist_interface_definitions)[:MAX_CONTEXT_FILES],
        "type_aliases_in_bundle": sorted(all_type_aliases)[:MAX_CONTEXT_FILES],
        "type_aliases_in_filelist": sorted(filelist_type_aliases)[:MAX_CONTEXT_FILES],
        "instantiated_modules": sorted(instantiated_modules)[:MAX_CONTEXT_FILES],
        "interface_references": sorted(interface_references)[:MAX_CONTEXT_FILES],
        "type_alias_references": sorted(type_alias_references)[:MAX_CONTEXT_FILES],
        "fpga_only_artifacts": [
            {{"path": rel_path, "reason": reason}}
            for rel_path, reason in sorted(fpga_only_artifacts.items())[:MAX_CONTEXT_FILES]
        ],
        "fpga_only_filelist_entries": fpga_only_filelist_entries[:MAX_CONTEXT_FILES],
        "missing_from_bundle": missing_from_bundle[:MAX_CONTEXT_FILES],
        "missing_from_filelist": missing_from_filelist[:MAX_CONTEXT_FILES],
        "duplicate_filelist_entries": duplicate_filelist_entries[:MAX_CONTEXT_FILES] if filelist_path.exists() else [],
        "resolved_source_entries": resolved_source_entries[:MAX_CONTEXT_FILES],
        "truncated": (
            len(all_module_definitions) > MAX_CONTEXT_FILES
            or len(filelist_module_definitions) > MAX_CONTEXT_FILES
            or len(all_interface_definitions) > MAX_CONTEXT_FILES
            or len(filelist_interface_definitions) > MAX_CONTEXT_FILES
            or len(all_type_aliases) > MAX_CONTEXT_FILES
            or len(filelist_type_aliases) > MAX_CONTEXT_FILES
            or len(instantiated_modules) > MAX_CONTEXT_FILES
            or len(interface_references) > MAX_CONTEXT_FILES
            or len(type_alias_references) > MAX_CONTEXT_FILES
            or len(fpga_only_artifacts) > MAX_CONTEXT_FILES
            or len(fpga_only_filelist_entries) > MAX_CONTEXT_FILES
            or len(missing_from_bundle) > MAX_CONTEXT_FILES
            or len(missing_from_filelist) > MAX_CONTEXT_FILES
            or (filelist_path.exists() and len(duplicate_filelist_entries) > MAX_CONTEXT_FILES)
            or len(resolved_source_entries) > MAX_CONTEXT_FILES
        ),
    }}

    intended_top_module = {str(state.get("top_module", "")).strip()!r}
    top_module_path = candidate / "top_module.txt"
    top_module = ""
    if top_module_path.exists():
        top_module = top_module_path.read_text(encoding="utf-8", errors="replace").strip()
        if not top_module:
            errors.append("top_module.txt is empty.")
            suggested_fixes.append("Write the inferred synthesis top module into top_module.txt.")
    else:
        suggested_fixes.append("Emit top_module.txt if the prep flow supports top inference output.")

    selected_top_analysis = module_port_analysis.get(top_module, {{}})
    intended_top_analysis = module_port_analysis.get(intended_top_module, {{}})
    adaptation_hints = load_adaptation_hints(candidate)
    adaptation = build_interface_top_adaptation(
        selected_top_analysis,
        all_interface_definitions,
        adaptation_hints,
    )
    selected_memory_controller_recipe = build_memory_controller_bundle_recipe(
        selected_top_analysis,
        adaptation_hints,
    )
    intended_memory_controller_recipe = (
        build_memory_controller_bundle_recipe(
            intended_top_analysis,
            adaptation_hints,
        )
        if intended_top_module and intended_top_module != top_module
        else {{"matches": False, "plan": {{}}, "generated_files": [], "reason": ""}}
    )
    memory_controller_recipe = (
        selected_memory_controller_recipe
        if selected_memory_controller_recipe.get("matches")
        else intended_memory_controller_recipe
    )
    recommended_top_override = ""
    auto_apply_top_override = False
    non_testbench_candidates = [
        item for item in top_selection_candidates
        if not item.get("likely_testbench_or_harness")
    ]
    safe_candidates = [
        item for item in non_testbench_candidates
        if not item.get("has_generic_interface_ports")
    ]
    if selected_top_analysis.get("likely_testbench_or_harness") and non_testbench_candidates:
        best_candidate = non_testbench_candidates[0]
        if top_module and best_candidate.get("module") != top_module:
            recommended_top_override = str(best_candidate.get("module", "")).strip()
            auto_apply_top_override = bool(recommended_top_override)
    elif safe_candidates:
        best_candidate = safe_candidates[0]
        second_score = safe_candidates[1]["score"] if len(safe_candidates) > 1 else None
        if (
            top_module
            and best_candidate.get("module") != top_module
            and int(best_candidate.get("score", 0)) >= 45
            and (second_score is None or int(best_candidate.get("score", 0)) - int(second_score) >= 10)
        ):
            recommended_top_override = str(best_candidate.get("module", "")).strip()
            auto_apply_top_override = bool(recommended_top_override)

    if (
        memory_controller_recipe.get("matches")
        and intended_top_module
        and intended_top_module != top_module
    ):
        recommended_top_override = intended_top_module
        auto_apply_top_override = True

    detected_patterns = []
    applicable_recipe_ids = []
    chosen_recipe_id = ""
    ambiguity_reason = ""
    generated_files = []
    original_files_preserved = True

    if selected_top_analysis.get("has_generic_interface_ports"):
        detected_patterns.append({{
            "pattern_id": "raw_generic_interface_top",
            "subclass": "generic_interface_top",
            "owner": "prep",
            "evidence": selected_top_analysis.get("generic_interface_ports", [])[:8],
        }})
        applicable_recipe_ids.extend(adaptation.get("applicable_recipe_ids", []))

    if memory_controller_recipe.get("matches"):
        detected_patterns.extend(
            [
                {{
                    "pattern_id": "ddr_behavioral_readwrite",
                    "subclass": "ddr_behavioral_readwrite_to_clock2x_abstraction",
                    "owner": "prep",
                    "evidence": ["rtl/ReadWrite.sv uses DQS-driven always_ff and tri-state behavior."],
                }},
                {{
                    "pattern_id": "interface_heavy_communication_wrappers",
                    "subclass": "communication_wrapper_to_flat_transaction_ports",
                    "owner": "prep",
                    "evidence": ["rtl/Receive_command.sv", "rtl/Read_data_comm.sv"],
                }},
            ]
        )
        applicable_recipe_ids.extend(
            [
                "memory_controller_impl_bundle",
                "ddr_behavioral_readwrite_to_clock2x_abstraction",
                "communication_wrapper_to_flat_transaction_ports",
            ]
        )
        chosen_recipe_id = "memory_controller_impl_bundle"
        generated_files = memory_controller_recipe.get("generated_files", [])
        ambiguity_reason = ""
    elif selected_top_analysis.get("has_generic_interface_ports") and adaptation.get("inferable"):
        chosen_recipe_id = str(adaptation.get("chosen_recipe_id", "")).strip()
        generated_files = [str(adaptation.get("generated_top_file", "")).strip()] if adaptation.get("generated_top_file") else []
    elif selected_top_analysis.get("has_generic_interface_ports"):
        ambiguity_reason = str(adaptation.get("reason", "")).strip()

    applicable_recipe_ids = unique_list(
        [str(item).strip() for item in applicable_recipe_ids if str(item).strip()]
    )

    synthesis_top_checks = {{
        "selected_top_module": top_module or "",
        "intended_top_module": intended_top_module,
        "selection_source": "top_module.txt" if top_module_path.exists() else "missing_top_module.txt",
        "selected_top_path": selected_top_analysis.get("path", ""),
        "selected_top_has_generic_interface_ports": bool(
            selected_top_analysis.get("has_generic_interface_ports")
        ),
        "selected_top_has_typed_interface_modports": bool(
            selected_top_analysis.get("has_typed_interface_modports")
        ),
        "selected_top_has_any_interface_ports": bool(
            selected_top_analysis.get("has_any_interface_ports")
        ),
        "selected_top_likely_testbench_or_harness": bool(
            selected_top_analysis.get("likely_testbench_or_harness")
        ),
        "selected_top_testbench_marker_examples": selected_top_analysis.get(
            "testbench_marker_examples",
            [],
        )[:8],
        "generic_interface_port_examples": selected_top_analysis.get(
            "generic_interface_ports",
            [],
        )[:8],
        "typed_interface_port_examples": selected_top_analysis.get(
            "typed_interface_ports",
            [],
        )[:8],
        "generic_interface_ports": adaptation.get("generic_interface_ports", []),
        "selected_top_file": selected_top_analysis.get("path", ""),
        "adaptation_inferable": bool(adaptation.get("inferable")),
        "adaptation_deterministic": bool(adaptation.get("deterministic")),
        "adaptation_reason": adaptation.get("reason", ""),
        "inferred_modport_map": adaptation.get("inferred_modport_map", {{}}),
        "mapping_evidence": adaptation.get("mapping_evidence", {{}}),
        "ambiguous_interface_ports": adaptation.get("ambiguous_ports", []),
        "missing_interface_ports": adaptation.get("missing_ports", []),
        "generated_top_module": (
            memory_controller_recipe.get("plan", {{}}).get("generated_top_module", "")
            if memory_controller_recipe.get("matches")
            else adaptation.get("generated_top_module", "")
        ),
        "generated_top_file": (
            memory_controller_recipe.get("plan", {{}}).get("generated_top_file", "")
            if memory_controller_recipe.get("matches")
            else adaptation.get("generated_top_file", "")
        ),
        "adaptation_plan": (
            memory_controller_recipe.get("plan", {{}})
            if memory_controller_recipe.get("matches")
            else adaptation.get("plan", {{}})
        ),
        "detected_patterns": detected_patterns,
        "applicable_recipe_ids": applicable_recipe_ids,
        "chosen_recipe_id": chosen_recipe_id,
        "generated_files": generated_files,
        "original_files_preserved": original_files_preserved,
        "ambiguity_reason": ambiguity_reason,
        "adaptation_hints": adaptation_hints,
        "recommended_top_override": recommended_top_override,
        "auto_apply_top_override": auto_apply_top_override,
        "preferred_recipe_top_module": (
            intended_top_module
            if memory_controller_recipe.get("matches") and intended_top_module
            else ""
        ),
        "top_selection_candidates": top_selection_candidates[:12],
    }}
    context["synthesis_top_checks"] = synthesis_top_checks
    summary_details = []

    if top_module and not selected_top_analysis:
        warnings.append(
            f"Selected synthesis top '{{top_module}}' was not found among prepared module definitions."
        )
    elif selected_top_analysis.get("likely_testbench_or_harness"):
        examples = ", ".join(
            selected_top_analysis.get("testbench_marker_examples", [])[:4]
        )
        errors.append(
            f"Selected synthesis top '{{top_module}}' looks like a testbench or simulation harness"
            + (f": {{examples}}" if examples else ".")
        )
        suggested_fixes.append(
            "Choose the real RTL design top instead of a harness/testbench module with initial blocks, delay-based clocks, or no external ports."
        )
        if recommended_top_override:
            suggested_fixes.append(
                f"Consider updating top_module.txt to '{{recommended_top_override}}', which ranks as the likelier RTL design top in the prepared bundle."
            )
    elif selected_top_analysis.get("has_generic_interface_ports"):
        examples = ", ".join(selected_top_analysis.get("generic_interface_ports", [])[:4])
        errors.append(
            f"Selected synthesis top '{{top_module}}' exposes raw generic interface ports that are not suitable for direct Genus elaboration"
            + (f": {{examples}}" if examples else ".")
        )
        if chosen_recipe_id == "memory_controller_impl_bundle":
            summary_details.append(
                "Recognized a deterministic MemoryController-specific synthesis adaptation path for the raw-interface top."
            )
            errors.append(
                "Known MemoryController synthesis adaptation recipe is required because the selected top also depends on DDR behavioral ReadWrite transport logic that is not synthesis-safe as-is."
            )
            suggested_fixes.append(
                "Apply the approved 'memory_controller_impl_bundle' recipe to generate build-only *_impl modules plus a synthesis-safe ReadWrite replacement, then switch the prepared build to MemoryController_impl."
            )
        elif adaptation.get("inferable"):
            summary_details.append(
                f"Recognized generic_interface_top_to_impl and built a deterministic wrapper plan for '{{adaptation.get('generated_top_module', '') or top_module}}'."
            )
            suggested_fixes.append(
                f"Apply the approved '{{chosen_recipe_id or adaptation.get('chosen_recipe_id', 'generic_interface_top_to_impl')}}' recipe to generate a synthesis-only adapted copy '{{adaptation.get('generated_top_module', '')}}' from '{{top_module}}' that rewrites the raw interface ports to inferred typed modports, then switch top_module.txt and filelist_genus.f to use that generated copy."
            )
        else:
            missing_ports = [
                str(item).strip()
                for item in adaptation.get("missing_ports", [])
                if str(item).strip()
            ]
            ambiguous_ports = []
            for item in adaptation.get("ambiguous_ports", []):
                if not isinstance(item, dict):
                    continue
                port_name = str(item.get("port", "")).strip()
                mappings = [
                    str(mapping).strip()
                    for mapping in item.get("mappings", [])
                    if str(mapping).strip()
                ]
                if port_name:
                    detail = port_name
                    if mappings:
                        detail += " -> " + ", ".join(mappings[:4])
                    ambiguous_ports.append(detail)
            adaptation_reason = str(adaptation.get("reason", "")).strip()
            detail_parts = []
            if missing_ports:
                detail_parts.append(
                    "missing deterministic modport mappings for ports: " + ", ".join(missing_ports)
                )
            if ambiguous_ports:
                detail_parts.append(
                    "ambiguous mapping evidence for ports: " + "; ".join(ambiguous_ports)
                )
            if adaptation_reason:
                detail_parts.append(adaptation_reason)
            detail_text = "; ".join(part for part in detail_parts if part).strip()
            if detail_text:
                errors.append(
                    "Deterministic synthesis-only wrapper generation could not be completed: "
                    + detail_text
                    + "."
                )
                summary_details.append(
                    "Recognized generic_interface_top_to_impl, but deterministic wrapper generation was blocked because "
                    + detail_text
                    + "."
                )
            else:
                summary_details.append(
                    "Recognized generic_interface_top_to_impl, but deterministic wrapper generation was blocked because the interface mapping evidence was incomplete."
                )
            suggested_fixes.append(
                "Choose a synthesis-safe wrapper or explicit top override that does not expose raw generic interface ports at the top boundary."
            )
            if missing_ports or ambiguous_ports:
                suggested_fixes.append(
                    "Provide explicit deterministic interface-port mapping hints for the raw top ports so prep can generate the approved synthesis-only wrapper automatically."
                )
            if recommended_top_override:
                suggested_fixes.append(
                    f"Consider updating top_module.txt to '{{recommended_top_override}}', which ranks as a safer synthesis-top candidate in the prepared bundle."
                )
            else:
                suggested_fixes.append(
                    "No approved synthesis adaptation recipe or clear wrapper candidate was auto-selected from the prepared bundle; provide an explicit top override, mapping file, or manual recipe."
                )

    if not selected_top_analysis.get("has_generic_interface_ports"):
        recipe_requirements = adaptation_hints.get("project_recipe_requirements", {{}})
        memory_requirements = (
            recipe_requirements.get("memory_controller_impl_bundle", {{}})
            if isinstance(recipe_requirements, dict)
            else {{}}
        )
        memory_recipe_required = bool(
            top_module in memory_requirements.get("top_modules", [])
            and memory_controller_recipe.get("missing_files")
        )
        if memory_recipe_required:
            errors.append(
                "Known MemoryController synthesis adaptation recipe cannot be applied because required source files are missing from the prepared bundle."
            )
            suggested_fixes.append(
                "Repair the prepared bundle so the approved MemoryController synthesis recipe has all required source files before retrying prep."
            )

    run_tcl_path = candidate / "run_genus_mc.tcl"
    if run_tcl_path.exists():
        if "filelist_genus.f" not in run_tcl_text:
            errors.append("run_genus_mc.tcl does not reference filelist_genus.f.")
            suggested_fixes.append("Update run_genus_mc.tcl to read filelist_genus.f.")
        if top_module and top_module not in run_tcl_text:
            errors.append(
                f"run_genus_mc.tcl does not reference the inferred top module '{{top_module}}'."
            )
            suggested_fixes.append(
                f"Update run_genus_mc.tcl so it references the inferred top module '{{top_module}}'."
            )
        if "Preprocessor macros detected in source:" in readme_text and "set_db hdl_define" not in run_tcl_text:
            errors.append(
                "Prep bundle detects source macros but run_genus_mc.tcl does not apply any `define settings."
            )
            suggested_fixes.append(
                "Update run_genus_mc.tcl so detected synthesis defines are applied explicitly before read_hdl."
            )

    sdc_path = candidate / "constraints" / "mc_genus.sdc"
    if sdc_path.exists():
        sdc_text = sdc_path.read_text(encoding="utf-8", errors="replace")
        if "create_clock" not in sdc_text:
            errors.append("constraints/mc_genus.sdc is missing create_clock.")
            suggested_fixes.append("Add a create_clock constraint to constraints/mc_genus.sdc.")

    readme_path = candidate / "README_prepared.txt"
    if readme_path.exists():
        readme_text = readme_path.read_text(encoding="utf-8", errors="replace")
        if top_module and top_module not in readme_text:
            errors.append(
                f"README_prepared.txt does not mention the inferred top module '{{top_module}}'."
            )
            warnings.append(
                f"README_prepared.txt is missing the inferred top module '{{top_module}}'."
            )

errors.extend(unique_messages(dependency_errors))
result["prep_dependency_errors"] = unique_messages(dependency_errors)
result["errors"] = unique_messages(errors)
result["warnings"] = unique_messages(warnings)
result["suggested_fixes"] = unique_messages(suggested_fixes)
result["detected_patterns"] = detected_patterns
result["applicable_recipe_ids"] = applicable_recipe_ids
result["chosen_recipe_id"] = chosen_recipe_id or None
result["subclass"] = None
result["pass"] = not result["errors"]
if context.get("synthesis_top_checks", {{}}).get("selected_top_has_generic_interface_ports"):
    result["failure_stage_owner"] = "prep"
    result["failure_class"] = "generic_interface_top"
    result["subclass"] = "raw_generic_interface_top"
    result["route_back_to_stage"] = "rtl_prep"
elif detected_patterns and not applicable_recipe_ids and result["errors"]:
    result["failure_stage_owner"] = "prep"
    result["failure_class"] = "unsupported_synthesis_recipe_needed"
    result["subclass"] = detected_patterns[0].get("subclass")
    result["route_back_to_stage"] = "rtl_prep"
elif result["prep_dependency_errors"] or result["errors"]:
    result["failure_stage_owner"] = "prep"
    result["failure_class"] = "prep_bundle_completeness_problem"
    result["route_back_to_stage"] = "rtl_prep"
else:
    result["failure_stage_owner"] = None
    result["failure_class"] = None
    result["route_back_to_stage"] = None
result["summary"] = (
    f"Local prep review {{'passed' if result['pass'] else 'failed'}} "
    f"with {{len(result['errors'])}} errors and {{len(result['warnings'])}} warnings."
)
if summary_details:
    result["summary"] += " " + " ".join(summary_details)
print(json.dumps(result))
"""

    return f"{remote_python} -c {shlex.quote(review_script)}"


def _build_prepare_fix_command(
    state: FlowState,
    candidate_path: str,
    actions: list[dict[str, str]],
) -> str:
    remote_python = _remote_python(state)
    actions_json = json.dumps(actions)
    memory_controller_impl_template = """// Generated synthesis-only adaptation for MemoryController.
module MemoryController_impl
#(
`include "Definitions.pkg"
)
(
    input logic reset, clock, clock2x,
    DDR4Interface.Controller DDR4Bus,
    input logic [31:0] MCRx_addr,
    input logic [511:0] MCRx_data_tran,
    input logic MCRx_rw,
    input logic MCRx_valid_tran,
    output logic MCRx_ack_tran,
    output logic [2:0] MCRx_tag_tran,
    output logic MCRx_full,
    input logic MCTx_ack_data,
    output logic [511:0] MCTx_read_data,
    output logic [2:0] MCTx_tag_data,
    output logic MCTx_valid_data
);

logic [ADDRESSWIDTH-1:0] Address_cs, Address_sf;
logic [DATAWIDTH-1:0] Data_cs, Data_sf;
logic rw_cs, rw_sf;
logic [2:0] tag, tagreadin, tagreadout;
logic full;
logic buff;

logic start, refresh, ap, done;
wire [DATAWIDTH-1:0] data;
logic [BWIDTH-1:0] bank;
logic [BGWIDTH-1:0] bankgroup;
logic [RWIDTH-1:0] row;
logic [CWIDTH-1:0] column;
logic [1:0] PageHit;
logic PageMiss, PageEmpty;
logic valid;

logic [DATAWIDTH+TWIDTH-1:0] ReadBuffer;
wire [514:3] databuff;
always_ff @(posedge clock) begin
    if (buff) begin
        ReadBuffer[514:3] <= databuff;
        ReadBuffer[2:0] <= tagreadout;
    end else begin
        ReadBuffer <= ReadBuffer;
    end
end

assign row = Address_sf[31:17];
assign bank = Address_sf[16:15];
assign column = {Address_sf[14:8], Address_sf[5:3]};
assign bankgroup = Address_sf[7:6];

MemContFSM_impl m(
    .start, .rw(rw_sf), .refresh, .ap,
    .bank, .bankgroup,
    .row, .column,
    .datawrite(Data_sf), .dataread(databuff), .buff, .tagreadin, .tagreadout,
    .DDR4Bus(DDR4Bus),
    .reset, .clock, .clock2x, .done,
    .PageHit, .PageEmpty, .PageMiss
);

Scheduler S(
    .DataIn(Data_cs), .AddrIn(Address_cs),
    .rw_in(rw_cs), .reset, .clock, .valid,
    .DataOut(Data_sf), .AddrOut(Address_sf),
    .rw_out(rw_sf), .full, .refresh, .ap, .tag,
    .start, .done, .tagread(tagreadin), .tagfsm(tagreadout),
    .buff, .PageHit, .PageMiss, .PageEmpty
);

receive_command_impl rc(
    .IF_clock(clock),
    .IF_reset(reset),
    .IF_addr(MCRx_addr),
    .IF_data_tran(MCRx_data_tran),
    .IF_rw(MCRx_rw),
    .IF_valid_tran(MCRx_valid_tran),
    .IF_ack_tran(MCRx_ack_tran),
    .IF_tag_tran(MCRx_tag_tran),
    .IF_full(MCRx_full),
    .fifo_full(full),
    .tag_gen(tag),
    .rw_out(rw_cs),
    .valid(valid),
    .addr_out(Address_cs),
    .data_out(Data_cs)
);

send_read_data_impl srd(
    .IF_ack_data(MCTx_ack_data),
    .IF_read_data(MCTx_read_data),
    .IF_tag_data(MCTx_tag_data),
    .IF_valid_data(MCTx_valid_data),
    .internal_ready(buff),
    .data_in(ReadBuffer[514:3]),
    .tag_gen(ReadBuffer[2:0])
);

endmodule
"""
    receive_command_impl_template = """// Generated synthesis-only adaptation for receive_command.
module receive_command_impl
#(
    parameter DWIDTH = 512,
    parameter AWIDTH = 32
)
(
    input logic IF_clock,
    input logic IF_reset,
    input logic [AWIDTH-1:0] IF_addr,
    input logic [DWIDTH-1:0] IF_data_tran,
    input logic IF_rw,
    input logic IF_valid_tran,
    output logic IF_ack_tran,
    output logic [2:0] IF_tag_tran,
    output logic IF_full,
    input logic fifo_full,
    input logic [2:0] tag_gen,
    output logic rw_out,
    output logic valid,
    output logic [AWIDTH-1:0] addr_out,
    output logic [DWIDTH-1:0] data_out
);

typedef enum logic [1:0] {init=2'b01, receive=2'b10} st;
st state, next_state;
logic [DWIDTH-1:0] data;
logic [AWIDTH-1:0] addr;
logic rw;

assign valid = IF_ack_tran;

always_ff @(posedge IF_clock) begin
    data_out <= data;
    addr_out <= addr;
    rw_out <= rw;
    if (IF_reset) begin
        state <= init;
    end else begin
        state <= next_state;
    end
end

always_comb begin
    case (state)
        init: next_state = (fifo_full || !IF_valid_tran) ? init : receive;
        receive: next_state = (fifo_full || IF_valid_tran) ? init : receive;
        default: next_state = init;
    endcase
end

always_comb begin
    IF_ack_tran = 1'b0;
    IF_tag_tran = 3'b0;
    IF_full = fifo_full;
    addr = IF_addr;
    data = IF_data_tran;
    rw = IF_rw;
    case (state)
        init: begin
            IF_ack_tran = 1'b0;
            IF_tag_tran = tag_gen;
        end
        receive: begin
            IF_ack_tran = 1'b1;
            IF_tag_tran = tag_gen;
        end
    endcase
end

endmodule
"""
    send_read_data_impl_template = """// Generated synthesis-only adaptation for send_read_data.
module send_read_data_impl
#(
    parameter DWIDTH = 512
)
(
    input logic IF_ack_data,
    output logic [DWIDTH-1:0] IF_read_data,
    output logic [2:0] IF_tag_data,
    output logic IF_valid_data,
    input logic internal_ready,
    input logic [DWIDTH-1:0] data_in,
    input logic [2:0] tag_gen
);

always_comb begin
    IF_read_data = '0;
    IF_tag_data = '0;
    IF_valid_data = 1'b0;
    if (internal_ready) begin
        IF_read_data = data_in;
        IF_tag_data = tag_gen;
        IF_valid_data = 1'b1;
    end
end

endmodule
"""
    readwrite_synth_template = """// Generated synthesis-only abstraction for DDR ReadWrite behavioral transport.
module ReadMod_synth(
    input logic clock2x,
    input logic read,
    input logic reset,
    input logic [63:0] DataBus,
    output logic [7:0][63:0] DataHost,
    input logic DQS_t,
    input logic DQS_c
);
logic [2:0] count;
always_ff @(posedge clock2x) begin
    if (reset) begin
        count <= 3'b0;
        DataHost <= '0;
    end else if (read) begin
        DataHost[count] <= DataBus;
        count <= count + 1'b1;
    end
end
endmodule

module WriteMod_synth(
    input logic clock2x,
    input logic reset,
    input logic write,
    output logic [63:0] DataBus,
    input logic [7:0][63:0] DataHost,
    output logic DQS_t,
    output logic DQS_c
);
logic [2:0] count;
always_ff @(posedge clock2x) begin
    if (reset) begin
        count <= 3'b0;
        DataBus <= '0;
        DQS_t <= 1'b0;
        DQS_c <= 1'b1;
    end else if (write) begin
        DataBus <= DataHost[count];
        count <= count + 1'b1;
        DQS_t <= ~DQS_t;
        DQS_c <= DQS_t;
    end else begin
        DataBus <= '0;
        DQS_t <= 1'b0;
        DQS_c <= 1'b1;
    end
end
endmodule
"""
    fix_script = f"""
import json
import pathlib
import re

candidate = pathlib.Path({candidate_path!r}).resolve()
actions = json.loads({actions_json!r})
MEMORY_CONTROLLER_IMPL_TEMPLATE = {memory_controller_impl_template!r}
RECEIVE_COMMAND_IMPL_TEMPLATE = {receive_command_impl_template!r}
SEND_READ_DATA_IMPL_TEMPLATE = {send_read_data_impl_template!r}
READWRITE_SYNTH_TEMPLATE = {readwrite_synth_template!r}

result = {{
    "applied_actions": [],
    "skipped_actions": [],
    "summary": "",
    "adaptation_applied": False,
    "chosen_recipe_id": "",
    "generated_files": [],
    "original_files_preserved": True,
    "generated_top_module": "",
    "generated_top_file": "",
    "adaptation_reason": "",
}}

candidate_name = candidate.name

def normalize_rel_path(raw_path: str) -> str:
    normalized = raw_path.strip().replace("\\\\", "/").lstrip("./")
    if normalized == candidate_name:
        return "."
    prefix = candidate_name + "/"
    if normalized.startswith(prefix):
        return normalized[len(prefix):]
    return normalized

def resolve_target(rel_path: str) -> pathlib.Path:
    normalized_rel_path = normalize_rel_path(rel_path)
    target = (candidate / normalized_rel_path).resolve()
    target.relative_to(candidate)
    return target

def is_filelist_target(target: pathlib.Path) -> bool:
    return target.name == "filelist_genus.f"

def is_allowed_filelist_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return True
    if stripped.startswith("+incdir+"):
        return True
    if stripped.startswith("+"):
        return False
    return True

def sanitize_filelist_text(text: str) -> str:
    if not text:
        return text
    output_lines = []
    seen_entries = set()
    changed = False
    for line in text.splitlines():
        stripped = line.strip()
        if not is_allowed_filelist_line(line):
            changed = True
            continue
        if not stripped or stripped.startswith("#"):
            output_lines.append(line)
            continue
        normalized = stripped.replace("\\\\", "/")
        if normalized in seen_entries:
            changed = True
            continue
        seen_entries.add(normalized)
        output_lines.append(line)
    if not changed:
        return text
    sanitized = "\\n".join(output_lines)
    if text.endswith("\\n"):
        sanitized += "\\n"
    return sanitized

def ensure_line_present(text: str, line: str) -> str:
    stripped_line = line.strip()
    if not stripped_line:
        return text
    existing = [item.strip().replace("\\\\", "/") for item in text.splitlines() if item.strip()]
    if stripped_line.replace("\\\\", "/") in existing:
        return text
    suffix = "" if text.endswith("\\n") or not text else "\\n"
    return sanitize_filelist_text(text + suffix + stripped_line + "\\n")

def ensure_readme_note(text: str, note: str) -> str:
    note = note.strip()
    if not note:
        return text
    if note in text:
        return text
    suffix = "" if not text or text.endswith("\\n") else "\\n"
    return text + suffix + note + "\\n"

def remove_filelist_entries(text: str, entries):
    normalized_entries = {{
        item.strip().replace("\\\\", "/").lstrip("./")
        for item in entries
        if str(item).strip()
    }}
    output_lines = []
    changed = False
    for line in text.splitlines():
        stripped = line.strip()
        normalized_line = stripped.replace("\\\\", "/").lstrip("./")
        if normalized_line in normalized_entries:
            changed = True
            continue
        output_lines.append(line)
    updated = "\\n".join(output_lines)
    if text.endswith("\\n"):
        updated += "\\n"
    return sanitize_filelist_text(updated) if changed else sanitize_filelist_text(text)

def replace_module_name(source_text: str, source_module: str, generated_module: str) -> str:
    module_pattern = re.compile(rf"(?m)^(\\s*module\\s+){{re.escape(source_module)}}\\b")
    return module_pattern.sub(rf"\\1{{generated_module}}", source_text, count=1)

def generate_memory_controller_impl_text() -> str:
    return MEMORY_CONTROLLER_IMPL_TEMPLATE

def generate_receive_command_impl_text() -> str:
    return RECEIVE_COMMAND_IMPL_TEMPLATE

def generate_send_read_data_impl_text() -> str:
    return SEND_READ_DATA_IMPL_TEMPLATE

def generate_readwrite_synth_text() -> str:
    return READWRITE_SYNTH_TEMPLATE

def apply_memory_controller_impl_recipe(plan):
    required_source_files = plan.get("required_source_files", [])
    missing = [item for item in required_source_files if not resolve_target(item).exists()]
    if missing:
        raise FileNotFoundError("missing required recipe source files: " + ", ".join(missing))

    controller_fsm_target = resolve_target("rtl/Controller_FSM.sv")
    controller_fsm_text = controller_fsm_target.read_text(encoding="utf-8", errors="replace")
    memcontfsm_impl_text = replace_module_name(controller_fsm_text, "MemContFSM", "MemContFSM_impl")
    memcontfsm_impl_text = memcontfsm_impl_text.replace(
        "interface DDR4Bus",
        "DDR4Interface.Controller DDR4Bus",
        1,
    )
    memcontfsm_impl_text = memcontfsm_impl_text.replace(
        "ReadMod RC (",
        "ReadMod_synth RC (.clock2x(clock2x), ",
        1,
    )
    memcontfsm_impl_text = memcontfsm_impl_text.replace(
        "WriteMod WC(",
        "WriteMod_synth WC(",
        1,
    )

    generated_files = {{
        "generated/MemoryController_impl.sv": generate_memory_controller_impl_text(),
        "generated/MemContFSM_impl.sv": memcontfsm_impl_text,
        "generated/receive_command_impl.sv": generate_receive_command_impl_text(),
        "generated/send_read_data_impl.sv": generate_send_read_data_impl_text(),
        "generated/ReadWrite_synth.sv": generate_readwrite_synth_text(),
    }}
    for rel_path, text in generated_files.items():
        target = resolve_target(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    top_module_target = resolve_target("top_module.txt")
    top_module_target.write_text("MemoryController_impl\\n", encoding="utf-8")

    filelist_target = resolve_target("filelist_genus.f")
    if filelist_target.exists() and filelist_target.is_file():
        filelist_text = filelist_target.read_text(encoding="utf-8", errors="replace")
        filelist_text = remove_filelist_entries(
            filelist_text,
            plan.get("excluded_source_files", []),
        )
        # Generated sources rely on `include "Definitions.pkg"`, so prefer
        # include search paths over treating the package file as a compilation unit.
        filelist_text = remove_filelist_entries(
            filelist_text,
            ["./rtl/Definitions.pkg", "rtl/Definitions.pkg"],
        )
        filelist_text = ensure_line_present(filelist_text, "+incdir+./generated")
        for rel_path in generated_files.keys():
            filelist_text = ensure_line_present(filelist_text, "./" + rel_path)
        filelist_target.write_text(filelist_text, encoding="utf-8")

    readme_target = resolve_target("README_prepared.txt")
    if readme_target.exists() and readme_target.is_file():
        readme_text = readme_target.read_text(encoding="utf-8", errors="replace")
        note = (
            "Approved synthesis adaptation recipe applied:\\n"
            "- Recipe: memory_controller_impl_bundle\\n"
            "- Generated top: MemoryController_impl (generated/MemoryController_impl.sv)\\n"
            "- Additional generated files: generated/MemContFSM_impl.sv, generated/receive_command_impl.sv, generated/send_read_data_impl.sv, generated/ReadWrite_synth.sv\\n"
            "- Original source files were preserved unchanged in the prepared bundle.\\n"
        )
        readme_target.write_text(ensure_readme_note(readme_text, note), encoding="utf-8")

    return {{
        "generated_top_module": "MemoryController_impl",
        "generated_top_file": "generated/MemoryController_impl.sv",
        "generated_files": list(generated_files.keys()),
        "adaptation_reason": str(plan.get("adaptation_reason", "")).strip(),
    }}

for action in actions:
    action_type = str(action.get("type", "")).strip()
    rel_path = str(action.get("path", "")).strip()
    reason = str(action.get("reason", "")).strip()
    search_text = str(action.get("search_text", ""))
    replacement_text = str(action.get("replacement_text", ""))
    line_text = str(action.get("line_text", ""))
    if action_type == "remove_line" and not line_text and search_text:
        line_text = search_text.rstrip("\\n")

    if not action_type or not rel_path:
        result["skipped_actions"].append({{
            "action": action,
            "reason": "missing type or path",
        }})
        continue

    try:
        target = resolve_target(rel_path)
    except Exception as exc:
        result["skipped_actions"].append({{
            "action": action,
            "reason": f"invalid target path: {{exc}}",
        }})
        continue

    if action_type == "remove_file":
        if not target.exists():
            result["skipped_actions"].append({{
                "action": action,
                "reason": "target file does not exist",
            }})
            continue
        if target.is_dir():
            result["skipped_actions"].append({{
                "action": action,
                "reason": "remove_file only supports files",
            }})
            continue
        target.unlink()
        result["applied_actions"].append({{
            "type": action_type,
            "path": rel_path,
            "reason": reason,
        }})
        continue

    if action_type == "apply_synthesis_recipe":
        try:
            plan = json.loads(replacement_text or "{{}}")
        except json.JSONDecodeError as exc:
            result["skipped_actions"].append({{
                "action": action,
                "reason": f"invalid synthesis recipe plan JSON: {{exc}}",
            }})
            continue

        recipe_id = str(plan.get("recipe_id", "")).strip()
        if recipe_id != "memory_controller_impl_bundle":
            result["skipped_actions"].append({{
                "action": action,
                "reason": f"unsupported synthesis recipe '{{recipe_id}}'",
            }})
            continue

        try:
            recipe_result = apply_memory_controller_impl_recipe(plan)
        except Exception as exc:
            result["skipped_actions"].append({{
                "action": action,
                "reason": f"failed to apply synthesis recipe '{{recipe_id}}': {{exc}}",
            }})
            continue

        result["applied_actions"].append({{
            "type": action_type,
            "path": rel_path,
            "reason": reason,
        }})
        result["adaptation_applied"] = True
        result["chosen_recipe_id"] = recipe_id
        result["generated_files"] = [
            str(item) for item in recipe_result.get("generated_files", []) if str(item).strip()
        ]
        result["generated_top_module"] = str(recipe_result.get("generated_top_module", "")).strip()
        result["generated_top_file"] = str(recipe_result.get("generated_top_file", "")).strip()
        result["adaptation_reason"] = str(recipe_result.get("adaptation_reason", "")).strip()
        continue

    if action_type == "adapt_synthesis_top":
        try:
            plan = json.loads(replacement_text or "{{}}")
        except json.JSONDecodeError as exc:
            result["skipped_actions"].append({{
                "action": action,
                "reason": f"invalid adaptation plan JSON: {{exc}}",
            }})
            continue

        source_top_file = str(plan.get("source_top_file", "")).strip()
        source_top_module = str(plan.get("source_top_module", "")).strip()
        generated_top_file = str(plan.get("generated_top_file", "")).strip()
        generated_top_module = str(plan.get("generated_top_module", "")).strip()
        port_rewrites = plan.get("port_rewrites", [])
        inferred_modport_map = plan.get("inferred_modport_map", {{}})
        adaptation_reason = str(plan.get("adaptation_reason", "")).strip()
        if (
            not source_top_file
            or not source_top_module
            or not generated_top_file
            or not generated_top_module
            or not isinstance(port_rewrites, list)
            or not port_rewrites
        ):
            result["skipped_actions"].append({{
                "action": action,
                "reason": "adaptation plan is missing required fields",
            }})
            continue

        try:
            source_target = resolve_target(source_top_file)
            generated_target = resolve_target(generated_top_file)
        except Exception as exc:
            result["skipped_actions"].append({{
                "action": action,
                "reason": f"invalid adaptation target path: {{exc}}",
            }})
            continue

        if not source_target.exists() or source_target.is_dir():
            result["skipped_actions"].append({{
                "action": action,
                "reason": "source top file for adaptation does not exist",
            }})
            continue

        source_text = source_target.read_text(encoding="utf-8", errors="replace")
        module_pattern = re.compile(
            rf"(?m)^(\\s*module\\s+){{re.escape(source_top_module)}}\\b"
        )
        if not module_pattern.search(source_text):
            result["skipped_actions"].append({{
                "action": action,
                "reason": "source top module declaration not found",
            }})
            continue

        generated_text = module_pattern.sub(rf"\\1{{generated_top_module}}", source_text, count=1)
        rewrite_failed = False
        for item in port_rewrites:
            if not isinstance(item, dict):
                rewrite_failed = True
                break
            raw_snippet = str(item.get("raw_snippet", ""))
            replacement_port_text = str(item.get("replacement_text", ""))
            if not raw_snippet or not replacement_port_text:
                rewrite_failed = True
                break
            if raw_snippet not in generated_text:
                rewrite_failed = True
                break
            generated_text = generated_text.replace(raw_snippet, replacement_port_text, 1)

        if rewrite_failed:
            result["skipped_actions"].append({{
                "action": action,
                "reason": "failed to rewrite one or more raw interface port declarations",
            }})
            continue

        generated_target.parent.mkdir(parents=True, exist_ok=True)
        generated_target.write_text(generated_text, encoding="utf-8")

        top_module_target = resolve_target("top_module.txt")
        top_module_target.write_text(generated_top_module + "\\n", encoding="utf-8")

        filelist_target = resolve_target("filelist_genus.f")
        if filelist_target.exists() and filelist_target.is_file():
            filelist_text = filelist_target.read_text(encoding="utf-8", errors="replace")
            filelist_rel = "./" + normalize_rel_path(generated_top_file).lstrip("./")
            filelist_target.write_text(
                ensure_line_present(filelist_text, filelist_rel),
                encoding="utf-8",
            )

        readme_target = resolve_target("README_prepared.txt")
        if readme_target.exists() and readme_target.is_file():
            readme_text = readme_target.read_text(encoding="utf-8", errors="replace")
            map_lines = []
            if isinstance(inferred_modport_map, dict):
                for port_name, mapping in inferred_modport_map.items():
                    map_lines.append(f"  - {{port_name}} -> {{mapping}}")
            note = (
                "Synthesis-only top adaptation generated:\\n"
                f"- Source top: {{source_top_module}} ({{normalize_rel_path(source_top_file)}})\\n"
                f"- Generated top: {{generated_top_module}} ({{normalize_rel_path(generated_top_file)}})\\n"
                + (f"- Reason: {{adaptation_reason}}\\n" if adaptation_reason else "")
                + ("- Inferred interface modports:\\n" + "\\n".join(map_lines) + "\\n" if map_lines else "")
                + "- Original top source was preserved unchanged in the prepared bundle.\\n"
            )
            readme_target.write_text(
                ensure_readme_note(readme_text, note),
                encoding="utf-8",
            )

        result["applied_actions"].append({{
            "type": action_type,
            "path": normalize_rel_path(generated_top_file),
            "reason": reason,
        }})
        result["adaptation_applied"] = True
        result["chosen_recipe_id"] = "generic_interface_top_to_impl"
        result["generated_files"] = [normalize_rel_path(generated_top_file)]
        result["generated_top_module"] = generated_top_module
        result["generated_top_file"] = normalize_rel_path(generated_top_file)
        result["adaptation_reason"] = adaptation_reason
        continue

    if not target.exists() or target.is_dir():
        result["skipped_actions"].append({{
            "action": action,
            "reason": "target text file does not exist",
        }})
        continue

    text = target.read_text(encoding="utf-8", errors="replace")

    if action_type == "remove_line":
        original_lines = text.splitlines()
        filtered_lines = [line for line in original_lines if line != line_text]
        if len(filtered_lines) == len(original_lines):
            result["skipped_actions"].append({{
                "action": action,
                "reason": "line_text not found",
            }})
            continue
        trailing_newline = text.endswith("\\n")
        new_text = "\\n".join(filtered_lines)
        if trailing_newline:
            new_text += "\\n"
        if is_filelist_target(target):
            new_text = sanitize_filelist_text(new_text)
        target.write_text(new_text, encoding="utf-8")
        result["applied_actions"].append({{
            "type": action_type,
            "path": rel_path,
            "reason": reason,
        }})
        continue

    if action_type == "replace_text":
        if search_text not in text:
            result["skipped_actions"].append({{
                "action": action,
                "reason": "search_text not found",
            }})
            continue
        new_text = text.replace(search_text, replacement_text)
        if is_filelist_target(target):
            new_text = sanitize_filelist_text(new_text)
        target.write_text(new_text, encoding="utf-8")
        result["applied_actions"].append({{
            "type": action_type,
            "path": rel_path,
            "reason": reason,
        }})
        continue

    if action_type == "append_text":
        append_payload = replacement_text or line_text
        new_text = text + append_payload
        if is_filelist_target(target):
            new_text = sanitize_filelist_text(new_text)
            if new_text == text:
                result["skipped_actions"].append({{
                    "action": action,
                    "reason": "append_text introduced no allowed filelist changes",
                }})
                continue
        target.write_text(new_text, encoding="utf-8")
        result["applied_actions"].append({{
            "type": action_type,
            "path": rel_path,
            "reason": reason,
        }})
        continue

    result["skipped_actions"].append({{
        "action": action,
        "reason": f"unsupported action type: {{action_type}}",
    }})

result["summary"] = (
    f"Applied {{len(result['applied_actions'])}} actions; "
    f"skipped {{len(result['skipped_actions'])}} actions."
)
print(json.dumps(result))
"""

    return f"{remote_python} -c {shlex.quote(fix_script)}"


def _build_stage_access_command(state: FlowState, stage_name: str) -> str:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)

    return (
        f"cd {shlex.quote(project_root)}; "
        "echo 'HOST:'; hostname; "
        "echo 'PWD:'; pwd; "
        f"echo '{stage_name} server access ok'"
    )


def _build_netlist_command(state: FlowState) -> str:
    builddir, builddir_source = resolve_prepare_dir(
        state.get("prep_candidate_path"),
        state.get("remote_prepare_dir"),
    )
    outdir, outdir_source = resolve_netlist_dir_choice(
        state.get("remote_netlist_dir"),
        builddir,
    )
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    netlist_script = state.get(
        "remote_netlist_script",
        "run_genus_netlist_universal.py",
    )
    remote_python = _remote_python(state)
    heartbeat_sec = max(15, int(state.get("netlist_heartbeat_sec", 60)))
    configured_genus_bin = str(state.get("remote_genus_bin") or "").strip()
    fallback_genus_bins = [
        configured_genus_bin,
        "/opt/coe/cadence/DDI231/bin/genus",
    ]
    unique_genus_bins: list[str] = []
    for candidate in fallback_genus_bins:
        if candidate and candidate not in unique_genus_bins:
            unique_genus_bins.append(candidate)
    candidate_list = " ".join(shlex.quote(path) for path in unique_genus_bins)

    inner_cmd = (
        "set -e; "
        f"cd {shlex.quote(project_root)}; "
        f"OUTDIR={shlex.quote(outdir)}; "
        f"BUILDDIR={shlex.quote(builddir)}; "
        f"NETLIST_SCRIPT={shlex.quote(netlist_script)}; "
        f"NETLIST_HEARTBEAT_SEC={heartbeat_sec}; "
        "echo 'HOST:'; hostname; "
        "echo 'PATH:'; echo $PATH; "
        "rm -rf \"$OUTDIR\"; "
        "mkdir -p \"$OUTDIR\"; "
        "EDA_NETLIST_PROGRESS_LOG=\"$OUTDIR/agent_netlist_progress.log\"; "
        "EDA_NETLIST_STDOUT_LOG=\"$OUTDIR/agent_netlist_stdout.log\"; "
        "EDA_NETLIST_STDERR_LOG=\"$OUTDIR/agent_netlist_stderr.log\"; "
        "touch \"$EDA_NETLIST_PROGRESS_LOG\" \"$EDA_NETLIST_STDOUT_LOG\" \"$EDA_NETLIST_STDERR_LOG\"; "
        "echo \"EDA_NETLIST_LOG_DIR:$OUTDIR\"; "
        "echo \"EDA_NETLIST_BUILDDIR:$BUILDDIR\"; "
        f"echo \"EDA_NETLIST_BUILDDIR_SOURCE:{shlex.quote(builddir_source)}\"; "
        "echo \"EDA_NETLIST_OUTDIR:$OUTDIR\"; "
        f"echo \"EDA_NETLIST_OUTDIR_SOURCE:{shlex.quote(outdir_source)}\"; "
        "echo \"EDA_NETLIST_PROGRESS_LOG:$EDA_NETLIST_PROGRESS_LOG\"; "
        "echo \"EDA_NETLIST_STDOUT_LOG:$EDA_NETLIST_STDOUT_LOG\"; "
        "echo \"EDA_NETLIST_STDERR_LOG:$EDA_NETLIST_STDERR_LOG\"; "
        "printf '[%s] netlist launcher started\\n' \"$(date -Is)\" | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        "printf '[%s] builddir=%s\\n' \"$(date -Is)\" \"$BUILDDIR\" | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        f"printf '[%s] builddir_source=%s\\n' \"$(date -Is)\" {shlex.quote(builddir_source)} | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        "printf '[%s] outdir=%s\\n' \"$(date -Is)\" \"$OUTDIR\" | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        f"printf '[%s] outdir_source=%s\\n' \"$(date -Is)\" {shlex.quote(outdir_source)} | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        "printf '[%s] netlist_script=%s\\n' \"$(date -Is)\" \"$NETLIST_SCRIPT\" | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        "echo 'GENUS_CHECK:'; "
        "EDA_RESOLVED_GENUS_BIN=''; "
        f"for candidate in {candidate_list}; do "
        "if [ -n \"$candidate\" ] && [ -x \"$candidate\" ]; then "
        "EDA_RESOLVED_GENUS_BIN=\"$candidate\"; break; "
        "fi; "
        "done; "
        "if [ -z \"$EDA_RESOLVED_GENUS_BIN\" ] && "
        "[ -n \"${EDA_GENUS_BIN:-}\" ] && [ -x \"${EDA_GENUS_BIN}\" ]; then "
        "EDA_RESOLVED_GENUS_BIN=\"${EDA_GENUS_BIN}\"; "
        "fi; "
        "if [ -z \"$EDA_RESOLVED_GENUS_BIN\" ] && "
        "[ -n \"${GENUS_BIN:-}\" ] && [ -x \"${GENUS_BIN}\" ]; then "
        "EDA_RESOLVED_GENUS_BIN=\"${GENUS_BIN}\"; "
        "fi; "
        "if [ -z \"$EDA_RESOLVED_GENUS_BIN\" ]; then "
        "EDA_RESOLVED_GENUS_BIN=$(command -v genus 2>/dev/null || true); "
        "fi; "
        "echo \"$EDA_RESOLVED_GENUS_BIN\"; "
        "if [ -z \"$EDA_RESOLVED_GENUS_BIN\" ] || [ ! -x \"$EDA_RESOLVED_GENUS_BIN\" ]; then "
        "echo 'EDA_NETLIST_ENV_ERROR:genus_executable_not_found' | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        "exit 97; "
        "fi; "
        "echo \"EDA_NETLIST_GENUS_BIN:${EDA_RESOLVED_GENUS_BIN}\" | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        "echo 'GENUS_OK'; "
        "set +e; "
        "( "
        "while true; do "
        "printf '[%s] netlist still running\\n' \"$(date -Is)\" | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        "sleep \"$NETLIST_HEARTBEAT_SEC\"; "
        "done "
        ") & "
        "EDA_NETLIST_HEARTBEAT_PID=$!; "
        f"{remote_python} \"$NETLIST_SCRIPT\" "
        "--builddir \"$BUILDDIR\" "
        "--genus \"$EDA_RESOLVED_GENUS_BIN\" "
        "--outdir \"$OUTDIR\" "
        "> >(tee -a \"$EDA_NETLIST_STDOUT_LOG\") "
        "2> >(tee -a \"$EDA_NETLIST_STDERR_LOG\" >&2); "
        "NETLIST_RC=$?; "
        "kill \"$EDA_NETLIST_HEARTBEAT_PID\" >/dev/null 2>&1 || true; "
        "wait \"$EDA_NETLIST_HEARTBEAT_PID\" 2>/dev/null || true; "
        "set -e; "
        "printf '[%s] netlist exited rc=%s\\n' \"$(date -Is)\" \"$NETLIST_RC\" | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        "echo \"EDA_NETLIST_RUN_COMPLETE:${NETLIST_RC}\" | tee -a \"$EDA_NETLIST_PROGRESS_LOG\"; "
        "exit \"$NETLIST_RC\""
    )

    return _build_inner_ssh_command(state, inner_cmd)


def _build_netlist_review_command(
    state: FlowState,
    builddir: str,
    candidate_path: str,
) -> str:
    rules_text, preflight_errors = _load_rules_text(
        state.get("netlist_rules_path"),
        label="Universal netlist",
    )
    rules_path = state.get("netlist_rules_path") or ""
    remote_python = _remote_python(state)

    review_script = f"""
import fnmatch
import json
import pathlib
import re

builddir = pathlib.Path({builddir!r})
candidate = pathlib.Path({candidate_path!r})
rules_path = {rules_path!r}
rules_text = {rules_text!r}
preflight_errors = {preflight_errors!r}
MAX_CONTEXT_FILES = 400
MAX_CONTEXT_CHARS = 6000
MAX_KEY_FILE_CHARS = 3000
MAX_KEY_FILE_COUNT = 8
MAX_LOG_BYTES = 250000
MAX_ERROR_EXAMPLES = 12

result = {{
    "pass": False,
    "errors": list(preflight_errors),
    "blocking_errors": list(preflight_errors),
    "warnings": [],
    "suggested_fixes": [],
    "failure_stage_owner": "netlist",
    "failure_class": "netlist_output_validation_failure",
    "route_back_to_stage": "netlist",
    "summary": "",
    "rules_path": rules_path,
    "candidate_path": str(candidate),
    "context": {{
        "input_builddir": str(builddir),
        "resolved_top": "",
        "prep_artifact_status": {{}},
        "output_inventory": {{}},
        "key_files": {{}},
        "detected_outputs": [],
        "log_classification": {{}},
    }},
}}

errors = result["blocking_errors"]
warnings = result["warnings"]
suggested_fixes = result["suggested_fixes"]
context = result["context"]
prep_contract_errors = []
required_prep_files = [
    "filelist_genus.f",
    "run_genus_mc.tcl",
    "README_prepared.txt",
    "top_module.txt",
    "constraints/mc_genus.sdc",
]
key_file_candidates = [
    "top_module.txt",
    "filelist_genus.f",
    "run_genus_mc.tcl",
    "constraints/mc_genus.sdc",
    "README_prepared.txt",
]
netlist_output_patterns = [
    "*_generic.v",
    "*_mapped.v",
    "*.db",
    "*.vg",
    "*.v",
    "*.sv",
    "*_generic.sdc",
    "*_mapped.sdc",
]
log_patterns = ["*.log", "*.rpt", "*.txt"]

def read_text_snippet(path: pathlib.Path, *, limit: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit] + "\\n...<truncated>..."

def strip_comments(text: str) -> str:
    text = re.sub(r"/\\*.*?\\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)

def resolve_filelist_entry(entry: str) -> pathlib.Path:
    raw = entry.strip()
    if raw.startswith("+incdir+"):
        raw = raw[len("+incdir+"):]
    resolved = pathlib.Path(raw)
    if not resolved.is_absolute():
        resolved = builddir / raw
    return resolved.resolve()

def unique_list(values):
    seen = set()
    ordered = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered

def read_log_text(path: pathlib.Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= MAX_LOG_BYTES:
        return text
    return text[-MAX_LOG_BYTES:]

def extract_matching_lines(text, pattern, *, flags=0, limit=MAX_ERROR_EXAMPLES):
    regex = re.compile(pattern, flags)
    matches = []
    for line in text.splitlines():
        if regex.search(line):
            matches.append(line.strip())
        if len(matches) >= limit:
            break
    return unique_list(matches)

def extract_blackbox_modules(text):
    modules = []
    patterns = [
        re.compile(r"(?i)black\\s*box(?:ed)?(?:\\s+module)?\\s+([A-Za-z_][A-Za-z0-9_$]*)"),
        re.compile(r"(?i)module\\s+([A-Za-z_][A-Za-z0-9_$]*)\\s+is treated as a black\\s*box"),
        re.compile(r"(?i)treated as (?:a )?black\\s*box.*?\\b([A-Za-z_][A-Za-z0-9_$]*)\\b"),
        re.compile(r"(?i)unresolved (?:reference|module).*?\\b([A-Za-z_][A-Za-z0-9_$]*)\\b"),
    ]
    for line in text.splitlines():
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                modules.append(match.group(1))
    return unique_list(modules)

def extract_genus_diagnostics(text, *, limit=MAX_ERROR_EXAMPLES):
    diagnostics = []
    lines = text.splitlines()
    index = 0
    while index < len(lines) and len(diagnostics) < limit:
        line = lines[index].rstrip()
        match = re.match(
            r"^\\s*(Info|Warning|Error|Fatal)\\s*:\\s*(.+?)\\s+\\[([A-Z]+-\\d+)\\]\\s*$",
            line,
        )
        if not match:
            index += 1
            continue

        severity, message, code = match.groups()
        detail_parts = []
        detail_index = index + 1
        while detail_index < len(lines):
            detail_line = lines[detail_index].rstrip()
            if re.match(r"^\\s*(Info|Warning|Error|Fatal)\\s*:", detail_line):
                break
            if re.match(r"^\\s+:", detail_line):
                detail_parts.append(detail_line.strip())
                detail_index += 1
                continue
            if not detail_line.strip():
                break
            break

        detail_text = " ".join(
            part.lstrip(":").strip()
            for part in detail_parts
            if part.lstrip(":").strip()
        )
        formatted = f"{{severity}} [{{code}}] {{message.strip()}}"
        if detail_text:
            formatted += f" {{detail_text}}"
        diagnostics.append(
            {{
                "severity": severity,
                "code": code,
                "message": message.strip(),
                "details": detail_text,
                "formatted": formatted,
            }}
        )
        index = detail_index
    return diagnostics

def select_primary_netlist_log(all_files):
    genus_log = None
    for rel_path in all_files:
        normalized = str(rel_path).replace("\\\\", "/")
        if normalized.lower().endswith("genus.log"):
            genus_log = rel_path
            break
    if genus_log:
        return {{
            "primary_log": genus_log,
            "inspected_logs": [genus_log],
            "fallback_used": False,
            "reason": "candidate/genus.log present; using it as the primary truth source.",
        }}

    fallback_logs = [
        rel_path for rel_path in all_files
        if str(rel_path).lower().endswith((".log", ".rpt", ".txt"))
    ]
    preferred_logs = sorted(
        fallback_logs,
        key=lambda item: (
            0 if item.endswith("genus.log") else 1 if "agent_netlist_stdout" in item else 2,
            item,
        ),
    )[:1]
    return {{
        "primary_log": preferred_logs[0] if preferred_logs else None,
        "inspected_logs": preferred_logs,
        "fallback_used": bool(preferred_logs),
        "reason": (
            "candidate/genus.log was missing; using the best available fallback log."
            if preferred_logs
            else "No netlist log/report file was available for classifier input."
        ),
    }}

def classify_genus_logs(
    log_text: str,
    *,
    resolved_top: str = "",
    selected_top_has_generic_interface_ports: bool = False,
    generic_interface_examples: list[str] | None = None,
) -> dict:
    saw_elaborate = bool(re.search(r"(?i)\\belaborate\\b", log_text))
    reached_syn_generic = bool(re.search(r"(?i)\\bsyn_generic\\b", log_text))
    reached_reporting = bool(
        re.search(r"(?i)\\breport_(?:qor|area|timing|power)\\b|\\bwrite_hdl\\b|\\bwrite_design\\b", log_text)
    )
    interactive_prompt = bool(
        re.search(r"(?m)^\\s*(?:genus(?::[^>\\s]+)?|Genus(?::[^>\\s]+)?)>\\s*$", log_text)
    )
    constant_expression_lines = extract_matching_lines(
        log_text,
        r"(?i)constant expression required",
    )
    generic_interface_lines = extract_matching_lines(
        log_text,
        r"(?i)unresolved generic interface|CDFG-435",
    )
    unsynthesizable_process_lines = unique_list(
        [
            *extract_matching_lines(
                log_text,
                r"(?i)unsynthesizable process|CDFG-364",
            ),
            *extract_matching_lines(
                log_text,
                r"(?i)always_ff.*more than one clock|more than one clock|multiple clocks|suspicious event control",
            ),
        ]
    )
    elaborate_error_lines = extract_matching_lines(
        log_text,
        r"(?i)(error|fatal).*elaborate|elaborate.*(error|fatal)",
    )
    config_error_lines = unique_list(
        [
            *extract_matching_lines(log_text, r"(?i)cannot open.*(filelist|sdc|lib|top_module)"),
            *extract_matching_lines(log_text, r"(?i)(top module|design).*not found"),
            *extract_matching_lines(log_text, r"(?i)(undefined|unknown).*(define|macro)"),
            *extract_matching_lines(log_text, r"(?i)GENUS_LIBS"),
        ]
    )
    blackbox_modules = extract_blackbox_modules(log_text)
    blackbox_lines = extract_matching_lines(log_text, r"(?i)black\\s*box|unresolved (?:reference|module)")
    genus_diagnostics = extract_genus_diagnostics(log_text)
    semantic_codes = {{
        "CDFG-286",
        "CDFG-372",
        "CDFG-381",
        "CDFG-476",
        "CDFG-934",
    }}
    semantic_error_lines = unique_list(
        [
            diagnostic["formatted"]
            for diagnostic in genus_diagnostics
            if diagnostic.get("code") in semantic_codes
        ]
    )

    prep_owned_errors = []
    netlist_owned_errors = []
    suggested = []
    summary_parts = []
    detected_patterns = []
    applicable_recipe_ids = []
    chosen_recipe_id = ""
    made_meaningful_progress = reached_syn_generic or reached_reporting

    if generic_interface_lines:
        top_name = resolved_top or "the selected synthesis top"
        example_suffix = ""
        if generic_interface_examples:
            example_suffix = " Examples: " + ", ".join(generic_interface_examples[:4])
        generic_interface_messages = [
            f"Chosen synthesis top '{{top_name}}' exposes generic interface ports that Genus cannot elaborate."
            + example_suffix,
            *[
                "Genus reported unresolved generic interface: " + line
                for line in generic_interface_lines[:MAX_ERROR_EXAMPLES]
            ],
        ]
        if made_meaningful_progress:
            warnings.extend(generic_interface_messages)
            summary_parts.append(
                "Current genus.log reached synthesis progress, so prep-owned generic-interface reroute was suppressed."
            )
        else:
            prep_owned_errors.extend(generic_interface_messages)
            suggested.append(
                "Route back to prep and choose a synthesis-safe wrapper or explicit top override instead of patching RTL architecture in the netlist stage."
            )
            summary_parts.append(
                "The selected top exposes generic interface ports that are not suitable for direct Genus elaboration."
            )
        detected_patterns.append(
            {{
                "pattern_id": "raw_generic_interface_top",
                "subclass": "generic_interface_top",
                "owner": "prep" if not made_meaningful_progress else "warning",
                "evidence": generic_interface_lines[:MAX_ERROR_EXAMPLES],
            }}
        )
        applicable_recipe_ids.append("generic_interface_top_to_impl")

    if blackbox_modules:
        blackbox_message = "Genus reported unresolved blackboxed modules: " + ", ".join(blackbox_modules)
        if made_meaningful_progress:
            warnings.append(
                "Current genus.log reached synthesis progress, so prep-owned blackbox reroute was suppressed: "
                + blackbox_message
            )
            summary_parts.append(
                "Current genus.log reached synthesis progress, so prep-owned blackbox reroute was suppressed."
            )
        else:
            prep_owned_errors.append(blackbox_message)
            suggested.append(
                "Route back to prep and repair the prepared bundle/filelist so every instantiated helper module definition is present."
            )
            summary_parts.append("Blackboxed or unresolved modules point to bundle completeness problems.")

    if config_error_lines:
        config_messages = [
            "Genus reported prep-owned top/define/config issue: " + line
            for line in config_error_lines[:MAX_ERROR_EXAMPLES]
        ]
        if made_meaningful_progress:
            warnings.extend(config_messages)
            summary_parts.append(
                "Current genus.log reached synthesis progress, so prep-owned config reroute was suppressed."
            )
        else:
            prep_owned_errors.extend(config_messages)
            suggested.append(
                "Route back to prep and repair the top selection, define configuration, or generated metadata/Tcl inputs."
            )
            summary_parts.append("Top/define/config diagnostics indicate a prep-owned issue.")

    if constant_expression_lines:
        netlist_owned_errors.extend(
            [
                "Genus constant-expression elaboration error: " + line
                for line in constant_expression_lines[:MAX_ERROR_EXAMPLES]
            ]
        )
        suggested.append(
            "Keep this in the netlist stage and propose only bounded synthesis-oriented RTL edits for the constant-expression usage."
        )
        summary_parts.append("Constant-expression failure is owned by the netlist/elaboration stage.")

    if unsynthesizable_process_lines:
        netlist_owned_errors.extend(
            [
                "Genus unsynthesizable-process error: " + line
                for line in unsynthesizable_process_lines[:MAX_ERROR_EXAMPLES]
            ]
        )
        suggested.append(
            "Keep this in the netlist stage and propose only bounded synthesis-oriented RTL edits for the unsynthesizable process, such as replacing unsupported multi-clock always_ff usage with a synthesis-safe equivalent in the prepared build."
        )
        summary_parts.append(
            "Genus reached elaboration and stopped on an unsynthesizable RTL process."
        )
        detected_patterns.append(
            {{
                "pattern_id": "unsynthesizable_process",
                "subclass": "ddr_behavioral_readwrite_to_clock2x_abstraction"
                if re.search(r"(?i)readwrite|dqs_t|dqs_c", log_text)
                else "unsynthesizable_process",
                "owner": "netlist",
                "evidence": unsynthesizable_process_lines[:MAX_ERROR_EXAMPLES],
            }}
        )
        if re.search(r"(?i)readwrite|dqs_t|dqs_c", log_text):
            applicable_recipe_ids.extend(
                [
                    "ddr_behavioral_readwrite_to_clock2x_abstraction",
                    "memory_controller_impl_bundle",
                ]
            )

    if (
        saw_elaborate
        and elaborate_error_lines
        and not constant_expression_lines
        and not generic_interface_lines
        and not unsynthesizable_process_lines
    ):
        netlist_owned_errors.extend(
            [
                "Genus elaborate failure: " + line
                for line in elaborate_error_lines[:MAX_ERROR_EXAMPLES]
            ]
        )
        suggested.append(
            "Inspect the elaboration diagnostics and keep bounded synthesis-compatibility fixes in the netlist stage."
        )
        summary_parts.append("Elaboration failed before synthesis progressed.")

    semantic_lines = [
        line for line in semantic_error_lines
        if "constant expression required" not in line.lower()
        and "black" not in line.lower()
        and "GENUS_LIBS" not in line
        and "unsynthesizable process" not in line.lower()
        and "cdfg-364" not in line.lower()
    ]
    if (
        not constant_expression_lines
        and not unsynthesizable_process_lines
        and semantic_lines
        and not prep_owned_errors
    ):
        netlist_owned_errors.extend(
            [
                "Genus synthesis-time RTL semantic issue: " + line
                for line in semantic_lines[:MAX_ERROR_EXAMPLES]
            ]
        )
        suggested.append(
            "Keep the failure in the netlist stage and limit edits to bounded synthesis-compatibility changes in existing RTL."
        )
        summary_parts.append("Synthesis found an RTL semantic issue after launch.")

    if interactive_prompt and not reached_syn_generic:
        warnings.append(
            "Genus log appears to drop to an interactive prompt before reaching syn_generic."
        )

    if not reached_syn_generic and (
        constant_expression_lines or unsynthesizable_process_lines or elaborate_error_lines
    ):
        warnings.append("The run never reached syn_generic.")

    prep_like_blocking = (
        not made_meaningful_progress
        and bool(generic_interface_lines or blackbox_modules or config_error_lines)
    )

    if not prep_owned_errors and not netlist_owned_errors:
        failure_class = "non_fatal_warning_only_issue"
        owner = "netlist"
        route = None
        summary_parts.append("No blocking Genus log classifier findings were detected.")
    elif prep_like_blocking and generic_interface_lines:
        failure_class = "generic_interface_top"
        owner = "prep"
        route = "rtl_prep"
        chosen_recipe_id = "generic_interface_top_to_impl"
        summary_parts.append(
            "Current genus.log failed before syn_generic with unresolved generic interface, so the run was routed back to prep."
        )
    elif prep_like_blocking and prep_owned_errors and netlist_owned_errors:
        failure_class = "mixed_prep_dependency_and_netlist_semantic"
        owner = "prep"
        route = "rtl_prep"
        summary_parts.append(
            "Prep completeness defects were detected alongside netlist/elaboration defects; repair prep first, then rerun netlist."
        )
    elif prep_like_blocking and prep_owned_errors:
        owner = "prep"
        route = "rtl_prep"
        if generic_interface_lines:
            failure_class = "generic_interface_top"
        elif blackbox_modules:
            failure_class = "missing_dependency_blackbox_problem"
        else:
            failure_class = "top_define_or_config_problem"
    elif unsynthesizable_process_lines:
        owner = "netlist"
        route = "netlist"
        failure_class = "unsynthesizable_process"
        if "memory_controller_impl_bundle" in applicable_recipe_ids:
            chosen_recipe_id = "memory_controller_impl_bundle"
    elif constant_expression_lines:
        owner = "netlist"
        route = "netlist"
        failure_class = "genus_constant_expression_elaboration_problem"
    else:
        owner = "netlist"
        route = "netlist"
        failure_class = "synthesis_time_rtl_semantic_issue"

    return {{
        "failure_stage_owner": owner,
        "failure_class": failure_class,
        "route_back_to_stage": route,
        "prep_owned_errors": unique_list(prep_owned_errors),
        "netlist_owned_errors": unique_list(netlist_owned_errors),
        "blocking_errors": unique_list([*prep_owned_errors, *netlist_owned_errors]),
        "warning_lines": unique_list([*blackbox_lines[:MAX_ERROR_EXAMPLES]]),
        "detected_patterns": detected_patterns,
        "applicable_recipe_ids": unique_list(applicable_recipe_ids),
        "chosen_recipe_id": chosen_recipe_id or None,
        "summary": " ".join(summary_parts).strip(),
        "signals": {{
            "reached_syn_generic": reached_syn_generic,
            "reached_reporting": reached_reporting,
            "made_meaningful_progress": made_meaningful_progress,
            "saw_elaborate": saw_elaborate,
            "interactive_prompt_fallthrough": interactive_prompt,
            "blackbox_modules": blackbox_modules,
            "blackbox_lines": blackbox_lines[:MAX_ERROR_EXAMPLES],
            "generic_interface_lines": generic_interface_lines[:MAX_ERROR_EXAMPLES],
            "unsynthesizable_process_lines": unsynthesizable_process_lines[:MAX_ERROR_EXAMPLES],
            "constant_expression_lines": constant_expression_lines[:MAX_ERROR_EXAMPLES],
            "elaborate_error_lines": elaborate_error_lines[:MAX_ERROR_EXAMPLES],
            "config_error_lines": config_error_lines[:MAX_ERROR_EXAMPLES],
            "semantic_error_lines": semantic_lines[:MAX_ERROR_EXAMPLES],
            "semantic_diagnostics": semantic_lines[:MAX_ERROR_EXAMPLES],
        }},
        "suggested_fixes": unique_list(suggested),
    }}

if not builddir.exists():
    message = f"Prepared build directory does not exist: {{builddir}}"
    errors.append(message)
    prep_contract_errors.append(message)
elif not builddir.is_dir():
    message = f"Prepared build directory is not a directory: {{builddir}}"
    errors.append(message)
    prep_contract_errors.append(message)
else:
    for rel_path in required_prep_files:
        full_path = builddir / rel_path
        context["prep_artifact_status"][rel_path] = full_path.exists()
        if not full_path.exists():
            message = f"Missing required prep artifact for netlist stage: {{rel_path}}"
            errors.append(message)
            prep_contract_errors.append(message)
            suggested_fixes.append(f"Repair or regenerate prep outputs so {{rel_path}} exists before rerunning netlist.")
        elif full_path.is_file() and not full_path.read_text(encoding="utf-8", errors="replace").strip():
            message = f"Prep artifact is empty: {{rel_path}}"
            errors.append(message)
            prep_contract_errors.append(message)
            suggested_fixes.append(f"Populate {{rel_path}} with valid content before rerunning netlist.")

    if rules_text:
        required_phrases = [
            "filelist_genus.f",
            "run_genus_mc.tcl",
            "top_module.txt",
            "constraints/mc_genus.sdc",
        ]
        missing_rule_phrases = [
            phrase for phrase in required_phrases if phrase not in rules_text
        ]
        if missing_rule_phrases:
            warnings.append(
                "Universal netlist rules file is missing expected references: "
                + ", ".join(missing_rule_phrases)
            )

    for rel_path in key_file_candidates[:MAX_KEY_FILE_COUNT]:
        full_path = builddir / rel_path
        if full_path.exists() and full_path.is_file():
            context["key_files"][rel_path] = read_text_snippet(
                full_path,
                limit=MAX_KEY_FILE_CHARS,
            )

    def find_matching_paren(text: str, start_index: int) -> int:
        depth = 0
        for index in range(start_index, len(text)):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
        return -1

    def extract_module_port_list(text: str, module_name: str) -> str:
        cleaned = re.sub(r"/\\*.*?\\*/", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"//.*", "", cleaned)
        match = re.search(
            rf"(?m)^\\s*module\\s+{{re.escape(module_name)}}\\b",
            cleaned,
        )
        if not match:
            return ""
        cursor = match.end()
        while cursor < len(cleaned) and cleaned[cursor].isspace():
            cursor += 1
        if cursor < len(cleaned) and cleaned[cursor] == "#":
            param_start = cleaned.find("(", cursor)
            if param_start == -1:
                return ""
            param_end = find_matching_paren(cleaned, param_start)
            if param_end == -1:
                return ""
            cursor = param_end + 1
            while cursor < len(cleaned) and cleaned[cursor].isspace():
                cursor += 1
        if cursor >= len(cleaned) or cleaned[cursor] != "(":
            return ""
        port_end = find_matching_paren(cleaned, cursor)
        if port_end == -1:
            return ""
        return cleaned[cursor + 1 : port_end]

    def parse_interface_definitions(text: str):
        cleaned = strip_comments(text)
        return re.findall(r"(?m)^\\s*interface\\s+([A-Za-z_][A-Za-z0-9_$]*)\\b", cleaned)

    def detect_generic_interface_ports(port_text: str, known_interface_names=None):
        if not port_text:
            return []
        known_interface_names = sorted(
            {{
                str(item).strip()
                for item in (known_interface_names or [])
                if str(item).strip()
            }},
            key=len,
            reverse=True,
        )
        matches = []
        seen = set()
        for match in re.finditer(r"(?im)(?:^|,)\\s*(interface\\b[^\\n;]*)", port_text):
            snippet = re.sub(r"\\s+", " ", match.group(1)).strip()
            if snippet and snippet not in seen:
                seen.add(snippet)
                matches.append(snippet)
        for interface_name in known_interface_names:
            pattern = re.compile(
                rf"(?im)(?:^|,)\\s*({{re.escape(interface_name)}}(?:\\s*\\.\\s*[A-Za-z_][A-Za-z0-9_$]*)?\\s+[^,;\\n]+)"
            )
            for match in pattern.finditer(port_text):
                snippet = re.sub(r"\\s+", " ", match.group(1)).strip()
                modport_match = re.match(
                    rf"^{{re.escape(interface_name)}}(?:\\s*\\.\\s*([A-Za-z_][A-Za-z0-9_$]*))?",
                    snippet,
                    flags=re.IGNORECASE,
                )
                if modport_match and str(modport_match.group(1) or "").strip():
                    continue
                if snippet and snippet not in seen:
                    seen.add(snippet)
                    matches.append(snippet)
        return matches

    top_module_path = builddir / "top_module.txt"
    top_module = ""
    generic_interface_examples = []
    if top_module_path.exists():
        top_module = top_module_path.read_text(encoding="utf-8", errors="replace").strip()
        context["resolved_top"] = top_module
        if not top_module:
            message = "top_module.txt is empty for the netlist stage."
            errors.append(message)
            prep_contract_errors.append(message)
            suggested_fixes.append("Write the intended synthesis top into top_module.txt or pass an explicit top.")

    filelist_path = builddir / "filelist_genus.f"
    source_entries = []
    if filelist_path.exists():
        filelist_entries = [
            line.strip()
            for line in filelist_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        source_entries = [
            entry for entry in filelist_entries
            if not entry.startswith("+incdir+")
        ]
        if not source_entries:
            message = "filelist_genus.f does not list any RTL source entries."
            errors.append(message)
            prep_contract_errors.append(message)
            suggested_fixes.append("Regenerate filelist_genus.f so it lists the prepared RTL sources.")

        for entry in source_entries[:400]:
            resolved_entry = resolve_filelist_entry(entry)
            try:
                resolved_entry.relative_to(builddir.resolve())
            except ValueError:
                pass
            if not resolved_entry.exists():
                message = f"filelist_genus.f references a missing source file: {{entry}}"
                errors.append(message)
                prep_contract_errors.append(message)
                suggested_fixes.append(f"Repair filelist_genus.f so the entry '{{entry}}' resolves inside the prepared bundle.")

    known_interface_names = set()
    for entry in source_entries[:400]:
        resolved_entry = resolve_filelist_entry(entry)
        if not resolved_entry.exists() or resolved_entry.suffix.lower() not in (".sv", ".v", ".svh", ".vh"):
            continue
        source_text = resolved_entry.read_text(encoding="utf-8", errors="replace")
        for interface_name in parse_interface_definitions(source_text):
            known_interface_names.add(interface_name)

    if top_module and source_entries:
        for entry in source_entries[:400]:
            resolved_entry = resolve_filelist_entry(entry)
            if not resolved_entry.exists() or resolved_entry.suffix.lower() not in (".sv", ".v", ".svh", ".vh"):
                continue
            source_text = resolved_entry.read_text(encoding="utf-8", errors="replace")
            if re.search(rf"(?m)^\\s*module\\s+{{re.escape(top_module)}}\\b", source_text):
                generic_interface_examples = detect_generic_interface_ports(
                    extract_module_port_list(source_text, top_module),
                    known_interface_names,
                )
                context["synthesis_top_checks"] = {{
                    "selected_top_module": top_module,
                    "selected_top_path": str(resolved_entry.relative_to(builddir.resolve())),
                    "selected_top_has_generic_interface_ports": bool(generic_interface_examples),
                    "generic_interface_port_examples": generic_interface_examples[:8],
                }}
                break

    run_tcl_path = builddir / "run_genus_mc.tcl"
    if run_tcl_path.exists():
        run_tcl_text = run_tcl_path.read_text(encoding="utf-8", errors="replace")
        if "filelist_genus.f" not in run_tcl_text:
            message = "run_genus_mc.tcl does not reference filelist_genus.f."
            errors.append(message)
            prep_contract_errors.append(message)
            suggested_fixes.append("Update run_genus_mc.tcl so it reads filelist_genus.f.")
        if top_module and top_module not in run_tcl_text:
            warnings.append(
                f"run_genus_mc.tcl does not mention the resolved top module '{{top_module}}'."
            )

if not candidate.exists():
    errors.append(f"Netlist output directory does not exist: {{candidate}}")
elif not candidate.is_dir():
    errors.append(f"Netlist output path is not a directory: {{candidate}}")
else:
    all_files = sorted(
        str(path.relative_to(candidate))
        for path in candidate.rglob("*")
        if path.is_file()
    )
    context["output_inventory"] = {{
        "total_files": len(all_files),
        "files": all_files[:MAX_CONTEXT_FILES],
        "truncated": len(all_files) > MAX_CONTEXT_FILES,
    }}

    detected_outputs = []
    detected_logs = []
    for rel_path in all_files:
        if any(fnmatch.fnmatch(rel_path, pattern) for pattern in netlist_output_patterns):
            detected_outputs.append(rel_path)
        if any(fnmatch.fnmatch(rel_path, pattern) for pattern in log_patterns):
            detected_logs.append(rel_path)

    context["detected_outputs"] = detected_outputs[:MAX_CONTEXT_FILES]

    if not detected_outputs:
        errors.append(
            "Netlist output directory does not contain an expected synthesized artifact (*.v, *.sv, *.vg, *.db, or *_generic.sdc)."
        )
        suggested_fixes.append(
            "Inspect the Genus run logs and prep bundle, then rerun netlist after fixing the missing output artifact cause."
        )

    if not detected_logs:
        warnings.append("Netlist output directory does not contain a saved log/report file; relying on captured command logs only.")

    for rel_path in detected_logs[:3]:
        full_path = candidate / rel_path
        if full_path.is_file():
            context["key_files"][f"output:{{rel_path}}"] = read_text_snippet(
                full_path,
                limit=MAX_KEY_FILE_CHARS,
            )

    log_selection = select_primary_netlist_log(all_files)
    primary_log = log_selection.get("primary_log")
    inspected_logs = [
        str(item) for item in log_selection.get("inspected_logs", []) if str(item).strip()
    ]
    classifier_input = ""
    if primary_log:
        full_path = candidate / primary_log
        if full_path.is_file():
            classifier_input = read_log_text(full_path)

    classifier = classify_genus_logs(
        classifier_input,
        resolved_top=top_module,
        selected_top_has_generic_interface_ports=bool(generic_interface_examples),
        generic_interface_examples=generic_interface_examples,
    )
    errors.extend(classifier["blocking_errors"])
    warnings.extend(classifier["warning_lines"])
    warnings.extend(
        [
            "Netlist run did not reach syn_generic."
        ]
        if not classifier["signals"]["reached_syn_generic"]
        else []
    )
    suggested_fixes.extend(classifier["suggested_fixes"])
    result["failure_stage_owner"] = classifier["failure_stage_owner"]
    result["failure_class"] = classifier["failure_class"]
    result["route_back_to_stage"] = classifier["route_back_to_stage"]
    result["detected_patterns"] = classifier.get("detected_patterns", [])
    result["applicable_recipe_ids"] = classifier.get("applicable_recipe_ids", [])
    result["chosen_recipe_id"] = classifier.get("chosen_recipe_id")
    context["log_classification"] = {{
        **classifier["signals"],
        "primary_log": primary_log,
        "inspected_logs": inspected_logs,
        "fallback_used": bool(log_selection.get("fallback_used")),
        "selection_reason": str(log_selection.get("reason", "")).strip(),
    }}

errors = unique_list(errors)
warnings = unique_list(warnings)
suggested_fixes = unique_list(suggested_fixes)
if prep_contract_errors:
    result["failure_stage_owner"] = "prep"
    result["failure_class"] = "prep_bundle_completeness_problem"
    result["route_back_to_stage"] = "rtl_prep"
result["errors"] = errors
result["blocking_errors"] = errors
result["warnings"] = warnings
result["suggested_fixes"] = suggested_fixes
result["pass"] = not errors
result["summary"] = (
    (
        " ".join(
            part
            for part in [
                classifier.get("summary", "") if "classifier" in locals() else "",
                f"Local netlist review {{'passed' if result['pass'] else 'failed'}} with {{len(errors)}} errors and {{len(warnings)}} warnings.",
            ]
            if part
        ).strip()
    )
)
print(json.dumps(result))
"""

    return f"{remote_python} -c {shlex.quote(review_script)}"


def _build_netlist_edit_command(
    state: FlowState,
    builddir: str,
    actions: list[dict[str, str]],
) -> str:
    remote_python = _remote_python(state)
    actions_json = json.dumps(actions)
    edit_script = f"""
import json
import pathlib

candidate = pathlib.Path({builddir!r}).resolve()
actions = json.loads({actions_json!r})

result = {{
    "applied_actions": [],
    "skipped_actions": [],
    "summary": "",
}}

candidate_name = candidate.name

def normalize_rel_path(raw_path: str) -> str:
    normalized = raw_path.strip().replace("\\\\", "/").lstrip("./")
    if normalized == candidate_name:
        return "."
    prefix = candidate_name + "/"
    if normalized.startswith(prefix):
        return normalized[len(prefix):]
    return normalized

def resolve_target(rel_path: str) -> pathlib.Path:
    normalized_rel_path = normalize_rel_path(rel_path)
    target = (candidate / normalized_rel_path).resolve()
    target.relative_to(candidate)
    return target

def is_filelist_target(target: pathlib.Path) -> bool:
    return target.name == "filelist_genus.f"

def is_allowed_filelist_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return True
    if stripped.startswith("+incdir+"):
        return True
    if stripped.startswith("+"):
        return False
    return True

def sanitize_filelist_text(text: str) -> str:
    if not text:
        return text
    output_lines = []
    seen_entries = set()
    changed = False
    for line in text.splitlines():
        stripped = line.strip()
        if not is_allowed_filelist_line(line):
            changed = True
            continue
        if not stripped or stripped.startswith("#"):
            output_lines.append(line)
            continue
        normalized = stripped.replace("\\\\", "/")
        if normalized in seen_entries:
            changed = True
            continue
        seen_entries.add(normalized)
        output_lines.append(line)
    if not changed:
        return text
    sanitized = "\\n".join(output_lines)
    if text.endswith("\\n"):
        sanitized += "\\n"
    return sanitized

for action in actions:
    action_type = str(action.get("type", "")).strip()
    rel_path = str(action.get("path", "")).strip()
    reason = str(action.get("reason", "")).strip()
    search_text = str(action.get("search_text", ""))
    replacement_text = str(action.get("replacement_text", ""))
    line_text = str(action.get("line_text", ""))
    if action_type == "remove_line" and not line_text and search_text:
        line_text = search_text.rstrip("\\n")

    if not action_type or not rel_path:
        result["skipped_actions"].append({{
            "action": action,
            "reason": "missing type or path",
        }})
        continue

    try:
        target = resolve_target(rel_path)
    except Exception as exc:
        result["skipped_actions"].append({{
            "action": action,
            "reason": f"invalid target path: {{exc}}",
        }})
        continue

    if action_type == "remove_file":
        if not target.exists():
            result["skipped_actions"].append({{
                "action": action,
                "reason": "target file does not exist",
            }})
            continue
        if target.is_dir():
            result["skipped_actions"].append({{
                "action": action,
                "reason": "remove_file only supports files",
            }})
            continue
        target.unlink()
        result["applied_actions"].append({{
            "type": action_type,
            "path": rel_path,
            "reason": reason,
        }})
        continue

    if not target.exists() or target.is_dir():
        result["skipped_actions"].append({{
            "action": action,
            "reason": "target text file does not exist",
        }})
        continue

    text = target.read_text(encoding="utf-8", errors="replace")

    if action_type == "remove_line":
        original_lines = text.splitlines()
        filtered_lines = [line for line in original_lines if line != line_text]
        if len(filtered_lines) == len(original_lines):
            result["skipped_actions"].append({{
                "action": action,
                "reason": "line_text not found",
            }})
            continue
        trailing_newline = text.endswith("\\n")
        new_text = "\\n".join(filtered_lines)
        if trailing_newline:
            new_text += "\\n"
        if is_filelist_target(target):
            new_text = sanitize_filelist_text(new_text)
        target.write_text(new_text, encoding="utf-8")
        result["applied_actions"].append({{
            "type": action_type,
            "path": rel_path,
            "reason": reason,
        }})
        continue

    if action_type == "replace_text":
        if search_text not in text:
            result["skipped_actions"].append({{
                "action": action,
                "reason": "search_text not found",
            }})
            continue
        new_text = text.replace(search_text, replacement_text)
        if is_filelist_target(target):
            new_text = sanitize_filelist_text(new_text)
        target.write_text(new_text, encoding="utf-8")
        result["applied_actions"].append({{
            "type": action_type,
            "path": rel_path,
            "reason": reason,
        }})
        continue

    if action_type == "append_text":
        append_payload = replacement_text or line_text
        new_text = text + append_payload
        if is_filelist_target(target):
            new_text = sanitize_filelist_text(new_text)
            if new_text == text:
                result["skipped_actions"].append({{
                    "action": action,
                    "reason": "append_text introduced no allowed filelist changes",
                }})
                continue
        target.write_text(new_text, encoding="utf-8")
        result["applied_actions"].append({{
            "type": action_type,
            "path": rel_path,
            "reason": reason,
        }})
        continue

    result["skipped_actions"].append({{
        "action": action,
        "reason": f"unsupported action type: {{action_type}}",
    }})

result["summary"] = (
    f"Applied {{len(result['applied_actions'])}} actions; "
    f"skipped {{len(result['skipped_actions'])}} actions."
)
print(json.dumps(result))
"""

    return f"{remote_python} -c {shlex.quote(edit_script)}"


def _build_mapped_command(state: FlowState) -> str:
    prep_dir, _ = resolve_prepare_dir(
        state.get("prep_candidate_path"),
        state.get("remote_prepare_dir"),
    )
    builddir, builddir_source = resolve_netlist_dir_choice(
        state.get("netlist_candidate_path") or state.get("remote_netlist_dir"),
        prep_dir,
    )
    outdir, outdir_source = resolve_mapped_dir_choice(
        state.get("remote_mapped_dir"),
        builddir,
        prep_dir,
    )
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    mapped_script = str(state.get("remote_mapped_script", DEFAULT_MAPPED_SCRIPT)).strip()
    if not mapped_script:
        mapped_script = DEFAULT_MAPPED_SCRIPT
    remote_python = _remote_python(state)
    heartbeat_sec = max(15, int(state.get("mapped_heartbeat_sec", 60)))
    configured_genus_bin = str(state.get("remote_genus_bin") or "").strip()
    fallback_genus_bins = [
        configured_genus_bin,
        "/opt/coe/cadence/DDI231/bin/genus",
    ]
    unique_genus_bins: list[str] = []
    for candidate in fallback_genus_bins:
        if candidate and candidate not in unique_genus_bins:
            unique_genus_bins.append(candidate)
    candidate_list = " ".join(shlex.quote(path) for path in unique_genus_bins)
    mapped_top = (
        str(state.get("mapped_top_module") or "").strip()
        or str(state.get("netlist_resolved_top") or "").strip()
        or f"{state.get('top_module', 'MemoryController')}_impl"
    )

    inner_cmd = (
        "set -e; "
        f"cd {shlex.quote(project_root)}; "
        f"OUTDIR={shlex.quote(outdir)}; "
        f"BUILDDIR={shlex.quote(builddir)}; "
        f"MAPPED_SCRIPT={shlex.quote(mapped_script)}; "
        f"MAPPED_HEARTBEAT_SEC={heartbeat_sec}; "
        f"MAPPED_TOP={shlex.quote(mapped_top)}; "
        "echo 'HOST:'; hostname; "
        "echo 'PATH:'; echo $PATH; "
        "rm -rf \"$OUTDIR\"; "
        "mkdir -p \"$OUTDIR\"; "
        "EDA_MAPPED_PROGRESS_LOG=\"$OUTDIR/agent_mapped_progress.log\"; "
        "EDA_MAPPED_STDOUT_LOG=\"$OUTDIR/agent_mapped_stdout.log\"; "
        "EDA_MAPPED_STDERR_LOG=\"$OUTDIR/agent_mapped_stderr.log\"; "
        "touch \"$EDA_MAPPED_PROGRESS_LOG\" \"$EDA_MAPPED_STDOUT_LOG\" \"$EDA_MAPPED_STDERR_LOG\"; "
        "echo \"EDA_MAPPED_LOG_DIR:$OUTDIR\"; "
        "echo \"EDA_MAPPED_BUILDDIR:$BUILDDIR\"; "
        f"echo \"EDA_MAPPED_BUILDDIR_SOURCE:{shlex.quote(builddir_source)}\"; "
        "echo \"EDA_MAPPED_OUTDIR:$OUTDIR\"; "
        f"echo \"EDA_MAPPED_OUTDIR_SOURCE:{shlex.quote(outdir_source)}\"; "
        "echo \"EDA_MAPPED_TOP:$MAPPED_TOP\"; "
        "printf '[%s] mapped launcher started\\n' \"$(date -Is)\" | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        "printf '[%s] builddir=%s\\n' \"$(date -Is)\" \"$BUILDDIR\" | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        f"printf '[%s] builddir_source=%s\\n' \"$(date -Is)\" {shlex.quote(builddir_source)} | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        "printf '[%s] outdir=%s\\n' \"$(date -Is)\" \"$OUTDIR\" | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        f"printf '[%s] outdir_source=%s\\n' \"$(date -Is)\" {shlex.quote(outdir_source)} | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        "printf '[%s] mapped_script=%s\\n' \"$(date -Is)\" \"$MAPPED_SCRIPT\" | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        "printf '[%s] mapped_top=%s\\n' \"$(date -Is)\" \"$MAPPED_TOP\" | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        "echo 'GENUS_CHECK:'; "
        "EDA_RESOLVED_GENUS_BIN=''; "
        f"for candidate in {candidate_list}; do "
        "if [ -n \"$candidate\" ] && [ -x \"$candidate\" ]; then "
        "EDA_RESOLVED_GENUS_BIN=\"$candidate\"; break; "
        "fi; "
        "done; "
        "if [ -z \"$EDA_RESOLVED_GENUS_BIN\" ] && "
        "[ -n \"${EDA_GENUS_BIN:-}\" ] && [ -x \"${EDA_GENUS_BIN}\" ]; then "
        "EDA_RESOLVED_GENUS_BIN=\"${EDA_GENUS_BIN}\"; "
        "fi; "
        "if [ -z \"$EDA_RESOLVED_GENUS_BIN\" ] && "
        "[ -n \"${GENUS_BIN:-}\" ] && [ -x \"${GENUS_BIN}\" ]; then "
        "EDA_RESOLVED_GENUS_BIN=\"${GENUS_BIN}\"; "
        "fi; "
        "if [ -z \"$EDA_RESOLVED_GENUS_BIN\" ]; then "
        "EDA_RESOLVED_GENUS_BIN=$(command -v genus 2>/dev/null || true); "
        "fi; "
        "echo \"$EDA_RESOLVED_GENUS_BIN\"; "
        "if [ -z \"$EDA_RESOLVED_GENUS_BIN\" ] || [ ! -x \"$EDA_RESOLVED_GENUS_BIN\" ]; then "
        "echo 'EDA_MAPPED_ENV_ERROR:genus_executable_not_found' | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        "exit 97; "
        "fi; "
        "echo \"EDA_MAPPED_GENUS_BIN:${EDA_RESOLVED_GENUS_BIN}\" | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        "echo 'GENUS_OK'; "
        "set +e; "
        "( "
        "while true; do "
        "printf '[%s] mapped still running\\n' \"$(date -Is)\" | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        "sleep \"$MAPPED_HEARTBEAT_SEC\"; "
        "done "
        ") & "
        "EDA_MAPPED_HEARTBEAT_PID=$!; "
        f"{remote_python} \"$MAPPED_SCRIPT\" "
        "--netlistdir \"$BUILDDIR\" "
        "--genus \"$EDA_RESOLVED_GENUS_BIN\" "
        "--top \"$MAPPED_TOP\" "
        "--outdir \"$OUTDIR\" "
        "> >(tee -a \"$EDA_MAPPED_STDOUT_LOG\") "
        "2> >(tee -a \"$EDA_MAPPED_STDERR_LOG\" >&2); "
        "MAPPED_RC=$?; "
        "kill \"$EDA_MAPPED_HEARTBEAT_PID\" >/dev/null 2>&1 || true; "
        "wait \"$EDA_MAPPED_HEARTBEAT_PID\" 2>/dev/null || true; "
        "set -e; "
        "printf '[%s] mapped exited rc=%s\\n' \"$(date -Is)\" \"$MAPPED_RC\" | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        "echo \"EDA_MAPPED_RUN_COMPLETE:${MAPPED_RC}\" | tee -a \"$EDA_MAPPED_PROGRESS_LOG\"; "
        "exit \"$MAPPED_RC\""
    )

    return _build_inner_ssh_command(state, inner_cmd)


def _build_mapped_review_command(
    state: FlowState,
    builddir: str,
    candidate_path: str,
) -> str:
    rules_text, preflight_errors = _load_rules_text(
        state.get("mapped_rules_path"),
        label="Universal mapped",
    )
    rules_path = state.get("mapped_rules_path") or ""
    remote_python = _remote_python(state)

    review_script = f"""
import fnmatch
import json
import pathlib
import re

builddir = pathlib.Path({builddir!r})
candidate = pathlib.Path({candidate_path!r})
rules_path = {rules_path!r}
rules_text = {rules_text!r}
preflight_errors = {preflight_errors!r}
MAX_CONTEXT_FILES = 400
MAX_KEY_FILE_CHARS = 3000
MAX_LOG_BYTES = 250000
MAX_ERROR_EXAMPLES = 12

result = {{
    "pass": False,
    "errors": list(preflight_errors),
    "blocking_errors": list(preflight_errors),
    "warnings": [],
    "suggested_fixes": [],
    "failure_stage_owner": "mapped_netlist",
    "failure_class": "mapped_output_validation_failure",
    "route_back_to_stage": "mapped_netlist",
    "summary": "",
    "rules_path": rules_path,
    "candidate_path": str(candidate),
    "context": {{
        "input_builddir": str(builddir),
        "resolved_top": "",
        "input_inventory": {{}},
        "output_inventory": {{}},
        "key_files": {{}},
        "detected_outputs": [],
        "log_classification": {{}},
    }},
}}

errors = result["blocking_errors"]
warnings = result["warnings"]
suggested_fixes = result["suggested_fixes"]
context = result["context"]
input_contract_errors = []
generic_input_patterns = [
    "*_generic.v",
    "*_generic.sdc",
    "*.db",
]
mapped_output_patterns = [
    "*_mapped.v",
    "*_mapped.sdc",
    "*.db",
    "*.vg",
]
log_patterns = ["*.log", "*.rpt", "*.txt"]

def unique_list(values):
    seen = set()
    ordered = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered

def read_text_snippet(path: pathlib.Path, *, limit: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit] + "\\n...<truncated>..."

def read_log_text(path: pathlib.Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= MAX_LOG_BYTES:
        return text
    return text[-MAX_LOG_BYTES:]

def extract_matching_lines(text, pattern, *, flags=0, limit=MAX_ERROR_EXAMPLES):
    regex = re.compile(pattern, flags)
    matches = []
    for line in text.splitlines():
        if regex.search(line):
            matches.append(line.strip())
        if len(matches) >= limit:
            break
    return unique_list(matches)

def extract_genus_diagnostics(text, *, limit=MAX_ERROR_EXAMPLES):
    diagnostics = []
    lines = text.splitlines()
    index = 0
    while index < len(lines) and len(diagnostics) < limit:
        line = lines[index].rstrip()
        match = re.match(
            r"^\\s*(Info|Warning|Error|Fatal)\\s*:\\s*(.+?)\\s+\\[([A-Z]+-\\d+)\\]\\s*$",
            line,
        )
        if not match:
            index += 1
            continue

        severity, message, code = match.groups()
        detail_parts = []
        detail_index = index + 1
        while detail_index < len(lines):
            detail_line = lines[detail_index].rstrip()
            if re.match(r"^\\s*(Info|Warning|Error|Fatal)\\s*:", detail_line):
                break
            if re.match(r"^\\s+:", detail_line):
                detail_parts.append(detail_line.strip())
                detail_index += 1
                continue
            if not detail_line.strip():
                break
            break

        detail_text = " ".join(
            part.lstrip(":").strip()
            for part in detail_parts
            if part.lstrip(":").strip()
        )
        formatted = f"{{severity}} [{{code}}] {{message.strip()}}"
        if detail_text:
            formatted += f" {{detail_text}}"
        diagnostics.append({{
            "severity": severity,
            "code": code,
            "message": message.strip(),
            "details": detail_text,
            "formatted": formatted,
        }})
        index = detail_index
    return diagnostics

def select_primary_mapped_log(all_files):
    mapped_log = None
    for rel_path in all_files:
        normalized = str(rel_path).replace("\\\\", "/")
        if normalized.lower().endswith("genus.log"):
            mapped_log = rel_path
            break
    if mapped_log:
        return {{
            "primary_log": mapped_log,
            "inspected_logs": [mapped_log],
            "fallback_used": False,
            "reason": "candidate/genus.log present; using it as the primary truth source.",
        }}

    fallback_logs = [
        rel_path for rel_path in all_files
        if str(rel_path).lower().endswith((".log", ".rpt", ".txt"))
    ]
    preferred_logs = sorted(
        fallback_logs,
        key=lambda item: (
            0 if item.endswith("genus.log") else 1 if "agent_mapped_stdout" in item else 2,
            item,
        ),
    )[:1]
    return {{
        "primary_log": preferred_logs[0] if preferred_logs else None,
        "inspected_logs": preferred_logs,
        "fallback_used": bool(preferred_logs),
        "reason": (
            "candidate/genus.log was missing; using the best available fallback log."
            if preferred_logs
            else "No mapped log/report file was available for classifier input."
        ),
    }}

def classify_mapped_logs(log_text: str, *, resolved_top: str = "") -> dict:
    reached_mapping = bool(re.search(r"(?i)\\bsyn_map\\b|\\bmap_opt\\b|\\bmapping\\b", log_text))
    reached_reporting = bool(
        re.search(r"(?i)\\breport_(?:qor|area|timing|power)\\b|\\bwrite_hdl\\b|\\bwrite_design\\b", log_text)
    )
    made_meaningful_progress = reached_mapping or reached_reporting
    genus_diagnostics = extract_genus_diagnostics(log_text)
    def is_ignorable_library_info(diagnostic):
        severity = str(diagnostic.get("severity", "")).strip()
        code = str(diagnostic.get("code", "")).strip().upper()
        message = str(diagnostic.get("message", "")).strip()
        details = str(diagnostic.get("details", "")).strip()
        combined = " ".join(part for part in [message, details] if part).lower()
        if severity != "Info":
            return False
        if "ignoring" not in combined:
            return False
        if code == "LBR-40":
            return True
        return (
            "unknown liberty attribute" in combined
            or "unsupported liberty" in combined
            or "liberty" in combined and "attribute" in combined
        )

    ignorable_library_info_lines = unique_list(
        [
            diagnostic["formatted"]
            for diagnostic in genus_diagnostics
            if is_ignorable_library_info(diagnostic)
        ]
    )
    config_error_lines = unique_list(
        [
            *extract_matching_lines(log_text, r"(?i)cannot open.*(netlist|sdc|lib|db|top)"),
            *extract_matching_lines(log_text, r"(?i)(top module|design).*not found"),
            *extract_matching_lines(
                log_text,
                r"(?i)(can't read|no such variable|undefined variable).*(?:env\\(|genus_|lib|db|sdc|top|path|script|file|variable)",
            ),
            *extract_matching_lines(log_text, r"(?i)GENUS_LIBS"),
        ]
    )
    blocking_diagnostics = [
        diagnostic["formatted"]
        for diagnostic in genus_diagnostics
        if diagnostic.get("severity") in ("Error", "Fatal")
    ]
    database141_lines = [
        diagnostic["formatted"]
        for diagnostic in genus_diagnostics
        if diagnostic.get("code") == "DATABASE-141"
    ]

    mapped_owned_errors = []
    warning_lines = []
    suggested = []
    summary_parts = []

    if ignorable_library_info_lines:
        warning_lines.extend(
            [
                "Non-blocking Liberty compatibility info: " + line
                for line in ignorable_library_info_lines[:MAX_ERROR_EXAMPLES]
            ]
        )
        summary_parts.append(
            "Benign Liberty parser info diagnostics that Genus explicitly ignored were downgraded to warnings."
        )

    if config_error_lines:
        mapped_owned_errors.extend(
            [
                "Mapped-stage configuration issue: " + line
                for line in config_error_lines[:MAX_ERROR_EXAMPLES]
            ]
        )
        suggested.append(
            "Keep the failure in the mapped stage and repair the mapped launch configuration or input metadata."
        )
        summary_parts.append("Mapped-stage configuration diagnostics were detected after launch.")

    if blocking_diagnostics:
        mapped_owned_errors.extend(
            [
                "Mapped-stage synthesis issue: " + line
                for line in blocking_diagnostics[:MAX_ERROR_EXAMPLES]
            ]
        )
        suggested.append(
            "Keep the failure in the mapped stage and limit edits to bounded mapped-flow compatibility changes."
        )
        summary_parts.append("Mapped-stage logs contain blocking Genus diagnostics.")

    if database141_lines:
        suggested.append(
            "If the only blocker is DATABASE-141 during write_db -common, prefer a bounded Tcl edit that saves a synthesis-only database instead."
        )

    if not mapped_owned_errors:
        failure_class = "non_fatal_warning_only_issue"
        owner = "mapped_netlist"
        route = None
        summary_parts.append("No blocking mapped-stage log classifier findings were detected.")
    else:
        failure_class = "mapped_stage_execution_problem"
        owner = "mapped_netlist"
        route = "mapped_netlist"

    return {{
        "failure_stage_owner": owner,
        "failure_class": failure_class,
        "route_back_to_stage": route,
        "blocking_errors": unique_list(mapped_owned_errors),
        "warning_lines": unique_list(warning_lines),
        "summary": " ".join(summary_parts).strip(),
        "signals": {{
            "reached_mapping": reached_mapping,
            "reached_reporting": reached_reporting,
            "made_meaningful_progress": made_meaningful_progress,
            "config_error_lines": config_error_lines[:MAX_ERROR_EXAMPLES],
            "blocking_diagnostics": blocking_diagnostics[:MAX_ERROR_EXAMPLES],
            "database141_lines": database141_lines[:MAX_ERROR_EXAMPLES],
            "ignored_library_info_lines": ignorable_library_info_lines[:MAX_ERROR_EXAMPLES],
            "resolved_top": resolved_top,
        }},
        "suggested_fixes": unique_list(suggested),
    }}

resolved_top = ""
if not builddir.exists():
    message = f"Mapped-stage input netlist directory does not exist: {{builddir}}"
    errors.append(message)
    input_contract_errors.append(message)
elif not builddir.is_dir():
    message = f"Mapped-stage input netlist path is not a directory: {{builddir}}"
    errors.append(message)
    input_contract_errors.append(message)
else:
    input_files = sorted(
        str(path.relative_to(builddir))
        for path in builddir.rglob("*")
        if path.is_file()
    )
    context["input_inventory"] = {{
        "total_files": len(input_files),
        "files": input_files[:MAX_CONTEXT_FILES],
        "truncated": len(input_files) > MAX_CONTEXT_FILES,
    }}

    generic_inputs = [
        rel_path
        for rel_path in input_files
        if any(fnmatch.fnmatch(rel_path, pattern) for pattern in generic_input_patterns)
    ]
    if not generic_inputs:
        message = (
            "Mapped-stage input netlist directory does not contain an expected generic netlist artifact "
            "(*_generic.v, *_generic.sdc, or *.db)."
        )
        errors.append(message)
        input_contract_errors.append(message)
        suggested_fixes.append(
            "Route back to netlist and regenerate the generic netlist bundle before retrying mapped synthesis."
        )
    else:
        context["key_files"]["input_artifacts"] = "\\n".join(generic_inputs[:12])
        for rel_path in generic_inputs:
            name = pathlib.Path(rel_path).name
            if name.endswith("_generic.v"):
                resolved_top = name[: -len("_generic.v")]
                break
        context["resolved_top"] = resolved_top

if not candidate.exists():
    errors.append(f"Mapped output directory does not exist: {{candidate}}")
elif not candidate.is_dir():
    errors.append(f"Mapped output path is not a directory: {{candidate}}")
else:
    all_files = sorted(
        str(path.relative_to(candidate))
        for path in candidate.rglob("*")
        if path.is_file()
    )
    context["output_inventory"] = {{
        "total_files": len(all_files),
        "files": all_files[:MAX_CONTEXT_FILES],
        "truncated": len(all_files) > MAX_CONTEXT_FILES,
    }}

    detected_outputs = []
    detected_logs = []
    for rel_path in all_files:
        if any(fnmatch.fnmatch(rel_path, pattern) for pattern in mapped_output_patterns):
            detected_outputs.append(rel_path)
        if any(fnmatch.fnmatch(rel_path, pattern) for pattern in log_patterns):
            detected_logs.append(rel_path)
    context["detected_outputs"] = detected_outputs[:MAX_CONTEXT_FILES]

    if not detected_outputs:
        errors.append(
            "Mapped output directory does not contain an expected mapped artifact (*.v, *.vg, *.db, or *_mapped.sdc)."
        )
        suggested_fixes.append(
            "Inspect the mapped-stage logs and input generic netlist bundle, then rerun mapped synthesis after fixing the missing output artifact cause."
        )

    if not detected_logs:
        warnings.append(
            "Mapped output directory does not contain a saved log/report file; relying on captured command logs only."
        )

    for rel_path in detected_logs[:3]:
        full_path = candidate / rel_path
        if full_path.is_file():
            context["key_files"][f"output:{{rel_path}}"] = read_text_snippet(
                full_path,
                limit=MAX_KEY_FILE_CHARS,
            )

    log_selection = select_primary_mapped_log(all_files)
    primary_log = log_selection.get("primary_log")
    inspected_logs = [
        str(item) for item in log_selection.get("inspected_logs", []) if str(item).strip()
    ]
    classifier_input = ""
    if primary_log:
        full_path = candidate / primary_log
        if full_path.is_file():
            classifier_input = read_log_text(full_path)

    classifier = classify_mapped_logs(
        classifier_input,
        resolved_top=resolved_top,
    )
    errors.extend(classifier["blocking_errors"])
    warnings.extend(classifier["warning_lines"])
    suggested_fixes.extend(classifier["suggested_fixes"])
    result["failure_stage_owner"] = classifier["failure_stage_owner"]
    result["failure_class"] = classifier["failure_class"]
    result["route_back_to_stage"] = classifier["route_back_to_stage"]
    context["log_classification"] = {{
        **classifier["signals"],
        "primary_log": primary_log,
        "inspected_logs": inspected_logs,
        "fallback_used": bool(log_selection.get("fallback_used")),
        "selection_reason": str(log_selection.get("reason", "")).strip(),
    }}

errors = unique_list(errors)
warnings = unique_list(warnings)
suggested_fixes = unique_list(suggested_fixes)
if input_contract_errors:
    result["failure_stage_owner"] = "netlist"
    result["failure_class"] = "mapped_input_contract_problem"
    result["route_back_to_stage"] = "netlist"
result["errors"] = errors
result["blocking_errors"] = errors
result["warnings"] = warnings
result["suggested_fixes"] = suggested_fixes
result["pass"] = not errors
result["summary"] = (
    " ".join(
        part
        for part in [
            classifier.get("summary", "") if "classifier" in locals() else "",
            f"Local mapped-stage review {{'passed' if result['pass'] else 'failed'}} with {{len(errors)}} errors and {{len(warnings)}} warnings.",
        ]
        if part
    ).strip()
)
print(json.dumps(result))
"""

    return f"{remote_python} -c {shlex.quote(review_script)}"


def _build_mapped_edit_command(
    state: FlowState,
    builddir: str,
    actions: list[dict[str, str]],
) -> str:
    return _build_netlist_edit_command(state, builddir, actions)


def _build_gdsii_command(state: FlowState) -> str:
    prep_dir, prep_dir_source = resolve_prepare_dir(
        state.get("prep_candidate_path"),
        state.get("remote_prepare_dir"),
    )
    netlist_dir, netlist_dir_source = resolve_netlist_dir_choice(
        state.get("netlist_candidate_path") or state.get("remote_netlist_dir"),
        prep_dir,
    )
    mappeddir, mappeddir_source = resolve_mapped_dir_choice(
        state.get("mapped_candidate_path") or state.get("remote_mapped_dir"),
        netlist_dir,
        prep_dir,
    )
    outdir, outdir_source = resolve_gdsii_dir_choice(
        state.get("remote_gdsii_dir"),
        mappeddir,
        netlist_dir,
        prep_dir,
    )
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    gdsii_script = str(state.get("gdsii_script", DEFAULT_GDSII_SCRIPT)).strip()
    if not gdsii_script:
        gdsii_script = DEFAULT_GDSII_SCRIPT
    remote_python = _remote_python(state)
    heartbeat_sec = max(15, int(state.get("gdsii_heartbeat_sec", 60)))
    timing_closure_args = _build_gdsii_timing_closure_args(state)
    prepare_outdir_cmd = (
        "mkdir -p \"$OUTDIR\"; "
        if timing_closure_args.strip()
        else "rm -rf \"$OUTDIR\"; mkdir -p \"$OUTDIR\"; "
    )

    inner_cmd = (
        "set -e; "
        f"cd {shlex.quote(project_root)}; "
        f"OUTDIR={shlex.quote(outdir)}; "
        f"MAPPEDDIR={shlex.quote(mappeddir)}; "
        f"GDSII_SCRIPT={shlex.quote(gdsii_script)}; "
        f"GDSII_HEARTBEAT_SEC={heartbeat_sec}; "
        "echo 'HOST:'; hostname; "
        "echo 'PATH:'; echo $PATH; "
        + prepare_outdir_cmd
        +
        "EDA_GDSII_PROGRESS_LOG=\"$OUTDIR/agent_gdsii_progress.log\"; "
        "EDA_GDSII_STDOUT_LOG=\"$OUTDIR/agent_gdsii_stdout.log\"; "
        "EDA_GDSII_STDERR_LOG=\"$OUTDIR/agent_gdsii_stderr.log\"; "
        "touch \"$EDA_GDSII_PROGRESS_LOG\" \"$EDA_GDSII_STDOUT_LOG\" \"$EDA_GDSII_STDERR_LOG\"; "
        "echo \"EDA_GDSII_LOG_DIR:$OUTDIR\"; "
        "echo \"EDA_GDSII_MAPPEDDIR:$MAPPEDDIR\"; "
        f"echo \"EDA_GDSII_MAPPEDDIR_SOURCE:{shlex.quote(mappeddir_source)}\"; "
        "echo \"EDA_GDSII_OUTDIR:$OUTDIR\"; "
        f"echo \"EDA_GDSII_OUTDIR_SOURCE:{shlex.quote(outdir_source)}\"; "
        "echo \"EDA_GDSII_SCRIPT:$GDSII_SCRIPT\"; "
        "printf '[%s] gdsii launcher started\\n' \"$(date -Is)\" | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "printf '[%s] prep_dir_source=%s\\n' \"$(date -Is)\" "
        f"{shlex.quote(prep_dir_source)} | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "printf '[%s] netlist_dir_source=%s\\n' \"$(date -Is)\" "
        f"{shlex.quote(netlist_dir_source)} | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "printf '[%s] mappeddir=%s\\n' \"$(date -Is)\" \"$MAPPEDDIR\" | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "printf '[%s] mappeddir_source=%s\\n' \"$(date -Is)\" "
        f"{shlex.quote(mappeddir_source)} | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "printf '[%s] outdir=%s\\n' \"$(date -Is)\" \"$OUTDIR\" | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "printf '[%s] outdir_source=%s\\n' \"$(date -Is)\" "
        f"{shlex.quote(outdir_source)} | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "printf '[%s] gdsii_script=%s\\n' \"$(date -Is)\" \"$GDSII_SCRIPT\" | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "echo 'INNOVUS_CHECK:'; "
        "EDA_RESOLVED_INNOVUS_BIN=''; "
        "EDA_RESOLVED_INNOVUS_SOURCE=''; "
        "if [ -n \"${EDA_INNOVUS_BIN:-}\" ] && [ -x \"${EDA_INNOVUS_BIN}\" ]; then "
        "EDA_RESOLVED_INNOVUS_BIN=\"${EDA_INNOVUS_BIN}\"; "
        "EDA_RESOLVED_INNOVUS_SOURCE='env.EDA_INNOVUS_BIN'; "
        "fi; "
        "if [ -z \"$EDA_RESOLVED_INNOVUS_BIN\" ] && "
        "[ -n \"${INNOVUS_BIN:-}\" ] && [ -x \"${INNOVUS_BIN}\" ]; then "
        "EDA_RESOLVED_INNOVUS_BIN=\"${INNOVUS_BIN}\"; "
        "EDA_RESOLVED_INNOVUS_SOURCE='env.INNOVUS_BIN'; "
        "fi; "
        "if [ -z \"$EDA_RESOLVED_INNOVUS_BIN\" ] && "
        f"[ -x {shlex.quote(DEFAULT_REMOTE_INNOVUS_BIN)} ]; then "
        f"EDA_RESOLVED_INNOVUS_BIN={shlex.quote(DEFAULT_REMOTE_INNOVUS_BIN)}; "
        "EDA_RESOLVED_INNOVUS_SOURCE='workers.default_innovus_path'; "
        "fi; "
        "if [ -z \"$EDA_RESOLVED_INNOVUS_BIN\" ]; then "
        "EDA_RESOLVED_INNOVUS_BIN=$(command -v innovus 2>/dev/null || true); "
        "if [ -n \"$EDA_RESOLVED_INNOVUS_BIN\" ]; then "
        "EDA_RESOLVED_INNOVUS_SOURCE='path.command_v'; "
        "fi; "
        "fi; "
        "echo \"$EDA_RESOLVED_INNOVUS_BIN\"; "
        "if [ -z \"$EDA_RESOLVED_INNOVUS_BIN\" ] || [ ! -x \"$EDA_RESOLVED_INNOVUS_BIN\" ]; then "
        "echo 'EDA_GDSII_ENV_ERROR:innovus_executable_not_found' | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "exit 97; "
        "fi; "
        "echo \"EDA_GDSII_INNOVUS_BIN:${EDA_RESOLVED_INNOVUS_BIN}\" | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "echo \"EDA_GDSII_INNOVUS_BIN_SOURCE:${EDA_RESOLVED_INNOVUS_SOURCE}\" | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "echo 'INNOVUS_OK'; "
        "set +e; "
        "( "
        "while true; do "
        "printf '[%s] gdsii still running\\n' \"$(date -Is)\" | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "sleep \"$GDSII_HEARTBEAT_SEC\"; "
        "done "
        ") & "
        "EDA_GDSII_HEARTBEAT_PID=$!; "
        f"{remote_python} \"$GDSII_SCRIPT\" "
        "--mappeddir \"$MAPPEDDIR\" "
        "--outdir \"$OUTDIR\" "
        "--innovus \"$EDA_RESOLVED_INNOVUS_BIN\" "
        f"{timing_closure_args} "
        "> >(tee -a \"$EDA_GDSII_STDOUT_LOG\") "
        "2> >(tee -a \"$EDA_GDSII_STDERR_LOG\" >&2); "
        "GDSII_RC=$?; "
        "kill \"$EDA_GDSII_HEARTBEAT_PID\" >/dev/null 2>&1 || true; "
        "wait \"$EDA_GDSII_HEARTBEAT_PID\" 2>/dev/null || true; "
        "set -e; "
        "printf '[%s] gdsii exited rc=%s\\n' \"$(date -Is)\" \"$GDSII_RC\" | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "echo \"EDA_GDSII_RUN_COMPLETE:${GDSII_RC}\" | tee -a \"$EDA_GDSII_PROGRESS_LOG\"; "
        "exit \"$GDSII_RC\""
    )

    return _build_inner_ssh_command(state, inner_cmd)


def _build_gdsii_review_command(
    state: FlowState,
    mappeddir: str,
    candidate_path: str,
) -> str:
    rules_text, preflight_errors = _load_rules_text(
        state.get("gdsii_rules_path"),
        label="Universal GDSII",
    )
    rules_path = state.get("gdsii_rules_path") or ""
    remote_python = _remote_python(state)

    review_script = f"""
import fnmatch
import json
import pathlib
import re

mappeddir = pathlib.Path({mappeddir!r})
candidate = pathlib.Path({candidate_path!r})
rules_path = {rules_path!r}
rules_text = {rules_text!r}
preflight_errors = {preflight_errors!r}
MAX_CONTEXT_FILES = 400
MAX_KEY_FILE_CHARS = 3000
MAX_LOG_BYTES = 250000
MAX_ERROR_EXAMPLES = 12

result = {{
    "pass": False,
    "errors": list(preflight_errors),
    "blocking_errors": list(preflight_errors),
    "warnings": [],
    "suggested_fixes": [],
    "failure_stage_owner": "gdsii",
    "failure_class": "gdsii_output_validation_failure",
    "route_back_to_stage": "gdsii",
    "summary": "",
    "rules_path": rules_path,
    "candidate_path": str(candidate),
    "context": {{
        "input_mappeddir": str(mappeddir),
        "input_inventory": {{}},
        "output_inventory": {{}},
        "key_files": {{}},
        "detected_outputs": [],
        "log_classification": {{}},
    }},
}}

errors = result["blocking_errors"]
warnings = result["warnings"]
suggested_fixes = result["suggested_fixes"]
context = result["context"]
input_contract_errors = []
mapped_input_patterns = [
    "*_mapped.v",
    "*_mapped.sdc",
    "*.db",
    "*.def",
]
gdsii_output_patterns = [
    "*.gds",
    "*.gdsii",
]
log_patterns = ["*.log", "*.rpt", "*.txt"]

def unique_list(values):
    seen = set()
    ordered = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered

def read_text_snippet(path: pathlib.Path, *, limit: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit] + "\\n...<truncated>..."

def read_log_text(path: pathlib.Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= MAX_LOG_BYTES:
        return text
    return text[-MAX_LOG_BYTES:]

def extract_matching_lines(text, pattern, *, flags=0, limit=MAX_ERROR_EXAMPLES):
    regex = re.compile(pattern, flags)
    matches = []
    for line in text.splitlines():
        if regex.search(line):
            matches.append(line.strip())
        if len(matches) >= limit:
            break
    return unique_list(matches)

def extract_tool_diagnostics(text, *, limit=MAX_ERROR_EXAMPLES):
    diagnostics = []
    lines = text.splitlines()
    index = 0
    while index < len(lines) and len(diagnostics) < limit:
        line = lines[index].rstrip()
        match = re.match(
            r"^\\s*(Info|Warning|Error|Fatal)\\s*:\\s*(.+?)\\s+\\[([A-Z]+-\\d+)\\]\\s*$",
            line,
        )
        if match:
            severity, message, code = match.groups()
            diagnostics.append({{
                "severity": severity,
                "code": code,
                "message": message.strip(),
                "formatted": f"{{severity}} [{{code}}] {{message.strip()}}",
            }})
            index += 1
            continue
        alt_match = re.match(r"^\\s*(ERROR|FATAL|WARN|WARNING|INFO)\\s*[:\\-]\\s*(.+)$", line, re.IGNORECASE)
        if alt_match:
            severity = alt_match.group(1).upper()
            normalized = (
                "Fatal" if severity == "FATAL" else
                "Error" if severity == "ERROR" else
                "Warning" if severity in ("WARN", "WARNING") else
                "Info"
            )
            message = alt_match.group(2).strip()
            diagnostics.append({{
                "severity": normalized,
                "code": "",
                "message": message,
                "formatted": f"{{normalized}} {{message}}".strip(),
            }})
        index += 1
    return diagnostics

def select_primary_gdsii_log(all_files):
    for rel_path in all_files:
        normalized = str(rel_path).replace("\\\\", "/").lower()
        if normalized.endswith("innovus.log") or normalized.endswith("gdsii.log"):
            return {{
                "primary_log": rel_path,
                "inspected_logs": [rel_path],
                "fallback_used": False,
                "reason": "Found primary GDSII log file in output directory.",
            }}
    fallback_logs = [
        rel_path for rel_path in all_files
        if str(rel_path).lower().endswith((".log", ".rpt", ".txt"))
    ]
    preferred_logs = sorted(
        fallback_logs,
        key=lambda item: (
            0 if item.endswith("innovus.log") else 1 if "agent_gdsii_stdout" in item else 2,
            item,
        ),
    )[:1]
    return {{
        "primary_log": preferred_logs[0] if preferred_logs else None,
        "inspected_logs": preferred_logs,
        "fallback_used": bool(preferred_logs),
        "reason": (
            "No dedicated GDSII log was found; using the best available fallback log."
            if preferred_logs
            else "No GDSII log/report file was available for classifier input."
        ),
    }}

def classify_gdsii_logs(log_text: str) -> dict:
    reached_import = bool(re.search(r"(?i)read_(?:netlist|verilog|def|lef)|init_design|place_design", log_text))
    reached_streamout = bool(re.search(r"(?i)streamout|write_gds|write_stream", log_text))
    made_meaningful_progress = reached_import or reached_streamout
    tool_diagnostics = extract_tool_diagnostics(log_text)
    config_error_lines = unique_list(
        [
            *extract_matching_lines(log_text, r"(?i)cannot open.*(lef|def|lib|db|netlist|sdc|tech|gds)"),
            *extract_matching_lines(log_text, r"(?i)(top module|design|cell).*not found"),
            *extract_matching_lines(log_text, r"(?i)(can't read|no such variable|undefined variable).*(env\\(|innovus|gds|lef|def|tech|path|script|file|variable)"),
        ]
    )
    blocking_diagnostics = [
        diagnostic["formatted"]
        for diagnostic in tool_diagnostics
        if diagnostic.get("severity") in ("Error", "Fatal")
    ]
    gdsii_owned_errors = []
    warning_lines = []
    suggested = []
    summary_parts = []

    if config_error_lines:
        gdsii_owned_errors.extend(
            [
                "GDSII-stage configuration issue: " + line
                for line in config_error_lines[:MAX_ERROR_EXAMPLES]
            ]
        )
        suggested.append(
            "Keep the failure in the gdsii stage and repair the Innovus/GDSII launch configuration or required physical-design inputs."
        )
        summary_parts.append("GDSII-stage configuration diagnostics were detected after launch.")

    if blocking_diagnostics:
        gdsii_owned_errors.extend(
            [
                "GDSII-stage execution issue: " + line
                for line in blocking_diagnostics[:MAX_ERROR_EXAMPLES]
            ]
        )
        suggested.append(
            "Keep the failure in the gdsii stage and limit edits to bounded GDSII-flow compatibility changes."
        )
        summary_parts.append("GDSII-stage logs contain blocking tool diagnostics.")

    if not gdsii_owned_errors:
        failure_class = "non_fatal_warning_only_issue"
        owner = "gdsii"
        route = None
        summary_parts.append("No blocking GDSII-stage log classifier findings were detected.")
    else:
        failure_class = "gdsii_stage_execution_problem"
        owner = "gdsii"
        route = "gdsii"

    return {{
        "failure_stage_owner": owner,
        "failure_class": failure_class,
        "route_back_to_stage": route,
        "blocking_errors": unique_list(gdsii_owned_errors),
        "warning_lines": unique_list(warning_lines),
        "summary": " ".join(summary_parts).strip(),
        "signals": {{
            "reached_import": reached_import,
            "reached_streamout": reached_streamout,
            "made_meaningful_progress": made_meaningful_progress,
            "config_error_lines": config_error_lines[:MAX_ERROR_EXAMPLES],
            "blocking_diagnostics": blocking_diagnostics[:MAX_ERROR_EXAMPLES],
        }},
        "suggested_fixes": unique_list(suggested),
    }}

if not mappeddir.exists():
    message = f"GDSII-stage input mapped directory does not exist: {{mappeddir}}"
    errors.append(message)
    input_contract_errors.append(message)
elif not mappeddir.is_dir():
    message = f"GDSII-stage input mapped path is not a directory: {{mappeddir}}"
    errors.append(message)
    input_contract_errors.append(message)
else:
    input_files = sorted(
        str(path.relative_to(mappeddir))
        for path in mappeddir.rglob("*")
        if path.is_file()
    )
    context["input_inventory"] = {{
        "total_files": len(input_files),
        "files": input_files[:MAX_CONTEXT_FILES],
        "truncated": len(input_files) > MAX_CONTEXT_FILES,
    }}
    mapped_inputs = [
        rel_path
        for rel_path in input_files
        if any(fnmatch.fnmatch(rel_path, pattern) for pattern in mapped_input_patterns)
    ]
    if not mapped_inputs:
        message = (
            "GDSII-stage input mapped directory does not contain an expected mapped artifact "
            "(*_mapped.v, *_mapped.sdc, *.db, or *.def)."
        )
        errors.append(message)
        input_contract_errors.append(message)
        suggested_fixes.append(
            "Route back to mapped and regenerate the mapped implementation bundle before retrying GDSII."
        )
    else:
        context["key_files"]["input_artifacts"] = "\\n".join(mapped_inputs[:12])

if not candidate.exists():
    errors.append(f"GDSII output directory does not exist: {{candidate}}")
elif not candidate.is_dir():
    errors.append(f"GDSII output path is not a directory: {{candidate}}")
else:
    all_files = sorted(
        str(path.relative_to(candidate))
        for path in candidate.rglob("*")
        if path.is_file()
    )
    context["output_inventory"] = {{
        "total_files": len(all_files),
        "files": all_files[:MAX_CONTEXT_FILES],
        "truncated": len(all_files) > MAX_CONTEXT_FILES,
    }}
    detected_outputs = []
    detected_logs = []
    for rel_path in all_files:
        if any(fnmatch.fnmatch(rel_path, pattern) for pattern in gdsii_output_patterns):
            detected_outputs.append(rel_path)
        if any(fnmatch.fnmatch(rel_path, pattern) for pattern in log_patterns):
            detected_logs.append(rel_path)
    context["detected_outputs"] = detected_outputs[:MAX_CONTEXT_FILES]

    if not detected_outputs:
        errors.append(
            "GDSII output directory does not contain an expected layout artifact (*.gds or *.gdsii)."
        )
        suggested_fixes.append(
            "Inspect the GDSII-stage logs and mapped implementation bundle, then rerun GDSII after fixing the missing stream-out cause."
        )

    if not detected_logs:
        warnings.append(
            "GDSII output directory does not contain a saved log/report file; relying on captured command logs only."
        )

    for rel_path in detected_logs[:3]:
        full_path = candidate / rel_path
        if full_path.is_file():
            context["key_files"][f"output:{{rel_path}}"] = read_text_snippet(
                full_path,
                limit=MAX_KEY_FILE_CHARS,
            )

    log_selection = select_primary_gdsii_log(all_files)
    primary_log = log_selection.get("primary_log")
    inspected_logs = [
        str(item) for item in log_selection.get("inspected_logs", []) if str(item).strip()
    ]
    classifier_input = ""
    if primary_log:
        full_path = candidate / primary_log
        if full_path.is_file():
            classifier_input = read_log_text(full_path)

    classifier = classify_gdsii_logs(classifier_input)
    errors.extend(classifier["blocking_errors"])
    warnings.extend(classifier["warning_lines"])
    suggested_fixes.extend(classifier["suggested_fixes"])
    result["failure_stage_owner"] = classifier["failure_stage_owner"]
    result["failure_class"] = classifier["failure_class"]
    result["route_back_to_stage"] = classifier["route_back_to_stage"]
    context["log_classification"] = {{
        **classifier["signals"],
        "primary_log": primary_log,
        "inspected_logs": inspected_logs,
        "fallback_used": bool(log_selection.get("fallback_used")),
        "selection_reason": str(log_selection.get("reason", "")).strip(),
    }}

errors = unique_list(errors)
warnings = unique_list(warnings)
suggested_fixes = unique_list(suggested_fixes)
if input_contract_errors:
    result["failure_stage_owner"] = "mapped_netlist"
    result["failure_class"] = "gdsii_input_contract_problem"
    result["route_back_to_stage"] = "mapped_netlist"
result["errors"] = errors
result["blocking_errors"] = errors
result["warnings"] = warnings
result["suggested_fixes"] = suggested_fixes
result["pass"] = not errors
result["summary"] = (
    " ".join(
        part
        for part in [
            classifier.get("summary", "") if "classifier" in locals() else "",
            f"Local GDSII-stage review {{'passed' if result['pass'] else 'failed'}} with {{len(errors)}} errors and {{len(warnings)}} warnings.",
        ]
        if part
    ).strip()
)
print(json.dumps(result))
"""

    return f"{remote_python} -c {shlex.quote(review_script)}"


def _build_gdsii_edit_command(
    state: FlowState,
    actions: list[dict[str, str]],
) -> str:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    return _build_netlist_edit_command(state, project_root, actions)
