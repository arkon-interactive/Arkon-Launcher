"""A sliding on/off switch.

Qt has no switch widget, and a checkbox reads as "tick this to agree" rather
than "this is currently on" - which is the wrong sense for granting an ability
to someone. Green means the ability applies; the neutral track means it does
not, so a panel of these can be read at a glance without inspecting labels.
"""

from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractButton

from . import theme

TRACK_WIDTH = 38
TRACK_HEIGHT = 20
KNOB_MARGIN = 2


class ToggleSwitch(QAbstractButton):
    """Checkable switch. ``toggled`` behaves exactly like a checkbox's."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(TRACK_WIDTH, TRACK_HEIGHT)
        self._offset = 0.0

        self._slide = QPropertyAnimation(self, b"offset", self)
        self._slide.setDuration(130)
        self._slide.setEasingCurve(QEasingCurve.InOutCubic)
        self.toggled.connect(self._animate)

    # --- Animation ---

    def get_offset(self) -> float:
        return self._offset

    def set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, get_offset, set_offset)

    def _animate(self, checked: bool) -> None:
        self._slide.stop()
        self._slide.setStartValue(self._offset)
        self._slide.setEndValue(1.0 if checked else 0.0)
        self._slide.start()

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt naming
        super().setChecked(checked)
        # Jump rather than slide when set programmatically: rebuilding a panel
        # of forty switches should not play forty animations.
        if not self._slide.state():
            self._offset = 1.0 if checked else 0.0
            self.update()

    # --- Painting ---

    def sizeHint(self) -> QSize:
        return QSize(TRACK_WIDTH, TRACK_HEIGHT)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        enabled = self.isEnabled()
        if self.isChecked():
            track = QColor(theme.ACCENT if enabled else theme.ACCENT_MUTED)
        else:
            track = QColor(theme.INPUT if enabled else theme.SURFACE)

        radius = TRACK_HEIGHT / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(self.rect(), radius, radius)

        if not self.isChecked():
            painter.setPen(QColor(theme.BORDER_STRONG if enabled else theme.BORDER))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(
                self.rect().adjusted(0, 0, -1, -1), radius, radius
            )

        knob_size = TRACK_HEIGHT - KNOB_MARGIN * 2
        travel = TRACK_WIDTH - knob_size - KNOB_MARGIN * 2
        left = KNOB_MARGIN + travel * self._offset

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.TEXT if enabled else theme.TEXT_DISABLED))
        painter.drawEllipse(int(left), KNOB_MARGIN, knob_size, knob_size)
        painter.end()
