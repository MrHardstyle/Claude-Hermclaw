"""P10 10.2: magic packet format, validation and a real UDP send to local listeners."""

from __future__ import annotations

import asyncio
import socket

import pytest

from hermclaw.wol.errors import WakeFailureCode, WolError
from hermclaw.wol.magic import MAGIC_PACKET_SIZE, build_magic_packet, normalize_mac, send_magic_packet, validate_target

MAC = "52:54:00:12:34:56"


class _Listener(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.packets: list[tuple[bytes, tuple[str, int]]] = []
        self.received = asyncio.Event()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.packets.append((data, addr))
        self.received.set()


async def _listen(host: str = "127.0.0.1") -> tuple[asyncio.DatagramTransport, _Listener, int]:
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(_Listener, local_addr=(host, 0), family=socket.AF_INET)
    port = transport.get_extra_info("sockname")[1]
    return transport, proto, port


def test_packet_layout_is_6_ff_plus_16_mac() -> None:
    pkt = build_magic_packet(MAC)
    assert len(pkt) == MAGIC_PACKET_SIZE == 102
    assert pkt[:6] == b"\xff" * 6
    mac_bytes = bytes.fromhex("525400123456")
    assert pkt[6:] == mac_bytes * 16
    for i in range(16):
        assert pkt[6 + 6 * i : 12 + 6 * i] == mac_bytes


@pytest.mark.parametrize(
    "raw",
    ["52:54:00:12:34:56", "52-54-00-12-34-56", "525400123456", "5254.0012.3456", " 52:54:00:12:34:56 ", "52:54:00:12:34:56".upper()],
)
def test_mac_forms_normalize(raw: str) -> None:
    assert normalize_mac(raw) == MAC
    assert build_magic_packet(raw) == build_magic_packet(MAC)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "52:54:00:12:34",
        "52:54:00:12:34:56:78",
        "52:54-00:12:34:56",  # mixed separators
        "zz:54:00:12:34:56",
        "00:00:00:00:00:00",
        "ff:ff:ff:ff:ff:ff",
        "01:00:5e:00:00:01",  # multicast
        "52:54:00:12:34:56; rm -rf /",
    ],
)
def test_invalid_mac_rejected(raw: str) -> None:
    with pytest.raises(WolError) as ei:
        build_magic_packet(raw)
    assert ei.value.code == WakeFailureCode.WOL_SEND_FAILED
    assert ei.value.details["reason"] == "invalid_mac"


@pytest.mark.parametrize(
    ("broadcast", "port", "reason"),
    [
        ("not-an-ip", 9, "invalid_broadcast"),
        ("ff02::1", 9, "invalid_broadcast"),
        ("0.0.0.0", 9, "invalid_broadcast"),
        ("192.168.178.255", 0, "invalid_port"),
        ("192.168.178.255", 70000, "invalid_port"),
        ("192.168.178.255", True, "invalid_port"),
    ],
)
def test_invalid_target_rejected(broadcast: str, port: int, reason: str) -> None:
    with pytest.raises(WolError) as ei:
        validate_target(broadcast, port)
    assert ei.value.details["reason"] == reason


def test_valid_target() -> None:
    assert validate_target(" 192.168.178.255 ", 9) == ("192.168.178.255", 9)


async def test_real_udp_send_unicast_listener() -> None:
    transport, proto, port = await _listen()
    try:
        sent = await send_magic_packet(MAC, "127.0.0.1", port)
        await asyncio.wait_for(proto.received.wait(), 2)
    finally:
        transport.close()
    assert sent == 102
    assert len(proto.packets) == 1
    assert proto.packets[0][0] == build_magic_packet(MAC)


async def test_real_udp_send_multiple_copies() -> None:
    transport, proto, port = await _listen()
    try:
        sent = await send_magic_packet(MAC, "127.0.0.1", port, copies=3)
        for _ in range(50):
            if len(proto.packets) >= 3:
                break
            await asyncio.sleep(0.02)
    finally:
        transport.close()
    assert sent == 3 * 102
    assert [p for p, _ in proto.packets] == [build_magic_packet(MAC)] * 3


async def test_real_udp_broadcast_send_uses_so_broadcast() -> None:
    """Loopback broadcast: a listener bound to 0.0.0.0 receives a datagram sent to 127.255.255.255 –
    the send itself only succeeds because the socket has SO_BROADCAST."""
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(_Listener, local_addr=("0.0.0.0", 0), family=socket.AF_INET)
    port = transport.get_extra_info("sockname")[1]
    try:
        try:
            await send_magic_packet(MAC, "127.255.255.255", port)
        except WolError as exc:  # pragma: no cover - kernel without loopback broadcast route
            pytest.skip(f"loopback broadcast not routable here: {exc}")
        try:
            await asyncio.wait_for(proto.received.wait(), 1.0)
        except TimeoutError:  # pragma: no cover - some kernels drop loopback broadcasts
            pytest.skip("loopback broadcast not delivered by this kernel")
    finally:
        transport.close()
    assert proto.packets[0][0] == build_magic_packet(MAC)


async def test_broadcast_without_so_broadcast_would_fail() -> None:
    """Regression guard for the SO_BROADCAST requirement: a plain UDP socket cannot send to a broadcast address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(PermissionError):
            sock.sendto(build_magic_packet(MAC), ("127.255.255.255", 9))
    except AssertionError:  # pragma: no cover - permissive kernels
        pytest.skip("kernel allows broadcast without SO_BROADCAST")
    finally:
        sock.close()


async def test_socket_error_maps_to_wol_send_failed() -> None:
    # binding to an address that is not local fails with EADDRNOTAVAIL – a real socket error
    with pytest.raises(WolError) as ei:
        await send_magic_packet(MAC, "127.0.0.1", 9, source_address="192.0.2.77")
    assert ei.value.code == "WOL_SEND_FAILED"
    assert ei.value.details["reason"] == "socket_error"
    assert ei.value.details["broadcast"] == "127.0.0.1"


async def test_send_rejects_invalid_input_before_opening_a_socket() -> None:
    with pytest.raises(WolError) as ei:
        await send_magic_packet("bogus", "127.0.0.1", 9)
    assert ei.value.details["reason"] == "invalid_mac"
    with pytest.raises(WolError) as ei:
        await send_magic_packet(MAC, "256.1.1.1", 9)
    assert ei.value.details["reason"] == "invalid_broadcast"
