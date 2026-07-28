"""Edit the modpack's config files without leaving the launcher.

Edits go to the **instance's** config folder, which is the source of truth: the
server's config directory is a mirror rebuilt on every start, so writing there
instead would be silently undone. After saving, the single edited file is
re-mirrored so a running server can see it.

"Reload after saving" runs ``/reload``, which genuinely re-reads datapacks, loot
tables and functions. Most mod configs are read once at startup and will not
change until a restart - the UI says so rather than implying the reload did more
than it did.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

HINT = "color:#8b949e;"

# Text-shaped config formats. Anything else in there is not ours to edit.
EDITABLE_SUFFIXES = {
    ".json", ".json5", ".toml", ".cfg", ".conf", ".config", ".properties",
    ".yaml", ".yml", ".txt", ".snbt", ".ini", ".md", ".mcmeta",
}
MAX_EDITABLE_BYTES = 2 * 1024 * 1024


class ConfigEditor(QWidget):
    save_requested = Signal(object, str, bool)  # (path, text, reload_after)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._root: Path | None = None
        self._current: Path | None = None
        self._original = ""

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemSelectionChanged.connect(self._on_selected)

        self.filter_box = QLineEdit(placeholderText="Filter files...")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self._apply_filter)

        left = QGroupBox("Config files")
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.filter_box)
        left_layout.addWidget(self.tree, 1)

        self.path_label = QLabel("Select a file.")
        self.path_label.setStyleSheet(HINT)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.editor = QPlainTextEdit()
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        self.editor.setFont(font)
        self.editor.setEnabled(False)
        self.editor.textChanged.connect(self._on_text_changed)

        self.reload_after = QCheckBox("Run /reload after saving")
        self.reload_after.setChecked(True)
        self.reload_after.setToolTip(
            "Re-reads datapacks, loot tables and functions on the running server. "
            "Most mod configs are only read at startup and need a restart instead."
        )

        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save)
        self.save_button.setEnabled(False)

        self.revert_button = QPushButton("Revert")
        self.revert_button.clicked.connect(self._revert)
        self.revert_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.reload_after)
        buttons.addStretch(1)
        buttons.addWidget(self.revert_button)
        buttons.addWidget(self.save_button)

        right = QGroupBox("Editor")
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.path_label)
        right_layout.addWidget(self.editor, 1)
        right_layout.addLayout(buttons)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 820])

        self.notice = QLabel(
            "Editing the instance's config folder, which is what the server copies "
            "from on every start."
        )
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(HINT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.notice)

    # --- Population ---

    def set_root(self, root: Path | None) -> None:
        if root is not None and self._root == Path(root):
            return
        self._root = Path(root) if root else None
        self.tree.clear()
        self._current = None
        self.editor.clear()
        self.editor.setEnabled(False)
        self.path_label.setText("Select a file.")
        self._update_buttons()

        if not self._root or not self._root.is_dir():
            self.notice.setText("No config folder found for this instance.")
            return

        self._populate(self._root, self.tree.invisibleRootItem(), depth=0)
        self.tree.sortItems(0, Qt.AscendingOrder)

    def _populate(self, directory: Path, parent, depth: int) -> None:
        # Config trees can nest deeply; a few levels is enough to reach anything
        # worth hand-editing without building a model of the entire folder.
        if depth > 4:
            return
        try:
            entries = sorted(
                directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError:
            return

        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                node = QTreeWidgetItem([entry.name])
                node.setData(0, Qt.UserRole, None)
                self._populate(entry, node, depth + 1)
                # Only show folders that ended up with something in them.
                if node.childCount():
                    parent.addChild(node)
            elif entry.suffix.lower() in EDITABLE_SUFFIXES:
                try:
                    if entry.stat().st_size > MAX_EDITABLE_BYTES:
                        continue
                except OSError:
                    continue
                node = QTreeWidgetItem([entry.name])
                node.setData(0, Qt.UserRole, str(entry))
                parent.addChild(node)

    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()

        def visit(item) -> bool:
            matched = False
            for index in range(item.childCount()):
                if visit(item.child(index)):
                    matched = True
            own = not text or text in item.text(0).lower()
            item.setHidden(not (own or matched))
            if matched:
                item.setExpanded(True)
            return own or matched

        for index in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(index))

    # --- Editing ---

    def _on_selected(self) -> None:
        item = self.tree.currentItem()
        path = item.data(0, Qt.UserRole) if item else None
        if not path:
            return

        if self._is_dirty():
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                f"{self._current.name if self._current else 'This file'} has unsaved "
                f"changes. Discard them?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self._load(Path(path))

    def _load(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            QMessageBox.warning(self, "Could not open", str(exc))
            return

        self._current = path
        self._original = text
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self.editor.setEnabled(True)
        try:
            shown = path.relative_to(self._root) if self._root else path
        except ValueError:
            shown = path
        self.path_label.setText(str(shown))
        self._update_buttons()

    def _is_dirty(self) -> bool:
        return self._current is not None and self.editor.toPlainText() != self._original

    def _on_text_changed(self) -> None:
        self._update_buttons()

    def _update_buttons(self) -> None:
        dirty = self._is_dirty()
        self.save_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)
        self.save_button.setText("Save *" if dirty else "Save")

    def _revert(self) -> None:
        if self._current:
            self.editor.setPlainText(self._original)

    def _save(self) -> None:
        if not self._current:
            return
        self.save_requested.emit(
            self._current, self.editor.toPlainText(), self.reload_after.isChecked()
        )

    def mark_saved(self) -> None:
        self._original = self.editor.toPlainText()
        self._update_buttons()
