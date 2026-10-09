"""Database schema awareness: tables/views declared in SQL or in the migration DSLs of common frameworks."""

from __future__ import annotations

import bisect
import re

_IDENT = r"[`\"\[]?[\w$]+[`\"\]]?"
_SQL_CREATE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP(?:ORARY)?\s+|UNLOGGED\s+|VIRTUAL\s+)?"
    r"(TABLE|VIEW|MATERIALIZED\s+VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?((?:" + _IDENT + r"\.)?" + _IDENT + r")",
    re.I,
)
_DSL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bop\.create_table\(\s*[\"'](\w+)[\"']"), "table"),  # alembic
    (re.compile(r"\bmigrations\.CreateModel\(\s*name\s*=\s*[\"'](\w+)[\"']"), "table"),  # django
    (re.compile(r"\bSchema::create\(\s*[\"'](\w+)[\"']"), "table"),  # laravel
    (re.compile(r"\.createTable(?:IfNotExists)?\(\s*[\"'`](\w+)[\"'`]"), "table"),  # knex / sequelize / typeorm
    (re.compile(r"\bcreate_table\s*\(?\s*[:\"'](\w+)"), "table"),  # rails / sqlalchemy-migrate
    (re.compile(r"^\s*__tablename__\s*=\s*[\"'](\w+)[\"']", re.M), "table"),  # SQLAlchemy ORM models
    (re.compile(r"->createTable\(\s*[\"'](\w+)[\"']"), "table"),  # doctrine / phinx
)


def _strip(name: str) -> str:
    return ".".join(part.strip('`"[]') for part in name.split("."))


def extract_tables(text: str) -> list[tuple[str, str, int, int]]:
    """``(name, kind, start_line, end_line)`` for every declared table/view (sorted, unique by name+kind+line)."""
    if not text:
        return []
    starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line(off: int) -> int:
        return bisect.bisect_right(starts, off)

    out: set[tuple[str, str, int, int]] = set()
    for m in _SQL_CREATE.finditer(text):
        kind = "view" if "VIEW" in m.group(1).upper() else "table"
        end_off = text.find(";", m.end())
        start = line(m.start())
        end = line(end_off) if end_off != -1 else start
        out.add((_strip(m.group(2)), kind, start, min(end, start + 500)))
    for rx, kind in _DSL:
        for m in rx.finditer(text):
            start = line(m.start())
            out.add((m.group(1), kind, start, start))
    return sorted(out, key=lambda t: (t[2], t[0], t[1]))
