"""P28.3 – the sandbox compose model (unique project, remapped loopback ports, no host state) and sandbox refusals."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from hermclaw.infratools.compose import SANDBOX_LABEL, load_compose
from hermclaw.infratools.compose_sandbox import (
    PROJECT_PREFIX,
    LocalPortAllocator,
    RemotePortAllocator,
    build_sandbox_model,
    check_bind_sources,
    check_sandbox_rules,
    resolve_extends,
)
from hermclaw.infratools.errors import INFRA_COMPOSE_REFUSED, InfraToolError


class CountingAllocator:
    def __init__(self, start: int = 41000) -> None:
        self.next = start
        self.calls: list[str] = []

    def __call__(self, protocol: str) -> int:
        self.calls.append(protocol)
        self.next += 1
        return self.next


def _write(tmp_path: Path, text: str, files: dict[str, str] | None = None) -> Path:
    for rel, content in (files or {}).items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content)
    path = tmp_path / "compose.yaml"
    path.write_text(text)
    return path


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    text = """
name: original-name
x-common: &common
  restart: unless-stopped
services:
  web:
    <<: *common
    image: docker.io/library/nginx:1.28
    container_name: fixed-name
    ports:
      - "8080:80"
      - "127.0.0.1:8443:443"
      - "9000-9001:9000-9001/udp"
      - target: 5000
        published: 5000
      - "6000"
    labels:
      - "keep=me"
  worker:
    extends: {file: base.yaml, service: base}
    deploy: {replicas: 2}
  debug:
    image: alpine:3.20
    profiles: [debug]
volumes:
  data: {}
"""
    base = "services:\n  base:\n    image: alpine:3.20\n    command: ['sleep', '60']\n    ports: ['7000:7000']\n"
    yield _write(tmp_path, text, {"base.yaml": base})


def test_build_sandbox_model_rewrites_ports_names_and_labels(project: Path) -> None:
    loaded = load_compose(project)
    alloc = CountingAllocator()
    sbx = build_sandbox_model(loaded, "abc123", alloc)
    model = sbx.model
    assert model["name"] == PROJECT_PREFIX + "abc123"
    web = model["services"]["web"]
    assert "container_name" not in web and web["restart"] == "no"
    assert web["labels"] == {"keep": "me", SANDBOX_LABEL: "abc123"}
    # every published/ephemeral port is bound to a fresh loopback port, original host ports are gone
    assert all(p.startswith("127.0.0.1:") for p in web["ports"])
    assert not any(":8080:" in p or ":8443:" in p for p in web["ports"])
    assert web["ports"] == [
        "127.0.0.1:41001:80/tcp",
        "127.0.0.1:41002:443/tcp",
        "127.0.0.1:41003:9000/udp",
        "127.0.0.1:41004:9001/udp",
        "127.0.0.1:41005:5000/tcp",
        "127.0.0.1:41006:6000/tcp",
    ]
    assert alloc.calls[:6] == ["tcp", "tcp", "udp", "udp", "tcp", "tcp"]
    assert [(p.service, p.container_port, p.host_port) for p in sbx.ports][:2] == [("web", "80", 41001), ("web", "443", 41002)]
    # extends from a file in the same directory is inlined (and its ports remapped as well)
    worker = model["services"]["worker"]
    assert worker["image"] == "alpine:3.20" and worker["ports"] == ["127.0.0.1:41007:7000/tcp"] and "extends" not in worker
    assert sbx.expected == {"web": 1, "worker": 2}  # profile services are not expected to start
    assert any("container_name" in n for n in sbx.notes) and any("unless-stopped" in n for n in sbx.notes)
    assert model["volumes"] == {"data": {}} and "x-common" in model
    # the generated file round-trips through a YAML 1.1 parser (podman-compose) unchanged
    dumped = yaml.safe_load(yaml.safe_dump(model, sort_keys=False))
    assert dumped["services"]["web"]["ports"] == web["ports"] and dumped["services"]["web"]["restart"] == "no"


@pytest.mark.parametrize(
    "text",
    [
        "include: [other.yaml]\nservices:\n  a: {image: alpine:3.20}\n",
        "services:\n  a: {image: alpine:3.20, networks: [ext]}\nnetworks:\n  ext: {external: true}\n",
        "services:\n  a: {image: alpine:3.20, volumes: ['v:/v']}\nvolumes:\n  v: {external: true}\n",
        "services:\n  a: {image: alpine:3.20, network_mode: 'container:other'}\n",
        "services:\n  a: {image: alpine:3.20, network_mode: host}\n",
    ],
)
def test_build_sandbox_model_refuses_host_state(tmp_path: Path, text: str) -> None:
    loaded = load_compose(_write(tmp_path, text, {"other.yaml": "services: {}\n"}))
    with pytest.raises(InfraToolError) as err:
        build_sandbox_model(loaded, "x", CountingAllocator())
    assert err.value.code == INFRA_COMPOSE_REFUSED


@pytest.mark.parametrize(
    ("text", "files"),
    [
        ("services:\n  a: {image: alpine:3.20, env_file: /etc/hermclaw/secrets.env}\n", {}),
        ("services:\n  a: {image: alpine:3.20, env_file: ../outside.env}\n", {}),
        ("services:\n  a: {image: alpine:3.20, env_file: [{path: /root/.env, required: false}]}\n", {}),
        ("services:\n  a: {build: {context: /srv/other}}\n", {}),
        ("services:\n  a: {build: {context: ., dockerfile: ../../Dockerfile}}\n", {"Dockerfile": "FROM alpine\n"}),
        ("services:\n  a: {build: 'https://github.com/x/y.git'}\n", {}),
        ("services:\n  a: {image: alpine:3.20, devices: ['/dev/kvm']}\n", {}),
        ("services:\n  a: {image: alpine:3.20, cap_add: [NET_ADMIN]}\n", {}),
        ("services:\n  a: {image: alpine:3.20, volumes_from: ['container:db']}\n", {}),
        ("services:\n  a: {image: alpine:3.20, external_links: ['db:db']}\n", {}),
        ("services:\n  a: {image: alpine:3.20, ipc: 'container:x'}\n", {}),
        ("services:\n  a: {image: alpine:3.20}\nsecrets:\n  s: {file: /etc/shadow}\n", {}),
        ("services:\n  a: {image: alpine:3.20}\nsecrets:\n  s: {environment: HOME}\n", {}),
    ],
)
def test_sandbox_rules_refuse_reads_outside_the_project(tmp_path: Path, text: str, files: dict[str, str]) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    loaded = load_compose(_write(project, text, files))
    with pytest.raises(InfraToolError) as err:
        check_sandbox_rules(loaded, resolve_extends(loaded))
    assert err.value.code == INFRA_COMPOSE_REFUSED


def test_sandbox_rules_accept_project_local_files(tmp_path: Path) -> None:
    text = (
        "services:\n  a:\n    build: {context: ., dockerfile: Dockerfile}\n    env_file: [app.env]\n    volumes_from: [b]\n"
        "  b: {image: alpine:3.20}\nsecrets:\n  s: {file: ./secret.txt}\n"
    )
    loaded = load_compose(_write(tmp_path, text, {"Dockerfile": "FROM alpine:3.20\n", "app.env": "A=1\n", "secret.txt": "x"}))
    services = resolve_extends(loaded)
    check_sandbox_rules(loaded, services)
    check_bind_sources(loaded, services)


@pytest.mark.parametrize(
    "volume",
    ["/srv/data:/data", "~/x:/x", "${HOME}:/h", "../up:/up", {"type": "bind", "source": "/etc", "target": "/e"}],
)
def test_bind_sources_must_stay_inside_project(tmp_path: Path, volume: object) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    loaded = load_compose(_write(project, yaml.safe_dump({"services": {"a": {"image": "alpine:3.20", "volumes": [volume]}}})))
    with pytest.raises(InfraToolError) as err:
        check_bind_sources(loaded, resolve_extends(loaded))
    assert err.value.code == INFRA_COMPOSE_REFUSED


def test_bind_source_symlink_escape_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    os.symlink("/etc", project / "link")
    loaded = load_compose(_write(project, "services:\n  a: {image: alpine:3.20, volumes: ['./link:/data']}\n"))
    with pytest.raises(InfraToolError, match="resolves outside"):
        check_bind_sources(loaded, resolve_extends(loaded))


def test_extends_cycle_and_foreign_directory(tmp_path: Path) -> None:
    loaded = load_compose(_write(tmp_path, "services:\n  a: {extends: b}\n  b: {extends: a}\n"))
    with pytest.raises(InfraToolError, match="cycle"):
        resolve_extends(loaded)
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "base.yaml").write_text("services:\n  base: {image: alpine:3.20}\n")
    loaded = load_compose(_write(sub, "services:\n  a: {extends: {file: ../base.yaml, service: base}}\n"))
    with pytest.raises(InfraToolError) as err:
        resolve_extends(loaded)
    assert err.value.code == INFRA_COMPOSE_REFUSED


def test_local_allocator_hands_out_distinct_free_ports() -> None:
    first, second = LocalPortAllocator(), LocalPortAllocator()
    ports = {first("tcp") for _ in range(5)} | {second("tcp") for _ in range(5)}
    assert len(ports) == 10 and all(1024 < p < 65536 for p in ports)
    first.release()
    second.release()
    assert not first.mine and not second.mine


def test_remote_allocator_avoids_used_ports() -> None:
    used = set(range(20000, 31990))
    alloc = RemotePortAllocator(used)
    picked = {alloc("tcp") for _ in range(5)}
    assert picked.isdisjoint(used) and len(picked) == 5
    for _ in range(5):
        alloc("tcp")
    with pytest.raises(InfraToolError):
        alloc("tcp")
