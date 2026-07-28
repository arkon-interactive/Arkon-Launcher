"""Getting friends connected, without asking the host to configure a router.

Essential Mod solves this with ICE - hole-punched peer-to-peer, TURN relay as a
fallback - but that only works because the host is sitting in the game with
Essential loaded. It is client-only, it needs Essential's signalling servers and
friend graph, and there is no headless build. A dedicated server cannot use it,
so this aims at the same *outcome* by other means.

Three rungs, tried in order, each falling through quietly:

1. **UPnP** - ask the router to open the port. Direct connection, no third
   party, no added latency. Only works if UPnP is enabled and the host is not
   behind carrier-grade NAT.
2. **playit.gg** - a relay tunnel. Costs 10-50 ms, but works behind CGNAT and
   any router, and carries UDP so Simple Voice Chat works too.
3. **Manual** - show the addresses and what to forward.

Whichever rung wins, the address is verified by actually pinging the server
through it rather than assuming it worked.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ElementTree
from urllib.parse import urljoin
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from . import paths

# Simple Voice Chat's default port. Easy to forget, and a confusing failure when
# forgotten, so it is mapped and reported alongside the game port.
VOICE_CHAT_UDP_PORT = 24454

SSDP_ADDRESS = ("239.255.255.250", 1900)
IGD_SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
WAN_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)

PLAYIT_RELEASE_API = "https://api.github.com/repos/playit-cloud/playit-agent/releases/latest"
PLAYIT_ASSET = "playit-windows-x86_64-signed.exe"
PLAYIT_CLAIM_RE = re.compile(r"https://playit\.gg/claim/\S+")
PLAYIT_TUNNEL_RE = re.compile(r"\b([\w-]+\.(?:joinmc\.link|craft\.ply\.gg|ply\.gg))(?::(\d+))?\b")

ProgressCallback = Callable[[str], None]


class Rung(str, Enum):
    UPNP = "upnp"
    PLAYIT = "playit"
    MANUAL = "manual"


class ConnectionError_(Exception):
    pass


# --- Basic network facts ------------------------------------------------------


def local_ips() -> list[str]:
    """LAN addresses friends on the same network can use."""
    found: list[str] = []
    try:
        # Doesn't send anything; just asks the OS which interface would be used.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found and not address.startswith("127."):
                found.append(address)
    except OSError:
        pass
    return found


def public_ip(timeout: float = 8.0) -> str | None:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ArkonLauncher"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                candidate = response.read().decode("utf-8", "replace").strip()
            ipaddress.ip_address(candidate)
            return candidate
        except (OSError, ValueError):
            continue
    return None


def is_private(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_private or parsed.is_loopback or parsed.is_link_local


def is_cgnat(address: str) -> bool:
    """100.64.0.0/10 - the carrier-grade NAT range, where port forwarding is futile."""
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


# --- Minecraft server list ping ----------------------------------------------


def _write_varint(value: int) -> bytes:
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        out += struct.pack("B", byte | (0x80 if value else 0))
        if not value:
            return out


def _read_varint(sock: socket.socket) -> int:
    number = 0
    for shift in range(0, 35, 7):
        chunk = sock.recv(1)
        if not chunk:
            raise OSError("connection closed while reading varint")
        number |= (chunk[0] & 0x7F) << shift
        if not chunk[0] & 0x80:
            return number
    raise OSError("varint too long")


def ping_server(host: str, port: int, timeout: float = 6.0) -> dict | None:
    """Speak Minecraft's status protocol. Returns the server's status, or None.

    A successful ping is proof that a *Minecraft server* is reachable at that
    address, which is a far stronger statement than "the TCP port accepted a
    connection".
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)

            address = host.encode("utf-8")
            handshake = (
                b"\x00"
                + _write_varint(767)  # Protocol version; any recent value works.
                + _write_varint(len(address))
                + address
                + struct.pack(">H", port)
                + b"\x01"  # Next state: status.
            )
            sock.sendall(_write_varint(len(handshake)) + handshake)
            sock.sendall(_write_varint(1) + b"\x00")  # Status request.

            _read_varint(sock)  # Total packet length.
            if _read_varint(sock) != 0x00:
                return None

            payload_length = _read_varint(sock)
            buffer = b""
            while len(buffer) < payload_length:
                chunk = sock.recv(min(4096, payload_length - len(buffer)))
                if not chunk:
                    break
                buffer += chunk

            return json.loads(buffer.decode("utf-8", "replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


# --- Rung 1: UPnP -------------------------------------------------------------


@dataclass
class Gateway:
    control_url: str
    service_type: str
    location: str


def discover_gateway(timeout: float = 4.0) -> Gateway | None:
    """Find an Internet Gateway Device on the LAN.

    Many networks simply don't have one, or have UPnP switched off, so this must
    fail fast and quietly rather than holding up a server start.
    """
    locations: list[str] = []
    deadline = time.monotonic() + timeout

    # All search targets go out on one socket against one shared deadline.
    # Retrying them serially would multiply the wait on the common case - a
    # network with no IGD at all - and that delay sits in front of every start.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            for target in IGD_SEARCH_TARGETS:
                message = (
                    "M-SEARCH * HTTP/1.1\r\n"
                    f"HOST:{SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}\r\n"
                    f"ST:{target}\r\n"
                    "MX:2\r\n"
                    'MAN:"ssdp:discover"\r\n\r\n'
                ).encode()
                try:
                    sock.sendto(message, SSDP_ADDRESS)
                except OSError:
                    continue

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, _ = sock.recvfrom(65507)
                except (socket.timeout, OSError):
                    break
                match = re.search(
                    r"(?i)^LOCATION:\s*(\S+)", data.decode("utf-8", "replace"), re.M
                )
                if match and match.group(1) not in locations:
                    locations.append(match.group(1))
    except OSError:
        return None

    for location in locations:
        gateway = _describe_gateway(location)
        if gateway:
            return gateway
    return None


def _describe_gateway(location: str) -> Gateway | None:
    try:
        request = urllib.request.Request(location, headers={"User-Agent": "ArkonLauncher"})
        with urllib.request.urlopen(request, timeout=8) as response:
            root = ElementTree.fromstring(response.read())
    except (OSError, ElementTree.ParseError):
        return None

    namespace = {"u": "urn:schemas-upnp-org:device-1-0"}
    for service in root.iter(f"{{{namespace['u']}}}service"):
        service_type = service.findtext(f"{{{namespace['u']}}}serviceType") or ""
        control = service.findtext(f"{{{namespace['u']}}}controlURL") or ""
        if service_type in WAN_SERVICE_TYPES and control:
            # controlURL is usually relative to the description document.
            return Gateway(
                control_url=urljoin(location, control),
                service_type=service_type,
                location=location,
            )
    return None


def _soap(gateway: Gateway, action: str, arguments: dict[str, object]) -> dict[str, str]:
    body_arguments = "".join(f"<{k}>{v}</{k}>" for k, v in arguments.items())
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{gateway.service_type}">{body_arguments}'
        f"</u:{action}></s:Body></s:Envelope>"
    ).encode()

    request = urllib.request.Request(
        gateway.control_url,
        data=envelope,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{gateway.service_type}#{action}"',
            "User-Agent": "ArkonLauncher",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        root = ElementTree.fromstring(response.read())

    return {
        element.tag.split("}")[-1]: (element.text or "")
        for element in root.iter()
        if element.text and not element.tag.endswith(("Envelope", "Body"))
    }


def gateway_external_ip(gateway: Gateway) -> str | None:
    try:
        result = _soap(gateway, "GetExternalIPAddress", {})
    except (OSError, ElementTree.ParseError):
        return None
    address = result.get("NewExternalIPAddress")
    return address or None


def add_port_mapping(
    gateway: Gateway, external_port: int, internal_ip: str, protocol: str = "TCP"
) -> bool:
    try:
        _soap(
            gateway,
            "AddPortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": external_port,
                "NewProtocol": protocol,
                "NewInternalPort": external_port,
                "NewInternalClient": internal_ip,
                "NewEnabled": 1,
                "NewPortMappingDescription": "Arkon Launcher",
                "NewLeaseDuration": 0,
            },
        )
        return True
    except (OSError, ElementTree.ParseError):
        return False


def delete_port_mapping(gateway: Gateway, external_port: int, protocol: str = "TCP") -> bool:
    """Remove a mapping we made. Leaving these behind on someone's router is rude."""
    try:
        _soap(
            gateway,
            "DeletePortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": external_port,
                "NewProtocol": protocol,
            },
        )
        return True
    except (OSError, ElementTree.ParseError):
        return False


# --- Rung 2: playit.gg --------------------------------------------------------


@dataclass
class PlayitRelease:
    url: str
    name: str
    size: int
    version: str


def playit_release() -> PlayitRelease:
    """Look up the current agent build so the user can see what they'd download."""
    request = urllib.request.Request(
        PLAYIT_RELEASE_API, headers={"User-Agent": "ArkonLauncher"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read())

    for asset in data.get("assets", []):
        if asset.get("name") == PLAYIT_ASSET:
            return PlayitRelease(
                url=asset["browser_download_url"],
                name=asset["name"],
                size=int(asset.get("size") or 0),
                version=data.get("tag_name", "unknown"),
            )
    raise ConnectionError_(f"playit.gg release does not contain {PLAYIT_ASSET}.")


def playit_binary_path() -> Path:
    return paths.cache_dir() / PLAYIT_ASSET


def playit_is_downloaded() -> bool:
    binary = playit_binary_path()
    return binary.is_file() and binary.stat().st_size > 0


# --- Detecting an existing playit.gg installation -----------------------------
#
# The official installer lays out C:\Program Files\playit_gg\bin with playit.exe
# (CLI), playitd.exe (agent), playitd-service.exe (Windows service) and
# playitd-tray.exe (tray UI). If the user already has that, there is no reason to
# download our own copy - we point at theirs instead.

PLAYIT_PROCESS_NAMES = ("playit", "playitd", "playitd-tray", "playitd-service")
# Tray first: it starts the agent and gives the user somewhere to manage it,
# and unlike the service it does not need administrator rights.
PLAYIT_LAUNCH_ORDER = ("playitd-tray.exe", "playit.exe", "playitd.exe")


@dataclass
class PlayitInstall:
    executable: Path | None = None
    directory: Path | None = None
    from_system: bool = False
    running_pids: list[int] = field(default_factory=list)

    @property
    def installed(self) -> bool:
        return self.executable is not None

    @property
    def running(self) -> bool:
        return bool(self.running_pids)


def _registry_playit_locations() -> list[Path]:
    locations: list[Path] = []
    try:
        import winreg
    except ImportError:
        return locations

    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )

    for hive, subkey in roots:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, name) as entry:
                            display, _ = winreg.QueryValueEx(entry, "DisplayName")
                            if "playit" not in str(display).lower():
                                continue
                            location, _ = winreg.QueryValueEx(entry, "InstallLocation")
                            if location:
                                locations.append(Path(location))
                    except OSError:
                        continue
        except OSError:
            continue
    return locations


def find_playit_install() -> PlayitInstall:
    """Locate playit.gg: the user's own installation first, ours as a fallback."""
    candidates: list[Path] = list(_registry_playit_locations())

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))
    for base in (program_files, program_files_x86, local_appdata):
        candidates.append(Path(base) / "playit_gg")
        candidates.append(Path(base) / "Programs" / "playit_gg")

    install = PlayitInstall(running_pids=playit_running_pids())

    for directory in candidates:
        if not directory or not directory.is_dir():
            continue
        for folder in (directory / "bin", directory):
            for name in PLAYIT_LAUNCH_ORDER:
                candidate = folder / name
                if candidate.is_file():
                    install.executable = candidate
                    install.directory = directory
                    install.from_system = True
                    return install

    on_path = shutil.which("playit")
    if on_path:
        install.executable = Path(on_path)
        install.directory = Path(on_path).parent
        install.from_system = True
        return install

    if playit_is_downloaded():
        install.executable = playit_binary_path()
        install.directory = playit_binary_path().parent
        install.from_system = False

    return install


def playit_running_pids() -> list[int]:
    """PIDs of any running playit process, whichever copy it came from."""
    pids: list[int] = []
    try:
        import psutil
    except ImportError:
        return pids

    for process in psutil.process_iter(["pid", "name"]):
        try:
            name = (process.info.get("name") or "").lower()
        except Exception:
            continue
        stem = name[:-4] if name.endswith(".exe") else name
        if stem in PLAYIT_PROCESS_NAMES or stem.startswith("playit"):
            pids.append(process.info["pid"])
    return pids


def launch_playit(install: PlayitInstall) -> None:
    """Start the user's own playit.gg installation and leave it running.

    Detached on purpose: this is their application, so it should outlive the
    launcher rather than being torn down when Arkon Launcher closes.
    """
    if install.executable is None:
        raise ConnectionError_("playit.gg is not installed.")

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    try:
        subprocess.Popen(
            [str(install.executable)],
            cwd=str(install.executable.parent),
            creationflags=creation_flags,
            close_fds=True,
        )
    except OSError as exc:
        raise ConnectionError_(f"Could not start {install.executable.name}: {exc}") from exc


def download_playit(release: PlayitRelease, on_progress: ProgressCallback | None = None) -> Path:
    """Download the agent. Only ever call this after the user has agreed to it.

    playit.gg is a third party and this puts an executable on their machine, so
    consent is the caller's job and is never assumed here.
    """
    destination = playit_binary_path()
    partial = destination.with_suffix(".part")

    request = urllib.request.Request(release.url, headers={"User-Agent": "ArkonLauncher"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(partial, "wb") as out:
            done = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if on_progress and release.size:
                    on_progress(f"Downloading playit.gg agent... {done * 100 // release.size}%")
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ConnectionError_(f"Could not download the playit.gg agent: {exc}") from exc

    partial.replace(destination)
    return destination


class PlayitAgent:
    """Runs the playit.gg agent and reports the address it hands out."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.process: subprocess.Popen[str] | None = None
        self.claim_url: str | None = None
        self.tunnel_address: str | None = None
        self.lines: list[str] = []
        self._thread: threading.Thread | None = None
        self._listeners: list[Callable[[str], None]] = []

    def on_line(self, callback: Callable[[str], None]) -> None:
        self._listeners.append(callback)

    @property
    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        binary = playit_binary_path()
        if not binary.is_file():
            raise ConnectionError_("The playit.gg agent has not been downloaded yet.")

        self.process = subprocess.Popen(
            [str(binary), "--secret-path", str(paths.state_dir() / "playit.toml")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._thread = threading.Thread(target=self._pump, name="playit", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for raw in self.process.stdout:
            line = raw.rstrip("\r\n")
            self.lines.append(line)
            if len(self.lines) > 300:
                del self.lines[:100]

            claim = PLAYIT_CLAIM_RE.search(line)
            if claim and not self.claim_url:
                self.claim_url = claim.group(0)

            tunnel = PLAYIT_TUNNEL_RE.search(line)
            if tunnel:
                self.tunnel_address = tunnel.group(0)

            for listener in list(self._listeners):
                try:
                    listener(line)
                except Exception:
                    pass

    def stop(self) -> None:
        if self.process and self.is_alive:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


# --- The ladder ---------------------------------------------------------------


@dataclass
class ConnectionStatus:
    rung: Rung = Rung.MANUAL
    address: str | None = None
    port: int = 25565
    lan_addresses: list[str] = field(default_factory=list)
    public_address: str | None = None
    behind_cgnat: bool = False
    upnp_available: bool = False
    notes: list[str] = field(default_factory=list)
    verified: bool = False

    def friend_address(self) -> str:
        if self.address:
            return self.address
        if self.public_address:
            return f"{self.public_address}:{self.port}"
        return f"(unknown):{self.port}"


class ConnectionManager:
    """Owns whatever the current rung set up, so it can be undone on stop."""

    def __init__(self, port: int = 25565) -> None:
        self.port = port
        self.status = ConnectionStatus(port=port)
        self._gateway: Gateway | None = None
        self._mapped: list[tuple[int, str]] = []
        self.agent: PlayitAgent | None = None

    def gather(self, on_progress: ProgressCallback | None = None) -> ConnectionStatus:
        """Work out the best available rung. Does not start playit."""
        status = ConnectionStatus(port=self.port)

        if on_progress:
            on_progress("Checking local network...")
        status.lan_addresses = local_ips()

        if on_progress:
            on_progress("Looking up public address...")
        status.public_address = public_ip()

        if on_progress:
            on_progress("Looking for a UPnP router...")
        gateway = discover_gateway()
        self._gateway = gateway
        status.upnp_available = gateway is not None

        if gateway is None:
            status.notes.append(
                "Your router did not answer a UPnP request, so the port cannot be "
                "opened automatically."
            )
        else:
            external = gateway_external_ip(gateway)
            if external and (is_cgnat(external) or is_private(external)):
                status.behind_cgnat = True
                status.notes.append(
                    f"Your router's internet address ({external}) is a shared carrier "
                    f"address, so port forwarding cannot work on this connection no "
                    f"matter how it is configured. A tunnel is the only option."
                )
            elif external and status.public_address and external != status.public_address:
                status.behind_cgnat = True
                status.notes.append(
                    "Your router reports a different internet address than the one the "
                    "outside world sees, which means there is a second router or a "
                    "carrier NAT in the way. Port forwarding will not work."
                )

        if status.upnp_available and not status.behind_cgnat:
            status.rung = Rung.UPNP
        elif playit_is_downloaded():
            status.rung = Rung.PLAYIT
        else:
            status.rung = Rung.MANUAL

        self.status = status
        return status

    def apply_upnp(self, on_progress: ProgressCallback | None = None) -> bool:
        """Map the game and voice ports. Returns True if the game port was mapped."""
        if self._gateway is None or not self.status.lan_addresses:
            return False

        internal = self.status.lan_addresses[0]
        if on_progress:
            on_progress("Asking the router to open the port...")

        mapped = add_port_mapping(self._gateway, self.port, internal, "TCP")
        if mapped:
            self._mapped.append((self.port, "TCP"))

        # Best effort; voice chat failing to map is not fatal to the server.
        if add_port_mapping(self._gateway, VOICE_CHAT_UDP_PORT, internal, "UDP"):
            self._mapped.append((VOICE_CHAT_UDP_PORT, "UDP"))
        else:
            self.status.notes.append(
                f"Could not open UDP {VOICE_CHAT_UDP_PORT}, so Simple Voice Chat may "
                f"not work for people outside your network."
            )

        if mapped and self.status.public_address:
            self.status.address = f"{self.status.public_address}:{self.port}"
        return mapped

    def verify(self, on_progress: ProgressCallback | None = None) -> bool:
        """Ping the server through the address friends would actually use.

        A failure here is reported as "could not confirm", not "broken": many
        routers refuse to loop a connection back from inside the network even
        when the port is genuinely open to the outside.
        """
        target = self.status.address or (
            f"{self.status.public_address}:{self.port}" if self.status.public_address else None
        )
        if not target:
            return False

        host, _, port_text = target.rpartition(":")
        try:
            port = int(port_text)
        except ValueError:
            host, port = target, self.port

        if on_progress:
            on_progress(f"Checking {target} from outside...")

        # Retry briefly: the server accepts connections a moment after it logs
        # "Done", and a single attempt right on that boundary reports a false
        # negative.
        self.status.verified = False
        for attempt in range(4):
            if ping_server(host, port) is not None:
                self.status.verified = True
                break
            if attempt < 3:
                time.sleep(2)
        if not self.status.verified:
            self.status.notes.append(
                "Could not confirm the address from this computer. That is common - "
                "many routers block connections that loop back from inside - so ask a "
                "friend to try it before assuming it is broken."
            )
        return self.status.verified

    def release(self) -> None:
        """Undo everything: remove router mappings, stop the tunnel."""
        if self._gateway is not None:
            for port, protocol in self._mapped:
                delete_port_mapping(self._gateway, port, protocol)
        self._mapped.clear()

        if self.agent is not None:
            self.agent.stop()
            self.agent = None
