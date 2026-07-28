"""Resolution of every path the launcher reads or writes.

Two deployment modes, decided by a single marker file:

* **portable** - ``portable.txt`` sits beside the executable, so all mutable
  state lives in ``.\\data`` next to it and the whole folder can be moved.
* **installed** - no marker, so state lives in ``%LOCALAPPDATA%\\Arkon Launcher``
  because the install directory (Program Files) is read-only at runtime.

Every other module asks this one for paths and never joins its own. That keeps
the portable/installed split in one place instead of smeared across the app.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME

PORTABLE_MARKER = "portable.txt"

# Set by _resolve_state_dir() when a portable copy turns out to be read-only.
_state_dir_fallback_reason: str | None = None


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle rather than source."""
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """Directory containing the executable, or the repo root when run from source."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(relative: str) -> Path:
    """Locate a bundled read-only data file (e.g. ``data/client_only.json``).

    PyInstaller unpacks bundled data to ``sys._MEIPASS``; from source it sits in
    the package directory.
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def is_portable() -> bool:
    return (app_dir() / PORTABLE_MARKER).is_file()


def _is_writable(directory: Path) -> bool:
    """Probe by actually writing, since Windows ACLs make stat() unreliable."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _local_appdata_state() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / "AppData" / "Local" / APP_NAME


def _resolve_state_dir() -> Path:
    """Pick the state directory, falling back if a portable copy can't be written.

    A portable folder can legitimately end up somewhere read-only - a
    write-protected USB stick, or a zip extracted straight into Program Files.
    Falling back beats failing to start, but the reason is recorded so the UI can
    explain why settings aren't where the user expects.
    """
    global _state_dir_fallback_reason

    if is_portable():
        portable_state = app_dir() / "data"
        if _is_writable(portable_state):
            return portable_state
        _state_dir_fallback_reason = (
            f"{portable_state} is not writable, so settings are being stored in "
            f"{_local_appdata_state()} instead."
        )

    return _local_appdata_state()


_STATE_DIR = _resolve_state_dir()


def state_dir_fallback_reason() -> str | None:
    """Non-None when portable state was requested but wasn't usable."""
    return _state_dir_fallback_reason


def state_dir() -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR


def settings_path() -> Path:
    return state_dir() / "settings.json"


def user_denylist_path() -> Path:
    """Client-only mod ids the user marked, plus ones crash triage worked out."""
    return state_dir() / "client_only.user.json"


def cache_dir() -> Path:
    """Downloaded server jars and the playit agent, shared across instances."""
    path = state_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = state_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def launcher_log_path() -> Path:
    return logs_dir() / "launcher.log"


# --- Per-instance paths -------------------------------------------------------
#
# These live with the instance rather than with the app, in both modes: they hold
# world backups and per-world settings, which belong to the world data. The
# uninstaller must never touch this tree.

INSTANCE_DATA_DIRNAME = ".arkonlauncher"


def instance_data_dir(instance_dir: Path) -> Path:
    return Path(instance_dir) / INSTANCE_DATA_DIRNAME


def instance_settings_path(instance_dir: Path) -> Path:
    return instance_data_dir(instance_dir) / "launcher.json"


def server_dir(instance_dir: Path, world_folder: str) -> Path:
    """Working directory java runs in for one world's server."""
    return instance_data_dir(instance_dir) / "servers" / world_folder


def backups_dir(instance_dir: Path, world_folder: str) -> Path:
    return instance_data_dir(instance_dir) / "backups" / world_folder
