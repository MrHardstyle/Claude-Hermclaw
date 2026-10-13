"""P36 security checks that span components: secret scan of the Hermclaw repository itself (36.1), API and worker
API permissions on the assembled app (36.2/36.3), secret reference policy (36.2). Component-level security tests
(sandbox escape, network policy, command policy, SSRF guard, signed worker requests) are mapped in TEST_MATRIX.md."""

from __future__ import annotations

import os
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from hermclaw.core.errors import ConfigError
from hermclaw.persistence.models import ApiToken
from hermclaw.security.secrets import SecretStore
from hermclaw.security.tokens import generate_token, hash_token
from hermclaw.verifier.secrets import SecretScanner
from hermclaw.verifier.types import AddedLine
from tests.integration.test_api import _client, app  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2]
# test code carries deliberately fake credentials to prove redaction; they are scanned by the tests themselves
SKIP_PREFIXES = ("tests/", "ui/node_modules/", "ui/dist/")
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".sh",
    ".j2",
    ".service",
    ".timer",
    ".ts",
    ".tsx",
    ".json",
    ".txt",
    ".conf",
    "",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode()
    return [p for p in out.split("\0") if p and not p.startswith(SKIP_PREFIXES)]


def test_repository_contains_no_secrets() -> None:
    scanner = SecretScanner()
    findings = []
    for rel in _tracked_files():
        path = ROOT / rel
        if path.suffix not in TEXT_SUFFIXES or not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        found = scanner.scan_file(rel, [AddedLine(i + 1, t) for i, t in enumerate(lines)])
        # the redaction marker itself is only suspicious inside a job diff (verifier), not in docs/code about it
        findings += [f for f in found if f.rule != "redacted_secret"]
    assert findings == [], "\n".join(f"{f.path}:{f.line} {f.rule}" for f in findings[:30])


async def test_worker_api_views_need_admin_scope_and_heartbeats_a_signature(app: Any, sessionmaker: Any) -> None:  # noqa: F811
    read_token = generate_token()
    async with sessionmaker() as s:
        s.add(ApiToken(name=f"ro-{uuid.uuid4().hex[:6]}", token_hash=hash_token(read_token), scopes=["read", "control"]))
        await s.commit()
    async with _client(app, token=None) as c:
        assert (await c.get("/api/workers")).status_code == 401
        hb = await c.post("/api/workers/heartbeat", content=b'{"worker_id":"exec-222"}')
        assert hb.status_code == 401, "unsigned heartbeat rejected (bearer user tokens are not worker credentials)"
    async with _client(app, token=read_token) as c:
        assert (await c.get("/api/workers")).status_code == 200, "UI worker list (system API) is readable"
        assert (await c.get("/api/workers/exec-222")).status_code == 403, "registry detail view needs admin"
        assert (await c.post("/api/workers/exec-222/drain", json={"drain": True})).status_code == 403, "drain needs admin"
    async with _client(app) as c:  # admin token
        r = await c.get("/api/workers")
        assert r.status_code == 200 and isinstance(r.json(), list)


def test_secret_references_policy_in_production(tmp_path: Path) -> None:
    store = SecretStore(env="production", secrets_dir=tmp_path)
    loose = tmp_path / "token"
    loose.write_text("s3cr3t-value-for-test\n")
    os.chmod(loose, 0o644)
    with pytest.raises(ConfigError) as exc:
        store.resolve(f"file:{loose}")
    assert exc.value.code == "SECRET_PERMISSIONS"
    os.chmod(loose, stat.S_IRUSR)
    assert store.resolve(f"file:{loose}") == "s3cr3t-value-for-test"
    for ref in ("env:HOME", "literal:abc"):
        with pytest.raises(ConfigError) as exc:
            store.resolve(ref)
        assert exc.value.code == "SECRET_POLICY"


async def test_api_errors_never_echo_tokens(app: Any) -> None:  # noqa: F811
    secret = "glpat-" + "A1b2C3d4E5f6G7h8I9j0"
    async with _client(app, token=secret) as c:
        r = await c.get("/api/jobs")
    assert r.status_code == 401 and secret not in r.text
