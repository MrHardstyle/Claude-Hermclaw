"""Host metrics for heartbeats: CPU, load, RAM, disk, uptime (``/proc`` + :mod:`shutil`, no psutil).

All readers degrade to zeros instead of failing: a missing ``/proc`` file must never stop heartbeats.
The ``proc`` root is injectable so the parsers are testable against fixture files.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from hermclaw.core.logging import get_logger

log = get_logger(__name__)

PROC = Path("/proc")


@dataclass(frozen=True)
class CpuTimes:
    idle: int
    total: int


@dataclass(frozen=True)
class SystemMetrics:
    cpu_percent: float
    load_avg: list[float]
    ram_total_mb: int
    ram_used_mb: int
    disk_free_mb: int
    host_uptime_seconds: int


def read_cpu_times(proc: Path = PROC) -> CpuTimes | None:
    """Aggregate ``cpu`` line of ``/proc/stat``; idle includes iowait."""
    try:
        with (proc / "stat").open(encoding="ascii") as fh:
            first = fh.readline()
    except OSError:
        return None
    parts = first.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        values = [int(v) for v in parts[1:]]
    except ValueError:
        return None
    # user nice system idle iowait irq softirq steal guest guest_nice; guest time is already in user/nice
    counted = values[:8]
    idle = counted[3] + (counted[4] if len(counted) > 4 else 0)
    return CpuTimes(idle=idle, total=sum(counted))


def cpu_percent_between(before: CpuTimes | None, after: CpuTimes | None) -> float:
    if before is None or after is None:
        return 0.0
    total = after.total - before.total
    idle = after.idle - before.idle
    if total <= 0:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 1)


def read_meminfo(proc: Path = PROC) -> tuple[int, int]:
    """``(total_mb, used_mb)`` where used = MemTotal - MemAvailable."""
    fields: dict[str, int] = {}
    try:
        text = (proc / "meminfo").read_text(encoding="ascii")
    except OSError:
        return 0, 0
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[key.strip()] = int(parts[0])  # kB
    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable", fields.get("MemFree", 0) + fields.get("Cached", 0) + fields.get("Buffers", 0))
    return total // 1024, max(0, total - available) // 1024


def read_loadavg(proc: Path = PROC) -> list[float]:
    try:
        parts = (proc / "loadavg").read_text(encoding="ascii").split()
        return [float(p) for p in parts[:3]]
    except (OSError, ValueError):
        try:
            return [round(v, 2) for v in os.getloadavg()]
        except OSError:
            return []


def read_uptime(proc: Path = PROC) -> int:
    try:
        return int(float((proc / "uptime").read_text(encoding="ascii").split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


def disk_free_mb(path: Path) -> int:
    """Free space for unprivileged users of the filesystem holding ``path`` (nearest existing parent)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return int(shutil.disk_usage(probe).free // (1024 * 1024))
    except OSError:
        return 0


class SystemSampler:
    """Stateful sampler: CPU percent is the utilisation between two consecutive :meth:`sample` calls."""

    def __init__(self, disk_path: Path, *, proc: Path = PROC) -> None:
        self.disk_path = disk_path
        self.proc = proc
        self._last_cpu = read_cpu_times(proc)

    def sample(self) -> SystemMetrics:
        now_cpu = read_cpu_times(self.proc)
        cpu = cpu_percent_between(self._last_cpu, now_cpu)
        if now_cpu is not None:
            self._last_cpu = now_cpu
        total, used = read_meminfo(self.proc)
        return SystemMetrics(
            cpu_percent=cpu,
            load_avg=read_loadavg(self.proc),
            ram_total_mb=total,
            ram_used_mb=used,
            disk_free_mb=disk_free_mb(self.disk_path),
            host_uptime_seconds=read_uptime(self.proc),
        )
