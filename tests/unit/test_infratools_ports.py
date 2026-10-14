"""P28.4 – port collisions against the Bauplan §6 port plan and live listening sockets (``ss`` output)."""

from __future__ import annotations

import pytest

from hermclaw.core.config import HostConfig, HostsConfig
from hermclaw.infratools.ports import (
    BAUPLAN_PORT_PLAN,
    ListeningSocket,
    PortChecker,
    PortPlan,
    addresses_overlap,
    detect_kind,
    evaluate_collisions,
    nginx_requested_ports,
    normalise_address,
    parse_ss,
    requested_ports,
    systemd_requested_ports,
)
from hermclaw.infratools.process import HostExecutor

SS_TCP = """\
LISTEN 0      511          0.0.0.0:80        0.0.0.0:*    users:(("nginx",pid=812,fd=6),("nginx",pid=811,fd=6))
LISTEN 0      4096       127.0.0.1:5432      0.0.0.0:*    users:(("postgres",pid=900,fd=5))
LISTEN 0      128             [::]:22           [::]:*    users:(("sshd",pid=700,fd=4))
LISTEN 0      4096   192.168.178.225:8000    0.0.0.0:*
LISTEN 0      511                *:8787            *:*
LISTEN 0      4096   [fe80::1%eth0]:9999        [::]:*
garbage line
"""

HOSTS = HostsConfig(
    hosts=[
        HostConfig(id="orchestrator-225", address="192.168.178.225", role="orchestrator"),
        HostConfig(id="web-223", address="192.168.178.223", role="webui"),
    ]
)


class SsRunner:
    def __init__(self, tcp: str = SS_TCP, udp: str = "", rc: int = 0) -> None:
        self.tcp, self.udp, self.rc = tcp, udp, rc
        self.commands: list[str] = []

    async def run(self, host_id: str, command: str, timeout: float) -> tuple[int, str, str]:
        self.commands.append(command)
        if self.rc:
            return self.rc, "", "ss: permission denied"
        return 0, (self.udp if "-lun" in command else self.tcp), ""


def test_parse_ss_with_and_without_process_info() -> None:
    sockets = parse_ss(SS_TCP)
    by_port = {s.port: s for s in sockets}
    assert by_port[80].process == "nginx" and by_port[80].pid == 812 and by_port[80].address == "0.0.0.0"
    assert by_port[22].address == "::" and by_port[22].process == "sshd"
    assert by_port[8000].process is None and by_port[8000].address == "192.168.178.225"
    assert by_port[8787].address == "*"
    assert by_port[9999].address == "fe80::1"
    assert len(sockets) == 6
    # "ss -tulnH" style output with a Netid column
    mixed = parse_ss("udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:*\ntcp LISTEN 0 128 127.0.0.1:631 0.0.0.0:*\n")
    assert [(s.protocol, s.port) for s in mixed] == [("udp", 53), ("tcp", 631)]


def test_address_helpers() -> None:
    assert normalise_address("[::ffff:10.0.0.1]") == "10.0.0.1"
    assert normalise_address("localhost") == "127.0.0.1"
    assert normalise_address("0.0.0.0") == "*" and normalise_address(None) == "*"
    assert addresses_overlap("127.0.0.1", "::1")
    assert addresses_overlap("*", "10.1.1.1")
    assert not addresses_overlap("192.168.178.225", "127.0.0.1")


def test_detect_kind() -> None:
    assert detect_kind("[Unit]\nDescription=x\n[Service]\nExecStart=/bin/x\n") == "systemd"
    assert detect_kind("services:\n  a: {image: x}\n") == "compose"
    assert detect_kind("server { listen 80; }") == "nginx"


def test_nginx_requested_ports_shared_sockets_and_implicit_80() -> None:
    conf = """
    upstream api { server 127.0.0.1:8000; }
    server { listen 443 ssl; server_name a; }
    server { listen 443 ssl; server_name b; }
    server { listen [::]:443 ssl; server_name a; }
    server { listen 127.0.0.1:8080; listen unix:/run/x.sock; }
    server { listen 443 quic reuseport; }
    server { server_name implicit; }
    """
    ports, _ = nginx_requested_ports(conf)
    keys = {(p.address, p.port, p.protocol) for p in ports}
    assert keys == {(None, 443, "tcp"), ("::", 443, "tcp"), ("127.0.0.1", 8080, "tcp"), (None, 443, "udp"), (None, 80, "tcp")}
    assert len(ports) == len(keys)


def test_systemd_requested_ports() -> None:
    unit = """
[Socket]
ListenStream=127.0.0.1:8787
ListenStream=
ListenStream=[::]:9100
ListenDatagram=5353
ListenStream=/run/hermclaw.sock
[Service]
ExecStart=/opt/hermclaw/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Environment=LITELLM_PORT=4000
"""
    ports, findings = systemd_requested_ports(unit)
    assert not findings
    assert [(p.address, p.port, p.protocol, p.heuristic) for p in ports] == [
        ("::", 9100, "tcp", False),
        (None, 5353, "udp", False),
        (None, 8000, "tcp", True),
        (None, 4000, "tcp", True),
    ]


def test_plan_collision_and_owner_match() -> None:
    _, ports, _ = requested_ports("services:\n  api:\n    image: x:1\n    ports: ['8000:8000', '127.0.0.1:5432:5432']\n")
    plan = PortPlan().for_address("192.168.178.225")
    collisions, notes = evaluate_collisions(ports, plan_entries=plan, listening=None)
    assert {(c.port, c.kind, c.owner) for c in collisions} == {(8000, "plan", "hermclaw-api"), (5432, "plan", "postgresql")}
    collisions, notes = evaluate_collisions(ports, plan_entries=plan, listening=None, owner="hermclaw-api")
    assert {(c.port, c.owner) for c in collisions} == {(5432, "postgresql")}
    assert any("8000/tcp is planned for hermclaw-api" in n for n in notes)


def test_live_collision_owner_process_and_unknown_process() -> None:
    _, ports, _ = requested_ports("server { listen 80; server_name x; }\nserver { listen 8000; }", "nginx")
    listening = parse_ss(SS_TCP)
    plan = PortPlan().for_address("192.168.178.223")
    collisions, _ = evaluate_collisions(ports, plan_entries=plan, listening=listening, owner="nginx", check_duplicates=False)
    # 80 is planned for nginx and served by an nginx process → expected; a wildcard listen on 8000 overlaps the
    # specific 192.168.178.225:8000 socket of an unknown process → live collision
    assert [(c.port, c.kind, c.owner) for c in collisions] == [(8000, "live", "unknown process")]
    collisions, _ = evaluate_collisions(ports, plan_entries=plan, listening=listening, owner="caddy", check_duplicates=False)
    live = [c for c in collisions if c.kind == "live" and c.port == 80]
    assert len(live) == 1 and live[0].owner == "nginx" and live[0].pid == 812
    assert any(c.kind == "plan" and c.port == 80 for c in collisions)
    # listening without process info (no root for ss -p) on a planned port of the owner → note, not a collision
    anon = [ListeningSocket("0.0.0.0", 80, "tcp")]
    collisions, notes = evaluate_collisions(ports[:1], plan_entries=plan, listening=anon, owner="nginx")
    assert not collisions and any("process unknown" in n for n in notes)


def test_duplicate_requests_in_one_config() -> None:
    _, ports, _ = requested_ports("[Socket]\nListenStream=8080\n[Service]\nExecStart=/x --port 8080\n", "systemd")
    collisions, _ = evaluate_collisions(ports, plan_entries=(), listening=None)
    assert ports and not [c for c in collisions if c.kind == "duplicate"]  # deduplicated identical heuristic hits
    _, ports, _ = requested_ports("services:\n  a: {image: x:1, ports: ['9000:1']}\n  b: {image: x:1, ports: ['9000:2']}\n")
    collisions, _ = evaluate_collisions(ports, plan_entries=(), listening=None)
    assert [c.kind for c in collisions] == ["duplicate"]


def test_port_plan_matches_bauplan_section_6() -> None:
    plan = {(p.address.rsplit(".", 1)[1], p.port) for p in BAUPLAN_PORT_PLAN}
    assert plan == {
        ("223", 80), ("223", 443),
        ("225", 8000), ("225", 4000), ("225", 5432), ("225", 9100),
        ("222", 8787),
        ("224", 8787), ("224", 11434),
        ("226", 22), ("226", 80), ("226", 443),
    }  # fmt: skip


async def test_checker_remote_host_uses_hosts_address_and_ss() -> None:
    runner = SsRunner()
    checker = PortChecker(HostExecutor(remote=runner, hosts=HOSTS), hosts=HOSTS)
    report = await checker.check("services:\n  db:\n    image: postgres:16\n    ports: ['5432:5432']\n", host="orchestrator-225")
    assert report.address == "192.168.178.225" and report.live_checked
    kinds = {(c.kind, c.owner) for c in report.collisions}
    assert ("plan", "postgresql") in kinds and ("live", "postgres") in kinds
    assert not report.ok and "COLLISIONS" in report.render()
    assert runner.commands == ["ss -ltnpH"]
    ok = await checker.check(
        "services:\n  db:\n    image: postgres:16\n    ports: ['5432:5432']\n", host="orchestrator-225", owner="postgresql"
    )
    assert ok.ok, ok.render()


async def test_checker_live_failure_is_reported_not_hidden() -> None:
    checker = PortChecker(HostExecutor(remote=SsRunner(rc=1), hosts=HOSTS), hosts=HOSTS)
    report = await checker.check("server { listen 8443; }", host="web-223")
    assert not report.live_checked and report.live_error and not report.ok


@pytest.mark.parametrize("text", ["services:\n  a: {image: x:1, ports: ['${PORT}:80']}\n"])
def test_unresolved_variables_are_warned(text: str) -> None:
    _, ports, findings = requested_ports(text)
    assert not ports and findings[0].code == "PORT_UNRESOLVED"
