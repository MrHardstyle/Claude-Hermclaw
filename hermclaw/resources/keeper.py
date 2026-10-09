"""Lease keeper: keeps held leases alive and turns manager-side preemption into a holder callback (P09 9.4/9.6).

The holder never preempts anybody: it only *observes* that the manager has marked one of its leases ``preempting``
and is expected to bring its work to a safe checkpoint and release before ``policies.leases.preemption_grace_seconds``
elapse. Otherwise the manager force-expires the lease (``resource.expired`` with ``reason=preemption_grace_timeout``)
and the next heartbeat reports the loss through :attr:`LeaseKeeper.lost`.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from hermclaw.core.logging import get_logger
from hermclaw.resources.types import Lease, LeaseLost, LeaseStatus

if TYPE_CHECKING:
    from hermclaw.resources.manager import ResourceManager

log = get_logger(__name__)

PreemptCallback = Callable[[Lease, LeaseStatus], Awaitable[None] | None]
LostCallback = Callable[[Lease], Awaitable[None] | None]

MIN_INTERVAL = 0.05


class LeaseKeeper:
    """Background heartbeat for a set of leases of one holder.

    * ``yield_requested`` is set (and ``on_preempt`` called once per lease) as soon as the manager requests preemption,
    * ``lost`` is set (and ``on_lost`` called) when a lease was released/expired behind the holder's back.
    """

    def __init__(
        self,
        manager: ResourceManager,
        leases: Sequence[Lease],
        *,
        interval: float | None = None,
        on_preempt: PreemptCallback | None = None,
        on_lost: LostCallback | None = None,
    ) -> None:
        self.manager = manager
        self.leases: list[Lease] = list(leases)
        self.interval = max(MIN_INTERVAL, interval if interval is not None else manager.keeper_interval(self.leases))
        self.on_preempt = on_preempt
        self.on_lost = on_lost
        self.yield_requested = asyncio.Event()
        self.lost = asyncio.Event()
        self.preempt_status: dict[uuid.UUID, LeaseStatus] = {}
        self.lost_ids: set[uuid.UUID] = set()
        self.heartbeat_errors = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def lease(self) -> Lease:
        return self.leases[0]

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name=f"lease-keeper:{self.lease.resource}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def beat(self) -> None:
        """One heartbeat round over all still-held leases (also used by the background loop)."""
        for lease in list(self.leases):
            if lease.id in self.lost_ids:
                continue
            try:
                status = await self.manager.heartbeat(lease.id)
            except LeaseLost:
                self.lost_ids.add(lease.id)
                self.lost.set()
                log.warning("lease lost resource=%s lease=%s", lease.resource, lease.id)
                await _call(self.on_lost, lease)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # transient DB problems must not kill the keeper; the TTL is the safety net
                self.heartbeat_errors += 1
                log.warning("lease heartbeat failed resource=%s lease=%s error=%s", lease.resource, lease.id, type(exc).__name__)
                continue
            if status.should_yield and lease.id not in self.preempt_status:
                self.preempt_status[lease.id] = status
                self.yield_requested.set()
                await _call(self.on_preempt, lease, status)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.beat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # callback errors are logged, the keeper keeps the remaining leases alive
                log.warning("lease keeper callback failed: %s", type(exc).__name__)


async def _call(cb: Callable[..., Awaitable[None] | None] | None, *args: object) -> None:
    if cb is None:
        return
    res = cb(*args)
    if inspect.isawaitable(res):
        await res
