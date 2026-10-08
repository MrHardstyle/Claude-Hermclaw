"""NVIDIA GPU telemetry via ``nvidia-smi`` CSV (research 20261008-017).

Query::

    nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu,driver_version
               --format=csv,noheader,nounits

Values may be ``[N/A]``/``[Not Supported]`` on some boards; those become ``0``/``None``. A host without
``nvidia-smi`` reports ``available=False`` (not an error: the execution worker has no GPU).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import shutil
from dataclasses import dataclass, field

from hermclaw.contracts.worker import GpuInfo

GPU_QUERY_FIELDS = ("index", "name", "memory.total", "memory.used", "utilization.gpu", "temperature.gpu", "driver_version")
NVIDIA_SMI_ARGS = (f"--query-gpu={','.join(GPU_QUERY_FIELDS)}", "--format=csv,noheader,nounits")


@dataclass
class GpuQueryResult:
    available: bool
    gpus: list[GpuInfo] = field(default_factory=list)
    error: str | None = None

    @property
    def driver(self) -> str | None:
        return self.gpus[0].driver if self.gpus and self.gpus[0].driver else None


def _num(value: str) -> int | None:
    v = value.strip()
    if not v or v.startswith("["):
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def parse_nvidia_smi_csv(text: str) -> list[GpuInfo]:
    """Parse ``--format=csv,noheader,nounits`` output; malformed lines are skipped."""
    gpus: list[GpuInfo] = []
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        if len(row) < len(GPU_QUERY_FIELDS):
            continue
        index, name, mem_total, mem_used, util, temp, driver = (c.strip() for c in row[: len(GPU_QUERY_FIELDS)])
        idx = _num(index)
        if idx is None:
            continue
        gpus.append(
            GpuInfo(
                index=idx,
                name=name,
                memory_total_mb=_num(mem_total) or 0,
                memory_used_mb=_num(mem_used) or 0,
                utilization_percent=_num(util) or 0,
                temperature_c=_num(temp),
                driver="" if driver.startswith("[") else driver,
            )
        )
    return gpus


async def query_gpus(nvidia_smi: str = "nvidia-smi", *, timeout_seconds: float = 10.0) -> GpuQueryResult:
    exe = shutil.which(nvidia_smi)
    if exe is None:
        return GpuQueryResult(available=False, error="nvidia-smi not found")
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, *NVIDIA_SMI_ARGS, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        return GpuQueryResult(available=False, error=f"nvidia-smi failed to start: {exc.strerror}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return GpuQueryResult(available=False, error=f"nvidia-smi timed out after {timeout_seconds:.0f} s")
    if proc.returncode != 0:
        msg = (err or out).decode("utf-8", "replace").strip().splitlines()
        return GpuQueryResult(available=False, error=f"nvidia-smi exit {proc.returncode}: {msg[0] if msg else ''}"[:300])
    gpus = parse_nvidia_smi_csv(out.decode("utf-8", "replace"))
    if not gpus:
        return GpuQueryResult(available=False, error="nvidia-smi reported no GPUs")
    return GpuQueryResult(available=True, gpus=gpus)
