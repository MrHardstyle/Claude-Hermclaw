"""Value objects and errors of the resource manager."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hermclaw.core.errors import ConflictError, ResourceUnavailable
from hermclaw.persistence.models import ResourceLease, ResourceRequest
from hermclaw.resources.constants import HOLDING_STATES, LEASE_ACTIVE, LEASE_PREEMPTING


class LeaseLost(ResourceUnavailable):
    """The lease is no longer held (released, expired, force-expired after the preemption grace). Stop the work."""

    code = "LEASE_LOST"


class LeaseNotOwned(ConflictError):
    code = "LEASE_NOT_OWNED"


@dataclass(frozen=True)
class Lease:
    id: uuid.UUID
    resource: str
    resource_group: str
    owner_kind: str
    holder: str
    priority: int
    state: str
    exclusive: bool
    weight: float
    preemptible: bool
    job_id: uuid.UUID | None
    step_id: uuid.UUID | None
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None = None
    release_reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def holding(self) -> bool:
        return self.state in HOLDING_STATES

    @property
    def should_yield(self) -> bool:
        """True once the manager asked the holder to checkpoint and release (or the lease is gone)."""
        return self.state != LEASE_ACTIVE

    @property
    def preemption(self) -> Mapping[str, Any] | None:
        p = self.metadata.get("preempt")
        return p if isinstance(p, Mapping) else None

    @classmethod
    def from_row(cls, row: ResourceLease) -> Lease:
        return cls(
            id=row.id,
            resource=row.resource,
            resource_group=row.resource_group,
            owner_kind=row.owner_kind,
            holder=row.holder,
            priority=row.priority,
            state=row.state,
            exclusive=row.exclusive,
            weight=float(row.weight),
            preemptible=row.preemptible,
            job_id=row.owner_job_id,
            step_id=row.owner_step_id,
            acquired_at=row.acquired_at,
            heartbeat_at=row.heartbeat_at,
            expires_at=row.expires_at,
            released_at=row.released_at,
            release_reason=row.release_reason,
            metadata=dict(row.metadata_ or {}),
        )


@dataclass(frozen=True)
class LeaseStatus:
    """Result of a heartbeat / status poll."""

    lease_id: uuid.UUID
    state: str
    expires_at: datetime
    should_yield: bool
    preempt_reason: str | None = None
    preempt_deadline: datetime | None = None


@dataclass(frozen=True)
class WaitingRequest:
    id: uuid.UUID
    resource: str
    owner_kind: str
    holder: str
    priority: int
    state: str
    created_at: datetime
    expires_at: datetime
    job_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None

    @classmethod
    def from_row(cls, row: ResourceRequest) -> WaitingRequest:
        return cls(
            id=row.id,
            resource=row.resource,
            owner_kind=row.owner_kind,
            holder=row.holder,
            priority=row.priority,
            state=row.state,
            created_at=row.created_at,
            expires_at=row.expires_at,
            job_id=row.owner_job_id,
            step_id=row.owner_step_id,
        )


@dataclass(frozen=True)
class PreemptionResult:
    resource: str
    requested: list[Lease] = field(default_factory=list)
    already_preempting: list[Lease] = field(default_factory=list)
    refused_non_preemptible: list[Lease] = field(default_factory=list)
    refused_priority: list[Lease] = field(default_factory=list)

    @property
    def pending(self) -> list[Lease]:
        """Leases that will release (or be force-expired) because of this or an earlier preemption request."""
        return [*self.requested, *self.already_preempting]


@dataclass(frozen=True)
class SweepReport:
    expired: list[Lease] = field(default_factory=list)
    grace_timeouts: list[Lease] = field(default_factory=list)
    stale_requests: list[uuid.UUID] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.expired) + len(self.grace_timeouts) + len(self.stale_requests)


@dataclass(frozen=True)
class RecoveryReport:
    holder: str
    released: list[Lease] = field(default_factory=list)
    cancelled_requests: list[uuid.UUID] = field(default_factory=list)
    sweep: SweepReport = field(default_factory=SweepReport)


@dataclass(frozen=True)
class MediaLeases:
    """Leases held by one media job: the GPU (+ video slot) and the drained AI model resource groups."""

    kind: str
    gpu: Lease
    video: Lease | None
    drained: list[Lease] = field(default_factory=list)

    @property
    def all(self) -> list[Lease]:
        out = [self.gpu]
        if self.video is not None:
            out.append(self.video)
        out.extend(self.drained)
        return out


def is_preempting(state: str) -> bool:
    return state == LEASE_PREEMPTING
