"""Database tools for jobs that work on the databases of user projects (Bauplan §30, P27).

Entry point: :class:`hermclaw.dbtools.tools.DbTools` (``db_introspect``, ``db_query_readonly``,
``db_migration_generate``, ``db_migration_apply_sandbox``, ``db_backup``, ``db_restore_test``); the tool-engine adapter
is :mod:`hermclaw.dbtools.provider`. See ``docs/architecture/dbtools.md``.
"""
