"""Inline pending-decision panel; no nested modal input loops."""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QPlainTextEdit, QLineEdit,
                              QPushButton, QHBoxLayout, QLabel)
from .interaction import rpc


class InputPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.request = None
        self.socket_path = None
        layout = QVBoxLayout(self)
        self.label = QLabel('')
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.context = QPlainTextEdit()
        self.context.setReadOnly(True)
        self.context.setMaximumHeight(180)
        layout.addWidget(self.context)
        from .spec_editor import SpecificationEditor
        self.spec_editor = SpecificationEditor()
        layout.addWidget(self.spec_editor)
        self.assumptions = QLabel()
        from PySide6.QtCore import Qt
        self.assumptions.setTextFormat(Qt.TextFormat.PlainText)
        self.assumptions.setWordWrap(True)
        self.assumptions.setStyleSheet('padding: 12px; background: #19643e; border: 1px solid #489565; border-radius: 6px;')
        layout.addWidget(self.assumptions)
        self.value = QLineEdit()
        layout.addWidget(self.value)
        buttons = QHBoxLayout()
        self.continue_button = QPushButton('Continue')
        self.revise_button = QPushButton('Revise')
        self.advice_button = QPushButton('Ask for frequency advice')
        self.cancel_button = QPushButton('Decline input and stop flow')
        for button in (self.continue_button, self.revise_button, self.advice_button, self.cancel_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.continue_button.clicked.connect(self.proceed)
        self.revise_button.clicked.connect(lambda: self.send('revise'))
        self.advice_button.clicked.connect(lambda: self.send('discuss'))
        self.cancel_button.clicked.connect(lambda: self.send('cancel'))
        self.hide()

    def display(self, path, request):
        if self.request and self.request['request_id'] == request['request_id']:
            return
        self.socket_path = path
        self.request = request
        kind, context = request['kind'], request['context']
        self.value.clear()
        self.value.setEchoMode(QLineEdit.EchoMode.Password if kind == 'credential' else QLineEdit.EchoMode.Normal)
        self.revise_button.setVisible(kind == 'spec_approval')
        self.advice_button.setVisible(kind == 'frequency')
        self.continue_button.setText('Approve specification' if kind == 'spec_approval' else 'Continue')
        self.spec_editor.setVisible(kind == 'spec_approval')
        self.assumptions.setVisible(kind == 'spec_approval')
        self.context.setVisible(kind != 'spec_approval')
        self.value.setPlaceholderText('Optional: describe broader changes, then choose Revise')
        if kind == 'spec_approval':
            import yaml
            self.spec_editor.load(yaml.safe_load(context['candidate']))
            self.assumptions.setText('Interpretation notes\n' + '\n'.join(context['assumptions']) +
                f'\nStarting preset: {context["preset"]} · Revision {context["revision"]}\nSelections below override the starting preset.')
            self.label.setText(context.get('validation_error') or 'Review your design. Adjust the selections, then approve to continue.')
            text = context['candidate'] + '\nAssumptions:\n' + '\n'.join(context['assumptions'])
            text += f'\nPreset: {context["preset"]} · Revision: {context["revision"]}'
            self.value.setText(context['description'])
        elif kind == 'credential':
            self.label.setText(f'SSH password for {context["username"]}@{context["host"]}')
            text = context.get('reason', 'Credentials remain in worker memory for this run.')
        elif kind == 'frequency':
            self.label.setText('Select controller target MHz. This is a target, not measured achieved frequency.')
            self.value.setText(str(context['target_mhz']))
            text = 'Approved clock constraints: ' + str(context['requirements'])
            text += '\nA different target leaves approved operating requirements unchanged.\n'
            text += '\n'.join(turn['content'] for turn in context['conversation'])
        else:
            self.label.setText('Revise the design description')
            text = context.get('reason', '')
            self.value.setText(context.get('description', ''))
        self.context.setPlainText(text)
        self.show()

    def proceed(self):
        if self.request['kind'] == 'frequency':
            from shared.top_orchestrator.frequency import positive_mhz
            try:
                positive_mhz(self.value.text())
            except ValueError as exc:
                self.label.setText(str(exc))
                return
        self.send('approve' if self.request['kind'] == 'spec_approval' else 'submit')

    def send(self, action):
        if action not in ('cancel', 'approve') and not self.value.text().strip():
            self.label.setText('Enter a value before continuing.')
            return
        response = dict(action=action, value=self.value.text())
        if action == 'approve':
            response['digest'] = self.request['context']['digest']
            try:
                response['configuration'] = self.spec_editor.configuration()
            except (ValueError, TypeError) as exc:
                self.label.setText(f'Please correct the selections: {exc}')
                return
        try:
            result = rpc(self.socket_path, 'respond', request_id=self.request['request_id'], response=response)
            if result.get('error'):
                self.label.setText(result['error'])
                return
        except (OSError, ValueError):
            self.label.setText('Worker unavailable. Response was not confirmed; retry when connected.')
            return
        self.value.clear()
        self.request = None
        self.hide()
