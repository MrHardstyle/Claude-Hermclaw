"""Retention of backup sets (keep N daily / weekly / monthly, grandfather-father-son).

Pure planning over the target's listing (set time = the UTC timestamp in the set id):

* the newest ``keep_last`` complete sets are always kept;
* daily: the newest complete set of each of the ``keep_daily`` most recent days that have a backup;
* weekly: the newest complete set of each of the ``keep_weekly`` most recent ISO weeks that have a backup;
* monthly: likewise for ``keep_monthly`` calendar months;
* incomplete sets (no manifest / ``.partial``) are removed once older than ``stale_partial_hours`` – younger ones may
  be a transfer in progress and are kept;
* everything else is deleted. A set is never deleted when it is in ``protect`` (e.g. the set just created).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from hermclaw.backup.config import RetentionPolicy
from hermclaw.backup.transfer import SetListing


@dataclass
class RetentionPlan:
    keep: list[SetListing] = field(default_factory=list)
    delete: list[SetListing] = field(default_factory=list)
    reasons: dict[str, list[str]] = field(default_factory=dict)  # set id (+.partial) -> why kept / deleted

    def to_dict(self) -> dict[str, object]:
        return {
            "keep": [s.set_id for s in self.keep],
            "delete": [_key(s) for s in self.delete],
            "reasons": self.reasons,
        }


def _key(listing: SetListing) -> str:
    return listing.set_id + (".partial" if listing.partial_dir else "")


def _bucket_keep(
    sets: list[SetListing], count: int, bucket: Callable[[datetime], Hashable], label: str, reasons: dict[str, list[str]]
) -> set[str]:
    kept: set[str] = set()
    seen: list[Hashable] = []
    for listing in sets:  # newest first
        if len(seen) >= count:
            break
        key = bucket(listing.created_at)
        if key in seen:
            continue
        seen.append(key)
        kept.add(listing.set_id)
        reasons.setdefault(listing.set_id, []).append(f"{label} {key}")
    return kept


def plan_retention(
    listings: Iterable[SetListing],
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
    protect: Iterable[str] = (),
) -> RetentionPlan:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    protected = set(protect)
    plan = RetentionPlan()
    reasons = plan.reasons
    all_sets = list(listings)
    complete = sorted((s for s in all_sets if s.complete and not s.partial_dir), key=lambda s: (s.created_at, s.set_id), reverse=True)
    keep_ids: set[str] = set()
    for listing in complete[: policy.keep_last]:
        keep_ids.add(listing.set_id)
        reasons.setdefault(listing.set_id, []).append("keep_last")
    keep_ids |= _bucket_keep(complete, policy.keep_daily, lambda t: t.date().isoformat(), "daily", reasons)
    keep_ids |= _bucket_keep(complete, policy.keep_weekly, lambda t: "{}-W{:02d}".format(*t.isocalendar()[:2]), "weekly", reasons)
    keep_ids |= _bucket_keep(complete, policy.keep_monthly, lambda t: f"{t:%Y-%m}", "monthly", reasons)
    for listing in complete:
        if listing.set_id in protected and listing.set_id not in keep_ids:
            keep_ids.add(listing.set_id)
            reasons.setdefault(listing.set_id, []).append("protected")
        if listing.set_id in keep_ids:
            plan.keep.append(listing)
        else:
            plan.delete.append(listing)
            reasons[listing.set_id] = ["outside retention"]
    stale_before = current - timedelta(hours=policy.stale_partial_hours)
    for listing in sorted((s for s in all_sets if not s.complete or s.partial_dir), key=lambda s: s.created_at):
        key = _key(listing)
        if listing.set_id in protected or listing.created_at >= stale_before:
            reasons[key] = ["incomplete, may still be in progress"]
            continue
        if listing.set_id in keep_ids:  # a stale '.partial' twin of a complete set: remove the leftover only
            reasons[key] = ["stale leftover of a complete set"]
        else:
            reasons[key] = ["stale incomplete set"]
        plan.delete.append(listing)
    return plan
