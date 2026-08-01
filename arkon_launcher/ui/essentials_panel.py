"""Arkon Essentials' own settings, when the mod is installed.

The mod keeps its configuration in one JSON file and also exposes
``/arkon config <key> <value>``, which applies immediately with no restart. So
this panel has two jobs depending on whether the server is up:

* **Stopped** - edit the file. It is read at startup, so that is enough.
* **Running** - send the command *as well*, because the running server owns its
  own copy of the settings and would overwrite a file edited underneath it.

Rendering reuses the generic config form, so descriptions written as comments in
the file show up here for free and a setting added by a future version of the
mod appears without this file changing. What is added on top is the grouping and
the live-apply, which are specific to Essentials.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import configform, essentials
from . import theme
from .config_form import ConfigForm

CONFIG_NAME = "arkonessentials.json"


class EssentialsPanel(QWidget):
    """Settings for Arkon Essentials."""

    save_requested = Signal(object, str)  # path, text
    apply_live = Signal(list)  # [(key, value), ...]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path: Path | None = None
        self._running = False
        self._original: dict[str, str] = {}
        # Setting metadata from the jar: label, description, bounds, command.
        self._meta: dict[str, essentials.Setting] = {}

        self.header = QLabel("")
        self.header.setWordWrap(True)
        self.header.setStyleSheet(f"color:{theme.TEXT_MUTED};")

        self.form = ConfigForm()
        self.form.changed.connect(self._on_changed)

        self.save_button = QPushButton("Save")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)

        self.revert_button = QPushButton("Revert")
        self.revert_button.setEnabled(False)
        self.revert_button.clicked.connect(self.reload)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{theme.TEXT_MUTED};")

        buttons = QHBoxLayout()
        buttons.addWidget(self.status, 1)
        buttons.addWidget(self.revert_button)
        buttons.addWidget(self.save_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.header)
        layout.addWidget(self.form, 1)
        layout.addLayout(buttons)

    # --- Population ---

    def set_instance(self, config_dir, mods_dir) -> bool:
        """Point at an instance. False when Essentials is not installed."""
        if not essentials.is_installed(Path(mods_dir)):
            self._path = None
            return False

        self._path = Path(config_dir) / CONFIG_NAME
        # Read the mod's own wording rather than showing raw keys. Hand-written
        # labels go stale the moment a setting is reworded; these cannot.
        self._meta = essentials.settings_by_key(essentials.read_settings(Path(mods_dir)))
        self.reload()
        return True

    def set_running(self, running: bool) -> None:
        self._running = running
        self._update_hint()

    def reload(self) -> None:
        if self._path is None:
            return

        if not self._path.is_file():
            # The file is written on the mod's first run, so before that there
            # is genuinely nothing to edit - say so rather than showing a blank.
            self.form.clear()
            self.header.setText(
                f"{CONFIG_NAME} has not been created yet. Start the server once "
                f"with Arkon Essentials installed and it will appear here."
            )
            self.save_button.setEnabled(False)
            self.revert_button.setEnabled(False)
            return

        self.form.set_metadata(self._meta)
        if not self.form.load(self._path):
            self.header.setText(
                f"{self._path.name} could not be read as settings. Edit it from "
                f"the Mods tab instead."
            )
            return

        self._original = self._snapshot()
        self.save_button.setEnabled(False)
        self.revert_button.setEnabled(False)
        self._update_hint()

    def _update_hint(self) -> None:
        self.header.setText(
            "Applied to the running server as soon as you save - Essentials "
            "re-reads these without a restart."
            if self._running else
            "The server is stopped. These are read when it next starts."
        )

    def _snapshot(self) -> dict[str, str]:
        document = getattr(self.form, "_document", None)
        if document is None:
            return {}
        return {entry.key: entry.raw_value for entry in document.entries}

    # --- Editing ---

    def _on_changed(self) -> None:
        dirty = self._snapshot() != self._original
        self.save_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)
        self.save_button.setText("Save *" if dirty else "Save")

    def changed_keys(self) -> list[tuple[str, str]]:
        """Only what actually differs, so live-apply sends the minimum."""
        current = self._snapshot()
        return [
            (key, value.strip().strip('"'))
            for key, value in current.items()
            if self._original.get(key) != value
        ]

    def _save(self) -> None:
        if self._path is None:
            return
        changes = self.changed_keys()
        self.save_requested.emit(self._path, self.form.text())
        if self._running and changes:
            self.apply_live.emit(changes)
        self._original = self._snapshot()
        self._on_changed()
        self.status.setText(
            f"Saved {len(changes)} change(s)."
            + (" Applied to the running server." if self._running and changes else "")
        )
