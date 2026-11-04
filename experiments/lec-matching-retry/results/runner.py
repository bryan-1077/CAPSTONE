"""Isolated EQY retries using the original run's already elaborated models.

Run on Olympus with --submit; never modifies the original proof or validator.
"""

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


PROJECT = Path('/mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE')
SOURCE = PROJECT / ('shared/allocations/733265/lec_runs/356dc9917c74489c94a97b3ca5358e24/'
                    'verification_reports/agent4_runs/2026-11-03_23-50-08_ts9t2jq0/agent4')
TOOLS = PROJECT / 'tools/oss-cad-suite/bin'
ROOT = Path(__file__).resolve().parent
TOP = 'ddr4_controller_top'


def save(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def configuration(mode, ports, mismatches):
    text = '\n'.join('[{}]\nread_rtlil "{}"\n'.format(side, SOURCE / 'eqy_work' / (side + '.il'))
                     for side in ('gold', 'gate'))
    text += '\n[match {}]\n'.format(TOP)
    if mode == 'width_checked':
        text += ''.join('gold-nomatch /^{0}$/\ngate-nomatch /^{0}$/\n'.format(name)
                        for name in mismatches)
    else:
        text += ''.join('gold-match {0} {0}\n'.format(name) for name in ports)
        text += 'gold-nomatch *\ngate-nomatch *\n'
        # One closed cone: include all observable outputs and their producers.
        text += '\n[collect {}]\nbind *\ngroup *\n'.format(TOP)
    return text + '\n[strategy prove]\nuse sby\nengine abc pdr\ndepth 20\ntimeout 600\n'


def submit():
    modules = [json.loads((SOURCE / (side + '.json')).read_text())['modules'][TOP]
               for side in ('gold', 'gate')]
    gold, gate = modules
    assert gold['ports'].keys() == gate['ports'].keys()
    for name, port in gold['ports'].items():
        assert (port['direction'], len(port['bits'])) == (gate['ports'][name]['direction'],
                                                        len(gate['ports'][name]['bits']))
    mismatches = [name for name, net in gold['netnames'].items()
                  if name in gate['netnames'] and name not in gold['ports']
                  and (len(net['bits']), net.get('offset', 0), net.get('upto', 0)) !=
                  (len(gate['netnames'][name]['bits']), gate['netnames'][name].get('offset', 0),
                   gate['netnames'][name].get('upto', 0))]
    # Names enter an anchored regex in the experiment config.
    import re
    patterns = [re.escape(name) for name in mismatches]
    manifest = dict(source=str(SOURCE), width_mismatches=mismatches,
                    ports={name: dict(direction=p['direction'], width=len(p['bits']))
                           for name, p in gold['ports'].items()},
                    input_hashes={side: hashlib.sha256((SOURCE / 'eqy_work' / (side + '.il')).read_bytes()).hexdigest()
                                  for side in ('gold', 'gate')})
    save(ROOT / 'manifest.json', manifest)
    for mode in ('width_checked', 'closed_outputs'):
        directory = ROOT / mode
        directory.mkdir(exist_ok=False)
        (directory / 'proof.eqy').write_text(configuration(mode, gold['ports'], patterns))
    script = '#!/bin/bash\n#SBATCH --nodes=1\n#SBATCH --ntasks=1\n#SBATCH --cpus-per-task=6\n'
    script += '#SBATCH --time=00:30:00\n#SBATCH --partition=adademic\n#SBATCH --qos=olympus-academic\n'
    script += '#SBATCH --job-name=lec-matching-retry\n#SBATCH --output=' + str(ROOT / 'job.log') + '\n'
    script += 'source ~/.bashrc\nexport PATH="' + str(TOOLS) + ':$PATH"\n'
    script += 'python3 -u ' + str(ROOT / 'runner.py') + ' --run\n'
    (ROOT / 'job.sh').write_text(script)
    result = subprocess.check_output(['sbatch', '--parsable', str(ROOT / 'job.sh')]).decode().strip()
    (ROOT / 'job_id.txt').write_text(result + '\n')
    print('SUBMITTED ' + result + ' ' + str(ROOT), flush=True)


def run():
    for mode in ('width_checked', 'closed_outputs'):
        directory = ROOT / mode
        started = time.monotonic()
        print('START ' + mode, flush=True)
        command = [str(TOOLS / 'eqy'), '--yosys', str(TOOLS / 'yosys'), '-j', '6',
                   '-d', 'work', 'proof.eqy']
        timed_out = False
        with (directory / 'eqy.log').open('w') as output:
            process = subprocess.Popen(command, cwd=str(directory), stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            while process.poll() is None:
                if time.monotonic() - started > 720:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    break
                time.sleep(2)
            process.wait()
        work = directory / 'work'
        statuses, cones = {}, {}
        listing = work / 'partition.list'
        for line in listing.read_text().splitlines() if listing.exists() else []:
            name = line.split()[1]
            status = work / 'strategies' / name / 'prove/status'
            statuses[name] = status.read_text().strip() if status.exists() else 'MISSING'
            part = json.loads((work / 'partitions' / (name + '.json')).read_text())['partition']
            cones[name] = {key: part.get(key, {}) for key in ('inbits', 'outbits', 'crossbits')}
        result = dict(mode=mode, returncode=process.returncode, timed_out=timed_out,
                      elapsed_seconds=time.monotonic() - started, statuses=statuses, cones=cones,
                      passed=(work / 'PASS').exists(), failed=(work / 'FAIL').exists())
        save(directory / 'result.json', result)
        print('RESULT ' + json.dumps({k: v for k, v in result.items() if k != 'cones'}), flush=True)
    print('EXPERIMENT COMPLETE', flush=True)


if __name__ == '__main__':
    submit() if '--submit' in sys.argv else run()
