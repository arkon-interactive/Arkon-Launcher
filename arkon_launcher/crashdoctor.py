"""Working out which mod broke the server, from the logs it left behind.

Filtering by ``environment`` and the shipped denylist gets most packs booting,
but no static list is complete. This is the loop that closes the gap: when the
server dies, name a culprit, let the user disable it in one click, and remember
the answer so the next pack that contains it is right first time.

Signals, most trustworthy first:

1. Fabric's own preflight errors, which name the mod id outright.
2. Mixin apply failures, which name the failing mixin - and Fabric usually names
   the owning mod alongside it.
3. Client classes touched on a server (``net/minecraft/client/...``), where the
   nearest mod-owned stack frame is the culprit.

Every diagnosis carries the log excerpt it came from, so the user can disagree.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from .modsync import read_mod_jar


class Confidence(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass
class Diagnosis:
    mod_id: str | None
    reason: str
    confidence: Confidence
    excerpt: str
    suggestion: str = ""

    @property
    def actionable(self) -> bool:
        return bool(self.mod_id)


# Fabric names the mod directly in these. The entrypoint failure is the single
# best signal there is - it is what actually stops the server, and it carries the
# mod id verbatim.
ENTRYPOINT_FAILURE = re.compile(
    r"Could not execute entrypoint stage '[^']*' due to errors, provided by '([\w\-.]+)'", re.I
)
FROM_MOD = re.compile(r"from mod ([\w\-.]+)", re.I)
MIXIN_FOR_MOD = re.compile(r"Mixin apply for mod ([\w\-.]+) failed", re.I)
MIXIN_CONFIG = re.compile(r"([\w\-.]+)\.mixins\.json", re.I)

# Where the run actually went wrong. Mixin "target was not found" warnings appear
# in perfectly healthy servers, so the fatal region has to be isolated first or
# the diagnosis latches onto noise.
FATAL_MARKERS = (
    "Failed to start the minecraft server",
    "Could not execute entrypoint",
    "Incompatible mods found",
    "A mod crashed on startup",
)
FABRIC_REQUIRES = re.compile(
    r"Mod '([^']+)' \(([\w\-.]+)\)(?: [\w.+\-]+)? requires .*?, which is missing", re.I
)
CLIENT_CLASS = re.compile(
    r"(?:NoClassDefFoundError|ClassNotFoundException):\s*"
    r"([\w./$]*net[./]minecraft[./]client[\w./$]*)",
    re.I,
)
# A stack frame: "\tat some.package.Class.method(Class.java:42)"
STACK_FRAME = re.compile(r"^\s*at ([\w$]+(?:\.[\w$]+)+)\.[\w$<>]+\(")

# Packages that never identify a culprit.
IGNORED_PREFIXES = (
    "java.",
    "javax.",
    "jdk.",
    "sun.",
    "net.minecraft.",
    "com.mojang.",
    "net.fabricmc.",
    "org.spongepowered.",
    "org.apache.",
    "io.netty.",
    "com.google.",
    "it.unimi.",
    "org.slf4j.",
)


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


def read_crash_sources(server_dir: Path) -> str:
    """Combine the newest crash report with the tail of the latest log."""
    server_dir = Path(server_dir)
    parts: list[str] = []

    crash_dir = server_dir / "crash-reports"
    if crash_dir.is_dir():
        reports = sorted(crash_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if reports:
            try:
                parts.append(reports[0].read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass

    latest = server_dir / "logs" / "latest.log"
    if latest.is_file():
        try:
            parts.append(_tail(latest.read_text(encoding="utf-8", errors="replace"), 20000))
        except OSError:
            pass

    return "\n".join(parts)


class _JarIndex:
    """Lazily maps a class or package back to the jar that contains it."""

    def __init__(self, mods_dir: Path) -> None:
        self.mods_dir = Path(mods_dir)
        self._packages: dict[str, str] | None = None

    def _build(self) -> dict[str, str]:
        index: dict[str, str] = {}
        if not self.mods_dir.is_dir():
            return index

        for jar_path in self.mods_dir.glob("*.jar"):
            mod = read_mod_jar(jar_path)
            if not mod.mod_id:
                continue
            try:
                with zipfile.ZipFile(jar_path) as archive:
                    for entry in archive.namelist():
                        if not entry.endswith(".class"):
                            continue
                        package = entry.rsplit("/", 1)[0].replace("/", ".")
                        # First writer wins; ties are vanishingly rare and the
                        # excerpt lets the user override anyway.
                        index.setdefault(package, mod.mod_id)
            except (OSError, zipfile.BadZipFile):
                continue
        return index

    def owner_of(self, class_name: str) -> str | None:
        if self._packages is None:
            self._packages = self._build()

        parts = class_name.replace("/", ".").split(".")
        # Walk from the most specific package outwards.
        for cut in range(len(parts) - 1, 0, -1):
            owner = self._packages.get(".".join(parts[:cut]))
            if owner:
                return owner
        return None


def _excerpt_around(text: str, match: re.Match, before: int = 200, after: int = 400) -> str:
    start = max(0, match.start() - before)
    return text[start : min(len(text), match.end() + after)].strip()


def _fatal_region(text: str) -> str:
    """Narrow to the failure itself, discarding earlier harmless warnings.

    A healthy modded server logs plenty of "@Mixin target ... was not found"
    warnings on startup. Searching the whole log would confidently blame one of
    those instead of the thing that actually killed the process.
    """
    best = -1
    for marker in FATAL_MARKERS:
        position = text.rfind(marker)
        if position > best:
            best = position
    if best >= 0:
        return text[best:]
    return text


def diagnose(server_dir: Path, log_text: str | None = None) -> Diagnosis | None:
    """Name the mod most likely responsible, or None if the logs don't say."""
    full_text = log_text if log_text is not None else read_crash_sources(server_dir)
    if not full_text.strip():
        return None

    index = _JarIndex(Path(server_dir) / "mods")
    text = _fatal_region(full_text)

    # 0. Fabric says which mod's entrypoint threw. Nothing beats this.
    match = ENTRYPOINT_FAILURE.search(text)
    if match:
        mod_id = match.group(1)
        client_class = CLIENT_CLASS.search(text)
        if client_class:
            reason = (
                f"{mod_id} failed on startup trying to use "
                f"{client_class.group(1).replace('/', '.')}, a class that only exists in "
                f"the game client. It is a client-only mod."
            )
        else:
            reason = f"{mod_id} threw an error while starting up and stopped the server."
        return Diagnosis(
            mod_id=mod_id,
            reason=reason,
            confidence=Confidence.HIGH,
            excerpt=_excerpt_around(text, match, before=0, after=700),
            suggestion=f"Disable {mod_id} for this server and try again.",
        )

    # 1. Fabric names the mod itself - highest confidence available.
    match = MIXIN_FOR_MOD.search(text)
    if match:
        mod_id = match.group(1)
        return Diagnosis(
            mod_id=mod_id,
            reason=f"{mod_id} failed to apply its mixins, which usually means it is a "
            f"client-only mod running on a server.",
            confidence=Confidence.HIGH,
            excerpt=_excerpt_around(text, match),
            suggestion=f"Disable {mod_id} for this server and try again.",
        )

    match = FABRIC_REQUIRES.search(text)
    if match:
        name, mod_id = match.group(1), match.group(2)
        return Diagnosis(
            mod_id=mod_id,
            reason=f"{name} ({mod_id}) needs a mod that is not available on the server.",
            confidence=Confidence.HIGH,
            excerpt=_excerpt_around(text, match),
            suggestion=f"Disable {mod_id} for this server and try again.",
        )

    # 2. A client class on a server: blame the nearest mod-owned frame after it,
    #    or the mod Fabric attributes the mixin to.
    match = CLIENT_CLASS.search(text)
    if match:
        mod_id = _blame_stack_after(text, match.end(), index)
        if not mod_id:
            attributed = FROM_MOD.search(text[match.end() : match.end() + 400])
            if attributed:
                mod_id = attributed.group(1)
        excerpt = _excerpt_around(text, match)
        if mod_id:
            return Diagnosis(
                mod_id=mod_id,
                reason=f"{mod_id} tried to use {match.group(1).replace('/', '.')}, which "
                f"only exists in the game client. It is a client-only mod.",
                confidence=Confidence.HIGH,
                excerpt=excerpt,
                suggestion=f"Disable {mod_id} for this server and try again.",
            )
        return Diagnosis(
            mod_id=None,
            reason="A mod tried to use a client-only Minecraft class, but the log does "
            "not say which one.",
            confidence=Confidence.LOW,
            excerpt=excerpt,
        )

    # 3. A mixin config names its mod by convention.
    match = MIXIN_CONFIG.search(text)
    if match:
        mod_id = match.group(1)
        return Diagnosis(
            mod_id=mod_id,
            reason=f"The failure mentions {mod_id}'s mixin configuration.",
            confidence=Confidence.MEDIUM,
            excerpt=_excerpt_around(text, match),
            suggestion=f"Disable {mod_id} for this server and try again.",
        )

    # 4. Last resort: the first mod-owned frame in the stack.
    mod_id = _blame_stack_after(text, 0, index)
    if mod_id:
        return Diagnosis(
            mod_id=mod_id,
            reason=f"{mod_id} appears in the crash stack trace.",
            confidence=Confidence.LOW,
            excerpt=_tail(text, 1200),
            suggestion=f"Disabling {mod_id} may help, but this is a guess.",
        )

    return None


def _blame_stack_after(text: str, offset: int, index: _JarIndex) -> str | None:
    for line in text[offset:].splitlines():
        frame = STACK_FRAME.match(line)
        if not frame:
            continue
        class_name = frame.group(1)
        if class_name.startswith(IGNORED_PREFIXES):
            continue
        owner = index.owner_of(class_name)
        if owner:
            return owner
    return None
