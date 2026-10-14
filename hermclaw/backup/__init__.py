"""Hermclaw backup/restore (Bauplan §36, P33): PostgreSQL + config + artifacts to Terra/Unraid ``.60``, restore tests.

Public API::

    create_backup(target, *, config=None, …) -> BackupResult         # 33.1-33.4
    restore_test(backup_set="latest", *, config=None, …) -> RestoreReport   # 33.5, 33.6
    restore_full(backup_set, target_db_url, *, force=False, …) -> RestoreReport
    verify_set_dir(directory, …) -> (VerifyReport, Manifest | None)
    prune_backups(config, …) / list_backups(config, …)

CLI: ``python -m hermclaw.backup {create,verify,restore-test,restore-full,list,prune}``.
"""

from __future__ import annotations

from hermclaw.backup.backup import BackupResult, PruneResult, create_backup, list_backups, prune_backups
from hermclaw.backup.config import (
    BackupConfig,
    BackupTarget,
    LocalTarget,
    RestoreTestConfig,
    RetentionPolicy,
    RsyncTarget,
    load_backup_config,
    parse_backup_config,
)
from hermclaw.backup.errors import BackupError
from hermclaw.backup.manifest import Manifest
from hermclaw.backup.restore import RestoreReport, restore_full, restore_test
from hermclaw.backup.verify import VerifyReport, verify_set_dir

__all__ = [
    "BackupConfig",
    "BackupError",
    "BackupResult",
    "BackupTarget",
    "LocalTarget",
    "Manifest",
    "PruneResult",
    "RestoreReport",
    "RestoreTestConfig",
    "RetentionPolicy",
    "RsyncTarget",
    "VerifyReport",
    "create_backup",
    "list_backups",
    "load_backup_config",
    "parse_backup_config",
    "prune_backups",
    "restore_full",
    "restore_test",
    "verify_set_dir",
]
