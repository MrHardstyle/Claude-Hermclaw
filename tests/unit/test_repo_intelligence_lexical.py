"""P11 11.4: lexical search via ripgrep (``rg --json``) and the Python fallback, plus file-name search."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from hermclaw.core.errors import HermclawError, ValidationFailed
from hermclaw.repo_intelligence import LexicalSearcher, RepoIntelConfig
from tests.integration.test_repo_intelligence_support import build_fixture_repo, write_files

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
ENGINES = ("ripgrep", "python")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return build_fixture_repo(tmp_path / "fx")


def _pairs(res: object) -> list[tuple[str, int]]:
    return [(h.path, h.line) for h in res.hits]  # type: ignore[attr-defined]


@pytest.mark.parametrize("engine", ENGINES)
async def test_fixed_string_search_with_identical_semantics(repo: Path, engine: str) -> None:
    s = LexicalSearcher()
    res = await s.search(repo, "calculate_invoice_total", engine=engine)  # type: ignore[arg-type]
    assert res.engine == engine and not res.truncated
    assert _pairs(res) == [
        ("app/main.py", 3),
        ("app/main.py", 16),
        ("app/services/billing.py", 6),
        ("tests/test_billing.py", 3),
        ("tests/test_billing.py", 7),
    ]
    hit = res.hits[2]
    assert hit.text.startswith("def calculate_invoice_total") and hit.column == 5 and hit.matches == ["calculate_invoice_total"]
    # fixed strings are literal: regex metacharacters do not match anything else
    assert (await s.search(repo, "sum(amounts)", engine=engine)).hits[0].path == "app/services/billing.py"  # type: ignore[arg-type]


@pytest.mark.parametrize("engine", ENGINES)
async def test_regex_globs_case_word_and_limits(repo: Path, engine: str) -> None:
    s = LexicalSearcher()
    rx = await s.search(repo, r"^function \w+\(", mode="regex", engine=engine)  # type: ignore[arg-type]
    assert {h.path for h in rx.hits} == {"web/lib/util.js", "web/server.js", "php/src/helpers.php"}
    only_js = await s.search(repo, r"^function \w+\(", mode="regex", globs=["*.js"], engine=engine)  # type: ignore[arg-type]
    assert {h.path for h in only_js.hits} == {"web/lib/util.js", "web/server.js"}
    no_web = await s.search(repo, r"^function \w+\(", mode="regex", exclude_globs=["web/**"], engine=engine)  # type: ignore[arg-type]
    assert {h.path for h in no_web.hits} == {"php/src/helpers.php"}
    # smart case: lower-case pattern is case-insensitive, an upper-case letter makes it case-sensitive
    assert {h.path for h in (await s.search(repo, "shoppingcart", engine=engine)).hits} == {"web/src/cart.ts"}  # type: ignore[arg-type]
    assert (await s.search(repo, "SHOPPINGCART", engine=engine)).hits == []  # type: ignore[arg-type]
    # whole words only
    word = await s.search(repo, "total", word=True, globs=["*.py"], engine=engine)  # type: ignore[arg-type]
    assert all(h.path != "app/services/billing.py" or "total" in h.text for h in word.hits)
    assert not any(h.text.strip().startswith("def calculate_invoice_total") for h in word.hits)
    # limits: per file and total
    per_file = await s.search(repo, "invoice", max_per_file=1, engine=engine)  # type: ignore[arg-type]
    assert len({h.path for h in per_file.hits}) == len(per_file.hits)
    capped = await s.search(repo, "invoice", max_results=2, engine=engine)  # type: ignore[arg-type]
    assert len(capped.hits) == 2 and capped.truncated
    # several patterns in one call
    multi = await s.search(repo, ["slugify", "render_profile"], engine=engine)  # type: ignore[arg-type]
    assert {"web/lib/util.js", "php/src/helpers.php"} <= {h.path for h in multi.hits}


@pytest.mark.parametrize("engine", ENGINES)
async def test_secret_and_ignored_files_are_never_searched(repo: Path, engine: str) -> None:
    s = LexicalSearcher()
    assert (await s.search(repo, "OPENSSH PRIVATE KEY", engine=engine)).hits == []  # type: ignore[arg-type]
    # not even when the caller explicitly includes them by glob or path
    assert (await s.search(repo, "PRIVATE", globs=["deploy/**"], engine=engine)).hits == []  # type: ignore[arg-type]
    assert (await s.search(repo, "PRIVATE", paths=["deploy/id_rsa"], engine=engine)).hits == []  # type: ignore[arg-type]
    assert (await s.search(repo, "IGNORED_BUILD_OUTPUT", engine=engine)).hits == []  # type: ignore[arg-type]
    assert (await s.search(repo, "ignored by .gitignore", engine=engine)).hits == []  # type: ignore[arg-type]
    assert (await s.search(repo, "IHDR", engine=engine)).hits == []  # type: ignore[arg-type]  # binary
    assert (await s.search(repo, "repositoryformatversion", paths=[".git"], engine=engine)).hits == []  # type: ignore[arg-type]


@pytest.mark.parametrize("engine", ENGINES)
async def test_explicit_paths_are_confined_to_the_workspace(repo: Path, tmp_path: Path, engine: str) -> None:
    s = LexicalSearcher()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("TOP-SECRET-OUTSIDE\n", encoding="utf-8")
    os.symlink(outside, repo / "linked_dir")
    os.symlink(outside / "leak.txt", repo / "linked_file.txt")
    for p in (["../outside/leak.txt"], ["/etc/passwd"]):
        with pytest.raises(HermclawError):
            await s.search(repo, "root", paths=p, engine=engine)  # type: ignore[arg-type]
    res = await s.search(repo, "TOP-SECRET-OUTSIDE", paths=["linked_dir", "linked_file.txt", "linked_dir/leak.txt"], engine=engine)  # type: ignore[arg-type]
    assert res.hits == []
    assert (await s.search(repo, "TOP-SECRET-OUTSIDE", engine=engine)).hits == []  # type: ignore[arg-type]
    scoped = await s.search(repo, "slugify", paths=["web/lib"], engine=engine)  # type: ignore[arg-type]
    assert {h.path for h in scoped.hits} == {"web/lib/util.js"}


async def test_invalid_patterns_are_rejected(repo: Path) -> None:
    s = LexicalSearcher()
    for engine in ENGINES:
        with pytest.raises(ValidationFailed):
            await s.search(repo, "(unclosed", mode="regex", engine=engine)  # type: ignore[arg-type]
    for bad in ("", ["", ""], "line\nbreak", "x" * 3000):
        with pytest.raises(ValidationFailed):
            await s.search(repo, bad)
    # a pattern that looks like a flag is a pattern, not an option
    write_files(repo, {"flags.txt": "--files-with-matches\n"})
    res = await s.search(repo, "--files-with-matches")
    assert [h.path for h in res.hits] == ["flags.txt"]


async def test_ripgrep_timeout_returns_truncated_result(repo: Path, tmp_path: Path) -> None:
    fake = tmp_path / "bin" / "slow-rg"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nsleep 10\n", encoding="utf-8")
    fake.chmod(0o755)
    s = LexicalSearcher(RepoIntelConfig(rg_binary=str(fake)))
    started = time.monotonic()
    res = await s.search(repo, "anything", timeout_s=0.5)
    assert res.truncated and res.hits == [] and res.engine == "ripgrep"
    assert time.monotonic() - started < 8


async def test_missing_ripgrep_falls_back_to_python(repo: Path) -> None:
    s = LexicalSearcher(RepoIntelConfig(rg_binary="definitely-not-installed-rg"))
    assert not s.has_ripgrep
    res = await s.search(repo, "slugify")
    assert res.engine == "python" and {h.path for h in res.hits} == {"web/lib/util.js", "web/server.js", "web/src/cart.ts"}


async def test_filename_search(repo: Path) -> None:
    s = LexicalSearcher()
    hits = await s.find_files(repo, "billing")
    assert hits[0].path == "app/services/billing.py"
    assert {h.path for h in hits} >= {"tests/test_billing.py"}
    assert (await s.find_files(repo, "app/services/billing.py"))[0].score == 1.0
    assert (await s.find_files(repo, "id_rsa")) == []  # protected files are not listed
    assert (await s.find_files(repo, "")) == []
    fuzzy = LexicalSearcher.rank_filenames(["src/ProfileController.php", "src/other.php"], "prfctrl")
    assert [m.path for m in fuzzy] == ["src/ProfileController.php"]


async def test_python_fallback_is_bounded_by_its_deadline(tmp_path: Path) -> None:
    root = tmp_path / "redos"
    write_files(root, {"evil.txt": "a" * 26 + "b\n"})
    s = LexicalSearcher()
    started = time.monotonic()
    res = await s.search(root, r"(a+)+$", mode="regex", engine="python", timeout_s=0.2)
    assert res.truncated and res.hits == []
    assert time.monotonic() - started < 5  # the caller gets an answer although the regex is still backtracking
