"""Server settings and game rules, as controls rather than a text file.

Both halves share a rule: never silently pretend a change took effect. Settings
that need a restart say so and stay pending; game rules changed while the server
is stopped are queued and applied the moment it next reports ready.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..serversettings import (
    SETTING_GROUPS,
    GameRule,
    Kind,
    Setting,
    settings_in,
)

HINT_STYLE = "color:#8b949e; font-size:11px;"
PENDING_STYLE = "color:#c9a227;"


class SettingRow(QWidget):
    """One setting, with its editor and a note when it needs a restart."""

    changed = Signal(object, str)  # (Setting, new value as a properties string)

    def __init__(self, setting: Setting, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setting = setting
        self._editor: QWidget

        if setting.kind is Kind.BOOL:
            self._editor = QCheckBox()
            self._editor.toggled.connect(
                lambda on: self.changed.emit(setting, "true" if on else "false")
            )
        elif setting.kind is Kind.CHOICE:
            self._editor = QComboBox()
            self._editor.addItems(list(setting.choices))
            self._editor.currentTextChanged.connect(
                lambda text: self.changed.emit(setting, text)
            )
        elif setting.kind is Kind.INT:
            self._editor = QSpinBox()
            self._editor.setRange(setting.minimum, setting.maximum or 2**31 - 1)
            self._editor.valueChanged.connect(
                lambda value: self.changed.emit(setting, str(value))
            )
        else:
            self._editor = QLineEdit()
            self._editor.editingFinished.connect(
                lambda: self.changed.emit(setting, self._editor.text())
            )

        self.note = QLabel("")
        self.note.setStyleSheet(PENDING_STYLE)
        self.note.setVisible(False)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)

        # Free-text fields fill the form; a trailing stretch would pin them to
        # their tiny size hint. Fixed-size controls keep the stretch so they stay
        # left-aligned rather than smearing across the row.
        expands = setting.kind is Kind.TEXT
        row.addWidget(self._editor, 1 if expands else 0)
        row.addWidget(self.note)
        if not expands:
            row.addStretch(1)

    def set_value(self, value: str) -> None:
        """Set without emitting - used when loading from the properties file."""
        self._editor.blockSignals(True)
        if isinstance(self._editor, QCheckBox):
            self._editor.setChecked(str(value).lower() == "true")
        elif isinstance(self._editor, QComboBox):
            index = self._editor.findText(str(value))
            self._editor.setCurrentIndex(max(0, index))
        elif isinstance(self._editor, QSpinBox):
            try:
                self._editor.setValue(int(value))
            except (TypeError, ValueError):
                pass
        else:
            self._editor.setText(str(value))
        self._editor.blockSignals(False)

    def mark_pending(self, pending: bool, reason: str = "restart needed") -> None:
        self.note.setText(f"- {reason}" if pending else "")
        self.note.setVisible(pending)


class GameRuleRow(QWidget):
    changed = Signal(object, str)  # (GameRule, command value)

    def __init__(self, rule: GameRule, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rule = rule

        if rule.numeric:
            self._editor = QSpinBox()
            self._editor.setRange(0, 2**31 - 1)
            self._editor.setValue(int(rule.value))
            self._editor.valueChanged.connect(
                lambda value: self.changed.emit(rule, str(value))
            )
        else:
            self._editor = QCheckBox()
            self._editor.setChecked(rule.as_bool)
            self._editor.toggled.connect(
                lambda on: self.changed.emit(rule, "true" if on else "false")
            )

        self.note = QLabel("")
        self.note.setStyleSheet(PENDING_STYLE)
        self.note.setVisible(False)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._editor)
        row.addWidget(self.note)
        row.addStretch(1)

    def mark_pending(self, pending: bool) -> None:
        self.note.setText("- applies when the server starts" if pending else "")
        self.note.setVisible(pending)


def _scrollable(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.NoFrame)
    area.setWidget(inner)
    return area


class ServerSettingsPanel(QWidget):
    """server.properties and game rules for the selected world."""

    setting_changed = Signal(object, str)  # (Setting, value)
    game_rule_changed = Signal(object, str)  # (GameRule, value)
    reload_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setting_rows: dict[str, SettingRow] = {}
        self._rule_rows: dict[str, GameRuleRow] = {}
        self._running = False

        self.tabs = QTabWidget()
        self.tabs.addTab(_scrollable(self._build_settings()), "Server")
        self._rules_host = QWidget()
        self._rules_layout = QVBoxLayout(self._rules_host)
        self.tabs.addTab(_scrollable(self._rules_host), "Game rules")

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(HINT_STYLE)

        self.reload_button = QPushButton("Reload from disk")
        self.reload_button.clicked.connect(self.reload_requested.emit)

        # Settings save the instant they are changed, which is not obvious from
        # a form with no Save button - so say so, every time.
        self.saved_label = QLabel("")
        self.saved_label.setStyleSheet("color:#5fb37a;")
        self._saved_timer = QTimer(self)
        self._saved_timer.setSingleShot(True)
        self._saved_timer.timeout.connect(lambda: self.saved_label.setText(""))

        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        footer.addWidget(self.saved_label)
        footer.addWidget(self.reload_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(footer)

    def _build_settings(self) -> QWidget:
        host = QWidget()
        outer = QVBoxLayout(host)

        for group_name in SETTING_GROUPS:
            group = QGroupBox(group_name)
            form = QFormLayout(group)
            form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

            for setting in settings_in(group_name):
                row = SettingRow(setting)
                row.changed.connect(self.setting_changed.emit)
                self._setting_rows[setting.key] = row

                label = QLabel(setting.label)
                if setting.help:
                    label.setToolTip(setting.help)
                    row.setToolTip(setting.help)
                form.addRow(label, row)

                # Everything has a tooltip; only the consequential ones spell it
                # out on the page.
                if setting.help and setting.inline_help:
                    hint = QLabel(setting.help)
                    hint.setStyleSheet(HINT_STYLE)
                    hint.setWordWrap(True)
                    form.addRow("", hint)

            outer.addWidget(group)

        self.seed_label = QLabel("World seed: unknown")
        self.seed_label.setStyleSheet(HINT_STYLE)
        self.seed_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(self.seed_label)

        outer.addStretch(1)
        return host

    # --- Population ---

    def load_properties(self, values: dict[str, str]) -> None:
        for key, row in self._setting_rows.items():
            row.set_value(values.get(key, row.setting.default))
            row.mark_pending(False)

    def set_seed(self, seed: int | None) -> None:
        self.seed_label.setText(
            f"World seed: {seed}" if seed is not None else "World seed: unknown"
        )

    def load_game_rules(self, rules: list[GameRule]) -> None:
        while self._rules_layout.count():
            item = self._rules_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rule_rows.clear()

        if not rules:
            message = QLabel(
                "No game rules found yet. They are created the first time the world "
                "is loaded - start the server once and they will appear here."
            )
            message.setWordWrap(True)
            message.setStyleSheet(HINT_STYLE)
            self._rules_layout.addWidget(message)
            self._rules_layout.addStretch(1)
            return

        from ..serversettings import COMMON_GAME_RULES

        common = QGroupBox("Commonly changed")
        common_form = QFormLayout(common)
        rest = QGroupBox("All game rules")
        rest_form = QFormLayout(rest)

        for rule in rules:
            row = GameRuleRow(rule)
            row.changed.connect(self.game_rule_changed.emit)
            self._rule_rows[rule.name] = row

            label = QLabel(rule.label)
            label.setToolTip(rule.name)
            row.setToolTip(rule.name)
            (common_form if rule.name in COMMON_GAME_RULES else rest_form).addRow(label, row)

        self._rules_layout.addWidget(common)
        self._rules_layout.addWidget(rest)
        self._rules_layout.addStretch(1)

    # --- State ---

    def set_server_running(self, running: bool) -> None:
        self._running = running
        if running:
            self.status.setText(
                "Changes marked 'restart needed' take effect next time the server "
                "starts. Everything else is applied immediately."
            )
        else:
            self.status.setText(
                "The server is stopped. Changes are saved now, and game rules are "
                "applied automatically when it next starts."
            )

    def flash_saved(self, message: str) -> None:
        """Confirm a write actually happened, then fade the message away."""
        self.saved_label.setText(message)
        self._saved_timer.start(4000)

    def mark_setting_pending(self, key: str, pending: bool, reason: str = "restart needed") -> None:
        row = self._setting_rows.get(key)
        if row:
            row.mark_pending(pending, reason)

    def mark_rule_pending(self, name: str, pending: bool) -> None:
        row = self._rule_rows.get(name)
        if row:
            row.mark_pending(pending)
