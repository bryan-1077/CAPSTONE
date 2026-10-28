"""Run copied Agent 4 tests against the experimental runtime in this process only."""

import importlib.util
from pathlib import Path
import sys
import unittest


def main():
    sys.dont_write_bytecode = True
    directory = Path(__file__).resolve().parent
    sys.path[:0] = [str(directory), str(directory.parents[1] / 'shared')]
    source = directory / 'agent4_lec_experimental.py'
    # Original tests and their fixture modules import this name. Binding it in
    # this test process directs all of them to the copy without editing originals.
    spec = importlib.util.spec_from_file_location('agent4_lec', str(source))
    module = importlib.util.module_from_spec(spec)
    sys.modules['agent4_lec'] = module
    spec.loader.exec_module(module)
    print('Testing experimental Agent 4: ' + str(source), flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:] or [
        'experimental_test_agent4_preparation', 'experimental_test_system_agent4',
    ])
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
