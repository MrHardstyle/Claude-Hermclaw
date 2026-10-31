from __future__ import annotations

from hermclaw.tools.locations import enclosing, keyword_hint, source_locations

FILES = {
    "tests/test_app.py": [
        "class TestTasks:",
        "    def test_open(self):",
        "        done = make()",
        "",
        "    def test_done(self):",
        "        first = make()",
        "        second = make()",
        "        if first:",
        "            use(first)",
    ],
    "src/app.ts": ["export function load() {", "  const x = 1;", "}"],
}


def lines_of(rel: str) -> list[str] | None:
    return FILES.get(rel)


def test_lines_named_by_linters_come_with_their_definition() -> None:
    out = "tests/test_app.py:7:9: F841 Local variable `second` is assigned to but never used\nsrc/app.ts(2,9): error TS6133"
    assert source_locations(out, lines_of) == [
        "tests/test_app.py:7 (in def test_done(self):): second = make()",
        "src/app.ts:2 (in export function load() {): const x = 1;",
    ]


def test_sandbox_paths_relative_cwd_and_unknown_files() -> None:
    out = (
        "/workspace/tests/test_app.py:9: AssertionError\n"
        "test_app.py:3: E\n"  # relative to the command's directory
        "example.com:443 refused\n"
        "missing.py:1: E\n"
        "tests/test_app.py:99: out of range\n"
        "tests/test_app.py:9: repeated\n"
    )
    assert source_locations(out, lines_of, cwd="tests") == [
        "tests/test_app.py:9 (in def test_done(self):): use(first)",
        "tests/test_app.py:3 (in def test_open(self):): done = make()",
    ]


def test_limit_and_top_level_lines() -> None:
    out = "\n".join(f"tests/test_app.py:{n}: x" for n in (1, 2, 3, 5, 6, 7))
    named = source_locations(out, lines_of, limit=2)
    assert named == ["tests/test_app.py:1: class TestTasks:", "tests/test_app.py:2 (in class TestTasks:): def test_open(self):"]
    assert enclosing(["x = 1"], 0) is None


def test_vb_keywords_used_as_names_are_named() -> None:
    """Live R3-vb (BUG-391): vbc says only "Keyword is not valid as an identifier"; the hint names the word."""
    files = {
        "src/Rechner/Parser.vb": [
            "Public Class BinaryNode",
            "    Public Property Operator As String",
            "    Public Sub New(operator As String, [Next] As Object)",
        ]
    }
    output = (
        "/workspace/src/Rechner/Parser.vb(2,21): error BC30183: Keyword is not valid as an identifier. [/workspace/x.vbproj]\n"
        "/workspace/src/Rechner/Parser.vb(3,20): error BC30183: Keyword is not valid as an identifier. [/workspace/x.vbproj]\n"
        "/workspace/src/Rechner/Parser.vb(3,41): error BC30183: Keyword is not valid as an identifier. [/workspace/x.vbproj]\n"
    )
    hint = keyword_hint(output, files.get)
    assert hint is not None and hint.startswith("[hint] `Operator`, `Next` is a reserved word in Visual Basic")
    assert "`[Operator]`" in hint
    assert keyword_hint(output.replace("BC30183", "BC30201"), files.get) is None  # another error
    assert keyword_hint(output, {}.get) is None  # file not in the workspace
    assert keyword_hint("/workspace/src/Rechner/Parser.vb(9,1): error BC30183: x", files.get) is None  # beyond the file
