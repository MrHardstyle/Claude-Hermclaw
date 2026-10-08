"""Pure unit tests for the gitops building blocks (no database, no network)."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import httpx
import pytest

from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import HermclawConfig, HostConfig, HostsConfig, ScopePolicy, SshConfig, load_config
from hermclaw.core.errors import ValidationFailed
from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED
from hermclaw.gitops import GitLabClient, GitRunner, GitSshOptions, build_commit_message, ssh_options_from_config
from hermclaw.gitops._secrets import read_secret, secret_file_path
from hermclaw.gitops.errors import GitCommandError, GitLabError, GitLockTimeout, InvalidRemoteUrl, SecretRefError
from hermclaw.gitops.locks import KeyedLocks, combined_lock, file_lock
from hermclaw.gitops.naming import job_branch_name, matches_protected, safe_dir_name, slugify, validate_branch_name
from hermclaw.gitops.parsing import parse_name_status, parse_numstat, parse_porcelain_v1, parse_push_porcelain
from hermclaw.gitops.scope_guard import StagingGuard, decide_path, gitattributes_lines, normalise_repo_path
from hermclaw.gitops.types import StatusEntry
from hermclaw.gitops.urls import project_path_from_url, redact_url, strip_userinfo, validate_remote_url


# ============================================================================================ naming
def test_slug_and_job_branch_name():
    job = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert slugify("Fix: Login Bug!") == "fix-login-bug"
    assert slugify("Ümlaut ärger") == "umlaut-arger"
    assert slugify("!!!") == "job"
    assert len(slugify("x" * 100)) == 40
    assert job_branch_name("hermclaw/", job, "Fix: Login Bug!") == "hermclaw/12345678-fix-login-bug"
    assert job_branch_name("", job, "a") == "12345678-a"


@pytest.mark.parametrize(
    "name",
    [
        "-x",
        "a..b",
        "a b",
        "a~b",
        "a^b",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        "x.lock",
        ".hidden",
        "a/",
        "/a",
        "a//b",
        "@",
        "HEAD",
        "refs/heads/x",
        "a@{b",
    ],
)
def test_invalid_branch_names(name):
    with pytest.raises(ValidationFailed):
        validate_branch_name(name)


def test_valid_branch_names_and_protection_globs():
    for name in ["main", "hermclaw/abc-1", "release/1.2.3", "feature/x_y"]:
        assert validate_branch_name(name) == name
    pats = ["main", "release/*", "*-stable"]
    assert matches_protected("main", pats) == "main"
    assert matches_protected("refs/heads/release/1.0", pats) == "release/*"
    assert matches_protected("v2-stable", pats) == "*-stable"
    assert matches_protected("Main", pats) is None  # case-sensitive
    assert matches_protected("hermclaw/x", pats) is None


def test_safe_dir_name():
    assert safe_dir_name("demo") == "demo"
    a, b = safe_dir_name("grp/demo"), safe_dir_name("grp__demo")
    assert "/" not in a and a != b
    assert ".." not in safe_dir_name("../../etc")


# ============================================================================================ urls
@pytest.mark.parametrize(
    "url",
    [
        "ssh://git@192.168.178.226:22/grp/demo.git",
        "git@192.168.178.226:grp/demo.git",
        "https://gitlab.example/grp/demo.git",
        "file:///srv/x.git",
        "/srv/x.git",
    ],
)
def test_valid_remote_urls(url):
    assert validate_remote_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c id",
        "fd::1",
        "-oProxyCommand=x",
        "https://u:p@host/x.git",
        "ftp://h/x",
        "file://relative",
        "rel/path",
        "a b",
        "http:///x",
    ],
)
def test_invalid_remote_urls(url):
    with pytest.raises(InvalidRemoteUrl):
        validate_remote_url(url)


def test_url_redaction_helpers():
    assert redact_url("fatal: https://oauth2:glpat-xyz@host/x.git not found") == "fatal: https://oauth2:***@host/x.git not found"
    assert strip_userinfo("https://user@host:8443/x.git") == "https://host:8443/x.git"
    assert project_path_from_url("git@host:grp/sub/demo.git") == "grp/sub/demo"
    assert project_path_from_url("ssh://git@host/grp/demo.git") == "grp/demo"
    assert project_path_from_url("/srv/x") is None


# ============================================================================================ parsing
def test_parse_porcelain_handles_renames_and_odd_names():
    data = b" M app.py\0?? new file.txt\0R  new.py\0old.py\0UU conflict.py\0D  gone\nline.py\0"
    entries = parse_porcelain_v1(data)
    assert [(e.code, e.path, e.orig_path) for e in entries] == [
        (" M", "app.py", None),
        ("??", "new file.txt", None),
        ("R ", "new.py", "old.py"),
        ("UU", "conflict.py", None),
        ("D ", "gone\nline.py", None),
    ]
    assert entries[1].untracked and entries[3].conflicted
    with pytest.raises(ValueError):
        parse_porcelain_v1(b"garbage")


def test_parse_name_status_and_numstat():
    ns = parse_name_status(b"M\0a.py\0R087\0old.py\0new.py\0A\0b c.py\0")
    assert [(n.status, n.path, n.old_path, n.score) for n in ns] == [
        ("M", "a.py", None, None),
        ("R", "new.py", "old.py", 87),
        ("A", "b c.py", None, None),
    ]
    st = parse_numstat(b"1\t2\ta.py\0-\t-\tbin.dat\0" + b"3\t0\t\0old.py\0new.py\0")
    assert [(n.path, n.additions, n.deletions, n.old_path, n.binary) for n in st] == [
        ("a.py", 1, 2, None, False),
        ("bin.dat", None, None, None, True),
        ("new.py", 3, 0, "old.py", False),
    ]


def test_parse_push_porcelain():
    out = "To file:///srv/x.git\n*\trefs/heads/a:refs/heads/a\t[new branch]\n!\trefs/heads/b:refs/heads/b\t[rejected] (stale info)\n+\trefs/heads/c:refs/heads/c\tabc...def (forced update)\nDone\n"
    res = parse_push_porcelain(out)
    assert [(r.flag, r.destination, r.ok, r.reason) for r in res] == [
        ("*", "refs/heads/a", True, None),
        ("!", "refs/heads/b", False, "stale info"),
        ("+", "refs/heads/c", True, "forced update"),
    ]


def test_status_entry_operation():
    assert StatusEntry(path="a", index="?", worktree="?").operation == "create"
    assert StatusEntry(path="a", index=" ", worktree="D").operation == "delete"
    assert StatusEntry(path="a", index=" ", worktree="M").operation == "modify"
    assert StatusEntry(path="a", index=" ", worktree="T").operation == "modify"


# ============================================================================================ staging decisions
SCOPE = ScopeContract(
    target_paths=["app.py", "src/**"],
    allowed_new_paths=["src/**", "tests/*.py"],
    forbidden_paths=["src/vendor/**"],
    allowed_operations=["create", "modify"],
)
POLICY = ScopePolicy()


@pytest.mark.parametrize(
    "path,op,allowed,reason",
    [
        ("app.py", "modify", True, None),
        ("src/a/b.py", "create", True, None),
        ("tests/test_x.py", "create", True, None),
        ("tests/sub/test_x.py", "create", False, "outside_scope"),
        ("README.md", "modify", False, "outside_scope"),
        ("src/vendor/lib.py", "modify", False, "forbidden"),
        ("src/.env", "create", False, "always_forbidden"),
        ("src/key.pem", "create", False, "always_forbidden"),
        (".git/config", "modify", False, "git_internal"),
        ("src/sub/.git/HEAD", "modify", False, "git_internal"),
        ("src/a.py", "delete", False, "operation_not_allowed"),
        ("../x.py", "modify", False, "invalid_path"),
        ("/etc/passwd", "modify", False, "invalid_path"),
        (" app.py", "modify", False, "invalid_path"),
        ("src\\a.py", "modify", False, "invalid_path"),
        ("src/nested/", "create", False, "embedded_repository"),
    ],
)
def test_staging_guard_decisions(path, op, allowed, reason):
    d = StagingGuard(SCOPE, POLICY).decide(path, op)
    assert d.allowed is allowed and d.reason == reason


def test_staging_guard_strictness_matches_scope_authority():
    strict = ScopeContract(target_paths=["docs/a.md"], allowed_new_paths=["new/**"], allowed_operations=["create", "modify"])
    lax = strict.model_copy(update={"strict_target_paths": False})
    assert not decide_path("new/x.md", "modify", strict, []).allowed
    assert decide_path("new/x.md", "modify", lax, []).allowed
    assert decide_path("docs/a.md", "create", strict, []).allowed  # explicitly listed target may be (re)created


def test_normalise_repo_path_never_rewrites():
    assert normalise_repo_path("a/b.py") == "a/b.py"
    for bad in ["./a", "a/./b", "a/../b", "", "a\x00b", "a ", "a\\b"]:
        assert normalise_repo_path(bad) is None


def test_gitattributes_lines():
    lines = gitattributes_lines([".git/**", "**/.env", "**/*.pem", "secrets/", "config/prod.yaml"])
    assert "**/.env -diff" in lines and "**/*.pem -diff" in lines
    assert "/secrets/** -diff" in lines and "/config/prod.yaml -diff" in lines and "/config/prod.yaml/** -diff" in lines
    assert not any(line.startswith("/.git") for line in lines)


# ============================================================================================ commit message
def test_build_commit_message_trailers_and_redaction():
    job, step, vr = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    msg = build_commit_message("  Fix thing\r\n\r\nuses token=abcd1234secret\x07\n", job_id=job, step_id=step, verification_run_id=vr)
    lines = msg.splitlines()
    assert lines[0] == "Fix thing" and "abcd1234secret" not in msg and REDACTED in msg and "\x07" not in msg
    assert lines[-3:] == [f"Hermclaw-Job: {job}", f"Hermclaw-Step: {step}", f"Hermclaw-Verification: {vr}"]
    long = build_commit_message("x" * 500, job_id=job, step_id=None, verification_run_id=vr)
    assert len(long.splitlines()[0]) == 200 and "Hermclaw-Step" not in long
    with pytest.raises(ValidationFailed):
        build_commit_message(" \n ", job_id=job, step_id=None, verification_run_id=vr)


# ============================================================================================ secrets
def test_read_secret_env_file_cred(tmp_path):
    f = tmp_path / "tok"
    f.write_text("glpat-file-secret-value-123456\n")
    assert read_secret(f"file:{f}") == "glpat-file-secret-value-123456"
    assert read_secret("env:HC_TEST_TOKEN", environ={"HC_TEST_TOKEN": " env-secret-value "}) == "env-secret-value"
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "gitlab-token").write_text("cred-secret-value-xyz")
    env = {"CREDENTIALS_DIRECTORY": str(creds)}
    assert read_secret("cred:gitlab-token", environ=env) == "cred-secret-value-xyz"
    assert secret_file_path("cred:gitlab-token", environ=env) == creds / "gitlab-token"
    assert secret_file_path("cred:other", environ=env, secrets_dir=tmp_path) == tmp_path / "other"
    # resolved values are redacted everywhere afterwards
    assert DEFAULT_REDACTOR.text("x cred-secret-value-xyz y") == f"x {REDACTED} y"


@pytest.mark.parametrize(
    "ref",
    ["plain", "env:", "env:1BAD", "env:MISSING_HC_VAR", "file:relative", "file:/nonexistent/hc", "cred:../etc", "vault:x"],
)
def test_read_secret_errors_never_leak(ref):
    with pytest.raises(SecretRefError) as err:
        read_secret(ref, environ={})
    assert err.value.code == "SECRET_REF_INVALID"


def test_read_secret_empty_file(tmp_path):
    f = tmp_path / "empty"
    f.write_text("\n")
    with pytest.raises(SecretRefError):
        read_secret(f"file:{f}")
    with pytest.raises(SecretRefError):
        secret_file_path("env:X")


# ============================================================================================ runner / ssh
def test_runner_environment_is_hardened():
    runner = GitRunner(
        author_name="Hermclaw Runtime", author_email="h@x", ssh=GitSshOptions(key_path=Path("/k/id"), known_hosts=Path("/k/kh"))
    )
    env = runner.environment()
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["LC_ALL"] == "C" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_ALLOW_PROTOCOL"] == "file:ssh:https:http"
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "Hermclaw Runtime"
    ssh = env["GIT_SSH_COMMAND"]
    assert "StrictHostKeyChecking=yes" in ssh and "BatchMode=yes" in ssh and "-i /k/id" in ssh and "UserKnownHostsFile=/k/kh" in ssh
    argv = runner.argv(["status"])
    assert "core.autocrlf=false" in argv and "core.hooksPath=/dev/null" in argv and argv[-1] == "status"


def test_ssh_options_from_config(tmp_path):
    base = load_config()
    assert ssh_options_from_config(base).key_path is None  # gitlab host without ssh block
    key = tmp_path / "id_ed25519"
    key.write_text("k")
    hosts = HostsConfig(
        hosts=[
            HostConfig(
                id="gitlab-226",
                address="192.168.178.226",
                role="gitlab",
                ssh=SshConfig(user="git", key_ref=f"file:{key}", known_hosts=str(tmp_path / "kh")),
            )
        ]
    )
    cfg = HermclawConfig(hosts=hosts, models=base.models, policies=base.policies, capabilities=base.capabilities, logging=base.logging)
    opts = ssh_options_from_config(cfg)
    assert opts.key_path == key and opts.known_hosts == tmp_path / "kh"


async def test_runner_errors_are_redacted(tmp_path):
    runner = GitRunner(author_name="t", author_email="t@x")
    DEFAULT_REDACTOR.add_literal("supersecret-token-value")
    with pytest.raises(GitCommandError) as err:
        await runner.run(["ls-remote", "https://user:supersecret-token-value@127.0.0.1:9/x.git"], cwd=tmp_path, timeout_s=10)
    text = str(err.value) + str(err.value.details)
    assert "supersecret-token-value" not in text
    with pytest.raises(GitCommandError):
        await runner.run(["status"], cwd=tmp_path / "missing")
    res = await runner.run(["--version"], max_stdout=4)
    assert res.stdout_truncated and len(res.stdout) == 4


async def test_runner_blocks_ext_transport(tmp_path):
    runner = GitRunner(author_name="t", author_email="t@x")
    marker = tmp_path / "pwned"
    with pytest.raises(GitCommandError):
        await runner.run(["ls-remote", f"ext::sh -c touch% {marker}"], cwd=tmp_path, timeout_s=10)
    assert not marker.exists()


# ============================================================================================ locks
async def test_file_lock_serialises_and_times_out(tmp_path):
    lock = tmp_path / "l.lock"
    order: list[str] = []

    async def worker(name: str) -> None:
        async with file_lock(lock, wait_seconds=5):
            order.append(f"{name}-in")
            await asyncio.sleep(0.05)
            order.append(f"{name}-out")

    await asyncio.gather(worker("a"), worker("b"))
    assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"])
    async with file_lock(lock):
        with pytest.raises(GitLockTimeout):
            async with file_lock(lock, wait_seconds=0.1):
                pass
    locks = KeyedLocks()
    async with combined_lock(locks, "k", lock):
        with pytest.raises(GitLockTimeout):
            async with combined_lock(locks, "k", lock, wait_seconds=0.1):
                pass


# ============================================================================================ GitLab client (MockTransport)
async def test_gitlab_client_retries_and_errors():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"message": "busy"})
        if request.method == "GET":
            return httpx.Response(200, json=[], headers={"X-Next-Page": ""})
        return httpx.Response(422, json={"message": "token=glpat-abcdefghijklmnopqrstu invalid"})

    client = GitLabClient(
        "http://gitlab.test", token="glpat-unit-token-0000000000", retries=2, backoff_seconds=0.0, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(GitLabError) as err:
            await client.create_merge_request("grp/demo", source_branch="hermclaw/x", target_branch="main", title="t")
        assert err.value.details["status"] == 422 and "glpat-abcdefghijklmnopqrstu" not in str(err.value)
        assert calls[0].url.raw_path.split(b"?")[0] == b"/api/v4/projects/grp%2Fdemo/merge_requests"  # path stays encoded on the wire
        assert calls[0].headers["PRIVATE-TOKEN"] == "glpat-unit-token-0000000000"
        assert len(calls) == 3  # 503 retried, then lookup ok, then POST 422
    finally:
        await client.aclose()
    with pytest.raises(GitLabError):
        GitLabClient("http://gitlab.test")


async def test_gitlab_client_lazy_token_ref(tmp_path):
    tok = tmp_path / "t"
    tok.write_text("glpat-lazy-token-11111111111111")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["PRIVATE-TOKEN"])
        return httpx.Response(200, json={"id": 1, "path_with_namespace": "grp/demo"})

    client = GitLabClient("http://gitlab.test/api/v4", token_ref=f"file:{tok}", transport=httpx.MockTransport(handler))
    try:
        assert (await client.get_project(7))["id"] == 1
        assert seen == ["glpat-lazy-token-11111111111111"] and client.api_url == "http://gitlab.test/api/v4"
    finally:
        await client.aclose()
