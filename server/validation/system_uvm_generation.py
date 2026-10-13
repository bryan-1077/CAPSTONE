"""System-specific UVM generation, integrity and reuse policy for Agent 2.

Shares Agent 2's LLM client, response/edit parser, logging and VCS APIs. The
model authors UVM infrastructure, never reference-model or traffic semantics.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from system_uvm_audit import compare_structure


BEGIN = '// BEGIN SYSTEM UVM INFRASTRUCTURE'
END = '// END SYSTEM UVM INFRASTRUCTURE'
COMPONENTS = ('dut_item', 'dut_sequence', 'dut_sequencer', 'dut_driver',
              'dut_monitor', 'dut_agent', 'dut_scoreboard', 'dut_env', 'dut_test')
SYSTEM_PROMPT = """You are Agent 2, generating/adapting a full-controller VCS UVM
environment from approved metadata. Author explicit dut_sequencer, dut_agent,
dut_env and dut_test infrastructure around the supplied trusted components.
Return only JSON {"edits":[{"file":"dut_pkg.sv","old":"exact source",
"new":"replacement SystemVerilog"}]}. Replace the complete marked SYSTEM UVM
INFRASTRUCTURE region, including its boundary comments, even when retaining
the reviewed scaffolding. Do not return the large reference model again.
Preserve the class names, typed hierarchy, constructors, factory registration,
phase methods, connections, vif propagation, objection lifecycle, sequence start,
scoreboard finalization and SYSTEM_FAILED fatal. Component creation in build_phase
may use type_id::create or typed new and may be reordered. Comments/formatting
are free. Handle names, split declaration/assignment, super.connect_phase and
explicit helpers with empty or uvm_phase arguments are supported. Preserve
create-before-vif-before-start, raise-before-start, finalize-before-fatal-before-drop.
No new runtime hooks, overrides, configuration, report suppression,
severity policy, assertions, expected values, traffic or hierarchical observations.
All source outside this region is trusted and immutable. No RTL edits. Contract,
configuration and SYS-REQ catalog determine behavior; never reinterpret them to
make failures pass. Compile repair is only for generated verification syntax/
types/connectivity. Simulation failures are returned to the frontend without
testbench repair. Use uvm_pkg, the supplied VCS UVM flags, explicit virtual dut_if,
factory-compatible constructors and the supplied class hierarchy.
"""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def tokens(api, source):
    # Retain strings, operators and macro tokens; ignore ONLY comments/spacing.
    clean = api._sv_without_comments(source)
    return re.findall(r'"(?:\\.|[^"\\])*"|[a-zA-Z_$][\w$]*|\d+|'
                      r'===|!==|<<<|>>>|::|&&|\|\||<=|>=|==|!=|<<|>>|\+\+|--|'
                      r'\+=|-=|\*=|/=|%=|&=|\|=|\^=|[^\s]', clean)


def infrastructure(source):
    if source.count(BEGIN) != 1 or source.count(END) != 1:
        raise ValueError('System infrastructure boundaries must each occur exactly once.')
    before, rest = source.split(BEGIN)
    region, after = rest.split(END)
    return before, region, after


def inspect_environment(api, files, trusted):
    failures = {key: [] for key in ('required_classes', 'trusted_checker', 'topology',
                                    'lifecycle', 'interface_contract')}
    words = tokens(api, files.get('dut_pkg.sv', ''))
    classes = [words[index + 1] for index, word in enumerate(words[:-1]) if word == 'class']
    if set(files) != set(trusted):
        failures['required_classes'].append('SOURCE_SET: requires exactly ' + ', '.join(sorted(trusted)))
    for name in COMPONENTS:
        if classes.count(name) != 1:
            failures['required_classes'].append('REQUIRED_CLASS: requires one explicit ' + name)
    for name in ('dut_if.sv', 'dut_tb_top.sv'):
        if files.get(name) != trusted[name]:
            failures['interface_contract'].append('INTERFACE_CONTRACT: trusted sampling/observability changed: ' + name)
    try:
        before, region, after = infrastructure(files.get('dut_pkg.sv', ''))
        original_before, original_region, original_after = infrastructure(trusted['dut_pkg.sv'])
        if before != original_before or after != original_after:
            failures['trusted_checker'].append('TRUSTED_CHECKER: scoreboard, reference model or traffic bytes changed')
        structure = compare_structure(tokens(api, region), tokens(api, original_region))
        for key in structure:
            failures[key].extend(structure[key])
    except ValueError as exc:
        failures['trusted_checker'].append('TRUSTED_BOUNDARIES: ' + str(exc))
    return {'passed': not any(failures.values()), 'policy': 'trusted_semantics_and_uvm_structure_v2',
            'checks': {key: 'FAIL' if value else 'PASS' for key, value in failures.items()},
            'failures': failures, 'classes_detected': classes,
            'trusted_scaffolding_hash': digest(trusted), 'components': list(COMPONENTS)}


def audit_diagnostics(audit):
    return '\n'.join('{}: {}'.format(key, failure) for key, values in audit['failures'].items()
                     for failure in values)


def audit_environment(api, files, trusted):
    audit = inspect_environment(api, files, trusted)
    if not audit['passed']:
        raise ValueError(audit_diagnostics(audit))
    return audit


def generation_inputs(contract, requirements, approved, manifest):
    return {'top_module': contract['top_module'], 'ports': approved['dut_spec']['ports'],
            'clock': contract['clock'], 'reset': contract['reset'],
            'rtl_files': approved['rtl_files'],
            'rtl_metadata': {key: approved.get(key) for key in
                             ('module_definitions', 'generated_modules', 'reachable_modules',
                              'unused_generated_modules', 'dut_spec')},
            'manifest': manifest, 'configuration': contract['configuration'],
            'controller_contract': {k: v for k, v in contract.items() if not k.startswith('_')},
            'requirements': requirements, 'components': list(COMPONENTS),
            'trusted_rules': [
                'Accept only txn_valid && txn_ready at the consuming rising edge.',
                'Reconstruct queued identity; permit arbitrary pre-dispatch reordering.',
                'Commit accepted stimulus data only at active write completion_fire.',
                'Read expectations use associative bank/address memory in observed service order.',
                'Never derive expectations from DUT outputs or internal DUT memory.',
                'Reset cancels queued/active/pending responses and clears reference state.',
                'Preserve every traffic scenario, comparison, timing and final accounting check.']}


def generate_candidate(api, report, inputs, trusted, previous, attempt, error=None):
    sources = previous or trusted
    prompt = ('Generate/adapt the system UVM infrastructure.\nAPPROVED INPUTS:\n' +
              json.dumps(inputs, sort_keys=True, indent=2) + '\nTRUSTED SCAFFOLDING / CURRENT CANDIDATE:\n' +
              '\n'.join('=== filename: {} ===\n{}'.format(name, code) for name, code in sources.items()))
    if error:
        prompt += '\nEXACT PREVIOUS TESTBENCH COMPILE / INTEGRITY DIAGNOSTIC (repair only permitted code):\n' + error
    prompt_name = 'generation/prompt_{}.txt'.format(attempt)
    report.artifact(prompt_name, text=SYSTEM_PROMPT + '\n' + prompt)
    api.log('UVM_TB_AGENT', 'System UVM {} attempt {}'.format('repair' if error else 'generation', attempt + 1), 'info')
    raw = api.call_llm(SYSTEM_PROMPT, prompt, 'UVM_TB_AGENT')
    raw_path = api.save_raw_llm_response(raw, attempt, suffix='_system')
    report.artifact('generation/raw_{}.txt'.format(attempt), source=raw_path)
    report.artifact('generation/raw_{}.json'.format(attempt), source=raw_path + '.json')
    clean = api.strip_markdown_fence(re.sub(r'<think>.*?</think>', '', str(raw), flags=re.S)).strip()
    parse_error = None
    try:
        if clean.startswith('{'):
            files = dict(api.apply_audit_edits(list(sources.items()), clean))
        else:
            updates = api.parse_file_blocks(raw)
            if not updates or any(name not in trusted for name, _ in updates):
                raise ValueError('System generation needs JSON edits or recognized TB file blocks.')
            files = dict(sources, **dict(updates))
    except (ValueError, KeyError, TypeError) as exc:
        files = dict(sources)
        parse_error = 'GENERATION_RESPONSE: ' + str(exc)
    folder = 'generation/attempt_{}'.format(attempt + 1)
    report.artifact(folder + '/llm_raw.txt', source=raw_path)
    report.artifact(folder + '/llm_raw.json', source=raw_path + '.json')
    report.artifact(folder + '/prompt.txt', text=SYSTEM_PROMPT + '\n' + prompt)
    for name, source in files.items():
        if name in trusted:
            report.artifact(folder + '/' + name, text=source)
    audit = inspect_environment(api, files, trusted)
    if parse_error:
        audit['passed'] = False
        audit['checks']['required_classes'] = 'FAIL'
        audit['failures']['required_classes'].append(parse_error)
    report.artifact(folder + '/audit.json', data=audit)
    return files, audit, {'prompt_hash': digest([SYSTEM_PROMPT, prompt]),
                          'input_hash': digest(inputs), 'raw_response': raw_path,
                          'timestamp': datetime.now(timezone.utc).isoformat(),
                          'model': os.getenv('TAMU_MODEL')}


def compile_failure_classification(output, tb_files, rtl_files):
    """Only repair diagnostics positively localized to generated TB sources.

    A tool/license failure, unlocated error or mixed RTL/TB diagnostic never
    authorizes model edits. Agent 1 still owns RTL elaboration approval.
    """
    output = output or ''
    starts = [match.start() for match in re.finditer(r'^\s*Error-\[', output, re.M)]
    errors = [output[start:stop] for start, stop in zip(starts, starts[1:] + [len(output)])]
    if not errors:
        errors = [line for line in (output or '').splitlines() if re.search(r'\berror\b', line, re.I)]
    def mentions(block, paths):
        return any(re.search(r'(?<![\w])' + re.escape(Path(p).name) + r'(?![\w])', block) for p in paths)
    if any(mentions(block, rtl_files) for block in errors):
        return 'RTL_COMPILE_FAILURE'
    if errors and all(mentions(block, tb_files) for block in errors):
        return 'TESTBENCH_COMPILE_FAILURE'
    return 'VERIFICATION_TOOL_FAILURE'


def validate_saved_environment(api, saved, contract, requirements, trusted):
    if not saved or saved.get('mode') != 'system' or saved.get('dut_top_module') != contract['top_module']:
        raise ValueError('Saved environment is not for the approved system top.')
    if not saved.get('checker_compile_valid') or not saved.get('checker_reuse_eligible'):
        raise ValueError('Saved checker is not compile-valid and reuse-eligible.')
    if (saved.get('source_manifest_sha256') != api.file_sha256(contract['sources']['manifest']) or
            saved.get('configuration') != contract['configuration'] or
            saved.get('configuration_hash') != digest(contract['configuration'])):
        raise ValueError('Saved manifest/configuration changed.')
    for row in saved['rtl_files'] + saved.get('source_metadata', []) + saved.get('approved_module_specs', []):
        if not Path(row['path']).is_file() or api.file_sha256(row['path']) != row['sha256']:
            raise ValueError('Saved source hash changed: ' + row['path'])
    for row in saved.get('approved_module_specs', []):
        if not Path(row['source']).is_file() or api.file_sha256(row['source']) != row['sha256']:
            raise ValueError('Saved source hash changed: ' + row['source'])
    if {row['path'] for row in saved['rtl_files']} != set(contract['_rtl_files']):
        raise ValueError('Saved RTL inventory changed.')
    public = {k: v for k, v in contract.items() if not k.startswith('_')}
    stored_contract = json.loads(Path(saved['controller_verification_contract']).read_text())
    catalog = json.loads(Path(saved['requirement_catalog']).read_text())
    if stored_contract != public or saved.get('contract_hash') != digest(public):
        raise ValueError('Saved controller contract changed.')
    if catalog != requirements or saved.get('requirement_hash') != digest(requirements):
        raise ValueError('Saved SYS requirements changed.')
    files = saved['testbench_files']
    if len(files) != len(trusted) or {Path(row['path']).name for row in files} != set(trusted):
        raise ValueError('Saved UVM source set is incomplete.')
    for row in files:
        if api.file_sha256(row['path']) != row['sha256']:
            raise ValueError('Saved checker source changed: ' + row['path'])
    # checker_identity uses the existing shared encoding, not digest().
    identity = hashlib.sha256(json.dumps([{'name': Path(row['path']).name, 'sha256': row['sha256']}
                                         for row in files], sort_keys=True).encode()).hexdigest()
    if identity != saved['checker_identity']:
        raise ValueError('Saved checker identity changed.')
    audit_environment(api, {Path(row['path']).name: Path(row['path']).read_text() for row in files}, trusted)
    if saved.get('generation', {}).get('audit', {}).get('trusted_scaffolding_hash') != digest(trusted):
        raise ValueError('Trusted checker/scaffolding revision changed.')
    return saved


def select_saved_environment(api, report, contract, requirements, trusted, explicit=None):
    reports = Path(contract['sources']['manifest']).parent / 'verification_reports'
    candidates = [Path(explicit)] if explicit else sorted(reports.glob('*/agent2/uvm_environment.json'), reverse=True)
    for path in candidates:
        try:
            saved = validate_saved_environment(api, json.loads(path.read_text()), contract, requirements, trusted)
            return str(path), saved
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            report.artifact('generation/reuse_rejections/{}.json'.format(path.parent.parent.name),
                            data={'environment': str(path), 'reason': str(exc)})
    return None, None
