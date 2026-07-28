"""Player head images, fetched from Mojang and cached on disk.

The route is entirely first-party: a UUID goes to Mojang's session server, which
returns a base64 profile containing a URL on ``textures.minecraft.net``; that PNG
is the player's skin, and the head is the 8x8 region at (8,8) with the "hat"
overlay at (40,8) composited on top.

No third-party head-render service is involved, and the only identifier sent is
the UUID the game already sends. Everything is cached, so a given player is
looked up once; if the network is unavailable the caller gets ``None`` and shows
a placeholder instead.
"""

from __future__ import annotations

import base64
import json
import urllib.request
from pathlib import Path

from . import paths

SESSION_PROFILE = "https://sessionserver.mojang.com/session/minecraft/profile/{uuid}"
HEAD_SIZE = 8  # Pixels in the skin; scaled up for display.


class AvatarError(Exception):
    pass


def cache_dir() -> Path:
    directory = paths.cache_dir() / "avatars"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def cached_head(uuid: str) -> Path | None:
    path = cache_dir() / f"{uuid.replace('-', '').lower()}.png"
    return path if path.is_file() and path.stat().st_size > 0 else None


def _skin_url(uuid: str, timeout: float = 10.0) -> str | None:
    """Ask Mojang for the profile and dig the skin URL out of it."""
    request = urllib.request.Request(
        SESSION_PROFILE.format(uuid=uuid.replace("-", "")),
        headers={"User-Agent": "ArkonLauncher"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            profile = json.loads(response.read())
    except (OSError, ValueError):
        return None

    for entry in profile.get("properties", []) or []:
        if entry.get("name") != "textures":
            continue
        try:
            decoded = json.loads(base64.b64decode(entry.get("value", "")))
        except (ValueError, TypeError):
            continue
        skin = (decoded.get("textures") or {}).get("SKIN") or {}
        url = skin.get("url")
        if url:
            return str(url)
    return None


def fetch_head(uuid: str, size: int = 32) -> Path | None:
    """Return a cached head image for a UUID, downloading it if needed.

    Returns None rather than raising when offline or when the player has no
    skin - a missing avatar is a cosmetic problem, never a functional one.
    """
    if not uuid:
        return None

    existing = cached_head(uuid)
    if existing:
        return existing

    url = _skin_url(uuid)
    if not url:
        return None

    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ArkonLauncher"})
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read()
    except OSError:
        return None

    return _render_head(payload, uuid, size)


def _render_head(skin_png: bytes, uuid: str, size: int) -> Path | None:
    """Crop the face out of a skin and scale it up, using Qt's image support."""
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QImage, QPainter

    skin = QImage()
    if not skin.loadFromData(skin_png, "PNG"):
        return None

    face = skin.copy(QRect(8, 8, HEAD_SIZE, HEAD_SIZE))

    # The hat layer sits on a second region and is usually where the character
    # of a skin lives, so it is composited rather than ignored.
    if skin.width() >= 48:
        overlay = skin.copy(QRect(40, 8, HEAD_SIZE, HEAD_SIZE))
        painter = QPainter(face)
        painter.drawImage(0, 0, overlay)
        painter.end()

    # Nearest-neighbour: these are 8x8 pixel art, and smoothing turns them to mush.
    scaled = face.scaled(size, size, Qt.IgnoreAspectRatio, Qt.FastTransformation)

    destination = cache_dir() / f"{uuid.replace('-', '').lower()}.png"
    if not scaled.save(str(destination), "PNG"):
        return None
    return destination
