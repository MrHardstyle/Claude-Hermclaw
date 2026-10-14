"""``hermclaw doctor`` – installation and operations preflight of a Hermclaw Next deployment (P32.9).

Runs on the orchestrator (``.225``) with the runtime configuration and answers "is this installation able to run
jobs?" with one line per check (``ok`` / ``warn`` / ``fail`` / ``skip``) and an exit code (1 when any check failed):

* ``config``       – every architecture role has an enabled model profile (Gemma planner, Qwen3-Coder worker,
                     Qwen3.8 heavy reviewer, Qwen3 router, embedding), sandbox toolchain aliases (R-022), worker hosts;
                     production reads real ``<name>.yaml`` files (not the ``.example.yaml`` fallback) and no
                     Wake-on-LAN host still carries a placeholder MAC address;
* ``database``     – reachable, migrated to the release's Alembic head, pgvector ≥ 0.8.2 (CVE-2026-3172);
* ``paths``        – data directories exist, are writable and have free space (disk thresholds of the runtime);
* ``api_tokens``   – at least one active admin token exists (``hermclaw token-create``);
* ``litellm``      – the gateway is alive and serves every enabled model alias;
* ``worker:<id>``  – registered, protocol-compatible, recent heartbeat, ``/health`` ok (sleeping workers: Wake-on-LAN);
* ``models:<id>``  – the model worker has every model tag its profiles name installed (``hermclaw-model-pull``);
* ``sandbox:<id>`` – execution worker selftest incl. all sandbox images built (``hermclaw-build-sandbox-images``);
* ``research``     – SearXNG answers JSON searches (unless research is disabled).

``--offline`` skips every network check (config, database, paths and tokens only). No secret values are printed.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw import __version__
from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION
from hermclaw.core.config import HermclawConfig, HostConfig
from hermclaw.core.settings import Settings

Status = Literal["ok", "warn", "fail", "skip"]
REQUIRED_ROLES = ("planner", "coder", "heavy", "fast", "embedding")
CONFIG_FILES = ("hosts", "models", "policies", "capabilities", "logging")  # hermclaw.core.config.load_config
TOOLCHAIN_ALIASES = ("python", "node", "php", "generic")
MIN_PGVECTOR = (0, 8, 2)
HEARTBEAT_STALE_SECONDS = 120
_ADMIN_HINT = "hermclaw token-create admin --scopes admin"


@dataclass
class DoctorCheck:
    name: str
    status: Status
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    def counts(self) -> dict[str, int]:
        out = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
        for c in self.checks:
            out[c.status] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "ok": self.ok,
            "counts": self.counts(),
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, **({"data": c.data} if c.data else {})} for c in self.checks
            ],
        }

    def render(self) -> str:
        width = max((len(c.name) for c in self.checks), default=10)
        lines = [f"{c.status.upper():<4}  {c.name:<{width}}  {c.detail}" for c in self.checks]
        n = self.counts()
        lines.append(f"\n{'OK' if self.ok else 'FAILED'}: {n['ok']} ok, {n['warn']} warn, {n['fail']} fail, {n['skip']} skip")
        return "\n".join(lines)


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for p in value.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def _free_mb(path: Path) -> int:
    import shutil

    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return int(shutil.disk_usage(probe).free // (1024 * 1024))


def _placeholder_mac(mac: str) -> bool:
    """The example/inventory placeholders (``00:00:00:00:..``) or an empty MAC."""
    return not mac or mac.lower().startswith("00:00:00:00:")


class Doctor:
    def __init__(
        self,
        config: HermclawConfig,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None,
        *,
        offline: bool = False,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        token_store: Any = None,
        litellm_key: Callable[[], str | None] | None = None,
        head_revision: Callable[[], str | None] | None = None,
    ) -> None:
        self.config, self.settings, self.sm = config, settings, sessionmaker
        self.offline, self.timeout, self.transport = offline, timeout_seconds, transport
        self.token_store = token_store
        self._litellm_key = litellm_key
        self._head = head_revision
        self.report = DoctorReport()

    def add(self, name: str, status: Status, detail: str, **data: Any) -> None:
        self.report.checks.append(DoctorCheck(name, status, detail, data))

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)

    async def run(self) -> DoctorReport:
        self.check_config()
        self.check_separation()
        await self.check_database()
        self.check_paths()
        await self.check_tokens()
        if self.offline:
            self.add("network", "skip", "--offline: LiteLLM, workers, models, sandbox images and research not checked")
            return self.report
        for check in (self.check_litellm, self.check_workers, self.check_research):
            try:
                await check()
            except Exception as exc:  # one broken probe must not hide the others
                self.add(check.__name__.removeprefix("check_"), "fail", f"{type(exc).__name__}: {str(exc)[:300]}")
        return self.report

    # ------------------------------------------------------------------------------------------- config
    def enabled_profiles(self) -> list[Any]:
        return [p for p in self.config.models.profiles if p.enabled]

    def check_config(self) -> None:
        by_role: dict[str, list[Any]] = {}
        for p in self.enabled_profiles():
            by_role.setdefault(p.role, []).append(p)
        missing = [r for r in REQUIRED_ROLES if r not in by_role]
        roles = {r: [f"{p.alias}={p.model}" for p in by_role.get(r, [])] for r in REQUIRED_ROLES}
        if missing:
            self.add("config:roles", "fail", f"no enabled model profile for role(s): {', '.join(missing)}", roles=roles)
        else:
            self.add("config:roles", "ok", "; ".join(f"{r}: {', '.join(v)}" for r, v in roles.items()), roles=roles)
        sandbox = self.config.policies.sandbox
        absent = [a for a in TOOLCHAIN_ALIASES if a not in sandbox.images]
        if absent:
            self.add(
                "config:sandbox_images",
                "warn",
                f"policies.sandbox.images has no alias for {', '.join(absent)} – such projects run in the default image "
                f"{sandbox.image} (R-022)",
            )
        else:
            self.add("config:sandbox_images", "ok", ", ".join(f"{k}={v}" for k, v in sorted(sandbox.images.items())))
        cdir = Path(self.settings.config_dir)
        fallback = [n for n in CONFIG_FILES if not (cdir / f"{n}.yaml").is_file()]
        if fallback and self.settings.env == "production":
            self.add(
                "config:files",
                "fail",
                f"{cdir} has no {', '.join(f'{n}.yaml' for n in fallback)} – the shipped .example.yaml defaults are in use "
                "(deploy the configuration, docs/operations/INSTALLATION.md)",
            )
        else:
            files = ", ".join(f"{n}.yaml" + (" (example)" if n in fallback else "") for n in CONFIG_FILES)
            self.add("config:files", "ok", f"{cdir}: {files}")
        placeholder = [h.id for h in self.config.hosts.hosts if h.wake_on_lan.enabled and _placeholder_mac(h.wake_on_lan.mac)]
        if placeholder:
            self.add(
                "config:wake_on_lan",
                "warn",
                f"placeholder MAC address for {', '.join(placeholder)} in hosts.yaml – Wake-on-LAN cannot wake them "
                "(set the real MAC: ip -br link on the host)",
            )
        kinds = {h.worker_kind for h in self.worker_hosts()}
        lacking = [k for k in ("execution", "model") if k not in kinds]
        if lacking:
            self.add("config:hosts", "fail", f"hosts.yaml has no {' / '.join(lacking)} worker with worker_api")
        else:
            self.add("config:hosts", "ok", ", ".join(f"{h.id} ({h.worker_kind}) {h.worker_api}" for h in self.worker_hosts()))

    def check_separation(self) -> None:
        """SSH host registry is valid; the sandbox can never reach SSH credentials or the host (P26.8)."""
        from hermclaw.sshtools.registry import HostRegistry
        from hermclaw.sshtools.separation import sandbox_isolation_issues

        try:
            HostRegistry.from_config(self.config)
        except Exception as exc:
            self.add("config:ssh_hosts", "fail", f"invalid SSH host entry: {type(exc).__name__}: {str(exc)[:300]}")
        else:
            allowed = list(self.config.policies.ssh.allowed_hosts)
            self.add("config:ssh_hosts", "ok", f"{len(allowed)} allow-listed SSH host(s): {', '.join(allowed) or 'none'}")
        issues = sandbox_isolation_issues(self.config, workspaces_dir=Path(self.settings.workspaces_dir))
        if issues:
            self.add("config:sandbox_separation", "fail", "; ".join(issues)[:600])
        else:
            self.add("config:sandbox_separation", "ok", "sandbox gets no SSH variables, keys or known_hosts")

    def worker_hosts(self) -> list[HostConfig]:
        return [h for h in self.config.hosts.hosts if h.worker_api and h.worker_kind]

    # ------------------------------------------------------------------------------------------- database
    async def check_database(self) -> None:
        if self.sm is None:
            self.add("database", "skip", "no database configured")
            return
        try:
            async with self.sm() as s:
                await s.execute(text("SELECT 1"))
                try:
                    rev = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
                except Exception:
                    await s.rollback()
                    rev = None
                vector = (await s.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))).scalar_one_or_none()
        except Exception as exc:
            self.add("database", "fail", f"unreachable: {type(exc).__name__}: {str(exc)[:200]}")
            return
        head = self._head() if self._head is not None else _release_head()
        if rev is None:
            self.add("database", "fail", "schema not migrated – run: hermclaw migrate", head=head)
        elif head is not None and rev != head:
            self.add("database", "fail", f"revision {rev}, release head {head} – run: hermclaw migrate", revision=rev, head=head)
        else:
            self.add("database", "ok", f"revision {rev} (release head)", revision=rev)
        if vector is None:
            self.add("database:pgvector", "fail", "extension vector not installed (CREATE EXTENSION vector)")
        elif _version_tuple(str(vector)) < MIN_PGVECTOR:
            self.add("database:pgvector", "fail", f"pgvector {vector} < 0.8.2 (CVE-2026-3172) – install postgresql-17-pgvector from PGDG")
        else:
            self.add("database:pgvector", "ok", f"pgvector {vector}")

    # ------------------------------------------------------------------------------------------- paths
    def check_paths(self) -> None:
        s = self.settings
        for name, path in (
            ("data", Path(s.data_dir)),
            ("workspaces", Path(s.workspaces_dir)),
            ("artifacts", Path(s.artifacts_dir)),
            ("repos", Path(s.repos_cache_dir)),
        ):
            if not path.is_dir():
                self.add(f"paths:{name}", "fail", f"{path} missing (bootstrap creates it)")
                continue
            if not os.access(path, os.W_OK | os.X_OK):
                self.add(f"paths:{name}", "fail", f"{path} not writable by uid {os.getuid()}")
                continue
            free = _free_mb(path)
            if free < s.disk_critical_free_mb:
                self.add(f"paths:{name}", "fail", f"{path}: {free} MiB free < critical {s.disk_critical_free_mb} MiB", free_mb=free)
            elif free < s.disk_warn_free_mb:
                self.add(f"paths:{name}", "warn", f"{path}: {free} MiB free < warn {s.disk_warn_free_mb} MiB", free_mb=free)
            else:
                self.add(f"paths:{name}", "ok", f"{path}: {free} MiB free", free_mb=free)

    async def check_tokens(self) -> None:
        if self.sm is None:
            return
        from hermclaw.persistence.models import ApiToken

        try:
            async with self.sm() as s:
                rows = (await s.execute(select(ApiToken.name, ApiToken.scopes).where(ApiToken.revoked_at.is_(None)))).all()
        except Exception as exc:
            self.add("api_tokens", "fail", f"{type(exc).__name__}: {str(exc)[:200]}")
            return
        admins = [n for n, scopes in rows if "admin" in (scopes or [])]
        if admins:
            self.add("api_tokens", "ok", f"{len(rows)} active token(s), admin: {', '.join(admins)}")
        else:
            self.add("api_tokens", "warn", f"{len(rows)} active token(s), none with admin scope – {_ADMIN_HINT}")

    # ------------------------------------------------------------------------------------------- LiteLLM
    async def check_litellm(self) -> None:
        base = self.config.models.litellm.base_url.rstrip("/")
        key = None
        try:
            key = self._litellm_key() if self._litellm_key is not None else _resolve_litellm_key(self.config)
        except Exception as exc:
            self.add("litellm:key", "fail", f"master key reference not resolvable: {type(exc).__name__}")
        async with self._http() as http:
            try:
                live = await http.get(f"{base}/health/liveliness")
            except httpx.HTTPError as exc:
                self.add("litellm", "fail", f"{base} unreachable: {type(exc).__name__} (systemctl status hermclaw-litellm)")
                return
            if live.status_code != 200:
                self.add("litellm", "fail", f"{base}/health/liveliness → HTTP {live.status_code}")
                return
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            models = await http.get(f"{base}/v1/models", headers=headers)
        if models.status_code != 200:
            self.add("litellm", "fail", f"alive, but /v1/models → HTTP {models.status_code} (master key?)")
            return
        served = {m.get("id") for m in (models.json().get("data") or []) if isinstance(m, dict)}
        aliases = [p.alias for p in self.enabled_profiles()]
        missing = [a for a in aliases if a not in served]
        if missing:
            self.add("litellm", "fail", f"aliases not served: {', '.join(missing)} – hermclaw litellm-config + restart hermclaw-litellm")
        else:
            self.add("litellm", "ok", f"alive, serves {len(aliases)} aliases")

    # ------------------------------------------------------------------------------------------- workers
    async def _rows(self) -> dict[str, Any]:
        if self.sm is None:
            return {}
        from hermclaw.persistence.models import Worker

        async with self.sm() as s:
            return {w.id: w for w in (await s.execute(select(Worker))).scalars()}

    async def check_workers(self) -> None:
        rows = await self._rows()
        for host in self.worker_hosts():
            row = rows.get(host.id)
            name = f"worker:{host.id}"
            if row is None:
                self.add(name, "fail", f"not registered – start the {host.worker_kind} worker daemon on {host.address} (heartbeat)")
                continue
            if row.protocol_version is not None and row.protocol_version != WORKER_PROTOCOL_VERSION:
                self.add(name, "fail", f"protocol {row.protocol_version} ≠ {WORKER_PROTOCOL_VERSION}: upgrade the worker to {__version__}")
                continue
            if row.state in ("sleeping", "offline") and host.wake_on_lan.enabled:
                self.add(name, "warn", f"state {row.state}: woken by Wake-on-LAN before work (not probed)")
                continue
            age = (datetime.now(UTC) - row.last_heartbeat_at).total_seconds() if row.last_heartbeat_at else None
            health = await self._health(host)
            if health is None:
                self.add(name, "fail", f"state {row.state}, /health unreachable at {host.worker_api}")
                continue
            if age is not None and age > HEARTBEAT_STALE_SECONDS:
                self.add(name, "warn", f"/health {health}, but last heartbeat {age:.0f} s ago (orchestrator URL/token on the worker?)")
            else:
                self.add(name, "ok" if health == "ok" else "warn", f"state {row.state}, /health {health}, version {row.worker_version}")
            if host.worker_kind == "model":
                await self._models(host, row)
            elif host.worker_kind == "execution":
                await self._sandbox(host, row)

    async def _health(self, host: HostConfig) -> str | None:
        async with self._http() as http:
            try:
                r = await http.get(f"{str(host.worker_api).rstrip('/')}/health")
            except httpx.HTTPError:
                return None
        if r.status_code != 200:
            return None
        return str(r.json().get("status", "?"))

    def _client(self, cls: Any, row: Any) -> Any:
        from hermclaw.workers.schemas import WorkerInfo

        info = WorkerInfo.model_validate(
            {"id": row.id, "hostname": row.hostname, "address": row.address, "kind": row.kind, "state": row.state, "api_url": row.api_url}
        )
        store = self.token_store
        if store is None:
            from hermclaw.workers.auth import RefTokenStore

            store = RefTokenStore(self.settings.worker_token_refs)
        return cls.for_worker(info, store, transport=self.transport, timeout_seconds=max(self.timeout, 30.0))

    async def _models(self, host: HostConfig, row: Any) -> None:
        from hermclaw.workers.client import ModelWorkerClient

        wanted = sorted({p.model for p in self.enabled_profiles() if p.host == host.id})
        try:
            client = self._client(ModelWorkerClient, row)
        except Exception as exc:
            self.add(f"models:{host.id}", "fail", f"no worker credential: {type(exc).__name__} (worker_token_refs)")
            return
        async with client:
            listing = await client.models()
        installed = set(listing.installed)
        missing = [m for m in wanted if m not in installed and f"{m}:latest" not in installed]
        if missing:
            self.add(f"models:{host.id}", "fail", f"not installed: {', '.join(missing)} – scripts/ops/hermclaw-model-pull", missing=missing)
        else:
            self.add(f"models:{host.id}", "ok", f"{len(wanted)} role model(s) installed: {', '.join(wanted)}")

    async def _sandbox(self, host: HostConfig, row: Any) -> None:
        from hermclaw.workers.client import ExecutionWorkerClient

        try:
            client = self._client(ExecutionWorkerClient, row)
        except Exception as exc:
            self.add(f"sandbox:{host.id}", "fail", f"no worker credential: {type(exc).__name__} (worker_token_refs)")
            return
        async with client:
            result = await client.selftest(timeout_seconds=300.0)
        failed = [f"{c.name}: {c.detail}" for c in result.checks if not c.ok]
        if failed:
            self.add(f"sandbox:{host.id}", "fail", "; ".join(failed)[:600])
        else:
            self.add(f"sandbox:{host.id}", "ok", ", ".join(f"{c.name}" for c in result.checks))

    # ------------------------------------------------------------------------------------------- research
    async def check_research(self) -> None:
        policy = self.config.policies.research
        if policy.search_provider == "none":
            self.add("research", "skip", "research disabled (policies.research.search_provider: none)")
            return
        url = policy.searxng_url.rstrip("/")
        async with self._http() as http:
            try:
                r = await http.get(f"{url}/search", params={"q": "hermclaw doctor", "format": "json"})
            except httpx.HTTPError as exc:
                self.add("research", "fail", f"SearXNG {url} unreachable: {type(exc).__name__}")
                return
        if r.status_code != 200:
            hint = " (enable the json format in settings.yml: search.formats)" if r.status_code == 403 else ""
            self.add("research", "fail", f"SearXNG {url}/search → HTTP {r.status_code}{hint}")
            return
        try:
            n = len(r.json().get("results") or [])
        except ValueError:
            self.add("research", "fail", "SearXNG answered without JSON (search.formats: [html, json])")
            return
        self.add("research", "ok" if n else "warn", f"SearXNG {url}: {n} result(s) for a test query")


def _release_head() -> str | None:
    try:
        from hermclaw.persistence.migrate import head_revision

        return head_revision()
    except Exception:
        return None


def _resolve_litellm_key(config: HermclawConfig) -> str | None:
    from hermclaw.models.profiles import resolve_api_key

    return resolve_api_key(config.models.litellm.api_key_ref, required=False)


async def run_doctor(config: HermclawConfig, settings: Settings, *, offline: bool = False, timeout_seconds: float = 5.0) -> DoctorReport:
    from hermclaw.persistence.db import dispose_engine, get_sessionmaker, init_engine

    init_engine(settings.database_url)
    try:
        return await asyncio.wait_for(
            Doctor(config, settings, get_sessionmaker(), offline=offline, timeout_seconds=timeout_seconds).run(), timeout=900
        )
    finally:
        await dispose_engine()


__all__ = ["Doctor", "DoctorCheck", "DoctorReport", "run_doctor"]
