import pytest

from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import ScopePolicy
from hermclaw.core.errors import ScopeViolation
from hermclaw.scope.guard import ScopeGuard, path_matches


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
def test_glob_semantics(path, pattern, ok):
    assert path_matches(path, pattern) is ok


def test_guard_decisions():
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
