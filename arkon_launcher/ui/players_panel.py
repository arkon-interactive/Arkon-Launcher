"""Everything about one player, in one place.

The list on the left is who exists; the panel on the right is who they are -
status, the three things you can do to them, and what they are allowed to do.

Two deliberate choices:

* **The status bubble means presence and nothing else.** Reusing it to signal a
  half-confirmed action would mean that while confirming, you cannot see whether
  the player is online.
* **Destructive toggles arm rather than ask.** Op and ban use the same countdown
  button as Start and Stop: click to arm, click again to go, right-click or Esc
  to cancel. One gesture to learn for every consequential action in the app.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .countdown_button import CountdownButton


def bubble(colour: str, size: int = 12) -> QPixmap:
    """A filled status dot."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(colour))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return pixmap


class PlayerDetail(QWidget):
    """The right-hand panel for the selected player."""

    op_toggled = Signal(object, bool)
    whitelist_toggled = Signal(object, bool)
    ban_toggled = Signal(object, bool)
    kick_requested = Signal(object)
    group_added = Signal(object, str)
    group_removed = Signal(object, str)
    permission_set = Signal(object, str, bool)
    permission_unset = Signal(object, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._player = None

        # --- Identity ---
        self.avatar = QLabel()
        self.avatar.setFixedSize(56, 56)
        self.avatar.setStyleSheet(
            f"background:{theme.INPUT}; border:1px solid {theme.BORDER};"
            f"border-radius:{theme.RADIUS};"
        )
        self.avatar.setAlignment(Qt.AlignCenter)

        self.name = QLabel("-")
        self.name.setStyleSheet("font-size:19px; font-weight:700;")

        self.status_dot = QLabel()
        self.status_text = QLabel("")
        self.status_text.setStyleSheet(f"color:{theme.TEXT_MUTED};")

        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_text)
        status_row.addStretch(1)

        self.uuid = QLabel("")
        self.uuid.setStyleSheet(f"color:{theme.TEXT_DISABLED}; font-size:11px;")
        self.uuid.setTextInteractionFlags(Qt.TextSelectableByMouse)

        identity = QVBoxLayout()
        identity.setSpacing(2)
        identity.addWidget(self.name)
        identity.addLayout(status_row)
        identity.addWidget(self.uuid)

        header = QHBoxLayout()
        header.setContentsMargins(2, 2, 2, 2)
        header.addWidget(self.avatar, 0, Qt.AlignTop)
        header.addSpacing(12)
        header.addLayout(identity, 1)

        # --- Session ---
        self.session = QLabel("-")
        self.ping = QLabel("not available")
        self.ping.setToolTip(
            "Minecraft does not report per-player latency to the console, so this "
            "needs a mod to provide it."
        )
        self.ping.setStyleSheet(f"color:{theme.TEXT_DISABLED};")

        session_box = QGroupBox("Session")
        session_layout = QHBoxLayout(session_box)
        session_layout.addWidget(QLabel("Connected for"))
        session_layout.addWidget(self.session)
        session_layout.addStretch(1)
        session_layout.addWidget(QLabel("Ping"))
        session_layout.addWidget(self.ping)

        # --- Actions ---
        self.op_button = CountdownButton("Make operator")
        self.op_button.triggered.connect(self._toggle_op)
        self.op_button.setToolTip(
            "Operators can run any command. Click to arm, click again to confirm, "
            "right-click or press Esc to cancel."
        )

        self.whitelist_button = QPushButton("Add to whitelist")
        self.whitelist_button.clicked.connect(self._toggle_whitelist)

        self.ban_button = CountdownButton("Ban")
        self.ban_button.triggered.connect(self._toggle_ban)
        self.ban_button.setToolTip(
            "Click to arm, click again to confirm, right-click or press Esc to cancel."
        )

        self.kick_button = QPushButton("Kick")
        self.kick_button.clicked.connect(
            lambda: self._player and self.kick_requested.emit(self._player)
        )

        actions_box = QGroupBox("Actions")
        actions = QHBoxLayout(actions_box)
        actions.addWidget(self.op_button)
        actions.addWidget(self.whitelist_button)
        actions.addWidget(self.kick_button)
        actions.addWidget(self.ban_button)
        actions.addStretch(1)

        # --- Groups ---
        self.groups = QListWidget()
        self.groups.setMaximumHeight(96)

        self.group_picker = QComboBox()
        add_group = QPushButton("Add to group")
        add_group.clicked.connect(self._add_group)
        remove_group = QPushButton("Remove")
        remove_group.clicked.connect(self._remove_group)

        group_row = QHBoxLayout()
        group_row.addWidget(self.group_picker, 1)
        group_row.addWidget(add_group)
        group_row.addWidget(remove_group)

        self.groups_box = QGroupBox("Groups")
        groups_layout = QVBoxLayout(self.groups_box)
        groups_layout.addWidget(self.groups)
        groups_layout.addLayout(group_row)

        # --- Permissions ---
        self.permissions = QListWidget()

        self.node_entry = QLineEdit(placeholderText="Permission node, e.g. minecraft.command.tp")
        self.node_entry.returnPressed.connect(lambda: self._set_permission(True))
        allow = QPushButton("Allow")
        allow.clicked.connect(lambda: self._set_permission(True))
        deny = QPushButton("Deny")
        deny.clicked.connect(lambda: self._set_permission(False))
        self.remove_node = QPushButton("Remove")
        self.remove_node.clicked.connect(self._unset_permission)

        node_row = QHBoxLayout()
        node_row.addWidget(self.node_entry, 1)
        node_row.addWidget(allow)
        node_row.addWidget(deny)
        node_row.addWidget(self.remove_node)

        self.permissions_hint = QLabel(
            "Greyed entries come from a group - change those on the Permissions tab."
        )
        self.permissions_hint.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        self.permissions_hint.setWordWrap(True)

        self.permissions_box = QGroupBox("Permissions")
        permissions_layout = QVBoxLayout(self.permissions_box)
        permissions_layout.addWidget(self.permissions, 1)
        permissions_layout.addLayout(node_row)
        permissions_layout.addWidget(self.permissions_hint)

        # --- Essentials abilities ---
        # Only appears when the mod ships its ability manifest; until then there
        # is nothing to bind toggles to, and guessing node names would produce
        # switches that silently do nothing.
        self.abilities_box = QGroupBox("Essentials abilities")
        abilities_outer = QVBoxLayout(self.abilities_box)
        self.abilities_hint = QLabel("")
        self.abilities_hint.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        self.abilities_hint.setWordWrap(True)
        abilities_outer.addWidget(self.abilities_hint)
        self._abilities_layout = QVBoxLayout()
        abilities_outer.addLayout(self._abilities_layout)
        self.abilities_box.setVisible(False)
        self._ability_boxes: dict[str, QWidget] = {}

        # Fixed-height boxes must not grow. Without this, hiding the permissions
        # box (server stopped, or no LuckPerms) hands its space to Session and
        # Actions, which then float in the middle of enormous empty panels.
        for box in (session_box, actions_box, self.groups_box, self.abilities_box):
            box.setSizePolicy(box.sizePolicy().horizontalPolicy(), QSizePolicy.Fixed)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addWidget(session_box)
        layout.addWidget(actions_box)
        layout.addWidget(self.groups_box)
        layout.addWidget(self.abilities_box)
        layout.addWidget(self.permissions_box, 1)
        layout.addStretch(0)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setWidget(inner)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)

        self.set_player(None)

    # --- Population ---

    def set_player(self, player, avatar: str = "", session: str = "") -> None:
        self._player = player
        enabled = player is not None
        for widget in (
            self.op_button, self.whitelist_button, self.ban_button,
            self.kick_button, self.groups_box, self.permissions_box,
        ):
            widget.setEnabled(enabled)

        if player is None:
            self.name.setText("Select a player")
            self.status_dot.clear()
            self.status_text.setText("")
            self.uuid.setText("")
            self.avatar.clear()
            self.session.setText("-")
            self.groups.clear()
            self.permissions.clear()
            return

        self.name.setText(player.name)
        self.uuid.setText(player.uuid or "")
        self.session.setText(session or ("connected" if player.is_online else "-"))

        if avatar:
            pixmap = QPixmap(avatar)
            if not pixmap.isNull():
                self.avatar.setPixmap(
                    pixmap.scaled(48, 48, Qt.KeepAspectRatio, Qt.FastTransformation)
                )
        else:
            self.avatar.setText(player.name[:1].upper())

        if getattr(player, "is_banned", False):
            colour, text = theme.BANNED, "Banned"
        elif player.is_online:
            colour, text = theme.ONLINE, "Online"
        else:
            colour, text = theme.OFFLINE, "Offline"
        self.status_dot.setPixmap(bubble(colour))
        self.status_text.setText(text)

        self.op_button.setText("Remove operator" if player.is_op else "Make operator")
        self.whitelist_button.setText(
            "Remove from whitelist" if player.is_whitelisted else "Add to whitelist"
        )
        self.ban_button.setText("Unban" if getattr(player, "is_banned", False) else "Ban")
        self.kick_button.setEnabled(player.is_online)

    def set_groups(self, groups: list[str], available: list[str], primary: str = "") -> None:
        self.groups.clear()
        for group in groups:
            label = f"{group}   (primary)" if group == primary else group
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, group)
            self.groups.addItem(item)
        if not groups:
            placeholder = QListWidgetItem("No groups.")
            placeholder.setFlags(Qt.NoItemFlags)
            self.groups.addItem(placeholder)

        current = self.group_picker.currentText()
        self.group_picker.clear()
        self.group_picker.addItems(available)
        index = self.group_picker.findText(current)
        if index >= 0:
            self.group_picker.setCurrentIndex(index)

    def set_permissions(self, own: list, inherited: dict) -> None:
        """own: [Permission]; inherited: node -> (value, from_group)."""
        self.permissions.clear()
        owned = set()
        for permission in own:
            owned.add(permission.node)
            item = QListWidgetItem(
                f"{'ALLOW' if permission.value else 'DENY '}   {permission.node}"
            )
            item.setData(Qt.UserRole, permission.node)
            item.setForeground(QColor(theme.ACCENT if permission.value else theme.DANGER))
            self.permissions.addItem(item)

        for node, (value, origin) in sorted(inherited.items()):
            if node in owned:
                continue
            item = QListWidgetItem(
                f"{'allow' if value else 'deny '}   {node}      (from {origin})"
            )
            item.setData(Qt.UserRole, None)
            item.setForeground(QColor(theme.TEXT_DISABLED))
            item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.permissions.addItem(item)

        if not own and not inherited:
            placeholder = QListWidgetItem("No permissions beyond the defaults.")
            placeholder.setFlags(Qt.NoItemFlags)
            self.permissions.addItem(placeholder)

    def set_abilities(
        self, abilities: list, resolved: dict | None = None, live: bool = False
    ) -> None:
        """Show every declared Essentials ability, grouped by category.

        ``resolved`` is what ``/arkon perms`` said: path -> (effective, origin).
        ``live`` says whether that reflects the running server or whether we are
        only showing what the manifest declares. The difference is worth being
        explicit about: a box ticked because the player is an operator and one
        ticked because someone granted the node look identical otherwise, and
        only the second survives them being deopped.
        """
        from PySide6.QtWidgets import QCheckBox

        while self._abilities_layout.count():
            item = self._abilities_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._ability_boxes.clear()

        if not abilities:
            self.abilities_box.setVisible(False)
            return

        from ..essentials import categories

        resolved = resolved or {}
        self.abilities_hint.setText(
            "Ticked means the ability applies right now. Grey text is why - only "
            "'granted' and 'denied' are set on this player; the rest is the "
            "declared default."
            if live else
            "Start the server to see how these resolve for this player. Until "
            "then these are the mod's declared defaults, not what is in force."
        )

        for category, entries in sorted(categories(abilities).items()):
            group = QGroupBox(category)
            group_layout = QVBoxLayout(group)
            group_layout.setSpacing(4)

            for ability in entries:
                effective, origin = resolved.get(ability.path, (None, ""))

                row = QHBoxLayout()
                row.setSpacing(8)

                # A node the running server does not report is not gated by a
                # permission at all - the mod reads it from a server setting. A
                # toggle for one would look identical to a working toggle and do
                # nothing, so those are shown as a value with its source named.
                config_backed = ability.is_numeric or (live and ability.path not in resolved)

                if config_backed:
                    control = QLabel(ability.label)
                    # Indent past where a checkbox indicator would be, so every
                    # label in the group starts in the same column.
                    row.addSpacing(24)
                    row.addWidget(control)
                    row.addStretch(1)
                    note = (
                        f"set by {ability.config_key}"
                        if ability.config_key else "set by the server config"
                    )
                else:
                    control = QCheckBox(ability.label)
                    control.setChecked(bool(effective))
                    control.setEnabled(live)
                    control.toggled.connect(
                        lambda on, node=ability.node: self._player
                        and self.permission_set.emit(self._player, node, on)
                    )
                    row.addWidget(control)
                    row.addStretch(1)
                    note = origin if origin in ("granted", "denied") else ability.default_text

                control.setToolTip(
                    f"{ability.description or ability.label}\n\n{ability.node}"
                    + (f"\nSet by: {ability.config_key}" if ability.config_key else "")
                )

                # Explicit settings are worth reading; a default is context.
                # "denied" earns the danger colour because it is the one state
                # someone deliberately imposed against the grain.
                colour = {
                    "granted": theme.TEXT_MUTED,
                    "denied": theme.DANGER,
                }.get(note, theme.TEXT_DISABLED)
                caption = QLabel(note)
                caption.setStyleSheet(f"color:{colour}; font-size:11px;")
                row.addWidget(caption)

                group_layout.addLayout(row)
                self._ability_boxes[ability.node] = control

            self._abilities_layout.addWidget(group)

        self.abilities_box.setVisible(True)

    def set_permissions_available(self, available: bool, reason: str = "") -> None:
        self.groups_box.setVisible(available)
        self.permissions_box.setVisible(available)
        if not available and reason:
            self.permissions_hint.setText(reason)

    # --- Actions ---

    def _toggle_op(self) -> None:
        if self._player:
            self.op_toggled.emit(self._player, not self._player.is_op)

    def _toggle_whitelist(self) -> None:
        if self._player:
            self.whitelist_toggled.emit(self._player, not self._player.is_whitelisted)

    def _toggle_ban(self) -> None:
        if self._player:
            self.ban_toggled.emit(self._player, not getattr(self._player, "is_banned", False))

    def _add_group(self) -> None:
        group = self.group_picker.currentText()
        if self._player and group:
            self.group_added.emit(self._player, group)

    def _remove_group(self) -> None:
        item = self.groups.currentItem()
        group = item.data(Qt.UserRole) if item else None
        if self._player and group:
            self.group_removed.emit(self._player, group)

    def _set_permission(self, allow: bool) -> None:
        node = self.node_entry.text().strip()
        if self._player and node:
            self.node_entry.clear()
            self.permission_set.emit(self._player, node, allow)

    def _unset_permission(self) -> None:
        item = self.permissions.currentItem()
        node = item.data(Qt.UserRole) if item else None
        if self._player and node:
            self.permission_unset.emit(self._player, node)


class PlayersPanel(QWidget):
    """List of known players, and the detail panel for the selected one."""

    player_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._players: list = []

        self.search = QLineEdit(placeholderText="Search players...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)

        self.list = QListWidget()
        self.list.setIconSize(QSize(24, 24))
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.itemSelectionChanged.connect(self._on_selected)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.list, 1)

        self.detail = PlayerDetail()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 720])
        splitter.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

    def set_players(self, players: list) -> None:
        previous = self.selected().name if self.selected() else None
        self._players = players

        self.list.blockSignals(True)
        self.list.clear()
        for player in players:
            item = QListWidgetItem(player.name)
            item.setData(Qt.UserRole, player.name)
            item.setIcon(QIcon(bubble(self._colour_for(player), 10)))
            if getattr(player, "is_banned", False):
                item.setForeground(QColor(theme.DANGER))
            elif not player.is_online:
                item.setForeground(QColor(theme.TEXT_MUTED))
            self.list.addItem(item)
        self.list.blockSignals(False)

        for index in range(self.list.count()):
            if self.list.item(index).data(Qt.UserRole) == previous:
                self.list.setCurrentRow(index)
                break
        else:
            if players:
                self.list.setCurrentRow(0)
        self._apply_filter(self.search.text())

    @staticmethod
    def _colour_for(player) -> str:
        if getattr(player, "is_banned", False):
            return theme.BANNED
        return theme.ONLINE if player.is_online else theme.OFFLINE

    def set_avatar(self, name: str, path: str) -> None:
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.data(Qt.UserRole) != name:
                continue
            head = QPixmap(path)
            if head.isNull():
                return
            item.setIcon(QIcon(head))
            return

    def selected(self):
        item = self.list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.UserRole)
        for player in self._players:
            if player.name == name:
                return player
        return None

    def _on_selected(self) -> None:
        self.player_selected.emit(self.selected())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setHidden(bool(needle) and needle not in item.text().lower())
