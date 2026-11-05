"""Launch with python -m shared.gui [--runs PATH]."""
import argparse
import sys
from .run_reader import DEFAULT_RUNS


def main():
    parser = argparse.ArgumentParser(description='CAPSTONE desktop design flow')
    parser.add_argument('--runs', default=DEFAULT_RUNS, help='History directory containing run folders')
    parser.add_argument('--doctor', action='store_true', help='Report local prerequisites without remote connections')
    parser.add_argument('--smoke-test', action='store_true', help='Check Qt startup with the offscreen display plugin')
    args = parser.parse_args()
    if args.doctor or args.smoke_test:
        import json
        from .doctor import inspect_environment, smoke_test
        result = inspect_environment() if args.doctor else smoke_test()
        print(json.dumps(result, indent=2))
        return 0
    from PySide6.QtWidgets import QApplication
    from .window import RunWindow
    app = QApplication(sys.argv[:1])
    window = RunWindow(args.runs)
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
