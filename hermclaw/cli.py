"""``hermclaw`` operator CLI (installation, operations, smoke tests).

Subcommands
  api                     run the HTTP API (uvicorn) – systemd unit hermclaw-api
  scheduler               run the DAG scheduler with all runtime services – systemd unit hermclaw-scheduler
  migrate                 apply database migrations (alembic upgrade head)
  config-check            load + validate the YAML config and print a summary (no secrets)
  token-create NAME       create an API token (printed once) with --scopes read,control,admin
  token-revoke NAME       revoke an API token
  repo-add NAME URL       register a repository (--default-branch, --provider gitlab|generic, --project-id, --protected,
                          --sandbox-image ALIAS)
  repo-list               list registered repositories
  job-submit PROMPT       create a job (--title, --repo NAME, --base-branch, --constraint …)
  job-metrics JOB_ID      measured acceptance criteria of a finished job (--allow-dependencies)
  acceptance-jobs SPEC    live job battery: submit every job of SPEC, wait, measure, report (--out, --only)
  litellm-config OUT      generate the LiteLLM proxy config from config/models.yaml
  schemas OUT_DIR         export the JSON schemas of all contracts
  doctor                  installation/operations preflight: config, DB, paths, tokens, LiteLLM, workers, models,
                          sandbox images, research (--offline, --json); exit 1 when a check fails
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
    from hermclaw.persistence.migrate import head_revision, upgrade_head

    upgrade_head()  # source checkout or the release's <venv>/share/hermclaw (BUG-060)
    print(f"database migrated to head ({head_revision()})")
    return 0


def cmd_doctor(ns: argparse.Namespace) -> int:
    from hermclaw.core.config import load_config
    from hermclaw.core.settings import get_settings
    from hermclaw.doctor import run_doctor

    report = asyncio.run(run_doctor(load_config(), get_settings(), offline=ns.offline, timeout_seconds=ns.timeout))
    if ns.json:
        _print(report.to_dict())
    else:
        print(report.render())
    return 0 if report.ok else 1


def cmd_config_check(_: argparse.Namespace) -> int:
    from hermclaw.core.config import load_config
    from hermclaw.core.errors import ConfigError
    from hermclaw.core.settings import get_settings
    from hermclaw.models.profiles import assert_architecture

    settings = get_settings()
    cfg = load_config()
    # the fixed model architecture (Gemma planner + fallback, Qwen3-Coder, Qwen3.8 reviewer, Qwen3 router, embedding):
    # `hermclaw litellm-config` and the scheduler's gateway refuse a violation, so the deployment's staging check must too
    try:
        warnings = assert_architecture(cfg.models)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
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
        "model_architecture_warnings": [w.as_dict() for w in warnings],
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
            metadata={"sandbox_image": ns.sandbox_image} if ns.sandbox_image else None,
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


async def _create_job(
    sm: Any,
    prompt: str,
    *,
    title: str | None = None,
    repo: str | None = None,
    base_branch: str | None = None,
    priority: int = 50,
    constraints: Sequence[str] = (),
    created_by: str = "cli",
) -> Any:
    from hermclaw.contracts.events import EventType
    from hermclaw.events.store import append_event
    from hermclaw.persistence.models import Job, JobInput

    repo_id = (await _git_engine(sm).registry.resolve(repo)).id if repo else None
    async with sm() as s:
        job = Job(
            title=(title or prompt)[:200],
            prompt=prompt,
            repository_id=repo_id,
            base_branch=base_branch,
            priority=priority,
            created_by=created_by,
        )
        s.add(job)
        await s.flush()
        s.add(JobInput(job_id=job.id, kind="prompt", content=prompt))
        for c in constraints:
            s.add(JobInput(job_id=job.id, kind="constraint", content=c))
        await append_event(
            s,
            EventType.JOB_CREATED,
            source_type=created_by,
            job_id=job.id,
            payload={"title": job.title, "repository_id": str(repo_id) if repo_id else None},
        )
        await s.commit()
        return job


def cmd_job_submit(ns: argparse.Namespace) -> int:
    async def run(sm: Any) -> Any:
        job = await _create_job(
            sm, ns.prompt, title=ns.title, repo=ns.repo, base_branch=ns.base_branch, priority=ns.priority, constraints=ns.constraint or []
        )
        return {"job_id": str(job.id), "status": job.status}

    _print(asyncio.run(_with_db(run)))
    return 0


def cmd_job_metrics(ns: argparse.Namespace) -> int:
    import uuid

    from hermclaw.acceptance.job_metrics import Expectation, collect, evaluate

    async def run(sm: Any) -> Any:
        return await collect(sm, uuid.UUID(ns.job_id))

    metrics = asyncio.run(_with_db(run))
    criteria = evaluate(metrics, Expectation(allow_dependencies=ns.allow_dependencies))
    _print({"metrics": metrics.as_dict(), "criteria": [c.__dict__ for c in criteria], "passed": all(c.passed for c in criteria)})
    return 0


def cmd_acceptance_jobs(ns: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from hermclaw.acceptance.runner import load_spec, report, run_battery, write_report

    specs = load_spec(ns.spec)
    if ns.only:
        specs = [s for s in specs if s.id in set(ns.only)]

    async def run(sm: Any) -> Any:
        async def create(job: dict[str, Any]) -> Any:
            row = await _create_job(
                sm,
                job["prompt"],
                title=job["title"],
                repo=job["repo"],
                base_branch=job["base_branch"],
                constraints=job["constraints"],
                created_by="acceptance",
            )
            return row.id

        return await run_battery(sm, specs, create, poll_s=ns.poll, log=lambda m: print(m, flush=True))

    rep = report(asyncio.run(_with_db(run)))
    js, md = write_report(rep, ns.out, datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    print(f"report: {md} ({js})")
    return 0 if rep["passed"] else 1


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


def cmd_backup(ns: argparse.Namespace) -> int:
    """``hermclaw backup <create|verify|restore-test|restore-full|list|prune> …`` – one CLI for the operator wrapper."""
    from hermclaw.backup.cli import main as backup_main

    return backup_main(ns.args)


def cmd_schemas(ns: argparse.Namespace) -> int:
    from hermclaw.contracts.schema_export import export as export_schemas
    from hermclaw.contracts.schema_export import export_openapi

    paths = export_schemas(Path(ns.out_dir))
    api = export_openapi(Path(ns.out_dir).parent / "openapi.json")
    print(f"{len(paths)} schemas written to {ns.out_dir}; OpenAPI document {api}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermclaw", description="Hermclaw Next operator CLI")
    p.add_argument("--version", action="version", version=f"hermclaw {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("api", help="run the HTTP API").set_defaults(fn=cmd_api)
    sub.add_parser("scheduler", help="run the scheduler + runtime services").set_defaults(fn=cmd_scheduler)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)
    d = sub.add_parser("doctor", help="installation/operations preflight (exit 1 on failures)")
    d.add_argument("--offline", action="store_true", help="skip LiteLLM, worker, model, sandbox and research probes")
    d.add_argument("--json", action="store_true", help="machine-readable report")
    d.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout per probe in seconds")
    d.set_defaults(fn=cmd_doctor)
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
    r.add_argument("--sandbox-image", default=None, help="sandbox image alias/image for this repository (policies.sandbox.images, R-022)")
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
    jm = sub.add_parser("job-metrics", help="measured acceptance criteria of a finished job (analysis 2026-10-16)")
    jm.add_argument("job_id")
    jm.add_argument("--allow-dependencies", action="store_true", help="the job asked for external dependencies")
    jm.set_defaults(fn=cmd_job_metrics)
    aj = sub.add_parser("acceptance-jobs", help="live job battery: submit, wait, measure (exit 1 when a criterion fails)")
    aj.add_argument("spec", type=Path, help="job specification (YAML, see config/acceptance-jobs.example.yaml)")
    aj.add_argument("--out", type=Path, default=Path("/var/lib/hermclaw/acceptance"))
    aj.add_argument("--only", action="append", help="run only this job id (repeatable)")
    aj.add_argument("--poll", type=float, default=30.0, help="status poll interval in seconds")
    aj.set_defaults(fn=cmd_acceptance_jobs)
    lc = sub.add_parser("litellm-config", help="generate the LiteLLM proxy config")
    lc.add_argument("out", type=Path)
    lc.add_argument("--config-dir", type=Path, default=None)
    lc.set_defaults(fn=cmd_litellm_config)
    sc = sub.add_parser("schemas", help="export contract JSON schemas")
    sc.add_argument("out_dir")
    sc.set_defaults(fn=cmd_schemas)
    b = sub.add_parser("backup", add_help=False, help="backup/restore of Hermclaw's own state (python -m hermclaw.backup)")
    b.add_argument("args", nargs=argparse.REMAINDER)
    b.set_defaults(fn=cmd_backup)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["backup"]:  # pass everything (incl. --help and options) to the backup CLI unchanged
        return cmd_backup(argparse.Namespace(args=args[1:]))
    ns = build_parser().parse_args(args)
    return int(ns.fn(ns) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
