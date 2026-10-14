"""Backup configuration (``backup.yaml``) and backup targets (Bauplan §36, research 20261008-022).

A backup target is either ``local`` (a directory, typically a mounted Unraid share of Terra ``.60``) or ``rsync``
(``rsync -a`` over SSH with a dedicated key to ``user@host:/path``; without ``host`` the rsync destination is a local
path, e.g. a mounted share that should still be checksum-verified by rsync). ``host_id`` takes address, user, port,
key and known_hosts from ``hosts.yaml`` (explicit fields override).

The file is looked up as: explicit path, ``$HERMCLAW_BACKUP_CONFIG``, ``<config_dir>/backup.yaml`` – an example file
is never used implicitly (a backup must not silently go to an example target).
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hermclaw.backup.errors import BACKUP_CONFIG_INVALID, BackupError
from hermclaw.core.config import HostsConfig
from hermclaw.core.settings import Settings, get_settings

#: plaintext secrets and private keys never enter the config tarball (cannot be overridden by configuration)
ALWAYS_EXCLUDE: tuple[str, ...] = (
    "secrets/",
    "private/",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.secret",
    "*.token",
    ".pgpass",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "*_rsa",
    "*_dsa",
    "*_ecdsa",
    "*_ed25519",
    "*.swp",
    "*~",
)

#: systemd's plaintext credential stores (``/etc/credstore``, ``/run/credstore``) – never backed up
PLAINTEXT_CREDSTORE_NAMES: frozenset[str] = frozenset({"credstore", "credentials", "secrets"})

#: tables exported as JSON lines into the artifacts tarball (research records, deployment manifests)
DEFAULT_RECORD_EXPORTS: tuple[str, ...] = (
    "research_runs",
    "research_sources",
    "research_claims",
    "research_claim_sources",
    "deployments",
)

_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})|[0-9A-Fa-f:]{2,45})$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
_REMOTE_PATH_RE = re.compile(r"^[A-Za-z0-9_./+@-]+$")
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_NO_SPACE_PATH_RE = re.compile(r"^[^\s'\"\\]+$")  # usable inside rsync's -e command string
_PATTERN_RE = re.compile(r"^[^\x00\n]{1,200}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_remote_path(value: str) -> str:
    if not value or not _REMOTE_PATH_RE.match(value) or value.startswith("-"):
        raise ValueError("path may only contain letters, digits and _ . / + @ - (no spaces, no leading '-')")
    if any(part == ".." for part in PurePosixPath(value).parts):
        raise ValueError("path must not contain '..'")
    return value


def _check_ssh_path(value: Path | None) -> Path | None:
    if value is not None and not _NO_SPACE_PATH_RE.match(str(value)):
        raise ValueError("paths used in the ssh command must not contain whitespace, quotes or backslashes")
    return value


class LocalTarget(_Strict):
    """A directory (mounted Unraid share). Sets are written to ``<set>.partial`` and renamed after verification."""

    kind: Literal["local"] = "local"
    path: Path
    # an unmounted share must not silently fill the local disk: the directory is not created unless asked, and
    # require_mount demands that the path (or an ancestor other than /) is a mount point
    create_missing: bool = False
    require_mount: bool = False

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("local target path must be absolute")
        return value

    def describe(self) -> str:
        return str(self.path)


class RsyncTarget(_Strict):
    """``rsync -a --partial`` over SSH (or to a local path when ``host`` and ``host_id`` are unset)."""

    kind: Literal["rsync"] = "rsync"
    path: str  # remote base directory (absolute, or relative to an rrsync-restricted directory)
    host_id: str | None = None  # hosts.yaml entry (role backup, e.g. terra-60)
    host: str | None = None
    user: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    key_ref: str | None = None  # file:/etc/hermclaw/ssh/backup_ed25519 | cred:<name> | env:/literal: (dev/test)
    known_hosts: Path | None = None
    rsync_binary: str = "rsync"
    ssh_binary: str = "ssh"
    remote_rsync_path: str | None = None  # --rsync-path (e.g. /usr/bin/rsync)
    bwlimit_kbps: int | None = Field(default=None, ge=1)
    io_timeout_seconds: int = Field(default=300, ge=10, le=86400)
    connect_timeout_seconds: int = Field(default=20, ge=1, le=600)
    command_timeout_seconds: int = Field(default=6 * 3600, ge=10)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _check_remote_path(value)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str | None) -> str | None:
        if value is not None and (not _HOST_RE.match(value) or value.startswith("-")):
            raise ValueError("host must be a hostname or an IP address")
        return value

    @field_validator("user")
    @classmethod
    def _user(cls, value: str | None) -> str | None:
        if value is not None and not _USER_RE.match(value):
            raise ValueError("user must be a plain POSIX user name")
        return value

    @field_validator("known_hosts")
    @classmethod
    def _known_hosts(cls, value: Path | None) -> Path | None:
        return _check_ssh_path(value)

    @field_validator("remote_rsync_path")
    @classmethod
    def _remote_rsync(cls, value: str | None) -> str | None:
        return None if value is None else _check_remote_path(value)

    @field_validator("rsync_binary", "ssh_binary")
    @classmethod
    def _binary(cls, value: str) -> str:
        if not _NO_SPACE_PATH_RE.match(value) or value.startswith("-"):
            raise ValueError("binary must be a plain command name or path")
        return value

    def describe(self) -> str:
        if self.host or self.host_id:
            return f"{self.user or '?'}@{self.host or self.host_id}:{self.path}"
        return self.path


BackupTarget = Annotated[LocalTarget | RsyncTarget, Field(discriminator="kind")]


@dataclass(frozen=True)
class RsyncEndpoint:
    """An rsync target with host defaults from ``hosts.yaml`` applied."""

    path: str
    host: str | None
    user: str | None
    port: int
    key_ref: str | None
    known_hosts: Path | None

    @property
    def remote(self) -> bool:
        return self.host is not None

    def location(self, sub: str = "", *, slash: bool = False) -> str:
        """``user@host:path/sub`` (IPv6 in brackets) or the plain local path; ``slash`` appends ``/`` (rsync: "contents")."""
        full = posixpath.join(self.path, sub) if sub else self.path
        if slash and not full.endswith("/"):
            full += "/"
        if self.host is None:
            return full
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.user}@{host}:{full}"


def resolve_rsync_endpoint(target: RsyncTarget, hosts: HostsConfig | None) -> RsyncEndpoint:
    host, user, port, key_ref, known_hosts = target.host, target.user, target.port, target.key_ref, target.known_hosts
    if target.host_id:
        if hosts is None:
            raise BackupError(f"backup target host_id '{target.host_id}' needs hosts.yaml", code=BACKUP_CONFIG_INVALID)
        entry = next((h for h in hosts.hosts if h.id == target.host_id), None)
        if entry is None:
            raise BackupError(f"backup target host_id '{target.host_id}' is not in hosts.yaml", code=BACKUP_CONFIG_INVALID)
        host = host or entry.address
        if entry.ssh is not None:
            user = user or entry.ssh.user
            port = port or entry.ssh.port
            key_ref = key_ref or entry.ssh.key_ref
            known_hosts = known_hosts or Path(entry.ssh.known_hosts)
    if host is None and not target.path.startswith("/"):
        raise BackupError("an rsync target without host needs an absolute path", code=BACKUP_CONFIG_INVALID)
    if host is not None:
        try:
            RsyncTarget(path=target.path, host=host, user=user, known_hosts=known_hosts)
        except ValueError as exc:
            raise BackupError(f"invalid rsync endpoint from hosts.yaml: {exc}", code=BACKUP_CONFIG_INVALID) from None
        if not user:
            raise BackupError("rsync over SSH needs a user", code=BACKUP_CONFIG_INVALID)
        if not key_ref:
            raise BackupError("rsync over SSH needs a dedicated key (key_ref)", code=BACKUP_CONFIG_INVALID)
        if known_hosts is None:
            known_hosts = Path("/etc/hermclaw/ssh/known_hosts")
    return RsyncEndpoint(path=target.path, host=host, user=user, port=port or 22, key_ref=key_ref, known_hosts=known_hosts)


class RetentionPolicy(_Strict):
    """Grandfather-father-son retention: the newest set of each of the last N days / ISO weeks / months is kept."""

    keep_last: int = Field(default=1, ge=1)  # the newest N complete sets are always kept
    keep_daily: int = Field(default=7, ge=0)
    keep_weekly: int = Field(default=4, ge=0)
    keep_monthly: int = Field(default=0, ge=0)
    stale_partial_hours: float = Field(default=24.0, gt=0)  # incomplete sets older than this are removed
    prune_after_backup: bool = True


class RestoreTestConfig(_Strict):
    """Clean temporary environment for restore tests (a throw-away database on the same server by default)."""

    # connection with CREATEDB (and CREATE EXTENSION for the dump's extensions, or use ``template``);
    # default: the Hermclaw connection, database ``postgres``
    admin_url: str | None = None
    admin_password_ref: str | None = None
    template: str | None = None  # CREATE DATABASE … TEMPLATE (e.g. a template with pgvector pre-installed)
    db_prefix: str = "hermclaw_restoretest"
    jobs: int = Field(default=1, ge=1, le=16)
    timeout_seconds: int = Field(default=6 * 3600, ge=10)
    work_dir: Path | None = None  # temp extraction / fetched sets (default: <data_dir>/backup/restore-test)

    @field_validator("template", "db_prefix")
    @classmethod
    def _ident(cls, value: str | None) -> str | None:
        if value is not None and not _IDENT_RE.match(value):
            raise ValueError("must be a lower-case SQL identifier")
        return value


class BackupConfig(_Strict):
    target: BackupTarget
    staging_dir: Path | None = None  # default <data_dir>/backup/staging
    database_url: str | None = None  # default settings.database_url
    database_password_ref: str | None = None  # overrides the URL's password (cred:hermclaw-db-password)
    config_dir: Path | None = None  # default settings.config_dir
    artifacts_dir: Path | None = None  # default settings.artifacts_dir
    credstore_dirs: list[Path] = Field(default_factory=list)  # ENCRYPTED credential blobs (systemd-creds) only
    config_exclude_extra: list[str] = Field(default_factory=list)
    artifacts_exclude: list[str] = Field(default_factory=list)
    record_exports: list[str] = Field(default_factory=lambda: list(DEFAULT_RECORD_EXPORTS))
    include_globals: bool = True  # pg_dumpall --globals-only (skipped gracefully when not permitted)
    globals_role_passwords: bool = False  # include role password hashes (default: --no-role-passwords)
    pg_bin_dir: Path | None = None
    dump_compress_level: int = Field(default=6, ge=0, le=9)
    tar_compress_level: int = Field(default=6, ge=1, le=9)
    dump_timeout_seconds: int = Field(default=6 * 3600, ge=10)
    lock_timeout_seconds: int = Field(default=60, ge=1)  # pg_dump --lock-wait-timeout
    lock_wait_seconds: int = Field(default=2 * 3600, ge=0)  # wait for a concurrent backup/prune/restore test
    manifest_signing_key_ref: str | None = None  # HMAC-SHA256 over MANIFEST.json (MANIFEST.json.sig)
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)
    restore_test: RestoreTestConfig = Field(default_factory=RestoreTestConfig)

    @field_validator("config_exclude_extra", "artifacts_exclude")
    @classmethod
    def _patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not _PATTERN_RE.match(pattern):
                raise ValueError(f"invalid exclude pattern {pattern!r}")
        return value

    @field_validator("record_exports")
    @classmethod
    def _tables(cls, value: list[str]) -> list[str]:
        for table in value:
            if not _IDENT_RE.match(table):
                raise ValueError(f"record export {table!r} is not a plain table name")
        return value

    @model_validator(mode="after")
    def _absolute_paths(self) -> BackupConfig:
        for name in ("staging_dir", "config_dir", "artifacts_dir", "pg_bin_dir"):
            value = getattr(self, name)
            if value is not None and not Path(value).is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        for value in self.credstore_dirs:
            if not value.is_absolute():
                raise ValueError("credstore_dirs must be absolute paths")
            if value.name in PLAINTEXT_CREDSTORE_NAMES:
                raise ValueError(f"{value} is a plaintext credential store; only encrypted stores (credstore.encrypted) are backed up")
        return self

    # ------------------------------------------------------------------------------------- effective values
    def effective_database_url(self, settings: Settings | None = None) -> str:
        return self.database_url or (settings or get_settings()).database_url

    def effective_config_dir(self, settings: Settings | None = None) -> Path:
        return self.config_dir or (settings or get_settings()).config_dir

    def effective_artifacts_dir(self, settings: Settings | None = None) -> Path:
        return self.artifacts_dir or (settings or get_settings()).artifacts_dir

    def effective_staging_dir(self, settings: Settings | None = None) -> Path:
        return self.staging_dir or (settings or get_settings()).data_dir / "backup" / "staging"

    def effective_restore_work_dir(self, settings: Settings | None = None) -> Path:
        return self.restore_test.work_dir or (settings or get_settings()).data_dir / "backup" / "restore-test"

    def config_excludes(self) -> list[str]:
        return [*ALWAYS_EXCLUDE, *self.config_exclude_extra]

    def lock_path(self, settings: Settings | None = None) -> Path:
        """One lock for create/prune/restore-test on this host (a prune must not delete a set under test)."""
        return self.effective_staging_dir(settings) / ".hermclaw-backup.lock"


def find_backup_config(path: Path | None = None, settings: Settings | None = None) -> Path:
    if path is not None:
        return path
    env = os.environ.get("HERMCLAW_BACKUP_CONFIG")
    if env:
        return Path(env)
    cdir = (settings or get_settings()).config_dir
    candidate = cdir / "backup.yaml"
    if candidate.is_file():
        return candidate
    raise BackupError(f"no backup configuration (backup.yaml) in {cdir}; set HERMCLAW_BACKUP_CONFIG", code=BACKUP_CONFIG_INVALID)


def parse_backup_config(data: Any) -> BackupConfig:
    if isinstance(data, dict) and set(data) == {"backup"}:
        data = data["backup"]
    if not isinstance(data, dict):
        raise BackupError("backup configuration must be a mapping", code=BACKUP_CONFIG_INVALID)
    try:
        return BackupConfig.model_validate(data)
    except ValueError as exc:
        raise BackupError(f"backup configuration invalid: {exc}", code=BACKUP_CONFIG_INVALID) from None


def load_backup_config(path: Path | None = None, settings: Settings | None = None) -> BackupConfig:
    file = find_backup_config(path, settings)
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BackupError(f"cannot read backup configuration {file}: {exc}", code=BACKUP_CONFIG_INVALID) from None
    return parse_backup_config(data)


def load_hosts(settings: Settings | None = None) -> HostsConfig | None:
    """``hosts.yaml`` of the config dir (None when absent/invalid – only needed for ``host_id`` targets)."""
    cdir = (settings or get_settings()).config_dir
    for candidate in (cdir / "hosts.yaml", cdir / "hosts.example.yaml"):
        if candidate.is_file():
            try:
                return HostsConfig.model_validate(yaml.safe_load(candidate.read_text(encoding="utf-8")) or {})
            except (OSError, ValueError, yaml.YAMLError) as exc:
                raise BackupError(f"hosts configuration invalid: {exc}", code=BACKUP_CONFIG_INVALID) from None
    return None
