"""The main window: pick an instance, pick a world, start the server, drive it.

Long operations (mod sync, downloads, world sizing, graceful stop) run on worker
threads; the server's output arrives on its reader thread and is marshalled onto
the GUI thread by a queued signal. Nothing blocking runs in an event handler.
"""

from __future__ import annotations

import os
import time
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QCursor, QDesktopServices, QImage
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QTextBrowser,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import (
    __version__,
    avatars,
    backups,
    connection,
    crashdoctor,
    instances,
    modsync,
    modupdater,
    paths,
    players,
    provision,
    updater,
    worlds,
)
from .. import runner
from ..runner import ServerConfig, ServerProcess, ServerState, sanitize_jvm_args
from ..settings import AppSettings, WorldSettings
from .. import (
    essentials,
    luckperms,
    permissionnodes,
    placeholders,
    serverlist,
    serversettings,
    serverstats,
)
from ..serversettings import PendingChanges
from .config_editor import ConfigEditor
from .console_view import ConsoleView
from .countdown_button import CountdownButton
from .essentials_panel import EssentialsPanel
from .extra_panel import ExtraPanel, describe_hours, describe_restart_lead
from .mods_panel import NOTABLE as NOTABLE_EXCLUSIONS, ModsPanel
from .panels import BackupsPanel, ConnectionPanel
from .players_panel import PlayersPanel
from .permissions_panel import PermissionsPanel
from .settings_panel import ServerSettingsPanel
from .stats_panel import ServerStatsPanel


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
    player_joined = Signal(str)

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
        self.player_joined.connect(self._on_player_joined)

        self._uptime_timer = QTimer(self)
        self._uptime_timer.timeout.connect(self._refresh_status)
        self._uptime_timer.timeout.connect(self._poll_stats)
        self._uptime_timer.start(1000)

        self._process_sampler = serverstats.ProcessSampler()
        self._tick_sampler = serverstats.TickSampler()
        self._tps_countdown = 3
        # Group list, cached across player selections; cleared when it changes.
        self._group_cache: list = []
        self._tps_pending = False
        # None until we learn whether this pack has a /tps command.
        self._tps_command_works: bool | None = None

        self._backup_timer = QTimer(self)
        self._backup_timer.timeout.connect(self._scheduled_backup)
        self._announce_timers: list[QTimer] = []
        self._restart_backup_schedule()

        self._restart_timer = QTimer(self)
        self._restart_timer.timeout.connect(self._scheduled_restart)
        self._restart_announce_timers: list[QTimer] = []
        self._restart_restart_schedule()

        # What server.properties looked like when we last agreed with it, so an
        # edit made outside the launcher can be noticed rather than clobbered.
        self._properties_snapshot: dict[str, str] = {}
        self._last_drift_reported: list[str] = []
        # Whitelist additions made while stopped, applied on next start.
        self._queued_whitelist: set[str] = set()
        # Players whose head has already been requested this session.
        self._avatars_requested: set[str] = set()
        # Set once the window starts closing, so late callbacks stay quiet.
        self._shutting_down = False
        # name -> join time, for session length. Only known for people who
        # joined while the launcher was watching.
        self._player_sessions: dict[str, float] = {}
        # Per-player facts only Arkon Essentials can provide.
        self._player_telemetry: dict = {}
        self._essentials_abilities: list = []

        self._scanning = False
        self._scan_buffer: list[str] = []
        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._harvest_scan)

        self._kill_timer = QTimer(self)
        self._kill_timer.timeout.connect(self._kill_tick)
        self._kill_deadline = 0

        QTimer.singleShot(0, self.discover_instances)
        # A server left behind by a previous session blocks the world and the
        # port, so look before the user tries to start and fails confusingly.
        QTimer.singleShot(1500, self.check_for_orphans)
        # Give the window a moment to appear before touching the network.
        QTimer.singleShot(4000, self.check_for_update)
        QTimer.singleShot(6000, self.check_mod_updates)

    # --- Updates ---

    def check_for_update(self, announce_when_current: bool = False) -> None:
        """See whether a newer release exists. Never downloads on its own."""
        if not self.settings.check_for_updates and not announce_when_current:
            return

        def work(report):
            return updater.fetch_latest()

        def done(release):
            if release is None:
                if announce_when_current:
                    self.console.append_notice(
                        "Could not reach GitHub to check for updates.", "#ffa94d"
                    )
                return
            if not updater.is_newer(release.version):
                if announce_when_current:
                    self.console.append_notice(
                        f"Arkon Launcher {updater.installed_version()} is the newest version."
                    )
                return
            self._offer_update(release)

        self._run(work, done)

    def refresh_mods(self) -> None:
        """Build the mods list, including why each mod is or isn't on the server."""
        if not self.instance:
            return
        instance = self.instance
        world = self.selected_world()
        world_settings = (
            WorldSettings.load(instance.directory, world.folder_name) if world else None
        )

        def work(report):
            report("Reading mods...")
            result = modsync.select_server_mods(
                instance.mods_dir,
                user_disabled_ids=set(world_settings.disabled_mod_ids) if world_settings else set(),
                force_include_ids=set(world_settings.force_include_mod_ids)
                if world_settings
                else set(),
            )
            disabled = modsync.read_disabled_jars(instance.mods_dir)
            duplicates = modsync.duplicates_in(result.included + result.excluded)
            # Every copy except the newest of a duplicated id is the one
            # worth warning about.
            older = {
                jar.path
                for jars in duplicates.values()
                for jar in jars[1:]
            }

            # One walk of the config folder for the whole pack, rather than one
            # per mod.
            config_index = modsync.build_config_index(instance.config_dir)
            gaps = modsync.missing_dependencies(result, instance.mods_dir)

            rows = []
            for mod in result.included + result.excluded + disabled:
                rows.append(
                    {
                        "mod_id": mod.mod_id or "",
                        "name": mod.display_name or mod.mod_id or mod.name,
                        "version": mod.version or "",
                        "environment": mod.environment,
                        "included": mod.included,
                        "reason": mod.excluded_by.value if mod.excluded_by else "",
                        "detail": mod.detail,
                        "notable": mod.excluded_by in NOTABLE_EXCLUSIONS,
                        "file": mod.name,
                        "path": mod.path,
                        "disabled": modsync.is_disabled(mod.path),
                        "is_duplicate": mod.path in older,
                        "configs": modsync.configs_for(mod.mod_id or "", config_index),
                    }
                )
            rows.sort(key=lambda r: r["name"].lower())

            needed_by: dict[str, list] = {}
            for gap in gaps:
                needed_by.setdefault(gap.required_by, []).append(gap)
            for row in rows:
                row["missing"] = needed_by.get(row["name"], [])

            updates = {
                u.installed_file: u
                for u in modupdater.curseforge_updates(instance.directory, instance.mods_dir)
            }
            for row in rows:
                row["update"] = updates.get(row["file"])

            return {
                "rows": rows,
                "duplicates": duplicates,
                "updates": {k: v for k, v in updates.items()},
                "missing": gaps,
            }

        def done(payload):
            self.mods_panel.set_mods(payload)
            self.mods_panel.set_config_root(instance.config_dir)
            self._set_mods_badge(len(payload["updates"]))

            rows = payload["rows"]
            loaded = sum(1 for row in rows if row["included"])
            message = f"{len(rows)} mods read, {loaded} load on the server"
            gaps = payload["missing"]
            if gaps:
                fixable = sum(1 for gap in gaps if gap.fixable)
                message += f" - {len(gaps)} missing dependency(s)"
                if fixable:
                    message += f", {fixable} fixable here"
            self._set_status(message + ".")

        self._run(work, done)

    def _set_mods_badge(self, count: int) -> None:
        """Show the number of available updates on the Mods tab itself."""
        index = self.tabs.indexOf(self.mods_panel)
        if index >= 0:
            self.tabs.setTabText(index, f"Mods ({count})" if count else "Mods")

    def _mods_busy_guard(self) -> bool:
        """Refuse to touch mod files while something has them open."""
        if self.server and self.server.is_alive:
            QMessageBox.information(
                self,
                "Stop the server first",
                "The server has the mod files open. Stop it before changing mods.",
            )
            return False
        return True

    def _apply_mod_update(self, update) -> None:
        if not self.instance or not self._mods_busy_guard():
            return

        answer = QMessageBox.question(
            self,
            f"Update {update.name}?",
            f"{update.installed_file}\n->\n{update.latest_file}\n\n"
            f"{update.size / 1024:,.0f} KB. This changes your CurseForge instance, "
            f"so the Minecraft client gets the new version too.\n\nUpdate now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        self._run_mod_updates([update])

    def _apply_all_mod_updates(self) -> None:
        if not self.instance or not self._mods_busy_guard():
            return
        updates = list(self.mods_panel._updates.values())
        if not updates:
            return

        listing = "\n".join(f"  {u.name}: {u.installed_file} -> {u.latest_file}" for u in updates[:12])
        more = f"\n  ...and {len(updates) - 12} more" if len(updates) > 12 else ""
        total = sum(u.size for u in updates)
        answer = QMessageBox.question(
            self,
            f"Update {len(updates)} mod(s)?",
            f"{listing}{more}\n\nAbout {total / 1024**2:.1f} MB in total.\n\n"
            f"This changes your CurseForge instance. Update all now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._run_mod_updates(updates)

    def _run_mod_updates(self, updates: list) -> None:
        instance = self.instance

        def work(report):
            done, failed = [], []
            for update in updates:
                try:
                    report(f"Updating {update.name}...")
                    modupdater.apply_curseforge_update(
                        update, instance.directory, instance.mods_dir, report
                    )
                    done.append(update)
                except modupdater.ModUpdateError as exc:
                    failed.append((update, str(exc)))
            return done, failed

        def finished(result):
            done, failed = result
            for update in done:
                self.console.append_notice(
                    f"Updated {update.name} to {update.latest_file}."
                )
            for update, reason in failed:
                self.console.append_notice(f"{update.name}: {reason}", "#ff6b6b")
            if done:
                self.console.append_notice(
                    "Updated mods are used the next time the server starts."
                )
            self.refresh_mods()

        self._run(work, finished)

    def _toggle_mod(self, row: dict, enable: bool) -> None:
        """Switch a mod on or off by renaming it, never by deleting it."""
        if not self._mods_busy_guard():
            return
        path = Path(row["path"])

        def work(report):
            if enable:
                report(f"Enabling {row['name']}...")
                return modsync.enable_jar(path), True
            report(f"Disabling {row['name']}...")
            return modsync.disable_jar(path), False

        def done(result):
            new_path, enabled = result
            self.console.append_notice(
                f"{'Enabled' if enabled else 'Disabled'} {row['name']} "
                f"({Path(new_path).name}). Takes effect next time the server starts."
            )
            self.refresh_mods()

        self._run(work, done)

    def _save_essentials_config(self, path, text: str) -> None:
        """Write the mod's config file, mirroring it if a server is up."""
        self._save_config_file(path, text, False)

    def _apply_essentials_live(self, changes: list) -> None:
        """Push changed settings to the running server.

        Sent as well as written, not instead: the running mod holds its own copy
        and rewrites the file on shutdown, which would undo an edit made only on
        disk.
        """
        if not (self.server and self.server.is_alive):
            return
        for key, value in changes:
            self._send_command(f"arkon config {key} {value}")
        self.console.append_notice(
            f"Applied {len(changes)} Essentials setting(s) to the running server."
        )

    def join_world(self) -> None:
        """Put the running world in the client's server list, then open CurseForge.

        Not an auto-join: starting Minecraft straight into a server needs the
        account's session token, which CurseForge holds and which this app has
        no business handling. The two halves it can legitimately do - the list
        entry and opening the launcher - leave one click.
        """
        world = self.selected_world()
        if not (self.instance and world and self.server and self.server.is_alive):
            return

        port = self.settings.server_port or 25565
        address = f"localhost:{port}"
        name = world.level_name or world.folder_name

        try:
            outcome = serverlist.upsert(self.instance.directory, name, address)
        except serverlist.ServerListError as exc:
            QMessageBox.warning(
                self,
                "Could not update the server list",
                f"{exc}\n\nYou can still join by adding {address} by hand.",
            )
            return

        self.console.append_notice(
            f"'{name}' {outcome} in Minecraft's server list as {address}."
            if outcome != "unchanged"
            else f"'{name}' is already in Minecraft's server list as {address}."
        )

        if not QDesktopServices.openUrl(QUrl("curseforge://")):
            QMessageBox.information(
                self,
                "Open CurseForge yourself",
                f"The world is in your multiplayer list as '{name}'. Start the "
                f"instance from CurseForge and it will be waiting under "
                f"Multiplayer.",
            )

    def _fix_dependencies(self, gaps: list) -> None:
        """Switch missing dependencies back on.

        Only ever called with gaps the checker marked fixable, which means the
        jar is sitting in the pack under a .jar.disabled name. Nothing is
        downloaded and nothing is deleted.
        """
        if not self.instance or not self._mods_busy_guard():
            return

        # One jar can supply several missing ids, so enable each file once.
        jars = {gap.jar_path for gap in gaps if gap.jar_path}

        def work(report):
            enabled, failed = [], []
            for jar in sorted(jars):
                try:
                    report(f"Enabling {Path(jar).name}...")
                    modsync.enable_jar(Path(jar))
                    enabled.append(Path(jar).name)
                except OSError as exc:
                    failed.append((Path(jar).name, str(exc)))
            return enabled, failed

        def done(result):
            enabled, failed = result
            for name in enabled:
                self.console.append_notice(f"Switched {name} back on.")
            for name, reason in failed:
                self.console.append_notice(f"Could not enable {name}: {reason}", "#ff6b6b")
            if enabled:
                self.console.append_notice(
                    "Dependencies are picked up the next time the server starts."
                )
            self.refresh_mods()

        self._run(work, done)

    def _install_mod(self, source: str) -> None:
        if not self.instance or not self._mods_busy_guard():
            return
        mods_dir = self.instance.mods_dir

        def work(report):
            report(f"Installing {Path(source).name}...")
            return modsync.install_mod(Path(source), mods_dir)

        def done(result):
            path, mod = result
            self.console.append_notice(
                f"Installed {mod.display_name or mod.mod_id} {mod.version or ''} "
                f"({Path(path).name})."
            )
            if mod.environment == "client":
                self.console.append_notice(
                    f"{mod.display_name or mod.mod_id} is client-only, so it will not "
                    f"be loaded on the server.",
                    "#ffa94d",
                )
            self.refresh_mods()

        self._run(work, done)

    def _uninstall_mod(self, row: dict) -> None:
        if not self._mods_busy_guard():
            return

        path = Path(row["path"])
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(f"Uninstall {row['name']}?")
        box.setText(f"Delete {path.name}?")
        box.setInformativeText(
            "This cannot be undone. Disabling it instead keeps the file and can "
            "be reversed.\n\n"
            "Removing a mod other mods depend on will stop the pack loading."
        )
        disable = box.addButton("Disable instead", QMessageBox.AcceptRole)
        delete = box.addButton("Delete", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(disable)
        box.exec()

        clicked = box.clickedButton()
        if clicked is disable:
            self._toggle_mod(row, enable=False)
            return
        if clicked is not delete:
            return

        def work(report):
            report(f"Removing {path.name}...")
            modsync.uninstall_mod(path)
            return path

        def done(removed):
            self.console.append_notice(f"Deleted {Path(removed).name}.")
            self.refresh_mods()

        self._run(work, done)

    def _fix_duplicate(self, mod_id: str, keep, discard: list) -> None:
        if not self._mods_busy_guard():
            return

        names = "\n".join(f"  {jar.name}" for jar in discard)
        answer = QMessageBox.question(
            self,
            f"Disable {len(discard)} copy of {mod_id}?" if len(discard) == 1
            else f"Disable {len(discard)} copies of {mod_id}?",
            f"Keeping:\n  {keep.name}\n\nRenaming to .jar.disabled:\n{names}\n\n"
            f"Nothing is deleted - rename them back to undo.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        def work(report):
            moved = []
            for jar in discard:
                try:
                    report(f"Disabling {jar.name}...")
                    moved.append(modsync.disable_jar(jar.path))
                except OSError as exc:
                    self.console.append_notice(f"{jar.name}: {exc}", "#ff6b6b")
            return moved

        def finished(moved):
            for path in moved:
                self.console.append_notice(f"Disabled {Path(path).name}.")
            self.refresh_mods()

        self._run(work, finished)

    @staticmethod
    def _up_to_date_summary(named: list, total_current: int) -> str:
        """Name the first-party mods, count the rest.

        Listing a hundred and forty mod names would bury the console; listing
        none reads as though nothing was checked. The Essentials suite is what
        someone actually wants confirmed by name.
        """
        if not named:
            return (
                f"All {total_current} mods are up to date."
                if total_current
                else "No mods are installed, so there was nothing to check."
            )

        listed = ", ".join(f"{name} {version}" for name, version in named)
        others = max(total_current - len(named), 0)
        if others:
            return f"Mods up to date: {listed} and {others} others."
        return f"Mods up to date: {listed}."

    def check_mod_updates(self, announce_when_current: bool = False) -> None:
        """See whether any first-party mod in the pack has a newer release."""
        if not self.instance:
            return
        mods_dir = self.instance.mods_dir

        instance_dir = self.instance.directory

        def work(report):
            report("Checking for mod updates...")
            checked = [
                (modupdater.tracked_for(mod_id).display_name, version)
                for mod_id, (_, version) in modupdater.installed_jars(mods_dir).items()
                if modupdater.tracked_for(mod_id)
            ]
            # The whole pack, not just the mods we track on GitHub - the count
            # someone actually wants is "how many of my mods need updating".
            pack = modupdater.curseforge_updates(instance_dir, mods_dir)
            return modupdater.check_for_updates(mods_dir), checked, pack

        def done(payload):
            updates, checked, pack = payload
            pending = len(pack) + len(updates)

            if not updates:
                # Say so even when there is nothing to do. Silence here is
                # indistinguishable from the check being broken. The two facts
                # are reported separately because they call for different
                # reactions - one is reassurance, the other is a to-do.
                total = len(modupdater.installed_jars(mods_dir))
                if checked or announce_when_current:
                    self.console.append_notice(
                        self._up_to_date_summary(checked, total - pending)
                    )
                if pending:
                    self.console.append_notice(
                        f"{pending} mod(s) pending update. Open the Mods tab to "
                        f"review them.",
                        "#ffa94d",
                    )
                self.mods_panel.set_update_status(
                    f"{pending} update(s) available" if pending else "Up to date"
                )
                self._set_mods_badge(pending)
                return

            self.mods_panel.set_update_status(f"{len(updates)} update(s) available")
            for release, jar, installed in updates:
                self._offer_mod_update(release, jar, installed)

        self._run(work, done)

    def _offer_mod_update(self, release, old_jar, installed_version: str) -> None:
        notes = release.notes.strip()
        if len(notes) > 500:
            notes = notes[:500] + "..."

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(f"{release.mod.display_name} update")
        box.setText(
            f"{release.mod.display_name} {release.version} is available. "
            f"You have {installed_version}."
        )
        box.setInformativeText(
            f"Download it and replace the installed jar?\n\n"
            f"  {release.asset_name}\n"
            f"  {release.asset_size / 1024:.0f} KB\n\n"
            f"This changes your CurseForge instance, so the Minecraft client "
            f"gets the new version too."
        )
        if notes:
            box.setDetailedText(notes)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return

        if self.server and self.server.is_alive:
            QMessageBox.information(
                self,
                "Stop the server first",
                "The server is running and has the mod file open. Stop it, then "
                "try the update again.",
            )
            return

        mods_dir = self.instance.mods_dir

        def work(report):
            return modupdater.install_update(release, old_jar, mods_dir, report)

        def done(path):
            self.console.append_notice(
                f"Updated {release.mod.display_name} to {release.version} "
                f"({path.name}). It will be used the next time the server starts."
            )

        self._run(work, done)

    def _offer_update(self, release) -> None:
        notes = release.notes.strip()
        if len(notes) > 600:
            notes = notes[:600] + "..."

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Update available")
        box.setText(
            f"Arkon Launcher {release.version} is available. You have {updater.installed_version()}."
        )
        if notes:
            box.setDetailedText(notes)

        if not release.has_installer:
            box.setInformativeText(
                "That release has no installer attached, so it has to be downloaded "
                "by hand from the releases page."
            )
            box.setStandardButtons(QMessageBox.Open | QMessageBox.Cancel)
            if box.exec() == QMessageBox.Open:
                QDesktopServices.openUrl(QUrl(release.url))
            return

        if paths.is_portable():
            box.setInformativeText(
                "This is a portable copy, so the installer will not update it in "
                "place. Download it and unpack over this folder yourself - your "
                "data folder is left alone."
            )
            box.setStandardButtons(QMessageBox.Open | QMessageBox.Cancel)
            if box.exec() == QMessageBox.Open:
                QDesktopServices.openUrl(QUrl(release.url))
            return

        box.setInformativeText(
            f"Download and install it now?\n\n"
            f"  {release.asset_name}\n"
            f"  {release.asset_size / 1024**2:.0f} MB\n\n"
            f"The server should be stopped first - the installer will close the "
            f"launcher to replace it."
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return

        if self.server and self.server.is_alive:
            answer = QMessageBox.question(
                self,
                "Server is running",
                "The server is still running. Updating will close the launcher, "
                "and the installer may stop the server with it.\n\nContinue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        def work(report):
            return updater.download_installer(release, report)

        def done(installer):
            answer = QMessageBox.question(
                self,
                "Run the installer?",
                f"Downloaded to:\n{installer}\n\nRun it now?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                try:
                    updater.run_installer(installer)
                except updater.UpdateError as exc:
                    QMessageBox.warning(self, "Update", str(exc))

        self._run(work, done)

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

        self.stop_button = CountdownButton("Server stopped")
        self.stop_button.triggered.connect(self.stop_server)
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip(
            "Saves the world and shuts the server down.\n"
            "Click again to stop immediately, right-click or press Esc to cancel.\n\n"
            "Ctrl+click to force quit instead - use only if it has stopped "
            "responding, as unsaved changes are lost."
        )
        self.stop_button.ctrl_triggered.connect(self.kill_server)

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

        self.join_button = QPushButton("Add to server list")
        self.join_button.setEnabled(False)
        self.join_button.clicked.connect(self.join_world)
        self.join_button.setToolTip(
            "Adds this world to Minecraft's multiplayer list as localhost and "
            "opens CurseForge. It cannot join for you - that needs the account's "
            "session token, which CurseForge holds."
        )

        button_row_two = QHBoxLayout()
        button_row_two.addWidget(self.reload_button)
        button_row_two.addWidget(self.restart_button)

        button_row_three = QHBoxLayout()
        button_row_three.addWidget(self.join_button)

        # Pickers while stopped, health while running: you cannot change world
        # without stopping, so the pickers are dead weight once it is up.
        picker = QWidget()
        picker_layout = QVBoxLayout(picker)
        picker_layout.setContentsMargins(0, 0, 0, 0)
        picker_layout.addWidget(instance_group)
        picker_layout.addWidget(world_group, 1)

        self.stats_panel = ServerStatsPanel()

        self.left_stack = QStackedWidget()
        self.left_stack.addWidget(picker)
        self.left_stack.addWidget(self.stats_panel)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.left_stack, 1)
        left_layout.addWidget(options_group)
        left_layout.addLayout(button_row)
        left_layout.addLayout(button_row_two)
        left_layout.addLayout(button_row_three)

        self.console = ConsoleView()
        self.console.command_entered.connect(self._send_command)
        self.console.player_clicked.connect(self._show_player_actions)

        self.connection_panel = ConnectionPanel()
        self.connection_panel.refresh_requested.connect(self.refresh_connection)
        self.connection_panel.playit_requested.connect(self.setup_playit)

        self.players_panel = PlayersPanel()
        self.players_panel.player_selected.connect(self._on_player_selected)
        detail = self.players_panel.detail
        detail.op_toggled.connect(self._set_op)
        detail.whitelist_toggled.connect(self._set_whitelisted)
        detail.ban_toggled.connect(self._set_banned)
        detail.kick_requested.connect(self._kick_player)
        detail.abilities_applied.connect(self._apply_abilities)
        detail.teleport_requested.connect(self._send_command)
        detail.group_added.connect(self._add_user_to_group)
        detail.group_removed.connect(self._remove_user_from_group)
        detail.permission_set.connect(self._set_user_permission)
        detail.permission_unset.connect(self._unset_user_permission)

        self.backups_panel = BackupsPanel()
        self.backups_panel.backup_requested.connect(self.backup_now)
        self.backups_panel.restore_requested.connect(self.restore_backup)
        self.backups_panel.schedule_changed.connect(self._on_schedule_changed)
        self.backups_panel.location_changed.connect(self._on_backup_location_changed)
        self.backups_panel.announcements_changed.connect(self._on_announcements_changed)
        self.backups_panel.load_settings(self.settings)

        self.extra_panel = ExtraPanel()
        self.extra_panel.join_broadcast_changed.connect(self._on_join_broadcast_changed)
        self.extra_panel.restart_schedule_changed.connect(self._on_restart_schedule_changed)
        self.extra_panel.restart_announcements_changed.connect(
            self._on_restart_announcements_changed
        )
        self.extra_panel.restart_countdown_changed.connect(self._on_restart_countdown_changed)
        self.extra_panel.actions_changed.connect(self._on_custom_actions_changed)
        self.extra_panel.help_requested.connect(self.show_placeholder_help)
        self.extra_panel.load_settings(self.settings)

        self.mods_panel = ModsPanel()
        self.mods_panel.refresh_requested.connect(self.refresh_mods)
        self.mods_panel.check_updates_requested.connect(
            lambda: self.check_mod_updates(announce_when_current=True)
        )
        self.mods_panel.save_config.connect(self._save_config_file)
        self.mods_panel.update_one.connect(self._apply_mod_update)
        self.mods_panel.update_all.connect(self._apply_all_mod_updates)
        self.mods_panel.fix_duplicate.connect(self._fix_duplicate)
        self.mods_panel.toggle_mod.connect(self._toggle_mod)
        self.mods_panel.install_mod.connect(self._install_mod)
        self.mods_panel.uninstall_mod.connect(self._uninstall_mod)
        self.mods_panel.fix_dependencies.connect(self._fix_dependencies)
        self.config_editor = self.mods_panel.config_editor

        self.settings_panel = ServerSettingsPanel()
        self.settings_panel.save_requested.connect(self.save_settings)
        self.settings_panel.save_and_restart_requested.connect(self.save_and_restart)
        self.settings_panel.refresh_requested.connect(lambda: self.refresh_settings(force=True))
        self.settings_panel.whitelist.add_requested.connect(self._whitelist_add)
        self.settings_panel.whitelist.remove_requested.connect(self._whitelist_remove)
        self.settings_panel.icon_picker.icon_chosen.connect(self._set_server_icon)
        self.settings_panel.icon_picker.icon_cleared.connect(self._clear_server_icon)

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

        # Backups and the extras are settings, so they sit inside Settings rather
        # than competing with it at the top level.
        self.settings_panel.add_sub_tab(self.backups_panel, "Backups")
        self.settings_panel.add_sub_tab(self.extra_panel, "Extra")

        self.essentials_panel = EssentialsPanel()
        self.essentials_panel.save_requested.connect(self._save_essentials_config)
        self.essentials_panel.apply_live.connect(self._apply_essentials_live)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.console, "Console")
        self.tabs.addTab(self.connection_panel, "Connection")
        self.tabs.addTab(self.settings_panel, "Settings")
        self.tabs.addTab(self.players_panel, "Players")
        self.tabs.addTab(self.permissions_panel, "Permissions")
        self.tabs.addTab(self.mods_panel, "Mods")
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
        # Abilities come from a resource in the mod jar, so this works with
        # the server stopped and costs nothing when the mod is absent.
        self._essentials_abilities = essentials.live_state(
            essentials.read_abilities(instance.mods_dir)
        )

        # The Essentials tab exists only for instances that have the mod.
        present = self.essentials_panel.set_instance(
            instance.config_dir, instance.mods_dir
        )
        self.settings_panel.set_essentials_panel(
            self.essentials_panel if present else None
        )

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
        # Reads as a state when there is nothing to stop, an action when there is.
        self.stop_button.setText("Stop server" if running else "Server stopped")
        self.reload_button.setEnabled(running)
        self.restart_button.setEnabled(running)
        self.settings_panel.set_server_running(running)
        self.essentials_panel.set_running(running)
        self.join_button.setEnabled(running)

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
        server.on_join(self.player_joined.emit)
        self.server = server

        try:
            server.start()
        except Exception as exc:
            self._on_worker_failed(str(exc))
            return

        self.console.set_enabled_for_running(True)
        self._update_buttons()

        world = self.selected_world()
        if world:
            self.stats_panel.set_world(world.level_name, instance.mc_version)
        self.stats_panel.clear()
        self._tick_sampler.reset()
        self._tps_command_works = None
        self._process_sampler.attach(
            server._process.pid if server._process else None
        )
        # Work out how friends will connect while the world finishes loading.
        QTimer.singleShot(1500, self.refresh_connection)

    def _on_server_state(self, state: ServerState) -> None:
        # Nothing may open a dialog or schedule work once the window is closing.
        if self._shutting_down:
            return
        if state is ServerState.RUNNING:
            self.console.append_notice("Server is ready. Friends can connect now.")
            self._restarts = 0
            QTimer.singleShot(1000, self._apply_pending)
            QTimer.singleShot(2000, self._auto_op_owner)
            QTimer.singleShot(3000, self.refresh_permissions)
            QTimer.singleShot(4000, self._start_passive_scan)
            QTimer.singleShot(5000, self._apply_queued_whitelist)
            QTimer.singleShot(5500, self._load_command_completions)
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

    # --- Player actions from the console ---

    def _show_player_actions(self, name: str) -> None:
        """Menu of things that can be done to the player whose head was clicked.

        Toggles read their current state first, so the menu says what clicking
        will actually do rather than offering both directions and hoping.
        """
        server_dir = self._server_dir()
        if server_dir is None or not (self.server and self.server.is_alive):
            return

        is_op = any(
            str(entry.get("name", "")).lower() == name.lower()
            for entry in players.read_ops(server_dir)
        )
        is_whitelisted = any(
            str(entry.get("name", "")).lower() == name.lower()
            for entry in players.read_whitelist(server_dir)
        )

        menu = QMenu(self)
        menu.addAction(name).setEnabled(False)
        menu.addSeparator()

        def add(label: str, command: str) -> None:
            action = menu.addAction(label)
            action.triggered.connect(lambda _=False, c=command: self._player_command(c, name))

        add("Remove operator" if is_op else "Make operator",
            "deop {player}" if is_op else "op {player}")
        add("Remove from whitelist" if is_whitelisted else "Add to whitelist",
            "whitelist remove {player}" if is_whitelisted else "whitelist add {player}")
        menu.addSeparator()
        add("Kick", "kick {player}")
        add("Ban", "ban {player}")
        menu.addSeparator()
        add("Teleport to spawn point", "tp {player} @e[type=marker,limit=1]")
        add("Survival", "gamemode survival {player}")
        add("Creative", "gamemode creative {player}")
        add("Spectator", "gamemode spectator {player}")

        custom = [
            entry
            for entry in (self.settings.custom_player_actions or [])
            if isinstance(entry, dict) and entry.get("label") and entry.get("command")
        ]
        if custom:
            menu.addSeparator()
            for entry in custom:
                add(str(entry["label"]), str(entry["command"]))

        menu.addSeparator()
        insert = menu.addAction("Put the name in the command box")
        insert.triggered.connect(lambda: self.console.append_player_name(name))

        button = self.console.player_button(name)
        if button is not None:
            menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        else:
            menu.exec(QCursor.pos())

    def _player_command(self, template: str, name: str) -> None:
        world = self.selected_world()
        properties = self._server_properties()
        command = placeholders.substitute(
            template,
            player=name,
            world=world.level_name if world else "",
            online=len(self.server.players) if self.server else 0,
            max=properties.get("max-players", ""),
            version=self.instance.mc_version if self.instance else "",
        )
        self._send_command(command)

    def _on_custom_actions_changed(self, entries: list) -> None:
        self.settings.custom_player_actions = [
            {"label": str(e.get("label")), "command": str(e.get("command"))}
            for e in entries
            if isinstance(e, dict) and e.get("label") and e.get("command")
        ]
        self.settings.save()

    def show_placeholder_help(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Placeholders and colours")
        dialog.resize(620, 640)

        view = QTextBrowser()
        view.setHtml(placeholders.help_html())
        view.setOpenExternalLinks(True)

        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)

        layout = QVBoxLayout(dialog)
        layout.addWidget(view, 1)
        layout.addWidget(close, alignment=Qt.AlignRight)
        dialog.exec()

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
        self._refresh_console_players()

    def _refresh_console_players(self) -> None:
        """Update the console's player strip and the completer's word list."""
        online = sorted(self.server.players) if self.server and self.server.is_alive else []
        self.console.set_players(online)
        self._refresh_completions(online)

        # Heads come from Mojang, so they are fetched off the GUI thread and
        # only once per player - the result is cached on disk.
        for name in online:
            uuid = (self.server.player_uuids or {}).get(name) if self.server else None
            if not uuid or name in self._avatars_requested:
                continue
            self._avatars_requested.add(name)
            self._run(
                lambda report, u=uuid: avatars.fetch_head(u, 24),
                lambda path, n=name: self.console.set_avatar(n, str(path)) if path else None,
            )

    def _load_command_completions(self) -> None:
        """Read the command tree once per run so the console can complete it."""
        if self._help_lines is not None or not (self.server and self.server.is_alive):
            self._refresh_completions()
            return
        server = self.server

        def work(report):
            return server.query("help", settle=1.5, timeout=20)

        def done(lines):
            self._help_lines = lines
            self._refresh_completions()

        self._run(work, done)

    def _refresh_completions(self, online: list[str] | None = None) -> None:
        """Commands from the live server, plus whoever is online."""
        words: list[str] = []
        if self._help_lines:
            words.extend(
                node.node.rsplit(".", 1)[-1]
                for node in permissionnodes.nodes_from_help(self._help_lines)
            )
        if online is None:
            online = sorted(self.server.players) if self.server and self.server.is_alive else []
        words.extend(online)
        self.console.set_completions(sorted(set(words)))

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
        """Rebuild the player list and, if one is selected, its detail panel."""
        world = self.selected_world()
        if not world or not self.instance:
            return
        server_dir = paths.server_dir(self.instance.directory, world.folder_name)
        online = set(self.server.players) if self.server and self.server.is_alive else set()

        known = players.gather_players(
            self.instance.directory, server_dir, world.owner_uuid, online
        )
        banned = players.read_banned(server_dir)
        for player in known:
            player.is_banned = any(
                str(entry.get("name", "")).lower() == player.name.lower()
                for entry in banned
            )

        self.players_panel.set_players(known)
        self._request_player_avatars(known)
        self._on_player_selected(self.players_panel.selected())

    def _request_player_avatars(self, known: list) -> None:
        """Faces for the list, fetched once each and cached on disk."""
        uuids = {p.name: p.uuid for p in known if p.uuid}
        for name, uuid in uuids.items():
            cached = avatars.cached_head(uuid)
            if cached:
                self.players_panel.set_avatar(name, str(cached))
                continue
            if name in self._avatars_requested:
                continue
            self._avatars_requested.add(name)
            self._run(
                lambda report, u=uuid: avatars.fetch_head(u, 24),
                lambda path, n=name: self.players_panel.set_avatar(n, str(path))
                if path
                else None,
            )

    def _on_player_selected(self, player) -> None:
        detail = self.players_panel.detail
        if player is None:
            detail.set_player(None)
            return

        avatar = avatars.cached_head(player.uuid) if player.uuid else None
        session = ""
        started = self._player_sessions.get(player.name)
        if player.is_online and started:
            session = serverstats.format_uptime(time.time() - started)
        elif player.is_online:
            # Joined before the launcher was watching - say so rather than
            # inventing a duration.
            session = "connected"

        detail.set_player(player, str(avatar) if avatar else "", session)

        # Declared defaults first, so the section is populated immediately; the
        # live resolution replaces it when the server answers.
        detail.set_abilities(self._essentials_abilities, {}, live=False,
                             online=sorted(self.server.players) if self.server else [])
        self._show_ping(player)
        self._load_essentials(player)

        if not self._luckperms_ready():
            detail.set_permissions_available(
                False,
                "Start the server to see and change permissions - LuckPerms only "
                "answers while it is running.",
            )
            return

        detail.set_permissions_available(True)
        self._load_player_permissions(player)

    def _essentials_ready(self) -> bool:
        """True when the mod is installed, declares a manifest, and is running."""
        return bool(
            self._essentials_abilities and self.server and self.server.is_alive
        )

    def _show_ping(self, player) -> None:
        telemetry = self._player_telemetry.get(player.name)
        if telemetry is None or telemetry.ping_ms is None:
            self.players_panel.detail.ping.setText(
                "not available" if player.is_online else "-"
            )
            return
        self.players_panel.detail.ping.setText(f"{telemetry.ping_ms} ms")

    def _load_essentials(self, player) -> None:
        """Latency and resolved abilities, both straight from the mod.

        ``/arkon perms`` is asked in preference to reading LuckPerms because it
        reports what the mod itself concluded - including its own fallbacks -
        rather than what one particular permission plugin was told.
        """
        if not self._essentials_ready():
            return

        server = self.server
        name = player.name
        # Offline players are not in the ping report and cannot be looked up by
        # name; the mod accepts a UUID for exactly this case.
        target = name if player.is_online else (player.uuid or name)

        def ask(command, parse):
            """Send a command and parse the reply, retrying once.

            Commands run on the server thread, which blocks during an autosave -
            on a large world that can outlast a single capture window, and the
            reply then arrives after nobody is listening. One retry costs a
            second in the rare case and turns a blank ping into a real one.
            """
            for attempt in range(2):
                parsed = parse(server.query(command, timeout=10.0))
                if parsed:
                    return parsed
            return parse([])

        def work(report):
            report(f"Asking Arkon Essentials about {name}...")
            pings = ask(essentials.PING_COMMAND, essentials.parse_ping_report)
            resolved = ask(
                essentials.perms_command(target), essentials.parse_perms_report
            )
            return pings, resolved

        def done(payload):
            pings, resolved = payload
            for row in pings:
                self._player_telemetry[row.name] = row

            current = self.players_panel.selected()
            if current is None or current.name != name:
                return  # Selection moved on while we were asking.
            self._show_ping(current)
            if resolved:
                self.players_panel.detail.set_abilities(
                    self._essentials_abilities, resolved, live=True,
                    online=sorted(server.players),
                )

        self._run(work, done)

    def _apply_abilities(self, player, changes: dict) -> None:
        """Apply staged mode changes as the mod's own grant/revoke commands.

        A mode is live state, not a permission, so it is set with
        ``/admin grant <mode> <player>`` rather than by writing a node. Turning
        one off is a revoke, which the mod treats as "no mode" - it has no
        per-mode off switch, because only one can be active at a time.
        """
        if not (self.server and self.server.is_alive) or not changes:
            return

        name = self._player_name(player)
        server = self.server
        by_node = {a.node: a for a in self._essentials_abilities}

        commands: list[str] = []
        skipped: list[str] = []
        for node, on in sorted(changes.items()):
            ability = by_node.get(node)
            if ability is None:
                continue
            template = ability.grant_command if on else ability.revoke_command
            if not template:
                skipped.append(ability.label)
                continue
            commands.append(template.lstrip("/").replace("<player>", name))

        if skipped:
            self.console.append_notice(
                f"Not applied - Arkon Essentials has no command to set these for "
                f"another player: {', '.join(sorted(set(skipped)))}.",
                "#ffa94d",
            )
        if not commands:
            return

        def work(report):
            for index, command in enumerate(commands, 1):
                report(f"Applying {index} of {len(commands)} to {name}...")
                server.query(command, settle=0.2, timeout=8)
            return len(commands)

        def done(count):
            self.console.append_notice(f"Applied {count} change(s) to {name}.")
            self._load_essentials(player)

        self._run(work, done)

    def _load_player_permissions(self, player) -> None:
        """Groups, own permissions, and what those groups grant."""
        if not self._luckperms_ready():
            return
        server = self.server
        name = player.name

        cached_groups = self._group_cache

        def work(report):
            report(f"Reading permissions for {name}...")
            ask = lambda command: server.query(command, settle=0.25)

            info = luckperms.parse_user_info(ask(luckperms.user_info(name)), name)
            nodes = luckperms.parse_permission_nodes(ask(luckperms.user_permissions(name)))

            probe = nodes[0] if nodes else "minecraft.command.help"
            reply = ask(luckperms.check_user_permission(name, probe))
            values = luckperms.parse_permission_values(reply)
            inherited = luckperms.parse_inherited_permissions(reply)

            # The group list barely changes and costs a round trip per player
            # selection, so it is fetched once and reused.
            groups = cached_groups or luckperms.parse_groups(ask(luckperms.list_groups()))
            return info, luckperms.combine_permissions(nodes, values), inherited, groups

        def done(payload):
            info, own, inherited, groups = payload
            self._group_cache = groups
            detail = self.players_panel.detail
            detail.set_groups(info.groups, [g.name for g in groups], info.primary_group or "")
            detail.set_permissions(own, inherited)
            # Abilities are not derived from LuckPerms: /arkon perms answers for
            # whatever provider is installed, and reports the mod's own fallback
            # too, which LuckPerms has no way to know about.

        self._run(work, done)

    def _set_banned(self, player, banned: bool) -> None:
        if self.server and self.server.is_alive:
            self._send_command(
                f"ban {player.name}" if banned else f"pardon {player.name}"
            )
        else:
            self.console.append_notice(
                "Start the server to ban or unban - the ban list is owned by it "
                "while it runs.",
                "#ffa94d",
            )
            return
        QTimer.singleShot(800, self.refresh_players)

    def _set_user_permission(self, player, node: str, allow: bool) -> None:
        self._lp(
            luckperms.set_user_permission(player.name, node, allow),
            lambda _: self._load_player_permissions(player),
        )

    def _unset_user_permission(self, player, node: str) -> None:
        self._lp(
            luckperms.unset_user_permission(player.name, node),
            lambda _: self._load_player_permissions(player),
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
        self.settings_panel.icon_picker.set_icon(self._server_icon_path())
        self._refresh_whitelist()

        # What the file looked like when we last agreed with it.
        self._properties_snapshot = dict(properties)

        # Game rules queued for the next start are still outstanding.
        for name in self.pending.game_rules:
            self.settings_panel.mark_rule_pending(name, True)

    # --- Whitelist ---

    def _refresh_whitelist(self) -> None:
        server_dir = self._server_dir()
        if server_dir is None:
            return
        names = [
            str(entry.get("name"))
            for entry in players.read_whitelist(server_dir)
            if entry.get("name")
        ]
        self.settings_panel.whitelist.set_names(
            names,
            sorted(self._queued_whitelist),
            bool(self.server and self.server.is_alive),
        )

    def _whitelist_add(self, name: str) -> None:
        """Whitelist someone, including people who have never connected.

        A running server resolves the name against Mojang for us, which is the
        only reliable way to get their UUID. With the server stopped we queue it
        rather than writing a UUID-less entry the server would ignore.
        """
        if self.server and self.server.is_alive:
            self._send_command(f"whitelist add {name}")
            QTimer.singleShot(1200, self._refresh_whitelist)
        else:
            self._queued_whitelist.add(name)
            self.console.append_notice(
                f"{name} will be whitelisted when the server next starts."
            )
            self._refresh_whitelist()

    def _whitelist_remove(self, name: str) -> None:
        if name in self._queued_whitelist:
            self._queued_whitelist.discard(name)
            self._refresh_whitelist()
            return

        server_dir = self._server_dir()
        if self.server and self.server.is_alive:
            self._send_command(f"whitelist remove {name}")
            QTimer.singleShot(1200, self._refresh_whitelist)
        elif server_dir is not None:
            players.set_whitelisted_offline(
                server_dir, players.KnownPlayer(name=name), False
            )
            self._refresh_whitelist()

    def _apply_queued_whitelist(self) -> None:
        if not self._queued_whitelist or not (self.server and self.server.is_alive):
            return
        for name in sorted(self._queued_whitelist):
            self._send_command(f"whitelist add {name}")
        self.console.append_notice(
            f"Whitelisted {len(self._queued_whitelist)} player(s) saved earlier."
        )
        self._queued_whitelist.clear()
        QTimer.singleShot(1500, self._refresh_whitelist)

    # --- Server icon ---

    def _server_icon_path(self):
        server_dir = self._server_dir()
        return (server_dir / "server-icon.png") if server_dir else None

    def _set_server_icon(self, source: str) -> None:
        """Scale whatever they picked into the 64x64 PNG Minecraft requires."""
        destination = self._server_icon_path()
        if destination is None:
            return

        image = QImage(source)
        if image.isNull():
            QMessageBox.warning(self, "Server icon", f"Could not read {source}.")
            return

        scaled = image.scaled(
            64, 64, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
        ).convertToFormat(QImage.Format_ARGB32)

        destination.parent.mkdir(parents=True, exist_ok=True)
        if not scaled.save(str(destination), "PNG"):
            QMessageBox.warning(self, "Server icon", "Could not write server-icon.png.")
            return

        self.settings_panel.icon_picker.set_icon(destination)
        self.console.append_notice(
            "Server icon updated. Players see it after the server restarts."
        )

    def _clear_server_icon(self) -> None:
        destination = self._server_icon_path()
        if destination and destination.is_file():
            try:
                destination.unlink()
            except OSError as exc:
                QMessageBox.warning(self, "Server icon", str(exc))
                return
        self.settings_panel.icon_picker.set_icon(None)

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
            # LuckPerms answers in one burst, so the default 0.6s of required
            # quiet was pure waiting - and the permissions editor runs several
            # of these per selection, which is what made it feel sluggish.
            return server.query(command, settle=0.25)

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

    @staticmethod
    def _player_name(player) -> str:
        """Accept either a name or a player object.

        Two panels drive these: the Permissions tab passes a name, the Players
        tab passes the KnownPlayer it is showing. Passing the object straight
        through built commands like ``lp user KnownPlayer(name=...) parent add``,
        which the server rejected silently - the group simply never appeared.
        """
        return player if isinstance(player, str) else getattr(player, "name", str(player))

    def _add_user_to_group(self, player, group: str) -> None:
        name = self._player_name(player)
        self._lp(
            luckperms.add_user_to_group(name, group),
            lambda _: self._after_group_change(player, name),
        )

    def _remove_user_from_group(self, player, group: str) -> None:
        name = self._player_name(player)
        self._lp(
            luckperms.remove_user_from_group(name, group),
            lambda _: self._after_group_change(player, name),
        )

    def _set_user_primary_group(self, player, group: str) -> None:
        name = self._player_name(player)
        self._lp(
            luckperms.set_primary_group(name, group),
            lambda _: self._after_group_change(player, name),
        )

    def _after_group_change(self, player, name: str) -> None:
        """Refresh whichever panel asked for the change."""
        self._load_user_info(name)
        if not isinstance(player, str):
            self._load_player_permissions(player)

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

    # --- Config file editing ---

    def _save_config_file(self, path, text: str, reload_after: bool) -> None:
        """Write the instance's config file, then re-mirror it to the server.

        The instance copy is authoritative - the server's config folder is
        rebuilt from it on every start - so that is what gets written. The
        mirrored copy is refreshed too, otherwise a running server would keep
        using the old contents until the next restart.
        """
        path = Path(path)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return

        self.config_editor.mark_saved()
        self.console.append_notice(f"Saved {path.name}.")

        server_dir = self._server_dir()
        if server_dir and self.instance:
            try:
                relative = path.relative_to(self.instance.config_dir)
                modsync._mirror_file(path, server_dir / "config" / relative)
            except (ValueError, OSError):
                pass

        if reload_after and self.server and self.server.is_alive:
            self.console.append_notice(
                "Running /reload. Datapacks and functions are re-read; most mod "
                "configs only load at startup and need a restart.",
                "#ffa94d",
            )
            self._send_command("reload")

    # --- Join broadcast and scheduled restarts ---

    def _on_join_broadcast_changed(self, enabled: bool, message: str) -> None:
        self.settings.join_broadcast_enabled = enabled
        self.settings.join_broadcast_message = message
        self.settings.save()

    def _on_player_joined(self, name: str) -> None:
        self._player_sessions[name] = time.time()
        if not self.settings.join_broadcast_enabled:
            return
        message = self.settings.join_broadcast_message.replace("{player}", name)
        if message.strip():
            self._send_command(f"say {message}")

    def _on_restart_schedule_changed(self, enabled: bool, hours: int) -> None:
        self.settings.restart_schedule_enabled = enabled
        self.settings.restart_interval_hours = hours
        self.settings.save()
        self._restart_restart_schedule()

    def _on_restart_announcements_changed(self, enabled: bool, values: list) -> None:
        self.settings.restart_announce_enabled = enabled
        self.settings.restart_announcements = [int(v) for v in values]
        self.settings.save()
        self._restart_restart_schedule()

    def _on_restart_countdown_changed(self, enabled: bool, seconds: int) -> None:
        self.settings.restart_countdown_enabled = enabled
        self.settings.restart_countdown_seconds = seconds
        self.settings.save()

    def _restart_restart_schedule(self) -> None:
        """(Re)arm the periodic restart and its warnings."""
        self._restart_timer.stop()
        for timer in self._restart_announce_timers:
            timer.stop()
        self._restart_announce_timers.clear()

        if not self.settings.restart_schedule_enabled:
            self.extra_panel.set_next_run("Scheduled restarts are off.")
            return

        interval_ms = max(1, self.settings.restart_interval_hours) * 3600 * 1000
        self._restart_timer.start(interval_ms)

        if self.settings.restart_announce_enabled:
            for seconds in self.settings.restart_announcements:
                lead_ms = interval_ms - seconds * 1000
                if lead_ms <= 0:
                    continue  # A warning longer than the interval can never fire.
                timer = QTimer(self)
                timer.setInterval(interval_ms)
                timer.timeout.connect(lambda s=seconds: self._announce_restart(s))
                QTimer.singleShot(lead_ms, timer.start)
                QTimer.singleShot(lead_ms, lambda s=seconds: self._announce_restart(s))
                self._restart_announce_timers.append(timer)

        self.extra_panel.set_next_run(
            f"Next automatic restart in "
            f"{describe_hours(self.settings.restart_interval_hours).lower().replace('every ', '')}."
        )

    def _announce_restart(self, seconds_before: int) -> None:
        if not (self.server and self.server.is_alive):
            return
        self._send_command(
            f"say Server restart in {describe_restart_lead(seconds_before)}."
        )

    def _scheduled_restart(self) -> None:
        """Count down out loud, then restart."""
        if not (self.server and self.server.is_alive):
            return

        if not self.settings.restart_countdown_enabled:
            self.restart_server()
            return

        total = self.settings.restart_countdown_seconds
        for remaining in range(total, 0, -1):
            QTimer.singleShot(
                (total - remaining) * 1000,
                lambda r=remaining: self._countdown_tick(r),
            )
        QTimer.singleShot(total * 1000, self.restart_server)

    def _countdown_tick(self, remaining: int) -> None:
        if self.server and self.server.is_alive:
            self._send_command(f"say Restarting in {remaining}...")

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
        elif widget is self.mods_panel:
            self.refresh_mods()
        elif widget is self.connection_panel and not self.connection.status.lan_addresses:
            self.refresh_connection()

    def kill_server(self, countdown: int = 10) -> None:
        """Force quit after a short countdown - the hung-server escape hatch."""
        if not (self.server and self.server.is_alive):
            self.check_for_orphans(announce_when_none=True)
            return

        answer = QMessageBox.question(
            self,
            "Force quit the server?",
            f"This kills the server without letting it save. Anything since the "
            f"last save is lost.\n\n"
            f"Only worth doing if it has stopped responding - Stop asks it to "
            f"shut down cleanly.\n\n"
            f"Force quit in {countdown} seconds?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.console.append_notice(
            f"Force quitting in {countdown} seconds. Press Stop to shut down "
            f"cleanly instead.",
            "#ff6b6b",
        )
        self._kill_deadline = countdown
        self._kill_timer.start(1000)

    def _kill_tick(self) -> None:
        self._kill_deadline -= 1
        if not (self.server and self.server.is_alive):
            self._kill_timer.stop()
            return
        if self._kill_deadline > 0:
            self._set_status(f"Force quitting in {self._kill_deadline}...")
            return

        self._kill_timer.stop()
        self.console.append_notice("Force quitting the server now.", "#ff6b6b")
        server = self.server
        manager = self.connection

        def work(report):
            report("Force quitting...")
            code = server.kill()
            manager.release()
            return code

        self._run(work, lambda _: self._update_buttons())

    def check_for_orphans(self, announce_when_none: bool = False) -> None:
        """Find and offer to kill servers left behind by a previous session.

        These hold the world lock and the port, so the next start fails in a way
        that looks like the world is broken. They are also easy to miss in Task
        Manager, where they are just another java.exe.
        """
        running_pid = None
        if self.server and self.server.is_alive and self.server._process:
            running_pid = self.server._process.pid

        orphans = runner.find_orphan_servers(exclude_pid=running_pid)
        if not orphans:
            if announce_when_none:
                self.console.append_notice("No leftover server processes found.")
            return

        listing = "\n".join(
            f"  {o.world}  -  running {int(o.uptime // 60)} min  (pid {o.pid})"
            for o in orphans
        )
        answer = QMessageBox.question(
            self,
            "Leftover server found",
            f"{len(orphans)} Minecraft server started by Arkon Launcher is still "
            f"running with nothing attached to it:\n\n{listing}\n\n"
            f"It holds the world open, so starting that world again will fail "
            f"until it is gone.\n\nStop it now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        def work(report):
            results = []
            for orphan in orphans:
                report(f"Stopping leftover server {orphan.pid}...")
                results.append((orphan, runner.kill_process(orphan.pid)))
            return results

        def done(results):
            for orphan, killed in results:
                self.console.append_notice(
                    f"Stopped leftover server for '{orphan.world}' (pid {orphan.pid})."
                    if killed
                    else f"Could not stop pid {orphan.pid} - it may need Task Manager.",
                    "#9ae6b4" if killed else "#ffa94d",
                )
            # The world lock is free again, so the list is now wrong.
            self.load_worlds()
            self._update_buttons()

        self._run(work, done)

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

    # --- Live stats ---

    def _poll_stats(self) -> None:
        """Refresh the stats panel. Runs once a second while the server is up."""
        if self._shutting_down:
            return
        running = bool(self.server and self.server.is_alive)
        self.left_stack.setCurrentIndex(1 if running else 0)
        if not running:
            return

        self.stats_panel.set_uptime(self.server.uptime)
        self.stats_panel.set_players(
            len(self.server.players), int(self._server_properties().get("max-players", 0)) or None
        )
        self.stats_panel.set_resources(
            self._process_sampler.sample(), self.memory_spin.value()
        )

        status = self.connection.status
        self.stats_panel.set_address(
            status.friend_address() if status.lan_addresses else "",
            status.verified if status.lan_addresses else None,
        )

        # Tick rate is a command round-trip, so it is polled far less often than
        # the cheap local numbers.
        self._tps_countdown -= 1
        if self._tps_countdown <= 0:
            self._tps_countdown = 5
            self._measure_tps()

    def _server_properties(self) -> dict:
        path = self._properties_path()
        return provision.read_properties(path) if path else {}

    def _server_paused(self, tps: float | None) -> bool:
        """Whether a zero tick rate is Minecraft's empty-server pause.

        ``pause-when-empty-seconds`` stops the tick loop once the server has
        been empty for that long, so an idle server genuinely reports zero. It
        is only a pause if nobody is on and the setting is actually enabled.
        """
        if tps is None or tps > 0.5:
            return False
        if self.server and self.server.players:
            return False
        return self._pause_when_empty_seconds() > 0

    def _pause_when_empty_seconds(self) -> int:
        world = self.selected_world()
        if not (self.instance and world):
            return 0
        path = (
            paths.server_dir(self.instance.directory, world.folder_name)
            / "server.properties"
        )
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("pause-when-empty-seconds="):
                    return int(line.split("=", 1)[1].strip() or 0)
        except (OSError, ValueError):
            return 0
        return 0

    def _measure_tps(self) -> None:
        """Ask the pack's /tps command, or work it out from the tick counter."""
        if not (self.server and self.server.is_alive) or self._tps_pending:
            return
        self._tps_pending = True
        server = self.server
        use_command = self._tps_command_works is not False

        def work(report):
            if use_command:
                # Generous window: commands run on the server thread, so one
                # coinciding with an autosave can miss a short one. Giving up
                # after a single miss used to disable /tps - and with it MSPT,
                # which has no other source - for the rest of the session.
                reply = server.query("tps", settle=0.4, timeout=10)
                tps, mspt = serverstats.parse_tps(reply)
                if tps is not None:
                    return tps, mspt, True
            # No /tps in this pack - measure it from the game clock instead.
            ticks = serverstats.parse_gametime(
                server.query("time query gametime", settle=0.4, timeout=6)
            )
            return None, None, False, ticks

        def done(result):
            self._tps_pending = False
            if len(result) == 3:
                tps, mspt, worked = result
                self._tps_command_works = worked
                self._tps_misses = 0
                self.stats_panel.set_tps(tps, mspt, self._server_paused(tps))
                return

            _, _, worked, ticks = result
            # Only conclude the pack has no /tps after it has failed repeatedly.
            # A single miss is far more likely to be a busy server thread.
            self._tps_misses = getattr(self, "_tps_misses", 0) + 1
            if self._tps_misses >= 3:
                self._tps_command_works = worked

            measured = self._tick_sampler.sample(ticks)
            if measured is not None:
                self.stats_panel.set_tps(measured, None, self._server_paused(measured))

        def failed(_):
            self._tps_pending = False

        worker = Worker(work, self)
        self._workers.append(worker)
        worker.finished_ok.connect(done)
        worker.failed.connect(failed)
        worker.finished.connect(
            lambda: self._workers.remove(worker) if worker in self._workers else None
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

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
        """Shut down without letting a dying window take the app with it.

        The order matters. The reader thread keeps running until the process
        actually exits, so if its callbacks are still attached while the window
        is being destroyed they will fire into a half-deleted widget - and a
        state change during shutdown used to open a modal crash-triage dialog on
        top of that. Detaching first makes the rest safe.
        """
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

            self._shutting_down = True
            self._quiesce()

            if answer == QMessageBox.Yes:
                self._set_status("Stopping server...")
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    self.server.stop()
                finally:
                    QApplication.restoreOverrideCursor()
            else:
                # Leaving it running is a choice, but it must not be a silent
                # one - an invisible java process holding the world lock is
                # exactly what stops the next launch working.
                self._remember_orphan()

        self._shutting_down = True
        self._quiesce()
        self.connection.release()
        event.accept()

    def _quiesce(self) -> None:
        """Detach everything that could call back into a closing window."""
        if self.server is not None:
            self.server.detach()
        for timer in (
            getattr(self, "_uptime_timer", None),
            getattr(self, "_backup_timer", None),
            getattr(self, "_restart_timer", None),
            getattr(self, "_scan_timer", None),
        ):
            if timer is not None:
                timer.stop()
        for timer in getattr(self, "_announce_timers", []) + getattr(
            self, "_restart_announce_timers", []
        ):
            timer.stop()

    def _remember_orphan(self) -> None:
        self.console.append_notice(
            "The server is still running in the background. Reopen Arkon Launcher "
            "to take control of it again, or stop it from there."
        )
