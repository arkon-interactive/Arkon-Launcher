"""What the left-hand panel shows once a server is running.

While stopped you need the instance and world pickers. Once it's up they're
useless - you can't switch worlds without stopping - and what you actually want
is whether the thing is healthy. So the panel swaps.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ..serverstats import format_uptime, tps_health

HINT = "color:#8b949e;"
VALUE = "font-weight:600;"


class StatRow(QLabel):
    def __init__(self, text: str = "-") -> None:
        super().__init__(text)
        self.setStyleSheet(VALUE)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)


class ServerStatsPanel(QWidget):
    """Live health for the running server."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.world_name = QLabel("-")
        self.world_name.setStyleSheet("font-size:15px; font-weight:600;")
        self.world_name.setWordWrap(True)

        self.state = QLabel("")
        self.state.setStyleSheet(HINT)

        header = QVBoxLayout()
        header.addWidget(self.world_name)
        header.addWidget(self.state)

        # --- Health ---
        self.tps = StatRow()
        self.mspt = StatRow()
        self.uptime = StatRow()
        self.players = StatRow()

        health = QGroupBox("Health")
        health_form = QFormLayout(health)
        health_form.addRow("Tick rate", self.tps)
        health_form.addRow("Tick time", self.mspt)
        health_form.addRow("Uptime", self.uptime)
        health_form.addRow("Players", self.players)

        # --- Resources ---
        self.cpu_bar = QProgressBar()
        self.cpu_bar.setRange(0, 100)
        self.cpu_bar.setFormat("%p%")
        self.cpu_bar.setToolTip(
            "Share of this PC's total processing power, averaged across all cores - "
            "not the per-core figure Task Manager shows, which can read far above "
            "100%."
        )

        self.memory_bar = QProgressBar()
        self.memory_bar.setRange(0, 100)
        self.memory_bar.setFormat("%v%")

        self.memory_label = QLabel("-")
        self.memory_label.setStyleSheet(HINT)
        self.threads = StatRow()

        resources = QGroupBox("Resources")
        resources_form = QFormLayout(resources)
        resources_form.addRow("CPU", self.cpu_bar)
        resources_form.addRow("Memory", self.memory_bar)
        resources_form.addRow("", self.memory_label)
        resources_form.addRow("Threads", self.threads)

        # --- Connection ---
        self.address = StatRow()
        self.address.setToolTip("What to give friends - full details on the Connection tab.")
        self.reachable = QLabel("")
        self.reachable.setStyleSheet(HINT)

        connection = QGroupBox("Connection")
        connection_form = QFormLayout(connection)
        connection_form.addRow("Address", self.address)
        connection_form.addRow("", self.reachable)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(health)
        layout.addWidget(resources)
        layout.addWidget(connection)
        layout.addStretch(1)

    # --- Updates ---

    def set_world(self, name: str, mc_version: str = "") -> None:
        self.world_name.setText(name)
        self.state.setText(f"Minecraft {mc_version}" if mc_version else "")

    def set_tps(
        self, tps: float | None, mspt: float | None, paused: bool = False
    ) -> None:
        if paused:
            # Minecraft stops ticking an empty server on purpose, so zero here
            # is the setting working rather than the server struggling. Reading
            # "0.0 / 20" in red would send someone hunting a problem that is
            # not there.
            self.tps.setText("paused - no players")
            self.tps.setStyleSheet(f"{VALUE} color:#8b949e;")
            self.tps.setToolTip(
                "server.properties sets pause-when-empty-seconds, so the server "
                "stops ticking once it has been empty for that long. It resumes "
                "the moment someone joins."
            )
            self.mspt.setText("-")
            return

        self.tps.setToolTip("")
        if tps is None:
            self.tps.setText("measuring...")
            self.tps.setStyleSheet(f"{VALUE} color:#8b949e;")
        else:
            self.tps.setText(f"{tps:.1f} / 20")
            self.tps.setStyleSheet(f"{VALUE} color:{tps_health(tps)};")

        if mspt is None:
            self.mspt.setText("-")
        else:
            # 50 ms per tick is the budget; above that the server can't keep 20 TPS.
            over = mspt > 50
            self.mspt.setText(f"{mspt:.1f} ms" + ("  (over budget)" if over else ""))
            self.mspt.setStyleSheet(
                f"{VALUE} color:{'#e06c75' if over else '#5fb37a'};"
            )

    def set_uptime(self, seconds: float) -> None:
        self.uptime.setText(format_uptime(seconds))

    def set_players(self, online: int, maximum: int | None = None) -> None:
        self.players.setText(f"{online} of {maximum}" if maximum else str(online))

    def set_resources(self, stats, max_memory_mb: int) -> None:
        if stats is None:
            self.cpu_bar.setValue(0)
            self.memory_bar.setValue(0)
            self.memory_label.setText("-")
            self.threads.setText("-")
            return

        self.cpu_bar.setValue(int(round(stats.cpu_percent)))
        used_pct = int(round(100 * stats.memory_mb / max(1, max_memory_mb)))
        self.memory_bar.setValue(min(100, used_pct))
        self.memory_label.setText(
            f"{stats.memory_gb:.1f} GB of {max_memory_mb / 1024:.1f} GB allocated"
        )
        self.threads.setText(str(stats.threads))

    def set_address(self, address: str, verified: bool | None) -> None:
        self.address.setText(address or "-")
        if verified is None:
            self.reachable.setText("")
        elif verified:
            self.reachable.setText("Confirmed reachable from outside.")
        else:
            self.reachable.setText("Not confirmed - ask a friend to try it.")

    def clear(self) -> None:
        self.set_tps(None, None)
        self.set_resources(None, 1)
        self.set_players(0)
        self.uptime.setText("-")
