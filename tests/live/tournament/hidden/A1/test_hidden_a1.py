"""A1: the F841 in count_active is fixed, behaviour and the identically built neighbour are unchanged."""

import ast
from pathlib import Path

from accounts.stats import count_active, count_admins, most_active, total_logins

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(__file__).resolve().parent / "_base"


def _functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _unused_locals(fn):
    stored = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    loaded = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return stored - loaded


def test_no_unused_local_left():
    for name, fn in _functions(ROOT / "accounts/stats.py").items():
        assert not _unused_locals(fn), f"{name}: {_unused_locals(fn)}"


def test_the_neighbour_and_the_other_functions_are_unchanged():
    now, before = _functions(ROOT / "accounts/stats.py"), _functions(BASE / "accounts/stats.py")
    for name in ("count_admins", "total_logins", "most_active"):
        assert ast.dump(now[name]) == ast.dump(before[name]), name


def test_behaviour_is_unchanged():
    users = [
        {"name": "x", "role": "admin", "active": True, "logins": 2},
        {"name": "y", "role": "member", "active": True, "logins": None},
        {"name": "z", "role": "member", "active": False},
    ]
    assert count_active(users) == 2
    assert count_active([]) == 0
    assert count_admins(users) == 1
    assert total_logins(users) == 2
    assert most_active(users) == "x"


def test_existing_tests_are_unchanged():
    assert (ROOT / "tests/test_stats.py").read_bytes() == (BASE / "tests/test_stats.py").read_bytes()
