"""``python -m hermclaw.backup {create,verify,restore-test,restore-full,list,prune}``.

Every command prints one JSON document to stdout (logs go to stderr) and exits with

* 0 – success (restore test passed, set verified, …)
* 1 – verification or restore test failed (the report says why)
* 2 – usage / configuration error
* 3 – the operation failed (dump, transfer, restore …)
* 75 – another backup operation holds the lock (EX_TEMPFAIL, the timer simply tries again)

The configuration is ``--config`` / ``$HERMCLAW_BACKUP_CONFIG`` / ``<config_dir>/backup.yaml``; ``--target-dir``
replaces the configured target by a local directory (e.g. a manually mounted share).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hermclaw.backup.backup import create_backup, list_backups, prune_backups
from hermclaw.backup.config import BackupConfig, LocalTarget, load_backup_config
from hermclaw.backup.errors import BACKUP_CONFIG_INVALID, BACKUP_LOCKED, BackupError
from hermclaw.backup.restore import restore_full, restore_test
from hermclaw.backup.verify import verify_set_dir
from hermclaw.core.errors import HermclawError
from hermclaw.core.logging import configure_logging
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.core.settings import Settings, get_settings

EXIT_OK = 0
EXIT_FAILED_CHECK = 1
EXIT_USAGE = 2
EXIT_ERROR = 3
EXIT_LOCKED = 75


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="backup.yaml (default: $HERMCLAW_BACKUP_CONFIG or <config_dir>/backup.yaml)")
    common.add_argument("--target-dir", type=Path, help="use this local directory as the backup target")
    common.add_argument("--wait", type=int, metavar="SECONDS", help="wait this long for a running backup operation")
    common.add_argument("--no-events", action="store_true", help="do not write events to the Hermclaw database")
    parser = argparse.ArgumentParser(prog="python -m hermclaw.backup", description="Hermclaw backup/restore (P33)")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", parents=[common], help="create a backup set and transfer it to the target")
    create.add_argument("--no-prune", action="store_true", help="skip retention after the backup")

    verify = sub.add_parser("verify", parents=[common], help="verify checksums/archives of a set on the target")
    verify.add_argument("set", nargs="?", default="latest", help="set id, 'latest' or a set directory")
    verify.add_argument("--latest", action="store_true")
    verify.add_argument("--quick", action="store_true", help="manifest + checksums only")

    rtest = sub.add_parser("restore-test", parents=[common], help="restore a set into a temporary database and verify it")
    rtest.add_argument("set", nargs="?", default="latest", help="set id, 'latest' or a set directory")
    rtest.add_argument("--latest", action="store_true")
    rtest.add_argument("--no-report", action="store_true", help="do not write RESTORE_REPORT.json next to the backup")

    full = sub.add_parser("restore-full", parents=[common], help="disaster recovery: restore a set into a database")
    full.add_argument("--set", dest="set", required=True, help="set id, 'latest' or a set directory")
    full.add_argument("--target-db-url", required=True, help="database URL of the (empty) target database")
    full.add_argument("--password-ref", help="secret reference for the target database password (cred:/file:)")
    full.add_argument("--files-dest", type=Path, help="extract config + artifacts into this empty directory")
    full.add_argument("--force", action="store_true", help="restore into a non-empty database (--clean --if-exists)")

    sub.add_parser("list", parents=[common], help="list the sets on the target")

    prune = sub.add_parser("prune", parents=[common], help="apply the retention policy")
    prune.add_argument("--dry-run", action="store_true")
    return parser


def _set_arg(value: str, latest: bool) -> str | Path:
    if latest or value in ("latest", ""):
        return "latest"
    if "/" in value or value.startswith("."):
        return Path(value).resolve()
    return value


def _load(args: argparse.Namespace, settings: Settings) -> BackupConfig:
    if args.target_dir is not None and args.config is None:
        try:
            cfg = load_backup_config(None, settings)
        except BackupError:
            cfg = BackupConfig(target=LocalTarget(path=args.target_dir.resolve()))
    else:
        cfg = load_backup_config(args.config, settings)
    if args.target_dir is not None:
        cfg = cfg.model_copy(update={"target": LocalTarget(path=args.target_dir.resolve())})
    if args.wait is not None:
        cfg = cfg.model_copy(update={"lock_wait_seconds": max(0, args.wait)})
    return cfg


async def _with_events(args: argparse.Namespace, settings: Settings) -> Any:
    if args.no_events:
        return None
    from hermclaw.persistence.db import get_sessionmaker, init_engine

    init_engine(settings.database_url)
    return get_sessionmaker()


async def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    settings = get_settings()
    cfg = _load(args, settings)
    sessionmaker = await _with_events(args, settings) if args.command in ("create", "restore-test", "restore-full", "prune") else None
    try:
        return await _dispatch(args, cfg, settings, sessionmaker)
    finally:
        if sessionmaker is not None:
            from hermclaw.persistence.db import dispose_engine

            await dispose_engine()


async def _dispatch(args: argparse.Namespace, cfg: BackupConfig, settings: Settings, sessionmaker: Any) -> tuple[int, dict[str, Any]]:
    command = args.command
    if command == "create":
        result = await create_backup(config=cfg, settings=settings, sessionmaker=sessionmaker, prune=False if args.no_prune else None)
        return EXIT_OK, result.model_dump(mode="json")
    if command == "list":
        listings = await list_backups(cfg, settings=settings)
        return EXIT_OK, {
            "target": cfg.target.describe(),
            "sets": [
                {
                    "set_id": s.set_id,
                    "complete": s.complete,
                    "created_at": s.created_at.isoformat(),
                    "location": s.location,
                    "partial": s.partial_dir,
                }
                for s in listings
            ],
        }
    if command == "prune":
        pruned = await prune_backups(cfg, settings=settings, sessionmaker=sessionmaker, dry_run=args.dry_run)
        return (EXIT_ERROR if pruned.errors else EXIT_OK), pruned.model_dump(mode="json")
    if command == "verify":
        return await _verify(args, cfg, settings)
    if command == "restore-test":
        report = await restore_test(
            _set_arg(args.set, args.latest), config=cfg, settings=settings, sessionmaker=sessionmaker, write_report=not args.no_report
        )
        return (EXIT_OK if report.ok else EXIT_FAILED_CHECK), report.model_dump(mode="json")
    if command == "restore-full":
        files_dest = args.files_dest.resolve() if args.files_dest else None
        report = await restore_full(
            _set_arg(args.set, False),
            args.target_db_url,
            force=args.force,
            password_ref=args.password_ref,
            files_dest=files_dest,
            config=cfg,
            settings=settings,
            sessionmaker=sessionmaker,
        )
        return (EXIT_OK if report.ok else EXIT_FAILED_CHECK), report.model_dump(mode="json")
    raise BackupError(f"unknown command {command}", code=BACKUP_CONFIG_INVALID)


async def _verify(args: argparse.Namespace, cfg: BackupConfig, settings: Settings) -> tuple[int, dict[str, Any]]:
    from hermclaw.backup.backup import build_store, latest_complete, signing_key
    from hermclaw.backup.lock import backup_lock

    key = signing_key(cfg, settings)
    chosen = _set_arg(args.set, args.latest)
    if isinstance(chosen, Path):
        report, _ = await verify_set_dir(chosen, signing_key=key, deep=not args.quick, pg_bin_dir=cfg.pg_bin_dir)
        return (EXIT_OK if report.ok else EXIT_FAILED_CHECK), report.model_dump(mode="json")
    store = build_store(cfg, settings)
    async with backup_lock(cfg.lock_path(settings), wait_seconds=cfg.lock_wait_seconds, what="verify"):
        set_id = await latest_complete(cfg, settings=settings, store=store) if chosen == "latest" else chosen
        async with store.materialize(set_id, cfg.effective_restore_work_dir(settings)) as directory:
            report, _ = await verify_set_dir(
                directory, signing_key=key, deep=not args.quick, location=f"{store.describe()}/{set_id}", pg_bin_dir=cfg.pg_bin_dir
            )
    return (EXIT_OK if report.ok else EXIT_FAILED_CHECK), report.model_dump(mode="json")


def _error_doc(exc: HermclawError) -> dict[str, Any]:
    return {"ok": False, "error": {"code": exc.code, "message": DEFAULT_REDACTOR.text(exc.message)}}


def _logs_to_stderr() -> None:
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) is sys.stdout:
            handler.setStream(sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    settings = get_settings()
    configure_logging(settings.log_level, service="hermclaw-backup", json_output=settings.log_json)
    _logs_to_stderr()  # stdout carries exactly one JSON document
    try:
        code, document = asyncio.run(_run(args))
    except BackupError as exc:
        print(json.dumps(_error_doc(exc), indent=2, sort_keys=True))
        if exc.code == BACKUP_LOCKED:
            return EXIT_LOCKED
        return EXIT_USAGE if exc.code == BACKUP_CONFIG_INVALID else EXIT_ERROR
    except HermclawError as exc:  # configuration/secret errors from the foundation
        print(json.dumps(_error_doc(exc), indent=2, sort_keys=True))
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    print(json.dumps(document, indent=2, sort_keys=True, default=str))
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
