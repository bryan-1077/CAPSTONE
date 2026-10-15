"""Bounded Verilator reproductions against copied, unmodified RTL sources."""
import hashlib
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess


ALLOWED_SYSTEM_NAMES = {
    'display', 'write', 'error', 'warning', 'fatal', 'finish', 'stop', 'time', 'realtime',
    'clog2', 'bits', 'signed', 'unsigned', 'isunknown', 'countones', 'countbits',
    'onehot', 'onehot0', 'urandom', 'urandom_range', 'random', 'itor', 'rtoi',
    'past', 'stable', 'rose', 'fell', 'changed', 'root', 'unit',
}


def run_process(command: list[str], cwd: Path, log: Path, timeout: int) -> dict:
    """Keep output on disk and terminate the complete build/run process group."""
    def limit_outputs():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 ** 3, 2 * 1024 ** 3))
    with log.open('w') as output:
        process = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True, preexec_fn=limit_outputs)
        try:
            return {'exit_code': process.wait(timeout=timeout), 'timed_out': False}
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return {'exit_code': process.returncode, 'timed_out': True}


def run_experiment(action: dict, root: Path, failure: Path, number: int, catalog: dict) -> dict:
    bench = action.get('testbench')
    paths = action.get('rtl_paths')
    if not isinstance(bench, str) or not 1 <= len(bench) <= 16000:
        raise ValueError('simulate requires a SystemVerilog testbench of at most 16000 characters')
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 32
            or any(not isinstance(name, str) or catalog.get(name) != 'rtl' for name in paths)):
        raise ValueError('simulate rtl_paths must name 1..32 current catalog RTL files')
    # No shell, foreign-function interface, file I/O, includes, or environment
    # access from model-generated HDL. Ordinary timing/stimulus/assertions remain.
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/', '', bench)
    names = set(re.findall(r'\$([A-Za-z_][A-Za-z_0-9]*)', masked))
    directives = set(re.findall(r'`([A-Za-z_][A-Za-z_0-9]*)', masked))
    if (names - ALLOWED_SYSTEM_NAMES or directives - {'timescale', 'default_nettype'}
            or re.search(r'\b(?:DPI|VPI|PLI)(?:-C)?\b', masked)):
        raise ValueError('simulate testbench contains unsupported external access or directives')
    if not re.search(r'\bmodule\s+debug_repro\b', masked):
        raise ValueError('simulate testbench must define module debug_repro')
    compiler = shutil.which('verilator')
    if compiler is None:
        raise ValueError('Verilator is unavailable for a focused simulation')
    directory = failure / 'analysis/experiments' / f'experiment_{number:02d}'
    directory.mkdir(parents=True, exist_ok=False)
    sources = directory / 'sources'
    sources.mkdir()
    hashes = {}
    copied = []
    for name in dict.fromkeys(paths):
        path = (root / name).resolve()
        rtl = (root / 'rtl_output').resolve()
        if not path.is_relative_to(rtl) or not path.is_file():
            raise ValueError('simulate sources must stay inside current rtl_output')
        relative = path.relative_to(rtl)
        destination = sources / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        destination.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
        copied.append(str(destination))
    bench_path = directory / 'testbench.sv'
    bench_path.write_text(bench)
    build_log = directory / 'compile.log'
    build = run_process([compiler, '--binary', '--timing', '--assert', '-Wno-fatal',
        '--threads', '1', '-j', '2', '--top-module', 'debug_repro', '--Mdir', str(directory / 'build'),
        '-I' + str(sources), str(bench_path), *copied], directory, build_log, 60)
    result = {'source_hashes': hashes, 'testbench': str(bench_path), 'compile': build,
              'compile_log': str(build_log), 'simulation': None}
    if build['exit_code'] == 0:
        sim_log = directory / 'simulation.log'
        result['simulation'] = run_process([str(directory / 'build/Vdebug_repro')], directory, sim_log, 10)
        result['simulation_log'] = str(sim_log)
    return result
