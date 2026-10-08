"""Unit tests for P15 scope engine helpers (no database): hint classification, confidence selection, delete
constraints, acceptance parsing, forbidden merge, workspace listing, porcelain mapping and import evidence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import ScopePolicy
from hermclaw.core.interfaces import GitStatusEntry
from hermclaw.scope.audit import derive_changes_from_status
from hermclaw.scope.engine import (
    WorkspaceFiles,
    classify_hint,
    constraint_delete_tokens,
    deny_all_contract,
    list_workspace_files,
    merged_forbidden,
    parse_acceptance,
    select_confident_hits,
)
from hermclaw.scope.expansion import find_import_of, import_references, is_test_path, language_of, subject_of_test
from hermclaw.scope.guard import ScopeGuard
from tests.integration.test_scope_support import build_repo, git, hit, write


def _files(*paths: str) -> WorkspaceFiles:
    dirs = set()
    for p in paths:
        parts = p.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return WorkspaceFiles(root=Path("/nonexistent"), files=frozenset(paths), dirs=frozenset(dirs), source="git")


FILES = _files("src/app.py", "src/util/helpers.py", "README", "Makefile", "docs/guide.md", ".env")


# ---------------------------------------------------------------------------------------------- classification
@pytest.mark.parametrize(
    "hint,kind",
    [
        ("src/app.py", "path"),
        ("./src/app.py", "path"),
        ("src/*.py", "glob"),
        ("src/**/*.py", "glob"),
        ("src", "directory"),
        ("src/util/", "directory"),
        ("newdir/", "directory"),
        ("README", "path"),
        ("Makefile", "path"),
        ("app.py", "path"),
        ("new_module.py", "path"),
        (".env", "path"),
        ("src/new/thing.py", "path"),
        ("ScopeGuard", "symbol"),
        ("ScopeGuard.decide", "symbol"),
        ("hermclaw.scope.guard", "symbol"),
        ("App\\Models\\User", "symbol"),
        ("Foo::bar", "symbol"),
        ("run()", "symbol"),
        ("login handler for users", "text"),
        ("/etc/passwd", "invalid"),
        ("../outside.py", "invalid"),
        ("/abs/*.py", "invalid"),
        ("", "invalid"),
        ("   ", "invalid"),
    ],
)
def test_classify_hint(hint: str, kind: str) -> None:
    assert classify_hint(hint, FILES) == kind


# ---------------------------------------------------------------------------------------------- confidence
def test_select_confident_hits_thresholds_and_relative_floor() -> None:
    hits = [hit("src/app.py", 0.95, symbol=1.0), hit("src/util/helpers.py", 0.7), hit("docs/guide.md", 0.9)]
    sel = select_confident_hits(hits, FILES, min_score=0.75, relative_floor=0.98, max_files=5)
    assert sel.accepted == ["src/app.py"]  # guide.md (0.9) is below 0.98 * 0.95
    assert not sel.ambiguous
    recorded = {r["path"]: r for r in sel.records}
    assert recorded["src/app.py"]["accepted"] is True
    assert recorded["src/app.py"]["signals"] == {"symbol": 1.0}
    assert recorded["src/util/helpers.py"]["accepted"] is False


def test_select_confident_hits_dedupes_clamps_and_ignores_foreign_paths() -> None:
    hits = [hit("src/app.py", 0.5), hit("src/app.py", 7.0), hit("vendor/x.py", 0.99), hit("/abs/y.py", 0.99)]
    sel = select_confident_hits(hits, FILES, min_score=0.8, relative_floor=0.8, max_files=5)
    assert sel.accepted == ["src/app.py"]
    notes = {r["path"]: r.get("note") for r in sel.records}
    assert notes["vendor/x.py"] == "not in workspace"
    assert notes["/abs/y.py"] == "invalid path"
    assert next(r for r in sel.records if r["path"] == "src/app.py")["score"] == 1.0


def test_select_confident_hits_ambiguous_takes_nothing() -> None:
    files = _files(*(f"m{i}.py" for i in range(6)))
    sel = select_confident_hits([hit(f"m{i}.py", 0.9) for i in range(6)], files, min_score=0.8, relative_floor=0.8, max_files=5)
    assert sel.ambiguous and sel.accepted == []
    assert all(r["accepted"] is False for r in sel.records)


# ---------------------------------------------------------------------------------------------- delete constraints
@pytest.mark.parametrize(
    "constraint,expected",
    [
        ("Delete legacy/old_module.py after migrating callers.", ["legacy/old_module.py"]),
        ("remove the file `legacy/old_module.py`", ["legacy/old_module.py"]),
        ("rm legacy/*.py; keep docs", ["legacy/*.py"]),
        ("Die Datei legacy/old_module.py löschen", ["legacy/old_module.py"]),
        ("lösche die Datei legacy/old_module.py", ["legacy/old_module.py"]),
        ("Do not delete legacy/old_module.py", []),
        ("never remove config/settings.yaml", []),
        ("legacy/old_module.py nicht löschen", []),
        ("remove the deprecated function from src/app.py", []),
        ("keep everything", []),
    ],
)
def test_constraint_delete_tokens(constraint: str, expected: list[str]) -> None:
    tokens = constraint_delete_tokens([constraint])
    for e in expected:
        assert e in tokens
    if not expected:
        assert not [t for t in tokens if "/" in t or "." in t]


def test_constraint_delete_tokens_ignores_non_strings_and_invalid_paths() -> None:
    assert constraint_delete_tokens([123, None, "delete ../etc/passwd"]) == []


# ---------------------------------------------------------------------------------------------- acceptance / forbidden
def test_parse_acceptance_skips_invalid_items() -> None:
    items = parse_acceptance(
        [
            {"type": "absence", "path_glob": "legacy/*.py"},
            {"type": "presence", "path_glob": "src/new.py"},
            {"type": "unknown"},
            "garbage",
        ]
    )
    assert [i.type for i in items] == ["absence", "presence"]


def test_merged_forbidden_normalises_dedupes_and_reports_invalid() -> None:
    policy = ScopePolicy(always_forbidden=[".git/**", "**/.env"])
    merged, rejected = merged_forbidden(policy, ["./secrets/", "**/.env", "/abs", 7])
    assert merged == [".git/**", "**/.env", "secrets/"]
    assert rejected and rejected[0]["path"] == "/abs"


def test_deny_all_contract_allows_nothing() -> None:
    guard = ScopeGuard(deny_all_contract(forbidden=["**/.env"], reason="none"), ScopePolicy())
    for op in ("create", "modify", "delete"):
        assert not guard.allowed("src/app.py", op)  # type: ignore[arg-type]


def test_workspace_files_creatable() -> None:
    assert FILES.creatable("src/new.py") == (True, "creatable")
    assert FILES.creatable("src/app.py")[0] is False
    assert FILES.creatable("src")[1] == "is a directory"
    assert FILES.creatable("src/app.py/inner.py")[1] == "parent 'src/app.py' is a file"


# ---------------------------------------------------------------------------------------------- workspace listing
async def test_list_workspace_files_git(tmp_repo: Path, tmp_path: Path) -> None:
    repo = build_repo(tmp_repo)
    (repo / "app.py").unlink()  # tracked but deleted in the work tree -> not listed
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, repo / "escape.txt")
    os.symlink(repo / "README.md", repo / "inside-link.md")
    files = await list_workspace_files(repo)
    assert files.source == "git"
    assert "src/app/core.py" in files.files
    assert "notes/untracked.md" in files.files  # untracked, not ignored
    assert "build/out.js" not in files.files and "debug.log" not in files.files  # ignored
    assert "app.py" not in files.files
    assert "escape.txt" not in files.files  # symlink leaving the workspace
    assert "inside-link.md" in files.files
    assert not any(f.startswith(".git/") for f in files.files)
    assert files.is_dir("src/app") and files.is_dir("src")


async def test_list_workspace_files_walk_fallback(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write(plain, "a/b.txt")
    write(plain, ".git/config", "not a repo")  # directory named .git without a repository
    files = await list_workspace_files(plain)
    assert files.source == "walk"
    assert files.files == frozenset({"a/b.txt"})


# ---------------------------------------------------------------------------------------------- porcelain mapping
def test_derive_changes_from_status() -> None:
    entries = [
        GitStatusEntry("src/a.py", " M"),
        GitStatusEntry("src/b.py", "M "),
        GitStatusEntry("src/c.py", "MM"),
        GitStatusEntry("new.py", "??"),
        GitStatusEntry("added.py", "A "),
        GitStatusEntry("added2.py", "AM"),
        GitStatusEntry("gone.py", " D"),
        GitStatusEntry("gone2.py", "D "),
        GitStatusEntry("old.py -> renamed.py", "R "),
        GitStatusEntry("orig.py -> copy.py", "C "),
        GitStatusEntry("conflict.py", "UU"),
        GitStatusEntry("typechange", " T"),
        GitStatusEntry("ignored.log", "!!"),
        GitStatusEntry('"spaced name.py"', "??"),
        GitStatusEntry('"\\303\\244.py"', "??"),
        GitStatusEntry("src/a.py", " M"),  # duplicate
    ]
    assert derive_changes_from_status(entries) == [
        ("src/a.py", "modify"),
        ("src/b.py", "modify"),
        ("src/c.py", "modify"),
        ("new.py", "create"),
        ("added.py", "create"),
        ("added2.py", "create"),
        ("gone.py", "delete"),
        ("gone2.py", "delete"),
        ("old.py", "delete"),
        ("renamed.py", "create"),
        ("copy.py", "create"),
        ("conflict.py", "modify"),
        ("typechange", "modify"),
        ("spaced name.py", "create"),
        ("ä.py", "create"),
    ]


def test_derive_changes_from_real_git_status(tmp_repo: Path) -> None:
    write(tmp_repo, "app.py", "def add(a, b):\n    return a - b\n")
    write(tmp_repo, "fresh.py")
    git(tmp_repo, "rm", "-q", "README.md")
    out = git(tmp_repo, "status", "--porcelain")
    entries = [GitStatusEntry(line[3:], line[:2]) for line in out.splitlines()]
    assert sorted(derive_changes_from_status(entries)) == [("README.md", "delete"), ("app.py", "modify"), ("fresh.py", "create")]


# ---------------------------------------------------------------------------------------------- expansion helpers
@pytest.mark.parametrize(
    "path,is_test,subject",
    [
        ("tests/test_core.py", True, "core"),
        ("src/core_test.go", True, "core"),
        ("src/web/fmt.spec.ts", True, "fmt"),
        ("src/web/fmt.test.ts", True, "fmt"),
        ("src/test/java/AppTest.java", True, "app"),
        ("tests/conftest.py", True, None),
        ("src/app/core.py", False, None),
        ("src/contest.py", False, None),
    ],
)
def test_test_path_detection(path: str, is_test: bool, subject: str | None) -> None:
    assert is_test_path(path) is is_test
    assert subject_of_test(path) == subject


def test_language_of() -> None:
    assert language_of("a/b.py") == "python"
    assert language_of("a/b.ts") == language_of("a/c.js") == "javascript"
    assert language_of("a/b.go") == "go"
    assert language_of("Makefile") is None


@pytest.mark.parametrize(
    "importer,line,imported",
    [
        ("src/app/core.py", "from app.helpers import slug", "src/app/helpers.py"),
        ("src/app/core.py", "from . import models", "src/app/models.py"),
        ("src/app/core.py", "from .models import User", "src/app/models.py"),
        ("src/app/sub/x.py", "from ..helpers import slug", "src/app/helpers.py"),
        ("src/app/core.py", "import app.helpers as h", "src/app/helpers.py"),
        ("src/web/index.ts", "import { fmt } from './fmt';", "src/web/fmt.ts"),
        ("src/web/index.ts", "const x = require('../lib/util')", "src/lib/util.js"),
        ("src/web/index.ts", "export { y } from './fmt'", "src/web/fmt.ts"),
        ("src/main.c", '#include "util.h"', "src/util.h"),
        ("src/main.rs", "use crate::scope::guard::ScopeGuard;", "src/scope/guard.rs"),
        ("src/main.rs", "mod helpers;", "src/helpers.rs"),
        ("app/Http/Controller.php", "use App\\Models\\User;", "app/Models/User.php"),
        ("src/pkg/a.py", "import pkg", "src/pkg/__init__.py"),
    ],
)
def test_find_import_of_positive(importer: str, line: str, imported: str) -> None:
    assert find_import_of(importer, line + "\n", imported) == line.strip()


@pytest.mark.parametrize(
    "importer,content,imported",
    [
        ("src/app/core.py", "from app.helpers import slug\n", "src/lib/util.py"),
        ("src/app/core.py", "x = 'helpers'\n", "src/app/helpers.py"),
        ("src/web/index.ts", "export const fmt = 1;\n", "src/web/fmt.ts"),
        ("src/app/core.py", "import os\n", "src/lib/os.py"),
    ],
)
def test_find_import_of_negative(importer: str, content: str, imported: str) -> None:
    assert find_import_of(importer, content, imported) is None


def test_import_references_python_from_multiple_names() -> None:
    refs = import_references("from app.models import User, Group as G", "src/app/core.py")
    assert "app/models" in refs and "app/models/User" in refs and "app/models/Group" in refs


def test_scope_contract_roundtrip_matches_guard() -> None:
    c = ScopeContract(target_paths=["src/app.py"], allowed_new_paths=["tests/test_app.py"], allowed_operations=["create", "modify"])
    g = ScopeGuard(c, ScopePolicy())
    assert g.allowed("src/app.py", "modify") and g.allowed("tests/test_app.py", "create")
    assert not g.allowed("src/app.py", "delete")
