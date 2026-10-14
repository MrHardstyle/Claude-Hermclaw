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


def test_openapi_status_phrases_are_interpreter_independent() -> None:
    """CPython 3.13 renamed HTTPStatus phrases (RFC 9110); the exported document uses the RFC 9110 phrase on 3.12
    and 3.13 alike, so docs/contracts/openapi.json matches on the production interpreter (research R-027 Y4)."""
    from hermclaw.contracts.schema_export import _normalise_status_phrases

    doc = {
        "paths": {
            "/x": {
                "post": {
                    "responses": {
                        "422": {"description": "Unprocessable Entity"},
                        "413": {"description": "Content Too Large"},
                        "400": {"description": "Unprocessable Entity"},
                    }
                }
            }
        }
    }
    out = _normalise_status_phrases(doc)["paths"]["/x"]["post"]["responses"]
    assert out["422"]["description"] == "Unprocessable Content" and out["413"]["description"] == "Content Too Large"
    assert out["400"]["description"] == "Unprocessable Entity", "only the status code's own phrase is rewritten"
