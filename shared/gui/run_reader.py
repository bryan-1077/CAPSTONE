"""Read saved evidence without importing execution or Qt modules."""
import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS = REPO_ROOT / 'shared/top_orchestrator/runs'


@dataclass
class SavedRun:
    directory: Path
    state: dict
    error: str = ''

    @property
    def status(self):
        return 'Unreadable' if self.error else str(self.state.get('status', 'unknown'))

    @property
    def results(self):
        return self.state.get('results', [])

    @property
    def config(self):
        return self.state.get('config', {})

    def resolve(self, value):
        """Rebase old absolute in-run paths when a run directory has moved."""
        path = Path(value)
        if not path.is_absolute():
            return self.directory / path
        old = str(path).split('/' + self.directory.name + '/', 1)
        if len(old) == 2:
            return self.directory / old[1]
        return path

    def files(self):
        # Bound discovery; reports remain accessible through their directory.
        paths = {self.directory / 'state.json'}
        from .run_storage import lifecycle_path, worker_log_path
        for path in (lifecycle_path(self.directory), worker_log_path(self.directory)):
            if path.exists():
                paths.add(path)
        for pattern in ('*.log', '*.json', '*.yaml', '*.txt', '*/*.log'):
            paths.update(self.directory.glob(pattern))
        for result in self.results:
            for value in result.get('artifacts', {}).values():
                if isinstance(value, str):
                    candidate = self.resolve(value)
                    if candidate.exists():
                        paths.add(candidate)
        return sorted(paths, key=str)


def load_run(directory):
    directory = Path(directory).resolve()
    try:
        state = json.loads((directory / 'state.json').read_text(encoding='utf-8'))
        if not isinstance(state, dict) or not isinstance(state.get('config', {}), dict):
            raise ValueError('Expected a state object with a configuration object')
        results = state.get('results', [])
        if not isinstance(results, list) or any(
            not isinstance(r, dict) or not isinstance(r.get('artifacts', {}), dict)
            for r in results
        ):
            raise ValueError('Invalid stage results')
        return SavedRun(directory, state)
    except (OSError, ValueError) as exc:
        return SavedRun(directory, {}, str(exc))


def discover(root):
    root = Path(root)
    if not root.is_dir():
        return []
    return [load_run(p.parent) for p in sorted(root.glob('*/state.json'),
                                              key=lambda p: p.stat().st_mtime, reverse=True)]


def preview(path, limit=256 * 1024):
    """Bound log reads and avoid interpreting file content as rich text."""
    try:
        with Path(path).open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read(limit)
        if b'\x00' in data:
            return 'Binary artifact. Use Open Selected to view externally.'
        return ('[Showing final 256 KiB]\n' if size > limit else '') + data.decode('utf-8', errors='replace')
    except OSError as exc:
        return str(exc)


def summary(run):
    if run.error:
        return 'Cannot read saved run: ' + run.error
    from .registry import read_record, lifecycle_path
    lifecycle = read_record(lifecycle_path(run.directory))
    lines = ([f'Worker outcome: {lifecycle.get("lifecycle")}',
              f'Cleanup: {lifecycle.get("remote_cleanup", {}).get("status", "not recorded")}'] if lifecycle else [])
    from .problems import run_connection_error, CONNECTION_TITLE, CONNECTION_REASON, CONNECTION_GUIDANCE
    if run_connection_error(run.state):
        lines = [CONNECTION_TITLE, CONNECTION_REASON, CONNECTION_GUIDANCE, ''] + lines
    lines += [f'Saved outcome: {run.status}',
             f'Flow: {run.config.get("frontend_flow", "expand-spec")}',
             f'Target clock: {run.config.get("target_mhz") or "not recorded"} MHz',
             f'Repair cycles: {run.state.get("repair_cycles", 0)}']
    if run.status in ('running', 'pending') or run.state.get('current_stage'):
        lines.append('Saved activity only: worker liveness is unknown. This viewer cannot resume runs.')
    for flag, label in [('no_bist', 'BIST'), ('skip_verification', 'Agents 1–3 verification'),
                        ('no_backend', 'Backend'), ('skip_lec', 'LEC')]:
        if run.config.get(flag):
            lines.append(f'{label}: skipped by configuration')
    lines.append('Stage outcomes retain their original scope. Local/module passes do not establish system coverage or backend readiness.')
    for result in run.results:
        lines.append(f'\n{result.get("stage", "unknown")}: {result.get("status", "unknown")}\n{result.get("message", "")}')
        for key, value in result.get('artifacts', {}).items():
            lines.append(f'  {key}: {value}')
    return '\n'.join(lines)
