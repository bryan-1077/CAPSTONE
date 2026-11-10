"""Small verification reports and bounded live-log observations for the GUI."""
import json
from pathlib import Path
import re

AGENTS = {
    'agent1': ('Agent 1 — Structural Validation', 'Checks RTL structure, hierarchy, interfaces, and compilation.'),
    'agent2': ('Agent 2 — Functional Verification', 'Generates and runs functional verification with UVM checks.'),
    'agent3': ('Agent 3 — Protocol and Timing', 'Exercises protocol and timing scenarios and reports coverage gaps.'),
}


def small_json(path, *, limit=512 * 1024):
    try:
        with Path(path).open('rb') as handle:
            raw = handle.read(limit + 1)
        value = json.loads(raw) if len(raw) <= limit else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def validation_directory(run):
    # Retry directories are numbered by the orchestrator. Inspect only the latest
    # attempt, so an old successful report cannot mask a running/failed retry.
    attempts = sorted(run.directory.glob('[0-9]*_validation'), reverse=True)
    if attempts:
        return attempts[0]
    result = next((r for r in reversed(run.results) if r.get('stage') == 'validation'), {})
    log = result.get('artifacts', {}).get('log')
    return run.resolve(log).parent if isinstance(log, str) else run.directory


def log_observations(path, *, alive):
    try:
        with path.open('rb') as handle:
            start = handle.read(1024 * 1024)
            handle.seek(0, 2)
            end = handle.tell()
            if end > len(start):
                handle.seek(max(len(start), end - 1024 * 1024))
                start += b'\n' + handle.read(1024 * 1024)
        text = start.decode('utf-8', errors='replace')
    except OSError:
        return {}
    observations = {}
    for line in text.splitlines():
        result = re.search(r'(?:AGENT\s*([123])(?:\s+SYSTEM)?\s+RESULT\s*:|agent([123])\s*:)\s*([A-Z][A-Z_]+)', line)
        started = re.search(r'\[REPORTS\].*/agent([123])/report\.json', line)
        if result:
            agent = 'agent' + (result[1] or result[2])
            observations[agent] = result[3]
        elif started:
            # A new agent's start confirms that earlier agents are no longer the
            # active one, but does not establish their success.
            for agent, status in list(observations.items()):
                if status == 'RUNNING':
                    observations[agent] = 'UNCONFIRMED'
            observations['agent' + started[1]] = 'RUNNING' if alive else 'UNCONFIRMED'
    return observations


def compact(value):
    if isinstance(value, dict):
        return '; '.join(f'{key.replace("_", " ")}: {compact(item)}' for key, item in list(value.items())[:20])[:1800]
    if isinstance(value, list):
        return '; '.join(compact(item) for item in value[:12])[:1800]
    return str(value)[:1800]


def verification_agents(run, *, alive=False):
    directory = validation_directory(run)
    log = directory / 'validation.log'
    observed = log_observations(log, alive=alive and run.state.get('current_stage') == 'validation')
    # Report discovery is restricted to this validation attempt's report root.
    finals = sorted(directory.glob('reports/rtl/verification_reports/*/final_system_report.json'))[:32]
    final_path = finals[-1] if finals else None
    final = small_json(final_path) if final_path else {}
    result = []
    for key, (title, role) in AGENTS.items():
        status = observed.get(key, 'NOT_REPORTED')
        reported = final.get('agents', {})
        if isinstance(reported, dict) and isinstance(reported.get(key), str):
            status = reported[key]
        details = [('Role', role), ('Evidence', 'Latest validation log entry' if key in observed else 'No agent result reported yet.')]
        if final:
            details.append(('Mode', str(final.get('mode', 'Not reported'))))
            details.append(('Top module', str(final.get('top_module', 'Not reported'))))
            details.append(('Report', str(final_path)))
        if final_path:
            agent_report = final_path.parent / key / ('report.json' if key == 'agent1' else 'system_report.json')
            report = small_json(agent_report)
            for field in ('verification_scope', 'phase', 'reason', 'failure_detail', 'checks',
                          'compile_passed', 'simulation_completed', 'verification_readiness'):
                if report.get(field) is not None and report.get(field) != '':
                    details.append((field.replace('_', ' ').capitalize(), compact(report[field])))
            fields = ('requirements', 'simulation', 'traffic') if key == 'agent2' else (
                'vectors', 'protocol_timing_compliance', 'coverage_limitations') if key == 'agent3' else ()
            for field in fields:
                if field in final:
                    details.append((field.replace('_', ' ').capitalize(), compact(final[field])))
            details[1] = ('Evidence', 'Retrieved verification report; scope and gaps remain authoritative.')
        if run.config.get('skip_verification') and status == 'NOT_REPORTED':
            status = 'SKIPPED'
        result.append((key, title, status, details))
    return result


def populate_agents(tree, agents):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QTreeWidgetItem
    from .cards import status_cell
    if getattr(tree, '_agents', None) == agents:
        return
    expanded = {tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                for i in range(tree.topLevelItemCount()) if tree.topLevelItem(i).isExpanded()}
    position = tree.verticalScrollBar().value()
    tree._agents = agents
    tree.clear()
    for key, title, status, details in agents:
        label = {'PASS': 'Passed', 'PASS_WITH_GAPS': 'Passed with gaps', 'FAIL': 'Failed',
                 'RUNNING': 'Running', 'NOT_REPORTED': 'Not reported',
                 'UNCONFIRMED': 'Unconfirmed', 'SKIPPED': 'Skipped'}.get(status, status.replace('_', ' ').capitalize())
        color = {'PASS': 'passed', 'PASS_WITH_GAPS': 'needs_attention', 'FAIL': 'failed',
                 'RUNNING': 'running', 'Passed': 'passed', 'Failed': 'failed',
                 'In progress': 'running', 'Skipped': 'skipped'}.get(status, 'unconfirmed')
        item = QTreeWidgetItem(tree, [title, label])
        item.setToolTip(0, 'Click the name or arrow to expand or collapse its details.')
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        font = item.font(0)
        font.setWeight(QFont.Weight.DemiBold)
        item.setFont(0, font)
        tree.setItemWidget(item, 1, status_cell(color, label))
        for name, value in details:
            child = QTreeWidgetItem(item, [name, value])
            child.setToolTip(1, value)
        item.setExpanded(key in expanded)
    tree.verticalScrollBar().setValue(position)
