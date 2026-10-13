"""Alembic migrations of the Hermclaw database, from a source checkout or an installed release.

A source checkout has ``alembic.ini`` + ``migrations/`` at the repository root. A wheel installation (the deployment
venv ``/opt/hermclaw/venv-<version>``) carries them as data files under ``<venv>/share/hermclaw/`` (pyproject
``[tool.setuptools.data-files]``), so ``hermclaw migrate`` and the rollback in ``docs/runbooks/upgrade.md`` work
without a checkout (BUG-060). ``HERMCLAW_MIGRATIONS_DIR`` (directory holding ``alembic.ini`` and ``migrations/``)
overrides both.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from hermclaw.core.errors import ConfigError


def migration_candidates() -> list[Path]:
    env = os.environ.get("HERMCLAW_MIGRATIONS_DIR")
    out = [Path(env)] if env else []
    out += [Path(__file__).resolve().parents[2], Path(sys.prefix) / "share" / "hermclaw"]
    return out


def migrations_root() -> Path:
    """Directory that holds ``migrations/env.py`` (and normally ``alembic.ini``)."""
    for base in migration_candidates():
        if (base / "migrations" / "env.py").is_file():
            return base
    raise ConfigError(
        "alembic migrations not found (looked in: " + ", ".join(str(p) for p in migration_candidates()) + ")",
        code="MIGRATIONS_MISSING",
    )


def alembic_config() -> Config:
    root = migrations_root()
    ini = root / "alembic.ini"
    cfg = Config(str(ini)) if ini.is_file() else Config()
    cfg.set_main_option("script_location", str(root / "migrations"))
    return cfg


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def upgrade_head() -> None:
    from alembic import command

    command.upgrade(alembic_config(), "head")


__all__ = ["alembic_config", "head_revision", "migration_candidates", "migrations_root", "upgrade_head"]
