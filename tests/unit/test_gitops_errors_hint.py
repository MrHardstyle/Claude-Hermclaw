"""``push_stderr_hint`` (BUG-152): the server's reason for a refused push, short and redacted."""

from __future__ import annotations

from hermclaw.core.redaction import REDACTED
from hermclaw.gitops.engine import push_stderr_hint

GITLAB_DEPLOY_KEY = """remote:
remote: ========================================================================
remote:
remote: This deploy key does not have write access to this project.
remote:
remote: ========================================================================
remote:
fatal: Could not read from remote repository.

Please make sure you have the correct access rights
and the repository exists.
"""


def test_remote_lines_win_over_git_fatal() -> None:
    assert push_stderr_hint(GITLAB_DEPLOY_KEY) == "This deploy key does not have write access to this project."


def test_fatal_line_without_remote_text_and_german_locale() -> None:
    assert push_stderr_hint("fatal: unable to access 'x': Connection refused\nmore\n") == "fatal: unable to access 'x': Connection refused"
    german = "Schwerwiegend: Konnte nicht vom Remote-Repository lesen.\n\nBitte stellen Sie sicher ..."
    assert push_stderr_hint(german) == "Schwerwiegend: Konnte nicht vom Remote-Repository lesen."
    assert push_stderr_hint("") is None and push_stderr_hint("remote:\nremote: =====\n") is None


def test_hint_is_redacted_and_bounded() -> None:
    secret = "glpat-" + "A1b2C3d4E5f6G7h8I9j0"
    hint = push_stderr_hint(f"remote: token {secret} rejected\nremote: " + "x" * 1000)
    assert hint is not None and secret not in hint and REDACTED in hint and len(hint) <= 300
