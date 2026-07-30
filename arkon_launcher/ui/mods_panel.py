"""Every mod in the pack, what version it is, and whether the server loads it.

The interesting column is the last one. A modpack has a lot of mods that never
reach the server - client-only ones, older duplicates, things stranded by a
dependency - and until now the only way to find out why a mod was missing was to
read the console at startup. This lists the reason next to the mod.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..modsync import Exclusion

HINT = "color:#8b949e;"

INCLUDED_COLOUR = "#5fb37a"
EXCLUDED_COLOUR = "#8b949e"
# Exclusions worth noticing rather than shrugging at - these mean a mod you
# probably wanted is not running.
NOTABLE = {Exclusion.DEPENDENCY_MISSING, Exclusion.UNREADABLE, Exclusion.SUPERSEDED}


class ModsPanel(QWidget):
    edit_config = Signal(object)  # Path
    refresh_requested = Signal()
    check_updates_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []

        self.search = QLineEdit(placeholderText="Search mods...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)

        self.summary = QLabel("")
        self.summary.setStyleSheet(HINT)

        self.update_status = QLabel("")
        self.update_status.setStyleSheet(HINT)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_requested.emit)
        check_updates = QPushButton("Check for mod updates")
        check_updates.clicked.connect(self.check_updates_requested.emit)

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(check_updates)
        top.addWidget(refresh)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Mod", "Version", "Side", "On the server", "Config"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._on_double_click)

        footer = QHBoxLayout()
        footer.addWidget(self.summary, 1)
        footer.addWidget(self.update_status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(footer)

    # --- Population ---

    def set_mods(self, rows: list[dict]) -> None:
        """rows: mod_id, name, version, environment, included, reason, configs."""
        self._rows = rows
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))

        for index, row in enumerate(rows):
            name = QTableWidgetItem(row["name"])
            name.setToolTip(f"{row['mod_id']}\n{row['file']}")
            self.table.setItem(index, 0, name)

            self.table.setItem(index, 1, QTableWidgetItem(row["version"] or "-"))

            side = {"client": "Client", "server": "Server"}.get(
                row["environment"], "Both"
            )
            self.table.setItem(index, 2, QTableWidgetItem(side))

            status = QTableWidgetItem("Loaded" if row["included"] else row["reason"])
            status.setForeground(
                Qt.green if row["included"] else Qt.gray
            )
            if not row["included"] and row.get("notable"):
                status.setForeground(Qt.yellow)
            if row.get("detail"):
                status.setToolTip(row["detail"])
            self.table.setItem(index, 3, status)

            configs = row["configs"]
            cell = QTableWidgetItem(
                f"{len(configs)} file(s)" if configs else "-"
            )
            if configs:
                cell.setToolTip(
                    "Double-click to edit:\n"
                    + "\n".join(str(p.name) for p in configs[:8])
                )
            self.table.setItem(index, 4, cell)

        self.table.setSortingEnabled(True)
        loaded = sum(1 for r in rows if r["included"])
        self.summary.setText(
            f"{len(rows)} mods installed, {loaded} loaded on the server. "
            f"Double-click a row to edit its config."
        )
        self._apply_filter(self.search.text())

    def set_update_status(self, text: str) -> None:
        self.update_status.setText(text)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for index, row in enumerate(self._rows):
            haystack = f"{row['name']} {row['mod_id']} {row['version']}".lower()
            self.table.setRowHidden(index, bool(needle) and needle not in haystack)

    # --- Actions ---

    def _row_for(self, index: int) -> dict | None:
        item = self.table.item(index, 0)
        if item is None:
            return None
        # Sorting reorders the view, so match on the displayed name rather than
        # trusting the row index to still line up with the source list.
        for row in self._rows:
            if row["name"] == item.text():
                return row
        return None

    def _on_double_click(self, index: int, _column: int) -> None:
        row = self._row_for(index)
        if not row:
            return
        configs = row["configs"]
        if not configs:
            return
        if len(configs) == 1:
            self.edit_config.emit(configs[0])
            return

        menu = QMenu(self)
        menu.addAction(f"{row['name']} config files").setEnabled(False)
        menu.addSeparator()
        for path in configs:
            action = menu.addAction(Path(path).name)
            action.triggered.connect(lambda _=False, p=path: self.edit_config.emit(p))
        menu.exec(self.table.viewport().mapToGlobal(
            self.table.visualItemRect(self.table.item(index, 4)).center()
        ))
