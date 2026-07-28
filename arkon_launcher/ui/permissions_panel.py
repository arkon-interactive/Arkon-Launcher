"""LuckPerms editor: groups, permissions, inheritance and promotion tracks.

The permission editor is a two-box layout - known nodes on the left, the group's
own nodes on the right - and supports both dragging between the boxes and the
arrow buttons, because neither alone suits everyone.

Two things it is careful about:

* **Inherited permissions are shown, but marked.** LuckPerms reports where a
  permission resolves from, so anything coming from a parent group is greyed out
  and labelled with its origin rather than being presented as the group's own.
  Removing it has to happen on the parent, and the UI says so.
* **Nothing is moved locally on drop.** The lists are re-read from the server
  after every change, so what you see is what the server believes rather than
  what the UI hoped would happen.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..luckperms import Group, Permission
from ..permissionnodes import Node, NodeCatalogue, Source, owners_for

HINT = "color:#8b949e;"
INHERITED_COLOUR = QColor("#7f8c9b")
ALLOW_COLOUR = QColor("#5fb37a")
DENY_COLOUR = QColor("#e06c75")

# Short on purpose: these sit after the node name, and node names are already
# long enough to need scrolling.
SOURCE_LABELS = {
    Source.RECORDED: "seen",
    Source.ASSIGNED: "used",
    Source.LUCKPERMS: "lp",
    Source.COMMAND: "cmd",
    Source.MANUAL: "common",
}


class NodeList(QListWidget):
    """A list of permission nodes that accepts drops from its counterpart."""

    nodes_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        source = event.source()
        if source is self or not isinstance(source, NodeList):
            event.ignore()
            return

        # Inherited entries are cleared of ItemIsDragEnabled, so a mixed
        # selection drags only the nodes the group actually owns.
        nodes = [
            item.data(Qt.UserRole)
            for item in source.selectedItems()
            if item.data(Qt.UserRole) and item.flags() & Qt.ItemIsDragEnabled
        ]
        # Deliberately not calling super(): the lists are rebuilt from the
        # server's reply, so moving items here would briefly show a state the
        # server has not agreed to yet.
        event.acceptProposedAction()
        if nodes:
            self.nodes_dropped.emit(nodes)

    def selected_nodes(self) -> list[str]:
        return [
            item.data(Qt.UserRole)
            for item in self.selectedItems()
            if item.data(Qt.UserRole)
        ]


class GroupsTab(QWidget):
    """Groups, their inheritance, and the permission editor."""

    group_selected = Signal(str)
    create_group = Signal(str)
    delete_group = Signal(str)
    set_weight = Signal(str, int)
    assign_nodes = Signal(str, list, bool)  # group, nodes, allow
    unassign_nodes = Signal(str, list)
    add_parent = Signal(str, str)
    remove_parent = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._catalogue = NodeCatalogue()
        self._mod_ids: dict[str, str] = {}
        self._assigned: list[Permission] = []
        self._inherited: dict[str, tuple[bool, str]] = {}

        # --- Groups column ---
        self.groups = QListWidget()
        self.groups.itemSelectionChanged.connect(self._on_group_selected)

        new_button = QPushButton("New...")
        new_button.clicked.connect(self._create)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._delete)
        self.weight_button = QPushButton("Weight...")
        self.weight_button.setToolTip(
            "When two inherited groups disagree about a permission, the group with "
            "the higher weight wins."
        )
        self.weight_button.clicked.connect(self._weight)

        group_buttons = QHBoxLayout()
        group_buttons.addWidget(new_button)
        group_buttons.addWidget(self.delete_button)
        group_buttons.addWidget(self.weight_button)

        self.parents = QListWidget()
        self.parents.setMaximumHeight(90)
        self.parent_picker = QComboBox()
        add_parent_button = QPushButton("Inherit")
        add_parent_button.setToolTip(
            "This group gains every permission of the chosen group."
        )
        add_parent_button.clicked.connect(self._add_parent)
        remove_parent_button = QPushButton("Stop")
        remove_parent_button.clicked.connect(self._remove_parent)

        parent_buttons = QHBoxLayout()
        parent_buttons.addWidget(self.parent_picker, 1)
        parent_buttons.addWidget(add_parent_button)
        parent_buttons.addWidget(remove_parent_button)

        groups_box = QGroupBox("Groups")
        groups_layout = QVBoxLayout(groups_box)
        groups_layout.addWidget(self.groups, 1)
        groups_layout.addLayout(group_buttons)
        groups_layout.addWidget(QLabel("Inherits from:"))
        groups_layout.addWidget(self.parents)
        groups_layout.addLayout(parent_buttons)

        # --- Available nodes ---
        self.mod_filter = QComboBox()
        self.mod_filter.setToolTip(
            "Narrow the list to permissions belonging to one mod. Attribution is a "
            "best guess from the node name, so a mod that adds a command under an "
            "unrelated name may appear under Minecraft until you record its nodes."
        )
        self.mod_filter.currentIndexChanged.connect(self._refresh_available)

        self.search = QLineEdit(placeholderText="Search permissions...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refresh_available)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Mod:"))
        filter_row.addWidget(self.mod_filter, 1)

        self.available = NodeList()
        self.available.nodes_dropped.connect(self._on_dropped_to_available)
        self.available.itemDoubleClicked.connect(lambda _: self._allow())

        self.custom_node = QLineEdit(
            placeholderText="Or type any permission node your mods use"
        )
        self.custom_node.returnPressed.connect(self._add_custom)
        add_custom = QPushButton("Add")
        add_custom.clicked.connect(self._add_custom)
        custom_row = QHBoxLayout()
        custom_row.addWidget(self.custom_node, 1)
        custom_row.addWidget(add_custom)

        available_box = QGroupBox("Known permissions")
        available_layout = QVBoxLayout(available_box)
        available_layout.addLayout(filter_row)
        available_layout.addWidget(self.search)
        available_layout.addWidget(self.available, 1)
        available_layout.addLayout(custom_row)

        # --- Arrows ---
        self.allow_button = QPushButton("Allow  >")
        self.allow_button.clicked.connect(self._allow)
        self.deny_button = QPushButton("Deny  >")
        self.deny_button.clicked.connect(self._deny)
        self.remove_button = QPushButton("<  Remove")
        self.remove_button.clicked.connect(self._remove_nodes)

        arrows = QVBoxLayout()
        arrows.addStretch(1)
        arrows.addWidget(self.allow_button)
        arrows.addWidget(self.deny_button)
        arrows.addSpacing(14)
        arrows.addWidget(self.remove_button)
        arrows.addStretch(1)
        arrow_host = QWidget()
        arrow_host.setLayout(arrows)
        arrow_host.setFixedWidth(120)

        # --- Assigned nodes ---
        self.assigned = NodeList()
        self.assigned.nodes_dropped.connect(self._on_dropped_to_assigned)
        self.assigned.itemDoubleClicked.connect(lambda _: self._remove_nodes())
        self.assigned.itemSelectionChanged.connect(self._update_buttons)

        self.assigned_hint = QLabel("")
        self.assigned_hint.setStyleSheet(HINT)
        self.assigned_hint.setWordWrap(True)

        assigned_box = QGroupBox("Granted to this group")
        assigned_layout = QVBoxLayout(assigned_box)
        assigned_layout.addWidget(self.assigned, 1)
        assigned_layout.addWidget(self.assigned_hint)

        # A splitter rather than fixed proportions: node names are long and vary
        # by pack, so the useful split between the two boxes is the user's call.
        editor_split = QSplitter(Qt.Horizontal)
        editor_split.addWidget(available_box)
        editor_split.addWidget(arrow_host)
        editor_split.addWidget(assigned_box)
        editor_split.setStretchFactor(0, 1)
        editor_split.setStretchFactor(1, 0)
        editor_split.setStretchFactor(2, 1)
        editor_split.setSizes([380, 120, 380])
        editor_split.setChildrenCollapsible(False)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(groups_box)
        splitter.addWidget(editor_split)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 1000])
        splitter.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self._update_buttons()

    # --- Population ---

    def set_groups(self, groups: list[Group]) -> None:
        previous = self.selected_group()
        self.groups.blockSignals(True)
        self.groups.clear()
        for group in groups:
            item = QListWidgetItem(group.label)
            item.setData(Qt.UserRole, group.name)
            self.groups.addItem(item)
        self.groups.blockSignals(False)

        self.parent_picker.clear()
        self.parent_picker.addItems([g.name for g in groups])

        for index in range(self.groups.count()):
            if self.groups.item(index).data(Qt.UserRole) == previous:
                self.groups.setCurrentRow(index)
                break
        else:
            if self.groups.count():
                self.groups.setCurrentRow(0)
        self._update_buttons()

    def set_catalogue(
        self, catalogue: NodeCatalogue, mod_ids: dict[str, str] | None = None
    ) -> None:
        self._catalogue = catalogue
        self._mod_ids = mod_ids or {}

        previous = self.mod_filter.currentData()
        self.mod_filter.blockSignals(True)
        self.mod_filter.clear()
        self.mod_filter.addItem(f"All permissions ({len(catalogue.nodes)})", "")
        for owner in owners_for(catalogue.nodes, self._mod_ids):
            self.mod_filter.addItem(f"{owner.label} ({owner.count})", owner.key)
        index = self.mod_filter.findData(previous) if previous else 0
        self.mod_filter.setCurrentIndex(max(0, index))
        self.mod_filter.blockSignals(False)

        self._refresh_available()

    def set_parents(self, parents: list[str]) -> None:
        self.parents.clear()
        for parent in parents:
            item = QListWidgetItem(parent)
            item.setData(Qt.UserRole, parent)
            self.parents.addItem(item)
        if not parents:
            placeholder = QListWidgetItem("(inherits nothing)")
            placeholder.setFlags(Qt.NoItemFlags)
            self.parents.addItem(placeholder)

    def set_permissions(
        self,
        assigned: list[Permission],
        inherited: dict[str, tuple[bool, str]] | None = None,
    ) -> None:
        self._assigned = assigned
        self._inherited = inherited or {}
        self._refresh_assigned()
        self._refresh_available()

    def _refresh_assigned(self) -> None:
        self.assigned.clear()

        for permission in self._assigned:
            item = QListWidgetItem(
                f"{'ALLOW' if permission.value else 'DENY '}   {permission.node}"
            )
            item.setData(Qt.UserRole, permission.node)
            item.setForeground(QBrush(ALLOW_COLOUR if permission.value else DENY_COLOUR))
            self.assigned.addItem(item)

        own = {p.node for p in self._assigned}
        for node, (value, origin) in sorted(self._inherited.items()):
            if node in own:
                continue
            item = QListWidgetItem(
                f"{'allow' if value else 'deny '}   {node}      (from {origin})"
            )
            item.setData(Qt.UserRole, node)
            item.setForeground(QBrush(INHERITED_COLOUR))
            font = QFont()
            font.setItalic(True)
            item.setFont(font)
            # Inherited permissions belong to the parent, so they are shown for
            # context but cannot be dragged or removed from here.
            item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.assigned.addItem(item)

        if not self._assigned and not self._inherited:
            placeholder = QListWidgetItem("No permissions yet.")
            placeholder.setFlags(Qt.NoItemFlags)
            self.assigned.addItem(placeholder)

        self.assigned_hint.setText(
            "Greyed entries are inherited from a parent group - edit them there."
            if self._inherited
            else ""
        )

    def _refresh_available(self) -> None:
        assigned = {p.node for p in self._assigned}
        self.available.clear()
        owner = self.mod_filter.currentData() or ""
        for node in self._catalogue.search(self.search.text(), owner, self._mod_ids):
            if node.node in assigned:
                continue
            source = SOURCE_LABELS.get(node.source, "")
            suffix = f"      [{source}]" if source else ""
            item = QListWidgetItem(f"{node.node}{suffix}")
            item.setData(Qt.UserRole, node.node)
            if node.description:
                item.setToolTip(node.description)
            self.available.addItem(item)

    # --- Selection ---

    def selected_group(self) -> str | None:
        item = self.groups.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _on_group_selected(self) -> None:
        group = self.selected_group()
        self._update_buttons()
        if group:
            self.group_selected.emit(group)

    def _update_buttons(self) -> None:
        group = self.selected_group()
        has_group = group is not None
        self.delete_button.setEnabled(has_group and group != "default")
        self.weight_button.setEnabled(has_group)
        self.allow_button.setEnabled(has_group)
        self.deny_button.setEnabled(has_group)

        removable = [
            item
            for item in self.assigned.selectedItems()
            if item.flags() & Qt.ItemIsDragEnabled
        ]
        self.remove_button.setEnabled(has_group and bool(removable))

    # --- Actions ---

    def _create(self) -> None:
        name, ok = QInputDialog.getText(self, "New group", "Group name:")
        name = name.strip().lower().replace(" ", "_")
        if ok and name:
            self.create_group.emit(name)

    def _delete(self) -> None:
        group = self.selected_group()
        if not group:
            return
        answer = QMessageBox.question(
            self,
            "Delete group",
            f"Delete '{group}'? Players in it lose the permissions it granted.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.delete_group.emit(group)

    def _weight(self) -> None:
        group = self.selected_group()
        if not group:
            return
        weight, ok = QInputDialog.getInt(
            self,
            "Group weight",
            "Higher weight wins when inherited groups disagree:",
            0,
            -1000,
            1000,
        )
        if ok:
            self.set_weight.emit(group, weight)

    def _nodes_to_assign(self) -> list[str]:
        nodes = self.available.selected_nodes()
        if not nodes:
            typed = self.custom_node.text().strip()
            if typed:
                nodes = [typed]
        return nodes

    def _confirm_wildcard(self, nodes: list[str], group: str) -> bool:
        if "*" not in nodes:
            return True
        return (
            QMessageBox.question(
                self,
                "Grant everything?",
                f"The '*' node gives '{group}' every permission on the server, "
                f"including full operator powers.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.Yes
        )

    def _assign(self, allow: bool) -> None:
        group = self.selected_group()
        nodes = self._nodes_to_assign()
        if not group or not nodes or not self._confirm_wildcard(nodes, group):
            return
        self.custom_node.clear()
        self.assign_nodes.emit(group, nodes, allow)

    def _allow(self) -> None:
        self._assign(True)

    def _deny(self) -> None:
        self._assign(False)

    def _add_custom(self) -> None:
        if self.custom_node.text().strip():
            self._assign(True)

    def _remove_nodes(self) -> None:
        group = self.selected_group()
        nodes = [
            item.data(Qt.UserRole)
            for item in self.assigned.selectedItems()
            if item.flags() & Qt.ItemIsDragEnabled
        ]
        if group and nodes:
            self.unassign_nodes.emit(group, nodes)

    def _on_dropped_to_assigned(self, nodes: list[str]) -> None:
        group = self.selected_group()
        if group and self._confirm_wildcard(nodes, group):
            self.assign_nodes.emit(group, nodes, True)

    def _on_dropped_to_available(self, nodes: list[str]) -> None:
        group = self.selected_group()
        if group and nodes:
            self.unassign_nodes.emit(group, nodes)

    def _add_parent(self) -> None:
        group, parent = self.selected_group(), self.parent_picker.currentText()
        if not group or not parent:
            return
        if group == parent:
            QMessageBox.information(
                self, "Not possible", "A group cannot inherit from itself."
            )
            return
        self.add_parent.emit(group, parent)

    def _remove_parent(self) -> None:
        group = self.selected_group()
        item = self.parents.currentItem()
        parent = item.data(Qt.UserRole) if item else self.parent_picker.currentText()
        if group and parent:
            self.remove_parent.emit(group, parent)


class PlayersTab(QWidget):
    """Group membership and promotion for one player."""

    user_selected = Signal(str)
    add_to_group = Signal(str, str)
    remove_from_group = Signal(str, str)
    set_primary = Signal(str, str)
    promote = Signal(str, str)
    demote = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.players = QListWidget()
        self.players.itemSelectionChanged.connect(self._on_selected)

        players_box = QGroupBox("Players")
        players_layout = QVBoxLayout(players_box)
        players_layout.addWidget(self.players)

        self.summary = QLabel("Select a player.")
        self.summary.setWordWrap(True)
        self.member_groups = QListWidget()

        self.group_picker = QComboBox()
        add_button = QPushButton("Add to group")
        add_button.clicked.connect(self._add)
        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(self._remove)
        primary_button = QPushButton("Make primary")
        primary_button.clicked.connect(self._primary)

        group_row = QHBoxLayout()
        group_row.addWidget(self.group_picker, 1)
        group_row.addWidget(add_button)
        group_row.addWidget(remove_button)
        group_row.addWidget(primary_button)

        self.track_picker = QComboBox()
        promote_button = QPushButton("Promote")
        promote_button.clicked.connect(self._promote)
        demote_button = QPushButton("Demote")
        demote_button.clicked.connect(self._demote)

        track_row = QHBoxLayout()
        track_row.addWidget(QLabel("Track:"))
        track_row.addWidget(self.track_picker, 1)
        track_row.addWidget(promote_button)
        track_row.addWidget(demote_button)

        detail_box = QGroupBox("Membership")
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.addWidget(self.summary)
        detail_layout.addWidget(self.member_groups, 1)
        detail_layout.addLayout(group_row)
        detail_layout.addLayout(track_row)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(players_box)
        splitter.addWidget(detail_box)
        splitter.setSizes([260, 640])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

    def set_players(self, names: list[str]) -> None:
        previous = self.selected_player()
        self.players.blockSignals(True)
        self.players.clear()
        self.players.addItems(names)
        self.players.blockSignals(False)
        if previous in names:
            self.players.setCurrentRow(names.index(previous))

    def set_groups(self, groups: list[Group]) -> None:
        current = self.group_picker.currentText()
        self.group_picker.clear()
        self.group_picker.addItems([g.name for g in groups])
        index = self.group_picker.findText(current)
        if index >= 0:
            self.group_picker.setCurrentIndex(index)

    def set_tracks(self, tracks: list[str]) -> None:
        current = self.track_picker.currentText()
        self.track_picker.clear()
        self.track_picker.addItems(tracks)
        index = self.track_picker.findText(current)
        if index >= 0:
            self.track_picker.setCurrentIndex(index)

    def set_user_info(self, info) -> None:
        self.summary.setText(
            f"<b>{info.name}</b> &nbsp; primary group: {info.primary_group or 'unknown'}"
            f"<br><span style='{HINT}'>{info.uuid or ''}</span>"
        )
        self.member_groups.clear()
        for group in info.groups:
            label = f"{group}   (primary)" if group == info.primary_group else group
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, group)
            self.member_groups.addItem(item)

    def selected_player(self) -> str | None:
        item = self.players.currentItem()
        return item.text() if item else None

    def _selected_group(self) -> str:
        item = self.member_groups.currentItem()
        return (item.data(Qt.UserRole) if item else None) or self.group_picker.currentText()

    def _on_selected(self) -> None:
        player = self.selected_player()
        if player:
            self.user_selected.emit(player)

    def _add(self) -> None:
        player = self.selected_player()
        if player and self.group_picker.currentText():
            self.add_to_group.emit(player, self.group_picker.currentText())

    def _remove(self) -> None:
        player, group = self.selected_player(), self._selected_group()
        if player and group:
            self.remove_from_group.emit(player, group)

    def _primary(self) -> None:
        player, group = self.selected_player(), self._selected_group()
        if player and group:
            self.set_primary.emit(player, group)

    def _promote(self) -> None:
        player, track = self.selected_player(), self.track_picker.currentText()
        if player and track:
            self.promote.emit(player, track)

    def _demote(self) -> None:
        player, track = self.selected_player(), self.track_picker.currentText()
        if player and track:
            self.demote.emit(player, track)


class TracksTab(QWidget):
    """Promotion ladders: default -> member -> mod -> admin."""

    track_selected = Signal(str)
    create_track = Signal(str)
    delete_track = Signal(str)
    append_group = Signal(str, str)
    remove_group = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.tracks = QListWidget()
        self.tracks.itemSelectionChanged.connect(self._on_selected)

        new_button = QPushButton("New track...")
        new_button.clicked.connect(self._create)
        delete_button = QPushButton("Delete")
        delete_button.clicked.connect(self._delete)

        buttons = QHBoxLayout()
        buttons.addWidget(new_button)
        buttons.addWidget(delete_button)

        tracks_box = QGroupBox("Tracks")
        tracks_layout = QVBoxLayout(tracks_box)
        tracks_layout.addWidget(self.tracks, 1)
        tracks_layout.addLayout(buttons)

        self.path = QListWidget()
        self.group_picker = QComboBox()
        append_button = QPushButton("Add to end")
        append_button.clicked.connect(self._append)
        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(self._remove)

        path_buttons = QHBoxLayout()
        path_buttons.addWidget(self.group_picker, 1)
        path_buttons.addWidget(append_button)
        path_buttons.addWidget(remove_button)

        explanation = QLabel(
            "A track is an ordered ladder of groups. Promoting a player moves them "
            "one step up it, demoting moves them one step down."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet(HINT)

        path_box = QGroupBox("Ladder, lowest first")
        path_layout = QVBoxLayout(path_box)
        path_layout.addWidget(explanation)
        path_layout.addWidget(self.path, 1)
        path_layout.addLayout(path_buttons)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(tracks_box)
        splitter.addWidget(path_box)
        splitter.setSizes([260, 640])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

    def set_tracks(self, tracks: list[str]) -> None:
        previous = self.selected_track()
        self.tracks.blockSignals(True)
        self.tracks.clear()
        self.tracks.addItems(tracks)
        self.tracks.blockSignals(False)
        if previous in tracks:
            self.tracks.setCurrentRow(tracks.index(previous))
        elif tracks:
            self.tracks.setCurrentRow(0)

    def set_groups(self, groups: list[Group]) -> None:
        self.group_picker.clear()
        self.group_picker.addItems([g.name for g in groups])

    def set_path(self, groups: list[str]) -> None:
        self.path.clear()
        for position, group in enumerate(groups, start=1):
            item = QListWidgetItem(f"{position}.   {group}")
            item.setData(Qt.UserRole, group)
            self.path.addItem(item)
        if not groups:
            placeholder = QListWidgetItem("(empty - add groups below)")
            placeholder.setFlags(Qt.NoItemFlags)
            self.path.addItem(placeholder)

    def selected_track(self) -> str | None:
        item = self.tracks.currentItem()
        return item.text() if item else None

    def _on_selected(self) -> None:
        track = self.selected_track()
        if track:
            self.track_selected.emit(track)

    def _create(self) -> None:
        name, ok = QInputDialog.getText(self, "New track", "Track name:")
        name = name.strip().lower().replace(" ", "_")
        if ok and name:
            self.create_track.emit(name)

    def _delete(self) -> None:
        track = self.selected_track()
        if track:
            self.delete_track.emit(track)

    def _append(self) -> None:
        track, group = self.selected_track(), self.group_picker.currentText()
        if track and group:
            self.append_group.emit(track, group)

    def _remove(self) -> None:
        track = self.selected_track()
        item = self.path.currentItem()
        group = item.data(Qt.UserRole) if item else None
        if track and group:
            self.remove_group.emit(track, group)


class PermissionsPanel(QWidget):
    refresh_requested = Signal()
    discover_started = Signal()
    discover_stopped = Signal()
    passive_scan_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.groups_tab = GroupsTab()
        self.users_tab = PlayersTab()
        self.tracks_tab = TracksTab()

        self.tabs = QTabWidget()
        self.tabs.addTab(self.groups_tab, "Groups")
        self.tabs.addTab(self.users_tab, "Players")
        self.tabs.addTab(self.tracks_tab, "Tracks")

        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(HINT)

        self.passive_check = QCheckBox("Keep watching in the background")
        self.passive_check.setToolTip(
            "While the server runs, quietly note which permissions get checked and "
            "add them to this list. Costs almost nothing - an idle server produces "
            "no output at all - and the console is unaffected."
        )
        self.passive_check.toggled.connect(self.passive_scan_toggled.emit)

        self.discover_button = QPushButton("Record permissions...")
        self.discover_button.setToolTip(
            "Watches which permissions the server actually checks, so nodes your "
            "mods use can be discovered. Someone needs to be playing for anything "
            "to be recorded."
        )
        self.discover_button.clicked.connect(self._toggle_discover)
        self._recording = False

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)

        footer = QHBoxLayout()
        footer.addWidget(self.notice, 1)
        footer.addWidget(self.passive_check)
        footer.addWidget(self.discover_button)
        footer.addWidget(self.refresh_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(footer)

    def _toggle_discover(self) -> None:
        self._recording = not self._recording
        self.discover_button.setText(
            "Stop recording" if self._recording else "Record permissions..."
        )
        (self.discover_started if self._recording else self.discover_stopped).emit()

    def set_recording(self, recording: bool) -> None:
        self._recording = recording
        self.discover_button.setText(
            "Stop recording" if recording else "Record permissions..."
        )

    def set_available(self, luckperms_installed: bool, server_running: bool) -> None:
        usable = luckperms_installed and server_running
        self.tabs.setEnabled(usable)
        self.refresh_button.setEnabled(usable)
        self.discover_button.setEnabled(usable)

        if not luckperms_installed:
            self.notice.setText(
                "LuckPerms is not installed in this pack. Add the LuckPerms Fabric "
                "mod to manage permissions here; operators and the whitelist still "
                "work on the Players tab."
            )
        elif not server_running:
            self.notice.setText(
                "Start the server to edit permissions - LuckPerms only answers while "
                "it is running."
            )
        else:
            self.notice.setText("")
