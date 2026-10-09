"""P18 sandbox: exact engine command line, policy checks and helpers (no container engine needed)."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from hermclaw.contracts.worker import CommandRequest
from hermclaw.core.config import SandboxPolicy
from hermclaw.core.errors import ConfigError, PolicyViolation
from worker.common.settings import DEFAULT_CONTAINER_LABEL
from worker.execution import sandbox as sb
from worker.execution.sandbox import (
    DockerSandbox,
    LocalSandbox,
    OutputCapture,
    PodmanSandbox,
    SandboxRunner,
    container_name_for,
    default_max_output_bytes,
    label_value,
    make_sandbox,
    parse_docker_info,
    parse_podman_info,
    parse_size,
    resolve_image,
    validate_request_env,
)

POLICY = SandboxPolicy(
    engine="podman",
    image="docker.io/library/python:3.12-slim",
    images={"node": "docker.io/library/node:22-bookworm-slim"},
    cpus=2.0,
    memory="2g",
    pids_limit=256,
    tmpfs_size="128m",
    default_timeout_seconds=300,
    env_allowlist=["CI", "LANG", "APP_*"],
)


def req(**kw: Any) -> CommandRequest:
    base: dict[str, Any] = {"request_id": "req-1", "job_id": "job-1", "step_id": "step-1", "workspace": "ws1", "command": "pytest -q"}
    base.update(kw)
    return CommandRequest(**base)


def flag(argv: list[str], name: str) -> list[str]:
    return [a.split("=", 1)[1] for a in argv if a.startswith(name + "=")]


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws1"
    d.mkdir()
    return d


def test_podman_argv_has_every_isolation_flag(ws: Path) -> None:
    s = PodmanSandbox(POLICY, max_output_bytes=10_000)
    inv = s.build_invocation(req(), ws)
    argv = inv.argv
    assert argv[:3] == ["podman", "run", "--rm"]
    assert flag(argv, "--name") == ["hermclaw-req-1"] == [inv.container_name]
    assert flag(argv, "--label") == [DEFAULT_CONTAINER_LABEL, "hermclaw.request=req-1", "hermclaw.job=job-1", "hermclaw.step=step-1"]
    assert flag(argv, "--network") == ["none"] and inv.network is False
    assert "--read-only" in argv
    assert flag(argv, "--tmpfs") == ["/tmp:rw,nosuid,nodev,size=128m,mode=1777", "/dev/shm:rw,nosuid,nodev,noexec,size=64m,mode=1777"]
    assert "--read-only-tmpfs=false" in argv  # no implicit writable /run and /var/tmp
    assert "--userns=keep-id" in argv
    assert "--cap-drop=ALL" in argv and "--security-opt=no-new-privileges" in argv
    assert flag(argv, "--pids-limit") == ["256"]
    assert flag(argv, "--memory") == ["2g"] and flag(argv, "--memory-swap") == ["2g"]
    assert flag(argv, "--cpus") == ["2"]
    assert flag(argv, "--volume") == [f"{ws.resolve()}:/workspace:Z"]
    assert flag(argv, "--workdir") == ["/workspace"]
    assert "--http-proxy=false" in argv and "--log-driver=none" in argv
    assert flag(argv, "--pull") == ["missing"]
    # image, then the shell and the command as ONE argv element (no shell interpolation by us)
    assert flag(argv, "--entrypoint") == ["sh"]
    assert argv[-3:] == ["docker.io/library/python:3.12-slim", "-lc", "pytest -q"]
    # options come before the image
    assert argv.index("docker.io/library/python:3.12-slim") > max(i for i, a in enumerate(argv) if a.startswith("--"))
    assert inv.timeout_seconds == 300  # policy default when the request did not set one
    assert inv.env_names == ("HOME", "TMPDIR")


def test_docker_argv_equivalent_flags(ws: Path) -> None:
    s = DockerSandbox(POLICY, max_output_bytes=10_000)
    argv = s.build_invocation(req(network=True), ws).argv
    assert argv[:3] == ["docker", "run", "--rm"]
    for f in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
        assert f in argv
    st = ws.stat()
    assert flag(argv, "--user") == [f"{st.st_uid}:{st.st_gid}"]
    assert flag(argv, "--mount") == [f"type=bind,source={ws.resolve()},target=/workspace"]
    assert flag(argv, "--network") == ["bridge"]
    assert flag(argv, "--memory") == flag(argv, "--memory-swap") == ["2g"]
    assert "--userns=keep-id" not in argv and "--http-proxy=false" not in argv  # podman-only flags
    assert flag(argv, "--shm-size") == ["64m"] and "--log-driver=none" in argv
    assert flag(argv, "--tmpfs") == ["/tmp:rw,nosuid,nodev,size=128m,mode=1777"]


def test_network_modes(ws: Path) -> None:
    assert flag(
        PodmanSandbox(POLICY, max_output_bytes=10_000, allowed_network="pasta").build_invocation(req(network=True), ws).argv, "--network"
    ) == ["pasta"]
    auto = PodmanSandbox(POLICY, max_output_bytes=10_000).build_invocation(req(network=True), ws)
    assert auto.network is True and flag(auto.argv, "--network")[0] in ("pasta", "slirp4netns")
    allowed = SandboxPolicy(**{**POLICY.model_dump(), "network_default": "allowed"})
    inv = PodmanSandbox(allowed, max_output_bytes=10_000, allowed_network="slirp4netns").build_invocation(req(), ws)
    assert inv.network is True and flag(inv.argv, "--network") == ["slirp4netns"]


def test_pasta_auto_only_rootless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert PodmanSandbox._auto_network() == "pasta"
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert PodmanSandbox._auto_network() == "slirp4netns"


def test_image_allowlist_and_aliases(ws: Path) -> None:
    assert resolve_image(POLICY, None) == POLICY.image
    assert resolve_image(POLICY, "node") == "docker.io/library/node:22-bookworm-slim"
    assert resolve_image(POLICY, "docker.io/library/node:22-bookworm-slim") == "docker.io/library/node:22-bookworm-slim"
    assert resolve_image(POLICY, "ghcr.io/x/y:1", extra=["ghcr.io/x/y:1"]) == "ghcr.io/x/y:1"
    for bad in ("alpine:latest", "--privileged", "docker.io/library/python:3.12"):
        with pytest.raises(PolicyViolation) as exc:
            resolve_image(POLICY, bad)
        assert exc.value.code == "SANDBOX_IMAGE_NOT_ALLOWED"
    broken = SandboxPolicy(image="-v /:/host")
    with pytest.raises(PolicyViolation) as exc:
        resolve_image(broken, None)
    assert exc.value.code == "SANDBOX_IMAGE_INVALID"
    s = PodmanSandbox(POLICY, max_output_bytes=10_000)
    assert s.build_invocation(req(image="node"), ws).image == "docker.io/library/node:22-bookworm-slim"
    assert s.allowed_images() == {POLICY.image, "docker.io/library/node:22-bookworm-slim"}


def test_env_allowlist(ws: Path) -> None:
    assert validate_request_env({"CI": "1", "APP_MODE": "test"}, POLICY.env_allowlist) == {"CI": "1", "APP_MODE": "test"}
    for env, code in (
        ({"LD_PRELOAD": "/x.so"}, "SANDBOX_ENV_NOT_ALLOWED"),
        ({"PATH": "/evil"}, "SANDBOX_ENV_NOT_ALLOWED"),
        ({"1BAD": "x"}, "SANDBOX_ENV_INVALID"),
        ({"A B": "x"}, "SANDBOX_ENV_INVALID"),
        ({"CI=1 --privileged": "x"}, "SANDBOX_ENV_INVALID"),
        ({"CI": "a\x00b"}, "SANDBOX_ENV_INVALID"),
    ):
        with pytest.raises(PolicyViolation) as exc:
            validate_request_env(env, POLICY.env_allowlist)
        assert exc.value.code == code, env
    inv = PodmanSandbox(POLICY, max_output_bytes=10_000).build_invocation(req(env={"APP_TOKEN": "s3cr3t-value", "CI": "1"}), ws)
    envs = [a.removeprefix("--env=") for a in inv.argv if a.startswith("--env=")]
    # request values are forwarded by name from the engine's own environment, never written into argv
    assert envs == ["HOME=/tmp", "TMPDIR=/tmp", "APP_TOKEN", "CI"]
    assert dict(inv.passthrough_env) == {"APP_TOKEN": "s3cr3t-value", "CI": "1"}
    assert not any("s3cr3t-value" in a for a in inv.argv)
    assert inv.secrets == ("s3cr3t-value",)  # masked in the output later
    # nothing of the worker's own environment is forwarded
    assert not any(e.startswith(("PATH=", "HTTPS_PROXY=", "https_proxy=")) for e in envs)


def test_request_env_may_override_base_home(ws: Path) -> None:
    policy = SandboxPolicy(**{**POLICY.model_dump(), "env_allowlist": ["HOME"]})
    inv = PodmanSandbox(policy, max_output_bytes=10_000).build_invocation(req(env={"HOME": "/workspace"}), ws)
    # HOME would change the engine binary's own behaviour (storage path) -> passed as NAME=value, not via its env
    assert flag(inv.argv, "--env") == ["HOME=/workspace", "TMPDIR=/tmp"] and dict(inv.passthrough_env) == {}


def test_engine_sensitive_names(ws: Path) -> None:
    assert sb.is_engine_env_name("PATH") and sb.is_engine_env_name("XDG_RUNTIME_DIR") and sb.is_engine_env_name("LD_PRELOAD")
    assert sb.is_engine_env_name("CONTAINERS_CONF") and sb.is_engine_env_name("LC_ALL")
    assert not sb.is_engine_env_name("CI") and not sb.is_engine_env_name("APP_HOME")
    policy = SandboxPolicy(**{**POLICY.model_dump(), "env_allowlist": ["DOCKER_*", "XDG_CACHE_HOME"]})
    s = PodmanSandbox(policy, max_output_bytes=10_000)
    with pytest.raises(PolicyViolation) as exc:  # secret-shaped AND engine-sensitive: neither argv nor engine env
        s.build_invocation(req(env={"DOCKER_AUTH_CONFIG": "{}"}), ws)
    assert exc.value.code == "SANDBOX_ENV_INVALID"
    inv = s.build_invocation(req(env={"XDG_CACHE_HOME": "/tmp/c"}), ws)
    assert "--env=XDG_CACHE_HOME=/tmp/c" in inv.argv and dict(inv.passthrough_env) == {}


def test_limits_may_be_lowered_not_raised(ws: Path) -> None:
    s = PodmanSandbox(POLICY, max_output_bytes=10_000)
    inv = s.build_invocation(req(cpus=0.5, memory="512M"), ws)
    assert (inv.cpus, inv.memory) == ("0.5", "512m")
    assert flag(inv.argv, "--memory-swap") == ["512m"]
    for kw, code in (
        ({"cpus": 4.0}, "SANDBOX_LIMIT_EXCEEDED"),
        ({"cpus": 0.0}, "SANDBOX_LIMIT_EXCEEDED"),
        ({"cpus": 0.00001}, "SANDBOX_LIMIT_EXCEEDED"),
        ({"memory": "3g"}, "SANDBOX_LIMIT_EXCEEDED"),
        ({"memory": "1k"}, "SANDBOX_LIMIT_INVALID"),
        ({"memory": "lots"}, "SANDBOX_LIMIT_INVALID"),
    ):
        with pytest.raises(PolicyViolation) as exc:
            s.build_invocation(req(**kw), ws)
        assert exc.value.code == code, kw


def test_cpus_never_formatted_with_exponent(ws: Path) -> None:
    s = PodmanSandbox(POLICY, max_output_bytes=10_000)
    assert s.build_invocation(req(cpus=0.01), ws).cpus == "0.01"
    assert s.build_invocation(req(cpus=1.0), ws).cpus == "1"
    assert s.build_invocation(req(cpus=1.25), ws).cpus == "1.25"


def test_managed_label_defaults_to_worker_container_label(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sb.default_managed_label({}) == DEFAULT_CONTAINER_LABEL
    assert sb.default_managed_label({"WORKER_CONTAINER_LABEL": " hermclaw.site=lab "}) == "hermclaw.site=lab"
    monkeypatch.setenv("WORKER_CONTAINER_LABEL", "hermclaw.site=lab")
    s = PodmanSandbox(POLICY, max_output_bytes=10_000)
    assert s.managed_label == "hermclaw.site=lab"
    assert flag(s.build_invocation(req(), ws).argv, "--label")[0] == "hermclaw.site=lab"
    monkeypatch.setenv("WORKER_CONTAINER_LABEL", "bad label")
    with pytest.raises(ConfigError):
        PodmanSandbox(POLICY, max_output_bytes=10_000)


def test_shm_size_configurable(ws: Path) -> None:
    argv = PodmanSandbox(POLICY, max_output_bytes=10_000, shm_size="16m").build_invocation(req(), ws).argv
    assert "/dev/shm:rw,nosuid,nodev,noexec,size=16m,mode=1777" in flag(argv, "--tmpfs")
    with pytest.raises(ConfigError):
        PodmanSandbox(POLICY, max_output_bytes=10_000, shm_size="big")


def test_explicit_timeout_wins_over_policy_default(ws: Path) -> None:
    s = PodmanSandbox(POLICY, max_output_bytes=10_000)
    assert s.build_invocation(req(timeout_seconds=5), ws).timeout_seconds == 5
    assert s.build_invocation(req(), ws).timeout_seconds == POLICY.default_timeout_seconds


def test_workspace_validation(tmp_path: Path, ws: Path) -> None:
    s = PodmanSandbox(POLICY, max_output_bytes=10_000)
    link = tmp_path / "link"
    link.symlink_to(ws)
    weird = tmp_path / "a:b"
    weird.mkdir()
    comma = tmp_path / "a,b"
    comma.mkdir()
    for path, code in (
        (tmp_path / "missing", "SANDBOX_WORKSPACE_MISSING"),
        (link, "SANDBOX_WORKSPACE_INVALID"),
        (weird, "SANDBOX_WORKSPACE_INVALID"),
        (comma, "SANDBOX_WORKSPACE_INVALID"),
        (Path("/"), "SANDBOX_WORKSPACE_INVALID"),
    ):
        with pytest.raises(PolicyViolation) as exc:
            s.build_invocation(req(), path)
        assert exc.value.code == code, path


def test_command_with_nul_rejected(ws: Path) -> None:
    with pytest.raises(PolicyViolation) as exc:
        PodmanSandbox(POLICY, max_output_bytes=10_000).build_invocation(req(command="echo a\x00b"), ws)
    assert exc.value.code == "SANDBOX_COMMAND_INVALID"


async def test_rejected_request_returns_error_result_without_running(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def no_exec(*a: Any, **k: Any) -> None:
        calls.append(a)
        raise AssertionError("must not start a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_exec)
    result = await PodmanSandbox(POLICY, max_output_bytes=10_000).run(req(image="evil:latest"), ws)
    assert result.exit_code is None and result.sandbox == "podman"
    assert result.error is not None and result.error.startswith("SANDBOX_IMAGE_NOT_ALLOWED")
    assert calls == []


def test_container_names_and_labels() -> None:
    assert container_name_for("abc-123_x.y") == "hermclaw-abc-123_x.y"
    odd = container_name_for("job/1 step:2")
    assert odd.startswith("hermclaw-job-1-step-2-") and len(odd) <= 80
    assert container_name_for("a/b") != container_name_for("a b")  # sanitized ids keep a hash suffix
    long_name = container_name_for("x" * 200)
    assert len(long_name) < 80 and long_name != container_name_for("x" * 199)
    assert label_value("job 1\n--privileged") == "job_1_--privileged"
    assert len(label_value("y" * 500)) == 128


def test_require_rootless(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    s = make_sandbox(POLICY, environment="production", max_output_bytes=10_000)
    assert isinstance(s, PodmanSandbox) and s.require_rootless is True
    with pytest.raises(PolicyViolation) as exc:
        s.build_invocation(req(), ws)
    assert exc.value.code == "SANDBOX_NOT_ROOTLESS"
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert s.build_invocation(req(), ws).container_name == "hermclaw-req-1"
    dev = make_sandbox(POLICY, environment="development", max_output_bytes=10_000)
    assert isinstance(dev, PodmanSandbox) and dev.require_rootless is False


def test_make_sandbox_engines() -> None:
    assert isinstance(make_sandbox(POLICY, environment="test", max_output_bytes=10_000), PodmanSandbox)
    docker = make_sandbox(SandboxPolicy(engine="docker"), environment="production", max_output_bytes=10_000)
    assert isinstance(docker, DockerSandbox)
    local = make_sandbox(SandboxPolicy(engine="local"), environment="test", max_output_bytes=10_000)
    assert isinstance(local, LocalSandbox)
    for runner in (docker, local):
        assert isinstance(runner, SandboxRunner)
    with pytest.raises(PolicyViolation) as exc:
        make_sandbox(SandboxPolicy(engine="local"), environment="production")
    assert exc.value.code == "SANDBOX_LOCAL_FORBIDDEN"
    with pytest.raises(ConfigError):
        make_sandbox(SandboxPolicy(engine="local"), environment="test", executable="x")


def test_local_sandbox_uses_hermclaw_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermclaw.core.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("HERMCLAW_ENV", "production")
    try:
        with pytest.raises(PolicyViolation):
            make_sandbox(SandboxPolicy(engine="local"))
    finally:
        get_settings.cache_clear()


def test_invalid_configuration_rejected() -> None:
    for kw in ({"memory": "2 GB"}, {"tmpfs_size": "-1"}, {"cpus": 0}, {"pids_limit": 0}):
        with pytest.raises(ConfigError):
            PodmanSandbox(SandboxPolicy(**kw), max_output_bytes=10_000)  # type: ignore[arg-type]
    for label in ("", "a b", "key=", "x;rm"):
        with pytest.raises(ConfigError):
            PodmanSandbox(POLICY, max_output_bytes=10_000, managed_label=label)
    with pytest.raises(ConfigError):
        PodmanSandbox(POLICY, max_output_bytes=10_000, shell=())


def test_parse_size() -> None:
    assert parse_size("512m") == 512 * 1024**2
    assert parse_size("2G") == 2 * 1024**3
    assert parse_size("1024") == 1024 and parse_size("7b") == 7 and parse_size("3k") == 3072
    for bad in ("", "1.5g", "-1m", "10t", "1 g"):
        with pytest.raises(ValueError, match="invalid size"):
            parse_size(bad)


def test_output_capture_keeps_head_and_tail() -> None:
    cap = OutputCapture(2048)
    cap.feed(b"HEAD" + b"x" * 5000)
    for _ in range(100):
        cap.feed(b"y" * 100)
    cap.feed(b"TAIL")
    text = cap.text()
    assert cap.truncated and cap.total == 4 + 5000 + 10_000 + 4
    assert text.startswith("HEAD") and text.endswith("TAIL")
    assert "bytes omitted" in text and len(text) < 2048 + 64
    small = OutputCapture(2048)
    small.feed(b"a" * 1000)
    small.feed(b"b" * 1048)
    assert not small.truncated and small.text() == "a" * 1000 + "b" * 1048
    # invalid UTF-8 never raises
    bad = OutputCapture(1024)
    bad.feed(b"\xff\xfe ok")
    assert bad.text().endswith(" ok")


def test_default_max_output_bytes_from_policies_file(tmp_path: Path) -> None:
    assert default_max_output_bytes({}) == 200_000
    f = tmp_path / "policies.yaml"
    f.write_text("commands:\n  max_output_bytes: 4096\nsandbox:\n  engine: podman\n")
    assert default_max_output_bytes({"WORKER_POLICIES_FILE": str(f)}) == 4096
    f.write_text("commands:\n  max_output_bytes: nope\n")
    with pytest.raises(ConfigError):
        default_max_output_bytes({"WORKER_POLICIES_FILE": str(f)})
    with pytest.raises(ConfigError):
        default_max_output_bytes({"WORKER_POLICIES_FILE": str(tmp_path / "missing.yaml")})


def test_podman_info_assessment() -> None:
    rootless_v2 = parse_podman_info(
        {
            "host": {
                "cgroupVersion": "v2",
                "cgroupManager": "systemd",
                "cgroupControllers": ["cpu", "memory", "pids"],
                "security": {"rootless": True},
            },
            "version": {"Version": "5.4.2"},
        }
    )
    assert rootless_v2.limits_effective and rootless_v2.warnings == [] and rootless_v2.version == "5.4.2"
    undelegated = parse_podman_info(
        {"host": {"cgroupVersion": "v2", "cgroupControllers": ["memory", "pids"], "security": {"rootless": True}}, "version": {}}
    )
    assert not undelegated.limits_effective and any("cpu" in w and "Delegate" in w for w in undelegated.warnings)
    rootless_v1 = parse_podman_info(
        {"host": {"cgroupVersion": "v1", "cgroupControllers": ["cpu", "memory", "pids"], "security": {"rootless": True}}}
    )
    assert not rootless_v1.limits_effective and any("cgroups v1" in w for w in rootless_v1.warnings)
    rootful = parse_podman_info(
        {"host": {"cgroupVersion": "v1", "cgroupControllers": ["cpu", "memory", "pids"], "security": {"rootless": False}}}
    )
    assert rootful.limits_effective and any("root" in w for w in rootful.warnings)
    assert rootful.as_dict()["engine"] == "podman"


def test_docker_info_assessment() -> None:
    info = parse_docker_info(
        {
            "ServerVersion": "27.0",
            "CgroupVersion": "2",
            "CgroupDriver": "systemd",
            "SecurityOptions": ["name=seccomp,profile=builtin", "name=rootless", "name=cgroupns"],
            "MemoryLimit": True,
            "CpuCfsQuota": True,
            "PidsLimit": True,
        }
    )
    assert info.rootless is True and info.cgroup_version == "v2" and info.limits_effective


async def test_engine_info_unavailable() -> None:
    info = await sb.engine_info("podman", "/nonexistent/podman")
    assert not info.available and info.error


async def test_engine_exec_timeout_and_missing() -> None:
    rc, _out, err = await sb.engine_exec(["sleep", "5"], timeout_seconds=0.2)
    assert rc == -9 and "timed out" in err
    rc, _out, _err = await sb.engine_exec(["/nonexistent/binary"])
    assert rc == 127


def test_secret_env_values() -> None:
    env = {"DB_PASSWORD": "hunter22", "API_KEY": "k-1234", "MODE": "secretless-but-long", "GH_TOKEN": "abc"}
    assert sorted(sb.secret_env_values(env)) == ["hunter22", "k-1234"]  # short values are not registered


def test_module_has_no_host_env_passthrough() -> None:
    """Regression guard: the container env is built only from BASE_CONTAINER_ENV and the request."""
    assert dict(sb.BASE_CONTAINER_ENV) == {"HOME": "/tmp", "TMPDIR": "/tmp"}
