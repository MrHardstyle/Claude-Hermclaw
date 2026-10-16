"""D-014 dependency installs: allow-listing egress proxy, egress gateway and the sandbox command line (no engine)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from hermclaw.contracts.worker import CommandRequest
from hermclaw.core.config import EgressPolicy, SandboxPolicy
from hermclaw.core.errors import ConfigError
from worker.execution import egress_proxy as ep
from worker.execution.egress import LABEL_CONFIG, PROXY_CONTAINER, EgressGateway, host_nameservers
from worker.execution.sandbox import LocalSandbox, PodmanSandbox

EGRESS = EgressPolicy(enabled=True, dns=["192.0.2.53"])
POLICY = SandboxPolicy(engine="podman", image="docker.io/library/python:3.12-slim", egress=EGRESS)


def req(**kw: Any) -> CommandRequest:
    base: dict[str, Any] = {"request_id": "req-1", "job_id": "job-1", "step_id": "step-1", "workspace": "ws1", "command": "true"}
    base.update(kw)
    return CommandRequest(**base)


def flag(argv: list[str], name: str) -> list[str]:
    return [a.split("=", 1)[1] for a in argv if a.startswith(name + "=")]


# --------------------------------------------------------------------------------------------- proxy
@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("pypi.org", True),
        ("PyPI.org.", True),
        ("evil-pypi.org", False),
        ("pypi.org.evil.com", False),
        ("cdn.files.example", True),
        ("files.example", False),
        ("", False),
    ],
)
def test_proxy_host_allow_list(host: str, ok: bool) -> None:
    assert ep.host_allowed(host, ["pypi.org", ".files.example"]) is ok


def test_proxy_parses_only_connect_requests() -> None:
    assert ep.parse_connect(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org\r\n\r\n") == ("pypi.org", 443)
    assert ep.parse_connect(b"GET http://pypi.org/ HTTP/1.1\r\n\r\n") is None
    assert ep.parse_connect(b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n") is None
    assert ep.parse_connect(b"CONNECT pypi.org HTTP/1.1\r\n\r\n") is None
    assert ep.public_address("151.101.0.223") and not ep.public_address("192.168.178.222") and not ep.public_address("127.0.0.1")


async def _ask(port: int, head: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(4096), 5)
    writer.close()
    return data


async def test_proxy_refuses_plain_http_other_hosts_and_private_addresses(capsys: pytest.CaptureFixture[str]) -> None:
    proxy = ep.EgressProxy(["localhost"], {443})
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert b" 405 " in await _ask(port, b"GET http://pypi.org/simple/ HTTP/1.1\r\nHost: pypi.org\r\n\r\n")
        assert b" 403 " in await _ask(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        assert b" 403 " in await _ask(port, b"CONNECT localhost:22 HTTP/1.1\r\n\r\n")  # port not allowed
        reply = await _ask(port, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")  # allow-listed name, loopback address
        assert b" 403 " in reply and b"public address" in reply
    assert '"decision": "blocked"' in capsys.readouterr().out


async def test_proxy_tunnels_to_an_allowed_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.read(100))
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    monkeypatch.setattr(ep, "public_address", lambda addr: True)  # stands in for the registry's public address
    proxy = ep.EgressProxy(["localhost"], {up_port})
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with upstream, server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT localhost:{up_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert b"200 Connection established" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(4), 5) == b"ping"
        writer.close()


def test_proxy_requires_an_allow_list() -> None:
    assert ep.main(["--listen", "127.0.0.1:0"]) == 2


# --------------------------------------------------------------------------------------------- policy + gateway
def test_egress_policy_validation() -> None:
    assert EgressPolicy().enabled is False and "pypi.org" in EgressPolicy().allowed_hosts
    with pytest.raises(ValueError, match="not inside"):
        EgressPolicy(proxy_ip="10.1.1.1")
    with pytest.raises(ValueError, match="must differ"):
        EgressPolicy(outbound_network="hermclaw-egress")
    with pytest.raises(ValueError, match="not a host name"):
        EgressPolicy(allowed_hosts=["https://pypi.org"])
    assert EgressPolicy(proxy_port=8080).proxy_url == "http://10.89.240.2:8080"


def test_host_nameservers_skip_loopback_and_prefer_ipv4(tmp_path: Path) -> None:
    conf = tmp_path / "resolv.conf"
    conf.write_text("nameserver 127.0.0.53\nnameserver fd00::1\nnameserver 8.8.8.8\nnameserver fe80::1%eth0\noptions edns0\n")
    assert host_nameservers(conf) == ["8.8.8.8", "fd00::1"]
    assert host_nameservers(tmp_path / "missing") == []


class FakeEngine:
    def __init__(self, *, networks: set[str] | None = None, proxy: tuple[bool, str] | None = None) -> None:
        self.networks = networks or set()
        self.proxy = proxy  # (running, config label)
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], *, timeout_seconds: float = 60.0) -> tuple[int, str, str]:
        self.calls.append(argv)
        sub = argv[1:3]
        if sub == ["network", "exists"]:
            return (0 if argv[3] in self.networks else 1), "", ""
        if sub == ["network", "create"]:
            self.networks.add(argv[-1])
            return 0, "", ""
        if sub == ["container", "inspect"]:
            if self.proxy is None:
                return 125, "", "no such container"
            return 0, f"{'true' if self.proxy[0] else 'false'} {self.proxy[1]}\n", ""
        if argv[1] == "rm":
            self.proxy = None
            return 0, "", ""
        if argv[1] == "run":
            label = next(a.split("=", 2)[2] for a in argv if a.startswith(f"--label={LABEL_CONFIG}="))
            self.proxy = (True, label)
            return 0, "cid\n", ""
        if argv[1] == "logs":
            return 0, '{"decision": "listening"}\n', ""
        return 0, "", ""


async def test_gateway_creates_networks_and_proxy_once(tmp_path: Path) -> None:
    engine = FakeEngine()
    gw = EgressGateway(EGRESS, exec_fn=engine)
    assert await gw.ensure() == "http://10.89.240.2:3128"
    creates = [c for c in engine.calls if c[1:3] == ["network", "create"]]
    assert ["--internal" in c for c in creates] == [True, False] and all("--disable-dns" in c for c in creates)
    run = next(c for c in engine.calls if c[1] == "run")
    assert f"--name={PROXY_CONTAINER}" in run and "--network=hermclaw-egress:ip=10.89.240.2" in run
    assert "--network=hermclaw-egress-out" in run and "--dns=192.0.2.53" in run
    assert {"--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534"} <= set(run)
    assert not any(a.startswith("--label=hermclaw.managed") for a in run)  # recovery never kills the proxy
    assert run[run.index("--allow") + 1] == "pypi.org"
    engine.calls.clear()
    await gw.ensure()
    assert not any(c[1] in ("run", "rm") or c[1:3] == ["network", "create"] for c in engine.calls)


async def test_gateway_recreates_an_outdated_proxy_and_refuses_when_disabled() -> None:
    engine = FakeEngine(networks={"hermclaw-egress", "hermclaw-egress-out"}, proxy=(True, "old"))
    await EgressGateway(EGRESS, exec_fn=engine).ensure()
    assert [c[1] for c in engine.calls if c[1] in ("rm", "run")] == ["rm", "run"]
    with pytest.raises(ConfigError, match="disabled"):
        await EgressGateway(EgressPolicy(), exec_fn=FakeEngine()).ensure()
    status = await EgressGateway(EGRESS, exec_fn=FakeEngine()).status()
    assert not status.ready and "not running" in status.detail


# --------------------------------------------------------------------------------------------- sandbox command line
@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "workspaces" / "ws1"
    d.mkdir(parents=True)
    return d


def test_install_request_gets_the_internal_network_proxy_env_and_writable_deps(ws: Path) -> None:
    deps_root = ws.parent.parent / "deps"
    box = PodmanSandbox(POLICY, deps_root=deps_root, selinux_relabel=False)
    argv = box.build_invocation(req(egress=True, command="pip install -r requirements.txt"), ws).argv
    assert flag(argv, "--network") == ["hermclaw-egress"]
    env = dict(e.split("=", 1) for e in flag(argv, "--env"))
    assert env["HTTPS_PROXY"] == env["http_proxy"] == "http://10.89.240.2:3128" and env["NO_PROXY"] == ""
    assert env["PYTHONPATH"] == "/workspace:/deps/python"
    assert f"{deps_root / 'ws1'}:/deps:rw" in flag(argv, "--volume")
    assert f"{deps_root / 'ws1' / 'node_modules'}:/workspace/node_modules:rw" in flag(argv, "--volume")
    assert (deps_root / "ws1" / "python").is_dir()


def test_later_commands_see_the_deps_read_only_without_network(ws: Path) -> None:
    deps_root = ws.parent.parent / "deps"
    box = PodmanSandbox(POLICY, deps_root=deps_root, selinux_relabel=False)
    plain = box.build_invocation(req(), ws).argv
    assert not any(v.endswith(":/deps:ro") for v in flag(plain, "--volume")) and "PYTHONPATH=/workspace" in flag(plain, "--env")
    (deps_root / "ws1" / "python").mkdir(parents=True)
    argv = box.build_invocation(req(command="pytest -q"), ws).argv
    assert flag(argv, "--network") == ["none"]
    assert f"{deps_root / 'ws1'}:/deps:ro" in flag(argv, "--volume") and "PYTHONPATH=/workspace:/deps/python" in flag(argv, "--env")
    assert not any("PROXY" in e for e in flag(argv, "--env"))


async def test_install_requests_are_refused_unless_enabled_and_never_get_full_network(ws: Path) -> None:
    deps_root = ws.parent.parent / "deps"
    disabled = PodmanSandbox(SandboxPolicy(engine="podman"), deps_root=deps_root)
    res = await disabled.run(req(egress=True), ws)
    assert res.exit_code is None and (res.error or "").startswith("EGRESS_DISABLED")
    both = await PodmanSandbox(POLICY, deps_root=deps_root).run(req(egress=True, network=True), ws)
    assert (both.error or "").startswith("EGRESS_INVALID")
    no_root = await PodmanSandbox(POLICY).run(req(egress=True), ws)
    assert (no_root.error or "").startswith("EGRESS_UNSUPPORTED")
    local = await LocalSandbox(SandboxPolicy(engine="local", egress=EGRESS), environment="test").run(req(egress=True), ws)
    assert (local.error or "").startswith("EGRESS_UNSUPPORTED")
