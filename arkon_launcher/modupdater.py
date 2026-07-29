"""Keeping first-party mods up to date from their GitHub releases.

Only mods listed in ``TRACKED`` are checked - this is not a general mod updater,
and it will never touch something it was not told about. The installed version
comes from the jar's own ``fabric.mod.json``, so it stays right even if the file
has been renamed.

The mods folder edited is the **instance's**, not the server's mirror: the
mirror is rebuilt from the instance on every start, so replacing a jar there
would be undone by the next launch.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .modsync import read_mod_jar
from .updater import version_tuple

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class TrackedMod:
    mod_id: str
    repository: str
    display_name: str


# First-party mods, checked against their own repositories.
TRACKED: tuple[TrackedMod, ...] = (
    TrackedMod("arkonessentials", "arkon-interactive/Arkon-Essentials", "Arkon Essentials"),
)

# A release usually carries more than the mod jar. Sources and javadoc jars
# contain no compiled classes, so installing one would break the pack in a
# thoroughly confusing way.
EXCLUDED_ASSET = re.compile(r"-(sources|javadoc|dev|api|slim)\.jar$", re.I)


class ModUpdateError(Exception):
    pass


@dataclass
class ModRelease:
    mod: TrackedMod
    version: str
    tag: str
    notes: str
    url: str
    asset_name: str
    asset_url: str
    asset_size: int


def tracked_for(mod_id: str) -> TrackedMod | None:
    for entry in TRACKED:
        if entry.mod_id == mod_id:
            return entry
    return None


def installed_jars(mods_dir: Path) -> dict[str, tuple[Path, str]]:
    """mod id -> (jar path, version) for every tracked mod that is installed."""
    found: dict[str, tuple[Path, str]] = {}
    mods_dir = Path(mods_dir)
    if not mods_dir.is_dir():
        return found

    wanted = {entry.mod_id for entry in TRACKED}
    for jar in mods_dir.glob("*.jar"):
        mod = read_mod_jar(jar)
        if mod.mod_id in wanted and mod.version:
            found[mod.mod_id] = (jar, mod.version)
    return found


def latest_release(mod: TrackedMod, timeout: float = 15.0) -> ModRelease | None:
    """Newest release for a tracked mod, or None when offline or unreleased."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{mod.repository}/releases/latest",
        headers={"User-Agent": "ArkonLauncher", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except (OSError, ValueError):
        return None

    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None

    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        if not name.lower().endswith(".jar") or EXCLUDED_ASSET.search(name):
            continue
        return ModRelease(
            mod=mod,
            version=tag.lstrip("vV"),
            tag=tag,
            notes=str(data.get("body") or "").strip(),
            url=str(data.get("html_url") or ""),
            asset_name=name,
            asset_url=str(asset.get("browser_download_url") or ""),
            asset_size=int(asset.get("size") or 0),
        )
    return None


def check_for_updates(mods_dir: Path) -> list[tuple[ModRelease, Path, str]]:
    """(release, installed jar, installed version) for anything out of date."""
    updates: list[tuple[ModRelease, Path, str]] = []
    for mod_id, (jar, version) in installed_jars(mods_dir).items():
        mod = tracked_for(mod_id)
        if mod is None:
            continue
        release = latest_release(mod)
        if release and version_tuple(release.version) > version_tuple(version):
            updates.append((release, jar, version))
    return updates


def install_update(
    release: ModRelease,
    old_jar: Path,
    mods_dir: Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Download the new jar and replace the old one.

    Downloaded to a temporary name and size-checked before anything is removed,
    so a failed download leaves the working mod in place rather than a pack with
    a hole in it.
    """
    mods_dir = Path(mods_dir)
    destination = mods_dir / release.asset_name
    partial = destination.with_suffix(".jar.part")

    request = urllib.request.Request(
        release.asset_url, headers={"User-Agent": "ArkonLauncher"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(
            partial, "wb"
        ) as out:
            done = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if on_progress and release.asset_size:
                    on_progress(
                        f"Downloading {release.mod.display_name}... "
                        f"{done * 100 // release.asset_size}%"
                    )
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ModUpdateError(f"Could not download the update: {exc}") from exc

    if release.asset_size and partial.stat().st_size != release.asset_size:
        partial.unlink(missing_ok=True)
        raise ModUpdateError("The downloaded jar was the wrong size; discarded it.")

    # Sanity check: a jar is a zip, and a mod jar has a fabric.mod.json.
    try:
        import zipfile

        with zipfile.ZipFile(partial) as archive:
            if "fabric.mod.json" not in archive.namelist():
                raise ModUpdateError(
                    "The downloaded jar has no fabric.mod.json, so it is not a "
                    "Fabric mod. Nothing was changed."
                )
    except (OSError, Exception) as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, ModUpdateError):
            raise
        raise ModUpdateError(f"The downloaded jar could not be read: {exc}") from exc

    # Only now is it safe to remove the old one.
    old_path = Path(old_jar)
    if old_path.is_file() and old_path.resolve() != destination.resolve():
        try:
            old_path.unlink()
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise ModUpdateError(
                f"Could not remove the old jar ({exc}). Is the server running?"
            ) from exc

    partial.replace(destination)
    return destination
