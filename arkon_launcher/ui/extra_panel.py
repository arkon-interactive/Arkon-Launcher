"""Odds and ends that don't belong to server.properties or the game rules.

Currently the join broadcast and scheduled restarts. Both are launcher features
rather than Minecraft ones - the server has no notion of either - so they live
here rather than being smuggled into the settings form.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

HINT = "color:#8b949e;"

# A restart is disruptive enough that hourly is the shortest sensible cadence.
RESTART_INTERVALS = (6, 12, 24, 48, 72, 168)


def describe_hours(hours: int) -> str:
    if hours % 24 == 0:
        days = hours // 24
        if days == 7:
            return "Every week"
        return f"Every {days} day{'s' if days != 1 else ''}"
    return f"Every {hours} hour{'s' if hours != 1 else ''}"


def describe_restart_lead(seconds: int) -> str:
    """Lead times for restarts are entered in hours or days, not minutes."""
    if seconds >= 86400 and seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


class ExtraPanel(QWidget):
    join_broadcast_changed = Signal(bool, str)
    restart_schedule_changed = Signal(bool, int)
    restart_announcements_changed = Signal(bool, list)
    restart_countdown_changed = Signal(bool, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # --- Join broadcast ---
        self.join_box = QGroupBox("Greet players when they join")
        self.join_box.setCheckable(True)
        self.join_box.setChecked(False)
        self.join_box.toggled.connect(self._emit_join)

        self.join_message = QLineEdit()
        self.join_message.setPlaceholderText("Welcome to the server, {player}!")
        self.join_message.editingFinished.connect(self._emit_join)

        join_hint = QLabel(
            "Sent to everyone online whenever somebody connects. Use <b>{player}</b> "
            "for their name."
        )
        join_hint.setWordWrap(True)
        join_hint.setStyleSheet(HINT)

        join_layout = QVBoxLayout(self.join_box)
        join_layout.addWidget(self.join_message)
        join_layout.addWidget(join_hint)

        # --- Scheduled restarts ---
        self.restart_box = QGroupBox("Restart the server automatically")
        self.restart_box.setCheckable(True)
        self.restart_box.setChecked(False)
        self.restart_box.toggled.connect(self._emit_schedule)

        self.interval = QComboBox()
        for hours in RESTART_INTERVALS:
            self.interval.addItem(describe_hours(hours), hours)
        self.interval.setCurrentIndex(RESTART_INTERVALS.index(24))
        self.interval.currentIndexChanged.connect(self._emit_schedule)

        self.next_run = QLabel("")
        self.next_run.setStyleSheet(HINT)

        interval_row = QHBoxLayout()
        interval_row.addWidget(self.interval)
        interval_row.addWidget(self.next_run, 1)

        restart_hint = QLabel(
            "A periodic restart clears memory a long-running modded server tends to "
            "leak. The world is saved first, exactly as a manual restart would."
        )
        restart_hint.setWordWrap(True)
        restart_hint.setStyleSheet(HINT)

        # Warnings, entered in hours or days.
        self.warnings = QListWidget()
        self.warnings.setMaximumHeight(96)
        self.warnings.itemSelectionChanged.connect(self._update_buttons)

        self.warn_enabled = QCheckBox("Warn players beforehand")
        self.warn_enabled.setChecked(True)
        self.warn_enabled.toggled.connect(self._emit_announcements)

        self.amount = QSpinBox()
        self.amount.setRange(1, 90)
        self.amount.setValue(1)
        self.unit = QComboBox()
        self.unit.addItems(["hours", "days", "minutes"])

        add_button = QPushButton("Add warning")
        add_button.clicked.connect(self._add_warning)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_warning)

        warn_row = QHBoxLayout()
        warn_row.addWidget(self.amount)
        warn_row.addWidget(self.unit)
        warn_row.addWidget(add_button)
        warn_row.addWidget(self.remove_button)
        warn_row.addStretch(1)

        # Final countdown, in seconds.
        self.countdown_enabled = QCheckBox("Count down out loud in the last")
        self.countdown_enabled.setChecked(True)
        self.countdown_enabled.toggled.connect(self._emit_countdown)

        self.countdown_seconds = QSpinBox()
        self.countdown_seconds.setRange(3, 60)
        self.countdown_seconds.setValue(10)
        self.countdown_seconds.setSuffix(" seconds")
        self.countdown_seconds.valueChanged.connect(self._emit_countdown)

        countdown_row = QHBoxLayout()
        countdown_row.addWidget(self.countdown_enabled)
        countdown_row.addWidget(self.countdown_seconds)
        countdown_row.addStretch(1)

        restart_layout = QVBoxLayout(self.restart_box)
        restart_layout.addLayout(interval_row)
        restart_layout.addWidget(restart_hint)
        restart_layout.addWidget(self.warn_enabled)
        restart_layout.addWidget(self.warnings)
        restart_layout.addLayout(warn_row)
        restart_layout.addLayout(countdown_row)

        layout = QVBoxLayout(self)
        layout.addWidget(self.join_box)
        layout.addWidget(self.restart_box)
        layout.addStretch(1)

        self._update_buttons()

    # --- Settings in/out ---

    def load_settings(self, settings) -> None:
        for widget in (self.join_box, self.restart_box, self.warn_enabled,
                       self.countdown_enabled, self.interval, self.countdown_seconds):
            widget.blockSignals(True)

        self.join_box.setChecked(settings.join_broadcast_enabled)
        self.join_message.setText(settings.join_broadcast_message)

        self.restart_box.setChecked(settings.restart_schedule_enabled)
        index = self.interval.findData(settings.restart_interval_hours)
        self.interval.setCurrentIndex(index if index >= 0 else 2)

        self.warn_enabled.setChecked(settings.restart_announce_enabled)
        self.countdown_enabled.setChecked(settings.restart_countdown_enabled)
        self.countdown_seconds.setValue(settings.restart_countdown_seconds)

        for widget in (self.join_box, self.restart_box, self.warn_enabled,
                       self.countdown_enabled, self.interval, self.countdown_seconds):
            widget.blockSignals(False)

        self._set_warnings(settings.restart_announcements)

    def _set_warnings(self, seconds: list[int]) -> None:
        self.warnings.clear()
        for value in sorted(set(seconds), reverse=True):
            item = QListWidgetItem(f"{describe_restart_lead(value)} before")
            item.setData(Qt.UserRole, value)
            self.warnings.addItem(item)
        if not seconds:
            placeholder = QListWidgetItem("No warnings - the restart is unannounced.")
            placeholder.setFlags(Qt.NoItemFlags)
            self.warnings.addItem(placeholder)
        self._update_buttons()

    def announcements(self) -> list[int]:
        return sorted(
            {
                self.warnings.item(i).data(Qt.UserRole)
                for i in range(self.warnings.count())
                if self.warnings.item(i).data(Qt.UserRole)
            },
            reverse=True,
        )

    def set_next_run(self, text: str) -> None:
        self.next_run.setText(text)

    # --- Actions ---

    def _add_warning(self) -> None:
        multiplier = {"hours": 3600, "days": 86400, "minutes": 60}[self.unit.currentText()]
        seconds = self.amount.value() * multiplier
        values = self.announcements()
        if seconds in values:
            return
        self._set_warnings(values + [seconds])
        self._emit_announcements()

    def _remove_warning(self) -> None:
        item = self.warnings.currentItem()
        if not item or not item.data(Qt.UserRole):
            return
        self._set_warnings([v for v in self.announcements() if v != item.data(Qt.UserRole)])
        self._emit_announcements()

    def _update_buttons(self) -> None:
        item = self.warnings.currentItem()
        self.remove_button.setEnabled(bool(item and item.data(Qt.UserRole)))

    def _emit_join(self) -> None:
        self.join_broadcast_changed.emit(
            self.join_box.isChecked(),
            self.join_message.text().strip() or self.join_message.placeholderText(),
        )

    def _emit_schedule(self) -> None:
        self.restart_schedule_changed.emit(
            self.restart_box.isChecked(), self.interval.currentData() or 24
        )

    def _emit_announcements(self) -> None:
        self.restart_announcements_changed.emit(
            self.warn_enabled.isChecked(), self.announcements()
        )

    def _emit_countdown(self) -> None:
        self.restart_countdown_changed.emit(
            self.countdown_enabled.isChecked(), self.countdown_seconds.value()
        )
