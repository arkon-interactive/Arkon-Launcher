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

import hashlib
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


# --- CurseForge updates ------------------------------------------------------
#
# The CurseForge app records, for every mod it installed, both the file that is
# installed and the newest file available - in minecraftinstance.json, with a
# direct download URL. That means updates for the whole pack can be found with
# no API key and no network call at all.
#
# The catch is freshness: latestFile is only as current as the last time the
# CurseForge app refreshed the instance. It is a good signal, not an oracle, and
# the UI says so.


@dataclass
class CurseForgeUpdate:
    addon_id: int
    name: str
    installed_file: str
    installed_id: int
    latest_file: str
    latest_id: int
    download_url: str
    size: int
    sha1: str
    jar_path: Path | None = None

    @property
    def has_download(self) -> bool:
        return bool(self.download_url)


def _hash_of(file_entry: dict, kind: int = 1) -> str:
    """CurseForge hash types: 1 is SHA1, 2 is MD5."""
    for entry in file_entry.get("hashes") or []:
        if entry.get("type") == kind:
            return str(entry.get("value") or "")
    return ""


def read_instance_manifest(instance_dir: Path) -> dict:
    try:
        with open(Path(instance_dir) / "minecraftinstance.json", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def curseforge_updates(
    instance_dir: Path, mods_dir: Path, require_present: bool = True
) -> list[CurseForgeUpdate]:
    """Mods CurseForge believes have a newer file than the one installed.

    ``require_present`` skips entries whose jar is not actually in the folder.
    The manifest can list a mod that has since been removed by hand, and
    "updating" that would silently reinstall something deliberately deleted -
    which is not an update, it is a resurrection.
    """
    manifest = read_instance_manifest(instance_dir)
    mods_dir = Path(mods_dir)
    updates: list[CurseForgeUpdate] = []

    for addon in manifest.get("installedAddons") or []:
        installed = addon.get("installedFile") or {}
        latest = addon.get("latestFile") or {}
        if not installed or not latest:
            continue

        installed_id = installed.get("id")
        latest_id = latest.get("id")
        if not installed_id or not latest_id or installed_id == latest_id:
            continue

        jar_name = installed.get("fileName") or installed.get("fileNameOnDisk") or ""
        jar_path = mods_dir / jar_name if jar_name else None
        present = bool(jar_path and jar_path.is_file())
        if require_present and not present:
            continue

        updates.append(
            CurseForgeUpdate(
                addon_id=int(addon.get("addonID") or 0),
                name=str(addon.get("name") or jar_name or "Unknown mod"),
                installed_file=jar_name,
                installed_id=int(installed_id),
                latest_file=str(latest.get("fileName") or ""),
                latest_id=int(latest_id),
                download_url=str(latest.get("downloadUrl") or ""),
                size=int(latest.get("fileLength") or 0),
                sha1=_hash_of(latest, 1),
                jar_path=jar_path if jar_path and jar_path.is_file() else None,
            )
        )

    updates.sort(key=lambda u: u.name.lower())
    return updates


def apply_curseforge_update(
    update: CurseForgeUpdate,
    instance_dir: Path,
    mods_dir: Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Download the newer jar, swap it in, and keep the manifest honest.

    The manifest is updated too, because CurseForge reads it to decide what is
    installed - leaving it stale would have the app offer the same update
    forever, or quietly put the old file back. It is backed up first.
    """
    mods_dir = Path(mods_dir)
    if not update.has_download:
        raise ModUpdateError(
            f"CurseForge did not provide a download link for {update.name}. "
            f"Its author may have disabled third-party downloads."
        )

    destination = mods_dir / (update.latest_file or f"{update.addon_id}.jar")
    partial = destination.with_suffix(".jar.part")

    request = urllib.request.Request(
        update.download_url, headers={"User-Agent": "ArkonLauncher"}
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response, open(
            partial, "wb"
        ) as out:
            done = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if on_progress and update.size:
                    on_progress(f"{update.name}... {done * 100 // update.size}%")
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ModUpdateError(f"Could not download {update.name}: {exc}") from exc

    if update.size and partial.stat().st_size != update.size:
        partial.unlink(missing_ok=True)
        raise ModUpdateError(f"{update.name} downloaded at the wrong size; discarded.")

    if update.sha1:
        digest = hashlib.sha1()
        with open(partial, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest().lower() != update.sha1.lower():
            partial.unlink(missing_ok=True)
            raise ModUpdateError(f"{update.name} failed its checksum; discarded.")

    # Only remove the old jar once the new one is verified.
    if update.jar_path and update.jar_path.is_file():
        if update.jar_path.resolve() != destination.resolve():
            try:
                update.jar_path.unlink()
            except OSError as exc:
                partial.unlink(missing_ok=True)
                raise ModUpdateError(
                    f"Could not remove {update.jar_path.name} ({exc}). "
                    f"Is the server or Minecraft running?"
                ) from exc

    partial.replace(destination)
    _mark_installed(instance_dir, update)
    return destination


def _mark_installed(instance_dir: Path, update: CurseForgeUpdate) -> None:
    """Record the new file as the installed one in CurseForge's manifest."""
    path = Path(instance_dir) / "minecraftinstance.json"
    manifest = read_instance_manifest(instance_dir)
    if not manifest:
        return

    changed = False
    for addon in manifest.get("installedAddons") or []:
        if int(addon.get("addonID") or 0) != update.addon_id:
            continue
        latest = addon.get("latestFile")
        if latest:
            addon["installedFile"] = latest
            addon["fileNameOnDisk"] = latest.get("fileName") or addon.get("fileNameOnDisk")
            changed = True
        break

    if not changed:
        return

    backup = path.with_suffix(".json.arkonbak")
    try:
        if path.is_file() and not backup.is_file():
            backup.write_bytes(path.read_bytes())
        temporary = path.with_suffix(".json.tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
        temporary.replace(path)
    except OSError:
        # A stale manifest is survivable; a broken one is not, so leave it be.
        pass


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
