"""Resource manager (P09): PostgreSQL leases with priorities, budgets, safe preemption and crash recovery."""

from hermclaw.resources.budget import (
    BudgetUsage,
    ResourceBudget,
    model_host_budget,
    model_host_budgets,
    validate_model_resources,
    worst_case_resident_gb,
)
from hermclaw.resources.constants import (
    CODE_EXECUTOR_222,
    GPU_224,
    LARGE_MODEL_224,
    MODEL_RESOURCES,
    PRIORITIES,
    ROLE_OWNER_KIND,
    SMALL_MODEL_224,
    VIDEO_224,
    OwnerKind,
    priority_for,
)
from hermclaw.resources.keeper import LeaseKeeper
from hermclaw.resources.manager import ResourceManager
from hermclaw.resources.types import (
    Lease,
    LeaseLost,
    LeaseNotOwned,
    LeaseStatus,
    MediaLeases,
    PreemptionResult,
    RecoveryReport,
    SweepReport,
    WaitingRequest,
)

__all__ = [
    "CODE_EXECUTOR_222",
    "GPU_224",
    "LARGE_MODEL_224",
    "MODEL_RESOURCES",
    "PRIORITIES",
    "ROLE_OWNER_KIND",
    "SMALL_MODEL_224",
    "VIDEO_224",
    "BudgetUsage",
    "Lease",
    "LeaseKeeper",
    "LeaseLost",
    "LeaseNotOwned",
    "LeaseStatus",
    "MediaLeases",
    "OwnerKind",
    "PreemptionResult",
    "RecoveryReport",
    "ResourceBudget",
    "ResourceManager",
    "SweepReport",
    "WaitingRequest",
    "model_host_budget",
    "model_host_budgets",
    "priority_for",
    "validate_model_resources",
    "worst_case_resident_gb",
]
