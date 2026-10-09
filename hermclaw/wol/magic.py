"""Wake-on-LAN magic packet (P10 10.2).

A magic packet is ``6 × 0xFF`` followed by the target MAC repeated 16 times (102 bytes), sent as a UDP
datagram to the LAN broadcast address (conventionally port 9). The socket needs ``SO_BROADCAST``; it is
opened through an asyncio datagram endpoint with ``allow_broadcast=True``.

No external dependency (research 20261008-016). Errors surface as :class:`WolError` ``WOL_SEND_FAILED``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Callable

from hermclaw.core.logging import get_logger
from hermclaw.wol.errors import WakeFailureCode, WolError

log = get_logger(__name__)

MAGIC_PACKET_SIZE = 102
DEFAULT_WOL_PORT = 9
_HEX_ONLY = re.compile(r"^[0-9a-f]{12}$")
_SEPARATED = re.compile(r"^[0-9a-f]{2}([:-])[0-9a-f]{2}(\1[0-9a-f]{2}){4}$")
_DOTTED = re.compile(r"^[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}$")


def normalize_mac(mac: str) -> str:
    """Return ``aa:bb:cc:dd:ee:ff``. Accepts ``:``/``-`` separated, Cisco dotted and bare 12-hex forms."""
    if not isinstance(mac, str):
        raise WolError("MAC address must be a string", details={"reason": "invalid_mac"})
    value = mac.strip().lower()
    if _SEPARATED.match(value):
        digits = re.sub(r"[:-]", "", value)
    elif _DOTTED.match(value):
        digits = value.replace(".", "")
    elif _HEX_ONLY.match(value):
        digits = value
    else:
        raise WolError(f"invalid MAC address {mac!r}", details={"reason": "invalid_mac"})
    if digits in ("000000000000", "ffffffffffff"):
        raise WolError(f"MAC address {mac!r} cannot identify a NIC", details={"reason": "invalid_mac"})
    if int(digits[:2], 16) & 0x01:
        raise WolError(f"MAC address {mac!r} is a multicast address", details={"reason": "invalid_mac"})
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def build_magic_packet(mac: str) -> bytes:
    """``b'\\xff' * 6 + mac_bytes * 16`` – exactly 102 bytes."""
    mac_bytes = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    assert len(packet) == MAGIC_PACKET_SIZE  # invariant of the format, not input validation
    return packet


def validate_target(broadcast: str, port: int) -> tuple[str, int]:
    """The broadcast target must be an IPv4 literal (IPv6 has no broadcast) and a valid UDP port."""
    try:
        addr = ipaddress.IPv4Address(broadcast.strip())
    except (ipaddress.AddressValueError, AttributeError) as exc:
        raise WolError(f"broadcast address {broadcast!r} is not an IPv4 address", details={"reason": "invalid_broadcast"}) from exc
    if addr.is_unspecified:
        raise WolError("broadcast address 0.0.0.0 is not a valid target", details={"reason": "invalid_broadcast"})
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise WolError(f"invalid WOL port {port!r}", details={"reason": "invalid_port"})
    return str(addr), port


class _CaptureProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.error: Exception | None = None

    def error_received(self, exc: Exception) -> None:
        self.error = exc


async def send_magic_packet(
    mac: str,
    broadcast: str = "255.255.255.255",
    port: int = DEFAULT_WOL_PORT,
    *,
    copies: int = 1,
    source_address: str | None = None,
    timeout_seconds: float = 2.0,
) -> int:
    """Send ``copies`` magic packets for ``mac`` to ``broadcast:port``. Returns the bytes sent.

    ``source_address`` binds the socket to one local interface address (multi-homed orchestrator).
    Raises :class:`WolError` (``WOL_SEND_FAILED``) for invalid input or any socket error.
    """
    packet = build_magic_packet(mac)
    target = validate_target(broadcast, port)
    copies = max(1, min(int(copies), 16))
    details = {"broadcast": target[0], "port": target[1]}
    loop = asyncio.get_running_loop()
    transport: asyncio.DatagramTransport | None = None
    try:
        tr, protocol = await asyncio.wait_for(
            loop.create_datagram_endpoint(
                _CaptureProtocol,
                local_addr=(source_address, 0) if source_address else None,
                family=socket.AF_INET,
                allow_broadcast=True,
            ),
            timeout_seconds,
        )
        transport = tr
        sent = 0
        for _ in range(copies):
            tr.sendto(packet, target)
            # the selector transport reports a failed immediate send synchronously via error_received;
            # one loop iteration also flushes a (rare) buffered datagram and surfaces its error
            await asyncio.sleep(0)
            if protocol.error is not None:
                raise protocol.error
            sent += len(packet)
        buffered: Callable[[], int] = getattr(tr, "get_write_buffer_size", lambda: 0)
        deadline = loop.time() + timeout_seconds
        while buffered() > 0:
            if loop.time() >= deadline:
                raise TimeoutError("datagram could not be flushed")
            await asyncio.sleep(0.01)
        if protocol.error is not None:
            raise protocol.error
    except (OSError, TimeoutError) as exc:
        log.warning("wake-on-lan send failed", extra={"broadcast": target[0], "port": target[1], "error": type(exc).__name__})
        raise WolError(
            f"sending magic packet to {target[0]}:{target[1]} failed: {type(exc).__name__}: {exc}",
            code=WakeFailureCode.WOL_SEND_FAILED,
            details={**details, "reason": "socket_error", "error": type(exc).__name__},
        ) from exc
    finally:
        if transport is not None:
            transport.close()
    return sent
