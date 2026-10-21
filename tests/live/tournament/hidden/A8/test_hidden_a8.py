"""Hidden checks of task A8 (task manager web application with SQLite). A failed ``security`` check disqualifies."""

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path.cwd()


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
        assert status == expect, (method, path, status, raw[:300])
        return json.loads(raw.decode() or "null")

    def add(self, title):
        return self.json("POST", "/api/tasks", {"title": title}, expect=201)


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / "data" / "tasks.db")
    a.db.parent.mkdir()
    yield a.start()
    a.stop()


def test_create_list_and_shape(app):
    task = app.add("  Milch kaufen  ")
    assert task["title"] == "Milch kaufen" and task["done"] is False and isinstance(task["id"], int)
    assert app.json("GET", "/api/tasks") == [task]


def test_filters(app):
    a, b, c = app.add("a"), app.add("b"), app.add("c")
    done = app.json("PATCH", f"/api/tasks/{b['id']}", {"done": True})
    assert done["done"] is True and done["id"] == b["id"] and done["title"] == "b"
    ids = lambda rows: [r["id"] for r in rows]  # noqa: E731
    assert ids(app.json("GET", "/api/tasks?status=all")) == [a["id"], b["id"], c["id"]]
    assert ids(app.json("GET", "/api/tasks")) == [a["id"], b["id"], c["id"]]
    assert ids(app.json("GET", "/api/tasks?status=open")) == [a["id"], c["id"]]
    assert ids(app.json("GET", "/api/tasks?status=done")) == [b["id"]]
    app.json("PATCH", f"/api/tasks/{b['id']}", {"done": False})
    assert ids(app.json("GET", "/api/tasks?status=done")) == []


def test_delete(app):
    a, b = app.add("a"), app.add("b")
    assert app.call("DELETE", f"/api/tasks/{a['id']}")[0] == 204
    assert [r["id"] for r in app.json("GET", "/api/tasks")] == [b["id"]]


def test_data_survives_a_restart_in_the_configured_file(app):
    t = app.add("bleibt")
    app.json("PATCH", f"/api/tasks/{t['id']}", {"done": True})
    app.stop()
    assert app.db.exists(), "the database must be the file named by TASKS_DB"
    app.start()
    assert app.json("GET", "/api/tasks") == [{**t, "done": True}]


@pytest.mark.parametrize(
    "raw",
    [b"{kaputt", b"[]", b"{}", b'{"title": ""}', b'{"title": "   "}', b'{"title": 7}', ('{"title": "%s"}' % ("t" * 201)).encode()],
)
def test_invalid_new_task_is_400(app, raw):
    status, body, _ = app.call("POST", "/api/tasks", raw=raw)
    assert status == 400 and isinstance(json.loads(body)["error"], str)
    assert app.json("GET", "/api/tasks") == []


def test_title_of_200_characters_is_accepted(app):
    assert app.add("t" * 200)["title"] == "t" * 200


@pytest.mark.parametrize("raw", [b'{"done": "yes"}', b'{"done": 1}', b"{}", b"{kaputt"])
def test_invalid_done_is_400(app, raw):
    t = app.add("x")
    assert app.call("PATCH", f"/api/tasks/{t['id']}", raw=raw)[0] == 400
    assert app.json("GET", "/api/tasks")[0]["done"] is False


def test_unknown_status_filter_is_400(app):
    assert app.call("GET", "/api/tasks?status=bald")[0] == 400


@pytest.mark.parametrize(("method", "body"), [("PATCH", {"done": True}), ("DELETE", None)])
def test_unknown_task_is_404(app, method, body):
    for path in ("/api/tasks/987654", "/api/tasks/abc"):
        status, raw, _ = app.call(method, path, body)
        assert status == 404, (method, path, status)
        assert isinstance(json.loads(raw)["error"], str)


def test_security_titles_are_stored_as_data_not_sql(app):
    evil = ["x'); DROP TABLE tasks; --", "a' OR '1'='1", 'b"; DELETE FROM tasks; --']
    for title in evil:
        assert app.add(title)["title"] == title
    assert [r["title"] for r in app.json("GET", "/api/tasks")] == evil
    with sqlite3.connect(app.db) as con:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert tables, "no table in TASKS_DB"


def test_security_filter_parameter_is_not_sql(app):
    app.add("a")
    for status in ("open' OR '1'='1", "done;--"):
        assert app.call("GET", "/api/tasks?status=" + urllib.request.quote(status))[0] == 400


def _page(app) -> str:
    status, raw, headers = app.call("GET", "/")
    assert status == 200 and "text/html" in headers.get("Content-Type", "")
    html = raw.decode("utf-8", errors="replace")
    for src in re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)""", html):  # inline the page's own assets
        if not re.match(r"(?i)[a-z]+:|//|#", src):
            s, body, _ = app.call("GET", "/" + src.lstrip("./"))
            if s == 200:
                html += "\n" + body.decode("utf-8", errors="replace")
    return html


def test_the_page_uses_the_api_without_external_resources(app):
    page = _page(app)
    assert "/api/tasks" in page, "the page does not call the JSON API"
    assert not re.search(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", page), "external resource in the page"
    assert not re.search(r"""@import\s+(?:url\()?["']?(?:https?:)?//""", page), "external stylesheet import"


def test_own_tests_pass():
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                         capture_output=True, text=True, timeout=300, cwd=ROOT)
    assert res.returncode == 0, res.stdout[-3000:]
    passed = int(res.stdout.rsplit(" passed", 1)[0].split()[-1]) if " passed" in res.stdout else 0
    assert passed >= 6, f"only {passed} own tests"
