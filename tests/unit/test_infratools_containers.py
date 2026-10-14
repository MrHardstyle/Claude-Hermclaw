"""P28.1 – container inventory parsing, argv safety and engine handling (no real engine needed)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hermclaw.core.config import HostConfig, HostsConfig
from hermclaw.infratools.containers import (
    ContainerInventory,
    health_from_status,
    parse_docker_labels,
    parse_docker_ports,
    parse_docker_ps,
    parse_inspect,
    parse_podman_ps,
)
from hermclaw.infratools.errors import (
    INFRA_CONTAINER_NOT_FOUND,
    INFRA_ENGINE_UNAVAILABLE,
    INFRA_HOST_UNKNOWN,
    INFRA_INVALID_ARGUMENT,
    INFRA_OUTPUT_INVALID,
    INFRA_REMOTE_UNAVAILABLE,
    InfraToolError,
)
from hermclaw.infratools.process import HostExecutor

DOCKER_PS = "\n".join(
    json.dumps(item)
    for item in (
        {
            "Command": '"/docker-entrypoint.…"',
            "CreatedAt": "2026-10-14 10:00:00 +0000 UTC",
            "ID": "abc123",
            "Image": "nginx:1.28",
            "Labels": "com.docker.compose.project=web,com.docker.compose.service=proxy,note=a,b",
            "Names": "web-proxy-1",
            "Networks": "web_default,bridge",
            "Ports": "0.0.0.0:8080->80/tcp, [::]:8080->80/tcp, 127.0.0.1:7000-7001->7000-7001/udp, 9000/tcp",
            "State": "running",
            "Status": "Up 3 minutes (healthy)",
        },
        {
            "ID": "def456",
            "Image": "alpine:3.20",
            "Labels": "",
            "Names": "job",
            "Ports": "",
            "State": "",
            "Status": "Exited (1) 2 seconds ago",
        },
    )
)

PODMAN_PS = json.dumps(
    [
        {
            "Id": "f00",
            "Image": "docker.io/library/nginx:1.28",
            "Names": ["sbx_web_1"],
            "Ports": [{"host_ip": "127.0.0.1", "container_port": 80, "host_port": 41000, "range": 2, "protocol": "tcp"}],
            "State": "running",
            "Status": "Up 5 seconds (starting)",
            "Labels": {"hermclaw.sandbox": "x1", "api_token": "s3cr3t-value-123"},
            "Networks": ["sbx_default"],
            "Restarts": 0,
        },
        {"Id": "f01", "Image": "alpine", "Names": ["sbx_job_1"], "State": "exited", "Status": "Exited (2) 1 second ago", "ExitCode": 2},
    ]
)


def _inspect_doc(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "Id": "f00",
        "Name": "/sbx_web_1",
        "Created": "2026-10-14T10:00:00Z",
        "RestartCount": 1,
        "State": {"Status": "running", "ExitCode": 0, "StartedAt": "2026-10-14T10:00:01Z", "Health": {"Status": "healthy"}},
        "Config": {
            "Image": "docker.io/library/nginx:1.28",
            "Cmd": ["nginx", "-g", "daemon off;"],
            "Labels": {"com.docker.compose.service": "web", "DB_PASSWORD": "hunter2hunter2"},
            "Env": ["PATH=/usr/bin", "DB_PASSWORD=hunter2hunter2", "Passwd=true"],
        },
        "HostConfig": {"Privileged": False, "NetworkMode": "bridge", "RestartPolicy": {"Name": "no"}},
        "NetworkSettings": {
            "Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "41000"}], "443/tcp": None},
            "Networks": {"sbx_default": {}},
        },
        "Mounts": [{"Type": "bind", "Source": "/srv/data", "Destination": "/data", "RW": False}],
    }
    doc.update(over)
    return doc


class FakeRunner:
    """Remote runner double: answers by the first words of the command line."""

    def __init__(self, answers: dict[str, tuple[int, str, str]]) -> None:
        self.answers = answers
        self.commands: list[str] = []

    async def run(self, host_id: str, command: str, timeout: float) -> tuple[int, str, str]:
        self.commands.append(command)
        for prefix, answer in self.answers.items():
            if command.startswith(prefix):
                return answer
        return 127, "", "sh: command not found"


HOSTS = HostsConfig(hosts=[HostConfig(id="web-223", address="192.168.178.223", role="webui")])


def test_docker_ports_parsing_expands_ranges_and_ipv6() -> None:
    ports = parse_docker_ports("0.0.0.0:8080->80/tcp, :::8080->80/tcp, [::1]:5432->5432/tcp, 127.0.0.1:7000-7001->7000-7001/udp, 9000/tcp")
    rendered = [p.render() for p in ports]
    assert "0.0.0.0:8080->80/tcp" in rendered
    assert "::1:5432->5432/tcp" in rendered
    assert {p.host_port for p in ports if p.protocol == "udp"} == {7000, 7001}
    unpublished = [p for p in ports if p.host_port is None]
    assert len(unpublished) == 1 and unpublished[0].container_port == 9000
    assert any(p.host_ip == "::" and p.host_port == 8080 for p in ports)


def test_docker_labels_keep_commas_in_values() -> None:
    assert parse_docker_labels("a=1,b=x,y,c=") == {"a": "1", "b": "x,y", "c": ""}
    assert parse_docker_labels({"k": 1}) == {"k": "1"}


@pytest.mark.parametrize(
    ("status", "health"),
    [
        ("Up 3 minutes (healthy)", "healthy"),
        ("Up 1 second (health: starting)", "starting"),
        ("Up 2 hours (unhealthy)", "unhealthy"),
        ("Up 5 s", None),
    ],
)
def test_health_from_status(status: str, health: str | None) -> None:
    assert health_from_status(status) == health


def test_parse_docker_ps_normalises() -> None:
    items = parse_docker_ps(DOCKER_PS, "local")
    web, job = items
    assert web.engine == "docker" and web.name == "web-proxy-1" and web.state == "running" and web.health == "healthy"
    assert web.labels["note"] == "a,b" and web.networks == ["web_default", "bridge"]
    assert {(p.host_ip, p.host_port) for p in web.ports if p.host_port} >= {("0.0.0.0", 8080), ("::", 8080)}
    assert job.state == "exited"  # derived from Status when State is empty


def test_parse_podman_ps_normalises() -> None:
    web, job = parse_podman_ps(PODMAN_PS, "local")
    assert web.name == "sbx_web_1" and web.health == "starting"
    assert [(p.host_port, p.container_port) for p in web.ports] == [(41000, 80), (41001, 81)]
    assert job.exit_code == 2 and job.state == "exited"
    assert parse_podman_ps("null", "local") == []


@pytest.mark.parametrize("bad", ["{", '{"a": 1}'])
def test_parse_podman_ps_rejects_invalid(bad: str) -> None:
    with pytest.raises(InfraToolError) as err:
        parse_podman_ps(bad, "local")
    assert err.value.code == INFRA_OUTPUT_INVALID


def test_parse_inspect_details_and_env_values_never_returned() -> None:
    (info,) = parse_inspect(json.dumps([_inspect_doc()]), "podman", "local")
    assert info.name == "sbx_web_1" and info.state == "running" and info.health == "healthy"
    assert info.command == "nginx -g daemon off;" and info.restart_count == 1 and info.privileged is False
    assert info.mounts[0].read_only and info.mounts[0].source == "/srv/data"
    assert info.env_keys == ["DB_PASSWORD", "PATH", "Passwd"]
    assert "hunter2" not in info.model_dump_json().replace(info.labels.get("DB_PASSWORD", ""), "")
    assert any(p.host_port == 41000 for p in info.ports) and any(p.container_port == 443 and p.host_port is None for p in info.ports)
    # podman < 4 calls it Healthcheck; "none" means no healthcheck
    (old,) = parse_inspect(json.dumps([_inspect_doc(State={"Status": "running", "Healthcheck": {"Status": "none"}})]), "podman", "h")
    assert old.health is None


@pytest.mark.parametrize("name", ["a;rm -rf /", "-rf", "x y", "", "$(id)", "a" * 300])
def test_inspect_argv_rejects_injection(name: str) -> None:
    with pytest.raises(InfraToolError) as err:
        ContainerInventory.inspect_argv("docker", [name])
    assert err.value.code == INFRA_INVALID_ARGUMENT


@pytest.mark.parametrize("flt", ["label=a b", "foo=bar", "label=$(id)", "label=x;y"])
def test_ps_argv_rejects_bad_filters(flt: str) -> None:
    with pytest.raises(InfraToolError):
        ContainerInventory.ps_argv("podman", filters=[flt])


def test_ps_argv_formats() -> None:
    assert ContainerInventory.ps_argv("podman", all_containers=True) == ["podman", "ps", "-a", "--format", "json"]
    assert ContainerInventory.ps_argv("docker", filters=["label=hermclaw.sandbox=1"]) == [
        "docker",
        "ps",
        "--filter",
        "label=hermclaw.sandbox=1",
        "--no-trunc",
        "--format",
        "{{json .}}",
    ]


async def test_list_remote_merges_engines_and_reports_unavailable() -> None:
    runner = FakeRunner(
        {
            "podman ps": (0, PODMAN_PS, ""),
            "docker ps": (1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"),
        }
    )
    inv = ContainerInventory(HostExecutor(remote=runner, hosts=HOSTS))
    res = await inv.list_containers(host="web-223", all_containers=True)
    assert res.ok and res.host == "web-223"
    assert [c.name for c in res.containers] == ["sbx_job_1", "sbx_web_1"]
    docker = next(s for s in res.engines if s.engine == "docker")
    assert not docker.available and "daemon" in (docker.error or "")
    # labels with secret-looking keys are redacted
    web = next(c for c in res.containers if c.name == "sbx_web_1")
    assert "s3cr3t" not in web.labels["api_token"]
    assert runner.commands[0] == "podman ps -a --format json"
    assert runner.commands[1] == "docker ps -a --no-trunc --format '{{json .}}'"


async def test_list_fails_when_no_engine_usable() -> None:
    runner = FakeRunner({})
    res = await ContainerInventory(HostExecutor(remote=runner, hosts=HOSTS)).list_containers(host="web-223")
    assert not res.ok and res.code == INFRA_ENGINE_UNAVAILABLE


async def test_list_reports_invalid_json() -> None:
    runner = FakeRunner({"podman ps": (0, "not json", "")})
    res = await ContainerInventory(HostExecutor(remote=runner, hosts=HOSTS)).list_containers(host="web-223", engine="podman")
    assert not res.ok and res.code == INFRA_OUTPUT_INVALID


async def test_inspect_secret_like_json_keys_do_not_break_parsing() -> None:
    """Regression: redacting the raw JSON (``"Passwd": true`` → ``***REDACTED***``) made inspect output unparsable."""
    doc = _inspect_doc()
    doc["Config"]["Passwd"] = True
    doc["Config"]["Labels"]["token"] = "abcdefabcdef"
    runner = FakeRunner({"podman inspect": (0, json.dumps([doc]), ""), "docker inspect": (1, "", "Error: No such object: x")})
    inv = ContainerInventory(HostExecutor(remote=runner, hosts=HOSTS))
    info = await inv.inspect("sbx_web_1", host="web-223")
    assert info.engine == "podman" and info.labels["token"] != "abcdefabcdef"


async def test_inspect_not_found_and_unknown_host() -> None:
    runner = FakeRunner(
        {"podman inspect": (125, "[]", "Error: no such container nope"), "docker inspect": (1, "[]", "Error: No such object: nope")}
    )
    inv = ContainerInventory(HostExecutor(remote=runner, hosts=HOSTS))
    with pytest.raises(InfraToolError) as err:
        await inv.inspect("nope", host="web-223")
    assert err.value.code == INFRA_CONTAINER_NOT_FOUND
    with pytest.raises(InfraToolError) as err:
        await inv.inspect("nope", host="mystery")
    assert err.value.code == INFRA_HOST_UNKNOWN
    with pytest.raises(InfraToolError) as err:
        await ContainerInventory(HostExecutor(hosts=HOSTS)).list_containers(host="web-223")
    assert err.value.code == INFRA_REMOTE_UNAVAILABLE


async def test_remote_inspect_survives_transport_redaction() -> None:
    """Regression: the SSH runner redacts its output; ``"Passwd": true`` turned into invalid JSON on remote hosts."""
    from hermclaw.core.redaction import DEFAULT_REDACTOR
    from hermclaw.infratools.containers import loads_lenient

    doc = _inspect_doc()
    doc["Config"]["Passwd"] = True
    doc["Config"]["api_key"] = 123456
    redacted = DEFAULT_REDACTOR.text(json.dumps([doc], indent=2))
    with pytest.raises(json.JSONDecodeError):
        json.loads(redacted)
    assert loads_lenient(redacted)[0]["Config"]["Passwd"] == "***REDACTED***"
    runner = FakeRunner({"podman inspect": (0, redacted, "")})
    info = await ContainerInventory(HostExecutor(remote=runner, hosts=HOSTS)).inspect("sbx_web_1", host="web-223", engine="podman")
    assert info.name == "sbx_web_1" and info.health == "healthy"
    with pytest.raises(json.JSONDecodeError):
        loads_lenient("{broken")
