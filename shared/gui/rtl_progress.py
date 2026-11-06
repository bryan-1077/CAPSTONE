"""Module generation evidence from this run's log and immutable RTL snapshot."""
import json
from pathlib import Path
import re

EVENT = re.compile(r'--- (CREATING|FINISHED) --- ([\w$]+)\.sv\b([^\n]*)')


def module_progress(run, *, alive=False):
    modules = {}
    from .run_storage import worker_log_path
    log = worker_log_path(run.directory)
    try:
        # Generation appears near the start, before potentially large BIST logs.
        with log.open('r', encoding='utf-8', errors='replace') as handle:
            text = handle.read(2 * 1024 * 1024)
    except OSError:
        text = ''
    for event in EVENT.finditer(text):
        phase, name, detail = event.groups()
        if name not in modules and len(modules) >= 256:
            continue
        if phase == 'CREATING':
            modules[name] = ('running', 'Generating') if alive else ('unconfirmed', 'Generation unconfirmed')
        elif re.search(r'\bSUCCESS\b', detail):
            modules[name] = ('passed', 'Generated')
        elif re.search(r'\b(FAIL(?:ED|URE)?|ERROR)\b', detail):
            modules[name] = ('failed', 'Generation failed')

    # Never use local/frontend/rtl_output: another run may have overwritten it.
    generation = next((r for r in reversed(run.results) if r.get('stage') == 'generate'), {})
    mailbox = generation.get('artifacts', {}).get('mailbox') or run.config.get('mailbox')
    if isinstance(mailbox, str) and mailbox:
        path = Path(mailbox)
        if not path.is_absolute():
            path = run.directory / path
        try:
            with (path / 'rtl' / 'manifest.json').open(encoding='utf-8') as handle:
                manifest = json.loads(handle.read(1024 * 1024))
            names = manifest.get('generated_modules', []) if isinstance(manifest, dict) else []
            if isinstance(names, list):
                for name in names[:256]:
                    if isinstance(name, str) and re.fullmatch(r'[\w$]+', name):
                        modules.setdefault(name, ('passed', 'Generated'))
        except (OSError, ValueError):
            pass
    rows = [(name, status, label) for name, (status, label) in modules.items()]
    # Stable ordering keeps completed modules readable as the active one changes.
    return sorted(rows, key=lambda row: row[1] != 'running')


def module_count_text(rows):
    total = len(rows)
    generated = sum(status == 'passed' for _, status, _ in rows)
    active = sum(status == 'running' for _, status, _ in rows)
    failed = sum(status == 'failed' for _, status, _ in rows)
    unconfirmed = sum(status == 'unconfirmed' for _, status, _ in rows)
    parts = [f'{total} module' + ('s' if total != 1 else '') + ' observed', f'{generated} generated']
    for count, label in ((active, 'generating'), (failed, 'failed'), (unconfirmed, 'unconfirmed')):
        if count:
            parts.append(f'{count} {label}')
    return ' · '.join(parts) + '\nGenerated means RTL was produced; verification is a separate stage.'


def populate_module_tree(tree, rows):
    from PySide6.QtWidgets import QTreeWidgetItem
    from .cards import status_badge
    # Avoid replacing widgets and scroll position on every poll.
    if getattr(tree, '_module_rows', None) == rows:
        return
    tree._module_rows = rows
    position = tree.verticalScrollBar().value()
    current = next((name for name, status, _ in rows if status == 'running'), None)
    changed_current = current and current != getattr(tree, '_active_module', None)
    tree._active_module = current
    tree.clear()
    for name, status, label in rows:
        item = QTreeWidgetItem(tree, [name, label])
        tree.setItemWidget(item, 1, status_badge(status, label))
    tree.verticalScrollBar().setValue(0 if changed_current else position)
