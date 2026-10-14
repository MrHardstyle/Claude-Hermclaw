"""P33 live acceptance against the real Terra/Unraid ``.60`` target (BLOCKER-001: LAN unreachable from the build
environment – run on ``.225`` with ``pytest -m live tests/integration/test_backup_live.py``).

Uses the production configuration (``$HERMCLAW_BACKUP_CONFIG`` or ``/etc/hermclaw/backup.yaml``): one backup set to
``.60``, a restore test of exactly that set, retention afterwards. Nothing is deleted except by the configured
retention policy.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermclaw.backup import create_backup, load_backup_config, restore_test
from hermclaw.backup.backup import list_backups

pytestmark = [pytest.mark.live, pytest.mark.integration]


def _config_path() -> Path:
    return Path(os.environ.get("HERMCLAW_BACKUP_CONFIG", "/etc/hermclaw/backup.yaml"))


@pytest.mark.skipif(not _config_path().is_file(), reason="no production backup.yaml (BLOCKER-001: run on .225)")
async def test_live_backup_to_terra_60_and_restore_test() -> None:
    cfg = load_backup_config(_config_path())
    made = await create_backup(config=cfg)
    assert made.transfer.verified
    assert made.set_id in {s.set_id for s in await list_backups(cfg) if s.complete}
    report = await restore_test(made.set_id, config=cfg)
    assert report.ok, report.failed_checks()
    assert report.rows_restored == made.rows and report.database_dropped is True
