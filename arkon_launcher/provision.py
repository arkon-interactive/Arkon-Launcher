"""Getting a server directory to the point where java can be run in it.

Four jobs: find a JRE, fetch the Fabric server launcher, record EULA acceptance,
and write ``server.properties``. Every version string comes from the instance
itself, so this works on whatever Minecraft and Fabric version the host happens
to run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths
from .instances import Instance

FABRIC_META = "https://meta.fabricmc.net/v2/versions"
EULA_URL = "https://aka.ms/MinecraftEULA"

ProgressCallback = Callable[[int, int], None]


class ProvisionError(Exception):
    pass


# --- Java ---------------------------------------------------------------------


@dataclass
class JavaRuntime:
    executable: Path
    version: str
    major: int

    @property
    def display(self) -> str:
        return f"Java {self.major} ({self.version})"


_VERSION_LINE = re.compile(r'version "([^"]+)"')


def probe_java(executable: Path) -> JavaRuntime | None:
    """Run ``java -version`` and parse it. None if it isn't a working JRE."""
    try:
        completed = subprocess.run(
            [str(executable), "-version"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None

    # `java -version` writes to stderr.
    output = f"{completed.stderr}\n{completed.stdout}"
    match = _VERSION_LINE.search(output)
    if not match:
        return None

    version = match.group(1)
    head = version.split("-")[0].split(".")
    try:
        major = int(head[0])
        if major == 1 and len(head) > 1:  # Legacy "1.8.0_402" form.
            major = int(head[1])
    except (ValueError, IndexError):
        return None

    return JavaRuntime(executable=Path(executable), version=version, major=major)


def required_java_major(instance: Instance) -> int | None:
    """What this Minecraft version asks for, per its own version manifest."""
    version_json = instance.version_json_path()
    if version_json is None:
        return None
    try:
        with open(version_json, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    java_version = data.get("javaVersion") or {}
    major = java_version.get("majorVersion")
    return int(major) if isinstance(major, int) else None


def _preferred_component(instance: Instance) -> str | None:
    version_json = instance.version_json_path()
    if version_json is None:
        return None
    try:
        with open(version_json, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return (data.get("javaVersion") or {}).get("component")


def find_java_runtimes(instance: Instance) -> list[JavaRuntime]:
    """Locate usable JREs, best first.

    CurseForge names its runtime folders after Mojang's component
    (``java-runtime-epsilon``, ``-gamma``, ``-delta``...), which varies by
    Minecraft version - hence a glob plus a preference, never a fixed name.
    """
    candidates: list[Path] = []

    if instance.curseforge_root:
        java_root = instance.curseforge_root / "Install" / "java"
        preferred = _preferred_component(instance)
        if preferred:
            exact = java_root / preferred / "bin" / "java.exe"
            if exact.is_file():
                candidates.append(exact)
        if java_root.is_dir():
            candidates.extend(sorted(java_root.glob("*/bin/java.exe")))

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(Path(java_home) / "bin" / "java.exe")

    found_on_path = shutil_which("java")
    if found_on_path:
        candidates.append(Path(found_on_path))

    runtimes: list[JavaRuntime] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        runtime = probe_java(resolved)
        if runtime:
            runtimes.append(runtime)
    return runtimes


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def select_java(instance: Instance) -> JavaRuntime:
    """Pick a JRE new enough for this Minecraft version, or explain what's wrong."""
    runtimes = find_java_runtimes(instance)
    if not runtimes:
        raise ProvisionError(
            "No Java runtime found. CurseForge normally installs one under "
            "Install\\java; you can also install a JRE and set JAVA_HOME."
        )

    minimum = required_java_major(instance) or 21
    usable = [r for r in runtimes if r.major >= minimum]
    if not usable:
        best = max(runtimes, key=lambda r: r.major)
        raise ProvisionError(
            f"Minecraft {instance.mc_version} needs Java {minimum} or newer, but the "
            f"newest one found is {best.display} at {best.executable}."
        )
    return max(usable, key=lambda r: r.major)


# --- Server jar ---------------------------------------------------------------


def _download(url: str, destination: Path, on_progress: ProgressCallback | None = None) -> Path:
    """Download to a temp file and rename, so a cancelled run leaves no half-jar."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")

    request = urllib.request.Request(url, headers={"User-Agent": "ArkonLauncher"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(partial, "wb") as handle:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ProvisionError(f"Download failed ({url}): {exc}") from exc

    partial.replace(destination)
    return destination


def _fetch_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "ArkonLauncher"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def latest_fabric_installer() -> str:
    versions = _fetch_json(f"{FABRIC_META}/installer")
    if not versions:
        raise ProvisionError("Fabric returned no installer versions.")
    return versions[0]["version"]


def ensure_server_jar(
    instance: Instance, on_progress: ProgressCallback | None = None
) -> Path:
    """Return a Fabric server launcher jar for this instance's exact versions.

    The launcher jar self-provisions the loader, its libraries and the vanilla
    server, so there is no interactive installer step. Cached per
    Minecraft+loader pair and shared across instances and worlds.
    """
    if not instance.loader_version:
        raise ProvisionError(
            f"'{instance.name}' does not record a Fabric loader version."
        )

    cached = paths.cache_dir() / (
        f"fabric-server-{instance.mc_version}-{instance.loader_version}.jar"
    )
    if cached.is_file() and cached.stat().st_size > 0:
        return cached

    installer = latest_fabric_installer()
    url = (
        f"{FABRIC_META}/loader/{instance.mc_version}/"
        f"{instance.loader_version}/{installer}/server/jar"
    )
    return _download(url, cached, on_progress)


def vanilla_server_download(instance: Instance) -> tuple[str, int, str] | None:
    """(url, size, sha1) for the vanilla server jar, read from the local manifest.

    CurseForge already downloaded the version JSON for the client, and it names
    the matching server jar. Using it means no version-manifest lookup and no way
    to end up with a server that doesn't match the client.
    """
    version_json = instance.version_json_path()
    if version_json is None:
        return None
    try:
        with open(version_json, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None

    server = (data.get("downloads") or {}).get("server") or {}
    url = server.get("url")
    if not url:
        return None
    return url, int(server.get("size") or 0), str(server.get("sha1") or "")


def ensure_vanilla_server_jar(
    instance: Instance, on_progress: ProgressCallback | None = None
) -> Path:
    """Fallback path used when Fabric's meta service can't be reached."""
    info = vanilla_server_download(instance)
    if info is None:
        raise ProvisionError(
            f"No local version manifest for Minecraft {instance.mc_version}, so the "
            f"server jar URL could not be determined offline."
        )
    url, size, sha1 = info

    destination = paths.cache_dir() / f"minecraft-server-{instance.mc_version}.jar"
    if destination.is_file() and (not size or destination.stat().st_size == size):
        return destination

    _download(url, destination, on_progress)

    if size and destination.stat().st_size != size:
        destination.unlink(missing_ok=True)
        raise ProvisionError("Downloaded server jar has the wrong size; discarded it.")
    if sha1:
        digest = hashlib.sha1()
        with open(destination, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha1:
            destination.unlink(missing_ok=True)
            raise ProvisionError("Downloaded server jar failed its checksum; discarded it.")

    return destination


# --- EULA ---------------------------------------------------------------------


def eula_accepted(server_dir: Path) -> bool:
    path = Path(server_dir) / "eula.txt"
    if not path.is_file():
        return False
    try:
        return re.search(
            r"^\s*eula\s*=\s*true\s*$", path.read_text(encoding="utf-8"), re.I | re.M
        ) is not None
    except OSError:
        return False


def write_eula(server_dir: Path, accepted: bool) -> Path:
    """Record the user's EULA decision.

    Only ever called with what the user actually chose. The launcher does not
    accept Mojang's terms on their behalf - the checkbox starts unticked.
    """
    server_dir = Path(server_dir)
    server_dir.mkdir(parents=True, exist_ok=True)
    path = server_dir / "eula.txt"
    path.write_text(
        f"# Minecraft EULA: {EULA_URL}\n"
        f"# Accepted by the user through Arkon Launcher.\n"
        f"eula={'true' if accepted else 'false'}\n",
        encoding="utf-8",
    )
    return path


# --- server.properties --------------------------------------------------------


def read_properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_properties(path: Path, values: dict[str, str]) -> None:
    lines = ["# Minecraft server properties", "# Managed by Arkon Launcher"]
    lines.extend(f"{key}={value}" for key, value in sorted(values.items()))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_server_properties(
    server_dir: Path,
    world_name: str,
    port: int = 25565,
    overrides: dict[str, str] | None = None,
    level_name: str = "world",
) -> dict[str, str]:
    """Create or top up ``server.properties``, never clobbering hand edits.

    Existing keys are left exactly as the user left them; only missing ones are
    filled in. ``level-name`` and ``server-port`` are the two the launcher owns,
    because they have to match the junction and the connection panel.
    """
    server_dir = Path(server_dir)
    server_dir.mkdir(parents=True, exist_ok=True)
    path = server_dir / "server.properties"

    defaults = {
        "level-name": level_name,
        "server-port": str(port),
        "query.port": str(port),
        "online-mode": "true",
        "max-players": "10",
        "view-distance": "10",
        "simulation-distance": "8",
        "motd": f"{world_name} - hosted with Arkon Launcher",
        "enable-command-block": "true",
        "spawn-protection": "0",
        "white-list": "false",
        "sync-chunk-writes": "false",
    }
    if overrides:
        defaults.update(overrides)

    values = read_properties(path)
    for key, value in defaults.items():
        values.setdefault(key, value)

    # The launcher owns these two regardless of what was there before: the level
    # name has to match the world the server is actually told to open, and the
    # port has to match what the Connection tab advertises.
    values["level-name"] = level_name
    values["server-port"] = str(port)

    write_properties(path, values)
    return values
