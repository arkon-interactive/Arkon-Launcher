"""Checking GitHub for a newer release, and installing it on request.

Deliberately not silent. The check runs on its own, but nothing is downloaded
until the user says yes and nothing is executed until they say yes again - an
app that replaces itself without asking is an app you cannot trust with a server
that people are playing on.

Only the public releases API is used, so no token or login is involved.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__, paths

REPOSITORY = "arkon-interactive/Arkon-Launcher"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases"

# The installer asset. Matched loosely so a renamed or versioned file still
# resolves rather than the update silently finding nothing.
INSTALLER_PATTERN = re.compile(r"ArkonLauncher.*Setup.*\.exe$", re.I)

ProgressCallback = Callable[[str], None]


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    url: str
    asset_name: str = ""
    asset_url: str = ""
    asset_size: int = 0

    @property
    def has_installer(self) -> bool:
        return bool(self.asset_url)


def version_tuple(text: str) -> tuple[int, ...]:
    """Turn '0.5.0' or 'v0.5.0-beta' into something comparable."""
    cleaned = text.strip().lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(candidate: str, current: str = __version__) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def fetch_latest(timeout: float = 15.0) -> Release | None:
    """Ask GitHub for the newest release. None when offline or none published."""
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "User-Agent": "ArkonLauncher",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except (OSError, ValueError):
        return None

    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None

    release = Release(
        version=tag.lstrip("vV"),
        tag=tag,
        notes=str(data.get("body") or "").strip(),
        url=str(data.get("html_url") or RELEASES_PAGE),
    )

    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        if INSTALLER_PATTERN.search(name):
            release.asset_name = name
            release.asset_url = str(asset.get("browser_download_url") or "")
            release.asset_size = int(asset.get("size") or 0)
            break

    return release


def download_installer(
    release: Release, on_progress: ProgressCallback | None = None
) -> Path:
    """Fetch the installer into the app cache. Only call after consent."""
    if not release.has_installer:
        raise UpdateError("That release has no installer attached.")

    destination = paths.cache_dir() / release.asset_name
    partial = destination.with_suffix(destination.suffix + ".part")

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
                        f"Downloading update... {done * 100 // release.asset_size}%"
                    )
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"Could not download the update: {exc}") from exc

    if release.asset_size and partial.stat().st_size != release.asset_size:
        partial.unlink(missing_ok=True)
        raise UpdateError("The downloaded update was the wrong size; discarded it.")

    partial.replace(destination)
    return destination


def run_installer(installer: Path) -> None:
    """Launch the downloaded installer and leave it running.

    Detached, because the installer needs to replace this executable - it closes
    the running app itself via Inno's CloseApplications.
    """
    creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(
            [str(installer)],
            cwd=str(installer.parent),
            creationflags=creation_flags,
            close_fds=True,
        )
    except OSError as exc:
        raise UpdateError(f"Could not start the installer: {exc}") from exc
