"""``hermclaw`` operator CLI (installation, operations, smoke tests).

Subcommands
  api                     run the HTTP API (uvicorn) – systemd unit hermclaw-api
  scheduler               run the DAG scheduler with all runtime services – systemd unit hermclaw-scheduler
  migrate                 apply database migrations (alembic upgrade head)
  config-check            load + validate the YAML config and print a summary (no secrets)
  token-create NAME       create an API token (printed once) with --scopes read,control,admin
  token-revoke NAME       revoke an API token
  repo-add NAME URL       register a repository (--default-branch, --provider gitlab|generic, --project-id, --protected)
  repo-list               list registered repositories
  job-submit PROMPT       create a job (--title, --repo NAME, --base-branch, --constraint …)
  litellm-config OUT      generate the LiteLLM proxy config from config/models.yaml
  schemas OUT_DIR         export the JSON schemas of all contracts
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hermclaw import __version__


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


async def _with_db(coro_fn: Any) -> Any:
    from hermclaw.core.settings import get_settings
    from hermclaw.persistence.db import dispose_engine, get_sessionmaker, init_engine

    init_engine(get_settings().database_url)
    try:
        return await coro_fn(get_sessionmaker())
    finally:
        await dispose_engine()


def cmd_migrate(_: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    ini = root / "alembic.ini"
    cfg = Config(str(ini)) if ini.exists() else Config()
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")
    print("database migrated to head")
    return 0


def cmd_config_check(_: argparse.Namespace) -> int:
    from hermclaw.core.config import load_config
    from hermclaw.core.settings import get_settings

    settings = get_settings()
    cfg = load_config()
    out = {
        "version": __version__,
        "config_dir": str(settings.config_dir),
        "data_dir": str(settings.data_dir),
        "hosts": [h.model_dump(include={"id", "address", "role", "worker_kind"}) for h in cfg.hosts.hosts],
        "litellm": cfg.models.litellm.base_url,
        "model_profiles": {p.alias: {"model": p.model, "role": p.role, "context": p.context_tokens} for p in cfg.models.profiles},
        "capabilities": sorted(c.name for c in cfg.capabilities.capabilities),
        "coder_max_turns": cfg.policies.coder.max_turns,
        "max_replans_per_job": cfg.policies.correction.max_replans_per_job,
    }
    _print(out)
    return 0


def cmd_token_create(ns: argparse.Namespace) -> int:
    from hermclaw.persistence.models import ApiToken
    from hermclaw.security.tokens import generate_token, hash_token

    scopes = [s.strip() for s in ns.scopes.split(",") if s.strip()]
    bad = set(scopes) - {"read", "control", "admin"}
    if bad:
        print(f"unknown scopes: {', '.join(sorted(bad))}", file=sys.stderr)
        return 2
    token = generate_token()

    async def run(sm: Any) -> None:
        async with sm() as s:
            s.add(ApiToken(name=ns.name, token_hash=hash_token(token), scopes=scopes))
            await s.commit()

    asyncio.run(_with_db(run))
    print(f"token '{ns.name}' ({', '.join(scopes)}) – shown once, store it now:\n{token}")
    return 0


def cmd_token_revoke(ns: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from sqlalchemy import update

    from hermclaw.persistence.models import ApiToken

    async def run(sm: Any) -> int:
        async with sm() as s:
            res = await s.execute(
                update(ApiToken).where(ApiToken.name == ns.name, ApiToken.revoked_at.is_(None)).values(revoked_at=datetime.now(UTC))
            )
            await s.commit()
            return int(res.rowcount or 0)  # type: ignore[attr-defined]

    n = asyncio.run(_with_db(run))
    print(f"revoked {n} token(s)")
    return 0 if n else 1


def _git_engine(sm: Any) -> Any:
    from hermclaw.core.config import get_config
    from hermclaw.core.settings import get_settings
    from hermclaw.gitops.engine import GitEngine

    return GitEngine.from_config(get_config(), get_settings(), sm, gitlab=None)


def cmd_repo_add(ns: argparse.Namespace) -> int:
    async def run(sm: Any) -> Any:
        engine = _git_engine(sm)
        info = await engine.registry.register(
            ns.name,
            ns.url,
            default_branch=ns.default_branch,
            provider=ns.provider,
            gitlab_project_id=ns.project_id,
            protected_branches=tuple(ns.protected or ()),
            update=ns.update,
        )
        return info.model_dump()

    _print(asyncio.run(_with_db(run)))
    return 0


def cmd_repo_list(_: argparse.Namespace) -> int:
    async def run(sm: Any) -> Any:
        return [r.model_dump() for r in await _git_engine(sm).registry.list_repositories()]

    _print(asyncio.run(_with_db(run)))
    return 0


def cmd_job_submit(ns: argparse.Namespace) -> int:
    from hermclaw.contracts.events import EventType
    from hermclaw.events.store import append_event
    from hermclaw.persistence.models import Job, JobInput

    async def run(sm: Any) -> Any:
        repo_id = None
        if ns.repo:
            repo_id = (await _git_engine(sm).registry.resolve(ns.repo)).id
        async with sm() as s:
            job = Job(
                title=(ns.title or ns.prompt)[:200],
                prompt=ns.prompt,
                repository_id=repo_id,
                base_branch=ns.base_branch,
                priority=ns.priority,
                created_by="cli",
            )
            s.add(job)
            await s.flush()
            s.add(JobInput(job_id=job.id, kind="prompt", content=ns.prompt))
            for c in ns.constraint or []:
                s.add(JobInput(job_id=job.id, kind="constraint", content=c))
            await append_event(
                s,
                EventType.JOB_CREATED,
                source_type="cli",
                job_id=job.id,
                payload={"title": job.title, "repository_id": str(repo_id) if repo_id else None},
            )
            await s.commit()
            return {"job_id": str(job.id), "status": job.status}

    _print(asyncio.run(_with_db(run)))
    return 0


def cmd_api(_: argparse.Namespace) -> int:  # pragma: no cover - process entry point
    from hermclaw.api.app import main as api_main

    api_main()
    return 0


def cmd_scheduler(_: argparse.Namespace) -> int:  # pragma: no cover - process entry point
    from hermclaw.runtime.main import main as scheduler_main

    return int(scheduler_main())


def cmd_litellm_config(ns: argparse.Namespace) -> int:
    from hermclaw.models.litellm_config import main as litellm_main

    return litellm_main([str(ns.out), *(["--config-dir", str(ns.config_dir)] if ns.config_dir else [])])


def cmd_schemas(ns: argparse.Namespace) -> int:
    from hermclaw.contracts.schema_export import export as export_schemas

    paths = export_schemas(Path(ns.out_dir))
    print(f"{len(paths)} schemas written to {ns.out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermclaw", description="Hermclaw Next operator CLI")
    p.add_argument("--version", action="version", version=f"hermclaw {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("api", help="run the HTTP API").set_defaults(fn=cmd_api)
    sub.add_parser("scheduler", help="run the scheduler + runtime services").set_defaults(fn=cmd_scheduler)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)
    sub.add_parser("config-check", help="validate the configuration").set_defaults(fn=cmd_config_check)
    t = sub.add_parser("token-create", help="create an API token")
    t.add_argument("name")
    t.add_argument("--scopes", default="read,control")
    t.set_defaults(fn=cmd_token_create)
    tr = sub.add_parser("token-revoke", help="revoke an API token")
    tr.add_argument("name")
    tr.set_defaults(fn=cmd_token_revoke)
    r = sub.add_parser("repo-add", help="register a repository")
    r.add_argument("name")
    r.add_argument("url")
    r.add_argument("--default-branch", default="main")
    r.add_argument("--provider", default="gitlab", choices=["gitlab", "generic"])
    r.add_argument("--project-id", default=None)
    r.add_argument("--protected", action="append", help="protected branch glob (repeatable)")
    r.add_argument("--update", action="store_true", help="update an existing registration")
    r.set_defaults(fn=cmd_repo_add)
    sub.add_parser("repo-list", help="list repositories").set_defaults(fn=cmd_repo_list)
    j = sub.add_parser("job-submit", help="create a job")
    j.add_argument("prompt")
    j.add_argument("--title", default=None)
    j.add_argument("--repo", default=None, help="registered repository name or id")
    j.add_argument("--base-branch", default=None)
    j.add_argument("--priority", type=int, default=50)
    j.add_argument("--constraint", action="append")
    j.set_defaults(fn=cmd_job_submit)
    lc = sub.add_parser("litellm-config", help="generate the LiteLLM proxy config")
    lc.add_argument("out", type=Path)
    lc.add_argument("--config-dir", type=Path, default=None)
    lc.set_defaults(fn=cmd_litellm_config)
    sc = sub.add_parser("schemas", help="export contract JSON schemas")
    sc.add_argument("out_dir")
    sc.set_defaults(fn=cmd_schemas)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    return int(ns.fn(ns) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
