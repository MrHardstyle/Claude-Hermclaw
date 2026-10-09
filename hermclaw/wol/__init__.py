"""Wake-on-LAN, worker readiness and idle sleep (Bauplan §2.7, §26; P10).

* :mod:`hermclaw.wol.magic`     – magic packet build/send (UDP broadcast, ``SO_BROADCAST``)
* :mod:`hermclaw.wol.probes`    – ping/TCP/SSH-banner/HTTP probes
* :mod:`hermclaw.wol.readiness` – :class:`WakeController` ``ensure_worker_ready(worker_id)``
* :mod:`hermclaw.wol.idle`      – :class:`IdleSleepPolicy` idle sleep hooks
* :mod:`hermclaw.wol.status`    – UI status values
"""

from hermclaw.wol.errors import WakeFailureCode, WolError
from hermclaw.wol.idle import (
    IdleDecision,
    IdleReason,
    IdleSleepOutcome,
    IdleSleepPolicy,
    IdleSleepSettings,
    RemoteCommandRunner,
    SleepResult,
)
from hermclaw.wol.magic import MAGIC_PACKET_SIZE, build_magic_packet, normalize_mac, send_magic_packet
from hermclaw.wol.probes import ProbeOutcome, Probes
from hermclaw.wol.readiness import (
    ReadyResult,
    RegistryLookup,
    RegistrySnapshot,
    StageResult,
    StageStatus,
    WakeController,
    WakeSettings,
    WakeStage,
    default_registry_lookup,
    ensure_workers_ready,
)
from hermclaw.wol.status import WorkerUiStatus, ui_status_for

__all__ = [
    "MAGIC_PACKET_SIZE",
    "IdleDecision",
    "IdleReason",
    "IdleSleepOutcome",
    "IdleSleepPolicy",
    "IdleSleepSettings",
    "ProbeOutcome",
    "Probes",
    "ReadyResult",
    "RegistryLookup",
    "RegistrySnapshot",
    "RemoteCommandRunner",
    "SleepResult",
    "StageResult",
    "StageStatus",
    "WakeController",
    "WakeFailureCode",
    "WakeSettings",
    "WakeStage",
    "WolError",
    "WorkerUiStatus",
    "build_magic_packet",
    "default_registry_lookup",
    "ensure_workers_ready",
    "normalize_mac",
    "send_magic_packet",
    "ui_status_for",
]
