"""A button that waits a beat before doing something consequential.

Starting, stopping and restarting a server are all easy to hit by accident and
awkward to undo, so each one arms a short countdown instead of firing straight
away:

* **Click** - arms the countdown; a green wash sweeps left to right and the
  label counts down.
* **Click again** - fires immediately, with the button's ordinary pressed look.
* **Right-click, or Esc** - cancels, flashing a red outline.

The button paints itself normally and the wash goes on top with alpha, so the
widget keeps its native styling in whatever theme is in use rather than being
recoloured wholesale.
"""

from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QPushButton

# Sits with the dark Fusion palette and the console's level colours rather than
# being an arbitrary bright green.
ARMED_GREEN = QColor(63, 163, 77, 110)
CANCEL_RED = QColor(224, 108, 117)

DEFAULT_DELAY_MS = 3000
TICK_MS = 40
CANCEL_FLASH_MS = 450


class CountdownButton(QPushButton):
    """A QPushButton whose action is delayed, confirmable and cancellable."""

    triggered = Signal()
    armed = Signal()
    cancelled = Signal()

    def __init__(self, text: str, parent=None, delay_ms: int = DEFAULT_DELAY_MS) -> None:
        super().__init__(text, parent)
        self.delay_ms = delay_ms
        self._idle_text = text
        self._cancel_flash = False
        # Measured against a real clock rather than counting ticks: QTimer
        # intervals drift under load, and a "3 second" countdown that actually
        # takes five is worse than no countdown at all.
        self._clock = QElapsedTimer()

        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._on_tick)

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._clear_flash)

        self.setFocusPolicy(Qt.StrongFocus)
        # Right-click is the cancel gesture, so no context menu should appear.
        self.setContextMenuPolicy(Qt.PreventContextMenu)
        super().clicked.connect(self._on_clicked)

        self._apply_minimum_width()

    def _apply_minimum_width(self) -> None:
        """Reserve room for the countdown suffix so arming never shifts layout."""
        metrics = self.fontMetrics()
        width = metrics.horizontalAdvance(f"{self._idle_text}  (0s)") + 28
        self.setMinimumWidth(max(self.minimumWidth(), width))

    # --- State ---

    @property
    def is_armed(self) -> bool:
        return self._tick.isActive()

    @property
    def elapsed_ms(self) -> int:
        return int(self._clock.elapsed()) if self._clock.isValid() else 0

    @property
    def progress(self) -> float:
        if not self.delay_ms:
            return 1.0
        return min(1.0, self.elapsed_ms / self.delay_ms)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        if not self.is_armed:
            self._idle_text = text
            super().setText(text)
            self._apply_minimum_width()
        else:
            self._idle_text = text

    def idle_text(self) -> str:
        return self._idle_text

    # --- Interaction ---

    def _on_clicked(self) -> None:
        if self.is_armed:
            self._fire()
        else:
            self.arm()

    def arm(self) -> None:
        if self.is_armed or not self.isEnabled():
            return
        self._idle_text = super().text() or self._idle_text
        self._cancel_flash = False
        self._clock.restart()
        self._tick.start()
        self._update_label()
        self.armed.emit()

    def _on_tick(self) -> None:
        if self.elapsed_ms >= self.delay_ms:
            self._fire()
            return
        self._update_label()

    def _update_label(self) -> None:
        remaining = max(0, self.delay_ms - self.elapsed_ms)
        # Ceiling, so a 3000 ms delay reads 3 - 2 - 1 rather than starting at 2.
        seconds = (remaining + 999) // 1000
        super().setText(f"{self._idle_text}  ({seconds}s)")
        self.update()

    def _stop(self) -> None:
        self._tick.stop()
        self._elapsed = 0
        super().setText(self._idle_text)
        self.update()

    def _fire(self) -> None:
        self._stop()
        self.triggered.emit()

    def cancel(self) -> None:
        if not self.is_armed:
            return
        self._stop()
        self._cancel_flash = True
        self._flash_timer.start(CANCEL_FLASH_MS)
        self.cancelled.emit()
        self.update()

    def _clear_flash(self) -> None:
        self._cancel_flash = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.RightButton:
            # Cancels when armed; harmlessly ignored otherwise.
            self.cancel()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key_Escape and self.is_armed:
            self.cancel()
            event.accept()
            return
        super().keyPressEvent(event)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt naming
        # A button that is disabled mid-countdown must not fire afterwards.
        if not enabled and self.is_armed:
            self._stop()
        super().setEnabled(enabled)

    # --- Painting ---

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)

        if not self.is_armed and not self._cancel_flash:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        area = self.rect().adjusted(1, 1, -1, -1)

        if self.is_armed:
            width = int(area.width() * self.progress)
            if width > 0:
                painter.fillRect(area.adjusted(0, 0, -(area.width() - width), 0), ARMED_GREEN)

        if self._cancel_flash:
            pen = QPen(CANCEL_RED)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(area, 3, 3)

        painter.end()
