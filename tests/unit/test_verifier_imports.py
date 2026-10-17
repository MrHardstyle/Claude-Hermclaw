"""New-import resolution (BUG-193): which imports of a change must resolve."""

from __future__ import annotations

from pathlib import Path

from hermclaw.verifier.imports import imported_modules, new_unresolved_candidates, resolve_command
from hermclaw.verifier.types import AddedContent, AddedLine

SOURCE = """\
from __future__ import annotations
import os, requests.adapters
from typing import TYPE_CHECKING
from . import sibling
from .pkg import thing
if TYPE_CHECKING:
    import typing_only_zz9
try:
    import ujson as json
except ImportError:
    import json
def main():
    import lazy_zz9
    from fancy_zz9.sub import x
"""


def test_only_required_absolute_imports_on_added_lines_count() -> None:
    found = imported_modules(SOURCE, range(1, 20))
    assert [m for m, _ in found] == ["__future__", "os", "requests", "typing", "json", "lazy_zz9", "fancy_zz9"]
    assert imported_modules(SOURCE, [13]) == [("lazy_zz9", 13)]  # untouched lines are not judged
    assert imported_modules("def broken(:\n", [1]) == []  # the syntax check reports it


def test_standard_library_and_repository_modules_need_no_sandbox(tmp_path: Path) -> None:
    root = tmp_path
    (root / "src" / "mylib").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "tools" / "helper.py").write_text("X = 1\n")
    (root / "local_mod.py").write_text("Y = 2\n")
    code = "import os\nimport local_mod\nimport mylib\nimport helper\nimport missing_zz9\n"
    (root / "tools" / "run.py").write_text(code)
    added = AddedContent(lines={"tools/run.py": [AddedLine(i, t) for i, t in enumerate(code.splitlines(), start=1)]})
    assert new_unresolved_candidates(root, added) == {"missing_zz9": ["tools/run.py:5"]}
    assert "find_spec" in resolve_command(["b", "a"]) and resolve_command(["b", "a"]).endswith(" a b")
