"""Committed contract documents match the code (``hermclaw schemas docs/contracts/schemas`` regenerates them)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermclaw.contracts.schema_export import CONTRACTS, openapi_document, render_schema

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "contracts"


def test_openapi_document_is_current() -> None:
    committed = json.loads((DOCS / "openapi.json").read_text(encoding="utf-8"))
    assert committed == openapi_document(), "docs/contracts/openapi.json is stale – run: hermclaw schemas docs/contracts/schemas"


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_contract_schema_is_current(name: str) -> None:
    path = DOCS / "schemas" / f"{name}.schema.json"
    assert path.is_file(), f"{path} missing – run: hermclaw schemas docs/contracts/schemas"
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(render_schema(name)), f"{path.name} is stale"
