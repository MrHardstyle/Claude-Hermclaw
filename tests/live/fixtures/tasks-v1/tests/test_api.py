"""Tests for the HTTP API of tasks_app."""
import json
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import pytest

from http.server import ThreadingHTTPServer

from tasks_app.app import create_app


@pytest.fixture
def app_server(monkeypatch, tmp_path):
    """Start an in-process HTTP server with a temporary database."""
    db_path = str(tmp_path / "tasks.db")
    monkeypatch.setenv("TASKS_DB", db_path)
    monkeypatch.setenv("PORT", "0")

    handler = create_app()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base_url = f"http://127.0.0.1:{port}"
    yield base_url, db_path
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=5)


def _request(base_url, path, method="GET", data=None):
    """Helper to make an HTTP request and return (status, body_dict_or_None)."""
    url = base_url + path
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req)
        resp_body = resp.read().decode("utf-8")
        return resp.status, json.loads(resp_body) if resp_body else None
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode("utf-8")
        return e.code, json.loads(resp_body) if resp_body else None


class TestGetTasks:
    def test_list_all_empty(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks")
        assert status == 200
        assert body == []

    def test_list_open(self, app_server):
        base_url, _ = app_server
        _request(base_url, "/api/tasks", "POST", {"title": "Task 1"})
        status, body = _request(base_url, "/api/tasks?status=open")
        assert status == 200
        assert len(body) == 1
        assert body[0]["done"] is False

    def test_list_done(self, app_server):
        base_url, db_path = app_server
        _request(base_url, "/api/tasks", "POST", {"title": "Task 1"})
        _request(base_url, "/api/tasks/1", "PATCH", {"done": True})
        status, body = _request(base_url, "/api/tasks?status=done")
        assert status == 200
        assert len(body) == 1
        assert body[0]["done"] is True

    def test_list_invalid_status(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks?status=invalid")
        assert status == 400
        assert "error" in body


class TestCreateTask:
    def test_create_task(self, app_server):
        base_url, db_path = app_server
        status, body = _request(base_url, "/api/tasks", "POST", {"title": "New task"})
        assert status == 201
        assert body["title"] == "New task"
        assert body["done"] is False
        assert body["id"] is not None
        # Verify persistence
        conn = __import__("sqlite3").connect(db_path)
        row = conn.execute("SELECT id, title, done FROM tasks WHERE id=?", (body["id"],)).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "New task"

    def test_create_task_strips_title(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks", "POST", {"title": "  spaced  "})
        assert status == 201
        assert body["title"] == "spaced"

    def test_create_empty_title(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks", "POST", {"title": ""})
        assert status == 400
        assert "error" in body

    def test_create_missing_title(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks", "POST", {})
        assert status == 400
        assert "error" in body

    def test_create_title_too_long(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks", "POST", {"title": "a" * 201})
        assert status == 400
        assert "error" in body

    def test_create_invalid_json(self, app_server):
        base_url, _ = app_server
        url = base_url + "/api/tasks"
        req = urllib.request.Request(url, data=b"not json", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
            assert False, "Should have raised"
        except urllib.error.HTTPError as e:
            assert e.code == 400

    def test_create_array_body(self, app_server):
        base_url, _ = app_server
        url = base_url + "/api/tasks"
        req = urllib.request.Request(url, data=b"[]", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
            assert False, "Should have raised"
        except urllib.error.HTTPError as e:
            assert e.code == 400


class TestUpdateTask:
    def test_update_task(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks", "POST", {"title": "Task 1"})
        task_id = body["id"]
        status, body = _request(base_url, f"/api/tasks/{task_id}", "PATCH", {"done": True})
        assert status == 200
        assert body["done"] is True

    def test_update_nonexistent(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks/999", "PATCH", {"done": True})
        assert status == 404
        assert "error" in body

    def test_update_invalid_id(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks/abc", "PATCH", {"done": True})
        assert status == 404
        assert "error" in body

    def test_update_invalid_done_type(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks", "POST", {"title": "Task 1"})
        task_id = body["id"]
        status, body = _request(base_url, f"/api/tasks/{task_id}", "PATCH", {"done": "yes"})
        assert status == 400
        assert "error" in body


class TestDeleteTask:
    def test_delete_task(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks", "POST", {"title": "Task 1"})
        task_id = body["id"]
        status, _ = _request(base_url, f"/api/tasks/{task_id}", "DELETE")
        assert status == 204
        # Verify gone
        status, _ = _request(base_url, f"/api/tasks/{task_id}")
        assert status == 404

    def test_delete_nonexistent(self, app_server):
        base_url, _ = app_server
        status, body = _request(base_url, "/api/tasks/999", "DELETE")
        assert status == 404
        assert "error" in body


class TestRootPage:
    def test_root_returns_html(self, app_server):
        base_url, _ = app_server
        url = base_url + "/"
        resp = urllib.request.urlopen(url)
        assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
        body = resp.read().decode("utf-8")
        assert "<html" in body
        assert "Tasks" in body


class TestEntryPoint:
    def test_python_m_starts(self, monkeypatch, tmp_path):
        """Verify python -m tasks_app starts and serves."""
        db_path = str(tmp_path / "tasks.db")
        monkeypatch.setenv("TASKS_DB", db_path)
        monkeypatch.setenv("PORT", "8765")
        proc = subprocess.Popen(
            [sys.executable, "-m", "tasks_app"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait for server to be ready
        ready = False
        for _ in range(30):
            time.sleep(0.2)
            try:
                urllib.request.urlopen("http://127.0.0.1:8765/")
                ready = True
                break
            except Exception:
                pass
        if not ready:
            proc.terminate()
            proc.wait(timeout=5)
            assert False, "Server did not start in time"
        proc.terminate()
        proc.wait(timeout=5)
