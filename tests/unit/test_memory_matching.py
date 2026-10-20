"""D-019: which knowledge entries match the error lines of a failed run (scores measured with embeddinggemma)."""

from __future__ import annotations

import uuid

from hermclaw.memory.store import MemoryHit, distinctive_names, failure_matches

PESTER = "Pester on Linux: Windows cmdlets such as Get-Service do not exist", "`Mock Get-Service` fails with CommandNotFoundException"
IMPORT = "Python: an import inside a branch makes the name local", "`import sys` inside an `if` block raises UnboundLocalError"


def _hit(entry: tuple[str, str], score: float) -> MemoryHit:
    return MemoryHit(uuid.uuid4(), "lesson", entry[0], entry[1], "active", None, score)


def test_distinctive_names_leave_out_what_every_failure_log_says() -> None:
    names = distinctive_names(
        "CommandNotFoundException: Could not find Command Get-Service\nAssertionError in test_login (NOTES_API_TOKEN)"
    )
    assert names == {"CommandNotFoundException", "Get-Service", "NOTES_API_TOKEN"}


def test_a_failure_below_the_threshold_matches_only_by_a_shared_identifier() -> None:
    pester = "CommandNotFoundException: Could not find Command Get-Service"
    assert [h.title for h in failure_matches([_hit(PESTER, 0.50), _hit(IMPORT, 0.31)], pester, 0.55)] == [PESTER[0]]
    missing = "ImportError while importing test module\nModuleNotFoundError: No module named 'requests'"
    assert failure_matches([_hit(IMPORT, 0.54), _hit(PESTER, 0.38)], missing, 0.55) == []
    unbound = "UnboundLocalError: cannot access local variable 'sys'"
    assert [h.title for h in failure_matches([_hit(IMPORT, 0.59)], unbound, 0.55)] == [IMPORT[0]]
    assert failure_matches([_hit(PESTER, 0.40)], pester, 0.55) == []  # an identifier does not lift a far entry
