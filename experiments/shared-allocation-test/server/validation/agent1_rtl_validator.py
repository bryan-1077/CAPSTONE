#!/usr/bin/env python3

import os
import re
import sys
import json
import yaml
import shutil
import subprocess
from pathlib import Path


GENERATED_DIRS = {
    "verification_reports",
    "tb_auto",
    "build_auto",
    "tv_auto",
    "build_auto_tv",
    ".agent1_compile",
    "agent1",
    "agent2",
    "agent3",
    "backend",
    "gdsii",
    "validation2",
    "__pycache__",
}


# ============================================================
# BASIC HELPERS
# ============================================================

def log(msg):
    print(f"[RTL_VALIDATOR_AGENT] {msg}")


def read_text(path):
    with open(path, "r", errors="ignore") as f:
        return f.read()


def write_json(path, data):

    Path(path).parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(path, "w") as f:

        json.dump(
            data,
            f,
            indent=2
        )


def load_yaml(path):

    with open(path, "r") as f:

        return yaml.safe_load(f)


def strip_comments_sv(text):

    text = re.sub(
        r"/\*.*?\*/",
        "",
        text,
        flags=re.DOTALL
    )

    return re.sub(
        r"//.*",
        "",
        text
    )


def safe_int(
    value,
    default=None
):

    if isinstance(
        value,
        bool
    ):
        return default

    try:

        return int(
            value
        )

    except Exception:

        return default


def valid_hdl_identifier(value):

    return (
        isinstance(
            value,
            str
        )
        and
        bool(
            re.fullmatch(
                r"[A-Za-z_]\w*",
                value.strip()
            )
        )
    )


# ============================================================
# FILE DISCOVERY
# ============================================================

def collect_rtl_files(path_in):

    p = Path(
        path_in
    )

    # --------------------------------------------------------
    # Single RTL file
    # --------------------------------------------------------

    if p.is_file():

        if p.suffix.lower() in (
            ".sv",
            ".v"
        ):

            return [
                str(
                    p.resolve()
                )
            ]

        return []

    # --------------------------------------------------------
    # RTL directory
    # --------------------------------------------------------

    if not p.is_dir():

        return []

    files = []

    for candidate in p.rglob("*"):

        if not candidate.is_file():

            continue

        if candidate.suffix.lower() not in (
            ".sv",
            ".v"
        ):

            continue

        relative = candidate.relative_to(
            p
        )

        # ----------------------------------------------------
        # Ignore generated verification/backend RTL
        # ----------------------------------------------------

        if any(
            part in GENERATED_DIRS
            for part in relative.parts
        ):

            continue

        files.append(
            str(
                candidate.resolve()
            )
        )

    return sorted(
        set(
            files
        )
    )


def collect_yaml_files(path_in):

    p = Path(
        path_in
    )

    root = (
        p
        if p.is_dir()
        else p.parent
    )

    if not root.is_dir():

        return []

    files = []

    for candidate in root.rglob("*"):

        if not candidate.is_file():

            continue

        if candidate.suffix.lower() not in (
            ".yaml",
            ".yml"
        ):

            continue

        relative = candidate.relative_to(
            root
        )

        if any(
            part in GENERATED_DIRS
            for part in relative.parts
        ):

            continue

        files.append(
            str(
                candidate.resolve()
            )
        )

    return sorted(
        set(
            files
        )
    )


def find_matching_yaml(
    path_in,
    top_name
):

    yaml_files = collect_yaml_files(
        path_in
    )

    if not yaml_files:

        return None

    # --------------------------------------------------------
    # If exactly one YAML exists, use it
    # --------------------------------------------------------

    if len(
        yaml_files
    ) == 1:

        return yaml_files[
            0
        ]

    # --------------------------------------------------------
    # First try exact filename match
    # --------------------------------------------------------

    for candidate in yaml_files:

        if (
            Path(
                candidate
            ).stem.lower()
            ==
            (
                top_name
                or ""
            ).lower()
        ):

            return candidate

    # --------------------------------------------------------
    # Then inspect design_name in the YAML
    # --------------------------------------------------------

    if top_name:

        for candidate in yaml_files:

            try:

                spec = load_yaml(
                    candidate
                )

                if (
                    isinstance(
                        spec,
                        dict
                    )
                    and
                    spec.get(
                        "design_name"
                    )
                    ==
                    top_name
                ):

                    return candidate

            except Exception:

                pass

    return None


# ============================================================
# SYSTEMVERILOG PARSING
# ============================================================

def extract_module_blocks(text):

    pattern = re.compile(

        r"^\s*module\s+"
        r"([A-Za-z_]\w*)\b"
        r".*?"
        r"^\s*endmodule\b",

        re.MULTILINE
        |
        re.DOTALL
    )

    return [

        (
            match.group(1),
            match.group(0)
        )

        for match
        in pattern.finditer(
            text
        )
    ]


def detect_top_module(full_text):

    modules = [

        name

        for name, _
        in extract_module_blocks(
            full_text
        )
    ]

    if not modules:

        return None

    instantiated = set()

    # --------------------------------------------------------
    # Determine which modules are instantiated
    # --------------------------------------------------------

    for current, block in extract_module_blocks(
        full_text
    ):

        for candidate in modules:

            if candidate == current:

                continue

            pattern = (

                r"^\s*"
                +
                re.escape(
                    candidate
                )
                +
                r"(?:\s*#\s*\(.*?\))?"
                +
                r"\s+[A-Za-z_]\w*\s*\("
            )

            if re.search(

                pattern,

                block,

                re.MULTILINE
                |
                re.DOTALL
            ):

                instantiated.add(
                    candidate
                )

    tops = [

        name

        for name in modules

        if name not in instantiated
    ]

    if not tops:

        return modules[
            0
        ]

    preferred = [

        name

        for name in tops

        if any(

            word in name.lower()

            for word in (
                "top",
                "ctrl",
                "controller"
            )
        )
    ]

    return (
        preferred[0]
        if preferred
        else tops[0]
    )


def find_module_block(
    full_text,
    module_name
):

    for name, block in extract_module_blocks(
        full_text
    ):

        if name == module_name:

            return block

    return None


# ============================================================
# PARENTHESIS HELPERS
#
# Allows parameterized module declarations:
#
# module design #(
#     parameter DEPTH = 4
# ) (
#     ...
# );
# ============================================================

def find_matching_paren(
    text,
    open_index
):

    depth = 0

    for i in range(
        open_index,
        len(text)
    ):

        if text[i] == "(":

            depth += 1

        elif text[i] == ")":

            depth -= 1

            if depth == 0:

                return i

    return None


def extract_port_blob(
    block,
    module_name
):

    if not block:

        return None

    match = re.search(

        r"\bmodule\s+"
        +
        re.escape(
            module_name
        )
        +
        r"\b",

        block
    )

    if not match:

        return None

    position = match.end()

    while (
        position < len(
            block
        )
        and
        block[
            position
        ].isspace()
    ):

        position += 1

    # --------------------------------------------------------
    # Optional parameter list
    # --------------------------------------------------------

    if (
        position < len(
            block
        )
        and
        block[
            position
        ]
        ==
        "#"
    ):

        position += 1

        while (
            position < len(
                block
            )
            and
            block[
                position
            ].isspace()
        ):

            position += 1

        if (
            position >= len(
                block
            )
            or
            block[
                position
            ]
            !=
            "("
        ):

            return None

        end_parameters = find_matching_paren(

            block,

            position
        )

        if end_parameters is None:

            return None

        position = (
            end_parameters
            +
            1
        )

        while (
            position < len(
                block
            )
            and
            block[
                position
            ].isspace()
        ):

            position += 1

    # --------------------------------------------------------
    # Port list
    # --------------------------------------------------------

    if (
        position >= len(
            block
        )
        or
        block[
            position
        ]
        !=
        "("
    ):

        return None

    end_ports = find_matching_paren(

        block,

        position
    )

    if end_ports is None:

        return None

    return block[
        position + 1:
        end_ports
    ]


def split_top_level_commas(text):

    parts = []

    start = 0

    paren = 0
    bracket = 0
    brace = 0

    for i, char in enumerate(
        text
    ):

        if char == "(":

            paren += 1

        elif char == ")":

            paren = max(
                0,
                paren - 1
            )

        elif char == "[":

            bracket += 1

        elif char == "]":

            bracket = max(
                0,
                bracket - 1
            )

        elif char == "{":

            brace += 1

        elif char == "}":

            brace = max(
                0,
                brace - 1
            )

        elif (
            char == ","
            and
            paren == 0
            and
            bracket == 0
            and
            brace == 0
        ):

            parts.append(

                text[
                    start:i
                ].strip()
            )

            start = i + 1

    tail = text[
        start:
    ].strip()

    if tail:

        parts.append(
            tail
        )

    return parts


# ============================================================
# RTL PORT PARSER
# ============================================================

def extract_ports(
    block,
    module_name
):

    port_blob = extract_port_blob(

        block,

        module_name
    )

    if port_blob is None:

        return []

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

    ports = []

    current_direction = None
    current_width = None
    current_custom_type = None

    for item in split_top_level_commas(
        port_blob
    ):

        original_item = item.strip()

        direction = None

        # ----------------------------------------------------
        # Direction
        # ----------------------------------------------------

        if re.search(
            r"\binput\b",
            item
        ):

            direction = "input"

        elif re.search(
            r"\boutput\b",
            item
        ):

            direction = "output"

        elif re.search(
            r"\binout\b",
            item
        ):

            direction = "inout"

        # ----------------------------------------------------
        # Width expression
        #
        # May be:
        # [31:0]
        # [DEPTH-1:0]
        # [$clog2(DEPTH)-1:0]
        # ----------------------------------------------------

        width_match = re.search(

            r"(\[[^\]]+\])",

            item
        )

        width_text = (

            width_match.group(1)

            if width_match

            else None
        )

        # ----------------------------------------------------
        # Remove common declaration keywords
        # ----------------------------------------------------

        cleaned = re.sub(

            r"\b("
            r"input|"
            r"output|"
            r"inout|"
            r"wire|"
            r"reg|"
            r"logic|"
            r"bit|"
            r"signed|"
            r"unsigned|"
            r"var"
            r")\b",

            "",

            item
        )

        # Remove packed/unpacked dimensions
        cleaned = re.sub(

            r"\[[^\]]+\]",

            "",

            cleaned
        ).strip()

        # Remove default assignment if present
        cleaned = cleaned.split(
            "="
        )[0].strip()

        tokens = cleaned.split()

        if not tokens:

            continue

        name = tokens[
            -1
        ]

        if not valid_hdl_identifier(
            name
        ):

            continue

        # ----------------------------------------------------
        # Detect custom typedef
        #
        # Example:
        #
        # input request_t enq_req
        #
        # tokens become:
        #
        # request_t enq_req
        #
        # so request_t is the custom type.
        # ----------------------------------------------------

        custom_type = None

        if len(
            tokens
        ) > 1:

            custom_type = tokens[
                -2
            ]

        # ----------------------------------------------------
        # Carry declaration state for:
        #
        # input logic [3:0] a, b
        #
        # or
        #
        # output request_t a, b
        # ----------------------------------------------------

        if direction:

            current_direction = direction

            current_width = width_text

            current_custom_type = custom_type

        else:

            direction = current_direction

            if width_text is None:

                width_text = current_width

            if custom_type is None:

                custom_type = current_custom_type

        ports.append({

            "name":
                name,

            "direction":
                direction,

            "width_text":
                width_text,

            "declaration_text":
                original_item,

            "custom_type":
                custom_type,
        })

    # --------------------------------------------------------
    # Remove duplicates
    # --------------------------------------------------------

    unique = []

    seen = set()

    for port in ports:

        if port[
            "name"
        ] in seen:

            continue

        seen.add(
            port[
                "name"
            ]
        )

        unique.append(
            port
        )

    return unique


def width_from_range(
    width_text
):

    if not width_text:

        return 1

    match = re.fullmatch(

        r"\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]",

        width_text.strip()
    )

    if not match:

        return None

    msb = int(
        match.group(1)
    )

    lsb = int(
        match.group(2)
    )

    return abs(
        msb - lsb
    ) + 1


def resolved_rtl_port_width(port):
    """
    Return a numeric RTL width only when Agent 1 can
    determine that width safely.

    Examples:

        logic [31:0] data
            -> 32

        logic [DEPTH-1:0] valid
            -> None

        request_t enq_req
            -> None

        logic clk
            -> 1
    """

    width_text = port.get(
        "width_text"
    )

    if width_text:

        return width_from_range(
            width_text
        )

    # --------------------------------------------------------
    # Custom typedef could be more than one bit
    # --------------------------------------------------------

    if port.get(
        "custom_type"
    ):

        return None

    return 1


def infer_clock_reset(
    port_names
):

    clocks = []

    resets = []

    for name in port_names:

        lower = name.lower()

        if (
            "clk" in lower
            or
            "clock" in lower
        ):

            clocks.append(
                name
            )

        if (
            "rst" in lower
            or
            "reset" in lower
        ):

            resets.append(
                name
            )

    return (
        clocks,
        resets
    )


# ============================================================
# RESET STYLE DETECTION
# ============================================================

def detect_reset_style(
    block,
    reset_name
):

    result = {

        "found_reset_logic":
            False,

        "asynchronous":
            False,

        "synchronous":
            False,

        "active_low_detected":
            None,
    }

    if (
        not block
        or
        not reset_name
    ):

        return result

    reset = re.escape(
        reset_name
    )

    # --------------------------------------------------------
    # Check sensitivity lists
    # --------------------------------------------------------

    sensitivity_lists = re.findall(

        r"(?:always_ff|always)"
        r"\s*@\s*\((.*?)\)",

        block,

        flags=re.DOTALL
    )

    for sensitivity in sensitivity_lists:

        if re.search(

            r"(?:posedge|negedge)\s+"
            +
            reset
            +
            r"\b",

            sensitivity
        ):

            result[
                "asynchronous"
            ] = True

            if re.search(

                r"negedge\s+"
                +
                reset
                +
                r"\b",

                sensitivity
            ):

                result[
                    "active_low_detected"
                ] = True

            elif re.search(

                r"posedge\s+"
                +
                reset
                +
                r"\b",

                sensitivity
            ):

                result[
                    "active_low_detected"
                ] = False

    # --------------------------------------------------------
    # Reset conditions
    # --------------------------------------------------------

    low_patterns = [

        (
            r"\bif\s*\(\s*!\s*"
            +
            reset
            +
            r"\s*\)"
        ),

        (
            r"\bif\s*\(\s*~\s*"
            +
            reset
            +
            r"\s*\)"
        ),

        (
            r"\bif\s*\(\s*"
            +
            reset
            +
            r"\s*==\s*1'b0\s*\)"
        ),

        (
            r"\bif\s*\(\s*1'b0\s*==\s*"
            +
            reset
            +
            r"\s*\)"
        ),
    ]

    high_patterns = [

        (
            r"\bif\s*\(\s*"
            +
            reset
            +
            r"\s*\)"
        ),

        (
            r"\bif\s*\(\s*"
            +
            reset
            +
            r"\s*==\s*1'b1\s*\)"
        ),

        (
            r"\bif\s*\(\s*1'b1\s*==\s*"
            +
            reset
            +
            r"\s*\)"
        ),
    ]

    if any(

        re.search(
            pattern,
            block
        )

        for pattern in low_patterns
    ):

        result[
            "found_reset_logic"
        ] = True

        result[
            "active_low_detected"
        ] = True

    elif any(

        re.search(
            pattern,
            block
        )

        for pattern in high_patterns
    ):

        result[
            "found_reset_logic"
        ] = True

        result[
            "active_low_detected"
        ] = False

    # --------------------------------------------------------
    # If reset is used but not in sensitivity list,
    # treat it as synchronous
    # --------------------------------------------------------

    if (
        result[
            "found_reset_logic"
        ]
        and
        not result[
            "asynchronous"
        ]
    ):

        result[
            "synchronous"
        ] = True

    return result


def is_combinationally_assigned(
    block,
    signal_name
):

    signal = re.escape(
        signal_name
    )

    patterns = [

        (
            r"\bassign\s+"
            +
            signal
            +
            r"\s*="
        ),

        (
            r"\balways_comb\b"
            r".*?\b"
            +
            signal
            +
            r"\b\s*="
        ),

        (
            r"\balways\s*@\s*"
            r"\(\s*\*\s*\)"
            r".*?\b"
            +
            signal
            +
            r"\b\s*="
        ),
    ]

    return any(

        re.search(

            pattern,

            block or "",

            flags=re.DOTALL
        )

        for pattern in patterns
    )


def signal_declared(
    block,
    signal_name
):

    return bool(

        re.search(

            r"\b("
            r"logic|"
            r"reg|"
            r"wire|"
            r"bit|"
            r"integer|"
            r"int"
            r")\b"
            r"[^;]*\b"
            +
            re.escape(
                signal_name
            )
            +
            r"\b",

            block or ""
        )
    )


# ============================================================
# YAML HELPERS
# ============================================================

def yaml_width(value):

    if isinstance(
        value,
        bool
    ):

        return None

    if isinstance(
        value,
        int
    ):

        return value

    if isinstance(
        value,
        str
    ):

        value = value.strip()

        if value.isdigit():

            return int(
                value
            )

        if re.fullmatch(

            r"\[\s*-?\d+\s*:\s*-?\d+\s*\]",

            value
        ):

            return width_from_range(
                value
            )

    return None


def validate_string_list(
    value,
    field_name,
    errors
):

    if value is None:

        return

    if not isinstance(
        value,
        list
    ):

        errors.append(

            "{} must be a list.".format(
                field_name
            )
        )

        return

    for index, item in enumerate(
        value
    ):

        if (
            not isinstance(
                item,
                str
            )
            or
            not item.strip()
        ):

            errors.append(

                "{}[{}] must be a non-empty string.".format(
                    field_name,
                    index
                )
            )


# ============================================================
# IMPLEMENTATION CONSTRAINT NORMALIZATION
#
# SUPPORTS BOTH YAML STYLES
#
# STYLE 1:
#
# implementation_constraints:
#   - DEPTH parameter defaults to 4
#   - do not implement FIFO shifting
#
# STYLE 2:
#
# implementation_constraints:
#   required:
#     - use_cooldown_counter
#
#   forbidden:
#     - timestamp_tracking
# ============================================================

def normalize_implementation_constraints(value):

    required = []

    forbidden = []

    errors = []

    # --------------------------------------------------------
    # Not provided
    # --------------------------------------------------------

    if value is None:

        return (
            required,
            forbidden,
            errors
        )

    # ========================================================
    # LIST FORMAT
    # ========================================================

    if isinstance(
        value,
        list
    ):

        for index, item in enumerate(
            value
        ):

            if (
                not isinstance(
                    item,
                    str
                )
                or
                not item.strip()
            ):

                errors.append(

                    "implementation_constraints[{}] "
                    "must be a non-empty string.".format(
                        index
                    )
                )

            else:

                # Preserve the original statement.
                #
                # We deliberately do not automatically turn
                # statements containing "do not" into another
                # category because the original YAML wording
                # should remain the source of truth.

                required.append(
                    item.strip()
                )

        return (
            required,
            forbidden,
            errors
        )

    # ========================================================
    # OBJECT FORMAT
    # ========================================================

    if isinstance(
        value,
        dict
    ):

        required_value = value.get(
            "required",
            []
        )

        forbidden_value = value.get(
            "forbidden",
            []
        )

        if required_value is None:

            required_value = []

        if forbidden_value is None:

            forbidden_value = []

        # ----------------------------------------------------
        # Required constraints
        # ----------------------------------------------------

        if not isinstance(
            required_value,
            list
        ):

            errors.append(

                "implementation_constraints.required "
                "must be a list."
            )

        else:

            for index, item in enumerate(
                required_value
            ):

                if (
                    not isinstance(
                        item,
                        str
                    )
                    or
                    not item.strip()
                ):

                    errors.append(

                        "implementation_constraints.required[{}] "
                        "must be a non-empty string.".format(
                            index
                        )
                    )

                else:

                    required.append(
                        item.strip()
                    )

        # ----------------------------------------------------
        # Forbidden constraints
        # ----------------------------------------------------

        if not isinstance(
            forbidden_value,
            list
        ):

            errors.append(

                "implementation_constraints.forbidden "
                "must be a list."
            )

        else:

            for index, item in enumerate(
                forbidden_value
            ):

                if (
                    not isinstance(
                        item,
                        str
                    )
                    or
                    not item.strip()
                ):

                    errors.append(

                        "implementation_constraints.forbidden[{}] "
                        "must be a non-empty string.".format(
                            index
                        )
                    )

                else:

                    forbidden.append(
                        item.strip()
                    )

        # ----------------------------------------------------
        # Exact same statement cannot be required + forbidden
        # ----------------------------------------------------

        overlap = sorted(

            set(
                required
            ).intersection(
                forbidden
            )
        )

        for item in overlap:

            errors.append(

                "Implementation constraint '{}' "
                "is both required and forbidden.".format(
                    item
                )
            )

        return (
            required,
            forbidden,
            errors
        )

    # ========================================================
    # INVALID FORMAT
    # ========================================================

    errors.append(

        "implementation_constraints must be "
        "either a list or an object."
    )

    return (
        required,
        forbidden,
        errors
    )


# ============================================================
# REQUEST STRUCT VALIDATION
# ============================================================

def validate_request_struct(
    request_struct,
    errors,
    warnings
):

    if request_struct is None:

        return None

    if not isinstance(
        request_struct,
        dict
    ):

        errors.append(

            "behavior.request_struct must be "
            "an object when provided."
        )

        return None

    # --------------------------------------------------------
    # typedef name
    # --------------------------------------------------------

    typedef_name = request_struct.get(
        "typedef_name"
    )

    if (
        typedef_name is not None
        and
        not valid_hdl_identifier(
            typedef_name
        )
    ):

        errors.append(

            "behavior.request_struct.typedef_name "
            "must be a valid HDL identifier."
        )

    # --------------------------------------------------------
    # packed
    # --------------------------------------------------------

    packed = request_struct.get(
        "packed"
    )

    if (
        packed is not None
        and
        not isinstance(
            packed,
            bool
        )
    ):

        errors.append(

            "behavior.request_struct.packed "
            "must be boolean when provided."
        )

    # --------------------------------------------------------
    # fields
    # --------------------------------------------------------

    fields = request_struct.get(
        "fields",
        []
    )

    if (
        not isinstance(
            fields,
            list
        )
        or
        not fields
    ):

        errors.append(

            "behavior.request_struct.fields "
            "must be a non-empty list."
        )

        return None

    names = []

    total_width = 0

    width_known = True

    for index, field in enumerate(
        fields
    ):

        if not isinstance(
            field,
            dict
        ):

            errors.append(

                "behavior.request_struct.fields[{}] "
                "must be an object.".format(
                    index
                )
            )

            width_known = False

            continue

        name = field.get(
            "name"
        )

        width = yaml_width(

            field.get(
                "width"
            )
        )

        # ----------------------------------------------------
        # Field name
        # ----------------------------------------------------

        if not valid_hdl_identifier(
            name
        ):

            errors.append(

                "behavior.request_struct.fields[{}].name "
                "must be a valid HDL identifier.".format(
                    index
                )
            )

        else:

            names.append(
                name
            )

        # ----------------------------------------------------
        # Field width
        # ----------------------------------------------------

        if (
            width is None
            or
            width < 1
        ):

            errors.append(

                "behavior.request_struct.fields[{}].width "
                "must be an integer >= 1.".format(
                    index
                )
            )

            width_known = False

        else:

            total_width += width

    # --------------------------------------------------------
    # Duplicate struct fields
    # --------------------------------------------------------

    duplicates = sorted({

        name

        for name in names

        if names.count(
            name
        ) > 1
    })

    for name in duplicates:

        errors.append(

            "Duplicate request_struct field '{}'.".format(
                name
            )
        )

    if packed is False:

        warnings.append(

            "behavior.request_struct.packed is false; "
            "flattened bit-width comparisons may not apply."
        )

    if width_known:

        return total_width

    return None


# ============================================================
# YAML VALIDATION
#
# YAML IS VALIDATED BEFORE RTL.
#
# IF YAML FAILS, AGENT 1 STOPS.
# ============================================================

def validate_yaml(spec):

    errors = []

    warnings = []

    checks = [

        "YAML syntax/root structure",

        "design_name and design_type",

        "clock definition",

        "reset definition",

        "YAML port names/directions/widths",

        "duplicate YAML ports",

        "clock/reset signal validity",

        "behavior field types and numeric ranges",

        "request_struct definition when present",

        "correctness criteria and behavior rules",

        "invariants",

        "implementation constraints",

        "forbidden patterns",

        "verification section",
    ]

    # ========================================================
    # ROOT
    # ========================================================

    if not isinstance(
        spec,
        dict
    ):

        return (

            False,

            [
                "YAML root must be a mapping/object."
            ],

            warnings,

            checks
        )

    # ========================================================
    # DESIGN NAME
    # ========================================================

    if not valid_hdl_identifier(

        spec.get(
            "design_name"
        )
    ):

        errors.append(

            "design_name must be a valid HDL identifier."
        )

    # ========================================================
    # DESIGN TYPE
    # ========================================================

    if (
        "design_type"
        in spec
        and
        not isinstance(
            spec.get(
                "design_type"
            ),
            str
        )
    ):

        errors.append(

            "design_type must be a string when provided."
        )

    # ========================================================
    # CLOCK
    # ========================================================

    clock = spec.get(
        "clock"
    )

    if not isinstance(
        clock,
        dict
    ):

        errors.append(

            "clock must be an object containing clock.name."
        )

        clock = {}

    elif not valid_hdl_identifier(

        clock.get(
            "name"
        )
    ):

        errors.append(

            "clock.name must be a valid HDL identifier."
        )

    # ========================================================
    # RESET
    # ========================================================

    reset = spec.get(
        "reset"
    )

    if not isinstance(
        reset,
        dict
    ):

        errors.append(

            "reset must be an object containing reset.name."
        )

        reset = {}

    else:

        if not valid_hdl_identifier(

            reset.get(
                "name"
            )
        ):

            errors.append(

                "reset.name must be a valid HDL identifier."
            )

        if (
            "active_low"
            in reset
            and
            not isinstance(
                reset.get(
                    "active_low"
                ),
                bool
            )
        ):

            errors.append(

                "reset.active_low must be boolean."
            )

        if (
            "synchronous"
            in reset
            and
            not isinstance(
                reset.get(
                    "synchronous"
                ),
                bool
            )
        ):

            errors.append(

                "reset.synchronous must be boolean."
            )

    # --------------------------------------------------------
    # Clock and reset cannot be identical
    # --------------------------------------------------------

    if (
        clock.get(
            "name"
        )
        and
        reset.get(
            "name"
        )
        and
        clock.get(
            "name"
        )
        ==
        reset.get(
            "name"
        )
    ):

        errors.append(

            "clock.name and reset.name "
            "cannot be the same signal."
        )

    # ========================================================
    # PORTS
    #
    # Both frontend formats are supported:
    #
    # clock/reset may appear inside ports,
    # OR clock/reset may only be top-level fields.
    # ========================================================

    ports = spec.get(
        "ports",
        []
    )

    if not isinstance(
        ports,
        list
    ):

        errors.append(

            "ports must be a list."
        )

        ports = []

    names = []

    port_map = {}

    for index, port in enumerate(
        ports
    ):

        if not isinstance(
            port,
            dict
        ):

            errors.append(

                "ports[{}] must be an object.".format(
                    index
                )
            )

            continue

        name = port.get(
            "name"
        )

        direction = port.get(
            "direction"
        )

        width_raw = port.get(
            "width",
            1
        )

        width = yaml_width(
            width_raw
        )

        # ----------------------------------------------------
        # Port name
        # ----------------------------------------------------

        if not valid_hdl_identifier(
            name
        ):

            errors.append(

                "ports[{}].name must be "
                "a valid HDL identifier.".format(
                    index
                )
            )

        else:

            names.append(
                name
            )

            port_map[
                name
            ] = port

        # ----------------------------------------------------
        # Port direction
        # ----------------------------------------------------

        if direction not in (
            "input",
            "output",
            "inout"
        ):

            errors.append(

                "Port '{}' has invalid direction '{}'.".format(
                    name,
                    direction
                )
            )

        # ----------------------------------------------------
        # Port width
        # ----------------------------------------------------

        if (
            width is None
            or
            width < 1
        ):

            errors.append(

                "Port '{}' has invalid width '{}'.".format(
                    name,
                    width_raw
                )
            )

    # --------------------------------------------------------
    # Duplicate ports
    # --------------------------------------------------------

    duplicates = sorted({

        name

        for name in names

        if names.count(
            name
        ) > 1
    })

    for name in duplicates:

        errors.append(

            "Duplicate YAML port '{}'.".format(
                name
            )
        )

    # --------------------------------------------------------
    # If clock/reset ARE listed in ports,
    # they must be inputs.
    # --------------------------------------------------------

    special_signals = [

        (
            clock.get(
                "name"
            ),
            "Clock"
        ),

        (
            reset.get(
                "name"
            ),
            "Reset"
        ),
    ]

    for (
        special_name,
        special_type
    ) in special_signals:

        if (
            special_name
            in port_map
            and
            port_map[
                special_name
            ].get(
                "direction"
            )
            !=
            "input"
        ):

            errors.append(

                "{} '{}' must be an input "
                "if it is listed in ports.".format(
                    special_type,
                    special_name
                )
            )

    # ========================================================
    # BEHAVIOR
    # ========================================================

    behavior = spec.get(
        "behavior",
        {}
    )

    if behavior is None:

        behavior = {}

    if not isinstance(
        behavior,
        dict
    ):

        errors.append(

            "behavior must be an object when provided."
        )

        behavior = {}

    # --------------------------------------------------------
    # String behavior fields
    # --------------------------------------------------------

    for key in (

        "combinational_behavior",

        "sequential_behavior",

        "implementation_type"
    ):

        if (
            key in behavior
            and
            not isinstance(
                behavior.get(
                    key
                ),
                str
            )
        ):

            errors.append(

                "behavior.{} must be a string.".format(
                    key
                )
            )

    # --------------------------------------------------------
    # Correctness criteria
    # --------------------------------------------------------

    validate_string_list(

        behavior.get(
            "correctness_criteria"
        ),

        "behavior.correctness_criteria",

        errors
    )

    # --------------------------------------------------------
    # Rules
    # --------------------------------------------------------

    validate_string_list(

        behavior.get(
            "rules"
        ),

        "behavior.rules",

        errors
    )

    # ========================================================
    # INTERNAL SIGNALS
    # ========================================================

    internal_signals = behavior.get(
        "internal_signals"
    )

    if (
        internal_signals
        is not None
        and
        not isinstance(
            internal_signals,
            dict
        )
    ):

        errors.append(

            "behavior.internal_signals must "
            "be an object when provided."
        )

    elif isinstance(
        internal_signals,
        dict
    ):

        for (
            signal_name,
            signal_info
        ) in internal_signals.items():

            if not valid_hdl_identifier(
                signal_name
            ):

                errors.append(

                    "Internal signal '{}' is not "
                    "a valid HDL identifier.".format(
                        signal_name
                    )
                )

            if not isinstance(
                signal_info,
                dict
            ):

                errors.append(

                    "behavior.internal_signals.{} "
                    "must be an object.".format(
                        signal_name
                    )
                )

    # ========================================================
    # REQUEST STRUCT
    # ========================================================

    validate_request_struct(

        behavior.get(
            "request_struct"
        ),

        errors,

        warnings
    )

    # ========================================================
    # GENERIC NUMERIC SANITY
    #
    # Supports common frontend fields such as:
    #
    # tRRD_cycles
    # tFAW_cycles
    # depth_default
    # window_size
    # timestamp_width
    # timestamp_count
    # ========================================================

    for key, value in behavior.items():

        key_lower = str(
            key
        ).lower()

        # ----------------------------------------------------
        # Cycle values
        # ----------------------------------------------------

        if key_lower.endswith(
            "_cycles"
        ):

            parsed = safe_int(
                value
            )

            if (
                parsed is None
                or
                parsed < 0
            ):

                errors.append(

                    "behavior.{} must be "
                    "an integer >= 0.".format(
                        key
                    )
                )

        # ----------------------------------------------------
        # Width/count/depth
        # ----------------------------------------------------

        elif (

            key_lower.endswith(
                "_width"
            )

            or

            key_lower.endswith(
                "_count"
            )

            or

            key_lower in (
                "window_size",
                "depth",
                "depth_default"
            )
        ):

            parsed = safe_int(
                value
            )

            if (
                parsed is None
                or
                parsed < 1
            ):

                errors.append(

                    "behavior.{} must be "
                    "an integer >= 1.".format(
                        key
                    )
                )

    # ========================================================
    # INVARIANTS
    # ========================================================

    validate_string_list(

        spec.get(
            "invariants"
        ),

        "invariants",

        errors
    )

    # ========================================================
    # FORBIDDEN PATTERNS
    # ========================================================

    validate_string_list(

        spec.get(
            "forbidden_patterns"
        ),

        "forbidden_patterns",

        errors
    )

    # ========================================================
    # IMPLEMENTATION CONSTRAINTS
    #
    # IMPORTANT CHANGE:
    #
    # LIST OR OBJECT ARE BOTH VALID.
    # ========================================================

    (
        implementation_required,
        implementation_forbidden,
        implementation_errors
    ) = normalize_implementation_constraints(

        spec.get(
            "implementation_constraints"
        )
    )

    errors.extend(
        implementation_errors
    )

    # ========================================================
    # VERIFICATION
    # ========================================================

    verification = spec.get(
        "verification",
        {}
    )

    if (
        verification is not None
        and
        not isinstance(
            verification,
            dict
        )
    ):

        errors.append(

            "verification must be an object when provided."
        )

    return (

        len(
            errors
        )
        ==
        0,

        errors,

        warnings,

        checks
    )


# ============================================================
# SPEC SUMMARY
# ============================================================

def build_spec_summary(spec):

    clock = (

        spec.get(
            "clock",
            {}
        )

        if isinstance(
            spec.get(
                "clock"
            ),
            dict
        )

        else {}
    )

    reset = (

        spec.get(
            "reset",
            {}
        )

        if isinstance(
            spec.get(
                "reset"
            ),
            dict
        )

        else {}
    )

    behavior = (

        spec.get(
            "behavior",
            {}
        )

        if isinstance(
            spec.get(
                "behavior"
            ),
            dict
        )

        else {}
    )

    # --------------------------------------------------------
    # Support both implementation constraint formats
    # --------------------------------------------------------

    (
        implementation_required,
        implementation_forbidden,
        _
    ) = normalize_implementation_constraints(

        spec.get(
            "implementation_constraints"
        )
    )

    # ========================================================
    # PORT MAP
    # ========================================================

    port_map = {}

    ports = (

        spec.get(
            "ports",
            []
        )

        if isinstance(
            spec.get(
                "ports"
            ),
            list
        )

        else []
    )

    for port in ports:

        if (
            isinstance(
                port,
                dict
            )
            and
            port.get(
                "name"
            )
        ):

            port_map[
                port[
                    "name"
                ]
            ] = {

                "direction":
                    port.get(
                        "direction"
                    ),

                "width":
                    yaml_width(

                        port.get(
                            "width",
                            1
                        )
                    ),

                "description":
                    port.get(
                        "description"
                    ),
            }

    # ========================================================
    # REQUEST STRUCT WIDTH
    #
    # Example current request_t:
    #
    # bank     2
    # row     10
    # col      6
    # is_write 1
    # wdata   32
    #
    # Total = 51 bits
    # ========================================================

    request_struct_width = None

    request_struct = behavior.get(
        "request_struct"
    )

    if (
        isinstance(
            request_struct,
            dict
        )
        and
        isinstance(
            request_struct.get(
                "fields"
            ),
            list
        )
    ):

        widths = []

        for field in request_struct.get(
            "fields",
            []
        ):

            if not isinstance(
                field,
                dict
            ):

                widths = []

                break

            width = yaml_width(

                field.get(
                    "width"
                )
            )

            if width is None:

                widths = []

                break

            widths.append(
                width
            )

        if widths:

            request_struct_width = sum(
                widths
            )

    return {

        "design_name":
            spec.get(
                "design_name"
            ),

        "design_type":
            spec.get(
                "design_type"
            ),

        "clock_name":
            clock.get(
                "name"
            ),

        "reset_name":
            reset.get(
                "name"
            ),

        "reset_active_low":
            reset.get(
                "active_low"
            ),

        "reset_synchronous":
            reset.get(
                "synchronous"
            ),

        "ports":
            port_map,

        "behavior":
            behavior,

        "correctness_criteria":
            behavior.get(
                "correctness_criteria",
                []
            ),

        "rules":
            behavior.get(
                "rules",
                []
            ),

        "combinational_behavior":
            behavior.get(
                "combinational_behavior",
                ""
            ),

        "sequential_behavior":
            behavior.get(
                "sequential_behavior",
                ""
            ),

        "implementation_type":
            behavior.get(
                "implementation_type"
            ),

        "internal_signals":
            behavior.get(
                "internal_signals",
                {}
            ),

        "request_struct":
            request_struct or {},

        "request_struct_width":
            request_struct_width,

        "invariants":
            spec.get(
                "invariants",
                []
            ),

        "implementation_required":
            implementation_required,

        "implementation_forbidden":
            implementation_forbidden,

        "forbidden_patterns":
            spec.get(
                "forbidden_patterns",
                []
            ),

        "verification":
            spec.get(
                "verification",
                {}
            ),
    }


# ============================================================
# DESIGN INTENT
#
# WHAT DID THE USER ASK FOR?
# ============================================================

def build_design_intent(spec):

    summary = build_spec_summary(
        spec
    )

    purpose_parts = []

    if summary.get(
        "implementation_type"
    ):

        purpose_parts.append(

            "Implementation type: {}.".format(

                summary[
                    "implementation_type"
                ]
            )
        )

    if summary.get(
        "combinational_behavior"
    ):

        purpose_parts.append(

            summary[
                "combinational_behavior"
            ].strip()
        )

    if summary.get(
        "sequential_behavior"
    ):

        purpose_parts.append(

            summary[
                "sequential_behavior"
            ].strip()
        )

    if (
        summary.get(
            "rules"
        )
        and
        not purpose_parts
    ):

        purpose_parts.append(

            "Behavior is defined by the YAML rules."
        )

    purpose = (

        " ".join(
            purpose_parts
        )

        if purpose_parts

        else

        "Defined by the YAML requirements."
    )

    return {

        "design_name":
            summary[
                "design_name"
            ],

        "design_type":
            summary[
                "design_type"
            ],

        "purpose":
            purpose,

        "clock": {

            "name":
                summary[
                    "clock_name"
                ]
        },

        "reset": {

            "name":
                summary[
                    "reset_name"
                ],

            "active_low":
                summary[
                    "reset_active_low"
                ],

            "synchronous":
                summary[
                    "reset_synchronous"
                ],
        },

        "yaml_ports": [

            {

                "name":
                    name,

                "direction":
                    info[
                        "direction"
                    ],

                "width":
                    info[
                        "width"
                    ],

                "description":
                    info.get(
                        "description"
                    ),
            }

            for name, info
            in summary[
                "ports"
            ].items()
        ],

        "behavior":
            summary[
                "behavior"
            ],

        "rules":
            summary[
                "rules"
            ],

        "request_struct_width":
            summary[
                "request_struct_width"
            ],

        "invariants":
            summary[
                "invariants"
            ],

        "implementation_required":
            summary[
                "implementation_required"
            ],

        "implementation_forbidden":
            summary[
                "implementation_forbidden"
            ],

        "forbidden_patterns":
            summary[
                "forbidden_patterns"
            ],

        "verification_targets":
            summary[
                "verification"
            ],
    }


# ============================================================
# RTL STATIC VALIDATION
# ============================================================

def check_rtl(
    top_name,
    top_block
):

    errors = []

    warnings = []

    checks = [

        "top module exists",

        "ANSI-style top-level port parsing",

        "obvious non-SystemVerilog/AST junk patterns",

        "basic RTL structure",
    ]

    if not top_block:

        return (

            False,

            [
                "Could not find top module block "
                "for '{}'.".format(
                    top_name
                )
            ],

            warnings,

            checks
        )

    ports = extract_ports(

        top_block,

        top_name
    )

    if not ports:

        errors.append(

            "Top module '{}' has no parseable "
            "ANSI-style ports.".format(
                top_name
            )
        )

    # --------------------------------------------------------
    # Detect obvious generation junk
    # --------------------------------------------------------

    bad_patterns = [

        r"SignalRef\s*\(",

        r"Compare\s*\(",

        r"Const\s*\(",

        r"next_state_node",

        r"object representation",
    ]

    for pattern in bad_patterns:

        if re.search(
            pattern,
            top_block
        ):

            errors.append(

                "RTL contains suspicious "
                "non-SystemVerilog pattern '{}'.".format(
                    pattern
                )
            )

            break

    return (

        len(
            errors
        )
        ==
        0,

        errors,

        warnings,

        checks
    )


# ============================================================
# YAML <-> RTL VALIDATION
# ============================================================

def compare_yaml_rtl(
    top_name,
    top_block,
    spec
):

    errors = []

    warnings = []

    checks = [

        "top module name vs YAML design_name",

        "YAML clock exists in RTL",

        "YAML reset exists in RTL",

        "reset synchronous/asynchronous style",

        "reset active-high/active-low polarity",

        "YAML ports exist in RTL",

        "YAML port directions",

        "YAML port widths when statically resolvable",

        "YAML-declared internal signal presence",

        (
            "outputs named in combinational_behavior "
            "are combinationally driven"
        ),
    ]

    summary = build_spec_summary(
        spec
    )

    rtl_ports = extract_ports(

        top_block,

        top_name
    )

    rtl_map = {

        port[
            "name"
        ]:
            port

        for port
        in rtl_ports
    }

    # ========================================================
    # MODULE NAME
    # ========================================================

    if (
        summary[
            "design_name"
        ]
        !=
        top_name
    ):

        errors.append(

            "Top module '{}' does not match "
            "YAML design_name '{}'.".format(

                top_name,

                summary[
                    "design_name"
                ]
            )
        )

    # ========================================================
    # CLOCK
    # ========================================================

    clock_name = summary[
        "clock_name"
    ]

    if (
        clock_name
        and
        clock_name
        not in rtl_map
    ):

        errors.append(

            "Clock '{}' from YAML is "
            "missing from RTL ports.".format(
                clock_name
            )
        )

    elif clock_name:

        if (
            rtl_map[
                clock_name
            ].get(
                "direction"
            )
            not in (
                None,
                "input"
            )
        ):

            errors.append(

                "Clock '{}' must be an RTL input.".format(
                    clock_name
                )
            )

    # ========================================================
    # RESET
    # ========================================================

    reset_name = summary[
        "reset_name"
    ]

    if (
        reset_name
        and
        reset_name
        not in rtl_map
    ):

        errors.append(

            "Reset '{}' from YAML is "
            "missing from RTL ports.".format(
                reset_name
            )
        )

    elif reset_name:

        if (
            rtl_map[
                reset_name
            ].get(
                "direction"
            )
            not in (
                None,
                "input"
            )
        ):

            errors.append(

                "Reset '{}' must be an RTL input.".format(
                    reset_name
                )
            )

        reset_style = detect_reset_style(

            top_block,

            reset_name
        )

        expected_sync = summary[
            "reset_synchronous"
        ]

        expected_active_low = summary[
            "reset_active_low"
        ]

        # ----------------------------------------------------
        # Reset logic must be present
        # ----------------------------------------------------

        if not reset_style[
            "found_reset_logic"
        ]:

            errors.append(

                "Reset logic using '{}' "
                "was not found in the top module.".format(
                    reset_name
                )
            )

        else:

            # ------------------------------------------------
            # Sync / async
            # ------------------------------------------------

            if (
                expected_sync
                is False
                and
                not reset_style[
                    "asynchronous"
                ]
            ):

                errors.append(

                    "YAML requires asynchronous reset '{}', "
                    "but RTL does not show it in the "
                    "sensitivity list.".format(
                        reset_name
                    )
                )

            if (
                expected_sync
                is True
                and
                reset_style[
                    "asynchronous"
                ]
            ):

                errors.append(

                    "YAML requires synchronous reset '{}', "
                    "but RTL appears asynchronous.".format(
                        reset_name
                    )
                )

            # ------------------------------------------------
            # Reset polarity
            # ------------------------------------------------

            detected_active_low = (

                reset_style[
                    "active_low_detected"
                ]
            )

            if (
                expected_active_low
                is not None
                and
                detected_active_low
                is not None
                and
                expected_active_low
                !=
                detected_active_low
            ):

                errors.append(

                    "Reset polarity mismatch for '{}': "
                    "YAML active_low={} "
                    "RTL detected active_low={}.".format(

                        reset_name,

                        expected_active_low,

                        detected_active_low
                    )
                )

    # ========================================================
    # YAML PORTS
    # ========================================================

    for (
        port_name,
        expected
    ) in summary[
        "ports"
    ].items():

        if port_name not in rtl_map:

            errors.append(

                "Missing YAML-required RTL port '{}'.".format(
                    port_name
                )
            )

            continue

        actual = rtl_map[
            port_name
        ]

        # ----------------------------------------------------
        # Direction
        # ----------------------------------------------------

        if (
            actual.get(
                "direction"
            )
            and
            expected.get(
                "direction"
            )
            and
            actual[
                "direction"
            ]
            !=
            expected[
                "direction"
            ]
        ):

            errors.append(

                "Port '{}' direction mismatch: "
                "YAML={} RTL={}.".format(

                    port_name,

                    expected[
                        "direction"
                    ],

                    actual[
                        "direction"
                    ]
                )
            )

        # ----------------------------------------------------
        # Width
        #
        # Only compare when RTL width can be determined safely.
        #
        # If RTL is request_t or [DEPTH-1:0], Agent 1 reports
        # a warning instead of falsely assuming one bit.
        # ----------------------------------------------------

        rtl_width = resolved_rtl_port_width(
            actual
        )

        yaml_port_width = expected.get(
            "width"
        )

        if (
            yaml_port_width
            is not None
            and
            rtl_width
            is not None
            and
            yaml_port_width
            !=
            rtl_width
        ):

            errors.append(

                "Port '{}' width mismatch: "
                "YAML={} RTL={}.".format(

                    port_name,

                    yaml_port_width,

                    rtl_width
                )
            )

        elif (
            yaml_port_width
            is not None
            and
            rtl_width
            is None
        ):

            warnings.append(

                "Port '{}' RTL width is parameterized "
                "or typedef-based, so Agent 1 could not "
                "statically compare its numeric width "
                "to YAML={}. VCS compilation still "
                "checks type legality.".format(

                    port_name,

                    yaml_port_width
                )
            )

    # ========================================================
    # INTERNAL SIGNALS
    # ========================================================

    internal_signals = summary.get(
        "internal_signals",
        {}
    )

    if isinstance(
        internal_signals,
        dict
    ):

        for signal_name in internal_signals.keys():

            if not signal_declared(

                top_block,

                signal_name
            ):

                errors.append(

                    "YAML internal signal '{}' "
                    "was not found as a simple "
                    "RTL declaration.".format(
                        signal_name
                    )
                )

    # ========================================================
    # COMBINATIONAL BEHAVIOR
    # ========================================================

    combinational_text = (

        summary.get(
            "combinational_behavior",
            ""
        )

        or ""
    )

    for (
        port_name,
        port_info
    ) in summary[
        "ports"
    ].items():

        if (
            port_info.get(
                "direction"
            )
            ==
            "output"
            and
            re.search(

                r"\b"
                +
                re.escape(
                    port_name
                )
                +
                r"\b",

                combinational_text
            )
        ):

            if not is_combinationally_assigned(

                top_block,

                port_name
            ):

                errors.append(

                    "YAML combinational_behavior describes "
                    "output '{}', but RTL does not clearly "
                    "drive it with assign/always_comb/"
                    "always @(*).".format(
                        port_name
                    )
                )

    return (

        len(
            errors
        )
        ==
        0,

        errors,

        warnings,

        checks
    )


# ============================================================
# COMPILE VALIDATION
# ============================================================

def compile_rtl(
    rtl_files,
    top_name,
    work_dir=".agent1_compile"
):

    # --------------------------------------------------------
    # Prefer VCS
    # --------------------------------------------------------

    if shutil.which(
        "vcs"
    ):

        tool = "vcs"

    elif shutil.which(
        "iverilog"
    ):

        tool = "iverilog"

    else:

        return (

            None,

            False,

            "No VCS or iverilog executable was found."
        )

    os.makedirs(
        work_dir,
        exist_ok=True
    )

    # ========================================================
    # VCS
    # ========================================================

    if tool == "vcs":

        command = [

            "vcs",

            "-sverilog",

            "-full64",

            "-timescale=1ns/1ps",

            "-top",
            top_name,

            "-o",
            os.path.join(
                work_dir,
                "simv"
            ),

        ] + ['+incdir+' + directory for directory in sorted({os.path.dirname(os.path.abspath(p)) for p in rtl_files})] + rtl_files

    # ========================================================
    # IVERILOG
    # ========================================================

    else:

        command = [

            "iverilog",

            "-g2012",

            "-s",
            top_name,

            "-o",
            os.path.join(
                work_dir,
                "a.out"
            ),

        ] + rtl_files

    try:

        process = subprocess.run(

            command,

            stdout=subprocess.PIPE,

            stderr=subprocess.PIPE,

            universal_newlines=True,

            timeout=60
        )

        output = (

            (
                process.stdout
                or ""
            )

            +
            "\n"

            +
            (
                process.stderr
                or ""
            )
        )

        return (

            tool,

            process.returncode
            ==
            0,

            output.strip()
        )

    except Exception as error:

        return (

            tool,

            False,

            "Compile exception: {}".format(
                error
            )
        )


# ============================================================
# DUT SPEC
#
# USED BY AGENT 2 AND AGENT 3
# ============================================================

def build_dut_spec(
    top_name,
    top_block,
    spec
):

    rtl_ports = extract_ports(

        top_block,

        top_name
    )

    summary = build_spec_summary(
        spec
    )

    port_names = [

        port[
            "name"
        ]

        for port
        in rtl_ports
    ]

    (
        inferred_clocks,
        inferred_resets
    ) = infer_clock_reset(
        port_names
    )

    # ========================================================
    # CLOCK
    # ========================================================

    clocks = (

        [
            summary[
                "clock_name"
            ]
        ]

        if summary.get(
            "clock_name"
        )

        else

        inferred_clocks
    )

    # ========================================================
    # RESET
    # ========================================================

    resets = []

    if summary.get(
        "reset_name"
    ):

        active_low = bool(

            summary.get(
                "reset_active_low"
            )
        )

        synchronous = bool(

            summary.get(
                "reset_synchronous"
            )
        )

        resets.append({

            "name":
                summary[
                    "reset_name"
                ],

            "active_low":
                active_low,

            "synchronous":
                synchronous,

            # ------------------------------------------------
            # Agent 3 compatibility
            # ------------------------------------------------

            "active":

                (
                    "low"

                    if active_low

                    else

                    "high"
                ),

            "style":

                (
                    "sync"

                    if synchronous

                    else

                    "async"
                ),
        })

    else:

        for name in inferred_resets:

            active_low = (

                name.lower().endswith(
                    "_n"
                )
            )

            resets.append({

                "name":
                    name,

                "active_low":
                    active_low,

                "synchronous":
                    True,

                "active":

                    (
                        "low"

                        if active_low

                        else

                        "high"
                    ),

                "style":
                    "sync",
            })

    reset_names = {

        reset[
            "name"
        ]

        for reset
        in resets
    }

    # ========================================================
    # PORTS
    # ========================================================

    dut_ports = []

    input_ports = []

    output_ports = []

    for port in rtl_ports:

        direction = (

            port.get(
                "direction"
            )

            or

            "input"
        )

        width = resolved_rtl_port_width(
            port
        )

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # If the RTL uses:
        #
        # request_t enq_req
        #
        # or:
        #
        # logic [DEPTH-1:0] req_valid
        #
        # Agent 1 may not be able to calculate the numeric RTL
        # width from text alone.
        #
        # Since the YAML has already passed validation, use its
        # width as the downstream Agent 2/3 width.
        # ----------------------------------------------------

        if (
            width is None
            and
            port[
                "name"
            ]
            in
            summary[
                "ports"
            ]
        ):

            width = summary[
                "ports"
            ][
                port[
                    "name"
                ]
            ].get(
                "width"
            )

        if width is None:

            width = 1

        item = {

            "name":
                port[
                    "name"
                ],

            "dir":
                direction,

            "direction":
                direction,

            "range":
                port.get(
                    "width_text"
                ),

            "width":
                width,
        }

        dut_ports.append(
            item
        )

        # ----------------------------------------------------
        # Inputs controllable by Agent 3
        #
        # Clock/reset are excluded.
        # ----------------------------------------------------

        if (
            direction
            ==
            "input"
            and
            port[
                "name"
            ]
            not in clocks
            and
            port[
                "name"
            ]
            not in reset_names
        ):

            input_ports.append(
                item
            )

        elif (
            direction
            ==
            "output"
        ):

            output_ports.append(
                item
            )

    return {

        "module_name":
            top_name,

        "ports":
            dut_ports,

        "input_ports":
            input_ports,

        "output_ports":
            output_ports,

        "clock_ports":
            clocks,

        "reset_ports":
            resets,
    }


# ============================================================
# VERIFICATION READINESS
#
# WHAT HAS AGENT 1 PROVEN?
#
# WHAT STILL NEEDS AGENT 2 / AGENT 3?
# ============================================================

def build_requirement_summary(
    spec,
    is_valid
):

    summary = build_spec_summary(
        spec
    )

    static_checked = [

        (
            "YAML design_name matches the "
            "detected RTL top module."
        ),

        (
            "YAML clock/reset are checked against "
            "the RTL interface and reset style/polarity."
        ),

        (
            "YAML ports are checked against RTL names, "
            "directions, and resolvable widths."
        ),
    ]

    if summary.get(
        "internal_signals"
    ):

        static_checked.append(

            "YAML-declared internal signal names "
            "are checked for RTL declarations."
        )

    if summary.get(
        "combinational_behavior"
    ):

        static_checked.append(

            "Outputs named in combinational_behavior "
            "are checked for combinational RTL assignment."
        )

    dynamic_needed = []

    # ========================================================
    # CORRECTNESS CRITERIA
    # ========================================================

    for item in (

        summary.get(
            "correctness_criteria",
            []
        )

        or []
    ):

        dynamic_needed.append(

            "Correctness criterion: {}".format(
                item
            )
        )

    # ========================================================
    # RULES
    # ========================================================

    for item in (

        summary.get(
            "rules",
            []
        )

        or []
    ):

        dynamic_needed.append(

            "Behavior rule: {}".format(
                item
            )
        )

    # ========================================================
    # INVARIANTS
    # ========================================================

    for item in (

        summary.get(
            "invariants",
            []
        )

        or []
    ):

        dynamic_needed.append(

            "Invariant: {}".format(
                item
            )
        )

    # ========================================================
    # IMPLEMENTATION CONSTRAINTS
    # ========================================================

    for item in (

        summary.get(
            "implementation_required",
            []
        )

        or []
    ):

        dynamic_needed.append(

            "Implementation constraint "
            "to confirm: {}".format(
                item
            )
        )

    # ========================================================
    # FORBIDDEN IMPLEMENTATION CONSTRAINTS
    # ========================================================

    for item in (

        summary.get(
            "implementation_forbidden",
            []
        )

        or []
    ):

        dynamic_needed.append(

            "Forbidden implementation pattern "
            "to confirm absent: {}".format(
                item
            )
        )

    # ========================================================
    # FORBIDDEN PATTERNS
    # ========================================================

    for item in (

        summary.get(
            "forbidden_patterns",
            []
        )

        or []
    ):

        dynamic_needed.append(

            "Forbidden pattern "
            "to confirm absent: {}".format(
                item
            )
        )

    # ========================================================
    # SEQUENTIAL BEHAVIOR
    # ========================================================

    if summary.get(
        "sequential_behavior"
    ):

        dynamic_needed.append(

            "Cycle-by-cycle sequential behavior "
            "from the YAML must be exercised "
            "in simulation."
        )

    # ========================================================
    # COMBINATIONAL BEHAVIOR
    # ========================================================

    if summary.get(
        "combinational_behavior"
    ):

        dynamic_needed.append(

            "Combinational output behavior must "
            "be verified dynamically, not only "
            "structurally."
        )

    return {

        "status":

            (
                "READY_FOR_AGENT_2_AND_AGENT_3"

                if is_valid

                else

                "NOT_READY_FOR_VERIFICATION"
            ),

        "message":

            (
                (
                    "Agent 1 confirmed the YAML is "
                    "internally valid and the RTL is "
                    "statically consistent with it. "
                    "Agent 2 and Agent 3 must still "
                    "prove runtime behavior."
                )

                if is_valid

                else

                (
                    "Agent 1 found blocking errors. "
                    "Agent 2 and Agent 3 should not "
                    "run until they are fixed."
                )
            ),

        "statically_checked":
            static_checked,

        "still_requires_dynamic_verification":
            dynamic_needed,
    }


# ============================================================
# VALIDATION STAGE OBJECT
# ============================================================

def stage(
    status,
    checks=None,
    errors=None,
    warnings=None,
    **extra
):

    data = {

        "status":
            status,

        "checks_performed":
            checks or [],

        "errors":
            errors or [],

        "warnings":
            warnings or [],
    }

    data.update(
        extra
    )

    return data


# ============================================================
# EARLY FAILURE RESULT
# ============================================================

def make_early_failure(
    rtl_files,
    top_name,
    top_file,
    yaml_file,
    code,
    failure_stage,
    summary,
    issues,
    yaml_stage=None
):

    return {

        "is_valid":
            False,

        "severity":
            "critical",

        "error_code":
            code,

        "failure_stage":
            failure_stage,

        "summary":
            summary,

        "issues":
            issues,

        "warnings":
            [],

        # ====================================================
        # Compatibility fields
        # ====================================================

        "rtl_files":
            rtl_files,

        "top_module_name":
            top_name,

        "top_module_file":
            top_file,

        "compile_tool":
            None,

        "compile_passed":
            None,

        "compile_output_snippet":
            "",

        "spec_file":
            yaml_file,

        "yaml_sane":
            False,

        "yaml_issues":

            (
                issues

                if failure_stage.startswith(
                    "YAML"
                )

                else []
            ),

        "spec_consistent":
            None,

        "spec_issues":
            [],

        "rtl_sane":
            None,

        "rtl_issues":
            [],

        "dut_spec":
            None,

        # ====================================================
        # New Agent 1 fields
        # ====================================================

        "design_intent":
            None,

        "what_agent1_tested":
            {},

        "verification_readiness": {

            "status":
                "NOT_READY_FOR_VERIFICATION",

            "message":

                (
                    "Validation stopped before downstream "
                    "verification readiness could be established."
                ),

            "statically_checked":
                [],

            "still_requires_dynamic_verification":
                [],
        },

        "validation_stages": {

            "yaml":

                (
                    yaml_stage

                    or

                    stage(
                        "FAIL",
                        errors=issues
                    )
                ),

            "rtl":
                stage(
                    "NOT_RUN"
                ),

            "compile":
                stage(
                    "NOT_RUN"
                ),

            "yaml_vs_rtl":
                stage(
                    "NOT_RUN"
                ),
        },
    }


# ============================================================
# MAIN AGENT 1 VALIDATION FLOW
# ============================================================

def validate_rtl(
    path_in,
    output_json="validation_result.json",
    top_module=None,
    yaml_path=None,
    rtl_files_override=None
):

    # ========================================================
    # FIND RTL
    # ========================================================

    rtl_files = rtl_files_override if rtl_files_override is not None else collect_rtl_files(path_in)

    if not rtl_files:

        result = make_early_failure(

            [],

            None,

            None,

            None,

            "RTL_NOT_FOUND",

            "INPUT_DISCOVERY",

            "No RTL files found.",

            [
                "No DUT .sv/.v files were found."
            ]
        )

        write_json(
            output_json,
            result
        )

        return result

    # ========================================================
    # READ RTL
    #
    # We detect the module here only so we can pair the RTL
    # with the correct YAML.
    #
    # No RTL PASS decision has happened yet.
    # ========================================================

    full_text = "\n\n".join(

        strip_comments_sv(

            read_text(
                path
            )
        )

        for path
        in rtl_files
    )

    top_name = top_module or detect_top_module(full_text)

    top_file = None

    if top_name:

        for path in rtl_files:

            if re.search(

                r"^\s*module\s+"
                +
                re.escape(
                    top_name
                )
                +
                r"\b",

                strip_comments_sv(

                    read_text(
                        path
                    )
                ),

                re.MULTILINE
            ):

                top_file = path

                break

    log(

        "Top module detected: {}".format(
            top_name
        )
    )

    log(

        "Top module file: {}".format(
            top_file
        )
    )

    # ========================================================
    # FIND MATCHING YAML
    # ========================================================

    yaml_file = yaml_path or find_matching_yaml(path_in, top_name)

    if yaml_file is None:

        issues = [

            (
                "No unique matching YAML specification "
                "was found for '{}'.".format(
                    top_name
                )
            ),

            (
                "Agent 1 will not validate RTL "
                "without a trusted YAML source of truth."
            ),
        ]

        result = make_early_failure(

            rtl_files,

            top_name,

            top_file,

            None,

            "YAML_SPEC_NOT_FOUND",

            "YAML_DISCOVERY",

            (
                "Validation stopped: matching "
                "YAML specification not found."
            ),

            issues
        )

        write_json(
            output_json,
            result
        )

        return result

    log(

        "Matching YAML found: {}".format(
            yaml_file
        )
    )

    # ========================================================
    # STAGE 1A
    #
    # YAML PARSING
    # ========================================================

    try:

        spec = load_yaml(
            yaml_file
        )

    except yaml.YAMLError as error:

        message = (

            "YAML syntax error: {}".format(
                error
            )
        )

        result = make_early_failure(

            rtl_files,

            top_name,

            top_file,

            yaml_file,

            "YAML_PARSE_ERROR",

            "YAML_PARSE",

            (
                "Validation stopped: "
                "YAML could not be parsed."
            ),

            [
                message
            ],

            stage(

                "FAIL",

                [
                    "YAML syntax/parsing"
                ],

                [
                    message
                ]
            )
        )

        write_json(
            output_json,
            result
        )

        return result

    except Exception as error:

        message = (

            "Could not read YAML: {}".format(
                error
            )
        )

        result = make_early_failure(

            rtl_files,

            top_name,

            top_file,

            yaml_file,

            "YAML_READ_ERROR",

            "YAML_PARSE",

            (
                "Validation stopped: "
                "YAML could not be read."
            ),

            [
                message
            ],

            stage(

                "FAIL",

                [
                    "YAML file read"
                ],

                [
                    message
                ]
            )
        )

        write_json(
            output_json,
            result
        )

        return result

    # ========================================================
    # STAGE 1B
    #
    # YAML SANITY VALIDATION
    # ========================================================

    (
        yaml_ok,
        yaml_errors,
        yaml_warnings,
        yaml_checks
    ) = validate_yaml(
        spec
    )

    yaml_stage = stage(

        (
            "PASS"

            if yaml_ok

            else

            "FAIL"
        ),

        yaml_checks,

        yaml_errors,

        yaml_warnings
    )

    log(

        "YAML validation: {}".format(
            yaml_stage[
                "status"
            ]
        )
    )

    # ========================================================
    # BAD YAML = HARD STOP
    # ========================================================

    if not yaml_ok:

        result = make_early_failure(

            rtl_files,

            top_name,

            top_file,

            yaml_file,

            "YAML_VALIDATION_FAILED",

            "YAML_VALIDATION",

            (
                "Validation stopped: "
                "YAML specification is invalid."
            ),

            yaml_errors,

            yaml_stage
        )

        result[
            "warnings"
        ] = yaml_warnings

        write_json(
            output_json,
            result
        )

        return result

    # ========================================================
    # YAML HAS PASSED
    #
    # NOW IT BECOMES OUR SOURCE OF TRUTH
    # ========================================================

    design_intent = build_design_intent(
        spec
    )

    # ========================================================
    # STAGE 2
    #
    # RTL STATIC VALIDATION
    # ========================================================

    if not top_name:

        result = make_early_failure(

            rtl_files,

            None,

            None,

            yaml_file,

            "TOP_MODULE_NOT_FOUND",

            "RTL_VALIDATION",

            (
                "YAML is valid, but the RTL "
                "top module could not be detected."
            ),

            [
                "Top module detection failed."
            ],

            yaml_stage
        )

        result[
            "yaml_sane"
        ] = True

        result[
            "design_intent"
        ] = design_intent

        write_json(
            output_json,
            result
        )

        return result

    top_block = find_module_block(

        full_text,

        top_name
    )

    (
        rtl_ok,
        rtl_errors,
        rtl_warnings,
        rtl_checks
    ) = check_rtl(

        top_name,

        top_block
    )

    rtl_stage = stage(

        (
            "PASS"

            if rtl_ok

            else

            "FAIL"
        ),

        rtl_checks,

        rtl_errors,

        rtl_warnings
    )

    log(

        "RTL validation: {}".format(
            rtl_stage[
                "status"
            ]
        )
    )

    # ========================================================
    # STAGE 3
    #
    # VCS / HDL COMPILATION
    # ========================================================

    (
        compile_tool,
        compile_ok,
        compile_output
    ) = compile_rtl(

        rtl_files,

        top_name
    )

    compile_errors = (

        []

        if compile_ok

        else

        [

            (
                "{} compile/elaboration failed.".format(
                    compile_tool
                )

                if compile_tool

                else

                "No HDL compiler was available."
            )
        ]
    )

    compile_stage = stage(

        (
            "PASS"

            if compile_ok

            else

            "FAIL"
        ),

        [
            "HDL compile/elaboration"
        ],

        compile_errors,

        [],

        tool=compile_tool
    )

    log(

        "Compile validation: {}".format(
            compile_stage[
                "status"
            ]
        )
    )

    # ========================================================
    # STAGE 4
    #
    # YAML <-> RTL
    # ========================================================

    (
        spec_ok,
        spec_errors,
        spec_warnings,
        spec_checks
    ) = compare_yaml_rtl(

        top_name,

        top_block,

        spec
    )

    spec_stage = stage(

        (
            "PASS"

            if spec_ok

            else

            "FAIL"
        ),

        spec_checks,

        spec_errors,

        spec_warnings
    )

    log(

        "YAML <-> RTL validation: {}".format(
            spec_stage[
                "status"
            ]
        )
    )

    # ========================================================
    # COLLECT ERRORS / WARNINGS
    # ========================================================

    issues = (

        list(
            rtl_errors
        )

        +

        list(
            compile_errors
        )

        +

        list(
            spec_errors
        )
    )

    warnings = (

        list(
            yaml_warnings
        )

        +

        list(
            rtl_warnings
        )

        +

        list(
            spec_warnings
        )
    )

    # ========================================================
    # FINAL PASS / FAIL
    # ========================================================

    is_valid = (

        yaml_ok

        and

        rtl_ok

        and

        compile_ok

        and

        spec_ok

        and

        not issues
    )

    # ========================================================
    # FAILURE LOCATION
    # ========================================================

    if not rtl_ok:

        failure_stage = (
            "RTL_VALIDATION"
        )

        error_code = (
            "RTL_VALIDATION_FAILED"
        )

    elif not compile_ok:

        failure_stage = (
            "RTL_COMPILE"
        )

        error_code = (
            "RTL_COMPILE_FAILED"
        )

    elif not spec_ok:

        failure_stage = (
            "YAML_VS_RTL"
        )

        error_code = (
            "YAML_RTL_MISMATCH"
        )

    else:

        failure_stage = None

        error_code = None

    # ========================================================
    # FINAL RESULT
    # ========================================================

    result = {

        "is_valid":
            is_valid,

        "severity":

            (
                "none"

                if is_valid

                else

                "high"
            ),

        "error_code":
            error_code,

        "failure_stage":
            failure_stage,

        "summary":

            (
                (
                    "YAML is valid, RTL passes "
                    "static/compile checks, and RTL "
                    "is structurally consistent with "
                    "the YAML. Ready for Agent 2/3 "
                    "runtime verification."
                )

                if is_valid

                else

                (
                    "YAML is valid, but Agent 1 found "
                    "blocking RTL-related errors in "
                    "stage '{}'.".format(
                        failure_stage
                    )
                )
            ),

        "issues":
            issues,

        "warnings":
            warnings,

        # ====================================================
        # COMPATIBILITY FIELDS
        #
        # Agent 2 / Agent 3 / pipeline can still consume these.
        # ====================================================

        "rtl_files":
            rtl_files,

        "top_module_name":
            top_name,

        "top_module_file":
            top_file,

        "compile_tool":
            compile_tool,

        "compile_passed":
            compile_ok,

        "compile_output_snippet":

            (
                compile_output
                or ""
            )[:4000],

        "spec_file":
            yaml_file,

        "yaml_sane":
            yaml_ok,

        "yaml_issues":
            yaml_errors,

        "spec_consistent":
            spec_ok,

        "spec_issues":
            spec_errors,

        "rtl_sane":
            rtl_ok,

        "rtl_issues":
            rtl_errors,

        # ====================================================
        # DUT SPEC FOR AGENT 2 / AGENT 3
        # ====================================================

        "dut_spec":

            (
                build_dut_spec(

                    top_name,

                    top_block,

                    spec
                )

                if is_valid

                else None
            ),

        # ====================================================
        # HUMAN / FRONTEND SUMMARY DATA
        # ====================================================

        "design_intent":
            design_intent,

        "what_agent1_tested": {

            "yaml":
                yaml_checks,

            "rtl":
                rtl_checks,

            "compile":
                [
                    "HDL compile/elaboration"
                ],

            "yaml_vs_rtl":
                spec_checks,
        },

        "verification_readiness":

            build_requirement_summary(

                spec,

                is_valid
            ),

        "validation_stages": {

            "yaml":
                yaml_stage,

            "rtl":
                rtl_stage,

            "compile":
                compile_stage,

            "yaml_vs_rtl":
                spec_stage,
        },
    }

    # ========================================================
    # SAVE JSON
    # ========================================================

    from requirement_verification import build_requirement_catalog, attach_static_results
    catalog = attach_static_results(build_requirement_catalog(spec), full_text)
    result["requirement_catalog"] = catalog
    # Structural coverage gaps do not prevent UVM/vector verification.
    # Actual violations still fail Agent 1 below.
    result["static_requirement_coverage"] = {
        "passed": sum(r["static_result"]["status"] == "PASS" for r in catalog),
        "failed": sum(r["static_result"]["status"] == "FAIL" for r in catalog),
        "unverified_static": sum(r["verification_method"] == "static" and
                                 r["static_result"]["status"] == "NOT_TESTED" for r in catalog),
        "policy": "Unverified source constraints are coverage gaps, not behavioral failures.",
    }
    static_failures = [r for r in catalog if r["static_result"]["status"] == "FAIL"]
    if static_failures:
        result["is_valid"] = False
        result["error_code"] = "STATIC_REQUIREMENT_FAILED"
        result["failure_stage"] = "YAML_VS_RTL"
        result["summary"] = "Static requirement checks found violations."
        result["issues"].extend(r["id"] + ": " + r["static_result"]["evidence"] for r in static_failures)
    write_json(output_json, result)
    return result


# ============================================================
# TERMINAL OUTPUT
# ============================================================

def print_stage(
    title,
    data
):

    print(
        "\n"
        +
        "-" * 58
    )

    print(
        title
    )

    print(
        "-" * 58
    )

    print(

        "  status: {}".format(
            data.get(
                "status"
            )
        )
    )

    # --------------------------------------------------------
    # Checks
    # --------------------------------------------------------

    if data.get(
        "checks_performed"
    ):

        print(
            "  checked:"
        )

        for item in data[
            "checks_performed"
        ]:

            print(

                "    - {}".format(
                    item
                )
            )

    # --------------------------------------------------------
    # Errors
    # --------------------------------------------------------

    if data.get(
        "errors"
    ):

        print(
            "  errors:"
        )

        for item in data[
            "errors"
        ]:

            print(

                "    - {}".format(
                    item
                )
            )

    # --------------------------------------------------------
    # Warnings
    # --------------------------------------------------------

    if data.get(
        "warnings"
    ):

        print(
            "  warnings:"
        )

        for item in data[
            "warnings"
        ]:

            print(

                "    - {}".format(
                    item
                )
            )


def print_design_intent(
    intent
):

    print(
        "\n"
        +
        "-" * 58
    )

    print(
        "DESIGN INTENT - WHAT THE USER REQUESTED"
    )

    print(
        "-" * 58
    )

    print(

        "  design      : {}".format(
            intent.get(
                "design_name"
            )
        )
    )

    print(

        "  design type : {}".format(
            intent.get(
                "design_type"
            )
        )
    )

    print(

        "  clock       : {}".format(

            intent.get(
                "clock",
                {}
            ).get(
                "name"
            )
        )
    )

    # ========================================================
    # RESET
    # ========================================================

    reset = intent.get(
        "reset",
        {}
    )

    reset_description = []

    if (
        reset.get(
            "synchronous"
        )
        is True
    ):

        reset_description.append(
            "synchronous"
        )

    elif (
        reset.get(
            "synchronous"
        )
        is False
    ):

        reset_description.append(
            "asynchronous"
        )

    if (
        reset.get(
            "active_low"
        )
        is True
    ):

        reset_description.append(
            "active-low"
        )

    elif (
        reset.get(
            "active_low"
        )
        is False
    ):

        reset_description.append(
            "active-high"
        )

    print(

        "  reset       : {} {}".format(

            reset.get(
                "name"
            ),

            " ".join(
                reset_description
            )
        )
    )

    # ========================================================
    # PORTS
    # ========================================================

    print(
        "  YAML ports:"
    )

    for port in intent.get(
        "yaml_ports",
        []
    ):

        print(

            "    - {} {} width={}".format(

                port.get(
                    "direction"
                ),

                port.get(
                    "name"
                ),

                port.get(
                    "width"
                )
            )
        )

    # ========================================================
    # REQUEST STRUCT
    # ========================================================

    if (
        intent.get(
            "request_struct_width"
        )
        is not None
    ):

        print(

            "  request struct packed width: {} bits".format(

                intent.get(
                    "request_struct_width"
                )
            )
        )

    # ========================================================
    # BEHAVIOR
    # ========================================================

    behavior = intent.get(
        "behavior",
        {}
    )

    if behavior:

        print(
            "  requested behavior:"
        )

        for key, value in behavior.items():

            print(

                "    - {}: {}".format(
                    key,
                    value
                )
            )

    # ========================================================
    # INVARIANTS
    # ========================================================

    if intent.get(
        "invariants"
    ):

        print(
            "  invariants:"
        )

        for item in intent[
            "invariants"
        ]:

            print(

                "    - {}".format(
                    item
                )
            )

    # ========================================================
    # IMPLEMENTATION CONSTRAINTS
    # ========================================================

    if intent.get(
        "implementation_required"
    ):

        print(
            "  implementation constraints:"
        )

        for item in intent[
            "implementation_required"
        ]:

            print(

                "    - {}".format(
                    item
                )
            )

    # ========================================================
    # FORBIDDEN PATTERNS
    # ========================================================

    if (
        intent.get(
            "implementation_forbidden"
        )
        or
        intent.get(
            "forbidden_patterns"
        )
    ):

        print(
            "  forbidden implementation patterns:"
        )

        for item in intent.get(
            "implementation_forbidden",
            []
        ):

            print(

                "    - {}".format(
                    item
                )
            )

        for item in intent.get(
            "forbidden_patterns",
            []
        ):

            print(

                "    - {}".format(
                    item
                )
            )


def print_readiness(data):

    print(
        "\n"
        +
        "-" * 58
    )

    print(
        "VERIFICATION READINESS - ARE WE STILL ON TRACK?"
    )

    print(
        "-" * 58
    )

    print(

        "  status : {}".format(
            data.get(
                "status"
            )
        )
    )

    print(

        "  summary: {}".format(
            data.get(
                "message"
            )
        )
    )

    # --------------------------------------------------------
    # Static checks
    # --------------------------------------------------------

    if data.get(
        "statically_checked"
    ):

        print(
            "  Agent 1 confirmed:"
        )

        for item in data[
            "statically_checked"
        ]:

            print(

                "    - {}".format(
                    item
                )
            )

    # --------------------------------------------------------
    # Future dynamic checks
    # --------------------------------------------------------

    if data.get(
        "still_requires_dynamic_verification"
    ):

        print(
            "  still needs Agent 2/3 testing:"
        )

        for item in data[
            "still_requires_dynamic_verification"
        ]:

            print(

                "    - {}".format(
                    item
                )
            )


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser(

        description=(
            "Agent 1 YAML + RTL Specification Gate"
        )
    )

    parser.add_argument(

        "rtl_input",

        help=(
            "RTL file or frontend/run directory"
        )
    )

    parser.add_argument(

        "--out",

        default="validation_result.json",

        help=(
            "Output JSON path"
        )
    )

    parser.add_argument('--top', help='Explicit DUT module selected for this YAML')
    parser.add_argument('--yaml', help='Exact YAML specification for this DUT')
    parser.add_argument('--rtl-file', action='append', help='RTL compilation unit; repeat for dependencies')
    args = parser.parse_args()

    # ========================================================
    # HEADER
    # ========================================================

    print(
        "\n"
        +
        "=" * 58
    )

    print(
        "  AGENT 1 - YAML + RTL SPECIFICATION GATE"
    )

    print(
        "=" * 58
    )

    # ========================================================
    # RUN AGENT
    # ========================================================

    result = validate_rtl(

        args.rtl_input,

        args.out,
        top_module=args.top,
        yaml_path=args.yaml,
        rtl_files_override=args.rtl_file
    )

    stages = result.get(
        "validation_stages",
        {}
    )

    # ========================================================
    # 1. YAML VALIDATION
    # ========================================================

    if stages.get(
        "yaml"
    ):

        print_stage(

            "1. YAML VALIDATION",

            stages[
                "yaml"
            ]
        )

    # ========================================================
    # DESIGN INTENT
    # ========================================================

    if result.get(
        "design_intent"
    ):

        print_design_intent(

            result[
                "design_intent"
            ]
        )

    # ========================================================
    # 2. RTL VALIDATION
    # ========================================================

    if stages.get(
        "rtl"
    ):

        print_stage(

            "2. RTL VALIDATION",

            stages[
                "rtl"
            ]
        )

    # ========================================================
    # 3. RTL COMPILE
    # ========================================================

    if stages.get(
        "compile"
    ):

        print_stage(

            "3. RTL COMPILE",

            stages[
                "compile"
            ]
        )

    # ========================================================
    # 4. YAML <-> RTL
    # ========================================================

    if stages.get(
        "yaml_vs_rtl"
    ):

        print_stage(

            "4. YAML <-> RTL VALIDATION",

            stages[
                "yaml_vs_rtl"
            ]
        )

    # ========================================================
    # READINESS
    # ========================================================

    if result.get(
        "verification_readiness"
    ):

        print_readiness(

            result[
                "verification_readiness"
            ]
        )

    # ========================================================
    # FINAL RESULT
    # ========================================================

    print(
        "\n"
        +
        "=" * 58
    )

    print(
        "FINAL AGENT 1 RESULT"
    )

    print(
        "=" * 58
    )

    print(

        "  is_valid      : {}".format(
            result.get(
                "is_valid"
            )
        )
    )

    print(

        "  error_code    : {}".format(
            result.get(
                "error_code"
            )
        )
    )

    print(

        "  failure_stage : {}".format(
            result.get(
                "failure_stage"
            )
        )
    )

    print(

        "  summary       : {}".format(
            result.get(
                "summary"
            )
        )
    )

    print(

        "  output_json   : {}".format(
            args.out
        )
    )

    # --------------------------------------------------------
    # Blocking issues
    # --------------------------------------------------------

    if result.get(
        "issues"
    ):

        print(
            "  blocking issues:"
        )

        for issue in result[
            "issues"
        ]:

            print(

                "    - {}".format(
                    issue
                )
            )

    print(
        "=" * 58
    )

    # ========================================================
    # EXIT CODE
    # ========================================================

    if result.get(
        "is_valid"
    ):

        log(

            "PASS -> {}".format(
                args.out
            )
        )

        sys.exit(
            0
        )

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

                result[
                    "error_code"
                ]
            )
        )

    sys.exit(
        1
    )
