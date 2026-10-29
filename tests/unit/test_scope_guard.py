import re
import warnings

import pytest

from hermclaw.contracts.scope import ScopeContract, normalise_path
from hermclaw.core.config import ScopePolicy
from hermclaw.core.errors import ScopeViolation
from hermclaw.scope.engine import glob_escape
from hermclaw.scope.guard import ScopeGuard, _glob_regex, path_matches

# a regex built from a glob must never warn (e.g. "FutureWarning: Possible nested set" for '[[]')
pytestmark = pytest.mark.filterwarnings("error::FutureWarning")


@pytest.mark.parametrize(
    "path,pattern,ok",
    [
        ("src/a.py", "src/a.py", True),
        ("src/a.py", "src/*.py", True),
        ("src/x/a.py", "src/*.py", False),
        ("src/x/a.py", "src/**/*.py", True),
        ("src/a.py", "src/**/*.py", True),
        ("a/b/.env", "**/.env", True),
        (".env", "**/.env", True),
        ("docs/x/y.md", "docs/", True),
        ("docsx/y.md", "docs/", False),
        ("src/a.py", "src", False),
    ],
)
def test_glob_semantics(path: str, pattern: str, ok: bool) -> None:
    assert path_matches(path, pattern) is ok


def test_guard_decisions() -> None:
    g = ScopeGuard(
        ScopeContract(
            target_paths=["src/app.py", "tests/**"],
            allowed_new_paths=["src/new/*.py"],
            forbidden_paths=["src/secret.py"],
            allowed_operations=["create", "modify"],
        ),
        ScopePolicy(),
    )
    assert g.allowed("src/app.py", "modify")
    assert g.allowed("tests/unit/test_x.py", "modify")
    assert g.allowed("src/new/mod.py", "create")
    assert not g.allowed("src/new/deep/mod.py", "create")
    assert not g.allowed("src/other.py", "modify")
    assert not g.allowed("src/app.py", "delete")  # delete not allowed
    assert not g.allowed(".git/config", "modify")  # policy forbidden
    assert not g.allowed("src/secret.py", "modify")
    assert not g.allowed("../etc/passwd", "modify")
    with pytest.raises(ScopeViolation):
        g.check("src/other.py", "modify")
    assert [v["path"] for v in g.audit([("src/app.py", "modify"), ("README.md", "modify")])] == ["README.md"]


def test_a_lock_file_beside_a_writable_manifest_may_be_written() -> None:
    """BUG-378: `cargo test` writes Cargo.lock beside Cargo.toml; a scope listing only the manifest refused every test
    run of ladder R2-rust as COMMAND_SCOPE_VIOLATION."""
    g = ScopeGuard(
        ScopeContract(
            target_paths=["web/package.json"],
            allowed_new_paths=["Cargo.toml", "src/main.rs"],
            forbidden_paths=["svc/go.mod", "svc/go.sum"],
            allowed_operations=["create", "modify"],
        ),
        ScopePolicy(),
    )
    assert g.decide("Cargo.lock", "create") == (True, "lock file of Cargo.toml")
    assert g.allowed("Cargo.lock", "modify")
    assert g.allowed("web/package-lock.json", "create")
    assert not g.allowed("web/Cargo.lock", "create")  # no manifest of its own beside it
    assert not g.allowed("Cargo.lock", "delete")
    assert not g.allowed("svc/go.sum", "create")  # forbidden wins
    assert not g.allowed("src/Cargo.lock.bak", "create")


# ---------------------------------------------------------------------------------------------- character classes
@pytest.mark.parametrize(
    "literal,same_length_other",
    [("routes/[id].tsx", "routes/i.tsx"), ("[slug]/page.tsx", "s/page.tsx"), ("app/[...all]/[[opt]].ts", "app/./o.ts")],
)
def test_escaped_bracket_file_names_match_only_themselves(literal: str, same_length_other: str) -> None:
    pattern = glob_escape(literal)
    assert "[[]" in pattern
    _glob_regex.cache_clear()
    re.purge()  # the warning is emitted at compile time only: compile afresh
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert path_matches(literal, pattern)
        assert not path_matches(same_length_other, pattern)
        assert not path_matches(literal.replace("[", "").replace("]", ""), pattern)


def test_escaped_bracket_target_guards_modify_and_explicit_create() -> None:
    g = ScopeGuard(ScopeContract(target_paths=[glob_escape("routes/[id].tsx"), "src/a.py"], allowed_operations=["create", "modify"]))
    assert g.allowed("routes/[id].tsx", "modify") and not g.allowed("routes/i.tsx", "modify")
    # an explicitly listed target may be (re-)created – also when its name had to be glob-escaped
    assert g.allowed("src/a.py", "create")
    assert g.allowed("routes/[id].tsx", "create") and not g.allowed("routes/d.tsx", "create")
    assert not g.allowed("routes/[[]id].tsx", "create")  # the escaped spelling is not itself a file name


@pytest.mark.parametrize(
    "pattern,path,ok",
    [
        ("[ab]c", "ac", True),
        ("[ab]c", "bc", True),
        ("[ab]c", "cc", False),
        ("[ab]c", "[c", False),
        ("[a-c]x", "bx", True),
        ("[a-c]x", "dx", False),
        ("[!a]x", "bx", True),  # glob negation, not the literal '!'
        ("[!a]x", "ax", False),
        ("[!a]x", "!x", True),
        ("[^a]x", "bx", True),
        ("[^a]x", "ax", False),
        ("[]]", "]", True),  # a leading ']' is a member
        ("[]]", "a", False),
        ("a[!]]", "ab", True),
        ("a[!]]", "a]", False),
        ("a[b-]", "a-", True),
        ("a[]", "a[]", True),  # never closed: literal characters
        ("x[", "x[", True),
        ("[z-a]x", "zx", False),  # inverted range: empty
        ("[z-a]x", "ax", False),
    ],
)
def test_character_class_semantics(pattern: str, path: str, ok: bool) -> None:
    _glob_regex.cache_clear()
    re.purge()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert path_matches(path, pattern) is ok


@pytest.mark.parametrize("pattern", ["x[/]y", "x[!a]y", "x[^a]y", "x[+-0]y", "x[!]]y"])
def test_character_classes_never_cross_a_path_segment(pattern: str) -> None:
    assert not path_matches("x/y", pattern)


def test_malformed_classes_in_a_contract_never_crash_the_guard() -> None:
    g = ScopeGuard(ScopeContract(target_paths=["a[]", "[z-a]"], allowed_new_paths=["[]]"], forbidden_paths=["b[]"]))
    assert g.decide("a[]", "modify") == (True, "target path")
    assert not g.allowed("z", "modify") and not g.allowed("a", "modify")
    assert g.allowed("]", "create")
    assert not g.allowed("b[]", "modify")


# ---------------------------------------------------------------------------------------------- canonical paths
@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("src/./a.py", "src/a.py"),
        ("src//a.py", "src/a.py"),
        ("./src/a.py", "src/a.py"),
        ("././src/.//a.py", "src/a.py"),
        ("src/a.py/.", "src/a.py"),
        ("docs//", "docs/"),
        ("docs/./", "docs/"),
        ("src\\a.py", "src/a.py"),
    ],
)
def test_normalise_path_collapses_dot_and_empty_segments(raw: str, canonical: str) -> None:
    assert normalise_path(raw) == canonical
    assert normalise_path(canonical) == canonical  # idempotent


@pytest.mark.parametrize("raw", ["", " ", ".", "./", "././", "//", "/src/a.py", "//src/a.py", "..", "src/../a.py", "src/a\x00.py", "\x00"])
def test_normalise_path_rejects_invalid_paths(raw: str) -> None:
    with pytest.raises(ValueError, match="repository-relative"):
        normalise_path(raw)


def test_contract_rejects_root_and_nul_entries() -> None:
    with pytest.raises(ValueError):
        ScopeContract(allowed_new_paths=["./"])  # used to become the absolute '/'
    with pytest.raises(ValueError):
        ScopeContract(target_paths=["src/a\x00.py"])
    contract = ScopeContract(target_paths=["src//a.py", "./src/a.py"], allowed_new_paths=["new/./"])
    assert contract.target_paths == ["src/a.py"] and contract.allowed_new_paths == ["new/"]


def _odd(path: str) -> list[str]:
    """Spellings of ``path`` that denote the same file (what a worker or a tool call might send)."""
    return [f"./{path}", path.replace("/", "//"), path.replace("/", "/./"), f" {path} ", f"././{path.replace('/', '/.//')}"]


def _guard() -> ScopeGuard:
    return ScopeGuard(
        ScopeContract(
            target_paths=["src/app/a.py", "lib/**"],
            allowed_new_paths=["src/app/new/"],
            forbidden_paths=["lib/secret.py"],
            allowed_operations=["create", "modify"],
        ),
        ScopePolicy(),
    )


@pytest.mark.parametrize("spelling", _odd("src/app/a.py"))
def test_odd_spelling_of_a_target_is_judged_as_the_target(spelling: str) -> None:
    assert _guard().decide(spelling, "modify") == (True, "target path")
    assert _guard().decide(spelling, "create") == (True, "allowed new path")  # explicitly listed target


@pytest.mark.parametrize("spelling", [*_odd("src/app/new/x.py"), *_odd("lib/deep/b.py")])
def test_odd_spelling_of_a_scoped_path_stays_allowed(spelling: str) -> None:
    canonical = normalise_path(spelling)
    for op in ("create", "modify"):
        assert _guard().decide(spelling, op) == _guard().decide(canonical, op)
    assert _guard().allowed(spelling, "create" if canonical.startswith("src/") else "modify")


@pytest.mark.parametrize("spelling", [*_odd("lib/secret.py"), *_odd(".git/config"), *_odd("a/b/.env"), *_odd(".env")])
def test_odd_spelling_never_bypasses_a_forbidden_path(spelling: str) -> None:
    for op in ("create", "modify", "delete"):
        ok, reason = _guard().decide(spelling, op)
        assert not ok and f"path '{normalise_path(spelling)}' is forbidden" == reason


@pytest.mark.parametrize("spelling", [*_odd("src/app/b.py"), *_odd("src/other/x.py"), "src/app/new/./../x.py", "src//app/../a.py"])
def test_odd_spelling_never_widens_the_scope(spelling: str) -> None:
    for op in ("create", "modify"):
        assert not _guard().allowed(spelling, op), (spelling, op)


def test_patterns_are_compared_in_canonical_form_too() -> None:
    policy = ScopePolicy(always_forbidden=["./vault//**", "**//*.secret", "/etc/passwd", "../x"])
    g = ScopeGuard(ScopeContract(target_paths=["**"], allowed_operations=["modify"]), policy)
    assert not g.allowed("vault/k.txt", "modify") and not g.allowed("vault//k.txt", "modify")
    assert not g.allowed("a/b.secret", "modify")
    assert g.allowed("etc/passwd", "modify") and g.allowed("x", "modify")  # absolute / '..' patterns match nothing
    assert path_matches("src/a.py", "src//a.py") and path_matches("src//a.py", "./src/a.py")


def test_invalid_paths_are_refused_not_raised() -> None:
    g = ScopeGuard(ScopeContract(target_paths=["**"], allowed_new_paths=["**"], allowed_operations=["create", "modify"]))
    for raw in ["./", "././", "", "/abs", "a/../b", "a\x00b"]:
        ok, reason = g.decide(raw, "create")
        assert not ok and "repository-relative" in reason
