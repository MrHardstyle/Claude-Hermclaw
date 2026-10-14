"""Event type contract (BUG-063): every type the runtime emits is an ``EventType`` member and has a live SSE listener
in the UI (``ui/src/lib/events.ts`` – types without one only arrive through the REST reconciliation)."""

from __future__ import annotations

import re
from pathlib import Path

from hermclaw.contracts.events import EventType

ROOT = Path(__file__).resolve().parents[2]
UI_ONLY = {"message"}  # the SSE default event name


def contract_types() -> set[str]:
    return {v for k, v in vars(EventType).items() if k.isupper() and isinstance(v, str)}


def test_event_type_values_are_unique_and_dotted() -> None:
    members = [v for k, v in vars(EventType).items() if k.isupper() and isinstance(v, str)]
    assert len(members) == len(set(members))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*", v) for v in members)


def test_runtime_emits_only_contract_types() -> None:
    literal = re.compile(r"append_event\(\s*\w+,\s*f?\"([a-z0-9_.{}]+)\"", re.S)
    found: set[str] = set()
    for path in (ROOT / "hermclaw").rglob("*.py"):
        found |= set(literal.findall(path.read_text(encoding="utf-8")))
    expanded = {f.replace("{action}", a) for f in found for a in ("cancel", "pause", "resume", "retry", "replan")}
    assert expanded - contract_types() == set(), "event types emitted as literals but missing in EventType"


def test_ui_listens_to_every_contract_type() -> None:
    src = (ROOT / "ui" / "src" / "lib" / "events.ts").read_text(encoding="utf-8")
    block = src.split("KNOWN_EVENT_TYPES", 1)[1].split("];", 1)[0]
    ui = set(re.findall(r"\"([a-z0-9_.]+)\"", block))
    assert contract_types() - ui == set(), "UI has no live listener for these event types"
    assert ui - contract_types() - UI_ONLY == set(), "UI lists event types the runtime never emits"
