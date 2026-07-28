"""Server settings and game rules, as controls rather than a text file.

Both halves share a rule: never silently pretend a change took effect. Settings
that need a restart say so and stay pending; game rules changed while the server
is stopped are queued and applied the moment it next reports ready.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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


class WhitelistEditor(QGroupBox):
    """The whitelist as a list you can actually edit.

    Names can be added for people who have never connected: when the server is
    running the name is resolved through it, and when it is stopped the addition
    is queued and applied on the next start. Writing a name into whitelist.json
    without a UUID is deliberately avoided - the server would not reliably
    honour it.
    """

    add_requested = Signal(str)
    remove_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Whitelisted players", parent)

        self.list = QListWidget()
        self.list.setMaximumHeight(130)
        self.list.itemSelectionChanged.connect(self._update_buttons)

        self.entry = QLineEdit(placeholderText="Minecraft username")
        self.entry.returnPressed.connect(self._add)
        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)

        row = QHBoxLayout()
        row.addWidget(self.entry, 1)
        row.addWidget(add_button)
        row.addWidget(self.remove_button)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(HINT_STYLE)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list)
        layout.addLayout(row)
        layout.addWidget(self.hint)
        self._update_buttons()

    def set_names(self, names: list[str], queued: list[str], running: bool) -> None:
        self.list.clear()
        for name in sorted(names, key=str.lower):
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            self.list.addItem(item)
        for name in sorted(queued, key=str.lower):
            item = QListWidgetItem(f"{name}   (added on next start)")
            item.setData(Qt.UserRole, name)
            item.setForeground(Qt.darkYellow)
            self.list.addItem(item)

        if not names and not queued:
            placeholder = QListWidgetItem("Nobody is whitelisted yet.")
            placeholder.setFlags(Qt.NoItemFlags)
            self.list.addItem(placeholder)

        self.hint.setText(
            "Names are checked against Mojang when added, so people who have never "
            "joined can be whitelisted in advance."
            if running
            else "The server is stopped, so additions are queued and applied when it "
            "next starts."
        )
        self._update_buttons()

    def _add(self) -> None:
        name = self.entry.text().strip()
        if name:
            self.entry.clear()
            self.add_requested.emit(name)

    def _remove(self) -> None:
        item = self.list.currentItem()
        if item and item.data(Qt.UserRole):
            self.remove_requested.emit(item.data(Qt.UserRole))

    def _update_buttons(self) -> None:
        item = self.list.currentItem()
        self.remove_button.setEnabled(bool(item and item.data(Qt.UserRole)))


class ServerIconPicker(QGroupBox):
    """Sets server-icon.png, the image shown in the multiplayer list."""

    icon_chosen = Signal(str)
    icon_cleared = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Server icon", parent)

        self.preview = QLabel()
        self.preview.setFixedSize(64, 64)
        self.preview.setStyleSheet("border:1px solid #2f343b;")
        self.preview.setAlignment(Qt.AlignCenter)

        choose = QPushButton("Choose image...")
        choose.clicked.connect(self._choose)
        self.clear_button = QPushButton("Remove")
        self.clear_button.clicked.connect(self.icon_cleared.emit)

        hint = QLabel(
            "Any image works - it is scaled to the 64x64 PNG Minecraft expects. "
            "Players see it next to the server in their list."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(HINT_STYLE)

        buttons = QVBoxLayout()
        buttons.addWidget(choose)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)

        row = QHBoxLayout()
        row.addWidget(self.preview)
        row.addLayout(buttons)
        row.addWidget(hint, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(row)

    def set_icon(self, path: Path | None) -> None:
        if path and Path(path).is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                self.preview.setPixmap(
                    pixmap.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
                self.clear_button.setEnabled(True)
                return
        self.preview.clear()
        self.preview.setText("none")
        self.clear_button.setEnabled(False)

    def _choose(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a server icon", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif)"
        )
        if chosen:
            self.icon_chosen.emit(chosen)


def _scrollable(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.NoFrame)
    area.setWidget(inner)
    return area


class ServerSettingsPanel(QWidget):
    """server.properties and game rules, saved when you say so.

    Edits are held as pending changes rather than written on every keystroke, so
    a half-typed MOTD never reaches the file and it is always clear what has and
    has not been committed. Rows with unsaved edits are marked, and Save is only
    enabled when there is something to save.
    """

    save_requested = Signal()
    save_and_restart_requested = Signal()
    refresh_requested = Signal()
    pending_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setting_rows: dict[str, SettingRow] = {}
        self._rule_rows: dict[str, GameRuleRow] = {}
        self._running = False

        # key -> new value, awaiting Save.
        self.pending_settings: dict[str, str] = {}
        self.pending_rules: dict[str, str] = {}

        self.tabs = QTabWidget()
        self.tabs.addTab(_scrollable(self._build_settings()), "Server")
        self._rules_host = QWidget()
        self._rules_layout = QVBoxLayout(self._rules_host)
        self.tabs.addTab(_scrollable(self._rules_host), "Game rules")

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(HINT_STYLE)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip(
            "Re-read the settings from the files on disk, discarding anything "
            "unsaved here."
        )
        self.refresh_button.clicked.connect(self.refresh_requested.emit)

        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.save_requested.emit)
        self.save_button.setEnabled(False)

        self.save_restart_button = QPushButton("Save and restart")
        self.save_restart_button.clicked.connect(self.save_and_restart_requested.emit)
        self.save_restart_button.setVisible(False)
        self.save_restart_button.setToolTip(
            "Some of these changes only take effect on startup. This saves them "
            "and restarts the server now."
        )

        self.saved_label = QLabel("")
        self.saved_label.setStyleSheet("color:#5fb37a;")
        self._saved_timer = QTimer(self)
        self._saved_timer.setSingleShot(True)
        self._saved_timer.timeout.connect(lambda: self.saved_label.setText(""))

        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        footer.addWidget(self.saved_label)
        footer.addWidget(self.refresh_button)
        footer.addWidget(self.save_button)
        footer.addWidget(self.save_restart_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(footer)

    # --- Pending edits ---

    @property
    def has_pending(self) -> bool:
        return bool(self.pending_settings or self.pending_rules)

    def pending_needs_restart(self) -> bool:
        return any(
            row.setting.needs_restart
            for key, row in self._setting_rows.items()
            if key in self.pending_settings
        )

    def _on_setting_edited(self, setting: Setting, value: str) -> None:
        self.pending_settings[setting.key] = value
        self._setting_rows[setting.key].mark_pending(True, "unsaved")
        if setting.key == "white-list":
            # Reflect the toggle immediately - waiting for Save would make the
            # editor feel broken.
            self.whitelist.setVisible(value == "true")
        self._update_actions()

    def _on_rule_edited(self, rule: GameRule, value: str) -> None:
        self.pending_rules[rule.name] = value
        self._rule_rows[rule.name].mark_pending(True, "unsaved")
        self._update_actions()

    def clear_pending(self) -> None:
        for key in list(self.pending_settings):
            row = self._setting_rows.get(key)
            if row:
                row.mark_pending(False)
        for name in list(self.pending_rules):
            row = self._rule_rows.get(name)
            if row:
                row.mark_pending(False)
        self.pending_settings.clear()
        self.pending_rules.clear()
        self._update_actions()

    def _update_actions(self) -> None:
        count = len(self.pending_settings) + len(self.pending_rules)
        self.save_button.setEnabled(count > 0)
        self.save_button.setText(f"Save ({count})" if count else "Save")

        needs_restart = self.pending_needs_restart() and self._running
        self.save_restart_button.setVisible(needs_restart)
        if needs_restart:
            self.save_button.setToolTip(
                "Saves now. Settings that need a restart will apply the next time "
                "the server starts."
            )
        else:
            self.save_button.setToolTip("Write these changes to the server's settings.")
        self.pending_changed.emit()

    def _build_settings(self) -> QWidget:
        host = QWidget()
        outer = QVBoxLayout(host)

        for group_name in SETTING_GROUPS:
            group = QGroupBox(group_name)
            form = QFormLayout(group)
            form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

            for setting in settings_in(group_name):
                row = SettingRow(setting)
                row.changed.connect(self._on_setting_edited)
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

            # Two controls that aren't properties but belong with their group.
            if group_name == "Appearance":
                self.icon_picker = ServerIconPicker()
                outer.addWidget(self.icon_picker)
            elif group_name == "Players":
                self.whitelist = WhitelistEditor()
                # Only meaningful when the whitelist is actually switched on.
                self.whitelist.setVisible(False)
                outer.addWidget(self.whitelist)

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
        self.pending_settings.clear()
        self.whitelist.setVisible(str(values.get("white-list", "false")).lower() == "true")
        self._update_actions()

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
            row.changed.connect(self._on_rule_edited)
            self._rule_rows[rule.name] = row

            label = QLabel(rule.label)
            label.setToolTip(rule.help)
            row.setToolTip(rule.help)
            (common_form if rule.name in COMMON_GAME_RULES else rest_form).addRow(label, row)

        self._rules_layout.addWidget(common)
        self._rules_layout.addWidget(rest)
        self._rules_layout.addStretch(1)

    # --- State ---

    def set_server_running(self, running: bool) -> None:
        self._running = running
        if running:
            self.status.setText(
                "Press Save to write your changes. Anything marked 'restart needed' "
                "applies when the server next starts; everything else takes effect "
                "straight away."
            )
        else:
            self.status.setText(
                "The server is stopped. Press Save to write your changes - game "
                "rules are applied automatically when it next starts."
            )
        self._update_actions()

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
