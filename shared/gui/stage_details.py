"""Stage-scoped evidence without starting or modifying any worker."""
from PySide6.QtWidgets import QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView
from .cards import SectionCard
from .node_graph import MAIN_NODES
from .rtl_progress import module_progress, populate_module_tree, module_count_text
from .display_tree import DisplayTree

DESCRIPTIONS = {
    'generate': 'Produces RTL and runs the configured local simulation checks.',
    'validation': 'Checks the generated design. Module checks and system verification retain their recorded scope.',
    'backend': 'Builds the physical layout and runs configured equivalence checks.',
    'gdsii-validation': 'Checks the resulting layout using the configured GDSII validation flow.',
}


def evidence_rows(run, stage, *, alive=False):
    rows = []
    results = [r for r in run.results if r.get('stage') == stage]
    current = run.state.get('current_stage')
    if current == 'debug':
        current = 'generate'
    if alive and current == stage:
        status = 'In progress'
    elif results:
        from .problems import is_connection_error, CONNECTION_TITLE
        status = (CONNECTION_TITLE if is_connection_error(results[-1]) else
                  str(results[-1].get('status', 'unknown')).replace('_', ' ').capitalize())
    elif ((stage in ('backend', 'gdsii-validation') and run.config.get('no_backend'))
          or (stage == 'validation' and run.config.get('skip_verification'))):
        status = 'Skipped'
    else:
        status = 'Not reached'
    rows.append(('Status', status))
    if run.config.get('target_mhz'):
        rows.append(('Target clock', f'{run.config["target_mhz"]} MHz'))
    if stage == 'generate':
        rows.append(('Local simulation', 'Skipped by configuration' if run.config.get('no_bist') else
                     'Included in generation; see recorded outcome and log'))
    if stage == 'backend':
        rows.append(('Equivalence checking', 'Skipped by configuration' if run.config.get('skip_lec') else
                     'Enabled; results are recorded by the physical design stage'))
    for index, result in enumerate(results, 1):
        rows.append((f'Attempt {index}', str(result.get('message') or result.get('status', 'unknown'))))
        for key, value in result.get('artifacts', {}).items():
            rows.append((key.replace('_', ' ').capitalize(), str(value)))
    if not results:
        rows.append(('Evidence', 'Waiting for stage results.' if alive and current == stage else
                     'No results recorded for this stage.'))
    return rows


class StageDetails(SectionCard):
    def __init__(self):
        super().__init__('Stage Details', 'Click a flow node to inspect its evidence.')
        # The workspace header names the selected stage; avoid repeating it here.
        heading = self.content.itemAt(0).layout()
        for index in range(heading.count()):
            widget = heading.itemAt(index).widget()
            if widget:
                widget.hide()
        self.content.setContentsMargins(10, 8, 10, 8)
        self.content.setSpacing(8)
        self.pages = QTabWidget()
        self.modules = DisplayTree()
        self.modules.setHeaderLabels(['Module', 'Generation Status'])
        self.modules.setIndentation(0)
        self.modules.setWordWrap(True)
        self.modules.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.modules.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.evidence = DisplayTree()
        self.evidence.setHeaderLabels(['Detail', 'Recorded Evidence'])
        self.evidence.setIndentation(0)
        self.evidence.setWordWrap(True)
        self.evidence.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.evidence.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.pages.addTab(self.modules, 'Modules')
        self.pages.addTab(self.evidence, 'Stage Notes')
        self.agents = DisplayTree()
        self.agents.setExpandsOnDoubleClick(False)
        self.agents.itemClicked.connect(self.toggle_agent_details)
        self.agents.setHeaderLabels(['Verification Agent / Detail', 'Status / Evidence'])
        self.agents.setWordWrap(True)
        self.agents.setIndentation(18)
        self.agents.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.agents.setColumnWidth(0, 290)
        self.agents.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.pages.addTab(self.agents, 'Agents')
        self.pages.setTabVisible(2, False)
        self.physical = DisplayTree()
        self.physical.setHeaderLabels(['Physical Design Step / Detail', 'Status / Evidence'])
        self.physical.setWordWrap(True)
        self.physical.setExpandsOnDoubleClick(False)
        self.physical.itemClicked.connect(self.toggle_agent_details)
        self.physical.setColumnWidth(0, 290)
        self.physical.header().setStretchLastSection(True)
        self.pages.addTab(self.physical, 'Physical Steps')
        self.pages.setTabVisible(3, False)
        self.add_widget(self.pages, 1)
        self._rows = None
        self._stage = None

    def toggle_agent_details(self, item, column):
        if column == 0 and item.parent() is None and item.childCount():
            item.setExpanded(not item.isExpanded())

    def display(self, run, stage, *, alive=False):
        if getattr(self, '_run_directory', None) != run.directory:
            self._run_directory = run.directory
            self._stage = self._rows = None
            self.agents.clear()
            self.agents._agents = None
            self.physical.clear()
            self.physical._agents = None
        self.title.setText(dict(MAIN_NODES)[stage])
        self.pages.setTabVisible(2, stage == 'validation')
        self.pages.setTabVisible(3, stage == 'backend')
        rows = evidence_rows(run, stage, alive=alive)
        modules = module_progress(run, alive=alive and run.state.get('current_stage') in ('generate', 'debug'))
        if stage == 'generate' and modules:
            had_modules = self.pages.isTabVisible(0)
            self.description.setText(module_count_text(modules))
            populate_module_tree(self.modules, modules)
            self.pages.setTabVisible(0, True)
            if self._stage != stage or not had_modules:
                self.pages.setCurrentWidget(self.modules)
        else:
            self.description.setText(DESCRIPTIONS[stage])
            self.pages.setTabVisible(0, False)
            if stage == 'validation':
                from .verification_details import verification_agents, populate_agents
                populate_agents(self.agents, verification_agents(run, alive=alive))
                if self._stage != stage:
                    self.pages.setCurrentWidget(self.agents)
            elif stage == 'backend':
                from .physical_details import physical_steps
                from .verification_details import populate_agents
                populate_agents(self.physical, physical_steps(run, alive=alive))
                if self._stage != stage:
                    self.pages.setCurrentWidget(self.physical)
            else:
                self.pages.setCurrentWidget(self.evidence)
        self._stage = stage
        if rows != self._rows:
            self._rows = rows
            self.evidence.clear()
            for key, value in rows:
                item = QTreeWidgetItem(self.evidence, [key, value])
                item.setToolTip(1, value)
