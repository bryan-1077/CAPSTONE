"""Summarize downloaded diagnostics without treating missing proofs as passes."""

from collections import Counter
import json
from pathlib import Path
import sys


def summarize(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    summary = dict(source=manifest['source'], input_hashes=manifest['input_hashes'], trials={})
    for mode in ('width_checked', 'closed_outputs'):
        result = json.loads((root / mode / 'result.json').read_text())
        counts = dict(Counter(result['statuses'].values()))
        row = dict(elapsed_seconds=result['elapsed_seconds'], partition_counts=counts,
                   returncode=result['returncode'], wall_timeout=result['timed_out'])
        strategy_logs = list((root / mode / 'work/strategies').glob('*/prove/*/logfile.txt'))
        row['strategy_timeout'] = any('TIMEOUT' in p.read_text() for p in strategy_logs)
        row['verdict'] = 'INCONCLUSIVE'
        if result['passed'] and result['returncode'] == 0 and counts == {'PASS': len(result['statuses'])}:
            row['verdict'] = 'PASS'
        if mode == 'closed_outputs':
            expected = {name: list(range(port['width'])) for name, port in manifest['ports'].items()
                        if port['direction'] == 'output'}
            covered = {}
            for cone in result['cones'].values():
                assert not cone['crossbits'], 'Unexpected internal crosspoints'
                assert all(manifest['ports'].get(name, {}).get('direction') == 'input'
                           for name in cone['inbits']), 'Unexpected internal proof inputs'
                for name, bits in cone['outbits'].items():
                    covered.setdefault(name, set()).update(bits)
            assert {name: sorted(bits) for name, bits in covered.items()} == expected, 'Output coverage differs'
            row['covered_output_bits'] = sum(len(bits) for bits in covered.values())
            row['only_top_level_inputs'] = True
            traces = list((root / mode / 'work/strategies').glob('*/prove/*/engine_*/trace.vcd'))
            row['counterexample_traces'] = [str(p.relative_to(root)) for p in traces]
            if 'FAIL' in counts and traces:
                row['verdict'] = 'FAIL'
        summary['trials'][mode] = row
    return summary


if __name__ == '__main__':
    root = Path(sys.argv[1])
    summary = summarize(root)
    text = json.dumps(summary, indent=2, sort_keys=True) + '\n'
    (root / 'summary.json').write_text(text)
    print(text)
