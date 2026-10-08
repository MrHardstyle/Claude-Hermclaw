"""Worker registry: registration, heartbeat ingest, capability registry, offline detection (Bauplan §24, P07).

PostgreSQL is the single source of truth (tables ``workers``, ``worker_capabilities``, ``worker_health``).
Every observable change is written to the event store in the caller's transaction:

- ``worker.registered`` – a worker row was created (from config or an authenticated first heartbeat) or
  its configuration (address, api_url, kind, WOL) changed
- ``worker.state``      – the effective state changed (payload ``from``/``to``/``reason``) or the reported
  capability set changed (``reason=capabilities_changed``, ``from == to``)
- ``worker.offline``    – the offline sweep declared a worker offline (missed heartbeats / wake timeout) or
  the worker announced its shutdown with a final ``offline`` heartbeat (``reason=worker_shutdown``)

State ownership: a live worker *reports* ``starting|ready|busy|draining|error|offline`` in its heartbeat.
The orchestrator owns ``offline`` (sweep), ``sleeping``/``waking`` (Wake-on-LAN controller via
:meth:`WorkerRegistry.set_state`) and the sticky operator drain flag (:meth:`WorkerRegistry.set_drain`).
A protocol-version mismatch (7.9) or a kind mismatch forces ``error`` regardless of the reported state.

The caller owns the transaction (``await session.commit()``); methods only ``flush``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Table, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.common import Severity, WorkerKind, WorkerState
from hermclaw.contracts.events import EventType
from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION, WorkerHeartbeat
from hermclaw.core.config import CapabilitiesConfig, HostConfig, HostsConfig
from hermclaw.core.logging import get_logger
from hermclaw.events.store import append_event
from hermclaw.persistence.models import Worker, WorkerCapability, WorkerHealth
from hermclaw.workers.errors import WorkerNotFound
from hermclaw.workers.schemas import WorkerDetail, WorkerHealthSample, WorkerInfo, WorkerResources

log = get_logger(__name__)

SOURCE_TYPE = "worker_registry"
ROLE_KIND: dict[str, WorkerKind] = {"execution_worker": WorkerKind.execution, "model_worker": WorkerKind.model}
DEFAULT_WORKER_PORT = 8787
# states in which the worker is expected to heartbeat (sweep candidates)
_LIVE_STATES = frozenset({WorkerState.starting, WorkerState.ready, WorkerState.busy, WorkerState.draining, WorkerState.error})
# states the orchestrator may set explicitly
_ADMIN_STATES = frozenset({WorkerState.offline, WorkerState.sleeping, WorkerState.waking, WorkerState.error})
_OUT_OF_ORDER_WINDOW = timedelta(seconds=120)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class RegistrySettings:
    """Tuning of the registry. Defaults match the worker daemons (``WORKER_HEARTBEAT_SECONDS=15``)."""

    heartbeat_interval_seconds: float = 15.0
    offline_after_missed: int = 3
    waking_timeout_seconds: float = 600.0
    auto_register: bool = True
    health_retention_days: int = 7

    @property
    def offline_after(self) -> timedelta:
        return timedelta(seconds=self.heartbeat_interval_seconds * self.offline_after_missed)


@dataclass
class HeartbeatOutcome:
    worker_id: str
    accepted: bool
    state: WorkerState
    previous_state: WorkerState | None
    compatible: bool
    created: bool = False
    stale: bool = False
    reason: str = "heartbeat"
    capabilities_added: list[str] = field(default_factory=list)
    capabilities_removed: list[str] = field(default_factory=list)

    @property
    def state_changed(self) -> bool:
        return self.previous_state != self.state


class WorkerRegistry:
    def __init__(self, settings: RegistrySettings | None = None, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self.settings = settings or RegistrySettings()
        self._clock = clock

    def now(self) -> datetime:
        return _aware(self._clock())

    # ------------------------------------------------------------------------------------- registration
    async def register_from_config(
        self,
        session: AsyncSession,
        hosts: HostsConfig,
        capabilities: CapabilitiesConfig | None = None,
    ) -> list[Worker]:
        """Create/update one ``workers`` row per host with role ``execution_worker``/``model_worker``.

        Existing rows keep their state; config fields (address, api_url, kind, WOL, labels, declared
        capabilities) are refreshed. Rows of workers no longer in the config are marked ``in_config=false``.
        """
        out: list[Worker] = []
        seen: set[str] = set()
        for host in hosts.hosts:
            kind = self._host_kind(host)
            if kind is None:
                continue
            seen.add(host.id)
            out.append(await self._upsert_host(session, host, kind, capabilities))
        stale = (await session.execute(select(Worker).where(Worker.id.not_in(seen) if seen else Worker.id.is_not(None)))).scalars()
        for row in stale:
            meta = dict(row.metadata_ or {})
            if meta.get("source") == "config" and meta.get("in_config", True):
                meta["in_config"] = False
                row.metadata_ = meta
                await append_event(
                    session,
                    EventType.WORKER_REGISTERED,
                    source_type=SOURCE_TYPE,
                    source_id=row.id,
                    severity=Severity.warning,
                    payload={"worker_id": row.id, "action": "removed_from_config"},
                )
        await session.flush()
        return out

    @staticmethod
    def _host_kind(host: HostConfig) -> WorkerKind | None:
        if host.role not in ROLE_KIND:
            return None
        return WorkerKind(host.worker_kind) if host.worker_kind else ROLE_KIND[host.role]

    async def _upsert_host(
        self, session: AsyncSession, host: HostConfig, kind: WorkerKind, capabilities: CapabilitiesConfig | None
    ) -> Worker:
        api_url = (host.worker_api or f"http://{host.address}:{DEFAULT_WORKER_PORT}").rstrip("/")
        wol = host.wake_on_lan.model_dump()
        declared = sorted(c.name for c in capabilities.capabilities if c.worker_kind == kind.value) if capabilities else []
        row = (await session.execute(select(Worker).where(Worker.id == host.id).with_for_update())).scalar_one_or_none()
        if row is None:
            row = Worker(
                id=host.id,
                hostname=host.id,
                address=host.address,
                kind=kind.value,
                state=WorkerState.offline.value,
                api_url=api_url,
                wol=wol,
                metadata_={
                    "source": "config",
                    "in_config": True,
                    "labels": dict(host.labels),
                    "declared_capabilities": declared,
                    "state_reason": "registered",
                    "state_changed_at": _iso(self.now()),
                },
            )
            session.add(row)
            await session.flush()
            await append_event(
                session,
                EventType.WORKER_REGISTERED,
                source_type=SOURCE_TYPE,
                source_id=host.id,
                payload={"worker_id": host.id, "kind": kind.value, "address": host.address, "api_url": api_url, "source": "config"},
            )
            return row
        changes: list[str] = []
        if row.address != host.address:
            row.address, changes = host.address, [*changes, "address"]
        if row.api_url != api_url:
            row.api_url, changes = api_url, [*changes, "api_url"]
        if row.kind != kind.value:
            row.kind, changes = kind.value, [*changes, "kind"]
        if (row.wol or {}) != wol:
            row.wol, changes = wol, [*changes, "wol"]
        meta = dict(row.metadata_ or {})
        new_meta = {**meta, "source": "config", "in_config": True, "labels": dict(host.labels), "declared_capabilities": declared}
        if new_meta != meta:
            row.metadata_ = new_meta
            if meta.get("declared_capabilities") != declared or not meta.get("in_config", True):
                changes.append("metadata")
        if changes:
            await append_event(
                session,
                EventType.WORKER_REGISTERED,
                source_type=SOURCE_TYPE,
                source_id=host.id,
                payload={"worker_id": host.id, "action": "config_updated", "changed": changes},
            )
        await session.flush()
        return row

    # ------------------------------------------------------------------------------------- heartbeats
    async def ingest_heartbeat(
        self,
        session: AsyncSession,
        hb: WorkerHeartbeat,
        *,
        remote_addr: str | None = None,
        received_at: datetime | None = None,
    ) -> HeartbeatOutcome:
        """Apply one (already authenticated) heartbeat. Concurrent heartbeats serialize on the row lock."""
        now = _aware(received_at) if received_at else self.now()
        row = (await session.execute(select(Worker).where(Worker.id == hb.worker_id).with_for_update())).scalar_one_or_none()
        created = False
        if row is None:
            if not self.settings.auto_register:
                raise WorkerNotFound(f"worker '{hb.worker_id}' is not registered", details={"worker_id": hb.worker_id})
            row, created = await self._register_from_heartbeat(session, hb, remote_addr, now)

        meta: dict[str, Any] = dict(row.metadata_ or {})
        previous = WorkerState(row.state)
        sent_at = _aware(hb.sent_at)
        last_sent = _parse_dt(meta.get("last_sent_at"))
        if last_sent is not None and sent_at <= last_sent and last_sent - sent_at <= _OUT_OF_ORDER_WINDOW:
            # duplicate or out-of-order delivery: never let an older snapshot overwrite a newer one
            return HeartbeatOutcome(
                worker_id=row.id,
                accepted=False,
                stale=True,
                state=previous,
                previous_state=previous,
                compatible=bool(meta.get("compatible", True)),
                reason="stale_heartbeat",
            )

        compatible = hb.protocol_version == WORKER_PROTOCOL_VERSION
        kind_ok = hb.kind.value == row.kind
        reported = WorkerState(hb.state)
        incompat: dict[str, Any] | None = None
        if not compatible:
            new_state, reason = WorkerState.error, "protocol_version_mismatch"
            incompat = {"reason": reason, "expected": WORKER_PROTOCOL_VERSION, "got": hb.protocol_version}
        elif not kind_ok:
            new_state, reason = WorkerState.error, "kind_mismatch"
            incompat = {"reason": reason, "expected": row.kind, "got": hb.kind.value}
        elif meta.get("admin_drain") and reported in (WorkerState.ready, WorkerState.busy):
            new_state, reason = WorkerState.draining, "admin_drain"
        else:
            new_state, reason = reported, "heartbeat"

        row.hostname = hb.hostname[:200]
        row.worker_version = hb.worker_version[:64]
        row.protocol_version = hb.protocol_version
        row.last_heartbeat_at = now
        row.active_job_id = hb.active_job[:64] if hb.active_job else None
        row.active_step_id = hb.active_step[:64] if hb.active_step else None
        resources = WorkerResources(
            cpu_percent=hb.cpu_percent,
            load_avg=list(hb.load_avg),
            ram_total_mb=hb.ram_total_mb,
            ram_used_mb=hb.ram_used_mb,
            disk_free_mb=hb.disk_free_mb,
            gpus=list(hb.gpus),
            loaded_models=list(hb.loaded_models),
            uptime_seconds=hb.uptime_seconds,
        )
        meta.update(
            {
                "last_sent_at": _iso(sent_at),
                "clock_skew_seconds": round((now - sent_at).total_seconds(), 3),
                "service_versions": dict(hb.service_versions),
                "resources": resources.model_dump(mode="json"),
                "compatible": compatible and kind_ok,
                "incompatibility": incompat,
                "reported_state": reported.value,
            }
        )
        if remote_addr:
            meta["last_remote_addr"] = remote_addr

        added, removed = await self._sync_capabilities(session, row.id, hb.capabilities, hb.worker_version)
        session.add(
            WorkerHealth(
                worker_id=row.id,
                state=new_state.value,
                cpu_percent=hb.cpu_percent,
                ram_total_mb=hb.ram_total_mb,
                ram_used_mb=hb.ram_used_mb,
                disk_free_mb=hb.disk_free_mb,
                gpus=[g.model_dump(mode="json") for g in hb.gpus],
                loaded_models=[m.model_dump(mode="json") for m in hb.loaded_models],
                active_job=row.active_job_id,
                active_step=row.active_step_id,
                uptime_seconds=hb.uptime_seconds,
                service_versions=dict(hb.service_versions),
            )
        )
        if new_state != previous:
            extra: dict[str, Any] = {"reported": reported.value}
            if incompat:
                extra["incompatibility"] = incompat
            meta = self._apply_state(row, meta, new_state, reason, now)
            await self._state_event(session, row.id, previous, new_state, reason=reason, extra=extra)
            if new_state == WorkerState.offline:
                # graceful shutdown (final heartbeat): same downstream signal as a missed-heartbeat timeout,
                # so the scheduler can requeue the worker's active step without waiting for the sweep
                await append_event(
                    session,
                    EventType.WORKER_OFFLINE,
                    source_type=SOURCE_TYPE,
                    source_id=row.id,
                    severity=Severity.warning,
                    payload={
                        "worker_id": row.id,
                        "reason": "worker_shutdown",
                        "previous_state": previous.value,
                        "last_heartbeat_at": _iso(now),
                        "active_job": row.active_job_id,
                        "active_step": row.active_step_id,
                    },
                )
        else:
            meta["state_reason"] = reason
        if added or removed:
            await append_event(
                session,
                EventType.WORKER_STATE,
                source_type=SOURCE_TYPE,
                source_id=row.id,
                payload={
                    "worker_id": row.id,
                    "from": new_state.value,
                    "to": new_state.value,
                    "reason": "capabilities_changed",
                    "added": added,
                    "removed": removed,
                },
            )
        row.metadata_ = meta
        await session.flush()
        if incompat:
            log.error("worker incompatible", extra={"worker_id": row.id, **incompat})
        return HeartbeatOutcome(
            worker_id=row.id,
            accepted=True,
            state=new_state,
            previous_state=None if created else previous,
            compatible=compatible and kind_ok,
            created=created,
            reason=reason,
            capabilities_added=added,
            capabilities_removed=removed,
        )

    async def _register_from_heartbeat(
        self, session: AsyncSession, hb: WorkerHeartbeat, remote_addr: str | None, now: datetime
    ) -> tuple[Worker, bool]:
        """Insert the row unless a concurrent first heartbeat (or config registration) won the race;
        either way return the row locked ``FOR UPDATE`` plus whether *this* call created it."""
        address = (remote_addr or hb.hostname)[:200]
        table = cast(Table, Worker.__table__)
        insert_stmt = (
            pg_insert(table)
            .values(
                id=hb.worker_id,
                hostname=hb.hostname[:200],
                address=address,
                kind=hb.kind.value,
                state=WorkerState.offline.value,
                api_url=None,
                wol={},
                metadata={"source": "heartbeat", "in_config": False, "state_reason": "registered", "state_changed_at": _iso(now)},
            )
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(table.c.id)
        )
        inserted = (await session.execute(insert_stmt)).scalar_one_or_none() is not None
        locked = select(Worker).where(Worker.id == hb.worker_id).with_for_update().execution_options(populate_existing=True)
        row = (await session.execute(locked)).scalar_one()
        if not inserted:
            return row, False
        await append_event(
            session,
            EventType.WORKER_REGISTERED,
            source_type=SOURCE_TYPE,
            source_id=hb.worker_id,
            severity=Severity.warning,
            payload={"worker_id": hb.worker_id, "kind": hb.kind.value, "address": address, "source": "heartbeat"},
        )
        return row, True

    async def _sync_capabilities(
        self, session: AsyncSession, worker_id: str, capabilities: Iterable[str], version: str
    ) -> tuple[list[str], list[str]]:
        wanted = sorted({c.strip()[:64] for c in capabilities if c and c.strip()})
        cap_q = select(WorkerCapability.capability).where(WorkerCapability.worker_id == worker_id)
        existing = set((await session.execute(cap_q)).scalars())
        added = sorted(set(wanted) - existing)
        removed = sorted(existing - set(wanted))
        if removed:
            await session.execute(
                delete(WorkerCapability).where(WorkerCapability.worker_id == worker_id).where(WorkerCapability.capability.in_(removed))
            )
        if wanted:
            stmt = pg_insert(WorkerCapability).values(
                [{"worker_id": worker_id, "capability": c, "version": version[:64], "details": {"source": "heartbeat"}} for c in wanted]
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[WorkerCapability.worker_id, WorkerCapability.capability],
                set_={"version": stmt.excluded.version, "details": stmt.excluded.details, "updated_at": func.now()},
            )
            await session.execute(stmt)
        return added, removed

    # ------------------------------------------------------------------------------------- state control
    @staticmethod
    def _apply_state(row: Worker, meta: dict[str, Any], state: WorkerState, reason: str, now: datetime) -> dict[str, Any]:
        row.state = state.value
        meta = {**meta, "state_reason": reason, "state_changed_at": _iso(now)}
        row.metadata_ = meta
        return meta

    async def _state_event(
        self,
        session: AsyncSession,
        worker_id: str,
        previous: WorkerState,
        new: WorkerState,
        *,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        severity = Severity.error if new == WorkerState.error else Severity.warning if new == WorkerState.offline else Severity.info
        await append_event(
            session,
            EventType.WORKER_STATE,
            source_type=SOURCE_TYPE,
            source_id=worker_id,
            severity=severity,
            payload={"worker_id": worker_id, "from": previous.value, "to": new.value, "reason": reason, **(extra or {})},
        )

    async def set_state(self, session: AsyncSession, worker_id: str, state: WorkerState | str, *, reason: str) -> bool:
        """Orchestrator-owned transition (``offline``, ``sleeping``, ``waking``, ``error``). Returns ``True`` on change."""
        target = WorkerState(state)
        if target not in _ADMIN_STATES:
            raise ValueError(f"state '{target}' is reported by the worker itself and cannot be set by the orchestrator")
        row = await self._locked(session, worker_id)
        previous = WorkerState(row.state)
        if previous == target:
            return False
        self._apply_state(row, dict(row.metadata_ or {}), target, reason, self.now())
        await self._state_event(session, worker_id, previous, target, reason=reason)
        if target == WorkerState.offline:
            await append_event(
                session,
                EventType.WORKER_OFFLINE,
                source_type=SOURCE_TYPE,
                source_id=worker_id,
                severity=Severity.warning,
                payload={"worker_id": worker_id, "reason": reason},
            )
        await session.flush()
        return True

    async def set_drain(self, session: AsyncSession, worker_id: str, drain: bool, *, reason: str = "operator") -> WorkerState:
        """Sticky operator drain: a draining worker finishes its work but is never selected for new work."""
        row = await self._locked(session, worker_id)
        meta = dict(row.metadata_ or {})
        if bool(meta.get("admin_drain")) == drain:
            return WorkerState(row.state)
        meta["admin_drain"] = drain
        previous = WorkerState(row.state)
        new = previous
        if drain and previous in (WorkerState.ready, WorkerState.busy):
            new = WorkerState.draining
        elif not drain and previous == WorkerState.draining:
            reported = meta.get("reported_state")
            new = WorkerState(reported) if reported in (WorkerState.ready, WorkerState.busy) else WorkerState.ready
        row.metadata_ = meta
        if new != previous:
            self._apply_state(row, meta, new, "admin_drain" if drain else "admin_undrain", self.now())
        await self._state_event(
            session,
            worker_id,
            previous,
            new,
            reason="admin_drain" if drain else "admin_undrain",
            extra={"drain": drain, "note": reason[:200]},
        )
        await session.flush()
        return new

    async def _locked(self, session: AsyncSession, worker_id: str) -> Worker:
        row = (await session.execute(select(Worker).where(Worker.id == worker_id).with_for_update())).scalar_one_or_none()
        if row is None:
            raise WorkerNotFound(f"worker '{worker_id}' is not registered", details={"worker_id": worker_id})
        return row

    # ------------------------------------------------------------------------------------- offline detection
    async def sweep_offline(self, session: AsyncSession, *, now: datetime | None = None) -> list[str]:
        """Declare workers offline whose last heartbeat is older than ``offline_after_missed * interval``
        (7.8), and ``waking`` workers that did not come up within ``waking_timeout_seconds``.
        Rows locked by a concurrent heartbeat are skipped (``SKIP LOCKED``) and handled next sweep."""
        t = _aware(now) if now else self.now()
        cutoff = t - self.settings.offline_after
        live = [s.value for s in _LIVE_STATES]
        stmt = (
            select(Worker)
            .where(Worker.state.in_([*live, WorkerState.waking.value]))
            .where(or_(Worker.last_heartbeat_at.is_(None), Worker.last_heartbeat_at < cutoff, Worker.state == WorkerState.waking.value))
            .with_for_update(skip_locked=True)
        )
        went_offline: list[str] = []
        for row in (await session.execute(stmt)).scalars():
            meta = dict(row.metadata_ or {})
            previous = WorkerState(row.state)
            last = _aware(row.last_heartbeat_at) if row.last_heartbeat_at else None
            if previous == WorkerState.waking:
                changed_at = _parse_dt(meta.get("state_changed_at")) or (_aware(row.updated_at) if row.updated_at else t)
                if last is not None and last >= changed_at:
                    continue  # a heartbeat already arrived after the wake started; state is updated by it
                if (t - changed_at).total_seconds() < self.settings.waking_timeout_seconds:
                    continue
                reason = "wake_timeout"
            else:
                reason = "heartbeat_timeout"
            silent = round((t - last).total_seconds(), 1) if last else None
            self._apply_state(row, meta, WorkerState.offline, reason, t)
            await self._state_event(session, row.id, previous, WorkerState.offline, reason=reason, extra={"silent_seconds": silent})
            await append_event(
                session,
                EventType.WORKER_OFFLINE,
                source_type=SOURCE_TYPE,
                source_id=row.id,
                severity=Severity.warning,
                payload={
                    "worker_id": row.id,
                    "reason": reason,
                    "previous_state": previous.value,
                    "last_heartbeat_at": _iso(last),
                    "silent_seconds": silent,
                    "threshold_seconds": self.settings.offline_after.total_seconds(),
                    "active_job": row.active_job_id,
                    "active_step": row.active_step_id,
                },
            )
            went_offline.append(row.id)
            log.warning("worker offline", extra={"worker_id": row.id, "reason": reason, "silent_seconds": silent})
        await session.flush()
        return went_offline

    async def prune_health(self, session: AsyncSession, *, older_than_days: int | None = None, now: datetime | None = None) -> int:
        days = self.settings.health_retention_days if older_than_days is None else older_than_days
        cutoff = (_aware(now) if now else self.now()) - timedelta(days=days)
        result = await session.execute(delete(WorkerHealth).where(WorkerHealth.created_at < cutoff))
        return int(getattr(result, "rowcount", 0) or 0)

    # ------------------------------------------------------------------------------------- queries
    async def _capabilities(self, session: AsyncSession, worker_ids: Sequence[str]) -> dict[str, list[str]]:
        if not worker_ids:
            return {}
        rows = await session.execute(
            select(WorkerCapability.worker_id, WorkerCapability.capability)
            .where(WorkerCapability.worker_id.in_(list(worker_ids)))
            .order_by(WorkerCapability.capability)
        )
        out: dict[str, list[str]] = {wid: [] for wid in worker_ids}
        for wid, cap in rows:
            out[wid].append(cap)
        return out

    def to_info(self, row: Worker, capabilities: list[str], *, now: datetime | None = None) -> WorkerInfo:
        meta = dict(row.metadata_ or {})
        t = _aware(now) if now else self.now()
        last = _aware(row.last_heartbeat_at) if row.last_heartbeat_at else None
        res = meta.get("resources")
        return WorkerInfo(
            id=row.id,
            hostname=row.hostname,
            address=row.address,
            kind=WorkerKind(row.kind),
            state=WorkerState(row.state),
            api_url=row.api_url,
            worker_version=row.worker_version,
            protocol_version=row.protocol_version,
            compatible=bool(meta.get("compatible", True)),
            last_heartbeat_at=last,
            heartbeat_age_seconds=round((t - last).total_seconds(), 3) if last else None,
            active_job=row.active_job_id,
            active_step=row.active_step_id,
            capabilities=capabilities,
            declared_capabilities=list(meta.get("declared_capabilities") or []),
            wol_enabled=bool((row.wol or {}).get("enabled", False)),
            admin_drain=bool(meta.get("admin_drain", False)),
            state_reason=meta.get("state_reason"),
            state_changed_at=_parse_dt(meta.get("state_changed_at")),
            service_versions={str(k): str(v) for k, v in (meta.get("service_versions") or {}).items()},
            resources=WorkerResources.model_validate(res) if isinstance(res, dict) else None,
            in_config=bool(meta.get("in_config", True)),
        )

    async def list_workers(
        self, session: AsyncSession, *, kind: WorkerKind | str | None = None, state: WorkerState | str | None = None
    ) -> list[WorkerInfo]:
        stmt = select(Worker).order_by(Worker.id)
        if kind is not None:
            stmt = stmt.where(Worker.kind == WorkerKind(kind).value)
        if state is not None:
            stmt = stmt.where(Worker.state == WorkerState(state).value)
        rows = list((await session.execute(stmt)).scalars())
        caps = await self._capabilities(session, [r.id for r in rows])
        now = self.now()
        return [self.to_info(r, caps.get(r.id, []), now=now) for r in rows]

    async def get_worker(self, session: AsyncSession, worker_id: str, *, health_limit: int = 0) -> WorkerDetail:
        row = await session.get(Worker, worker_id, populate_existing=True)
        if row is None:
            raise WorkerNotFound(f"worker '{worker_id}' is not registered", details={"worker_id": worker_id})
        caps = await self._capabilities(session, [worker_id])
        info = self.to_info(row, caps.get(worker_id, []))
        samples: list[WorkerHealthSample] = []
        if health_limit > 0:
            health_q = (
                select(WorkerHealth)
                .where(WorkerHealth.worker_id == worker_id)
                .order_by(WorkerHealth.id.desc())
                .limit(min(health_limit, 1000))
            )
            rows = await session.execute(health_q)
            samples = [
                WorkerHealthSample(
                    created_at=h.created_at,
                    state=WorkerState(h.state),
                    cpu_percent=h.cpu_percent,
                    ram_total_mb=h.ram_total_mb,
                    ram_used_mb=h.ram_used_mb,
                    disk_free_mb=h.disk_free_mb,
                    gpus=list(h.gpus or []),
                    loaded_models=list(h.loaded_models or []),
                    active_job=h.active_job,
                    active_step=h.active_step,
                    uptime_seconds=h.uptime_seconds,
                )
                for h in rows.scalars()
            ]
        return WorkerDetail(**info.model_dump(), health=samples)

    async def select_worker(
        self,
        session: AsyncSession,
        capability: str,
        *,
        kind: WorkerKind | str | None = None,
        include_busy: bool = False,
        exclude: Iterable[str] = (),
        now: datetime | None = None,
    ) -> WorkerInfo | None:
        """A dispatchable worker that *reported* ``capability``: state ``ready`` (or ``busy`` if allowed),
        fresh heartbeat, compatible protocol, known ``api_url``, not drained. Prefers idle, then most
        recently seen."""
        t = _aware(now) if now else self.now()
        states = [WorkerState.ready.value] + ([WorkerState.busy.value] if include_busy else [])
        stmt = (
            select(Worker)
            .join(WorkerCapability, WorkerCapability.worker_id == Worker.id)
            .where(WorkerCapability.capability == capability)
            .where(Worker.state.in_(states))
            .where(Worker.protocol_version == WORKER_PROTOCOL_VERSION)
            .where(Worker.api_url.is_not(None))  # heartbeat-only registrations have no dispatch address
            .where(Worker.last_heartbeat_at >= t - self.settings.offline_after)
            .order_by(
                (Worker.state == WorkerState.ready.value).desc(),
                Worker.active_job_id.is_(None).desc(),
                Worker.last_heartbeat_at.desc(),
                Worker.id,
            )
        )
        if kind is not None:
            stmt = stmt.where(Worker.kind == WorkerKind(kind).value)
        excluded = set(exclude)
        for row in (await session.execute(stmt)).scalars():
            meta = row.metadata_ or {}
            if row.id in excluded or meta.get("admin_drain") or not meta.get("compatible", True):
                continue
            caps = await self._capabilities(session, [row.id])
            return self.to_info(row, caps.get(row.id, []), now=t)
        return None

    async def workers_for_capability(
        self, session: AsyncSession, capability: str, *, kind: WorkerKind | str | None = None
    ) -> list[WorkerInfo]:
        """Every worker that reported *or* is declared (config) to provide ``capability``, in any state.
        Used by the Wake-on-LAN controller to find a sleeping/offline worker worth waking."""
        workers = await self.list_workers(session, kind=kind)
        return [w for w in workers if capability in w.capabilities or capability in w.declared_capabilities]
