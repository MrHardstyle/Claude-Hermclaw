"""Parsing and schema validation of planner answers (P14 14.2/14.3).

Only the final ``content`` of a model answer is ever parsed. Reasoning text is never returned by the gateway and
is never interpreted as a plan; an empty answer is a validation error that triggers a repair turn.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from hermclaw.planner.errors import PlanInvalid

MAX_ERROR_CHARS = 400


def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        s = s[first_newline + 1 :] if first_newline != -1 else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def extract_json_object(content: str, *, reasoning_chars: int = 0) -> dict[str, Any]:
    """Parse the JSON object of a model answer; tolerates code fences and surrounding prose."""
    if not content or not content.strip():
        hint = " (the model produced only hidden reasoning; reasoning is never accepted as a plan)" if reasoning_chars else ""
        raise PlanInvalid("parse", [f"empty answer: no JSON object found{hint}. Answer with the JSON object only."])
    text = _strip_fences(content)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as first:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise PlanInvalid("parse", [f"invalid JSON: {first.msg} at line {first.lineno} column {first.colno}"]) from first
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as second:
            raise PlanInvalid("parse", [f"invalid JSON: {second.msg} at line {second.lineno} column {second.colno}"]) from second
    if not isinstance(value, dict):
        raise PlanInvalid("parse", [f"top-level JSON value must be an object, got {type(value).__name__}"])
    return value


def _loc(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "plan"


def format_validation_errors(exc: ValidationError, data: dict[str, Any] | None = None, *, limit: int = 30) -> list[str]:
    """Stable, model-readable error list: ``steps[2](S003).acceptance[0]: <message>``."""
    errors: list[str] = []
    steps = data.get("steps") if isinstance(data, dict) else None
    for err in exc.errors(include_url=False):
        loc = tuple(err.get("loc", ()))
        where = _loc(loc)
        if len(loc) >= 2 and loc[0] == "steps" and isinstance(loc[1], int) and isinstance(steps, list) and loc[1] < len(steps):
            step = steps[loc[1]]
            if isinstance(step, dict) and isinstance(step.get("id"), str):
                where = where.replace(f"steps[{loc[1]}]", f"steps[{loc[1]}]({step['id'][:12]})", 1)
        msg = str(err.get("msg", "invalid"))
        if err.get("type") in ("extra_forbidden",):
            msg = "unknown field (not allowed by the schema)"
        line = f"{where}: {msg}"
        if line not in errors:
            errors.append(line[:MAX_ERROR_CHARS])
        if len(errors) >= limit:
            break
    return errors


def validate_schema[M: BaseModel](model: type[M], data: dict[str, Any], *, limit: int = 30) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise PlanInvalid("schema", format_validation_errors(exc, data, limit=limit)) from exc
