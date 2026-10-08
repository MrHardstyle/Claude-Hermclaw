"""Export JSON schemas for all public contracts (P13.12): ``python -m hermclaw.contracts.schema_export DIR``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
    for name, model in CONTRACTS.items():
        path = directory / f"{name}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/contracts/schemas")
    for p in export(out):
        print(p)
