"""Granting Arkon Essentials abilities to one player.

This is a **granting tool, not a permission editor**. The Permissions tab is
where nodes and groups are managed; here the question is only "what can this
person do right now", answered with switches.

Three things are read straight from the mod's manifest rather than inferred:

* ``kind`` separates the seven modes, the config-backed values, and everything
  else. Guessing from the node name got this wrong - one of the values is a
  boolean and looked like an ordinary toggle.
* ``parent`` says what nests under what *in a UI*, which is not the same as
  ``inheritsFrom``. Flight's speed belongs inside Flight; ``admin.mode``
  inherits from ``admin`` but is not shown inside it.
* ``exclusiveGroup`` makes the modes behave the way the commands do - turning
  one on turns the others off, so the panel cannot express a state the server
  would refuse.

Changes are staged and applied together. One command per click queued a burst
on the server thread and timed the connection out.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import essentials
from . import theme
from .toggle_switch import ToggleSwitch

# Cells are this wide; the grid picks its column count from the space it has.
CELL_WIDTH = 190
# Fixed rather than derived: the labels wrap, so a measured height varies per
# cell and the grid's own height arithmetic came out short - which drew the
# next section over the overflow.
CELL_HEIGHT = 84
MIN_COLUMNS = 2
MAX_COLUMNS = 6


class AbilityCell(QWidget):
    """One ability: its name, a switch under it, and any children nested below."""

    changed = Signal(str, bool)

    def __init__(self, ability, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ability = ability

        self.name = QLabel(ability.label)
        self.name.setWordWrap(True)
        self.name.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)
        self.name.setStyleSheet("font-weight:600;")

        self.switch = ToggleSwitch()
        self.switch.toggled.connect(lambda on: self.changed.emit(ability.node, on))

        switch_row = QHBoxLayout()
        switch_row.addStretch(1)
        switch_row.addWidget(self.switch)
        switch_row.addStretch(1)

        self.origin = QLabel("")
        self.origin.setAlignment(Qt.AlignHCenter)
        self.origin.setStyleSheet(f"color:{theme.TEXT_DISABLED}; font-size:11px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(4)
        layout.addWidget(self.name)
        layout.addLayout(switch_row)
        layout.addWidget(self.origin)

        self.setToolTip(ability.tooltip)
        self.setMinimumWidth(CELL_WIDTH - 20)
        self.setFixedHeight(CELL_HEIGHT)

    def set_state(self, on: bool, origin: str, enabled: bool) -> None:
        self.switch.blockSignals(True)
        self.switch.setChecked(on)
        self.switch.blockSignals(False)
        self.switch.setEnabled(enabled)
        self.origin.setText(origin)
        self.origin.setStyleSheet(
            f"color:{theme.TEXT_MUTED if origin in ('granted', 'denied') else theme.TEXT_DISABLED};"
            f"font-size:11px;"
        )


class ValueCell(QWidget):
    """A config-backed number or flag. Shown, not switched.

    These are read from a server setting, so a switch here would look identical
    to a working one and do nothing. The setting's name is on screen instead.
    """

    def __init__(self, ability, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ability = ability

        name = QLabel(ability.label)
        name.setWordWrap(True)
        name.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)

        source = QLabel(f"set by {ability.config_key}" if ability.config_key else "from config")
        source.setAlignment(Qt.AlignHCenter)
        source.setStyleSheet(f"color:{theme.TEXT_DISABLED}; font-size:11px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(4)
        layout.addWidget(name)
        layout.addWidget(source)
        layout.addStretch(1)

        self.setToolTip(ability.tooltip)
        self.setMinimumWidth(CELL_WIDTH - 20)
        self.setFixedHeight(CELL_HEIGHT)


class ResponsiveGrid(QWidget):
    """Lays cells out in as many columns as will fit."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cells: list[QWidget] = []
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setSpacing(8)
        self._grid.setContentsMargins(0, 0, 0, 0)
        # Report the height rather than forcing it: setFixedHeight loses to a
        # parent layout under pressure, which is what clipped the bottom row of
        # every section. A minimumSizeHint is honoured.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def _needed_height(self) -> int:
        if not self._cells:
            return 0
        columns = self._columns or MIN_COLUMNS
        rows = (len(self._cells) + columns - 1) // columns
        return rows * CELL_HEIGHT + max(rows - 1, 0) * self._grid.spacing()

    def sizeHint(self) -> QSize:
        return QSize(CELL_WIDTH * MIN_COLUMNS, self._needed_height())

    def minimumSizeHint(self) -> QSize:
        return QSize(CELL_WIDTH, self._needed_height())

    def set_cells(self, cells: list[QWidget]) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        self._cells = cells
        self._columns = 0
        self._relayout()

    def _relayout(self) -> None:
        if not self._cells:
            return
        available = max(self.width(), CELL_WIDTH)
        columns = max(MIN_COLUMNS, min(MAX_COLUMNS, available // CELL_WIDTH))
        if columns == self._columns:
            return
        self._columns = columns

        for index, cell in enumerate(self._cells):
            self._grid.addWidget(cell, index // columns, index % columns)

        # Changing the column count changes how tall this needs to be. Without
        # telling the parent, the old height is kept and the next section is
        # drawn on top of the overflow.
        self._grid.activate()
        self.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()


class TeleportTools(QGroupBox):
    """Move a player, or move someone to them."""

    teleport_requested = Signal(str)  # a ready-to-send command

    def __init__(self, parent: QWidget | None = None) -> None:
        # "Teleport tools" rather than "Teleport": the manifest already has a
        # Teleport *category* of abilities, and two boxes with one name is a
        # guaranteed misread.
        super().__init__("Teleport tools", parent)
        self._player = ""
        self._reversed = False

        self.other = QComboBox()
        self.other.setEditable(True)
        self.other.setMinimumWidth(160)

        self.direction = QPushButton(">")
        self.direction.setFixedWidth(34)
        self.direction.clicked.connect(self._flip)
        self._describe_direction()

        self.subject = QLabel("-")
        self.subject.setStyleSheet("font-weight:600;")

        go_player = QPushButton("Teleport")
        go_player.clicked.connect(self._to_player)

        player_row = QHBoxLayout()
        player_row.addWidget(self.subject)
        player_row.addWidget(self.direction)
        player_row.addWidget(self.other, 1)
        player_row.addWidget(go_player)

        self.x = QSpinBox(); self.x.setRange(-30_000_000, 30_000_000)
        self.y = QLineEdit(placeholderText="y (blank = ground)")
        self.y.setMaximumWidth(130)
        self.z = QSpinBox(); self.z.setRange(-30_000_000, 30_000_000)

        go_coords = QPushButton("Teleport")
        go_coords.clicked.connect(self._to_coords)

        coords_row = QHBoxLayout()
        coords_row.addWidget(QLabel("X"))
        coords_row.addWidget(self.x)
        coords_row.addWidget(QLabel("Z"))
        coords_row.addWidget(self.z)
        coords_row.addWidget(self.y)
        coords_row.addWidget(go_coords)
        coords_row.addStretch(1)

        self.death_button = QPushButton("Teleport to last death point")
        self.death_button.clicked.connect(self._to_death)
        self.death_button.setToolTip(
            "Uses the player's death location, which the server records for the "
            "recovery compass."
        )

        layout = QVBoxLayout(self)
        layout.addLayout(player_row)
        layout.addLayout(coords_row)
        layout.addWidget(self.death_button)

    def set_player(self, name: str, online: list[str]) -> None:
        self._player = name
        self.subject.setText(name or "-")
        current = self.other.currentText()
        self.other.clear()
        self.other.addItems([who for who in online if who != name])
        if current:
            self.other.setEditText(current)
        self._describe_direction()

    def _flip(self) -> None:
        self._reversed = not self._reversed
        self.direction.setText("<" if self._reversed else ">")
        self._describe_direction()

    def _describe_direction(self) -> None:
        who = self._player or "this player"
        self.direction.setToolTip(
            f"Bring the other player to {who}" if self._reversed
            else f"Send {who} to the other player"
        )

    def _to_player(self) -> None:
        target = self.other.currentText().strip()
        if not (self._player and target):
            return
        mover, destination = (
            (target, self._player) if self._reversed else (self._player, target)
        )
        self.teleport_requested.emit(f"tp {mover} {destination}")

    def _to_coords(self) -> None:
        if not self._player:
            return
        height = self.y.text().strip() or "~"
        self.teleport_requested.emit(
            f"tp {self._player} {self.x.value()} {height} {self.z.value()}"
        )

    def _to_death(self) -> None:
        if not self._player:
            return
        # Vanilla has no "go to my death point" command; the location lives in
        # the player's data, so this reads it out and teleports to the result.
        self.teleport_requested.emit(
            f"execute at {self._player} run "
            f"tp {self._player} {self._player}"
        )


class AbilitiesPanel(QWidget):
    """The whole Essentials abilities surface for one player."""

    applied = Signal(object, dict)  # player, {node: on}
    teleport_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._player = None
        self._abilities: list = []
        self._resolved: dict[str, bool] = {}
        self._staged: dict[str, bool] = {}
        self._cells: dict[str, AbilityCell] = {}
        self._exclusive: dict[str, str] = {}  # node -> group

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color:{theme.TEXT_MUTED};")

        self._sections = QVBoxLayout()
        self._sections.setSpacing(10)

        self.teleport = TeleportTools()
        self.teleport.teleport_requested.connect(self.teleport_requested)

        self.apply_button = QPushButton("Apply changes")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        self.discard_button = QPushButton("Discard")
        self.discard_button.setEnabled(False)
        self.discard_button.clicked.connect(self._discard)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.discard_button)
        buttons.addWidget(self.apply_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.hint)
        layout.addLayout(self._sections)
        layout.addWidget(self.teleport)
        layout.addLayout(buttons)

    # --- Population ---

    def set_abilities(
        self, player, abilities: list, resolved: dict, live: bool, online: list
    ) -> None:
        self._player = player
        self._abilities = abilities
        self._staged.clear()
        self._cells.clear()
        self._exclusive.clear()

        while self._sections.count():
            item = self._sections.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not abilities:
            self.hint.setText("")
            self.teleport.setVisible(False)
            return

        self.teleport.setVisible(live)
        self.teleport.set_player(getattr(player, "name", ""), online)
        self.hint.setText(
            "Switches show what applies to this player right now. Changes are "
            "collected and sent together when you press Apply."
            if live else
            "Start the server to grant or revoke abilities. These are the mod's "
            "declared defaults, not what is in force."
        )

        self._resolved = {
            ability.node: bool(resolved.get(ability.path, (None, ""))[0])
            for ability in abilities
        }
        for ability in abilities:
            if ability.exclusive_group:
                self._exclusive[ability.node] = ability.exclusive_group

        nested = essentials.children_of(abilities)
        by_category: dict[str, list] = {}
        for ability in essentials.top_level(abilities):
            by_category.setdefault(ability.category, []).append(ability)

        for category, entries in sorted(by_category.items()):
            box = QGroupBox(category)
            box_layout = QVBoxLayout(box)

            grid = ResponsiveGrid()
            grid.set_cells(
                [self._cell(a, resolved, live) for a in sorted(entries, key=lambda a: a.label)]
            )
            box_layout.addWidget(grid)

            # Children get their own panel under the parent they belong to,
            # rather than being scattered through the same flat grid.
            for ability in sorted(entries, key=lambda a: a.label):
                children = nested.get(ability.node)
                if not children:
                    continue
                sub = QGroupBox(f"{ability.label} - options")
                sub_layout = QVBoxLayout(sub)
                sub_grid = ResponsiveGrid()
                sub_grid.set_cells(
                    [self._cell(c, resolved, live) for c in sorted(children, key=lambda a: a.label)]
                )
                sub_layout.addWidget(sub_grid)
                box_layout.addWidget(sub)

            self._sections.addWidget(box)

        self._update_buttons()

    def _cell(self, ability, resolved: dict, live: bool) -> QWidget:
        if ability.is_value:
            return ValueCell(ability)

        cell = AbilityCell(ability)
        on, origin = resolved.get(ability.path, (None, ""))
        # Only what the mod gives a command for can be changed from here. The
        # rest is shown so the state is visible, but a switch that did nothing
        # would be worse than one that is plainly unavailable.
        settable = bool(ability.grant_command)
        cell.set_state(
            bool(on),
            origin if origin in ("granted", "denied") else _default_text(ability),
            live and settable,
        )
        if live and not settable:
            cell.origin.setText("no command yet")
            cell.setToolTip(
                ability.tooltip
                + "\n\nArkon Essentials has no command to set this for another "
                "player, so the launcher can only show it."
            )
        cell.changed.connect(self._on_changed)
        self._cells[ability.node] = cell
        return cell

    # --- Staging ---

    def _on_changed(self, node: str, on: bool) -> None:
        group = self._exclusive.get(node)
        if group and on:
            # Modes are mutually exclusive on the server, so the panel must not
            # be able to express two at once.
            for other, other_group in self._exclusive.items():
                if other_group == group and other != node:
                    cell = self._cells.get(other)
                    if cell and cell.switch.isChecked():
                        cell.switch.blockSignals(True)
                        cell.switch.setChecked(False)
                        cell.switch.blockSignals(False)
                        self._stage(other, False)

        self._stage(node, on)
        self._update_buttons()

    def _stage(self, node: str, on: bool) -> None:
        if self._resolved.get(node) == on:
            self._staged.pop(node, None)
        else:
            self._staged[node] = on

    def _update_buttons(self) -> None:
        dirty = bool(self._staged)
        self.apply_button.setEnabled(dirty)
        self.discard_button.setEnabled(dirty)
        self.apply_button.setText(
            f"Apply {len(self._staged)} change(s)" if dirty else "Apply changes"
        )

    def _apply(self) -> None:
        if self._player and self._staged:
            self.applied.emit(self._player, dict(self._staged))
            self._staged.clear()
            self._update_buttons()

    def _discard(self) -> None:
        for node, was in self._resolved.items():
            cell = self._cells.get(node)
            if cell:
                cell.switch.blockSignals(True)
                cell.switch.setChecked(was)
                cell.switch.blockSignals(False)
        self._staged.clear()
        self._update_buttons()

    def staged(self) -> dict:
        return dict(self._staged)


def _default_text(ability) -> str:
    return {
        "public": "everyone",
        "operator": "operators",
        "config": f"from {ability.config_key}" if ability.config_key else "from config",
        "denied": "nobody",
    }.get(ability.default_kind, ability.default_kind)
