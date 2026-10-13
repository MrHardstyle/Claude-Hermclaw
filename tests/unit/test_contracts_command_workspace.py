"""``CommandRequest.workspace`` carries the execution worker's workspace-id pattern itself (defense in depth: the
daemon's ``workspace_dir`` and the client's ``_workspace_segment`` enforce the same rule)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from hermclaw.contracts.worker import CommandRequest
from hermclaw.workers.schemas import WORKSPACE_ID_PATTERN


def _req(workspace: str) -> CommandRequest:
    return CommandRequest(request_id="r1", job_id="j", step_id="s", workspace=workspace, command="true")


@pytest.mark.parametrize(
    "workspace", ["/", "..", ".", "../data", "/etc", "ws1/../..", ".incoming-x", "a/b", "", "-x", "_x", "a b", "x" * 129]
)
def test_unsafe_workspace_ids_do_not_validate(workspace: str) -> None:
    with pytest.raises(ValidationError, match="workspace"):
        _req(workspace)


@pytest.mark.parametrize(
    "workspace", ["ws", "w", "ws1", "selftest-0123456789ab", uuid.uuid4().hex, str(uuid.uuid4()), "a.b_c-d", "x" * 128]
)
def test_valid_workspace_ids_validate(workspace: str) -> None:
    assert _req(workspace).workspace == workspace


def test_runtime_callers_produce_valid_ids() -> None:
    """The ids the orchestrator sends: ``str(handle.id)`` (sandbox executor) and ``handle.id.hex`` (remote
    executor)."""
    ws_id = uuid.uuid4()
    assert _req(str(ws_id)).workspace == str(ws_id)
    assert _req(ws_id.hex).workspace == ws_id.hex


def test_pattern_matches_the_worker_schema_pattern() -> None:
    field = CommandRequest.model_fields["workspace"]
    patterns = [getattr(m, "pattern", None) for m in field.metadata]
    assert WORKSPACE_ID_PATTERN in patterns
    assert CommandRequest.model_json_schema()["properties"]["workspace"]["pattern"] == WORKSPACE_ID_PATTERN
