"""Unresolvable new imports (BUG-193).

A step that adds ``import some_package`` inside a function passes its tests as long as no test calls that function,
and the merge request then ships code that crashes at runtime (rc18 battery J4: an invented package imported in
``main()``). This check resolves every module the change newly imports at top level:

1. only absolute imports on added lines of changed ``.py`` files; imports guarded by ``try/except ImportError`` (or a
   broader handler) and ``if TYPE_CHECKING:`` blocks are optional by design and skipped;
2. the standard library and modules of the repository (repository root, ``src/``, the importing file's directory)
   resolve without the sandbox;
3. the rest is resolved in the sandbox with ``importlib.util.find_spec`` – with the interpreter the step's test and
   command criteria use (``.venv/bin/python -m pytest``, ``uv run pytest`` …; plain ``python3`` otherwise), so a
   project virtualenv counts as installed. A module that does not resolve fails the check with what to do next.

Parse-only on the verifier host; nothing of the project is executed outside the sandbox.
"""

from __future__ import annotations

import ast
import posixpath
import re
import shlex
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from hermclaw.verifier.types import AddedContent

#: handlers that make an import optional
_OPTIONAL_HANDLERS = frozenset({"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})
_STDLIB = frozenset(sys.stdlib_module_names) | {"__future__"}

RESOLVE_SCRIPT = (
    "import importlib.util,sys\n"
    "missing=[m for m in sys.argv[1:] if importlib.util.find_spec(m) is None]\n"
    "print(' '.join(missing))\n"
    "sys.exit(3 if missing else 0)\n"
)
MISSING_EXIT_CODE = 3
_PYTHON = re.compile(r"python(\d+(\.\d+)*)?")
#: launchers that run their arguments inside the project environment
_RUNNERS = {"uv": "run", "poetry": "run", "pipenv": "run", "pdm": "run", "hatch": "run", "rye": "run"}
#: test tools that run in the interpreter they are installed in (``tox``/``nox`` build their own environments)
_PYTHON_TOOLS = frozenset({"pytest", "py.test", "coverage"})


def _optional_try(node: ast.Try) -> bool:
    for handler in node.handlers:
        if handler.type is None:
            return True
        names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for n in names:
            if isinstance(n, ast.Name) and n.id in _OPTIONAL_HANDLERS:
                return True
            if isinstance(n, ast.Attribute) and n.attr in _OPTIONAL_HANDLERS:
                return True
    return False


def _type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def imported_modules(source: str, lines: Iterable[int]) -> list[tuple[str, int]]:
    """``(top-level module, line)`` of the required absolute imports on ``lines`` (empty on a syntax error)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []  # the syntax check reports it
    wanted = set(lines)
    out: list[tuple[str, int]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Try) and _optional_try(node):
            for child in [*node.handlers, *node.orelse, *node.finalbody]:
                visit(child)  # only the guarded body is optional
            return
        if isinstance(node, ast.If) and _type_checking(node):
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.Import) and node.lineno in wanted:
            out.extend((alias.name.split(".")[0], node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.lineno in wanted:
            out.append((node.module.split(".")[0], node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return out


def _local(root: Path, importer: str, module: str) -> bool:
    bases = {root, root / "src", (root / importer).parent}
    return any((b / f"{module}.py").is_file() or (b / module).is_dir() for b in bases)


def new_unresolved_candidates(root: Path, added: AddedContent) -> dict[str, list[str]]:
    """Modules newly imported by the change that are neither standard library nor part of the repository:
    ``{module: ["path:line", ...]}`` – to be resolved in the sandbox."""
    out: dict[str, list[str]] = {}
    for path, added_lines in sorted(added.lines.items()):
        if not path.endswith(".py") or not added_lines:
            continue
        file = root / path
        try:
            source = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for module, line in imported_modules(source, (a.line for a in added_lines)):
            if module in _STDLIB or not module.isidentifier() or _local(root, path, module):
                continue
            out.setdefault(module, []).append(f"{path}:{line}")
    return out


def _interpreter(command: str) -> list[str] | None:
    """The Python launcher a command runs with, or ``None`` when it does not visibly run Python."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words = words[1:]  # FOO=bar prefixes
    if words and words[0] == "env":
        words = words[1:]
    if len(words) >= 2 and _RUNNERS.get(posixpath.basename(words[0])) == words[1]:
        return [*words[:2], "python"]
    if not words:
        return None
    head, base = posixpath.split(words[0])
    if _PYTHON.fullmatch(base):
        return [words[0]]
    if base in _PYTHON_TOOLS:
        # .venv/bin/pytest -> .venv/bin/python; a bare pytest from PATH runs in the default python3
        return [posixpath.join(head, "python")] if head else ["python3"]
    return None


def interpreter_for(commands: Iterable[str]) -> tuple[list[str], bool]:
    """``(launcher, named)``: the interpreter of the first command that names one, else a guessed ``python3``.

    Only a named interpreter may fail the check: a project whose tests run through ``make``/``tox``/``npm`` keeps its
    packages somewhere a guessed ``python3`` does not see, and failing such a step would be a false alarm (BUG-194)."""
    for command in commands:
        found = _interpreter(command)
        if found:
            return found, True
    return ["python3"], False


def resolve_command(modules: Iterable[str], interpreter: Sequence[str] = ("python3",)) -> str:
    names = " ".join(shlex.quote(m) for m in sorted(set(modules)))
    launcher = " ".join(shlex.quote(w) for w in interpreter)
    return f"{launcher} -c {shlex.quote(RESOLVE_SCRIPT)} {names}"
