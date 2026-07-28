"""Running the server process and talking to its console.

Deliberately free of any UI framework: the process is driven with ``subprocess``
and a reader thread, and callers subscribe with plain callbacks. That keeps the
whole thing testable from a script, which matters because "does the server
actually boot" is the question this project lives or dies on.

Threading model: one reader thread pumps merged stdout/stderr and fans each line
out to listeners. Callbacks therefore arrive on that thread - the Qt layer is
responsible for hopping to the GUI thread.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

LineCallback = Callable[[str], None]
StateCallback = Callable[["ServerState"], None]

# "[16:04:52] [Server thread/INFO]: Done (12.345s)! For help, type "help""
LOG_LINE = re.compile(
    r"^\[(?P<time>\d{2}:\d{2}:\d{2})\]\s*"
    r"\[(?P<thread>[^/\]]+)/(?P<level>[A-Z]+)\][^:]*:\s?(?P<message>.*)$"
)
DONE_LINE = re.compile(r'Done \(([\d.]+)s\)! For help, type "help"')

# Connection events, not chat.
#
# "X joined the game" looks like the obvious signal but is not reliable: it is a
# broadcast chat message, and in practice it is missing for some players while
# present for others on the same server. "logged in with entity id" and "lost
# connection" are emitted by the connection handling itself and always appear.
PLAYER_LOGGED_IN = re.compile(r"^(\w{1,16})\[/[^\]]+\] logged in with entity id")
PLAYER_LOST_CONNECTION = re.compile(r"^(\w{1,16}) lost connection:")
# Kept as secondary signals - harmless when present, never relied on alone.
PLAYER_JOINED = re.compile(r"^(\w{1,16}) joined the game$")
PLAYER_LEFT = re.compile(r"^(\w{1,16}) left the game$")
# "UUID of player FenixRysing is 77086af9-..." - the authenticator, so it is the
# authoritative name-to-uuid mapping for anyone who connects.
PLAYER_UUID = re.compile(r"^UUID of player (\w{1,16}) is ([0-9a-fA-F-]{32,36})")
# Fabric refuses to start and names the mod, which is the highest-confidence
# signal we ever get about a bad mod.
FABRIC_MOD_ERROR = re.compile(r"Mod '([^']+)' \(([^)]+)\)")


class ServerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"


@dataclass
class LogLine:
    raw: str
    level: str = "INFO"
    thread: str = ""
    message: str = ""
    timestamp: str = ""

    @classmethod
    def parse(cls, raw: str) -> "LogLine":
        match = LOG_LINE.match(raw)
        if not match:
            return cls(raw=raw, message=raw)
        return cls(
            raw=raw,
            level=match.group("level"),
            thread=match.group("thread"),
            message=match.group("message"),
            timestamp=match.group("time"),
        )


@dataclass
class ServerConfig:
    java: Path
    server_jar: Path
    working_dir: Path
    min_memory_mb: int = 2048
    max_memory_mb: int = 6144
    extra_jvm_args: list[str] = field(default_factory=list)
    # Where the worlds live and which one to load. Passing these lets the server
    # open the instance's save folder directly, instead of reaching it through a
    # directory junction - see the note in modsync.remove_legacy_world_link.
    universe: Path | None = None
    world_name: str | None = None

    def command(self) -> list[str]:
        command = [
            str(self.java),
            f"-Xms{self.min_memory_mb}m",
            f"-Xmx{self.max_memory_mb}m",
            *self.extra_jvm_args,
            "-jar",
            str(self.server_jar),
            "nogui",
        ]
        if self.universe is not None:
            # A trailing separator would escape the closing quote when Windows
            # rebuilds the command line, swallowing the next argument.
            command += ["--universe", str(self.universe).rstrip("\\/")]
        if self.world_name:
            command += ["--world", self.world_name]
        return command


# JVM flags worth carrying over from the client's CurseForge settings. Anything
# else there (heap sizes especially) is client-specific and must not leak in.
_SAFE_JVM_PREFIXES = ("-XX:", "-Dfile.encoding", "-Dsun.stdout", "-Dsun.stderr")


def sanitize_jvm_args(raw: str | None) -> list[str]:
    """Filter an instance's javaArgsOverride down to server-appropriate flags."""
    if not raw:
        return []
    keep: list[str] = []
    for token in raw.split():
        if token.startswith(("-Xmx", "-Xms", "-Xmn")):
            continue  # Memory is the launcher's to decide.
        if token.startswith(_SAFE_JVM_PREFIXES):
            keep.append(token)
    return keep


class ServerProcess:
    """One running (or not) Minecraft server."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._state = ServerState.STOPPED
        self._lock = threading.Lock()

        self._line_listeners: list[LineCallback] = []
        self._state_listeners: list[StateCallback] = []
        self._player_listeners: list[Callable[[set[str]], None]] = []
        # Console output is one undifferentiated stream, so two queries running at
        # once would each capture the other's replies. Serialise them.
        self._query_lock = threading.Lock()

        self.started_at: float | None = None
        self.ready_at: float | None = None
        self.exit_code: int | None = None
        self.players: set[str] = set()
        # Name -> uuid, learned from the authenticator as people connect.
        self.player_uuids: dict[str, str] = {}
        self.recent_lines: list[str] = []
        self.max_recent = 500

    # --- Subscriptions ---

    def on_line(self, callback: LineCallback) -> None:
        self._line_listeners.append(callback)

    def on_state(self, callback: StateCallback) -> None:
        self._state_listeners.append(callback)

    def on_players(self, callback: Callable[[set[str]], None]) -> None:
        """Called whenever someone joins or leaves, so the UI can keep up."""
        self._player_listeners.append(callback)

    def _notify_players(self) -> None:
        for listener in list(self._player_listeners):
            try:
                listener(set(self.players))
            except Exception:
                pass

    # --- State ---

    @property
    def state(self) -> ServerState:
        return self._state

    def _set_state(self, state: ServerState) -> None:
        if state is self._state:
            return
        self._state = state
        for listener in list(self._state_listeners):
            try:
                listener(state)
            except Exception:
                pass

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    # --- Lifecycle ---

    def start(self) -> None:
        if self.is_alive:
            raise RuntimeError("Server is already running.")

        working_dir = Path(self.config.working_dir)
        working_dir.mkdir(parents=True, exist_ok=True)

        self.players.clear()
        self.recent_lines.clear()
        self.exit_code = None
        self.ready_at = None
        self.started_at = time.time()
        self._set_state(ServerState.STARTING)

        self._process = subprocess.Popen(
            self.config.command(),
            cwd=str(working_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        self._reader = threading.Thread(target=self._pump, name="server-output", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            for raw in self._process.stdout:
                self._handle_line(raw.rstrip("\r\n"))
        except (OSError, ValueError):
            pass

        self.exit_code = self._process.wait()

        if self._state is ServerState.STOPPING:
            self._set_state(ServerState.STOPPED)
        elif self.ready_at is None:
            # Never reached "Done", so it failed during startup regardless of the
            # exit code - Fabric exits 0 after reporting a fatal mod error.
            self._set_state(ServerState.CRASHED)
        elif self.exit_code == 0:
            self._set_state(ServerState.STOPPED)
        else:
            self._set_state(ServerState.CRASHED)

    def _handle_line(self, raw: str) -> None:
        self.recent_lines.append(raw)
        if len(self.recent_lines) > self.max_recent:
            del self.recent_lines[: len(self.recent_lines) - self.max_recent]

        parsed = LogLine.parse(raw)

        if DONE_LINE.search(parsed.message):
            self.ready_at = time.time()
            self._set_state(ServerState.RUNNING)

        for pattern in (PLAYER_LOGGED_IN, PLAYER_JOINED):
            match = pattern.match(parsed.message)
            if match:
                name = match.group(1)
                if name not in self.players:
                    self.players.add(name)
                    self._notify_players()
                break

        for pattern in (PLAYER_LOST_CONNECTION, PLAYER_LEFT):
            match = pattern.match(parsed.message)
            if match:
                name = match.group(1)
                if name in self.players:
                    self.players.discard(name)
                    self._notify_players()
                break

        uuid_match = PLAYER_UUID.match(parsed.message)
        if uuid_match:
            self.player_uuids[uuid_match.group(1)] = uuid_match.group(2)

        for listener in list(self._line_listeners):
            try:
                listener(raw)
            except Exception:
                pass

    def send(self, command: str) -> None:
        """Write a console command to the server's stdin."""
        if not self.is_alive or self._process is None or self._process.stdin is None:
            raise RuntimeError("Server is not running.")
        command = command.strip()
        if not command:
            return
        with self._lock:
            try:
                self._process.stdin.write(command + "\n")
                self._process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Could not send command: {exc}") from exc

    def query(self, command: str, settle: float = 0.6, timeout: float = 6.0) -> list[str]:
        """Send a command and collect the lines it produces.

        The server has no request/response protocol - output is just a stream -
        so this captures everything logged between sending the command and the
        output going quiet. Good enough to drive a UI off commands like
        ``lp group list``, and honest about its limits: unrelated server chatter
        arriving at the same moment will be captured too, so callers should
        parse for what they expect rather than trusting line positions.
        """
        if not self.is_alive:
            raise RuntimeError("Server is not running.")

        collected: list[str] = []
        done = threading.Event()

        with self._query_lock:
            last_line = time.time()

            def capture(line: str) -> None:
                nonlocal last_line
                collected.append(line)
                last_line = time.time()

            self._line_listeners.append(capture)
            try:
                self.send(command)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if collected and (time.time() - last_line) >= settle:
                        break
                    done.wait(0.1)
            finally:
                if capture in self._line_listeners:
                    self._line_listeners.remove(capture)

        return collected

    def stop(self, timeout: float = 60.0) -> int | None:
        """Ask the server to save and shut down, escalating only if it won't.

        The graceful path matters: `stop` flushes chunks. Killing the process
        risks losing recent world state, so terminate/kill are last resorts.
        """
        if not self.is_alive or self._process is None:
            return self.exit_code

        self._set_state(ServerState.STOPPING)
        try:
            self.send("stop")
        except RuntimeError:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline and self.is_alive:
            time.sleep(0.25)

        if self.is_alive and self._process is not None:
            self._process.terminate()
            grace = time.time() + 10
            while time.time() < grace and self.is_alive:
                time.sleep(0.25)

        if self.is_alive and self._process is not None:
            self._process.kill()

        if self._reader is not None:
            self._reader.join(timeout=10)

        return self.exit_code

    def wait_for_ready(self, timeout: float = 600.0) -> bool:
        """Block until the server reports Done, or it dies, or we give up."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._state is ServerState.RUNNING:
                return True
            if self._state in (ServerState.STOPPED, ServerState.CRASHED):
                return False
            time.sleep(0.25)
        return False
