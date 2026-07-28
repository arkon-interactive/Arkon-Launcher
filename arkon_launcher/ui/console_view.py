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

from PySide6.QtCore import QSize, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImage,
    QPainter,
    QPixmap,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QCompleter,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
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


class WordCompleter(QCompleter):
    """Completes the word under the cursor rather than the whole line.

    A console line is `command arg arg`, so completing against the entire text
    would only ever match the first token. Splitting on spaces means the command
    completes at the start and player names complete wherever they appear.
    """

    def splitPath(self, path: str) -> list[str]:  # noqa: N802 - Qt naming
        return [path.split(" ")[-1]]

    def pathFromIndex(self, index) -> str:  # noqa: N802 - Qt naming
        completion = super().pathFromIndex(index)
        text = self.widget().text() if self.widget() else ""
        words = text.split(" ")[:-1]
        return " ".join(words + [completion])


class PlayerStrip(QWidget):
    """Heads of everyone online; clicking one drops their name into the input."""

    player_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buttons: dict[str, QToolButton] = {}

        self.empty_label = QLabel("Nobody is online.")
        self.empty_label.setStyleSheet("color:#8b949e;")

        self._row = QHBoxLayout()
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(6)
        self._row.addWidget(self.empty_label)
        self._row.addStretch(1)

        host = QWidget()
        host.setLayout(self._row)

        # Scrolls horizontally rather than wrapping: a full server would
        # otherwise push the console itself off the screen.
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setFixedHeight(58)
        area.setWidget(host)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(area)

    def set_players(self, names: list[str]) -> None:
        for name in list(self._buttons):
            if name not in names:
                button = self._buttons.pop(name)
                self._row.removeWidget(button)
                button.deleteLater()

        for name in names:
            if name in self._buttons:
                continue
            button = QToolButton()
            button.setText(name)
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setAutoRaise(True)
            button.setIconSize(QSize(24, 24))
            button.setIcon(self._placeholder_icon(name))
            button.setToolTip(f"Add {name} to the command")
            button.clicked.connect(lambda _=False, n=name: self.player_clicked.emit(n))
            self._buttons[name] = button
            self._row.insertWidget(self._row.count() - 1, button)

        self.empty_label.setVisible(not names)

    def set_avatar(self, name: str, path: str) -> None:
        button = self._buttons.get(name)
        if button and path:
            icon = QIcon(path)
            if not icon.isNull():
                button.setIcon(icon)

    @staticmethod
    def _placeholder_icon(name: str) -> QIcon:
        """A coloured initial, shown until (or instead of) the real head."""
        size = 24
        image = QImage(size, size, QImage.Format_ARGB32)
        # Deterministic colour per name, so a player looks the same each session.
        hue = (sum(ord(c) for c in name) * 37) % 360
        image.fill(QColor.fromHsv(hue, 120, 140))

        painter = QPainter(image)
        painter.setPen(QColor("#f0f2f0"))
        font = QFont()
        font.setBold(True)
        font.setPointSize(11)
        painter.setFont(font)
        painter.drawText(image.rect(), Qt.AlignCenter, name[:1].upper())
        painter.end()
        return QIcon(QPixmap.fromImage(image))


class ConsoleView(QWidget):
    """Read-only log pane plus the command entry beneath it."""

    command_entered = Signal(str)
    player_clicked = Signal(str)

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

        self._completion_model = QStringListModel(self)
        self.completer = WordCompleter(self._completion_model, self)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.setFilterMode(Qt.MatchStartsWith)
        self.command_box.setCompleter(self.completer)

        self.players = PlayerStrip()
        self.players.player_clicked.connect(self._append_player)

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
        layout.addWidget(self.players)
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

    def _append_player(self, name: str) -> None:
        """Ask the window what can be done to this player, and offer it.

        Appending the name was the original behaviour, but with autocompletion
        in the box that is the easy half of the job - the useful half is the
        actions themselves. Appending is still offered at the bottom of the
        menu for anything not covered.
        """
        self.player_clicked.emit(name)

    def append_player_name(self, name: str) -> None:
        text = self.command_box.text()
        if text and not text.endswith(" "):
            text += " "
        self.command_box.setText(text + name)
        self.command_box.setFocus()

    def player_button(self, name: str):
        return self.players._buttons.get(name)

    def set_completions(self, words: list[str]) -> None:
        """Words offered by the completer: commands, then online player names."""
        self._completion_model.setStringList(words)

    def set_players(self, names: list[str]) -> None:
        self.players.set_players(names)

    def set_avatar(self, name: str, path: str) -> None:
        self.players.set_avatar(name, path)

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
