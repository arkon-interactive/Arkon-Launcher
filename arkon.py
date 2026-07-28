"""Entry point for the packaged executable.

PyInstaller runs its target as a top-level script, so pointing it at
``arkon_launcher/__main__.py`` would break every relative import in the package.
This module imports the package by name instead, which works both frozen and
from source.

Running from source works either way:

    python arkon.py
    python -m arkon_launcher
"""

from __future__ import annotations

import multiprocessing
import sys

from arkon_launcher.__main__ import main

if __name__ == "__main__":
    # Harmless when unused, essential if a frozen build ever spawns a process:
    # without it, each child would re-run the app instead of the worker.
    multiprocessing.freeze_support()
    sys.exit(main())
