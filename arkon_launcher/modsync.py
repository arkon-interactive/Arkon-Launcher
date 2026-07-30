"""Building a server-safe mods folder from a client modpack.

This is the module that decides whether the server boots at all. A CurseForge
instance's ``mods`` folder is built for a *client*, and three things in it will
stop a dedicated server:

1. **Client-only mods.** Sodium, Iris, Essential and friends declare
   ``environment: "client"`` and are dropped automatically.
2. **Mods that lie.** Plenty declare ``"*"`` but are really client-only. No
   static list can be complete for an arbitrary pack, so a seed list ships with
   the app and crash triage appends to a user list as it learns.
3. **Duplicate mod ids.** CurseForge can leave two versions of the same mod in
   place; Fabric will refuse to start or pick one unpredictably. We keep the
   highest version.

The result is mirrored with hardlinks rather than copies - a mods folder is
easily hundreds of megabytes, and it is rebuilt on every start so mod updates
from CurseForge are picked up automatically.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import paths

FABRIC_MANIFEST = "fabric.mod.json"


class Exclusion(str, Enum):
    """Why a jar didn't make it into the server's mods folder."""

    CLIENT_ENVIRONMENT = "declares environment: client"
    KNOWN_CLIENT_ONLY = "known client-only mod"
    USER_DISABLED = "disabled by you"
    SUPERSEDED = "older duplicate"
    UNREADABLE = "could not read fabric.mod.json"
    DEPENDENCY_MISSING = "needs a client-only mod"


# Satisfied by the loader or the game itself, never by a jar in the folder.
BUILTIN_MOD_IDS = frozenset({"minecraft", "java", "fabricloader", "fabric-loader"})


@dataclass
class ModJar:
    path: Path
    mod_id: str | None
    version: str | None
    environment: str
    display_name: str = ""
    depends: dict[str, object] = field(default_factory=dict)
    provides: list[str] = field(default_factory=list)
    nested_ids: set[str] = field(default_factory=set)
    excluded_by: Exclusion | None = None
    detail: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def included(self) -> bool:
        return self.excluded_by is None

    def supplied_ids(self) -> set[str]:
        """Every mod id this jar makes available, including bundled ones.

        Mods routinely ship their dependencies inside ``META-INF/jars``; Fabric
        API alone provides dozens of submodules that way. Ignoring them would
        make the dependency check hallucinate missing mods.
        """
        ids = set(self.nested_ids)
        ids.update(p for p in self.provides if isinstance(p, str))
        if self.mod_id:
            ids.add(self.mod_id)
        return ids


@dataclass
class SyncResult:
    included: list[ModJar] = field(default_factory=list)
    excluded: list[ModJar] = field(default_factory=list)
    linked: int = 0
    copied: int = 0
    removed: int = 0

    @property
    def total(self) -> int:
        return len(self.included) + len(self.excluded)

    def summary(self) -> str:
        return (
            f"{self.total} mods -> {len(self.included)} server mods "
            f"({len(self.excluded)} excluded)"
        )


# --- Reading mod metadata -----------------------------------------------------


def _parse_manifest(raw: bytes) -> dict:
    """Parse a fabric.mod.json the way Fabric's own loader tolerates them.

    Real-world manifests contain raw newlines inside description strings, which
    strict JSON rejects - ``strict=False`` accepts exactly those control
    characters. Comments and trailing commas are stripped as a second pass,
    since other packs contain those instead.
    """
    text = raw.decode("utf-8", errors="replace").lstrip("﻿")

    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass

    without_comments = re.sub(r"//[^\n\r]*", "", text)
    without_comments = re.sub(r"/\*.*?\*/", "", without_comments, flags=re.DOTALL)
    without_trailing_commas = re.sub(r",(\s*[}\]])", r"\1", without_comments)
    return json.loads(without_trailing_commas, strict=False)


def _collect_nested_ids(archive: zipfile.ZipFile, manifest: dict, depth: int = 0) -> set[str]:
    """Recursively read mod ids out of bundled ``META-INF/jars`` entries.

    Nested modules carry their own environment, and Fabric disables the
    client-only ones on a server, so those are not counted as available.
    """
    if depth > 4:
        return set()

    found: set[str] = set()
    for entry in manifest.get("jars") or []:
        name = entry.get("file") if isinstance(entry, dict) else None
        if not name:
            continue
        try:
            with archive.open(name) as raw:
                payload = raw.read()
            with zipfile.ZipFile(io.BytesIO(payload)) as nested:
                if FABRIC_MANIFEST not in nested.namelist():
                    continue
                nested_manifest = _parse_manifest(nested.read(FABRIC_MANIFEST))
                if str(nested_manifest.get("environment", "*")) == "client":
                    continue
                nested_id = nested_manifest.get("id")
                if nested_id:
                    found.add(str(nested_id))
                for provided in nested_manifest.get("provides") or []:
                    if isinstance(provided, str):
                        found.add(provided)
                found |= _collect_nested_ids(nested, nested_manifest, depth + 1)
        except (KeyError, OSError, zipfile.BadZipFile, json.JSONDecodeError, ValueError):
            continue
    return found


def read_mod_jar(jar_path: Path) -> ModJar:
    """Read one jar's identity. Never raises - unreadable jars are excluded."""
    jar_path = Path(jar_path)
    try:
        with zipfile.ZipFile(jar_path) as archive:
            if FABRIC_MANIFEST not in archive.namelist():
                return ModJar(
                    path=jar_path,
                    mod_id=None,
                    version=None,
                    environment="*",
                    excluded_by=Exclusion.UNREADABLE,
                    detail="no fabric.mod.json (not a Fabric mod?)",
                )
            manifest = _parse_manifest(archive.read(FABRIC_MANIFEST))
            nested_ids = _collect_nested_ids(archive, manifest)
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, ValueError) as exc:
        return ModJar(
            path=jar_path,
            mod_id=None,
            version=None,
            environment="*",
            excluded_by=Exclusion.UNREADABLE,
            detail=f"{type(exc).__name__}: {exc}",
        )

    depends = manifest.get("depends")
    provides = manifest.get("provides")

    return ModJar(
        path=jar_path,
        mod_id=manifest.get("id"),
        version=str(manifest.get("version")) if manifest.get("version") is not None else None,
        environment=str(manifest.get("environment", "*")),
        depends=depends if isinstance(depends, dict) else {},
        provides=[p for p in (provides or []) if isinstance(p, str)],
        nested_ids=nested_ids,
        display_name=str(manifest.get("name") or manifest.get("id") or jar_path.stem),
    )


# --- Version comparison -------------------------------------------------------

_VERSION_TOKEN = re.compile(r"(\d+)|([A-Za-z]+)")


def version_key(version: str | None) -> tuple:
    """Sortable key for the loose version strings mods actually use.

    Handles the shapes seen in practice - ``26.2-6.0.1``, ``26.2.0.5``,
    ``1.0.31+26.2-fabric`` - by comparing numeric runs numerically and letter
    runs lexically. Not semver, because mod versions frequently aren't.
    """
    if not version:
        return ()
    key: list[tuple[int, int, str]] = []
    for number, word in _VERSION_TOKEN.findall(version):
        if number:
            key.append((1, int(number), ""))
        else:
            # Pre-release words sort below numbers of the same position.
            key.append((0, 0, word.lower()))
    return tuple(key)


# --- Denylist -----------------------------------------------------------------


def _load_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def load_client_only_ids() -> set[str]:
    """Seed list shipped with the app, merged with what triage has learned."""
    seed = _load_json(paths.resource_path("data/client_only.json"))
    user = _load_json(paths.user_denylist_path())

    ids: set[str] = set()
    for source in (seed, user):
        for mod_id in source.get("client_only", []) or []:
            if isinstance(mod_id, str) and mod_id.strip():
                ids.add(mod_id.strip())
    return ids


def remember_client_only(mod_id: str) -> None:
    """Record a mod id that crash triage identified as client-only."""
    path = paths.user_denylist_path()
    data = _load_json(path)
    existing = [m for m in data.get("client_only", []) or [] if isinstance(m, str)]
    if mod_id in existing:
        return
    existing.append(mod_id)
    data["client_only"] = sorted(existing)
    data.setdefault(
        "_comment",
        "Mod ids Arkon Launcher determined to be client-only, usually from a crash.",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def forget_client_only(mod_id: str) -> None:
    """Undo a triage decision, so the user can re-enable a mod they want back."""
    path = paths.user_denylist_path()
    data = _load_json(path)
    remaining = [
        m for m in data.get("client_only", []) or [] if isinstance(m, str) and m != mod_id
    ]
    data["client_only"] = sorted(remaining)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


# --- Selection ----------------------------------------------------------------


def select_server_mods(
    mods_dir: Path,
    client_only_ids: set[str] | None = None,
    user_disabled_ids: set[str] | None = None,
    force_include_ids: set[str] | None = None,
) -> SyncResult:
    """Decide which jars belong on the server. Pure - touches no destination."""
    mods_dir = Path(mods_dir)
    client_only_ids = client_only_ids if client_only_ids is not None else load_client_only_ids()
    user_disabled_ids = user_disabled_ids or set()
    force_include_ids = force_include_ids or set()

    result = SyncResult()
    if not mods_dir.is_dir():
        return result

    # `.jar.disabled` is CurseForge's own convention for a switched-off mod.
    candidates = [read_mod_jar(p) for p in sorted(mods_dir.glob("*.jar"))]

    keepers: dict[str, ModJar] = {}
    for mod in candidates:
        if mod.excluded_by is not None:
            result.excluded.append(mod)
            continue

        forced = mod.mod_id in force_include_ids

        if mod.mod_id in user_disabled_ids:
            mod.excluded_by = Exclusion.USER_DISABLED
            result.excluded.append(mod)
            continue

        if not forced and mod.environment == "client":
            mod.excluded_by = Exclusion.CLIENT_ENVIRONMENT
            result.excluded.append(mod)
            continue

        if not forced and mod.mod_id in client_only_ids:
            mod.excluded_by = Exclusion.KNOWN_CLIENT_ONLY
            mod.detail = "declares both environments but is client-only in practice"
            result.excluded.append(mod)
            continue

        key = mod.mod_id or f"__anonymous__{mod.path.name}"
        rival = keepers.get(key)
        if rival is None:
            keepers[key] = mod
            continue

        # Duplicate mod id: keep the higher version, exclude the other.
        winner, loser = (
            (mod, rival)
            if version_key(mod.version) > version_key(rival.version)
            else (rival, mod)
        )
        loser.excluded_by = Exclusion.SUPERSEDED
        loser.detail = f"superseded by {winner.version} ({winner.name})"
        keepers[key] = winner
        result.excluded.append(loser)

    _repair_dependencies(keepers, result)

    result.included = sorted(keepers.values(), key=lambda m: m.name.lower())
    result.excluded.sort(key=lambda m: m.name.lower())
    return result


def _repair_dependencies(keepers: dict[str, ModJar], result: SyncResult) -> None:
    """Reconcile the filtered set with what mods actually require of each other.

    Filtering by environment is necessary but not sufficient: removing a mod can
    strand a server-side mod that hard-depends on it. Two different repairs are
    needed, and picking the wrong one for each case is what makes a server fail
    to boot:

    * The dependency was dropped by our **heuristic denylist**. That's a guess,
      and a hard dependency is a fact, so the guess loses - put it back.
    * The dependency declares ``environment: client``. Fabric discards those on a
      server no matter what we do, so the requirement can never be satisfied and
      the *dependent* has to go instead. Removing it can strand something else,
      so this runs to a fixpoint.
    """
    by_id: dict[str, ModJar] = {}
    for mod in result.excluded:
        if mod.mod_id and mod.mod_id not in by_id:
            by_id[mod.mod_id] = mod

    for _ in range(20):  # Fixpoint; the bound just guards against a cycle.
        available: set[str] = set(BUILTIN_MOD_IDS)
        for mod in keepers.values():
            available |= mod.supplied_ids()

        reinstated = False
        dropped = False

        for mod in list(keepers.values()):
            for dependency in mod.depends:
                if dependency in available:
                    continue

                candidate = by_id.get(dependency)

                if candidate is not None and candidate.excluded_by is Exclusion.KNOWN_CLIENT_ONLY:
                    # Our guess was wrong for this pack - a real mod needs it.
                    candidate.excluded_by = None
                    candidate.detail = f"required by {mod.mod_id or mod.name}"
                    keepers[candidate.mod_id or candidate.name] = candidate
                    result.excluded.remove(candidate)
                    reinstated = True
                    break

                # Unsatisfiable on a server: drop the dependent instead.
                reason = (
                    f"needs {dependency}, which is client-only"
                    if candidate is not None
                    else f"needs {dependency}, which is not installed"
                )
                mod.excluded_by = Exclusion.DEPENDENCY_MISSING
                mod.detail = reason
                keepers.pop(mod.mod_id or mod.name, None)
                result.excluded.append(mod)
                if mod.mod_id:
                    by_id.setdefault(mod.mod_id, mod)
                dropped = True
                break

            if reinstated or dropped:
                break

        if not (reinstated or dropped):
            return


# --- Mirroring ----------------------------------------------------------------


def _mirror_file(source: Path, destination: Path) -> str:
    """Hardlink if possible, else copy. Returns 'linked', 'copied' or 'current'."""
    if destination.exists():
        try:
            src_stat, dst_stat = source.stat(), destination.stat()
            same_content = (
                src_stat.st_size == dst_stat.st_size
                and int(src_stat.st_mtime) == int(dst_stat.st_mtime)
            )
            if same_content:
                return "current"
        except OSError:
            pass
        try:
            destination.unlink()
        except OSError:
            pass

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
        return "linked"
    except OSError:
        # Different volume, or a filesystem without hardlinks.
        shutil.copy2(source, destination)
        return "copied"


def mirror_mods(result: SyncResult, destination: Path) -> SyncResult:
    """Make ``destination`` contain exactly the included jars, and nothing else."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    wanted = {mod.name for mod in result.included}

    for stale in destination.glob("*.jar"):
        if stale.name not in wanted:
            try:
                stale.unlink()
                result.removed += 1
            except OSError:
                pass

    for mod in result.included:
        outcome = _mirror_file(mod.path, destination / mod.name)
        if outcome == "linked":
            result.linked += 1
        elif outcome == "copied":
            result.copied += 1

    return result


def mirror_tree(source: Path, destination: Path) -> int:
    """Mirror a whole directory (config, defaultconfigs) by hardlink where possible."""
    source, destination = Path(source), Path(destination)
    if not source.is_dir():
        return 0

    count = 0
    for item in source.rglob("*"):
        if item.is_dir():
            continue
        target = destination / item.relative_to(source)
        try:
            _mirror_file(item, target)
            count += 1
        except OSError:
            continue
    return count


# --- World junction -----------------------------------------------------------


class JunctionError(Exception):
    pass


def _is_junction_to(link: Path, target: Path) -> bool:
    if not link.exists():
        return False
    try:
        return link.resolve() == Path(target).resolve()
    except OSError:
        return False


def remove_legacy_world_link(server_dir: Path, link_name: str = "world") -> bool:
    """Delete the ``world`` junction earlier versions created. True if one went.

    Arkon Launcher used to point the server at the save folder with a directory
    junction. That works until Windows decides not to follow it: **Redirection
    Guard** refuses to traverse a junction created by a lower-integrity user,
    raising ``[WinError 448] The path cannot be traversed because it contains an
    untrusted mount point``. Installing the app to Program Files makes that easy
    to trigger, and it is not something the app can talk Windows out of.

    The server accepts ``--universe`` and ``--world`` instead, which opens the
    save folder directly. No reparse point, so nothing to distrust - and the
    world is still shared in place, which was the only reason for the junction.

    Deleting the link never touches the world it pointed at: ``unlink`` on a
    junction removes the reparse point itself.
    """
    link = Path(server_dir) / link_name
    try:
        if not (link.is_symlink() or os.path.isjunction(link)):
            return False
    except OSError:
        return False

    try:
        link.unlink(missing_ok=True)
        return True
    except OSError:
        try:
            # A junction can also be removed as an empty directory.
            os.rmdir(link)
            return True
        except OSError:
            return False


# Config files are named by convention rather than by anything declared in the
# mod, so matching them is a heuristic: `<modid>.json`, `<modid>-common.toml`,
# a `<modid>/` folder, and so on.
CONFIG_SUFFIXES = {
    ".json", ".json5", ".toml", ".cfg", ".conf", ".properties",
    ".yaml", ".yml", ".txt", ".snbt", ".ini",
}


def duplicates_in(mods: list[ModJar]) -> dict[str, list[ModJar]]:
    """Mod ids appearing more than once, newest version first.

    Takes an already-read list rather than re-scanning: parsing a jar means
    opening it and every jar nested inside it, so doing that twice over a
    140-mod pack is several seconds of pointless work.

    Fabric refuses to start with two jars claiming the same id, so duplicates
    are not untidiness - they stop the server.
    """
    by_id: dict[str, list[ModJar]] = {}
    for mod in mods:
        if not mod.mod_id or mod.excluded_by is Exclusion.UNREADABLE:
            continue
        by_id.setdefault(mod.mod_id, []).append(mod)

    return {
        mod_id: sorted(jars, key=lambda m: version_key(m.version), reverse=True)
        for mod_id, jars in by_id.items()
        if len(jars) > 1
    }


def find_duplicates(mods_dir: Path) -> dict[str, list[ModJar]]:
    """Convenience wrapper that reads the folder first."""
    mods_dir = Path(mods_dir)
    if not mods_dir.is_dir():
        return {}
    return duplicates_in([read_mod_jar(p) for p in sorted(mods_dir.glob("*.jar"))])


DISABLED_SUFFIX = ".disabled"


def disable_jar(jar_path: Path) -> Path:
    """Rename a jar out of the way rather than deleting it.

    `.jar.disabled` is CurseForge's own convention, so the app understands it
    and the file can be brought back by renaming - which matters when the guess
    about which version to keep turns out to be wrong.
    """
    jar_path = Path(jar_path)
    destination = jar_path.with_suffix(jar_path.suffix + DISABLED_SUFFIX)
    counter = 1
    while destination.exists():
        destination = jar_path.with_suffix(f"{jar_path.suffix}{DISABLED_SUFFIX}.{counter}")
        counter += 1
    jar_path.rename(destination)
    return destination


def find_mod_configs(mod_id: str, config_dir: Path, limit: int = 12) -> list[Path]:
    """Config files that appear to belong to a mod.

    Matched on filename, since nothing in a Fabric mod declares where its config
    lives. A mod whose config is named after something other than its id will
    not be found - which is why the UI says "looks like" rather than asserting.
    """
    config_dir = Path(config_dir)
    if not mod_id or not config_dir.is_dir():
        return []

    needle = mod_id.lower()
    found: list[Path] = []

    for entry in sorted(config_dir.iterdir()):
        if len(found) >= limit:
            break
        name = entry.name.lower()
        if entry.is_dir():
            if name == needle or name.startswith(needle):
                for child in sorted(entry.rglob("*")):
                    if child.is_file() and child.suffix.lower() in CONFIG_SUFFIXES:
                        found.append(child)
                        if len(found) >= limit:
                            break
        elif entry.suffix.lower() in CONFIG_SUFFIXES:
            stem = entry.stem.lower()
            if stem == needle or stem.startswith(f"{needle}-") or stem.startswith(f"{needle}_"):
                found.append(entry)

    return found


def world_container(save_dir: Path) -> tuple[Path, str]:
    """Split a save path into the (universe, world name) the server wants."""
    save_dir = Path(save_dir)
    return save_dir.parent, save_dir.name
