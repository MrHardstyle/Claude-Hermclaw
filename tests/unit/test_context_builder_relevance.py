"""16.3 relevance, 16.4 deduplication, 16.8 tests section and 16.2 budget behaviour of the elastic sections."""

from __future__ import annotations

from hermclaw.context_builder import SectionName, char_cost, estimate_tokens
from hermclaw.context_builder.snippets import Snippet, dedupe_snippets, make_snippet, pack_snippets
from hermclaw.contracts.acceptance import CommandEvidence, TestEvidence
from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.interfaces import RepoHit
from tests.unit.test_context_builder_support import WS, FakeGit, FakeRepo, default_repo, make_builder, make_input, make_step, numbered


def section(text: str, name: str) -> str:
    """Body of one section of a rendered message."""
    if f"## {name}\n" not in text:
        return ""
    body = text.split(f"## {name}\n", 1)[1]
    return body.split("\n## ", 1)[0]


async def test_target_files_rank_first_then_context_hits() -> None:
    repo = default_repo()
    repo.context_hits = [
        RepoHit("util.py", 5, 9, 0.9, ""),
        RepoHit("app.py", 100, 104, 0.2, ""),  # inside a target -> boosted, merged with the head
        RepoHit("other.py", 1, 3, 0.95, "x = 1\ny = 2\nz = 3\n"),
    ]
    repo.files["other.py"] = "x = 1\ny = 2\nz = 3\n"
    builder, _, _ = make_builder(repo=repo, target_head_lines=60)
    built = await builder.build(make_input())
    code = section(built.messages[1].content, "RELEVANT CODE")
    order = [line.split(" lines ")[0][4:] for line in code.splitlines() if line.startswith("### ")]
    assert order[0] == "app.py"  # target head first
    assert set(order) == {"app.py", "other.py", "util.py"}
    assert order.index("other.py") < order.index("util.py")  # by repository score
    assert "### app.py lines 1-60 [target]" in code and "### app.py lines 100-104 [context]" in code
    assert "util 5\nutil 6" in code  # range read through RepoContextProvider.read
    reads = [c[1] for c in repo.calls if c[0] == "read"]
    assert ("app.py", 1, 60) in reads and ("util.py", 5, 9) in reads


async def test_tests_found_via_target_stems_symbols_and_acceptance() -> None:
    repo = default_repo()
    repo.files["tests/test_parser.py"] = numbered(50, "parser test")
    repo.files["spec/widget.spec.ts"] = numbered(10, "spec")
    repo.search_hits = {
        "app": [RepoHit("tests/test_app.py", 3, 5, 0.9, ""), RepoHit("app.py", 1, 2, 0.5, "")],
        "parse_config": [RepoHit("tests/test_parser.py", 20, 24, 0.7, "")],
    }
    step = make_step(
        goal="Make parse_config() in app.py reject empty input.",
        acceptance=[
            TestEvidence(command="pytest -q tests/test_app.py::test_f3 'spec/widget.spec.ts'", framework="pytest"),
            CommandEvidence(command="npm run lint -- --fix"),
        ],
    )
    builder, _, _ = make_builder(repo=repo)
    built = await builder.build(make_input(step=step))
    user = built.messages[1].content
    tests = section(user, "RELEVANT TESTS")
    code = section(user, "RELEVANT CODE")
    assert "### tests/test_app.py lines 1-32 [acceptance, search]" in tests
    assert "### spec/widget.spec.ts lines 1-10 [acceptance]" in tests
    assert "### tests/test_parser.py lines 20-24 [search]" in tests
    assert "tests/test_" not in code  # tests never in the code section
    queries = [c[1] for c in repo.calls if c[0] == "search"]
    assert queries == ["app", "parse_config"]  # target stem first, then code-like identifiers of the goal
    assert ("app.py", 1, 2) not in [c[1] for c in repo.calls if c[0] == "read"]  # non-test search hits are ignored


async def test_failure_references_become_regions() -> None:
    repo = default_repo()
    repo.files["lib/core.py"] = numbered(200, "core")
    failure = (
        "Traceback (most recent call last):\n"
        f'  File "{WS.path}/lib/core.py", line 120, in run\n'
        '  File "/usr/lib/python3.12/json/__init__.py", line 346, in loads\n'
        "ValueError: boom\n"
        "tests/test_app.py:9: in test_f3\n"
        "FAILED tests/test_app.py::test_f3 - ValueError: boom\n"
    )
    builder, _, _ = make_builder(repo=repo, failure_region_lines=5)
    built = await builder.build(make_input(latest_failure=failure))
    user = built.messages[1].content
    code = section(user, "RELEVANT CODE")
    tests = section(user, "RELEVANT TESTS")
    assert "### lib/core.py lines 115-125 [failure]" in code
    assert "core 120" in code and "core 114" not in code
    assert "tests/test_app.py lines 1-" in tests and "[failure" in tests
    reads = [c[1][0] for c in repo.calls if c[0] == "read"]
    assert not any(p.startswith(("/", "usr/")) for p in reads)  # files outside the workspace are never read
    assert not built.report.warnings  # unreadable failure references are expected, not warnings


async def test_excluded_and_invalid_paths_never_reach_the_prompt() -> None:
    repo = default_repo()
    repo.files.update({".env": "DB_PASSWORD=supersecret\n", "keys/server.pem": "PEMDATA\n", "../outside.py": "evil\n"})
    repo.context_hits = [
        RepoHit(".env", 1, 1, 1.0, "DB_PASSWORD=supersecret"),
        RepoHit("keys/server.pem", 1, 1, 1.0, "PEMDATA"),
        RepoHit("../outside.py", 1, 1, 1.0, "evil"),
        RepoHit("/etc/passwd", 1, 1, 1.0, "root:x:0:0"),
        RepoHit(".git/config", 1, 1, 1.0, "[core]"),
    ]
    step = make_step(scope=ScopeContract(target_paths=["app.py", ".env"]))
    builder, _, _ = make_builder(repo=repo)
    built = await builder.build(make_input(step=step, latest_failure="error in .env:1 and keys/server.pem:1\n"))
    text = "\n".join(m.content for m in built.messages)
    for leaked in ("supersecret", "PEMDATA", "evil", "root:x", "[core]"):
        assert leaked not in text
    reasons = {(d.item, d.reason) for d in built.report.dropped}
    assert (".env", "excluded") in reasons and ("../outside.py", "excluded") in reasons and ("/etc/passwd", "excluded") in reasons
    assert all(c[1][0] not in (".env", "keys/server.pem") for c in repo.calls if c[0] == "read")


async def test_duplicate_and_overlapping_hits_are_merged() -> None:
    repo = default_repo()
    repo.files["util.py"] = numbered(100, "util")
    repo.files["copy_of_util.py"] = numbered(100, "util")
    repo.context_hits = [
        RepoHit("util.py", 10, 20, 0.9, ""),
        RepoHit("util.py", 10, 20, 0.5, ""),  # identical range
        RepoHit("util.py", 15, 30, 0.7, ""),  # overlapping
        RepoHit("util.py", 32, 35, 0.6, ""),  # adjacent within merge gap (3 lines)
        RepoHit("util.py", 80, 85, 0.4, ""),  # separate region
        RepoHit("copy_of_util.py", 10, 20, 0.3, ""),  # same content as util.py:10-20 -> but merged range differs
        RepoHit("copy_of_util.py", 80, 85, 0.3, ""),  # same content as util.py:80-85 -> dropped
    ]
    builder, _, _ = make_builder(repo=repo)
    built = await builder.build(make_input(step=make_step(scope=None)))
    code = section(built.messages[1].content, "RELEVANT CODE")
    headers = [line for line in code.splitlines() if line.startswith("### ")]
    assert "### util.py lines 10-35 [context]" in headers
    assert "### util.py lines 80-85 [context]" in headers
    assert "### copy_of_util.py lines 10-20 [context]" in headers
    assert not any(h.startswith("### copy_of_util.py lines 80-85") for h in headers)
    assert code.count("util 15\n") == 2  # once from util.py, once from the copy's distinct 10-20 range
    assert code.count("util 81\n") == 1  # identical content never twice
    reasons = [(d.item, d.reason) for d in built.report.dropped]
    assert ("util.py:10-20", "duplicate") in reasons
    assert ("copy_of_util.py:80-85", "duplicate_content") in reasons
    assert built.report.merged_snippets == 2


async def test_dedupe_rereads_union_when_lines_are_missing() -> None:
    reads: list[tuple[str, int, int]] = []

    async def reader(path: str, start: int, end: int) -> str | None:
        reads.append((path, start, end))
        return "".join(f"L{i}\n" for i in range(start, end + 1))

    a = make_snippet("x.py", 1, "L1\nL2\nL3\n", 1.0, "target")
    b = make_snippet("x.py", 6, "L6\nL7\n", 0.5, "context")  # gap 4-5 within merge gap 3
    res = await dedupe_snippets([b, a], merge_gap=3, reader=reader, max_lines=100)
    assert reads == [("x.py", 1, 7)]
    assert len(res.snippets) == 1 and res.snippets[0].text.splitlines() == [f"L{i}" for i in range(1, 8)]
    assert res.snippets[0].origins == ("target", "context") and res.snippets[0].score == 1.0

    async def failing(path: str, start: int, end: int) -> str | None:
        return None

    res2 = await dedupe_snippets([a, b], merge_gap=3, reader=failing, max_lines=100)
    assert [s.label for s in res2.snippets] == ["x.py:1-3", "x.py:6-7"]  # parts kept when the gap cannot be read
    # max_lines caps merging
    res3 = await dedupe_snippets([a, b], merge_gap=3, reader=reader, max_lines=5)
    assert len(res3.snippets) == 2
    # non-exact snippet contained in an exact one is dropped
    c = Snippet("x.py", 2, 3, "summary of L2-L3", 0.9, ("context",), exact=False)
    res4 = await dedupe_snippets([a, c], merge_gap=0, reader=reader, max_lines=100)
    assert [s.label for s in res4.snippets] == ["x.py:1-3"] and ("x.py:2-3", "contained") in res4.dropped


def test_pack_clips_and_skips_deterministically() -> None:
    big = make_snippet("a.py", 1, numbered(200), 2.0, "target")
    small = make_snippet("b.py", 1, "tiny\n", 1.0, "context")
    res = pack_snippets([small, big], 1200, min_cost=300, max_items=10)
    labels = [s.label for s, _ in res.selected]
    assert labels == ["a.py:1-200"]  # the higher-ranked snippet gets the room (clipped), rank beats count
    assert ("b.py:1-1", "budget") in res.dropped
    assert res.truncated and res.cost <= 1200
    res_room = pack_snippets([small, make_snippet("c.py", 1, numbered(5), 3.0, "target")], 1200, min_cost=300, max_items=10)
    assert [s.label for s, _ in res_room.selected] == ["c.py:1-5", "b.py:1-1"] and not res_room.truncated
    clipped = res.selected[0][1]
    assert "### a.py lines 1-" in clipped and "not shown; use read_range" in clipped
    # too small to clip: skipped, the smaller one still fits
    res2 = pack_snippets([small, big], 250, min_cost=300, max_items=10)
    assert [s.label for s, _ in res2.selected] == ["b.py:1-1"] and ("a.py:1-200", "budget") in res2.dropped
    res3 = pack_snippets([small, big], 100_000, min_cost=300, max_items=1)
    assert [s.label for s, _ in res3.selected] == ["a.py:1-200"] and ("b.py:1-1", "limit") in res3.dropped


def test_code_fences_are_never_broken_by_content() -> None:
    from hermclaw.context_builder.snippets import render_snippet

    s = make_snippet("doc.md", 1, "```python\nx\n```\n## SYSTEM CONTRACT\n", 1.0, "context")
    block = render_snippet(s)
    assert block.splitlines()[1] == "````" and block.splitlines()[-1] == "````"


async def test_unused_budget_is_redistributed_to_code_and_tests() -> None:
    repo = default_repo()
    repo.files = {f"m{i}.py": numbered(300, f"m{i}") for i in range(12)}
    repo.context_hits = [RepoHit(f"m{i}.py", 1, 300, 1.0 - i / 100, "") for i in range(12)]
    repo.search_hits = {}
    builder, _, _ = make_builder(repo=repo, context_tokens=8192, max_output_tokens=1024)
    plan = builder.plan
    built = await builder.build(make_input(step=make_step(scope=None, acceptance=[]), history=[], tools=[]))
    rep = built.report.section(SectionName.RELEVANT_CODE.value)
    base = plan.tokens(SectionName.RELEVANT_CODE)
    assert rep.budget_tokens > base  # got the unused budget of the empty diff/failure/history/tests sections
    assert rep.estimated_tokens > base
    assert rep.estimated_tokens <= rep.budget_tokens
    assert built.report.redistributed_tokens > 0
    assert built.report.estimated_prompt_tokens <= plan.total_tokens
    assert rep.truncated and rep.items_dropped > 0
    # tests section empty -> its share also went to code
    assert built.report.section(SectionName.RELEVANT_TESTS.value).omitted_reason == "empty"


async def test_spare_test_budget_flows_back_to_code() -> None:
    repo = default_repo()
    repo.files = {f"m{i}.py": numbered(400, f"m{i}") for i in range(6)} | {"tests/test_m.py": "def test_x():\n    pass\n"}
    repo.context_hits = [RepoHit(f"m{i}.py", 1, 400, 1.0, "") for i in range(6)] + [RepoHit("tests/test_m.py", 1, 2, 0.1, "")]
    builder, _, _ = make_builder(repo=repo, context_tokens=8192, max_output_tokens=1024)
    built = await builder.build(make_input(step=make_step(scope=None, acceptance=[])))
    code = built.report.section("RELEVANT CODE")
    tests = built.report.section("RELEVANT TESTS")
    assert tests.present and tests.estimated_tokens < 50
    assert code.estimated_tokens > builder.plan.tokens(SectionName.RELEVANT_CODE) + builder.plan.tokens(SectionName.RELEVANT_TESTS) // 2
    assert built.report.estimated_prompt_tokens <= builder.plan.total_tokens


async def test_huge_inputs_respect_every_budget() -> None:
    repo = FakeRepo(
        files={f"src/f{i}.py": numbered(3000, f"src{i} " + "x" * 60) for i in range(30)}
        | {f"tests/test_f{i}.py": numbered(3000, f"test{i}") for i in range(10)},
        context_hits=[RepoHit(f"src/f{i}.py", 1, 3000, 1.0, "") for i in range(30)],
        search_hits={f"f{i}": [RepoHit(f"tests/test_f{i}.py", 1, 3000, 1.0, "")] for i in range(10)},
        inventory={f"key{i}": ["v" * 50] * 100 for i in range(200)},
    )
    huge_diff = "".join(
        f"diff --git a/src/f{i}.py b/src/f{i}.py\n--- a/src/f{i}.py\n+++ b/src/f{i}.py\n@@ -1 +1 @@\n" + "+added line\n" * 5000
        for i in range(50)
    )
    git = FakeGit(diff_text=huge_diff, changed=[f"src/f{i}.py" for i in range(500)])
    from hermclaw.context_builder import TurnRecord

    step = make_step(
        goal="Refactor everything. " * 2000,
        constraints=[f"constraint {i} " * 50 for i in range(40)],
        acceptance=[TestEvidence(command=f"pytest tests/test_f{i}.py " + "-k x " * 100) for i in range(30)],
        scope=ScopeContract(target_paths=[f"src/f{i}.py" for i in range(25)], allowed_new_paths=[f"new/{i}/**" for i in range(25)]),
        repo_hints=[f"hint{i}" for i in range(40)],
    )
    history = [TurnRecord(i, "run_test", "x" * 5000, i % 2 == 0, "y" * 5000, None if i % 2 == 0 else "test_failed") for i in range(1, 400)]
    failure = "E   first error line\n" + "noise\n" * 200_000 + "=== 3 failed, 1 passed in 9.0s ===\n"
    for ctx, out in ((32768, 6144), (8192, 2048), (4096, 1024)):
        builder, _, _ = make_builder(repo=repo, git=git, context_tokens=ctx, max_output_tokens=out, safety_margin_tokens=128)
        built = await builder.build(make_input(step=step, history=history, latest_failure=failure))
        plan = builder.plan
        total = sum(estimate_tokens(m.content) for m in built.messages)
        assert total + 16 <= plan.total_tokens
        assert built.report.estimated_prompt_tokens <= plan.total_tokens
        for sec in built.report.sections:
            if sec.name != "SYSTEM CONTRACT":
                assert sec.estimated_tokens <= max(sec.budget_tokens, 0), (ctx, sec)
        elastic = sum(char_cost(section(built.messages[1].content, n)) for n in ("RELEVANT CODE", "RELEVANT TESTS"))
        assert elastic <= plan.available_cost
        assert built.report.truncated
        user = built.messages[1].content
        assert "first error line" in user and "=== 3 failed, 1 passed in 9.0s ===" in user  # 16.5 even under pressure
        assert "chars omitted" in user


async def test_git_status_codes_and_excluded_changes_in_repo_facts() -> None:
    from hermclaw.core.interfaces import GitStatusEntry

    git = FakeGit(
        status_entries=[GitStatusEntry("app.py", " M"), GitStatusEntry("new.py", "??"), GitStatusEntry(".env", " M")],
        changed=["app.py", "old.py"],
    )
    builder, _, _ = make_builder(git=git)
    built = await builder.build(make_input())
    facts = section(built.messages[1].content, "CURRENT REPO FACTS")
    assert "Changed vs base (3): M app.py, old.py, ?? new.py" in facts
    assert ".env" not in facts


async def test_stale_index_snippets_are_replaced_by_fresh_workspace_reads() -> None:
    repo = default_repo()
    repo.files["svc.py"] = "def handler():\n    return 'new'\n"
    repo.context_hits = [RepoHit("svc.py", 1, 2, 1.0, "def handler():\n    return 'old'\n")]  # indexed before the edit
    builder, _, _ = make_builder(repo=repo)
    built = await builder.build(make_input(step=make_step(scope=None)))
    code = section(built.messages[1].content, "RELEVANT CODE")
    assert "return 'new'" in code and "return 'old'" not in code


async def test_provider_snippet_is_fallback_when_read_fails() -> None:
    repo = default_repo()
    repo.context_hits = [RepoHit("gone.py", 3, 4, 1.0, "a = 1\nb = 2\n"), RepoHit("nothing.py", 1, 1, 0.5, "")]
    builder, _, _ = make_builder(repo=repo)
    built = await builder.build(make_input(step=make_step(scope=None)))
    code = section(built.messages[1].content, "RELEVANT CODE")
    assert "### gone.py lines 3-4 [context]" in code and "a = 1\nb = 2" in code
    assert ("nothing.py:1-1", "read_failed") in [(d.item, d.reason) for d in built.report.dropped]
    assert any("repo.read gone.py: NotFoundError" in w for w in built.report.warnings)


async def test_hits_per_query_are_capped_by_score() -> None:
    repo = default_repo()
    repo.files.update({f"h{i:02d}.py": f"v = {i}\n" for i in range(30)})
    repo.context_hits = [RepoHit(f"h{i:02d}.py", 1, 1, i / 30, "") for i in range(30)]
    builder, _, _ = make_builder(repo=repo, max_hits_per_query=5)
    built = await builder.build(make_input(step=make_step(scope=None)))
    code = section(built.messages[1].content, "RELEVANT CODE")
    shown = sorted(line.split()[1] for line in code.splitlines() if line.startswith("### "))
    assert shown == [f"h{i}.py" for i in range(25, 30)]
    assert sum(1 for d in built.report.dropped if d.reason == "limit") == 25
