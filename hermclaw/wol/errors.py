"""Stable failure codes of the Wake-on-LAN / readiness pipeline (Bauplan §26, P10 10.7)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from hermclaw.core.errors import WorkerError


class WakeFailureCode(StrEnum):
    """The six failure codes fixed by the architecture (Bauplan §26). Nothing else is ever reported."""

    WOL_SEND_FAILED = "WOL_SEND_FAILED"
    PING_TIMEOUT = "PING_TIMEOUT"
    SSH_TIMEOUT = "SSH_TIMEOUT"
    WORKER_API_TIMEOUT = "WORKER_API_TIMEOUT"
    MODEL_SERVICE_TIMEOUT = "MODEL_SERVICE_TIMEOUT"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"


class WolError(WorkerError):
    """Error of the Wake-on-LAN component. ``code`` is one of :class:`WakeFailureCode`.

    Raised directly by :func:`hermclaw.wol.magic.send_magic_packet` (``WOL_SEND_FAILED``) and by
    :meth:`hermclaw.wol.readiness.ReadyResult.raise_for_failure` (any of the six codes).
    """

    code = WakeFailureCode.WOL_SEND_FAILED.value
    http_status = 503

    def __init__(
        self,
        message: str,
        *,
        code: WakeFailureCode | str = WakeFailureCode.WOL_SEND_FAILED,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=WakeFailureCode(code).value, details=details)

    @property
    def failure_code(self) -> WakeFailureCode:
        return WakeFailureCode(self.code)
