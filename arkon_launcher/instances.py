"""Finding CurseForge instances and reading what the launcher needs from them.

Nothing here may assume the developer's machine. The CurseForge root, the drive,
the instance name, and the Minecraft/loader versions are all discovered, because
the machine that runs this is not the machine it was written on. Auto-detection
finding nothing is a normal outcome - the UI always offers a folder picker.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

INSTANCE_MANIFEST = "minecraftinstance.json"

# CurseForge's own logs mention the configured instance root, which is the only
# machine-readable trace of a relocated install we can find without poking at its
# Electron LevelDB. Best-effort only.
_INSTANCES_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\"'<>|\r\n]*?[Ii]nstances")


class InstanceError(Exception):
    """Raised when a folder isn't a usable CurseForge instance."""


@dataclass(frozen=True)
class Instance:
    directory: Path
    name: str
    mc_version: str
    loader_family: str | None
    loader_version: str | None
    curseforge_root: Path | None

    @property
    def is_fabric(self) -> bool:
        return self.loader_family == "fabric"

    @property
    def mods_dir(self) -> Path:
        return self.directory / "mods"

    @property
    def saves_dir(self) -> Path:
        return self.directory / "saves"

    @property
    def config_dir(self) -> Path:
        return self.directory / "config"

    @property
    def usercache_path(self) -> Path:
        return self.directory / "usercache.json"

    @property
    def java_args_override(self) -> str | None:
        """The client's JVM args, reused as a starting point for the server's."""
        return _read_manifest(self.directory).get("javaArgsOverride")

    def version_json_path(self) -> Path | None:
        """``Install/versions/<mc>/<mc>.json`` - holds the server jar URL and the
        Java component this Minecraft version expects."""
        if self.curseforge_root is None:
            return None
        candidate = (
            self.curseforge_root / "Install" / "versions" / self.mc_version / f"{self.mc_version}.json"
        )
        return candidate if candidate.is_file() else None


def _read_manifest(directory: Path) -> dict:
    path = Path(directory) / INSTANCE_MANIFEST
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise InstanceError(f"{path} not found - this is not a CurseForge instance.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise InstanceError(f"Could not read {path}: {exc}") from exc


def _curseforge_root(instance_dir: Path) -> Path | None:
    """Walk up from ``.../minecraft/Instances/<name>`` to ``.../minecraft``.

    Derived rather than assumed, so a CurseForge install on another drive still
    resolves its bundled Java and version metadata.
    """
    for ancestor in instance_dir.parents:
        if (ancestor / "Install" / "versions").is_dir():
            return ancestor
    return None


def _parse_loader(manifest: dict) -> tuple[str | None, str | None]:
    """Return (family, version), e.g. ``("fabric", "0.19.3")``.

    CurseForge stores the loader version in a field called ``forgeVersion``
    whatever the loader actually is; the family only shows up in ``name``.
    """
    loader = manifest.get("baseModLoader") or {}
    name = loader.get("name") or ""
    version = loader.get("forgeVersion") or None

    family = None
    for known in ("neoforge", "fabric", "quilt", "forge"):
        if name.lower().startswith(known):
            family = known
            break

    if family is None and name:
        family = name.split("-", 1)[0].lower() or None

    return family, version


def load_instance(directory: str | os.PathLike[str]) -> Instance:
    """Read one instance folder. Raises InstanceError if it isn't one."""
    directory = Path(directory).resolve()
    manifest = _read_manifest(directory)
    family, loader_version = _parse_loader(manifest)

    mc_version = manifest.get("gameVersion") or ""
    if not mc_version:
        raise InstanceError(f"{directory / INSTANCE_MANIFEST} has no gameVersion.")

    return Instance(
        directory=directory,
        name=manifest.get("name") or directory.name,
        mc_version=mc_version,
        loader_family=family,
        loader_version=loader_version,
        curseforge_root=_curseforge_root(directory),
    )


def _curseforge_log_roots() -> list[Path]:
    """Scrape the newest CurseForge logs for a configured instance root."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []

    logs_dir = Path(appdata) / "CurseForge" / "logs"
    if not logs_dir.is_dir():
        return []

    try:
        sessions = sorted(
            (d for d in logs_dir.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )[:5]
    except OSError:
        return []

    found: list[Path] = []
    for session in sessions:
        for log in session.glob("main-*.log"):
            try:
                text = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in _INSTANCES_PATH_RE.findall(text):
                candidate = Path(match)
                if candidate.is_dir() and candidate not in found:
                    found.append(candidate)
    return found


def candidate_instance_roots() -> list[Path]:
    """Folders that may contain instance subfolders, best guess first."""
    roots: list[Path] = []

    def add(path: Path | None) -> None:
        if path and path.is_dir() and path not in roots:
            roots.append(path)

    home = Path.home()
    add(home / "curseforge" / "minecraft" / "Instances")
    add(home / "Documents" / "curseforge" / "minecraft" / "Instances")
    add(home / "OneDrive" / "Documents" / "curseforge" / "minecraft" / "Instances")

    for root in _curseforge_log_roots():
        add(root)

    # A second drive is common for large modpack libraries.
    for letter in "DEFGH":
        add(Path(f"{letter}:/curseforge/minecraft/Instances"))

    return roots


def find_instances(extra_roots: list[Path] | None = None) -> list[Instance]:
    """Scan the candidate roots. Returns [] rather than raising when none exist."""
    roots = list(extra_roots or []) + candidate_instance_roots()

    instances: list[Instance] = []
    seen: set[Path] = set()

    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue

        # A root may itself be a single instance folder, e.g. from "Browse...".
        candidates = [root] if (root / INSTANCE_MANIFEST).is_file() else sorted(
            child for child in root.iterdir() if child.is_dir()
        )

        for candidate in candidates:
            if not (candidate / INSTANCE_MANIFEST).is_file():
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                instances.append(load_instance(resolved))
            except InstanceError:
                continue  # Unreadable manifest - not worth failing the whole scan.

    return instances


def describe_unsupported(instance: Instance) -> str | None:
    """Explain why an instance can't be served, or None when it's fine.

    Names the loader that was actually found - "this is a Forge pack" is far more
    useful than "unsupported".
    """
    if instance.is_fabric:
        return None
    if instance.loader_family is None:
        return (
            f"'{instance.name}' has no mod loader recorded, so it looks like a "
            f"vanilla instance. Arkon Launcher currently supports Fabric packs."
        )
    return (
        f"'{instance.name}' is a {instance.loader_family.capitalize()} pack. "
        f"Arkon Launcher currently supports Fabric packs only."
    )
