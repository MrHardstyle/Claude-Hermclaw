"""P28.2 – compose structural validation (PyYAML, YAML 1.2 semantics, security rules) without a container engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermclaw.infratools.compose import (
    ComposePolicy,
    bind_addresses_overlap,
    load_compose,
    load_compose_text,
    parse_port_entry,
    parse_volume_entry,
    service_ports,
    validate_structure,
)
from hermclaw.infratools.errors import INFRA_COMPOSE_INVALID, INFRA_PATH_INVALID, InfraToolError, has_errors

GOOD = """
name: shop
services:
  web:
    image: docker.io/library/nginx:1.28
    ports:
      - "8080:80"
      - "127.0.0.1:8443:443/tcp"
    volumes:
      - ./site:/usr/share/nginx/html:ro
      - cache:/var/cache/nginx
    env_file: [web.env]
    depends_on:
      api: {condition: service_healthy}
    networks: [front]
    healthcheck:
      test: ["CMD", "nginx", "-t"]
  api:
    build: ./api
    environment:
      DB_PASSWORD: ${DB_PASSWORD}
      DEBUG: yes
    networks: [front]
volumes:
  cache: {}
networks:
  front: {}
"""


def _project(tmp_path: Path, text: str, files: dict[str, str] | None = None) -> Path:
    for rel, content in (files or {}).items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    path = tmp_path / "compose.yaml"
    path.write_text(text)
    return path


def _codes(findings: list) -> dict[str, str]:  # type: ignore[type-arg]
    return {f.code: f.severity for f in findings}


def test_good_project_has_no_errors(tmp_path: Path) -> None:
    path = _project(tmp_path, GOOD, {"site/index.html": "<h1>hi</h1>", "web.env": "A=1\n", "api/Dockerfile": "FROM alpine:3.20\n"})
    loaded = load_compose(path)
    findings = validate_structure(loaded)
    assert not has_errors(findings), [f.render() for f in findings]
    # YAML 1.2 core schema: "yes" stays a string (Compose semantics), ${VAR} values are not literal secrets
    assert loaded.services["api"]["environment"]["DEBUG"] == "yes"
    assert "COMPOSE_LITERAL_SECRET" not in _codes(findings)


def test_unquoted_sexagesimal_port_is_flagged_but_parsed_as_string(tmp_path: Path) -> None:
    loaded = load_compose_text("services:\n  ssh:\n    image: alpine:3.20\n    ports:\n      - 22:22\n", path=tmp_path / "c.yaml")
    assert loaded.services["ssh"]["ports"] == ["22:22"]
    finding = next(f for f in loaded.findings if f.code == "COMPOSE_PORT_UNQUOTED")
    assert finding.line == 5


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("services:\n  a:\n    image: alpine:3.20\n    privileged: true\n", "COMPOSE_PRIVILEGED"),
        ("services:\n  a:\n    image: alpine:3.20\n    network_mode: host\n", "COMPOSE_HOST_NETWORK"),
        ("services:\n  a:\n    image: alpine:3.20\n    pid: host\n", "COMPOSE_HOST_NAMESPACE"),
        ("services:\n  a:\n    image: alpine:3.20\n    volumes: ['/var/run/docker.sock:/var/run/docker.sock']\n", "COMPOSE_DOCKER_SOCKET"),
        ("services:\n  a:\n    image: alpine:3.20\n    volumes: ['/run/podman/podman.sock:/s']\n", "COMPOSE_DOCKER_SOCKET"),
        (
            "services:\n  a:\n    image: alpine:3.20\n    volumes: [{type: bind, source: /var/run/docker.sock, target: /s}]\n",
            "COMPOSE_DOCKER_SOCKET",
        ),
        ("services:\n  a:\n    image: alpine:3.20\n    volumes: ['/etc:/host-etc:ro']\n", "COMPOSE_SENSITIVE_MOUNT"),
        ("services:\n  a:\n    image: alpine:3.20\n    cap_add: [SYS_ADMIN]\n", "COMPOSE_CAPABILITY"),
        ("services:\n  a:\n    image: alpine:3.20\n    security_opt: ['seccomp=unconfined']\n", "COMPOSE_SECURITY_OPT"),
        ("services:\n  a: {}\n", "COMPOSE_NO_IMAGE"),
        ("services: {}\n", "COMPOSE_NO_SERVICES"),
        ("services:\n  a:\n    image: 'bad image'\n", "COMPOSE_IMAGE_INVALID"),
        ("services:\n  a:\n    image: alpine:3.20\n    ports: ['abc:80']\n", "COMPOSE_PORT_INVALID"),
        ("services:\n  a:\n    image: alpine:3.20\n    ports: ['70000:80']\n", "COMPOSE_PORT_INVALID"),
        ("services:\n  a:\n    image: alpine:3.20\n    ports: ['80/xyz']\n", "COMPOSE_PORT_INVALID"),
        ("services:\n  a:\n    image: alpine:3.20\n    ports: ['8000-8002:80-81']\n", "COMPOSE_PORT_INVALID"),
        ("services:\n  a:\n    image: alpine:3.20\n    env_file: missing.env\n", "COMPOSE_ENV_FILE_MISSING"),
        ("services:\n  a:\n    image: alpine:3.20\n    depends_on: [b]\n", "COMPOSE_DEPENDS_UNKNOWN"),
        ("services:\n  a:\n    image: alpine:3.20\n    networks: [nope]\n", "COMPOSE_NETWORK_UNDECLARED"),
        ("services:\n  a:\n    image: alpine:3.20\n    volumes: ['data:/data']\n", "COMPOSE_VOLUME_UNDECLARED"),
        ("services:\n  a:\n    image: alpine:3.20\n    volumes: ['./x:relative']\n", "COMPOSE_VOLUME_TARGET"),
        ("services:\n  a:\n    build: ./nothing-here\n", "COMPOSE_BUILD_CONTEXT_MISSING"),
        (
            "services:\n  a:\n    image: alpine:3.20\n    ports: ['8080:80']\n  b:\n    image: alpine:3.20\n    ports: ['8080:81']\n",
            "COMPOSE_PORT_DUPLICATE",
        ),
        (
            "services:\n  a:\n    image: alpine:3.20\n    container_name: x\n  b:\n    image: alpine:3.20\n    container_name: x\n",
            "COMPOSE_CONTAINER_NAME_DUPLICATE",
        ),
        ("secrets:\n  s: {file: ./missing.txt}\nservices:\n  a:\n    image: alpine:3.20\n", "COMPOSE_FILE_MISSING"),
    ],
)
def test_bad_fixtures_are_errors(tmp_path: Path, text: str, code: str) -> None:
    findings = validate_structure(load_compose(_project(tmp_path, text)))
    assert _codes(findings).get(code) == "error", [f.render() for f in findings]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("services:\n  a:\n    image: alpine\n", "COMPOSE_IMAGE_UNPINNED"),
        ("services:\n  a:\n    image: alpine:3.20\n    environment: ['API_TOKEN=abcdef123456']\n", "COMPOSE_LITERAL_SECRET"),
        ("services:\n  a:\n    image: alpine:3.20\n    volumes: ['/srv/data:/data']\n", "COMPOSE_ABSOLUTE_BIND"),
        ("services:\n  a:\n    image: alpine:3.20\n    volumes: ['../outside:/data']\n", "COMPOSE_BIND_OUTSIDE"),
        ("services:\n  a:\n    image: alpine:3.20\n    cap_add: [NET_ADMIN]\n", "COMPOSE_CAPABILITY"),
        ("version: '3.8'\nservices:\n  a:\n    image: alpine:3.20\n", "COMPOSE_VERSION_OBSOLETE"),
        ("bogus: 1\nservices:\n  a:\n    image: alpine:3.20\n", "COMPOSE_UNKNOWN_KEY"),
        ("services:\n  a:\n    image: alpine:3.20\n    ports: ['80:80']\n", "COMPOSE_PRIVILEGED_PORT"),
    ],
)
def test_non_blocking_findings(tmp_path: Path, text: str, code: str) -> None:
    findings = validate_structure(load_compose(_project(tmp_path, text)))
    assert _codes(findings).get(code) in ("warning", "info"), [f.render() for f in findings]
    assert not has_errors(findings)


def test_policy_flags_allow_explicitly(tmp_path: Path) -> None:
    text = (
        "services:\n  a:\n    image: alpine:3.20\n    privileged: true\n    network_mode: host\n"
        "    volumes: ['/var/run/docker.sock:/var/run/docker.sock', '/etc:/etc2']\n"
    )
    loaded = load_compose(_project(tmp_path, text))
    strict = _codes(validate_structure(loaded))
    assert {"COMPOSE_PRIVILEGED", "COMPOSE_HOST_NETWORK", "COMPOSE_DOCKER_SOCKET", "COMPOSE_SENSITIVE_MOUNT"} <= set(strict)
    relaxed = validate_structure(
        loaded, ComposePolicy(allow_privileged=True, allow_host_network=True, allow_docker_socket=True, allow_sensitive_mounts=True)
    )
    assert not has_errors(relaxed), [f.render() for f in relaxed]


def test_absolute_bind_can_be_made_an_error(tmp_path: Path) -> None:
    loaded = load_compose(_project(tmp_path, "services:\n  a:\n    image: alpine:3.20\n    volumes: ['/srv/data:/data']\n"))
    assert _codes(validate_structure(loaded, ComposePolicy(allow_absolute_binds=False)))["COMPOSE_ABSOLUTE_BIND"] == "error"


def test_ipv4_and_ipv6_wildcards_may_coexist(tmp_path: Path) -> None:
    text = "services:\n  a:\n    image: alpine:3.20\n    ports: ['0.0.0.0:8080:80', '[::]:8080:80']\n"
    assert "COMPOSE_PORT_DUPLICATE" not in _codes(validate_structure(load_compose(_project(tmp_path, text))))
    text = "services:\n  a:\n    image: alpine:3.20\n    ports: ['8080:80', '127.0.0.1:8080:81']\n"
    assert "COMPOSE_PORT_DUPLICATE" in _codes(validate_structure(load_compose(_project(tmp_path, text))))


def test_bind_addresses_overlap() -> None:
    assert bind_addresses_overlap(None, "127.0.0.1")
    assert bind_addresses_overlap("0.0.0.0", "10.0.0.1")
    assert not bind_addresses_overlap("0.0.0.0", "::")
    assert bind_addresses_overlap("::", "[::1]")
    assert not bind_addresses_overlap("127.0.0.1", "10.0.0.1")
    assert bind_addresses_overlap("127.0.0.1", "127.0.0.1")


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("80", [(None, "80", None, "tcp")]),
        ("8080:80", [(8080, "80", None, "tcp")]),
        ("127.0.0.1:5432:5432", [(5432, "5432", "127.0.0.1", "tcp")]),
        ("[::1]:53:53/udp", [(53, "53", "::1", "udp")]),
        ("7000-7001:7000-7001", [(7000, "7000", None, "tcp"), (7001, "7001", None, "tcp")]),
        (8443, [(None, "8443", None, "tcp")]),
        ({"target": 80, "published": "8081", "host_ip": "127.0.0.1", "protocol": "tcp"}, [(8081, "80", "127.0.0.1", "tcp")]),
        ("${WEB_PORT:-8080}:80", [(None, "80", None, "tcp")]),
    ],
)
def test_parse_port_entry(entry: object, expected: list[tuple[object, ...]]) -> None:
    ports, problems = parse_port_entry("svc", entry)
    assert not problems
    assert [(p.published, p.target, p.host_ip, p.protocol) for p in ports] == expected


def test_host_range_for_single_container_port() -> None:
    ports, problems = parse_port_entry("svc", "9000-9002:80")
    assert not problems and len(ports) == 3 and all(p.published_range for p in ports)


def test_service_ports_reports_non_list() -> None:
    _ports, findings = service_ports({"a": {"ports": "8080:80"}})
    assert findings and findings[0].code == "COMPOSE_PORT_INVALID"


def test_parse_volume_entry() -> None:
    vol, problem = parse_volume_entry("a", "./data:/data:ro,z")
    assert problem is None and vol is not None and vol.type == "bind" and vol.read_only
    vol, _ = parse_volume_entry("a", "named:/data")
    assert vol is not None and vol.type == "volume"
    _, problem = parse_volume_entry("a", "a:b:c:d")
    assert problem


def test_loader_rejects_non_mapping_alias_bomb_and_large_files(tmp_path: Path) -> None:
    with pytest.raises(InfraToolError) as err:
        load_compose_text("- a\n- b\n", path=tmp_path / "c.yaml")
    assert err.value.code == INFRA_COMPOSE_INVALID
    # billion laughs: few aliases, exponential expansion (9 levels x 9 aliases = 81 aliases, 9^9 nodes)
    levels = ["x0: &x0 [lol, lol, lol, lol, lol, lol, lol, lol, lol]"]
    levels += [f"x{i}: &x{i} [" + ", ".join([f"*x{i - 1}"] * 9) + "]" for i in range(1, 10)]
    bomb = "\n".join(levels) + "\nservices: {a: {image: alpine, labels: *x9}}\n"
    with pytest.raises(InfraToolError) as err:
        load_compose_text(bomb, path=tmp_path / "c.yaml")
    assert "alias bomb" in err.value.message
    many = "x: &x 1\nservices: {a: {image: alpine, labels: [" + ", ".join(["*x"] * 201) + "]}}\n"
    with pytest.raises(InfraToolError) as err:
        load_compose_text(many, path=tmp_path / "c.yaml")
    assert "aliases" in err.value.message
    with pytest.raises(InfraToolError):
        load_compose_text("services: {a: {image: x}}\n" + "#" * 1_000_001, path=tmp_path / "c.yaml")
    with pytest.raises(InfraToolError) as err:
        load_compose(tmp_path / "nope.yaml")
    assert err.value.code == INFRA_PATH_INVALID
    with pytest.raises(InfraToolError) as err:
        load_compose_text("services: [unclosed\n", path=tmp_path / "c.yaml")
    assert err.value.code == INFRA_COMPOSE_INVALID


def test_reset_and_override_tags_are_accepted(tmp_path: Path) -> None:
    loaded = load_compose_text(
        "services:\n  a:\n    image: alpine:3.20\n    ports: !reset []\n    environment: !override {A: '1'}\n", path=tmp_path / "c.yaml"
    )
    assert loaded.services["a"]["ports"] == [] and loaded.services["a"]["environment"] == {"A": "1"}


async def test_remote_includes_are_never_fetched(tmp_path: Path) -> None:
    from hermclaw.infratools.compose import ComposeEngines, ComposeValidator
    from hermclaw.infratools.process import HostExecutor

    text = "include:\n  - oci://registry.example/stack:1\n  - path: [git@github.com:x/y.git]\nservices:\n  a: {image: alpine:3.20}\n"
    executor = HostExecutor(env={"PATH": "/nonexistent"})  # an engine call would be visible as 'no compose implementation'
    report = await ComposeValidator(executor, ComposeEngines(executor)).validate(_project(tmp_path, text))
    assert report.engine_check.status == "skipped" and "SSRF" in (report.engine_check.reason or "")
    assert [f.code for f in report.findings if f.severity == "error"] == ["COMPOSE_REMOTE_REFERENCE", "COMPOSE_REMOTE_REFERENCE"]
