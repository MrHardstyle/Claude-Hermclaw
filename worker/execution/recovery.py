"""Cleanup and abandoned-container recovery of the execution sandbox (P18 steps 18.8 / 18.9).

Every sandbox container carries the managed label (``WORKER_CONTAINER_LABEL``, default
``hermclaw.managed=true``) plus ``hermclaw.request``/``hermclaw.job``/``hermclaw.step``. Containers are
normally removed by ``--rm`` or by the sandbox itself (timeout/cancel). Leftovers appear when the worker
crashes, is killed or loses the engine mid-run; :func:`recover_abandoned` finds them by label and removes
every container that is

* **not tracked** – not in the set of containers of commands currently running in this process
  (:meth:`ContainerSandbox.active_containers`), and
* **older than the threshold** – protects containers that another process sharing the label (a selftest,
  a second daemon) is just starting. Containers whose creation time cannot be parsed count as old.

:func:`prune_stale_workspaces` removes workspace directories (and interrupted ``.incoming-``/``.trash-``
uploads) nobody touched for a given time; :func:`run_periodic_recovery` combines both as a maintenance
loop. Nothing here reads container output or environment, so no secrets can end up in the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import stat
import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from hermclaw.core.errors import ExternalServiceError
from hermclaw.core.logging import get_logger
from worker.execution.sandbox import LABEL_JOB, LABEL_REQUEST, LABEL_STEP, ContainerSandbox, default_managed_label, engine_exec

log = get_logger(__name__)

ContainerEngine = Literal["podman", "docker"]
#: untracked managed containers younger than this are left alone (they may be starting in another process)
DEFAULT_ABANDON_AFTER_SECONDS = 60.0
_TIME_RE = re.compile(r"^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)(?:\.(\d+))?\s*(Z|[+-]\d\d:?\d\d)?(?:\s+\S+)?$")


@dataclass(frozen=True)
class ContainerRecord:
    id: str
    name: str
    state: str
    created_at: datetime | None
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def request_id(self) -> str | None:
        return self.labels.get(LABEL_REQUEST)

    def age_seconds(self, now: datetime) -> float | None:
        if self.created_at is None:
            return None
        return (now - self.created_at).total_seconds()


@dataclass
class RecoveryReport:
    engine: str
    label: str
    dry_run: bool = False
    scanned: int = 0
    removed: list[str] = field(default_factory=list)
    kept: dict[str, str] = field(default_factory=dict)  # name -> reason
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "label": self.label,
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "removed": list(self.removed),
            "kept": dict(self.kept),
            "errors": list(self.errors),
        }


def parse_engine_time(value: str | None) -> datetime | None:
    """Parse podman/docker timestamps (RFC 3339 with nanoseconds, or ``2026-10-09 10:00:00 +0000 UTC``)."""
    if not value:
        return None
    m = _TIME_RE.match(value.strip())
    if m is None:
        return None
    base, fraction, zone = m.group(1).replace(" ", "T"), (m.group(2) or "0")[:6].ljust(6, "0"), m.group(3) or "Z"
    if zone == "Z":
        zone = "+00:00"
    elif ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"
    try:
        return datetime.fromisoformat(f"{base}.{fraction}{zone}")
    except ValueError:
        return None


def _record(raw: dict[str, Any]) -> ContainerRecord | None:
    cid = str(raw.get("Id") or raw.get("ID") or "")
    if not cid:
        return None
    state = raw.get("State")
    status = str(state.get("Status") or "") if isinstance(state, dict) else str(state or "")
    config = raw.get("Config") if isinstance(raw.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config, dict) else None
    return ContainerRecord(
        id=cid,
        name=str(raw.get("Name") or "").lstrip("/") or cid[:12],
        state=status,
        created_at=parse_engine_time(str(raw.get("Created") or "")),
        labels={str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {},
    )


def _parse_inspect(out: str) -> list[ContainerRecord]:
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    return [r for r in (_record(item) for item in items if isinstance(item, dict)) if r is not None]


async def list_managed_containers(executable: str, *, label: str | None = None) -> list[ContainerRecord]:
    """All containers (any state) carrying ``label`` (``key`` or ``key=value``; default
    :func:`~worker.execution.sandbox.default_managed_label`)."""
    label = label or default_managed_label()
    rc, out, err = await engine_exec([executable, "ps", "-a", "-q", "--no-trunc", "--filter", f"label={label}"], timeout_seconds=60)
    if rc != 0:
        raise ExternalServiceError(
            f"listing sandbox containers failed: {(err.strip() or f'exit {rc}')[:300]}", code="CONTAINER_LIST_FAILED"
        )
    ids = sorted({line.strip() for line in out.splitlines() if line.strip()})
    if not ids:
        return []
    rc, out, _err = await engine_exec([executable, "container", "inspect", *ids], timeout_seconds=60)
    if rc == 0:
        records = _parse_inspect(out)
        if len(records) == len(ids):
            return sorted(records, key=lambda r: r.name)
    # some container vanished between ps and inspect (``--rm`` finishing): inspect one by one
    records = []
    for cid in ids:
        rc, out, _err = await engine_exec([executable, "container", "inspect", cid], timeout_seconds=30)
        if rc == 0:
            records.extend(_parse_inspect(out))
    return sorted(records, key=lambda r: r.name)


async def remove_containers(executable: str, names: Iterable[str], *, engine: ContainerEngine = "podman") -> tuple[list[str], list[str]]:
    """Kill and force-remove containers (18.8). Returns ``(removed, errors)``; already-gone counts as removed."""
    removed: list[str] = []
    errors: list[str] = []
    for name in names:
        await engine_exec([executable, "kill", "-s", "KILL", name], timeout_seconds=30)
        argv = [executable, "rm", "-f", "-i", "-t", "0", name] if engine == "podman" else [executable, "rm", "-f", name]
        rc, _out, err = await engine_exec(argv, timeout_seconds=120)
        if rc == 0 or "no such container" in err.lower():
            removed.append(name)
        else:
            errors.append(f"{name}: {(err.strip() or f'exit {rc}')[:200]}")
    return removed, errors


async def recover_abandoned(
    *,
    engine: ContainerEngine = "podman",
    executable: str | None = None,
    label: str | None = None,
    older_than_seconds: float = DEFAULT_ABANDON_AFTER_SECONDS,
    tracked: Collection[str] | Callable[[], Collection[str]] = (),
    dry_run: bool = False,
    now: datetime | None = None,
) -> RecoveryReport:
    """Remove managed containers that are neither tracked nor younger than ``older_than_seconds`` (18.9).

    ``tracked`` holds container names or ids that belong to running commands, or a callable returning them;
    the callable is evaluated *after* the listing, so a command that registered while the engine was being
    queried is never mistaken for a leftover. With ``dry_run`` the candidates are reported in ``removed``
    without touching them."""
    exe = executable or engine
    label = label or default_managed_label()
    report = RecoveryReport(engine=engine, label=label, dry_run=dry_run)
    containers = await list_managed_containers(exe, label=label)
    tracked_now = frozenset(tracked() if callable(tracked) else tracked)
    report.scanned = len(containers)
    current = now or datetime.now(UTC)
    candidates: list[str] = []
    for c in containers:
        if c.name in tracked_now or c.id in tracked_now:
            report.kept[c.name] = "tracked"
            continue
        age = c.age_seconds(current)
        if age is not None and age < older_than_seconds:
            report.kept[c.name] = f"younger than {older_than_seconds:g}s"
            continue
        candidates.append(c.name)
    if dry_run:
        report.removed = candidates
    elif candidates:
        report.removed, report.errors = await remove_containers(exe, candidates, engine=engine)
    if report.removed or report.errors:
        log.info(
            "abandoned sandbox containers recovered",
            extra={
                "engine": engine,
                "label": label,
                "dry_run": dry_run,
                "removed": report.removed,
                "errors": report.errors,
                "kept": len(report.kept),
                "requests": sorted({c.labels.get(LABEL_REQUEST, "") for c in containers if c.name in report.removed} - {""}),
                "jobs": sorted({c.labels.get(LABEL_JOB, "") for c in containers if c.name in report.removed} - {""}),
                "steps": sorted({c.labels.get(LABEL_STEP, "") for c in containers if c.name in report.removed} - {""}),
            },
        )
    return report


async def recover_for_sandbox(
    sandbox: ContainerSandbox, *, older_than_seconds: float = DEFAULT_ABANDON_AFTER_SECONDS, dry_run: bool = False
) -> RecoveryReport:
    """:func:`recover_abandoned` with the engine, executable, label and running containers of ``sandbox``."""
    return await recover_abandoned(
        engine=sandbox.engine,
        executable=sandbox.executable,
        label=sandbox.managed_label,
        older_than_seconds=older_than_seconds,
        tracked=sandbox.active_containers,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------------------------- workspaces
def _newest_mtime(path: Path) -> float:
    """Newest mtime of ``path`` and everything below it (symlinks are not followed)."""
    newest = os.lstat(path).st_mtime
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        return newest
    for _dirpath, dirnames, filenames, dfd in os.fwalk(path, follow_symlinks=False):
        for name in (*dirnames, *filenames):
            with contextlib.suppress(FileNotFoundError):
                newest = max(newest, os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mtime)
    return newest


def prune_stale_workspaces(
    root: Path, *, older_than_seconds: float, keep: Collection[str] = (), now: float | None = None, dry_run: bool = False
) -> list[str]:
    """Remove workspace directories below ``root`` untouched for ``older_than_seconds`` (18.8).

    ``keep`` lists workspace ids in use (locked, running commands). Only direct children of ``root`` are
    considered; symlinks there are removed as links, never followed. Returns the removed names."""
    if not root.is_dir():
        return []
    current = time.time() if now is None else now
    removed: list[str] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.name in keep:
            continue
        try:
            if current - _newest_mtime(child) < older_than_seconds:
                continue
            if not dry_run:
                if child.is_symlink() or not child.is_dir():
                    child.unlink()
                else:
                    shutil.rmtree(child)
            removed.append(child.name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("workspace prune failed", extra={"workspace": child.name, "error": str(exc)[:200]})
    if removed:
        log.info("stale workspaces pruned", extra={"root": str(root), "removed": removed, "dry_run": dry_run})
    return removed


async def run_periodic_recovery(
    sandbox: ContainerSandbox,
    *,
    interval_seconds: float,
    stop: asyncio.Event,
    older_than_seconds: float = DEFAULT_ABANDON_AFTER_SECONDS,
    workspaces_root: Path | None = None,
    workspace_max_age_seconds: float | None = None,
    busy_workspaces: Callable[[], Collection[str]] | None = None,
) -> None:
    """Maintenance loop: container recovery (and optional workspace pruning) every ``interval_seconds``
    until ``stop`` is set. ``busy_workspaces`` returns the workspace ids currently in use (evaluated on
    every round). Errors are logged and never end the loop."""
    while not stop.is_set():
        try:
            await recover_for_sandbox(sandbox, older_than_seconds=older_than_seconds)
        except Exception as exc:
            log.warning("periodic container recovery failed", extra={"error": str(exc)[:300]})
        if workspaces_root is not None and workspace_max_age_seconds is not None:
            try:
                await asyncio.to_thread(
                    prune_stale_workspaces,
                    workspaces_root,
                    older_than_seconds=workspace_max_age_seconds,
                    keep=set(busy_workspaces() if busy_workspaces else ()),
                )
            except Exception as exc:
                log.warning("periodic workspace prune failed", extra={"error": str(exc)[:300]})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval_seconds)
