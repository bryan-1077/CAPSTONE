#!/usr/bin/env python3
"""
agent2_uvm_tb.py
================
Agent 2: RTL UVM verification with YAML requirement traceability.

The RTL is the DUT being verified.
The YAML is the approved source of expected behavior from Agent 1.

Flow:
1. Read validation_result.json from Agent 1.
2. Load the exact YAML file Agent 1 approved.
3. Extract traceable requirements (REQ-001, REQ-002, ...).
4. Ask the LLM to generate a UVM testbench for the RTL DUT.
5. Compile RTL + UVM with Synopsys VCS.
6. Simulate the RTL DUT.
7. Parse scoreboard results and per-requirement PASS/FAIL/NOT_TESTED markers.
8. Combine structural and simulation evidence in requirement_traceability.json.
9. Preserve each compiled run's UVM sources and evidence under build_auto/uvm_runs.

Use --reuse-tb to compile and run the existing reviewed tb_auto sources using
Agent 1's approved inputs, without calling the LLM or modifying the testbench.
Generated candidates are audited before simulation; simulation failures stop
the run and never enter the automatic generation/compile-repair loop.
Clean simulations with unexecuted baseline checks may append directed sequence
traffic, retaining the existing checks and each run's sources/evidence.
Incomplete output is completed one file at a time before replacing tb_auto.
AGENT2_MAX_TOKENS overrides the default 16384-token output budget.

Python compatibility:
- Python 3.6+
- Older PyYAML versions used on TAMU servers
"""

import os
import re
import sys
import json
import shutil
import subprocess
import textwrap
import tempfile
import hashlib
import time
from urllib.parse import urlsplit
from requirement_verification import (build_requirement_catalog,
    attach_static_results as _attach_static_results, combine_requirement_results)
from run_pipeline import run_reported_agent

import requests
import yaml
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

TB_ROOT = "tb_auto"
BUILD_DIR = "build_auto"
LOG_DIR = os.path.join(BUILD_DIR, "logs")
SIMV_NAME = "simv_auto"
MAX_ITERS = 5
DEFAULT_LLM_MAX_TOKENS = 16384
MAX_FILE_COMPLETION_ATTEMPTS = 2
MAX_COVERAGE_EXTENSIONS = 2
VALIDATION_RESULT_FILE = os.environ.get('RTL_VALIDATION_RESULT_FILE', 'validation_result.json')
ACTIVE_REPORT = None

REQUIREMENT_REPORT_FILE = os.path.join(
    BUILD_DIR,
    "requirement_traceability.json"
)

VCS_UVM_FLAGS = [
    "-ntb_opts",
    "uvm"
]

ENABLE_DEBUG = True

ALLOWED_TB_FILES = {
    "dut_pkg.sv",
    "dut_if.sv",
    "dut_tb_top.sv",
}

COMPILE_ORDER = [
    "dut_pkg.sv",
    "dut_if.sv",
    "dut_tb_top.sv",
]

COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "cyan": "\033[96m",
    "magenta": "\033[95m",
    "grey": "\033[90m",
}


# ============================================================
# TERMINAL HELPERS
# ============================================================

def c(color, text):
    return "{}{}{}".format(
        COLORS.get(color, ""),
        text,
        COLORS["reset"]
    )


def banner(title, color="magenta"):
    line = "═" * 58

    print(
        "\n{}".format(
            c(color, line)
        )
    )

    print(
        c(
            "bold",
            "  {}".format(title)
        )
    )

    print(
        "{}\n".format(
            c(color, line)
        )
    )


def section(title):
    print(
        "\n{}".format(
            c(
                "grey",
                "─" * 50
            )
        )
    )

    print(
        c(
            "bold",
            title
        )
    )

    print(
        c(
            "grey",
            "─" * 50
        )
    )


def log(agent, msg, level="info"):
    color_map = {
        "info": "cyan",
        "ok": "green",
        "warn": "yellow",
        "error": "red",
        "muted": "grey",
    }

    prefix = c(
        color_map.get(
            level,
            "cyan"
        ),
        "[{}]".format(agent)
    )

    print(
        "{} {}".format(
            prefix,
            msg
        )
    )


# ============================================================
# DIRECTORY / FILE HELPERS
# ============================================================

def ensure_build_dirs():
    os.makedirs(
        BUILD_DIR,
        exist_ok=True
    )

    os.makedirs(
        LOG_DIR,
        exist_ok=True
    )

    return LOG_DIR


def read_file(path):
    with open(
        path,
        "r",
        errors="ignore"
    ) as f:

        return f.read()


def load_validation_result():
    if not os.path.exists(
        VALIDATION_RESULT_FILE
    ):

        return None

    with open(
        VALIDATION_RESULT_FILE,
        "r"
    ) as f:

        return json.load(f)


# ============================================================
# PYAML COMPATIBILITY
# ============================================================

def safe_yaml_dump(data):
    """
    Work with both newer and older PyYAML versions.
    """

    try:

        return yaml.safe_dump(
            data,
            sort_keys=False,
            default_flow_style=False,
        )

    except TypeError:

        return yaml.safe_dump(
            data,
            default_flow_style=False,
        )


def gather_sv_files(root):
    result = []

    if not os.path.exists(
        root
    ):

        return result

    for (
        current_root,
        _,
        files
    ) in os.walk(
        root
    ):

        for filename in sorted(
            files
        ):

            if (
                filename.endswith(".sv")
                or
                filename.endswith(".v")
            ):

                result.append(
                    os.path.join(
                        current_root,
                        filename
                    )
                )

    return result


def gather_incdirs(root):
    result = set()

    if not os.path.exists(
        root
    ):

        return []

    for (
        current_root,
        _,
        _
    ) in os.walk(
        root
    ):

        result.add(
            current_root
        )

    return sorted(
        result
    )


def which(tool):
    return shutil.which(
        tool
    )


def ordered_tb_files(root):
    all_files = gather_sv_files(
        root
    )

    by_name = {
        os.path.basename(path): path
        for path in all_files
    }

    result = []

    for name in COMPILE_ORDER:

        if name in by_name:

            result.append(
                by_name[name]
            )

    return result


def read_tb_files_for_context(
    max_chars=None
):
    parts = []

    total = 0

    for path in ordered_tb_files(
        TB_ROOT
    ):

        code = read_file(
            path
        )

        block = (
            "\n// ===== {} =====\n{}"
        ).format(
            path,
            code
        )

        parts.append(
            block
        )

        total += len(
            block
        )

        if max_chars is not None and total >= max_chars:

            parts.append(
                "\n// ... truncated ..."
            )

            break

    return "\n".join(
        parts
    )


# ============================================================
# LLM
# ============================================================

class LLMText(str):
    """Text-compatible result retaining output-limit diagnostics for logging."""
    def __new__(cls, content, finish_reason=None, usage=None, max_tokens=None):
        result = str.__new__(cls, content)
        result.metadata = {'finish_reason': finish_reason, 'usage': usage,
                           'requested_max_tokens': max_tokens}
        return result


def llm_output_limit():
    value = os.getenv('AGENT2_MAX_TOKENS', str(DEFAULT_LLM_MAX_TOKENS))
    try:
        limit = int(value)
    except ValueError:
        raise RuntimeError('AGENT2_MAX_TOKENS must be a positive integer.')
    if limit <= 0:
        raise RuntimeError('AGENT2_MAX_TOKENS must be a positive integer.')
    return limit


def post_llm_request(base_url, headers, payload, agent):
    """Retry the same request on transient gateway failures, never change models."""
    endpoint = urlsplit(base_url)
    display_url = endpoint._replace(netloc=endpoint.netloc.rsplit('@', 1)[-1], query='', fragment='').geturl()
    configuration = {'endpoint': display_url, 'model': payload['model'],
                     'max_tokens': payload['max_tokens']}
    log(agent, 'LLM model={!r}, endpoint={}, max_tokens={}'.format(
        payload['model'], display_url, payload['max_tokens']), 'info')
    if ACTIVE_REPORT is not None:
        ACTIVE_REPORT.update(llm_configuration=configuration, failure_detail=None)
    for attempt in range(1, 4):
        response = None
        try:
            response = requests.post(base_url, headers=headers, data=json.dumps(payload), timeout=300)
            if response.status_code == 200:
                if ACTIVE_REPORT is not None:
                    ACTIVE_REPORT.update(llm_http_status=200, llm_request_attempts=attempt, failure_detail=None)
                return response
            try:
                body = response.json()
            except ValueError:
                body = {}
            # TAMU has returned this exact error intermittently for a working alias.
            model_unavailable = (response.status_code == 403 and isinstance(body, dict) and
                                 body.get('detail') == 'Model not found')
            retryable = model_unavailable or response.status_code in (408, 429, 500, 502, 503, 504)
            error = 'LLM HTTP {} for model {!r}: {}'.format(
                response.status_code, payload['model'], (response.text or '')[:700])
        except (requests.Timeout, requests.ConnectionError) as exc:
            retryable = True
            error = 'LLM connection failure for model {!r}: {}'.format(payload['model'], type(exc).__name__)
        if ACTIVE_REPORT is not None:
            ACTIVE_REPORT.update(llm_http_status=response.status_code if response is not None else None,
                                 llm_request_attempts=attempt, failure_detail=error)
        if not retryable or attempt == 3:
            raise RuntimeError(error)
        delay = 2 ** attempt
        log(agent, '{}; retrying the same request in {}s ({}/3).'.format(error, delay, attempt + 1), 'warn')
        time.sleep(delay)


def call_llm(
    system_prompt,
    user_prompt,
    agent="LLM"
):
    load_dotenv()

    api_key = os.getenv(
        "TAMU_API_KEY"
    )

    base_url = os.getenv(
        "TAMU_BASE_URL",
        ""
    ).strip()

    model = os.getenv(
        "TAMU_MODEL"
    )

    if not all([
        api_key,
        base_url,
        model
    ]):

        raise RuntimeError(
            "Missing TAMU_API_KEY / "
            "TAMU_BASE_URL / TAMU_MODEL in .env"
        )

    headers = {
        "Authorization":
            "Bearer {}".format(
                api_key
            ),

        "Content-Type":
            "application/json",
    }

    max_tokens = llm_output_limit()
    payload = {
        "model":
            model,

        "messages": [
            {
                "role":
                    "system",

                "content":
                    system_prompt
            },

            {
                "role":
                    "user",

                "content":
                    user_prompt
            },
        ],

        # IMPORTANT:
        #
        # Claude Sonnet 4.5 through the TAMU/Bedrock
        # endpoint requires temperature=1 when
        # thinking is enabled.
        #
        # Your original Agent 2 also used 1.
        "temperature":
            1,

        "max_tokens":
            max_tokens,
    }

    response = post_llm_request(base_url, headers, payload, agent)

    raw_http_text = (
        response.text
        or ""
    )

    # ========================================================
    # NORMAL HTTP ERROR
    # ========================================================

    if response.status_code != 200:

        raise RuntimeError(
            "LLM HTTP {}: {}".format(
                response.status_code,
                raw_http_text[:700]
            )
        )

    # ========================================================
    # TAMU / LiteLLM gateways can sometimes return an error
    # message inside a successful HTTP wrapper.
    #
    # Catch that instead of pretending it is an LLM answer.
    # ========================================================

    lower_raw = raw_http_text.lower()

    if (
        "litellm.badrequesterror"
        in lower_raw

        or

        "bedrockexception"
        in lower_raw

        or

        "unexpected error"
        in lower_raw
    ):

        raise RuntimeError(
            "LLM gateway returned an error: {}".format(
                raw_http_text[:900]
            )
        )

    # ========================================================
    # STREAMING / SSE RESPONSE
    # ========================================================

    chunks = []
    finish_reason = None
    usage = None

    for line in raw_http_text.splitlines():

        line = line.strip()

        if not line.startswith(
            "data:"
        ):

            continue

        chunk = line[
            5:
        ].strip()

        if chunk in (
            "",
            "[DONE]"
        ):

            continue

        try:

            data = json.loads(
                chunk
            )

            if data.get('usage') is not None:
                usage = data['usage']

            choices = data.get(
                "choices",
                []
            )

            if not choices:

                continue

            if choices[0].get('finish_reason') is not None:
                finish_reason = choices[0]['finish_reason']

            delta = choices[
                0
            ].get(
                "delta",
                {}
            )

            content = delta.get(
                "content"
            )

            if content:

                chunks.append(
                    content
                )

        except Exception:

            continue

    if chunks:

        return LLMText(''.join(chunks).strip(), finish_reason, usage, max_tokens)

    # ========================================================
    # NORMAL JSON RESPONSE FALLBACK
    # ========================================================

    try:

        data = response.json()

        choices = data.get(
            "choices",
            []
        )

        if choices:

            message = choices[
                0
            ].get(
                "message",
                {}
            )

            content = message.get(
                "content"
            )

            if content:

                return LLMText(content.strip(), choices[0].get('finish_reason'),
                               data.get('usage'), max_tokens)

            delta = choices[
                0
            ].get(
                "delta",
                {}
            )

            content = delta.get(
                "content"
            )

            if content:

                return LLMText(content.strip(), choices[0].get('finish_reason'),
                               data.get('usage'), max_tokens)

    except Exception:

        pass

    # ========================================================
    # FINAL FALLBACK
    #
    # Caller saves this for debugging.
    # ========================================================

    return LLMText(raw_http_text.strip(), finish_reason, usage, max_tokens)


def save_raw_llm_response(
    text,
    iter_idx,
    suffix=""
):
    ensure_build_dirs()

    path = os.path.join(
        BUILD_DIR,
        "llm_raw_iter_{}{}.txt".format(
            iter_idx, suffix
        )
    )

    with open(
        path,
        "w"
    ) as f:

        f.write(
            text
            or ""
        )

    with open(path + '.json', 'w') as meta_file:
        json.dump(getattr(text, 'metadata', {}), meta_file, indent=2)

    return path


# ============================================================
# GENERATED FILE BLOCK PARSING
# ============================================================

FILE_BLOCK_RE = re.compile(
    r"===\s*filename:\s*(.*?)\s*===\s*\n"
    r"(.*?)(?=\n===\s*filename:|\Z)",
    re.DOTALL,
)


def strip_markdown_fence(code):
    code = (
        code
        or ""
    ).strip()

    code = re.sub(
        r"^\s*```(?:systemverilog|sv|verilog|json)?\s*\n",
        "",
        code,
        flags=re.IGNORECASE,
    )

    code = re.sub(
        r"\n\s*```\s*$",
        "",
        code,
    )

    return code.strip()


def strip_timescale_directives(code):
    return re.sub(
        r"^\s*`timescale[^\n]*\n",
        "",
        code,
        flags=re.MULTILINE,
    )


def parse_file_blocks(text):
    # Gateways may prepend a complete thinking block immediately before a header.
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).replace('\r\n', '\n')
    # Collect all header styles together. An early return after the first style
    # used to hide interface/top files if the model mixed formats.
    headers = []
    for match in re.finditer(
            r"^[ \t]*===[ \t]*(?:filename:[ \t]*)?([^\r\n=]+?)[ \t]*===[ \t]*$",
            text, re.M):
        headers.append((match.start(), match.end(), match.group(1).strip()))
    for match in re.finditer(
            r"^[ \t]*(?:#+[ \t]*)?((?:tb_auto/)?dut_(?:pkg|if|tb_top)\.sv)[ \t]*:?[ \t]*\n"
            r"[ \t]*```(?:systemverilog|sv|verilog)?[ \t]*\n", text, re.M):
        headers.append((match.start(), match.end(), match.group(1)))
    headers.sort()
    files = []
    for index, (_, end, filename) in enumerate(headers):
        stop = headers[index + 1][0] if index + 1 < len(headers) else len(text)
        files.append((filename, strip_markdown_fence(text[end:stop]) + "\n"))
    return files


def generated_file_complete(filename, code):
    """Detect an obvious cutoff, not SystemVerilog correctness (VCS does that)."""
    declarations = {
        'dut_pkg.sv': ('package', 'dut_pkg', 'endpackage'),
        'dut_if.sv': ('interface', 'dut_if', 'endinterface'),
        'dut_tb_top.sv': ('module', 'dut_tb_top', 'endmodule'),
    }
    kind = declarations.get(os.path.basename(filename))
    if kind is None:
        return False
    start, name, end = kind
    source = _sv_without_comments(code)
    # Ignore keywords inside strings, e.g. an evidence note saying endpackage.
    source = re.sub(r'"(?:\\.|[^"\\])*"', '', source)
    if '"' in source or '/*' in source:
        return False  # Unterminated string/comment, even if its text says endpackage.
    return bool(re.search(r'\b' + start + r'\s+(?:(?:automatic|static)\s+)?' +
                          name + r'\b', source) and
                re.search(r'\b' + end + r'\b(?:\s*:\s*' + name + r')?\s*;?\s*$', source))


def candidate_file_status(files):
    by_name = {}
    for path, code in files:
        name = os.path.basename(path.strip())
        if name in ALLOWED_TB_FILES:
            by_name[name] = code
    missing = sorted(ALLOWED_TB_FILES - set(by_name))
    incomplete = sorted(name for name, code in by_name.items()
                        if not generated_file_complete(name, code))
    return by_name, missing, incomplete


def complete_generated_candidate(raw, base_prompt, iter_idx, previous_files=None):
    """Recover one missing/truncated file at a time without touching tb_auto.

    Complete siblings are context only; replies cannot overwrite them. Recovery
    is bounded and happens strictly before VCS compilation/simulation.
    """
    # A repair may return only changed files. Never borrow files from an older
    # design on disk: only this run's in-memory candidate is eligible for reuse.
    clean = strip_markdown_fence(re.sub(r'<think>.*?</think>', '', str(raw), flags=re.S)).strip()
    if previous_files and clean.startswith('{'):
        return apply_audit_edits(previous_files, clean)
    updates = parse_file_blocks(raw)
    if previous_files and not any(os.path.basename(path) in ALLOWED_TB_FILES
                                  for path, _ in updates):
        raise ValueError('Repair response contained no recognized JSON edits or file blocks. '
                         'Return the requested format; the existing candidate has been retained.')
    candidate, missing, incomplete = candidate_file_status(
        list(previous_files or []) + updates)
    if not missing and not incomplete:
        return [(name, candidate[name]) for name in COMPILE_ORDER]
    reason = getattr(raw, 'metadata', {}).get('finish_reason')
    log('UVM_TB_AGENT', 'Incomplete LLM output: missing={}; truncated/unfinished={}; '
        'finish_reason={}. Completing individual files.'.format(
            ', '.join(missing) or 'none', ', '.join(incomplete) or 'none',
            reason or 'not provided'), 'warn')
    # A small interface establishes the sampling/type contract for a large pkg.
    for name in ('dut_if.sv', 'dut_pkg.sv', 'dut_tb_top.sv'):
        if name in candidate and generated_file_complete(name, candidate[name]):
            continue
        for attempt in range(MAX_FILE_COMPLETION_ATTEMPTS):
            context = '\n'.join('=== filename: tb_auto/{} ===\n{}'.format(n, candidate[n])
                                for n in COMPILE_ORDER if n in candidate)
            prompt = (base_prompt + '\n\nSINGLE-FILE COMPLETION FOR THIS RESPONSE\n'
                      'The preceding candidate was incomplete. Return ONLY the full file '
                      'tb_auto/' + name + ', from its opening declaration to its closing '
                      'end declaration. Replace any partial version; do not append a fragment. '
                      'The other complete files are fixed context: keep their ports, types, '
                      'clocking aliases and class names compatible. Do not emit other files. '
                      'Do not remove scenarios, requirement comparisons or evidence to shorten '
                      'the response. Use shared helpers and loops for reporting, not a repeated '
                      'if/else/report block for every REQ ID.\n'
                      'CURRENT CANDIDATE (may include an unfinished file):\n' + context)
            system = ('You generate complete SystemVerilog UVM source. For this request '
                      'output exactly ONE file, using === filename: tb_auto/' + name +
                      ' === followed by the full source. No prose or markdown fences. '
                      'Use compact shared reporting helpers while preserving actual checks.')
            log('UVM_TB_AGENT', 'Completing {} (attempt {}/{})...'.format(
                name, attempt + 1, MAX_FILE_COMPLETION_ATTEMPTS), 'info')
            reply = call_llm(system, prompt, 'UVM_TB_AGENT')
            path = save_raw_llm_response(reply, iter_idx, '_{}_{}'.format(name, attempt))
            log('UVM_TB_AGENT', 'File-completion response saved: ' + path, 'muted')
            recovered, _, _ = candidate_file_status(parse_file_blocks(reply))
            if name in recovered:
                candidate[name] = recovered[name]
                if generated_file_complete(name, candidate[name]):
                    break
            log('UVM_TB_AGENT', '{} is still incomplete (finish_reason={}).'.format(
                name, getattr(reply, 'metadata', {}).get('finish_reason')), 'warn')
        else:
            raise RuntimeError('LLM generation could not complete {} after {} focused attempts. '
                               'This incomplete candidate was not installed or simulated. Inspect the saved '
                               'file-completion responses; AGENT2_MAX_TOKENS controls the '
                               'requested output budget.'.format(name, MAX_FILE_COMPLETION_ATTEMPTS))
    return [(name, candidate[name]) for name in COMPILE_ORDER]


def apply_audit_edits(files, raw):
    """Apply exact, bounded edits in memory; the full audit still runs afterward."""
    try:
        edits = json.loads(raw)['edits']
    except (ValueError, KeyError, TypeError):
        raise ValueError('Audit repair must contain a JSON edits list or complete file blocks.')
    if not isinstance(edits, list) or not edits or len(edits) > 30:
        raise ValueError('Audit repair needs 1 to 30 exact edits.')
    candidate = {os.path.basename(path): code for path, code in files}
    unsupported_before = set(re.findall(r'//\s*AGENT2_UNSUPPORTED\|[^\n]*', '\n'.join(candidate.values())))
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError('Each audit edit must identify file, old and new source.')
        name = edit.get('file')
        old, new = edit.get('old'), edit.get('new')
        if name not in ALLOWED_TB_FILES or name not in candidate:
            raise ValueError('Audit edit references an unknown testbench file: {!r}'.format(name))
        if not isinstance(old, str) or not old.strip() or not isinstance(new, str) or not new.strip():
            raise ValueError('Audit edits must replace nonempty exact source snippets.')
        if candidate[name].count(old) != 1:
            raise ValueError('Audit edit old text must match exactly once in {} (found {}). '
                             'Candidate unchanged; fix the original defects still listed. '
                             'Use a larger unique exact snippet or return the complete corrected file. '
                             'Rejected snippet: {!r}'.format(name, candidate[name].count(old), old[:240]))
        candidate[name] = candidate[name].replace(old, new, 1)
    unsupported_after = set(re.findall(r'//\s*AGENT2_UNSUPPORTED\|[^\n]*', '\n'.join(candidate.values())))
    if unsupported_after - unsupported_before:
        raise ValueError('Audit repairs cannot replace rejected checks with new unsupported markers.')
    if any(not generated_file_complete(name, code) for name, code in candidate.items()):
        raise ValueError('Audit edits left an incomplete testbench file.')
    return [(name, candidate[name]) for name in COMPILE_ORDER]


def normalize_generated_filename(
    filename
):
    basename = os.path.basename(
        filename.strip()
    )

    if basename not in ALLOWED_TB_FILES:

        return None

    return os.path.join(
        TB_ROOT,
        basename
    )


def write_files(files):
    written = []

    for (
        filename,
        code
    ) in files:

        output_path = (
            normalize_generated_filename(
                filename
            )
        )

        if output_path is None:

            log(
                "FILE_WRITER",
                "Ignoring unexpected generated file: {}".format(
                    filename
                ),
                "warn",
            )

            continue

        code = strip_timescale_directives(
            code
        )

        os.makedirs(
            os.path.dirname(
                output_path
            ),
            exist_ok=True
        )

        with open(
            output_path,
            "w"
        ) as f:

            f.write(
                code
            )

        written.append(
            output_path
        )

        log(
            "FILE_WRITER",
            "Wrote: {}".format(
                output_path
            ),
            "muted",
        )

    return written


def publish_generated_files(files):
    """Stage a complete candidate before replacing any installed TB source."""
    candidate, missing, incomplete = candidate_file_status(files)
    if missing or incomplete:
        raise RuntimeError('Refusing to install incomplete testbench: missing={}, unfinished={}'.format(
            missing, incomplete))
    ensure_build_dirs()
    staging = tempfile.mkdtemp(prefix='tb_candidate_', dir=BUILD_DIR)
    try:
        for name in COMPILE_ORDER:
            with open(os.path.join(staging, name), 'w') as handle:
                handle.write(strip_timescale_directives(candidate[name]))
        os.makedirs(TB_ROOT, exist_ok=True)
        written = []
        for name in COMPILE_ORDER:
            destination = os.path.join(TB_ROOT, name)
            os.replace(os.path.join(staging, name), destination)
            written.append(destination)
            log('FILE_WRITER', 'Installed complete source: ' + destination, 'muted')
        return written
    finally:
        shutil.rmtree(staging)


def _sv_without_comments(source):
    """Preserve strings and line numbers while excluding comments from checks."""
    pattern = r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/'
    return re.sub(pattern, lambda m: m.group(0) if m.group(0).startswith('"')
                  else re.sub(r'[^\n]', ' ', m.group(0)), source, flags=re.S)


def _sv_call_arguments(source, opening):
    """Read a call without confusing nested expressions/strings with commas."""
    stack = []
    arguments = []
    start = opening + 1
    quoted = False
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in '([{':
            stack.append(char)
        elif char == ')' and not stack:
            arguments.append(source[start:index].strip())
            return arguments
        elif char in ')]}':
            if stack:
                stack.pop()
        elif char == ',' and not stack:
            arguments.append(source[start:index].strip())
            start = index + 1
    return []


def _requirement_check_signatures(source):
    """Locate a helper's condition formal instead of assuming its position.

    Offsets are retained so method declarations are not mistaken for calls.
    Ambiguous/unsupported signatures are left to VCS rather than guessed by lint.
    """
    lexical = re.sub(r'"(?:\\.|[^"\\])*"',
                     lambda m: re.sub(r'[^\n]', ' ', m.group(0)), source)
    scopes = [(m.start(), m.end()) for m in re.finditer(
        r'\bclass\s+\w+[^;]*;.*?\bendclass\b', lexical, re.S)]

    def scope_at(offset):
        return next((span for span in scopes if span[0] <= offset < span[1]), None)

    signatures = []
    pattern = r'\b(?:function|task)\b[^;()]*?\b(?P<name>check_requirement)\s*\('
    for match in re.finditer(pattern, lexical):
        formals = _sv_call_arguments(source, match.end() - 1)
        parameters = []
        scalar_conditions = []
        named_conditions = []
        for index, formal in enumerate(formals):
            declaration, separator, default = formal.partition('=')
            identifiers = re.findall(r'\b[A-Za-z_]\w*\b', re.sub(r'\[[^\]]*\]', '', declaration))
            name = identifiers[-1] if identifiers else ''
            parameters.append((name, default.strip() if separator else None))
            if name.lower() in ('condition', 'cond', 'passed', 'check_pass',
                                'check_result', 'pass_condition', 'ok', 'matched', 'success'):
                named_conditions.append(index)
            if re.search(r'\b(?:bit|logic)\b', declaration) and '[' not in declaration:
                scalar_conditions.append(index)
        candidates = named_conditions if named_conditions else scalar_conditions
        condition_index = candidates[0] if len(candidates) == 1 else None
        signatures.append({'offset': match.start('name'), 'scope': scope_at(match.start()),
                           'parameters': parameters, 'condition_index': condition_index})
    return signatures, scope_at


def _requirement_check_condition(args, signatures, call_scope):
    """Return (condition expression, parameter label), or None if unresolved."""
    local = [signature for signature in signatures if signature['scope'] == call_scope]
    if not local:
        local = [signature for signature in signatures if signature['scope'] is None]
    resolved = []
    for signature in local:
        parameters = signature['parameters']
        position = signature['condition_index']
        if position is None or len(args) > len(parameters):
            continue
        actuals = {}
        for index, argument in enumerate(args):
            named = re.fullmatch(r'\.(\w+)\s*\((.*)\)', argument, re.S)
            if named:
                actuals[named.group(1)] = named.group(2).strip()
            else:
                actuals[parameters[index][0]] = argument
        if any(name not in actuals and default is None for name, default in parameters):
            continue
        name, default = parameters[position]
        resolved.append((actuals.get(name, default),
                         'argument {} ({})'.format(position + 1, name)))
    if resolved and len(set(resolved)) == 1:
        return resolved[0]
    return None


_SV_AUDIT_TOKEN = re.compile(
    r'"(?:\\.|[^"\\])*"|(?:\d[\d_]*)?\s*\'[sS]?[bBoOdDhH][\da-fA-F_xXzZ?]+'
    r"|'[01xXzZ]|\d[\d_]*(?:\.\d+)?|[`$]?[A-Za-z_]\w*"
    r'|===|!==|==|!=|<=|>=|\+\+|--|\+=|-=|\*=|/=|&&|\|\||::|[^\s]')


def _sv_check_contexts(source, input_names):
    """Conservative, local provenance for generated checker expressions.

    Track assignments and control dependence within a routine, including branch
    merges and observed transition counters. Unsupported control flow invalidates
    knowledge; it must not turn an unknown expression into a constant diagnosis.
    This lint does not evaluate SV or prove the reference model is correct.
    """
    tokens = [(m.group(), m.start()) for m in _SV_AUDIT_TOKEN.finditer(source)]
    words = [t[0] for t in tokens]
    pairs, stack = {}, []
    for i, word in enumerate(words):
        if word in ('(', '[', '{'):
            stack.append(i)
        elif word in (')', ']', '}') and stack:
            opening = stack.pop()
            pairs[opening] = i
    unknown = (frozenset(['unknown']), False)
    uninitialized = (frozenset(['uninitialized']), False)
    constant = (frozenset(['constant']), True)
    parameters = set(re.findall(r'\b(?:localparam|parameter)\b[^;=]*?\b(\w+)\s*=', source))
    contexts = {}

    def default(name):
        if name in parameters:
            return constant
        if re.match(r'(?:exp_|expected|ref_|next_)', name):
            return (frozenset(['model']), False)
        return unknown

    def expression(expr, env):
        terms = [m.group() for m in _SV_AUDIT_TOKEN.finditer(expr)]
        origins, bool_terms = set(), []
        i = 0
        while i < len(terms):
            word = terms[i]
            if re.match(r'^[$A-Za-z_]\w*$', word):
                if word in ('true', 'false'):
                    value = constant
                elif (word in ('bit', 'logic', 'byte', 'shortint', 'int', 'integer',
                               'longint', 'time', 'signed', 'unsigned') and
                      i + 1 < len(terms) and terms[i + 1] == "'"):
                    # A scalar cast contributes no observation of its own.
                    value = constant
                elif i + 2 < len(terms) and terms[i + 1] == '.':
                    while i + 2 < len(terms) and terms[i + 1] == '.':
                        i += 2
                    member = terms[i]
                    base = re.sub(r'_(?:pre|post)$', '', member)
                    value = (frozenset(['input' if member in input_names or base in input_names
                                        else 'observed']), False)
                elif i + 1 < len(terms) and terms[i + 1] == '(':
                    # User functions may read DUT state or have side effects.
                    value = constant if word in ('$isunknown', '$countones', '$onehot',
                                                   '$onehot0', '$unsigned', '$signed', '$clog2') else unknown
                else:
                    value = env.get(word, default(word))
                origins.update(value[0])
                bool_terms.append(value[1])
            i += 1
        # A paired if/else may encode a check only when its guard cannot be X.
        # Limit recognition to case comparisons or Boolean combinations of
        # already known two-state predicates; ordinary == is not sufficient.
        safe = (bool_terms and all(bool_terms) and
                not any(t in terms for t in ('==', '!=', '<', '>', '<=', '>=')))
        if ('===' in terms or '!==' in terms) and not any(
                t in terms for t in ('==', '!=', '<', '>', '<=', '>=', '?', '&', '|', '^')):
            # One case comparison, or a conjunction/disjunction of them.
            parts = re.split(r'&&|\|\|', expr)
            safe = all('===' in part or '!==' in part for part in parts)
        return (frozenset(origins or ['constant']), bool(safe))

    def text_at(start, end):
        return source[tokens[start][1]:tokens[end][1]] if start < end else ''

    def merge(left, right):
        result = {}
        for name in set(left) | set(right):
            a, b = left.get(name, default(name)), right.get(name, default(name))
            result[name] = (a[0] | b[0], a[1] and b[1])
        return result

    def simple(start, end, env, guards):
        values = words[start:end]
        if values and values[0] in ('logic', 'bit', 'int', 'integer', 'string', 'longint', 'time'):
            # A declared local without an initializer is not an unknown source
            # of DUT observations. Keep that distinct from unsupported syntax.
            depth = 0
            for word in values[1:]:
                if word == '[':
                    depth += 1
                elif word == ']':
                    depth -= 1
                elif not depth and re.match(r'^[A-Za-z_]\w*$', word) and word not in ('signed', 'unsigned'):
                    env[word] = uninitialized
                    break
        guard_sources = frozenset().union(*(g[2][0] for g in guards))
        for j in range(start, end):
            if words[j] == 'check_requirement' and j + 1 < end and words[j + 1] == '(':
                contexts[tokens[j][1]] = (dict(env), tuple(guards), expression)
        # Unknown calls could write an inout/reference parameter. Forget local
        # facts instead of falsely reusing the value from an earlier assignment.
        if (len(values) > 1 and values[1] == '(' and
                values[0] != 'check_requirement' and not values[0].startswith(('`', '$'))):
            for name in env:
                env[name] = unknown
            return
        op = next((j for j in range(start, end) if words[j] in
                   ('=', '+=', '-=', '*=', '/=', '++', '--')), None)
        if op is None:
            return
        lhs = words[start:op]
        if not lhs:  # prefix increment/decrement
            lhs = words[op + 1:end]
        # Only simple local variables and indexed local arrays are understood.
        # Member writes are left to VCS/the runtime checker.
        if '.' in lhs or '(' in lhs or '{' in lhs:
            return
        names, depth = [], 0
        for w in lhs:
            if w == '[':
                depth += 1
            elif w == ']':
                depth -= 1
            elif not depth and re.match(r'^[A-Za-z_]\w*$', w):
                names.append(w)
        if not names:
            return
        name = names[-1]
        value = expression(text_at(op + 1, end), env)
        if words[op] != '=' or '[' in lhs:
            old = env.get(name, default(name))
            value = (old[0] | value[0], False)
        env[name] = (value[0] | guard_sources, value[1])

    def statement(start, end, env, guards):
        if start >= end:
            return start
        word = words[start]
        if word == 'begin':
            j = start + 1
            if j < end and words[j] == ':':
                j += 2
            # Block-local declarations must not replace an outer alias forever.
            outer, locals_ = dict(env), set()
            while j < end and words[j] != 'end':
                if words[j] in ('logic', 'bit', 'int', 'integer', 'string', 'longint', 'time'):
                    k = j + 1
                    if k < end and words[k] in ('signed', 'unsigned'):
                        k += 1
                    if k < end and words[k] == '[':
                        k = pairs.get(k, k) + 1
                    if k < end:
                        locals_.add(words[k])
                        env[words[k]] = uninitialized
                j = statement(j, end, env, guards)
            for name in locals_:
                if name in outer:
                    env[name] = outer[name]
                else:
                    env.pop(name, None)
            j += 1
            return j + 2 if j < end and words[j] == ':' else j
        if word == 'if' and start + 1 in pairs:
            close = pairs[start + 1]
            guard = expression(text_at(start + 2, close), env)
            before, yes = dict(env), dict(env)
            stop = statement(close + 1, end, yes, guards + [(start, True, guard)])
            no = dict(before)
            if stop < end and words[stop] == 'else':
                stop = statement(stop + 1, end, no, guards + [(start, False, guard)])
            env.clear()
            env.update(merge(yes, no))
            return stop
        if word == 'forever' or (word in ('for', 'foreach', 'while', 'repeat') and start + 1 in pairs):
            close = start if word == 'forever' else pairs[start + 1]
            # Loop-control variables are not observations. Capture initialization
            # and include the zero-iteration path when merging the loop body.
            separators = [j for j in range(start + 2, close) if words[j] == ';']
            if word == 'for' and len(separators) == 2:
                simple(start + 2, separators[0], env, guards)
                control = text_at(separators[0] + 1, separators[1])
            elif word != 'forever':
                control = text_at(start + 2, close)
            else:
                control = "1'b1"
            loop_guards = guards + [(start, None, expression(control, env))]
            before, body = dict(env), dict(env)
            stop = statement(close + 1, end, body, loop_guards)
            joined = merge(before, body)
            # Revisit once for loop-carried dependencies. If not stable, forget
            # changing facts and visit conservatively (never invent constants).
            second = dict(joined)
            statement(close + 1, end, second, loop_guards)
            final = merge(joined, second)
            for name in final:
                if final[name] != joined.get(name, default(name)):
                    final[name] = unknown
            statement(close + 1, end, dict(final), loop_guards)
            env.clear()
            env.update(final)
            return stop
        if word in ('case', 'casex', 'casez', 'fork'):
            closing = 'endcase' if word != 'fork' else 'join'
            depth, j = 1, start + 1
            while j < end and depth:
                if ((closing == 'endcase' and words[j] in ('case', 'casex', 'casez')) or
                        (closing == 'join' and words[j] == 'fork')):
                    depth += 1
                if words[j] == closing or (closing == 'join' and words[j] in ('join_any', 'join_none')):
                    depth -= 1
                j += 1
            for name in env:
                env[name] = unknown
            # Calls inside unsupported constructs intentionally have no inferred
            # provenance. Direct-literal checks are still audited by the caller.
            return j
        j = start
        while j < end and words[j] not in (';', 'end', 'else'):
            if j in pairs:
                j = pairs[j] + 1
            else:
                j += 1
            # UVM reporting macros are statements without a trailing semicolon.
            if word.startswith('`') and j > start + 1:
                break
        simple(start, j, env, guards)
        return j + 1 if j < end and words[j] == ';' else max(j, start + 1)

    i = 0
    while i < len(words):
        if words[i] not in ('function', 'task'):
            i += 1
            continue
        closing = 'endfunction' if words[i] == 'function' else 'endtask'
        end = next((j for j in range(i + 1, len(words)) if words[j] == closing), len(words))
        start = next((j + 1 for j in range(i + 1, end) if words[j] == ';'), end)
        env = {}
        while start < end:
            start = statement(start, end, env, [])
        i = end + 1
    return contexts


def packed_payload_layout(yaml_spec):
    """Resolve the approved packed-struct schema, never infer it from DUT data."""
    declaration = (yaml_spec or {}).get('behavior', {}).get('request_struct', {})
    if not declaration.get('packed') or not declaration.get('typedef_name'):
        return None
    fields = declaration.get('fields', [])
    if not fields or any(not isinstance(f.get('width'), int) or f['width'] <= 0 or
                         not re.fullmatch(r'[A-Za-z_]\w*', str(f.get('name', ''))) for f in fields):
        return None
    width = sum(f['width'] for f in fields)
    remaining = width
    offsets = {}
    for field in fields:
        remaining -= field['width']
        offsets[field['name']] = (remaining, field['width'])
    return {'type': declaration['typedef_name'], 'width': width, 'offsets': offsets}


def attach_static_results(requirements, rtl_text):
    """Add a narrow proof of explicit, mutually exclusive storage updates.

    Recognize the simple indexed-register implementation only. Unsupported
    preprocessing, extra writers, other control expressions or representations
    remain unverified. This does not infer functionality from a signal's name.
    """
    requirements = _attach_static_results(requirements, rtl_text)
    source = _sv_without_comments(rtl_text)
    compact = re.sub(r'\s+', '', source)
    if (re.search(r'`(?:ifdef|ifndef|include|define)', source) or
            re.search(r'\b(?:force|release|alias|ref|inout|bind|function|task|initial)\b', source) or
            len(re.findall(r'\bmodule\s+', source)) != 1 or
            len(re.findall(r'\balways_ff\b', source)) != 1):
        return requirements
    instances = re.findall(r'\b(\w+)\s+(?:#\s*\([^;]*?\)\s*)?(\w+)\s*\(', source)
    if any(kind not in ('module', 'else', 'end', 'begin') for kind, _ in instances):
        return requirements
    flag_requirements = [(row, re.search(r'explicit\s+(\w+)\s+signal', row['text']))
                         for row in requirements]
    ordering = [(row, re.search(r'update entry data and (\w+) with explicit next-state ordering', row['text']))
                for row in requirements]
    for flag_row, flag_match in flag_requirements:
        if not flag_match:
            continue
        flag = flag_match.group(1)
        definitions = [re.fullmatch(r'define\s+' + re.escape(flag) + r'\s*=\s*(.+)', row['text'])
                       for row in requirements]
        definitions = [m.group(1) for m in definitions if m]
        if len(definitions) != 1:
            continue
        formula = re.sub(r'\s+', '', definitions[0])
        terms = re.fullmatch(r'(\w+)&&(\w+)&&\((\w+)==(\w+)\)', formula)
        if not terms or not re.search(r'\blogic\s+' + re.escape(flag) + r'\s*;', source):
            continue
        deq, enq, insertion, selected = terms.groups()
        assigned = [re.sub(r'\s+', '', value) for value in re.findall(
            r'(?<![.\w])' + re.escape(flag) + r'\s*=(?!=)\s*([^;]+);', source)]
        if assigned != [formula]:
            continue
        flag_writes = re.findall(r'(?<![.\w])' + re.escape(flag) +
                                 r'\s*(?:<=|=(?!=)|\+\+|--|[+&|^-]=)', source)
        if len(flag_writes) != 1:
            continue
        for order_row, order_match in ordering:
            if not order_match:
                continue
            output = order_match.group(1)
            aliases = re.findall(r'(?<![.\w])' + re.escape(output) + r'\s*=\s*(\w+)\s*;', source)
            if len(aliases) != 1:
                continue
            output_writes = re.findall(r'(?<![.\w])' + re.escape(output) +
                                      r'(?:\[[^\]]+\])?\s*(?:<=|=(?!=)|\+\+|--|[+&|^-]=)', source)
            if len(output_writes) != 1:
                continue
            valid = aliases[0]
            # Plain, single-statement clear and paired data/valid enqueue blocks.
            clears = list(re.finditer(r'if\(([^;{}]+)\)begin' + re.escape(valid) +
                          r'\[(\w+)\]<=1\'b0;end', compact))
            writes = list(re.finditer(r'if\(([^;{}]+)\)begin(\w+)\[(\w+)\]<=(\w+);' +
                          re.escape(valid) + r'\[\3\]<=1\'b1;end', compact))
            if len(clears) != 1 or len(writes) != 1:
                continue
            clear, write = clears[0], writes[0]
            if clear.group(2) != write.group(3):
                continue
            # Equality operands must use the SAME index/cast expression. Merely
            # spotting !reuse_slot somewhere in a block would not prove this.
            def clauses(condition):
                return set(condition.replace('(', '').replace(')', '').split('&&'))
            clear_terms, write_terms = clauses(clear.group(1)), clauses(write.group(1))
            clear_eq = [s for s in clear_terms if s.endswith('==' + selected)]
            write_eq = [s for s in write_terms if s.endswith('==' + insertion)]
            if len(clear_eq) != 1 or len(write_eq) != 1:
                continue
            index_expr = clear_eq[0].split('==')[0]
            if (index_expr != write_eq[0].split('==')[0] or
                    not re.fullmatch(r'(?:\w+\')?' + re.escape(clear.group(2)), index_expr) or
                    clear_terms != {deq, '!' + flag, clear_eq[0]} or
                    enq not in write_terms or len(write_terms) != 3):
                continue
            extras = write_terms - {enq, write_eq[0]}
            if len(extras) != 1 or not re.fullmatch(r'\w+', next(iter(extras))):
                continue
            valid_writes = re.findall(r'(?<![.\w])' + re.escape(valid) +
                                      r'(?:\[[^\]]+\])?\s*(?:<=|=(?!=)|\+\+|--|[+&|^-]=)', source)
            data = write.group(2)
            data_writes = re.findall(r'(?<![.\w])' + re.escape(data) +
                                     r'(?:\[[^\]]+\])?(?:\.\w+)?\s*(?:<=|=(?!=)|\+\+|--|[+&|^-]=)', source)
            # Verify the third validity writer and second payload writer really
            # are the common reset loop, not an unexamined competing operation.
            reset = re.search(re.escape(data) + r'\[(\w+)\]<=\'0;' + re.escape(valid) +
                              r'\[\1\]<=1\'b0;', compact)
            if len(valid_writes) != 3 or len(data_writes) != 2 or not reset:
                continue
            # Restrict the accepted control-flow shape to the known sequential
            # reset/else loop; no extra nested guards or independent processes.
            sequential = re.search(r'\balways_ff\s*@\(\s*(?:posedge|negedge)\s+\w+\s*\)\s*begin(.*?)'
                                   r'\n\s*end\s*\n\s*(?://[^\n]*\n\s*)?always_comb', source, re.S)
            if not sequential or len(re.findall(r'\bif\s*\(', sequential.group(1))) != 3:
                continue
            evidence = ('{} = {}; dequeue clears {} only under !{}; enqueue writes {} and '
                        '{} together outside reset. At the same index, an enqueue/dequeue overlap implies {}, '
                        'which excludes the clear. The result therefore does not depend on write '
                        'statement order. No additional data/valid writers were found.').format(
                            flag, definitions[0], valid, flag, data, valid, flag)
            for row in (flag_row, order_row):
                row['static_result'] = {'status': 'PASS', 'method': 'static', 'evidence': evidence}
            # A clock-edge sample alone cannot establish no intermediate invalid
            # state. Here direct output wiring plus exclusion of the clear adds
            # structural support; the existing runtime validity check is retained.
            for row in requirements:
                if 'intermediate invalid state' in row['text'] and flag in row['text']:
                    row['static_result'] = {'status': 'PASS', 'method': 'static', 'evidence': evidence}
                    row['requires_static_support'] = True
    for row in requirements:
        if 'intermediate invalid state' in row['text']:
            row['requires_static_support'] = True
            if row['static_result']['status'] != 'PASS':
                row['static_result']['evidence'] = ('Clock-edge comparisons do not prove absence of an intermediate '
                    'invalid state. Requires event-level observation or supported structural proof of clear suppression.')
    return requirements


def packed_payload_prompt(yaml_spec):
    layout = packed_payload_layout(yaml_spec)
    if not layout:
        return ''
    return ('\nAPPROVED PACKED PAYLOAD LAYOUT (first declared field is most significant):\n'
            '{} is {} bits: {}.\n'
            'For an array entry, prefer a cast of the complete word to this struct type. '
            'Do not decode its first field from the least significant bits. '
            'Print actual and expected payload hex values on any payload mismatch.\n\n').format(
                layout['type'], layout['width'], ', '.join('{}[{}:{}]'.format(name, low + width - 1, low)
                    for name, (low, width) in layout['offsets'].items()))


def audit_packed_payload_decoding(files, yaml_spec):
    """Catch reversed manual slices of ports specified as packed struct entries.

    Only complete, contiguous field assignments with a common word base are
    diagnosed. Custom wire layouts, symbolic slices and other syntax are not
    guessed. VCS remains responsible for language legality.
    """
    layout = packed_payload_layout(yaml_spec)
    if not layout:
        return []
    ports = {p['name'] for p in yaml_spec.get('ports', [])
             if re.search(r'\b' + re.escape(layout['type']) + r'\b', p.get('description', ''))}
    aliases = {p: p for p in ports}
    for _, code in files:
        for alias, signal in re.findall(r'\b(\w+)\s*=\s*(\w+)\s*;', _sv_without_comments(code)):
            if signal in ports:
                aliases[alias] = signal

    def slice_layout(value):
        value = re.sub(r'\s+', '', value)
        if re.fullmatch(r'\d+:\d+', value):
            high, low = [int(n) for n in value.split(':')]
            return ('', low, high - low + 1) if high >= low else None
        parts = value.split('+:')
        if len(parts) == 2 and parts[1].isdigit():
            start, width = parts[0], int(parts[1])
        elif len(parts) == 1 and ':' not in value:
            start, width = value, 1
        else:
            return None
        if start.isdigit():
            return '', int(start), width
        offset = re.fullmatch(r'(.+)\+(\d+)', start)
        return (offset.group(1), int(offset.group(2)), width) if offset else (start, 0, width)

    assignment = re.compile(r'(?P<object>\w+(?:\[[^\]\n]+\])?)\.(?P<field>\w+)\s*=\s*'
                            r'(?P<signal>\w+(?:\.\w+)*)\[(?P<slice>[^\]\n]+)\]\s*;')
    issues = []
    for path, code in files:
        source = _sv_without_comments(code)
        group = []
        for match in assignment.finditer(source):
            if (group and (source[group[-1].end():match.start()].strip() or
                           match.group('object') != group[-1].group('object') or
                           match.group('signal') != group[-1].group('signal'))):
                group = []
            group.append(match)
            if len(group) != len(layout['offsets']):
                continue
            typed = re.search(r'\b' + re.escape(layout['type']) + r'\s+' +
                              re.escape(match.group('object').split('[')[0]) + r'\b', source)
            slices = [slice_layout(m.group('slice')) for m in group]
            if (typed and match.group('signal').split('.')[-1] in aliases and
                    set(m.group('field') for m in group) == set(layout['offsets']) and
                    all(slices) and len(set(s[0] for s in slices)) == 1 and
                    any(s[1:] != layout['offsets'][m.group('field')] for m, s in zip(group, slices))):
                issues.append('{}:{}: packed payload field decoding disagrees with the YAML {} layout. '
                              'Cast the complete {}-bit entry to {}, or use these LSB offsets/widths: {}. '
                              'Keep payload comparisons active.'.format(
                                  path, source.count('\n', 0, group[0].start()) + 1,
                                  layout['type'], layout['width'], layout['type'], layout['offsets']))
            group = []
    return issues


def audit_generated_testbench(files, dut_spec=None, requirements=None):
    """Catch concrete generation-contract defects BEFORE simulation.

    This is deliberately a narrow lint, not an SV parser or a proof that every
    comparison is correct. In particular it does not infer DUT correctness.
    """
    issues = []
    packages = []
    checked_ids = set()
    unsupported_ids = set()
    input_names = {p.get('name') for p in (dut_spec or {}).get('ports', [])
                   if p.get('dir', p.get('direction')) == 'input'}
    # Clocking blocks commonly rename rst_n to rst_pre. That remains a DUT
    # input, even though removing the _pre suffix does not recover its name.
    for _, code in files:
        for block in re.finditer(r'\bclocking\b.*?\bendclocking\b', _sv_without_comments(code), re.S):
            for declaration in re.finditer(r'\binput\b([^;]*);', block.group()):
                for alias, signal in re.findall(r'\b(\w+)\s*=\s*(\w+)\b', declaration.group(1)):
                    if signal in input_names:
                        input_names.add(alias)
    for filename, code in files:
        # An explicit unsupported reason is a coverage gap, never a passing check.
        unsupported_ids.update(re.findall(
            r'^\s*//\s*AGENT2_UNSUPPORTED\|(REQ-\d{3,})\|[^\r\n\s][^\r\n]*',
            code, re.M))
        source = _sv_without_comments(code)
        signatures, scope_at = _requirement_check_signatures(source)
        declaration_offsets = {signature['offset'] for signature in signatures}
        contexts = _sv_check_contexts(source, input_names)
        calls, branches = [], {}
        if os.path.basename(filename) == 'dut_pkg.sv':
            packages.append(source)
        # Match call names outside strings (strings may describe example code).
        pattern = r'"(?:\\.|[^"\\])*"|\bcheck_requirement\s*\('
        for match in re.finditer(pattern, source):
            if match.group(0).startswith('"'):
                continue
            if match.start() in declaration_offsets:
                continue
            args = _sv_call_arguments(source, match.end() - 1)
            ids = []
            for arg in args:
                req_literal = re.fullmatch(r'"(REQ-\d{3,})"', arg.strip())
                named_literal = re.fullmatch(r'\.\w+\s*\(\s*"(REQ-\d{3,})"\s*\)', arg.strip())
                if req_literal or named_literal:
                    ids.append((req_literal or named_literal).group(1))
                    checked_ids.add(ids[-1])
            resolved = _requirement_check_condition(args, signatures, scope_at(match.start()))
            if resolved is None:
                continue
            condition, parameter = resolved
            condition = condition.strip()
            while condition.startswith('(') and condition.endswith(')'):
                condition = condition[1:-1].strip()
            literal = re.fullmatch(r"(?:[01]|'[01]|1'[bBdDhH][01]|true|false)", condition)
            context = contexts.get(match.start())
            branch_key = None
            if literal and context and ids and context[1]:
                env, guards, evaluate = context
                branch, arm, guard = guards[-1]
                if arm is not None and 'observed' in guard[0] and guard[1]:
                    branch_key = (branch, tuple(ids), guards[:-1])
                    outcome = condition in ('1', "'1", 'true') or bool(re.search(r"'[bBdDhH]1$", condition))
                    branches.setdefault(branch_key, []).append((arm, outcome))
            call_text = 'check_requirement({})'.format(', '.join(args))
            calls.append((match.start(), condition, parameter, literal, context, branch_key,
                          ids, re.sub(r'\s+', ' ', call_text)))
        for offset, condition, parameter, literal, context, branch_key, ids, call_text in calls:
            reason = None
            paired = (branch_key is not None and len(branches[branch_key]) == 2 and
                      set(branches[branch_key]) in ({(True, True), (False, False)},
                                                   {(True, False), (False, True)}))
            if literal and not paired:
                reason = ('constant check condition; pass the real DUT comparison '
                          'instead of counting stimulus or assuming success')
            elif paired:
                pass  # A two-state DUT comparison controls opposite outcomes.
            elif re.fullmatch(r'(?:exp_|ref_)\w+\s*(?:===|==|!==|!=)\s*'
                              r'(?:exp_|ref_)\w+', condition):
                reason = ('comparison between reference-model variables only; '
                          'compare actual monitored DUT state to expected state')
            else:
                operands = re.split(r'\s*(?:===|==|!==|!=)\s*', condition)
                if len(operands) == 2:
                    for predicted, sampled in (operands, operands[::-1]):
                        sampled_input = re.fullmatch(r'\w+\.(\w+)', sampled)
                        if (re.fullmatch(r'(?:exp_|ref_)\w+', predicted) and
                                sampled_input and sampled_input.group(1) in input_names):
                            reason = ('reference prediction compared only with a DUT input; '
                                      'verify the effect on monitored DUT outputs')
                if reason is None and context:
                    env, guards, evaluate = context
                    sources, _ = evaluate(condition, env)
                    if sources <= {'constant', 'input', 'model', 'uninitialized'}:
                        reason = ('condition depends only on {} through local assignments; '
                                  'compare a monitored DUT output/state with its independent '
                                  'YAML prediction'.format(', '.join(sorted(sources))))
            if reason:
                issues.append('{}:{}: {} in {}; requirement={}; condition={}; call={}'.format(
                    filename, source.count('\n', 0, offset) + 1, reason, parameter,
                    ','.join(ids) or 'dynamic ID', condition, call_text))
        for match in re.finditer(r'AGENT2_REQ\|REQ-\d{3,}\|PASS\|', source):
            issues.append('{}:{}: literal PASS marker; derive status from executed '
                          'comparison/failure counters'.format(
                              filename, source.count('\n', 0, match.start()) + 1))
    package = '\n'.join(packages)
    for marker in ('AGENT2_REQ|', 'AGENT2_REQ_COUNTS|', 'AGENT2_REQ_EVIDENCE|'):
        if marker not in package:
            issues.append('dut_pkg.sv: missing {} runtime reporting'.format(marker))
    for req in requirements or []:
        if (req.get('verification_method', 'simulation') == 'simulation' and
                req['id'] not in checked_ids and req['id'] not in unsupported_ids):
            issues.append('{}: missing executable check_requirement call with literal ID "{}". '
                          'Implement a monitored DUT comparison and directed stimulus for: {}. '
                          'If required information or observability is unavailable, record '
                          '// AGENT2_UNSUPPORTED|{}|specific reason and leave it unverified.'.format(
                              req['id'], req['id'], req['text'], req['id']))
    return issues


def install_requirement_evidence(files):
    """Standardize reporting around generated comparisons, preserving checks.

    Keep the generated helper and add independent per-ID comparison counts,
    failures, evidence and report-phase output. Legacy three-argument helpers
    must supply their own complete evidence and are not guessed at.
    """
    installed = []
    for path, code in files:
        code = re.sub(r'(?:\n|^)[ \t]*// AGENT2_REPORT_BEGIN[^\n]*\n.*?'
                      r'^[ \t]*// AGENT2_REPORT_END[^\n]*\n?', '', code, flags=re.M | re.S)
        code = re.sub(r'\b_agent2_generated_report_phase\b', 'report_phase', code)
        code = re.sub(r'(?:\n|^)[ \t]*// AGENT2_EVIDENCE_BEGIN[^\n]*\n.*?'
                      r'^[ \t]*// AGENT2_EVIDENCE_END[^\n]*\n?', '', code, flags=re.M | re.S)
        code = re.sub(r'(\bfunction\s+(?:automatic\s+)?void\s+)'
                      r'_agent2_generated_check_requirement\b', r'\1check_requirement', code)
        code = re.sub(r'\bendfunction\s*:\s*_agent2_generated_check_requirement\b',
                      'endfunction : check_requirement', code)
        source = _sv_without_comments(code)
        signatures, _ = _requirement_check_signatures(source)
        for signature in reversed(signatures):
            names = [name for name, _ in signature['parameters']]
            def role(choices):
                matches = [name for name in names if name.lower() in choices]
                return matches[0] if len(matches) == 1 else None
            rid = role(('id', 'req_id', 'requirement_id', 'rid'))
            scenario = role(('scenario', 'scenario_str', 'scenario_description'))
            expected = role(('expected', 'expected_str', 'exp_str', 'expected_value'))
            observed = role(('observed', 'observed_str', 'obs_str', 'actual_str', 'observed_value'))
            position = signature['condition_index']
            if not all((rid, scenario, expected, observed)) or position is None:
                continue
            start = source.rfind('function', 0, signature['offset'])
            finish = re.search(r'\bendfunction\b(?:\s*:\s*check_requirement\b)?',
                               source[signature['offset']:])
            if start < 0 or not finish:
                continue
            stop = signature['offset'] + finish.end()
            header_end = code.find(';', signature['offset'], stop)
            header = code[start:header_end]
            if not re.match(r'function\s+(?:automatic\s+)?void\s+check_requirement\b', header):
                continue
            original = code[start:stop].replace('check_requirement',
                                                '_agent2_generated_check_requirement', 1)
            original = re.sub(r'\bendfunction\s*:\s*check_requirement\b',
                              'endfunction : _agent2_generated_check_requirement', original)
            wrapper = '''
// AGENT2_EVIDENCE_BEGIN canonical comparison records (owned by Agent 2)
bit _agent2_evidence_seen[string];
bit _agent2_failure_seen[string];
int _agent2_req_checks[string];
int _agent2_req_failures[string];
function void _agent2_report_requirements();
    string id;
    string status;
    foreach (_agent2_req_checks[id]) begin
        status = (_agent2_req_failures[id] > 0) ? "FAIL" : "PASS";
        `uvm_info("AGENT2_REPORT", $sformatf("AGENT2_REQ_COUNTS|%s|%0d|%0d",
            id, _agent2_req_checks[id], _agent2_req_failures[id]), UVM_NONE)
        `uvm_info("AGENT2_REPORT", $sformatf("AGENT2_REQ|%s|%s|%0d comparisons, %0d failures",
            id, status, _agent2_req_checks[id], _agent2_req_failures[id]), UVM_NONE)
    end
endfunction
function automatic string _agent2_evidence_field(string value);
    for (int i = 0; i < value.len(); i++) begin
        if (value.getc(i) == 8'd124 || value.getc(i) == 8'd10 || value.getc(i) == 8'd13)
            value.putc(i, 8'd32);
    end
    return value;
endfunction
{header};
    if (!_agent2_req_checks.exists({rid})) begin
        _agent2_req_checks[{rid}] = 0;
        _agent2_req_failures[{rid}] = 0;
    end
    _agent2_req_checks[{rid}]++;
    if ({condition} !== 1'b1) _agent2_req_failures[{rid}]++;
    if (!_agent2_evidence_seen.exists({rid}) ||
        ({condition} !== 1'b1 && !_agent2_failure_seen.exists({rid}))) begin
        `uvm_info("AGENT2_EVIDENCE", $sformatf(
            "AGENT2_REQ_EVIDENCE|%s|%s|time=%0t|expected=%s|observed=%s",
            {rid}, _agent2_evidence_field({scenario}), $time,
            _agent2_evidence_field({expected}), _agent2_evidence_field({observed})), UVM_NONE)
        _agent2_evidence_seen[{rid}] = 1'b1;
        if ({condition} !== 1'b1) _agent2_failure_seen[{rid}] = 1'b1;
    end
    _agent2_generated_check_requirement({arguments});
endfunction
// AGENT2_EVIDENCE_END canonical comparison records
'''.format(header=header, rid=rid, condition=names[position], scenario=scenario,
           expected=expected, observed=observed, arguments=', '.join(names))
            code = code[:start] + original + wrapper + code[stop:]
        source = _sv_without_comments(code)
        classes = list(re.finditer(r'\bclass\s+\w+[^;]*;.*?\bendclass\b', source, re.S))
        for cls in reversed(classes):
            block = code[cls.start():cls.end()]
            if 'function void _agent2_report_requirements();' not in block:
                continue
            phase = re.search(r'\bfunction\s+(?:automatic\s+)?void\s+report_phase\s*'
                              r'\(\s*uvm_phase\s+\w+\s*\)\s*;.*?'
                              r'\bendfunction\b(?:\s*:\s*report_phase\b)?', block, re.S)
            if not phase and not re.search(r'\bextends\s+uvm_(?:scoreboard|component|subscriber)\b', block):
                continue
            call = '_agent2_generated_report_phase(phase);' if phase else 'super.report_phase(phase);'
            reporting = '''
// AGENT2_REPORT_BEGIN per-requirement counts from executed comparisons
function void report_phase(uvm_phase phase);
    CALL
    _agent2_report_requirements();
endfunction
// AGENT2_REPORT_END
'''.replace('CALL', call)
            if phase:
                original = phase.group().replace('report_phase', '_agent2_generated_report_phase', 1)
                original = re.sub(r'endfunction\s*:\s*report_phase\b',
                                  'endfunction : _agent2_generated_report_phase', original)
                block = block[:phase.start()] + original + reporting + block[phase.end():]
            else:
                end = block.rfind('endclass')
                block = block[:end] + reporting + block[end:]
            code = code[:cls.start()] + block + code[cls.end():]
        installed.append((path, code))
    return installed


def audit_repair_context(files, issues, requirements):
    """Locate the rejected CALL, not a line in an LLM reply/thinking block."""
    sources = {os.path.basename(path): code.splitlines() for path, code in files}
    sections = ['Line numbers below refer to parsed SV files, never the raw LLM response. '
                'Repair the identified requirement and condition. Do not infer a different '
                'requirement from a similar counter or from raw-response line numbers.']
    seen = set()
    for issue in issues:
        sections.append(issue)
        location = re.match(r'([^:]+\.sv):(\d+):', issue)
        if location:
            name, line = os.path.basename(location.group(1)), int(location.group(2))
            if (name, line) not in seen:
                seen.add((name, line))
                lines = sources.get(name, [])
                sections.append('REJECTED CALL CONTEXT — {}:\n{}'.format(name, '\n'.join(
                    '{}:{}: {}'.format(name, i + 1, lines[i])
                    for i in range(max(0, line - 12), min(len(lines), line + 3)))))
        ids = set(re.findall(r'REQ-\d{3,}', issue))
        for req in requirements:
            if req['id'] in ids:
                sections.append('Required behavior for {}: {}'.format(req['id'], req['text']))
    sections.append(
        'For a constant/model-only condition, repair the observation path as well as '
        'the call. If the requirement defines an internal RTL control signal, expose '
        'that real signal through a read-only top-level hierarchical connection into '
        'dut_if, sample it at the correct edge in dut_monitor, and compare the sample '
        'against the independently computed YAML expectation. Include all affected '
        'files. Assigning observed=expected, renaming cond, changing an integer index, '
        'or editing evidence strings is not a repair. Existing output comparisons '
        'may be shared only when they establish the SAME required behavior.')
    return '\n\n'.join(sections)


# ============================================================
# LOAD YAML APPROVED BY AGENT 1
# ============================================================

def coverage_gap_targets(report, files):
    """Only a clean run with unexecuted, implemented checks needs more traffic."""
    if (report.get('overall_status') not in ('PASS', 'PASS_WITH_GAPS') or
            report.get('uvm_errors') or report.get('uvm_fatals') or
            report.get('failed_requirements')):
        return []
    unsupported = set(re.findall(r'//\s*AGENT2_UNSUPPORTED\|(REQ-\d{3,})\|',
                                 '\n'.join(code for _, code in files)))
    return [row for row in report['requirements']
            if row['verification_method'] == 'simulation' and
            row['status'] == 'NOT_TESTED' and row['checks'] == 0 and
            row['id'] not in unsupported]


def install_coverage_sequence(files, raw, name, parent):
    """Append sequence traffic while keeping compiled checkers byte-for-byte.

    This is a narrow generated-code contract, not a security sandbox. Extensions
    are restricted to a constructor and a body using the existing sequencer.
    """
    code = strip_markdown_fence(re.sub(r'<think>.*?</think>', '', str(raw), flags=re.S)).strip()
    source = _sv_without_comments(code).strip()
    header = r'class\s+' + re.escape(name) + r'\s+extends\s+' + re.escape(parent) + r'\s*;'
    if not re.match(header, source) or not re.search(r'\bendclass\s*$', source):
        raise ValueError('Return only class {} extends {}; ... endclass.'.format(name, parent))
    if len(re.findall(r'\bclass\b', source)) != 1:
        raise ValueError('Coverage extension must contain exactly one sequence class.')
    lexical = re.sub(r'"(?:\\.|[^"\\])*"', '""', source)
    forbidden = (r'`(?:include|define|undef)|\b(?:force|release|disable|fork|join|'
                 r'virtual|interface|initial|always|final|extern|import|export|'
                 r'uvm_config_db|uvm_resource_db|uvm_report_catcher|uvm_root|'
                 r'set_type_override\w*|set_inst_override\w*|set_report_\w*|'
                 r'check_requirement|req_checks|req_fails|pass_cnt|fail_cnt)\b|'
                 r'\$(?:root|finish|stop|system)|AGENT2_REQ|\.(?:vif|sb|scoreboard)\b')
    if re.search(forbidden, lexical):
        raise ValueError('Coverage extension may only add sequence traffic, not change checkers or simulation control.')
    routines = list(re.finditer(r'\b(task|function)\b(.*?)\b(endtask|endfunction)\b', source, re.S))
    if (len(routines) != 2 or
            not any(re.match(r'\s*new\s*\(', r.group(2)) and r.group(1) == 'function' for r in routines)):
        raise ValueError('Provide only function new and task body in the coverage sequence.')
    body = next((r for r in routines if r.group(1) == 'task' and
                 re.match(r'\s*body\s*\(\s*\)\s*;', r.group(2))), None)
    if not body or len(re.findall(r'\bsuper\s*\.\s*body\s*\(\s*\)\s*;', source)) != 1:
        raise ValueError('Coverage task body must call super.body() exactly once before added stimulus.')
    prefix = body.group(2).split(';', 1)[1].split('super', 1)[0].strip()
    declarations = r'(?:(?:const|var)\s+)?\w+(?:\s*\[[^\]]+\])?\s+\w+(?:\s*\[[^\]]*\])?\s*;'
    if re.sub(declarations, '', prefix).strip():
        raise ValueError('Only local declarations may precede the unconditional super.body() call.')
    by_name = {os.path.basename(path): content for path, content in files}
    package, top = by_name['dut_pkg.sv'], by_name['dut_tb_top.sv']
    if not re.search(r'\bclass\s+' + re.escape(parent) + r'\b', _sv_without_comments(package)):
        raise ValueError('The baseline sequence {} is unavailable for extension.'.format(parent))
    endings = list(re.finditer(r'\bendpackage\b', _sv_without_comments(package)))
    starts = list(re.finditer(r'\brun_test\s*\(', _sv_without_comments(top)))
    if len(endings) != 1 or len(starts) != 1:
        raise ValueError('Coverage extension requires one dut_pkg package and one run_test call.')
    offset = endings[0].start()
    by_name['dut_pkg.sv'] = package[:offset] + code + '\n' + package[offset:]
    offset = starts[0].start()
    statement_end = top.find(';', starts[0].end())
    if statement_end < 0:
        raise ValueError('The top-level run_test statement is incomplete.')
    override = ('dut_sequence::type_id::set_type_override({}::get_type());\n        '.format(name))
    by_name['dut_tb_top.sv'] = (top[:offset] + 'begin\n        ' + override +
                              top[offset:statement_end + 1] + '\n    end' + top[statement_end + 1:])
    return [(path, by_name[os.path.basename(path)]) for path, _ in files]


def prompt_coverage_extension(state, yaml_spec):
    return '''The saved baseline simulation had zero mismatches. Some implemented
checks never executed. Add directed UVM sequence traffic that satisfies their
actual preconditions. Preserve the baseline: call super.body() first, then
send additional items through its existing sequencer/driver. Restore a known
state using legal reset transactions if needed. Use distinct payloads and
explicit boundary/simultaneous cases; random traffic is not a substitute.

Return ONLY class {name} extends {parent}; ... endclass (no file header).
Register it with `uvm_object_utils({name}). Include only function new and task
body(). Put local declarations first, then an unconditional super.body();.
Do not access the DUT/interface directly, change checkers or reporting, end
simulation, or change factory/configuration. Agent 2 installs the override.
The DUT, monitor, predictor and all existing sequence classes remain frozen.

UNEXECUTED REQUIREMENTS WITH ACTUAL COUNTS:
{gaps}

APPROVED SPECIFICATION:
{spec}

EXISTING COMPILED TESTBENCH (read to use its item fields and checker guards):
{sources}

PREVIOUS EXTENSION DIAGNOSTIC (if any):
{error}
'''.format(name=state['name'], parent=state['parent'], gaps=json.dumps(state['gaps'], indent=2),
           spec=safe_yaml_dump(yaml_spec), error=state.get('error', ''),
           sources='\n'.join('=== {} ===\n{}'.format(p, s) for p, s in state['base']))


def load_yaml_spec_from_validation(
    validation_result
):
    spec_file = validation_result.get(
        "spec_file"
    )

    if not spec_file:

        return (
            None,
            None,
            "Agent 1 result does not contain spec_file."
        )

    if not os.path.exists(
        spec_file
    ):

        return (
            None,
            spec_file,
            "YAML file does not exist: {}".format(
                spec_file
            )
        )

    try:

        with open(
            spec_file,
            "r"
        ) as f:

            spec = yaml.safe_load(
                f
            )

    except Exception as exc:

        return (
            None,
            spec_file,
            "Could not load YAML: {}".format(
                exc
            )
        )

    if not isinstance(
        spec,
        dict
    ):

        return (
            None,
            spec_file,
            "YAML root is not an object."
        )

    return (
        spec,
        spec_file,
        None
    )


# ============================================================
# YAML REQUIREMENT EXTRACTION
# ============================================================

def format_requirements_for_prompt(
    requirements
):
    if not requirements:

        return (
            "(No explicit YAML requirements "
            "were extracted.)"
        )

    lines = []

    for req in requirements:

        lines.append(
            "{} [{}] {}: {}".format(
                req["id"],
                req["category"],
                req["source"],
                req["text"] + " (verification method: " + req.get("verification_method", "simulation") + ")",
            )
        )

    return "\n".join(
        lines
    )


# ============================================================
# DUT SPEC HELPERS
# ============================================================

def format_dut_spec_for_prompt(
    dut_spec
):
    return json.dumps(
        dut_spec,
        indent=2
    )


def build_port_summary(
    dut_spec
):
    lines = []

    for port in dut_spec.get(
        "ports",
        []
    ):

        lines.append(
            "- {} {} width={} range={}".format(
                port.get(
                    "dir"
                ),

                port.get(
                    "name"
                ),

                port.get(
                    "width"
                ),

                port.get(
                    "range"
                ),
            )
        )

    return "\n".join(
        lines
    )


def detect_single_clock_name(
    dut_spec
):
    clocks = dut_spec.get(
        "clock_ports",
        []
    )

    if len(
        clocks
    ) == 1:

        return clocks[
            0
        ]

    return None


def detect_single_reset(
    dut_spec
):
    resets = dut_spec.get(
        "reset_ports",
        []
    )

    if len(
        resets
    ) == 1:

        return resets[
            0
        ]

    return None


def startup_reset_config(dut_spec, yaml_spec, rtl_code):
    """Select a declared single clock/reset domain; never guess another domain."""
    clock = detect_single_clock_name(dut_spec)
    reset = detect_single_reset(dut_spec)
    if not clock or not isinstance(reset, dict):
        return None
    approved = (yaml_spec or {}).get('reset', {})
    if isinstance(approved, dict) and approved.get('name') == reset.get('name'):
        reset = dict(reset, **approved)
    name, active_low = reset.get('name'), reset.get('active_low')
    if not isinstance(active_low, bool) or not all(
            re.fullmatch(r'[A-Za-z_]\w*', pin or '') for pin in (clock, name)):
        return None
    clock_spec = (yaml_spec or {}).get('clock', {})
    edge = clock_spec.get('edge', clock_spec.get('active_edge')) if isinstance(clock_spec, dict) else None
    edge = {'rising': 'posedge', 'falling': 'negedge', 'positive': 'posedge',
            'negative': 'negedge'}.get(edge, edge)
    if edge is None:
        edges = set(re.findall(r'\b(posedge|negedge)\s+' + re.escape(clock) + r'\b',
                               _sv_without_comments(rtl_code)))
        if len(edges) != 1:
            return None
        edge = edges.pop()
    if edge not in ('posedge', 'negedge'):
        return None
    return {'clock': clock, 'reset': name, 'active_level': 0 if active_low else 1,
            'active_edge': edge, 'drive_edge': 'negedge' if edge == 'posedge' else 'posedge'}


def install_startup_reset_contract(files, config):
    """Own startup reset in the UVM driver, independently of generated stimulus.

    The small SV helper is generated from the approved domain. It does not check
    RTL functionality or drive any data/control inputs. The original driver and
    monitor continue doing that. Reinstallation is idempotent across LLM repairs.
    """
    if config is None:
        return files
    by_name = {}
    for path, code in files:
        by_name[os.path.basename(path)] = re.sub(
            r'^[ \t]*// AGENT2_RUNTIME_BEGIN[^\n]*\n.*?'
            r'^[ \t]*// AGENT2_RUNTIME_END[^\n]*\n?', '', code, flags=re.M | re.S)
    package = by_name['dut_pkg.sv']
    interface = by_name['dut_if.sv']
    top = by_name['dut_tb_top.sv']
    clock, reset = config['clock'], config['reset']
    for pin in (clock, reset):
        if not re.search(r'\b' + re.escape(pin) + r'\b', _sv_without_comments(interface)):
            raise ValueError('dut_if.sv: startup reset requires the declared {} signal.'.format(pin))
    driver = re.search(r'\bclass\s+dut_driver\b.*?\bendclass\b', _sv_without_comments(package), re.S)
    if not driver:
        raise ValueError('dut_pkg.sv: startup reset requires class dut_driver with a virtual dut_if handle.')
    driver_source = package[driver.start():driver.end()]
    driver_source = re.sub(r'(\btask\s+(?:automatic\s+)?)_agent2_generated_run_phase\s*\(',
                           r'\1run_phase(', driver_source)
    driver_source = re.sub(r'\bendtask\s*:\s*_agent2_generated_run_phase\b',
                           'endtask : run_phase', driver_source)
    handle = re.search(r'\bvirtual\s+(?:interface\s+)?dut_if\s+(\w+)\s*;', driver_source)
    task = re.search(r'\btask\s+(?:automatic\s+)?run_phase\s*\(\s*uvm_phase\s+(\w+)\s*\)',
                     driver_source)
    if not handle or not task:
        raise ValueError('dut_pkg.sv: dut_driver needs virtual dut_if and task run_phase(uvm_phase phase) '
                         'so Agent 2 can deliver startup reset before sequence traffic.')
    # Multiple reset owners defeated earlier generated startup sequences.
    # Ask for a concrete correction instead of quietly discarding user stimulus.
    top_code = _sv_without_comments(top)
    if re.search(r'\.\s*' + re.escape(reset) + r'\s*(?:<=|=(?!=))', top_code):
        raise ValueError('dut_tb_top.sv: reset {} has a second procedural driver. '
                         'Remove top-level reset writes; the UVM driver owns reset. '
                         'Keep the DUT reset port connection.'.format(reset))
    # A clocking output retains its own drive value. Directly writing the
    # underlying variable leaves that output uninitialized and it can overwrite
    # reset on the next clocking event. Share the generated driver's output.
    reset_outputs = []
    for cb in re.finditer(r'\bclocking\s+(\w+)\s*@\(\s*(posedge|negedge)\s+' +
                          re.escape(clock) + r'\s*\)\s*;(.*?)\bendclocking\b',
                          _sv_without_comments(interface), re.S):
        for declaration in re.finditer(r'\b(output|inout)\s+([^;]+);', cb.group(3)):
            pins = re.sub(r'#\s*(?:\([^)]*\)|[\w.]+)\s*', '', declaration.group(2))
            for pin in pins.split(','):
                names = pin.strip().split('=')
                alias = names[0].strip()
                target = names[-1].strip()
                if target == reset:
                    if cb.group(2) != config['drive_edge']:
                        raise ValueError('dut_if.sv: reset clocking output must use the opposite DUT edge.')
                    reset_outputs.append((cb.group(1), alias))
    if len(reset_outputs) > 1:
        raise ValueError('dut_if.sv: reset has multiple clocking output drivers; use one reset owner.')
    if reset_outputs:
        cb_name, alias = reset_outputs[0]
        reset_target = cb_name + '.' + alias
        drive_wait = '@({});'.format(cb_name)
        assignment = '<='
    else:
        reset_target = reset
        drive_wait = '@({} {});'.format(config['drive_edge'], clock)
        assignment = '='
    reset_names = [reset] + [alias for _, alias in reset_outputs]
    outside_driver = package[:driver.start()] + package[driver.end():]
    outside_code = _sv_without_comments(outside_driver)
    interface_handles = set(re.findall(
        r'\bvirtual\s+(?:interface\s+)?dut_if\s+(\w+)\s*;', outside_code))
    for signal in set(reset_names):
        if any(re.search(r'\b' + re.escape(bus) + r'\s*\.\s*(?:\w+\s*\.\s*)?' +
                         re.escape(signal) + r'\s*(?:<=|=(?!=))', outside_code)
               for bus in interface_handles):
            raise ValueError('dut_pkg.sv: reset {} is driven outside dut_driver (for example '
                             'by dut_test). Remove competing startup reset writes; Python '
                             'delivers startup reset in the driver. Send later reset scenarios '
                             'through the sequencer/driver, not another component.'.format(reset))
    helper = '''// AGENT2_RUNTIME_BEGIN startup reset (owned by Agent 2)
    clocking agent2_reset_cb @({active_edge} {clock});
        input #1step sampled_reset = {reset};
    endclocking
    task automatic agent2_startup_reset();
        int unsigned sampled_edges;
        sampled_edges = 0;
        {initial_wait}
        {reset_target} {assignment} 1'b{active_level};
        {drive_wait}
        repeat (2) begin
            @(agent2_reset_cb);
            if (agent2_reset_cb.sampled_reset !== 1'b{active_level})
                uvm_pkg::uvm_report_fatal("AGENT2_TB_RESET",
                    "Startup reset was not active at the DUT consuming edge");
            sampled_edges++;
        end
        {drive_wait}
        {reset_target} {assignment} 1'b{inactive_level};
        uvm_pkg::uvm_report_info("AGENT2_TB_RESET",
            $sformatf("AGENT2_RESET|{reset}|READY|%0d|time=%0t", sampled_edges, $time),
            uvm_pkg::UVM_NONE);
    endtask
// AGENT2_RUNTIME_END startup reset
'''.format(inactive_level=1 - config['active_level'], reset_target=reset_target,
           assignment=assignment, drive_wait=drive_wait,
           initial_wait=drive_wait if reset_outputs else '', **config)
    interface_end = list(re.finditer(r'\bendinterface\b', _sv_without_comments(interface)))
    if len(interface_end) != 1:
        raise ValueError('dut_if.sv: expected one complete dut_if interface for startup reset.')
    end = interface_end[0].start()
    by_name['dut_if.sv'] = interface[:end] + helper + interface[end:]
    wrapper = '''// AGENT2_RUNTIME_BEGIN driver startup (owned by Agent 2)
    task run_phase(uvm_phase phase);
        {handle}.agent2_startup_reset();
        _agent2_generated_run_phase(phase);
    endtask
// AGENT2_RUNTIME_END driver startup
'''.format(handle=handle.group(1))
    driver_source = driver_source[:task.start()] + re.sub(
        r'\brun_phase\b', '_agent2_generated_run_phase', driver_source[task.start():], count=1)
    driver_source = re.sub(r'\bendtask\s*:\s*run_phase\b',
                           'endtask : _agent2_generated_run_phase', driver_source)
    end = driver_source.rfind('endclass')
    driver_source = driver_source[:end] + wrapper + driver_source[end:]
    by_name['dut_pkg.sv'] = package[:driver.start()] + driver_source + package[driver.end():]
    return [(name, by_name[name]) for name in COMPILE_ORDER]


def normalize_lowest_slot_search(files, requirements):
    """Repair the known zero-sentinel loop only under an explicit YAML rule.

    Match the entire side-effect-free search loop, not arbitrary model code.
    Keep the original empty-mask/default handling and all DUT comparisons.
    """
    approved = any(re.search(r'lowest[- ]index free slot|first free slot', r.get('text', ''), re.I)
                   for r in requirements or [])
    if not approved:
        return files
    zero = r"(?:0|\d+'[bdh]0+)"
    pattern = (r'for\s*\(\s*int\s+(?P<j>\w+)\s*=\s*0\s*;\s*(?P=j)\s*<\s*(?P<bound>\w+)\s*;\s*(?P=j)\+\+\s*\)\s*begin\s*'
               r'if\s*\(\s*(?P<mask>\w+)\s*\[\s*(?P=j)\s*\]\s*&&\s*(?P<idx>\w+)\s*==\s*' + zero +
               r'\s*&&\s*(?P=j)\s*>\s*0\s*\)\s*begin\s*(?P=idx)\s*=\s*(?P=j)\s*;\s*end\s*'
               r'else\s+if\s*\(\s*(?P=mask)\s*\[\s*(?P=j)\s*\]\s*&&\s*(?P=j)\s*==\s*0\s*\)\s*begin\s*'
               r'(?P=idx)\s*=\s*' + zero + r'\s*;\s*end\s*end\b')
    output = []
    simple_pattern = (r'for\s*\(\s*(?P<decl>int\s+)?(?P<j>\w+)\s*=\s*0\s*;\s*(?P=j)\s*<\s*(?P<bound>\w+)\s*;\s*(?P=j)\+\+\s*\)\s*begin\s*'
                      r'if\s*\(\s*(?P<mask>\w+)\s*\[\s*(?P=j)\s*\]\s*&&\s*(?P<idx>\w+)\s*==\s*' + zero +
                      r'\s*\)\s*begin\s*(?P=idx)\s*=\s*(?P=j)\s*;\s*end\s*end\b')
    for name, code in files:
        if os.path.basename(name) != 'dut_pkg.sv':
            output.append((name, code))
            continue
        # Mask strings too: examples in diagnostics must never become code edits.
        masked = re.sub(r'"(?:\\.|[^"\\])*"', lambda m: ' ' * len(m.group()),
                        _sv_without_comments(code))
        matches = list(re.finditer(pattern, masked)) + list(re.finditer(simple_pattern, masked))
        for match in sorted(matches, key=lambda m: m.start(), reverse=True):
            fields = match.groupdict()
            fields['decl'] = fields.get('decl', 'int ') or ''
            replacement = ("for ({decl}{j} = 0; {j} < {bound}; {j}++) begin\n"
                           "                if ({mask}[{j}]) begin\n"
                           "                    {idx} = {j};\n"
                           "                    break;\n"
                           "                end\n"
                           "            end").format(**fields)
            code = code[:match.start()] + replacement + code[match.end():]
            log('UVM_TB_AGENT', '{}: repaired zero-sentinel slot search using the YAML lowest-free-slot rule.'.format(name), 'muted')
        output.append((name, code))
    return output


def normalize_generated_routines(files):
    """Repair narrow, unambiguous routine syntax without changing check logic."""
    result = []
    for name, source in files:
        masked = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\\[^\s]+',
                        lambda m: re.sub(r'[^\n]', ' ', m.group()), source, flags=re.S)
        item_types = re.findall(r'\bclass\s+(\w+)\s+extends\s+uvm_sequence_item\b', masked)
        scopes = list(re.finditer(r'\b(function|task)\b.*?\bend(?:function|task)\b', masked, re.S))
        changed = 0
        for scope in reversed(scopes):
            code = source[scope.start():scope.end()]
            clean = masked[scope.start():scope.end()]
            header_end = clean.find(';') + 1
            if not header_end:
                continue
            edits = []
            for call in re.finditer(r'(?<![\w.:])randomize\(\s*(\w+)\s*\)(?=\s+with\s*\{)', clean):
                handle = call.group(1)
                if any(re.search(r'\b' + re.escape(t) + r'\s+' + re.escape(handle) + r'\s*;', clean)
                       for t in item_types):
                    edits.append((call.start(), call.end(), handle + '.randomize()'))
            # Only scalar/numeric-packed declarations without initializers at
            # routine scope are movable. Nested blocks and initializers stay put.
            declarations = []
            pattern = r'^[ \t]*(?:logic|bit|int|integer|string|byte|shortint|longint)\s+(?:(?:signed|unsigned)\s+)?(?:\[\s*\d+\s*:\s*\d+\s*\]\s*)?\w+(?:\s*,\s*\w+)*\s*;[^\S\n]*$'
            for decl in re.finditer(pattern, clean, re.M):
                prefix = clean[header_end:decl.start()]
                depth = 0
                for token in re.findall(r'\b(?:begin|end|fork|join|join_any|join_none)\b', prefix):
                    depth += 1 if token in ('begin', 'fork') else -1
                if decl.start() >= header_end and depth == 0:
                    declarations.append(decl)
            if declarations:
                # Keep already-leading declarations in place; move only those
                # after an executable statement, not declarations of other types.
                for decl in declarations:
                    prefix = clean[header_end:decl.start()]
                    if not re.search(r'(?:=|\breturn\b|\bif\s*\(|\bfor\s*\(|\bforeach\s*\(|\w+\s*\([^;]*\)\s*;)', prefix):
                        continue
                    # Do not mistake initialized declarations for executable
                    # assignments when considering a following legal declaration.
                    if all(re.match(r'\s*(?:\w+\s+)+\w+\s*(?:=[^;]*)?;\s*$', line)
                           for line in prefix.splitlines() if line.strip()):
                        continue
                    edits.append((decl.start(), decl.end(), ''))
                moved = [code[start:end].strip() for start, end, new in edits if new == '']
                if moved:
                    edits.append((header_end, header_end, '\n' + '\n'.join(moved)))
            for start, end, new in sorted(edits, reverse=True):
                code = code[:start] + new + code[end:]
            if edits:
                changed += 1
                source = source[:scope.start()] + code + source[scope.end():]
        if changed:
            log('UVM_TB_AGENT', '{}: repaired declaration ordering/object randomization in {} routine(s).'.format(name, changed), 'muted')
        result.append((name, source))
    return result


def compile_error_context(files, errors):
    sources = {os.path.basename(p): c.splitlines() for p, c in files}
    sections = [errors, 'Exact installed source at VCS error locations (line numbers include Agent 2 wrappers):']
    seen = set()
    for path, number in re.findall(r'"?([^\s",]+\.sv)"?,\s*(\d+)', errors):
        name, line = os.path.basename(path), int(number)
        if name not in sources or (name, line) in seen:
            continue
        seen.add((name, line))
        lines = sources[name]
        sections.append('\n'.join('{}:{}: {}'.format(name, i + 1, lines[i])
                                  for i in range(max(0, line - 8), min(len(lines), line + 7))))
    sections.append('Repair the files/lines identified above. Do not return only unrelated or unchanged files.')
    return '\n\n'.join(sections)


def normalize_generated_identifiers(files):
    """Repair known generated syntax defects without changing comparisons.

    Limit renames to the declaring subroutine; strings, comments and escaped
    identifiers are preserved. Choose an unused name in that scope. Also use
    sized casts for invalid low-bit slices of a single $urandom call.
    """
    result = []
    for name, source in files:
        masked = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\\[^\s]+',
                        lambda m: re.sub(r'[^\n]', ' ', m.group()), source, flags=re.S)
        replacements = []
        for scope in re.finditer(r'\b(function|task)\b.*?\bend(?:function|task)\b', masked, re.S):
            body = scope.group()
            if not re.search(r'\b(?:logic|bit|int|string)\s+matches\s*[;,=]', body):
                continue
            replacement = 'agent2_state_matches'
            while re.search(r'\b' + replacement + r'\b', body):
                replacement += '_local'
            for token in re.finditer(r'\bmatches\b', body):
                replacements.append((scope.start() + token.start(), scope.start() + token.end(), replacement))
        identifier_count = len(replacements)
        # A function call cannot be directly part-selected in this syntax.
        # A sized cast preserves exactly the low N bits of the one random call.
        for call in re.finditer(r'\$urandom(?:\(\s*\))?\s*\[\s*(\d+)\s*:\s*0\s*\]', masked):
            high = int(call.group(1))
            if high < 32:
                replacements.append((call.start(), call.end(), "{}'($urandom())".format(high + 1)))
        for start, end, replacement in sorted(replacements, reverse=True):
            source = source[:start] + replacement + source[end:]
        if identifier_count:
            log('UVM_TB_AGENT', '{}: renamed reserved local identifier matches ({} references); '
                'comparison expressions preserved.'.format(os.path.basename(name), identifier_count), 'muted')
        if len(replacements) > identifier_count:
            log('UVM_TB_AGENT', '{}: replaced invalid low-bit $urandom slices with equivalent sized casts.'
                .format(os.path.basename(name)), 'muted')
        result.append((name, source))
    return result


def audit_monitor_contract(files, config):
    """Reject recognizable sampling/reset mistakes, not DUT behavior failures."""
    issues = []
    sources = {os.path.basename(p): _sv_without_comments(c) for p, c in files}
    interface = sources.get('dut_if.sv', '')
    package = sources.get('dut_pkg.sv', '')
    reset_aliases = set()
    for block in re.finditer(r'\bclocking\b.*?\bendclocking\b', interface, re.S):
        default = re.search(r'\bdefault\s+input\s+(#[^;\s]+)', block.group())
        default_skew = default.group(1) if default else '#1step'
        for decl in re.finditer(r'\binput\s+(#[^;\s]+\s+)?([^;]+);', block.group()):
            skew = (decl.group(1) or default_skew).strip()
            for alias, signal in re.findall(r'\b(\w+)\s*=\s*(\w+)\b', decl.group(2)):
                if alias.endswith('_post') and skew != '#0':
                    issues.append('dut_if.sv: {} is labeled post-edge but sampled with {}. '
                                  'Use input #0 {} = {} to observe settled NBA outputs; '
                                  'keep pre-edge samples at #1step.'.format(alias, skew, alias, signal))
                if alias.endswith('_pre') and skew != '#1step':
                    issues.append('dut_if.sv: {} is labeled pre-edge but sampled with {}. '
                                  'Use input #1step {} = {} for the value consumed at the edge.'.format(
                                      alias, skew, alias, signal))
                # Recognize a direct internal combinational probe compared with
                # its same-named PRE-input prediction. Do not apply this rule to
                # registered output predictions, which correctly use PRE inputs.
                if alias.endswith('_post'):
                    stem = alias[:-5]
                    probe = re.search(r'\bassign\s+' + re.escape(signal) +
                                      r'\s*=\s*\w+(?:\.\w+)*\.' + re.escape(stem) + r'\s*;', interface)
                    predictions = re.findall(r'(?<![\w.])' + re.escape(stem) +
                                             r'\s*=\s*([^;]+);', package)
                    compared = re.search(r'\b\w+\.' + re.escape(alias) +
                                         r'\s*===?\s*' + re.escape(stem) + r'\b', package)
                    if (probe and compared and len(predictions) == 1 and
                            re.search(r'\b\w+\.\w+_pre\b', predictions[0]) and
                            not re.search(r'\b\w+_post\b', predictions[0])):
                        issues.append('dut_if.sv: {} observes an internal probe after the edge, '
                                      'but dut_pkg.sv compares it with {} computed from PRE inputs. '
                                      'For a combinational decision consumed at the edge, sample #1step '
                                      'and rename the alias and transaction references to {}_pre. '
                                      'For a POST check, derive a separate prediction from POST model state; '
                                      'do not change the expected behavior to match the DUT.'.format(alias, stem, stem))
                if config and signal == config['reset']:
                    reset_aliases.add(alias)
    for declaration in re.finditer(r'\b(?:logic|bit|int|string)\s+(matches)\s*[;,=]', package):
        issues.append('dut_pkg.sv: matches is a SystemVerilog keyword, not a legal variable name. '
                      'Rename its declaration and references to state_matches.')
    # Zero is a valid index. Using it as both a selected slot and a 'not found'
    # sentinel allows a later slot to overwrite the lowest-index choice.
    if (re.search(r"\b(first_\w+)\s*==\s*(?:\d+'[bdh]0+|0)\s*\|\|\s*\w+\s*<\s*\1\b", package) or
            re.search(r"\bfirst_\w+\s*==\s*(?:\d+'[bdh]0+|0)\s*&&\s*\w+\s*>\s*0\b", package) or
            re.search(r"\b\w+\s*\[\s*\w+\s*\]\s*&&\s*first_\w+\s*==\s*(?:\d+'[bdh]0+|0)\s*\)", package)):
        issues.append('dut_pkg.sv: lowest-index search uses zero as both a valid index and '
                      'a not-found sentinel. A later free slot can overwrite index zero. '
                      'Use a separate found flag and stop at the first match, or scan from '
                      'high to low and overwrite on each free slot, as required by YAML.')
    if config:
        for cls in re.finditer(r'\bclass\s+\w+\s+extends\s+uvm_scoreboard\b.*?\bendclass', package, re.S):
            for alias in reset_aliases:
                # Only diagnose the unambiguous bare/negated reset early-return idiom.
                # More complicated predicates require review, not guessed rewriting.
                guard = r'\bif\s*\(\s*([!~]?)\s*\w+\.' + re.escape(alias) + r'\s*\)\s*begin'
                for match in re.finditer(guard, cls.group()):
                    body = cls.group()[match.end():]
                    if not re.match(r'(?:(?!\bif\b|\bendfunction\b).){0,600}?\breturn\s*;', body, re.S):
                        continue
                    guarded_level = 0 if match.group(1) else 1
                    if guarded_level != config['active_level']:
                        issues.append('dut_pkg.sv: scoreboard reset early-return for {} has inverted '
                                      'polarity. This is the raw monitored {} pin: reset is active at {}. '
                                      'Use an explicit case equality to the YAML active level; process '
                                      'normal cycles at the inactive level and handle unknown reset '
                                      'separately. Do not invert the monitor sample.'.format(
                                          alias, config['reset'], config['active_level']))
    return issues


def audit_driver_timing(files, config):
    """Recognize the simple clocking-block driver patterns from failed runs.

    More complex helper-based drivers are not guessed at by this narrow audit.
    Startup reset still has its independently generated sampling check.
    """
    if config is None:
        return []
    sources = {os.path.basename(path): _sv_without_comments(code) for path, code in files}
    package = sources.get('dut_pkg.sv', '')
    driver = re.search(r'\bclass\s+dut_driver\b.*?\bendclass\b', package, re.S)
    if not driver:
        return []
    source = driver.group()
    gets = list(re.finditer(r'\bseq_item_port\.get_next_item\([^;]*\)\s*;', source))
    dones = list(re.finditer(r'\bseq_item_port\.item_done\s*\(', source))
    if len(gets) != 1 or len(dones) != 1 or gets[0].end() >= dones[0].start():
        return []
    prefix = source[:gets[0].start()]
    reset_writes = list(re.finditer(
        r'\b\w+\.(?:\w+\.)?' + re.escape(config['reset']) +
        r"\s*(?:<=|=(?!=))\s*(1'b[01]|[01])\s*;", prefix))
    item_body = source[gets[0].end():dones[0].start()]
    item_drives_reset = re.search(r'\.' + re.escape(config['reset']) +
                                  r'\s*(?:<=|=(?!=))', item_body)
    if (reset_writes and not item_drives_reset and
            reset_writes[-1].group(1)[-1] == str(config['active_level'])):
        return ['dut_pkg.sv: generated driver reasserts startup reset before sequence traffic '
                'without releasing it. Python already delivers startup reset. Initialize reset '
                'to the inactive YAML level; preserve explicitly scheduled later reset scenarios.']
    body = source[gets[0].end():dones[0].start()]
    writes = list(re.finditer(r'\b(\w+)\.(\w+)\.\w+\s*<=', body))
    if not writes:
        return []
    handle, driving_cb = writes[0].group(1, 2)
    clocking_events = dict(re.findall(r'\bclocking\s+(\w+)\s*@\(\s*(posedge|negedge)',
                                      sources.get('dut_if.sv', '')))
    if clocking_events.get(driving_cb) != config['drive_edge']:
        return []
    wait_drive = r'@\s*\(\s*' + re.escape(handle + '.' + driving_cb) + r'\s*\)'
    suffix = body[writes[-1].end():]
    wait_consumed = any(re.search(r'@\s*\(\s*' + re.escape(handle + '.' + cb) + r'\s*\)', suffix)
                        for cb in clocking_events)
    wait_consumed = wait_consumed or bool(re.search(
        r'@\s*\(\s*' + config['active_edge'] + r'\s+' + re.escape(handle + '.' + config['clock']) + r'\s*\)', suffix))
    issues = []
    line = package.count('\n', 0, driver.start() + dones[0].start()) + 1
    if not re.search(wait_drive, body[:writes[0].start()]):
        issues.append('dut_pkg.sv:{}: driver writes clocking outputs before synchronizing to {}. '
                      'After get_next_item, wait on @({}.{}), then drive the item, wait for '
                      'the DUT consuming edge, and only then call item_done.'.format(
                          line, driving_cb, handle, driving_cb))
    if not wait_consumed:
        issues.append('dut_pkg.sv:{}: item_done occurs immediately after clocking-output writes. '
                      'Hold the item through the next {} of {} before item_done; use the '
                      'monitor clocking event or @({} {}) after the writes.'.format(
                          line, config['active_edge'], config['clock'], config['active_edge'],
                          handle + '.' + config['clock']))
    return issues


# ============================================================
# UVM PROMPTS
# ============================================================

UVM_SYSTEM = """\
You are an expert RTL verification engineer using SystemVerilog UVM
and Synopsys VCS.

You are verifying the RTL DUT.

The YAML specification is only the source of expected behavior.
The YAML itself is not the DUT.

Generate exactly three complete SystemVerilog files using this exact format:

=== filename: tb_auto/dut_if.sv ===
<complete source>

=== filename: tb_auto/dut_tb_top.sv ===
<complete source>

=== filename: tb_auto/dut_pkg.sv ===
<complete source>

Do not use markdown code fences.
Do not provide prose before or after the files.
"""

UVM_REPAIR_SYSTEM = """You repair SystemVerilog UVM source for Synopsys VCS.
Return ONLY files that need changes, each as a complete replacement using
=== filename: tb_auto/dut_pkg.sv === (or dut_if.sv or dut_tb_top.sv).
Unchanged files are retained automatically. Do not emit patches, fragments,
markdown or explanations. Preserve YAML-derived checks, scenarios, four-state
observations, counters and evidence. The RTL DUT must never be modified.
"""

UVM_AUDIT_REPAIR_SYSTEM = """Repair only the rejected checks in this UVM candidate.
Return a JSON object: {"edits": [{"file": "dut_pkg.sv", "old": "exact source",
"new": "replacement source"}]}. Each old snippet must match exactly once in the
provided file. Use the bare filenames dut_pkg.sv, dut_if.sv, dut_tb_top.sv only.
Edits are applied in order, then the complete source audit and VCS run again.
Change declarations, sampling paths and checker calls as needed, preserving all
other checks and stimulus. Do not delete checks, emit AGENT2_UNSUPPORTED to evade
the audit, weaken expected behavior, or modify RTL. Do not replace whole files.
Do not edit Python-owned AGENT2_RUNTIME, AGENT2_EVIDENCE or AGENT2_REPORT blocks.
Use a monitored DUT observation in every repaired comparison. A count of model
operations remains model-only even if renamed actual_count. For cardinality
requirements, compare the entire observed post-state with the independently
predicted state, including payloads and unaffected entries, then attribute that
real comparison to the applicable requirement IDs. Compute the expected state
BEFORE the comparisons and commit the reference state only AFTER them.
Return JSON only, without markdown or explanatory text.
"""


# Shared by initial generation AND compile repair to avoid losing timing rules.
UVM_GENERATION_CONTRACT = """
MANDATORY SYSTEMVERILOG AND SAMPLING CONTRACT

Apply domain-specific examples below only when the current YAML/RTL has those
operations. Build the environment for the current DUT; never carry ports,
parameters, queue scenarios or timing values over from a previous design.

Follow each requirement's assigned verification method. For static assignments,
report NOT_TESTED in simulation; Python combines separate static evidence.
For simulation assignments generate an actual comparison. Classification is
based on behavior, not the YAML category: implementation constraints about
no shifting or no request loss need executable checks. Do not blanket-label
implementation constraints structural. If a check is missing, state precisely
why; never claim its condition was absent just because no checker was written.
At every check_requirement call, pass a literal string REQ ID, for example
check_requirement("REQ-016", condition, evidence). An extra integer index is
allowed, but it does not replace the literal ID. Shared comparisons may call
the helper for several IDs; shared report loops are encouraged. Before VCS,
Python checks that each simulation requirement has a call site. This audit
does not establish that the call executes: runtime counts and evidence must
also confirm execution. If a requirement cannot be checked because approved
information or observability is missing, include this explicit source comment:
// AGENT2_UNSUPPORTED|REQ-016|specific missing information or observation
It will remain an uncovered requirement, never PASS or automatically N/A.
Do not use this exception for a missing checker or missing directed stimulus.

1. In every function, task and begin/end block, put ALL local declarations
   before the first executable statement in THAT block. This includes calls
   to super, assignments, return, assertions and UVM reporting macros.
   A declaration after a statement is illegal even if its packed width is a
   valid constant. Do not fix this by replacing DEPTH with a literal width.
   Audit ALL blocks in ALL three files, not only the first compiler error.
   If a temporary is needed later, open a new nested begin/end scope with its
   declarations first. Preserve initializer evaluation timing: declare at the
   top and assign at the original use point, rather than moving initializers.
   In report_phase declare string status/evidence and other locals before
   super.report_phase(phase). In scoreboard write declare free-slot vectors,
   expected payloads and other locals before any reset checks or assignments.

2. Use explicit PRE-edge and POST-edge transaction fields. A combinational
   ready/accept signal can change immediately after state updates on an edge.
   Its PRE value determines the transfer consumed at that edge. Its POST value
   cannot be compared against a PRE-state prediction or used for that transfer.
   This also applies to hierarchical internal combinational probes (grant,
   insert index, reuse_slot, write enable): sample the decision consumed by
   sequential logic at #1step and name its transaction field *_pre. A #0 probe
   observes the recomputed decision for the updated state, not the decision that
   produced that state. Never compare that probe with the PRE decision model.
   For a positive-edge synchronous DUT, a suitable interface sampling scheme is:

       clocking monitor_cb @(posedge clk);
           input #1step rst_pre = rst_n;
           // Declare consumed inputs with input #1step aliases here.
           // Declare ready_pre with input #1step from the ready signal.
           // Declare registered state outputs with input #0 aliases here.
       endclocking

   Expand this scheme with actual DUT port names; it is not a literal interface.
   Clocking input #0 observes the post-NBA value; input #1step observes the
   pre-edge value. Wait on @(vif.monitor_cb) and read its sampled aliases.
   Do not read raw ready after a delay and label it ready_pre. Do not mix raw
   @(posedge clk) sampling and clocking-block event sampling in the monitor.
   Drive on the opposite edge, keeping inputs stable through the consuming edge.
   Preserve actual reset in each sample and reset the reference model according
   to YAML reset semantics. Adapt edge choice for negative-edge DUTs. For other
   clock/reset schemes explicitly define equivalent race-free sample boundaries.

3. Scoreboard order: compute expected PRE combinational outputs from independent
   PRE reference state and consumed inputs; compare ready_pre; compute next state
   using YAML rules and EXPECTED acceptance; compare POST registered outputs;
   then commit the reference state. Never use observed ready to make a wrong DUT
   appear correct. If checking POST ready too, compute a separate POST prediction.

4. Functional and timing checks must use the approved YAML as the source of
   expected behavior. Exercise applicable cycle latency, minimum separation,
   boundary conditions, reset and handshake rules. Never invent JEDEC limits
   or claim physical timing closure from RTL simulation. Missing timing units,
   clock conversion or device configuration must be reported as a coverage gap.
   Agent 3 separately complements UVM with directed/random test vectors; do not
   claim that Agent 3 has checked anything during this Agent 2 run.

   For every dynamically tested requirement emit a representative real comparison:
   AGENT2_REQ_EVIDENCE|REQ-001|scenario description|cycle/time|expected value|observed value
   Populate cycle/time and values from the executed comparison, using $sformatf.
   Keep at least the first actual comparison and the first failure per ID.
   Do not print made-up examples or constants as runtime evidence. No field may
   contain a pipe or newline. Also describe the scenario and comparison in the
   final AGENT2_REQ note; counts alone are not an explanation of what was tested.
   Preserve this evidence format during compile repairs.
   Emit evidence INSIDE the shared check_requirement helper for every ID on
   its first comparison and first failure. Pass scenario, expected and observed
   strings as extra helper arguments if needed. Do not scatter evidence printing
   through selected scenarios: that loses evidence for other checked IDs.

5. Every mismatch must print cycle/time, requirement ID(s), actual and expected
   values, consumed inputs, reset and relevant PRE reference state. Label PRE
   and POST samples explicitly. Maintain four-state observations. Preserve all
   requirement check/failure counters and do not weaken checks during repair.
   Describe the actual failed comparison, not an inferred cause: a full-state
   mismatch does not by itself prove multiple enqueues, shifting, or data loss.
   For operation-count requirements compare DUT-observed changes against the
   YAML-permitted changes, including same-slot overwrite and simultaneous
   enqueue/dequeue. Label a shared full-state check as a state mismatch.
   For lowest-index selection, zero is a valid slot, never a not-found sentinel.
   Scan upward and break at the first free slot, or use a separate found flag.

6. Known invalid monitor pattern: @(posedge clk); #1; then read all raw ports
   including ready into one transaction and compare ready with PRE reference
   state. Filling the last free entry makes PRE ready=1 and POST ready=0;
   that is normal behavior, not a mismatch. Capture readiness before updates
   and registered outputs after updates as described above. Do not suppress
   the ready comparison at full/empty transitions to hide this timing error.

7. Every requirement counter must be tied to a real comparison and its failure
   path. Use a helper check_requirement(id, condition, evidence) that
   increments checks and, unless condition === 1'b1, increments failures and
   reports a UVM error. Call it only when the scenario's precondition holds.
   Incrementing checks merely because reuse_slot is true, an enqueue occurred,
   or write() ran is NOT a requirement check. A payload mismatch must fail all
   applicable payload/persistence/insertion requirements, not just fail_cnt.
   If several IDs share one comparison, propagate the result to every such ID.
   For same-slot reuse compare actual POST validity AND actual stored payload
   against the expected new request. For persistence/no-shifting compare
   unaffected valid entries with independent PRE reference payloads. Source
   facts such as absence of a struct field remain NOT_TESTED; checking a
   validity output does not prove the absence of a field in an RTL typedef.
   Counters printed by the TB are evidence claims, not independent proof;
   ensure each positive count has an executable DUT comparison behind it.
   Declare the comparison parameter as logic condition so an X cannot silently
   become a success. Observed transaction fields must also be logic, not bit.
   Additional parameters such as an integer requirement index are allowed;
   the source audit reads the declaration to locate the condition argument.

8. Persistence means persistence of UNMODIFIED entries, not of every entry
   that was valid before the edge. Derive per-entry write and removal masks
   from YAML rules, PRE reference state and expected acceptance, independently
   of observed DUT results. For each entry:
     - accepted write/replacement: compare actual POST payload and validity
       with the expected NEW payload and validity;
     - removal without replacement: compare actual POST validity with invalid;
     - previously valid, neither written nor removed: compare actual POST
       validity AND payload with independent PRE reference state;
     - other entries: compare any state the specification defines.
   These cases are mutually exclusive. Do not require an entry to retain its
   old payload when the specification permits replacement on that edge.
   Never skip replacement verification: exclude it only from OLD-data
   persistence, and check NEW-data replacement in its own branch. Include
   actual checks of newly inserted data when no simultaneous removal occurs.
   Check the whole expected validity vector to detect unintended drops or
   extra writes. Commit the reference state only after all comparisons.
   For other stateful DUTs use the equivalent YAML-defined update/hold cases.

9. Before random stimulus, use directed cases for update, hold and concurrent
   operations allowed by YAML. For indexed storage include full replacement,
   partial occupancy with removal at another index, and untouched neighbours.
   Keep all existing cases during repair. Drive idle after each consumed item
   if no next item is available; do not repeat the final transaction while the
   test drains. Release reset away from the DUT's active edge.

10. Pass the actual DUT comparison expression (or its computed result) to
    check_requirement. The source precheck follows simple local aliases and
    rejects model-only or unconditional constant checks. Opposite constant
    outcomes in both arms of a two-state DUT comparison are supported, but a
    single call with the comparison is clearer. Counters of observed transitions
    are allowed; counters of driven inputs or model operations are not checks.
    A literal counter index is not a condition and is allowed.
    Never hardcode PASS markers. All three report marker formats above
    must be implemented, including real expected/observed evidence.
    Keep condition computation traceable to monitored DUT outputs. This check
    is a limited source audit, not a replacement for a correct reference model.

11. Output size: use ONE report loop over requirement IDs with shared helpers
    for status, counts and saved runtime evidence. Do not repeat a full report
    if/else block 28 times. Use compact per-ID description tables. Preserve all
    real comparisons and directed cases; reduce repetition, not verification.
    Emit the small dut_if.sv and dut_tb_top.sv files before dut_pkg.sv. Each file
    must be complete through endinterface, endmodule or endpackage respectively.

12. Baseline coverage is the responsibility of this UVM run. For EACH simulation
    requirement select a directed scenario, its real precondition, the expected
    state/output, and the monitored comparison. Invoke every implemented checker
    before random traffic. Do not defer missing baseline checks to Agent 3.
    Invariants about maximum operations per cycle must check observed effects,
    not count asserted input enables or reference-model operations. For indexed
    storage, compare the COMPLETE actual POST valid vector with the independently
    predicted vector, and compare payloads in written and unaffected valid slots.
    This detects extra insertions/removals even if simultaneous operations leave
    occupancy unchanged. A scalar occupancy delta alone is insufficient.
    For a maximum-one-write rule, check non-target slots as well as the target;
    for a maximum-one-removal rule, verify every non-target valid slot survives.
    For no request loss, check every PRE-valid entry except a YAML-authorized
    removal or replacement, including idle, rejected enqueue, ordinary enqueue,
    dequeue and simultaneous-operation cycles. Compare both validity and payload.
    Do not guard a no-loss check solely with rejected enqueue. Use distinct
    payloads in directed cases so writes to unintended slots are observable.
    Include idle with outstanding data, fill, blocked enqueue when full, dequeue,
    full same-slot replacement, and partial-occupancy concurrent operations if
    the YAML permits them. Keep legal replacement separate from old-data hold.
    Never substitute a predictor self-comparison, a tautology such as
    (|valid || !(&valid)), or a constant counter for the actual output comparison.
    A post-edge validity sample cannot prove absence of an intermediate glitch;
    use an appropriate event-level checker, or explicitly report that limitation.

13. Keep dut_env, dut_agent, dut_driver, dut_monitor and dut_scoreboard reusable.
    Stimulus belongs in dut_sequence; dut_test creates it through the UVM factory.
    Keep assertions/checkers active regardless of which sequence drives traffic.
    Do not embed stimulus generation in the scoreboard. Retain the virtual
    interface configuration and transaction analysis connections so another test
    can use the same checkers. Python archives the complete environment and run
    evidence; no Agent 3 results are inferred from this baseline run.

14. For counters/timers, compare sampled DUT counter state with independently
    predicted state, or verify the specified externally visible timing interval.
    A reference counter compared with its own reload/decrement formula tests
    only the predictor. If YAML explicitly requires internal state behavior,
    expose read-only hierarchical probes through dut_tb_top/dut_if and sample
    them in the monitor; do not modify RTL or fabricate observations. Use the
    same phase for scenario guards, expected values and actual values. An ACT
    accepted when PRE counter=0 may legitimately produce POST block=1. Test
    reload, idle decrement, expiry, hold-zero and reset independently.
    To check asynchronous reset, assert reset between clock edges and observe
    the DUT after its reset NBA updates, before the next active clock edge.
    A check made only on clock edges does not establish asynchronous response.

15. Use indexed part selects for variable positions, e.g. data[i*WIDTH +: WIDTH].
    In data[high:low], both bounds must be constant. Derive widths, array shapes,
    casts, clock periods and parameters from the current RTL/YAML, not a previous
    design. Preserve exact RTL types when connecting packed/unpacked ports.

16. For a declared single clock/reset domain, Python installs a startup-reset
    task in dut_if and a wrapper around dut_driver.run_phase. This task asserts
    the YAML reset polarity, confirms assertion at two actual consuming edges,
    then releases at the opposite edge before invoking the generated driver.
    Do not generate AGENT2_RUNTIME blocks or call agent2_startup_reset yourself;
    Python owns them and reinstalls them during repair. Keep virtual dut_if in
    dut_driver and the standard run_phase(uvm_phase phase) method. The driver is
    the only procedural reset owner: no reset-driving initial block in the top.
    Initialize reset to its INACTIVE level in the generated driver; startup reset
    has already completed. Do not leave reset asserted throughout traffic.
    Sequences may still exercise additional reset scenarios. The monitor must
    observe real reset and reset-state outputs; reset delivery is not proof that
    the DUT implements reset correctly. Never replace reset testing with the
    startup READY marker. Do not treat unknown reset as a successful reset.
    A monitored reset alias is the RAW pin level, not a reset-request flag.
    For active-low reset, the scoreboard reset branch is (tr.rst_pre === 1'b0);
    normal operation is (tr.rst_pre === 1'b1). Reverse for active-high reset.
    If a sequence uses a logical reset-request flag, give it a DIFFERENT field
    name from the raw monitored reset and convert polarity only in the driver.
    Every clocking input named *_post MUST explicitly use input #0 (or a #0
    default); an unqualified input defaults to #1step and is PRE-edge.

    For a clocking-block driver: get_next_item(req), wait for the opposite-edge
    driver clocking event, drive the item, wait for the consuming clock/monitor
    event, THEN item_done(). Do not acknowledge an item on its drive edge.
    Drive idle after consumption when no next item is available. At time zero,
    initialize the generated clock to the inactive level before its first edge.

17. A scenario predicate is not a requirement result. For a requirement defining
    an internal control signal, sample that RTL signal through a read-only probe
    and compare it against the YAML expression, including true AND false cases.
    For output behavior, check the resulting observed output/state. Never use
    cond=1 or an observed string containing a constant as a substitute. On repair,
    follow the exact REQ ID and rejected call in the diagnostic, not a guessed
    location in an LLM response containing a thinking preamble.

18. Prefer this reporting helper signature:
    function void check_requirement(string id, logic condition, string scenario,
                                    string expected_str, string observed_str);
    Keep its real counters and UVM error handling. Python adds a wrapper that
    records the first comparison and first failure using these arguments and
    $time. Do not generate AGENT2_EVIDENCE blocks yourself. These wrapper blocks
    are owned by Python and reinstated during repairs. All expected/observed
    strings must describe the actual compared values; do not insert placeholders.
    Python also owns AGENT2_REPORT blocks, recording real per-ID counts and
    failures in report_phase. Do not generate or edit those blocks yourself.

19. For maximum-operation requirements, reuse a complete state comparison when
    it establishes the rule. Example for an indexed store (adapt names/types):
      // Compute exp_valid_post and exp_data_post from YAML and PRE model first.
      matches = (tr.req_valid_post === exp_valid_post);
      for (int k = 0; k < DEPTH; k++) begin
        if (exp_valid_post[k])
          matches &= (tr.req_array_post[k*WIDTH +: WIDTH] === exp_data_post[k]);
      end
      check_requirement("<maximum-write REQ ID>", matches, scenario,
                        expected_state_string, observed_state_string);
    Attribute the same complete comparison to maximum-removal when applicable.
    Call it every non-reset cycle, including idle, full, and simultaneous cases.
    A count of write_mask/remove_mask derived from INPUTS and MODEL is only a
    prediction, never an observed result. Do not pass that count alone as the
    condition. No constant true calls. No tautological bounds on model counters.
    Do not invent internal write-enable signals if the RTL lacks them.

20. For state_machine specifications, use the supplied REQ IDs for reset, state
    legality, every transition, hold behavior and outputs. Do not invent FSM-*
    IDs. Observe actual DUT state using read-only interface/top probes when
    outputs cannot distinguish states. A ref_state-only comparison is not a
    state check. Use RTL declarations to connect probes and map state labels;
    derive allowed transitions and guard thresholds from YAML only. Exercise
    each guard both true and false, plus reset from active states and output
    hold/ack behavior. Preserve >= versus >, threshold values and sampling edge.
    A transition guard check can compare sampled pre-state/guard operands with
    the YAML-defined next state; it does not by itself prove counter evolution
    or elapsed JEDEC timing. Do not copy missing counter reset/increment rules
    from RTL into the reference model and claim they were specified by YAML.
    If output prose lacks a complete timing definition, retain the requirement
    as a gap with a specific explanation while checking the defined behavior.

"""

def prompt_generate(
    rtl_code,
    dut_spec,
    rtl_files,
    yaml_spec,
    requirements,
    generation_attempt
):
    module_name = dut_spec[
        "module_name"
    ]

    clk_name = detect_single_clock_name(
        dut_spec
    )

    reset_info = detect_single_reset(
        dut_spec
    )

    return UVM_GENERATION_CONTRACT + packed_payload_prompt(yaml_spec) + """VERIFY THE ACTUAL RTL DUT BELOW WITH UVM + VCS.

================ RTL DUT ================

{rtl_code}

============== END RTL DUT ==============


AGENT 1 DUT METADATA:

{dut_spec}


RTL FILES VCS WILL COMPILE:

{rtl_files}


PORT SUMMARY:

{port_summary}


APPROVED YAML EXPECTED-BEHAVIOR SPECIFICATION:

{yaml_spec}


TRACEABLE REQUIREMENTS EXTRACTED FROM YAML:

{requirements}


GENERATION ATTEMPT:

{generation_attempt}


============================================================
MAIN OBJECTIVE
============================================================

Create a UVM testbench that drives and observes RTL module:

    {module_name}

The scoreboard/reference model must derive expected behavior
from the YAML and compare it against the ACTUAL behavior of
the RTL DUT.


============================================================
OUTPUT EXACTLY THESE THREE FILES
============================================================

1. tb_auto/dut_if.sv

2. tb_auto/dut_tb_top.sv

3. tb_auto/dut_pkg.sv


============================================================
dut_pkg.sv
============================================================

Must contain:

    package dut_pkg;

    import uvm_pkg::*;

    `include "uvm_macros.svh"

Classes:

    dut_seq_item
    dut_sequence
    dut_driver
    dut_monitor
    dut_sequencer
    dut_agent
    dut_scoreboard
    dut_env
    dut_test

End with:

    endpackage


============================================================
dut_if.sv
============================================================

- Plain SystemVerilog interface
- Preserve exact RTL DUT signal names
- Use legal widths/types compatible with the actual DUT


============================================================
dut_tb_top.sv
============================================================

- Plain SystemVerilog module

Must import:

    import uvm_pkg::*;
    import dut_pkg::*;

- Instantiate interface
- Instantiate RTL DUT {module_name}
- Connect every DUT port
- Generate clock if appropriate
- Configure virtual interface
- Call:

    run_test("dut_test")


============================================================
CLOCK
============================================================

Detected clock:

    {clock_name}


============================================================
RESET
============================================================

Agent 1 reset metadata:

    {reset_info}


============================================================
RTL VERIFICATION REQUIREMENTS
============================================================

- The RTL is the DUT.
- Actually simulate it.
- Drive controllable input ports.
- Observe actual RTL outputs.
- Build expected behavior independently from DUT outputs.
- Use YAML rules, invariants and correctness criteria as the
  expected specification.
- For stateful DUTs maintain an independent reference state.
- Carry actual reset values in monitored transactions; apply reset to the
  reference model on EVERY reset-active sample, respecting YAML polarity and
  synchronous/asynchronous semantics. Never skip a fixed number of samples.
- Hold stimulus idle until actual reset release. Deassert reset away from the
  active clock edge; do not race a synchronous DUT with blocking reset writes.
- For a rising-edge DUT, drive transactions on falling edges and keep them
  stable through the next rising edge. Wait until that transaction is consumed
  before item_done; drive idle when the sequence finishes.
- Sample consumed inputs and combinational handshake outputs BEFORE the active
  edge updates the DUT. Sample resulting registered outputs AFTER NBA updates
  and combinational settling, using clocking blocks or explicit VCS-compatible
  sampling skew. Keep these samples in a single transaction.
- Compare pre-edge ready against PRE-state reference readiness, then compute
  next reference state from the consumed inputs, then compare POST-state DUT
  outputs. Do not compare next-state predictions to pre-edge registered outputs.
- Preserve four-state logic in observed fields so X/Z cannot silently become 0.
- Ensure directed scenarios execute after reset; count scenarios only when their
  actual preconditions and DUT comparisons occur, not based on sequence labels.
- Exercise:
    * reset
    * normal operation
    * edge cases
    * simultaneous operations
    * empty/full states when applicable
    * boundary conditions
    * YAML-specific conditions
- Random stimulus may supplement directed testing.
- YAML-directed cases must be prioritized.


============================================================
REQUIREMENT TRACEABILITY
============================================================

Each supplied REQ ID must receive exactly one final marker.
Maintain per-requirement integer checks and failures counters initialized to zero.
Increment checks ONLY when the requirement's triggering condition occurs and
an actual DUT comparison executes. Increment failures on every mismatch.
Never assign positive counts as constants or count stimulus as a DUT check.
In report_phase emit one additional counter line for EVERY requirement:
    AGENT2_REQ_COUNTS|REQ-001|<checks>|<failures>
Use $sformatf with runtime counters. Derive the final status from these counters:
failures > 0 => FAIL; checks > 0 and failures == 0 => PASS; otherwise NOT_TESTED.
NEVER emit unconditional literal PASS markers. For requirements assigned to
static verification, emit NOT_TESTED with zero simulation counts. Python shows
their separately verified structural status in the consolidated report.

Exact format:

    AGENT2_REQ|REQ-001|PASS|short evidence

or:

    AGENT2_REQ|REQ-001|FAIL|short reason

or:

    AGENT2_REQ|REQ-001|NOT_TESTED|short reason


PASS means:

Executable simulation logic exercised the relevant condition
and checked the RTL against the expected behavior.


FAIL means:

The relevant condition was exercised and the RTL DUT violated
the expected behavior.

A failed executable requirement should also issue:

    `uvm_error


NOT_TESTED means:

Agent 2 cannot dynamically prove the requirement or the
condition was not actually exercised.


DO NOT mark PASS because:

- RTL compiled
- a signal exists
- YAML mentioned the requirement
- UVM_ERROR happens to be zero


Structural source-code constraints may be NOT_TESTED if UVM
simulation cannot prove them.

Evidence text must not contain the | character.


============================================================
SCOREBOARD
============================================================

Maintain:

    int pass_cnt;
    int fail_cnt;

Each successful executable comparison increments pass_cnt.

Each mismatch increments fail_cnt and reports `uvm_error.

In report_phase print:

    `uvm_info(
        "SB",
        "=== SCOREBOARD REPORT ===",
        UVM_NONE
    )

and:

    `uvm_info(
        "SB",
        $sformatf(
            "PASS=%0d FAIL=%0d",
            pass_cnt,
            fail_cnt
        ),
        UVM_NONE
    )

Also emit final AGENT2_REQ markers.


============================================================
IMPORTANT
============================================================

DO NOT modify the RTL DUT.

DO NOT test the YAML itself.

DO NOT invent behavior unsupported by YAML/RTL.

DO NOT include `timescale in generated files.

Output ONLY the three requested file blocks.
""".format(
        rtl_code=
            rtl_code,

        dut_spec=
            format_dut_spec_for_prompt(
                dut_spec
            ),

        rtl_files=
            json.dumps(
                rtl_files,
                indent=2
            ),

        port_summary=
            build_port_summary(
                dut_spec
            ),

        yaml_spec=
            safe_yaml_dump(
                yaml_spec
            ),

        requirements=
            format_requirements_for_prompt(
                requirements
            ),

        generation_attempt=
            generation_attempt,

        module_name=
            module_name,

        clock_name=
            clk_name,

        reset_info=
            json.dumps(
                reset_info,
                indent=2
            ),
    )


def prompt_compile_fix(
    rtl_code,
    dut_spec,
    rtl_files,
    yaml_spec,
    requirements,
    vcs_errors,
    current_tb,
    failure_stage='compile'
):
    return UVM_GENERATION_CONTRACT + packed_payload_prompt(yaml_spec) + """{failure_description}

Fix ONLY the generated testbench.

DO NOT MODIFY THE RTL DUT.

DO NOT weaken verification merely to get a compile pass.


================ RTL DUT ================

{rtl_code}

============== END RTL DUT ==============


DUT SPEC:

{dut_spec}


RTL FILE LIST:

{rtl_files}


APPROVED YAML EXPECTED BEHAVIOR:

{yaml_spec}


TRACEABLE REQUIREMENTS:

{requirements}


{diagnostic_heading}:

{vcs_errors}


CURRENT GENERATED TESTBENCH:

{current_tb}


REQUIRED OUTPUT:

{output_contract}


RULES:

- Preserve exact DUT module and port names
- Preserve YAML-driven directed stimulus
- Preserve independent expected behavior
- Preserve requirement traceability
- Each requirement must still end with:
    PASS
    FAIL
    NOT_TESTED
- Do not turn a real DUT failure into NOT_TESTED
- Keep scoreboard PASS/FAIL counters
- Preserve runtime per-requirement checks/failures counters and emit
  AGENT2_REQ_COUNTS|REQ-001|<checks>|<failures> for each ID
- Derive requirement status from counters; never emit unconditional PASS
- Preserve actual reset tracking and pre-edge input/post-NBA output alignment
- Preserve four-state observed signals and reset-aware stimulus
- Fix package/import/interface/type/order/VCS syntax issues
- Do not include `timescale directives
- Follow the REQUIRED OUTPUT format above; never remove checks to silence diagnostics
""".format(
        output_contract=('Return the JSON edits object required by the system message. '
                         'Use exact old/new snippets in the current candidate, updating every '
                         'affected declaration, observation path and call. Do not regenerate files.'
                         if failure_stage == 'audit' else
                         'Return ONLY changed files as complete replacements under their existing '
                         '=== filename: tb_auto/<name>.sv === headings. Unchanged candidate files are '
                         'retained automatically. Update every file affected by interface/type changes.'),
        failure_description=('The GENERATED UVM TESTBENCH failed to compile in Synopsys VCS.'
                             if failure_stage == 'compile' else
                             'The generated testbench failed the pre-simulation source audit. '
                             'The DUT has not been simulated for this candidate. Fix the '
                             'listed checker/report defects; these are not RTL violations.'),
        diagnostic_heading=('VCS COMPILE ERRORS' if failure_stage == 'compile' else
                            'PRE-SIMULATION GENERATION AUDIT FAILED'),
        rtl_code=
            rtl_code,

        dut_spec=
            format_dut_spec_for_prompt(
                dut_spec
            ),

        rtl_files=
            json.dumps(
                rtl_files,
                indent=2
            ),

        yaml_spec=
            safe_yaml_dump(
                yaml_spec
            ),

        requirements=
            format_requirements_for_prompt(
                requirements
            ),

        vcs_errors=
            vcs_errors,

        current_tb=
            current_tb,
    )


# ============================================================
# VCS COMPILE / RUN
# ============================================================

def extract_vcs_errors(
    compile_out
):
    lines = (
        compile_out
        or ""
    ).splitlines()

    result = []

    capturing = False

    for line in lines:

        if re.match(
            r"^\s*Error-\[",
            line
        ):

            capturing = True

        if capturing:

            result.append(
                line
            )

            if len(
                result
            ) >= 120:

                break

    if result:

        return "\n".join(
            result
        )

    fallback = [
        line
        for line in lines
        if "error" in line.lower()
    ]

    return "\n".join(
        fallback[:120]
    )


def vcs_compile(
    rtl_files,
    iter_idx
):
    ensure_build_dirs()

    compile_log = os.path.join(
        LOG_DIR,
        "compile_iter_{}.log".format(
            iter_idx
        )
    )

    filelist = os.path.join(
        BUILD_DIR,
        "tb_filelist.f"
    )

    simv_path = os.path.join(
        BUILD_DIR,
        SIMV_NAME
    )

    if not which(
        "vcs"
    ):

        message = (
            "ERROR: VCS executable not found in PATH."
        )

        with open(
            compile_log,
            "w"
        ) as f:

            f.write(
                message
                +
                "\n"
            )

        return (
            False,
            compile_log,
            message
        )

    tb_files = ordered_tb_files(
        TB_ROOT
    )

    with open(
        filelist,
        "w"
    ) as f:

        for path in tb_files:

            f.write(
                path
                +
                "\n"
            )

    inc_flags = [
        "+incdir+{}".format(
            directory
        )
        for directory
        in gather_incdirs(
            TB_ROOT
        )
    ]

    debug_flags = []

    if ENABLE_DEBUG:

        debug_flags = [
            "-debug_access+r+w-memcbk",
            "-debug_region+cell",
        ]

    command = (
        [
            "vcs",
            "-sverilog",
            "-full64",
            "-timescale=1ns/1ps",
        ]

        +

        VCS_UVM_FLAGS

        +

        debug_flags

        +

        inc_flags

        +

        rtl_files

        +

        [
            "-f",
            filelist,
            "-o",
            simv_path,
        ]
    )

    log(
        "VCS",
        (
            "Compiling {} RTL file(s) "
            "+ generated UVM TB..."
        ).format(
            len(
                rtl_files
            )
        ),
        "info",
    )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )

    output, _ = process.communicate()

    with open(
        compile_log,
        "w"
    ) as f:

        f.write(
            output
        )

    return (
        process.returncode == 0,
        compile_log,
        output
    )


def vcs_run(
    iter_idx
):
    ensure_build_dirs()

    sim_log = os.path.join(
        LOG_DIR,
        "sim_iter_{}.log".format(
            iter_idx
        )
    )

    simv_path = os.path.join(
        BUILD_DIR,
        SIMV_NAME
    )

    if not os.path.exists(
        simv_path
    ):

        return (
            False,
            sim_log,
            "Simulation executable does not exist."
        )

    process = subprocess.Popen(
        [
            simv_path
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )

    output, _ = process.communicate()

    with open(
        sim_log,
        "w"
    ) as f:

        f.write(
            output
        )

    return (
        process.returncode == 0,
        sim_log,
        output
    )


# ============================================================
# SCOREBOARD REPORT
# ============================================================

def count_uvm_issues(sim_text):
    counts = []
    for severity in ("UVM_ERROR", "UVM_FATAL"):
        lines = (sim_text or "").splitlines()
        emitted = sum(1 for line in lines if re.match(
            r"^" + severity + r"\s+(?!:)\S", line))
        summaries = re.findall(
            r"^\s*" + severity + r"\s*:\s*(\d+)",
            sim_text or "", re.MULTILINE)
        counts.append(max([emitted] + [int(n) for n in summaries]))
    return tuple(counts)


def extract_scoreboard_counts(
    sim_text
):
    matches = re.findall(
        r"\bPASS\s*=\s*(\d+)"
        r"\s+FAIL\s*=\s*(\d+)\b",
        sim_text
        or "",
    )

    if not matches:

        return (
            None,
            None
        )

    passed, failed = matches[
        -1
    ]

    return (
        int(
            passed
        ),

        int(
            failed
        )
    )


def extract_scoreboard_block(
    sim_text
):
    positions = [
        match.start()

        for match in re.finditer(
            r"SCOREBOARD\s+REPORT",
            sim_text
            or ""
        )
    ]

    if not positions:

        return None

    tail = (
        sim_text
        or ""
    )[
        positions[-1]:
    ]

    return "\n".join(
        tail.splitlines()[:35]
    )


def extract_simulation_errors(sim_text, limit=12):
    """Show real diagnostics before the final scoreboard summary hides them."""
    lines = (sim_text or "").splitlines()
    snippets = []
    for index, line in enumerate(lines):
        if not re.match(r"^\s*UVM_(?:ERROR|FATAL)\s+(?!:)\S", line):
            continue
        snippet = [line]
        for following in lines[index + 1:index + 4]:
            if not following.strip() or re.match(r"^\s*UVM_", following):
                break
            snippet.append(following)
        snippets.append("\n".join(snippet))
        if len(snippets) >= limit:
            break
    return snippets


def print_scoreboard(
    sim_text,
    iter_idx
):
    (
        n_err,
        n_fatal
    ) = count_uvm_issues(
        sim_text
    )

    (
        sb_pass,
        sb_fail
    ) = extract_scoreboard_counts(
        sim_text
    )

    scoreboard_exists = (
        sb_pass is not None
        and
        sb_fail is not None
    )

    passed = (
        n_err == 0
        and
        n_fatal == 0
        and
        scoreboard_exists
        and
        sb_fail == 0
    )

    print()

    print(
        c(
            "cyan",
            "═" * 58
        )
    )

    print(
        c(
            "bold",
            (
                "  RTL SCOREBOARD REPORT "
                "- Iteration {}"
            ).format(
                iter_idx + 1
            )
        )
    )

    print(
        c(
            "cyan",
            "═" * 58
        )
    )

    print(
        "  UVM Result : {}".format(
            (
                c(
                    "green",
                    "PASS"
                )

                if passed

                else

                c(
                    "red",
                    "FAIL"
                )
            )
        )
    )

    print(
        "  UVM_ERROR  : {}".format(
            n_err
        )
    )

    print(
        "  UVM_FATAL  : {}".format(
            n_fatal
        )
    )

    if scoreboard_exists:

        print(
            "  RTL Checks : PASS={} FAIL={}".format(
                sb_pass,
                sb_fail
            )
        )

    else:

        print(
            (
                "  RTL Checks : "
                "no scoreboard PASS/FAIL count found"
            )
        )

    diagnostics = extract_simulation_errors(sim_text)
    if diagnostics:
        print("\n  -- First simulation errors (DUT or testbench; investigate) --")
        for diagnostic in diagnostics:
            print(textwrap.indent(diagnostic, "  "))

    block = extract_scoreboard_block(
        sim_text
    )

    if block:

        print(
            c(
                "grey",
                "\n  -- RTL Verification Output --"
            )
        )

        for line in block.splitlines():

            print(
                "  {}".format(
                    line
                )
            )

    print(
        c(
            "cyan",
            "═" * 58
        )
    )

    return passed


# ============================================================
# REQUIREMENT TRACEABILITY
# ============================================================

REQ_RESULT_RE = re.compile(
    r"AGENT2_REQ\|"
    r"(REQ-\d{3,})\|"
    r"(PASS|FAIL|NOT_TESTED)\|"
    r"([^\r\n]*)"
)


def parse_requirement_results(sim_text):
    results = {}
    for match in REQ_RESULT_RE.finditer(sim_text or ""):
        req_id, status, note = match.groups()
        previous = results.get(req_id)
        # Never let a later PASS erase a failure.
        if previous and previous["status"] == "FAIL":
            continue
        results[req_id] = {"status": status, "note": note.strip()[:400]}
    return results


def parse_requirement_evidence(sim_text):
    """Read comparisons, including legacy records with a real UVM timestamp.

    A legacy record may omit its time field only if the SAME UVM log line
    supplies the simulation timestamp. Never manufacture a missing observation.
    """
    evidence = {}
    for line in (sim_text or '').splitlines():
        prefix, separator, payload = line.partition('AGENT2_REQ_EVIDENCE|')
        if not separator:
            continue
        fields = [part.strip() for part in payload.split('|')]
        if not fields or not re.fullmatch(r'REQ-\d{3,}', fields[0]):
            continue
        req_id, fields = fields[0], fields[1:]
        if fields and fields[0] in ('PASS', 'FAIL'):
            fields = fields[1:]
        if len(fields) == 4:
            scenario, cycle, expected, observed = fields
        elif (len(fields) == 3 and fields[1].startswith('expected=') and
              fields[2].startswith('observed=')):
            timestamp = re.search(r'\bUVM_(?:INFO|WARNING|ERROR|FATAL)\b.*?@\s*(\d+(?:\.\d+)?)\s*:', prefix)
            if not timestamp:
                continue
            scenario, expected, observed = fields
            cycle = 'time=' + timestamp.group(1)
        else:
            continue
        if not all((scenario, cycle, expected, observed)):
            continue
        if not re.search(r'\d', cycle):
            continue
        values = [re.sub(r'^(?:expected|observed)\s*=\s*', '', value).strip()
                  for value in (expected, observed)]
        if any(not value or re.fullmatch(
                r'(?:see (?:above|evidence|log)|(?:exp|obs|expected|observed)(?: in evidence)?|'
                r'n/?a|unknown|<.*?>)', value, re.I) for value in values):
            continue
        evidence.setdefault(req_id, []).append({
            "scenario": scenario, "cycle_or_time": cycle,
            "expected": expected, "observed": observed})
    return evidence


def build_requirement_report(requirements, sim_text, simulation_ok=True, reset_config=None):
    """Combine structural evidence with substantiated runtime comparisons.

    Retain the raw simulation result separately: static proof must neither look
    like a missing UVM test nor be mistaken for an executed simulation check.
    """
    observed = parse_requirement_results(sim_text)
    examples = parse_requirement_evidence(sim_text)
    counters = {}
    for match in re.finditer(
            r"AGENT2_REQ_COUNTS\|(REQ-\d{3,})\|(\d+)\|(\d+)",
            sim_text or ""):
        req_id, checks, failures = match.groups()
        previous = counters.get(req_id, (0, 0))
        counters[req_id] = (max(previous[0], int(checks)),
                            max(previous[1], int(failures)))

    errors, fatals = count_uvm_issues(sim_text)
    sb_pass, sb_fail = extract_scoreboard_counts(sim_text)
    reset_evidence = {'required': reset_config is not None, 'status': 'NOT_REQUIRED'}
    reset_failed = False
    if reset_config is not None:
        samples = [int(m.group(1)) for m in re.finditer(
            r'AGENT2_RESET\|' + re.escape(reset_config['reset']) + r'\|READY\|(\d+)\|', sim_text or '')]
        edges = max(samples or [0])
        reset_failed = edges < 2 or bool(re.search(r'UVM_FATAL[^\n]*\[AGENT2_TB_RESET\]', sim_text or ''))
        reset_evidence.update({'status': 'FAIL' if reset_failed else 'APPLIED',
                              'signal': reset_config['reset'], 'sampled_edges': edges,
                              'evidence': ('Startup reset was not confirmed at two DUT consuming edges.'
                                           if reset_failed else 'Reset application confirmed at {} DUT consuming edges; '
                                           'RTL reset behavior is checked separately by UVM.'.format(edges))})
    verification_failed = (not simulation_ok or errors > 0 or fatals > 0
                           or sb_pass is None or sb_fail != 0
                           or (sb_pass or 0) == 0 or reset_failed)
    rows = []
    for req in requirements:
        result = observed.get(req["id"], {})
        status = result.get("status", "NOT_TESTED")
        note = result.get("note", "No final requirement marker was emitted.")
        checks, failures = counters.get(req["id"], (0, 0))
        evidence_gap = ''
        if failures > 0:
            status = "FAIL"
            note = ("Executable requirement checks reported failures. {} of {} comparisons failed; "
                    "see scenario, expected and observed evidence. A failed comparison alone "
                    "does not establish whether the RTL or checker is incorrect.").format(failures, checks)
        elif status == "PASS" and checks == 0:
            status = "NOT_TESTED"
            note = "Unsubstantiated PASS: no positive executable check count. " + note
        elif status == "PASS" and not examples.get(req['id']):
            status = "NOT_TESTED"
            evidence_gap = "No scenario/expected/observed comparison marker emitted."
            note = "Unsubstantiated PASS: " + evidence_gap + ' ' + note
        simulation = {"status": status, "evidence": note, "checks": checks,
                      "failures": failures,
                      "comparison_evidence": examples.get(req['id'], [])}
        method = req.get('verification_method', 'simulation')
        static = req.get('static_result', {
            'status': 'NOT_TESTED', 'evidence': 'No structural verification evidence.',
            'method': 'static'})
        if method == 'static':
            if status != 'FAIL':
                status = static['status']
                note = static['evidence']
            evidence_gap = ''
        if static['status'] == 'FAIL':
            status = 'FAIL'
            note = static['evidence']
        if req.get('requires_static_support') and status != 'FAIL':
            if static['status'] != 'PASS':
                status = 'NOT_TESTED'
                note = static['evidence']
                evidence_gap = note
            elif status == 'PASS':
                note += ' Supporting structural evidence: ' + static['evidence']
        gap_kind = ('structural_check' if method == 'static' else
                    ('comparison_evidence' if evidence_gap else 'simulation_check'))
        rows.append({"id": req["id"], "category": req["category"],
                     "source": req["source"], "requirement": req["text"],
                     "status": status, "evidence": note,
                     "checks": checks, "failures": failures,
                     "verification_method": method,
                     "requires_static_support": req.get('requires_static_support', False),
                     "verification_label": ('structural verification' if method == 'static'
                                            else 'UVM simulation'),
                     "static": static, "simulation": simulation,
                     "gap_kind": gap_kind if status == 'NOT_TESTED' else '',
                     "comparison_evidence": examples.get(req["id"], []),
                     "evidence_gap": evidence_gap})

    passed = sum(row["status"] == "PASS" for row in rows)
    failed = sum(row["status"] == "FAIL" for row in rows)
    gaps = sum(row["status"] == "NOT_TESTED" for row in rows)
    simulation_passed = sum(row['verification_method'] == 'simulation' and
                            row['status'] == 'PASS' for row in rows)
    structural_passed = sum(row['verification_method'] == 'static' and
                            row['status'] == 'PASS' for row in rows)
    simulation_gaps = sum(row['verification_method'] == 'simulation' and
                          row['status'] == 'NOT_TESTED' for row in rows)
    if verification_failed or failed:
        overall = "FAIL"
    elif simulation_passed == 0:
        overall = "NOT_TESTED"
    elif gaps:
        overall = "PASS_WITH_GAPS"
    else:
        overall = "PASS"
    return {"overall_status": overall, "total_requirements": len(rows),
            "startup_reset": reset_evidence,
            "failure_stage": 'testbench_initialization' if reset_failed else
                             ('simulation' if overall == 'FAIL' else None),
            "tested_requirements": sum(row['verification_method'] == 'simulation' and
                                       row['simulation']['checks'] > 0 for row in rows),
            "passed_requirements": passed, "failed_requirements": failed,
            "not_tested_requirements": gaps, "requirements": rows,
            "simulation_passed_requirements": simulation_passed,
            "structural_passed_requirements": structural_passed,
            "simulation_gaps": simulation_gaps,
            "structural_gaps": gaps - simulation_gaps,
            "simulation_completed": simulation_ok,
            "scoreboard_passed_checks": sb_pass,
            "scoreboard_failed_checks": sb_fail,
            "uvm_errors": errors, "uvm_fatals": fatals,
            "simulation_errors": extract_simulation_errors(sim_text)}


def save_requirement_report(
    report
):
    ensure_build_dirs()

    with open(
        REQUIREMENT_REPORT_FILE,
        "w"
    ) as f:

        json.dump(
            report,
            f,
            indent=2
        )
    if ACTIVE_REPORT is not None:
        ACTIVE_REPORT.requirements(report)


def print_requirement_report(
    report
):
    banner(
        "RTL REQUIREMENT VERIFICATION",
        "cyan"
    )

    print(
        "  YAML requirements : {}".format(
            report[
                "total_requirements"
            ]
        )
    )

    print(
        "  Dynamically tested: {}".format(
            report[
                "tested_requirements"
            ]
        )
    )

    print(
        "  Passed            : {}".format(
            report[
                "passed_requirements"
            ]
        )
    )

    print(
        "  Failed            : {}".format(
            report[
                "failed_requirements"
            ]
        )
    )

    print("  UVM passed        : {}".format(report['simulation_passed_requirements']))
    print("  Structural passed : {}".format(report['structural_passed_requirements']))
    reset_evidence = report.get('startup_reset', {})
    if reset_evidence.get('required'):
        print("  Startup reset     : {} — {}".format(
            reset_evidence['status'], reset_evidence['evidence']))
    if report['not_tested_requirements']:
        print("  Unverified        : {} ({} UVM, {} structural)".format(
            report['not_tested_requirements'], report['simulation_gaps'], report['structural_gaps']))

    print(
        "  Overall           : {}".format(
            report[
                "overall_status"
            ]
        )
    )

    print(
        "\n  YAML defines expected behavior."
    )

    print(
        (
            "  Each result identifies its verification method and evidence."
        )
    )

    for row in report[
        "requirements"
    ]:

        status = row[
            "status"
        ]

        if status == "PASS":

            status_text = c(
                "green",
                status
            )

        elif status == "FAIL":

            status_text = c(
                "red",
                status
            )

        else:

            status_text = c(
                "yellow",
                status
            )

        print()

        print(
            "  [{}] {} ({}) — {}".format(
                status_text,
                row[
                    "id"
                ],
                row[
                    "category"
                ],
                row['verification_label'],
            )
        )

        wrapped_requirement = (
            textwrap.wrap(
                row[
                    "requirement"
                ],
                width=70
            )

            or

            [
                ""
            ]
        )

        for (
            index,
            line
        ) in enumerate(
            wrapped_requirement
        ):

            prefix = (
                "      Expected RTL behavior: "

                if index == 0

                else

                "                             "
            )

            print(
                prefix
                +
                line
            )

        evidence = row.get(
            "evidence",
            ""
        )

        if evidence:

            wrapped_evidence = (
                textwrap.wrap(
                    evidence,
                    width=70
                )

                or

                [
                    ""
                ]
            )

            for (
                index,
                line
            ) in enumerate(
                wrapped_evidence
            ):

                prefix = (
                    "      Verification evidence: "

                    if index == 0

                    else

                    "                            "
                )

                print(
                    prefix
                    +
                    line
                )

    print()

    print(
        "  Saved report: {}".format(
            os.path.abspath(
                REQUIREMENT_REPORT_FILE
            )
        )
    )


# ============================================================
# MAIN AGENT 2 FLOW
# ============================================================

def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def save_uvm_environment(report, requirements, rtl_files, dut_spec, yaml_spec,
                         compile_output, sim_output):
    """Preserve the sources and evidence for this run before a future generation.

    The manifest describes reuse; it does not assert that Agent 3 has integrated
    with UVM. RTL fingerprints identify inputs without copying an incomplete
    include/dependency tree or modifying the DUT.
    """
    runs_root = os.path.join(BUILD_DIR, 'uvm_runs')
    os.makedirs(runs_root, exist_ok=True)
    run_root = os.path.abspath(tempfile.mkdtemp(prefix='run_', dir=runs_root))
    saved_tb = os.path.join(run_root, 'tb_auto')
    os.makedirs(saved_tb)
    sources = []
    for path in ordered_tb_files(TB_ROOT):
        destination = os.path.join(saved_tb, os.path.basename(path))
        shutil.copyfile(path, destination)
        sources.append({'original_path': os.path.abspath(path),
                        'path': destination, 'sha256': file_sha256(destination)})
    manifest = {
        'schema_version': 1, 'dut_top_module': dut_spec['module_name'],
        'testbench_top': 'dut_tb_top', 'default_test': 'dut_test',
        'testbench_files': sources,
        'rtl_files': [{'path': os.path.abspath(path), 'sha256': file_sha256(path)}
                      for path in rtl_files],
        'uvm_flags': list(VCS_UVM_FLAGS), 'timescale': '1ns/1ps',
        'dut_spec': dut_spec, 'requirements': requirements,
        'approved_yaml': os.path.join(run_root, 'approved_spec.yaml'),
        'compile_log': os.path.join(run_root, 'compile.log'),
        'simulation_log': os.path.join(run_root, 'simulation.log'),
        'requirement_report': os.path.join(run_root, 'requirement_traceability.json'),
        'overall_status': report['overall_status'],
        'agent3_executed': False,
    }
    with open(manifest['approved_yaml'], 'w') as handle:
        handle.write(safe_yaml_dump(yaml_spec))
    for key, output in (('compile_log', compile_output), ('simulation_log', sim_output)):
        with open(manifest[key], 'w') as handle:
            handle.write(output)
    manifest_path = os.path.join(run_root, 'uvm_environment.json')
    report['uvm_environment_file'] = manifest_path
    snapshot_report = dict(report)
    snapshot_report['simulation_log'] = manifest['simulation_log']
    snapshot_report['compile_log'] = manifest['compile_log']
    snapshot_report['yaml_spec_file'] = manifest['approved_yaml']
    if report.get('combined_report_file'):
        combined_copy = os.path.join(run_root, 'combined_requirement_verification.json')
        shutil.copyfile(report['combined_report_file'], combined_copy)
        snapshot_report['combined_report_file'] = combined_copy
    with open(manifest['requirement_report'], 'w') as handle:
        json.dump(snapshot_report, handle, indent=2)
    with open(manifest_path, 'w') as handle:
        json.dump(manifest, handle, indent=2)
    # Update the discoverable manifest only after the complete snapshot exists.
    with tempfile.NamedTemporaryFile(mode='w', dir=BUILD_DIR, delete=False) as handle:
        pending = handle.name
        json.dump(manifest, handle, indent=2)
    os.replace(pending, os.path.join(BUILD_DIR, 'uvm_environment.json'))
    return manifest_path


def run_uvm_agent(
    rtl_files,
    top_module_file,
    dut_spec,
    yaml_spec_file,
    yaml_spec,
    requirements,
    reuse_tb=False
):
    banner(
        "AGENT 2 - RTL UVM VERIFICATION",
        "magenta"
    )

    ensure_build_dirs()

    rtl_code = read_file(
        top_module_file
    )
    reset_config = startup_reset_config(dut_spec, yaml_spec, rtl_code)
    if ACTIVE_REPORT is not None:
        ACTIVE_REPORT.update(module_name=dut_spec['module_name'], rtl_files=rtl_files,
                             yaml_spec_file=os.path.abspath(yaml_spec_file))
        ACTIVE_REPORT.artifact('approved_spec.yaml', source=yaml_spec_file)
        initial_rows = []
        for req in requirements:
            static = req.get('static_result', {}) if req.get('verification_method') == 'static' else {}
            initial_rows.append(dict(req, requirement=req['text'], status=static.get('status', 'NOT_TESTED'),
                                     evidence=static.get('evidence', 'Simulation has not run.')))
        ACTIVE_REPORT.update(requirements=initial_rows,
                             untested_requirements=[r for r in initial_rows if r['status'] == 'NOT_TESTED'],
                             failed_requirements=[r for r in initial_rows if r['status'] == 'FAIL'])

    log(
        "UVM_TB_AGENT",
        "RTL DUT top module: {}".format(
            dut_spec[
                "module_name"
            ]
        ),
        "ok",
    )

    log(
        "UVM_TB_AGENT",
        "RTL DUT top file: {}".format(
            top_module_file
        ),
        "ok",
    )

    log(
        "UVM_TB_AGENT",
        "RTL files to simulate: {}".format(
            len(
                rtl_files
            )
        ),
        "info",
    )

    log(
        "UVM_TB_AGENT",
        (
            "YAML expected-behavior source: {}"
        ).format(
            yaml_spec_file
        ),
        "info",
    )

    log(
        "UVM_TB_AGENT",
        (
        "Extracted {} requirements "
            "to check against the RTL."
        ).format(
            len(
                requirements
            )
        ),
        "info",
    )

    if ACTIVE_REPORT is not None:
        ACTIVE_REPORT.artifact('requirement_catalog.json', data={'requirements': requirements})
    if not requirements:
        detail = ('No requirements were extracted from the approved YAML. Verification cannot '
                  'start without a catalog of expected behavior. No LLM request or RTL simulation was performed.')
        if ACTIVE_REPORT is not None:
            ACTIVE_REPORT.step('requirement_extraction')
            ACTIVE_REPORT.update(failure_detail=detail, coverage_limitations=[detail])
        log('UVM_TB_AGENT', detail, 'error')
        return False

    last_compile_failed = False
    failed_candidate_sources = None

    last_compile_output = ""

    generation_failures = 0

    generation_issues = []
    candidate_files = []
    coverage_state = None
    coverage_round = 0
    last_sequence = 'dut_sequence'

    # ========================================================
    # GENERATION / COMPILE LOOP
    # ========================================================

    for i in range(
        MAX_ITERS
    ):
        if ACTIVE_REPORT is not None:
            ACTIVE_REPORT.step('coverage_generation' if coverage_state else
                               ('testbench_audit' if reuse_tb else 'testbench_generation'))

        section(
            "Iteration {} / {}".format(
                i + 1,
                MAX_ITERS
            )
        )

        # ====================================================
        # ONLY ENTER VCS REPAIR MODE IF VCS REALLY FAILED
        # ====================================================

        if coverage_state is not None:
            log('UVM_TB_AGENT', 'Adding directed sequence traffic for unexecuted baseline checks; '
                'keeping the compiled monitor and scoreboard unchanged.', 'info')
            try:
                raw = call_llm('Return only the requested SystemVerilog UVM sequence class.',
                               prompt_coverage_extension(coverage_state, yaml_spec), 'UVM_TB_AGENT')
                save_raw_llm_response(raw, i)
                candidate_files = install_coverage_sequence(
                    coverage_state['base'], raw, coverage_state['name'], coverage_state['parent'])
            except ValueError as exc:
                coverage_state['error'] = str(exc)
                log('UVM_TB_AGENT', 'Coverage sequence needs repair: ' + str(exc), 'warn')
                continue
            except Exception as exc:
                log('UVM_TB_AGENT', 'Coverage sequence generation failed: {}'.format(exc), 'error')
                return False
            publish_generated_files(candidate_files)
        elif reuse_tb:
            existing = [os.path.join(TB_ROOT, name) for name in sorted(ALLOWED_TB_FILES)]
            missing = [path for path in existing if not os.path.isfile(path)]
            if missing:
                log("UVM_TB_AGENT", "Missing existing TB files: " + ', '.join(missing), "error")
                return False
            issues = audit_generated_testbench([(path, read_file(path)) for path in existing], dut_spec, requirements)
            issues += audit_packed_payload_decoding([(path, read_file(path)) for path in existing], yaml_spec)
            issues += audit_monitor_contract([(path, read_file(path)) for path in existing], reset_config)
            issues += audit_driver_timing([(path, read_file(path)) for path in existing], reset_config)
            if issues:
                for issue in issues:
                    log("UVM_TB_AGENT", issue, "error")
                return False
            log("UVM_TB_AGENT", "Using existing reviewed testbench; no LLM generation or repair.", "info")
        else:
            if generation_issues:
                prompt = prompt_compile_fix(
                    rtl_code, dut_spec, rtl_files, yaml_spec, requirements,
                    audit_repair_context(candidate_files, generation_issues, requirements),
                    '\n'.join('=== filename: {} ===\n{}'.format(path, code)
                              for path, code in candidate_files), failure_stage='audit')
                log("UVM_TB_AGENT", "Repairing generated check/report defects before simulation...", "warn")

            elif last_compile_failed:

                error_block = extract_vcs_errors(
                    last_compile_output
                )

                if not error_block:

                    error_block = (
                        "VCS returned a compile failure, "
                        "but no concise error block "
                        "could be extracted."
                    )

                error_block = compile_error_context(
                    [(os.path.join(TB_ROOT, name), read_file(os.path.join(TB_ROOT, name)))
                     for name in COMPILE_ORDER], error_block)
                tb_source = read_tb_files_for_context()

                prompt = prompt_compile_fix(
                    rtl_code,
                    dut_spec,
                    rtl_files,
                    yaml_spec,
                    requirements,
                    error_block,
                    tb_source,
                )

                log(
                    "UVM_TB_AGENT",
                    (
                        "Repairing generated UVM TB "
                        "after a real VCS compile failure..."
                    ),
                    "warn",
                )

            else:

                prompt = prompt_generate(
                    rtl_code,
                    dut_spec,
                    rtl_files,
                    yaml_spec,
                    requirements,
                    generation_failures
                    +
                    1,
                )

                if generation_failures == 0:

                    log(
                        "UVM_TB_AGENT",
                        (
                            "Generating UVM testbench "
                            "to verify the RTL against "
                            "YAML requirements..."
                        ),
                        "info",
                    )

                else:

                    log(
                        "UVM_TB_AGENT",
                        (
                            "Retrying UVM testbench "
                            "generation because no usable "
                            "file blocks were returned..."
                        ),
                        "warn",
                    )

            # ====================================================
            # LLM GENERATION
            # ====================================================

            try:

                raw = call_llm(
                    UVM_AUDIT_REPAIR_SYSTEM if generation_issues else
                    (UVM_REPAIR_SYSTEM if candidate_files else UVM_SYSTEM),
                    prompt,
                    "UVM_TB_AGENT",
                )

            except Exception as exc:

                log(
                    "UVM_TB_AGENT",
                    "LLM request failed: {}".format(
                        exc
                    ),
                    "error",
                )

                return False

            raw_path = save_raw_llm_response(
                raw,
                i
            )

            log(
                "UVM_TB_AGENT",
                (
                    "Raw LLM response saved: {}"
                ).format(
                    raw_path
                ),
                "muted",
            )

            # An incomplete reply must not destroy the previous testbench or
            # trigger the same oversized three-file request on every iteration.
            try:
                candidate_files = complete_generated_candidate(raw, prompt, i, candidate_files)
            except ValueError as exc:
                generation_issues = (['Audit edit application failed: ' + str(exc)] +
                                     [issue for issue in generation_issues
                                      if not issue.startswith('Audit edit application failed:')])
                generation_failures += 1
                log('UVM_TB_AGENT', generation_issues[0], 'warn')
                continue
            except Exception as exc:
                log("UVM_TB_AGENT", "Testbench generation failed: {}".format(exc), "error")
                return False

            candidate_files = normalize_generated_identifiers(candidate_files)
            candidate_files = normalize_generated_routines(candidate_files)
            candidate_files = normalize_lowest_slot_search(candidate_files, requirements)
            runtime_issues = []
            try:
                candidate_files = install_startup_reset_contract(candidate_files, reset_config)
                candidate_files = install_requirement_evidence(candidate_files)
            except ValueError as exc:
                runtime_issues.append(str(exc))
            if failed_candidate_sources is not None and dict(candidate_files) == failed_candidate_sources:
                runtime_issues.append('Compile repair left the failing candidate unchanged. ' +
                                      compile_error_context(candidate_files, extract_vcs_errors(last_compile_output)))
            generation_issues = (runtime_issues + audit_generated_testbench(
                candidate_files, dut_spec, requirements) + audit_driver_timing(candidate_files, reset_config) +
                audit_monitor_contract(candidate_files, reset_config) +
                audit_packed_payload_decoding(candidate_files, yaml_spec))
            audit_path = os.path.join(LOG_DIR, "generation_audit_iter_{}.json".format(i))
            with open(audit_path, 'w') as audit_file:
                json.dump({'stage': 'generated_testbench', 'issues': generation_issues},
                          audit_file, indent=2)
            if ACTIVE_REPORT is not None:
                for candidate_path, candidate_code in candidate_files:
                    ACTIVE_REPORT.artifact('candidates/iter_{}/{}'.format(i, os.path.basename(candidate_path)),
                                           text=candidate_code)
                ACTIVE_REPORT.artifact('generation_audit_iter_{}.json'.format(i),
                                       data={'stage': 'generated_testbench', 'issues': generation_issues})
                ACTIVE_REPORT.update(generation_issues=generation_issues)
            if generation_issues:
                last_compile_failed = False
                generation_failures += 1
                for issue in generation_issues:
                    log("UVM_TB_AGENT", issue, "warn")
                continue

            publish_generated_files(candidate_files)

        # ====================================================
        # COMPILE RTL + UVM TB
        # ====================================================

        if ACTIVE_REPORT is not None:
            ACTIVE_REPORT.step('compile')
        (
            ok_compile,
            compile_log,
            compile_output
        ) = vcs_compile(
            rtl_files,
            i,
        )
        if ACTIVE_REPORT is not None:
            ACTIVE_REPORT.artifact('compile_iter_{}.log'.format(i), text=compile_output)
            ACTIVE_REPORT.update(compile_passed=ok_compile)

        log(
            "VCS",
            "Compile {} -> {}".format(
                (
                    "PASS"

                    if ok_compile

                    else

                    "FAIL"
                ),

                compile_log,
            ),
            (
                "ok"

                if ok_compile

                else

                "warn"
            ),
        )

        # ====================================================
        # TESTBENCH COMPILE FAILURE
        # ====================================================

        if not ok_compile:
            if coverage_state is not None:
                coverage_state['error'] = extract_vcs_errors(compile_output) or compile_output[-6000:]
                log('UVM_TB_AGENT', 'Repairing only the added sequence after its compile failure.', 'warn')
                continue
            if reuse_tb:
                log("UVM_TB_AGENT", "Existing testbench compile failed: " + compile_log, "error")
                return False


            last_compile_failed = True
            failed_candidate_sources = dict(candidate_files) if candidate_files else None

            last_compile_output = (
                compile_output
            )

            error_block = (
                extract_vcs_errors(
                    compile_output
                )
            )

            if error_block:

                print(
                    c(
                        "yellow",
                        "\n-- VCS Compile Errors --"
                    )
                )

                for line in (
                    error_block
                    .splitlines()[
                        :35
                    ]
                ):

                    print(
                        line
                    )

            continue

        # ====================================================
        # COMPILE SUCCEEDED
        #
        # DO NOT REGENERATE TESTS AFTER THIS SIMPLY BECAUSE
        # THE RTL DUT FAILS.
        # ====================================================

        last_compile_failed = False

        # ====================================================
        # SIMULATE ACTUAL RTL DUT
        # ====================================================

        log(
            "UVM_TB_AGENT",
            (
                "Running UVM simulation "
                "of the RTL DUT..."
            ),
            "info",
        )

        if ACTIVE_REPORT is not None:
            ACTIVE_REPORT.step('simulation')
        (
            ok_run,
            sim_log,
            sim_output
        ) = vcs_run(
            i
        )
        if ACTIVE_REPORT is not None:
            ACTIVE_REPORT.artifact('sim_iter_{}.log'.format(i), text=sim_output)
            ACTIVE_REPORT.update(simulation_completed=ok_run)

        log(
            "VCS",
            "Simulator process exit {} -> {}".format(
                (
                    "PASS"

                    if ok_run

                    else

                    "FAIL"
                ),

                sim_log,
            ),
            (
                "ok"

                if ok_run

                else

                "warn"
            ),
        )

        # ====================================================
        # SCOREBOARD
        # ====================================================

        scoreboard_passed = (
            print_scoreboard(
                sim_output,
                i
            )
        )

        # ====================================================
        # REQUIREMENT TRACEABILITY
        # ====================================================

        requirement_report = (
            build_requirement_report(
                requirements,
                sim_output,
                simulation_ok=ok_run,
                reset_config=reset_config,
            )
        )

        requirement_report[
            "rtl_top_module"
        ] = dut_spec[
            "module_name"
        ]

        requirement_report[
            "rtl_top_file"
        ] = os.path.abspath(
            top_module_file
        )

        requirement_report[
            "yaml_spec_file"
        ] = os.path.abspath(
            yaml_spec_file
        )

        requirement_report[
            "simulation_log"
        ] = os.path.abspath(
            sim_log
        )

        simulation_report = dict(requirement_report)
        simulation_report['requirements'] = [dict(row['simulation'], id=row['id'])
                                              for row in requirement_report['requirements']]
        for row, simulation_row in zip(requirement_report['requirements'], simulation_report['requirements']):
            if row.get('requires_static_support'):
                simulation_row.update(status=row['status'], evidence=row['evidence'])
        combined = combine_requirement_results(requirements, simulation_report)
        combined_path = os.path.join(BUILD_DIR, "combined_requirement_verification.json")
        with open(combined_path, "w") as report_file:
            json.dump(combined, report_file, indent=2)
        if ACTIVE_REPORT is not None:
            ACTIVE_REPORT.artifact('combined_requirement_verification.json', data=combined)
        requirement_report["coverage_policy"] = "Allow Agent 3 after passing UVM checks; report coverage gaps."
        requirement_report["combined_overall_status"] = combined["overall_status"]
        requirement_report["combined_report_file"] = os.path.abspath(combined_path)
        try:
            environment_path = save_uvm_environment(
                requirement_report, requirements, rtl_files, dut_spec, yaml_spec,
                compile_output, sim_output)
        except OSError as exc:
            log('UVM_TB_AGENT', 'Could not preserve UVM sources/evidence: {}'.format(exc), 'error')
            save_requirement_report(requirement_report)
            return False
        log('UVM_TB_AGENT', 'Preserved UVM environment and run evidence: ' + environment_path, 'info')

        save_requirement_report(
            requirement_report
        )

        print_requirement_report(
            requirement_report
        )
        for row in requirement_report["requirements"]:
            for example in row.get("comparison_evidence", [])[:2]:
                print("  {}: {} at {}; expected {}; observed {}".format(
                    row["id"], example["scenario"], example["cycle_or_time"],
                    example["expected"], example["observed"]))
            if row.get("evidence_gap"):
                print("  {} evidence gap: {}".format(row["id"], row["evidence_gap"]))

        if requirement_report.get('failure_stage') == 'testbench_initialization':
            log('UVM_TB_AGENT', 'TESTBENCH INITIALIZATION FAILED — startup reset was not '
                'confirmed. RTL correctness is inconclusive; inspect the saved UVM '
                'environment and AGENT2_TB_RESET messages.', 'error')
            return False


        # ====================================================
        # VCS SIMULATION FAILURE
        # ====================================================

        if not ok_run:

            log(
                "UVM_TB_AGENT",
                (
                    "FAIL - VCS simulation of the RTL "
                    "returned a non-zero status."
                ),
                "error",
            )

            return False

        # ====================================================
        # SCOREBOARD FAILURE
        # ====================================================

        if not scoreboard_passed:

            log(
                "UVM_TB_AGENT",
                (
                    "FAIL - UVM detected a verification failure. Review the saved "
                    "expected/observed evidence and checker before changing RTL."
                ),
                "error",
            )

            return False

        # ====================================================
        # YAML REQUIREMENT FAILURE
        # ====================================================

        if (
            requirement_report[
                "failed_requirements"
            ]
            >
            0
        ):

            log(
                "UVM_TB_AGENT",
                (
                    "FAIL - one or more YAML requirement checks failed. "
                    "See the saved comparisons for the exact mismatch."
                ),
                "error",
            )

            log(
                "UVM_TB_AGENT",
                (
                    "The testbench will NOT "
                    "be regenerated to hide "
                    "a DUT failure."
                ),
                "error",
            )

            return False

        if requirement_report["overall_status"] not in ("PASS", "PASS_WITH_GAPS"):
            log("UVM_TB_AGENT",
                "Verification incomplete or failed: no trustworthy overall pass.",
                "error")
            return False

        if coverage_state is not None:
            last_sequence = coverage_state['name']
            coverage_state = None
        gaps = coverage_gap_targets(requirement_report, candidate_files)
        if (gaps and not reuse_tb and coverage_round < MAX_COVERAGE_EXTENSIONS and
                i + 1 < MAX_ITERS):
            coverage_round += 1
            coverage_state = {'base': list(candidate_files), 'gaps': gaps,
                              'parent': last_sequence,
                              'name': 'agent2_coverage_sequence_{}'.format(coverage_round)}
            continue

        # ====================================================
        # PASS WITH GAPS
        # ====================================================

        if (
            requirement_report[
                "not_tested_requirements"
            ]
            >
            0
        ):

            print(
                "\n{}\n".format(
                    c(
                        "yellow",
                        (
                            "[UVM_TB_AGENT] RTL VERIFICATION "
                            "PASS WITH GAPS - all dynamically "
                            "tested RTL requirements passed, "
                            "some requirements remain unverified by this run. "
                            "See the report for gaps. Agent 3 may proceed."
                        ),
                    )
                )
            )

            return True

        # ====================================================
        # COMPLETE PASS
        # ====================================================

        print(
            "\n{}\n".format(
                c(
                    "green",
                    (
                        "[UVM_TB_AGENT] RTL VERIFICATION "
                        "PASS - all catalogued requirements have "
                        "passing UVM or structural evidence."
                    ),
                )
            )
        )

        return True

    # ========================================================
    # COULD NOT GENERATE/COMPILE TESTBENCH
    # ========================================================

    print(
        "\n{}\n".format(
            c(
                "red",
                (
                    "[UVM_TB_AGENT] FAIL - could not "
                    "generate and compile a usable "
                    "UVM testbench after {} attempts."
                ).format(
                    MAX_ITERS
                ),
            )
        )
    )

    return False


# ============================================================
# MAIN
# ============================================================

def _main(report):
    global ACTIVE_REPORT
    ACTIVE_REPORT = report
    reuse_tb = "--reuse-tb" in sys.argv[1:]
    rtl_arguments = [arg for arg in sys.argv[1:] if arg != "--reuse-tb"]
    validation_result = (
        load_validation_result()
    )

    if not validation_result:

        print(
            c(
                "red",
                (
                    "Missing validation_result.json. "
                    "Run Agent 1 first."
                ),
            )
        )

        sys.exit(
            1
        )

    # ========================================================
    # AGENT 1 MUST PASS
    # ========================================================

    if not validation_result.get(
        "is_valid"
    ):

        print(
            c(
                "red",
                (
                    "[BLOCKED] Agent 1 "
                    "marked the RTL invalid."
                ),
            )
        )

        print(
            c(
                "yellow",
                "Summary: {}".format(
                    validation_result.get(
                        "summary",
                        ""
                    )
                ),
            )
        )

        for issue in validation_result.get(
            "issues",
            []
        ):

            print(
                "  - {}".format(
                    issue
                )
            )

        sys.exit(
            1
        )

    # ========================================================
    # RTL FILES
    #
    # run_pipeline.py currently passes RTL files as CLI args.
    # ========================================================

    if rtl_arguments:

        rtl_files = [
            os.path.abspath(
                path
            )
            for path in rtl_arguments
        ]

    else:

        rtl_files = [
            os.path.abspath(
                path
            )
            for path in validation_result.get(
                "rtl_files",
                []
            )
        ]

    if not rtl_files:

        print(
            c(
                "red",
                (
                    "No RTL files were provided "
                    "by the pipeline or Agent 1."
                ),
            )
        )

        sys.exit(
            1
        )

    # ========================================================
    # AGENT 1 DUT INFORMATION
    # ========================================================

    top_module_file = (
        validation_result.get(
            "top_module_file"
        )
    )

    dut_spec = (
        validation_result.get(
            "dut_spec"
        )
    )

    if not top_module_file:

        print(
            c(
                "red",
                (
                    "Agent 1 did not provide "
                    "top_module_file."
                ),
            )
        )

        sys.exit(
            1
        )

    if not dut_spec:

        print(
            c(
                "red",
                (
                    "Agent 1 did not provide "
                    "dut_spec."
                ),
            )
        )

        sys.exit(
            1
        )

    # ========================================================
    # LOAD APPROVED YAML
    # ========================================================

    (
        yaml_spec,
        yaml_spec_file,
        yaml_error
    ) = load_yaml_spec_from_validation(
        validation_result
    )

    if yaml_error:

        print(
            c(
                "red",
                "[BLOCKED] {}".format(
                    yaml_error
                ),
            )
        )

        sys.exit(
            1
        )

    # ========================================================
    # BUILD REQUIREMENT CATALOG
    # ========================================================

    requirements = attach_static_results(
        build_requirement_catalog(yaml_spec),
        "\n".join(read_file(path) for path in rtl_files))

    # ========================================================
    # RUN AGENT 2
    # ========================================================

    ok = run_uvm_agent(
        rtl_files,
        top_module_file,
        dut_spec,
        yaml_spec_file,
        yaml_spec,
        requirements,
        reuse_tb=reuse_tb,
    )

    sys.exit(
        0
        if ok
        else
        1
    )


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    inputs = [arg for arg in sys.argv[1:] if arg != '--reuse-tb']
    if not inputs:
        try:
            validation = load_validation_result() or {}
        except (OSError, ValueError):
            validation = {}
        inputs = validation.get('rtl_files') or [os.getcwd()]
    try:
        run_reported_agent('agent2', _main, inputs)
    finally:
        global ACTIVE_REPORT
        ACTIVE_REPORT = None


if __name__ == "__main__":
    main()
