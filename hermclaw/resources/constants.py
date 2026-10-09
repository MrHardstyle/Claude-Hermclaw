"""Resource names, owner kinds and lease priorities (Bauplan §4, §25).

The priorities are the architecture's model resource policy (Bauplan §4)::

    video 100 > image 90 > planner 70 > heavy 60 > coder 50 > fast 30 > embedding 20

The resource names are the resource groups of Bauplan §25 plus ``small-model-224`` (the shared, memory-weighted group
of the small models – fast router and embedding – configured in ``models.yaml``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from hermclaw.core.errors import ValidationFailed

# ----------------------------------------------------------------------------------------------- resource names
GPU_224: Final = "gpu-224"
LARGE_MODEL_224: Final = "large-model-224"
SMALL_MODEL_224: Final = "small-model-224"
VIDEO_224: Final = "video-224"
CODE_EXECUTOR_222: Final = "code-executor-222"

#: AI model resource groups that a media job drains before it uses the GPU (Bauplan §3.7, §32).
MODEL_RESOURCES: Final[tuple[str, ...]] = (LARGE_MODEL_224, SMALL_MODEL_224)


# ----------------------------------------------------------------------------------------------- owner kinds
class OwnerKind:
    """``resource_leases.owner_kind`` values used by the runtime."""

    VIDEO: Final = "video"
    IMAGE: Final = "image"
    PLANNER: Final = "planner"
    HEAVY: Final = "heavy"
    CODER: Final = "coder"
    FAST: Final = "fast"
    EMBEDDING: Final = "embedding"
    EXEC: Final = "exec"
    ADMIN: Final = "admin"


PRIORITIES: Final[Mapping[str, int]] = MappingProxyType(
    {
        OwnerKind.VIDEO: 100,
        OwnerKind.IMAGE: 90,
        OwnerKind.PLANNER: 70,
        OwnerKind.HEAVY: 60,
        OwnerKind.CODER: 50,
        OwnerKind.FAST: 30,
        OwnerKind.EMBEDDING: 20,
    }
)

#: model profile role (``models.yaml``) -> lease owner kind
ROLE_OWNER_KIND: Final[Mapping[str, str]] = MappingProxyType(
    {
        "fast": OwnerKind.FAST,
        "planner": OwnerKind.PLANNER,
        "planner_fallback": OwnerKind.PLANNER,
        "coder": OwnerKind.CODER,
        "heavy": OwnerKind.HEAVY,
        "embedding": OwnerKind.EMBEDDING,
    }
)

MEDIA_KINDS: Final[frozenset[str]] = frozenset({OwnerKind.VIDEO, OwnerKind.IMAGE})

# ----------------------------------------------------------------------------------------------- states
LEASE_ACTIVE: Final = "active"
LEASE_PREEMPTING: Final = "preempting"
LEASE_RELEASED: Final = "released"
LEASE_EXPIRED: Final = "expired"
#: states in which a lease still holds its resource
HOLDING_STATES: Final[tuple[str, ...]] = (LEASE_ACTIVE, LEASE_PREEMPTING)

REQUEST_WAITING: Final = "waiting"
REQUEST_GRANTED: Final = "granted"
REQUEST_CANCELLED: Final = "cancelled"

MIN_PRIORITY: Final = 0
MAX_PRIORITY: Final = 1000

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._:@-]{0,199}$")
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_HOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,199}$")


def priority_for(owner_kind: str, default: int | None = None) -> int:
    """Lease priority of an owner kind (Bauplan §4); ``default`` for kinds outside the policy table."""
    if owner_kind in PRIORITIES:
        return PRIORITIES[owner_kind]
    if default is None:
        raise ValidationFailed(f"no default priority for owner kind '{owner_kind}'", code="RESOURCE_PRIORITY_UNKNOWN")
    return default


def validate_resource_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValidationFailed("invalid resource name (lowercase letters, digits, . _ : @ -; max 200)", code="RESOURCE_NAME_INVALID")
    return name


def validate_holder_id(holder: str) -> str:
    """Holder ids identify a process instance (stable across restarts, e.g. ``runtime@webui-223``)."""
    if not isinstance(holder, str) or not _HOLDER_RE.match(holder):
        raise ValidationFailed("invalid holder id (letters, digits, . _ : @ / + -; max 200)", code="RESOURCE_HOLDER_INVALID")
    return holder


def validate_owner_kind(kind: str) -> str:
    if not isinstance(kind, str) or not _KIND_RE.match(kind):
        raise ValidationFailed("invalid owner kind (lowercase letters, digits, . _ -; max 64)", code="RESOURCE_OWNER_KIND_INVALID")
    return kind


def validate_reason(reason: str) -> str:
    """Release/preemption reason codes are short machine-readable slugs; free text belongs into ``detail``."""
    if not isinstance(reason, str) or not _REASON_RE.match(reason):
        raise ValidationFailed("invalid reason code (lowercase slug, max 64)", code="RESOURCE_REASON_INVALID")
    return reason


def validate_priority(priority: int) -> int:
    if isinstance(priority, bool) or not isinstance(priority, int) or not MIN_PRIORITY <= priority <= MAX_PRIORITY:
        raise ValidationFailed(f"priority must be an int in [{MIN_PRIORITY}, {MAX_PRIORITY}]", code="RESOURCE_PRIORITY_INVALID")
    return priority
