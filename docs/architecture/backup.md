# P33 – Backup/Restore (`hermclaw.backup`)

Status: implemented and tested locally (PostgreSQL 16, local directory target, rsync to a local path, rsync over SSH
to a loopback `sshd` with an `rrsync`-restricted key). The real Terra/Unraid `.60` target is blocked by BLOCKER-001;
live acceptance: `tests/integration/test_backup_live.py`. Operator procedure incl. disaster recovery:
[`docs/operations/backup-restore.md`](../operations/backup-restore.md).

Sources: Bauplan §36 (BACKUP), Prompt §26, Phase 33 (33.1-33.6), research 20261008-022 (backup/restore),
-021 (SSH key restrictions), -020 (systemd credentials).

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | `BackupConfig` (`backup.yaml`), targets `LocalTarget` / `RsyncTarget` (discriminated by `kind`), `RetentionPolicy`, `RestoreTestConfig`; strict validation (absolute paths, no whitespace/quotes/`..`/`:`/leading `-` in anything that reaches rsync/ssh, plain SQL identifiers); `host_id` resolution against `hosts.yaml` (`terra-60`) |
| `pg.py` | `PgConn` from the SQLAlchemy URL → libpq env (`PGHOST`…) + private `PGPASSFILE`; catalog queries (tables, exact counts, alembic revision, extensions); `find_pg_tool` (newest installed major ≥ server); `filter_toc` |
| `runner.py` | subprocess runner: no shell, minimal env (parent env not inherited – no stray `PGPASSWORD`), own process group killed on timeout/cancellation, redacted error text |
| `archive.py` | deterministic tarballs (`os.walk` without following links, exclusion patterns, private-key sniffing, plaintext-secret scan, secret-reference exclusion) and safety inspection/extraction (`data` filter) |
| `manifest.py` | set layout, set ids `hermclaw-<UTC yyyymmddThhmmssmmmZ>-<6 hex>`, `Manifest` schema (`hermclaw-backup/1`), HMAC signature |
| `transfer.py` | `TargetStore` protocol; `LocalStore` (copy into `<set>.partial`, read-back sha256, atomic rename) and `RsyncStore` (`rsync -a --partial` over SSH, `--checksum --dry-run --itemize-changes` verification, listing/deletion via rsync only) |
| `retention.py` | pure GFS planning (keep_last / daily / weekly / monthly, stale incomplete sets, protected ids) |
| `lock.py` | host-wide `flock` serialising create/prune/verify/restore |
| `verify.py` | set verification (quick: manifest+checksums; deep: + archives + dump catalog) → `VerifyReport` |
| `backup.py` | `create_backup`, `prune_backups`, `list_backups` |
| `restore.py` | `restore_test`, `restore_full`, `RestoreReport` |
| `events.py` | `backup.*` events (best effort) + JSON log lines |
| `cli.py`, `__main__.py` | `python -m hermclaw.backup {create,verify,restore-test,restore-full,list,prune}` |

Ops: `scripts/ops/hermclaw-backup`, `scripts/ops/hermclaw-restore-test`, `infra/backup/*.service|timer`,
`infra/backup/backup.example.yaml`, `infra/backup/unraid-share-setup.md`.

## Public interface

```python
async def create_backup(target: BackupTarget | None = None, *, config: BackupConfig | None = None,
                        settings=None, sessionmaker=None, hosts=None, store=None, prune: bool | None = None,
                        now: datetime | None = None) -> BackupResult
async def restore_test(backup_set: str | Path = "latest", *, config=None, target=None, settings=None,
                       sessionmaker=None, hosts=None, store=None, write_report: bool = True) -> RestoreReport
async def restore_full(backup_set: str | Path, target_db_url: str, *, force: bool = False,
                       password_ref: str | None = None, files_dest: Path | None = None, config=None, …) -> RestoreReport
async def verify_set_dir(directory: Path, *, signing_key: bytes | None = None, deep: bool = True, …)
    -> tuple[VerifyReport, Manifest | None]
async def prune_backups(config, *, dry_run: bool = False, …) -> PruneResult
async def list_backups(config, …) -> list[SetListing]          # newest first
```

`backup_set` is a set id, `"latest"` (newest complete set on the target) or a set directory (`Path`).

## Backup flow (33.1-33.4)

```text
lock ─► staging/<set> (0700) ─► DB: REPEATABLE READ READ ONLY txn ─ pg_export_snapshot()
                                     ├─ exact count(*) of every table, alembic revision, extensions, encoding
                                     ├─ records/<table>.jsonl (row_to_json, server-side cursor)
                                     └─ pg_dump -Fc --snapshot=<id>   (PGPASSFILE; argv without credentials)
                                pg_restore --list (dump readable, TOC entry count)
                                pg_dumpall --globals-only --no-role-passwords (skipped + reason if not permitted)
                                config.tar.gz  (config dir − secrets + encrypted credstores)
                                artifacts.tar.gz (artifact store + records/)
                                MANIFEST.json (+ .sig) written last ─ quick self-verification
        ─► target.upload (local: .partial → read-back sha256 → rename | rsync: data → checksum dry-run → manifest → dry-run)
        ─► backup.completed event ─► retention (protects the new set) ─► staging removed (always)
```

Consistency: the row counts in the manifest and the record exports are taken in the transaction whose snapshot
`pg_dump` uses, so the restore test can demand *exact* equality even while Hermclaw keeps writing. Should the
snapshot export fail (e.g. a standby), the manifest says `snapshot_consistent: false` and count differences become
warnings instead of failures.

Completeness marker: a set is complete only with `MANIFEST.json`. `LocalStore` writes into `<set>.partial` and
renames after verification; `RsyncStore` sends the manifest only after the data files passed the checksum dry-run.
Interrupted transfers are therefore never used for a restore and are cleaned up by retention once stale.

## Restore test (33.5, 33.6)

`verify_set_dir` (deep) → config extraction into a temp dir → temporary database `<prefix>_<8 hex>` (template or
`template0` + source encoding) → extensions created up front (TOC entries `EXTENSION`/`COMMENT ON EXTENSION`
commented out of the restore list: they need ownership a restore role does not have) → `pg_restore --no-owner
--no-privileges --exit-on-error --single-transaction --use-list` → comparison (tables present, exact counts,
alembic revision, extensions, record exports) → `RESTORE_REPORT.json` next to the set (`put_file`) → event. The
temporary database is dropped in a `finally` (shielded from cancellation); `<prefix>_<8 hex>` leftovers of crashed
runs are dropped at the start of the next run (exact name pattern, the backup lock is held).

`restore_full` uses the same `pg_restore_into` / `compare_with_manifest` against a target database, refuses
unverifiable sets and non-empty databases (unless `force` → `--clean --if-exists`) and extracts files only into an
empty `files_dest`.

## Security properties

* No shell anywhere; every value that reaches rsync/ssh argv or the `-e` command string is validated (no
  whitespace, quotes, `..`, `:` in paths, leading `-`), remote locations are `user@host:path` built from validated
  parts; set ids are matched against `^hermclaw-\d{8}T\d{6}(\d{3})?Z-[0-9a-f]{6}$` before any path join.
* SSH: dedicated key (`file:`/`cred:` used in place; `literal:`/`env:` – dev/test only – written to a 0600 temp file
  and removed), `-F /dev/null`, `BatchMode`, `IdentitiesOnly`, `StrictHostKeyChecking=yes` with Hermclaw's own
  known_hosts, no password/keyboard-interactive auth. Listing and deletion are rsync operations, so an `rrsync`
  forced command (`restrict,from="192.168.178.225",command="rrsync <dir>"`) suffices – tested.
* SQL: identifiers via `psycopg.sql.Identifier`, values as parameters; database names validated.
* Secrets: DB passwords only in a 0600 `PGPASSFILE` in a 0700 temp dir (removed), registered with the redactor;
  tool output is truncated + redacted before it reaches errors, logs or events; manifests and reports contain only
  credential-free connection descriptions; plaintext secrets never enter the config tarball.
* Archives: members must be relative, `..`-free, regular files or directories; extraction uses the `data` filter
  after inspection; local targets never follow a symlinked set directory.
* The staging dir must be separate from a local target (its stale-set cleanup could otherwise delete real sets).

## Tests

| File | What |
|---|---|
| `tests/unit/test_backup_units.py` | config validation, `host_id` → endpoint, exact ssh/rsync command lines, itemize/listing parsers, retention plans, exclusion patterns, secret detection, archive round trip, set ids + signature, libpq passfile, TOC filter, pg tool selection, lock |
| `tests/integration/test_backup_restore.py` | real PG: full backup to a local target (contents, exclusions, record exports, events), restore test passes with equal counts and dropped temp DB, tampered tarball/dump, count/alembic mismatch, signed manifests, retention prune, `latest` |
| `tests/integration/test_backup_rsync.py` | rsync to a local path (backup/list/restore/prune), checksum verification catches same-size/same-mtime corruption, rsync over SSH with an `rrsync`-restricted key (backup, fetch + restore test, report put back, prune), unknown host key refused, absolute remote path |
| `tests/integration/test_backup_cli.py` | `restore_full` (refusals, empty DB, files, force), CLI + wrapper scripts end to end, exit codes 0/1/2/3/75 |
| `tests/failure/test_backup_failures.py` | unmounted share, `require_mount`, DB unreachable, failing pg_dump (redacted), globals not permitted, lock contention, interrupted copy, cancellation kills pg_dump, timeout kills the process group, missing rsync, password never in argv/events/logs, stale temp DB cleanup, unreachable restore server, truncated/crafted archives and dumps with matching checksums, incomplete sets, prune errors, symlinked sets, staging inside the target, unreadable config files |
| `tests/integration/test_backup_privileges.py` | least privilege: a non-superuser role with only `CREATEDB` restores (`restore_full`), backs up (incl. globals) and restore-tests via the template with `vector`; without the template the report fails with a clear extension message |
| `tests/integration/test_backup_live.py` | `@pytest.mark.live`: real `.60` (BLOCKER-001) |
