"""Capacity budgets over groups of resources (Bauplan §4: ``.224`` has limited RAM/VRAM).

A :class:`ResourceBudget` couples several resources into one capacity domain: the sum of the ``weight`` of all
holding leases (``active``/``preempting``) on its member resources must stay ``<= capacity``. The model host budget
couples the exclusive ``large-model-224`` group (weight = memory of the one resident large model) with the shared
``small-model-224`` group (weight = memory of each small model), capacity ``models.model_host_capacity_gb``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from hermclaw.core.config import ModelProfileConfig, ModelsConfig
from hermclaw.core.errors import ConfigError
from hermclaw.resources.constants import PRIORITIES, ROLE_OWNER_KIND, validate_resource_name

#: float tolerance for weight sums (GB values from YAML)
EPSILON = 1e-9


@dataclass(frozen=True)
class ResourceBudget:
    name: str
    capacity: float
    members: frozenset[str]

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigError("resource budget needs a name", code="RESOURCE_BUDGET_INVALID")
        if not math.isfinite(self.capacity) or self.capacity < 0:
            raise ConfigError(f"budget '{self.name}': capacity must be a finite number >= 0", code="RESOURCE_BUDGET_INVALID")
        if not self.members:
            raise ConfigError(f"budget '{self.name}' has no member resources", code="RESOURCE_BUDGET_INVALID")
        for m in self.members:
            validate_resource_name(m)

    def fits(self, used: float, weight: float) -> bool:
        return used + weight <= self.capacity + EPSILON


@dataclass(frozen=True)
class BudgetUsage:
    budget: str
    capacity: float
    used: float
    by_resource: Mapping[str, float] = field(default_factory=dict)

    @property
    def free(self) -> float:
        return max(0.0, self.capacity - self.used)


def model_host_budgets(models: ModelsConfig, *, include_disabled: bool = False) -> list[ResourceBudget]:
    """One budget per model host: all resource groups of that host's profiles, capacity ``model_host_capacity_gb``."""
    by_host: dict[str, set[str]] = {}
    for p in models.profiles:
        if not p.enabled and not include_disabled:
            continue
        by_host.setdefault(p.host, set()).add(p.resource_group)
    return [
        ResourceBudget(name=f"model-host:{host}", capacity=float(models.model_host_capacity_gb), members=frozenset(groups))
        for host, groups in sorted(by_host.items())
    ]


def model_host_budget(models: ModelsConfig, host: str = "model-224") -> ResourceBudget:
    for b in model_host_budgets(models):
        if b.name == f"model-host:{host}":
            return b
    raise ConfigError(f"no enabled model profiles on host '{host}'", code="RESOURCE_BUDGET_INVALID")


def check_budgets_disjoint(budgets: Sequence[ResourceBudget]) -> None:
    """A resource may belong to several budgets, but budget names must be unique."""
    names = [b.name for b in budgets]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ConfigError(f"duplicate resource budget names: {dupes}", code="RESOURCE_BUDGET_INVALID")


def validate_model_resources(models: ModelsConfig) -> list[str]:
    """Static checks of the model resource policy; returns human-readable issues (empty = consistent).

    * every profile fits into the host capacity on its own (otherwise it can never be leased),
    * the configured priority matches the Bauplan §4 priority of its role,
    * all members of one resource group agree on ``exclusive``.
    """
    issues: list[str] = []
    cap = float(models.model_host_capacity_gb)
    groups: dict[tuple[str, str], set[bool]] = {}
    for p in models.profiles:
        if not p.enabled:
            continue
        if p.memory_gb > cap + EPSILON:
            issues.append(f"{p.alias}: memory_gb {p.memory_gb} exceeds model_host_capacity_gb {cap}")
        kind = ROLE_OWNER_KIND.get(p.role)
        if kind is not None and PRIORITIES[kind] != p.priority:
            issues.append(f"{p.alias}: priority {p.priority} differs from policy priority {PRIORITIES[kind]} for role '{p.role}'")
        groups.setdefault((p.host, p.resource_group), set()).add(p.exclusive)
    for (host, group), flags in sorted(groups.items()):
        if len(flags) > 1:
            issues.append(f"{host}/{group}: profiles disagree on 'exclusive'")
    return issues


def worst_case_resident_gb(profiles: Iterable[ModelProfileConfig]) -> float:
    """Worst-case resident memory of the given profiles (one host): per exclusive resource group the largest
    member, plus every member of the shared groups."""
    exclusive: dict[str, float] = {}
    shared = 0.0
    for p in profiles:
        if not p.enabled:
            continue
        if p.exclusive:
            exclusive[p.resource_group] = max(exclusive.get(p.resource_group, 0.0), p.memory_gb)
        else:
            shared += p.memory_gb
    return float(sum(exclusive.values()) + shared)
