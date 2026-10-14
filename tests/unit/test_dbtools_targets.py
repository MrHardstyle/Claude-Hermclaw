"""Database targets (P27): configuration, secret references, DSN parsing and credential hygiene."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from hermclaw.core.errors import ConfigError
from hermclaw.core.redaction import Redactor
from hermclaw.dbtools.errors import DB_TARGET_INVALID, DB_TARGET_UNKNOWN, DbToolError
from hermclaw.dbtools.targets import (
    DatabaseTarget,
    DbToolsConfig,
    TargetEnvironment,
    TargetKind,
    TargetRegistry,
    load_dbtools_config,
    parse_dsn,
    redact_dsn,
    resolve_connection,
)
from hermclaw.security.secrets import SecretStore

PW = "p@ss:w/rd%41"


def test_url_dsn_is_parsed_with_quoted_credentials() -> None:
    conn = parse_dsn(
        "postgresql://shop%40reader:p%40ss%3Aw%2Frd%2541@db.lan:6432/shop?sslmode=require&application_name=x", TargetKind.postgresql
    )
    assert (conn.host, conn.port, conn.user, conn.password, conn.database) == ("db.lan", 6432, "shop@reader", PW, "shop")
    assert dict(conn.params) == {"application_name": "x", "sslmode": "require"}
    env = conn.libpq_env()
    assert env["PGPASSWORD"] == PW and env["PGSSLMODE"] == "require" and env["PGDATABASE"] == "shop"
    assert PW not in repr(conn) and PW not in conn.display() and PW not in str(conn)
    assert conn.display() == "postgresql://shop@reader@db.lan:6432/shop"
    # the URL round-trips (used for DATABASE_URL of migration commands)
    again = parse_dsn(conn.url(), TargetKind.postgresql)
    assert (again.user, again.password, again.host, again.port, again.database) == (
        conn.user,
        conn.password,
        conn.host,
        conn.port,
        conn.database,
    )


def test_keyword_dsn_and_unix_socket() -> None:
    conn = parse_dsn("host=/run/postgresql dbname=app user=app password='a b c' sslmode=disable", TargetKind.postgresql)
    assert conn.host == "/run/postgresql" and conn.password == "a b c" and conn.database == "app"
    assert "host=%2Frun%2Fpostgresql" in conn.url()
    kwargs = conn.psycopg_kwargs(options="-c statement_timeout=5", connect_timeout=3)
    assert kwargs["options"] == "-c statement_timeout=5" and kwargs["connect_timeout"] == 3 and kwargs["sslmode"] == "disable"


@pytest.mark.parametrize(
    "dsn",
    ["mysql://x@y/z", "postgresql://a@h1,h2/db", "postgresql://a@host:99999/db", "host=a port=notaport", "this is not = = valid"],
)
def test_invalid_pg_dsns_never_echo_the_dsn(dsn: str) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_dsn(dsn, TargetKind.postgresql)
    assert exc.value.code == DB_TARGET_INVALID
    assert dsn not in exc.value.message


def test_sqlite_paths(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    for dsn in (str(db), f"sqlite:///{db}", f"sqlite+pysqlite:///{db}?mode=ro", f"file:{db}?mode=ro"):
        assert parse_dsn(dsn, TargetKind.sqlite).path == db, dsn
    with pytest.raises(ConfigError):
        parse_dsn("relative/app.db", TargetKind.sqlite)
    assert parse_dsn(str(db), TargetKind.sqlite).display() == f"sqlite:{db}"


def test_redact_dsn_masks_url_and_keyword_passwords() -> None:
    text = "failed: postgresql://u:topsecret@h/db and host=h password=other user=u and password='quoted pw'"
    out = redact_dsn(text)
    assert "topsecret" not in out and "other" not in out and "quoted pw" not in out
    assert "postgresql://u:" in out and "user=u" in out


def test_production_targets_are_always_read_only() -> None:
    with pytest.raises(ValidationError, match="production targets are always read-only"):
        DatabaseTarget(name="p", dsn_ref="literal:x", environment=TargetEnvironment.production, read_only=False)
    t = DatabaseTarget(name="p", dsn_ref="literal:x")
    assert t.environment == TargetEnvironment.production and t.read_only, "fail-safe defaults"
    DatabaseTarget(name="s", dsn_ref="literal:x", environment=TargetEnvironment.sandbox, read_only=False)


@pytest.mark.parametrize("ref", ["postgresql://u:pw@h/db", "vault:x", ""])
def test_dsn_must_be_a_secret_reference(ref: str) -> None:
    with pytest.raises(ValidationError):
        DatabaseTarget(name="p", dsn_ref=ref)


def test_config_rejects_duplicates_and_bad_names() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        DbToolsConfig(targets=[DatabaseTarget(name="a", dsn_ref="literal:x"), DatabaseTarget(name="a", dsn_ref="literal:y")])
    with pytest.raises(ValidationError):
        DatabaseTarget(name="../etc", dsn_ref="literal:x")


def test_load_config_file(tmp_path: Path) -> None:
    assert load_dbtools_config(tmp_path / "missing.yaml").targets == []
    path = tmp_path / "databases.yaml"
    path.write_text(
        "targets:\n"
        "  - {name: shop, dsn_ref: 'cred:shop-dsn', kind: postgresql, environment: staging, read_only: true}\n"
        "sandbox: {admin_dsn_ref: 'cred:sbx-admin', name_prefix: hermclaw_sbx_}\n"
        "limits: {max_row_limit: 50}\n",
        encoding="utf-8",
    )
    cfg = load_dbtools_config(path)
    assert cfg.targets[0].environment == TargetEnvironment.staging and cfg.limits.max_row_limit == 50
    path.write_text("targets: [{name: x, dsn_ref: plain}]\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_dbtools_config(path)
    assert exc.value.code == DB_TARGET_INVALID


def test_registry_resolves_secrets_and_registers_them_for_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_file = tmp_path / "pw"
    secret_file.write_text("Fi1e-Secret-Pw\n", encoding="utf-8")
    monkeypatch.setenv("HC_TEST_DSN", "postgresql://app:Env-Dsn-Pw@db/app")
    red = Redactor()
    reg = TargetRegistry.from_targets(
        [DatabaseTarget(name="app", dsn_ref="env:HC_TEST_DSN", password_ref=f"file:{secret_file}")],
        secrets=SecretStore(env="test"),
        redactor=red,
    )
    resolved = reg.resolve("app")
    assert resolved.conn.password == "Fi1e-Secret-Pw", "password_ref overrides the DSN password"
    assert "Fi1e-Secret-Pw" not in repr(resolved) and resolved.describe()["server"] == "postgresql://app@db/app"
    leaked = "connect failed for postgresql://app:Env-Dsn-Pw@db/app with Fi1e-Secret-Pw"
    assert "Env-Dsn-Pw" not in red.text(leaked) and "Fi1e-Secret-Pw" not in red.text(leaked)
    with pytest.raises(DbToolError) as exc:
        reg.get("nope")
    assert exc.value.code == DB_TARGET_UNKNOWN and "app" in exc.value.message


def test_production_refuses_env_and_literal_secrets() -> None:
    with pytest.raises(ConfigError, match="not allowed in production"):
        resolve_connection("literal:postgresql://u@h/d", TargetKind.postgresql, secrets=SecretStore(env="production"))
