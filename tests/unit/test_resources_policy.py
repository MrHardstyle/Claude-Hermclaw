"""Resource policy unit tests: priorities, owner kinds, budgets, validation (P09, no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from hermclaw.core.config import LeasePolicy, ModelProfileConfig, ModelsConfig
from hermclaw.core.errors import ConfigError, ResourceUnavailable, ValidationFailed
from hermclaw.resources import (
    GPU_224,
    LARGE_MODEL_224,
    MODEL_RESOURCES,
    PRIORITIES,
    ROLE_OWNER_KIND,
    SMALL_MODEL_224,
    VIDEO_224,
    Lease,
    OwnerKind,
    ResourceBudget,
    ResourceManager,
    model_host_budget,
    model_host_budgets,
    priority_for,
    validate_model_resources,
    worst_case_resident_gb,
)
from hermclaw.resources.constants import validate_holder_id, validate_reason, validate_resource_name

ROOT = Path(__file__).resolve().parents[2]


def example_models() -> ModelsConfig:
    return ModelsConfig.model_validate(yaml.safe_load((ROOT / "config" / "models.example.yaml").read_text(encoding="utf-8")))


def test_priorities_match_bauplan_section_4() -> None:
    assert dict(PRIORITIES) == {"video": 100, "image": 90, "planner": 70, "heavy": 60, "coder": 50, "fast": 30, "embedding": 20}
    order = sorted(PRIORITIES, key=PRIORITIES.__getitem__, reverse=True)
    assert order == [
        OwnerKind.VIDEO,
        OwnerKind.IMAGE,
        OwnerKind.PLANNER,
        OwnerKind.HEAVY,
        OwnerKind.CODER,
        OwnerKind.FAST,
        OwnerKind.EMBEDDING,
    ]
    with pytest.raises(TypeError):
        PRIORITIES["video"] = 1  # type: ignore[index]


def test_priority_for_and_role_mapping() -> None:
    assert priority_for("planner") == 70
    assert priority_for("exec", default=40) == 40
    with pytest.raises(ValidationFailed):
        priority_for("exec")
    assert ROLE_OWNER_KIND["planner_fallback"] == OwnerKind.PLANNER
    assert ROLE_OWNER_KIND["heavy"] == OwnerKind.HEAVY and ROLE_OWNER_KIND["coder"] == OwnerKind.CODER


def test_resource_names_of_bauplan_section_25() -> None:
    assert (GPU_224, LARGE_MODEL_224, VIDEO_224) == ("gpu-224", "large-model-224", "video-224")
    assert set(MODEL_RESOURCES) == {LARGE_MODEL_224, SMALL_MODEL_224}


def test_example_config_model_host_budget_is_consistent() -> None:
    models = example_models()
    [budget] = model_host_budgets(models)
    assert budget == model_host_budget(models, "model-224")
    assert budget.capacity == 34 and budget.members == {"large-model-224", "small-model-224"}
    assert validate_model_resources(models) == []
    # worst case: heavy (25, the largest exclusive) + fast (7) + embedding (2) must fit on the host
    assert worst_case_resident_gb(models.profiles) == 34 <= budget.capacity
    with pytest.raises(ConfigError):
        model_host_budget(models, "no-such-host")


def test_validate_model_resources_reports_issues() -> None:
    models = example_models()
    bad = models.model_copy(
        update={
            "profiles": [
                *models.profiles,
                ModelProfileConfig(alias="huge", role="coder", model="huge:1", priority=55, memory_gb=99),
                ModelProfileConfig(alias="odd", role="fast", model="odd:1", priority=30, resource_group="small-model-224", exclusive=True),
            ]
        }
    )
    issues = validate_model_resources(bad)
    assert any("huge: memory_gb 99" in i for i in issues)
    assert any("huge: priority 55" in i for i in issues)
    assert any("small-model-224: profiles disagree" in i for i in issues)


def test_budget_validation_and_fit() -> None:
    b = ResourceBudget(name="b", capacity=10, members=frozenset({"a", "c"}))
    assert b.fits(6, 4) and not b.fits(6, 4.5)
    assert b.fits(9.1 + 0.9, 0)  # float tolerance
    for kwargs in ({"name": "", "capacity": 1}, {"name": "x", "capacity": -1}, {"name": "x", "capacity": float("nan")}):
        with pytest.raises(ConfigError):
            ResourceBudget(members=frozenset({"a"}), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        ResourceBudget(name="x", capacity=1, members=frozenset())
    with pytest.raises(ValidationFailed):
        ResourceBudget(name="x", capacity=1, members=frozenset({"Bad Name"}))


def test_name_validation_rejects_injection_and_traversal() -> None:
    for bad in ["", "A", "x y", "x;drop", "../etc", "x" * 201, "ü"]:
        with pytest.raises(ValidationFailed):
            validate_resource_name(bad)
    assert validate_resource_name("large-model-224") == "large-model-224"
    assert validate_holder_id("runtime@webui-223") == "runtime@webui-223"
    with pytest.raises(ValidationFailed):
        validate_holder_id("bad holder")
    with pytest.raises(ValidationFailed):
        validate_reason("Reason With Spaces")


def _mgr(**kw: object) -> ResourceManager:
    budgets = [ResourceBudget(name="host", capacity=34, members=frozenset({"large-model-224", "small-model-224"}))]
    return ResourceManager(None, LeasePolicy(), "unit@test", budgets=budgets, **kw)  # type: ignore[arg-type]


def test_manager_lock_set_and_intervals() -> None:
    m = _mgr()
    assert m.lock_set("small-model-224") == ["large-model-224", "small-model-224"]
    assert m.lock_set("gpu-224") == ["gpu-224"]
    assert m.request_ttl_seconds >= 30  # default: policies.leases.heartbeat_seconds
    assert m.keeper_interval() == 30.0  # min(heartbeat 30, ttl 300 / 3, grace 120 / 4)
    with pytest.raises(ValidationFailed):
        _mgr(poll_interval=0)
    with pytest.raises(ValidationFailed):
        _mgr(poll_interval=1, request_ttl_seconds=0.5)
    with pytest.raises(ConfigError):
        ResourceManager(None, LeasePolicy(), "unit@test", budgets=[ResourceBudget("x", 1, frozenset({"a"}))] * 2)  # type: ignore[arg-type]


async def test_over_capacity_request_fails_before_touching_the_database() -> None:
    m = _mgr()  # sessionmaker None: any DB access would crash
    with pytest.raises(ResourceUnavailable) as ei:
        await m.acquire("small-model-224", owner_kind="fast", exclusive=False, weight=35)
    assert ei.value.code == "RESOURCE_OVER_CAPACITY"
    with pytest.raises(ValidationFailed):
        await m.acquire_gpu_for_media("audio")


def test_lease_flags() -> None:
    now = datetime.now(UTC)
    base = {
        "id": uuid.uuid4(),
        "resource": "r",
        "resource_group": "r",
        "owner_kind": "coder",
        "holder": "h",
        "priority": 50,
        "exclusive": True,
        "weight": 0.0,
        "preemptible": True,
        "job_id": None,
        "step_id": None,
        "acquired_at": now,
        "heartbeat_at": now,
        "expires_at": now,
    }
    active = Lease(state="active", **base)  # type: ignore[arg-type]
    preempting = Lease(state="preempting", metadata={"preempt": {"reason": "x"}}, **base)  # type: ignore[arg-type]
    released = Lease(state="released", **base)  # type: ignore[arg-type]
    assert active.holding and not active.should_yield and active.preemption is None
    assert preempting.holding and preempting.should_yield and preempting.preemption == {"reason": "x"}
    assert not released.holding and released.should_yield
