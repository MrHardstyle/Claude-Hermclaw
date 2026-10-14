"""P33 least-privilege production path: the backup/restore role is NOT a superuser (only ``CREATEDB``); the
untrusted extension ``vector`` comes from a restore template created once by the superuser
(``docs/operations/backup-restore.md`` install step 4)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from hermclaw.backup import create_backup, restore_full, restore_test
from hermclaw.backup.config import RestoreTestConfig
from hermclaw.backup.manifest import MANIFEST, read_manifest
from tests.integration.test_backup_support import ADMIN_URL, TEST_PREFIX, BackupWorld, make_world, temp_databases

pytestmark = pytest.mark.integration


@dataclass
class Restricted:
    role: str
    template: str
    database: str

    def url(self, db: str | None = None) -> str:
        return f"postgresql+psycopg://{self.role}@127.0.0.1:5432/{db or self.database}"


@pytest.fixture
def world(tmp_path: Path, db_url: str) -> BackupWorld:
    return make_world(tmp_path, db_url)


@pytest.fixture
def restricted() -> Iterator[Restricted]:
    tag = uuid.uuid4().hex[:8]
    env = Restricted(role=f"hcbk_role_{tag}", template=f"hcbk_tpl_{tag}", database=f"hcbk_db_{tag}")
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'CREATE ROLE "{env.role}" LOGIN CREATEDB NOSUPERUSER')
        conn.execute(f"CREATE DATABASE \"{env.template}\" TEMPLATE template0 ENCODING 'UTF8'")
    with psycopg.connect(ADMIN_URL.rsplit("/", 1)[0] + f"/{env.template}", autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'ALTER DATABASE "{env.template}" IS_TEMPLATE true ALLOW_CONNECTIONS false')
        conn.execute(f'CREATE DATABASE "{env.database}" OWNER "{env.role}" TEMPLATE "{env.template}"')
    yield env
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        for name in [env.database, *temp_databases()]:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'ALTER DATABASE "{env.template}" IS_TEMPLATE false')
        conn.execute(f'DROP DATABASE IF EXISTS "{env.template}" WITH (FORCE)')
        conn.execute(f'DROP ROLE IF EXISTS "{env.role}"')


async def test_non_superuser_restore_full_backup_and_restore_test(world: BackupWorld, restricted: Restricted) -> None:
    # the Hermclaw database (superuser-made schema) is restored into a database owned by the restricted role ...
    made = await create_backup(config=world.config())
    full = await restore_full(made.set_id, restricted.url(), config=world.config())
    assert full.ok, full.failed_checks()

    # ... which then backs up its own database and restore-tests it, without any superuser right
    restore_cfg = RestoreTestConfig(
        admin_url=f"postgresql://{restricted.role}@127.0.0.1:5432/postgres",
        template=restricted.template,
        db_prefix=TEST_PREFIX,
        work_dir=world.work,
    )
    cfg = world.config(database_url=restricted.url(), restore_test=restore_cfg)
    own = await create_backup(config=cfg)
    manifest, _ = read_manifest(world.target_dir / own.set_id)
    assert manifest.database.connection.startswith(f"{restricted.role}@")
    assert {t.table: t.actual for t in full.tables} == manifest.database.tables
    # pg_dumpall --globals-only --no-role-passwords works for a normal role on PostgreSQL 16 (pg_roles is public)
    assert manifest.globals.included, manifest.globals.reason
    report = await restore_test(own.set_id, config=cfg)
    assert report.ok, [c for c in report.checks if not c.ok]
    assert report.database_dropped is True and temp_databases() == []

    # without the template the untrusted extension cannot be created by the role: a clear, failed report
    no_template = cfg.model_copy(update={"restore_test": restore_cfg.model_copy(update={"template": None})})
    report = await restore_test(own.set_id, config=no_template)
    assert not report.ok
    ext = report.check("extensions")
    assert ext is not None and "vector" in ext.detail and "superuser" in ext.detail
    assert report.database_dropped is True and temp_databases() == []
    assert (world.target_dir / own.set_id / MANIFEST).is_file()
