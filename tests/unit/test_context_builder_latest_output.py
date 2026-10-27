"""The latest successful output in LATEST FAILURE (BUG-363)."""

from __future__ import annotations

from hermclaw.context_builder.render import render_failure
from hermclaw.context_builder.tokens import char_cost

RECALL = "recall (turn 4)\n" + "\n".join(f"{n}. [research] Unraid GraphQL field {n}: arrays, disks, vms" for n in range(1, 40))
FAILURE = "error TESTS_FAILED: unittest: failed – 7 passed, 1 failed\nAssertionError: '1h 1m 1s' != '1h 1m'"


def test_the_output_is_shown_whole_and_titled_as_a_success() -> None:
    sec = render_failure(None, (), 4000, item_chars=400, latest_output=RECALL)
    assert sec.present and sec.items_included == 1 and not sec.truncated
    assert sec.body.startswith("Full output of your latest successful recall (turn 4)")
    assert "field 39: arrays" in sec.body and "Exact output of the latest failure" not in sec.body


def test_the_failure_keeps_priority_and_the_output_its_head() -> None:
    sec = render_failure(FAILURE, (), 900, item_chars=400, latest_output=RECALL)
    assert sec.body.index("Exact output of the latest failure") < sec.body.index("Full output of your latest successful")
    assert "'1h 1m 1s' != '1h 1m'" in sec.body, "the failure is whole"
    assert "field 1: arrays" in sec.body and "field 39" not in sec.body and sec.truncated
    assert char_cost(sec.body) <= 900


def test_no_output_and_no_failure_is_an_empty_section() -> None:
    assert render_failure(None, (), 4000, item_chars=400, latest_output="recall (turn 2)\n").omitted_reason == "empty"
    tight = render_failure(FAILURE, (), 260, item_chars=400, latest_output=RECALL)
    assert "Full output" not in tight.body and ("latest successful output", "budget") in tight.dropped
