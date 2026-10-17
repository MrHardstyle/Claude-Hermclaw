"""Finalize: which git failures are a network problem that a later finalize run overcomes (BUG-199)."""

from __future__ import annotations

from hermclaw.gitops.errors import GitCommandError, NothingToPushError
from hermclaw.runtime.driver import _network_failure


def test_network_failures_are_recognised_and_others_are_not() -> None:
    for stderr in (
        "fatal: unable to access 'https://gitlab/x.git/': Could not resolve host: gitlab",
        "fatal: the remote end hung up unexpectedly",
        "error: RPC failed; HTTP 502 curl 22 The requested URL returned error: 502",
        "ssh: connect to host gitlab port 22: Connection refused",
    ):
        assert _network_failure(GitCommandError("git push failed (exit 128)", details={"stderr": stderr})), stderr
    assert not _network_failure(GitCommandError("git push failed (exit 1)", details={"stderr": "error: src refspec x does not match any"}))
    assert not _network_failure(NothingToPushError("nothing to push", details={"stderr": "Could not resolve host"}))
