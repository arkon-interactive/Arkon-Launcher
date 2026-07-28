"""The side panels: connection details, players, and backups.

Each panel is deliberately passive - it renders what it is given and emits what
the user asked for. The main window owns the server and decides what actually
happens, so no panel can act on a server it doesn't fully know the state of.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..backups import Backup
from ..connection import ConnectionStatus, Rung, VOICE_CHAT_UDP_PORT
from ..players import KnownPlayer


class CopyableRow(QWidget):
    """A labelled value with a copy button - the thing you hand to a friend."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.value = QLineEdit(readOnly=True)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setFixedWidth(60)
        self.copy_button.clicked.connect(self._copy)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        caption = QLabel(label)
        caption.setFixedWidth(110)
        layout.addWidget(caption)
        layout.addWidget(self.value, 1)
        layout.addWidget(self.copy_button)

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.value.text())
        self.copy_button.setText("Copied")
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1200, lambda: self.copy_button.setText("Copy"))

    def set(self, text: str) -> None:
        self.value.setText(text)


class ConnectionPanel(QWidget):
    """How friends reach the server, and which rung of the ladder got us there."""

    refresh_requested = Signal()
    playit_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.headline = QLabel("Not checked yet.")
        self.headline.setWordWrap(True)
        self.headline.setTextFormat(Qt.RichText)

        self.friend_row = CopyableRow("Give friends")
        self.lan_row = CopyableRow("On your network")
        self.public_row = CopyableRow("Your public IP")

        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color:#c9a227;")

        self.refresh_button = QPushButton("Re-check connection")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)

        self.playit_button = QPushButton("Set up playit.gg tunnel...")
        self.playit_button.clicked.connect(self.playit_requested.emit)
        self.playit_button.setToolTip(
            "Uses playit.gg, a third-party relay, so friends can connect without you "
            "opening any ports. Requires a free playit.gg account."
        )
        self.playit_status = QLabel("")
        self.playit_status.setStyleSheet("color:#8b949e;")
        self.playit_status.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.playit_button)
        buttons.addStretch(1)

        group = QGroupBox("Connection")
        inner = QVBoxLayout(group)
        inner.addWidget(self.headline)
        inner.addWidget(self.friend_row)
        inner.addWidget(self.lan_row)
        inner.addWidget(self.public_row)
        inner.addWidget(self.notes)
        inner.addLayout(buttons)
        inner.addWidget(self.playit_status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(group)
        layout.addStretch(1)

    def update_status(self, status: ConnectionStatus) -> None:
        descriptions = {
            Rung.UPNP: (
                "<b>Port opened automatically</b> - your router accepted a UPnP request. "
                "This is a direct connection with no extra delay."
            ),
            Rung.PLAYIT: (
                "<b>Tunnelled through playit.gg</b> - no router setup needed. "
                "Expect roughly 10-50 ms of extra ping."
            ),
            Rung.MANUAL: (
                "<b>Manual setup needed</b> - forward the port on your router, or use "
                "the playit.gg tunnel below."
            ),
        }
        verified = (
            " <span style='color:#5fb37a;'>Verified reachable.</span>"
            if status.verified
            else ""
        )
        self.headline.setText(descriptions[status.rung] + verified)

        self.friend_row.set(status.friend_address())
        self.lan_row.set(
            f"{status.lan_addresses[0]}:{status.port}" if status.lan_addresses else "-"
        )
        self.public_row.set(status.public_address or "unknown")

        notes = list(status.notes)
        notes.append(
            f"Simple Voice Chat also needs UDP port {VOICE_CHAT_UDP_PORT} to be open, "
            f"which is separate from the game port."
        )
        self.notes.setText("\n\n".join(f"- {n}" for n in notes))

    def update_playit(self, install) -> None:
        """Reflect whether playit.gg is installed and running on this machine.

        If the user already has playit.gg, offering to download our own copy
        would be silly - the button starts theirs instead.
        """
        if not install.installed:
            self.playit_button.setText("Set up playit.gg tunnel...")
            self.playit_button.setEnabled(True)
            self.playit_button.setToolTip(
                "Downloads the playit.gg agent and walks you through linking it to a "
                "free playit.gg account."
            )
            self.playit_status.setText("")
            return

        where = "installed on this PC" if install.from_system else "downloaded by Arkon Launcher"

        if install.running:
            self.playit_button.setText("playit.gg is running")
            self.playit_button.setEnabled(False)
            self.playit_button.setToolTip(str(install.executable or ""))
            self.playit_status.setText(
                f"playit.gg is already running ({where}). Your tunnel address comes "
                f"from playit.gg itself - check its window or your playit.gg dashboard."
            )
            return

        self.playit_button.setText("Launch playit.gg")
        self.playit_button.setEnabled(True)
        self.playit_button.setToolTip(f"Start {install.executable}")
        self.playit_status.setText(f"playit.gg found ({where}) but is not running.")


class PlayersPanel(QWidget):
    """Operators, whitelist, and who is on right now."""

    op_toggled = Signal(object, bool)
    whitelist_toggled = Signal(object, bool)
    kick_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._players: list[KnownPlayer] = []

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Player", "Role", "Whitelisted", "Online"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_buttons)

        self.op_button = QPushButton("Toggle operator")
        self.op_button.clicked.connect(self._toggle_op)
        self.whitelist_button = QPushButton("Toggle whitelist")
        self.whitelist_button.clicked.connect(self._toggle_whitelist)
        self.kick_button = QPushButton("Kick")
        self.kick_button.clicked.connect(self._kick)

        buttons = QHBoxLayout()
        buttons.addWidget(self.op_button)
        buttons.addWidget(self.whitelist_button)
        buttons.addWidget(self.kick_button)
        buttons.addStretch(1)

        group = QGroupBox("Players")
        inner = QVBoxLayout(group)
        inner.addWidget(self.table)
        inner.addLayout(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(group, 1)

        self._update_buttons()

    def set_players(self, players: list[KnownPlayer]) -> None:
        self._players = players
        self.table.setRowCount(len(players))
        for row, player in enumerate(players):
            self.table.setItem(row, 0, QTableWidgetItem(player.name))
            self.table.setItem(row, 1, QTableWidgetItem(player.role))
            self.table.setItem(row, 2, QTableWidgetItem("Yes" if player.is_whitelisted else "-"))
            self.table.setItem(row, 3, QTableWidgetItem("Online" if player.is_online else "-"))
        self._update_buttons()

    def selected(self) -> KnownPlayer | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._players):
            return self._players[row]
        return None

    def _update_buttons(self) -> None:
        player = self.selected()
        self.op_button.setEnabled(player is not None)
        self.whitelist_button.setEnabled(player is not None)
        self.kick_button.setEnabled(player is not None and player.is_online)

    def _toggle_op(self) -> None:
        player = self.selected()
        if player:
            self.op_toggled.emit(player, not player.is_op)

    def _toggle_whitelist(self) -> None:
        player = self.selected()
        if player:
            self.whitelist_toggled.emit(player, not player.is_whitelisted)

    def _kick(self) -> None:
        player = self.selected()
        if player:
            self.kick_requested.emit(player)

    def _send_luckperms(self) -> None:
        text = self.lp_command.text().strip()
        if text:
            self.luckperms_command.emit(text)
            self.lp_command.clear()


# Offered intervals, in hours. Anything finer than an hour is more disruption
# than insurance on a modded world that takes seconds to save.
BACKUP_INTERVALS = (1, 2, 6, 12, 24)


class AnnouncementsEditor(QGroupBox):
    """Warnings broadcast to players before a scheduled backup runs."""

    changed = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Warn players before a scheduled backup", parent)
        self.setCheckable(True)
        self.toggled.connect(lambda _: self._emit())

        self.list = QListWidget()
        self.list.setMaximumHeight(110)
        self.list.itemSelectionChanged.connect(self._update_buttons)

        self.amount = QSpinBox()
        self.amount.setRange(1, 600)
        self.amount.setValue(5)
        self.unit = QComboBox()
        self.unit.addItems(["minutes", "seconds"])

        add_button = QPushButton("Add warning")
        add_button.clicked.connect(self._add)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)

        entry = QHBoxLayout()
        entry.addWidget(self.amount)
        entry.addWidget(self.unit)
        entry.addWidget(add_button)
        entry.addWidget(self.remove_button)
        entry.addStretch(1)

        hint = QLabel(
            "Each warning is sent to everyone online that long before the backup "
            "starts, so nobody is caught mid-build by the pause."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b949e;")

        layout = QVBoxLayout(self)
        layout.addWidget(self.list)
        layout.addLayout(entry)
        layout.addWidget(hint)

        self._update_buttons()

    def set_announcements(self, seconds: list[int], enabled: bool) -> None:
        self.blockSignals(True)
        self.setChecked(enabled)
        self.blockSignals(False)
        self.list.clear()
        for value in sorted(set(seconds), reverse=True):
            self._insert(value)
        self._update_buttons()

    def _insert(self, seconds: int) -> None:
        item = QListWidgetItem(f"{describe_lead_time(seconds)} before")
        item.setData(Qt.UserRole, seconds)
        self.list.addItem(item)

    def announcements(self) -> list[int]:
        return sorted(
            {self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())},
            reverse=True,
        )

    def _add(self) -> None:
        seconds = self.amount.value() * (60 if self.unit.currentText() == "minutes" else 1)
        if seconds in self.announcements():
            return
        values = self.announcements() + [seconds]
        self.set_announcements(values, self.isChecked())
        self._emit()

    def _remove(self) -> None:
        item = self.list.currentItem()
        if not item:
            return
        remaining = [v for v in self.announcements() if v != item.data(Qt.UserRole)]
        self.set_announcements(remaining, self.isChecked())
        self._emit()

    def _update_buttons(self) -> None:
        self.remove_button.setEnabled(self.list.currentItem() is not None)

    def _emit(self) -> None:
        self.changed.emit(self.announcements())


def describe_lead_time(seconds: int) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


class BackupsPanel(QWidget):
    """Existing backups, plus how and when new ones are made."""

    backup_requested = Signal()
    restore_requested = Signal(object)
    schedule_changed = Signal(bool, int)
    location_changed = Signal(str)
    announcements_changed = Signal(bool, list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._backups: list[Backup] = []

        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self._update_buttons)

        self.backup_button = QPushButton("Back up now")
        self.backup_button.clicked.connect(self.backup_requested.emit)
        self.restore_button = QPushButton("Restore selected...")
        self.restore_button.clicked.connect(self._restore)

        buttons = QHBoxLayout()
        buttons.addWidget(self.backup_button)
        buttons.addWidget(self.restore_button)
        buttons.addStretch(1)

        self.hint = QLabel(
            "A backup is taken automatically before every start. Restoring requires "
            "the server to be stopped, and always saves the current world first."
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#8b949e;")

        group = QGroupBox("Existing backups")
        inner = QVBoxLayout(group)
        inner.addWidget(self.list, 1)
        inner.addLayout(buttons)
        inner.addWidget(self.hint)

        # --- Schedule ---
        self.schedule_box = QGroupBox("Back up automatically")
        self.schedule_box.setCheckable(True)
        self.schedule_box.setChecked(False)
        self.schedule_box.toggled.connect(self._emit_schedule)

        self.interval = QComboBox()
        for hours in BACKUP_INTERVALS:
            self.interval.addItem(
                f"Every {hours} hour{'s' if hours != 1 else ''}", hours
            )
        self.interval.setCurrentIndex(BACKUP_INTERVALS.index(6))
        self.interval.currentIndexChanged.connect(self._emit_schedule)

        self.next_run = QLabel("")
        self.next_run.setStyleSheet("color:#8b949e;")

        schedule_row = QHBoxLayout()
        schedule_row.addWidget(self.interval)
        schedule_row.addWidget(self.next_run, 1)
        QVBoxLayout(self.schedule_box).addLayout(schedule_row)

        # --- Location ---
        self.location = QLineEdit(readOnly=True)
        browse = QPushButton("Change...")
        browse.clicked.connect(self._choose_location)
        reset = QPushButton("Use default")
        reset.clicked.connect(lambda: self.location_changed.emit(""))

        location_row = QHBoxLayout()
        location_row.addWidget(self.location, 1)
        location_row.addWidget(browse)
        location_row.addWidget(reset)

        location_box = QGroupBox("Where backups are saved")
        location_layout = QVBoxLayout(location_box)
        location_layout.addLayout(location_row)
        location_hint = QLabel(
            "The default keeps backups beside the instance, so they travel with the "
            "worlds they protect and survive uninstalling Arkon Launcher."
        )
        location_hint.setWordWrap(True)
        location_hint.setStyleSheet("color:#8b949e;")
        location_layout.addWidget(location_hint)

        self.announcements = AnnouncementsEditor()
        self.announcements.changed.connect(
            lambda values: self.announcements_changed.emit(
                self.announcements.isChecked(), values
            )
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(group, 1)
        layout.addWidget(self.schedule_box)
        layout.addWidget(location_box)
        layout.addWidget(self.announcements)

        self._update_buttons()

    # --- Settings in/out ---

    def load_settings(self, settings) -> None:
        self.schedule_box.blockSignals(True)
        self.schedule_box.setChecked(settings.backup_schedule_enabled)
        index = self.interval.findData(settings.backup_interval_hours)
        self.interval.setCurrentIndex(index if index >= 0 else 2)
        self.schedule_box.blockSignals(False)

        self.location.setText(settings.backup_location or "(default - beside the instance)")
        self.announcements.set_announcements(
            settings.backup_announcements, settings.backup_announce_enabled
        )

    def set_next_run(self, text: str) -> None:
        self.next_run.setText(text)

    def _emit_schedule(self) -> None:
        self.schedule_changed.emit(
            self.schedule_box.isChecked(), self.interval.currentData() or 6
        )

    def _choose_location(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder for backups")
        if chosen:
            self.location_changed.emit(chosen)

    def set_backups(self, backups: list[Backup]) -> None:
        self._backups = backups
        self.list.clear()
        for backup in backups:
            item = QListWidgetItem(f"{backup.label}    {backup.size_mb:,.0f} MB")
            item.setData(Qt.UserRole, backup)
            self.list.addItem(item)
        self._update_buttons()

    def selected(self) -> Backup | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def set_server_running(self, running: bool) -> None:
        self._running = running
        self._update_buttons()

    def _update_buttons(self) -> None:
        running = getattr(self, "_running", False)
        self.restore_button.setEnabled(self.selected() is not None and not running)
        self.restore_button.setToolTip(
            "Stop the server before restoring a backup." if running else ""
        )

    def _restore(self) -> None:
        backup = self.selected()
        if backup:
            self.restore_requested.emit(backup)
