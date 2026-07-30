"""Everything about the pack's mods in one place.

Three jobs that used to be scattered or absent:

* **What is installed and whether the server loads it.** A modpack has plenty of
  mods that never reach the server, and the reason was previously only
  discoverable by reading the startup log.
* **Updates.** CurseForge already records the newest available file for every
  mod it installed, so the whole pack can be checked with no API key.
* **Configs**, edited here rather than in a separate tab, since "which mod does
  this file belong to" is the question you actually start from.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..modsync import Exclusion
from .config_editor import ConfigEditor

HINT = "color:#8b949e;"

# Exclusions worth noticing rather than shrugging at - these mean a mod you
# probably wanted is not running.
NOTABLE = {Exclusion.DEPENDENCY_MISSING, Exclusion.UNREADABLE, Exclusion.SUPERSEDED}

COLUMN_MOD, COLUMN_VERSION, COLUMN_SIDE, COLUMN_SERVER, COLUMN_UPDATE, COLUMN_CONFIG = range(6)


class DuplicateDialog(QDialog):
    """Pick which copy of a duplicated mod to keep."""

    def __init__(self, mod_id: str, jars: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Duplicate: {mod_id}")
        self.resize(560, 320)
        self._jars = jars

        explanation = QLabel(
            f"<b>{mod_id}</b> is installed {len(jars)} times. Fabric will not start "
            f"with two copies of the same mod, so one has to win.<br><br>"
            f"The others are renamed to <code>.jar.disabled</code> rather than "
            f"deleted, so you can undo this by renaming them back."
        )
        explanation.setWordWrap(True)

        self.list = QListWidget()
        for index, jar in enumerate(jars):
            item = QListWidgetItem(f"{jar.version or '?'}      {jar.name}")
            item.setData(Qt.UserRole, jar)
            self.list.addItem(item)
            if index == 0:
                item.setText(item.text() + "      (newest)")
        self.list.setCurrentRow(0)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Keep selected")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(explanation)
        layout.addWidget(QLabel("Keep:"))
        layout.addWidget(self.list, 1)
        layout.addWidget(buttons)

    def keep(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def discard(self) -> list:
        keeping = self.keep()
        return [jar for jar in self._jars if jar is not keeping]


class ModsPanel(QWidget):
    refresh_requested = Signal()
    check_updates_requested = Signal()
    update_one = Signal(object)
    update_all = Signal()
    fix_duplicate = Signal(str, object, list)  # mod_id, keep, discard
    save_config = Signal(object, str, bool)
    toggle_mod = Signal(object, bool)  # row, enable
    install_mod = Signal(str)
    uninstall_mod = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []
        self._duplicates: dict[str, list] = {}
        self._updates: dict[str, object] = {}

        self.search = QLineEdit(placeholderText="Search mods...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)

        self.update_all_button = QPushButton("Update all")
        self.update_all_button.clicked.connect(self.update_all.emit)
        self.update_all_button.setEnabled(False)

        self.duplicates_button = QPushButton("Fix duplicates")
        self.duplicates_button.clicked.connect(self._fix_duplicates)
        self.duplicates_button.setEnabled(False)

        check = QPushButton("Check for updates")
        check.clicked.connect(self.check_updates_requested.emit)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_requested.emit)

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.duplicates_button)
        top.addWidget(self.update_all_button)
        top.addWidget(check)
        top.addWidget(refresh)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Mod", "Version", "Side", "On the server", "Update", "Config"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COLUMN_MOD, QHeaderView.Stretch)
        for column in (COLUMN_VERSION, COLUMN_SIDE, COLUMN_SERVER, COLUMN_UPDATE, COLUMN_CONFIG):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(lambda *_: self._configure_selected())

        self.configure_button = QPushButton("Configure mod")
        self.configure_button.clicked.connect(self._configure_selected)
        self.configure_button.setEnabled(False)

        self.update_one_button = QPushButton("Update this mod")
        self.update_one_button.clicked.connect(self._update_selected)
        self.update_one_button.setEnabled(False)

        self.toggle_button = QPushButton("Disable")
        self.toggle_button.clicked.connect(self._toggle_selected)
        self.toggle_button.setEnabled(False)
        self.toggle_button.setToolTip(
            "Switches the mod off by renaming it to .jar.disabled. Nothing is "
            "deleted, and it can be switched back on here."
        )

        self.uninstall_button = QPushButton("Uninstall")
        self.uninstall_button.clicked.connect(self._uninstall_selected)
        self.uninstall_button.setEnabled(False)

        install = QPushButton("Install mod...")
        install.clicked.connect(self._install)
        install.setToolTip("Add a Fabric mod jar that did not come from CurseForge.")

        self.show_all_configs = QPushButton("Browse all config files")
        self.show_all_configs.clicked.connect(self._show_all_configs)

        row_actions = QHBoxLayout()
        row_actions.addWidget(self.configure_button)
        row_actions.addWidget(self.update_one_button)
        row_actions.addWidget(self.toggle_button)
        row_actions.addWidget(self.uninstall_button)
        row_actions.addSpacing(12)
        row_actions.addWidget(install)
        row_actions.addStretch(1)
        row_actions.addWidget(self.show_all_configs)

        mods_side = QWidget()
        mods_layout = QVBoxLayout(mods_side)
        mods_layout.setContentsMargins(0, 0, 0, 0)
        mods_layout.addLayout(top)
        mods_layout.addWidget(self.table, 1)
        mods_layout.addLayout(row_actions)

        # The config editor lives here now rather than in its own tab: "which
        # mod does this file belong to" is where you actually start.
        self.config_editor = ConfigEditor()
        self.config_editor.save_requested.connect(self.save_config.emit)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(mods_side)
        splitter.addWidget(self.config_editor)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([460, 340])

        self.summary = QLabel("")
        self.summary.setStyleSheet(HINT)
        self.update_status = QLabel("")
        self.update_status.setStyleSheet(HINT)

        footer = QHBoxLayout()
        footer.addWidget(self.summary, 1)
        footer.addWidget(self.update_status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter, 1)
        layout.addLayout(footer)

    # --- Population ---

    def set_mods(self, payload: dict) -> None:
        rows = payload["rows"]
        self._rows = rows
        self._duplicates = payload.get("duplicates") or {}
        self._updates = payload.get("updates") or {}

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))

        for index, row in enumerate(rows):
            # Plain ASCII rather than warning glyphs: mod names end up in the
            # console and in log files, and a cp1252 stream cannot encode them.
            label = row["name"]
            if row.get("is_duplicate"):
                label = f"{label}  (older copy)"
            elif row.get("disabled"):
                label = f"{label}  (off)"

            name = QTableWidgetItem(label)
            name.setToolTip(f"{row['mod_id']}\n{row['file']}")
            # Carry the source index on the item. Matching rows back by name
            # breaks exactly where it matters - a duplicated mod appears twice
            # under the same name, and only one of them has the update.
            name.setData(Qt.UserRole, index)
            if row.get("is_duplicate"):
                name.setForeground(Qt.yellow)
            elif row.get("disabled"):
                name.setForeground(Qt.gray)
            self.table.setItem(index, COLUMN_MOD, name)
            self.table.setItem(index, COLUMN_VERSION, QTableWidgetItem(row["version"] or "-"))

            side = {"client": "Client", "server": "Server"}.get(row["environment"], "Both")
            self.table.setItem(index, COLUMN_SIDE, QTableWidgetItem(side))

            status = QTableWidgetItem("Loaded" if row["included"] else row["reason"])
            status.setForeground(Qt.green if row["included"] else Qt.gray)
            if not row["included"] and row.get("notable"):
                status.setForeground(Qt.yellow)
            if row.get("detail"):
                status.setToolTip(row["detail"])
            self.table.setItem(index, COLUMN_SERVER, status)

            update = row.get("update")
            update_cell = QTableWidgetItem("-")
            if update is not None:
                update_cell = QTableWidgetItem(f"-> {self._version_of(update.latest_file)}")
                update_cell.setForeground(Qt.cyan)
                update_cell.setToolTip(
                    f"{update.installed_file}\n->\n{update.latest_file}"
                )
            self.table.setItem(index, COLUMN_UPDATE, update_cell)

            configs = row["configs"]
            config_cell = QTableWidgetItem(f"{len(configs)} file(s)" if configs else "-")
            if configs:
                config_cell.setToolTip("\n".join(Path(p).name for p in configs[:8]))
            self.table.setItem(index, COLUMN_CONFIG, config_cell)

        self.table.setSortingEnabled(True)

        loaded = sum(1 for r in rows if r["included"])
        duplicate_count = sum(len(v) - 1 for v in self._duplicates.values())
        parts = [f"{len(rows)} mods installed", f"{loaded} loaded on the server"]
        if duplicate_count:
            parts.append(f"{duplicate_count} duplicate(s)")
        self.summary.setText(", ".join(parts) + ".")

        self.duplicates_button.setEnabled(bool(self._duplicates))
        self.duplicates_button.setText(
            f"Fix duplicates ({len(self._duplicates)})" if self._duplicates else "Fix duplicates"
        )

        count = len(self._updates)
        self.update_all_button.setEnabled(count > 0)
        self.update_all_button.setText(f"Update all ({count})" if count else "Update all")
        self.set_update_status(
            f"{count} update(s) available" if count else "All mods up to date"
        )

        self._apply_filter(self.search.text())
        self._update_buttons()

    @staticmethod
    def _version_of(filename: str) -> str:
        """A readable version out of a jar filename, falling back to the name."""
        import re

        match = re.search(r"(\d+(?:\.\d+){1,3})(?=[^\d]*\.jar$)", filename or "")
        return match.group(1) if match else (filename or "?")

    def update_count(self) -> int:
        return len(self._updates)

    def set_update_status(self, text: str) -> None:
        self.update_status.setText(text)

    def set_config_root(self, root) -> None:
        self.config_editor.set_root(root)

    def _apply_filter(self, text: str) -> None:
        """Hide non-matching rows.

        Iterates the *view*, not the source list: the table is sortable, so view
        row N is not source row N, and hiding by source index hides whatever
        happens to be sitting in that position instead.
        """
        needle = text.strip().lower()
        for view_row in range(self.table.rowCount()):
            row = self._row_at(view_row)
            if row is None:
                continue
            haystack = f"{row['name']} {row['mod_id']} {row['version']}".lower()
            self.table.setRowHidden(view_row, bool(needle) and needle not in haystack)

    def _row_at(self, view_row: int) -> dict | None:
        item = self.table.item(view_row, COLUMN_MOD)
        if item is None:
            return None
        index = item.data(Qt.UserRole)
        if isinstance(index, int) and 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    # --- Selection ---

    def _selected_row(self) -> dict | None:
        item = self.table.currentItem()
        if item is None:
            return None
        return self._row_at(item.row())

    def _update_buttons(self) -> None:
        row = self._selected_row()
        self.configure_button.setEnabled(bool(row and row["configs"]))
        self.update_one_button.setEnabled(bool(row and row.get("update")))
        self.uninstall_button.setEnabled(row is not None)

        self.toggle_button.setEnabled(row is not None)
        if row and row.get("disabled"):
            self.toggle_button.setText("Enable")
        elif row and row.get("is_duplicate"):
            # The common thing to do with an older duplicate is switch it off,
            # so say that rather than the generic label.
            self.toggle_button.setText("Disable this copy")
        else:
            self.toggle_button.setText("Disable")

        if row and row["configs"]:
            self.configure_button.setText(f"Configure {row['name'][:22]}")
        else:
            self.configure_button.setText("Configure mod")

    # --- Actions ---

    def _configure_selected(self) -> None:
        row = self._selected_row()
        if not row or not row["configs"]:
            return
        configs = row["configs"]
        if len(configs) == 1:
            self.config_editor.select_file(Path(configs[0]))
            return

        menu = QMenu(self)
        menu.addAction(f"{row['name']} config files").setEnabled(False)
        menu.addSeparator()
        for path in configs:
            action = menu.addAction(Path(path).name)
            action.triggered.connect(
                lambda _=False, p=path: self.config_editor.select_file(Path(p))
            )
        menu.exec(self.configure_button.mapToGlobal(
            self.configure_button.rect().bottomLeft()
        ))

    def _show_all_configs(self) -> None:
        self.config_editor.filter_box.clear()
        self.config_editor.setFocus()

    def _update_selected(self) -> None:
        row = self._selected_row()
        if row and row.get("update"):
            self.update_one.emit(row["update"])

    def _toggle_selected(self) -> None:
        row = self._selected_row()
        if row:
            self.toggle_mod.emit(row, bool(row.get("disabled")))

    def _uninstall_selected(self) -> None:
        row = self._selected_row()
        if row:
            self.uninstall_mod.emit(row)

    def _install(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a Fabric mod jar", "", "Mod jars (*.jar)"
        )
        if chosen:
            self.install_mod.emit(chosen)

    def _fix_duplicates(self) -> None:
        for mod_id, jars in list(self._duplicates.items()):
            dialog = DuplicateDialog(mod_id, jars, self)
            if dialog.exec() != QDialog.Accepted:
                continue
            keep = dialog.keep()
            discard = dialog.discard()
            if keep and discard:
                self.fix_duplicate.emit(mod_id, keep, discard)
