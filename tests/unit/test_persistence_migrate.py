"""BUG-060: migrations ship with the release and are found without a source checkout."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from hermclaw.core.errors import ConfigError
from hermclaw.persistence import migrate

ROOT = Path(__file__).resolve().parents[2]


def test_source_checkout_is_found_and_head_resolves() -> None:
    assert migrate.migrations_root() == ROOT
    head = migrate.head_revision()
    assert head and any((ROOT / "migrations" / "versions").glob(f"*_{head}_*.py"))


def test_installed_layout_and_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    share = tmp_path / "share" / "hermclaw"
    shutil.copytree(ROOT / "migrations", share / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "alembic.ini", share / "alembic.ini")
    monkeypatch.setenv("HERMCLAW_MIGRATIONS_DIR", str(share))
    assert migrate.migrations_root() == share
    cfg = migrate.alembic_config()
    assert cfg.get_main_option("script_location") == str(share / "migrations")
    assert migrate.head_revision() == migrate.head_revision()  # deterministic
    monkeypatch.setattr(migrate, "migration_candidates", lambda: [tmp_path / "nothing"])
    with pytest.raises(ConfigError):
        migrate.migrations_root()


def test_pyproject_data_files_cover_every_migration() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["data-files"]
    shipped: set[Path] = set()
    for _target, patterns in data.items():
        for pattern in patterns:
            shipped |= set(ROOT.glob(pattern))
    needed = {ROOT / "alembic.ini", ROOT / "migrations" / "env.py", ROOT / "migrations" / "script.py.mako"}
    needed |= set((ROOT / "migrations" / "versions").glob("*.py"))
    assert needed <= shipped, sorted(str(p.relative_to(ROOT)) for p in needed - shipped)
    assert data["share/hermclaw/migrations/versions"] == ["migrations/versions/*.py"]
