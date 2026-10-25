"""BUG-354: research facts reach a step with the ones about its own subject first."""

from __future__ import annotations

from hermclaw.research.findings import facts_block, relevant_first

FACTS = [
    "The command 'unraid-api start' accepts a --log-level option. (S5)",
    "LOG_LEVEL=trace unraid-api start sets the log level. (S5)",
    "API tokens set the HTTP Authorization header to PVEAPIToken=USER@REALM!TOKENID=UUID for Proxmox. (S1)",
    "Node status of Proxmox comes from /nodes/{node}/status. (S1)",
]


def test_facts_about_the_steps_subject_come_first_in_stored_order() -> None:
    """Live G1 on rc90: the Proxmox parser step got the Unraid CLI's log options before the Proxmox token header."""
    ranked = relevant_first(FACTS, "Implement Proxmox collector parser\nParse the Proxmox node status and token auth.")
    assert set(ranked[:2]) == {FACTS[2], FACTS[3]} and ranked[2:] == [FACTS[0], FACTS[1]], "Unraid facts keep their order"
    assert relevant_first(FACTS, "") == FACTS, "without a step text the stored order stays"
    block = facts_block("S001", "Research", ranked, max_chars=490)
    assert "PVEAPIToken" in block and "more facts not shown" in block and "--log-level" not in block
