"""Stage inspection for a selected saved run; never attaches to a worker."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSplitter, QLabel
from .progress import FlowStatus
from .stage_details import StageDetails
from .live_output import LiveOutput
from .run_storage import worker_log_path, lifecycle_path
from .registry import read_record
from .run_reader import preview
from .node_graph import MAIN_NODES
from .action_button import ActionButton


class RunReview(QWidget):
    specification_requested = Signal()

    def __init__(self):
        super().__init__()
        self.run = None
        self.selected_stage = 'generate'
        layout = QVBoxLayout(self)
        self.flow_status = FlowStatus()
        # History needs the node map, while the persistent run header supplies
        # the outcome. Reserve the working area for evidence rather than an idle
        # animation and an empty progress bar.
        self.flow_status.forge.hide()
        self.flow_status.activity.hide()
        self.flow_status.bar.hide()
        layout.addWidget(self.flow_status)
        self.flow_status.graph.stage_clicked.connect(self.open_stage)
        controls = QHBoxLayout()
        self.caption = QLabel()
        self.caption.setObjectName('fieldHeading')
        controls.addWidget(self.caption, 1)
        self.spec = ActionButton('Approved Specification', icon_kind='approved-spec')
        self.spec.clicked.connect(self.specification_requested)
        controls.addWidget(self.spec)
        self.log_toggle = ActionButton('Show Run Log', icon_kind='run-log')
        self.log_toggle.setCheckable(True)
        controls.addWidget(self.log_toggle)
        layout.addLayout(controls)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(8)
        self.details = StageDetails()
        self.split.addWidget(self.details)
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        latest = QPushButton('Jump to Latest')
        latest.hide()
        log_layout.addWidget(latest, alignment=Qt.AlignmentFlag.AlignRight)
        self.log = LiveOutput()
        self.log.setProperty('technicalText', True)
        self.log.following_changed.connect(lambda following: latest.setVisible(not following))
        latest.clicked.connect(self.log.jump_to_latest)
        log_layout.addWidget(self.log)
        self.split.addWidget(log_panel)
        log_panel.hide()
        self.log_toggle.toggled.connect(log_panel.setVisible)
        self.log_toggle.toggled.connect(lambda checked: self.log_toggle.setText(
            'Hide Run Log' if checked else 'Show Run Log'))
        layout.addWidget(self.split, 1)

    def display(self, run):
        changed = self.run is None or self.run.directory != run.directory
        self.run = run
        if changed:
            self.log.reset_output()
            self.details._stage = None
            self.details._rows = None
        receipt = read_record(lifecycle_path(run.directory))
        from .history import run_status
        status, _ = run_status(run)
        self.flow_status.update_run(run.state, lifecycle=status)
        # Historic elapsed is shown only when both endpoints are recorded.
        started, finished = receipt.get('started'), receipt.get('finished')
        known_elapsed = isinstance(started, (int, float)) and isinstance(finished, (int, float))
        if known_elapsed:
            elapsed = max(0, int(finished - started))
            self.flow_status.elapsed_label.setText(f'Elapsed {elapsed // 60}m {elapsed % 60:02d}s')
        self.flow_status.elapsed_label.setVisible(known_elapsed)
        from .approved_spec import approved_spec_path
        self.spec.setEnabled(approved_spec_path(run.directory) is not None)
        if changed:
            keys = dict(MAIN_NODES)
            stage = run.state.get('current_stage')
            if stage not in keys:
                stage = next((r.get('stage') for r in reversed(run.results)
                              if r.get('stage') in keys), 'generate')
            self.selected_stage = stage
        self.open_stage(self.selected_stage)

    def open_stage(self, stage):
        if self.run is None:
            return
        self.selected_stage = stage
        self.caption.setText(dict(MAIN_NODES)[stage])
        self.details.display(self.run, stage)
        log = worker_log_path(self.run.directory)
        if not log.is_file():
            result = next((r for r in reversed(self.run.results) if r.get('stage') == stage), {})
            candidate = result.get('artifacts', {}).get('log')
            log = self.run.resolve(candidate) if isinstance(candidate, str) else log
        self.log.update_snapshot(preview(log) if log.is_file() else
                                 'No local log is recorded here. Browse Files for stage reports and other evidence.')
        for key, node in self.flow_status.graph.nodes.items():
            node.setProperty('inspecting', key == stage)
            node.style().unpolish(node)
            node.style().polish(node)
