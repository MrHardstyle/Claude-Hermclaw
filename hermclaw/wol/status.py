"""UI-facing worker status (P10 10.8): six values derived from the registry's :class:`WorkerState`."""

from __future__ import annotations

from enum import StrEnum

from hermclaw.contracts.common import WorkerState


class WorkerUiStatus(StrEnum):
    STARTING = "STARTING"
    WAKING = "WAKING"
    READY = "READY"
    BUSY = "BUSY"
    ERROR = "ERROR"
    SLEEPING = "SLEEPING"


_MAP: dict[WorkerState, WorkerUiStatus] = {
    WorkerState.starting: WorkerUiStatus.STARTING,
    WorkerState.waking: WorkerUiStatus.WAKING,
    WorkerState.ready: WorkerUiStatus.READY,
    WorkerState.busy: WorkerUiStatus.BUSY,
    WorkerState.draining: WorkerUiStatus.BUSY,  # still finishing work, takes nothing new
    WorkerState.sleeping: WorkerUiStatus.SLEEPING,
    WorkerState.error: WorkerUiStatus.ERROR,
}


def ui_status_for(state: WorkerState | str | None, *, wakeable: bool = True) -> WorkerUiStatus:
    """Map a registry state to the UI status.

    ``offline`` (no heartbeat) is shown as ``SLEEPING`` when the host can be woken (Wake-on-LAN enabled),
    otherwise as ``ERROR`` – an offline host that cannot be woken needs an operator. Unknown/absent
    states (worker not registered yet) count as offline.
    """
    try:
        ws = WorkerState(state) if state is not None else WorkerState.offline
    except ValueError:
        return WorkerUiStatus.ERROR
    if ws == WorkerState.offline:
        return WorkerUiStatus.SLEEPING if wakeable else WorkerUiStatus.ERROR
    return _MAP[ws]
