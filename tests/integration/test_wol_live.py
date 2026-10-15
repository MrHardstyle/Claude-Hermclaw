"""P10 live verification on the real LAN hosts (BLOCKER-001: skipped unless run with ``-m live``).

Run on the orchestrator (.225) with the production configuration::

    HERMCLAW_CONFIG_DIR=/etc/hermclaw HERMCLAW_LIVE_WOL_WORKER=model-224 \\
        .venv/bin/pytest -m live tests/integration/test_wol_live.py -v

``test_live_probes`` checks ping/SSH/API/services, ``test_live_magic_packet`` sends one magic packet (safe while
the host is awake – the packet is ignored; skipped while Wake-on-LAN is disabled). ``test_live_ensure_worker_ready`` runs the full pipeline against
the production database configured by ``HERMCLAW_DATABASE_URL`` (heartbeats arrive there), so it is opt-in
via ``HERMCLAW_LIVE_WOL_DB=1``.
"""

from __future__ import annotations

import os

import pytest

from hermclaw.core.config import load_config
from hermclaw.wol import IssueSeverity, Probes, WakeController, assert_wake_config, send_magic_packet
from hermclaw.wol.probes import build_http_url

pytestmark = [pytest.mark.live, pytest.mark.integration]


def _worker_id() -> str:
    wid = os.environ.get("HERMCLAW_LIVE_WOL_WORKER")
    if not wid:
        pytest.skip("set HERMCLAW_LIVE_WOL_WORKER to a configured worker host id (e.g. model-224)")
    return wid


async def test_live_probes() -> None:
    """Production hosts.yaml passes the wake config check, and ping/SSH/API/services of the worker answer. Runs
    with Wake-on-LAN disabled too: the readiness probes do not depend on it."""
    cfg = load_config()
    host = cfg.hosts.by_id(_worker_id())
    issues = assert_wake_config(cfg)  # 10.1 on the production hosts.yaml: no errors (raises otherwise)
    assert all(i.severity == IssueSeverity.warning for i in issues)
    wol = host.wake_on_lan
    probes = Probes()
    try:
        ssh_port = host.ssh.port if host.ssh else 22
        ping = await probes.ping(host.address, timeout_seconds=float(wol.ping_timeout_seconds), fallback_port=ssh_port)
        assert ping.ok, ping.detail
        assert (await probes.ssh_banner(host.address, ssh_port, timeout_seconds=10)).ok
        for svc in host.services:
            if svc.http_path:
                out = await probes.http_get(build_http_url(host.address, svc.port, svc.http_path), timeout_seconds=10)
            else:
                out = await probes.tcp_connect(host.address, svc.port, timeout_seconds=10)
            assert out.ok, f"{svc.name}: {out.detail}"
    finally:
        await probes.aclose()


async def test_live_magic_packet() -> None:
    """Sends one magic packet (ignored by an awake host). Skipped while the operator keeps Wake-on-LAN disabled in
    hosts.yaml (not set up yet): a deliberate configuration, not a failure (BUG-151)."""
    cfg = load_config()
    host = cfg.hosts.by_id(_worker_id())
    wol = host.wake_on_lan
    if not wol.enabled:
        pytest.skip(f"Wake-on-LAN is disabled for {host.id} in hosts.yaml (wake_on_lan.enabled: false)")
    assert wol.mac and wol.mac != "00:00:00:00:00:00", "Wake-on-LAN enabled without a real MAC"
    assert await send_magic_packet(wol.mac, wol.broadcast, wol.port) == 102


async def test_live_ensure_worker_ready() -> None:
    if os.environ.get("HERMCLAW_LIVE_WOL_DB") != "1":
        pytest.skip("set HERMCLAW_LIVE_WOL_DB=1 to run against the production database")
    from hermclaw.persistence.db import dispose_engine, get_sessionmaker, init_engine

    init_engine()
    wc = WakeController(get_sessionmaker(), load_config())
    try:
        result = await wc.ensure_worker_ready(_worker_id())
    finally:
        await wc.aclose()
        await dispose_engine()
    assert result.ready, f"{result.error_code}: {result.message}"
