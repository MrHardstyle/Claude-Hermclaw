"""BUG-084: the environment the database tools set on migration commands passes the sandbox env check – also when an
operator overrides ``policies.sandbox.env_allowlist`` (the model itself can never set environment variables)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermclaw.core.config import RUNTIME_SANDBOX_ENV, SandboxPolicy
from hermclaw.core.errors import PolicyViolation
from worker.execution.sandbox import validate_request_env

MIGRATION_ENV = {
    "DATABASE_URL": "postgresql://sbx@127.0.0.1:5432/hermclaw_sbx_1",
    "HERMCLAW_SANDBOX_URL": "postgresql+psycopg://sbx@127.0.0.1:5432/hermclaw_sbx_1",
    "SQLALCHEMY_DATABASE_URI": "postgresql+psycopg://sbx@127.0.0.1:5432/hermclaw_sbx_1",
    "PGHOST": "127.0.0.1",
    "PGPORT": "5432",
    "PGUSER": "sbx",
    "PGPASSWORD": "s3cret-sandbox",
    "PGDATABASE": "hermclaw_sbx_1",
    "HERMCLAW_ALEMBIC_INI": "/workspace/alembic.ini",
    "HERMCLAW_ALEMBIC_SCRIPT_LOCATION": "/workspace/migrations",
    "HERMCLAW_ALEMBIC_TOML": "/workspace/pyproject.toml",
    "PYTHONDONTWRITEBYTECODE": "1",
}


@pytest.mark.parametrize("allowlist", [None, ["PATH"], []])
def test_migration_env_is_accepted_by_the_sandbox(allowlist: list[str] | None) -> None:
    policy = SandboxPolicy() if allowlist is None else SandboxPolicy(env_allowlist=allowlist)
    assert validate_request_env(MIGRATION_ENV, policy.request_env_allowlist) == MIGRATION_ENV


def test_other_names_stay_refused_and_dbtools_sets_only_runtime_names() -> None:
    with pytest.raises(PolicyViolation):
        validate_request_env({"LD_PRELOAD": "/tmp/x.so"}, SandboxPolicy().request_env_allowlist)
    import re

    root = Path(__file__).resolve().parents[2] / "hermclaw" / "dbtools"
    names = set()
    for f in root.glob("*.py"):
        names |= set(re.findall(r'"(HERMCLAW_[A-Z_]+|DATABASE_URL|SQLALCHEMY_DATABASE_URI|PG[A-Z]+)"', f.read_text()))
    policy = SandboxPolicy(env_allowlist=["PATH"])
    validate_request_env(dict.fromkeys(sorted(names), "x"), policy.request_env_allowlist)
    assert names >= {"DATABASE_URL", "HERMCLAW_SANDBOX_URL"} and "PGPASSWORD" in RUNTIME_SANDBOX_ENV


def test_image_safety_issues() -> None:
    import json

    from worker.execution.sandbox import CHECKPOINT_ANNOTATION, image_safety_issues

    safe = [{"Config": {"Env": ["PATH=/usr/bin", "LANG=C.UTF-8"], "Labels": {"maintainer": "x"}}, "Annotations": {}}]
    assert image_safety_issues(json.dumps(safe)) == []
    bare = [{"Config": {"Env": ["PATH=/usr/bin", "AWS_SECRET_ACCESS_KEY", "*"]}}]
    [issue] = image_safety_issues(json.dumps(bare))
    assert "AWS_SECRET_ACCESS_KEY" in issue and "*" in issue
    assert image_safety_issues(json.dumps([{"Config": {}, "Annotations": {CHECKPOINT_ANNOTATION: "runc"}}]))
    assert image_safety_issues(json.dumps([{"Config": {"Labels": {CHECKPOINT_ANNOTATION: "runc"}}}]))
    assert image_safety_issues("not json") == ["unreadable image metadata"]
    assert image_safety_issues(json.dumps([{"Config": None, "Annotations": None}])) == []
