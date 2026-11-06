"""Inline pending-decision panel; no nested modal input loops."""
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QFrame, QVBoxLayout, QPlainTextEdit, QLineEdit,
                              QPushButton, QHBoxLayout, QLabel)
from .interaction import rpc


class InputPanel(QFrame):
    decision_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('decisionPanel')
        self.request = None
        self.socket_path = None
        layout = QVBoxLayout(self)
        self.heading = QLabel('Waiting for you')
        self.heading.setObjectName('decisionHeading')
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.label = QLabel('')
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        # Keep the decision buttons visible before the potentially tall editor.
        buttons = QHBoxLayout()
        self.continue_button = QPushButton('Continue')
        self.continue_button.setObjectName('approvalAction')
        self.advice_button = QPushButton('Ask for Advice')
        self.cancel_button = QPushButton('Decline and Stop')
        self.cancel_button.setObjectName('dangerAction')
        for button in (self.continue_button, self.advice_button, self.cancel_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.context = QPlainTextEdit()
        self.context.setReadOnly(True)
        self.context.setMaximumHeight(180)
        layout.addWidget(self.context)
        from .spec_editor import SpecificationEditor
        self.spec_editor = SpecificationEditor()
        layout.addWidget(self.spec_editor)
        self.assumptions = QLabel()
        self.assumptions.setTextFormat(Qt.TextFormat.PlainText)
        self.assumptions.setWordWrap(True)
        self.assumptions.setObjectName('interpretationNotes')
        layout.addWidget(self.assumptions)
        self.value = QLineEdit()
        from .decoration import password_eye_icon
        self.password_visibility = self.value.addAction(password_eye_icon(), QLineEdit.ActionPosition.TrailingPosition)
        self.password_visibility.setText('Show password')
        self.password_visibility.setToolTip('Show password')
        self.password_visibility.setCheckable(True)
        self.password_visibility.setVisible(False)
        self.password_visibility.toggled.connect(self.toggle_password)
        layout.addWidget(self.value)
        self.continue_button.clicked.connect(self.proceed)
        self.value.returnPressed.connect(self.proceed)
        self.advice_button.clicked.connect(lambda: self.send('discuss'))
        self.cancel_button.clicked.connect(lambda: self.send('cancel'))
        self.hide()

    def display(self, path, request):
        if self.request and self.request['request_id'] == request['request_id']:
            return
        self.socket_path = path
        self.request = request
        kind, context = request['kind'], request['context']
        self.heading.setText({
            'spec_approval': 'Waiting for you — approve your design',
            'credential': 'Waiting for you — enter your server password',
            'frequency': 'Waiting for you — confirm the target speed',
            'description': 'Waiting for you — revise your description',
        }.get(kind, 'Waiting for you — input required'))
        self.value.clear()
        self.password_visibility.setChecked(False)
        self.password_visibility.setVisible(kind == 'credential')
        self.value.setEchoMode(QLineEdit.EchoMode.Password if kind == 'credential' else QLineEdit.EchoMode.Normal)
        self.value.setVisible(kind != 'spec_approval')
        self.advice_button.setVisible(kind == 'frequency')
        self.continue_button.setText('Approve Design' if kind == 'spec_approval' else 'Continue')
        self.spec_editor.setVisible(kind == 'spec_approval')
        self.assumptions.setVisible(kind == 'spec_approval')
        self.context.setVisible(kind not in ('spec_approval', 'credential'))
        layout = self.layout()
        layout.removeWidget(self.value)
        if kind == 'credential':
            layout.insertWidget(2, self.value)
        else:
            layout.addWidget(self.value)
        self.value.setPlaceholderText('Enter your response')
        if kind == 'spec_approval':
            import yaml
            self.spec_editor.load(yaml.safe_load(context['candidate']))
            self.assumptions.setText('Interpretation notes\n' + '\n'.join(context['assumptions']) +
                f'\nStarting preset: {context["preset"]} · Revision {context["revision"]}\nSelections below override the starting preset.')
            self.label.setText(context.get('validation_error') or 'Review your design. Adjust the selections, then approve to continue.')
            text = context['candidate'] + '\nAssumptions:\n' + '\n'.join(context['assumptions'])
            text += f'\nPreset: {context["preset"]} · Revision: {context["revision"]}'
        elif kind == 'credential':
            text = context.get('reason', 'Credentials remain in worker memory for this run.')
            self.label.setText(f'Connect to {context["username"]}@{context["host"]}\n{text}')
            self.value.setPlaceholderText('Server password')
            self.continue_button.setText('Connect')
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
        self.decision_changed.emit()
        if kind == 'credential':
            self.value.setFocus()

    def clear_request(self):
        self.value.clear()
        self.password_visibility.setChecked(False)
        self.password_visibility.setVisible(False)
        self.request = None
        self.hide()

    def toggle_password(self, visible):
        if self.request and self.request['kind'] == 'credential':
            self.value.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        from .decoration import password_eye_icon
        self.password_visibility.setIcon(password_eye_icon(visible))
        text = 'Hide password' if visible else 'Show password'
        self.password_visibility.setText(text)
        self.password_visibility.setToolTip(text)

    def proceed(self):
        if self.request is None:
            return
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
        self.clear_request()
