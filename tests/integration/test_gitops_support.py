"""Shared helpers for the gitops tests (real git CLI, real PostgreSQL, local bare remotes via file:// URLs).

Imported by the other ``test_gitops_*`` modules; the single test here is a smoke test of the helpers.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from sqlalchemy import select

from hermclaw.core.config import PoliciesConfig
from hermclaw.core.settings import Settings
from hermclaw.gitops import GitEngine, GitLabClient, RepositoryInfo
from hermclaw.persistence.models import Event, GitOperation, Job, Step, VerificationRun

pytestmark = pytest.mark.integration

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "upstream",
    "GIT_AUTHOR_EMAIL": "upstream@example.invalid",
    "GIT_COMMITTER_NAME": "upstream",
    "GIT_COMMITTER_EMAIL": "upstream@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

SEED_FILES = {
    "README.md": "# demo\n",
    "app.py": "def add(a, b):\n    return a + b\n",
    "src/module.py": "VALUE = 1\n",
    "src/util.py": "def helper():\n    return 'help'\n",
    "docs/guide.md": "guide\nline two\n",
    "config/settings.toml": "debug = false\n",
    ".gitignore": "*.log\nbuild/\n",
}


def git(*args: str, cwd: Path, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=False)
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr}")
    return res.stdout.rstrip("\n")


def write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


@dataclass
class Upstream:
    bare: Path
    seed: Path

    @property
    def url(self) -> str:
        return f"file://{self.bare}"

    def commit(self, files: dict[str, str | None], message: str = "upstream change", branch: str = "main") -> str:
        """Commit ``files`` (None deletes) on ``branch`` of the seed clone and push it to the bare remote."""
        git("checkout", "-q", branch, cwd=self.seed)
        git("pull", "-q", "--ff-only", "origin", branch, cwd=self.seed, check=False)
        for rel, content in files.items():
            if content is None:
                (self.seed / rel).unlink()
            else:
                write(self.seed, rel, content)
        git("add", "-A", cwd=self.seed)
        git("commit", "-q", "-m", message, cwd=self.seed)
        git("push", "-q", "origin", branch, cwd=self.seed)
        return git("rev-parse", "HEAD", cwd=self.seed)

    def head(self, branch: str = "main") -> str | None:
        out = git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=self.bare, check=False)
        return out or None

    def force_main(self, files: dict[str, str]) -> str:
        """Rewrite upstream main history (amend + force push) – simulates a force-pushed base."""
        git("checkout", "-q", "main", cwd=self.seed)
        for rel, content in files.items():
            write(self.seed, rel, content)
        git("add", "-A", cwd=self.seed)
        git("commit", "-q", "--amend", "-m", "rewritten", cwd=self.seed)
        git("push", "-q", "--force", "origin", "main", cwd=self.seed)
        return git("rev-parse", "HEAD", cwd=self.seed)


def make_upstream(root: Path) -> Upstream:
    seed = root / "seed"
    seed.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(seed), cwd=root)
    for rel, content in SEED_FILES.items():
        write(seed, rel, content)
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "initial", cwd=seed)
    bare = root / "remote.git"
    git("clone", "-q", "--bare", str(seed), str(bare), cwd=root)
    git("remote", "add", "origin", str(bare), cwd=seed)
    git("fetch", "-q", "origin", cwd=seed)
    git("branch", "-q", "--set-upstream-to=origin/main", "main", cwd=seed)
    return Upstream(bare=bare, seed=seed)


@dataclass
class World:
    engine: GitEngine
    sessionmaker: Any
    upstream: Upstream
    repo: RepositoryInfo
    root: Path
    settings: Settings
    extra: dict[str, Any] = field(default_factory=dict)

    async def job(self, title: str = "Fix login bug") -> uuid.UUID:
        async with self.sessionmaker() as s:
            job = Job(title=title, prompt="p", repository_id=self.repo.id)
            s.add(job)
            await s.commit()
            return job.id

    async def step(self, job_id: uuid.UUID, key: str = "S1") -> uuid.UUID:
        async with self.sessionmaker() as s:
            st = Step(job_id=job_id, step_key=key, title="t", kind="implement", capability="code", goal="g")
            s.add(st)
            await s.commit()
            return st.id

    async def verification(
        self,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        *,
        passed: bool = True,
        status: str | None = None,
        changed_files: list[Any] | None = None,
    ) -> uuid.UUID:
        async with self.sessionmaker() as s:
            vr = VerificationRun(
                job_id=job_id,
                step_id=step_id,
                passed=passed,
                status=status or ("passed" if passed else "failed"),
                changed_files=list(changed_files or []),
            )
            s.add(vr)
            await s.commit()
            return vr.id

    async def git_ops(self, job_id: uuid.UUID | None = None, *, workspace_id: uuid.UUID | None = None) -> list[GitOperation]:
        async with self.sessionmaker() as s:
            q = select(GitOperation).order_by(GitOperation.created_at, GitOperation.id)
            if job_id is not None:
                q = q.where(GitOperation.job_id == job_id)
            if workspace_id is not None:
                q = q.where(GitOperation.workspace_id == workspace_id)
            rows = list((await s.execute(q)).scalars())
            rows.sort(key=lambda r: r.created_at)
            return rows

    async def events(self, job_id: uuid.UUID) -> list[Event]:
        async with self.sessionmaker() as s:
            return list((await s.execute(select(Event).where(Event.job_id == job_id).order_by(Event.sequence))).scalars())


async def make_world(
    sessionmaker: Any,
    root: Path,
    *,
    policies: PoliciesConfig | None = None,
    gitlab: GitLabClient | None = None,
    provider: str = "generic",
    protected_branches: list[str] | None = None,
    gitlab_project_id: str | None = None,
) -> World:
    upstream = make_upstream(root / "upstream")
    settings = Settings(data_dir=root / "data")
    engine = GitEngine(sessionmaker, settings=settings, policies=policies or PoliciesConfig(), gitlab=gitlab)
    repo = await engine.registry.register(
        f"grp/demo-{uuid.uuid4().hex[:8]}",
        upstream.url,
        provider=provider,
        protected_branches=protected_branches or [],
        gitlab_project_id=gitlab_project_id,
    )
    return World(engine=engine, sessionmaker=sessionmaker, upstream=upstream, repo=repo, root=root, settings=settings)


# ------------------------------------------------------------------------------------------- fake GitLab
class FakeGitLab:
    """Minimal GitLab REST v4 server (merge requests, protected branches) on 127.0.0.1 for tests."""

    def __init__(self, token: str = "glpat-test-token-0123456789abcdef") -> None:
        self.token = token
        self.mrs: list[dict[str, Any]] = []
        self.protected: list[str] = ["main", "release/*"]
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.fail_status: int | None = None
        self.fail_count = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                parts = urlsplit(self.path)
                outer.requests.append((method, parts.path, dict(self.headers)))
                if self.headers.get("PRIVATE-TOKEN") != outer.token:
                    self._send(401, {"message": "401 Unauthorized"})
                    return
                if outer.fail_status is not None and outer.fail_count > 0:
                    outer.fail_count -= 1
                    self._send(outer.fail_status, {"message": "unavailable"})
                    return
                segs = [unquote(s) for s in parts.path.split("/") if s]
                # /api/v4/projects/<id>/...
                if len(segs) < 4 or segs[:3] != ["api", "v4", "projects"]:
                    self._send(404, {"message": "404 Not Found"})
                    return
                project, rest = segs[3], segs[4:]
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if rest == ["protected_branches"] and method == "GET":
                    self._send(200, [{"name": n} for n in outer.protected], {"X-Next-Page": ""})
                elif rest == ["merge_requests"] and method == "GET":
                    rows = [
                        m
                        for m in outer.mrs
                        if m["project"] == project
                        and m["state"] == query.get("state", m["state"])
                        and m["source_branch"] == query.get("source_branch", m["source_branch"])
                        and m["target_branch"] == query.get("target_branch", m["target_branch"])
                    ]
                    self._send(200, rows, {"X-Next-Page": ""})
                elif rest == ["merge_requests"] and method == "POST":
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    for m in outer.mrs:
                        if m["project"] == project and m["state"] == "opened" and m["source_branch"] == body["source_branch"]:
                            self._send(409, {"message": ["Another open merge request already exists for this source branch"]})
                            return
                    iid = len([m for m in outer.mrs if m["project"] == project]) + 1
                    mr = {
                        "id": 1000 + len(outer.mrs),
                        "iid": iid,
                        "project": project,
                        "state": "opened",
                        "title": body["title"],
                        "description": body.get("description", ""),
                        "source_branch": body["source_branch"],
                        "target_branch": body["target_branch"],
                        "labels": body.get("labels", ""),
                        "web_url": f"http://gitlab.test/{project}/-/merge_requests/{iid}",
                    }
                    outer.mrs.append(mr)
                    self._send(201, mr)
                else:
                    self._send(404, {"message": "404 Not Found"})

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host!s}:{port}"

    def __enter__(self) -> FakeGitLab:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


async def test_support_upstream_and_world(sessionmaker, tmp_path):
    world = await make_world(sessionmaker, tmp_path)
    assert world.upstream.head() is not None
    sha = world.upstream.commit({"README.md": "# changed\n"})
    assert world.upstream.head() == sha
    assert world.repo.default_branch == "main"
