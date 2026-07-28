"""Entry point: ``python -m arkon_launcher`` and the packaged executable."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from . import APP_NAME, __version__


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Arkon")
    app.setStyle("Fusion")

    # Imported here so a Qt failure surfaces after QApplication exists and can
    # therefore be shown in a dialog rather than a silent exit.
    from .ui.main_window import MainWindow

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
