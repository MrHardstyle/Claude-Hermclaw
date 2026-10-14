# Backup and restore (P33)

Hermclaw backs up its own state to Terra/Unraid `.60` every night and proves once a week that the newest backup can
actually be restored (Bauplan §36, Prompt §26: *restore test is mandatory*; research 20261008-022). Git repositories
are not part of this backup – they live in GitLab `.226`.

Architecture and guarantees: [`docs/architecture/backup.md`](../architecture/backup.md). Share setup on the Unraid
box: [`infra/backup/unraid-share-setup.md`](../../infra/backup/unraid-share-setup.md).

> Status: implemented and tested locally against PostgreSQL 16, a local directory target, rsync to a local path and
> rsync over SSH to a loopback `sshd` with an `rrsync`-restricted key. The run against the real `.60` is blocked by
> BLOCKER-001 (LAN unreachable from the build environment) – see "First run on .60" below
> (`pytest -m live tests/integration/test_backup_live.py`).

## What is backed up

| File in the set | Content | Step |
|---|---|---|
| `db.dump` | `pg_dump -Fc` of the Hermclaw database, taken on an exported snapshot | 33.1 |
| `globals.sql` | `pg_dumpall --globals-only --no-role-passwords` (roles, tablespaces); left out with a reason in the manifest when the role may not read them | 33.1 |
| `config.tar.gz` | the config dir (`/etc/hermclaw`) **without plaintext secrets** + encrypted credential stores (`credstore_dirs`) | 33.2 |
| `artifacts.tar.gz` | the artifact store (`<data_dir>/artifacts`) + `records/*.jsonl`: `research_runs`, `research_sources`, `research_claims`, `research_claim_sources`, `deployments` (deployment manifests) | 33.3 |
| `MANIFEST.json` | sha256 + size of every file, exact row count of every table (same snapshot as the dump), alembic revision, extensions, server/pg_dump/rsync/Python/Hermclaw versions, what was excluded/skipped and why | 33.1-33.4 |
| `MANIFEST.json.sig` | optional HMAC-SHA256 of the manifest (`manifest_signing_key_ref`) | 33.4 |
| `RESTORE_REPORT.json` | result of the last restore test of this set | 33.6 |

Never in a backup: `secrets/`, `private/`, `*.key`, `*.pem`, `*.p12`, `*.token`, `*.secret`, `.pgpass`, SSH private
keys (`id_*`, `*_ed25519`, …), any file whose content looks like a private key, any config text file with a plaintext
secret (token shapes, `password: <value>` that is not a `cred:`/`file:`/`env:` reference, a password inside a database
URL), and every file the configuration references as a `file:` secret. Each exclusion is listed in
`MANIFEST.json` (`config.excluded`). Symlinks, special files and unreadable files are skipped and listed
(`config.skipped`). Database credentials reach `pg_dump`/`pg_restore` only via a private `PGPASSFILE` – never argv,
logs or events.

## Install (once, on the orchestrator `.225`)

```bash
# 1. configuration
sudo install -m 0640 -o root -g hermclaw infra/backup/backup.example.yaml /etc/hermclaw/backup.yaml
sudoedit /etc/hermclaw/backup.yaml          # target, retention, restore_test.template

# 2. target: rsync over SSH (preferred) or a mounted share – infra/backup/unraid-share-setup.md
ssh-keyscan -t ed25519 192.168.178.60 | sudo tee -a /etc/hermclaw/ssh/known_hosts   # compare the fingerprint!

# 3. manifest signing key (optional, recommended): systemd credential, referenced as cred:backup-manifest-key
openssl rand -hex 32 | sudo systemd-creds encrypt --name=backup-manifest-key - /etc/credstore.encrypted/hermclaw-backup-manifest-key
#    keep a copy of the key in the password manager – without it signed sets cannot be verified on a new host

# 4. clean restore environment: the hermclaw role may create databases, a template has the extensions
sudo -u postgres psql -c 'ALTER ROLE hermclaw CREATEDB'
sudo -u postgres createdb -O hermclaw -T template0 -E UTF8 hermclaw_restore_template
sudo -u postgres psql -d hermclaw_restore_template -c 'CREATE EXTENSION vector'
sudo -u postgres psql -c "ALTER DATABASE hermclaw_restore_template IS_TEMPLATE true ALLOW_CONNECTIONS false"

# 5. units + timers (daily 02:30 backup, Sunday 04:30 restore test)
sudo install -m 0644 infra/backup/hermclaw-backup.{service,timer} infra/backup/hermclaw-restore-test.{service,timer} /etc/systemd/system/
sudo install -m 0755 scripts/ops/hermclaw-backup scripts/ops/hermclaw-restore-test /opt/hermclaw/bin/
sudo systemctl daemon-reload
sudo systemctl enable --now hermclaw-backup.timer hermclaw-restore-test.timer
```

The deployment playbook's `systemd_skeleton` role already renders a `hermclaw-backup.service/.timer` that calls
`/opt/hermclaw/bin/hermclaw-backup` (no arguments = `create`); the units in `infra/backup/` are the complete
versions (credentials, exit code 75, restore test).

## Daily operation

Run the commands in the services' context – the database URL comes from `/etc/hermclaw/hermclaw.env` and the
manifest key is a systemd credential, neither exists in a plain `sudo -u hermclaw` shell:

* scheduled work through the units: `sudo systemctl start hermclaw-backup.service` /
  `sudo systemctl start hermclaw-restore-test.service`, output in `journalctl -u hermclaw-backup -u hermclaw-restore-test`;
* everything else through the operator CLI of the deployment (`sudo hermclaw backup <command> …`, the same commands as
  `python -m hermclaw.backup`, run as `hermclaw` with the environment file and credentials of the services).


| Task | Command (as `hermclaw`, e.g. `sudo -u hermclaw …`) |
|---|---|
| backup now | `/opt/hermclaw/bin/hermclaw-backup` (= `python -m hermclaw.backup create`) |
| backup without retention | `hermclaw-backup create --no-prune` |
| list sets on the target | `hermclaw-backup list` |
| verify the newest set (checksums, archives, dump catalog) | `hermclaw-backup verify --latest` (`--quick`: checksums only) |
| restore test of the newest set | `/opt/hermclaw/bin/hermclaw-restore-test` (= `restore-test --latest`) |
| restore test of a given set | `hermclaw-restore-test hermclaw-20261015T023000Z-3fa1c2` |
| preview retention | `hermclaw-backup prune --dry-run` |
| apply retention | `hermclaw-backup prune` (runs automatically after each backup) |

Every command prints one JSON document on stdout (logs as JSON on stderr) and exits with `0` ok, `1` verification
or restore test failed, `2` configuration error, `3` operation failed, `75` another backup operation holds the
lock (the timer simply runs again next time). One host-wide lock (`<staging_dir>/.hermclaw-backup.lock`) serialises
backup, prune, verify and restore tests, so a prune never deletes a set that is being tested.

Monitoring: events `backup.started|completed|failed|pruned`, `backup.restore_test.passed|failed`,
`backup.restore.completed|failed` (UI event stream, `/api/events`), the journal of both units, and
`RESTORE_REPORT.json` next to each tested set. **Alert when there is no `backup.completed` within 26 h or no
`backup.restore_test.passed` within 8 days.**

Retention (`retention:`): the newest `keep_last` sets always stay; additionally the newest set of each of the last
`keep_daily` days, `keep_weekly` ISO weeks and `keep_monthly` months that have a backup (UTC dates). Incomplete
sets (interrupted transfer: no `MANIFEST.json`, or a `<set>.partial` directory) are removed once older than
`stale_partial_hours`. The set just created is never pruned by its own run.

## What the restore test proves (33.5, 33.6)

1. The set is read from the target (rsync targets: fetched into a private work dir) and verified: manifest schema and
   set id, HMAC signature (mandatory when a key is configured), size and sha256 of every file, every tarball read to
   its end (gzip CRC; members relative, `..`-free, only files/directories), file counts per top-level directory and
   the line count of each `records/*.jsonl` against the manifest, `pg_restore --list` of the dump.
2. The config tarball is extracted into a temporary directory with the `data` extraction filter (no absolute paths,
   links, devices) and the file count compared with the manifest.
3. A **clean temporary database** `hermclaw_restoretest_<8 hex>` is created (from `restore_test.template`, or
   `template0` with the source encoding), extensions first, then
   `pg_restore --no-owner --no-privileges --exit-on-error --single-transaction`.
4. Every table of the manifest must exist and have exactly the recorded row count (the counts were taken on the
   snapshot of the dump), the alembic revision and the extensions must be equal, the record exports must match the
   snapshot.
5. `RESTORE_REPORT.json` (all checks, per-table expected/actual counts, versions, duration) is written next to the
   set and returned; the temporary database is **always** dropped (`DROP DATABASE … WITH (FORCE)`); databases left
   behind by a crashed run are dropped by the next run.

A failed check never stops the remaining checks (a modified artifacts tarball still lets the database part be
restored and compared); a dump that fails its checksum is never fed to `pg_restore`.

## Disaster recovery

Situation: the orchestrator `.225` (database, config, artifacts) is lost or corrupted. Target: a running Hermclaw
with the state of the newest good backup. Keep at hand: the password manager entry with the plaintext secrets
(API admin token, LiteLLM master key, GitLab token, worker tokens, DB password if any, backup manifest key) –
encrypted credential blobs (`credstore.encrypted`) are bound to the host key of the old machine (or its TPM) and
are only usable when restoring onto that same host.

1. **Rebuild the host** with the deployment playbook (P01/P32: OS, PostgreSQL 17 + pgvector, users, venv,
   units – services stay stopped): `ansible-playbook -i inventory/hosts.yml playbooks/site.yml --limit orchestrator`,
   then `sudo systemctl stop hermclaw-scheduler hermclaw-api`.
2. **Reach the backups**: put the backup key `/etc/hermclaw/ssh/backup_ed25519` (from the password manager, or a new
   key authorized on `.60`) and Terra's host key into `/etc/hermclaw/ssh/known_hosts`, install
   `/etc/hermclaw/backup.yaml` (from `infra/backup/backup.example.yaml`; the copy inside the backup is restored in
   step 5), then `sudo -u hermclaw /opt/hermclaw/bin/hermclaw-backup list`.
3. **Choose the set**: the newest set whose `RESTORE_REPORT.json` says `"ok": true`, or test it now:
   `sudo -u hermclaw /opt/hermclaw/bin/hermclaw-restore-test <set-id>`.
4. **Roles** (only if the cluster is new and the roles are missing): extract `globals.sql` from the set and apply it
   as superuser – `sudo -u postgres psql -f globals.sql` (role passwords are not in it unless
   `globals_role_passwords: true`; set them again, the default setup uses peer authentication without passwords).
5. **Database + files** into an empty database:

   ```bash
   sudo -u postgres createdb -O hermclaw -T template0 -E UTF8 hermclaw      # skip if it exists and is empty
   sudo -u postgres psql -d hermclaw -c 'CREATE EXTENSION IF NOT EXISTS vector'
   sudo -u hermclaw /opt/hermclaw/venv/bin/python -m hermclaw.backup restore-full \
        --set <set-id> \
        --target-db-url 'postgresql+psycopg://hermclaw@/hermclaw?host=/run/postgresql' \
        --files-dest /var/lib/hermclaw/restore-files
   ```

   `restore-full` verifies the set first (refuses a set that fails verification), refuses a non-empty database
   unless `--force` (then `pg_restore --clean --if-exists`: objects of the backup are replaced, other objects stay –
   prefer a fresh database), restores, and compares tables/row counts/alembic revision with the manifest. The JSON
   report must say `"ok": true`. Pass a password only as `--password-ref cred:…|file:…`, never inside the URL on the
   command line.
6. **Put files into place** (the restore never writes into `/etc` itself):

   ```bash
   cd /var/lib/hermclaw/restore-files
   sudo rsync -a config/ /etc/hermclaw/          # review first: diff -r config /etc/hermclaw
   sudo chown -R root:hermclaw /etc/hermclaw && sudo chmod -R g-w,o-rwx /etc/hermclaw
   sudo -u hermclaw rsync -a artifacts/ /var/lib/hermclaw/artifacts/
   # same host only: sudo rsync -a credstore/credstore.encrypted/ /etc/credstore.encrypted/
   ```

   Secrets that were never backed up (`/etc/hermclaw/secrets/*`, SSH private keys, systemd credentials on a new
   host) are re-created from the password manager (`systemd-creds encrypt --name=<name> - /etc/credstore.encrypted/<name>`).
7. **Schema** of the installed release: `sudo -u hermclaw /opt/hermclaw/venv/bin/hermclaw migrate` (a no-op when
   the release matches the backup's alembic revision; upgrades an older backup).
8. **Start and check**: `sudo systemctl start hermclaw-api hermclaw-scheduler`,
   `curl -s http://127.0.0.1:8000/api/health?deep=true`, `hermclaw doctor`; the scheduler recovers jobs that were
   in flight at backup time (P34). Then run a fresh backup + restore test.

Restore into a *different* database (forensics, comparison) works the same way with another `--target-db-url`;
`restore_test` never touches anything but its temporary database.

## First run on `.60` (cutover checklist, BLOCKER-001)

1. Share and key set up as in `infra/backup/unraid-share-setup.md`; `hermclaw-backup list` returns `{"sets": []}`.
2. `sudo -u hermclaw /opt/hermclaw/bin/hermclaw-backup` – exit 0, `transfer.verified: true`.
3. `sudo -u hermclaw /opt/hermclaw/bin/hermclaw-restore-test` – exit 0, `"ok": true`, `RESTORE_REPORT.json` on the share.
4. `HERMCLAW_BACKUP_CONFIG=/etc/hermclaw/backup.yaml .venv/bin/pytest -m live tests/integration/test_backup_live.py`.
5. `systemctl list-timers 'hermclaw-*'` shows both timers; record the result in `STATUS.md` (backup/restore result).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `backup target … does not exist (share not mounted?)` | local target missing – mount the share (the directory is never created unless `create_missing: true`) |
| `… is not on a mounted file system` | `require_mount: true` and the share is not mounted |
| `rsync listing failed … No such file or directory` | base directory on `.60` missing – create `/mnt/user/backups/hermclaw` |
| `Host key verification failed` | Terra's host key not (or wrongly) in `/etc/hermclaw/ssh/known_hosts` |
| `CREATE EXTENSION vector failed … create it as superuser first` | configure `restore_test.template` (install step 4) |
| `manifest signature missing` | a key is configured but the set was made without one – verify such sets with a config without `manifest_signing_key_ref` after checking their origin |
| exit 75 | another backup/prune/restore test is running (`cat <staging_dir>/.hermclaw-backup.lock` shows pid and operation) |
| `globals.included: false` in the manifest | the role may not read roles/tablespaces – harmless, recreate roles by hand on DR |
