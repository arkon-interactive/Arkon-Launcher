"""The server console: log output, colouring, filtering, and a command box.

The console is the point of the app - it's how the host sees what the server is
doing and how they run commands - so it gets the care. Two things matter for it
not to fall over during a long session:

* **Bounded scrollback.** A busy modded server emits a lot; the document is
  capped so memory doesn't grow without limit.
* **Batched appends.** Output arrives from a reader thread and is flushed on a
  timer rather than per line, so a burst at startup can't stall the UI.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..runner import LogLine

MAX_SCROLLBACK_LINES = 5000
FLUSH_INTERVAL_MS = 80

LEVEL_COLOURS = {
    "ERROR": "#ff6b6b",
    "FATAL": "#ff4757",
    "WARN": "#ffa94d",
    "INFO": "#d8dee9",
    "DEBUG": "#7f8c9b",
    "TRACE": "#6b7684",
}
COMMAND_COLOUR = "#63b3ed"
NOTICE_COLOUR = "#9ae6b4"


class ConsoleView(QWidget):
    """Read-only log pane plus the command entry beneath it."""

    command_entered = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._pending: list[tuple[str, str]] = []
        self._history: list[str] = []
        self._history_index = 0
        self._filter = ""

        self.output = QPlainTextEdit(readOnly=True)
        self.output.setMaximumBlockCount(MAX_SCROLLBACK_LINES)
        self.output.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.output.setFont(self._console_font())
        self.output.setStyleSheet(
            "QPlainTextEdit { background:#1a1d21; color:#d8dee9; border:1px solid #2f343b; }"
        )

        self.filter_box = QLineEdit(placeholderText="Filter output...")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self._on_filter_changed)

        self.autoscroll = QCheckBox("Auto-scroll", checked=True)

        self.command_box = QLineEdit(placeholderText="Type a server command, e.g. list")
        self.command_box.returnPressed.connect(self._on_submit)
        self.command_box.installEventFilter(self)

        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._on_submit)

        top = QHBoxLayout()
        top.addWidget(QLabel("Console"))
        top.addStretch(1)
        top.addWidget(self.filter_box, 2)
        top.addWidget(self.autoscroll)

        bottom = QHBoxLayout()
        bottom.addWidget(self.command_box, 1)
        bottom.addWidget(self.send_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.output, 1)
        layout.addLayout(bottom)

        self.set_enabled_for_running(False)

        self._flush_timer = QTimer(self)
        self._flush_timer.timeout.connect(self._flush)
        self._flush_timer.start(FLUSH_INTERVAL_MS)

        self._all_lines: list[tuple[str, str]] = []

    @staticmethod
    def _console_font() -> QFont:
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        if not font.exactMatch():
            font = QFont("Consolas", 9)
            font.setStyleHint(QFont.Monospace)
        return font

    # --- Input ---

    def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
        if obj is self.command_box and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key_Up:
                self._recall(-1)
                return True
            if event.key() == Qt.Key_Down:
                self._recall(1)
                return True
        return super().eventFilter(obj, event)

    def _recall(self, direction: int) -> None:
        if not self._history:
            return
        self._history_index = max(
            0, min(len(self._history), self._history_index + direction)
        )
        if self._history_index == len(self._history):
            self.command_box.clear()
        else:
            self.command_box.setText(self._history[self._history_index])

    def _on_submit(self) -> None:
        command = self.command_box.text().strip()
        if not command:
            return
        self._history.append(command)
        self._history_index = len(self._history)
        self.command_box.clear()
        self.append_notice(f"> {command}", COMMAND_COLOUR)
        self.command_entered.emit(command)

    def set_enabled_for_running(self, running: bool) -> None:
        self.command_box.setEnabled(running)
        self.send_button.setEnabled(running)
        self.command_box.setPlaceholderText(
            "Type a server command, e.g. list" if running else "Server is not running"
        )

    # --- Output ---

    def append_line(self, raw: str) -> None:
        """Queue a server log line. Safe to call from any thread via a queued signal."""
        colour = LEVEL_COLOURS.get(LogLine.parse(raw).level, LEVEL_COLOURS["INFO"])
        self._pending.append((raw, colour))

    def append_notice(self, text: str, colour: str = NOTICE_COLOUR) -> None:
        """A message from the launcher itself rather than the server."""
        self._pending.append((text, colour))

    def _on_filter_changed(self, text: str) -> None:
        self._filter = text.strip().lower()
        self._rebuild()

    def _rebuild(self) -> None:
        self.output.clear()
        for raw, colour in self._all_lines:
            if self._matches(raw):
                self._write(raw, colour)
        self._scroll_if_wanted()

    def _matches(self, raw: str) -> bool:
        return not self._filter or self._filter in raw.lower()

    def _flush(self) -> None:
        if not self._pending:
            return

        batch, self._pending = self._pending, []
        self._all_lines.extend(batch)
        if len(self._all_lines) > MAX_SCROLLBACK_LINES:
            del self._all_lines[: len(self._all_lines) - MAX_SCROLLBACK_LINES]

        for raw, colour in batch:
            if self._matches(raw):
                self._write(raw, colour)
        self._scroll_if_wanted()

    def _write(self, text: str, colour: str) -> None:
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colour))
        cursor.insertText(text + "\n", fmt)

    def _scroll_if_wanted(self) -> None:
        if self.autoscroll.isChecked():
            bar = self.output.verticalScrollBar()
            bar.setValue(bar.maximum())

    def clear(self) -> None:
        self._all_lines.clear()
        self._pending.clear()
        self.output.clear()
