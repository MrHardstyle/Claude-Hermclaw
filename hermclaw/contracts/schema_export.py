"""Export JSON schemas for all public contracts (P13.12) and the OpenAPI document of the API.

``hermclaw schemas docs/contracts/schemas`` writes both (``docs/contracts/openapi.json`` next to the schema directory);
``tests/unit/test_contract_docs.py`` fails when the committed documents are stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from hermclaw.contracts.acceptance import AcceptanceList
from hermclaw.contracts.artifact import ArtifactContract
from hermclaw.contracts.events import EventEnvelope
from hermclaw.contracts.job import JobContract, JobCreate
from hermclaw.contracts.plan import PlanContract
from hermclaw.contracts.research import ResearchContract
from hermclaw.contracts.review import ReviewContract
from hermclaw.contracts.scope import ScopeContract, ScopeExpansionRequest
from hermclaw.contracts.step import StepContract
from hermclaw.contracts.tools import CoderAction, ToolCall, ToolResult
from hermclaw.contracts.verification import VerificationReport
from hermclaw.contracts.worker import CommandRequest, CommandResult, WorkerHeartbeat, WorkerInput, WorkerResult

CONTRACTS: dict[str, type[BaseModel]] = {
    "JobCreate": JobCreate,
    "JobContract": JobContract,
    "PlanContract": PlanContract,
    "StepContract": StepContract,
    "ScopeContract": ScopeContract,
    "ScopeExpansionRequest": ScopeExpansionRequest,
    "WorkerInput": WorkerInput,
    "WorkerResult": WorkerResult,
    "WorkerHeartbeat": WorkerHeartbeat,
    "CommandRequest": CommandRequest,
    "CommandResult": CommandResult,
    "ToolCall": ToolCall,
    "ToolResult": ToolResult,
    "CoderAction": CoderAction,
    "VerificationReport": VerificationReport,
    "AcceptanceList": AcceptanceList,
    "ReviewContract": ReviewContract,
    "ResearchContract": ResearchContract,
    "ArtifactContract": ArtifactContract,
    "EventEnvelope": EventEnvelope,
}


def export(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name in CONTRACTS:
        path = directory / f"{name}.schema.json"
        path.write_text(render_schema(name), encoding="utf-8")
        written.append(path)
    return written


#: CPython 3.13 renamed these ``HTTPStatus`` phrases per RFC 9110 (3.12 → 3.13); FastAPI uses the phrase as the
#: default response description. Normalising to the RFC 9110 phrase keeps the document identical on the build
#: interpreter (3.12) and the production interpreter (Debian 13: 3.13) – research 20261014-027 Y4.
_RFC9110_PHRASES: dict[str, tuple[str, str]] = {
    "413": ("Request Entity Too Large", "Content Too Large"),
    "414": ("Request-URI Too Long", "URI Too Long"),
    "416": ("Requested Range Not Satisfiable", "Range Not Satisfiable"),
    "422": ("Unprocessable Entity", "Unprocessable Content"),
}


def _normalise_status_phrases(doc: dict[str, Any]) -> dict[str, Any]:
    for item in (doc.get("paths") or {}).values():
        for op in item.values() if isinstance(item, dict) else ():
            for code, resp in ((op or {}).get("responses") or {}).items() if isinstance(op, dict) else ():
                old_new = _RFC9110_PHRASES.get(str(code))
                if old_new and isinstance(resp, dict) and resp.get("description") in old_new:
                    resp["description"] = old_new[1]
    return doc


def openapi_document() -> dict[str, object]:
    """The OpenAPI document of the Hermclaw API as served by ``create_app()`` (docs/contracts/openapi.json)."""
    from hermclaw.api.app import create_app

    return _normalise_status_phrases(create_app().openapi())


def export_openapi(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(openapi_document(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def render_schema(name: str) -> str:
    return json.dumps(CONTRACTS[name].model_json_schema(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/contracts/schemas")
    for p in export(out):
        print(p)
