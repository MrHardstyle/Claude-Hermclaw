"""Unit regressions from the P06 review: lock bookkeeping, GitLab pagination, workspace integrity checks."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from hermclaw.gitops import GitLabClient, ops
from hermclaw.gitops.errors import GitLockTimeout
from hermclaw.gitops.locks import KeyedLocks, combined_lock
from hermclaw.gitops.runner import GitRunner

ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    _git("init", "-q", "-b", "main", str(path), cwd=tmp_path)
    (path / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=path)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=path)
    _git("config", "remote.origin.url", "file:///srv/x.git", cwd=path)
    _git("config", "push.default", "nothing", cwd=path)
    return path


async def test_keyed_locks_release_entries_and_still_serialise(tmp_path):
    locks = KeyedLocks()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with combined_lock(locks, "k", tmp_path / "k.lock", wait_seconds=5):
            order.append(f"{name}-in")
            await asyncio.sleep(0.02)
            order.append(f"{name}-out")

    await asyncio.gather(worker("a"), worker("b"), worker("c"))
    assert all(order[i].split("-")[0] == order[i + 1].split("-")[0] for i in range(0, len(order), 2))
    assert len(locks) == 0
    async with combined_lock(locks, "k", tmp_path / "k.lock"):
        with pytest.raises(GitLockTimeout):
            async with combined_lock(locks, "k", tmp_path / "k.lock", wait_seconds=0.05):
                pass
        assert len(locks) == 1
    assert len(locks) == 0


async def test_gitlab_pagination_stops_on_malformed_or_looping_header():
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        pages.append(page)
        nxt = {"1": "2", "2": "2"}.get(page, "")  # page 2 points to itself
        return httpx.Response(200, json=[{"name": f"b{page}"}], headers={"X-Next-Page": nxt})

    client = GitLabClient("http://gitlab.test", token="glpat-unit-token-0000000000", transport=httpx.MockTransport(handler))
    try:
        assert await client.list_protected_branches("grp/demo") == ["b1", "b2"]
        assert pages == ["1", "2"]
    finally:
        await client.aclose()

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "main"}], headers={"X-Next-Page": "abc"})

    client = GitLabClient("http://gitlab.test", token="glpat-unit-token-0000000000", transport=httpx.MockTransport(garbage))
    try:
        assert await client.list_protected_branches(1) == ["main"]
    finally:
        await client.aclose()


async def test_integrity_accepts_runtime_shaped_config(repo):
    runner = GitRunner(author_name="t", author_email="t@t")
    assert await ops.integrity_problems(runner, repo) == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("filter.x.clean", "touch /tmp/pwned"),
        ("merge.x.driver", "touch /tmp/pwned"),
        ("diff.x.textconv", "touch /tmp/pwned"),
        ("core.worktree", "/etc"),
        ("core.sshCommand", "touch /tmp/pwned"),
        ("remote.origin.pushurl", "file:///tmp/evil.git"),
        ("remote.origin.receivepack", "touch /tmp/pwned"),
        ("http.extraHeader", "X: y"),
        ("includeIf.gitdir:/.path", "/tmp/evil"),
    ],
)
async def test_integrity_refuses_dangerous_config(repo, key, value):
    _git("config", key, value, cwd=repo)
    problems = await ops.integrity_problems(GitRunner(author_name="t", author_email="t@t"), repo)
    assert problems and key.lower().split(".")[0] in problems[0]


async def test_integrity_refuses_symlinked_git_dir(repo, tmp_path):
    moved = tmp_path / "elsewhere"
    (repo / ".git").rename(moved)
    (repo / ".git").symlink_to(moved)
    problems = await ops.integrity_problems(GitRunner(author_name="t", author_email="t@t"), repo)
    assert problems == [".git is not a plain directory inside the workspace"]


async def test_local_config_lists_include_entries_without_following_them(repo, tmp_path):
    inc = tmp_path / "inc.cfg"
    inc.write_text('[filter "y"]\n\tclean = cat\n')
    _git("config", "include.path", str(inc), cwd=repo)
    entries = dict(await ops.local_config(GitRunner(author_name="t", author_email="t@t"), repo))
    assert entries["include.path"] == str(inc) and "filter.y.clean" not in entries


@pytest.mark.parametrize("url", ["https://glpat-abcdefghijklmnopqrstu@gitlab.example/grp/demo.git", "http://user@h/x.git"])
def test_http_urls_with_userinfo_are_rejected(url):
    from hermclaw.gitops.errors import InvalidRemoteUrl
    from hermclaw.gitops.urls import validate_remote_url

    with pytest.raises(InvalidRemoteUrl) as err:
        validate_remote_url(url)
    assert "glpat" not in str(err.value)
