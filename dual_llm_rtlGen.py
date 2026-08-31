"""
dual_llm_rtlgen.py  —  Dual-LLM RTL generation loop
Replaces rtlGen.py for non-FSM design types.

Flow per round:
  1. LLM #1 (Generator)  generates or merges RTL
  2. Syntax check         lint pass; if errors, back to LLM #1 with report
  3. LLM #2 (Reviewer)   reviews code, produces critique + corrected RTL
  4. LLM #1 (Merger)     receives both versions + report, produces merged output
  5. Exit if LLM #2 reports clean  OR  round >= MAX_ROUNDS

Context window: sliding 2-round window passed to both LLMs each round.

Usage:
  from dual_llm_rtlgen import run_dual_llm_rtlgen
  rtl_code = run_dual_llm_rtlgen(design, yaml_text)

  python3 dual_llm_rtlgen.py inputs/some_spec.yaml
"""

import os
import re
import sys
import json
import hashlib
import shutil
import tempfile
import subprocess
import requests

from ir_to_llm_context import build_llm_context, llm_context_to_string
from width_safety import enforce_width_safety

# ---------------------------------------------------------------------------
# Configuration  — edit these to change models or loop behaviour
# ---------------------------------------------------------------------------

# LLM #1: generator / merger
LLM1_MODEL = "protected.gpt-4.1"

# LLM #2: reviewer / fixer — different model for diverse critique
LLM2_MODEL = "protected.Claude Sonnet 4.6"

# Maximum full rounds before accepting best-so-far output
MAX_ROUNDS = 3

# Maximum syntax-fix attempts per round before giving up and proceeding
MAX_SYNTAX_RETRIES = 1

# Avoid flooding logs with huge malformed reviewer/generator responses.
RAW_RESPONSE_PREVIEW_CHARS = 1000

# TAMU API base
API_BASE = "https://chat-api.tamu.ai"
ENDPOINT = API_BASE + "/openai/chat/completions"
DUAL_LLM_MARKER = "// GENERATED VIA DUAL-LLM FLOW"
CACHE_DIRNAME = ".llm_cache"

# ---------------------------------------------------------------------------
# API helper
# ---------------------------------------------------------------------------

def _call_llm(model, system_prompt, user_message, label="LLM"):
    """
    Single-shot call to the TAMU LLM API.
    Returns the response text string, or raises RuntimeError on failure.
    """
    api_key = os.environ.get("TAMUS_AI_CHAT_API_KEY", "")
    if not api_key:
        raise RuntimeError("TAMUS_AI_CHAT_API_KEY not set. Run: source ~/.bashrc")

    payload = {
        "model": model,
        "max_tokens": 4096,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message}
        ]
    }

    try:
        resp = requests.post(
            ENDPOINT,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type":  "application/json"
            },
            json=payload,
            timeout=120
        )
    except requests.exceptions.RequestException as e:
        raise RuntimeError("[{}] Network error: {}".format(label, e))

    if resp.status_code != 200:
        key_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:12]
        raise RuntimeError(
            "[{}] {} {} -> HTTP {}: {}; Allow={!r}; "
            "API key length={}, sha256[:12]={}".format(
                label,
                resp.request.method,
                resp.url,
                resp.status_code,
                resp.text[:300],
                resp.headers.get("Allow"),
                len(api_key),
                key_fingerprint,
            )
        )

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, ValueError) as e:
        raise RuntimeError("[{}] Unexpected response shape: {}".format(label, e))


# ---------------------------------------------------------------------------
# Syntax checker
# ---------------------------------------------------------------------------

# Patterns that strongly suggest broken SystemVerilog
_SV_ERROR_PATTERNS = [
    (r"endmodule\s*$",         False,  "missing endmodule"),
    (r"module\s+\w+",          False,  "missing module declaration"),
    (r"begin(?!.*end)",        True,   "unmatched begin/end (heuristic)"),
]

# Keywords that must appear in valid SV
_SV_REQUIRED = ["module", "endmodule"]


def _heuristic_syntax_check(rtl_code):
    """
    Lightweight regex-based syntax check.
    Returns (ok: bool, issues: list[str]).

    This is the fallback when Verilator is unavailable.
    """
    issues = []
    code = rtl_code.strip()

    if not code:
        return False, ["RTL output is empty"]

    for keyword in _SV_REQUIRED:
        if keyword not in code:
            issues.append("Missing required keyword: '{}'".format(keyword))

    # Count module/endmodule — should be equal
    mod_count     = len(re.findall(r"\bmodule\b",    code))
    endmod_count  = len(re.findall(r"\bendmodule\b", code))
    if mod_count != endmod_count:
        issues.append(
            "module/endmodule count mismatch: {} module vs {} endmodule".format(
                mod_count, endmod_count)
        )

    # begin/end balance
    begin_count = len(re.findall(r"\bbegin\b", code))
    end_count   = len(re.findall(r"\bend\b",   code))
    if begin_count != end_count:
        issues.append(
            "begin/end count mismatch: {} begin vs {} end".format(
                begin_count, end_count)
        )

    # Stray Python repr artifacts (common LLM failure mode for this project)
    if "dataclass" in code or "SignalRef(" in code or "Compare(" in code:
        issues.append("RTL contains Python dataclass repr — LLM serialised IR object instead of generating SV")

    # Obvious literal typos from known bugs
    if "1'b'" in code:
        issues.append("Literal typo detected: 1'b' (extra apostrophe)")

    return (len(issues) == 0), issues


def _run_verilator_syntax_check(rtl_code):
    """
    Run Verilator lint-only mode on a temporary SystemVerilog file.
    Returns (ok: bool, issues: list[str]), or (None, None) when Verilator
    is unavailable or the tool invocation itself fails unexpectedly.
    """
    verilator_bin = shutil.which("verilator")
    if not verilator_bin:
        return None, None

    temp_dir = tempfile.mkdtemp(prefix="dual_llm_verilator_")
    sv_path = os.path.join(temp_dir, "candidate.sv")

    try:
        with open(sv_path, "w") as fh:
            fh.write(rtl_code)

        proc = subprocess.run(
            [verilator_bin, "--lint-only", "-Wno-fatal", sv_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode == 0:
            return True, []

        issues = []
        for line in output.splitlines():
            text = line.strip()
            if not text:
                continue
            if "%Error" in text or "%Warning" in text:
                issues.append(text)

        if not issues and output:
            issues = [line.strip() for line in output.splitlines() if line.strip()]

        if not issues:
            issues = ["Verilator lint failed with no diagnostic output"]

        return False, issues

    except OSError:
        return None, None

    finally:
        try:
            shutil.rmtree(temp_dir)
        except OSError:
            pass


def syntax_check(rtl_code):
    """
    Run Verilator lint-only syntax checking when available, otherwise fall back
    to a lightweight heuristic checker.
    Returns (ok: bool, issues: list[str]).
    """
    ok, issues = _run_verilator_syntax_check(rtl_code)
    if ok is None:
        return _heuristic_syntax_check(rtl_code)
    return ok, issues


def _add_dual_llm_marker(rtl_code):
    """Tag RTL that came through this dual-LLM pipeline."""
    if DUAL_LLM_MARKER in rtl_code:
        return rtl_code
    lines = rtl_code.splitlines()
    insert_at = 0
    while insert_at < len(lines) and lines[insert_at].strip().startswith("`timescale"):
        insert_at += 1
    lines.insert(insert_at, DUAL_LLM_MARKER)
    return "\n".join(lines) + ("\n" if rtl_code.endswith("\n") else "")


def _finalize_dual_llm_rtl(rtl_code, module_name):
    return _add_dual_llm_marker(enforce_width_safety(rtl_code, module_name))


def _cache_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CACHE_DIRNAME)


def _cache_path(module_name):
    return os.path.join(_cache_dir(), "{}.sv".format(module_name))


def _cache_display_path(module_name):
    return os.path.join(CACHE_DIRNAME, "{}.sv".format(module_name))


def _write_reviewed_cache(module_name, rtl_code):
    cached_rtl = _finalize_dual_llm_rtl(rtl_code, module_name)
    os.makedirs(_cache_dir(), exist_ok=True)
    with open(_cache_path(module_name), "w") as fh:
        fh.write(cached_rtl)
    print("[dual_llm] Cached reviewed RTL: {}".format(
        _cache_display_path(module_name)))
    return cached_rtl


def _load_reviewed_cache(module_name):
    path = _cache_path(module_name)
    if not os.path.exists(path):
        return None

    with open(path, "r") as fh:
        cached_rtl = fh.read()

    syntax_ok, syntax_issues = syntax_check(cached_rtl)
    if syntax_ok:
        print("[dual_llm] WARNING: Reviewer failed; using cached reviewed RTL for {}".format(
            module_name))
        return _finalize_dual_llm_rtl(cached_rtl, module_name)

    print("[dual_llm] Cached reviewed RTL failed syntax check: {}".format(
        "; ".join(syntax_issues)))
    return None


def _reviewer_failed_fallback(module_name):
    cached_rtl = _load_reviewed_cache(module_name)
    if cached_rtl:
        return cached_rtl

    print("[dual_llm] ERROR: Reviewer failed and no cache exists for {}".format(
        module_name))
    raise RuntimeError(
        "dual_llm_rtlgen: reviewer unavailable and no cached reviewed RTL exists for {}".format(
            module_name)
    )


# ---------------------------------------------------------------------------
# Structured-response helpers
# ---------------------------------------------------------------------------

_ALLOWED_STATUS = {"VALID", "INVALID", "AMBIGUOUS"}
_ALLOWED_ISSUE_TYPES = {"SPEC_VIOLATION", "NON_ISSUE", "AMBIGUITY"}
_STATUS_ALIASES = {
    "CLEAN": "VALID",
}


def _strip_code_fences(text):
    """Best-effort cleanup if a model wraps RTL in markdown fences."""
    if not text:
        return ""
    text = text.strip()
    match = re.search(r"```(?:systemverilog|verilog|sv)?\s*(.*?)```", text,
                      re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text


def _string_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    text = str(value).strip()
    return [text] if text else []


def _normalize_issue(issue):
    if isinstance(issue, dict):
        issue_type = str(issue.get("type", "AMBIGUITY")).strip().upper()
        description = str(issue.get("description", "")).strip()
        location = str(issue.get("location", "unspecified")).strip()
    else:
        issue_type = "AMBIGUITY"
        description = str(issue).strip()
        location = "unspecified"

    if issue_type not in _ALLOWED_ISSUE_TYPES:
        issue_type = "AMBIGUITY"
    if not description:
        description = "Issue description missing"
    if not location:
        location = "unspecified"

    return {
        "type": issue_type,
        "description": description,
        "location": location
    }


def _make_payload(fallback_rtl, status, issues, invariants=None):
    status = _STATUS_ALIASES.get(status, status)
    if status not in _ALLOWED_STATUS:
        status = "AMBIGUOUS"

    normalized = []
    for issue in issues or []:
        normalized.append(_normalize_issue(issue))

    return {
        "invariants": _string_list(invariants),
        "status": status,
        "issues": normalized,
        "corrected_rtl": _strip_code_fences(fallback_rtl or "")
    }


def _parse_llm_json_payload(response_text, fallback_rtl, label, fallback_invariants=None):
    try:
        response_text = (response_text or "").strip()
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            response_text = json_match.group(0)

        data = json.loads(response_text)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value must be an object")

        payload = _make_payload(
            fallback_rtl,
            str(data.get("status", "AMBIGUOUS")).strip().upper(),
            data.get("issues", []),
            data.get("invariants", fallback_invariants)
        )

        corrected_rtl = _strip_code_fences(data.get("corrected_rtl", ""))
        if corrected_rtl:
            payload["corrected_rtl"] = corrected_rtl
        else:
            payload["issues"].append({
                "type": "AMBIGUITY",
                "description": "{} returned empty corrected_rtl; using fallback RTL".format(label),
                "location": "response.corrected_rtl"
            })

        payload["parse_failed"] = False
        return payload

    except Exception as e:
        print("[dual_llm] Failed to parse {} JSON: {}".format(label, e))
        if response_text:
            preview = response_text[:RAW_RESPONSE_PREVIEW_CHARS]
            suffix = "\n... [truncated]" if len(response_text) > RAW_RESPONSE_PREVIEW_CHARS else ""
            print("Raw response (first {} chars):\n{}{}".format(
                RAW_RESPONSE_PREVIEW_CHARS, preview, suffix))
        payload = _make_payload(
            fallback_rtl,
            "AMBIGUOUS",
            [{
                "type": "AMBIGUITY",
                "description": "{} returned invalid JSON".format(label),
                "location": "response"
            }],
            fallback_invariants
        )
        payload["parse_failed"] = True
        return payload


def _make_review_payload(fallback_rtl, status, issues, invariants=None,
                         corrected_rtl=""):
    payload = _make_payload(fallback_rtl, status, issues, invariants)
    if corrected_rtl:
        payload["corrected_rtl"] = _strip_code_fences(corrected_rtl)
    payload["parse_failed"] = False
    return payload


def _parse_review_json_payload(response_text, fallback_rtl, label,
                               fallback_invariants=None):
    try:
        response_text = (response_text or "").strip()
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            response_text = json_match.group(0)

        data = json.loads(response_text)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value must be an object")

        return _make_review_payload(
            fallback_rtl,
            str(data.get("status", "AMBIGUOUS")).strip().upper(),
            data.get("issues", []),
            data.get("invariants", fallback_invariants),
            data.get("corrected_rtl", "")
        )

    except Exception as e:
        print("[dual_llm] Failed to parse {} JSON: {}".format(label, e))
        if response_text:
            preview = response_text[:RAW_RESPONSE_PREVIEW_CHARS]
            suffix = "\n... [truncated]" if len(response_text) > RAW_RESPONSE_PREVIEW_CHARS else ""
            print("Raw response (first {} chars):\n{}{}".format(
                RAW_RESPONSE_PREVIEW_CHARS, preview, suffix))
        payload = _make_review_payload(
            fallback_rtl,
            "AMBIGUOUS",
            [{
                "type": "AMBIGUITY",
                "description": "{} returned invalid JSON".format(label),
                "location": "response"
            }],
            fallback_invariants
        )
        payload["parse_failed"] = True
        return payload


def _parse_review_json_compact(response_text, label):
    try:
        response_text = (response_text or "").strip()
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            response_text = json_match.group(0)

        data = json.loads(response_text)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value must be an object")

        status = str(data.get("status", "AMBIGUOUS")).strip().upper()
        status = _STATUS_ALIASES.get(status, status)
        if status not in _ALLOWED_STATUS:
            status = "AMBIGUOUS"

        issues = []
        for issue in (data.get("issues", []) or [])[:5]:
            normalized = _normalize_issue(issue)
            if normalized["type"] != "SPEC_VIOLATION":
                normalized["type"] = "AMBIGUITY"
            issues.append(normalized)

        return {
            "status": status,
            "issues": issues,
            "parse_failed": False
        }

    except Exception as e:
        print("[dual_llm] Failed to parse {} JSON: {}".format(label, e))
        if response_text:
            preview = response_text[:RAW_RESPONSE_PREVIEW_CHARS]
            suffix = "\n... [truncated]" if len(response_text) > RAW_RESPONSE_PREVIEW_CHARS else ""
            print("Raw response (first {} chars):\n{}{}".format(
                RAW_RESPONSE_PREVIEW_CHARS, preview, suffix))
        return {
            "status": "AMBIGUOUS",
            "issues": [{
                "type": "AMBIGUITY",
                "description": "{} returned invalid JSON".format(label),
                "location": "response"
            }],
            "parse_failed": True
        }


def _filter_issues(issues, issue_type):
    return [issue for issue in issues if issue.get("type") == issue_type]


def _issue_signature(issues):
    """Convert structured issues into a deterministic comparable signature."""
    if not issues:
        return tuple()

    signature = []
    for issue in issues:
        normalized = _normalize_issue(issue)
        signature.append(
            "{type}|{location}|{description}".format(
                type=normalized["type"],
                location=normalized["location"],
                description=normalized["description"]
            )
        )
    return tuple(sorted(signature))


def _format_string_block(title, items):
    if not items:
        return "{}\n- (none)".format(title)
    return "{}\n{}".format(
        title,
        "\n".join("- {}".format(item) for item in items)
    )


def _format_issue_block(title, issues):
    if not issues:
        return "{}\n- (none)".format(title)
    lines = [title]
    for issue in issues:
        lines.append(
            "- [{type}] {description} @ {location}".format(
                type=issue.get("type", "AMBIGUITY"),
                description=issue.get("description", ""),
                location=issue.get("location", "unspecified")
            )
        )
    return "\n".join(lines)


def _design_context_bundle(design):
    """Return authoritative context plus convenient prompt fragments."""

    ctx = build_llm_context(design)
    ctx_str = llm_context_to_string(ctx)
    invariants = _string_list(ctx.get("invariants"))
    implementation_constraints = _string_list(
        ctx.get("implementation_constraints")
    )
    forbidden_patterns = _string_list(ctx.get("forbidden_patterns"))
    must_not_assume = []
    validator_alignment = ctx.get("validator_alignment", {})
    if isinstance(validator_alignment, dict):
        must_not_assume = _string_list(validator_alignment.get("must_not_assume"))

    lines = [
        "Design name:  {}".format(design.design_name),
        "Design type:  {}".format(design.design_type),
    ]

    if design.clock:
        lines.append("Clock: {} @ {} MHz".format(
            design.clock.name,
            design.clock.frequency_mhz or "unspecified"
        ))

    if design.reset:
        lines.append("Reset: {} | active_{} | {}".format(
            design.reset.name,
            "low" if design.reset.active_low else "high",
            "synchronous" if design.reset.synchronous else "asynchronous"
        ))

    lines.append("\nSTRUCTURED LLM CONTEXT (authoritative):")
    lines.append(ctx_str)
    lines.append("")
    lines.append(_format_string_block("AUTHORITATIVE INVARIANTS:", invariants))
    lines.append("")
    lines.append(_format_string_block(
        "IMPLEMENTATION CONSTRAINTS:", implementation_constraints))
    lines.append("")
    lines.append(_format_string_block(
        "FORBIDDEN PATTERNS:", forbidden_patterns))
    lines.append("")
    lines.append(_format_string_block(
        "MUST NOT ASSUME:", must_not_assume))

    return {
        "ctx": ctx,
        "ctx_str": ctx_str,
        "prompt_text": "\n".join(lines),
        "invariants": invariants,
        "implementation_constraints": implementation_constraints,
        "forbidden_patterns": forbidden_patterns,
        "must_not_assume": must_not_assume
    }


def _compact_review_context(design):
    ctx = build_llm_context(design)
    compact_keys = [
        "module",
        "clock",
        "reset",
        "request_payload",
        "scheduler_policy",
        "invariants",
        "implementation_constraints",
        "forbidden_patterns",
        "validator_alignment",
    ]
    compact = {
        key: ctx[key]
        for key in compact_keys
        if key in ctx
    }
    return json.dumps(compact, indent=2, sort_keys=True)


def _history_block(history, current_round):
    """
    Format the sliding 2-round window (rounds N-1 and N) as a readable block.
    """
    if not history:
        return "(No previous rounds - this is round 1)"

    window = history[-2:]  # at most last 2 rounds
    parts = []
    for h in window:
        parts.append("=== Round {} ===".format(h["round"]))
        parts.append("LLM #1 status: {}".format(h.get("llm1_status", "(none)")))
        parts.append(_format_issue_block(
            "LLM #1 issues:", h.get("llm1_issues", [])))
        parts.append("--- LLM #1 output ---\n" + h.get("llm1_code", "(none)"))
        parts.append("LLM #2 status: {}".format(h.get("llm2_status", "(none)")))
        parts.append(_format_issue_block(
            "LLM #2 issues:", h.get("llm2_report", [])))
        parts.append("--- LLM #2 corrected code ---\n" + h.get("llm2_code", "(none)"))
        if h.get("merged_status"):
            parts.append("Merge status: {}".format(h["merged_status"]))
        if h.get("merged_issues") is not None:
            parts.append(_format_issue_block(
                "Merge issues:", h.get("merged_issues", [])))
        if h.get("merged_code"):
            parts.append("--- Merged output accepted for this round ---\n" + h["merged_code"])
    return "\n\n".join(parts)


# System prompts — written once, reused every round
_SYS_GENERATOR = """\
You are an expert RTL engineer specialising in synthesisable SystemVerilog.
Your job is to generate correct, spec-locked SystemVerilog from a validated design IR.

Return ONLY valid JSON in this exact shape:
{
  "invariants": ["..."],
  "status": "VALID | INVALID | AMBIGUOUS",
  "issues": [
    {
      "type": "SPEC_VIOLATION | NON_ISSUE | AMBIGUITY",
      "description": "...",
      "location": "..."
    }
  ],
  "corrected_rtl": "..."
}

Rules:
- Treat STRUCTURED LLM CONTEXT as the ONLY source of truth.
- Do NOT use prior DDR, DRAM, JEDEC, or tFAW knowledge unless it is explicitly present in STRUCTURED LLM CONTEXT.
- Do NOT infer unstated behavior.
- Do NOT introduce alternate architectures.
- Do NOT introduce implicit aging or decrement logic unless explicitly specified.
- Any expression involving parameters or constants MUST be explicitly sized to match the destination signal.
- Do not rely on implicit truncation or width expansion; use explicit casts, sized literals, or width-matched localparams.
- For request queues, define localparam int REQUEST_WIDTH = 51.
- For queue indices, define localparam int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH).
- Never cast a packed request struct with logic'(some_request_struct); use direct assignment when widths match or REQUEST_WIDTH'(expr) when an explicit packed request-width cast is needed.
- Do not hardcode queue index width as 2; use SEL_WIDTH for sel_idx, first_free_idx, insert_idx, and loop-derived index casts.
- Reset behavior must match STRUCTURED LLM CONTEXT exactly.
- Preserve the stated implementation constraints and forbidden patterns exactly.
- Use logic types (not reg/wire) throughout.
- Declare all signals before use.
- Use always_ff for sequential logic and always_comb for combinational logic.
- Do not declare module-scope loop variables for procedural for-loops.
- Use block-local loop variables: for (int i = 0; i < N; i++) begin.
- Do not reuse the same loop variable across multiple always_comb/always_ff blocks.
- Loop variables used only inside procedural blocks are not architectural signals.
- Every module must end with endmodule.
- Do not reference Python dataclasses or IR objects in the RTL.
- "invariants" must echo the authoritative invariants you applied.
- "status" must be VALID if corrected_rtl fully satisfies context, INVALID if you know it violates context,
  or AMBIGUOUS if the context itself is insufficient to decide.
- "issues" should usually be empty for generation unless the spec is ambiguous or conflicting.
- "corrected_rtl" must contain the full SystemVerilog module only, with no markdown fences.
- Do not use localparams in ANSI port declarations. Any symbol used in a port width must be a literal, a package-visible type/constant, or a parameter declared in the module parameter list.
"""

_SYS_REVIEWER = """\
You are a strict RTL reviewer.

Return ONLY valid compact JSON in this exact format:

{
  "status": "VALID | INVALID | AMBIGUOUS",
  "issues": [
    {
      "type": "SPEC_VIOLATION | AMBIGUITY",
      "description": "...",
      "location": "..."
    }
  ]
}

Rules:
- STRUCTURED LLM CONTEXT is the ONLY source of truth.
- Explicitly forbid use of prior DDR, DRAM, JEDEC, or tFAW knowledge.
- Flag SPEC_VIOLATION for any logic mismatch grounded in STRUCTURED LLM CONTEXT.
- Treat implicit width truncation/expansion as a SPEC_VIOLATION when constants or parameters are not explicitly resized.
- Treat logic'(some_request_struct) as a SPEC_VIOLATION because it truncates a 51-bit request payload to 1 bit.
- Treat hardcoded queue index width such as DEPTH_W = 2 as a SPEC_VIOLATION when DEPTH is parameterized.
- Treat simultaneous issue_ref and issue_txn assertions as a SPEC_VIOLATION when scheduler context requires refresh priority or mutual exclusion.
- Treat acquiring a new transaction lock while ref_req is high as a SPEC_VIOLATION when scheduler context requires refresh priority.
- Treat acquiring a new transaction lock for an immediately issued candidate as a SPEC_VIOLATION.
- Treat module-scope procedural loop variables reused across always_comb/always_ff blocks as a SPEC_VIOLATION.
- Do not classify block-local loop variables declared inside procedural for-loops as architectural signals.
- Classify every finding as SPEC_VIOLATION or AMBIGUITY.
- Only mark something SPEC_VIOLATION if STRUCTURED LLM CONTEXT explicitly supports it.
- If STRUCTURED LLM CONTEXT is incomplete or conflicting, classify that as AMBIGUITY.
- Do not include corrected RTL unless explicitly requested.
- Do not echo all invariants.
- List at most 5 issues.
- Each issue description must be one sentence.
- If there are no SPEC_VIOLATION issues, return status VALID and issues [].
- Flag use of localparams in ANSI port widths as a SPEC_VIOLATION unless the symbol is declared as a module parameter before the port list.
"""

_SYS_REVIEWER_COMPACT = """\
You are a strict RTL reviewer.

Return ONLY valid compact JSON in this exact format:
{
  "status": "VALID | INVALID | AMBIGUOUS",
  "issues": [
    {
      "type": "SPEC_VIOLATION | AMBIGUITY",
      "description": "...",
      "location": "..."
    }
  ]
}

Rules:
- Use only the provided structured context.
- Treat logic'(some_request_struct) as a SPEC_VIOLATION because it truncates a 51-bit request payload to 1 bit.
- Treat hardcoded queue index width such as DEPTH_W = 2 as a SPEC_VIOLATION when DEPTH is parameterized.
- Treat simultaneous issue_ref and issue_txn assertions as a SPEC_VIOLATION when scheduler context requires refresh priority or mutual exclusion.
- Treat acquiring a new transaction lock while ref_req is high as a SPEC_VIOLATION when scheduler context requires refresh priority.
- Treat acquiring a new transaction lock for an immediately issued candidate as a SPEC_VIOLATION.
- Treat module-scope procedural loop variables reused across always_comb/always_ff blocks as a SPEC_VIOLATION.
- Do not classify block-local loop variables declared inside procedural for-loops as architectural signals.
- Do not include corrected RTL.
- Do not echo invariants or context.
- List at most 5 issues.
- Each issue description must be one sentence.
- If there are no SPEC_VIOLATION issues, return status VALID and issues [].
"""

_SYS_MERGER = """\
You are an expert RTL engineer.
You will receive two versions of a SystemVerilog module plus a typed review report.

Return ONLY valid JSON in this exact format:
{
  "invariants": ["..."],
  "status": "VALID | INVALID | AMBIGUOUS",
  "issues": [
    {
      "type": "SPEC_VIOLATION | NON_ISSUE | AMBIGUITY",
      "description": "...",
      "location": "..."
    }
  ],
  "corrected_rtl": "<full SystemVerilog module>"
}

Rules:
- Treat STRUCTURED LLM CONTEXT as the ONLY source of truth.
- You MUST fix ALL reviewer issues that are grounded in STRUCTURED LLM CONTEXT.
- If an issue is labeled SPEC_VIOLATION, you MUST correct the logic exactly.
- Do NOT preserve any previous logic that conflicts with reviewer feedback.
- If an issue appears again, you MUST directly modify the exact offending logic.
- Do NOT ignore or partially fix issues.
- Apply ONLY fixes labeled SPEC_VIOLATION and only when they are grounded in STRUCTURED LLM CONTEXT.
- IGNORE NON_ISSUE findings and any suggestion that contradicts STRUCTURED LLM CONTEXT.
- IGNORE AMBIGUITY findings rather than inventing behavior.
- Preserve existing correct RTL from Version A whenever possible.
- Do NOT introduce alternate architectures.
- Do NOT introduce decrement logic, background aging, or wraparound handling unless explicitly required by STRUCTURED LLM CONTEXT.
- Preserve explicit width matching on every parameter/constant arithmetic, comparison, and assignment.
- For request queues, preserve localparam int REQUEST_WIDTH = 51.
- For queue indices, preserve localparam int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH).
- Never cast a packed request struct with logic'(some_request_struct); use direct assignment when widths match or REQUEST_WIDTH'(expr) when an explicit packed request-width cast is needed.
- Do not hardcode queue index width as 2; use SEL_WIDTH for sel_idx, first_free_idx, insert_idx, and loop-derived index casts.
- Do not declare module-scope loop variables for procedural for-loops.
- Use block-local loop variables: for (int i = 0; i < N; i++) begin.
- Do not reuse the same loop variable across multiple always_comb/always_ff blocks.
- Loop variables used only inside procedural blocks are not architectural signals.
- Reset behavior must remain exactly aligned to STRUCTURED LLM CONTEXT.
- "invariants" must echo the authoritative invariants preserved by the merge.
- "corrected_rtl" must contain the full merged SystemVerilog module only, with no markdown fences.
"""


# ---------------------------------------------------------------------------
# LLM #1: generate (round 1) or syntax-fix
# ---------------------------------------------------------------------------

def _llm1_generate(design, yaml_text, history, syntax_issues=None, previous_rtl=""):
    """
    LLM #1 call for initial generation or syntax repair.
    Returns parsed JSON payload.
    """
    bundle = _design_context_bundle(design)
    hist = _history_block(history, len(history) + 1)

    if syntax_issues:
        user_msg = (
            "The RTL you previously generated has syntax errors. Fix them without changing any behavior"
            " that already matches STRUCTURED LLM CONTEXT.\n\n"
            "SYNTAX ERRORS:\n{errors}\n\n"
            "CURRENT RTL:\n{current_rtl}\n\n"
            "DESIGN CONTEXT:\n{ctx}\n\n"
            "PREVIOUS ATTEMPTS (last 2 rounds):\n{hist}\n\n"
            "Return JSON only."
        ).format(
            errors="\n".join(syntax_issues),
            current_rtl=previous_rtl,
            ctx=bundle["prompt_text"],
            hist=hist
        )
    else:
        user_msg = (
            "Generate synthesisable SystemVerilog for the following design.\n\n"
            "DESIGN CONTEXT:\n{ctx}\n\n"
            "PREVIOUS ATTEMPTS (last 2 rounds - incorporate only spec-grounded lessons):\n{hist}\n\n"
            "Return JSON only."
        ).format(ctx=bundle["prompt_text"], hist=hist)

    response = _call_llm(LLM1_MODEL, _SYS_GENERATOR, user_msg, label="LLM1-Generate")
    fallback_rtl = previous_rtl if previous_rtl else ""
    return _parse_llm_json_payload(
        response, fallback_rtl, "LLM1-Generate", bundle["invariants"]
    )


# ---------------------------------------------------------------------------
# LLM #2: review + fix
# ---------------------------------------------------------------------------

def _llm2_review(design, yaml_text, rtl_code, history):
    """
    LLM #2 call: reviews rtl_code, returns parsed JSON payload.
    """
    bundle = _design_context_bundle(design)
    hist = _history_block(history, len(history) + 1)

    user_msg = (
        "Review the following SystemVerilog module for correctness and alignment with"
        " STRUCTURED LLM CONTEXT only.\n\n"
        "DESIGN CONTEXT:\n{ctx}\n\n"
        "CODE TO REVIEW:\n{code}\n\n"
        "PREVIOUS ROUNDS (context):\n{hist}\n\n"
        "Return compact JSON only."
    ).format(ctx=bundle["prompt_text"], code=rtl_code, hist=hist)

    response = _call_llm(LLM2_MODEL, _SYS_REVIEWER, user_msg, label="LLM2-Review")
    return _parse_review_json_payload(
        response, rtl_code, "LLM2-Review", bundle["invariants"]
    )


def _llm2_review_compact(design, rtl_code):
    """
    Retry reviewer with a smaller prompt and a smaller response schema.
    """
    user_msg = (
        "Review this RTL against the structured context only.\n\n"
        "STRUCTURED CONTEXT:\n{ctx}\n\n"
        "RTL:\n{code}\n\n"
        "Return only the compact JSON object. At most 5 issues."
    ).format(
        ctx=_compact_review_context(design),
        code=rtl_code
    )

    response = _call_llm(
        LLM2_MODEL,
        _SYS_REVIEWER_COMPACT,
        user_msg,
        label="LLM2-Review-Compact"
    )
    return _parse_review_json_compact(response, "LLM2-Review-Compact")



# ---------------------------------------------------------------------------
# LLM #1: merge
# ---------------------------------------------------------------------------

def _llm1_merge(design, yaml_text, llm1_code, llm2_code, review_report, history,
                repeated_issue=False):
    """
    LLM #1 merger call: receives both code versions + report, produces merged RTL.
    """
    bundle = _design_context_bundle(design)
    hist = _history_block(history, len(history) + 1)
    review_issues = review_report.get("issues", [])
    issues_text = _format_issue_block("Reviewer Issues:", review_issues)

    merge_prompt = (
        "You are fixing SystemVerilog RTL based on a formal review.\n\n"
        "STRICT RULES:\n\n"
        "1. You MUST fix ALL issues listed by the reviewer.\n"
        "2. If an issue is labeled SPEC_VIOLATION, you MUST correct the logic exactly.\n"
        "3. Do NOT preserve any previous logic that conflicts with reviewer feedback.\n"
        "4. If an issue appears again, you MUST directly modify the exact offending logic.\n"
        "5. Do NOT ignore or partially fix issues.\n"
        "6. Return COMPLETE corrected RTL (no partial edits).\n"
        "7. Output ONLY valid SystemVerilog in corrected_rtl (no explanations outside JSON).\n\n"
        "Your goal is to produce RTL that will PASS the reviewer with ZERO issues.\n\n"
        "DESIGN CONTEXT:\n{ctx}\n\n"
        "{issues_text}\n\n"
        "Previous RTL:\n{previous_rtl}\n\n"
        "Reviewer Suggested RTL (if any):\n{reviewer_rtl}\n\n"
        "PREVIOUS ROUNDS (context):\n{hist}\n\n"
        "Return JSON only."
    ).format(
        ctx=bundle["prompt_text"],
        issues_text=issues_text,
        previous_rtl=llm1_code,
        reviewer_rtl=llm2_code,
        hist=hist
    )

    if repeated_issue:
        merge_prompt += (
            "\n\nESCALATION MODE:\n\n"
            "The same issue has appeared multiple times.\n\n"
            "You MUST:\n"
            "- Identify the exact line causing the issue\n"
            "- Rewrite that logic completely\n"
            "- Do NOT reuse the incorrect pattern\n"
            "- Ensure the issue cannot reappear\n\n"
            "You are REQUIRED to change the structure of the logic, not just small edits.\n"
        )

    response = _call_llm(LLM1_MODEL, _SYS_MERGER, merge_prompt, label="LLM1-Merge")
    return _parse_llm_json_payload(
        response, llm1_code, "LLM1-Merge", bundle["invariants"]
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_dual_llm_rtlgen(design, yaml_text):
    """
    Run the dual-LLM RTL generation loop.

    Args:
        design:    validated Design IR object (from validator.validate_spec)
        yaml_text: raw YAML string (for context injection)

    Returns:
        str — final SystemVerilog RTL string

    Raises:
        RuntimeError if all rounds fail to produce syntactically valid RTL
    """
    history = []        # list of round dicts (sliding window source)
    issue_history = []  # reviewer issue signatures for convergence tracking

    print("[dual_llm] Starting dual-LLM loop | max_rounds={} | LLM1={} | LLM2={}".format(
        MAX_ROUNDS, LLM1_MODEL, LLM2_MODEL))

    for round_num in range(1, MAX_ROUNDS + 1):
        print("\n[dual_llm] ── Round {}/{} ──────────────────────────────".format(
            round_num, MAX_ROUNDS))

        round_record = {"round": round_num}

        # ── Step 1: LLM #1 generates (or continues from merge context) ──
        print("[dual_llm] Step 1: LLM #1 generating...")
        llm1_payload = _llm1_generate(design, yaml_text, history)
        llm1_code = enforce_width_safety(llm1_payload["corrected_rtl"], design.design_name)
        round_record["llm1_status"] = llm1_payload["status"]
        round_record["llm1_issues"] = llm1_payload["issues"]

        # ── Step 2: Syntax check; retry up to MAX_SYNTAX_RETRIES ──
        syntax_ok, syntax_issues = syntax_check(llm1_code)
        for attempt in range(MAX_SYNTAX_RETRIES):
            if syntax_ok:
                break
            print("[dual_llm] Step 2: Syntax issues (attempt {}/{}): {}".format(
                attempt + 1, MAX_SYNTAX_RETRIES, "; ".join(syntax_issues)))
            llm1_payload = _llm1_generate(
                design, yaml_text, history,
                syntax_issues=syntax_issues,
                previous_rtl=llm1_code
            )
            llm1_code = enforce_width_safety(llm1_payload["corrected_rtl"], design.design_name)
            round_record["llm1_status"] = llm1_payload["status"]
            round_record["llm1_issues"] = llm1_payload["issues"]
            syntax_ok, syntax_issues = syntax_check(llm1_code)

        if not syntax_ok:
            print("[dual_llm] Step 2: Syntax still failing after retries. "
                  "Proceeding to reviewer anyway.")
        else:
            print("[dual_llm] Step 2: Syntax OK.")

        round_record["llm1_code"] = llm1_code

        # ── Step 3: LLM #2 reviews ──
        print("[dual_llm] Step 3: LLM #2 reviewing...")
        try:
            review_payload = _llm2_review(design, yaml_text, llm1_code, history)
        except RuntimeError as e:
            print("[dual_llm] Reviewer failed: {}".format(e))
            print("[dual_llm] Reviewer unavailable; using cached fallback...")
            return _reviewer_failed_fallback(design.design_name)

        if review_payload.get("parse_failed"):
            print("[dual_llm] Reviewer JSON invalid; retrying compact review...")
            try:
                compact_review = _llm2_review_compact(design, llm1_code)
            except RuntimeError as e:
                print("[dual_llm] Compact reviewer failed: {}".format(e))
                print("[dual_llm] Reviewer unavailable; using cached fallback...")
                return _reviewer_failed_fallback(design.design_name)
            print("[dual_llm] Compact review status = {}".format(
                compact_review["status"]))
            if compact_review.get("parse_failed"):
                print("[dual_llm] Reviewer unavailable; using cached fallback...")
                return _reviewer_failed_fallback(design.design_name)
            review_payload = _make_review_payload(
                llm1_code,
                compact_review["status"],
                compact_review["issues"],
                []
            )

        review_issues = review_payload["issues"]
        llm2_corrected = enforce_width_safety(review_payload["corrected_rtl"], design.design_name)
        issue_signature = _issue_signature(review_issues)
        issue_history.append(issue_signature)
        repeated_issue = False
        if len(issue_history) >= 2 and issue_history[-1] == issue_history[-2]:
            repeated_issue = True
        spec_violations = _filter_issues(review_issues, "SPEC_VIOLATION")
        ambiguities = _filter_issues(review_issues, "AMBIGUITY")
        reviewer_valid = (
            review_payload["status"] == "VALID" and
            not spec_violations and
            not ambiguities
        )

        round_record["llm2_report"] = review_issues
        round_record["llm2_code"]   = llm2_corrected
        round_record["llm2_status"] = review_payload["status"]

        print("[dual_llm] Step 3: Reviewer status = {}".format(
            review_payload["status"]))
        if review_issues:
            issues_preview = " | ".join(
                "[{type}] {description}".format(
                    type=issue.get("type", "AMBIGUITY"),
                    description=issue.get("description", "")
                )
                for issue in review_issues
            )
            print("[dual_llm]   Issues: {}{}".format(
                issues_preview[:400],
                "..." if len(issues_preview) > 400 else ""
            ))
        if repeated_issue:
            print("[dual_llm] Repeated issue detected — escalation mode enabled")

        # ── Step 4: If issues, LLM #1 merges both versions ──
        if reviewer_valid:
            # Reviewer is happy — use LLM #1's syntax-checked code
            print("[dual_llm] Step 4: Skipped (reviewer clean). Accepting output.")
            if syntax_ok:
                final_rtl = llm1_code
            else:
                review_syntax_ok, review_syntax_issues = syntax_check(llm2_corrected)
                if review_syntax_ok:
                    final_rtl = llm2_corrected
                else:
                    print("[dual_llm] Step 4: Reviewer RTL also has syntax issues: {}".format(
                        "; ".join(review_syntax_issues)))
                    final_rtl = llm1_code
            final_syntax_ok, final_syntax_issues = syntax_check(final_rtl)
            if not final_syntax_ok:
                print("[dual_llm] Step 4: Reviewed RTL has syntax issues: {}".format(
                    "; ".join(final_syntax_issues)))
                print("[dual_llm] Reviewer unavailable; using cached fallback...")
                return _reviewer_failed_fallback(design.design_name)
            round_record["merged_code"] = final_rtl
            round_record["merged_status"] = "VALID"
            round_record["merged_issues"] = []
            history.append(round_record)
            print("[dual_llm] ---GOOD---: Converged at round {}.".format(round_num))
            return _write_reviewed_cache(design.design_name, final_rtl)
        elif not spec_violations:
            print("[dual_llm] Step 4: Skipped merge because reviewer found no SPEC_VIOLATION items.")
            merged = llm1_code if syntax_ok else llm2_corrected
            round_record["merged_code"] = merged
            round_record["merged_status"] = "SKIPPED_NO_SPEC_VIOLATION"
            round_record["merged_issues"] = review_issues
        else:
            print("[dual_llm] Step 4: LLM #1 merging both versions...")
            merge_payload = _llm1_merge(
                design, yaml_text,
                llm1_code, llm2_corrected,
                review_payload, history,
                repeated_issue=repeated_issue
            )
            merged = enforce_width_safety(merge_payload["corrected_rtl"], design.design_name)
            round_record["merged_status"] = merge_payload["status"]
            round_record["merged_issues"] = merge_payload["issues"]

            # Syntax-check the merge too
            merge_ok, merge_issues = syntax_check(merged)
            if merge_ok:
                print("[dual_llm] Step 4: Merged output passes syntax check.")
            else:
                print("[dual_llm] Step 4: Merged output has syntax issues: {}".format(
                    "; ".join(merge_issues)))
                # Prefer reviewer-corrected RTL when fixes are required.
                review_syntax_ok, review_syntax_issues = syntax_check(llm2_corrected)
                if review_syntax_ok:
                    print("[dual_llm] Step 4: Falling back to reviewer-corrected RTL.")
                    merged = llm2_corrected
                elif syntax_ok:
                    print("[dual_llm] Step 4: Reviewer RTL also has syntax issues: {}".format(
                        "; ".join(review_syntax_issues)))
                    merged = llm1_code

            round_record["merged_code"] = merged
            if repeated_issue:
                repeated_syntax_ok, repeated_syntax_issues = syntax_check(merged)
                if repeated_syntax_ok:
                    history.append(round_record)
                    print("[dual_llm] Escalation applied — accepting result")
                    return _finalize_dual_llm_rtl(merged, design.design_name)
                print("[dual_llm] Escalation result still has syntax issues: {}".format(
                    "; ".join(repeated_syntax_issues)))

        # ── Slide the history window ──
        history.append(round_record)
        if len(history) > 2:
            history = history[-2:]

    # ── Exhausted all rounds ──
    print("\n[dual_llm] ---CAUTION----: MAX_ROUNDS ({}) reached without clean reviewer pass.".format(
        MAX_ROUNDS))
    print("[dual_llm] Reviewer did not approve fresh RTL; using cached fallback if available.")
    return _reviewer_failed_fallback(design.design_name)


# ---------------------------------------------------------------------------
# CLI entry point  (mirrors design.py's usage pattern)
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 dual_llm_rtlgen.py <spec.yaml> [output.sv]")
        sys.exit(1)

    yaml_path = sys.argv[1]
    out_path  = sys.argv[2] if len(sys.argv) > 2 else None

    # Import validator — must be run from project root
    try:
        from validator import validate_spec
    except ImportError:
        print("ERROR: cannot import validator. Run from the project root directory.")
        sys.exit(1)

    # Load raw YAML text for context injection
    with open(yaml_path, "r") as f:
        yaml_text = f.read()

    print("[dual_llm] Validating spec: {}".format(yaml_path))
    design = validate_spec(yaml_path)
    print("[dual_llm] IR validated: {} ({})".format(
        design.design_name, design.design_type))

    rtl = run_dual_llm_rtlgen(design, yaml_text)

    if not out_path:
        out_dir = "rtl_output"
        out_path = os.path.join(out_dir, design.design_name + ".sv")

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(out_path, "w") as fh:
        fh.write(rtl)

    print("Written: {}".format(out_path))


if __name__ == "__main__":
    main()
