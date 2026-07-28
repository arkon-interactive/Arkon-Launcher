"""Generate installer/app.ico.

Kept as a script rather than a checked-in binary so the icon can be tweaked
without a graphics tool, and so the build has no external asset dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QLinearGradient, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)

    gradient = QLinearGradient(0, 0, 0, size)
    gradient.setColorAt(0.0, QColor("#3fa34d"))
    gradient.setColorAt(1.0, QColor("#1e6f3c"))

    radius = size * 0.22
    painter.setBrush(QBrush(gradient))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # A blocky "A", legible even at 16px.
    font = QFont("Segoe UI", int(size * 0.58))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#f5f7f5"))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, "A")

    painter.end()
    return image


def main() -> int:
    app = QApplication(sys.argv)  # noqa: F841 - required for QImage/QFont.

    destination = Path(__file__).resolve().parent.parent / "installer" / "app.ico"
    destination.parent.mkdir(parents=True, exist_ok=True)

    largest = render(256)
    if not largest.save(str(destination), "ICO"):
        print("Qt could not write ICO; falling back to PNG.")
        largest.save(str(destination.with_suffix(".png")), "PNG")
        return 1

    print(f"Wrote {destination} ({destination.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
