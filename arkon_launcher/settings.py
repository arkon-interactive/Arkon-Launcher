"""Persisted settings, split by where they belong.

App-level settings (last instance, memory, port) live in the state directory.
Per-world settings and mod overrides live with the instance, so they travel with
the world data rather than the installation.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths


def _load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    temporary.replace(path)


@dataclass
class AppSettings:
    last_instance: str | None = None
    last_world: str | None = None
    extra_instance_roots: list[str] = field(default_factory=list)
    max_memory_mb: int = 6144
    server_port: int = 25565
    auto_restart: bool = True
    max_restarts: int = 3
    backup_on_start: bool = True
    backup_keep: int = 10
    eula_accepted: bool = False
    auto_op_owner: bool = True
    # Keep LuckPerms verbose running while the server is up, harvesting the
    # permission nodes the pack actually checks. The lines are diverted from the
    # console so they never spam it.
    passive_permission_scan: bool = True

    # Scheduled backups, off unless asked for.
    backup_schedule_enabled: bool = False
    backup_interval_hours: int = 6
    backup_location: str = ""  # Empty means alongside the instance.
    # Warnings broadcast before a scheduled backup, as seconds beforehand.
    backup_announcements: list[int] = field(default_factory=lambda: [300, 60, 10])
    backup_announce_enabled: bool = True

    @classmethod
    def load(cls) -> "AppSettings":
        data = _load(paths.settings_path())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        _save(paths.settings_path(), asdict(self))


@dataclass
class WorldSettings:
    """Per-world overrides, stored inside the instance."""

    disabled_mod_ids: list[str] = field(default_factory=list)
    force_include_mod_ids: list[str] = field(default_factory=list)
    max_memory_mb: int | None = None
    server_port: int | None = None

    @classmethod
    def load(cls, instance_dir: Path, world_folder: str) -> "WorldSettings":
        data = _load(paths.instance_settings_path(instance_dir))
        world = (data.get("worlds") or {}).get(world_folder) or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in world.items() if k in known})

    def save(self, instance_dir: Path, world_folder: str) -> None:
        path = paths.instance_settings_path(instance_dir)
        data = _load(path)
        data.setdefault("worlds", {})[world_folder] = asdict(self)
        _save(path, data)
