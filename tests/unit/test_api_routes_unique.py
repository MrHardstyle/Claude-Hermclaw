"""BUG-058: every route of the Hermclaw API is registered once (no duplicate operation ids, e.g. GET /api/workers)."""

from __future__ import annotations

import warnings
from collections import Counter

from hermclaw.api.app import create_app


def test_operation_ids_and_routes_are_unique() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # FastAPI warns "Duplicate Operation ID" for doubly registered routes
        spec = create_app().openapi()
    ops = [op["operationId"] for path in spec["paths"].values() for op in path.values()]
    assert [o for o, n in Counter(ops).items() if n > 1] == []


def test_worker_list_is_the_read_scope_system_route() -> None:
    spec = create_app().openapi()
    assert list(spec["paths"]["/api/workers"]) == ["get"]
    assert spec["paths"]["/api/workers"]["get"]["tags"] == ["system"]  # hermclaw.api.system (read scope), not the admin list
    assert "get" in spec["paths"]["/api/workers/{worker_id}"]  # worker detail/drain still come from the workers router
