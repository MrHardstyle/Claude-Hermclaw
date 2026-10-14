"""Migration framework detection and skeleton generation (P27 27.3) plus the verification state machine (27.4)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hermclaw.dbtools.errors import (
    DB_MIGRATION_FRAMEWORK_UNKNOWN,
    DB_MIGRATION_INVALID,
    DB_MIGRATION_UNSUPPORTED,
    DB_PATH_INVALID,
    DbPolicyError,
    DbToolError,
)
from hermclaw.dbtools.migrations import (
    Framework,
    StepOutcome,
    alembic_chain,
    clean_description,
    detect_framework,
    generate_migration,
    parse_alembic_revisions,
    parse_sql_migrations,
    resolve_project_dir,
    schema_diff,
    slugify,
    split_marked_sql,
    split_statements,
    verify_migrations,
)

NOW = datetime(2026, 10, 14, 9, 30, 15, tzinfo=UTC)
REV_TEMPLATE = '''"""{msg}"""
revision = {rev!r}
down_revision: str | None = {down!r}

def upgrade():
    pass

def downgrade():
    pass
'''


def alembic_project(root: Path, *, ini_extra: str = "", revisions: list[tuple[str, Any]] | None = None) -> Path:
    (root / "migrations" / "versions").mkdir(parents=True)
    (root / "migrations" / "env.py").write_text("# env\n", encoding="utf-8")
    (root / "alembic.ini").write_text(
        f"[alembic]\nscript_location = %(here)s/migrations\nsqlalchemy.url = postgresql+psycopg://u@h/db\n{ini_extra}\n", encoding="utf-8"
    )
    for rev, down in revisions or []:
        (root / "migrations" / "versions" / f"{rev}_step.py").write_text(REV_TEMPLATE.format(msg=rev, rev=rev, down=down), encoding="utf-8")
    return root


# --------------------------------------------------------------------------------------------- paths
def test_project_dir_must_stay_in_the_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "app").mkdir(parents=True)
    (ws / ".git").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "link").symlink_to(outside)
    assert resolve_project_dir(ws, "app") == (ws / "app").resolve()
    assert resolve_project_dir(ws, "./app/") == (ws / "app").resolve()
    assert resolve_project_dir(ws, ".") == ws.resolve()
    for bad in ("../outside", "/etc", "app/../../outside", ".git", "link", "missing", "C:/x", "a\x00b"):
        with pytest.raises(DbPolicyError) as exc:
            resolve_project_dir(ws, bad)
        assert exc.value.code == DB_PATH_INVALID, bad


def test_unknown_framework(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(DbToolError) as exc:
        detect_framework(tmp_path, workspace_root=tmp_path)
    assert exc.value.code == DB_MIGRATION_FRAMEWORK_UNKNOWN


# --------------------------------------------------------------------------------------------- alembic
def test_alembic_generation_follows_the_head_and_naming(tmp_path: Path) -> None:
    alembic_project(tmp_path, revisions=[("aaa111", None), ("bbb222", "aaa111")])
    project = detect_framework(tmp_path, workspace_root=tmp_path)
    assert project.framework == Framework.alembic and project.can_generate and project.can_apply
    assert project.settings["url_scheme"] == "postgresql+psycopg"
    assert project.migrations_dir == (tmp_path / "migrations" / "versions")
    gen = generate_migration(project, 'Add "email" column to Users!', now=NOW)
    path = gen.files[0]
    assert gen.down_revision == "bbb222" and gen.revision is not None and len(gen.revision) == 12
    assert path.name == f"{gen.revision}_add_email_column_to_users.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)  # valid Python
    assert "def upgrade() -> None" in text and "def downgrade() -> None" in text
    assert any(isinstance(n, ast.FunctionDef) and n.name == "downgrade" for n in tree.body)
    revisions, warnings = parse_alembic_revisions(tmp_path / "migrations" / "versions")
    assert not warnings
    chain, linear = alembic_chain(revisions)
    assert chain == ["aaa111", "bbb222", gen.revision] and linear
    # deterministic: same parent + description in a fresh copy gives the same id
    other = tmp_path / "copy"
    other.mkdir()
    alembic_project(other, revisions=[("aaa111", None), ("bbb222", "aaa111")])
    again = generate_migration(detect_framework(other, workspace_root=other), 'Add "email" column to Users!', now=NOW)
    assert again.revision == gen.revision


def test_alembic_file_template_and_first_revision(tmp_path: Path) -> None:
    alembic_project(tmp_path, ini_extra="file_template = %%(year)d%%(month).2d%%(day).2d_%%(rev)s_%%(slug)s\ntruncate_slug_length = 10")
    gen = generate_migration(detect_framework(tmp_path, workspace_root=tmp_path), "create the accounts table", now=NOW)
    assert gen.down_revision is None
    assert gen.files[0].name == f"20261014_{gen.revision}_create_.py"
    assert "down_revision: Union[str, None] = None" in gen.files[0].read_text(encoding="utf-8")


def test_alembic_multiple_heads_and_duplicates_are_refused(tmp_path: Path) -> None:
    alembic_project(tmp_path, revisions=[("a1", None), ("b1", "a1"), ("c1", "a1")])
    project = detect_framework(tmp_path, workspace_root=tmp_path)
    with pytest.raises(DbToolError, match="several heads") as exc:
        generate_migration(project, "next", now=NOW)
    assert exc.value.code == DB_MIGRATION_INVALID
    (tmp_path / "migrations" / "versions" / "dup_step.py").write_text(REV_TEMPLATE.format(msg="d", rev="a1", down=None), encoding="utf-8")
    with pytest.raises(DbToolError, match="defined twice"):
        parse_alembic_revisions(tmp_path / "migrations" / "versions")


def test_alembic_merge_history_is_not_linear(tmp_path: Path) -> None:
    alembic_project(tmp_path, revisions=[("a1", None), ("b1", "a1"), ("c1", "a1"), ("m1", ("b1", "c1"))])
    revisions, _ = parse_alembic_revisions(tmp_path / "migrations" / "versions")
    chain, linear = alembic_chain(revisions)
    assert not linear and chain == ["m1"]


def test_alembic_script_location_outside_workspace_is_refused(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "versions").mkdir(parents=True)
    (elsewhere / "env.py").write_text("", encoding="utf-8")
    (ws / "alembic.ini").write_text(f"[alembic]\nscript_location = {elsewhere}\n", encoding="utf-8")
    with pytest.raises(DbPolicyError) as exc:
        detect_framework(ws, workspace_root=ws)
    assert exc.value.code == DB_PATH_INVALID


def test_alembic_symlinked_versions_dir_escaping_is_refused(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "migrations").mkdir(parents=True)
    (ws / "migrations" / "env.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "migrations" / "versions").symlink_to(outside)
    (ws / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n", encoding="utf-8")
    with pytest.raises(DbPolicyError):
        detect_framework(ws, workspace_root=ws)


def test_flask_migrate_layout_and_pyproject_config(tmp_path: Path) -> None:
    flask = tmp_path / "flask"
    (flask / "migrations" / "versions").mkdir(parents=True)
    (flask / "migrations" / "env.py").write_text("", encoding="utf-8")
    (flask / "migrations" / "alembic.ini").write_text("[alembic]\nfile_template = %%(rev)s-%%(slug)s\n", encoding="utf-8")
    project = detect_framework(flask, workspace_root=tmp_path)
    assert project.framework == Framework.alembic and project.settings["script_location"] == "migrations"
    assert project.settings["ini"] == "migrations/alembic.ini"
    gen = generate_migration(project, "init", now=NOW)
    assert gen.files[0].name == f"{gen.revision}-init.py"
    toml = tmp_path / "toml"
    (toml / "db" / "versions").mkdir(parents=True)
    (toml / "db" / "env.py").write_text("", encoding="utf-8")
    (toml / "pyproject.toml").write_text('[tool.alembic]\nscript_location = "%(here)s/db"\n', encoding="utf-8")
    project = detect_framework(toml, workspace_root=tmp_path)
    assert project.framework == Framework.alembic and project.migrations_dir == toml / "db" / "versions"


# --------------------------------------------------------------------------------------------- django
def test_django_generation_depends_on_the_latest_migration(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("", encoding="utf-8")
    mig = tmp_path / "shop" / "migrations"
    mig.mkdir(parents=True)
    (mig / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "shop" / "models.py").write_text("", encoding="utf-8")
    (mig / "0001_initial.py").write_text("", encoding="utf-8")
    (mig / "0002_add_price.py").write_text("", encoding="utf-8")
    project = detect_framework(tmp_path, workspace_root=tmp_path)
    assert project.framework == Framework.django and project.settings["app_label"] == "shop" and not project.can_apply
    gen = generate_migration(project, "Add stock level", now=NOW)
    assert gen.files[0] == mig / "0003_add_stock_level.py"
    text = gen.files[0].read_text(encoding="utf-8")
    ast.parse(text)
    assert "('shop', '0002_add_price')" in text and "migrations.RunPython(upgrade, downgrade)" in text
    # an app directory can be passed directly; several apps need the explicit path
    other = tmp_path / "blog" / "migrations"
    other.mkdir(parents=True)
    (other / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(DbToolError, match="several Django apps"):
        detect_framework(tmp_path, workspace_root=tmp_path)
    project = detect_framework(tmp_path / "blog", workspace_root=tmp_path)
    gen = generate_migration(project, "initial", now=NOW)
    assert gen.files[0] == other / "0001_initial.py" and gen.down_revision == []


# --------------------------------------------------------------------------------------------- raw SQL
def test_raw_sql_single_files_with_markers(tmp_path: Path) -> None:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "001_init.sql").write_text("-- migrate:up\nCREATE TABLE a (id int);\n-- migrate:down\nDROP TABLE a;\n", encoding="utf-8")
    (d / "002_more.sql").write_text(
        "-- migrate:up transaction:false\nCREATE INDEX CONCURRENTLY i ON a (id);\n\n-- migrate:down\nDROP INDEX i;\n", encoding="utf-8"
    )
    project = detect_framework(tmp_path, workspace_root=tmp_path)
    assert project.framework == Framework.raw_sql and project.settings == {
        "style": "single",
        "marker_style": "dbmate",
        "width": 3,
        "sep": "_",
    }
    gen = generate_migration(project, "add  users\ttable", now=NOW)
    assert gen.files == [d / "003_add_users_table.sql"] and gen.down_revision == "002"
    text = gen.files[0].read_text(encoding="utf-8")
    assert text.startswith("-- migrate:up\n-- add users table\n") and "-- migrate:down" in text
    migrations = parse_sql_migrations(d)
    assert [m.version for m in migrations] == ["001", "002", "003"]
    assert migrations[0].down_sql is not None and "DROP TABLE a" in migrations[0].down_sql
    assert not migrations[1].transactional and migrations[0].transactional


def test_raw_sql_paired_files_timestamps_and_goose(tmp_path: Path) -> None:
    d = tmp_path / "db" / "migrations"
    d.mkdir(parents=True)
    (d / "20240101120000_init.up.sql").write_text("CREATE TABLE a (id int);", encoding="utf-8")
    (d / "20240101120000_init.down.sql").write_text("DROP TABLE a;", encoding="utf-8")
    project = detect_framework(tmp_path, workspace_root=tmp_path)
    gen = generate_migration(project, "second", now=NOW)
    assert [f.name for f in gen.files] == ["20261014093015_second.up.sql", "20261014093015_second.down.sql"]
    gen2 = generate_migration(project, "third", now=NOW)  # same clock: still strictly increasing
    assert gen2.files[0].name.startswith("20261014093016_")
    goose = tmp_path / "goose"
    (goose / "migrations").mkdir(parents=True)
    (goose / "migrations" / "00001_a.sql").write_text(
        "-- +goose Up\n-- +goose StatementBegin\nCREATE TABLE g (id int);\n-- +goose StatementEnd\n-- +goose Down\nDROP TABLE g;\n",
        encoding="utf-8",
    )
    project = detect_framework(goose, workspace_root=tmp_path)
    gen = generate_migration(project, "b", now=NOW)
    assert gen.files[0].name == "00002_b.sql" and "-- +goose Down" in gen.files[0].read_text(encoding="utf-8")
    up, down, style, tx = split_marked_sql((goose / "migrations" / "00001_a.sql").read_text(encoding="utf-8"))
    assert style == "goose" and "CREATE TABLE g" in up and down is not None and "DROP TABLE g" in down and tx


def test_raw_sql_flyway_and_empty_directory(tmp_path: Path) -> None:
    fly = tmp_path / "fly" / "migrations"
    fly.mkdir(parents=True)
    (fly / "V1__init.sql").write_text("CREATE TABLE f (id int);", encoding="utf-8")
    (fly / "U1__init.sql").write_text("DROP TABLE f;", encoding="utf-8")
    project = detect_framework(fly.parent, workspace_root=tmp_path)
    gen = generate_migration(project, "two", now=NOW)
    assert [f.name for f in gen.files] == ["V2__two.sql", "U2__two.sql"]
    empty = tmp_path / "empty" / "migrations"
    empty.mkdir(parents=True)
    gen = generate_migration(detect_framework(empty.parent, workspace_root=tmp_path), "first", now=NOW)
    assert gen.files[0].name == "0001_first.sql"


def test_raw_sql_inconsistencies_are_reported(tmp_path: Path) -> None:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (d / "001-b.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(DbToolError, match="duplicate migration version"):
        parse_sql_migrations(d)
    (d / "001-b.sql").unlink()
    (d / "002_c.down.sql").write_text("SELECT 3;", encoding="utf-8")
    with pytest.raises(DbToolError, match=r"no \.up\.sql"):
        parse_sql_migrations(d)


def test_existing_file_is_never_overwritten(tmp_path: Path) -> None:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "001_a.sql").write_text("-- up\nSELECT 1;\n-- down\n", encoding="utf-8")
    (d / "002_next.sql").symlink_to(tmp_path / "victim")  # dangling symlink with the next name
    with pytest.raises(DbToolError, match="already exists"):
        generate_migration(detect_framework(tmp_path, workspace_root=tmp_path), "next", now=NOW)
    assert not (tmp_path / "victim").exists()


@pytest.mark.parametrize(("framework_file", "framework"), [("knexfile.js", Framework.knex), ("prisma/schema.prisma", Framework.prisma)])
def test_reported_frameworks(tmp_path: Path, framework_file: str, framework: Framework) -> None:
    (tmp_path / framework_file).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / framework_file).write_text("", encoding="utf-8")
    project = detect_framework(tmp_path, workspace_root=tmp_path)
    assert project.framework == framework and not project.can_generate and project.hint
    with pytest.raises(DbToolError) as exc:
        generate_migration(project, "x y", now=NOW)
    assert exc.value.code == DB_MIGRATION_UNSUPPORTED and project.hint in exc.value.message


def test_laravel_is_reported_not_raw_sql(tmp_path: Path) -> None:
    (tmp_path / "artisan").write_text("", encoding="utf-8")
    (tmp_path / "database" / "migrations").mkdir(parents=True)
    (tmp_path / "database" / "migrations" / "2024_01_01_000000_create_users_table.php").write_text("", encoding="utf-8")
    assert detect_framework(tmp_path, workspace_root=tmp_path).framework == Framework.laravel


def test_description_is_sanitised() -> None:
    assert slugify("Ünïcode & spaces --- here", 40) == "ünïcode_spaces_here"
    assert clean_description("a\x00b\nc\t  d" + "x" * 300) == "a b c d" + "x" * 193
    with pytest.raises(DbToolError):
        clean_description("!!! ---")


def test_split_statements_respects_literals() -> None:
    sql = "CREATE TABLE a (t text DEFAULT ';');\nCREATE FUNCTION f() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;\n-- c;\n;"
    assert split_statements(sql, "postgresql") == [
        "CREATE TABLE a (t text DEFAULT ';')",
        "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql",
    ]


# --------------------------------------------------------------------------------------------- verification state machine
class FakeDriver:
    """Schema = set of applied migration names; ``broken_down`` leaves a migration's effect in place."""

    name = "fake"

    def __init__(self, n: int, *, start: int = -1, broken_down: int | None = None, fail_up: int | None = None, exact: bool = True) -> None:
        self.chain = [f"m{i}" for i in range(n)]
        self.position = start
        self.residue: set[str] = set()
        self.broken_down = broken_down
        self.fail_up = fail_up
        self.exact_positions = exact
        self.can_downgrade = True
        self.calls: list[str] = []

    async def current(self) -> int:
        return self.position

    async def goto(self, position: int) -> StepOutcome:
        self.calls.append(f"goto {position}")
        while self.position < position:
            if self.position + 1 == self.fail_up:
                return StepOutcome(False, "boom")
            self.position += 1
        while self.position > position:
            if self.position == self.broken_down:
                self.residue.add(self.chain[self.position])
            self.position -= 1
        return StepOutcome(True, "ok")

    async def snapshot(self) -> tuple[str, dict[str, Any]]:
        names = sorted({*self.chain[: self.position + 1], *self.residue})
        desc = {"schemas": [{"name": "public", "tables": [{"name": t, "columns": []} for t in names], "views": []}]}
        return ",".join(names), desc


async def test_verify_latest_scope_checks_the_newest_migration() -> None:
    d = FakeDriver(3)
    report = await verify_migrations(d, d.snapshot, scope="latest")
    assert report.ok, report.to_dict()
    assert [s["step"] for s in report.steps] == ["upgrade to predecessor", "upgrade", "downgrade", "upgrade again"]
    assert d.calls == ["goto 1", "goto 2", "goto 1", "goto 2"]
    assert [c["ok"] for c in report.checks] == [True, True]


async def test_verify_detects_a_downgrade_that_does_not_revert() -> None:
    d = FakeDriver(2, broken_down=1)
    report = await verify_migrations(d, d.snapshot, scope="latest")
    assert not report.ok
    failed = [c for c in report.checks if not c["ok"]]
    assert failed and failed[0]["check"] == "downgrade restores the previous schema" and "+ table public.m1" in failed[0]["diff"]


async def test_verify_all_scope_and_failures() -> None:
    d = FakeDriver(3)
    report = await verify_migrations(d, d.snapshot, scope="all")
    assert report.ok and [s["to"] for s in report.steps] == ["m2", "base", "m2"]
    d = FakeDriver(3, fail_up=2)
    report = await verify_migrations(d, d.snapshot, scope="latest")
    failed = report.failed_step()
    assert not report.ok and failed is not None and failed["step"] == "upgrade"
    d = FakeDriver(2)
    report = await verify_migrations(d, d.snapshot, scope="none")
    assert report.ok and len(report.steps) == 1 and not report.checks


async def test_verify_seeded_with_head_already_applied() -> None:
    d = FakeDriver(2, start=1)
    report = await verify_migrations(d, d.snapshot, scope="latest")
    assert report.ok and [s["step"] for s in report.steps] == ["downgrade", "upgrade again"]
    assert any("already applied" in w for w in report.warnings)


async def test_verify_merge_history_only_compares_reupgrade() -> None:
    d = FakeDriver(1, exact=False, broken_down=0)
    report = await verify_migrations(d, d.snapshot, scope="latest")
    assert [c["check"] for c in report.checks] == ["re-upgrade reproduces the schema"]
    assert any("merge history" in w for w in report.warnings)


def test_schema_diff_lists_changes() -> None:
    a = {"schemas": [{"name": "public", "tables": [{"name": "t", "columns": [{"name": "a", "type": "INTEGER"}]}], "views": []}]}
    b = {
        "schemas": [
            {
                "name": "public",
                "tables": [
                    {"name": "t", "columns": [{"name": "a", "type": "BIGINT"}, {"name": "b", "type": "TEXT"}]},
                    {"name": "u", "columns": []},
                ],
                "views": [],
            }
        ]
    }
    diff = schema_diff(a, b)
    assert "+ table public.u" in diff and "+ column public.t.b" in diff and any(d.startswith("~ column public.t.a") for d in diff)
