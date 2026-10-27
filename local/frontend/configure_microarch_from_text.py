#!/usr/bin/env python3
"""Interactive, lossless plain-text intake for the staged specification flow.

No feature-table extraction or LLM call occurs here. The existing specification
stage elaborates the complete user description after request confirmation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

import yaml

from generate_microarch import DEFAULT_MODEL
from specification_request import build_specification_request

SCRIPT_DIR = Path(__file__).resolve().parent


def request_from_text(description, design_name='ddr4_controller_top', design_type='memory_controller'):
    if not isinstance(description, str) or not description.strip():
        raise ValueError('Describe the design before starting generation')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', design_name):
        raise ValueError('Design name must be an HDL identifier')
    if design_type not in ('memory_controller', 'generic'):
        raise ValueError('Unsupported design category')
    request = {'design_name': design_name, 'requirements': [description]}
    if design_type == 'memory_controller':
        request['design_type'] = design_type
    # This is the new request validator, not the legacy configuration validator.
    # No compatibility preset, interface or feature defaults are injected.
    build_specification_request(request)
    return request


def request_yaml(request):
    class RequestDumper(yaml.SafeDumper):
        pass
    def string(dumper, value):
        return dumper.represent_scalar('tag:yaml.org,2002:str', value,
                                       style='|' if '\n' in value else None)
    RequestDumper.add_representer(str, string)
    return yaml.dump(request, Dumper=RequestDumper, sort_keys=False, allow_unicode=True)


def collect_description():
    print('Describe the design you want in plain text.')
    print('Include interfaces, memory sizes, ordering, timing/refresh, reset and any hard constraints.')
    print('Say which choices the design flow may make. No preset feature table is applied.')
    print('''
Example descriptions (references to adapt, not selectable presets):

  Baseline controller:
    Build a DDR4-3200 controller with four banks and 32-bit data. Use a compact
    txn_valid/txn_ready interface, a 2-bit bank selector and 4-bit address
    (upper two bits row, lower two bits column). Provide read responses.
    Use FIFO ordering, four waiting entries plus one active request, open-page
    policy, refresh and tRCD/tRAS/tRP/tRRD/tFAW/tREFI/tRFC protection.
    Use synchronous active-low reset; clear memory and outstanding requests.

  Different host interface:
    Build a four-bank DDR4-3200 controller with 32-bit AXI4-Lite data and
    8-bit addresses. Handle independent write-address/data channels, byte
    strobes and read/write response backpressure. Allow one outstanding read
    and one outstanding write. Choose and document the memory address mapping.

  Additional timing behavior:
    Extend a FIFO DDR4-3200 controller with an explicit tWR=12-cycle minimum
    between WRITE and PRE to the same bank, in addition to the normal timing
    protections. Update history only on committed commands. Choose the
    internal decomposition and expose observations for testing the boundary.

New interfaces/timing features need matching verification support; the current
local simulation profile covers the compact-host FIFO controller.
''')
    print('Enter multiple lines; finish with a line containing only a period (.).')
    lines = []
    while True:
        try:
            line = input('> ' if not lines else '... ')
        except EOFError:
            break
        if line == '.':
            break
        lines.append(line)
    return '\n'.join(lines)


def confirm(prepare_only):
    action = 'Save this request' if prepare_only else 'Use this request and start specification/RTL generation'
    while True:
        try:
            answer = input(action + '? [y/N]: ').strip().lower()
        except EOFError:
            return False
        if answer in ('y', 'yes'):
            return True
        if answer in ('', 'n', 'no'):
            return False
        print('Please answer yes or no.')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text-file', type=Path, help='Read the original description from UTF-8 text instead of prompting')
    parser.add_argument('--design-name', default='ddr4_controller_top', help='Top/design name; default: ddr4_controller_top')
    parser.add_argument('--design-type', choices=['memory_controller', 'generic'], default='memory_controller',
                        help='Controller decomposition by default; generic allows other block types')
    parser.add_argument('--run-dir', type=Path, help='Fresh directory for request, source, review, RTL and logs')
    parser.add_argument('--yes', action='store_true', help='Explicitly accept the displayed request without a confirmation prompt')
    parser.add_argument('--prepare-only', action='store_true', help='Save the request YAML and stop; no LLM calls')
    parser.add_argument('--microarch-only', action='store_true', help='Generate a specification/architecture review export without RTL')
    parser.add_argument('--simulate', action='store_true', help='After successful RTL/lint, run the supported local FIFO simulation profile')
    parser.add_argument('--microarch-model', default=DEFAULT_MODEL, help='Specification and semantic-review model')
    recovery_flags = parser.add_mutually_exclusive_group()
    recovery_flags.add_argument('--rtl-recovery', dest='rtl_recovery', action='store_true',
                                help='Enable bounded RTL recovery; architecture RTL builds with lint enable it by default.')
    recovery_flags.add_argument('--no-rtl-recovery', dest='rtl_recovery', action='store_false',
                                help='Disable bounded RTL response recovery.')
    parser.set_defaults(rtl_recovery=None)
    parser.add_argument('--rtl-generator-model', help='Optional generator/merger override; existing default otherwise')
    parser.add_argument('--specification-response', type=Path, help='Offline staged-response replay; use with --microarch-only')
    parser.add_argument('--no-auto-spec-repair', action='store_true', help='Stop on semantic rejection without the optional single repair cycle')
    parser.add_argument('--no-lint', action='store_true', help='Forward the existing frontend lint opt-out')
    parser.add_argument('--interactive', action='store_true', help='Forward the existing frontend stage-interaction option')
    args = parser.parse_args(argv)
    if args.rtl_recovery and (args.prepare_only or args.microarch_only or args.no_lint):
        parser.error('--rtl-recovery requires an RTL build with lint')
    if args.simulate and (args.prepare_only or args.microarch_only or args.no_lint):
        parser.error('--simulate requires an RTL build with lint enabled')
    if args.rtl_generator_model is not None and (not args.rtl_generator_model.strip() or args.microarch_only):
        parser.error('--rtl-generator-model requires a nonempty model name and an RTL build')
    # A partial replay must never turn into an unannounced live call downstream.
    if args.specification_response and not (args.prepare_only or args.microarch_only):
        parser.error('--specification-response requires --microarch-only; resume approved RTL separately with run_flow.py')
    return args


def flow_command(args, root):
    command = [sys.executable, '-u', str(SCRIPT_DIR / 'run_flow.py'), str(root / 'request.yaml'),
               '--specification', '--specification-output', str(root / 'source'),
               '--architecture-output', str(root / 'architecture.yaml'),
               '--microarch-model', args.microarch_model]
    recovery = getattr(args, 'rtl_recovery', None)
    if recovery is not None:
        command.append('--rtl-recovery' if recovery else '--no-rtl-recovery')
    if args.microarch_only:
        command.append('--microarch-only')
    else:
        command += ['--automated-spec-review', '--spec-review-output', str(root / 'review'),
                    '--build-dir', str(root / 'build')]
        if not args.no_auto_spec_repair:
            command += ['--auto-spec-repair', '--spec-repair-output', str(root / 'repair')]
    for flag, value in (('--rtl-generator-model', args.rtl_generator_model),
                        ('--specification-response', args.specification_response)):
        if value is not None:
            command += [flag, str(value.resolve() if isinstance(value, Path) else value)]
    for flag, enabled in (('--no-lint', args.no_lint), ('--interactive', args.interactive)):
        if enabled:
            command.append(flag)
    return command


def stream_command(command, log):
    with log.open('a', encoding='utf-8') as evidence:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1) as process:
            for line in process.stdout:
                print(line, end='', flush=True)
                evidence.write(line)
                evidence.flush()
            return process.wait()


def main(argv=None):
    args = parse_args(argv)
    status, root = None, None
    try:
        planned_root = args.run_dir.expanduser().resolve() if args.run_dir else (
            SCRIPT_DIR / 'microarch/builds/text_runs' /
            (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8]))
        if planned_root.exists():
            raise ValueError(f'Run directory already exists: {planned_root}; choose a fresh directory')
        while True:
            description = args.text_file.read_bytes().decode('utf-8') if args.text_file else collect_description()
            request = request_from_text(description, args.design_name, args.design_type)
            serialized = request_yaml(request)
            print('\nRequest YAML (original description preserved):\n' + serialized)
            print('Run directory: ' + str(planned_root))
            if args.yes or confirm(args.prepare_only):
                break
            if args.text_file:
                print('Request declined; nothing was written or generated.')
                return 0
            print('Revise the description, or press Ctrl+C to cancel.')
        root = planned_root
        root.mkdir(parents=True, exist_ok=False)
        (root / 'description.txt').write_text(description, encoding='utf-8')
        (root / 'request.yaml').write_text(serialized, encoding='utf-8')
        status = {'route': 'plain_text_specification', 'status': 'request_prepared',
                  'request': 'request.yaml', 'original_description': 'description.txt', 'commands': []}
        if args.prepare_only:
            print('[TEXT-REQUEST] Saved request; no LLM calls made.')
            return 0
        if args.simulate:
            print('[TEXT-REQUEST] Local simulation supports the compact-host FIFO profile; other designs will report unsupported input.')
        command = flow_command(args, root)
        status['commands'].append(command)
        status['status'] = 'generation_in_progress'
        code = stream_command(command, root / 'frontend.log')
        status.update(status='generation_failed' if code else ('review_exported' if args.microarch_only else 'frontend_generated'),
                      exit_code=code)
        if code:
            print('[TEXT-REQUEST] Generation stopped; inspect ' + str(root / 'frontend.log'))
            return code
        if args.simulate:
            command = [sys.executable, '-u', str(SCRIPT_DIR / 'simulate_handoff.py'),
                       str(root / 'build/verification_handoff.json'), '--output-dir', str(root / 'simulation')]
            status['commands'].append(command)
            code = stream_command(command, root / 'frontend.log')
            status.update(status='simulation_failed' if code else 'simulation_passed', exit_code=code)
            return code
        print('[TEXT-REQUEST] Completed: ' + str(root))
        return 0
    except (ValueError, OSError, UnicodeError) as exc:
        print('[TEXT-REQUEST] ERROR: ' + str(exc))
        if status is not None:
            status.update(status='failed', error=str(exc))
        return 2
    except (KeyboardInterrupt, EOFError):
        print('\n[TEXT-REQUEST] Canceled.')
        if status is not None:
            status['status'] = 'canceled'
        return 130
    finally:
        if status is not None:
            (root / 'intake_status.json').write_text(json.dumps(status, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
