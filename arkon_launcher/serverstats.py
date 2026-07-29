"""Live server health: tick rate, memory, CPU, uptime.

Tick rate has two sources, tried in that order:

1. **A ``/tps`` command**, if the pack provides one. Cheap and exact, and it
   usually reports MSPT too.
2. **Measuring it ourselves** from ``time query gametime``: sample the tick
   counter twice and divide by the wall-clock gap. Needs no mod at all, and
   measured within a few percent of the real value in testing.

CPU is always reported normalised across cores. A server using two of sixteen
cores is at 12%, not 200% - the raw number is technically true and reliably
alarming to anyone reading it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

# "TPS 20.0 / 20  §  MSPT 13.8" - colour codes stripped before matching.
COLOUR_CODES = re.compile(r"[§&][0-9a-fk-orA-FK-OR]")
TPS_LINE = re.compile(r"\bTPS\b[^\d]{0,6}(\d+(?:\.\d+)?)", re.I)
MSPT_LINE = re.compile(r"\bMSPT\b[^\d]{0,6}(\d+(?:\.\d+)?)", re.I)
# "The time is 503861" / "gametime is 503861"
GAMETIME = re.compile(r"(?:gametime|time)\s+is\s+(\d+)", re.I)

MAX_TPS = 20.0


def clean(line: str) -> str:
    text = line.split("]: ", 1)[-1] if "]: " in line else line
    return COLOUR_CODES.sub("", text).strip()


def parse_tps(lines: list[str]) -> tuple[float | None, float | None]:
    """(tps, mspt) from a /tps reply. Either may be None."""
    tps = mspt = None
    for line in lines:
        text = clean(line)
        if tps is None:
            match = TPS_LINE.search(text)
            if match:
                try:
                    tps = min(MAX_TPS, float(match.group(1)))
                except ValueError:
                    pass
        if mspt is None:
            match = MSPT_LINE.search(text)
            if match:
                try:
                    mspt = float(match.group(1))
                except ValueError:
                    pass
    return tps, mspt


def parse_gametime(lines: list[str]) -> int | None:
    for line in lines:
        match = GAMETIME.search(clean(line))
        if match:
            return int(match.group(1))
    return None


@dataclass
class TickSampler:
    """Derives TPS from two gametime readings, for packs with no /tps."""

    last_ticks: int | None = None
    last_at: float = 0.0

    def sample(self, ticks: int | None) -> float | None:
        now = time.monotonic()
        if ticks is None:
            return None

        previous, previous_at = self.last_ticks, self.last_at
        self.last_ticks, self.last_at = ticks, now

        if previous is None or now <= previous_at:
            return None

        elapsed = now - previous_at
        # Too short a window and query jitter dominates the answer.
        if elapsed < 1.0:
            return None

        delta = ticks - previous
        if delta < 0:
            return None  # Clock went backwards; a restart, probably.
        return min(MAX_TPS, delta / elapsed)

    def reset(self) -> None:
        self.last_ticks = None
        self.last_at = 0.0


@dataclass
class ProcessStats:
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    threads: int = 0

    @property
    def memory_gb(self) -> float:
        return self.memory_mb / 1024


def sample_process(pid: int | None) -> ProcessStats | None:
    """CPU and memory for the server process, CPU normalised across cores."""
    if pid is None:
        return None
    try:
        import psutil

        process = psutil.Process(pid)
        cores = psutil.cpu_count() or 1
        # cpu_percent(None) is measured since the previous call on this object,
        # so the caller must keep the sampler alive between polls.
        raw = process.cpu_percent(None)
        memory = process.memory_info()
        return ProcessStats(
            cpu_percent=min(100.0, raw / cores),
            memory_mb=memory.rss / 1024**2,
            threads=process.num_threads(),
        )
    except Exception:
        return None


class ProcessSampler:
    """Holds a psutil handle so CPU deltas are measured between polls."""

    def __init__(self) -> None:
        self._process = None
        self._pid: int | None = None

    def attach(self, pid: int | None) -> None:
        if pid == self._pid:
            return
        self._pid = pid
        self._process = None
        if pid is None:
            return
        try:
            import psutil

            self._process = psutil.Process(pid)
            self._process.cpu_percent(None)  # Prime the delta.
        except Exception:
            self._process = None

    def sample(self) -> ProcessStats | None:
        if self._process is None:
            return None
        try:
            import psutil

            cores = psutil.cpu_count() or 1
            raw = self._process.cpu_percent(None)
            memory = self._process.memory_info()
            return ProcessStats(
                cpu_percent=min(100.0, raw / cores),
                memory_mb=memory.rss / 1024**2,
                threads=self._process.num_threads(),
            )
        except Exception:
            return None


def tps_health(tps: float | None) -> str:
    """A colour for the tick rate: green healthy, amber slipping, red bad."""
    if tps is None:
        return "#8b949e"
    if tps >= 19.0:
        return "#5fb37a"
    if tps >= 15.0:
        return "#c9a227"
    return "#e06c75"


def format_uptime(seconds: float) -> str:
    total = int(max(0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
