"""The main window: pick an instance, pick a world, start the server, drive it.

Long operations (mod sync, downloads, world sizing, graceful stop) run on worker
threads; the server's output arrives on its reader thread and is marshalled onto
the GUI thread by a queued signal. Nothing blocking runs in an event handler.
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import (
    __version__,
    backups,
    connection,
    crashdoctor,
    instances,
    modsync,
    paths,
    players,
    provision,
    worlds,
)
from ..runner import ServerConfig, ServerProcess, ServerState, sanitize_jvm_args
from ..settings import AppSettings, WorldSettings
from .. import luckperms, permissionnodes, serversettings
from ..serversettings import PendingChanges
from .console_view import ConsoleView
from .countdown_button import CountdownButton
from .panels import BackupsPanel, ConnectionPanel, PlayersPanel
from .permissions_panel import PermissionsPanel
from .settings_panel import ServerSettingsPanel


class Worker(QThread):
    """Run a callable off the GUI thread and report back."""

    finished_ok = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, work, parent=None) -> None:
        super().__init__(parent)
        self._work = work

    def run(self) -> None:
        try:
            self.finished_ok.emit(self._work(self.progress.emit))
        except Exception as exc:  # Surface the reason rather than dying silently.
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=3)}")


class MainWindow(QMainWindow):
    # Emitted from the server's reader thread; queued onto the GUI thread.
    server_line = Signal(str)
    server_state = Signal(object)
    players_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Arkon Launcher {__version__}")
        self.resize(1100, 720)

        self.settings = AppSettings.load()
        self.instance: instances.Instance | None = None
        self.worlds: list[worlds.World] = []
        self.server: ServerProcess | None = None
        self.connection = connection.ConnectionManager(self.settings.server_port)
        # Setting changes made while the server was stopped, applied once it is up.
        self.pending = PendingChanges()
        # Command tree, cached per server run - it cannot change while up.
        self._help_lines: list[str] | None = None
        self._recorded_from = 0
        self._worker: Worker | None = None
        self._workers: list[Worker] = []
        self._restarts = 0

        self._build_ui()

        self.server_line.connect(self._on_server_line)
        self.server_state.connect(self._on_server_state)
        self.players_changed.connect(self._on_players_changed)

        self._uptime_timer = QTimer(self)
        self._uptime_timer.timeout.connect(self._refresh_status)
        self._uptime_timer.start(1000)

        self._backup_timer = QTimer(self)
        self._backup_timer.timeout.connect(self._scheduled_backup)
        self._announce_timers: list[QTimer] = []
        self._restart_backup_schedule()

        # What server.properties looked like when we last agreed with it, so an
        # edit made outside the launcher can be noticed rather than clobbered.
        self._properties_snapshot: dict[str, str] = {}
        self._last_drift_reported: list[str] = []

        self._scanning = False
        self._scan_buffer: list[str] = []
        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._harvest_scan)

        QTimer.singleShot(0, self.discover_instances)

    # --- Layout ---

    def _build_ui(self) -> None:
        self.instance_box = QComboBox()
        self.instance_box.currentIndexChanged.connect(self._on_instance_changed)

        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self.browse_for_instance)

        self.instance_detail = QLabel("Looking for CurseForge instances...")
        self.instance_detail.setWordWrap(True)

        instance_row = QHBoxLayout()
        instance_row.addWidget(self.instance_box, 1)
        instance_row.addWidget(self.browse_button)

        instance_group = QGroupBox("Instance")
        instance_layout = QVBoxLayout(instance_group)
        instance_layout.addLayout(instance_row)
        instance_layout.addWidget(self.instance_detail)

        self.world_table = QTableWidget(0, 4)
        self.world_table.setHorizontalHeaderLabels(["World", "Size", "Last played", "Status"])
        self.world_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.world_table.setSelectionMode(QTableWidget.SingleSelection)
        self.world_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.world_table.verticalHeader().setVisible(False)
        header = self.world_table.horizontalHeader()
        # The name stretches; the rest size to their content so nothing is clipped.
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.world_table.itemSelectionChanged.connect(self._update_buttons)

        world_group = QGroupBox("Worlds")
        world_layout = QVBoxLayout(world_group)
        world_layout.addWidget(self.world_table)

        self.memory_spin = QSpinBox()
        self.memory_spin.setRange(2048, self._memory_ceiling())
        self.memory_spin.setSingleStep(512)
        self.memory_spin.setSuffix(" MB")
        self.memory_spin.setValue(min(self.settings.max_memory_mb, self._memory_ceiling()))

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(self.settings.server_port)

        self.autorestart_check = QCheckBox("Restart automatically if it crashes")
        self.autorestart_check.setChecked(self.settings.auto_restart)

        self.autoop_check = QCheckBox("Make the world's creator an operator")
        self.autoop_check.setChecked(self.settings.auto_op_owner)
        self.autoop_check.setToolTip(
            "On first start, gives operator rights to whoever created this world "
            "(read from the world itself). Does nothing if they already have them."
        )
        self.autoop_check.stateChanged.connect(self._save_options)

        self.eula_check = QCheckBox("I accept the Minecraft EULA (aka.ms/MinecraftEULA)")
        self.eula_check.setChecked(self.settings.eula_accepted)
        self.eula_check.stateChanged.connect(self._update_buttons)

        options_group = QGroupBox("Server options")
        options_form = QFormLayout(options_group)
        options_form.addRow("Memory", self.memory_spin)
        options_form.addRow("Port", self.port_spin)
        options_form.addRow("", self.autorestart_check)
        options_form.addRow("", self.autoop_check)
        options_form.addRow("", self.eula_check)

        # Each of these is disruptive and easy to hit by accident, so they arm a
        # three-second countdown: click again to go now, right-click or Esc to
        # call it off.
        self.start_button = CountdownButton("Start server")
        self.start_button.triggered.connect(self.start_server)
        self.start_button.setToolTip(
            "Starts the server for the selected world.\n"
            "Click again to start immediately, right-click or press Esc to cancel."
        )

        self.stop_button = CountdownButton("Stop server")
        self.stop_button.triggered.connect(self.stop_server)
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip(
            "Saves the world and shuts the server down.\n"
            "Click again to stop immediately, right-click or press Esc to cancel."
        )

        self.reload_button = CountdownButton("Reload server")
        self.reload_button.triggered.connect(self.reload_server)
        self.reload_button.setEnabled(False)
        self.reload_button.setToolTip(
            "Re-reads datapacks, loot tables and server config without disconnecting "
            "anyone. The server keeps running and players stay online.\n\n"
            "Does not pick up changes to mods or server.properties - those need a "
            "restart."
        )

        self.restart_button = CountdownButton("Restart server")
        self.restart_button.triggered.connect(self.restart_server)
        self.restart_button.setEnabled(False)
        self.restart_button.setToolTip(
            "Shuts the server down and starts it again. Everyone is disconnected "
            "and the world is saved first.\n\n"
            "Needed for mod changes, memory changes and most server settings."
        )

        button_row = QHBoxLayout()
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.stop_button)

        button_row_two = QHBoxLayout()
        button_row_two.addWidget(self.reload_button)
        button_row_two.addWidget(self.restart_button)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(instance_group)
        left_layout.addWidget(world_group, 1)
        left_layout.addWidget(options_group)
        left_layout.addLayout(button_row)
        left_layout.addLayout(button_row_two)

        self.console = ConsoleView()
        self.console.command_entered.connect(self._send_command)

        self.connection_panel = ConnectionPanel()
        self.connection_panel.refresh_requested.connect(self.refresh_connection)
        self.connection_panel.playit_requested.connect(self.setup_playit)

        self.players_panel = PlayersPanel()
        self.players_panel.op_toggled.connect(self._set_op)
        self.players_panel.whitelist_toggled.connect(self._set_whitelisted)
        self.players_panel.kick_requested.connect(self._kick_player)

        self.backups_panel = BackupsPanel()
        self.backups_panel.backup_requested.connect(self.backup_now)
        self.backups_panel.restore_requested.connect(self.restore_backup)
        self.backups_panel.schedule_changed.connect(self._on_schedule_changed)
        self.backups_panel.location_changed.connect(self._on_backup_location_changed)
        self.backups_panel.announcements_changed.connect(self._on_announcements_changed)
        self.backups_panel.load_settings(self.settings)

        self.settings_panel = ServerSettingsPanel()
        self.settings_panel.save_requested.connect(self.save_settings)
        self.settings_panel.save_and_restart_requested.connect(self.save_and_restart)
        self.settings_panel.refresh_requested.connect(lambda: self.refresh_settings(force=True))

        self.permissions_panel = PermissionsPanel()
        self.permissions_panel.refresh_requested.connect(self.refresh_permissions)
        self.permissions_panel.discover_started.connect(self.start_recording_nodes)
        self.permissions_panel.discover_stopped.connect(self.stop_recording_nodes)
        self.permissions_panel.passive_check.setChecked(self.settings.passive_permission_scan)
        self.permissions_panel.passive_scan_toggled.connect(self._on_passive_scan_toggled)

        groups_tab = self.permissions_panel.groups_tab
        groups_tab.group_selected.connect(self._load_group_permissions)
        groups_tab.create_group.connect(self._create_group)
        groups_tab.delete_group.connect(self._delete_group)
        groups_tab.set_weight.connect(self._set_group_weight)
        groups_tab.assign_nodes.connect(self._assign_nodes)
        groups_tab.unassign_nodes.connect(self._unassign_nodes)
        groups_tab.add_parent.connect(self._add_group_parent)
        groups_tab.remove_parent.connect(self._remove_group_parent)

        users_tab = self.permissions_panel.users_tab
        users_tab.user_selected.connect(self._load_user_info)
        users_tab.add_to_group.connect(self._add_user_to_group)
        users_tab.remove_from_group.connect(self._remove_user_from_group)
        users_tab.set_primary.connect(self._set_user_primary_group)
        users_tab.promote.connect(self._promote_user)
        users_tab.demote.connect(self._demote_user)

        tracks_tab = self.permissions_panel.tracks_tab
        tracks_tab.track_selected.connect(self._load_track_path)
        tracks_tab.create_track.connect(self._create_track)
        tracks_tab.delete_track.connect(self._delete_track)
        tracks_tab.append_group.connect(self._track_append)
        tracks_tab.remove_group.connect(self._track_remove)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.console, "Console")
        self.tabs.addTab(self.connection_panel, "Connection")
        self.tabs.addTab(self.settings_panel, "Settings")
        self.tabs.addTab(self.players_panel, "Players")
        self.tabs.addTab(self.permissions_panel, "Permissions")
        self.tabs.addTab(self.backups_panel, "Backups")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 680])
        self.setCentralWidget(splitter)

        self.setStatusBar(QStatusBar())
        self._set_status("Ready")

        if paths.state_dir_fallback_reason():
            self.console.append_notice(paths.state_dir_fallback_reason(), "#ffa94d")

    @staticmethod
    def _memory_ceiling() -> int:
        try:
            import psutil

            return max(2048, int(psutil.virtual_memory().total * 0.75) // (1024 * 1024))
        except Exception:
            return 8192

    def _set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)

    # --- Discovery ---

    def discover_instances(self) -> None:
        extra = [Path(p) for p in self.settings.extra_instance_roots]
        found = instances.find_instances(extra)

        self.instance_box.blockSignals(True)
        self.instance_box.clear()
        for instance in found:
            self.instance_box.addItem(f"{instance.name}  -  {instance.directory}", instance)
        self.instance_box.blockSignals(False)

        if not found:
            self.instance_detail.setText(
                "No CurseForge instances found automatically. Use Browse... to pick "
                "the folder containing minecraftinstance.json."
            )
            self._update_buttons()
            return

        index = 0
        if self.settings.last_instance:
            for position in range(self.instance_box.count()):
                if str(self.instance_box.itemData(position).directory) == self.settings.last_instance:
                    index = position
                    break
        self.instance_box.setCurrentIndex(index)
        self._on_instance_changed(index)

    def browse_for_instance(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select a CurseForge instance folder")
        if not chosen:
            return

        root = Path(chosen)
        if not (root / instances.INSTANCE_MANIFEST).is_file():
            # Might be the folder that *contains* instances; scan it either way.
            if not any(root.glob(f"*/{instances.INSTANCE_MANIFEST}")):
                QMessageBox.warning(
                    self,
                    "Not an instance",
                    f"No {instances.INSTANCE_MANIFEST} found in that folder or its "
                    f"immediate subfolders.",
                )
                return

        if chosen not in self.settings.extra_instance_roots:
            self.settings.extra_instance_roots.append(chosen)
            self.settings.save()
        self.discover_instances()

    def _on_instance_changed(self, index: int) -> None:
        instance = self.instance_box.itemData(index)
        if instance is None:
            return
        self.instance = instance

        unsupported = instances.describe_unsupported(instance)
        if unsupported:
            self.instance_detail.setText(unsupported)
            self.world_table.setRowCount(0)
            self._update_buttons()
            return

        self.instance_detail.setText(
            f"Minecraft {instance.mc_version} - Fabric {instance.loader_version}\n"
            f"{instance.directory}"
        )
        self.settings.last_instance = str(instance.directory)
        self.settings.save()
        self.load_worlds()

    def load_worlds(self) -> None:
        if not self.instance:
            return
        instance = self.instance
        self._set_status("Reading worlds...")

        def work(_report):
            found = worlds.find_worlds(instance.saves_dir)
            return [(w, worlds.folder_size(w.folder), worlds.is_world_busy(w.folder)) for w in found]

        self._run(work, self._populate_worlds)

    def _populate_worlds(self, rows) -> None:
        self.worlds = [row[0] for row in rows]
        self.world_table.setRowCount(len(rows))

        for index, (world, size, busy) in enumerate(rows):
            status = "Open in Minecraft" if busy else "Ready"
            if world.needs_player_migration():
                status += " - old player format"

            self.world_table.setItem(index, 0, QTableWidgetItem(world.folder_name))
            self.world_table.setItem(index, 1, QTableWidgetItem(f"{size / 1024**2:,.0f} MB"))
            last = "-"
            if world.last_played_ms:
                import datetime

                last = datetime.datetime.fromtimestamp(world.last_played_ms / 1000).strftime(
                    "%Y-%m-%d %H:%M"
                )
            self.world_table.setItem(index, 2, QTableWidgetItem(last))
            self.world_table.setItem(index, 3, QTableWidgetItem(status))

        if self.settings.last_world:
            for index, world in enumerate(self.worlds):
                if world.folder_name == self.settings.last_world:
                    self.world_table.selectRow(index)
                    break
        elif self.worlds:
            self.world_table.selectRow(0)

        self._set_status(f"{len(rows)} world(s) found")
        self._update_buttons()

    def selected_world(self) -> worlds.World | None:
        row = self.world_table.currentRow()
        if 0 <= row < len(self.worlds):
            return self.worlds[row]
        return None

    def _update_buttons(self) -> None:
        running = self.server is not None and self.server.is_alive
        world = self.selected_world()
        can_start = (
            not running
            and world is not None
            and self.instance is not None
            and self.instance.is_fabric
            and self.eula_check.isChecked()
            and not worlds.is_world_busy(world.folder)
        )
        self.start_button.setEnabled(can_start)
        self.stop_button.setEnabled(running)
        self.reload_button.setEnabled(running)
        self.restart_button.setEnabled(running)
        self.settings_panel.set_server_running(running)

        if world is not None and worlds.is_world_busy(world.folder) and not running:
            self._set_status(f"'{world.folder_name}' is open in Minecraft - close it first.")

    # --- Workers ---

    def _run(self, work, on_success) -> None:
        worker = Worker(work, self)
        # Keep a reference: a QThread that goes out of scope mid-run is destroyed
        # while still running, which crashes the process.
        self._workers.append(worker)
        worker.progress.connect(self._set_status)
        worker.failed.connect(self._on_worker_failed)
        worker.finished_ok.connect(on_success)
        worker.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_worker_failed(self, message: str) -> None:
        self.console.append_notice(message.splitlines()[0], "#ff6b6b")
        QMessageBox.critical(self, "Something went wrong", message)
        self._set_status("Failed")
        self._update_buttons()

    # --- Server lifecycle ---

    def start_server(self) -> None:
        world = self.selected_world()
        if not world or not self.instance:
            return

        if worlds.is_world_busy(world.folder):
            QMessageBox.warning(
                self,
                "World is in use",
                f"'{world.folder_name}' is currently open in Minecraft. Close the world "
                f"first, or the server and the game would both write to it.",
            )
            return

        if world.needs_player_migration():
            answer = QMessageBox.question(
                self,
                "Older player format",
                f"'{world.folder_name}' stores your character inside level.dat, which a "
                f"dedicated server does not read. If you start it as-is you will spawn "
                f"with an empty inventory.\n\nStart anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self.settings.last_world = world.folder_name
        self.settings.max_memory_mb = self.memory_spin.value()
        self.settings.server_port = self.port_spin.value()
        self.settings.auto_restart = self.autorestart_check.isChecked()
        self.settings.eula_accepted = self.eula_check.isChecked()
        self.settings.save()

        self.console.clear()
        self.start_button.setEnabled(False)
        self._restarts = 0
        # A different run may have a different mod set, so the command tree is
        # re-read rather than carried over.
        self._help_lines = None

        instance = self.instance
        memory = self.memory_spin.value()
        port = self.port_spin.value()
        world_settings = WorldSettings.load(instance.directory, world.folder_name)

        backup_first = self.settings.backup_on_start
        keep = self.settings.backup_keep

        def work(report):
            report("Checking Java...")
            java = provision.select_java(instance)

            server_dir = paths.server_dir(instance.directory, world.folder_name)

            if backup_first:
                report("Backing up the world before starting...")
                try:
                    backups.create_backup(
                        world.folder, instance.directory, world.folder_name, keep,
                        report, label="prestart",
                    )
                except backups.BackupError as exc:
                    # A failed backup is worth saying out loud, but it should not
                    # stop someone starting their server.
                    report(f"Backup skipped: {exc}")

            report("Selecting server-safe mods...")
            result = modsync.select_server_mods(
                instance.mods_dir,
                user_disabled_ids=set(world_settings.disabled_mod_ids),
                force_include_ids=set(world_settings.force_include_mod_ids),
            )
            modsync.mirror_mods(result, server_dir / "mods")
            modsync.mirror_tree(instance.config_dir, server_dir / "config")
            modsync.mirror_tree(instance.directory / "defaultconfigs", server_dir / "defaultconfigs")

            # Older versions linked the save in with a junction, which Windows
            # can refuse to traverse. The server is pointed straight at the
            # saves folder now, so any leftover link is removed.
            modsync.remove_legacy_world_link(server_dir)
            universe, world_folder = modsync.world_container(world.folder)
            provision.ensure_server_properties(
                server_dir, world.level_name, port, level_name=world_folder
            )
            provision.write_eula(server_dir, True)

            report("Fetching server jar...")
            jar = provision.ensure_server_jar(instance)

            return java, jar, server_dir, result, universe, world_folder

        self._run(work, lambda payload: self._launch(payload, memory, instance))

    def _launch(self, payload, memory: int, instance) -> None:
        java, jar, server_dir, result, universe, world_folder = payload

        self.console.append_notice(f"Java: {java.display}")
        self.console.append_notice(result.summary())
        for mod in result.excluded:
            if mod.excluded_by is modsync.Exclusion.DEPENDENCY_MISSING:
                self.console.append_notice(f"  excluded {mod.mod_id}: {mod.detail}", "#ffa94d")
        self.console.append_notice(f"Working directory: {server_dir}")
        self.console.append_notice("Starting server...")

        config = ServerConfig(
            java=java.executable,
            server_jar=jar,
            working_dir=server_dir,
            max_memory_mb=memory,
            min_memory_mb=min(2048, memory),
            extra_jvm_args=sanitize_jvm_args(instance.java_args_override),
            universe=universe,
            world_name=world_folder,
        )

        server = ServerProcess(config)
        # These callbacks fire on the reader thread; the signals hop to the GUI.
        server.on_line(self.server_line.emit)
        server.on_state(self.server_state.emit)
        server.on_players(lambda _: self.players_changed.emit())
        self.server = server

        try:
            server.start()
        except Exception as exc:
            self._on_worker_failed(str(exc))
            return

        self.console.set_enabled_for_running(True)
        self._update_buttons()
        # Work out how friends will connect while the world finishes loading.
        QTimer.singleShot(1500, self.refresh_connection)

    def _on_server_state(self, state: ServerState) -> None:
        if state is ServerState.RUNNING:
            self.console.append_notice("Server is ready. Friends can connect now.")
            self._restarts = 0
            QTimer.singleShot(1000, self._apply_pending)
            QTimer.singleShot(2000, self._auto_op_owner)
            QTimer.singleShot(3000, self.refresh_permissions)
            QTimer.singleShot(4000, self._start_passive_scan)
        elif state is ServerState.CRASHED:
            self.console.append_notice(
                f"Server exited unexpectedly (code {self.server.exit_code if self.server else '?'}).",
                "#ff6b6b",
            )
            # Triage first: a restart cannot fix a mod that will always crash.
            if not self._offer_triage():
                self._maybe_restart()
        elif state is ServerState.STOPPED:
            self.console.append_notice("Server stopped.")

        if state in (ServerState.STOPPED, ServerState.CRASHED):
            self.console.set_enabled_for_running(False)
            self._harvest_scan()
            self._stop_passive_scan()

        self._update_buttons()

    def _offer_triage(self) -> bool:
        """Name the mod that broke it and offer to disable it. True if handled."""
        world = self.selected_world()
        if not world or not self.instance:
            return False

        server_dir = paths.server_dir(self.instance.directory, world.folder_name)
        try:
            diagnosis = crashdoctor.diagnose(server_dir)
        except Exception:
            return False

        if diagnosis is None:
            return False

        self.console.append_notice(diagnosis.reason, "#ffa94d")
        if not diagnosis.actionable:
            return False

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("The server could not start")
        box.setText(diagnosis.reason)
        box.setInformativeText(
            f"Disable '{diagnosis.mod_id}' for the server and try again?\n\n"
            f"This only affects the server. Your Minecraft client keeps the mod, and "
            f"you can re-enable it later."
        )
        box.setDetailedText(diagnosis.excerpt)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.Yes)

        if box.exec() != QMessageBox.Yes:
            return True  # User declined; do not then blindly restart.

        modsync.remember_client_only(diagnosis.mod_id)
        self.console.append_notice(
            f"Disabled {diagnosis.mod_id} for servers. Restarting...", "#9ae6b4"
        )
        self._restarts = 0
        QTimer.singleShot(1000, self.start_server)
        return True

    def _maybe_restart(self) -> None:
        if not self.autorestart_check.isChecked():
            return
        if self._restarts >= self.settings.max_restarts:
            self.console.append_notice(
                f"Not restarting again after {self._restarts} attempts - "
                f"something is wrong that a restart will not fix.",
                "#ffa94d",
            )
            return
        self._restarts += 1
        self.console.append_notice(f"Restarting (attempt {self._restarts})...", "#ffa94d")
        QTimer.singleShot(5000, self.start_server)

    def _send_command(self, command: str) -> None:
        if not self.server or not self.server.is_alive:
            self.console.append_notice("Server is not running.", "#ffa94d")
            return
        try:
            self.server.send(command)
        except RuntimeError as exc:
            self.console.append_notice(str(exc), "#ff6b6b")

    def stop_server(self) -> None:
        if not self.server or not self.server.is_alive:
            return
        self.stop_button.setEnabled(False)
        self.console.append_notice("Stopping server (saving chunks)...")
        server = self.server
        manager = self.connection

        # Keep whatever the scan has seen before the server goes away.
        self._harvest_scan()
        self._stop_passive_scan()

        def work(report):
            code = server.stop()
            # Leaving port mappings behind on someone's router is rude, and a
            # stale tunnel would keep advertising a dead server.
            report("Releasing router ports...")
            manager.release()
            return code

        self._run(work, lambda _: self._update_buttons())

    # --- Connection ---

    def refresh_connection(self) -> None:
        port = self.port_spin.value()
        manager = self.connection
        manager.port = port

        def work(report):
            status = manager.gather(report)
            if status.rung is connection.Rung.UPNP:
                manager.apply_upnp(report)
            if self.server and self.server.is_alive:
                manager.verify(report)
            return status

        self.connection_panel.refresh_button.setEnabled(False)

        def done(status):
            self.connection_panel.refresh_button.setEnabled(True)
            self.connection_panel.update_status(status)
            self._set_status(f"Connection: {status.rung.value}")
            self.refresh_playit_state()

        self._run(work, done)

    def refresh_playit_state(self) -> None:
        """Re-check whether playit.gg is installed and running, off the GUI thread."""
        self._run(
            lambda report: connection.find_playit_install(),
            self.connection_panel.update_playit,
        )

    def setup_playit(self) -> None:
        """Launch an existing playit.gg, or walk through setting one up.

        Downloading our own copy when the user already has playit.gg installed
        would be pointless, so an existing installation is simply started.
        """
        install = connection.find_playit_install()

        if install.installed and install.from_system:
            if install.running:
                self.connection_panel.update_playit(install)
                return
            try:
                connection.launch_playit(install)
            except connection.ConnectionError_ as exc:
                QMessageBox.warning(self, "playit.gg", str(exc))
                return
            self.console.append_notice(f"Started {install.executable.name}.")
            # Give it a moment to appear in the process list before re-checking.
            QTimer.singleShot(3000, self.refresh_playit_state)
            return

        try:
            release = connection.playit_release()
        except Exception as exc:
            QMessageBox.warning(self, "playit.gg", f"Could not look up the agent: {exc}")
            return

        if not connection.playit_is_downloaded():
            answer = QMessageBox.question(
                self,
                "Download the playit.gg agent?",
                f"playit.gg is a third-party service that relays connections so your "
                f"friends can join without you opening any ports.\n\n"
                f"This will download:\n"
                f"  {release.name}\n"
                f"  {release.size / 1024**2:.1f} MB, version {release.version}\n"
                f"  from {release.url}\n\n"
                f"You will then need a free playit.gg account. Arkon Launcher will open "
                f"the sign-up page - it does not create the account or accept their "
                f"terms for you.\n\nDownload it now?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

            def work(report):
                return connection.download_playit(release, report)

            self._run(work, lambda _: self._start_playit())
            return

        self._start_playit()

    def _start_playit(self) -> None:
        agent = connection.PlayitAgent(self.port_spin.value())
        agent.on_line(lambda line: self.server_line.emit(f"[playit] {line}"))
        self.connection.agent = agent
        try:
            agent.start()
        except connection.ConnectionError_ as exc:
            QMessageBox.warning(self, "playit.gg", str(exc))
            return

        self.tabs.setCurrentWidget(self.console)
        self.console.append_notice("Starting the playit.gg agent...")
        QTimer.singleShot(4000, self._check_playit_claim)

    def _check_playit_claim(self) -> None:
        agent = self.connection.agent
        if agent is None:
            return

        if agent.claim_url:
            answer = QMessageBox.question(
                self,
                "Finish playit.gg setup",
                f"Open this page to link the tunnel to your playit.gg account?\n\n"
                f"{agent.claim_url}\n\n"
                f"Sign in (or create a free account) there, then come back - the "
                f"address for your friends will appear on the Connection tab.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                QDesktopServices.openUrl(QUrl(agent.claim_url))
            QTimer.singleShot(5000, self._check_playit_tunnel)
            return

        if agent.tunnel_address:
            self._check_playit_tunnel()
            return

        if agent.is_alive:
            QTimer.singleShot(3000, self._check_playit_claim)

    def _check_playit_tunnel(self) -> None:
        agent = self.connection.agent
        if agent is None:
            return
        if agent.tunnel_address:
            self.connection.status.rung = connection.Rung.PLAYIT
            self.connection.status.address = agent.tunnel_address
            self.connection_panel.update_status(self.connection.status)
            self.console.append_notice(
                f"playit.gg tunnel ready: {agent.tunnel_address}", "#9ae6b4"
            )
            self.tabs.setCurrentWidget(self.connection_panel)
        elif agent.is_alive:
            QTimer.singleShot(4000, self._check_playit_tunnel)

    # --- Players ---

    # --- Passive permission scanning ---

    def _on_server_line(self, line: str) -> None:
        """Route server output, diverting verbose permission checks.

        LuckPerms verbose lines are useful data but useless reading - left in
        the console they would bury everything else - so while a scan is running
        they are collected for node extraction and never displayed.
        """
        if self._scanning and permissionnodes.is_verbose_line(line):
            self._scan_buffer.append(line)
            # Bounded: a long session must not accumulate without limit. Older
            # lines have already been harvested by the periodic sweep.
            if len(self._scan_buffer) > 4000:
                del self._scan_buffer[:2000]
            return
        self.console.append_line(line)

    def _on_passive_scan_toggled(self, enabled: bool) -> None:
        self.settings.passive_permission_scan = enabled
        self.settings.save()
        if enabled:
            self._start_passive_scan()
        else:
            self._harvest_scan()
            self._stop_passive_scan()
            self.console.append_notice("Stopped watching for permission checks.")

    def _start_passive_scan(self) -> None:
        if not self.settings.passive_permission_scan or not self._luckperms_ready():
            return
        self._scanning = True
        self._scan_buffer.clear()
        # Filtered to what we do not already know: the vanilla command nodes are
        # enumerated from the command tree anyway, and they are by far the
        # noisiest checks.
        self.server.send(luckperms.verbose_on(permissionnodes.PASSIVE_FILTER))
        self._scan_timer.start(60_000)
        self.console.append_notice(
            "Watching which permissions the server checks, to fill in the "
            "permissions list. Console output is unaffected."
        )

    def _stop_passive_scan(self) -> None:
        self._scan_timer.stop()
        if self._scanning and self.server and self.server.is_alive:
            try:
                self.server.send(luckperms.verbose_off())
            except RuntimeError:
                pass
        self._scanning = False
        self._scan_buffer.clear()

    def _harvest_scan(self) -> None:
        """Fold whatever verbose has seen into the saved node list."""
        if not self._scan_buffer or not self.instance:
            return
        window, self._scan_buffer = self._scan_buffer, []
        found = permissionnodes.nodes_from_verbose(window)
        if not found:
            return

        known = set(permissionnodes.load_recorded(self.instance.directory))
        fresh = [node for node in found if node not in known]
        if not fresh:
            return

        permissionnodes.save_recorded(self.instance.directory, found)
        self.console.append_notice(
            f"Found {len(fresh)} new permission node(s): "
            f"{', '.join(fresh[:5])}{'...' if len(fresh) > 5 else ''}"
        )
        if self.tabs.currentWidget() is self.permissions_panel:
            self.refresh_permissions()

    def _on_players_changed(self) -> None:
        """Someone joined or left - keep the visible lists honest."""
        current = self.tabs.currentWidget()
        if current is self.players_panel:
            self.refresh_players()
        elif current is self.permissions_panel:
            self.refresh_permissions()
        self._refresh_status()

    def _auto_op_owner(self) -> None:
        """Give the person who created the world operator rights on first run.

        Only ever the world's own creator, taken from ``singleplayer_uuid`` in
        level.dat, and only when they are not an operator already. Turned off by
        unticking the option next to the memory slider.
        """
        if not self.settings.auto_op_owner:
            return
        world = self.selected_world()
        if not world or not world.owner_uuid or not self.instance:
            return
        if not (self.server and self.server.is_alive):
            return

        server_dir = paths.server_dir(self.instance.directory, world.folder_name)
        existing = players.read_ops(server_dir)
        owner_uuid = world.owner_uuid.lower()
        if any(str(entry.get("uuid", "")).lower() == owner_uuid for entry in existing):
            return

        name = players.name_for_uuid(self.instance.directory, world.owner_uuid)
        if not name:
            self.console.append_notice(
                "Could not work out who created this world, so nobody was made an "
                "operator automatically. Use the Players tab to op yourself.",
                "#ffa94d",
            )
            return

        self.console.append_notice(
            f"{name} created this world, so they have been made an operator."
        )
        self._send_command(f"op {name}")
        QTimer.singleShot(1500, self.refresh_players)

    def refresh_players(self) -> None:
        world = self.selected_world()
        if not world or not self.instance:
            return
        server_dir = paths.server_dir(self.instance.directory, world.folder_name)
        online = set(self.server.players) if self.server and self.server.is_alive else set()

        self.players_panel.set_players(
            players.gather_players(
                self.instance.directory, server_dir, world.owner_uuid, online
            )
        )

    def _server_dir(self):
        world = self.selected_world()
        if not world or not self.instance:
            return None
        return paths.server_dir(self.instance.directory, world.folder_name)

    def _set_op(self, player: players.KnownPlayer, op: bool) -> None:
        server_dir = self._server_dir()
        if server_dir is None:
            return
        if self.server and self.server.is_alive:
            # The running server owns these files; go through commands instead.
            self._send_command(f"{'op' if op else 'deop'} {player.name}")
        else:
            players.set_op_offline(server_dir, player, op)
        QTimer.singleShot(500, self.refresh_players)

    def _set_whitelisted(self, player: players.KnownPlayer, allowed: bool) -> None:
        server_dir = self._server_dir()
        if server_dir is None:
            return
        if self.server and self.server.is_alive:
            self._send_command(f"whitelist {'add' if allowed else 'remove'} {player.name}")
        else:
            players.set_whitelisted_offline(server_dir, player, allowed)
        QTimer.singleShot(500, self.refresh_players)

    def _kick_player(self, player: players.KnownPlayer) -> None:
        self._send_command(f"kick {player.name}")
        QTimer.singleShot(500, self.refresh_players)

    # --- Server settings and game rules ---

    def _properties_path(self):
        world = self.selected_world()
        if not world or not self.instance:
            return None
        return paths.server_dir(self.instance.directory, world.folder_name) / "server.properties"

    def refresh_settings(self, force: bool = False) -> None:
        """Repopulate the settings pages from the files on disk.

        Refuses to trample unsaved edits unless the user explicitly asked for a
        refresh, and warns when the file has been changed behind our back.
        """
        world = self.selected_world()
        if not world or not self.instance:
            return

        path = self._properties_path()
        on_disk = provision.read_properties(path) if path else {}

        if not force and self.settings_panel.has_pending:
            # Something is half-edited; leave it alone but check for drift.
            self._warn_if_drifted(on_disk)
            return

        properties = on_disk or {s.key: s.default for s in serversettings.SETTINGS}

        self.settings_panel.load_properties(properties)
        self.settings_panel.load_game_rules(serversettings.read_game_rules(world.folder))
        self.settings_panel.set_seed(serversettings.read_world_seed(world.folder))
        self.settings_panel.set_server_running(bool(self.server and self.server.is_alive))
        self.settings_panel.clear_pending()

        # What the file looked like when we last agreed with it.
        self._properties_snapshot = dict(properties)

        # Game rules queued for the next start are still outstanding.
        for name in self.pending.game_rules:
            self.settings_panel.mark_rule_pending(name, True)

    def _warn_if_drifted(self, on_disk: dict[str, str]) -> None:
        """Tell the user if server.properties was edited outside the launcher."""
        if not self._properties_snapshot:
            return
        drifted = serversettings.properties_differ(on_disk, self._properties_snapshot)
        if not drifted or drifted == self._last_drift_reported:
            return
        self._last_drift_reported = drifted

        names = ", ".join(serversettings.label_for(key) for key in drifted[:4])
        answer = QMessageBox.question(
            self,
            "Settings changed outside Arkon Launcher",
            f"server.properties has been edited since this page was loaded "
            f"({names}{'...' if len(drifted) > 4 else ''}).\n\n"
            f"Refresh this page to show what is actually in the file? Any unsaved "
            f"changes here will be discarded.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self.refresh_settings(force=True)

    def save_settings(self, then_restart: bool = False) -> None:
        """Write pending changes, applying live where the server allows it."""
        path = self._properties_path()
        if path is None:
            return

        panel = self.settings_panel
        running = bool(self.server and self.server.is_alive)
        applied_now: list[str] = []
        needs_restart: list[str] = []

        if panel.pending_settings:
            properties = provision.read_properties(path)
            properties.update(panel.pending_settings)
            path.parent.mkdir(parents=True, exist_ok=True)
            provision.write_properties(path, properties)
            self._properties_snapshot = dict(properties)

            for key, value in panel.pending_settings.items():
                setting = next((s for s in serversettings.SETTINGS if s.key == key), None)
                if setting is None:
                    continue
                if key == "server-port":
                    try:
                        self.port_spin.setValue(int(value))
                    except ValueError:
                        pass
                command = setting.command_for(
                    serversettings.boolean_command_value(setting, value)
                )
                if running and command:
                    self._send_command(command)
                    applied_now.append(setting.label)
                else:
                    needs_restart.append(setting.label)

        for name, value in panel.pending_rules.items():
            if running:
                self._send_command(f"gamerule {name} {value}")
                applied_now.append(name)
            else:
                self.pending.game_rules[name] = value

        queued_rules = 0 if running else len(panel.pending_rules)
        panel.clear_pending()
        for name in self.pending.game_rules:
            panel.mark_rule_pending(name, True)

        parts = []
        if applied_now:
            parts.append(f"{len(applied_now)} applied now")
        if needs_restart:
            parts.append(f"{len(needs_restart)} on next start")
        if queued_rules:
            parts.append(f"{queued_rules} game rule(s) queued")
        panel.flash_saved("Saved - " + (", ".join(parts) if parts else "no changes"))

        if needs_restart and not then_restart and running:
            self.console.append_notice(
                f"Saved. These need a restart to take effect: "
                f"{', '.join(needs_restart)}.",
                "#ffa94d",
            )

        if then_restart and running:
            self.restart_server()

    def save_and_restart(self) -> None:
        self.save_settings(then_restart=True)

    def _apply_pending(self) -> None:
        if self.pending.is_empty() or not (self.server and self.server.is_alive):
            return
        commands = self.pending.as_commands()
        self.console.append_notice(
            f"Applying {len(commands)} setting change(s) saved while the server was off."
        )
        for command in commands:
            try:
                self.server.send(command)
            except RuntimeError:
                break
        self.pending.clear()
        QTimer.singleShot(1500, self.refresh_settings)

    # --- Permissions (LuckPerms) ---

    def _luckperms_ready(self) -> bool:
        server_dir = self._server_dir()
        if server_dir is None:
            return False
        return players.has_luckperms(server_dir / "mods") and bool(
            self.server and self.server.is_alive
        )

    def refresh_permissions(self) -> None:
        server_dir = self._server_dir()
        installed = server_dir is not None and players.has_luckperms(server_dir / "mods")
        running = bool(self.server and self.server.is_alive)
        self.permissions_panel.set_available(installed, running)

        # Player list comes from the instance, so it is useful even when stopped.
        if self.instance and server_dir is not None:
            world = self.selected_world()
            known = players.gather_players(
                self.instance.directory,
                server_dir,
                world.owner_uuid if world else None,
                set(self.server.players) if running else set(),
            )
            self.permissions_panel.users_tab.set_players([p.name for p in known])

        if not self._luckperms_ready():
            return

        server = self.server
        instance_dir = self.instance.directory

        cached_help = self._help_lines

        def work(report):
            report("Reading LuckPerms groups and tracks...")
            groups = luckperms.parse_groups(server.query(luckperms.list_groups()))
            tracks = luckperms.parse_tracks(server.query(luckperms.list_tracks()))

            # Reading the command tree is slow and it cannot change while the
            # server is up, so it is fetched once per run rather than every time
            # the tab is opened.
            help_lines = cached_help
            if help_lines is None:
                report("Reading the command list...")
                help_lines = server.query("help", settle=1.5, timeout=20)

            catalogue = permissionnodes.build_catalogue(
                help_lines, permissionnodes.load_recorded(instance_dir)
            )

            # Mods actually mirrored to the server, so the filter lists what is
            # really loaded rather than everything in the client instance.
            mod_ids = {
                mod.mod_id.lower(): mod.display_name or mod.mod_id
                for mod in (
                    modsync.read_mod_jar(jar)
                    for jar in sorted((server_dir / "mods").glob("*.jar"))
                )
                if mod.mod_id
            }
            return groups, tracks, catalogue, help_lines, mod_ids

        def done(payload):
            groups, tracks, catalogue, help_lines, mod_ids = payload
            self._help_lines = help_lines
            self.permissions_panel.groups_tab.set_groups(groups)
            self.permissions_panel.groups_tab.set_catalogue(catalogue, mod_ids)
            self.permissions_panel.users_tab.set_groups(groups)
            self.permissions_panel.users_tab.set_tracks(tracks)
            self.permissions_panel.tracks_tab.set_groups(groups)
            self.permissions_panel.tracks_tab.set_tracks(tracks)
            self._set_status(
                f"{len(groups)} group(s), {len(tracks)} track(s), "
                f"{len(catalogue.nodes)} known permission(s)"
            )

        self._run(work, done)

    # --- Node discovery via LuckPerms verbose ---

    def start_recording_nodes(self) -> None:
        if not self._luckperms_ready():
            self.permissions_panel.set_recording(False)
            return
        self._recorded_from = len(self.server.recent_lines)
        self._send_command(luckperms.verbose_on())
        self.console.append_notice(
            "Recording permission checks. Have someone play and use commands, then "
            "press Stop recording.",
            "#9ae6b4",
        )

    def stop_recording_nodes(self) -> None:
        if not self._luckperms_ready() or not self.instance:
            return
        server = self.server
        instance_dir = self.instance.directory
        start = getattr(self, "_recorded_from", 0)

        def work(report):
            report("Collecting recorded permissions...")
            lines = server.query(luckperms.verbose_off())
            # Verbose output arrived on the console while recording, so the
            # captured window is the scrollback since it was switched on.
            window = server.recent_lines[start:] + lines
            found = permissionnodes.nodes_from_verbose(window)
            if found:
                permissionnodes.save_recorded(instance_dir, found)
            return found

        def done(found):
            if found:
                self.console.append_notice(
                    f"Recorded {len(found)} permission node(s): "
                    f"{', '.join(found[:6])}{'...' if len(found) > 6 else ''}"
                )
            else:
                self.console.append_notice(
                    "No permission checks were recorded. Nodes are only discovered "
                    "while somebody is actually playing.",
                    "#ffa94d",
                )
            self.refresh_permissions()

        self._run(work, done)

    def _lp(self, command: str, on_reply=None, description: str = "") -> None:
        """Run one LuckPerms command off the GUI thread and hand back its reply."""
        if not self._luckperms_ready():
            return
        server = self.server

        def work(report):
            if description:
                report(description)
            return server.query(command)

        def done(lines):
            if on_reply is not None:
                on_reply(lines)
            else:
                reply = luckperms.response_text(lines)
                if reply:
                    self.console.append_notice(reply.splitlines()[0])
                self.refresh_permissions()

        self._run(work, done)

    def _load_group_permissions(self, group: str) -> None:
        """Read a group's own permissions, what it inherits, and from where.

        Three commands: ``permission info`` lists the group's own nodes but not
        their values; ``permission check`` reports the value of every permission
        the group resolves, direct and inherited, and names the parent each
        inherited one came from; ``group info`` gives the parent list.
        """
        if not self._luckperms_ready():
            return
        server = self.server

        def work(report):
            report(f"Reading permissions for {group}...")
            nodes = luckperms.parse_permission_nodes(
                server.query(luckperms.group_permissions(group))
            )
            info = server.query(luckperms.group_info(group))
            parents = luckperms.parse_group_parents(info)

            values: dict[str, bool] = {}
            inherited: dict[str, tuple[bool, str]] = {}
            if nodes or parents:
                # Any node works as the probe; the reply enumerates them all.
                probe = nodes[0] if nodes else "minecraft.command.help"
                reply = server.query(luckperms.check_group_permission(group, probe))
                values = luckperms.parse_permission_values(reply)
                inherited = luckperms.parse_inherited_permissions(reply)

            return luckperms.combine_permissions(nodes, values), inherited, parents

        def done(payload):
            permissions, inherited, parents = payload
            self.permissions_panel.groups_tab.set_permissions(permissions, inherited)
            self.permissions_panel.groups_tab.set_parents(parents)

        self._run(work, done)

    def _create_group(self, group: str) -> None:
        self._lp(luckperms.create_group(group))

    def _delete_group(self, group: str) -> None:
        self._lp(luckperms.delete_group(group))

    def _assign_nodes(self, group: str, nodes: list[str], allow: bool) -> None:
        self._lp_batch(
            [luckperms.set_group_permission(group, node, allow) for node in nodes],
            lambda: self._load_group_permissions(group),
            f"{'Allowing' if allow else 'Denying'} {len(nodes)} permission(s)...",
        )

    def _unassign_nodes(self, group: str, nodes: list[str]) -> None:
        self._lp_batch(
            [luckperms.unset_group_permission(group, node) for node in nodes],
            lambda: self._load_group_permissions(group),
            f"Removing {len(nodes)} permission(s)...",
        )

    def _set_group_weight(self, group: str, weight: int) -> None:
        self._lp(luckperms.set_group_weight(group, weight))

    def _add_group_parent(self, group: str, parent: str) -> None:
        self._lp(
            luckperms.add_group_parent(group, parent),
            lambda _: self._load_group_permissions(group),
        )

    def _remove_group_parent(self, group: str, parent: str) -> None:
        self._lp(
            luckperms.remove_group_parent(group, parent),
            lambda _: self._load_group_permissions(group),
        )

    def _lp_batch(self, commands: list[str], on_done, description: str) -> None:
        """Run several LuckPerms commands in order, then refresh once."""
        if not self._luckperms_ready() or not commands:
            return
        server = self.server

        def work(report):
            report(description)
            replies = []
            for command in commands:
                replies.append(server.query(command))
            return replies

        def done(replies):
            for reply in replies:
                text = luckperms.response_text(reply)
                if text:
                    self.console.append_notice(text.splitlines()[0])
            on_done()

        self._run(work, done)

    # --- Tracks ---

    def _load_track_path(self, track: str) -> None:
        self._lp(
            luckperms.track_info(track),
            lambda lines: self.permissions_panel.tracks_tab.set_path(
                luckperms.parse_track_path(lines)
            ),
        )

    def _create_track(self, track: str) -> None:
        self._lp(luckperms.create_track(track))

    def _delete_track(self, track: str) -> None:
        self._lp(luckperms.delete_track(track))

    def _track_append(self, track: str, group: str) -> None:
        self._lp(
            luckperms.track_append(track, group),
            lambda _: self._load_track_path(track),
        )

    def _track_remove(self, track: str, group: str) -> None:
        self._lp(
            luckperms.track_remove(track, group),
            lambda _: self._load_track_path(track),
        )

    def _promote_user(self, player: str, track: str) -> None:
        self._lp(luckperms.promote(player, track), lambda _: self._load_user_info(player))

    def _demote_user(self, player: str, track: str) -> None:
        self._lp(luckperms.demote(player, track), lambda _: self._load_user_info(player))

    def _load_user_info(self, player: str) -> None:
        self._lp(
            luckperms.user_info(player),
            lambda lines: self.permissions_panel.users_tab.set_user_info(
                luckperms.parse_user_info(lines, player)
            ),
        )

    def _add_user_to_group(self, player: str, group: str) -> None:
        self._lp(
            luckperms.add_user_to_group(player, group),
            lambda _: self._load_user_info(player),
        )

    def _remove_user_from_group(self, player: str, group: str) -> None:
        self._lp(
            luckperms.remove_user_from_group(player, group),
            lambda _: self._load_user_info(player),
        )

    def _set_user_primary_group(self, player: str, group: str) -> None:
        self._lp(
            luckperms.set_primary_group(player, group),
            lambda _: self._load_user_info(player),
        )

    # --- Backups ---

    def _save_options(self) -> None:
        self.settings.auto_op_owner = self.autoop_check.isChecked()
        self.settings.auto_restart = self.autorestart_check.isChecked()
        self.settings.save()

    # --- Backup schedule ---

    def _on_schedule_changed(self, enabled: bool, hours: int) -> None:
        self.settings.backup_schedule_enabled = enabled
        self.settings.backup_interval_hours = hours
        self.settings.save()
        self._restart_backup_schedule()

    def _on_backup_location_changed(self, location: str) -> None:
        self.settings.backup_location = location
        self.settings.save()
        self.backups_panel.load_settings(self.settings)
        self.refresh_backups()
        self.console.append_notice(
            f"Backups will be saved to {location}" if location
            else "Backups will be saved beside the instance again."
        )

    def _on_announcements_changed(self, enabled: bool, values: list) -> None:
        self.settings.backup_announce_enabled = enabled
        self.settings.backup_announcements = [int(v) for v in values]
        self.settings.save()
        self._restart_backup_schedule()

    def _restart_backup_schedule(self) -> None:
        """(Re)arm the periodic backup, including its warning broadcasts."""
        self._backup_timer.stop()
        for timer in self._announce_timers:
            timer.stop()
        self._announce_timers.clear()

        if not self.settings.backup_schedule_enabled:
            self.backups_panel.set_next_run("Scheduled backups are off.")
            return

        interval_ms = max(1, self.settings.backup_interval_hours) * 3600 * 1000
        self._backup_timer.start(interval_ms)

        if self.settings.backup_announce_enabled:
            for seconds in self.settings.backup_announcements:
                lead_ms = interval_ms - seconds * 1000
                if lead_ms <= 0:
                    continue  # A warning longer than the interval cannot fire.
                timer = QTimer(self)
                timer.setSingleShot(False)
                timer.setInterval(interval_ms)
                timer.timeout.connect(
                    lambda s=seconds: self._announce_backup(s)
                )
                QTimer.singleShot(lead_ms, timer.start)
                QTimer.singleShot(lead_ms, lambda s=seconds: self._announce_backup(s))
                self._announce_timers.append(timer)

        hours = self.settings.backup_interval_hours
        self.backups_panel.set_next_run(
            f"Next automatic backup in {hours} hour{'s' if hours != 1 else ''}."
        )

    def _announce_backup(self, seconds_before: int) -> None:
        if not (self.server and self.server.is_alive):
            return
        from .panels import describe_lead_time

        self._send_command(
            f'say Server backup in {describe_lead_time(seconds_before)}. '
            f"You may notice a brief pause."
        )

    def _scheduled_backup(self) -> None:
        if not (self.server and self.server.is_alive):
            return
        self.console.append_notice("Running scheduled backup...")
        self.backup_now()

    def refresh_backups(self) -> None:
        world = self.selected_world()
        if not world or not self.instance:
            return
        self.backups_panel.set_server_running(bool(self.server and self.server.is_alive))
        self.backups_panel.set_backups(
            backups.list_backups(
                self.instance.directory, world.folder_name, self.settings.backup_location or None
            )
        )

    def backup_now(self) -> None:
        world = self.selected_world()
        if not world or not self.instance:
            return
        instance, server = self.instance, self.server
        keep = self.settings.backup_keep

        custom_root = self.settings.backup_location or None

        def work(report):
            return backups.backup_running_server(
                server if server and server.is_alive else None,
                world.folder,
                instance.directory,
                world.folder_name,
                keep,
                report,
                custom_root=custom_root,
            )

        self.backups_panel.backup_button.setEnabled(False)

        def done(backup):
            self.backups_panel.backup_button.setEnabled(True)
            self.console.append_notice(
                f"Backup saved: {backup.path.name} ({backup.size_mb:,.0f} MB)"
            )
            self.refresh_backups()

        self._run(work, done)

    def restore_backup(self, backup) -> None:
        world = self.selected_world()
        if not world or not self.instance:
            return
        if self.server and self.server.is_alive:
            QMessageBox.warning(
                self, "Stop the server first", "Stop the server before restoring a backup."
            )
            return

        answer = QMessageBox.question(
            self,
            "Restore this backup?",
            f"This replaces '{world.folder_name}' with the backup from "
            f"{backup.label}.\n\nThe current world is saved to a new backup first, so "
            f"this can be undone.\n\nRestore now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        instance = self.instance

        def work(report):
            return backups.restore_backup(
                backup, world.folder, instance.directory, world.folder_name, report
            )

        def done(safety):
            self.console.append_notice(
                f"Restored. Your previous world was saved as {safety.path.name}."
            )
            self.refresh_backups()
            self.load_worlds()

        self._run(work, done)

    def _on_tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is self.players_panel:
            self.refresh_players()
        elif widget is self.backups_panel:
            self.refresh_backups()
        elif widget is self.settings_panel:
            # Opening the page re-reads from disk when nothing is half-edited,
            # so it always reflects reality; otherwise it just checks for drift.
            self.refresh_settings()
        elif widget is self.permissions_panel:
            self.refresh_permissions()
        elif widget is self.connection_panel and not self.connection.status.lan_addresses:
            self.refresh_connection()

    def reload_server(self) -> None:
        """Re-read datapacks and server config without disconnecting anyone."""
        if not (self.server and self.server.is_alive):
            return
        self.console.append_notice("Reloading datapacks and server config...")
        self._send_command("reload")

    def restart_server(self) -> None:
        """Stop, then start again once the process has fully exited."""
        if not (self.server and self.server.is_alive):
            return
        self.console.append_notice("Restarting server...")
        server = self.server
        manager = self.connection

        def work(report):
            report("Stopping server...")
            code = server.stop()
            manager.release()
            return code

        def done(_):
            self._restarts = 0
            self._update_buttons()
            # Start once the old process is gone, so the port is free.
            QTimer.singleShot(2000, self.start_server)

        self._run(work, done)

    def _refresh_status(self) -> None:
        if self.server and self.server.is_alive:
            minutes, seconds = divmod(int(self.server.uptime), 60)
            hours, minutes = divmod(minutes, 60)
            players = len(self.server.players)
            self._set_status(
                f"{self.server.state.value.title()} - up {hours:02d}:{minutes:02d}:{seconds:02d} "
                f"- {players} player(s) online"
            )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Never yank a running server just because the window was closed."""
        if self.server and self.server.is_alive:
            answer = QMessageBox.question(
                self,
                "Server is running",
                "The server is still running. Stop it and quit?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Yes:
                self._set_status("Stopping server...")
                self.server.stop()

        self.connection.release()
        event.accept()
