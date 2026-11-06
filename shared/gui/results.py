"""Curated result summaries backed by bounded, run-local evidence reads."""
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                              QScrollArea, QTreeWidgetItem, QGridLayout, QFrame)
from .cards import SectionCard, status_badge
from .display_tree import DisplayTree
from .node_graph import MAIN_NODES
from .stage_details import evidence_rows
from .verification_details import small_json, validation_directory
from .physical_details import backend_report, measured_results


def verification_report(run):
    paths = sorted(validation_directory(run).glob(
        'reports/rtl/verification_reports/*/final_system_report.json'))
    path = paths[-1] if paths else None
    return path, small_json(path) if path else {}


def label(text):
    widget = QLabel(str(text))
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


class ResultsPanel(QScrollArea):
    stage_requested = Signal(str)
    evidence_requested = Signal(object)
    specification_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.setWidget(body)
        self._signature = None
        self.clear()

    def clear(self):
        self._signature = None
        self._remove_cards()
        self.body.addWidget(SectionCard('Results', 'Start a design or select a saved run to see its results.'))
        self.body.addStretch()

    def _remove_cards(self):
        self.stage_grid = None
        self.stage_cards = []
        self._columns = None
        while self.body.count():
            item = self.body.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange_stages()

    def arrange_stages(self):
        if not getattr(self, 'stage_grid', None):
            return
        columns = 2 if self.viewport().width() >= 860 else 1
        if self._columns == columns:
            return
        self._columns = columns
        while self.stage_grid.count():
            self.stage_grid.takeAt(0)
        for index, card in enumerate(self.stage_cards):
            self.stage_grid.addWidget(card, index // columns, index % columns)
        self.stage_grid.setColumnStretch(0, 1)
        self.stage_grid.setColumnStretch(1, 1 if columns == 2 else 0)

    def add_metrics(self, verification):
        if not verification:
            return
        metrics = []
        requirements = verification.get('requirements', {})
        if isinstance(requirements, dict) and type(requirements.get('passed')) is int:
            counts = [requirements.get(key) for key in ('passed', 'failed', 'not_tested')]
            total = sum(counts) if all(type(n) is int and n >= 0 for n in counts) else None
            value = f'{counts[0]} / {total}' if total is not None else f'{counts[0]} Passed'
            metrics.append(('Requirements', value, 'Reported verification checks'))
        vectors = verification.get('vectors', {})
        if isinstance(vectors, dict) and type(vectors.get('passed')) is int:
            metrics.append(('Test Vectors', f'{vectors["passed"]} Passed', 'Recorded vector outcomes'))
        gaps = verification.get('coverage_limitations')
        if isinstance(gaps, list):
            metrics.append(('Coverage Limitations', f'{len(gaps)} Reported', 'See verification scope and gaps'))
        if not metrics:
            return
        row = QWidget()
        grid = QGridLayout(row)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        for index, (title, value, detail) in enumerate(metrics):
            card = QFrame()
            card.setObjectName('metricCard')
            content = QVBoxLayout(card)
            content.setContentsMargins(16, 14, 16, 14)
            for text, name in [(title.upper(), 'metricHeading'), (value, 'metricValue'), (detail, 'metricDetail')]:
                widget = label(text)
                widget.setObjectName(name)
                content.addWidget(widget)
            grid.addWidget(card, 0, index)
            grid.setColumnStretch(index, 1)
        self.body.addWidget(row)

    def display(self, run):
        from .history import run_status, hardware_summary, LABELS
        from .approved_spec import approved_spec_path
        from .problems import is_connection_error, CONNECTION_REASON
        from .run_storage import lifecycle_path
        from .registry import read_record
        status, _ = run_status(run)
        report_path, verification = verification_report(run)
        backend_path, backend = backend_report(run)
        signature = (str(run.directory), run.state, status, verification, backend)
        if signature == self._signature:
            return
        self._signature = signature
        position = self.verticalScrollBar().value()
        self._remove_cards()
        summary = SectionCard('Design Results', hardware_summary(run))
        summary.add_widget(status_badge(status, LABELS.get(status, status.replace('_', ' ').title())))
        summary.add_widget(label('Target speed is a design request. Achieved timing requires physical timing evidence.'))
        if run.error:
            summary.add_widget(label('Saved evidence could not be read: ' + run.error))
        spec = QPushButton('Approved Specification')
        spec.setEnabled(approved_spec_path(run.directory) is not None)
        spec.clicked.connect(self.specification_requested)
        summary.content.itemAt(0).layout().insertWidget(1, spec)
        self.body.addWidget(summary)

        issues = []
        for stage, title in MAIN_NODES:
            latest = next((r for r in reversed(run.results) if r.get('stage') == stage), {})
            if latest.get('status') in ('failed', 'needs_attention', 'interrupted'):
                reason = CONNECTION_REASON if is_connection_error(latest) else latest.get('message', 'Review stage evidence.')
                issues.append(f'{title}: {reason}')
        gaps = verification.get('coverage_limitations', [])
        gaps = gaps if isinstance(gaps, list) else []
        cleanup = read_record(lifecycle_path(run.directory)).get('remote_cleanup', {})
        if cleanup.get('status') == 'cleanup_unconfirmed':
            issues.append('Remote cleanup is unconfirmed. Inspect the lifecycle receipt before retrying remote work.')
        if gaps or verification.get('status') == 'PASS_WITH_GAPS':
            issues.append(f'Verification passed with gaps. {len(gaps)} coverage limitations reported; physical timing is a separate check.')
        if issues:
            attention = SectionCard('Attention and Limitations', 'Review these before using the generated design.')
            attention.setProperty('emphasis', 'attention')
            attention.add_widget(status_badge('needs_attention', 'Review Required'))
            for issue in issues:
                attention.add_widget(label(issue))
            if gaps:
                toggle = QPushButton(f'Show Coverage Limitations ({len(gaps)})')
                toggle.setCheckable(True)
                details = DisplayTree()
                details.setHeaderLabels(['Timing Rule', 'Limitation'])
                details.setMinimumHeight(180)
                for gap in gaps:
                    if isinstance(gap, dict):
                        QTreeWidgetItem(details, [str(gap.get('timing_rule', '')), str(gap.get('reason', ''))])
                    else:
                        QTreeWidgetItem(details, ['', str(gap)])
                details.header().setStretchLastSection(True)
                details.setWordWrap(True)
                details.hide()
                toggle.toggled.connect(details.setVisible)
                attention.add_widget(toggle)
                attention.add_widget(details)
            self.body.addWidget(attention)

        self.add_metrics(verification)
        stages = QWidget()
        self.stage_grid = QGridLayout(stages)
        self.stage_grid.setContentsMargins(0, 0, 0, 0)
        self.stage_grid.setSpacing(12)
        self.body.addWidget(stages)
        for stage, title in MAIN_NODES:
            state = evidence_rows(run, stage)[0][1]
            if status == 'running' and run.state.get('current_stage') == stage:
                state = 'In progress'
            card = SectionCard(title, state)
            if stage == 'generate':
                from .rtl_progress import module_progress, module_count_text
                modules = module_progress(run)
                card.add_widget(label(module_count_text(modules) if modules else 'Module evidence unavailable.'))
                card.add_widget(label('RTL generation and local simulation outcomes apply to their recorded scope.'))
            elif stage == 'validation':
                if verification:
                    card.add_widget(status_badge('needs_attention' if verification.get('status') == 'PASS_WITH_GAPS' else
                                                 'passed' if verification.get('status') == 'PASS' else 'failed',
                                                 str(verification.get('status', 'Unknown')).replace('_', ' ').title()))
                    for key, title_text in [('requirements', 'Requirements'), ('vectors', 'Test Vectors')]:
                        values = verification.get(key, {})
                        if isinstance(values, dict) and values:
                            card.add_widget(label(title_text + ': ' + ' · '.join(
                                f'{name.replace("_", " ")}: {value}' for name, value in values.items()
                                if isinstance(value, (int, float)))))
                    simulation = verification.get('simulation', {})
                    if isinstance(simulation, dict):
                        card.add_widget(label(f'UVM errors: {simulation.get("uvm_error", "Not reported")} · '
                                              f'UVM fatal: {simulation.get("uvm_fatal", "Not reported")}'))
                else:
                    card.add_widget(label('Verification summary not available yet. Open stage details for agent activity and partial evidence.'))
            elif stage == 'backend':
                measurements = measured_results(backend)
                for title_text, value in measurements:
                    card.add_widget(label(f'{title_text}: {value}'))
                if not measurements:
                    card.add_widget(label('Timing, area, and power measurements are not available in this attempt’s summary.'))
                card.add_widget(label('Measured values retain their report conditions and build scope. Target speed alone does not establish timing closure.'))
                if backend.get('last_error'):
                    card.add_widget(label(backend['last_error']))
            else:
                card.add_widget(label('Layout validation applies to the delivered layout and the checks recorded in its report.'))
            controls = QHBoxLayout()
            inspect = QPushButton('Open Stage Details')
            inspect.clicked.connect(lambda checked=False, key=stage: self.stage_requested.emit(key))
            controls.addWidget(inspect)
            artifact = report_path if stage == 'validation' else backend_path if stage == 'backend' else None
            if artifact is None:
                result = next((r for r in reversed(run.results) if r.get('stage') == stage), {})
                values = result.get('artifacts', {})
                candidate = next((values[k] for k in ('result', 'report', 'log')
                                  if isinstance(values.get(k), str)), None)
                artifact = run.resolve(candidate) if candidate else None
            evidence = QPushButton('View Evidence')
            evidence.setEnabled(artifact is not None and artifact.exists())
            evidence.clicked.connect(lambda checked=False, path=artifact: self.evidence_requested.emit(path))
            controls.addWidget(evidence)
            controls.addStretch()
            card.content.addLayout(controls)
            self.stage_cards.append(card)
        self.arrange_stages()
        deliverables = SectionCard('Deliverables', 'Run files and published outputs are available in Files. Missing files are never counted as delivered.')
        delivered = {}
        for result in run.results:
            for key, value in result.get('artifacts', {}).items():
                if isinstance(value, str) and key in ('mailbox', 'gds_file', 'reports_zip', 'rtl_zip'):
                    path = run.resolve(value)
                    from pathlib import Path
                    if not path.exists() and Path(value).is_absolute():
                        path = Path(value)
                    if path.exists():
                        delivered[path] = key
        for path, key in delivered.items():
            button = QPushButton('Published RTL Snapshot' if key == 'mailbox' else path.name)
            button.clicked.connect(lambda checked=False, target=path: self.evidence_requested.emit(target))
            deliverables.add_widget(button)
        if not delivered:
            deliverables.add_widget(label('No published RTL or layout delivery is recorded locally. Stage reports may still be available in Files.'))
        self.body.addWidget(deliverables)
        self.body.addStretch()
        self.verticalScrollBar().setValue(position)
