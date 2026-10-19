"""Isolation probe for tests that keep failing after code changes (BUG-291).

Live cycle 2 M1 on 0.1.0rc60: two HTTP tests expected two tasks and found four, because every test of the class added
tasks to one shared SQLite file. Each test passed when run alone; the coder changed code and expected values for four
attempts and three plan versions without ever finding that out, and the job failed with REPLAN_LIMIT. The runtime now
runs the first failing test on its own once the same tests keep failing; when it passes alone, the coder is told that
the tests share state.
"""

from __future__ import annotations

import re
import shlex

from hermclaw.stagnation.fingerprints import extract_failing_tests

# the pytest launcher at the start of the coder's own command (``pytest`` or ``python -m pytest``)
_PYTEST_LAUNCHER = re.compile(r"^\s*((?:[\w./-]*python[\d.]*\s+-m\s+)?pytest)(?=\s|$)")
# a pytest node id: a file path, then ``::`` names (parametrised ids may carry brackets)
_NODE_ID = re.compile(r"^[\w./-]+\.py(?:::[\w.\[\]-]+)+$")

ISOLATION_PASSED = (
    "Isolation check by the runtime: '{test}' passes when it runs alone ({command}) but fails in the full run. The "
    "tests share state that earlier tests change (a database file, a global or module variable, a running server, "
    "files). Give every test its own fresh state (create and remove it in setUp/tearDown or a fixture) instead of "
    "changing the expected values or the code under test."
)


def isolation_probe(command: str, output: str) -> tuple[str, str] | None:
    """``(test_id, command)`` running the first failing pytest test alone, or ``None`` when no probe applies."""
    launcher = _PYTEST_LAUNCHER.match(command)
    if launcher is None:
        return None
    for test_id in extract_failing_tests(output):
        # a run of that single test already is the isolated run
        if _NODE_ID.match(test_id) and test_id not in command:
            return test_id, f"{launcher.group(1)} -q -p no:cacheprovider {shlex.quote(test_id)}"
    return None
