"""``policies.git.workspace_retention_hours`` and ``policies.wake_on_lan`` (model, shipped example, validation)
and the mapping of ``policies.wake_on_lan`` onto the ``hermclaw.wol`` settings used by the scheduler process."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hermclaw.core.config import GitPolicy, HostConfig, HostsConfig, PoliciesConfig, WakeOnLanConfig, WakeOnLanPolicy, load_config
from hermclaw.core.errors import ConfigError
from hermclaw.core.settings import Settings
from hermclaw.runtime.main import _build_wake, idle_settings_from_policy, wake_settings_from_policy
from hermclaw.wol.idle import IdleSleepSettings
from hermclaw.wol.readiness import WakeController, WakeSettings

ROOT = Path(__file__).resolve().parents[2]
CONFIG_NAMES = ("hosts", "models", "policies", "capabilities", "logging")


def _config_dir(tmp_path: Path, policies: dict[str, object] | None = None) -> Path:
    for name in CONFIG_NAMES:
        (tmp_path / f"{name}.yaml").write_text((ROOT / "config" / f"{name}.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    if policies is not None:
        data = yaml.safe_load((tmp_path / "policies.yaml").read_text(encoding="utf-8"))
        for section, values in policies.items():
            data.setdefault(section, {}).update(values)  # type: ignore[union-attr]
        (tmp_path / "policies.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return tmp_path


def test_defaults() -> None:
    p = PoliciesConfig()
    assert p.git.workspace_retention_hours == 72
    assert p.wake_on_lan == WakeOnLanPolicy(idle_after_minutes=30.0, idle_command_timeout_seconds=60.0, probe_interval_seconds=2.0)


def test_shipped_example_documents_both_settings() -> None:
    raw = yaml.safe_load((ROOT / "config" / "policies.example.yaml").read_text(encoding="utf-8"))
    assert raw["git"]["workspace_retention_hours"] == 72
    assert raw["wake_on_lan"] == {"idle_after_minutes": 30, "idle_command_timeout_seconds": 60, "probe_interval_seconds": 2}
    cfg = load_config(ROOT / "config")
    assert cfg.policies.git.workspace_retention_hours == 72
    assert cfg.policies.wake_on_lan.idle_after_minutes == 30


def test_overrides_load_from_policies_yaml(tmp_path: Path) -> None:
    cdir = _config_dir(
        tmp_path,
        {"git": {"workspace_retention_hours": 0}, "wake_on_lan": {"idle_after_minutes": 12.5, "probe_interval_seconds": 0.5}},
    )
    cfg = load_config(cdir)
    assert cfg.policies.git.workspace_retention_hours == 0, "0 keeps workspaces forever"
    assert cfg.policies.wake_on_lan.idle_after_minutes == 12.5
    assert cfg.policies.wake_on_lan.probe_interval_seconds == 0.5
    assert cfg.policies.wake_on_lan.idle_command_timeout_seconds == 60.0


@pytest.mark.parametrize(
    "section,values",
    [
        ("git", {"workspace_retention_hours": -1}),
        ("wake_on_lan", {"idle_after_minutes": 0}),
        ("wake_on_lan", {"idle_command_timeout_seconds": -5}),
        ("wake_on_lan", {"probe_interval_seconds": 0}),
        ("wake_on_lan", {"probe_interval_seconds": 120}),
        ("wake_on_lan", {"unknown_knob": 1}),
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, section: str, values: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        load_config(_config_dir(tmp_path, {section: values}))


def test_git_policy_rejects_negative_retention() -> None:
    with pytest.raises(ValidationError):
        GitPolicy(workspace_retention_hours=-1)


def test_policy_maps_onto_the_wol_settings() -> None:
    policy = WakeOnLanPolicy(idle_after_minutes=45, idle_command_timeout_seconds=20, probe_interval_seconds=0.75)
    wake = wake_settings_from_policy(policy)
    assert isinstance(wake, WakeSettings) and wake.probe_interval_seconds == 0.75
    assert wake.probe_timeout_seconds == WakeSettings().probe_timeout_seconds, "unmapped fields keep their defaults"
    idle = idle_settings_from_policy(policy)
    assert isinstance(idle, IdleSleepSettings)
    assert (idle.idle_after_minutes, idle.command_timeout_seconds) == (45.0, 20.0)
    assert idle.require_wake_on_lan is True and idle.check_interval_seconds == IdleSleepSettings().check_interval_seconds
    # the defaults of the policy are the code defaults of hermclaw.wol (no behaviour change without a policy)
    assert wake_settings_from_policy(WakeOnLanPolicy()) == WakeSettings()
    assert idle_settings_from_policy(WakeOnLanPolicy()) == IdleSleepSettings()


async def test_scheduler_wake_controller_uses_the_policy(tmp_path: Path) -> None:
    cfg = load_config(_config_dir(tmp_path, {"wake_on_lan": {"probe_interval_seconds": 0.25}}))
    host = HostConfig(
        id="model-x",
        address="192.0.2.10",
        role="model_worker",
        worker_kind="model",
        worker_api="http://192.0.2.10:8090",
        wake_on_lan=WakeOnLanConfig(enabled=True, mac="aa:bb:cc:dd:ee:ff", broadcast="192.0.2.255"),
    )
    cfg = cfg.model_copy(update={"hosts": HostsConfig(hosts=[host])})
    engine = create_async_engine("postgresql+psycopg://unused@127.0.0.1:1/unused")  # never connected
    sm = async_sessionmaker(engine, class_=AsyncSession)
    try:
        assert _build_wake(cfg, Settings(env="test", wake_on_lan=False), sm) is None, "disabled in the settings"
        wake = _build_wake(cfg, Settings(env="test", wake_on_lan=True), sm)
        assert isinstance(wake, WakeController)
        assert wake.settings.probe_interval_seconds == 0.25
        await wake.aclose()
    finally:
        await engine.dispose()


def test_the_shipped_lint_commands_cover_rust_and_go_and_skip_without_their_project(tmp_path: Path) -> None:
    """Rust ladder on rc98 (BUG-371): the verifier had a lint command for Python only. The shipped example (what the
    deployment installs) and the defaults agree, and a command for a crate or module exits 0 where there is none."""
    import subprocess

    from hermclaw.core.config import DEFAULT_LINT_COMMANDS

    shipped = load_config(ROOT / "config").policies.verifier.lint_commands
    assert shipped == DEFAULT_LINT_COMMANDS and {"python", "rust", "go"} <= set(shipped)
    assert "-D warnings" in shipped["rust"] and "go vet ./..." in shipped["go"]
    for language in ("rust", "go"):
        assert subprocess.run(["sh", "-c", shipped[language]], cwd=tmp_path, check=False).returncode == 0, language
