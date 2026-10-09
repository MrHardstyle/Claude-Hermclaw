"""PostgreSQL-backed resource manager (Bauplan §4, §25, §32; P09 9.1–9.9).

State lives exclusively in ``resource_leases`` / ``resource_requests`` so leases survive process restarts and every
runtime instance sees the same picture.

Concurrency model
-----------------
Every decision about a resource runs in one short transaction that first takes transaction-scoped advisory locks
(``pg_advisory_xact_lock``) on the resource's *lock set* – the resource itself plus every member of each budget
the resource belongs to – in sorted order (deadlock free). Within the lock the manager expires overdue leases,
evaluates exclusivity, the waiting queue and the budgets, and either inserts the lease or keeps the request waiting.
The partial unique index ``uq_resource_leases_exclusive_active`` is the last line of defence: a violation is
treated as contention and the attempt is retried. All time comparisons use the database clock.

Queue
-----
``acquire`` registers a ``resource_requests`` row (``waiting``) and polls. A request is granted only if no live
waiting request for the same resource is ahead of it (higher priority, then FIFO by ``created_at``, then id) and, for
budget members, no higher-priority request on another member of the same budget is waiting for capacity. Waiters
refresh ``expires_at`` on every poll; requests of crashed waiters go stale and are cancelled.

Preemption
----------
Only the manager preempts (``request_preemption`` / ``acquire(preempt=True)``): a preemptible, strictly
lower-priority ``active`` lease is set to ``preempting`` (``resource.preempt.requested``), its expiry is capped at
``now + policies.leases.preemption_grace_seconds``. The holder sees it via ``heartbeat``/``should_yield`` (or the
:class:`~hermclaw.resources.keeper.LeaseKeeper` callback), checkpoints and releases. If it does not, the lease is
force-expired at the deadline with ``reason=preemption_grace_timeout`` – never silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.core.config import HermclawConfig, LeasePolicy, ModelProfileConfig
from hermclaw.core.errors import ConflictError, NotFoundError, ResourceUnavailable, ValidationFailed
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.persistence.models import ResourceLease, ResourceRequest
from hermclaw.resources.budget import EPSILON, BudgetUsage, ResourceBudget, check_budgets_disjoint, model_host_budgets
from hermclaw.resources.constants import (
    GPU_224,
    HOLDING_STATES,
    LEASE_ACTIVE,
    LEASE_EXPIRED,
    LEASE_PREEMPTING,
    LEASE_RELEASED,
    MEDIA_KINDS,
    MODEL_RESOURCES,
    PRIORITIES,
    REQUEST_CANCELLED,
    REQUEST_GRANTED,
    REQUEST_WAITING,
    ROLE_OWNER_KIND,
    VIDEO_224,
    OwnerKind,
    priority_for,
    validate_holder_id,
    validate_owner_kind,
    validate_priority,
    validate_reason,
    validate_resource_name,
)
from hermclaw.resources.keeper import LeaseKeeper, LostCallback, PreemptCallback
from hermclaw.resources.types import (
    Lease,
    LeaseLost,
    LeaseNotOwned,
    LeaseStatus,
    MediaLeases,
    PreemptionResult,
    RecoveryReport,
    SweepReport,
    WaitingRequest,
)

log = get_logger(__name__)
T = TypeVar("T")

SOURCE_TYPE = "resource_manager"
LOCK_PREFIX = "hermclaw.resource:"
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001", "55P03"})
MAX_TX_RETRIES = 6
MAX_DETAIL_CHARS = 500

#: blocked reasons reported by an acquisition attempt
BLOCK_QUEUED = "queued"  # a live request for the same resource is ahead
BLOCK_BUDGET_QUEUED = "budget_queued"  # a higher-priority request on another member of a budget waits for capacity
BLOCK_HELD = "held"  # exclusive request, resource has holders
BLOCK_HELD_EXCLUSIVE = "held_exclusive"  # shared request, resource has an exclusive holder
BLOCK_BUDGET = "budget"  # capacity of a budget would be exceeded
BLOCK_CONTENTION = "contention"  # transaction lost a race (unique index / deadlock) – retried


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    state = getattr(orig, "sqlstate", None)
    return state if isinstance(state, str) else None


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _lease_payload(row: ResourceLease) -> dict[str, Any]:
    return {
        "lease_id": str(row.id),
        "resource": row.resource,
        "resource_group": row.resource_group,
        "owner_kind": row.owner_kind,
        "holder": row.holder,
        "priority": row.priority,
        "exclusive": row.exclusive,
        "weight": float(row.weight),
        "preemptible": row.preemptible,
        "state": row.state,
        "expires_at": _iso(row.expires_at),
    }


def _request_payload(row: ResourceRequest) -> dict[str, Any]:
    return {
        "request_id": str(row.id),
        "resource": row.resource,
        "owner_kind": row.owner_kind,
        "holder": row.holder,
        "priority": row.priority,
    }


def _preempt_meta(row: ResourceLease) -> dict[str, Any]:
    p = (row.metadata_ or {}).get("preempt")
    return dict(p) if isinstance(p, Mapping) else {}


def _parse_ts(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class _Spec:
    resource: str
    resource_group: str
    owner_kind: str
    priority: int
    job_id: uuid.UUID | None
    step_id: uuid.UUID | None
    ttl_seconds: float
    exclusive: bool
    weight: float
    preemptible: bool
    preempt: bool
    reason: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Attempt:
    lease: Lease | None
    blocked: str | None


class ResourceManager:
    """Leases on named resources with priorities, budgets, safe preemption and crash recovery.

    ``holder_id`` identifies the process instance and must be stable across restarts of the same instance (e.g.
    ``runtime@webui-223``) so that :meth:`recover` can release what a previous incarnation left behind; it must be
    unique among concurrently running processes.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        policy: LeasePolicy,
        holder_id: str,
        *,
        budgets: Sequence[ResourceBudget] = (),
        poll_interval: float = 0.25,
        request_ttl_seconds: float | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValidationFailed("poll_interval must be > 0", code="RESOURCE_CONFIG_INVALID")
        self._sm = sessionmaker
        self.policy = policy
        self.holder_id = validate_holder_id(holder_id)
        self.incarnation = uuid.uuid4().hex
        check_budgets_disjoint(budgets)
        self.budgets: tuple[ResourceBudget, ...] = tuple(budgets)
        self._budgets_by_resource: dict[str, list[ResourceBudget]] = {}
        for b in self.budgets:
            for m in b.members:
                self._budgets_by_resource.setdefault(m, []).append(b)
        self.poll_interval = float(poll_interval)
        ttl = request_ttl_seconds if request_ttl_seconds is not None else max(float(policy.heartbeat_seconds), poll_interval * 20, 2.0)
        if ttl <= poll_interval:
            raise ValidationFailed("request_ttl_seconds must be larger than poll_interval", code="RESOURCE_CONFIG_INVALID")
        self.request_ttl_seconds = float(ttl)
        self._live_requests: set[uuid.UUID] = set()

    @classmethod
    def from_config(cls, sessionmaker: async_sessionmaker[AsyncSession], config: HermclawConfig, holder_id: str, **kw: Any) -> ResourceManager:
        """Manager with ``policies.leases`` and the model host budgets of ``models.yaml``."""
        kw.setdefault("budgets", model_host_budgets(config.models))
        return cls(sessionmaker, config.policies.leases, holder_id, **kw)

    # ------------------------------------------------------------------------------------------- helpers
    def budgets_for(self, resource: str) -> list[ResourceBudget]:
        return list(self._budgets_by_resource.get(resource, ()))

    def lock_set(self, resource: str) -> list[str]:
        names = {resource}
        for b in self.budgets_for(resource):
            names |= b.members
        return sorted(names)

    def keeper_interval(self, leases: Sequence[Lease] = ()) -> float:
        """Heartbeat/poll interval for holders: often enough to renew the TTL and to notice preemption early."""
        ttls = [float(lease.metadata.get("ttl_seconds") or self.policy.default_ttl_seconds) for lease in leases] or [
            float(self.policy.default_ttl_seconds)
        ]
        return max(0.05, min(float(self.policy.heartbeat_seconds), min(ttls) / 3.0, self.policy.preemption_grace_seconds / 4.0))

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self._sm() as s, s.begin():
            yield s

    async def _retrying(self, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
        for attempt in range(MAX_TX_RETRIES):
            try:
                async with self._tx() as s:
                    return await fn(s)
            except DBAPIError as exc:
                if _sqlstate(exc) not in RETRYABLE_SQLSTATES or attempt == MAX_TX_RETRIES - 1:
                    raise
                await asyncio.sleep(0.01 * (attempt + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    async def _lock(s: AsyncSession, resources: Iterable[str]) -> None:
        for name in sorted(set(resources)):
            await s.execute(text("select pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": LOCK_PREFIX + name})

    @staticmethod
    async def _now(s: AsyncSession) -> datetime:
        return (await s.execute(select(func.clock_timestamp()))).scalar_one()

    async def _emit(
        self,
        s: AsyncSession,
        event_type: EventType,
        *,
        job_id: uuid.UUID | None,
        step_id: uuid.UUID | None,
        payload: dict[str, Any],
        severity: Severity = Severity.info,
    ) -> None:
        await append_event(
            s,
            event_type,
            source_type=SOURCE_TYPE,
            source_id=self.holder_id,
            job_id=job_id,
            step_id=step_id,
            severity=severity,
            payload=payload,
        )

    def _spec(
        self,
        resource: str,
        *,
        owner_kind: str,
        priority: int | None,
        job_id: uuid.UUID | None,
        step_id: uuid.UUID | None,
        ttl_seconds: float | None,
        exclusive: bool,
        weight: float,
        preemptible: bool,
        preempt: bool,
        resource_group: str | None,
        metadata: Mapping[str, Any] | None,
        reason: str | None,
    ) -> _Spec:
        validate_resource_name(resource)
        validate_owner_kind(owner_kind)
        prio = validate_priority(priority if priority is not None else priority_for(owner_kind))
        ttl = float(ttl_seconds if ttl_seconds is not None else self.policy.default_ttl_seconds)
        if not ttl > 0:
            raise ValidationFailed("ttl_seconds must be > 0", code="RESOURCE_TTL_INVALID")
        w = float(weight)
        if not w >= 0 or w != w or w == float("inf"):
            raise ValidationFailed("weight must be a finite number >= 0", code="RESOURCE_WEIGHT_INVALID")
        for b in self.budgets_for(resource):
            if not b.fits(0.0, w):
                raise ResourceUnavailable(
                    f"weight {w} exceeds the capacity {b.capacity} of budget '{b.name}' – can never be granted",
                    code="RESOURCE_OVER_CAPACITY",
                    details={"resource": resource, "budget": b.name, "capacity": b.capacity, "weight": w},
                )
        if reason is not None:
            validate_reason(reason)
        info = dict(DEFAULT_REDACTOR.obj(dict(metadata))) if metadata else {}
        return _Spec(
            resource=resource,
            resource_group=validate_resource_name(resource_group) if resource_group else resource,
            owner_kind=owner_kind,
            priority=prio,
            job_id=job_id,
            step_id=step_id,
            ttl_seconds=ttl,
            exclusive=bool(exclusive),
            weight=w,
            preemptible=bool(preemptible),
            preempt=bool(preempt),
            reason=reason,
            metadata=info,
        )

    # ------------------------------------------------------------------------------------------- acquire (9.2/9.5)
    async def acquire(
        self,
        resource: str,
        *,
        owner_kind: str,
        priority: int | None = None,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        ttl_seconds: float | None = None,
        exclusive: bool = True,
        weight: float = 0.0,
        preemptible: bool = True,
        wait_timeout: float | None = None,
        poll: float | None = None,
        preempt: bool = False,
        resource_group: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> Lease:
        """Acquire a lease, waiting in the priority queue until granted.

        ``priority`` defaults to the Bauplan §4 priority of ``owner_kind``. ``wait_timeout=None`` waits forever,
        ``0`` tries exactly once; on timeout ``ResourceUnavailable(code=RESOURCE_WAIT_TIMEOUT)`` is raised.
        ``preempt=True`` lets the manager request preemption of strictly lower-priority preemptible holders (the
        caller never preempts by itself). ``reason`` is a short slug recorded with a preemption request.
        """
        spec = self._spec(
            resource,
            owner_kind=owner_kind,
            priority=priority,
            job_id=job_id,
            step_id=step_id,
            ttl_seconds=ttl_seconds,
            exclusive=exclusive,
            weight=weight,
            preemptible=preemptible,
            preempt=preempt,
            resource_group=resource_group,
            metadata=metadata,
            reason=reason,
        )
        poll_s = float(poll) if poll is not None and poll > 0 else self.poll_interval
        poll_s = min(poll_s, self.request_ttl_seconds / 4.0)
        loop = asyncio.get_running_loop()
        deadline = None if wait_timeout is None else loop.time() + max(0.0, float(wait_timeout))
        request_id = uuid.uuid4()
        self._live_requests.add(request_id)
        lease: Lease | None = None
        blocked: str | None = None
        outcome = "error"
        try:
            while True:
                attempt = await self._attempt(spec, request_id)
                if attempt.lease is not None:
                    lease = attempt.lease
                    return lease
                blocked = attempt.blocked
                if deadline is not None and loop.time() >= deadline:
                    outcome = "wait_timeout"
                    raise ResourceUnavailable(
                        f"resource '{resource}' not acquired within {wait_timeout}s (blocked: {blocked})",
                        code="RESOURCE_WAIT_TIMEOUT",
                        details={"resource": resource, "blocked": blocked, "priority": spec.priority, "request_id": str(request_id)},
                    )
                delay = poll_s if deadline is None else max(0.0, min(poll_s, deadline - loop.time()))
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            if lease is None:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._abandon(spec, request_id, outcome=outcome, blocked=blocked))
            self._live_requests.discard(request_id)

    async def try_acquire(self, resource: str, **kw: Any) -> Lease | None:
        """Single non-waiting attempt; ``None`` if the resource is not available right now."""
        kw["wait_timeout"] = 0
        try:
            return await self.acquire(resource, **kw)
        except ResourceUnavailable as exc:
            if exc.code == "RESOURCE_WAIT_TIMEOUT":
                return None
            raise

    async def _attempt(self, spec: _Spec, request_id: uuid.UUID) -> _Attempt:
        try:
            async with self._tx() as s:
                return await self._attempt_tx(s, spec, request_id)
        except IntegrityError as exc:
            log.info("lease insert lost a race resource=%s (%s)", spec.resource, _sqlstate(exc))
            return _Attempt(None, BLOCK_CONTENTION)
        except DBAPIError as exc:
            if _sqlstate(exc) in RETRYABLE_SQLSTATES:
                return _Attempt(None, BLOCK_CONTENTION)
            raise

    async def _attempt_tx(self, s: AsyncSession, spec: _Spec, request_id: uuid.UUID) -> _Attempt:
        lock_set = self.lock_set(spec.resource)
        await self._lock(s, lock_set)
        now = await self._now(s)
        await self._expire_due(s, lock_set, now)

        req = await s.get(ResourceRequest, request_id, with_for_update=True, populate_existing=True)
        if req is None:
            req = ResourceRequest(
                id=request_id,
                resource=spec.resource,
                owner_job_id=spec.job_id,
                owner_step_id=spec.step_id,
                owner_kind=spec.owner_kind,
                holder=self.holder_id,
                priority=spec.priority,
                state=REQUEST_WAITING,
                expires_at=now + timedelta(seconds=self.request_ttl_seconds),
                created_at=now,
            )
            s.add(req)
            await s.flush()
            await self._emit(
                s,
                EventType.RESOURCE_REQUESTED,
                job_id=spec.job_id,
                step_id=spec.step_id,
                payload={**_request_payload(req), "exclusive": spec.exclusive, "weight": spec.weight, "preempt": spec.preempt},
            )
        elif req.state == REQUEST_GRANTED:
            # the grant committed but the caller never saw it (e.g. lost connection after COMMIT): hand it out now
            row = await self._lease_for_request(s, request_id)
            if row is not None:
                return _Attempt(Lease.from_row(row), None)
            req.state = REQUEST_WAITING
            req.created_at = now
        elif req.state != REQUEST_WAITING:
            # cancelled as stale (waiter was too slow to refresh) – re-queue at the back of its priority class
            req.state = REQUEST_WAITING
            req.created_at = now
            await self._emit(
                s,
                EventType.RESOURCE_REQUESTED,
                job_id=spec.job_id,
                step_id=spec.step_id,
                payload={**_request_payload(req), "requeued": True},
            )
        req.expires_at = now + timedelta(seconds=self.request_ttl_seconds)

        blocked = await self._blocked_reason(s, spec, req, now)
        if blocked is None:
            row = ResourceLease(
                id=uuid.uuid4(),
                resource=spec.resource,
                resource_group=spec.resource_group,
                owner_job_id=spec.job_id,
                owner_step_id=spec.step_id,
                owner_kind=spec.owner_kind,
                holder=self.holder_id,
                priority=spec.priority,
                state=LEASE_ACTIVE,
                preemptible=spec.preemptible,
                exclusive=spec.exclusive,
                weight=spec.weight,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=spec.ttl_seconds),
                metadata_={
                    "incarnation": self.incarnation,
                    "request_id": str(request_id),
                    "ttl_seconds": spec.ttl_seconds,
                    "info": spec.metadata,
                },
            )
            s.add(row)
            req.state = REQUEST_GRANTED
            await s.flush()
            waited = max(0.0, (now - req.created_at).total_seconds())
            await self._emit(
                s,
                EventType.RESOURCE_ACQUIRED,
                job_id=spec.job_id,
                step_id=spec.step_id,
                payload={**_lease_payload(row), "request_id": str(request_id), "waited_seconds": round(waited, 3), "info": spec.metadata},
            )
            log.info("lease acquired resource=%s lease=%s kind=%s prio=%s", spec.resource, row.id, spec.owner_kind, spec.priority)
            return _Attempt(Lease.from_row(row), None)

        if spec.preempt and blocked in (BLOCK_HELD, BLOCK_HELD_EXCLUSIVE, BLOCK_BUDGET):
            await self._preempt_for(s, spec, request_id, blocked, now)
        return _Attempt(None, blocked)

    async def _lease_for_request(self, s: AsyncSession, request_id: uuid.UUID) -> ResourceLease | None:
        stmt = select(ResourceLease).where(
            ResourceLease.metadata_["request_id"].astext == str(request_id),
            ResourceLease.state.in_(HOLDING_STATES),
        )
        return (await s.execute(stmt)).scalars().first()

    async def _blocked_reason(self, s: AsyncSession, spec: _Spec, req: ResourceRequest, now: datetime) -> str | None:
        r = ResourceRequest
        ahead = await s.scalar(
            select(func.count())
            .select_from(r)
            .where(
                r.resource == spec.resource,
                r.state == REQUEST_WAITING,
                r.expires_at > now,
                r.id != req.id,
                or_(
                    r.priority > spec.priority,
                    and_(r.priority == spec.priority, or_(r.created_at < req.created_at, and_(r.created_at == req.created_at, r.id < req.id))),
                ),
            )
        )
        if ahead:
            return BLOCK_QUEUED

        lease = ResourceLease
        budgets = self.budgets_for(spec.resource)
        for b in budgets:
            others = sorted(b.members - {spec.resource})
            if not others:
                continue
            exclusively_held = select(lease.resource).where(
                lease.resource.in_(others), lease.exclusive.is_(True), lease.state.in_(HOLDING_STATES)
            )
            higher = await s.scalar(
                select(func.count())
                .select_from(r)
                .where(
                    r.resource.in_(others),
                    r.state == REQUEST_WAITING,
                    r.expires_at > now,
                    r.priority > spec.priority,
                    r.resource.not_in(exclusively_held),
                )
            )
            if higher:
                return BLOCK_BUDGET_QUEUED

        holders = (
            await s.execute(
                select(lease.exclusive, func.count())
                .where(lease.resource == spec.resource, lease.state.in_(HOLDING_STATES))
                .group_by(lease.exclusive)
            )
        ).all()
        counts = {bool(ex): int(n) for ex, n in holders}
        if spec.exclusive and sum(counts.values()) > 0:
            if spec.step_id is not None:
                own = await s.scalar(
                    select(func.count())
                    .select_from(lease)
                    .where(
                        lease.resource == spec.resource,
                        lease.state.in_(HOLDING_STATES),
                        lease.holder == self.holder_id,
                        lease.owner_step_id == spec.step_id,
                    )
                )
                if own:
                    raise ConflictError(
                        f"step already holds a lease on '{spec.resource}' (leases are not re-entrant)",
                        code="RESOURCE_ALREADY_HELD",
                        details={"resource": spec.resource, "step_id": str(spec.step_id)},
                    )
            return BLOCK_HELD
        if not spec.exclusive and counts.get(True):
            return BLOCK_HELD_EXCLUSIVE

        for b in budgets:
            used = await self._used(s, b)
            if not b.fits(used, spec.weight):
                return BLOCK_BUDGET
        return None

    @staticmethod
    async def _used(s: AsyncSession, budget: ResourceBudget) -> float:
        total = await s.scalar(
            select(func.coalesce(func.sum(ResourceLease.weight), 0.0)).where(
                ResourceLease.resource.in_(sorted(budget.members)), ResourceLease.state.in_(HOLDING_STATES)
            )
        )
        return float(total or 0.0)

    async def _abandon(self, spec: _Spec, request_id: uuid.UUID, *, outcome: str, blocked: str | None) -> None:
        """Waiter gives up (timeout, cancellation, error): cancel its request, withdraw preemptions it caused and
        release a lease whose grant committed without the caller seeing it."""

        async def run(s: AsyncSession) -> None:
            lock_set = self.lock_set(spec.resource)
            await self._lock(s, lock_set)
            now = await self._now(s)
            req = await s.get(ResourceRequest, request_id, with_for_update=True, populate_existing=True)
            if req is None:
                return
            if req.state == REQUEST_WAITING:
                req.state = REQUEST_CANCELLED
                await s.flush()
                await self._emit(
                    s,
                    EventType.RESOURCE_EXPIRED,
                    job_id=req.owner_job_id,
                    step_id=req.owner_step_id,
                    severity=Severity.warning,
                    payload={**_request_payload(req), "subject": "request", "reason": outcome, "blocked": blocked},
                )
            elif req.state == REQUEST_GRANTED:
                row = await self._lease_for_request(s, request_id)
                if row is not None:
                    await self._release_row(s, row, now, reason="acquire_abandoned", detail=outcome)
            await self._withdraw_for_request(s, lock_set, request_id, now)

        await self._retrying(run)

    # ------------------------------------------------------------------------------------------- release (9.3)
    async def release(self, lease_id: uuid.UUID, reason: str = "released", *, detail: str | None = None) -> bool:
        """Release a lease. Idempotent: ``False`` if it was not held any more. Unknown ids raise ``NotFoundError``."""
        validate_reason(reason)

        async def run(s: AsyncSession) -> bool:
            row = await s.get(ResourceLease, lease_id, with_for_update=True, populate_existing=True)
            if row is None:
                raise NotFoundError(f"lease {lease_id} not found", code="LEASE_NOT_FOUND")
            if row.state not in HOLDING_STATES:
                return False
            now = await self._now(s)
            await self._release_row(s, row, now, reason=reason, detail=detail)
            return True

        return await self._retrying(run)

    async def release_many(self, lease_ids: Iterable[uuid.UUID], reason: str = "released") -> int:
        n = 0
        for lid in lease_ids:
            with contextlib.suppress(NotFoundError):
                n += int(await self.release(lid, reason))
        return n

    async def _release_row(self, s: AsyncSession, row: ResourceLease, now: datetime, *, reason: str, detail: str | None = None) -> None:
        was = row.state
        row.state = LEASE_RELEASED
        row.released_at = now
        row.release_reason = reason
        await s.flush()
        payload = {
            **_lease_payload(row),
            "reason": reason,
            "released_by": self.holder_id,
            "was_preempting": was == LEASE_PREEMPTING,
            "held_seconds": round(max(0.0, (now - row.acquired_at).total_seconds()), 3),
        }
        if detail:
            payload["detail"] = detail[:MAX_DETAIL_CHARS]
        await self._emit(s, EventType.RESOURCE_RELEASED, job_id=row.owner_job_id, step_id=row.owner_step_id, payload=payload)
        log.info("lease released resource=%s lease=%s reason=%s", row.resource, row.id, reason)

    # ------------------------------------------------------------------------------------------- heartbeat / expiry (9.4)
    async def heartbeat(self, lease_id: uuid.UUID, *, ttl_seconds: float | None = None) -> LeaseStatus:
        """Extend a held lease by its TTL. A ``preempting`` lease is never extended beyond its grace deadline.

        Raises :class:`LeaseLost` if the lease is gone (the holder must stop and must not keep using the resource) and
        :class:`LeaseNotOwned` for a lease of another holder."""

        async def run(s: AsyncSession) -> LeaseStatus:
            row = await s.get(ResourceLease, lease_id, with_for_update=True, populate_existing=True)
            if row is None:
                raise LeaseLost(f"lease {lease_id} not found", details={"lease_id": str(lease_id)})
            if row.holder != self.holder_id:
                raise LeaseNotOwned(f"lease {lease_id} is held by another holder", details={"lease_id": str(lease_id)})
            now = await self._now(s)
            if row.state in HOLDING_STATES and row.expires_at <= now:
                await self._expire_rows(s, [row], now)
            if row.state not in HOLDING_STATES:
                return self._status(row)
            ttl = float(ttl_seconds if ttl_seconds is not None else (row.metadata_ or {}).get("ttl_seconds") or self.policy.default_ttl_seconds)
            if not ttl > 0:
                raise ValidationFailed("ttl_seconds must be > 0", code="RESOURCE_TTL_INVALID")
            new_exp = now + timedelta(seconds=ttl)
            if row.state == LEASE_PREEMPTING:
                deadline = _parse_ts(_preempt_meta(row).get("deadline"))
                if deadline is not None:
                    new_exp = min(new_exp, deadline)
            row.expires_at = new_exp
            row.heartbeat_at = now
            return self._status(row)

        status = await self._retrying(run)
        if status.state not in HOLDING_STATES:
            raise LeaseLost(
                f"lease {lease_id} is {status.state}",
                details={"lease_id": str(lease_id), "state": status.state, "reason": status.preempt_reason},
            )
        return status

    async def heartbeat_many(self, lease_ids: Iterable[uuid.UUID]) -> list[LeaseStatus]:
        return [await self.heartbeat(lid) for lid in lease_ids]

    @staticmethod
    def _status(row: ResourceLease) -> LeaseStatus:
        p = _preempt_meta(row)
        reason = row.release_reason if row.state not in HOLDING_STATES else p.get("reason")
        return LeaseStatus(
            lease_id=row.id,
            state=row.state,
            expires_at=row.expires_at,
            should_yield=row.state != LEASE_ACTIVE,
            preempt_reason=str(reason) if reason else None,
            preempt_deadline=_parse_ts(p.get("deadline")),
        )

    async def status(self, lease_id: uuid.UUID) -> LeaseStatus:
        async with self._sm() as s:
            row = await s.get(ResourceLease, lease_id)
            if row is None:
                raise NotFoundError(f"lease {lease_id} not found", code="LEASE_NOT_FOUND")
            now = await self._now(s)
            st = self._status(row)
            if row.state in HOLDING_STATES and row.expires_at <= now:
                return LeaseStatus(row.id, LEASE_EXPIRED, row.expires_at, True, st.preempt_reason, st.preempt_deadline)
            return st

    async def should_yield(self, lease_id: uuid.UUID) -> bool:
        """True if the holder must checkpoint and release (preemption requested) or the lease is gone."""
        try:
            return (await self.status(lease_id)).should_yield
        except NotFoundError:
            return True

    async def _expire_due(self, s: AsyncSession, resources: Sequence[str], now: datetime) -> tuple[list[Lease], list[Lease], list[uuid.UUID]]:
        """Expire overdue leases and cancel stale waiting requests on ``resources`` (caller holds their locks)."""
        rows = list(
            (
                await s.execute(
                    select(ResourceLease)
                    .where(
                        ResourceLease.resource.in_(list(resources)),
                        ResourceLease.state.in_(HOLDING_STATES),
                        ResourceLease.expires_at <= now,
                    )
                    .order_by(ResourceLease.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        expired, grace = await self._expire_rows(s, rows, now)
        stale = list(
            (
                await s.execute(
                    select(ResourceRequest)
                    .where(
                        ResourceRequest.resource.in_(list(resources)),
                        ResourceRequest.state == REQUEST_WAITING,
                        ResourceRequest.expires_at <= now,
                    )
                    .order_by(ResourceRequest.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        for req in stale:
            req.state = REQUEST_CANCELLED
            await s.flush()
            await self._emit(
                s,
                EventType.RESOURCE_EXPIRED,
                job_id=req.owner_job_id,
                step_id=req.owner_step_id,
                severity=Severity.warning,
                payload={**_request_payload(req), "subject": "request", "reason": "request_stale"},
            )
        return expired, grace, [r.id for r in stale]

    async def _expire_rows(self, s: AsyncSession, rows: Sequence[ResourceLease], now: datetime) -> tuple[list[Lease], list[Lease]]:
        expired: list[Lease] = []
        grace: list[Lease] = []
        for row in rows:
            was_preempting = row.state == LEASE_PREEMPTING
            reason = "preemption_grace_timeout" if was_preempting else "ttl_expired"
            row.state = LEASE_EXPIRED
            row.released_at = now
            row.release_reason = reason
            await s.flush()
            payload = {
                **_lease_payload(row),
                "subject": "lease",
                "reason": reason,
                "last_heartbeat_at": _iso(row.heartbeat_at),
            }
            if was_preempting:
                p = _preempt_meta(row)
                payload["preemption"] = {k: p.get(k) for k in ("reason", "requester_kind", "requester_priority", "requested_at", "deadline")}
            await self._emit(
                s,
                EventType.RESOURCE_EXPIRED,
                job_id=row.owner_job_id,
                step_id=row.owner_step_id,
                severity=Severity.warning,
                payload=payload,
            )
            log.warning("lease expired resource=%s lease=%s reason=%s", row.resource, row.id, reason)
            (grace if was_preempting else expired).append(Lease.from_row(row))
        return expired, grace

    async def sweep_expired(self) -> SweepReport:
        """Expire overdue leases (TTL or preemption grace) and cancel stale requests on all resources."""
        async with self._sm() as s:
            now_q = func.clock_timestamp()
            lease_res = select(ResourceLease.resource).where(ResourceLease.state.in_(HOLDING_STATES), ResourceLease.expires_at <= now_q)
            req_res = select(ResourceRequest.resource).where(ResourceRequest.state == REQUEST_WAITING, ResourceRequest.expires_at <= now_q)
            resources = sorted({*(await s.execute(lease_res)).scalars(), *(await s.execute(req_res)).scalars()})
        expired: list[Lease] = []
        grace: list[Lease] = []
        stale: list[uuid.UUID] = []
        for name in resources:

            async def run(s: AsyncSession, name: str = name) -> tuple[list[Lease], list[Lease], list[uuid.UUID]]:
                await self._lock(s, self.lock_set(name))
                return await self._expire_due(s, [name], await self._now(s))

            e, g, r = await self._retrying(run)
            expired += e
            grace += g
            stale += r
        return SweepReport(expired=expired, grace_timeouts=grace, stale_requests=stale)

    async def run_maintenance(self, stop: asyncio.Event, *, interval: float | None = None) -> None:
        """Background sweeper loop (runtime service); returns when ``stop`` is set."""
        period = interval if interval is not None else max(0.5, float(self.policy.heartbeat_seconds))
        while not stop.is_set():
            try:
                await self.sweep_expired()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("resource sweep failed: %s", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=period)

    # ------------------------------------------------------------------------------------------- preemption (9.6)
    async def request_preemption(
        self,
        resource: str,
        requester_priority: int,
        reason: str,
        *,
        requester_kind: str | None = None,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
    ) -> PreemptionResult:
        """Ask every preemptible, strictly lower-priority holder of ``resource`` to checkpoint and release.

        Non-preemptible leases and holders with priority >= ``requester_priority`` are never touched."""
        validate_resource_name(resource)
        validate_priority(requester_priority)
        validate_reason(reason)
        if requester_kind is not None:
            validate_owner_kind(requester_kind)
        requester = {
            "requester_priority": requester_priority,
            "requester_kind": requester_kind,
            "requester_holder": self.holder_id,
            "requester_job_id": str(job_id) if job_id else None,
            "requester_step_id": str(step_id) if step_id else None,
            "request_id": None,
        }

        async def run(s: AsyncSession) -> PreemptionResult:
            await self._lock(s, self.lock_set(resource))
            now = await self._now(s)
            await self._expire_due(s, self.lock_set(resource), now)
            rows = list(
                (
                    await s.execute(
                        select(ResourceLease)
                        .where(ResourceLease.resource == resource, ResourceLease.state.in_(HOLDING_STATES))
                        .order_by(ResourceLease.acquired_at, ResourceLease.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalars()
            )
            result = PreemptionResult(resource=resource)
            targets: list[ResourceLease] = []
            for row in rows:
                if row.state == LEASE_PREEMPTING:
                    result.already_preempting.append(Lease.from_row(row))
                elif not row.preemptible:
                    result.refused_non_preemptible.append(Lease.from_row(row))
                elif row.priority >= requester_priority:
                    result.refused_priority.append(Lease.from_row(row))
                else:
                    targets.append(row)
            result.requested.extend(await self._mark_preempting(s, targets, now, reason=reason, requester=requester))
            return result

        return await self._retrying(run)

    async def _preempt_for(self, s: AsyncSession, spec: _Spec, request_id: uuid.UUID, blocked: str, now: datetime) -> None:
        requester = {
            "requester_priority": spec.priority,
            "requester_kind": spec.owner_kind,
            "requester_holder": self.holder_id,
            "requester_job_id": str(spec.job_id) if spec.job_id else None,
            "requester_step_id": str(spec.step_id) if spec.step_id else None,
            "request_id": str(request_id),
        }
        reason = spec.reason or f"{spec.owner_kind}-priority-{spec.priority}"
        validate_reason(reason)
        lease = ResourceLease
        if blocked in (BLOCK_HELD, BLOCK_HELD_EXCLUSIVE):
            stmt = select(lease).where(lease.resource == spec.resource, lease.state == LEASE_ACTIVE)
            if blocked == BLOCK_HELD_EXCLUSIVE:
                stmt = stmt.where(lease.exclusive.is_(True))
            rows = list((await s.execute(stmt.order_by(lease.id).with_for_update().execution_options(populate_existing=True))).scalars())
            targets = [r for r in rows if r.preemptible and r.priority < spec.priority]
            await self._mark_preempting(s, targets, now, reason=reason, requester=requester)
            return
        # budget: preempt the minimal set of lower-priority holders (lowest priority, newest first) that makes room
        for b in self.budgets_for(spec.resource):
            rows = list(
                (
                    await s.execute(
                        select(lease)
                        .where(lease.resource.in_(sorted(b.members)), lease.state.in_(HOLDING_STATES))
                        .order_by(lease.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalars()
            )
            used = sum(float(r.weight) for r in rows)
            if b.fits(used, spec.weight):
                continue
            pending_free = sum(float(r.weight) for r in rows if r.state == LEASE_PREEMPTING)
            need = used - pending_free + spec.weight - b.capacity
            if need <= EPSILON:
                continue  # earlier preemptions will make room
            candidates = sorted(
                (r for r in rows if r.state == LEASE_ACTIVE and r.preemptible and r.priority < spec.priority and r.weight > 0),
                key=lambda r: (r.priority, -r.acquired_at.timestamp()),
            )
            picked: list[ResourceLease] = []
            freed = 0.0
            for r in candidates:
                if freed >= need - EPSILON:
                    break
                picked.append(r)
                freed += float(r.weight)
            if freed >= need - EPSILON:
                await self._mark_preempting(s, picked, now, reason=reason, requester=requester)

    async def _mark_preempting(
        self, s: AsyncSession, rows: Sequence[ResourceLease], now: datetime, *, reason: str, requester: Mapping[str, Any]
    ) -> list[Lease]:
        out: list[Lease] = []
        grace = float(self.policy.preemption_grace_seconds)
        deadline = now + timedelta(seconds=grace)
        for row in rows:
            meta = dict(row.metadata_ or {})
            meta["preempt"] = {
                "reason": reason,
                "requested_at": _iso(now),
                "deadline": _iso(deadline),
                "grace_seconds": grace,
                "previous_expires_at": _iso(row.expires_at),
                **requester,
            }
            row.metadata_ = meta
            row.state = LEASE_PREEMPTING
            row.expires_at = min(row.expires_at, deadline)
            await s.flush()
            await self._emit(
                s,
                EventType.RESOURCE_PREEMPT_REQUESTED,
                job_id=row.owner_job_id,
                step_id=row.owner_step_id,
                severity=Severity.warning,
                payload={
                    **_lease_payload(row),
                    "action": "requested",
                    "reason": reason,
                    "deadline": _iso(deadline),
                    "grace_seconds": grace,
                    **{k: v for k, v in requester.items() if k != "requester_holder"},
                    "requester_holder": self.holder_id,
                },
            )
            log.warning("preemption requested resource=%s lease=%s reason=%s", row.resource, row.id, reason)
            out.append(Lease.from_row(row))
        return out

    async def withdraw_preemption(self, lease_id: uuid.UUID, reason: str = "withdrawn") -> bool:
        """Return a ``preempting`` lease to ``active`` (the requester no longer needs the resource)."""
        validate_reason(reason)

        async def run(s: AsyncSession) -> bool:
            row = await s.get(ResourceLease, lease_id)
            if row is None:
                raise NotFoundError(f"lease {lease_id} not found", code="LEASE_NOT_FOUND")
            await self._lock(s, self.lock_set(row.resource))
            row = await s.get(ResourceLease, lease_id, with_for_update=True, populate_existing=True)
            if row is None or row.state != LEASE_PREEMPTING:
                return False
            now = await self._now(s)
            if row.expires_at <= now:
                await self._expire_rows(s, [row], now)
                return False
            await self._unpreempt(s, row, now, reason)
            return True

        return await self._retrying(run)

    async def _withdraw_for_request(self, s: AsyncSession, resources: Sequence[str], request_id: uuid.UUID, now: datetime) -> None:
        rows = list(
            (
                await s.execute(
                    select(ResourceLease)
                    .where(
                        ResourceLease.resource.in_(list(resources)),
                        ResourceLease.state == LEASE_PREEMPTING,
                        ResourceLease.metadata_["preempt"]["request_id"].astext == str(request_id),
                    )
                    .order_by(ResourceLease.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        for row in rows:
            if row.expires_at > now:
                await self._unpreempt(s, row, now, "requester_gone")

    async def _unpreempt(self, s: AsyncSession, row: ResourceLease, now: datetime, reason: str) -> None:
        meta = dict(row.metadata_ or {})
        old = meta.pop("preempt", None)
        row.metadata_ = meta
        row.state = LEASE_ACTIVE
        ttl = float(meta.get("ttl_seconds") or self.policy.default_ttl_seconds)
        prev = _parse_ts(old.get("previous_expires_at")) if isinstance(old, Mapping) else None
        row.expires_at = max(now + timedelta(seconds=ttl), prev or now)
        await s.flush()
        await self._emit(
            s,
            EventType.RESOURCE_PREEMPT_REQUESTED,
            job_id=row.owner_job_id,
            step_id=row.owner_step_id,
            payload={**_lease_payload(row), "action": "withdrawn", "reason": reason},
        )

    # ------------------------------------------------------------------------------------------- recovery (9.9)
    async def recover(self, holder_id: str | None = None) -> RecoveryReport:
        """Startup recovery: release leases and cancel requests that a previous incarnation of ``holder_id``
        (default: this manager's holder) left behind, then expire stale leases of all holders."""
        holder = validate_holder_id(holder_id) if holder_id else self.holder_id
        live = sorted(self._live_requests) if holder == self.holder_id else []
        foreign_incarnation = ResourceLease.metadata_["incarnation"].astext.is_distinct_from(self.incarnation)
        async with self._sm() as s:
            lease_res = select(ResourceLease.resource).where(
                ResourceLease.holder == holder, ResourceLease.state.in_(HOLDING_STATES), foreign_incarnation
            )
            req_q = select(ResourceRequest.resource).where(ResourceRequest.holder == holder, ResourceRequest.state == REQUEST_WAITING)
            if live:
                req_q = req_q.where(ResourceRequest.id.not_in(live))
            resources = sorted({*(await s.execute(lease_res)).scalars(), *(await s.execute(req_q)).scalars()})
        released: list[Lease] = []
        cancelled: list[uuid.UUID] = []
        for name in resources:

            async def run(s: AsyncSession, name: str = name) -> tuple[list[Lease], list[uuid.UUID]]:
                await self._lock(s, self.lock_set(name))
                now = await self._now(s)
                rows = list(
                    (
                        await s.execute(
                            select(ResourceLease)
                            .where(
                                ResourceLease.resource == name,
                                ResourceLease.holder == holder,
                                ResourceLease.state.in_(HOLDING_STATES),
                                foreign_incarnation,
                            )
                            .order_by(ResourceLease.id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                    ).scalars()
                )
                out: list[Lease] = []
                for row in rows:
                    prev = (row.metadata_ or {}).get("incarnation")
                    await self._release_row(s, row, now, reason="holder_restarted", detail=f"previous incarnation {prev}")
                    out.append(Lease.from_row(row))
                rq = (
                    select(ResourceRequest)
                    .where(ResourceRequest.resource == name, ResourceRequest.holder == holder, ResourceRequest.state == REQUEST_WAITING)
                    .order_by(ResourceRequest.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if live:
                    rq = rq.where(ResourceRequest.id.not_in(live))
                reqs = list((await s.execute(rq)).scalars())
                for req in reqs:
                    req.state = REQUEST_CANCELLED
                    await s.flush()
                    await self._emit(
                        s,
                        EventType.RESOURCE_EXPIRED,
                        job_id=req.owner_job_id,
                        step_id=req.owner_step_id,
                        severity=Severity.warning,
                        payload={**_request_payload(req), "subject": "request", "reason": "holder_restarted"},
                    )
                return out, [r.id for r in reqs]

            rel, can = await self._retrying(run)
            released += rel
            cancelled += can
        sweep = await self.sweep_expired()
        if released or cancelled:
            log.warning("resource recovery holder=%s released=%d cancelled_requests=%d", holder, len(released), len(cancelled))
        return RecoveryReport(holder=holder, released=released, cancelled_requests=cancelled, sweep=sweep)

    # ------------------------------------------------------------------------------------------- queries
    async def get_lease(self, lease_id: uuid.UUID) -> Lease | None:
        async with self._sm() as s:
            row = await s.get(ResourceLease, lease_id)
            return Lease.from_row(row) if row is not None else None

    async def list_leases(
        self, resource: str | None = None, *, holder: str | None = None, states: Sequence[str] = HOLDING_STATES
    ) -> list[Lease]:
        stmt = select(ResourceLease).where(ResourceLease.state.in_(list(states)))
        if resource is not None:
            stmt = stmt.where(ResourceLease.resource == resource)
        if holder is not None:
            stmt = stmt.where(ResourceLease.holder == holder)
        async with self._sm() as s:
            rows = (await s.execute(stmt.order_by(ResourceLease.priority.desc(), ResourceLease.acquired_at))).scalars()
            return [Lease.from_row(r) for r in rows]

    async def waiting_requests(self, resource: str | None = None) -> list[WaitingRequest]:
        """Live waiting requests in grant order (priority desc, FIFO)."""
        stmt = select(ResourceRequest).where(ResourceRequest.state == REQUEST_WAITING, ResourceRequest.expires_at > func.clock_timestamp())
        if resource is not None:
            stmt = stmt.where(ResourceRequest.resource == resource)
        stmt = stmt.order_by(ResourceRequest.priority.desc(), ResourceRequest.created_at, ResourceRequest.id)
        async with self._sm() as s:
            return [WaitingRequest.from_row(r) for r in (await s.execute(stmt)).scalars()]

    async def budget_usage(self, budget: str | ResourceBudget) -> BudgetUsage:
        b = budget if isinstance(budget, ResourceBudget) else self._budget(budget)
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(ResourceLease.resource, func.coalesce(func.sum(ResourceLease.weight), 0.0))
                    .where(ResourceLease.resource.in_(sorted(b.members)), ResourceLease.state.in_(HOLDING_STATES))
                    .group_by(ResourceLease.resource)
                )
            ).all()
        by_res = {str(name): float(w) for name, w in rows}
        return BudgetUsage(budget=b.name, capacity=b.capacity, used=sum(by_res.values()), by_resource=by_res)

    def _budget(self, name: str) -> ResourceBudget:
        for b in self.budgets:
            if b.name == name:
                return b
        raise NotFoundError(f"unknown resource budget '{name}'", code="RESOURCE_BUDGET_UNKNOWN")

    # ------------------------------------------------------------------------------------------- models (9.7)
    async def acquire_model(
        self,
        profile: ModelProfileConfig,
        *,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        ttl_seconds: float | None = None,
        wait_timeout: float | None = None,
        preempt: bool = False,
        preemptible: bool = True,
        owner_kind: str | None = None,
        poll: float | None = None,
    ) -> Lease:
        """Lease for one model profile: ``resource_group`` (``large-model-224`` is exclusive: one large model resident
        at a time), weight ``memory_gb`` against the model host budget, priority from the profile."""
        if not profile.enabled:
            raise ResourceUnavailable(f"model profile '{profile.alias}' is disabled", code="MODEL_DISABLED")
        return await self.acquire(
            profile.resource_group,
            owner_kind=owner_kind or ROLE_OWNER_KIND.get(profile.role, profile.role.replace("_", "-")),
            priority=profile.priority,
            job_id=job_id,
            step_id=step_id,
            ttl_seconds=ttl_seconds,
            exclusive=profile.exclusive,
            weight=float(profile.memory_gb),
            preemptible=preemptible,
            wait_timeout=wait_timeout,
            poll=poll,
            preempt=preempt,
            resource_group=profile.resource_group,
            metadata={"model_alias": profile.alias, "model": profile.model, "host": profile.host, "role": profile.role},
            reason=f"model-{profile.role.replace('_', '-')}",
        )

    # ------------------------------------------------------------------------------------------- media (9.8)
    async def acquire_gpu_for_media(
        self,
        kind: str,
        *,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        ttl_seconds: float | None = None,
        wait_timeout: float | None = None,
        poll: float | None = None,
        gpu_resource: str = GPU_224,
        video_resource: str = VIDEO_224,
        drain: Sequence[str] = MODEL_RESOURCES,
    ) -> MediaLeases:
        """GPU for an image/video step (Bauplan §32): ``gpu-224`` (+ ``video-224`` for video) with priority 100/90,
        then drain the AI model resource groups – the manager requests preemption of their lower-priority holders and
        the media job holds exclusive, weightless drain leases on them until it releases, so AI workloads cannot be
        re-scheduled onto the GPU host in between. Media leases are not preemptible."""
        if kind not in MEDIA_KINDS:
            raise ValidationFailed(f"media kind must be one of {sorted(MEDIA_KINDS)}", code="RESOURCE_MEDIA_KIND_INVALID")
        priority = PRIORITIES[kind]
        loop = asyncio.get_running_loop()
        deadline = None if wait_timeout is None else loop.time() + max(0.0, float(wait_timeout))

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - loop.time())

        common: dict[str, Any] = {
            "owner_kind": kind,
            "priority": priority,
            "job_id": job_id,
            "step_id": step_id,
            "ttl_seconds": ttl_seconds,
            "exclusive": True,
            "weight": 0.0,
            "preemptible": False,
            "poll": poll,
            "preempt": True,
            "reason": f"media-{kind}",
        }
        acquired: list[Lease] = []
        try:
            gpu: Lease | None = None
            video: Lease | None = None
            names = [gpu_resource] + ([video_resource] if kind == OwnerKind.VIDEO else [])
            for name in sorted(names):  # canonical order: no deadlock between concurrent media jobs
                lease = await self.acquire(name, wait_timeout=remaining(), metadata={"media": kind}, **common)
                acquired.append(lease)
                if name == gpu_resource:
                    gpu = lease
                else:
                    video = lease
            drained = await self._acquire_concurrently(
                sorted(set(drain) - set(names)), wait_timeout=remaining(), metadata={"drain_for": kind}, **common
            )
            acquired.extend(drained)
            assert gpu is not None
            return MediaLeases(kind=kind, gpu=gpu, video=video, drained=drained)
        except BaseException:
            for lease in reversed(acquired):
                with contextlib.suppress(Exception):
                    await asyncio.shield(self.release(lease.id, "acquire_abandoned"))
            raise

    async def _acquire_concurrently(self, resources: Sequence[str], **kw: Any) -> list[Lease]:
        got: dict[str, Lease] = {}

        async def one(name: str) -> None:
            got[name] = await self.acquire(name, **kw)

        tasks = [asyncio.create_task(one(n), name=f"acquire:{n}") for n in resources]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for lease in got.values():
                with contextlib.suppress(Exception):
                    await asyncio.shield(self.release(lease.id, "acquire_abandoned"))
            raise
        return [got[n] for n in resources]

    async def release_media(self, media: MediaLeases, reason: str = "completed") -> int:
        """Release GPU/video first, then the drain leases (AI workloads resume, Bauplan §32 steps 6–7)."""
        order = [media.gpu, *([media.video] if media.video else []), *media.drained]
        return await self.release_many([lease.id for lease in order], reason)

    # ------------------------------------------------------------------------------------------- holding helpers
    @asynccontextmanager
    async def _held(
        self,
        leases: Sequence[Lease],
        *,
        on_preempt: PreemptCallback | None,
        on_lost: LostCallback | None,
        interval: float | None,
    ) -> AsyncIterator[LeaseKeeper]:
        keeper = LeaseKeeper(self, leases, interval=interval, on_preempt=on_preempt, on_lost=on_lost)
        await keeper.start()
        reason = "completed"
        try:
            yield keeper
            if keeper.yield_requested.is_set():
                reason = "preempted"
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except BaseException:
            reason = "preempted" if keeper.yield_requested.is_set() else "error"
            raise
        finally:
            await keeper.stop()
            for lease in leases:
                if lease.id in keeper.lost_ids:
                    continue
                with contextlib.suppress(Exception):
                    await asyncio.shield(self.release(lease.id, reason))

    @asynccontextmanager
    async def hold(
        self,
        resource: str,
        *,
        on_preempt: PreemptCallback | None = None,
        on_lost: LostCallback | None = None,
        keeper_interval: float | None = None,
        **acquire_kw: Any,
    ) -> AsyncIterator[LeaseKeeper]:
        """``async with manager.hold(...) as held``: acquire, heartbeat in the background, observe preemption
        (``held.yield_requested`` / ``on_preempt``), release on exit (``completed``/``preempted``/``error``/
        ``cancelled``)."""
        lease = await self.acquire(resource, **acquire_kw)
        async with self._held([lease], on_preempt=on_preempt, on_lost=on_lost, interval=keeper_interval) as keeper:
            yield keeper

    @asynccontextmanager
    async def hold_model(
        self,
        profile: ModelProfileConfig,
        *,
        on_preempt: PreemptCallback | None = None,
        on_lost: LostCallback | None = None,
        keeper_interval: float | None = None,
        **acquire_kw: Any,
    ) -> AsyncIterator[LeaseKeeper]:
        lease = await self.acquire_model(profile, **acquire_kw)
        async with self._held([lease], on_preempt=on_preempt, on_lost=on_lost, interval=keeper_interval) as keeper:
            yield keeper

    @asynccontextmanager
    async def hold_media(
        self,
        kind: str,
        *,
        on_lost: LostCallback | None = None,
        keeper_interval: float | None = None,
        **acquire_kw: Any,
    ) -> AsyncIterator[tuple[MediaLeases, LeaseKeeper]]:
        media = await self.acquire_gpu_for_media(kind, **acquire_kw)
        async with self._held(media.all, on_preempt=None, on_lost=on_lost, interval=keeper_interval) as keeper:
            yield media, keeper
