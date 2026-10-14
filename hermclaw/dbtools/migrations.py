"""Migration frameworks of user projects (P27 27.3 generate, 27.4 sandbox verification).

Detection (deterministic, file based, no model): ``alembic`` (``alembic.ini`` / ``[tool.alembic]`` in
``pyproject.toml`` / Flask-Migrate's ``migrations/{env.py,versions/}``), ``django`` (``manage.py`` + an app with
``migrations/__init__.py``), ``raw_sql`` (numbered ``*.sql`` files in ``migrations/``, ``db/migrations/`` …: paired
``.up.sql``/``.down.sql`` files, Flyway ``V<n>__``/``U<n>__`` files or single files with dbmate/goose/sql-migrate or
plain ``-- up``/``-- down`` markers). ``knex``, ``laravel``, ``prisma`` and ``rails`` projects are detected and
reported with the command of their own CLI – Hermclaw does not generate or apply those.

Generation creates the next skeleton file in the project's naming scheme (alembic ``file_template`` + deterministic
revision id, Django ``NNNN_slug.py``, raw SQL next number/timestamp in the existing width/style) with explicit
up/down sections; the coder fills the bodies. Files are created exclusively (never overwritten) and only inside the
workspace (no ``..``, no symlink escape, never ``.git``).

Verification (27.4) runs a :class:`MigrationDriver` against a throw-away sandbox database (created by
:mod:`hermclaw.dbtools.sandbox`) and checks upgrade → downgrade → upgrade with schema fingerprints
(:func:`verify_migrations`). Raw SQL runs in-process as the sandbox's isolated role (PostgreSQL) or with an
authorizer that forbids ``ATTACH``/``VACUUM INTO`` (SQLite) – never through ``psql`` (no ``\\!`` meta commands).
Alembic and custom commands run project code and therefore go through a
:class:`~hermclaw.dbtools.runner.MigrationCommandRunner` (the sandbox executor in production).
"""

from __future__ import annotations

import ast
import asyncio
import configparser
import contextlib
import hashlib
import os
import re
import sqlite3
import time
import tomllib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import quote

import psycopg
import psycopg.errors
from psycopg import sql as pgsql

from hermclaw.dbtools.errors import (
    DB_MIGRATION_FAILED,
    DB_MIGRATION_FRAMEWORK_UNKNOWN,
    DB_MIGRATION_INVALID,
    DB_MIGRATION_UNSUPPORTED,
    DB_PATH_INVALID,
    DbPolicyError,
    DbToolError,
)
from hermclaw.dbtools.introspect import describe, fingerprint
from hermclaw.dbtools.runner import MigrationCommandRunner, ProcessResult
from hermclaw.dbtools.sqlguard import Dialect, tokenize
from hermclaw.dbtools.targets import ConnectionInfo, TargetKind, redact_dsn

VerifyScope = Literal["latest", "all", "none"]
SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".tox", ".nox", ".mypy_cache", "site-packages", "dist", "build"}
    | {".hermclaw-dbsandbox"}
)
RAW_SQL_DIRS = ("migrations", "db/migrations", "sql/migrations", "database/migrations", "migrations/sql", "schema/migrations", "db/sql")
#: bookkeeping tables of migration tools – excluded from schema fingerprints, copied with data when seeding
BOOKKEEPING_TABLES = frozenset(
    {
        "alembic_version",
        "django_migrations",
        "schema_migrations",
        "goose_db_version",
        "flyway_schema_history",
        "knex_migrations",
        "knex_migrations_lock",
        "_prisma_migrations",
        "ar_internal_metadata",
        "gorp_migrations",
        "__diesel_schema_migrations",
    }
)
SLUG_RE = re.compile(r"\w+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_DJANGO_RE = re.compile(r"^(\d{4,})_(\w+)\.py$")
_PAIRED_RE = re.compile(r"^(?P<v>\d+)_(?P<name>.+)\.(?P<dir>up|down)\.sql$", re.I)
_FLYWAY_RE = re.compile(r"^(?P<p>[VU])(?P<v>\d+(?:[._]\d+)*)__(?P<name>.+)\.sql$")
_SINGLE_RE = re.compile(r"^(?P<v>\d+)(?P<sep>[_-])(?P<name>.+)\.sql$", re.I)
_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dbmate", re.compile(r"^--\s*migrate:(up|down)\b(.*)$", re.I | re.M)),
    ("goose", re.compile(r"^--\s*\+goose\s+(up|down)\b(.*)$", re.I | re.M)),
    ("sql-migrate", re.compile(r"^--\s*\+migrate\s+(up|down)\b(.*)$", re.I | re.M)),
    ("generic", re.compile(r"^--\s*(up|down)\s*:?\s*$", re.I | re.M)),
)
_NO_TX_RE = re.compile(r"^--\s*\+goose\s+NO\s+TRANSACTION\b|^--\s*\+migrate\s+(?:up|down)\b.*\bnotransaction\b", re.I | re.M)
_TX_KEYWORDS = frozenset({"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"})
_SQLITE_DENIED_ACTIONS = frozenset({sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH})
_SQLITE_DENIED_FUNCTIONS = frozenset({"load_extension", "writefile", "readfile", "edit"})
DEFAULT_MARKER_STYLE = "dbmate"
TIMESTAMP_DIGITS = 14


class Framework(StrEnum):
    alembic = "alembic"
    django = "django"
    raw_sql = "raw_sql"
    knex = "knex"
    laravel = "laravel"
    prisma = "prisma"
    rails = "rails"


#: frameworks Hermclaw only reports (generation/apply through the project's own CLI)
REPORTED_ONLY: dict[Framework, str] = {
    Framework.knex: "npx knex migrate:make <name>  /  npx knex migrate:latest",
    Framework.laravel: "php artisan make:migration <name>  /  php artisan migrate",
    Framework.prisma: "npx prisma migrate dev --name <name>",
    Framework.rails: "bin/rails generate migration <Name>  /  bin/rails db:migrate",
}


# ============================================================================================ workspace paths
def resolve_project_dir(workspace_root: Path, relative: str) -> Path:
    """``relative`` (project directory) inside the workspace; refuses traversal, symlink escapes and ``.git``."""
    text = (relative or ".").strip()
    if "\x00" in text or text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", text):
        raise DbPolicyError("project path must be relative to the repository root", code=DB_PATH_INVALID)
    parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or ".git" in parts:
        raise DbPolicyError("project path must not contain '..' or touch '.git'", code=DB_PATH_INVALID)
    base = workspace_root.resolve()
    try:
        target = base.joinpath(*parts).resolve(strict=True)
    except (OSError, RuntimeError):
        raise DbPolicyError(f"project path '{'/'.join(parts) or '.'}' does not exist in the workspace", code=DB_PATH_INVALID) from None
    ensure_inside(base, target)
    if not target.is_dir():
        raise DbPolicyError(f"project path '{'/'.join(parts)}' is not a directory", code=DB_PATH_INVALID)
    return target


def ensure_inside(base: Path, path: Path) -> Path:
    """``path`` (resolved) must be ``base`` or below it and must not be inside ``.git``."""
    root = base.resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise DbPolicyError("migration path resolves outside the workspace", code=DB_PATH_INVALID)
    if ".git" in resolved.relative_to(root).parts:
        raise DbPolicyError("migration path must not be inside '.git'", code=DB_PATH_INVALID)
    return resolved


def _rel(base: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix() or "."
    except ValueError:
        return path.name


def write_new_file(path: Path, content: str, *, workspace_root: Path) -> None:
    """Create ``path`` exclusively (``O_EXCL|O_NOFOLLOW``) inside the workspace."""
    ensure_inside(workspace_root, path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_inside(workspace_root, path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except FileExistsError:
        raise DbToolError(f"migration file {path.name} already exists", code=DB_MIGRATION_INVALID) from None
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


# ============================================================================================ detection
@dataclass
class MigrationProject:
    framework: Framework
    root: Path  # project directory (absolute, inside the workspace)
    workspace_root: Path
    migrations_dir: Path | None = None  # alembic versions dir / Django app migrations / SQL directory
    evidence: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    also_detected: list[str] = field(default_factory=list)

    @property
    def can_generate(self) -> bool:
        return self.framework in (Framework.alembic, Framework.django, Framework.raw_sql)

    @property
    def can_apply(self) -> bool:
        return self.framework in (Framework.alembic, Framework.raw_sql)

    @property
    def hint(self) -> str:
        return REPORTED_ONLY.get(self.framework, "")

    def describe(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "framework": self.framework.value,
            "project": _rel(self.workspace_root, self.root),
            "evidence": self.evidence,
            "can_generate": self.can_generate,
            "can_apply": self.can_apply,
        }
        if self.migrations_dir is not None:
            data["migrations_dir"] = _rel(self.workspace_root, self.migrations_dir)
        if self.hint:
            data["hint"] = self.hint
        if self.also_detected:
            data["also_detected"] = self.also_detected
        public = {k: v for k, v in self.settings.items() if k in ("file_template", "style", "app_label", "marker_style", "url_scheme")}
        if public:
            data["settings"] = public
        return data


def detect_framework(project_root: Path, *, workspace_root: Path) -> MigrationProject:
    """Detect the migration framework of ``project_root``; raises ``DB_MIGRATION_FRAMEWORK_UNKNOWN``."""
    found: list[MigrationProject] = []
    for probe in (_detect_alembic, _detect_django, _detect_reported, _detect_raw_sql):
        result = probe(project_root, workspace_root)
        if result is not None:
            found.append(result)
    if not found:
        raise DbToolError(
            f"no migration framework found in '{_rel(workspace_root, project_root)}' (looked for alembic.ini/env.py, "
            "manage.py + migrations/, knexfile, artisan, prisma/schema.prisma, db/migrate, numbered *.sql files)",
            code=DB_MIGRATION_FRAMEWORK_UNKNOWN,
        )
    primary = found[0]
    primary.also_detected = [p.framework.value for p in found[1:]]
    return primary


def _detect_alembic(root: Path, workspace: Path) -> MigrationProject | None:
    options: dict[str, str] = {}
    evidence: list[str] = []
    ini = root / "alembic.ini"
    here = root
    if ini.is_file():
        options.update(_read_ini_options(ini))
        evidence.append("alembic.ini")
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        toml_opts = _read_toml_alembic(pyproject)
        if toml_opts:
            options.update(toml_opts)
            evidence.append("pyproject.toml [tool.alembic]")
    script_location: Path | None = None
    if "script_location" in options:
        script_location = _resolve_location(options["script_location"], here, root)
    else:
        for candidate in ("migrations", "alembic"):  # Flask-Migrate / conventional layout without root ini
            d = root / candidate
            if (d / "env.py").is_file() and (d / "versions").is_dir():
                script_location = d
                inner_ini = d / "alembic.ini"
                if inner_ini.is_file() and not ini.is_file():
                    options = {**_read_ini_options(inner_ini), **options}
                    ini = inner_ini
                    evidence.append(f"{candidate}/alembic.ini")
                options.setdefault("script_location", candidate)
                evidence.append(f"{candidate}/env.py")
                break
    if script_location is None:
        return None
    if not (script_location / "env.py").is_file():
        if not evidence:
            return None
        raise DbToolError(
            f"alembic script_location '{_rel(workspace, script_location)}' has no env.py (run 'alembic init' first)",
            code=DB_MIGRATION_INVALID,
        )
    ensure_inside(workspace, script_location)
    versions = script_location / "versions"
    if options.get("version_locations"):
        first = _split_locations(options["version_locations"], options.get("version_path_separator"))
        if first:
            versions = _resolve_location(first[0], here, root)
    ensure_inside(workspace, versions)
    settings: dict[str, Any] = {
        "ini": _rel(root, ini) if ini.is_file() else None,
        "toml": "pyproject.toml" if "pyproject.toml [tool.alembic]" in evidence else None,
        "script_location": _rel(root, script_location),
        "file_template": options.get("file_template", "%(rev)s_%(slug)s"),
        "truncate_slug_length": _int_or(options.get("truncate_slug_length"), 40),
        "recursive": str(options.get("recursive_version_locations", "false")).lower() in ("true", "1", "yes"),
        "url_scheme": _url_scheme(options.get("sqlalchemy.url", "")),
    }
    return MigrationProject(Framework.alembic, root, workspace, versions, evidence, settings)


def _read_ini_options(path: Path) -> dict[str, str]:
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(path.read_text(encoding="utf-8"), source=path.name)
    except (configparser.Error, UnicodeDecodeError, OSError) as exc:
        raise DbToolError(f"{path.name} cannot be parsed: {str(exc).splitlines()[0][:200]}", code=DB_MIGRATION_INVALID) from None
    if not parser.has_section("alembic"):
        return {}
    here = str(path.parent)
    return {k: v.replace("%%", "%").replace("%(here)s", here) for k, v in parser.items("alembic")}


def _read_toml_alembic(path: Path) -> dict[str, str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError):
        return {}
    section = data.get("tool", {}).get("alembic", {})
    if not isinstance(section, dict):
        return {}
    out: dict[str, str] = {}
    here = str(path.parent)
    for key, value in section.items():
        if isinstance(value, list):
            out[key] = " ".join(str(v) for v in value)
        elif isinstance(value, str | int | bool):
            out[key] = str(value).replace("%(here)s", here)
    return out


def _resolve_location(value: str, here: Path, root: Path) -> Path:
    text = value.strip().replace("%(here)s", str(here))
    if ":" in text and not text.startswith("/") and not re.match(r"^[A-Za-z]:[\\/]", text):
        package, _, resource = text.partition(":")  # package resource "myapp:migrations"
        return root.joinpath(*package.split("."), resource)
    path = Path(text)
    return path if path.is_absolute() else root / path


def _split_locations(value: str, separator: str | None) -> list[str]:
    sep = (separator or "").strip()
    if sep in ("os",):
        parts = value.split(os.pathsep)
    elif sep in ("space", ""):
        parts = re.split(r"[ ,]+", value) if not sep else value.split(" ")
    elif sep == "newline":
        parts = value.splitlines()
    else:
        parts = value.split({"colon": ":", "semicolon": ";"}.get(sep, sep))
    return [p.strip() for p in parts if p.strip()]


def _int_or(value: str | None, default: int) -> int:
    try:
        return int(str(value)) if value not in (None, "") else default
    except ValueError:
        return default


def _url_scheme(url: str) -> str:
    scheme = url.strip().split("://", 1)[0].lower() if "://" in url else ""
    return scheme if re.match(r"^(postgresql|postgres|sqlite)(\+[a-z0-9_]+)?$", scheme) else ""


def _detect_django(root: Path, workspace: Path) -> MigrationProject | None:
    manage: Path | None = None
    current = root.resolve()
    base = workspace.resolve()
    while True:
        if (current / "manage.py").is_file():
            manage = current / "manage.py"
            break
        if current == base or base not in current.parents:
            break
        current = current.parent
    if manage is None:
        return None
    if (root / "migrations" / "__init__.py").is_file():
        app = root
    else:
        apps = sorted(
            {p.parent.parent for p in _walk_files(manage.parent, "__init__.py", max_depth=4) if p.parent.name == "migrations"},
            key=lambda p: p.as_posix(),
        )
        if not apps:
            return MigrationProject(Framework.django, root, workspace, None, [_rel(workspace, manage)], {"apps": []})
        if len(apps) > 1:
            names = ", ".join(_rel(workspace, a) for a in apps[:10])
            raise DbToolError(f"several Django apps have migrations ({names}); pass the app directory as path", code=DB_MIGRATION_INVALID)
        app = apps[0]
    ensure_inside(workspace, app)
    return MigrationProject(
        Framework.django,
        root,
        workspace,
        app / "migrations",
        [_rel(workspace, manage), _rel(workspace, app / "migrations")],
        {"app_label": app.name},
    )


def _walk_files(base: Path, name: str, *, max_depth: int) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        depth = len(Path(dirpath).relative_to(base).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")) if depth < max_depth else []
        if name in filenames:
            out.append(Path(dirpath) / name)
    return out


def _detect_reported(root: Path, workspace: Path) -> MigrationProject | None:
    knexfiles = [f"knexfile.{ext}" for ext in ("js", "ts", "cjs", "mjs")]
    hit = next((f for f in knexfiles if (root / f).is_file()), None)
    if hit:
        return MigrationProject(Framework.knex, root, workspace, root / "migrations", [hit])
    if (root / "artisan").is_file() and (root / "database" / "migrations").is_dir():
        return MigrationProject(Framework.laravel, root, workspace, root / "database" / "migrations", ["artisan", "database/migrations"])
    if (root / "prisma" / "schema.prisma").is_file():
        return MigrationProject(Framework.prisma, root, workspace, root / "prisma" / "migrations", ["prisma/schema.prisma"])
    if (root / "db" / "migrate").is_dir() and ((root / "bin" / "rails").is_file() or (root / "config" / "application.rb").is_file()):
        return MigrationProject(Framework.rails, root, workspace, root / "db" / "migrate", ["db/migrate"])
    return None


def _detect_raw_sql(root: Path, workspace: Path) -> MigrationProject | None:
    empty_candidate: Path | None = None
    for rel in RAW_SQL_DIRS:
        d = root / rel
        if not d.is_dir() or d.is_symlink():
            continue
        files = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".sql"]
        numbered = [p for p in files if _PAIRED_RE.match(p.name) or _FLYWAY_RE.match(p.name) or _SINGLE_RE.match(p.name)]
        if numbered:
            ensure_inside(workspace, d)
            style = _infer_style(d, numbered)
            return MigrationProject(Framework.raw_sql, root, workspace, d, [f"{rel}/*.sql"], style)
        if rel == "migrations" and not any(d.iterdir()):
            empty_candidate = d
    if empty_candidate is not None:
        ensure_inside(workspace, empty_candidate)
        return MigrationProject(
            Framework.raw_sql,
            root,
            workspace,
            empty_candidate,
            ["migrations/ (empty)"],
            {"style": "single", "marker_style": DEFAULT_MARKER_STYLE, "width": 4, "sep": "_"},
        )
    return None


def _infer_style(directory: Path, files: list[Path]) -> dict[str, Any]:
    if any(_PAIRED_RE.match(p.name) for p in files):
        versions = [m.group("v") for p in files if (m := _PAIRED_RE.match(p.name))]
        return {"style": "paired", "width": max(len(v) for v in versions), "sep": "_"}
    if any(_FLYWAY_RE.match(p.name) for p in files):
        undo = any((m := _FLYWAY_RE.match(p.name)) and m.group("p") == "U" for p in files)
        return {"style": "flyway", "undo": undo}
    singles = sorted(((_order_key(m.group("v")), p, m) for p in files if (m := _SINGLE_RE.match(p.name))), key=lambda x: x[0])
    latest = singles[-1][1]
    marker = _marker_style(_read_text(latest)) or DEFAULT_MARKER_STYLE
    width = max(len(m.group("v")) for _, _, m in singles)
    return {"style": "single", "marker_style": marker, "width": width, "sep": singles[-1][2].group("sep")}


def _marker_style(text: str) -> str | None:
    for name, pattern in _MARKERS:
        if pattern.search(text):
            return name
    return None


def _read_text(path: Path, limit: int = 2_000_000) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise DbToolError(f"cannot read {path.name}: {exc.strerror or exc}", code=DB_MIGRATION_INVALID) from None
    if len(data) > limit:
        raise DbToolError(f"{path.name} is larger than {limit} bytes", code=DB_MIGRATION_INVALID)
    return data.decode("utf-8", errors="replace")


def _order_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.split(r"[._]", version) if p != "")


# ============================================================================================ generation
@dataclass
class GeneratedMigration:
    framework: Framework
    files: list[Path]
    workspace_root: Path
    revision: str | None = None
    down_revision: str | list[str] | None = None
    notes: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "framework": self.framework.value,
            "files": [_rel(self.workspace_root, f) for f in self.files],
            "path": _rel(self.workspace_root, self.files[0]),
            "revision": self.revision,
            "down_revision": self.down_revision,
            "notes": self.notes,
        }


def clean_description(description: str) -> str:
    text = _CONTROL_RE.sub(" ", description).strip()
    text = " ".join(text.split())[:200]
    if len(SLUG_RE.findall(text)) == 0:
        raise DbToolError("migration description needs at least one word character", code=DB_MIGRATION_INVALID)
    return text


def slugify(text: str, limit: int = 40) -> str:
    slug = "_".join(SLUG_RE.findall(text)).lower()
    if len(slug) > limit:
        slug = slug[:limit].rsplit("_", 1)[0] + "_"
    return slug


def generate_migration(project: MigrationProject, description: str, *, now: datetime | None = None) -> GeneratedMigration:
    """Create the next migration skeleton of ``project`` (no model involved – the coder fills the bodies)."""
    message = clean_description(description)
    created = (now or datetime.now(UTC)).astimezone(UTC)
    if project.framework == Framework.alembic:
        return _generate_alembic(project, message, created)
    if project.framework == Framework.django:
        return _generate_django(project, message)
    if project.framework == Framework.raw_sql:
        return _generate_raw_sql(project, message, created)
    raise DbToolError(
        f"{project.framework.value} migrations are created with the project's CLI: {project.hint}", code=DB_MIGRATION_UNSUPPORTED
    )


# ----------------------------------------------------------------------------------------- alembic
@dataclass(frozen=True)
class AlembicRevision:
    revision: str
    down_revisions: tuple[str, ...]
    path: Path


def parse_alembic_revisions(versions_dir: Path, *, recursive: bool = False) -> tuple[dict[str, AlembicRevision], list[str]]:
    """Revision graph from the version files (``ast``, no import of project code); returns (revisions, warnings)."""
    revisions: dict[str, AlembicRevision] = {}
    warnings: list[str] = []
    if not versions_dir.is_dir():
        return revisions, warnings
    files = sorted(versions_dir.rglob("*.py") if recursive else versions_dir.glob("*.py"))
    for path in files:
        if path.name == "__init__.py" or any(part in SKIP_DIRS for part in path.relative_to(versions_dir).parts[:-1]):
            continue
        try:
            tree = ast.parse(_read_text(path), filename=path.name)
        except SyntaxError as exc:
            warnings.append(f"{path.name}: syntax error in line {exc.lineno} (ignored)")
            continue
        values: dict[str, Any] = {}
        for node in tree.body:
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            for tgt in targets:
                if isinstance(tgt, ast.Name) and tgt.id in ("revision", "down_revision") and value is not None:
                    with contextlib.suppress(ValueError, TypeError, SyntaxError):
                        values[tgt.id] = ast.literal_eval(value)
        rev = values.get("revision")
        if not isinstance(rev, str) or not rev:
            warnings.append(f"{path.name}: no literal 'revision' (ignored)")
            continue
        down = values.get("down_revision")
        downs: tuple[str, ...]
        if down is None or down == "":
            downs = ()
        elif isinstance(down, str):
            downs = (down,)
        elif isinstance(down, list | tuple) and all(isinstance(d, str) for d in down):
            downs = tuple(down)
        else:
            warnings.append(f"{path.name}: unsupported down_revision (treated as base)")
            downs = ()
        if rev in revisions:
            raise DbToolError(
                f"alembic revision '{rev}' is defined twice ({revisions[rev].path.name}, {path.name})", code=DB_MIGRATION_INVALID
            )
        revisions[rev] = AlembicRevision(rev, downs, path)
    return revisions, warnings


def alembic_heads(revisions: Mapping[str, AlembicRevision]) -> list[str]:
    referenced = {d for r in revisions.values() for d in r.down_revisions}
    return sorted(r for r in revisions if r not in referenced)


def alembic_chain(revisions: Mapping[str, AlembicRevision]) -> tuple[list[str], bool]:
    """Revisions from base to the single head; ``linear`` is False for merges/branches (then only the head)."""
    heads = alembic_heads(revisions)
    if len(heads) != 1:
        return heads, False
    chain: list[str] = []
    current: str | None = heads[0]
    seen: set[str] = set()
    linear = True
    while current is not None:
        if current in seen:
            raise DbToolError(f"alembic revision graph has a cycle at '{current}'", code=DB_MIGRATION_INVALID)
        seen.add(current)
        chain.append(current)
        rev = revisions.get(current)
        if rev is None:
            raise DbToolError(f"alembic down_revision '{current}' does not exist", code=DB_MIGRATION_INVALID)
        if len(rev.down_revisions) > 1:
            linear = False
            break
        current = rev.down_revisions[0] if rev.down_revisions else None
    chain.reverse()
    return chain, linear


def _alembic_rev_id(parent: str | None, slug: str, existing: set[str]) -> str:
    salt = 0
    while True:
        rev = hashlib.sha256(f"{parent or 'base'}|{slug}|{salt}".encode()).hexdigest()[:12]
        if rev not in existing:
            return rev
        salt += 1


def _generate_alembic(project: MigrationProject, message: str, created: datetime) -> GeneratedMigration:
    assert project.migrations_dir is not None
    versions = project.migrations_dir
    revisions, warnings = parse_alembic_revisions(versions, recursive=bool(project.settings.get("recursive")))
    heads = alembic_heads(revisions)
    if len(heads) > 1:
        raise DbToolError(
            f"alembic has several heads ({', '.join(heads)}); merge them first ('alembic merge heads')", code=DB_MIGRATION_INVALID
        )
    parent = heads[0] if heads else None
    slug = slugify(message, int(project.settings.get("truncate_slug_length") or 40))
    rev = _alembic_rev_id(parent, slug, set(revisions))
    template = str(project.settings.get("file_template") or "%(rev)s_%(slug)s")
    try:
        stem = template % {
            "rev": rev,
            "slug": slug,
            "epoch": int(created.timestamp()),
            "year": created.year,
            "month": created.month,
            "day": created.day,
            "hour": created.hour,
            "minute": created.minute,
            "second": created.second,
        }
    except (KeyError, ValueError, TypeError):
        raise DbToolError(f"alembic file_template '{template[:80]}' cannot be rendered", code=DB_MIGRATION_INVALID) from None
    if not stem or "\x00" in stem or ".." in PurePosixPath(stem).parts or stem.startswith("/"):
        raise DbToolError("alembic file_template renders an invalid file name", code=DB_MIGRATION_INVALID)
    path = versions / f"{stem}.py"
    ensure_inside(project.workspace_root, path.parent)
    doc = message.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    down_literal = repr(parent) if parent else "None"
    content = f'''"""{doc}

Revision ID: {rev}
Revises: {parent or ""}
Create Date: {created.strftime("%Y-%m-%d %H:%M:%S.%f")}

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = {rev!r}
down_revision: Union[str, None] = {down_literal}
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade: {doc}"""
    # TODO(coder): schema changes, e.g. op.add_column("table", sa.Column("name", sa.String(100), nullable=True))
    pass


def downgrade() -> None:
    """Downgrade: exact reverse of upgrade()."""
    # TODO(coder): undo every change of upgrade() in reverse order
    pass
'''
    write_new_file(path, content, workspace_root=project.workspace_root)
    return GeneratedMigration(Framework.alembic, [path], project.workspace_root, rev, parent, warnings)


# ----------------------------------------------------------------------------------------- django
def _generate_django(project: MigrationProject, message: str) -> GeneratedMigration:
    if project.migrations_dir is None:
        raise DbToolError("Django project has no app with a migrations/ package; pass the app directory as path", code=DB_MIGRATION_INVALID)
    directory = project.migrations_dir
    app_label = str(project.settings.get("app_label") or directory.parent.name)
    existing = [(int(m.group(1)), p.stem) for p in directory.glob("*.py") if (m := _DJANGO_RE.match(p.name))]
    number = max((n for n, _ in existing), default=0) + 1
    latest = max((n for n, _ in existing), default=None)
    deps = sorted(stem for n, stem in existing if n == latest) if latest is not None else []
    slug = slugify(message, 52).strip("_") or "auto"
    path = directory / f"{number:04d}_{slug}.py"
    dep_lines = "".join(f"        ({app_label!r}, {d!r}),\n" for d in deps)
    doc = message.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    content = f'''"""{doc}"""

from django.db import migrations


def upgrade(apps, schema_editor):
    """Forward data changes (fill in or delete together with the RunPython operation)."""


def downgrade(apps, schema_editor):
    """Exact reverse of upgrade()."""


class Migration(migrations.Migration):
    dependencies = [
{dep_lines}    ]

    operations = [
        # TODO(coder): schema operations (migrations.AddField(...), migrations.RunSQL(sql=..., reverse_sql=...))
        migrations.RunPython(upgrade, downgrade),
    ]
'''
    write_new_file(path, content, workspace_root=project.workspace_root)
    notes = [f"merge of {', '.join(deps)}"] if len(deps) > 1 else []
    return GeneratedMigration(Framework.django, [path], project.workspace_root, path.stem, deps, notes)


# ----------------------------------------------------------------------------------------- raw SQL
def _marker_template(style: str, header: str) -> str:
    if style == "goose":
        return (
            f"-- +goose Up\n{header}-- +goose StatementBegin\n\n-- +goose StatementEnd\n\n"
            "-- +goose Down\n-- +goose StatementBegin\n\n-- +goose StatementEnd\n"
        )
    if style == "sql-migrate":
        return f"-- +migrate Up\n{header}\n\n-- +migrate Down\n\n"
    if style == "generic":
        return f"-- up\n{header}\n\n-- down\n\n"
    return f"-- migrate:up\n{header}\n\n-- migrate:down\n\n"


def _next_number(versions: list[str], width: int, created: datetime) -> str:
    if versions and all(len(v) >= TIMESTAMP_DIGITS for v in versions):
        stamp = int(created.strftime("%Y%m%d%H%M%S"))
        return str(max(stamp, max(int(v) for v in versions) + 1))
    nxt = max((int(v) for v in versions), default=0) + 1
    return str(nxt).zfill(width)


def _generate_raw_sql(project: MigrationProject, message: str, created: datetime) -> GeneratedMigration:
    assert project.migrations_dir is not None
    directory = project.migrations_dir
    migrations = parse_sql_migrations(directory)
    style = str(project.settings.get("style") or "single")
    slug = slugify(message, 60).strip("_") or "migration"
    header = f"-- {message}\n"
    files: list[Path] = []
    if style == "flyway":
        majors = [m.order[0] for m in migrations if m.order]
        version = str(max(majors, default=0) + 1)
        up = directory / f"V{version}__{slug}.sql"
        _check_free([up] + ([directory / f"U{version}__{slug}.sql"] if project.settings.get("undo") else []))
        write_new_file(up, f"{header}\n", workspace_root=project.workspace_root)
        files.append(up)
        if project.settings.get("undo"):
            down = directory / f"U{version}__{slug}.sql"
            write_new_file(down, f"-- undo: {message}\n\n", workspace_root=project.workspace_root)
            files.append(down)
        return GeneratedMigration(Framework.raw_sql, files, project.workspace_root, version, None)
    versions = [m.version for m in migrations]
    width = int(project.settings.get("width") or 4)
    version = _next_number(versions, width, created)
    sep = str(project.settings.get("sep") or "_")
    if style == "paired":
        up = directory / f"{version}_{slug}.up.sql"
        down = directory / f"{version}_{slug}.down.sql"
        _check_free([up, down])
        write_new_file(up, f"{header}\n", workspace_root=project.workspace_root)
        write_new_file(down, f"-- revert: {message}\n\n", workspace_root=project.workspace_root)
        files = [up, down]
    else:
        path = directory / f"{version}{sep}{slug}.sql"
        _check_free([path])
        marker = str(project.settings.get("marker_style") or DEFAULT_MARKER_STYLE)
        write_new_file(path, _marker_template(marker, header), workspace_root=project.workspace_root)
        files = [path]
    previous = migrations[-1].version if migrations else None
    return GeneratedMigration(Framework.raw_sql, files, project.workspace_root, version, previous)


def _check_free(paths: list[Path]) -> None:
    for p in paths:
        if p.exists() or p.is_symlink():
            raise DbToolError(f"migration file {p.name} already exists", code=DB_MIGRATION_INVALID)


@dataclass(frozen=True)
class SqlMigration:
    version: str
    order: tuple[int, ...]
    name: str
    up_path: Path
    down_path: Path | None
    up_sql: str
    down_sql: str | None
    transactional: bool = True
    style: str = "single"

    @property
    def label(self) -> str:
        return f"{self.version}_{self.name}"


def split_marked_sql(text: str) -> tuple[str, str | None, str | None, bool]:
    """(up, down, marker style, transactional) of a single-file migration with up/down markers."""
    style = _marker_style(text)
    no_tx = bool(_NO_TX_RE.search(text))
    if style is None:
        return text, None, None, not no_tx
    pattern = dict(_MARKERS)[style]
    marks = list(pattern.finditer(text))
    up_parts: list[str] = []
    down_parts: list[str] = []
    for k, m in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        body = text[m.end() : end]
        (up_parts if m.group(1).lower() == "up" else down_parts).append(body)
        if style == "dbmate" and re.search(r"\btransaction\s*:\s*false\b", m.group(2) or "", re.I):
            no_tx = True
    down = "\n".join(down_parts) if down_parts else None
    return "\n".join(up_parts), down, style, not no_tx


def parse_sql_migrations(directory: Path) -> list[SqlMigration]:
    """Ordered raw SQL migrations of ``directory`` (paired, Flyway or single files with markers)."""
    if not directory.is_dir():
        return []
    paired: dict[str, dict[str, Path]] = {}
    names: dict[str, str] = {}
    flyway: dict[str, dict[str, Path]] = {}
    singles: dict[str, Path] = {}
    for p in sorted(directory.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".sql":
            continue
        if m := _PAIRED_RE.match(p.name):
            paired.setdefault(m.group("v"), {})[m.group("dir").lower()] = p
            names[m.group("v")] = m.group("name")
        elif m := _FLYWAY_RE.match(p.name):
            flyway.setdefault(m.group("v"), {})[m.group("p")] = p
            names[m.group("v")] = m.group("name")
        elif m := _SINGLE_RE.match(p.name):
            if m.group("v") in singles:
                raise DbToolError(
                    f"duplicate migration version {m.group('v')} ({singles[m.group('v')].name}, {p.name})", code=DB_MIGRATION_INVALID
                )
            singles[m.group("v")] = p
            names[m.group("v")] = m.group("name")
    out: list[SqlMigration] = []
    for v, pair in paired.items():
        if "up" not in pair:
            raise DbToolError(f"migration {v} has a .down.sql but no .up.sql file", code=DB_MIGRATION_INVALID)
        up_text = _read_text(pair["up"])
        down_path = pair.get("down")
        out.append(
            SqlMigration(
                v, _order_key(v), names[v], pair["up"], down_path, up_text, _read_text(down_path) if down_path else None, style="paired"
            )
        )
    for v, fw in flyway.items():
        if "V" not in fw:
            raise DbToolError(f"Flyway undo migration U{v} has no V{v} migration", code=DB_MIGRATION_INVALID)
        undo = fw.get("U")
        out.append(
            SqlMigration(v, _order_key(v), names[v], fw["V"], undo, _read_text(fw["V"]), _read_text(undo) if undo else None, style="flyway")
        )
    for v, p in singles.items():
        up, down, marker, transactional = split_marked_sql(_read_text(p))
        out.append(SqlMigration(v, _order_key(v), names[v], p, p if down is not None else None, up, down, transactional, marker or "plain"))
    out.sort(key=lambda item: (item.order, item.name))
    seen: dict[tuple[int, ...], str] = {}
    for mig in out:
        if mig.order in seen:
            raise DbToolError(
                f"duplicate migration version {mig.version} ({seen[mig.order]}, {mig.up_path.name})", code=DB_MIGRATION_INVALID
            )
        seen[mig.order] = mig.up_path.name
    return out


def has_statements(text: str, dialect: Dialect) -> bool:
    try:
        return any(not (t.kind == "punct" and t.value == ";") for t in tokenize(text, dialect))
    except DbPolicyError:
        return True  # the database reports the real syntax error


def split_statements(text: str, dialect: Dialect) -> list[str]:
    """Top-level statements (``;`` outside literals/comments/dollar quotes); used for non-transactional migrations."""
    tokens = tokenize(text, dialect)
    out: list[str] = []
    start = 0
    for tok in tokens:
        if tok.kind == "punct" and tok.value == ";":
            stmt = text[start : tok.start].strip()
            if stmt and has_statements(stmt, dialect):
                out.append(stmt)
            start = tok.end
    tail = text[start:].strip()
    if tail and has_statements(tail, dialect):
        out.append(tail)
    return out


def _pg_error(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    lines = str(primary or exc).strip().splitlines()
    position = getattr(diag, "statement_position", None) if diag is not None else None
    text = lines[0] if lines else type(exc).__name__
    return redact_dsn(text + (f" (at character {position})" if position else ""))[:500]


async def apply_sql_postgres(
    conn: ConnectionInfo, script: str, *, transactional: bool, timeout_seconds: float, connect_timeout: int = 10
) -> None:
    """Run one migration section on the sandbox database (simple query protocol, no client-side meta commands)."""
    if not has_statements(script, "postgresql"):
        return
    ms = max(1, int(timeout_seconds * 1000))
    kwargs = conn.psycopg_kwargs(options=f"-c statement_timeout={ms} -c lock_timeout={min(ms, 60_000)}", connect_timeout=connect_timeout)
    try:
        aconn = await psycopg.AsyncConnection.connect(autocommit=True, **kwargs)
    except psycopg.Error as exc:
        raise DbToolError(f"cannot connect to the sandbox database: {_pg_error(exc)}", code=DB_MIGRATION_FAILED) from None
    try:
        if transactional:
            async with aconn.transaction():
                await aconn.execute(script.encode("utf-8"))
        else:
            for statement in split_statements(script, "postgresql"):
                await aconn.execute(statement.encode("utf-8"))
    except psycopg.errors.QueryCanceled:
        raise DbToolError(f"migration SQL did not finish within {timeout_seconds:g}s", code=DB_MIGRATION_FAILED) from None
    except psycopg.Error as exc:
        raise DbToolError(f"migration SQL failed: {_pg_error(exc)}", code=DB_MIGRATION_FAILED) from None
    finally:
        await aconn.close()


def _migration_authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _src: str | None) -> int:
    if action in _SQLITE_DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY  # ATTACH/DETACH and VACUUM INTO would write files outside the sandbox
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _SQLITE_DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def open_sqlite_sandbox(path: Path, *, timeout_seconds: float) -> sqlite3.Connection:
    """Writable connection to a SQLite sandbox file: no ATTACH, no file functions, deadline via progress handler."""
    deadline = time.monotonic() + timeout_seconds
    db = sqlite3.connect(f"file:{quote(str(path))}?mode=rw", uri=True, isolation_level=None, timeout=min(timeout_seconds, 30.0))
    db.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    db.set_authorizer(_migration_authorizer)
    db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    return db


def apply_sql_sqlite(path: Path, script: str, *, transactional: bool, timeout_seconds: float) -> None:
    if not has_statements(script, "sqlite"):
        return
    try:
        db = open_sqlite_sandbox(path, timeout_seconds=timeout_seconds)
    except sqlite3.Error as exc:
        raise DbToolError(f"cannot open the SQLite sandbox: {exc}", code=DB_MIGRATION_FAILED) from None
    try:
        own_tx = transactional and not _has_tx_statements(script)
        try:
            db.executescript(f"BEGIN;\n{script}\n;COMMIT;" if own_tx else script)
        except sqlite3.Error as exc:
            if db.in_transaction:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
            msg = str(exc)
            if "interrupted" in msg:
                msg = f"did not finish within {timeout_seconds:g}s"
            raise DbToolError(f"migration SQL failed: {msg[:500]}", code=DB_MIGRATION_FAILED) from None
    finally:
        db.close()


def _has_tx_statements(script: str) -> bool:
    try:
        tokens = tokenize(script, "sqlite")
    except DbPolicyError:
        return False
    starts = [tokens[0]] if tokens else []
    starts += [tokens[k + 1] for k, t in enumerate(tokens[:-1]) if t.kind == "punct" and t.value == ";"]
    return any(t.upper in _TX_KEYWORDS for t in starts)


# ============================================================================================ verification
@dataclass
class StepOutcome:
    ok: bool
    detail: str = ""
    duration_ms: int = 0
    commands: list[dict[str, Any]] = field(default_factory=list)


class MigrationDriver(Protocol):
    """Moves the sandbox database along a linear chain of migrations (position -1 = nothing applied)."""

    name: str
    chain: list[str]
    exact_positions: bool  # False: downgrade target is only 'one step back' (merge history, custom command)
    can_downgrade: bool

    async def current(self) -> int: ...

    async def goto(self, position: int) -> StepOutcome: ...


SnapshotFn = Callable[[], Awaitable[tuple[str, dict[str, Any]]]]
CommandHook = Callable[[Sequence[str], str, Mapping[str, str], Path, float], Awaitable[ProcessResult]]


class RawSqlDriver:
    """Applies raw SQL sections in order (up) or reverse order (down) in-process."""

    name = "raw_sql"
    exact_positions = True

    def __init__(
        self,
        migrations: list[SqlMigration],
        conn: ConnectionInfo,
        *,
        timeout_seconds: float,
        connect_timeout: int = 10,
        applied: int = -1,
    ) -> None:
        self.migrations = migrations
        self.chain = [m.label for m in migrations]
        self.conn = conn
        self.timeout_seconds = timeout_seconds
        self.connect_timeout = connect_timeout
        self.position = applied
        self.can_downgrade = all(m.down_sql is not None for m in migrations)

    async def current(self) -> int:
        return self.position

    async def _run(self, migration: SqlMigration, script: str) -> None:
        if self.conn.kind == TargetKind.sqlite:
            assert self.conn.path is not None
            await asyncio.to_thread(
                apply_sql_sqlite, self.conn.path, script, transactional=migration.transactional, timeout_seconds=self.timeout_seconds
            )
        else:
            await apply_sql_postgres(
                self.conn,
                script,
                transactional=migration.transactional,
                timeout_seconds=self.timeout_seconds,
                connect_timeout=self.connect_timeout,
            )

    async def goto(self, position: int) -> StepOutcome:
        started = time.monotonic()
        done: list[str] = []
        label = ""
        try:
            while self.position < position:
                m = self.migrations[self.position + 1]
                label = f"up {m.label}"
                await self._run(m, m.up_sql)
                self.position += 1
                done.append(label)
            while self.position > position:
                m = self.migrations[self.position]
                label = f"down {m.label}"
                if m.down_sql is None:
                    raise DbToolError(f"migration {m.label} has no down section", code=DB_MIGRATION_UNSUPPORTED)
                await self._run(m, m.down_sql)
                self.position -= 1
                done.append(label)
        except DbToolError as exc:
            detail = "; ".join([*done, f"FAILED {label}: {exc.message}"])
            return StepOutcome(False, detail, int((time.monotonic() - started) * 1000))
        return StepOutcome(True, "; ".join(done) or "nothing to do", int((time.monotonic() - started) * 1000))


ALEMBIC_WRAPPER = (
    "import os, sys\n"
    "sys.path.insert(0, os.getcwd())\n"
    "from alembic import command\n"
    "from alembic.config import Config\n"
    "ini = os.environ.get('HERMCLAW_ALEMBIC_INI') or None\n"
    "toml = os.environ.get('HERMCLAW_ALEMBIC_TOML') or None\n"
    "cfg = Config(ini, toml_file=toml) if toml else Config(ini)\n"
    "loc = os.environ.get('HERMCLAW_ALEMBIC_SCRIPT_LOCATION')\n"
    "if loc and not cfg.get_main_option('script_location'):\n"
    "    cfg.set_main_option('script_location', loc)\n"
    "cfg.set_main_option('sqlalchemy.url', os.environ['HERMCLAW_SANDBOX_URL'].replace('%', '%%'))\n"
    "getattr(command, sys.argv[1])(cfg, sys.argv[2])\n"
)


class AlembicDriver:
    """Runs ``alembic upgrade/downgrade`` (through a wrapper that points ``sqlalchemy.url`` at the sandbox)."""

    name = "alembic"

    def __init__(
        self,
        project: MigrationProject,
        *,
        run: CommandHook,
        python: str,
        env: Mapping[str, str],
        state_conn: ConnectionInfo,
        timeout_seconds: float,
        connect_timeout: int = 10,
    ) -> None:
        assert project.migrations_dir is not None
        self.project = project
        revisions, self.warnings = parse_alembic_revisions(project.migrations_dir, recursive=bool(project.settings.get("recursive")))
        if not revisions:
            raise DbToolError("the alembic project has no revisions to apply", code=DB_MIGRATION_INVALID)
        heads = alembic_heads(revisions)
        if len(heads) > 1:
            raise DbToolError(f"alembic has several heads ({', '.join(heads)}); merge them first", code=DB_MIGRATION_INVALID)
        self.chain, linear = alembic_chain(revisions)
        self.exact_positions = linear
        self.can_downgrade = True
        self.run = run
        self.python = python
        self.state_conn = state_conn
        self.timeout_seconds = timeout_seconds
        self.connect_timeout = connect_timeout
        self.position: int | None = None
        settings = project.settings
        self.env: dict[str, str] = {**env, "PYTHONDONTWRITEBYTECODE": "1"}
        if settings.get("ini"):
            self.env["HERMCLAW_ALEMBIC_INI"] = str(settings["ini"])
        if settings.get("toml"):
            self.env["HERMCLAW_ALEMBIC_TOML"] = str(settings["toml"])
        if settings.get("script_location"):
            self.env["HERMCLAW_ALEMBIC_SCRIPT_LOCATION"] = str(settings["script_location"])

    async def current(self) -> int:
        versions = await read_version_rows(self.state_conn, "alembic_version", "version_num", connect_timeout=self.connect_timeout)
        if not versions:
            self.position = -1
            return -1
        known = [self.chain.index(v) for v in versions if v in self.chain]
        if not known:
            raise DbToolError(
                f"the sandbox database is at unknown alembic revision(s) {', '.join(sorted(versions))[:200]}", code=DB_MIGRATION_INVALID
            )
        self.position = max(known)
        return self.position

    async def _alembic(self, action: str, revision: str) -> ProcessResult:
        argv = [self.python, "-c", ALEMBIC_WRAPPER, action, revision]
        return await self.run(argv, f"alembic {action} {revision}", self.env, self.project.root, self.timeout_seconds)

    async def goto(self, position: int) -> StepOutcome:
        started = time.monotonic()
        current = self.position if self.position is not None else await self.current()
        if position == current:
            return StepOutcome(True, "nothing to do")
        if self.exact_positions:
            action = "upgrade" if position > current else "downgrade"
            revision = self.chain[position] if position >= 0 else "base"
        else:
            action = "upgrade" if position > current else "downgrade"
            revision = "heads" if action == "upgrade" else "-1"
        result = await self._alembic(action, revision)
        cmd = {"command": f"alembic {action} {revision}", "exit_code": result.exit_code, "timed_out": result.timed_out}
        if not result.ok:
            reason = "timed out" if result.timed_out else (result.error or f"exit {result.exit_code}")
            detail = f"alembic {action} {revision} failed ({reason}): {failure_text(result)}"
            self.position = None  # unknown – re-read from the database
            return StepOutcome(False, detail, int((time.monotonic() - started) * 1000), [cmd])
        reached = await self.current()  # the command must have moved *the sandbox* (env.py may ignore sqlalchemy.url)
        if self.exact_positions and reached != position:
            where = self.chain[reached] if reached >= 0 else "base"
            detail = (
                f"alembic {action} {revision} reported success but the sandbox database is at {where}: the project's env.py does "
                "not use sqlalchemy.url / DATABASE_URL – it may have migrated a different database"
            )
            return StepOutcome(False, detail, int((time.monotonic() - started) * 1000), [cmd])
        self.position = position
        return StepOutcome(True, f"alembic {action} {revision}", int((time.monotonic() - started) * 1000), [cmd])


_ERROR_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Failure|Warning)\b.*|^(?:ERROR|FATAL|error):.*")


def failure_text(result: ProcessResult, tail_chars: int = 1500) -> str:
    """Key error line first (the last ``…Error: …`` line of a traceback), then the output tail."""
    text = redact_dsn(result.stderr.strip() or result.stdout.strip() or result.error or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    key = next((ln for ln in reversed(lines) if _ERROR_LINE_RE.match(ln)), lines[-1] if lines else "no output")
    tail = text[-tail_chars:]
    return key[:500] + (f"\n{tail}" if tail and tail.strip() != key else "")


class CommandDriver:
    """A project-provided migration command (and optional downgrade command) – one pseudo migration."""

    name = "command"
    exact_positions = True

    def __init__(
        self,
        command: Sequence[str],
        *,
        downgrade_command: Sequence[str] | None,
        run: CommandHook,
        env: Mapping[str, str],
        cwd: Path,
        timeout_seconds: float,
    ) -> None:
        if not command or not all(isinstance(a, str) and a and "\x00" not in a for a in command):
            raise DbToolError("migration command must be a non-empty argv list", code=DB_MIGRATION_INVALID)
        self.command = list(command)
        self.downgrade_command = list(downgrade_command) if downgrade_command else None
        self.chain = ["command"]
        self.can_downgrade = self.downgrade_command is not None
        self.run = run
        self.env = {**env, "PYTHONDONTWRITEBYTECODE": "1"}
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.position = -1

    async def current(self) -> int:
        return self.position

    async def goto(self, position: int) -> StepOutcome:
        if position == self.position:
            return StepOutcome(True, "nothing to do")
        argv = self.command if position > self.position else self.downgrade_command
        if argv is None:
            return StepOutcome(False, "no downgrade command given")
        started = time.monotonic()
        label = " ".join(argv)[:300]
        result = await self.run(argv, label, self.env, self.cwd, self.timeout_seconds)
        cmd = {"command": label, "exit_code": result.exit_code, "timed_out": result.timed_out}
        if not result.ok:
            reason = "timed out" if result.timed_out else (result.error or f"exit {result.exit_code}")
            return StepOutcome(False, f"{label} failed ({reason}): {failure_text(result)}", int((time.monotonic() - started) * 1000), [cmd])
        self.position = position
        return StepOutcome(True, label, int((time.monotonic() - started) * 1000), [cmd])


async def read_version_rows(conn: ConnectionInfo, table: str, column: str, *, connect_timeout: int = 10) -> list[str]:
    """Values of a bookkeeping table in the sandbox (empty when the table does not exist)."""
    if conn.kind == TargetKind.sqlite:
        assert conn.path is not None
        path = conn.path

        def work() -> list[str]:
            db = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
            try:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if not exists:
                    return []
                return [str(r[0]) for r in db.execute(f'SELECT "{column}" FROM "{table}"').fetchall()]
            finally:
                db.close()

        return await asyncio.to_thread(work)
    try:
        aconn = await psycopg.AsyncConnection.connect(autocommit=True, **conn.psycopg_kwargs(connect_timeout=connect_timeout))
    except psycopg.Error as exc:
        raise DbToolError(f"cannot connect to the sandbox database: {_pg_error(exc)}", code=DB_MIGRATION_FAILED) from None
    try:
        cur = await aconn.execute("SELECT to_regclass(%s)::text", (table,))
        row = await cur.fetchone()
        if not row or row[0] is None:
            return []
        cur = await aconn.execute(pgsql.SQL("SELECT {}::text FROM {}").format(pgsql.Identifier(column), pgsql.Identifier(table)))
        return [str(r[0]) for r in await cur.fetchall()]
    except psycopg.Error as exc:
        raise DbToolError(f"cannot read {table}: {_pg_error(exc)}", code=DB_MIGRATION_FAILED) from None
    finally:
        await aconn.close()


async def applied_raw_position(
    conn: ConnectionInfo, migrations: list[SqlMigration], *, connect_timeout: int = 10
) -> tuple[int, str | None]:
    """Position of the last applied raw SQL migration according to a known bookkeeping table (seeded sandboxes)."""
    for table, column in (
        ("schema_migrations", "version"),
        ("goose_db_version", "version_id"),
        ("flyway_schema_history", "version"),
    ):
        rows = await read_version_rows(conn, table, column, connect_timeout=connect_timeout)
        keys = []
        for value in rows:
            with contextlib.suppress(ValueError):
                if value and value != "None":
                    keys.append(_order_key(value))
        if keys:
            latest = max(keys)
            position = -1
            for k, m in enumerate(migrations):
                if m.order <= latest:
                    position = k
            return position, table
    return -1, None


def strip_bookkeeping(description: dict[str, Any], extra: Sequence[str] = ()) -> dict[str, Any]:
    excluded = BOOKKEEPING_TABLES | set(extra)
    schemas = []
    for schema in description.get("schemas", []):
        tables = [t for t in schema.get("tables", []) if t.get("name") not in excluded]
        schemas.append({**schema, "tables": tables})
    return {**description, "schemas": schemas}


async def schema_snapshot(conn: ConnectionInfo, *, timeout_seconds: float = 120, connect_timeout: int = 10) -> tuple[str, dict[str, Any]]:
    """(fingerprint, description) of the sandbox schema without migration bookkeeping tables."""
    desc = strip_bookkeeping(await describe(conn, timeout_seconds=timeout_seconds, connect_timeout=connect_timeout, read_only=True))
    return fingerprint(desc), desc


def schema_diff(before: dict[str, Any], after: dict[str, Any], limit: int = 20) -> list[str]:
    """Readable differences between two descriptions (tables, columns, indexes, foreign keys, views)."""

    def index(desc: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for schema in desc.get("schemas", []):
            for table in schema.get("tables", []):
                out[f"{schema['name']}.{table['name']}"] = table
            for view in schema.get("views", []):
                out[f"{schema['name']}.{view['name']} (view)"] = {"columns": [], "definition": view.get("definition")}
        return out

    a, b = index(before), index(after)
    lines: list[str] = [f"+ table {n}" for n in sorted(set(b) - set(a))] + [f"- table {n}" for n in sorted(set(a) - set(b))]
    for name in sorted(set(a) & set(b)):
        ta, tb = a[name], b[name]
        ca = {c["name"]: c for c in ta.get("columns", [])}
        cb = {c["name"]: c for c in tb.get("columns", [])}
        lines += [f"+ column {name}.{c}" for c in sorted(set(cb) - set(ca))]
        lines += [f"- column {name}.{c}" for c in sorted(set(ca) - set(cb))]
        lines += [f"~ column {name}.{c}: {ca[c]} -> {cb[c]}" for c in sorted(set(ca) & set(cb)) if ca[c] != cb[c]]
        for key in ("primary_key", "indexes", "foreign_keys", "unique_constraints", "check_constraints", "definition"):
            if ta.get(key) != tb.get(key):
                lines.append(f"~ {key} of {name}")
    if len(lines) > limit:
        lines = [*lines[:limit], f"… {len(lines) - limit} more differences"]
    return lines


@dataclass
class VerificationReport:
    driver: str
    scope: str
    chain: list[str]
    steps: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    final_fingerprint: str | None = None

    @property
    def ok(self) -> bool:
        return all(s["ok"] for s in self.steps) and all(c["ok"] for c in self.checks)

    def failed_step(self) -> dict[str, Any] | None:
        return next((s for s in self.steps if not s["ok"]), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "driver": self.driver,
            "scope": self.scope,
            "migrations": len(self.chain),
            "head": self.chain[-1] if self.chain else None,
            "steps": self.steps,
            "checks": self.checks,
            "warnings": self.warnings,
            "final_fingerprint": self.final_fingerprint,
        }


async def verify_migrations(driver: MigrationDriver, snapshot: SnapshotFn, *, scope: VerifyScope = "latest") -> VerificationReport:
    """Upgrade → downgrade → upgrade on the sandbox; schema fingerprints prove downgrade and re-upgrade.

    ``latest``: checks the newest migration (upgrade to its predecessor, upgrade, downgrade to the predecessor –
    schema must equal the predecessor's –, upgrade again – schema must equal the first upgrade's). ``all``: the same
    from the starting state (empty or seeded) over every pending migration. ``none``: upgrade only.
    """
    report = VerificationReport(driver.name, scope, list(driver.chain))
    n = len(driver.chain)
    if n == 0:
        report.warnings.append("no migrations found – nothing to verify")
        return report
    start = await driver.current()
    head = n - 1
    base_fp, base_desc = await snapshot()

    async def step(label: str, position: int) -> tuple[str, dict[str, Any]] | None:
        outcome = await driver.goto(position)
        entry: dict[str, Any] = {
            "step": label,
            "to": driver.chain[position] if position >= 0 else "base",
            "ok": outcome.ok,
            "detail": outcome.detail[:2000],
            "duration_ms": outcome.duration_ms,
        }
        if outcome.commands:
            entry["commands"] = outcome.commands
        report.steps.append(entry)
        if not outcome.ok:
            return None
        fp, desc = await snapshot()
        entry["fingerprint"] = fp
        return fp, desc

    def check(name: str, expected: tuple[str, dict[str, Any]], actual: tuple[str, dict[str, Any]]) -> None:
        ok = expected[0] == actual[0]
        entry: dict[str, Any] = {"check": name, "ok": ok}
        if not ok:
            entry["diff"] = schema_diff(expected[1], actual[1])
        report.checks.append(entry)

    downgrade = scope != "none" and driver.can_downgrade
    if scope != "none" and not driver.can_downgrade:
        report.warnings.append("downgrade not verified: not every migration has a down section / no downgrade command")
    if start >= head:
        report.warnings.append("the newest migration was already applied in the seeded sandbox")
        if downgrade:
            back = head - 1 if scope == "latest" else -1
            if await step("downgrade", back) is not None and (again := await step("upgrade again", head)) is not None:
                check("re-upgrade reproduces the schema", (base_fp, base_desc), again)
        report.final_fingerprint = report.steps[-1].get("fingerprint") if report.steps else base_fp
        return report
    if scope == "latest":
        predecessor: tuple[str, dict[str, Any]] = (base_fp, base_desc)
        if head - 1 > start:
            reached = await step("upgrade to predecessor", head - 1)
            if reached is None:
                return report
            predecessor = reached
        upgraded = await step("upgrade", head)
        if upgraded is None:
            return report
        if upgraded[0] == predecessor[0]:
            report.warnings.append("the newest migration did not change the sandbox schema (data-only migration?)")
        if downgrade:
            down = await step("downgrade", head - 1)
            if down is None:
                return report
            if driver.exact_positions:
                check("downgrade restores the previous schema", predecessor, down)
            else:
                report.warnings.append("merge history: downgrade compared only by re-upgrade")
            again = await step("upgrade again", head)
            if again is None:
                return report
            check("re-upgrade reproduces the schema", upgraded, again)
    else:
        upgraded = await step("upgrade", head)
        if upgraded is None:
            return report
        if upgraded[0] == base_fp:
            report.warnings.append("the migrations did not change the sandbox schema")
        if downgrade:
            down = await step("downgrade", start)
            if down is None:
                return report
            if driver.exact_positions:
                check("downgrade restores the starting schema", (base_fp, base_desc), down)
            again = await step("upgrade again", head)
            if again is None:
                return report
            check("re-upgrade reproduces the schema", upgraded, again)
    report.final_fingerprint = report.steps[-1].get("fingerprint") if report.steps else None
    return report


__all__ = [
    "ALEMBIC_WRAPPER",
    "BOOKKEEPING_TABLES",
    "REPORTED_ONLY",
    "AlembicDriver",
    "AlembicRevision",
    "CommandDriver",
    "Framework",
    "GeneratedMigration",
    "MigrationCommandRunner",
    "MigrationDriver",
    "MigrationProject",
    "RawSqlDriver",
    "SqlMigration",
    "StepOutcome",
    "VerificationReport",
    "alembic_chain",
    "alembic_heads",
    "applied_raw_position",
    "apply_sql_postgres",
    "apply_sql_sqlite",
    "clean_description",
    "detect_framework",
    "ensure_inside",
    "generate_migration",
    "parse_alembic_revisions",
    "parse_sql_migrations",
    "read_version_rows",
    "resolve_project_dir",
    "schema_diff",
    "schema_snapshot",
    "slugify",
    "split_marked_sql",
    "split_statements",
    "strip_bookkeeping",
    "verify_migrations",
    "write_new_file",
]
