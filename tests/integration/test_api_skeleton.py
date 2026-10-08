import pytest
from httpx import ASGITransport, AsyncClient

from hermclaw.api.app import create_app

pytestmark = pytest.mark.integration


async def test_health_and_version(engine):
    app = create_app(lifespan_hooks=False)
    from hermclaw.api.state import AppState

    app.state.hermclaw = AppState()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        v = (await c.get("/api/version")).json()
        h = (await c.get("/api/health")).json()
    assert v["version"] and v["protocol_version"] == 1
    assert h["status"] == "ok" and h["database"]["pgvector"]
    assert h["database"]["schema_revision"] == "0001"
