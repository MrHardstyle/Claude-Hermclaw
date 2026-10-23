"""Hidden checks of task A10 (extend the inherited task manager: due dates, schema migration, search, sorting).

The fixture is the task manager of ladder A8 #2. ``_base/`` holds its original tests: their test functions must still
exist and pass. A failed ``security`` check disqualifies.
"""

import ast
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path.cwd()
BASE = Path(__file__).resolve().parent / "_base"
OLD_SCHEMA = "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    def __init__(self, db: Path):
        self.db, self.proc, self.port = db, None, 0

    def start(self) -> "App":
        self.port = free_port()
        env = {**os.environ, "PORT": str(self.port), "TASKS_DB": str(self.db)}
        self.proc = subprocess.Popen([sys.executable, "-m", "tasks_app"], cwd=ROOT, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return self
            except OSError:
                time.sleep(0.1)
        self.stop()
        pytest.fail("python -m tasks_app did not start: " + self.proc.stderr.read().decode(errors="replace")[-2000:])

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, res.read(), res.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read(), err.headers

    def json(self, method, path, body=None, expect=200):
        status, raw, _ = self.call(method, path, body)
        assert status == expect, (method, path, body, status, raw[:300])
        return json.loads(raw.decode() or "null")

    def add(self, title, **extra):
        return self.json("POST", "/api/tasks", {"title": title, **extra}, expect=201)

    def list(self, query=""):
        return self.json("GET", "/api/tasks" + (f"?{query}" if query else ""))

    def error(self, method, path, body=None, raw=None, expect=400):
        status, data, _ = self.call(method, path, body, raw)
        assert status == expect, (method, path, body if raw is None else raw, status, data[:300])
        payload = json.loads(data.decode())
        assert isinstance(payload, dict) and isinstance(payload.get("error"), str) and payload["error"], payload


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / "data" / "tasks.db")
    a.db.parent.mkdir()
    yield a.start()
    a.stop()


def q(**params):
    return urllib.parse.urlencode(params)


# ------------------------------------------------------------------------------------------- shape and creation
def test_tasks_carry_due_null_by_default(app):
    task = app.add("  Plain  ")
    assert task == {"id": task["id"], "title": "Plain", "done": False, "due": None}
    assert app.list() == [task]


def test_create_with_a_due_date(app):
    task = app.add("Taxes", due="2026-03-31")
    assert task["due"] == "2026-03-31" and task["done"] is False
    assert app.list()[0]["due"] == "2026-03-31"


@pytest.mark.parametrize("due", ["2026-02-30", "2026-13-01", "2026-2-3", "26-01-01", "2026-01-01T10:00:00", "tomorrow", "",
                                 20260101, True, ["2026-01-01"]])
def test_invalid_due_is_400(app, due):
    app.error("POST", "/api/tasks", {"title": "x", "due": due})
    assert app.list() == []


def test_leap_day_is_a_valid_date(app):
    assert app.add("Leap", due="2028-02-29")["due"] == "2028-02-29"


def test_unknown_field_on_create_is_400(app):
    app.error("POST", "/api/tasks", {"title": "x", "priority": 1})


# ------------------------------------------------------------------------------------------- partial updates
def test_patch_changes_only_the_given_fields(app):
    task = app.add("Draft", due="2026-05-01")
    changed = app.json("PATCH", f"/api/tasks/{task['id']}", {"title": "  Final  "})
    assert changed == {"id": task["id"], "title": "Final", "done": False, "due": "2026-05-01"}
    changed = app.json("PATCH", f"/api/tasks/{task['id']}", {"done": True})
    assert changed == {"id": task["id"], "title": "Final", "done": True, "due": "2026-05-01"}
    changed = app.json("PATCH", f"/api/tasks/{task['id']}", {"due": "2026-06-15", "done": False})
    assert changed == {"id": task["id"], "title": "Final", "done": False, "due": "2026-06-15"}
    assert app.list() == [changed]


def test_patch_due_null_removes_the_date(app):
    task = app.add("Dated", due="2026-01-10")
    assert app.json("PATCH", f"/api/tasks/{task['id']}", {"due": None})["due"] is None
    assert app.list()[0]["due"] is None


@pytest.mark.parametrize("body", [{}, {"title": ""}, {"title": "   "}, {"title": "x" * 201}, {"title": 5}, {"done": "yes"},
                                  {"due": "2026-02-30"}, {"colour": "red"}, {"title": "ok", "colour": "red"}])
def test_invalid_patch_is_400_and_changes_nothing(app, body):
    task = app.add("Keep", due="2026-01-01")
    app.error("PATCH", f"/api/tasks/{task['id']}", body)
    assert app.list() == [task]


def test_patch_of_an_unknown_task_is_404(app):
    app.error("PATCH", "/api/tasks/999", {"title": "x"}, expect=404)


# ------------------------------------------------------------------------------------------- search and sorting
def test_search_is_case_insensitive_and_combines_with_status(app):
    a = app.add("Buy MILK")
    b = app.add("milkshake recipe")
    app.add("Bread")
    app.json("PATCH", f"/api/tasks/{b['id']}", {"done": True})
    assert [t["id"] for t in app.list(q(q="milk"))] == [a["id"], b["id"]]
    assert [t["id"] for t in app.list(q(q="Milk", status="open"))] == [a["id"]]
    assert [t["id"] for t in app.list(q(q="milk", status="done"))] == [b["id"]]
    assert app.list(q(q="cheese")) == []


def test_search_text_is_literal(app):
    hundred = app.add("Save 100% of it")
    under = app.add("file_name check")
    app.add("Save 1000 now")
    app.add("one note")  # LIKE would match "e n" with "_"
    assert [t["id"] for t in app.list(q(q="100%"))] == [hundred["id"]]
    assert [t["id"] for t in app.list(q(q="e_n"))] == [under["id"]]


def test_sort_by_due_puts_tasks_without_date_last(app):
    t1 = app.add("no date 1")
    t2 = app.add("late", due="2026-12-01")
    t3 = app.add("early", due="2026-01-15")
    t4 = app.add("same early", due="2026-01-15")
    t5 = app.add("no date 2")
    assert [t["id"] for t in app.list(q(sort="due"))] == [t3["id"], t4["id"], t2["id"], t1["id"], t5["id"]]
    assert [t["id"] for t in app.list(q(sort="id"))] == [t1["id"], t2["id"], t3["id"], t4["id"], t5["id"]]
    app.json("PATCH", f"/api/tasks/{t3['id']}", {"done": True})
    assert [t["id"] for t in app.list(q(sort="due", status="open"))] == [t4["id"], t2["id"], t1["id"], t5["id"]]
    assert [t["id"] for t in app.list(q(sort="due", q="early"))] == [t3["id"], t4["id"]]


@pytest.mark.parametrize("query", ["sort=title", "sort=DUE", "status=later", "sort=due&status=x"])
def test_unknown_sort_or_status_is_400(app, query):
    app.error("GET", f"/api/tasks?{query}")


INJECTIONS = ["' OR 1=1 --", "%' OR '1'='1", "x'); DROP TABLE tasks; --", '" OR ""="']


def test_search_for_quotes_finds_nothing(app):
    app.add("alpha")
    app.add("beta")
    for text in INJECTIONS:
        assert app.list(q(q=text)) == [], text


def test_security_search_is_not_sql(app):
    app.add("alpha")
    app.add("beta")
    for text in INJECTIONS:
        status, body, _ = app.call("GET", "/api/tasks?" + q(q=text))
        assert status in (200, 400), (text, status, body[:200])
    assert [t["title"] for t in app.list()] == ["alpha", "beta"]
    con = sqlite3.connect(app.db)
    try:
        assert con.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
    finally:
        con.close()


# ------------------------------------------------------------------------------------------- migration
def _old_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(OLD_SCHEMA)
    con.executemany("INSERT INTO tasks (id, title, done) VALUES (?, ?, ?)", [(1, "old one", 0), (2, "old two", 1), (5, "old five", 0)])
    con.commit()
    con.close()


def test_an_existing_database_is_migrated_without_data_loss(tmp_path):
    db = tmp_path / "legacy" / "tasks.db"
    _old_database(db)
    app = App(db).start()
    try:
        assert app.list() == [
            {"id": 1, "title": "old one", "done": False, "due": None},
            {"id": 2, "title": "old two", "done": True, "due": None},
            {"id": 5, "title": "old five", "done": False, "due": None},
        ]
        assert app.json("PATCH", "/api/tasks/5", {"due": "2026-04-01"})["due"] == "2026-04-01"
        new = app.add("new one", due="2026-01-01")
        assert new["id"] > 5
        assert [t["id"] for t in app.list(q(sort="due"))] == [new["id"], 5, 1, 2]
    finally:
        app.stop()
    again = App(db).start()  # the migration runs again on an already migrated file
    try:
        assert [(t["id"], t["due"]) for t in again.list()] == [(1, None), (2, None), (5, "2026-04-01"), (new["id"], "2026-01-01")]
    finally:
        again.stop()


# ------------------------------------------------------------------------------------------- page, docs, tests
def _page(app) -> str:
    status, body, _ = app.call("GET", "/")
    assert status == 200
    html = body.decode("utf-8", errors="replace")
    for src in re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html):
        if not re.match(r"(?i)[a-z]+:|//|#", src):
            s, extra, _ = app.call("GET", "/" + src.lstrip("./"))
            if s == 200:
                html += "\n" + extra.decode("utf-8", errors="replace")
    return html


def test_the_page_shows_and_sets_the_due_date(app):
    page = _page(app)
    assert re.search(r"""type\s*=\s*["']date["']""", page), "no date input on the page"
    assert "due" in page, "the page does not use the due field"
    assert not re.search(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", page), "external resource in the page"


def test_the_readme_describes_the_new_api():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for word in ("due", "sort", "q="):
        assert word in readme, f"README does not mention {word!r}"


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")}


def test_the_existing_tests_are_kept():
    for base in sorted((BASE / "tests").glob("test_*.py")):
        current = ROOT / "tests" / base.name
        assert current.is_file(), f"{base.name} was removed"
        missing = _test_names(base) - _test_names(current)
        assert not missing, f"{base.name} lost {sorted(missing)}"


def test_own_tests_pass_and_cover_the_new_features():
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                         capture_output=True, text=True, timeout=300, cwd=ROOT, check=False)
    assert res.returncode == 0, res.stdout[-3000:]
    base = sum(len(_test_names(p)) for p in (BASE / "tests").glob("test_*.py"))
    now = sum(len(_test_names(p)) for p in (ROOT / "tests").rglob("test_*.py"))
    assert now >= base + 5, f"only {now - base} new test functions"
    text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "tests").rglob("test_*.py"))
    assert "due" in text and re.search(r"ALTER TABLE|migrat", text, re.I), "no test for due dates and the migration"
