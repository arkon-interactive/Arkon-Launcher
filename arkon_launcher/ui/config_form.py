"""A config file as a form, when the file is shaped like settings.

Built on :mod:`arkon_launcher.configform`, which does the reading and - more
importantly - the writing, by replacing one value on one line rather than
re-serialising the file. So a comment nobody can reproduce, or an ordering the
mod author cared about, survives being edited here.

The form is offered alongside the text editor rather than instead of it. Some
files are data, not settings, and some settings are lists this cannot represent;
in both cases the text is the honest view and the form politely declines.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import configform
from . import theme

# Wide enough for a real number, bounded so a stray keypress cannot write
# something the mod will reject.
INT_RANGE = (-2_147_483_648, 2_147_483_647)
FLOAT_RANGE = (-1e9, 1e9)


class ConfigForm(QWidget):
    """Editable widgets for one parsed config file."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document: configform.ConfigDocument | None = None
        self._dirty = False
        # Optional per-key metadata (label, description, bounds) supplied by a
        # mod that documents its own settings. Without it the form falls back to
        # the raw key, which is right for the hundreds of mods that ship nothing.
        self._metadata: dict = {}

    def set_metadata(self, metadata: dict) -> None:
        self._metadata = metadata or {}

        self.heading = QLabel("")
        self.heading.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        self.heading.setWordWrap(True)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setWidget(self._body)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.heading)
        layout.addWidget(area, 1)

    # --- Population ---

    def load(self, path, text: str | None = None) -> bool:
        """Show ``path`` as a form. False when it is not form-shaped.

        ``text`` lets the caller supply unsaved content, so switching to this
        view after typing in the text editor shows what is on screen rather
        than what is still on disk.
        """
        document = (
            configform.parse(path) if text is None else configform.parse_text(path, text)
        )
        self.clear()
        if document is None:
            return False

        self._document = document
        self._dirty = False

        editable = sum(1 for e in document.entries if not e.complex)
        skipped = len(document.entries) - editable
        note = f"{editable} setting(s)"
        if skipped:
            note += f", {skipped} shown read-only because a form cannot edit them safely"
        self.heading.setText(f"{document.path.name} - {note}.")

        # Metadata carries its own categories, which are written for a reader;
        # the file's own sections are whatever the format happened to nest.
        if self._metadata:
            grouped: dict[str, list] = {}
            for entry in document.entries:
                meta = self._metadata.get(entry.label)
                grouped.setdefault(meta.category if meta else "Other", []).append(entry)
            sections = sorted(grouped.items())
        else:
            sections = list(document.sections().items())

        for section, entries in sections:
            group = QGroupBox(section or "Settings")
            group_layout = QVBoxLayout(group)
            group_layout.setSpacing(2)
            for entry in entries:
                group_layout.addWidget(self._row(entry))
            self._body_layout.addWidget(group)

        self._body_layout.addStretch(1)
        return True

    def clear(self) -> None:
        self._document = None
        self._dirty = False
        self.heading.setText("")
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _row(self, entry: configform.Entry) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)

        meta = self._metadata.get(entry.label)
        label = QLabel(meta.label if meta else entry.label)
        label.setMinimumWidth(220)
        if meta:
            label.setToolTip(meta.tooltip)
        elif entry.description:
            label.setToolTip(entry.description)
        layout.addWidget(label)

        control = self._control(entry)
        if meta:
            control.setToolTip(meta.tooltip)
            # Bounds the mod declares beat the widget's generic range: they stop
            # a value the server would reject from being entered at all.
            if isinstance(control, QSpinBox) and meta.minimum is not None:
                control.setRange(int(meta.minimum), int(meta.maximum))
            elif isinstance(control, QDoubleSpinBox) and meta.minimum is not None:
                control.setRange(float(meta.minimum), float(meta.maximum))
        layout.addWidget(control, 1)

        if meta and meta.description:
            note = QLabel(
                meta.description
                if len(meta.description) <= 90
                else meta.description[:90] + "..."
            )
            note.setStyleSheet(f"color:{theme.TEXT_DISABLED}; font-size:11px;")
            note.setToolTip(meta.description)
            note.setMinimumWidth(260)
            layout.addWidget(note)
        elif entry.description:
            # The comment above a setting is usually its only documentation, so
            # it belongs on screen rather than hidden behind a hover.
            note = QLabel(
                entry.description
                if len(entry.description) <= 90
                else entry.description[:90] + "..."
            )
            note.setStyleSheet(f"color:{theme.TEXT_DISABLED}; font-size:11px;")
            note.setToolTip(entry.description)
            note.setMinimumWidth(260)
            layout.addWidget(note)

        return row

    def _control(self, entry: configform.Entry) -> QWidget:
        if entry.disabled:
            control = QLineEdit(entry.raw_value)
            control.setReadOnly(True)
            control.setToolTip(
                "This line is commented out in the file. Re-enabling it is an "
                "edit to the file's structure, so do that in the text view."
            )
            return control

        kind = entry.kind

        if kind == "complex":
            control = QLineEdit(entry.raw_value.strip() or "(spans several lines)")
            control.setReadOnly(True)
            control.setToolTip(
                "Lists and nested values are edited in the text view, where the "
                "structure is visible."
            )
            return control

        if kind == "bool":
            control = QCheckBox()
            control.setChecked(bool(entry.value))
            control.toggled.connect(
                lambda on, e=entry: self._write(e, "true" if on else "false")
            )
            return control

        if kind == "integer":
            control = QSpinBox()
            control.setRange(*INT_RANGE)
            control.setValue(int(entry.value))
            control.valueChanged.connect(lambda v, e=entry: self._write(e, str(v)))
            return control

        if kind == "number":
            control = QDoubleSpinBox()
            control.setRange(*FLOAT_RANGE)
            control.setDecimals(4)
            control.setValue(float(entry.value))
            control.valueChanged.connect(
                lambda v, e=entry: self._write(e, _trim(v))
            )
            return control

        control = QLineEdit(str(entry.value))
        quoted = entry.raw_value.strip().startswith('"')
        control.textChanged.connect(
            lambda text, e=entry, q=quoted: self._write(e, f'"{text}"' if q else text)
        )
        return control

    # --- Editing ---

    def _write(self, entry: configform.Entry, text: str) -> None:
        if self._document is None:
            return
        self._document.set_value(entry, text)
        self._dirty = True
        self.changed.emit()

    def is_dirty(self) -> bool:
        return self._dirty

    def text(self) -> str:
        """The whole file, with edits applied in place."""
        return self._document.text() if self._document else ""

    def path(self):
        return self._document.path if self._document else None


def _trim(value: float) -> str:
    """Write 1.5 rather than 1.5000, but keep a float looking like a float."""
    text = f"{value:.4f}".rstrip("0")
    return text + "0" if text.endswith(".") else text
