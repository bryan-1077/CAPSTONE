import os
import re
import sys
import json
import yaml
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ============================================================
# Helpers
# ============================================================

def log(msg: str) -> None:
    print("[RTL_VALIDATOR_AGENT] {}".format(msg))


def read_text(path: str) -> str:
    with open(path, "r", errors="ignore") as f:
        return f.read()


def write_json(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def strip_comments_sv(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*", "", text)
    return text


def load_yaml(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def collect_rtl_files(path_in: str) -> List[str]:
    p = Path(path_in)
    files = []

    if p.is_file():
        if p.suffix.lower() in [".sv", ".v"]:
            files.append(str(p.resolve()))

    elif p.is_dir():
        for ext in ("*.sv", "*.v"):
            files.extend(
                str(x.resolve())
                for x in p.rglob(ext)
            )

    return sorted(files)


def find_matching_yaml_for_top(
    top_module_name: str,
    top_module_file: str
) -> Optional[str]:
    """
    Find the YAML specification associated with the RTL top module.

    Search order:
    1. Exact <top_module_name>.yaml
    2. Exact <top_module_name>.yml
    3. Case-insensitive filename/stem match
    4. If exactly one YAML exists in the folder, use it
    5. Inspect YAML design_name fields

    YAML is mandatory for Agent 1.
    """

    if not top_module_file:
        return None

    folder = Path(top_module_file).parent

    log("Searching for YAML specification in: {}".format(folder))

    # --------------------------------------------------------
    # 1. Exact top-module-name match
    # --------------------------------------------------------

    exact_candidates = [
        folder / "{}.yaml".format(top_module_name),
        folder / "{}.yml".format(top_module_name),
    ]

    for candidate in exact_candidates:
        if candidate.is_file():
            log(
                "Matched YAML by exact module name: {}".format(
                    candidate
                )
            )
            return str(candidate.resolve())

    # --------------------------------------------------------
    # 2. Collect all YAML/YML files in RTL directory
    # --------------------------------------------------------

    yaml_files = []

    try:
        for item in folder.iterdir():
            if not item.is_file():
                continue

            if item.suffix.lower() in [".yaml", ".yml"]:
                yaml_files.append(item)

    except Exception as e:
        log(
            "Warning: could not search YAML directory: {}".format(e)
        )
        return None

    yaml_files = sorted(
        set(yaml_files),
        key=lambda x: str(x).lower()
    )

    if yaml_files:
        log(
            "Found {} YAML file(s) in design directory".format(
                len(yaml_files)
            )
        )

        for y in yaml_files:
            log("  YAML candidate: {}".format(y.name))

    else:
        log("Found 0 YAML files in design directory")
        return None

    # --------------------------------------------------------
    # 3. Case-insensitive stem match
    # --------------------------------------------------------

    top_lower = top_module_name.lower()

    for y in yaml_files:
        if y.stem.lower() == top_lower:
            log(
                "Matched YAML by filename stem: {}".format(
                    y.name
                )
            )
            return str(y.resolve())

    # --------------------------------------------------------
    # 4. If there is exactly one YAML file, use it
    # --------------------------------------------------------

    if len(yaml_files) == 1:
        log(
            "Only one YAML specification found. Using: {}".format(
                yaml_files[0].name
            )
        )
        return str(yaml_files[0].resolve())

    # --------------------------------------------------------
    # 5. Search YAML contents for matching design_name
    # --------------------------------------------------------

    for y in yaml_files:
        try:
            spec = load_yaml(str(y))

            if not isinstance(spec, dict):
                continue

            design_name = spec.get("design_name")

            if (
                isinstance(design_name, str)
                and design_name == top_module_name
            ):
                log(
                    "Matched YAML using design_name: {}".format(
                        y.name
                    )
                )
                return str(y.resolve())

            if (
                isinstance(design_name, str)
                and design_name.lower() == top_lower
            ):
                log(
                    "Matched YAML using case-insensitive "
                    "design_name: {}".format(y.name)
                )
                return str(y.resolve())

        except Exception as e:
            log(
                "Warning: could not inspect YAML '{}': {}".format(
                    y.name,
                    e
                )
            )

    return None


def safe_int(val, default=None):
    try:
        return int(val)
    except Exception:
        return default


# ============================================================
# Verilog / SystemVerilog parsing
# ============================================================

def extract_module_blocks(text: str) -> List[Tuple[str, str]]:
    pattern = re.compile(
        r"^\s*module\s+([a-zA-Z_]\w*)\b(.*?)(?=^\s*endmodule\b)",
        re.MULTILINE | re.DOTALL
    )

    return [
        (m.group(1), m.group(0))
        for m in pattern.finditer(text)
    ]


def extract_module_names(text: str) -> List[str]:
    return [
        name
        for name, _ in extract_module_blocks(text)
    ]


def detect_top_module(full_text: str) -> Optional[str]:
    module_names = extract_module_names(full_text)

    if not module_names:
        return None

    instantiated = set()

    for mod_name, block in extract_module_blocks(full_text):

        for candidate in module_names:

            if candidate == mod_name:
                continue

            inst_pat = (
                r"^\s*"
                + re.escape(candidate)
                + r"\s+([a-zA-Z_]\w*)\s*\("
            )

            if re.search(
                inst_pat,
                block,
                re.MULTILINE
            ):
                instantiated.add(candidate)

    top_candidates = [
        m
        for m in module_names
        if m not in instantiated
    ]

    if top_candidates:

        preferred = []

        for cand in top_candidates:
            lc = cand.lower()

            if (
                "top" in lc
                or "ctrl" in lc
                or "controller" in lc
            ):
                preferred.append(cand)

        return (
            preferred[0]
            if preferred
            else top_candidates[0]
        )

    return module_names[0]


def find_module_block(
    full_text: str,
    module_name: str
) -> Optional[str]:

    for name, block in extract_module_blocks(full_text):

        if name == module_name:
            return block

    return None


def extract_ports_from_module_block(
    block: str
) -> List[dict]:
    """
    ANSI-style parser.

    Returns:
    [
        {
            "name": ...,
            "direction": ...,
            "width_text": ...
        }
    ]
    """

    if not block:
        return []

    m = re.search(
        r"\bmodule\s+[a-zA-Z_]\w*\s*\((.*?)\)\s*;",
        block,
        re.DOTALL
    )

    if not m:
        return []

    port_blob = m.group(1)

    port_blob = re.sub(
        r"/\*.*?\*/",
        "",
        port_blob,
        flags=re.DOTALL
    )

    port_blob = re.sub(
        r"//.*",
        "",
        port_blob
    )

    raw = [
        x.strip()
        for x in port_blob.split(",")
    ]

    ports = []

    for item in raw:

        direction = None

        if re.search(r"\binput\b", item):
            direction = "input"

        elif re.search(r"\boutput\b", item):
            direction = "output"

        elif re.search(r"\binout\b", item):
            direction = "inout"

        width_match = re.search(
            r"(\[[^\]]+\])",
            item
        )

        width_text = (
            width_match.group(1)
            if width_match
            else None
        )

        cleaned = re.sub(
            r"\b(input|output|inout|wire|reg|logic|signed|unsigned)\b",
            "",
            item
        )

        cleaned = re.sub(
            r"\[[^\]]+\]",
            "",
            cleaned
        ).strip()

        if cleaned:

            tokens = cleaned.split()
            name = tokens[-1]

            ports.append({
                "name": name,
                "direction": direction,
                "width_text": width_text
            })

    seen = set()
    uniq = []

    for p in ports:

        if p["name"] not in seen:
            seen.add(p["name"])
            uniq.append(p)

    return uniq


def infer_clock_reset_ports(
    port_names: List[str]
) -> Tuple[List[str], List[str]]:

    clocks = []
    resets = []

    for p in port_names:

        lp = p.lower()

        if "clk" in lp or "clock" in lp:
            clocks.append(p)

        if "rst" in lp or "reset" in lp:
            resets.append(p)

    return clocks, resets


def detect_reset_style(
    module_block: str,
    reset_name: Optional[str]
) -> Dict[str, Optional[bool]]:

    result = {
        "found_reset_logic": False,
        "async_reset_detected": None,
        "sync_reset_detected": None
    }

    if not module_block or not reset_name:
        return result

    reset_name_escaped = re.escape(reset_name)

    always_blocks = re.findall(
        (
            r"(always_ff\s*@\s*\(.*?\)\s*begin.*?end|"
            r"always\s*@\s*\(.*?\)\s*begin.*?end)"
        ),
        module_block,
        flags=re.DOTALL
    )

    for blk in always_blocks:

        if re.search(
            r"\bif\s*\(\s*!?\s*"
            + reset_name_escaped
            + r"\s*\)",
            blk
        ):

            result["found_reset_logic"] = True

            sens = re.search(
                r"@\s*\((.*?)\)",
                blk,
                flags=re.DOTALL
            )

            if sens:

                sens_text = sens.group(1)

                if re.search(
                    reset_name_escaped,
                    sens_text
                ):
                    result["async_reset_detected"] = True

                else:
                    result["sync_reset_detected"] = True

    return result


def detect_always_comb_assignment(
    module_block: str,
    signal_name: str
) -> bool:

    pats = [
        (
            r"always_comb\s+begin.*?\b"
            + re.escape(signal_name)
            + r"\b\s*="
        ),
        (
            r"always\s*@\s*\(\s*\*\s*\)\s*begin.*?\b"
            + re.escape(signal_name)
            + r"\b\s*="
        )
    ]

    for pat in pats:

        if re.search(
            pat,
            module_block,
            flags=re.DOTALL
        ):
            return True

    return False


def signal_decl_width(
    module_block: str,
    signal_name: str
) -> Optional[int]:

    pat = (
        r"\b(?:logic|reg|wire)\b\s*"
        r"(\[[^\]]+\])?\s*"
        + re.escape(signal_name)
        + r"\b"
    )

    m = re.search(
        pat,
        module_block
    )

    if not m:
        return None

    width_text = m.group(1)

    if not width_text:
        return 1

    nums = re.findall(
        r"\d+",
        width_text
    )

    if len(nums) == 2:

        msb = int(nums[0])
        lsb = int(nums[1])

        return abs(msb - lsb) + 1

    return None


# ============================================================
# YAML sanity checks
# ============================================================

def port_width_from_yaml(width_val) -> int:

    if isinstance(width_val, int):
        return width_val

    if (
        isinstance(width_val, str)
        and width_val.isdigit()
    ):
        return int(width_val)

    return 1


def build_spec_summary(spec: dict) -> dict:

    ports = spec.get("ports", [])

    port_map = {}

    for p in ports:

        port_map[p["name"]] = {
            "direction": p.get("direction"),
            "width": port_width_from_yaml(
                p.get("width", 1)
            )
        }

    tfaw_cycles = safe_int(
        spec.get(
            "behavior",
            {}
        ).get(
            "tFAW_cycles",
            0
        ),
        0
    )

    return {
        "design_name": spec.get("design_name"),

        "clock_name": spec.get(
            "clock",
            {}
        ).get("name"),

        "reset_name": spec.get(
            "reset",
            {}
        ).get("name"),

        "reset_active_low": spec.get(
            "reset",
            {}
        ).get("active_low"),

        "reset_synchronous": spec.get(
            "reset",
            {}
        ).get("synchronous"),

        "ports": port_map,

        "cycle_counter_width": safe_int(
            spec.get(
                "behavior",
                {}
            ).get("cycle_counter_width")
        ),

        "timestamp_width": safe_int(
            spec.get(
                "behavior",
                {}
            ).get("timestamp_width")
        ),

        "timestamp_count": safe_int(
            spec.get(
                "behavior",
                {}
            ).get("timestamp_count")
        ),

        "window_size": safe_int(
            spec.get(
                "behavior",
                {}
            ).get("window_size")
        ),

        "tFAW_cycles": tfaw_cycles,

        "correctness_criteria": spec.get(
            "behavior",
            {}
        ).get(
            "correctness_criteria",
            []
        ),

        "combinational_behavior": spec.get(
            "behavior",
            {}
        ).get(
            "combinational_behavior",
            ""
        ),

        "sequential_behavior": spec.get(
            "behavior",
            {}
        ).get(
            "sequential_behavior",
            ""
        )
    }


def check_yaml_sanity(
    spec: dict
) -> Tuple[bool, List[str]]:

    issues = []

    if not isinstance(spec, dict):
        return False, [
            "YAML spec is not a dictionary/object"
        ]

    design_name = spec.get("design_name")

    if (
        not design_name
        or not isinstance(design_name, str)
    ):
        issues.append(
            "YAML missing valid design_name"
        )

    clock = spec.get("clock", {})

    if (
        not isinstance(clock, dict)
        or not clock.get("name")
    ):
        issues.append(
            "YAML missing valid clock.name"
        )

    reset = spec.get("reset", {})

    if (
        not isinstance(reset, dict)
        or not reset.get("name")
    ):
        issues.append(
            "YAML missing valid reset.name"
        )

    if isinstance(reset, dict):

        if (
            "active_low" in reset
            and not isinstance(
                reset.get("active_low"),
                bool
            )
        ):
            issues.append(
                "YAML reset.active_low must be boolean"
            )

        if (
            "synchronous" in reset
            and not isinstance(
                reset.get("synchronous"),
                bool
            )
        ):
            issues.append(
                "YAML reset.synchronous must be boolean"
            )

    ports = spec.get("ports", [])

    if (
        not isinstance(ports, list)
        or len(ports) == 0
    ):

        issues.append(
            "YAML ports must be a non-empty list"
        )

    else:

        names = []

        for p in ports:

            if not isinstance(p, dict):

                issues.append(
                    "YAML port entry is not an object"
                )

                continue

            pname = p.get("name")
            pdir = p.get("direction")
            pwidth = p.get("width", 1)

            if (
                not pname
                or not isinstance(pname, str)
            ):

                issues.append(
                    "YAML port missing valid name"
                )

            else:
                names.append(pname)

            if pdir not in [
                "input",
                "output",
                "inout"
            ]:

                issues.append(
                    "YAML port '{}' has invalid "
                    "direction '{}'".format(
                        pname,
                        pdir
                    )
                )

            pw = port_width_from_yaml(pwidth)

            if pw < 1:

                issues.append(
                    "YAML port '{}' has invalid "
                    "width '{}'".format(
                        pname,
                        pwidth
                    )
                )

        dupes = sorted(
            set(
                [
                    n
                    for n in names
                    if names.count(n) > 1
                ]
            )
        )

        for d in dupes:

            issues.append(
                "YAML has duplicate port name '{}'".format(
                    d
                )
            )

    spec_sum = build_spec_summary(spec)

    cycle_counter_width = spec_sum[
        "cycle_counter_width"
    ]

    timestamp_width = spec_sum[
        "timestamp_width"
    ]

    timestamp_count = spec_sum[
        "timestamp_count"
    ]

    window_size = spec_sum[
        "window_size"
    ]

    tfaw_cycles = spec_sum[
        "tFAW_cycles"
    ]

    if (
        cycle_counter_width is not None
        and cycle_counter_width < 1
    ):
        issues.append(
            "YAML cycle_counter_width must be >= 1"
        )

    if (
        timestamp_width is not None
        and timestamp_width < 1
    ):
        issues.append(
            "YAML timestamp_width must be >= 1"
        )

    if (
        timestamp_count is not None
        and timestamp_count < 1
    ):
        issues.append(
            "YAML timestamp_count must be >= 1"
        )

    if (
        window_size is not None
        and window_size < 1
    ):
        issues.append(
            "YAML window_size must be >= 1"
        )

    if (
        tfaw_cycles is not None
        and tfaw_cycles < 0
    ):
        issues.append(
            "YAML tFAW_cycles must be >= 0"
        )

    if (
        timestamp_count is not None
        and window_size is not None
        and timestamp_count != window_size
    ):

        issues.append(
            "YAML timestamp_count ({}) should match "
            "window_size ({})".format(
                timestamp_count,
                window_size
            )
        )

    if (
        cycle_counter_width is not None
        and tfaw_cycles is not None
    ):

        max_count = (
            2 ** cycle_counter_width
        )

        if tfaw_cycles >= max_count:

            issues.append(
                "YAML tFAW_cycles ({}) is not safe "
                "for cycle_counter_width {} "
                "(wrap range {})".format(
                    tfaw_cycles,
                    cycle_counter_width,
                    max_count
                )
            )

    correctness = " ".join(
        spec_sum[
            "correctness_criteria"
        ]
    )

    comb = spec_sum[
        "combinational_behavior"
    ]

    if (
        "must not be registered"
        in correctness.lower()
        and "always_comb"
        not in comb.lower()
    ):

        issues.append(
            "YAML says combinational/non-registered "
            "behavior, but combinational_behavior does "
            "not clearly describe always_comb"
        )

    if (
        spec_sum["clock_name"]
        and spec_sum["reset_name"]
        and spec_sum["clock_name"]
        == spec_sum["reset_name"]
    ):

        issues.append(
            "YAML clock.name and reset.name "
            "cannot be the same signal"
        )

    return len(issues) == 0, issues


# ============================================================
# YAML ↔ RTL checks
# ============================================================

def check_spec_vs_rtl(
    top_module_name: str,
    top_block: str,
    spec: dict
) -> Tuple[bool, List[str]]:

    issues = []

    spec_sum = build_spec_summary(spec)

    if (
        spec_sum["design_name"]
        and spec_sum["design_name"]
        != top_module_name
    ):

        issues.append(
            "Top module name '{}' does not match "
            "YAML design_name '{}'".format(
                top_module_name,
                spec_sum["design_name"]
            )
        )

    rtl_ports = extract_ports_from_module_block(
        top_block
    )

    rtl_port_map = {
        p["name"]: p
        for p in rtl_ports
    }

    for pname, pinfo in spec_sum[
        "ports"
    ].items():

        if pname not in rtl_port_map:

            issues.append(
                "Missing YAML port in RTL: {}".format(
                    pname
                )
            )

            continue

        rtl_dir = rtl_port_map[
            pname
        ]["direction"]

        if (
            pinfo["direction"]
            and rtl_dir
            and pinfo["direction"]
            != rtl_dir
        ):

            issues.append(
                "Port direction mismatch for '{}': "
                "YAML={} RTL={}".format(
                    pname,
                    pinfo["direction"],
                    rtl_dir
                )
            )

    clock_name = spec_sum[
        "clock_name"
    ]

    reset_name = spec_sum[
        "reset_name"
    ]

    if (
        clock_name
        and clock_name not in rtl_port_map
    ):

        issues.append(
            "Clock '{}' from YAML not found "
            "in RTL ports".format(
                clock_name
            )
        )

    if (
        reset_name
        and reset_name not in rtl_port_map
    ):

        issues.append(
            "Reset '{}' from YAML not found "
            "in RTL ports".format(
                reset_name
            )
        )

    if reset_name:

        reset_style = detect_reset_style(
            top_block,
            reset_name
        )

        expected_sync = spec_sum[
            "reset_synchronous"
        ]

        if expected_sync is True:

            if (
                reset_style[
                    "async_reset_detected"
                ] is True
            ):

                issues.append(
                    "YAML requires synchronous reset, "
                    "but RTL appears asynchronous "
                    "for '{}'".format(
                        reset_name
                    )
                )

            elif (
                reset_style[
                    "found_reset_logic"
                ] is False
            ):

                issues.append(
                    "No reset logic found for '{}'".format(
                        reset_name
                    )
                )

        elif expected_sync is False:

            if (
                reset_style[
                    "async_reset_detected"
                ] is not True
            ):

                issues.append(
                    "YAML requires asynchronous reset, "
                    "but RTL does not appear asynchronous "
                    "for '{}'".format(
                        reset_name
                    )
                )

    if "tFAW_ok" in spec_sum[
        "ports"
    ]:

        criteria = (
            " ".join(
                spec_sum[
                    "correctness_criteria"
                ]
            )
            + " "
            + spec_sum[
                "combinational_behavior"
            ]
        )

        if "combinational" in criteria.lower():

            if not detect_always_comb_assignment(
                top_block,
                "tFAW_ok"
            ):

                issues.append(
                    "YAML requires combinational "
                    "tFAW_ok, but RTL does not clearly "
                    "assign it in always_comb"
                )

    cycle_w = spec_sum[
        "cycle_counter_width"
    ]

    if cycle_w:

        rtl_w = signal_decl_width(
            top_block,
            "cycle_counter"
        )

        if (
            rtl_w is not None
            and rtl_w != cycle_w
        ):

            issues.append(
                "cycle_counter width mismatch: "
                "YAML={} RTL={}".format(
                    cycle_w,
                    rtl_w
                )
            )

    ts_w = spec_sum[
        "timestamp_width"
    ]

    if ts_w:

        rtl_w = signal_decl_width(
            top_block,
            "act_timestamps"
        )

        if (
            rtl_w is not None
            and rtl_w != ts_w
        ):

            issues.append(
                "act_timestamps width mismatch: "
                "YAML={} RTL={}".format(
                    ts_w,
                    rtl_w
                )
            )

    return len(issues) == 0, issues


# ============================================================
# RTL intrinsic sanity checks
# ============================================================

def check_rtl_intrinsic_sanity(
    top_module_name: str,
    top_block: str,
    full_text: str
) -> Tuple[bool, List[str]]:

    issues = []

    if not top_block:

        return False, [
            "Could not find top module block for '{}'".format(
                top_module_name
            )
        ]

    ports = extract_ports_from_module_block(
        top_block
    )

    if len(ports) == 0:

        issues.append(
            "Top module '{}' has no parseable "
            "ANSI-style ports".format(
                top_module_name
            )
        )

    bad_patterns = [
        r"SignalRef\s*\(",
        r"Compare\s*\(",
        r"Const\s*\(",
        r"left\s*=",
        r"right\s*=",
        r"op\s*=",
        r"next_state_node",
        r"object representation",
    ]

    for pat in bad_patterns:

        if re.search(
            pat,
            top_block
        ):

            issues.append(
                "RTL contains suspicious "
                "non-SystemVerilog pattern "
                "matching '{}'".format(
                    pat
                )
            )

            break

    if re.search(
        r"\bif\s*\(\s*Compare\s*\(",
        top_block
    ):

        issues.append(
            "RTL appears to use Python-like "
            "comparison objects inside if conditions"
        )

    if re.search(
        r"\btimer\b",
        top_block
    ):

        decl_pat = (
            r"\b(?:logic|reg|wire|integer|int)\b"
            r"[^;]*\btimer\b"
        )

        port_pat = (
            r"\bmodule\b.*?\((.*?)\)\s*;"
        )

        declared = bool(
            re.search(
                decl_pat,
                top_block
            )
        )

        port_decl = False

        pm = re.search(
            port_pat,
            top_block,
            re.DOTALL
        )

        if pm:

            port_decl = bool(
                re.search(
                    r"\btimer\b",
                    pm.group(1)
                )
            )

        if (
            not declared
            and not port_decl
        ):

            issues.append(
                "Signal 'timer' is referenced but "
                "no simple declaration/port was found"
            )

    port_names = [
        p["name"]
        for p in ports
    ]

    clocks, resets = infer_clock_reset_ports(
        port_names
    )

    if len(clocks) == 0:

        issues.append(
            "No obvious clock-like port "
            "detected in top module"
        )

    if len(resets) == 0:

        issues.append(
            "No obvious reset-like port "
            "detected in top module"
        )

    if re.search(
        r"always_ff.*?\btFAW_ok\b\s*<=",
        top_block,
        re.DOTALL
    ):

        issues.append(
            "tFAW_ok appears assigned in always_ff, "
            "which may indicate registered behavior"
        )

    return len(issues) == 0, issues


# ============================================================
# Compile sanity
# ============================================================

def detect_compile_tool() -> Optional[str]:

    if shutil.which("vcs"):
        return "vcs"

    if shutil.which("iverilog"):
        return "iverilog"

    return None


def run_compile_sanity(
    rtl_files: List[str],
    top_module_name: str,
    work_dir: str = ".agent1_compile"
) -> Tuple[Optional[str], bool, str]:

    tool = detect_compile_tool()

    if not tool:

        return (
            None,
            False,
            "No compile tool found "
            "(iverilog/vcs). Compile check skipped."
        )

    if not os.path.isdir(work_dir):
        os.makedirs(work_dir)

    if tool == "vcs":

        cmd = [
            "vcs",
            "-sverilog",
            "-full64",
            "-timescale=1ns/1ps",
            "-top",
            top_module_name,
            "-o",
            os.path.join(
                work_dir,
                "simv"
            ),
        ] + rtl_files

    else:

        cmd = [
            "iverilog",
            "-g2012",
            "-s",
            top_module_name,
            "-o",
            os.path.join(
                work_dir,
                "a.out"
            ),
        ] + rtl_files

    try:

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60
        )

        output = (
            (proc.stdout or "")
            + "\n"
            + (proc.stderr or "")
        )

        return (
            tool,
            proc.returncode == 0,
            output.strip()
        )

    except Exception as e:

        return (
            tool,
            False,
            "Compile sanity check exception: {}".format(
                e
            )
        )


# ============================================================
# LLM placeholder + filtering
# ============================================================

def call_llm_review(
    top_module_name: str,
    rtl_text: str,
    spec_text: Optional[str]
) -> dict:
    """
    Placeholder.

    Replace with your existing TAMU/LLM call if desired.
    """

    return {
        "is_valid": True,
        "severity": "low",
        "summary": (
            "Rule-based validation passed. "
            "No critical issues identified."
        ),
        "issues": []
    }


def filter_llm_issues_with_yaml(
    llm_result: dict,
    spec: Optional[dict]
) -> dict:

    if not spec:
        return llm_result

    spec_sum = build_spec_summary(
        spec
    )

    filtered = []
    dropped = []

    for issue in llm_result.get(
        "issues",
        []
    ):

        low = issue.lower()

        if (
            (
                "asynchronous" in low
                or "async" in low
            )
            and "reset" in low
        ):

            if (
                spec_sum[
                    "reset_synchronous"
                ] is True
            ):
                dropped.append(issue)
                continue

        if (
            "wrap" in low
            and "cycle_counter" in low
        ):

            if (
                spec_sum[
                    "cycle_counter_width"
                ] == 8
                and spec_sum[
                    "tFAW_cycles"
                ]
                and spec_sum[
                    "tFAW_cycles"
                ] < 256
            ):

                dropped.append(issue)
                continue

        if (
            (
                "underflow" in low
                or "subtraction" in low
            )
            and "cycle_counter" in low
        ):

            if (
                spec_sum[
                    "cycle_counter_width"
                ] == 8
                and spec_sum[
                    "tFAW_cycles"
                ]
                and spec_sum[
                    "tFAW_cycles"
                ] < 256
            ):

                dropped.append(issue)
                continue

        filtered.append(issue)

    result = dict(
        llm_result
    )

    result["issues"] = filtered

    if filtered:

        result["is_valid"] = False

        result["summary"] = llm_result.get(
            "summary",
            (
                "LLM review found issues "
                "after YAML-aware filtering."
            )
        )

    else:

        result["is_valid"] = True
        result["severity"] = "low"

        if dropped:

            result["summary"] = (
                "LLM raised issues, but they "
                "were filtered out because they "
                "contradict the YAML spec."
            )

        else:

            result["summary"] = llm_result.get(
                "summary",
                "No issues."
            )

    result["dropped_issues"] = dropped

    return result


# ============================================================
# DUT SPEC generation for Agent 2 / Agent 3
# ============================================================

def build_range_string_from_width_text(
    width_text: Optional[str]
) -> Optional[str]:

    if width_text:
        return width_text.strip()

    return None


def width_from_range_text(
    width_text: Optional[str]
) -> int:
    """
    Convert something like [7:0] into width 8.
    """

    if not width_text:
        return 1

    nums = re.findall(
        r"-?\d+",
        width_text
    )

    if len(nums) == 2:

        msb = int(nums[0])
        lsb = int(nums[1])

        return abs(msb - lsb) + 1

    return 1


def build_dut_spec(
    top_module_name: str,
    top_block: str,
    spec: Optional[dict]
) -> dict:

    rtl_ports = extract_ports_from_module_block(
        top_block
    )

    port_names = [
        p["name"]
        for p in rtl_ports
    ]

    inferred_clocks, inferred_resets = (
        infer_clock_reset_ports(
            port_names
        )
    )

    reset_ports = []

    if spec:

        spec_sum = build_spec_summary(
            spec
        )

        spec_reset_name = spec_sum.get(
            "reset_name"
        )

        if spec_reset_name:

            reset_ports.append({
                "name": spec_reset_name,

                "active_low": bool(
                    spec_sum.get(
                        "reset_active_low"
                    )
                ),

                "synchronous": bool(
                    spec_sum.get(
                        "reset_synchronous"
                    )
                )
            })

    if not reset_ports:

        for rp in inferred_resets:

            reset_ports.append({
                "name": rp,

                "active_low": (
                    rp.lower().endswith("_n")
                    or "rst_n" in rp.lower()
                    or "reset_n" in rp.lower()
                ),

                "synchronous": True
            })

    dut_ports = []

    input_ports = []
    output_ports = []

    for p in rtl_ports:

        direction = (
            p["direction"]
            if p["direction"]
            else "input"
        )

        width = width_from_range_text(
            p["width_text"]
        )

        port_data = {
            "name": p["name"],
            "dir": direction,
            "direction": direction,
            "range": build_range_string_from_width_text(
                p["width_text"]
            ),
            "width": width
        }

        dut_ports.append(
            port_data
        )

        if (
            direction == "input"
            and p["name"] not in inferred_clocks
            and p["name"] not in [
                r["name"]
                for r in reset_ports
            ]
        ):

            input_ports.append(
                port_data
            )

        elif direction == "output":

            output_ports.append(
                port_data
            )

    return {
        "module_name": top_module_name,

        "ports": dut_ports,

        "input_ports": input_ports,

        "output_ports": output_ports,

        "clock_ports": inferred_clocks,

        "reset_ports": reset_ports
    }


# ============================================================
# Summary generation
# ============================================================

def build_human_summary(
    top_module_name: str,
    spec: Optional[dict],
    yaml_sane: Optional[bool],
    compile_tool: Optional[str],
    compile_passed: bool,
    spec_consistent: Optional[bool],
    rtl_sane: Optional[bool]
) -> str:

    if yaml_sane is False:
        return "YAML sanity failed"

    if (
        compile_tool is not None
        and not compile_passed
    ):
        return "Compile sanity failed"

    if spec_consistent is False:
        return "Spec ↔ RTL mismatch"

    if rtl_sane is False:
        return "RTL intrinsic sanity failed"

    if spec:

        spec_sum = build_spec_summary(
            spec
        )

        reset_name = spec_sum.get(
            "reset_name"
        )

        active_low = spec_sum.get(
            "reset_active_low"
        )

        synchronous = spec_sum.get(
            "reset_synchronous"
        )

        reset_desc = []

        if synchronous is True:
            reset_desc.append(
                "synchronous"
            )

        elif synchronous is False:
            reset_desc.append(
                "asynchronous"
            )

        if active_low is True:
            reset_desc.append(
                "active-low"
            )

        elif active_low is False:
            reset_desc.append(
                "active-high"
            )

        reset_phrase = ""

        if reset_name:

            if reset_desc:

                reset_phrase = (
                    " with {} reset ({})".format(
                        " ".join(reset_desc),
                        reset_name
                    )
                )

            else:

                reset_phrase = (
                    " with reset ({})".format(
                        reset_name
                    )
                )

        combinational_phrase = ""

        comb_text = (
            " ".join(
                spec_sum.get(
                    "correctness_criteria",
                    []
                )
            )
            + " "
            + spec_sum.get(
                "combinational_behavior",
                ""
            )
        )

        if (
            "tfaw_ok" in comb_text.lower()
            and "combinational"
            in comb_text.lower()
        ):

            combinational_phrase = (
                " and combinational "
                "tFAW_ok output"
            )

        return "Valid {}{}{}".format(
            top_module_name,
            reset_phrase,
            combinational_phrase
        )

    return "RTL passed validation"


# ============================================================
# Main validator
# ============================================================

def validate_rtl(
    path_in: str,
    output_json: str = "validation_result.json"
) -> dict:

    rtl_files = collect_rtl_files(
        path_in
    )

    # --------------------------------------------------------
    # RTL files required
    # --------------------------------------------------------

    if not rtl_files:

        result = {
            "is_valid": False,
            "severity": "critical",
            "error_code": "RTL_NOT_FOUND",

            "summary": "No RTL files found",

            "issues": [
                "No .sv or .v files found"
            ],

            "rtl_files": []
        }

        write_json(
            output_json,
            result
        )

        return result

    log(
        "Collected {} RTL file(s)".format(
            len(rtl_files)
        )
    )

    for f in rtl_files:
        log("  - {}".format(f))

    # --------------------------------------------------------
    # Read RTL
    # --------------------------------------------------------

    full_text = "\n\n".join(
        strip_comments_sv(
            read_text(f)
        )
        for f in rtl_files
    )

    # --------------------------------------------------------
    # Detect top module
    # --------------------------------------------------------

    top_module_name = detect_top_module(
        full_text
    )

    if not top_module_name:

        result = {
            "is_valid": False,
            "severity": "critical",
            "error_code": "TOP_MODULE_NOT_FOUND",

            "summary": "Could not detect top module",

            "issues": [
                "Top module detection failed"
            ],

            "rtl_files": rtl_files
        }

        write_json(
            output_json,
            result
        )

        return result

    # --------------------------------------------------------
    # Find top module file
    # --------------------------------------------------------

    top_module_file = None

    for f in rtl_files:

        txt = strip_comments_sv(
            read_text(f)
        )

        if re.search(
            (
                r"^\s*module\s+"
                + re.escape(top_module_name)
                + r"\b"
            ),
            txt,
            flags=re.MULTILINE
        ):

            top_module_file = f
            break

    log(
        "Top module detected: {}".format(
            top_module_name
        )
    )

    log(
        "Top module file: {}".format(
            top_module_file
        )
    )

    if not top_module_file:

        result = {
            "is_valid": False,
            "severity": "critical",
            "error_code": "TOP_MODULE_FILE_NOT_FOUND",

            "summary": (
                "Top module was detected but "
                "its RTL source file could not be found."
            ),

            "issues": [
                "Could not locate source file "
                "containing top module '{}'".format(
                    top_module_name
                )
            ],

            "rtl_files": rtl_files,

            "top_module_name": top_module_name,
            "top_module_file": None
        }

        write_json(
            output_json,
            result
        )

        return result

    # ========================================================
    # YAML IS REQUIRED
    # ========================================================

    yaml_file = find_matching_yaml_for_top(
        top_module_name,
        top_module_file
    )

    if not yaml_file:

        error_code = "YAML_SPEC_NOT_FOUND"

        design_directory = str(
            Path(
                top_module_file
            ).parent
        )

        log("")
        log(
            "ERROR [{}]: No matching YAML "
            "specification found.".format(
                error_code
            )
        )

        log(
            "Expected a .yaml or .yml "
            "specification in: {}".format(
                design_directory
            )
        )

        log(
            "Validation cannot continue without "
            "the YAML specification."
        )

        log(
            "RTL compile, RTL/YAML comparison, "
            "LLM review, Agent 2 and Agent 3 "
            "must not proceed."
        )

        result = {
            "is_valid": False,
            "severity": "critical",

            "error_code": error_code,

            "summary": (
                "Validation stopped: matching "
                "YAML specification not found."
            ),

            "issues": [
                (
                    "A YAML specification is required "
                    "for Agent 1 validation."
                ),
                (
                    "Agent 1 cannot verify RTL against "
                    "user constraints without the YAML "
                    "specification."
                ),
                (
                    "Expected YAML directory: {}".format(
                        design_directory
                    )
                )
            ],

            "rtl_files": rtl_files,

            "top_module_name": top_module_name,

            "top_module_file": top_module_file,

            "compile_tool": None,

            "compile_passed": False,

            "compile_output_snippet": "",

            "spec_file": None,

            "yaml_sane": False,

            "yaml_issues": [
                "Matching YAML specification not found."
            ],

            "spec_consistent": False,

            "spec_issues": [
                (
                    "Spec ↔ RTL validation could not "
                    "run because YAML is missing."
                )
            ],

            "rtl_sane": None,

            "rtl_issues": [],

            "dut_spec": None,

            "llm_raw_result": None
        }

        write_json(
            output_json,
            result
        )

        return result

    # ========================================================
    # YAML FOUND
    # ========================================================

    log(
        "Found matching YAML spec: {}".format(
            yaml_file
        )
    )

    spec = None

    yaml_sane = None
    yaml_issues = []

    spec_consistent = None
    spec_issues = []

    top_block = find_module_block(
        full_text,
        top_module_name
    )

    # --------------------------------------------------------
    # Parse and validate YAML
    # --------------------------------------------------------

    try:

        spec = load_yaml(
            yaml_file
        )

        yaml_sane, yaml_issues = (
            check_yaml_sanity(
                spec
            )
        )

        log(
            "YAML sanity: {}".format(
                "PASS"
                if yaml_sane
                else "FAIL"
            )
        )

        if yaml_sane:

            spec_consistent, spec_issues = (
                check_spec_vs_rtl(
                    top_module_name,
                    top_block,
                    spec
                )
            )

            log(
                "Spec ↔ RTL consistency: {}".format(
                    "PASS"
                    if spec_consistent
                    else "FAIL"
                )
            )

        else:

            spec_consistent = False

            spec_issues.append(
                "Skipping spec ↔ RTL consistency "
                "because YAML sanity failed"
            )

            log(
                "Spec ↔ RTL consistency: FAIL"
            )

    except Exception as e:

        yaml_sane = False

        yaml_issues = [
            "Failed to parse/check YAML spec: {}".format(
                e
            )
        ]

        spec_consistent = False

        spec_issues = [
            "Skipping spec ↔ RTL consistency "
            "because YAML parsing failed"
        ]

        log("YAML sanity: FAIL")

        log(
            "Spec ↔ RTL consistency: FAIL"
        )

    # --------------------------------------------------------
    # RTL intrinsic sanity
    # --------------------------------------------------------

    rtl_sane, rtl_issues = (
        check_rtl_intrinsic_sanity(
            top_module_name,
            top_block,
            full_text
        )
    )

    log(
        "RTL intrinsic sanity: {}".format(
            "PASS"
            if rtl_sane
            else "FAIL"
        )
    )

    # --------------------------------------------------------
    # Compile
    # --------------------------------------------------------

    (
        compile_tool,
        compile_passed,
        compile_output
    ) = run_compile_sanity(
        rtl_files,
        top_module_name
    )

    if compile_tool:

        log(
            "Compile sanity check ({}): {}".format(
                compile_tool,
                (
                    "PASS"
                    if compile_passed
                    else "FAIL"
                )
            )
        )

    else:

        log(
            "Compile sanity check: SKIPPED"
        )

    # --------------------------------------------------------
    # Collect blocking errors
    # --------------------------------------------------------

    blocking_issues = []

    if yaml_sane is False:

        blocking_issues.extend(
            yaml_issues
        )

    if spec_consistent is False:

        for issue in spec_issues:

            if issue not in blocking_issues:
                blocking_issues.append(issue)

    if rtl_sane is False:

        for issue in rtl_issues:

            if issue not in blocking_issues:
                blocking_issues.append(issue)

    if (
        compile_tool
        and not compile_passed
    ):

        blocking_issues.append(
            "Compile sanity check failed"
        )

        if compile_output:

            compile_lines = [
                ln
                for ln in compile_output.splitlines()
                if ln.strip()
            ]

            short_compile = "\n".join(
                compile_lines[:25]
            )

            blocking_issues.append(
                "VCS compile output:\n{}".format(
                    short_compile
                )
            )

    # --------------------------------------------------------
    # LLM review
    # --------------------------------------------------------

    llm_result = call_llm_review(
        top_module_name=top_module_name,
        rtl_text=full_text,

        spec_text=(
            json.dumps(
                spec,
                indent=2
            )
            if spec
            else None
        )
    )

    llm_result = (
        filter_llm_issues_with_yaml(
            llm_result,
            spec
        )
    )

    # Rule-based checks override LLM
    if blocking_issues:

        llm_result = dict(
            llm_result
        )

        llm_result[
            "is_valid"
        ] = False

        llm_result[
            "severity"
        ] = "high"

        llm_result[
            "summary"
        ] = (
            "Rule-based validation failed; "
            "LLM verdict overridden."
        )

        llm_result[
            "overridden_by_rule_checks"
        ] = True

    log(
        "LLM verdict: {}".format(
            "VALID"
            if llm_result.get(
                "is_valid",
                False
            )
            else "INVALID"
        )
    )

    # --------------------------------------------------------
    # Final issues
    # --------------------------------------------------------

    final_issues = []

    final_issues.extend(
        blocking_issues
    )

    for issue in llm_result.get(
        "issues",
        []
    ):

        if issue not in final_issues:
            final_issues.append(issue)

    # ========================================================
    # FINAL VALIDITY
    #
    # YAML is mandatory.
    #
    # Therefore:
    # - YAML must exist
    # - YAML must be sane
    # - YAML ↔ RTL must be consistent
    # - RTL sanity must pass
    # - Compile must pass when a compiler exists
    # - No unresolved issues may remain
    # ========================================================

    is_valid = (
        yaml_file is not None
        and spec is not None
        and bool(yaml_sane)
        and bool(spec_consistent)
        and bool(rtl_sane)
        and (
            compile_tool is None
            or bool(compile_passed)
        )
        and len(final_issues) == 0
    )

    severity = (
        "none"
        if is_valid
        else "high"
    )

    error_code = (
        None
        if is_valid
        else "VALIDATION_FAILED"
    )

    summary = build_human_summary(
        top_module_name=top_module_name,
        spec=spec,
        yaml_sane=yaml_sane,
        compile_tool=compile_tool,
        compile_passed=compile_passed,
        spec_consistent=spec_consistent,
        rtl_sane=rtl_sane
    )

    # --------------------------------------------------------
    # Generate DUT spec only after YAML exists
    # --------------------------------------------------------

    dut_spec = build_dut_spec(
        top_module_name,
        top_block,
        spec
    )

    result = {
        "is_valid": is_valid,

        "severity": severity,

        "error_code": error_code,

        "summary": summary,

        "issues": final_issues,

        "rtl_files": rtl_files,

        "top_module_name": top_module_name,

        "top_module_file": top_module_file,

        "compile_tool": compile_tool,

        "compile_passed": compile_passed,

        "compile_output_snippet": (
            compile_output[:4000]
            if compile_output
            else ""
        ),

        "spec_file": yaml_file,

        "yaml_sane": yaml_sane,

        "yaml_issues": yaml_issues,

        "spec_consistent": spec_consistent,

        "spec_issues": spec_issues,

        "rtl_sane": rtl_sane,

        "rtl_issues": rtl_issues,

        "dut_spec": dut_spec,

        "llm_raw_result": llm_result
    }

    write_json(
        output_json,
        result
    )

    return result


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser(
        description="Agent 1 RTL Validator"
    )

    parser.add_argument(
        "rtl_input",
        help="RTL file or directory"
    )

    parser.add_argument(
        "--out",
        default="validation_result.json",
        help="Output JSON path"
    )

    args = parser.parse_args()

    print(
        "\n"
        + "═" * 58
    )

    print(
        "  AGENT 1 — RTL VALIDATOR"
    )

    print(
        "═" * 58
        + "\n"
    )

    result = validate_rtl(
        args.rtl_input,
        args.out
    )

    print(
        "Validation Summary"
    )

    print(
        "  is_valid        : {}".format(
            result.get("is_valid")
        )
    )

    print(
        "  severity        : {}".format(
            result.get("severity")
        )
    )

    print(
        "  error_code      : {}".format(
            result.get("error_code")
        )
    )

    print(
        "  top_module_name : {}".format(
            result.get("top_module_name")
        )
    )

    print(
        "  top_module_file : {}".format(
            result.get("top_module_file")
        )
    )

    print(
        "  compile_tool    : {}".format(
            result.get("compile_tool")
        )
    )

    print(
        "  compile_passed  : {}".format(
            result.get("compile_passed")
        )
    )

    print(
        "  spec_file       : {}".format(
            result.get("spec_file")
        )
    )

    print(
        "  yaml_sane       : {}".format(
            result.get("yaml_sane")
        )
    )

    print(
        "  spec_consistent : {}".format(
            result.get("spec_consistent")
        )
    )

    print(
        "  rtl_sane        : {}".format(
            result.get("rtl_sane")
        )
    )

    print(
        "  summary         : {}".format(
            result.get("summary")
        )
    )

    print(
        "  issues:"
    )

    for issue in result.get(
        "issues",
        []
    ):

        if "\n" in issue:

            first = issue.split(
                "\n"
            )[0]

            print(
                "    - {}".format(
                    first
                )
            )

            for extra_ln in issue.split(
                "\n"
            )[1:]:

                print(
                    "      {}".format(
                        extra_ln
                    )
                )

        else:

            print(
                "    - {}".format(
                    issue
                )
            )

    compile_snippet = result.get(
        "compile_output_snippet",
        ""
    )

    if (
        compile_snippet
        and not result.get(
            "compile_passed",
            True
        )
    ):

        print(
            "  compile_output  :"
        )

        for ln in compile_snippet.splitlines()[:30]:

            print(
                "    {}".format(
                    ln
                )
            )

    # ========================================================
    # EXIT CODE FOR BACKEND PIPELINE
    # ========================================================

    if result.get("is_valid"):

        log(
            "PASS -> {}".format(
                args.out
            )
        )

        # Successful process
        sys.exit(0)

    else:

        log(
            "FAIL -> {}".format(
                args.out
            )
        )

        if result.get(
            "error_code"
        ):

            log(
                "ERROR CODE -> {}".format(
                    result.get(
                        "error_code"
                    )
                )
            )

        # Failed process.
        # Backend/next agent can detect this.
        sys.exit(1)