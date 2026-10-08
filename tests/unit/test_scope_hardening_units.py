"""P15 regression tests for the scope-engine review fixes (pure functions, no database)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from hermclaw.core.interfaces import GitStatusEntry
from hermclaw.scope.audit import derive_changes_from_status, unresolved_renames
from hermclaw.scope.engine import (
    WorkspaceFiles,
    _filter_existing,
    canonical_path,
    classify_hint,
    constraint_delete_tokens,
    designated_deletes,
    implausible_new_path,
    is_unbounded_pattern,
    strip_location_suffix,
)


def _files(*paths: str) -> WorkspaceFiles:
    dirs = {"/".join(p.split("/")[:i]) for p in paths for i in range(1, p.count("/") + 1)}
    return WorkspaceFiles(root=Path("/nonexistent"), files=frozenset(paths), dirs=frozenset(dirs), source="git")


# ---------------------------------------------------------------------------------------------- canonical paths
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src//./app.py", "src/app.py"),
        ("./src/app.py", "src/app.py"),
        ("src/./", "src/"),
        ("src\\app.py", "src/app.py"),
        ("a/b/", "a/b/"),
    ],
)
def test_canonical_path_collapses_empty_and_dot_segments(raw: str, expected: str) -> None:
    assert canonical_path(raw) == expected


@pytest.mark.parametrize("raw", [".", "./", "././", "", "/etc/passwd", "../x.py", "src/../../x"])
def test_canonical_path_rejects_root_absolute_and_escaping(raw: str) -> None:
    with pytest.raises(ValueError):
        canonical_path(raw)


def test_repository_root_hint_is_invalid_not_a_new_path() -> None:
    files = _files("src/app.py")
    assert classify_hint(".", files) == "invalid"
    assert classify_hint("./", files) == "invalid"
    assert classify_hint("src/./app.py", files) == "path"


# ---------------------------------------------------------------------------------------------- creation patterns
@pytest.mark.parametrize(
    ("pattern", "unbounded"),
    [
        ("**", True),
        ("*", True),
        ("**/*.py", True),
        ("**.md", True),
        ("*/tests/", True),
        ("*/x.py", True),
        ("?*", True),
        ("src/**", False),
        ("tests/", False),
        ("*.md", False),
        ("docs/*.md", False),
        ("src/app/new.py", False),
    ],
)
def test_is_unbounded_pattern(pattern: str, unbounded: bool) -> None:
    assert is_unbounded_pattern(pattern) is unbounded


@pytest.mark.parametrize(
    ("path", "implausible"),
    [("src/a.py::Thing", True), ("src/a.py:12", True), ("C:/x.py", True), ("a\x00b.py", True), ("src/a.py", False)],
)
def test_implausible_new_path(path: str, implausible: bool) -> None:
    assert implausible_new_path(path) is implausible


# ---------------------------------------------------------------------------------------------- location suffixes
@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("src/a.py:12", "src/a.py"),
        ("src/a.py:12-40", "src/a.py"),
        ("src/a.py:3:5", "src/a.py"),
        ("src/a.py#L3", "src/a.py"),
        ("src/a.py#L3-L9", "src/a.py"),
        ("tests/t.py::test_x", "tests/t.py"),
        ("tests/t.py::TestA::test_b", "tests/t.py"),
        ("./src//a.py:7", "src/a.py"),
        ("missing.py:12", "missing.py:12"),  # base does not exist: left alone
        ("App::Models::User", "App::Models::User"),  # a symbol, not a file location
    ],
)
def test_strip_location_suffix(hint: str, expected: str) -> None:
    assert strip_location_suffix(hint, _files("src/a.py", "tests/t.py")) == expected


# ---------------------------------------------------------------------------------------------- delete constraints
@pytest.mark.parametrize(
    ("constraint", "tokens"),
    [
        ("remove src/flag.py from the build", []),
        ("remove the import from config.py", []),
        ("remove legacy/x.py from the repository", ["legacy/x.py"]),
        ("Remove legacy/x.py from git", ["legacy/x.py"]),
        ("delete legacy/x.py", ["legacy/x.py"]),
        ("die Zeile aus config.py entfernen", []),
        ("legacy/x.py aus dem Repo entfernen", ["legacy/x.py"]),
        ("legacy/x.py löschen", ["legacy/x.py"]),
        ("delete ./legacy//x.py", ["legacy/x.py"]),
    ],
)
def test_constraint_delete_tokens_respects_from_context(constraint: str, tokens: list[str]) -> None:
    assert constraint_delete_tokens([constraint]) == tokens


# ---------------------------------------------------------------------------------------------- delete designation
def test_designated_deletes() -> None:
    assert designated_deletes(None) is None
    assert designated_deletes({}) is None
    assert designated_deletes({"hints": []}) is None
    assert designated_deletes({"delete_paths": []}) == []
    evidence = {"delete_paths": [{"path": "a.py", "sources": ["x"]}, "b.py", {"path": "a.py"}, {"nope": 1}, 3]}
    assert designated_deletes(evidence) == ["a.py", "b.py"]


# ---------------------------------------------------------------------------------------------- rename origins
@dataclass(frozen=True)
class _EntryWithOrigin:
    path: str
    status: str
    orig_path: str | None = None


def test_rename_without_origin_is_unresolved() -> None:
    # exactly what hermclaw.gitops.reader.GitReader.status produces for a staged rename (origin dropped)
    entries = [GitStatusEntry("src/new.py", "R "), GitStatusEntry("src/copy.py", "C "), GitStatusEntry("a.py", " M")]
    assert derive_changes_from_status(entries) == [("src/new.py", "create"), ("src/copy.py", "create"), ("a.py", "modify")]
    assert unresolved_renames(entries) == ["src/new.py"]


def test_rename_with_origin_attribute_or_arrow_form_is_resolved() -> None:
    with_origin = _EntryWithOrigin("src/new.py", "R ", "src/old.py")
    arrow = GitStatusEntry("lib/a.py -> lib/b.py", "RM")
    changes = derive_changes_from_status([with_origin, arrow])  # type: ignore[list-item]
    assert changes == [("src/old.py", "delete"), ("src/new.py", "create"), ("lib/a.py", "delete"), ("lib/b.py", "create")]
    assert unresolved_renames([with_origin, arrow]) == []  # type: ignore[list-item]


# ---------------------------------------------------------------------------------------------- workspace listing
def test_files_below_a_symlinked_directory_escaping_the_workspace_are_dropped(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    outside = tmp_path / "outside"
    (root / "src").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("s\n", encoding="utf-8")
    (root / "src" / "ok.py").write_text("x\n", encoding="utf-8")
    (root / "inside").mkdir()
    (root / "inside" / "real.py").write_text("x\n", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "alias").symlink_to(root / "inside", target_is_directory=True)

    files, dirs = _filter_existing(root, ["src/ok.py", "link/secret.txt", "alias/real.py", "inside/real.py"])
    assert files == frozenset({"src/ok.py", "alias/real.py", "inside/real.py"})
    assert "link" not in dirs
