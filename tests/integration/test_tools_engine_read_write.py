"""P17 17.1-17.4 + 17.13: read tools, git read, file writes, patches – real git workspace + PostgreSQL."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermclaw.contracts.events import EventType
from hermclaw.contracts.tools import ToolName
from hermclaw.core.interfaces import RepoHit
from hermclaw.core.redaction import REDACTED
from hermclaw.tools import errors as E
from hermclaw.tools.context import ToolPermissions
from tests.integration.test_tools_support import (
    SM,
    ScriptedRepo,
    commit_all,
    events_for,
    git,
    make_harness,
    scope,
    tool_calls,
)


@pytest.fixture
def repo(tmp_repo: Path) -> Path:
    (tmp_repo / "src").mkdir()
    (tmp_repo / "src" / "calc.py").write_text("def mul(a, b):\n    return a * b\n\n\ndef div(a, b):\n    return a / b\n", encoding="utf-8")
    (tmp_repo / "src" / "util.py").write_text("X = 1\nX = 1\n", encoding="utf-8")
    (tmp_repo / "tests").mkdir()
    (tmp_repo / "tests" / "test_calc.py").write_text("from src.calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
    (tmp_repo / ".env").write_text("API_TOKEN=supersecretvalue123\n")
    (tmp_repo / "logo.bin").write_bytes(b"\x89PNG\x00\x00binary")
    (tmp_repo / "config.ini").write_text("[db]\npassword = hunter2hunter2\nhost = localhost\n")
    commit_all(tmp_repo)
    return tmp_repo


IMPLEMENT_SCOPE = {"target_paths": ["src/calc.py", "app.py"], "allowed_new_paths": ["src/new_*.py", "docs/"]}


# ------------------------------------------------------------------------------------------------ 17.1 read tools
async def test_list_files_hides_secrets_and_git_and_honours_filters(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo)
    res = await h.call("list_files")
    assert res.ok and not res.truncated
    listed = res.output.splitlines()
    assert "src/calc.py" in listed and "tests/test_calc.py" in listed and "README.md" in listed
    assert ".env" not in listed and not any(p.startswith(".git/") for p in listed)
    res = await h.call("list_files", path="src", pattern="c*.py")
    assert res.output.splitlines() == ["src/calc.py"]
    res = await h.call("list_files", recursive=False)
    assert "src/" in res.output.splitlines() and "app.py" in res.output.splitlines()
    res = await h.call("list_files", max_entries=2)
    assert res.ok and res.truncated and res.data["shown"] == 2 and "more entries" in res.output
    res = await h.call("list_files", path="app.py")
    assert res.error_code == E.NOT_A_DIRECTORY


async def test_read_file_and_range(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo)
    res = await h.call("read_file", path="./src//calc.py")
    assert (
        res.ok
        and res.output.startswith("def mul(a, b):")
        and res.data == {"path": "src/calc.py", "bytes": res.data["bytes"], "lines": 6, "first": 1, "last": 6}
    )
    res = await (await make_harness(sessionmaker, repo)).call("read_range", path="src/calc.py", start=5, end=99)
    assert res.ok and res.output.splitlines()[0] == "5| def div(a, b):" and "[end of file: 6 lines]" in res.output
    assert (await h.call("read_range", path="src/calc.py", start=50, end=60)).error_code == E.RANGE_INVALID
    assert (await h.call("read_range", path="src/calc.py", start=5, end=2)).error_code == E.ARGS_INVALID
    assert (await h.call("read_file", path="logo.bin")).error_code == E.BINARY_FILE
    assert (await h.call("read_file", path="missing.py")).error_code == E.NOT_FOUND
    assert (await h.call("read_file", path="src")).error_code == E.NOT_A_FILE


async def test_identical_read_of_an_unchanged_file_is_refused_within_the_context_window(sessionmaker: SM, repo: Path) -> None:
    """Live battery J6/J3: the coder re-read the same unchanged file 3-4 turns in a row although every turn's context
    already shows the files read in the attempt (BUG-179)."""
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["src/calc.py"]))
    assert (await h.call("read_file", path="src/calc.py")).ok
    again = await h.call("read_file", path="./src//calc.py")
    assert again.error_code == E.READ_REPEATED and again.data["repeated"] is True and "turn 1" in again.output
    assert (await h.call("read_range", path="src/util.py", start=1, end=1)).ok, "a different read runs"
    assert (await h.call("read_range", path="src/util.py", start=1, end=1)).error_code == E.READ_REPEATED
    assert (await h.call("read_file", path="missing.py")).error_code == E.NOT_FOUND
    assert (await h.call("read_file", path="missing.py")).error_code == E.NOT_FOUND, "failed reads are not remembered"
    assert (await h.call("replace_text", path="src/calc.py", old="a * b", new="b * a")).ok
    assert (await h.call("read_file", path="src/calc.py")).ok, "a file changed: reading again is allowed"
    for _ in range(6):
        await h.call("list_files", path=".")
    assert (await h.call("read_file", path="src/calc.py")).ok, "outside the window the file may have left the context"


async def test_a_read_the_context_cannot_show_in_full_may_be_repeated(sessionmaker: SM, repo: Path) -> None:
    """Live G1 (BUG-229): the guard refused re-reads of a clipped 624-line file as 'shown above' although the context
    showed only part of it; the coder circled on refused reads until the step stagnated."""
    (repo / "long.py").write_text("".join(f"v{i} = {i}\n" for i in range(1, 501)))
    h = await make_harness(sessionmaker, repo)
    assert (await h.call("read_file", path="long.py")).ok
    assert (await h.call("read_file", path="long.py")).ok, "more lines than the context shows: allowed again"
    assert (await h.call("read_range", path="long.py", start=1, end=450)).ok
    assert (await h.call("read_range", path="long.py", start=1, end=450)).ok, "range longer than the context shows"
    assert (await h.call("read_range", path="long.py", start=10, end=40)).ok
    assert (await h.call("read_range", path="long.py", start=10, end=40)).error_code == E.READ_REPEATED


async def test_a_part_of_a_visible_read_is_refused_until_a_file_changes(sessionmaker: SM, repo: Path) -> None:
    """Live G1 (BUG-244): the coder read collector.py 100-160, then 110-160, then 110-145 – each a part of what the
    context already showed – one ~45 s turn per read. A range inside a fully visible read of the window is refused."""
    (repo / "mid.py").write_text("".join(f"m{i} = {i}\n" for i in range(1, 201)))
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["mid.py"]))
    assert (await h.call("read_range", path="mid.py", start=100, end=160)).ok
    inner = await h.call("read_range", path="mid.py", start=110, end=145)
    assert inner.error_code == E.READ_REPEATED and "turn 1" in inner.output and "lines 100-160" in inner.output
    assert (await h.call("read_range", path="mid.py", start=90, end=120)).ok, "reaches beyond what was read"
    assert (await h.call("read_file", path="src/calc.py")).ok
    assert (await h.call("read_range", path="src/calc.py", start=2, end=4)).error_code == E.READ_REPEATED, "whole file shown"
    assert (await h.call("replace_text", path="mid.py", old="m150 = 150", new="m150 = 151")).ok
    assert (await h.call("read_range", path="mid.py", start=140, end=155)).ok, "a file changed: reading again is allowed"
    (repo / "long.py").write_text("".join(f"v{i} = {i}\n" for i in range(1, 501)))
    assert (await h.call("read_file", path="long.py")).ok
    assert (await h.call("read_range", path="long.py", start=420, end=440)).ok, "the clipped read did not show these lines"


async def test_reading_a_clipped_file_again_continues_where_the_last_read_stopped(sessionmaker: SM, repo: Path) -> None:
    """Live G1 (BUG-264): read_file of an 845-line collector.py showed its first ~300 lines; the coder read it again
    three times, saw the same head every time and was stopped for stagnation. Reading a clipped, unchanged file again
    continues where the previous read stopped; after the end, or after a file change, it starts at line 1."""
    (repo / "big.py").write_text("".join(f"value_number_{i:04d} = {i}  # a line of the big module\n" for i in range(1, 901)))
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["big.py"]))
    first = (await h.call("read_file", path="big.py")).output
    assert first.startswith("[big.py: lines 1-")  # the span lets the next turn's context show this page (BUG-266)
    assert "value_number_0001" in first and "big.py has 900 lines; lines 1-" in first and "read_file again shows lines" in first
    shown = int(first.rsplit("lines 1-", 1)[1].split(" ", 1)[0])
    second = (await h.call("read_file", path="big.py")).output
    assert second.startswith(f"[big.py: lines {shown + 1}-") and "continuing your read of turn 1" in second
    assert f"value_number_{shown + 1:04d}" in second and "value_number_0001 " not in second
    pages = [second]
    while "read_file again shows" in pages[-1]:
        pages.append((await h.call("read_file", path="big.py")).output)
    assert "value_number_0900" in pages[-1] and "of 900" in pages[-1]
    assert "value_number_0001" in (await h.call("read_file", path="big.py")).output, "after the end it starts over"
    assert (await h.call("read_file", path="big.py")).output.startswith("[big.py: lines")
    assert (await h.call("replace_text", path="big.py", old="value_number_0002 = 2 ", new="value_number_0002 = 3 ")).ok
    assert "value_number_0001" in (await h.call("read_file", path="big.py")).output, "a file changed: from line 1 again"


async def test_a_masked_secret_can_be_replaced_by_the_line_as_shown(sessionmaker: SM, repo: Path) -> None:
    """Live G1 (BUG-248): the verifier demanded removing ``api_key='…'`` from a test, every read showed the value as the
    redaction marker, and replace_text refused the masked line – no tool could fix it. The masked line as shown is
    matched on the redacted view and replaced; nothing masked is written back."""
    (repo / "tests" / "test_client.py").write_text("def make():\n    return Client(api_key='qwzrtvkplm', timeout=5)\n\n\nX = 1\n")
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["tests/test_client.py"]))
    shown = (await h.call("read_file", path="tests/test_client.py")).output
    masked_line = next(ln for ln in shown.splitlines() if "api_key" in ln)
    assert REDACTED in masked_line and "qwzrtvkplm" not in shown
    res = await h.call("replace_text", path="tests/test_client.py", old="api_key='" + REDACTED + "'", new="api_key='dummy-token'")
    assert res.ok, res.output
    text = (repo / "tests" / "test_client.py").read_text()
    assert "return Client(api_key='dummy-token', timeout=5)\n" in text and "qwzrtvkplm" not in text and REDACTED not in text
    assert text.startswith("def make():\n") and text.endswith("X = 1\n")
    # two identical masked lines: ambiguous, refused with the hint
    (repo / "tests" / "test_two.py").write_text("A = dict(password='aaaa1111')\nB = dict(password='bbbb2222')\n")
    h2 = await make_harness(sessionmaker, repo, contract=scope(target_paths=["tests/test_two.py"]))
    amb = await h2.call("replace_text", path="tests/test_two.py", old="password='" + REDACTED + "'", new="password='dummy'")
    assert amb.error_code == E.TEXT_NOT_FOUND and "copy the masked line exactly" in amb.output
    one = await h2.call(
        "replace_text", path="tests/test_two.py", old="B = dict(password='" + REDACTED + "')", new="B = dict(password='dummy')"
    )
    assert one.ok and (repo / "tests" / "test_two.py").read_text() == "A = dict(password='aaaa1111')\nB = dict(password='dummy')\n"
    # the text around the match would carry a masked part: refused, nothing masked is written back
    (repo / "tests" / "test_both.py").write_text("C = dict(password='x1x1x1x1', api_key='y2y2y2y2')\n")
    h3 = await make_harness(sessionmaker, repo, contract=scope(target_paths=["tests/test_both.py"]))
    refused = await h3.call("replace_text", path="tests/test_both.py", old="api_key='" + REDACTED + "'", new="api_key='dummy'")
    assert refused.error_code == E.TEXT_NOT_FOUND
    assert (repo / "tests" / "test_both.py").read_text() == "C = dict(password='x1x1x1x1', api_key='y2y2y2y2')\n"


async def test_a_masked_line_left_unchanged_keeps_its_content(sessionmaker: SM, repo: Path) -> None:
    """Live G1 (BUG-270): the coder changed the call around a fixture password; 'old' and 'new' both carried the
    masked password line unchanged and the edit was refused for the marker. A masked line copied unchanged from 'old'
    into 'new' keeps its real content; a marker on any other line is still refused."""
    body = "def make():\n    return Client(\n        'srv',\n        password='qwzrtvkplm',\n    )\n"
    (repo / "tests" / "test_client.py").write_text(body)
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["tests/test_client.py"]))
    masked = "        password='" + REDACTED + "',\n"
    old = "    return Client(\n        'srv',\n" + masked + "    )"
    res = await h.call("replace_text", path="tests/test_client.py", old=old, new="    return Client(\n" + masked + "    )")
    assert res.ok, res.output
    assert (repo / "tests" / "test_client.py").read_text() == "def make():\n    return Client(\n        password='qwzrtvkplm',\n    )\n"
    moved = "    return Client(\n" + masked + "    )"
    bad = await h.call("replace_text", path="tests/test_client.py", old=moved, new="    return Client(token='" + REDACTED + "')")
    assert bad.error_code == E.REDACTED_PLACEHOLDER and "qwzrtvkplm" in (repo / "tests" / "test_client.py").read_text()


async def test_an_identical_replace_says_whether_the_change_is_already_made(sessionmaker: SM, repo: Path) -> None:
    """Live G1 (BUG-272): the coder sent the same 2 KB replace_text with old == new four times; its fix was already in
    the file from an earlier write, and the bare "identical" refusal never said so until stagnation stopped the step."""
    (repo / "src" / "util.py").write_text("def f():\n    return 1\n")
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["src/util.py"]))
    done = await h.call("replace_text", path="src/util.py", old="    return 1\n", new="    return 1\n")
    assert done.error_code == E.NO_CHANGE and done.data["present"] is True and done.data["repeated"] is True
    assert "already contains this text" in done.output and "complete_step" in done.output
    missing = await h.call("replace_text", path="src/util.py", old="return 2", new="return 2")
    assert missing.error_code == E.NO_CHANGE and missing.data["present"] is False and "copy the current lines" in missing.output


async def test_read_output_budget_sets_truncated(sessionmaker: SM, repo: Path) -> None:
    (repo / "big.txt").write_text("".join(f"line {i:05d} " + "x" * 60 + "\n" for i in range(2000)))
    h = await make_harness(sessionmaker, repo)
    res = await h.call("read_file", path="big.txt")
    limit = h.engine.output_limit
    assert res.ok and res.truncated and len(res.output) <= limit + 400 and "read_range(start, end)" in res.output


async def test_path_traversal_absolute_and_symlink_escape_are_refused(sessionmaker: SM, repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("top secret outside\n")
    os.symlink(outside, repo / "escape.txt")
    os.symlink(tmp_path, repo / "escape_dir")
    os.symlink(repo / ".env", repo / "env_link")
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["escape.txt", "escape_dir/**", "env_link"]))
    assert (await h.call("read_file", path="../outside.txt")).error_code == E.PATH_INVALID
    assert (await h.call("read_file", path="src/../../outside.txt")).error_code == E.PATH_INVALID
    assert (await h.call("read_file", path="/etc/passwd")).error_code == E.PATH_INVALID
    assert (await h.call("read_file", path="escape.txt")).error_code == E.PATH_OUTSIDE_WORKSPACE
    assert (await h.call("read_file", path="escape_dir/outside.txt")).error_code == E.PATH_OUTSIDE_WORKSPACE
    assert (await h.call("read_file", path="env_link")).error_code == E.PATH_FORBIDDEN
    assert (await h.call("read_file", path=".env")).error_code == E.PATH_FORBIDDEN
    assert (await h.call("read_file", path=".git/config")).error_code == E.PATH_FORBIDDEN
    assert (await h.call("list_files", path="escape_dir")).error_code == E.PATH_OUTSIDE_WORKSPACE
    # writes through symlinks never happen, even when the scope lists the path
    res = await h.call("write_file", path="escape.txt", content="pwned\n")
    assert res.error_code in (E.PATH_OUTSIDE_WORKSPACE, E.SYMLINK_REFUSED)
    res = await h.call("write_file", path="escape_dir/new.txt", content="pwned\n")
    assert res.error_code == E.PATH_OUTSIDE_WORKSPACE
    assert outside.read_text() == "top secret outside\n" and not (tmp_path / "new.txt").exists()
    rows = await tool_calls(sessionmaker, h.step.id)
    assert all(r.status == "refused" for r in rows)


async def test_symlinked_directory_inside_workspace_cannot_bypass_scope(sessionmaker: SM, repo: Path) -> None:
    os.symlink(repo / "src", repo / "alias")
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["alias/**"], allowed_new_paths=["alias/**"]))
    res = await h.call("write_file", path="alias/util.py", content="X = 2\n")
    assert res.error_code == E.SYMLINK_REFUSED
    assert (repo / "src" / "util.py").read_text() == "X = 1\nX = 1\n"


async def test_find_text_literal_regex_and_secrets(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo)
    res = await h.call("find_text", pattern="def ")
    assert res.ok and "src/calc.py:1: def mul(a, b):" in res.output and "src/calc.py:5: def div(a, b):" in res.output
    res = await h.call("find_text", pattern=r"def \w+\(a", regex=True, path="src", glob="*.py")
    assert res.data["matches"] == 2
    res = await h.call("find_text", pattern="DEF MUL", ignore_case=True, path="src/calc.py")
    assert res.data["matches"] == 1
    res = await h.call("find_text", pattern="supersecretvalue")
    assert res.ok and res.data["matches"] == 0  # .env is never searched
    res = await h.call("find_text", pattern="password")
    assert "config.ini:2" in res.output and "hunter2hunter2" not in res.output and REDACTED in res.output
    assert (await h.call("find_text", pattern="(a+)+$", regex=True)).error_code == E.PATTERN_INVALID
    assert (await h.call("find_text", pattern="([", regex=True)).error_code == E.PATTERN_INVALID
    res = await h.call("find_text", pattern="X = 1", max_results=1)
    assert res.truncated and "result limit" in res.output


async def test_search_repo_and_symbol_filter_hidden_paths(sessionmaker: SM, repo: Path) -> None:
    provider = ScriptedRepo(
        hits=[
            RepoHit(path="src/calc.py", start_line=1, end_line=2, score=0.9, snippet="def mul(a, b):\n    return a * b"),
            RepoHit(path=".env", start_line=1, end_line=1, score=0.8, snippet="API_TOKEN=supersecretvalue123"),
            RepoHit(path="../etc/passwd", score=0.7, snippet="root:x:0:0"),
        ],
        symbols=[RepoHit(path="src/calc.py", start_line=5, end_line=6, score=1.0, snippet="def div(a, b):")],
    )
    h = await make_harness(sessionmaker, repo, repo_provider=provider)
    res = await h.call("search_repo", query="multiply numbers", k=5)
    assert res.ok and "src/calc.py:1-2" in res.output and ".env" not in res.output and "passwd" not in res.output
    assert res.data["hits"] == 1 and "supersecret" not in res.output
    res = await h.call("search_symbol", name="div")
    assert res.ok and "src/calc.py:5-6" in res.output
    provider.fail = True
    res = await h.call("search_repo", query="anything")
    assert not res.ok and res.error_code == E.REPO_UNAVAILABLE


# ------------------------------------------------------------------------------------------------ 17.2 git read
async def test_git_status_and_diff_are_read_only(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo)
    head = git(repo, "rev-parse", "HEAD")
    assert (await h.call("git_status")).output.startswith("working tree clean")
    (repo / "app.py").write_text("def add(a, b):\n    return b + a\n")
    (repo / "notes.md").write_text("n\n")
    (repo / ".env").write_text("API_TOKEN=changedsecretvalue\n")
    res = await h.call("git_status")
    assert " M app.py" in res.output and "?? notes.md" in res.output and res.data["count"] == 3
    res = await h.call("git_diff")
    assert "-    return a + b" in res.output and "+    return b + a" in res.output
    assert "changedsecretvalue" not in res.output and "protected file(s) omitted" in res.output
    res = await h.call("git_diff", paths=["app.py"])
    assert res.ok and "app.py" in res.output
    assert (await h.call("git_diff", paths=[".env"])).error_code == E.PATH_FORBIDDEN
    assert (await h.call("git_diff", paths=["../x"])).error_code == E.PATH_INVALID
    assert git(repo, "rev-parse", "HEAD") == head
    assert {t.value for t in ToolName if t.value.startswith("git_")} == {"git_status", "git_diff"}


# ------------------------------------------------------------------------------------------------ 17.3 file writes
async def test_write_file_create_modify_scope_and_events(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo, contract=scope(**IMPLEMENT_SCOPE))
    os.chmod(repo / "src" / "calc.py", 0o755)
    res = await h.call("write_file", path="src/calc.py", content="def mul(a, b):\n    return a * b * 1\n")
    assert res.ok and res.mutated_paths == ["src/calc.py"] and res.data["operation"] == "modify"
    assert (repo / "src" / "calc.py").read_text() == "def mul(a, b):\n    return a * b * 1\n"
    assert os.stat(repo / "src" / "calc.py").st_mode & 0o777 == 0o755  # mode preserved
    res = await h.call("write_file", path="src/new_feature.py", content="VALUE = 3\n")
    assert res.ok and res.data["operation"] == "create"
    res = await h.call("write_file", path="docs/guide/intro.md", content="# intro\n")
    assert res.ok and (repo / "docs" / "guide" / "intro.md").exists()
    # same content -> no write, refused as a no-op repeat that keeps the latest failure (BUG-253)
    res = await h.call("write_file", path="src/new_feature.py", content="VALUE = 3\n")
    assert not res.ok and res.error_code == E.NO_CHANGE and res.mutated_paths == []
    assert res.data["changed"] is False and res.data["repeated"] is True and "changes nothing" in res.output
    # out of scope: modify of a non-target, create outside allowed_new_paths, forbidden secret file
    for path, op in (("src/util.py", "modify"), ("src/other.py", "create"), (".env", "modify")):
        res = await h.call("write_file", path=path, content="nope\n")
        assert not res.ok, path
        assert res.error_code in (E.SCOPE_VIOLATION, E.PATH_FORBIDDEN), (path, op)
    assert (repo / "src" / "util.py").read_text() == "X = 1\nX = 1\n" and not (repo / "src" / "other.py").exists()
    assert not list(repo.rglob("*.hermclaw-tmp"))
    changed = await events_for(sessionmaker, h.step.id, EventType.FILE_CHANGED)
    assert [e.payload["changes"][0]["path"] for e in changed] == ["src/calc.py", "src/new_feature.py", "docs/guide/intro.md"]
    violations = await events_for(sessionmaker, h.step.id, EventType.SCOPE_VIOLATION)
    assert {e.payload["violations"][0]["path"] for e in violations} == {"src/util.py", "src/other.py"}


async def test_write_file_append_writes_large_files_in_parts(sessionmaker: SM, repo: Path) -> None:
    """BUG-223: one model answer has an output token limit, so large files are written part by part."""
    h = await make_harness(sessionmaker, repo, contract=scope(**IMPLEMENT_SCOPE))
    res = await h.call("write_file", path="src/new_feature.py", content="A = 1", append=True)
    assert res.ok and res.data["operation"] == "create" and "created" in res.output
    res = await h.call("write_file", path="src/new_feature.py", content="B = 2\n", append=True)
    assert res.ok and res.data["operation"] == "modify" and "appended to" in res.output and "2 lines" in res.output
    assert (repo / "src" / "new_feature.py").read_text() == "A = 1\nB = 2\n"  # a missing final newline is added first
    res = await h.call("write_file", path="src/util.py", content="Y = 2\n", append=True)  # scope still applies
    assert not res.ok and res.error_code == E.SCOPE_VIOLATION


async def test_write_tools_require_a_scope_contract(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo, contract=None)
    for tool, args in (
        ("write_file", {"path": "app.py", "content": "x\n"}),
        ("replace_text", {"path": "app.py", "old": "a + b", "new": "b + a"}),
        ("apply_patch", {"patch": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"}),
    ):
        res = await h.call(tool, **args)
        assert res.error_code == E.SCOPE_MISSING, tool
    assert git(repo, "status", "--porcelain") == ""


async def test_write_tools_not_allowed_for_read_only_steps(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo, contract=scope(**IMPLEMENT_SCOPE), permissions=ToolPermissions.for_step(kind="review"))
    res = await h.call("write_file", path="src/calc.py", content="x\n")
    assert res.error_code == E.TOOL_NOT_ALLOWED and "read_file" in res.output
    assert "write_file" not in {t["name"] for t in h.engine.tool_catalog()}


async def test_replace_text_exact_unique_and_count(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["src/calc.py", "src/util.py", "config.ini"]))
    res = await h.call("replace_text", path="src/calc.py", old="return a / b", new="return a / b if b else 0")
    assert res.ok and res.data["first_line"] == 6 and "if b else 0" in (repo / "src" / "calc.py").read_text()
    res = await h.call("replace_text", path="src/util.py", old="X = 1", new="X = 2")
    assert res.error_code == E.TEXT_COUNT_MISMATCH and res.data["occurrences"] == 2 and res.data["lines"] == [1, 2]
    res = await h.call("replace_text", path="src/util.py", old="X = 1", new="X = 2", count=2)
    assert res.ok and (repo / "src" / "util.py").read_text() == "X = 2\nX = 2\n"
    res = await h.call("replace_text", path="src/calc.py", old="return a*b", new="x")
    assert res.error_code == E.TEXT_NOT_FOUND
    res = await h.call("replace_text", path="src/calc.py", old="  return a * b  ", new="x")
    assert res.error_code == E.TEXT_NOT_FOUND and "whitespace" in res.output
    assert (await h.call("replace_text", path="src/calc.py", old="a", new="a")).error_code == E.NO_CHANGE
    assert (await h.call("replace_text", path="app.py", old="a + b", new="b + a")).error_code == E.SCOPE_VIOLATION
    # secrets are masked in output and the marker cannot be written back
    shown = await h.call("read_file", path="config.ini")
    assert "hunter2hunter2" not in shown.output and REDACTED in shown.output
    res = await h.call("replace_text", path="config.ini", old="host = localhost", new=f"host = db\npassword = {REDACTED}")
    assert res.error_code == E.REDACTED_PLACEHOLDER
    res = await h.call("replace_text", path="config.ini", old="host = localhost", new="host = db")
    assert res.ok and "password = hunter2hunter2" in (repo / "config.ini").read_text()


async def test_recall_finds_active_knowledge_of_this_repository(sessionmaker: SM, repo: Path) -> None:
    """D-019: the coder looks up Hermclaw's knowledge – active entries, global or of this repository, nearest first;
    without a knowledge store the tool is not offered."""
    from sqlalchemy import delete

    from hermclaw.core.config import MemoryPolicy
    from hermclaw.memory.store import MemoryStore
    from hermclaw.persistence.models import MemoryEntry
    from tests.integration.test_repo_intelligence_support import HashingEmbedder

    async with sessionmaker() as s:
        await s.execute(delete(MemoryEntry))
        await s.commit()
    store = MemoryStore(sessionmaker, HashingEmbedder(), MemoryPolicy(min_score=0.2))
    lesson = await store.add(
        kind="lesson", title="ruff F401 in tests", content="remove every unused import ruff names, tests too", status="active"
    )
    await store.add(kind="lesson", title="ruff F401 elsewhere", content="ruff unused import", status="active", scope="repo:other")
    await store.add(kind="lesson", title="ruff F401 unproven", content="ruff unused import", status="candidate")
    h = await make_harness(sessionmaker, repo, memory=store)
    assert ToolName.recall in h.engine.available_tools()
    res = await h.call("recall", query="ruff F401 unused import")
    assert res.ok and "[lesson] ruff F401 in tests" in res.output and "elsewhere" not in res.output and "unproven" not in res.output
    assert res.data["ids"] == [str(lesson)]
    none = await h.call("recall", query="kubernetes helm chart")
    assert none.ok and none.data["hits"] == 0 and "no knowledge entry matches" in none.output
    plain = await make_harness(sessionmaker, repo)
    assert ToolName.recall not in plain.engine.available_tools()


async def test_replace_text_handles_crlf_files(sessionmaker: SM, repo: Path) -> None:
    (repo / "src" / "calc.py").write_bytes(b"def mul(a, b):\r\n    return a * b\r\n")
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["src/calc.py"]))
    res = await h.call("replace_text", path="src/calc.py", old="def mul(a, b):\n    return a * b", new="def mul(a, b):\n    return b * a")
    assert res.ok
    assert (repo / "src" / "calc.py").read_bytes() == b"def mul(a, b):\r\n    return b * a\r\n"


async def test_spaces_at_line_ends_do_not_decide_a_match(sessionmaker: SM, repo: Path) -> None:
    """Live tournament GLM A3 (BUG-301): the file's blank lines held the indentation spaces, the coder's 'old' had
    empty lines, and four replaces failed with "indentation must match" while read_range showed no difference."""
    body = "def f(rows):\n    for r in rows:\n        a = r[0]\n        \n        if not a:  \n            continue\n"
    (repo / "src" / "util.py").write_text(body)
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["src/util.py"]))
    old = "        a = r[0]\n\n        if not a:\n            continue"
    res = await h.call(
        "replace_text", path="src/util.py", old=old, new="        a = r[0].strip()\n\n        if not a:\n            continue"
    )
    assert res.ok and "spaces at line ends were ignored" in res.output, res.output
    assert (repo / "src" / "util.py").read_text() == (
        "def f(rows):\n    for r in rows:\n        a = r[0].strip()\n\n        if not a:\n            continue\n"
    )
    # the last line is still compared exactly, and a loose match must be unique like an exact one
    (repo / "src" / "util.py").write_text("x = 1  \ny = 2\nx = 1 \ny = 2\n")
    assert (await h.call("replace_text", path="src/util.py", old="x = 1\ny = 2", new="z")).error_code == E.TEXT_COUNT_MISMATCH
    assert (await h.call("replace_text", path="src/util.py", old="y = 2  ", new="z")).error_code == E.TEXT_NOT_FOUND
    res = await h.call("replace_text", path="src/util.py", old="x = 1\ny = 2", new="z", count=2)
    assert res.ok and (repo / "src" / "util.py").read_text() == "z\nz\n"


# ------------------------------------------------------------------------------------------------ 17.4 patch
PATCH_OK = (
    "diff --git a/src/calc.py b/src/calc.py\n"
    "--- a/src/calc.py\n"
    "+++ b/src/calc.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def mul(a, b):\n"
    "-    return a * b\n"
    "+    return b * a\n"
    " \n"
    "diff --git a/src/new_mod.py b/src/new_mod.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/new_mod.py\n"
    "@@ -0,0 +1 @@\n"
    "+NEW = True\n"
)


async def test_apply_patch_applies_in_scope_without_committing(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo, contract=scope(**IMPLEMENT_SCOPE))
    head = git(repo, "rev-parse", "HEAD")
    res = await h.call("apply_patch", patch=PATCH_OK)
    assert res.ok, res.output
    assert res.mutated_paths == ["src/calc.py", "src/new_mod.py"]
    assert "return b * a" in (repo / "src" / "calc.py").read_text() and (repo / "src" / "new_mod.py").read_text() == "NEW = True\n"
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "diff", "--cached") == ""  # worktree only, never the index
    ev = await events_for(sessionmaker, h.step.id, EventType.FILE_CHANGED)
    assert {c["path"]: c["operation"] for c in ev[-1].payload["changes"]} == {"src/calc.py": "modify", "src/new_mod.py": "create"}


async def test_apply_patch_plain_unified_diff_p0(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo, contract=scope(**IMPLEMENT_SCOPE))
    patch = "--- app.py\n+++ app.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a + b + 0\n"
    res = await h.call("apply_patch", patch=patch)
    assert res.ok, res.output
    assert "a + b + 0" in (repo / "app.py").read_text()


async def test_apply_patch_scope_is_checked_for_every_file_before_applying(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo, contract=scope(**IMPLEMENT_SCOPE))
    patch = PATCH_OK + (
        "diff --git a/src/util.py b/src/util.py\n--- a/src/util.py\n+++ b/src/util.py\n@@ -1,2 +1,2 @@\n-X = 1\n+X = 9\n X = 1\n"
    )
    res = await h.call("apply_patch", patch=patch)
    assert res.error_code == E.SCOPE_VIOLATION and "src/util.py" in res.output
    assert git(repo, "status", "--porcelain") == ""  # nothing applied (all-or-nothing)
    # deletion requires the delete operation
    delete = "diff --git a/src/calc.py b/src/calc.py\ndeleted file mode 100644\n--- a/src/calc.py\n+++ /dev/null\n@@ -1,6 +0,0 @@\n"
    delete += "".join("-" + ln + "\n" for ln in (repo / "src" / "calc.py").read_text().splitlines())
    res = await h.call("apply_patch", patch=delete)
    assert res.error_code == E.SCOPE_VIOLATION and (repo / "src" / "calc.py").exists()
    # renames need delete (source) + create (destination)
    rename = "diff --git a/src/calc.py b/src/new_calc.py\nsimilarity index 100%\nrename from src/calc.py\nrename to src/new_calc.py\n"
    assert (await h.call("apply_patch", patch=rename)).error_code == E.SCOPE_VIOLATION
    h2 = await make_harness(
        sessionmaker,
        repo,
        contract=scope(target_paths=["src/calc.py"], allowed_new_paths=["src/new_*.py"], allowed_operations=["create", "modify", "delete"]),
    )
    res = await h2.call("apply_patch", patch=rename)
    assert res.ok, res.output
    assert (repo / "src" / "new_calc.py").exists() and not (repo / "src" / "calc.py").exists()
    assert set(res.mutated_paths) == {"src/calc.py", "src/new_calc.py"}


async def test_apply_patch_refusals(sessionmaker: SM, repo: Path) -> None:
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["src/calc.py", "config.ini"], allowed_new_paths=["**"]))
    assert (await h.call("apply_patch", patch="this is not a diff")).error_code == E.PATCH_INVALID
    stale = "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,2 @@\n def mul(a, b):\n-    return a ** b\n+    return 0\n"
    res = await h.call("apply_patch", patch=stale)
    assert res.error_code == E.PATCH_FAILED and "context lines must match" in res.output
    symlink = "diff --git a/src/link b/src/link\nnew file mode 120000\n--- /dev/null\n+++ b/src/link\n@@ -0,0 +1 @@\n+/etc/passwd\n"
    assert (await h.call("apply_patch", patch=symlink)).error_code == E.PATCH_SYMLINK_REFUSED
    traversal = "--- a/../evil.py\n+++ b/../evil.py\n@@ -0,0 +1 @@\n+x\n"
    assert (await h.call("apply_patch", patch=traversal)).error_code == E.PATH_INVALID
    git_internal = "--- /dev/null\n+++ b/.git/hooks/pre-commit\n@@ -0,0 +1 @@\n+echo pwned\n"
    assert (await h.call("apply_patch", patch=git_internal)).error_code == E.PATH_FORBIDDEN
    secret = f"--- a/config.ini\n+++ b/config.ini\n@@ -1,3 +1,3 @@\n [db]\n-password = hunter2hunter2\n+password = {REDACTED}\n host = localhost\n"
    assert (await h.call("apply_patch", patch=secret)).error_code == E.REDACTED_PLACEHOLDER
    assert git(repo, "status", "--porcelain") == "" and not (repo.parent / "evil.py").exists()


async def test_contract_forbidden_paths_and_removing_own_new_files(sessionmaker: SM, repo: Path) -> None:
    contract = scope(target_paths=["src/**"], allowed_new_paths=["src/**"], forbidden_paths=["src/util.py"])
    h = await make_harness(sessionmaker, repo, contract=contract)
    res = await h.call("write_file", path="src/util.py", content="X = 3\n")
    assert res.error_code == E.SCOPE_VIOLATION and "forbidden" in res.output
    assert (await h.call("write_file", path="src/tmp_helper.py", content="T = 1\n")).ok
    # removing a file this step created is not a deletion of base content (no 'delete' operation needed)
    patch = "diff --git a/src/tmp_helper.py b/src/tmp_helper.py\ndeleted file mode 100644\n--- a/src/tmp_helper.py\n+++ /dev/null\n"
    patch += "@@ -1 +0,0 @@\n-T = 1\n"
    res = await h.call("apply_patch", patch=patch)
    assert res.ok, res.output
    assert not (repo / "src" / "tmp_helper.py").exists()
    # deleting base content still requires the delete operation
    patch = "diff --git a/src/util.py b/src/util.py\ndeleted file mode 100644\n--- a/src/util.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-X = 1\n-X = 1\n"
    assert (await h.call("apply_patch", patch=patch)).error_code == E.SCOPE_VIOLATION


async def test_a_flagged_secret_stays_until_a_placeholder_replaces_it(sessionmaker: SM, repo: Path) -> None:
    """Live M3 (BUG-279): the verifier flagged a token in a test the coder wrote and bearer tokens in the README; the
    coder re-wrote the masked token line unchanged ("masked secret replaced", nothing changed) and swapped one README
    placeholder for another while four flagged lines stayed. An unchanged masked replace is refused; complete_step
    rescans the flagged files and names the lines that still carry a secret until placeholders replace them."""
    (repo / "tests" / "test_api.py").write_text("def call(token='zq81vkpw3mxt5'):\n    return token\n")
    h = await make_harness(sessionmaker, repo, contract=scope(target_paths=["tests/test_api.py"]))
    h.engine.expect_secret_rechecks(["tests/test_api.py"])
    masked = "def call(token='" + REDACTED + "'):"
    same = await h.call("replace_text", path="tests/test_api.py", old=masked, new=masked)
    assert same.error_code == E.NO_CHANGE and "keeps its real value" in same.output and "placeholder" in same.output
    around = await h.call("replace_text", path="tests/test_api.py", old=masked + "\n    return token", new=masked + "\n    return token\n")
    assert around.ok and "kept their real value" in around.output and "zq81vkpw3mxt5" in (repo / "tests" / "test_api.py").read_text()
    left = await h.call("complete_step", summary="secrets removed")
    assert left.error_code == E.CHECK_STILL_FAILING and "tests/test_api.py:1" in left.output and "zq81vkpw3mxt5" not in left.output
    fixed = await h.call("replace_text", path="tests/test_api.py", old=masked, new="def call(token='dummy-token'):")
    assert fixed.ok and "replaced by the new text" in fixed.output
    assert (await h.call("complete_step", summary="secrets removed")).ok
