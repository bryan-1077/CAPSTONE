#!/usr/bin/env python3
"""Post-synthesis EQY equivalence gate; no model calls or generated testbenches."""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time

from run_pipeline import AgentReport, create_report_run, utc_now


def yosys_quote(value):
    value = str(value)
    if any(c in value for c in '\n\r\x00'):
        raise ValueError('Newlines/NUL are not permitted in Yosys arguments')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


class LEC_Agent:
    def __init__(self, rtl_path, netlist, top_module=None, specification=None,
                 libraries=None, include_dirs=None, rtl_files=None,
                 timeout=600, depth=20, eqy='eqy'):
        self.rtl_path = os.path.abspath(rtl_path)
        self.netlist = os.path.realpath(netlist)
        self.top_module = top_module
        self.specification = specification
        self.libraries = [os.path.realpath(p) for p in libraries or []]
        self.include_dirs = [os.path.abspath(p) for p in include_dirs or []]
        self.explicit_files = rtl_files
        self.timeout, self.depth, self.eqy = timeout, depth, eqy
        self.rtl_files = []
        self.execution = {}
        self.errors = []
        self.status = 'ERROR'

    def discover_rtl_files(self):
        excluded = {'verification_reports', 'tb_auto', 'tv_auto', 'build_auto',
                    'build_auto_tv', '.git', '__pycache__'}
        paths = []
        if self.explicit_files:
            paths = self.explicit_files
        elif os.path.isdir(self.rtl_path):
            for root, dirs, files in os.walk(self.rtl_path):
                dirs[:] = sorted(d for d in dirs if d not in excluded)
                paths.extend(os.path.join(root, n) for n in sorted(files)
                             if n.lower().endswith(('.sv', '.v')))
        elif os.path.isfile(self.rtl_path):
            paths = [self.rtl_path]
        self.rtl_files = list(dict.fromkeys(os.path.realpath(p) for p in paths
                                         if os.path.realpath(p) not in [self.netlist] + self.libraries))
        if not self.rtl_files:
            raise ValueError('No RTL .sv/.v source files found')
        for path in self.rtl_files + [self.netlist] + self.libraries:
            if not os.path.isfile(path):
                raise ValueError('Input file does not exist: ' + path)
        for path in self.include_dirs:
            if not os.path.isdir(path):
                raise ValueError('Include directory does not exist: ' + path)
        return self.rtl_files

    def resolve_top(self):
        if self.specification:
            import yaml
            try:
                with open(self.specification) as handle:
                    metadata = yaml.safe_load(handle)
            except yaml.YAMLError as exc:
                raise ValueError('Invalid specification: ' + str(exc))
            if not isinstance(metadata, dict):
                raise ValueError('Specification must be a YAML/JSON mapping')
            candidates = {metadata[k] for k in ('top_module', 'design_name', 'module_name')
                          if isinstance(metadata.get(k), str) and metadata[k]}
            if self.top_module:
                if candidates and candidates != {self.top_module}:
                    raise ValueError('Explicit top conflicts with specification metadata')
            elif len(candidates) == 1:
                self.top_module = candidates.pop()
            else:
                raise ValueError('Specification must identify one unambiguous top module')
        if not self.top_module or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', self.top_module):
            raise ValueError('Supply --top or specification top_module/design_name/module_name')

    def generate_eqy_config(self):
        self.resolve_top()
        includes = list(dict.fromkeys(self.include_dirs + [os.path.dirname(p) for p in self.rtl_files]))
        options = ' '.join('-I' + yosys_quote(p) for p in includes)
        # Read all sources together; --rtl-file offers explicit package ordering.
        lines = ['[gold]', 'read_verilog -sv {} {}'.format(options, ' '.join(map(yosys_quote, self.rtl_files))),
                 'hierarchy -check -top ' + self.top_module, 'prep -top ' + self.top_module,
                 'memory_map', '', '[gate]']
        for path in self.libraries:
            # Functional models, not black boxes, are needed to prove equivalence.
            if path.lower().endswith('.lib'):
                lines.append('read_liberty ' + yosys_quote(path))
            else:
                lines.append('read_verilog -sv {} {}'.format(options, yosys_quote(path)))
        lines.extend(['read_verilog -sv {} {}'.format(options, yosys_quote(self.netlist)),
                      'hierarchy -check -top ' + self.top_module, 'prep -top ' + self.top_module,
                      'memory_map', '', '[strategy induction]', 'use sat', 'depth ' + str(self.depth), ''])
        self.config = os.path.join(self.report.directory, 'generated_config.eqy')
        self.report.artifact('generated_config.eqy', text='\n'.join(lines))
        return self.config

    def run_eqy(self):
        if not shutil.which(self.eqy) or not shutil.which('yosys'):
            raise RuntimeError('EQY and Yosys must both be installed and available on PATH')
        self.work = os.path.join(self.report.directory, 'eqy_work')
        command = [self.eqy, '-d', self.work, self.config]
        started = time.monotonic()
        timed_out = False
        with open(os.path.join(self.report.directory, 'stdout.log'), 'w') as out, open(
                os.path.join(self.report.directory, 'stderr.log'), 'w') as err:
            process = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True)
            try:
                process.wait(timeout=self.timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                if isinstance(exc, KeyboardInterrupt):
                    raise
                timed_out = True
        self.execution = {'command': command, 'return_code': process.returncode,
                          'runtime_seconds': time.monotonic() - started, 'timed_out': timed_out}
        for name in ('stdout.log', 'stderr.log'):
            self.report.artifact(name, source=os.path.join(self.report.directory, name))
        return self.execution

    def parse_results(self):
        partitions = {}
        for path in glob.glob(os.path.join(self.work, 'strategies', '*', '*', 'status')):
            with open(path) as handle:
                words = handle.read().split()
            partitions[os.path.relpath(path, self.work)] = words[0] if words else 'UNKNOWN'
        passed = os.path.isfile(os.path.join(self.work, 'PASS'))
        failed = os.path.isfile(os.path.join(self.work, 'FAIL'))
        if self.execution['timed_out']:
            self.status = 'INCOMPLETE'
            self.errors.append('EQY exceeded the wall-clock timeout; equivalence is unproven')
        elif passed and not failed and self.execution['return_code'] == 0:
            self.status = 'PASS'
        elif failed:
            self.status = ('ERROR' if 'ERROR' in partitions.values() else
                           'FAIL' if 'FAIL' in partitions.values() else 'INCOMPLETE')
            self.errors.append('EQY did not prove equivalence; inspect partition logs and traces. '
                               'This does not by itself establish an RTL/netlist mismatch.')
        else:
            self.status = 'ERROR'
            self.errors.append('EQY did not produce a consistent successful proof result')
        self.partitions = partitions
        return self.status

    def generate_report(self):
        data = {'schema_version': 1, 'agent': 'Agent 4 - Logic Equivalence Check',
                'tool': 'Yosys EQY', 'top_module': self.top_module, 'status': self.status,
                'rtl_files_checked': len(self.rtl_files), 'rtl_files': self.rtl_files,
                'netlist': self.netlist, 'libraries': self.libraries,
                'execution': self.execution,
                'eqy_work_directory': getattr(self, 'work', None),
                'allow_physical_design': self.status == 'PASS',
                'details': {'equivalent': True if self.status == 'PASS' else None,
                            'errors': self.errors, 'partition_statuses': getattr(self, 'partitions', {})},
                'input_sha256': {}, 'specification': self.specification}
        for path in self.rtl_files + [self.netlist] + self.libraries:
            if os.path.isfile(path):
                with open(path, 'rb') as handle:
                    data['input_sha256'][path] = hashlib.sha256(handle.read()).hexdigest()
        self.report.artifact('lec_report.json', data=data)
        self.report.update(status=self.status, verification_status=self.status,
                           exit_code=0 if self.status == 'PASS' else 1, finished_at=utc_now(),
                           failure_stage=None if self.status == 'PASS' else 'equivalence',
                           failure_detail='; '.join(self.errors), lec=data)
        return data

    def run(self):
        # Separate post-synthesis run: never insert LEC before a netlist exists.
        run_dir = create_report_run([self.rtl_path], requested=('agent4',))
        self.report = AgentReport('agent4', run_dir=run_dir)
        self.report.step('equivalence')
        try:
            if self.timeout <= 0 or self.depth <= 0:
                raise ValueError('Timeout and proof depth must be positive')
            self.discover_rtl_files()
            self.generate_eqy_config()
            self.run_eqy()
            self.parse_results()
        except (OSError, ValueError, RuntimeError) as exc:
            self.errors.append(str(exc))
            self.status = 'ERROR'
        except KeyboardInterrupt:
            self.status = 'INTERRUPTED'
            self.errors.append('LEC interrupted')
        return self.generate_report()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rtl_path')
    parser.add_argument('--netlist', required=True)
    parser.add_argument('--top', dest='top_module')
    parser.add_argument('--spec', dest='specification')
    parser.add_argument('--library', action='append', dest='libraries')
    parser.add_argument('--include-dir', action='append', dest='include_dirs')
    parser.add_argument('--rtl-file', action='append', dest='rtl_files', help='Explicit source order; repeat for each file')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--depth', type=int, default=20)
    parser.add_argument('--eqy', default='eqy')
    agent = LEC_Agent(**vars(parser.parse_args()))
    result = agent.run()
    print('Agent 4: {}\nReport: {}'.format(result['status'], os.path.join(agent.report.directory, 'lec_report.json')))
    for error in result['details']['errors']:
        print(error)
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
