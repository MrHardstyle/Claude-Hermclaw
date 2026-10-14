"""Backup set layout and ``MANIFEST.json`` (P33 33.1-33.4).

A backup set is one directory ``hermclaw-<UTC yyyymmddThhmmss[mmm]Z>-<6 hex>`` (milliseconds: sets made within one
second still sort exactly; second-precision ids stay valid) containing::

    db.dump            pg_dump -Fc of the Hermclaw database
    globals.sql        pg_dumpall --globals-only (optional: skipped when not permitted)
    config.tar.gz      config dir (+ encrypted credstore blobs), no plaintext secrets / private keys
    artifacts.tar.gz   artifact store + records/<table>.jsonl (research records, deployment manifests)
    MANIFEST.json      sha256 + size of every file, row-count snapshot, alembic revision, versions
    MANIFEST.json.sig  optional HMAC-SHA256 of MANIFEST.json (manifest_signing_key_ref)
    RESTORE_REPORT.json  written by the restore test (not covered by the manifest)

``MANIFEST.json`` is written last: a set without it is incomplete (interrupted) and never used for restores.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hermclaw.backup.errors import BACKUP_SET_INVALID, BACKUP_VERIFY_FAILED, BackupError

MANIFEST_FORMAT: Final = "hermclaw-backup/1"
DB_DUMP = "db.dump"
GLOBALS = "globals.sql"
CONFIG_TAR = "config.tar.gz"
ARTIFACTS_TAR = "artifacts.tar.gz"
MANIFEST = "MANIFEST.json"
MANIFEST_SIG = "MANIFEST.json.sig"
RESTORE_REPORT = "RESTORE_REPORT.json"
PARTIAL_SUFFIX = ".partial"
SET_ID_RE = re.compile(r"^hermclaw-(?P<ts>\d{8}T\d{6})(?P<ms>\d{3})?Z-(?P<rand>[0-9a-f]{6})$")
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
FileKind = Literal["db_dump", "globals", "config", "artifacts"]


def new_set_id(now: datetime | None = None) -> str:
    ts = (now or datetime.now(UTC)).astimezone(UTC)
    return f"hermclaw-{ts:%Y%m%dT%H%M%S}{ts.microsecond // 1000:03d}Z-{secrets.token_hex(3)}"


def is_set_id(value: str) -> bool:
    return SET_ID_RE.match(value) is not None


def check_set_id(value: str) -> str:
    if not is_set_id(value):
        raise BackupError(f"{value!r} is not a backup set id", code=BACKUP_SET_INVALID)
    return value


def set_time(set_id: str) -> datetime:
    match = SET_ID_RE.match(set_id)
    if not match:
        raise BackupError(f"{set_id!r} is not a backup set id", code=BACKUP_SET_INVALID)
    base = datetime.strptime(match.group("ts"), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    return base.replace(microsecond=int(match.group("ms") or 0) * 1000)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileEntry(_Model):
    name: str
    kind: FileKind
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _FILE_NAME_RE.match(value) or value in (MANIFEST, MANIFEST_SIG, RESTORE_REPORT):
            raise ValueError(f"invalid backup file name {value!r}")
        return value


class ArchiveInfo(_Model):
    sources: list[str] = Field(default_factory=list)
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    root_files: dict[str, int] = Field(default_factory=dict)
    excluded: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class RecordExport(_Model):
    table: str
    rows: int = Field(ge=0)
    member: str  # records/<table>.jsonl inside artifacts.tar.gz


class DatabaseInfo(_Model):
    dbname: str
    connection: str  # credential-free display
    server_version: str
    server_major: int
    pg_dump_version: str
    alembic_revision: str | None
    encoding: str | None = None
    extensions: list[str] = Field(default_factory=list)
    tables: dict[str, int]  # schema.table -> exact row count in the dump snapshot
    snapshot_consistent: bool  # counts/exports were taken on the snapshot pg_dump used
    toc_entries: int = Field(default=0, ge=0)  # entries of ``pg_restore --list`` (catalog check at backup time)
    dump_format: Literal["custom"] = "custom"


class GlobalsInfo(_Model):
    included: bool
    role_passwords: bool = False
    reason: str | None = None


class Manifest(_Model):
    format: Literal["hermclaw-backup/1"] = MANIFEST_FORMAT
    set_id: str
    created_at: datetime
    finished_at: datetime
    hermclaw_version: str
    instance_id: str
    hostname: str
    python_version: str
    target: str  # credential-free description of the backup target
    database: DatabaseInfo
    globals: GlobalsInfo
    config: ArchiveInfo
    artifacts: ArchiveInfo
    records: list[RecordExport] = Field(default_factory=list)
    files: list[FileEntry]
    tool_versions: dict[str, str] = Field(default_factory=dict)

    @field_validator("set_id")
    @classmethod
    def _set_id(cls, value: str) -> str:
        if not is_set_id(value):
            raise ValueError("invalid set id")
        return value

    def file(self, name: str) -> FileEntry | None:
        return next((f for f in self.files if f.name == name), None)

    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


# ================================================================================================ io
def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def sign(data: bytes, key: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def write_manifest(directory: Path, manifest: Manifest, *, signing_key: bytes | None = None) -> bytes:
    data = (json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode("utf-8")
    if signing_key:
        _write_private(directory / MANIFEST_SIG, (sign(data, signing_key) + "\n").encode("ascii"))
    _write_private(directory / MANIFEST, data)
    return data


def write_json(path: Path, payload: object) -> None:
    _write_private(path, (json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8"))


def parse_manifest(data: bytes, *, set_id: str | None = None) -> Manifest:
    try:
        manifest = Manifest.model_validate_json(data)
    except ValidationError as exc:
        raise BackupError(f"MANIFEST.json invalid: {exc.errors()[0].get('msg', 'schema error')}", code=BACKUP_SET_INVALID) from None
    if set_id is not None and manifest.set_id != set_id:
        raise BackupError(f"MANIFEST.json belongs to {manifest.set_id}, not {set_id}", code=BACKUP_VERIFY_FAILED)
    return manifest


def read_manifest(directory: Path, *, signing_key: bytes | None = None) -> tuple[Manifest, str]:
    """Manifest of a set directory and its signature state (``valid`` | ``unsigned`` | ``unverified``).

    With a signing key the signature is mandatory and must match; a tampered manifest raises BACKUP_VERIFY_FAILED.
    """
    path = directory / MANIFEST
    if not path.is_file():
        raise BackupError(f"{directory.name}: no {MANIFEST} (incomplete or not a backup set)", code=BACKUP_SET_INVALID)
    data = path.read_bytes()
    set_id = directory.name.removesuffix(PARTIAL_SUFFIX)
    manifest = parse_manifest(data, set_id=set_id if is_set_id(set_id) else None)
    sig_path = directory / MANIFEST_SIG
    if signing_key:
        if not sig_path.is_file():
            raise BackupError(f"{directory.name}: manifest signature missing", code=BACKUP_VERIFY_FAILED)
        expected = sign(data, signing_key)
        if not hmac.compare_digest(expected, sig_path.read_text(encoding="ascii", errors="replace").strip()):
            raise BackupError(f"{directory.name}: manifest signature mismatch (tampered manifest)", code=BACKUP_VERIFY_FAILED)
        return manifest, "valid"
    return manifest, "unverified" if sig_path.is_file() else "unsigned"
