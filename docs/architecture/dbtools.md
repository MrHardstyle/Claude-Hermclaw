# Database Tools (P27, Bauplan §30)

`hermclaw/dbtools/` gives `database` steps six tools for the databases of **user projects** (never Hermclaw's own
database). Core rule of §30: *keine unkontrollierten Produktions-DB-Mutationen* – registered targets are only ever read;
everything that writes happens in throw-away sandbox databases the tools create and always drop again.

| Tool | Step | What it does |
|---|---|---|
| `db_introspect` | 27.1 | SQLAlchemy reflection (batched `get_multi_*`) in a read-only session: schemas → tables (columns with type/nullability/default/identity/computed, PK, indexes, unique/check constraints, FKs, comments), views, materialized views; compact text for the model + structural fingerprint |
| `db_query_readonly` | 27.2 | one statically checked read statement, DB-enforced read-only, row cap (`row_limit + 1` fetched via server-side cursor), statement timeout, always rolled back |
| `db_migration_generate` | 27.3 | next migration skeleton of the project's framework (alembic / Django / raw SQL) with up/down sections, deterministic, no model |
| `db_migration_apply_sandbox` | 27.4 | sandbox DB (optionally seeded with a target's schema), upgrade → downgrade → upgrade, schema fingerprints prove both directions |
| `db_backup` | 27.5 | `pg_dump -Fc` (credentials in `PGPASSWORD`) / SQLite backup API → artifact store (`artifacts.kind=backup`, sha256, row-count snapshot) |
| `db_restore_test` | 27.6 | checksum, restore into a sandbox (`pg_restore --no-owner --no-privileges --exit-on-error`), table list + exact row counts vs. the backup snapshot |

## Modules

| Module | Responsibility |
|---|---|
| `targets.py` | `DatabaseTarget` (name, `dsn_ref` secret reference, optional `password_ref`, kind `postgresql`/`sqlite`/`mysql`, environment `production`/`staging`/`sandbox`, `read_only`), `DbToolsConfig` (`databases.yaml`), DSN parsing (URL + libpq keywords + SQLite paths), `ConnectionInfo` (password never in `repr`/`display`; `libpq_env()` for client tools), `redact_dsn`, `TargetRegistry` |
| `sqlguard.py` | lexer per dialect (strings, `E''`, dollar quotes, quoted identifiers, nested comments) + read-only check |
| `query.py` | PostgreSQL and SQLite execution with the database-side enforcement |
| `introspect.py` | reflection, rendering, fingerprint |
| `migrations.py` | framework detection, skeleton generation, raw SQL parsing/application, drivers (`RawSqlDriver`, `AlembicDriver`, `CommandDriver`), `verify_migrations` |
| `sandbox.py` | `PgSandboxServer` (database + isolated role per sandbox, drop, orphan clean-up), `SqliteSandboxes` |
| `backup.py` | `BackupService` (backup, restore test, schema seeding), `ArtifactStore`, TOC filtering, row counts |
| `runner.py` | `run_process` (no shell, minimal env, process-group kill, bounded output), `MigrationCommandRunner` (`ExecutorMigrationRunner` = sandbox `CommandExecutor`, `LocalMigrationRunner` = dev/test only) |
| `policy.py` | `DbPolicy` (capability gate, target matrix, sandbox-name guard), `DbAuditor` (`db.tool` events, `command_runs`) |
| `tools.py` | `DbTools` facade: one audited `_call` per tool |
| `provider.py` | `DbOpsProvider` for the tool engine + `build_provider(config, sessionmaker, settings, *, executor=None)` used by `hermclaw.runtime.ops` |

## Configuration – `<config_dir>/databases.yaml`

Absent file = no targets (generate/apply still work, every target name is unknown).

```yaml
targets:
  - name: shop
    dsn_ref: cred:shop-db-dsn            # postgresql://reader@db.lan:5432/shop?sslmode=require
    password_ref: cred:shop-db-password  # optional, overrides the DSN's password
    kind: postgresql                     # postgresql | sqlite | mysql (accepted, reported as unsupported)
    environment: production              # default production; production forces read_only
    read_only: true                      # default true
  - name: app-sqlite
    dsn_ref: literal:/srv/app/data/app.db   # literal: only outside production
    kind: sqlite
    environment: staging
sandbox:
  admin_dsn_ref: cred:sandbox-pg-admin   # role with CREATEDB + CREATEROLE on a NON-production server
  name_prefix: hermclaw_sbx_
  client_host: 192.168.178.225           # how the sandbox executor (.222) reaches the server (optional)
  orphan_max_age_hours: 6
limits:                                  # all optional
  default_row_limit: 100
  max_row_limit: 5000
  default_timeout_seconds: 30
  max_timeout_seconds: 600
  max_cell_chars: 2000
  migration_timeout_seconds: 900
  command_timeout_seconds: 3600
pg_bin_dir: null            # default: newest /usr/lib/postgresql/<major>/bin ≥ server major, else PATH
backup_dir: null            # default: <data_dir>/artifacts/db-backups
sqlite_sandbox_dir: null    # default: system temp dir
migration_python: python3   # interpreter that runs alembic (inside the sandbox image in production)
allowed_capabilities: [database]
```

Secret references follow `hermclaw.security.secrets` (`cred:` systemd credential, `file:`, `env:`/`literal:` outside
production only). Resolved DSNs and passwords are registered with the redactor.

## Policy (27.7)

| Operation | production / read-only target | staging (read-only) | sandbox target, writable | sandbox DB created by the tools |
|---|---|---|---|---|
| introspect, query, backup, schema seed (read) | yes | yes | yes | – |
| migration apply, restore | **no** | **no** | (no tool writes registered targets) | yes |

* Capability gate: the tool engine offers DB tools only to mutating steps with capability `database`
  (`OPS_TOOLS_BY_CAPABILITY`); `DbPolicy.check_caller` repeats the check (`DB_CAPABILITY_DENIED`, actor
  `runtime`/`operator` exempt).
* `DatabaseTarget` refuses `environment: production` with `read_only: false`; unlabelled targets default to
  production + read-only.
* Only names matching `<prefix><12 hex>` are ever dropped (`DbPolicy.check_sandbox_name`).
* Audit: every call – ok, failed, timed out, denied, cancelled – writes exactly one `db.tool` event (payload: tool,
  outcome, actor, capability, credential-free target description, SQL text for queries, step/check summaries, error
  code; never result rows). External commands (`pg_dump`, `pg_restore`, alembic, custom commands) get a
  `command_runs` row before they start, completed afterwards. Everything passes the redactor + `redact_dsn`; tool
  output to the model passes the redactor as well.

## Read-only queries (27.2) – two independent layers

1. **Static** (`sqlguard.check_readonly`): exactly one statement (optional trailing `;`), starting with
   `SELECT`/`WITH`/`VALUES`/`TABLE`/`SHOW` (SQLite: allow-listed read `PRAGMA`s without assignment) or `EXPLAIN` without
   `ANALYZE`; no data-modifying keyword anywhere (`INSERT`/`UPDATE`/`DELETE`/`MERGE`/`INTO`/`COPY`/DDL/…, which covers
   data-modifying CTEs and `SELECT … INTO`), no `FOR UPDATE/SHARE`, no deny-listed function (`pg_terminate_backend`,
   `pg_cancel_backend`, `lo_*`, `dblink*`, `set_config`, `nextval`/`setval`, `pg_read_file`, `pg_ls_*`, advisory
   locks, `query_to_xml`, `*_reset`, SQLite `load_extension`/`writefile` …) – also when schema-qualified, quoted or
   separated by comments. Unicode-escaped identifiers (`U&"…"`) are rejected because they could hide a function name.
   Unterminated literals/comments are rejected.
2. **Database**: PostgreSQL sessions start with `default_transaction_read_only=on`, `standard_conforming_strings=on`;
   each query runs in a transaction opened with `SET TRANSACTION READ ONLY` plus `statement_timeout`/`lock_timeout`
   and is rolled back; a cancelled call cancels the server query. SQLite opens `file:…?mode=ro`, sets
   `PRAGMA query_only`, installs an authorizer (reads, read PRAGMAs and non-denied functions only) and a progress
   handler for the timeout. Tests prove writes are refused by the database even when the static layer is bypassed.

## Migrations (27.3 / 27.4)

Detection order: alembic (`alembic.ini`, `[tool.alembic]` in `pyproject.toml`, Flask-Migrate `migrations/{env.py,
versions/}`), Django (`manage.py` + an app with `migrations/__init__.py`; with several apps the app directory is passed
as `path`), knex / Laravel / Prisma / Rails (**reported only**, with the CLI command to use), raw SQL (numbered `*.sql`
in `migrations/`, `db/migrations/`, `sql/migrations/`, `database/migrations/`, …).

Generation:

* **alembic**: revision graph parsed with `ast` (no project code imported); refuses several heads; revision id =
  `sha256(parent|slug|n)[:12]` (deterministic, unique among existing ids); file name from the project's
  `file_template`/`truncate_slug_length` exactly like alembic's `_rev_path`; skeleton with `upgrade()`/`downgrade()`.
  The project's `script.py.mako` is deliberately *not* rendered (Mako templates can execute code).
* **Django**: `NNNN_<slug>.py`, dependencies = the highest-numbered migration(s) (several → merge), `RunPython(upgrade,
  downgrade)` plus a slot for schema operations.
* **raw SQL**: style inferred from the existing files – paired `<v>_<slug>.up.sql`/`.down.sql` (golang-migrate),
  Flyway `V<n>__`/`U<n>__`, single files with dbmate (`-- migrate:up/down`), goose (`-- +goose Up/Down`), sql-migrate
  or plain `-- up`/`-- down` markers; next number keeps width and separator, 14-digit timestamps stay timestamps
  (strictly increasing); an empty `migrations/` gets `0001_<slug>.sql` with dbmate markers.
* Files are created with `O_EXCL|O_NOFOLLOW`, only inside the workspace (paths and alembic `script_location` /
  `version_locations` are resolved and must stay inside; never `.git`).

Sandbox verification (`verify_migrations`, scope `latest` by default):

```
latest: [upgrade to predecessor] → upgrade (head) → downgrade (predecessor) → upgrade again
        check: downgrade restores the predecessor's schema; re-upgrade reproduces the head schema
all:    upgrade → downgrade to the starting state → upgrade again (same two checks against the start)
none:   upgrade only
```

Fingerprints come from `db_introspect` on the sandbox without migration bookkeeping tables (`alembic_version`,
`django_migrations`, `schema_migrations`, `goose_db_version`, `flyway_schema_history`, …); a failed check carries a
readable diff (`+ column public.items.price` …). Missing down sections → upgrade verified, downgrade reported as not
verified.

* **Raw SQL** runs in-process as the sandbox's isolated role (PostgreSQL, simple query protocol, transaction per
  migration unless `transaction:false`/`NO TRANSACTION`, then statement by statement) or with an authorizer that
  forbids `ATTACH`/`DETACH`/`VACUUM INTO`/file functions and `SQLITE_LIMIT_ATTACHED=0` (SQLite). Never via `psql`
  (no `\!` meta commands).
* **alembic** runs the project's own alembic through a small wrapper (`python -c …`) that loads the project's config
  and sets `sqlalchemy.url` to the sandbox (URL only in the environment: `HERMCLAW_SANDBOX_URL`, `DATABASE_URL`,
  `SQLALCHEMY_DATABASE_URI`, `PG*`; driver scheme taken from the project's `sqlalchemy.url`), with
  `PYTHONDONTWRITEBYTECODE=1` (no `__pycache__` in the workspace). After every command the driver re-reads
  `alembic_version` **in the sandbox**: an `env.py` that ignores `sqlalchemy.url`/`DATABASE_URL` is reported as a
  failure ("may have migrated a different database").
* **Custom command** (`command`/`downgrade_command` argv, library API) – e.g. Django `manage.py migrate` – runs with
  the same environment through the runner.
* Project code (alembic, custom commands) runs through `ExecutorMigrationRunner` (the job's sandbox
  `CommandExecutor`, network on so it reaches the sandbox server via `client_host`); a SQLite sandbox is then placed
  in `<workspace>/.hermclaw-dbsandbox/` (URL relative to the command's cwd) and removed afterwards.
  `LocalMigrationRunner` (dev/test) refuses `HERMCLAW_ENV=production`.
* Seeding (`seed_schema_from=<target>`): PostgreSQL `pg_dump --schema-only` **plus the data of existing bookkeeping
  tables** (`pg_dump --data-only --table=…`), restored with `pg_restore` as the isolated role – so alembic/dbmate know
  which migrations the target already has and only the pending ones are applied. (The Bauplan sketch says
  `pg_dump | psql`; `pg_restore` was chosen because its TOC can be filtered for an unprivileged owner and it runs no
  client-side meta commands.) SQLite: online-backup copy of the target file.

## Sandbox server

Per sandbox: `CREATE ROLE hermclaw_sbx_<12hex> LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
NOINHERIT CONNECTION LIMIT 20 PASSWORD '<SCRAM verifier>' VALID UNTIL now()+1 day`, `CREATE DATABASE … OWNER <role>
TEMPLATE template0`, `REVOKE CONNECT, TEMPORARY … FROM PUBLIC`, `COMMENT ON DATABASE` with creation time. Migrations and
restores run as that role (cannot create roles, write server files, `ALTER SYSTEM` – tested). Extensions of a dump are
pre-created by the admin; `COMMENT ON EXTENSION` and event triggers are filtered from the restore list (they need
ownership/superuser) and reported as warnings. `DROP DATABASE … WITH (FORCE)` + `DROP ROLE` in a shielded `finally`.
`DbTools.cleanup_orphan_sandboxes()` (runtime/recovery) drops sandboxes older than `orphan_max_age_hours` – including
role-only leftovers; age comes from the comment or the role's `VALID UNTIL` – and stale SQLite sandbox directories.

**Operational requirement:** the sandbox server must be a dedicated non-production PostgreSQL instance (the admin role
needs `CREATEDB`/`CREATEROLE`; PUBLIC usually may connect to other databases there; trusted extensions such as
`dblink` would let migration SQL open outbound connections). The execution sandbox's network policy decides what a
project migration command can reach besides the sandbox server.

## Backups (27.5 / 27.6)

PostgreSQL: one `REPEATABLE READ READ ONLY` transaction exports its snapshot (`pg_export_snapshot`), counts every user
table (`relkind r/p`, not extension-owned; each count in a savepoint with `statement_timeout`) and keeps the snapshot
open while `pg_dump --format=custom --snapshot=<id> --lock-wait-timeout=60000` runs – counts and dump describe the same
state (tested with concurrent inserts). If exporting is not possible the backup is flagged `consistent: false`. SQLite:
backup API inside one read transaction, copy switched to `journal_mode=DELETE`. Files: `<backup_dir>/<target>/<UTC
stamp>-<id>.{dump,sqlite3}`, mode 0600 in a 0700 directory, written as `.partial` and renamed. Artifact metadata
(`metadata.dbtools`): format, target, environment, credential-free server, tool path, extensions, `tables`
(qualified name → count), `consistent`, server version.

Restore test refuses unknown ids, non-backup artifacts, files outside `backup_dir` and checksum/size mismatches
(`DB_ARTIFACT_INVALID`); differences → `DB_RESTORE_MISMATCH` with missing/extra tables and per-table count mismatches.

## Error codes

`DB_TARGET_UNKNOWN`, `DB_TARGET_INVALID`, `DB_TARGET_UNSUPPORTED`, `DB_CONNECT_FAILED`, `DB_POLICY_DENIED`,
`DB_CAPABILITY_DENIED`, `DB_QUERY_REJECTED`, `DB_READ_ONLY_VIOLATION`, `DB_QUERY_TIMEOUT`, `DB_QUERY_FAILED`,
`DB_INTROSPECT_FAILED`, `DB_PATH_INVALID`, `DB_MIGRATION_FRAMEWORK_UNKNOWN`, `DB_MIGRATION_INVALID`,
`DB_MIGRATION_UNSUPPORTED`, `DB_MIGRATION_FAILED`, `DB_SANDBOX_UNAVAILABLE`, `DB_SANDBOX_FAILED`, `DB_BACKUP_FAILED`,
`DB_ARTIFACT_UNKNOWN`, `DB_ARTIFACT_INVALID`, `DB_RESTORE_FAILED`, `DB_RESTORE_MISMATCH`, `DB_TOOL_ERROR`
(+ `SECRET_MISSING`/`SECRET_POLICY` from the secret store, `CANCELLED` in events).

## Known limitations

* MySQL targets are accepted in the configuration but unsupported (no driver in the runtime).
* alembic merge histories: downgrade is verified only by re-upgrade (`downgrade -1`/`upgrade heads`); a custom
  `version_table` in `env.py` is not read (seeded position assumed `base`).
* Django/knex/Laravel/Prisma/Rails migrations are applied only with an explicit `command` (library API; the tool
  argument schema has no command field yet).
* Non-transactional raw SQL is split at top-level `;` – PostgreSQL `BEGIN ATOMIC … END` bodies need a transactional
  migration.
* Raw SQL with a seed but without a known bookkeeping table assumes nothing is applied (warning in the report).

## Tests

| Suite | Covers |
|---|---|
| `tests/unit/test_dbtools_sqlguard.py` | accepted/rejected statements for both dialects (93 cases) |
| `tests/unit/test_dbtools_targets.py` | DSN parsing, redaction, secret refs, production read-only, config loading |
| `tests/unit/test_dbtools_migrations.py` | detection + generation for alembic/Flask-Migrate/pyproject/Django/raw SQL styles, path escapes, reported frameworks, verification state machine |
| `tests/integration/test_dbtools_query.py` | introspection of a PG fixture schema and SQLite, read-only enforcement on both layers, timeouts, cancellation, secret masking |
| `tests/integration/test_dbtools_migrations_apply.py` | alembic (local + `SubprocessExecutor`), raw SQL PG/SQLite, seeded sandboxes, broken downgrades, failing upgrades, escape attempts, env.py ignoring the sandbox URL, custom commands, refusals, sandbox always dropped |
| `tests/integration/test_dbtools_backup.py` | PG/SQLite backup + restore test, snapshot consistency under concurrent writes, tampering, mismatches, foreign artifacts |
| `tests/integration/test_dbtools_provider.py` | provider dispatch, tool engine with the real provider, `build_provider` |
| `tests/failure/test_dbtools_policy.py` | policy matrix, capability refusals for every tool, credential hygiene, audit failures contained, sandbox role isolation, orphan clean-up, production refusal of local commands |

All suites run against the local PostgreSQL 16 (throw-away databases via the admin URL) and SQLite; nothing needs the
LAN hosts (BLOCKER-001).

## Sources (behaviour verified locally against PostgreSQL 16.15, SQLite 3.45.1, alembic 1.20.0)

* PostgreSQL: SET TRANSACTION – https://www.postgresql.org/docs/16/sql-set-transaction.html; snapshot synchronisation
  (`pg_export_snapshot`) – https://www.postgresql.org/docs/16/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION;
  pg_dump (`--snapshot`, `--lock-wait-timeout`) – https://www.postgresql.org/docs/16/app-pgdump.html; pg_restore
  (`--use-list`, `--no-owner`, `--exit-on-error`) – https://www.postgresql.org/docs/16/app-pgrestore.html; client
  settings (`statement_timeout`, `default_transaction_read_only`) – https://www.postgresql.org/docs/16/runtime-config-client.html;
  CREATE ROLE – https://www.postgresql.org/docs/16/sql-createrole.html; libpq environment variables –
  https://www.postgresql.org/docs/16/libpq-envars.html; research note `docs/research/20261008-022-backup-restore.md`.
* SQLite: URI filenames (`mode=ro`) – https://www.sqlite.org/uri.html; authorizer –
  https://www.sqlite.org/c3ref/set_authorizer.html; online backup – https://www.sqlite.org/backup.html; Python sqlite3 –
  https://docs.python.org/3/library/sqlite3.html.
* Alembic: configuration/`file_template` – https://alembic.sqlalchemy.org/en/latest/tutorial.html; commands API –
  https://alembic.sqlalchemy.org/en/latest/api/commands.html; research note `docs/research/20261008-004-alembic.md`.
* Django migrations – https://docs.djangoproject.com/en/stable/topics/migrations/; dbmate, goose, golang-migrate,
  Flyway naming conventions from their official READMEs/docs.
