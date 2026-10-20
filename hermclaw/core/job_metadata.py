"""Runtime-reserved keys of ``jobs.metadata`` (BUG-140).

``jobs.metadata`` holds two kinds of data:

* free-form keys a client passes as ``metadata`` on ``POST /api/jobs`` (stored, shown, never trusted), and
* state that the runtime and the API write themselves and later *trust*: operator SSH approvals (the admin-only
  trust anchor for host mutations), the fast-router triage, manual replan/retry requests, recovery markers,
  planner provenance, and the per-job flags that come only from the typed request fields.

A client must never be able to plant a key of the second kind, so every such key is declared here once, every writer
uses the constant, and ``POST /api/jobs`` rejects a request whose ``metadata`` contains one of them
(:func:`check_client_metadata`, HTTP 422). Keys are compared after Unicode NFKC normalisation, case folding and
removal of whitespace and invisible format characters, so ``" SSH_Approvals"`` is refused as well.

Keys starting with one of :data:`RESERVED_PREFIXES` are reserved for future runtime keys: a runtime key added under
such a prefix can never have been planted by a client, even on rows created before the key existed.

``tests/unit/test_job_metadata.py`` scans ``hermclaw/`` for job metadata writes and reads and fails when a key is not
reserved here, so a new runtime key cannot be forgotten. The scanned forms (including reads through a local name
loaded via ``select(Job.metadata_)``) are listed in ``docs/architecture/api.md`` §2.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from hermclaw.core.errors import ValidationFailed

# --- SSH operator approvals (hermclaw/sshtools/approvals.py; written only via the admin-scoped API) ---------------
SSH_APPROVALS = "ssh_approvals"
SSH_APPROVALS_CONSUMED = "ssh_approvals_consumed"
# --- runtime driver / scheduler ----------------------------------------------------------------------------------
TRIAGE = "triage"
REPLAN_REQUESTED = "replan_requested"
REPLAN_REASON = "replan_reason"
RETRY_REQUESTED = "retry_requested"
REPLAN_BUDGET_BASE = "replan_budget_base"
REPLAN_PROGRESS_MARK = "replan_progress_mark"
RECOVERED_PREPARE_FROM = "recovered_prepare_from"
# --- planner provenance (hermclaw/planner/) ----------------------------------------------------------------------
PLANNER_MODEL_ALIAS = "planner_model_alias"
PLANNER_MODEL_FALLBACK = "planner_model_fallback"
PLANNER_FALLBACK_ALIAS = "planner_fallback_alias"
LAST_REPLAN = "last_replan"
# --- per-job flags: set only from the typed JobCreate fields -----------------------------------------------------
ALLOW_NETWORK_RESEARCH = "allow_network_research"
AUTO_COMMIT = "auto_commit"
CREATE_MERGE_REQUEST = "create_merge_request"
# --- coder tournament (hermclaw/tournament/; set only by the operator CLI) ---------------------------------------
TOURNAMENT = "tournament"

RESERVED_KEYS: Mapping[str, str] = MappingProxyType(
    {
        SSH_APPROVALS: "operator approvals of mutating SSH commands (POST /api/jobs/{id}/ssh-approvals, admin)",
        SSH_APPROVALS_CONSUMED: "history of used SSH approvals with the approving operator",
        TRIAGE: "fast-router triage result (runtime driver)",
        REPLAN_REQUESTED: "manual replan request (POST /api/jobs/{id}/replan)",
        REPLAN_REASON: "reason of the running replan (scheduler, restart recovery)",
        RETRY_REQUESTED: "retry marker (POST /api/jobs/{id}/retry)",
        REPLAN_BUDGET_BASE: "replan_count at the last retry or progress; the replan budget counts from there",
        REPLAN_PROGRESS_MARK: "completed steps when the last replan started; more completed steps since grant a fresh budget",
        RECOVERED_PREPARE_FROM: "startup recovery marker (scheduler)",
        PLANNER_MODEL_ALIAS: "model alias that produced the current plan",
        PLANNER_MODEL_FALLBACK: "the technical fallback planner served",
        PLANNER_FALLBACK_ALIAS: "alias of the fallback planner that served",
        LAST_REPLAN: "version and reason code of the last replan",
        ALLOW_NETWORK_RESEARCH: "typed field allow_network_research of POST /api/jobs",
        AUTO_COMMIT: "typed field auto_commit of POST /api/jobs",
        CREATE_MERGE_REQUEST: "typed field create_merge_request of POST /api/jobs",
        TOURNAMENT: "coder tournament run: prescribed plan, coder alias, review and replan switches (CLI coder-tournament)",
    }
)
RESERVED_PREFIXES: tuple[str, ...] = ("_", "hermclaw.")
RESERVED_KEY_ERROR = "JOB_METADATA_RESERVED_KEY"


def normalise_key(key: str) -> str:
    """Comparison form of a metadata key: NFKC, case folded, without whitespace and invisible format characters."""
    folded = unicodedata.normalize("NFKC", key).casefold()
    return "".join(ch for ch in folded if not ch.isspace() and unicodedata.category(ch) != "Cf")


_NORMALISED_RESERVED = frozenset(normalise_key(k) for k in RESERVED_KEYS)
_NORMALISED_PREFIXES = tuple(normalise_key(p) for p in RESERVED_PREFIXES)


def is_reserved(key: str) -> bool:
    """True if ``key`` (in any case/whitespace spelling) belongs to the runtime."""
    norm = normalise_key(key)
    return norm in _NORMALISED_RESERVED or norm.startswith(_NORMALISED_PREFIXES)


def reserved_keys_in(keys: Iterable[str]) -> list[str]:
    """The reserved keys among ``keys``, as the client spelled them (sorted, for a stable error message)."""
    return sorted({k for k in keys if is_reserved(k)})


def check_client_metadata(metadata: Mapping[str, Any]) -> None:
    """Refuse client metadata that would plant a runtime-reserved key (``ValidationFailed`` → HTTP 422)."""
    offending = reserved_keys_in(metadata)
    if offending:
        raise ValidationFailed(
            f"metadata contains runtime-reserved key(s): {', '.join(repr(k) for k in offending)}",
            code=RESERVED_KEY_ERROR,
            details={"field": "metadata", "reserved_keys": offending, "reserved_prefixes": list(RESERVED_PREFIXES)},
        )


def describe_reserved() -> str:
    """One-line description of the reserved keys (JobCreate.metadata field description / OpenAPI)."""
    keys = ", ".join(sorted(RESERVED_KEYS))
    prefixes = " or ".join(f"'{p}'" for p in RESERVED_PREFIXES)
    return (
        "Free-form client keys (stored and returned, never trusted by the runtime). Runtime-reserved keys are "
        f"rejected with 422 {RESERVED_KEY_ERROR} (compared case- and whitespace-insensitively): {keys}. "
        f"Keys starting with {prefixes} are reserved as well."
    )


def replans_used(replan_count: int, metadata: Mapping[str, Any] | None) -> int:
    """Replans counted against the budget: a retry (POST /api/jobs/{id}/retry) grants a fresh budget (BUG-189)."""
    base = (metadata or {}).get(REPLAN_BUDGET_BASE, 0)
    return max(0, replan_count - (base if isinstance(base, int) else 0))


__all__ = [
    "ALLOW_NETWORK_RESEARCH",
    "AUTO_COMMIT",
    "CREATE_MERGE_REQUEST",
    "LAST_REPLAN",
    "PLANNER_FALLBACK_ALIAS",
    "PLANNER_MODEL_ALIAS",
    "PLANNER_MODEL_FALLBACK",
    "RECOVERED_PREPARE_FROM",
    "REPLAN_BUDGET_BASE",
    "REPLAN_PROGRESS_MARK",
    "REPLAN_REASON",
    "REPLAN_REQUESTED",
    "RESERVED_KEYS",
    "RESERVED_KEY_ERROR",
    "RESERVED_PREFIXES",
    "RETRY_REQUESTED",
    "SSH_APPROVALS",
    "SSH_APPROVALS_CONSUMED",
    "TOURNAMENT",
    "TRIAGE",
    "check_client_metadata",
    "describe_reserved",
    "is_reserved",
    "normalise_key",
    "replans_used",
    "reserved_keys_in",
]
