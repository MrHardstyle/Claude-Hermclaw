"""Database targets of user projects (Bauplan §30, P27): configuration, secret resolution, DSN handling.

A *target* is a database of a user project that jobs may inspect (never Hermclaw's own database). Each target has a
secret reference for its DSN (``cred:``/``file:``/``env:``/``literal:``, see :mod:`hermclaw.security.secrets`), a kind,
an environment and a ``read_only`` flag. Resolved DSNs are parsed into :class:`ConnectionInfo`; the password is
registered with the redactor and never part of ``repr``/``display``/argv – external tools get it via ``PGPASSWORD``.

Configuration lives in ``<config_dir>/databases.yaml`` (see ``docs/architecture/dbtools.md``)::

    targets:
      - name: shop
        dsn_ref: cred:shop-db-dsn          # postgresql://reader@db.lan:5432/shop
        password_ref: cred:shop-db-password  # optional, overrides the password of the DSN
        kind: postgresql
        environment: production
        read_only: true
    sandbox:
      admin_dsn_ref: cred:sandbox-pg-admin  # role with CREATEDB + CREATEROLE on a non-production server
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import psycopg.conninfo
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.engine import URL

from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED, Redactor
from hermclaw.dbtools.errors import DB_TARGET_INVALID, DB_TARGET_UNKNOWN, DbConfigError, DbToolError
from hermclaw.security.secrets import SecretStore

TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SECRET_REF_RE = re.compile(r"^(cred|file|env|literal):.+$", re.S)
# libpq connection parameters a DSN may carry besides host/port/user/password/dbname (passed through as PG* env)
_LIBPQ_ENV: dict[str, str] = {
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslcrl": "PGSSLCRL",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "application_name": "PGAPPNAME",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "options": "PGOPTIONS",
    "gssencmode": "PGGSSENCMODE",
    "channel_binding": "PGCHANNELBINDING",
}
_CORE_KEYS = {"host", "hostaddr", "port", "user", "password", "dbname"}
_DSN_PASSWORD_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/@\s]*:)([^@\s]*)(@)")
_KV_PASSWORD_RE = re.compile(r"(?i)\b(password|sslpassword)(\s*=\s*)('(?:[^'\\]|\\.)*'|[^\s&]+)")


class TargetKind(StrEnum):
    postgresql = "postgresql"
    sqlite = "sqlite"
    mysql = "mysql"  # accepted in the configuration; the tools report DB_TARGET_UNSUPPORTED (no driver in the runtime)


class TargetEnvironment(StrEnum):
    production = "production"
    staging = "staging"
    sandbox = "sandbox"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_ref(value: str | None) -> str | None:
    if value is not None and not SECRET_REF_RE.match(value):
        raise ValueError("must be a secret reference (cred:/file:/env:/literal:)")
    return value


class DatabaseTarget(_Strict):
    name: str = Field(min_length=1, max_length=100)
    dsn_ref: str = Field(min_length=5, description="secret reference of the DSN (URL or libpq keywords; SQLite: absolute path)")
    password_ref: str | None = Field(default=None, description="optional secret reference of the password (overrides the DSN's)")
    kind: TargetKind = TargetKind.postgresql
    # fail-safe defaults: an unlabelled target is production and read-only
    environment: TargetEnvironment = TargetEnvironment.production
    read_only: bool = True
    description: str = Field(default="", max_length=500)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not TARGET_NAME_RE.match(value):
            raise ValueError("target name must match [A-Za-z0-9][A-Za-z0-9._-]*")
        return value

    @field_validator("dsn_ref", "password_ref")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        return _check_ref(value)

    @model_validator(mode="after")
    def _production_read_only(self) -> DatabaseTarget:
        if self.environment == TargetEnvironment.production and not self.read_only:
            raise ValueError("production targets are always read-only (Bauplan §30: no uncontrolled production mutations)")
        return self


class SandboxServerConfig(_Strict):
    """PostgreSQL server for throw-away sandbox databases (``CREATE DATABASE <prefix><id>`` + an isolated login role)."""

    admin_dsn_ref: str
    password_ref: str | None = None
    name_prefix: str = Field(default="hermclaw_sbx_", pattern=r"^[a-z][a-z0-9_]{2,30}$")
    client_host: str | None = Field(default=None, max_length=255, description="host the migration command uses (remote sandbox)")
    client_port: int | None = Field(default=None, ge=1, le=65535)
    orphan_max_age_hours: float = Field(default=6.0, gt=0)

    @field_validator("admin_dsn_ref", "password_ref")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        return _check_ref(value)


class DbLimits(_Strict):
    default_row_limit: int = Field(default=100, ge=1)
    max_row_limit: int = Field(default=5000, ge=1)
    default_timeout_seconds: float = Field(default=30, gt=0)
    max_timeout_seconds: float = Field(default=600, gt=0)
    max_cell_chars: int = Field(default=2000, ge=16)
    max_output_chars: int = Field(default=12000, ge=500)
    connect_timeout_seconds: int = Field(default=10, ge=1)
    introspect_timeout_seconds: float = Field(default=120, gt=0)
    introspect_max_tables: int = Field(default=1000, ge=1)
    command_timeout_seconds: float = Field(default=3600, gt=0)
    migration_timeout_seconds: float = Field(default=900, gt=0)
    count_timeout_seconds: float = Field(default=600, gt=0)
    max_count_tables: int = Field(default=2000, ge=1)


class DbToolsConfig(_Strict):
    targets: list[DatabaseTarget] = Field(default_factory=list)
    sandbox: SandboxServerConfig | None = None
    limits: DbLimits = Field(default_factory=DbLimits)
    pg_bin_dir: Path | None = Field(default=None, description="directory with pg_dump/pg_restore/psql (default: per server major, PATH)")
    backup_dir: Path | None = Field(default=None, description="default: <artifacts_dir>/db-backups")
    sqlite_sandbox_dir: Path | None = Field(default=None, description="default: system temp dir")
    migration_python: str = Field(default="python3", min_length=1, max_length=300)
    allowed_capabilities: list[str] = Field(default_factory=lambda: ["database"])

    @model_validator(mode="after")
    def _unique(self) -> DbToolsConfig:
        names = [t.name for t in self.targets]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate database target names: {', '.join(dupes)}")
        return self


def load_dbtools_config(path: Path | None) -> DbToolsConfig:
    """Load ``databases.yaml``; a missing file means 'no targets' (the DB tools then refuse every target)."""
    if path is None or not path.is_file():
        return DbToolsConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return DbToolsConfig.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise DbConfigError(f"{path.name}: invalid database tools configuration: {exc}", code=DB_TARGET_INVALID) from exc


@dataclass(frozen=True, repr=False)
class ConnectionInfo:
    """Parsed connection parameters. ``password`` never appears in ``repr``/``display``."""

    kind: TargetKind
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    params: tuple[tuple[str, str], ...] = ()
    path: Path | None = None  # SQLite database file

    def __repr__(self) -> str:
        return f"ConnectionInfo({self.display()})"

    __str__ = __repr__

    def display(self) -> str:
        """Credential-free description for events, logs and tool output."""
        if self.kind == TargetKind.sqlite:
            return f"sqlite:{self.path}"
        host = self.host or "localhost"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        user = f"{self.user}@" if self.user else ""
        port = f":{self.port}" if self.port else ""
        return f"{self.kind.value}://{user}{host}{port}/{self.database or ''}"

    def server_key(self) -> str:
        return f"{self.host or 'localhost'}:{self.port or 5432}"

    def with_database(
        self, database: str, *, user: str | None = None, password: str | None = None, host: str | None = None, port: int | None = None
    ) -> ConnectionInfo:
        return replace(
            self,
            database=database,
            user=user if user is not None else self.user,
            password=password if password is not None else self.password,
            host=host if host is not None else self.host,
            port=port if port is not None else self.port,
        )

    def extra(self) -> dict[str, str]:
        return dict(self.params)

    def libpq_env(self) -> dict[str, str]:
        """Environment for libpq tools (pg_dump/pg_restore/psql): credentials stay out of argv."""
        env: dict[str, str] = {}
        if self.host:
            env["PGHOST"] = self.host
        if self.port:
            env["PGPORT"] = str(self.port)
        if self.user:
            env["PGUSER"] = self.user
        if self.password is not None:
            env["PGPASSWORD"] = self.password
        if self.database:
            env["PGDATABASE"] = self.database
        for key, value in self.params:
            name = _LIBPQ_ENV.get(key)
            if name:
                env[name] = value
        return env

    def psycopg_kwargs(self, *, options: str = "", connect_timeout: int | None = None) -> dict[str, Any]:
        """Keyword arguments for ``psycopg.connect``; ``options`` (``-c`` settings) are appended after the DSN's own."""
        kwargs: dict[str, Any] = {k: v for k, v in self.params if k != "options"}
        if self.host:
            kwargs["host"] = self.host
        if self.port:
            kwargs["port"] = self.port
        if self.user:
            kwargs["user"] = self.user
        if self.password is not None:
            kwargs["password"] = self.password
        if self.database:
            kwargs["dbname"] = self.database
        merged = " ".join(part for part in (self.extra().get("options", ""), options) if part).strip()
        if merged:
            kwargs["options"] = merged
        if connect_timeout is not None:
            kwargs["connect_timeout"] = connect_timeout
        return kwargs

    def sqlalchemy_url(self, *, driver: str = "psycopg") -> URL:
        if self.kind == TargetKind.sqlite:
            return URL.create("sqlite+pysqlite", database=str(self.path))
        query = {k: v for k, v in self.params if k != "options"}
        return URL.create(
            f"postgresql+{driver}" if driver else "postgresql",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
            query=query,
        )

    def url(self, scheme: str = "postgresql") -> str:
        """URL with credentials (for the environment of a migration command – never logged)."""
        if self.kind == TargetKind.sqlite:
            return f"sqlite:///{self.path}"
        auth = ""
        if self.user:
            auth = quote(self.user, safe="")
            if self.password is not None:
                auth += ":" + quote(self.password, safe="")
            auth += "@"
        host = self.host or ""
        if host.startswith("/"):  # unix socket directory: libpq accepts it as query parameter
            params = [("host", host), *self.params]
            host = ""
        else:
            params = list(self.params)
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
        port = f":{self.port}" if self.port else ""
        query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params)
        return f"{scheme}://{auth}{host}{port}/{quote(self.database or '', safe='')}" + (f"?{query}" if query else "")


def redact_dsn(text: str) -> str:
    """Mask passwords of URL-style (``scheme://user:pw@``) and keyword-style (``password=…``) DSNs in ``text``."""
    out = _DSN_PASSWORD_RE.sub(lambda m: m.group(1) + REDACTED + m.group(3), text)
    return _KV_PASSWORD_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, out)


def parse_dsn(dsn: str, kind: TargetKind) -> ConnectionInfo:
    """Parse a PostgreSQL URL/keyword DSN or a SQLite path/URL. Errors never echo the DSN (it may hold a password)."""
    raw = dsn.strip()
    if kind == TargetKind.sqlite:
        return _parse_sqlite(raw)
    if kind == TargetKind.mysql:
        return ConnectionInfo(kind=kind)
    if "://" in raw:
        return _parse_pg_url(raw)
    try:
        parts = psycopg.conninfo.conninfo_to_dict(raw)
    except psycopg.ProgrammingError:
        raise DbConfigError("database DSN is not a valid libpq connection string", code=DB_TARGET_INVALID) from None
    return _from_mapping({k: str(v) for k, v in parts.items() if v is not None})


def _parse_sqlite(raw: str) -> ConnectionInfo:
    path_text = raw
    if raw.startswith("sqlite"):
        scheme, sep, rest = raw.partition("://")
        if not sep or not scheme.startswith("sqlite"):
            raise DbConfigError("SQLite DSN must be sqlite:////absolute/path or an absolute path", code=DB_TARGET_INVALID)
        path_text = rest[1:] if rest.startswith("/") else rest  # sqlite:///<path>: '/abs' after the third slash
        path_text = path_text.split("?", 1)[0]
    elif raw.startswith("file:"):
        path_text = unquote(raw[5:].split("?", 1)[0])
        if path_text.startswith("//"):
            path_text = path_text[2:]
    path = Path(path_text)
    if not path.is_absolute() or "\x00" in path_text:
        raise DbConfigError("SQLite target path must be absolute", code=DB_TARGET_INVALID)
    return ConnectionInfo(kind=TargetKind.sqlite, path=path)


def _parse_pg_url(raw: str) -> ConnectionInfo:
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("postgresql", "postgres") and not scheme.startswith("postgresql+"):
        raise DbConfigError("PostgreSQL DSN must use the postgresql:// scheme", code=DB_TARGET_INVALID)
    netloc = parts.netloc
    userinfo, _, hostport = netloc.rpartition("@")
    if "," in hostport:
        raise DbConfigError("multi-host PostgreSQL DSNs are not supported", code=DB_TARGET_INVALID)
    values: dict[str, str] = {}
    if userinfo:
        user, has_pw, password = userinfo.partition(":")
        if user:
            values["user"] = unquote(user)
        if has_pw:
            values["password"] = unquote(password)
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        raise DbConfigError("PostgreSQL DSN has an invalid port", code=DB_TARGET_INVALID) from None
    if host:
        values["host"] = unquote(host)
    if port:
        values["port"] = str(port)
    database = unquote(parts.path.lstrip("/"))
    if database:
        values["dbname"] = database
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        values[key] = value
    return _from_mapping(values)


def _from_mapping(values: Mapping[str, str]) -> ConnectionInfo:
    port_text = values.get("port")
    try:
        port = int(port_text) if port_text else None
    except ValueError:
        raise DbConfigError("PostgreSQL DSN has an invalid port", code=DB_TARGET_INVALID) from None
    if port is not None and not 0 < port < 65536:
        raise DbConfigError("PostgreSQL DSN has an invalid port", code=DB_TARGET_INVALID)
    host = values.get("host") or values.get("hostaddr")
    if host and "," in host:
        raise DbConfigError("multi-host PostgreSQL DSNs are not supported", code=DB_TARGET_INVALID)
    params = tuple(sorted((k, v) for k, v in values.items() if k not in _CORE_KEYS and k != "sslpassword"))
    return ConnectionInfo(
        kind=TargetKind.postgresql,
        host=host,
        port=port,
        user=values.get("user"),
        password=values.get("password"),
        database=values.get("dbname"),
        params=params,
    )


@dataclass(frozen=True, repr=False)
class ResolvedTarget:
    target: DatabaseTarget
    conn: ConnectionInfo

    def __repr__(self) -> str:
        return f"ResolvedTarget({self.target.name}, {self.conn.display()})"

    @property
    def name(self) -> str:
        return self.target.name

    def describe(self) -> dict[str, Any]:
        """Credential-free description for events and results."""
        return {
            "target": self.target.name,
            "kind": self.target.kind.value,
            "environment": self.target.environment.value,
            "read_only": self.target.read_only,
            "server": self.conn.display(),
        }


@dataclass
class TargetRegistry:
    """Registered targets by name; resolves secret references on demand (never cached in plain text in events)."""

    targets: dict[str, DatabaseTarget] = field(default_factory=dict)
    secrets: SecretStore | None = None
    redactor: Redactor = DEFAULT_REDACTOR

    @classmethod
    def from_targets(
        cls, targets: Iterable[DatabaseTarget], *, secrets: SecretStore | None = None, redactor: Redactor = DEFAULT_REDACTOR
    ) -> TargetRegistry:
        return cls({t.name: t for t in targets}, secrets, redactor)

    def names(self) -> list[str]:
        return sorted(self.targets)

    def get(self, name: str) -> DatabaseTarget:
        target = self.targets.get(name)
        if target is None:
            known = ", ".join(self.names()) or "none"
            raise DbToolError(f"unknown database target '{name[:100]}' (registered: {known})", code=DB_TARGET_UNKNOWN)
        return target

    def resolve(self, name_or_target: str | DatabaseTarget) -> ResolvedTarget:
        target = self.get(name_or_target) if isinstance(name_or_target, str) else name_or_target
        conn = resolve_connection(
            target.dsn_ref, target.kind, password_ref=target.password_ref, secrets=self.secrets, redactor=self.redactor
        )
        return ResolvedTarget(target, conn)


def resolve_connection(
    dsn_ref: str,
    kind: TargetKind,
    *,
    password_ref: str | None = None,
    secrets: SecretStore | None = None,
    redactor: Redactor = DEFAULT_REDACTOR,
) -> ConnectionInfo:
    store = secrets or SecretStore()
    dsn = store.resolve(dsn_ref)
    assert dsn is not None  # required=True raises instead
    redactor.add_literal(dsn)
    conn = parse_dsn(dsn, kind)
    if password_ref:
        password = store.resolve(password_ref)
        conn = replace(conn, password=password)
    if conn.password:
        redactor.add_literal(conn.password)
        if redactor is not DEFAULT_REDACTOR:
            DEFAULT_REDACTOR.add_literal(conn.password)
    return conn


__all__ = [
    "ConnectionInfo",
    "DatabaseTarget",
    "DbLimits",
    "DbToolsConfig",
    "ResolvedTarget",
    "SandboxServerConfig",
    "TargetEnvironment",
    "TargetKind",
    "TargetRegistry",
    "load_dbtools_config",
    "parse_dsn",
    "redact_dsn",
    "resolve_connection",
]
