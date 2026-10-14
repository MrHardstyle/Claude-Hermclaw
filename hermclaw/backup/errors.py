"""Error codes of the backup/restore component (stable, appear in events, logs and CLI output)."""

from __future__ import annotations

from hermclaw.core.errors import HermclawError

BACKUP_FAILED = "BACKUP_FAILED"
BACKUP_CONFIG_INVALID = "BACKUP_CONFIG_INVALID"
BACKUP_LOCKED = "BACKUP_LOCKED"  # another backup/prune run holds the lock
BACKUP_TOOL_MISSING = "BACKUP_TOOL_MISSING"  # pg_dump/pg_restore/rsync/ssh not installed
BACKUP_DUMP_FAILED = "BACKUP_DUMP_FAILED"
BACKUP_ARCHIVE_FAILED = "BACKUP_ARCHIVE_FAILED"
BACKUP_TRANSFER_FAILED = "BACKUP_TRANSFER_FAILED"
BACKUP_VERIFY_FAILED = "BACKUP_VERIFY_FAILED"  # checksum/size/manifest/signature mismatch
BACKUP_SET_INVALID = "BACKUP_SET_INVALID"  # not a backup set (no/invalid MANIFEST.json, bad id)
BACKUP_NOT_FOUND = "BACKUP_NOT_FOUND"
RESTORE_FAILED = "RESTORE_FAILED"
RESTORE_TARGET_NOT_EMPTY = "RESTORE_TARGET_NOT_EMPTY"


class BackupError(HermclawError):
    code = BACKUP_FAILED
