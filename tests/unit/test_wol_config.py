"""P10 10.1: semantic checks of the hosts' Wake-on-LAN configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hermclaw.core.config import HostConfig, HostsConfig, ServiceProbe, SshConfig, WakeOnLanConfig, load_config
from hermclaw.core.errors import ConfigError
from hermclaw.wol import IssueSeverity, assert_wake_config, validate_host_wake_config, validate_wake_config

ROOT = Path(__file__).resolve().parents[2]


def host(**kw: Any) -> HostConfig:
    wol = kw.pop("wol", {})
    values: dict[str, Any] = {
        "id": "w1",
        "address": "192.168.178.222",
        "role": "execution_worker",
        "worker_kind": "execution",
        "wake_on_lan": WakeOnLanConfig(**{"enabled": True, "mac": "52:54:00:aa:bb:cc", **wol}),
        "ssh": SshConfig(port=22),
        "services": [ServiceProbe(name="worker_api", port=8787, http_path="/health")],
    }
    values.update(kw)
    return HostConfig(**values)


def fields(h: HostConfig, severity: IssueSeverity | None = None) -> list[str]:
    return [i.field for i in validate_host_wake_config(h) if severity is None or i.severity == severity]


def test_example_configuration_has_no_errors() -> None:
    cfg = load_config(ROOT / "config")
    issues = validate_wake_config(cfg)
    assert [i for i in issues if i.severity == IssueSeverity.error] == []
    assert any(h.wake_on_lan.enabled for h in cfg.hosts.hosts)  # the check really covered WOL hosts
    assert assert_wake_config(cfg) == issues


def test_valid_host_is_clean() -> None:
    assert validate_host_wake_config(host()) == []


@pytest.mark.parametrize(
    ("wol", "field"),
    [
        ({"mac": ""}, "wake_on_lan.mac"),
        ({"mac": "01:00:5e:00:00:01"}, "wake_on_lan.mac"),  # multicast cannot identify a NIC
        ({"mac": "ff:ff:ff:ff:ff:ff"}, "wake_on_lan.mac"),
        ({"broadcast": "192.168.178"}, "wake_on_lan.broadcast"),
        ({"broadcast": "fe80::1"}, "wake_on_lan.broadcast"),  # IPv6 has no broadcast
        ({"broadcast": "0.0.0.0"}, "wake_on_lan.broadcast"),
        ({"port": 0}, "wake_on_lan.port"),
        ({"port": 70000}, "wake_on_lan.port"),
        ({"ping_timeout_seconds": 0}, "wake_on_lan.ping_timeout_seconds"),
        ({"service_timeout_seconds": -5}, "wake_on_lan.service_timeout_seconds"),
        ({"max_attempts": 0}, "wake_on_lan.max_attempts"),
    ],
)
def test_wol_errors(wol: dict[str, Any], field: str) -> None:
    assert fields(host(wol=wol), IssueSeverity.error) == [field]


def test_disabled_wol_is_not_checked() -> None:
    h = host(wol={"enabled": False, "mac": "", "ping_timeout_seconds": 0, "max_attempts": 0})
    assert validate_host_wake_config(h) == []


def test_many_attempts_is_only_a_warning() -> None:
    assert fields(host(wol={"max_attempts": 50}), IssueSeverity.warning) == ["wake_on_lan.max_attempts"]
    assert fields(host(wol={"max_attempts": 50}), IssueSeverity.error) == []


@pytest.mark.parametrize(
    ("kw", "field"),
    [
        ({"address": "-oProxyCommand=x"}, "address"),
        ({"worker_api": "http://user:pw@192.168.178.222:8787"}, "worker_api"),
        ({"worker_api": "ftp://192.168.178.222"}, "worker_api"),
        ({"ssh": SshConfig(port=0)}, "ssh.port"),
        ({"services": [ServiceProbe(name="ollama", port=11434, http_path="//evil.example/x")]}, "services[0]"),
        ({"services": [ServiceProbe(name="ollama", port=0)]}, "services[0]"),
        ({"idle_sleep_command": "systemctl suspend\nrm -rf /"}, "idle_sleep_command"),
        ({"idle_sleep_command": "   "}, "idle_sleep_command"),
    ],
)
def test_probe_target_and_command_errors(kw: dict[str, Any], field: str) -> None:
    assert field in fields(host(**kw), IssueSeverity.error)


def test_duplicate_service_and_sleep_without_wol_warn() -> None:
    svc = ServiceProbe(name="ollama", port=11434, http_path="/api/version")
    h = host(services=[svc, svc], idle_sleep_command="sudo systemctl suspend", wol={"enabled": False})
    assert fields(h, IssueSeverity.warning) == ["services[1].name", "idle_sleep_command"]
    assert fields(h, IssueSeverity.error) == []


def test_assert_wake_config_raises_with_all_errors() -> None:
    cfg = HostsConfig(hosts=[host(id="a", wol={"mac": ""}), host(id="b", wol={"ping_timeout_seconds": 0}), host(id="c")])
    with pytest.raises(ConfigError) as ei:
        assert_wake_config(cfg)
    assert ei.value.code == "CONFIG_INVALID"
    assert [(i["host_id"], i["field"]) for i in ei.value.details["issues"]] == [
        ("a", "wake_on_lan.mac"),
        ("b", "wake_on_lan.ping_timeout_seconds"),
    ]
    assert "host 'a'" in ei.value.message and "host 'c'" not in ei.value.message
