# Backup and restore (P33)

Hermclaw backs up its own state to Terra/Unraid `.60` every night and proves once a week that the newest backup can
actually be restored (Bauplan §36, Prompt §26: *restore test is mandatory*; research 20261008-022). Git repositories
are not part of this backup – they live in GitLab `.226`.

Architecture and guarantees: [`docs/architecture/backup.md`](../architecture/backup.md). Share setup on the Unraid
box: [`infra/backup/unraid-share-setup.md`](../../infra/backup/unraid-share-setup.md).

> Status: implemented and tested locally against PostgreSQL 16, a local directory target, rsync to a local path and
> rsync over SSH to a loopback `sshd` with an `rrsync`-restricted key. The run against the real `.60` is blocked by
> BLOCKER-001 (LAN unreachable from the build environment) – see "First run on .60" below
> (`tests/integration/test_backup_live.py`, run by `sudo scripts/ops/live-acceptance.sh --suite backup`).

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
keys (`id_ed25519`, `*_ed25519`, `*_rsa`, …; the public keys `*.pub` are backed up), any file whose content looks like
a private key, any config text file with a plaintext
secret (token shapes, `password: <value>` that is not a `cred:`/`file:`/`env:` reference, a password inside a database
URL), and every file the configuration references as a `file:` secret. Each exclusion is listed in
`MANIFEST.json` (`config.excluded`). Symlinks, special files and unreadable files are skipped and listed
(`config.skipped`). Database credentials reach `pg_dump`/`pg_restore` only via a private `PGPASSFILE` – never argv,
logs or events.

## Install (once, on the orchestrator `.225`)

The deployment (`playbooks/site.yml` + `playbooks/deploy.yml`, `docs/operations/deployment.md`) already provides
everything that is not site-specific:

* `/opt/hermclaw/bin/hermclaw-backup` and `/opt/hermclaw/bin/hermclaw-restore-test` (release scripts, role
  `hermclaw_release`), the commands the units start;
* the operator CLI `/usr/local/sbin/hermclaw` (role `hermclaw_orchestrator`): `sudo hermclaw backup <command> …`
  runs every backup command in the services' context (see "Daily operation");
* the units `hermclaw-backup.service/.timer` (daily 02:30) and `hermclaw-restore-test.service/.timer` (Sunday 04:30),
  rendered by `systemd_skeleton` with the environment file, `HERMCLAW_BACKUP_CONFIG`, exit code 75 = locked counts as
  success and the credential `backup-manifest-key` when it exists; both timers are enabled by `deploy.yml`. Until
  `/etc/hermclaw/backup.yaml` exists, every run is skipped (`ConditionPathExists`);
* the clean restore environment (role `postgresql`, `postgresql_restore_test_enabled: true`): `CREATEDB` for the
  role `hermclaw` and the template database `hermclaw_restore_template` (owner `postgres`, extension `vector`,
  `IS_TEMPLATE true`, `ALLOW_CONNECTIONS false`, updated together with pgvector).

`infra/backup/hermclaw-{backup,restore-test}.{service,timer}` are the reference form of the same units for a host
without the deployment. Do not copy them over the deployed units: the deployment refuses to overwrite unit files it
does not manage (`… exists and is not managed by Hermclaw`).

What remains is site-specific:

```bash
# 1. configuration (target, retention, restore_test.template = hermclaw_restore_template)
sudo install -m 0640 -o root -g hermclaw /opt/hermclaw/current/backup/backup.example.yaml /etc/hermclaw/backup.yaml
sudoedit /etc/hermclaw/backup.yaml

# 2. target: rsync over SSH (preferred) or a mounted share – infra/backup/unraid-share-setup.md
#    Terra's host key comes from the inventory (common_known_hosts_extra, INSTALLATION.md 4.3) into
#    /etc/hermclaw/ssh/known_hosts; a mounted share also needs systemd_skeleton_backup_write_paths (step 4)

# 3. manifest signing key (optional, recommended): systemd credential, referenced as cred:backup-manifest-key.
#    Create a random value (openssl rand -hex 32) and store it in the password manager first – without it signed
#    sets cannot be verified on a new host. Then paste it (not echoed). It is stored as
#    /etc/credstore.encrypted/hermclaw-backup-manifest-key, the path the units and the operator CLI load.
sudo /opt/hermclaw/bin/hermclaw-credential set backup-manifest-key

# 4. re-run the deployment of the orchestrator (admin machine, in infra/ansible), so the units load the new
#    credential (and, for a mounted share, may write to it: systemd_skeleton_backup_write_paths:
#    [/mnt/terra-backup/hermclaw] in the inventory). The operator CLI loads the credential as soon as it exists.
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --limit orchestrator -K

# 5. first run by hand, then let the timers take over
sudo systemctl start hermclaw-backup.service && sudo journalctl -u hermclaw-backup -n 20 -o cat
sudo systemctl start hermclaw-restore-test.service && sudo journalctl -u hermclaw-restore-test -n 20 -o cat
systemctl list-timers 'hermclaw-*'
```

Without the deployment (a hand-built host), additionally: `ALTER ROLE hermclaw CREATEDB`, the template database
(`createdb -O postgres -T template0 -E UTF8 hermclaw_restore_template`, `CREATE EXTENSION vector` inside it,
`ALTER DATABASE hermclaw_restore_template IS_TEMPLATE true ALLOW_CONNECTIONS false`), the two scripts in
`/opt/hermclaw/bin/` and the four units of `infra/backup/` in `/etc/systemd/system/`
(`systemctl enable --now hermclaw-backup.timer hermclaw-restore-test.timer`). Such a host has no operator CLI: run
the commands through the units.

## Daily operation

Run every command on `.225` in the services' context. The database URL comes from `/etc/hermclaw/hermclaw.env`
(Unix socket, peer authentication) and the manifest key is a systemd credential. Neither exists in a plain
`sudo -u hermclaw` shell: there the commands fail with `SECRET_MISSING` or a database authentication error.

* scheduled work through the units: `sudo systemctl start hermclaw-backup.service` /
  `sudo systemctl start hermclaw-restore-test.service`, output in
  `sudo journalctl -u hermclaw-backup -u hermclaw-restore-test`;
* everything else through the operator CLI of the deployment: `sudo hermclaw backup <command> …` runs the commands of
  `python -m hermclaw.backup` as `hermclaw` in a transient unit, with the environment file and the credentials of the
  services (`backup-manifest-key` included when it exists). Do not call `/opt/hermclaw/bin/hermclaw-backup` or
  `hermclaw-restore-test` directly: they are what the units start.

| Task | Command (on `.225`) |
|---|---|
| backup now | `sudo systemctl start hermclaw-backup.service`, or `sudo hermclaw backup create` |
| backup without retention | `sudo hermclaw backup create --no-prune` |
| list sets on the target | `sudo hermclaw backup list` |
| verify the newest set (checksums, archives, dump catalog) | `sudo hermclaw backup verify --latest` (`--quick`: checksums only) |
| restore test of the newest set | `sudo systemctl start hermclaw-restore-test.service`, or `sudo hermclaw backup restore-test --latest` |
| restore test of a given set | `sudo hermclaw backup restore-test hermclaw-20261015T023000Z-3fa1c2` |
| preview retention | `sudo hermclaw backup prune --dry-run` |
| apply retention | `sudo hermclaw backup prune` (runs automatically after each backup) |

Every command prints one JSON document on stdout (logs as JSON on stderr) and exits with `0` ok, `1` verification
or restore test failed, `2` configuration error, `3` operation failed, `75` another backup operation holds the
lock (the timer simply runs again next time). One host-wide lock (`<staging_dir>/.hermclaw-backup.lock`) serialises
backup, prune, verify and restore tests, so a prune never deletes a set that is being tested.

Monitoring: events `backup.started|completed|failed|pruned`, `backup.restore_test.passed|failed`,
`backup.restore.completed|failed` (global event stream of the API, `/api/events/stream`, token with scope `read`;
the UI itself does not show them), the journal of both units, and
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
with the state of the newest good backup. Keep at hand the password manager entry with the plaintext secrets: GitLab
token, worker tokens, backup manifest key, API admin token, the backup SSH key (or authorise a new one on `.60`), a
database password if you configured one. Encrypted credential blobs (`/etc/credstore.encrypted`) are bound to the
host key (or TPM) of the old installation. A reinstalled host cannot decrypt them.

The order follows what each part needs. The bootstrap installs the credential helper. The deployment refuses to run
without `gitlab-token`; it installs the release, the operator CLI and the backup scripts, and migrates the database.
`restore-full` needs all of that and an empty database. `ansible-playbook` runs on the admin machine in
`infra/ansible`, everything else on `.225`.

Only the database is damaged and the host is intact: stop the services and the timers (command at the end of step
3), then follow steps 5, 6, 8 and 10. Credentials, keys and configuration are still in place, and the admin token in
the backup still matches its credential.

1. **Rebuild the host**: reinstall Debian 13 with the same IP (INSTALLATION.md, chapter 3), then

   ```bash
   ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit orchestrator -K
   ```

   It installs PostgreSQL 17 + pgvector (role `hermclaw`, database `hermclaw`, the restore template), users,
   directories, the units and `/opt/hermclaw/bin/hermclaw-credential`. It generates new SSH keys in
   `/etc/hermclaw/ssh/` and new random credentials `api-admin-token`, `litellm-master-key` and
   `worker-token-<worker id>`. It never creates `gitlab-token` or `backup-manifest-key`.
2. **Credentials from the password manager.** `hermclaw-credential set` reads the value from stdin (not echoed) and
   stores it as `/etc/credstore.encrypted/hermclaw-<name>`, the path the units and the operator CLI load. `--force`
   replaces the value the bootstrap generated. The worker ids are the inventory's `hermclaw_worker_id`:

   ```bash
   sudo /opt/hermclaw/bin/hermclaw-credential set gitlab-token --force            # required: the deployment stops without it
   sudo /opt/hermclaw/bin/hermclaw-credential set backup-manifest-key --force     # signed sets are verified with it (steps 5, 6)
   sudo /opt/hermclaw/bin/hermclaw-credential set worker-token-exec-222 --force   # the old worker tokens: the workers need no change
   sudo /opt/hermclaw/bin/hermclaw-credential set worker-token-model-224 --force
   sudo /opt/hermclaw/bin/hermclaw-credential set api-admin-token --force         # optional: clients keep their admin token
   sudo /opt/hermclaw/bin/hermclaw-credential list
   ```

   `litellm-master-key` keeps the bootstrap's new value: only LiteLLM and the scheduler on this host use it. Without
   the old worker tokens, keep the new ones and hand them to the workers in step 9.
3. **Deploy the release that ran before** (the same `hermclaw_version`; the set's `MANIFEST.json` records it as
   `hermclaw_version`):

   ```bash
   ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --limit orchestrator -K
   ```

   It installs the release, the operator CLI `/usr/local/sbin/hermclaw` and the backup scripts, renders the
   configuration and the units (the backup units now load `backup-manifest-key`), migrates the empty database to the
   release head, starts LiteLLM, SearXNG, the API, the scheduler and the backup timers, and stores a new admin API
   token (credential `api-operator-token`, see step 11). If only the final `hermclaw doctor` fails (for example the
   workers, when they get their new tokens only in step 9), the deployment itself is complete: note the failed checks
   and go on, step 10 runs the doctor again. Then stop everything that writes to the database, and the timers:

   ```bash
   sudo systemctl stop hermclaw-scheduler hermclaw-api hermclaw-backup.timer hermclaw-restore-test.timer
   ```
4. **Reach the backups.**
   * Backup SSH key: either authorise the new key the bootstrap generated on `.60`
     (`sudo cat /etc/hermclaw/ssh/backup_ed25519.pub`, line format INSTALLATION.md 6.2; remove the old line), or put
     the old key from the password manager in place (copy it to your home directory on `.225` first):

     ```bash
     sudo install -m 0600 -o hermclaw -g hermclaw ~/backup_ed25519 /etc/hermclaw/ssh/backup_ed25519 && shred -u ~/backup_ed25519
     sudo ssh-keygen -y -f /etc/hermclaw/ssh/backup_ed25519 | sudo tee /etc/hermclaw/ssh/backup_ed25519.pub >/dev/null
     ```

     The private key stays `hermclaw:hermclaw` `0600`: the backup runs as `hermclaw`, and `ssh` refuses a key that
     others can read.
   * Terra's host key is already in `/etc/hermclaw/ssh/known_hosts` (bootstrap, `common_known_hosts_extra`).
   * `backup.yaml` as in install step 1 (the copy inside the set comes back in step 7), then list the sets:

     ```bash
     sudo install -m 0640 -o root -g hermclaw /opt/hermclaw/current/backup/backup.example.yaml /etc/hermclaw/backup.yaml
     sudoedit /etc/hermclaw/backup.yaml
     sudo hermclaw backup list
     ```
5. **Choose the set**: test the newest one; if it fails, test the next older one from the list. The report must say
   `"ok": true`.

   ```bash
   sudo hermclaw backup restore-test --latest        # or: sudo hermclaw backup restore-test <set-id>
   ```
6. **Empty database, then restore.** The database of step 3 is not empty (migrated, it holds the new admin token), and
   `restore-full` refuses a non-empty database. Re-create it as `postgres` – the API and the scheduler are stopped,
   LiteLLM does not use it – with the extension `vector`, which the role `hermclaw` may not create:

   ```bash
   sudo -u postgres dropdb --force hermclaw
   sudo -u postgres createdb -O hermclaw -T template0 -E UTF8 hermclaw
   sudo -u postgres psql -d hermclaw -c 'CREATE EXTENSION IF NOT EXISTS vector'
   sudo hermclaw backup restore-full --set <set-id> \
        --target-db-url 'postgresql+psycopg://hermclaw@/hermclaw?host=/run/postgresql' \
        --files-dest /var/lib/hermclaw/restore-files
   ```

   `restore-full` verifies the set first (refuses a set that fails verification), restores, and compares tables, row
   counts and the alembic revision with the manifest. The JSON report must say `"ok": true`. It extracts the config
   and artifacts tarballs into `--files-dest`: a new directory (`0700`, owner `hermclaw`) that must not exist yet or
   must be empty. `--force` (a non-empty database, `pg_restore --clean --if-exists`: objects of the backup are
   replaced, other objects stay) is not needed here. Pass a password only as `--password-ref cred:…|file:…`, never
   inside the URL on the command line.

   Roles: the bootstrap created `hermclaw` (peer authentication, no password). Only roles you added to the old cluster
   by hand are missing. They are in the set's `globals.sql` (on Terra, next to `MANIFEST.json`): apply them as
   superuser before the restore, `sudo -u postgres psql -f globals.sql` (role passwords are not in it unless
   `globals_role_passwords: true`).
7. **Put the files into place** (the restore never writes into `/etc` itself). Review first, then copy the
   configuration with the deployment's owner and modes (`root:hermclaw`, directories `0750`, files `0640`). Leave
   `ssh/` as the bootstrap made it (private keys `hermclaw:hermclaw` `0600`): the private keys are not in the set,
   the set's `*.pub` belong to the old host's keys, and step 9 registers the host keys in `known_hosts` again.

   ```bash
   sudo diff -r /var/lib/hermclaw/restore-files/config /etc/hermclaw
   sudo rsync -a --exclude=/ssh/ --exclude=/deploy-staging/ --chown=root:hermclaw --chmod=D0750,F0640 \
        /var/lib/hermclaw/restore-files/config/ /etc/hermclaw/
   sudo -u hermclaw rsync -a /var/lib/hermclaw/restore-files/artifacts/ /var/lib/hermclaw/artifacts/
   ```

   The deployment renders `hosts.yaml`, `models.yaml`, `policies.yaml`, `capabilities.yaml`, `logging.yaml`,
   `databases.yaml`, `litellm.yaml` and `hermclaw.env` from the inventory on every run. A difference there means the
   inventory changed since the backup: fix the inventory, not the file. Plaintext secret files
   (`/etc/hermclaw/secrets/*`, if you used any) were never backed up: put them back from the password manager
   (`sudo install -m 0600 -o hermclaw -g hermclaw <file> /etc/hermclaw/secrets/<name>`). Encrypted credentials do not
   come from the set either (`credstore_dirs: []`, INSTALLATION.md 11.4; blobs of the old installation would not
   decrypt here): step 2 re-created them. Delete `/var/lib/hermclaw/restore-files` (`sudo rm -rf`) once the system
   runs.
8. **Schema** of the installed release: `sudo hermclaw migrate` – a no-op when the backup's alembic revision is the
   release head, upgrades an older backup. The operator CLI runs it with `hermclaw.env` (Unix socket, peer
   authentication); in a plain `sudo -u hermclaw` shell it would connect over TCP without a password and fail.
9. **The other hosts and GitLab.** The bootstrap of step 1 generated a new runtime SSH key. Authorise it on `.222`,
   `.223` and `.224` (the run also registers their host keys in `/etc/hermclaw/ssh/known_hosts`):

   ```bash
   ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit webui,execution_workers,model_workers -K
   ```

   Git over SSH uses the same key: replace the deploy key of every Hermclaw project in GitLab with the new public key
   (`sudo cat /etc/hermclaw/ssh/id_ed25519.pub`, INSTALLATION.md 6.1 step 3). New worker tokens (step 2 without the
   old ones): before this run delete `/etc/credstore.encrypted/hermclaw-worker-token` on each worker, afterwards
   restart `hermclaw-exec-worker` (`.222`) and `hermclaw-model-worker` (`.224`).
10. **Start and check**:

    ```bash
    sudo systemctl restart hermclaw-litellm             # loads a restored litellm.yaml
    sudo systemctl start hermclaw-api hermclaw-scheduler hermclaw-backup.timer hermclaw-restore-test.timer
    curl -s 'http://192.168.178.225:8000/api/health?deep=true'    # "status": "ok"
    sudo hermclaw doctor
    ```

    The API listens on `HERMCLAW_API_HOST` (the LAN address of `.225`), not on `127.0.0.1`. The scheduler recovers
    the jobs that were in flight at backup time (P34).
11. **Admin API token.** The credential `api-operator-token` of step 3 names a token that was stored in the database
    that step 6 replaced: the API rejects it. The restored database holds the tokens of the old host instead
    (`sudo hermclaw doctor` lists the admin ones under `api_tokens`). Tokens whose value you still have keep working,
    and so does `api-admin-token` (step 2; it is not stored in the database). Let the deployment create a new
    operator token, then revoke the old host's token, whose value was lost with the old host:

    ```bash
    sudo rm /etc/credstore.encrypted/hermclaw-api-operator-token
    ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --limit orchestrator -K    # admin machine
    sudo systemd-creds decrypt --name=api-operator-token /etc/credstore.encrypted/hermclaw-api-operator-token -
    sudo hermclaw token-revoke operator-admin-<time>    # the older of the two operator-admin-* names in the doctor
    ```
12. **Fresh backup + restore test** – the first restore point of the new host:

    ```bash
    sudo systemctl start hermclaw-backup.service && sudo journalctl -u hermclaw-backup -n 30 -o cat
    sudo systemctl start hermclaw-restore-test.service && sudo journalctl -u hermclaw-restore-test -n 30 -o cat
    ```

Restore into a *different* database (forensics, comparison) works the same way: create it as in step 6 under another
name and pass that name in `--target-db-url`. `restore_test` never touches anything but its temporary database.

## First run on `.60` (cutover checklist, BLOCKER-001)

1. Share and key set up as in `infra/backup/unraid-share-setup.md`; `sudo hermclaw backup list` shows `"sets": []`.
2. `sudo hermclaw backup create` – exit 0, `transfer.verified: true`.
3. `sudo hermclaw backup restore-test --latest` – exit 0, `"ok": true`, `RESTORE_REPORT.json` on the share.
4. `sudo scripts/ops/live-acceptance.sh --suite backup` in the source checkout of the installed version
   (INSTALLATION.md, chapter 12): runs `tests/integration/test_backup_live.py` as `hermclaw` with the production
   configuration and credentials.
5. `systemctl list-timers 'hermclaw-*'` shows both timers; record the result in `STATUS.md` (backup/restore result).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `backup target … does not exist (share not mounted?)` | local target missing – mount the share (the directory is never created unless `create_missing: true`) |
| `… is not on a mounted file system` | `require_mount: true` and the share is not mounted |
| `rsync listing failed … No such file or directory` | base directory on `.60` missing – create `/mnt/user/backups/hermclaw` |
| `Host key verification failed` | Terra's host key not (or wrongly) in `/etc/hermclaw/ssh/known_hosts` – set it in the inventory (`common_known_hosts_extra`, INSTALLATION.md 4.3) and re-run the orchestrator bootstrap |
| `CREATE EXTENSION vector failed … create it as superuser first` | restore test: configure `restore_test.template` (install step 1); `restore-full`: create the extension in the target database as `postgres` first (Disaster recovery, step 6) |
| `secret 'cred:…' could not be resolved` (`SECRET_MISSING`, exit 2) | the command ran outside the services' context (use `sudo hermclaw backup …` or the units), or the credential `backup-manifest-key` does not exist (`sudo /opt/hermclaw/bin/hermclaw-credential list`) |
| `target database … is not empty` | `restore-full` into a database that is in use or was migrated by the deployment – re-create it first (Disaster recovery, step 6) |
| `manifest signature missing` | a key is configured but the set was made without one – verify such sets with a config without `manifest_signing_key_ref` after checking their origin |
| exit 75 | another backup/prune/restore test is running (`sudo cat <staging_dir>/.hermclaw-backup.lock` shows pid and operation) |
| `globals.included: false` in the manifest | the role may not read roles/tablespaces – harmless, recreate roles by hand on DR |
