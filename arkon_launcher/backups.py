"""World backups: zip the save, keep the last N, restore on request.

The one rule that matters: a backup taken while the server is running must be
consistent. Minecraft writes chunks lazily, so we ask the server to flush and
stop saving (``save-off`` + ``save-all flush``), take the copy, then turn saving
back on - and turn it back on even if the copy fails, because leaving a live
server with saving disabled would lose the session's progress.
"""

from __future__ import annotations

import datetime
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths

ProgressCallback = Callable[[str], None]

# Written by the running world; never worth carrying into a backup.
SKIP_NAMES = {"session.lock"}


class BackupError(Exception):
    pass


@dataclass
class Backup:
    path: Path
    created: datetime.datetime
    size: int

    @property
    def label(self) -> str:
        return self.created.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def size_mb(self) -> float:
        return self.size / 1024**2


def backup_root(instance_dir: Path, world_folder: str, custom: str | Path | None = None) -> Path:
    """Where this world's backups live.

    Defaults to the instance so backups travel with the worlds they protect and
    survive uninstalling the launcher; a custom folder can put them on another
    drive, which is the more useful place for them if the disk is the risk.
    """
    if custom:
        return Path(custom) / world_folder
    return paths.backups_dir(instance_dir, world_folder)


def list_backups(
    instance_dir: Path, world_folder: str, custom: str | Path | None = None
) -> list[Backup]:
    """Newest first."""
    directory = backup_root(instance_dir, world_folder, custom)
    if not directory.is_dir():
        return []

    found: list[Backup] = []
    for archive in directory.glob("*.zip"):
        try:
            stat = archive.stat()
        except OSError:
            continue
        found.append(
            Backup(
                path=archive,
                created=datetime.datetime.fromtimestamp(stat.st_mtime),
                size=stat.st_size,
            )
        )
    found.sort(key=lambda b: b.created, reverse=True)
    return found


def create_backup(
    world_dir: Path,
    instance_dir: Path,
    world_folder: str,
    keep: int = 10,
    on_progress: ProgressCallback | None = None,
    label: str = "",
    custom_root: str | Path | None = None,
) -> Backup:
    """Zip a world folder. Call with the server paused or stopped."""
    world_dir = Path(world_dir)
    if not world_dir.is_dir():
        raise BackupError(f"World folder not found: {world_dir}")

    destination_dir = backup_root(instance_dir, world_folder, custom_root)
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"Cannot write backups to {destination_dir}: {exc}") from exc

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label}" if label else ""
    destination = destination_dir / f"{stamp}{suffix}.zip"
    partial = destination.with_suffix(".zip.part")

    files = [
        p for p in world_dir.rglob("*") if p.is_file() and p.name not in SKIP_NAMES
    ]
    total = len(files) or 1

    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for index, item in enumerate(files, start=1):
                try:
                    archive.write(item, item.relative_to(world_dir))
                except (OSError, ValueError):
                    continue  # A file vanishing mid-backup shouldn't abort it.
                if on_progress and index % 200 == 0:
                    on_progress(f"Backing up... {index}/{total} files")
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(f"Could not write backup: {exc}") from exc

    partial.replace(destination)
    prune_backups(instance_dir, world_folder, keep, custom_root)

    stat = destination.stat()
    return Backup(
        path=destination,
        created=datetime.datetime.fromtimestamp(stat.st_mtime),
        size=stat.st_size,
    )


def prune_backups(
    instance_dir: Path, world_folder: str, keep: int, custom: str | Path | None = None
) -> int:
    """Delete all but the newest ``keep`` backups. Returns how many went."""
    if keep <= 0:
        return 0
    removed = 0
    for backup in list_backups(instance_dir, world_folder, custom)[keep:]:
        try:
            backup.path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def backup_running_server(
    server,
    world_dir: Path,
    instance_dir: Path,
    world_folder: str,
    keep: int = 10,
    on_progress: ProgressCallback | None = None,
    custom_root: str | Path | None = None,
) -> Backup:
    """Take a consistent backup of a live world.

    Saving is re-enabled in a finally block: an exception here must never leave
    a running server with ``save-off`` still in effect.
    """
    paused = False
    try:
        if server is not None and server.is_alive:
            if on_progress:
                on_progress("Flushing world to disk...")
            server.send("save-off")
            server.send("save-all flush")
            paused = True
            import time

            time.sleep(3)  # Give the flush a moment to land before copying.
        return create_backup(
            world_dir, instance_dir, world_folder, keep, on_progress, label="auto",
            custom_root=custom_root,
        )
    finally:
        if paused and server is not None and server.is_alive:
            try:
                server.send("save-on")
            except RuntimeError:
                pass


def restore_backup(
    backup: Backup,
    world_dir: Path,
    instance_dir: Path,
    world_folder: str,
    on_progress: ProgressCallback | None = None,
) -> Backup:
    """Replace a world with a backup, after safeguarding what's there now.

    The current world is backed up first, unconditionally. Restoring is the most
    destructive thing the app can do, and a mistaken restore should always be
    undoable.
    """
    world_dir = Path(world_dir)
    if not backup.path.is_file():
        raise BackupError(f"Backup file is missing: {backup.path}")

    if on_progress:
        on_progress("Backing up the current world first...")
    safety = create_backup(
        world_dir, instance_dir, world_folder, keep=0, on_progress=on_progress,
        label="before-restore",
    )

    if on_progress:
        on_progress("Clearing the current world...")
    for item in world_dir.iterdir():
        if item.name in SKIP_NAMES:
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError as exc:
            raise BackupError(f"Could not clear {item}: {exc}") from exc

    if on_progress:
        on_progress("Restoring...")
    try:
        with zipfile.ZipFile(backup.path) as archive:
            archive.extractall(world_dir)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(
            f"Restore failed: {exc}. Your previous world was saved to {safety.path.name}."
        ) from exc

    return safety
