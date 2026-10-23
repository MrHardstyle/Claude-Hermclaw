"""HTTP application for the task manager."""
import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from tasks_app.db import get_connection, get_tasks, create_task, update_task, delete_task


def create_app():
    """Create and return the request handler class."""

    class TaskHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress default logging

        def _send_json(self, status_code, data):
            body = json.dumps(data).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status_code, message):
            self._send_json(status_code, {"error": message})

        def _read_body(self):
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                return None
            return self.rfile.read(content_length)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._serve_index()
            elif parsed.path == "/api/tasks":
                self._get_tasks(parsed)
            else:
                self._send_error(404, "Not found")

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/tasks":
                self._create_task()
            else:
                self._send_error(404, "Not found")

        def do_PATCH(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/tasks/"):
                try:
                    task_id = int(parsed.path[len("/api/tasks/"):])
                except ValueError:
                    self._send_error(404, "Not found")
                    return
                self._update_task(task_id)
            else:
                self._send_error(404, "Not found")

        def do_DELETE(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/tasks/"):
                try:
                    task_id = int(parsed.path[len("/api/tasks/"):])
                except ValueError:
                    self._send_error(404, "Not found")
                    return
                self._delete_task(task_id)
            else:
                self._send_error(404, "Not found")

        def _serve_index(self):
            db_path = os.environ.get("TASKS_DB", "tasks.db")
            conn = get_connection(db_path)
            conn.close()
            html_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    html = f.read()
            except FileNotFoundError:
                html = "<html><body><h1>Tasks</h1><p>Template not found.</p></body></html>"
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _get_tasks(self, parsed):
            params = parse_qs(parsed.query)
            status = params.get("status", ["all"])[0]
            if status not in ("all", "open", "done"):
                self._send_error(400, "Invalid status")
                return
            db_path = os.environ.get("TASKS_DB", "tasks.db")
            tasks = get_tasks(status=status, db_path=db_path)
            self._send_json(200, tasks)

        def _create_task(self):
            raw = self._read_body()
            if raw is None:
                self._send_error(400, "Missing body")
                return
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                self._send_error(400, "Invalid JSON")
                return
            if not isinstance(data, dict):
                self._send_error(400, "body must be a JSON object")
                return
            title = data.get("title")
            if title is None or not isinstance(title, str):
                self._send_error(400, "title is required and must be a string")
                return
            title = title.strip()
            if not title:
                self._send_error(400, "title must not be empty")
                return
            if len(title) > 200:
                self._send_error(400, "title must be at most 200 characters")
                return
            db_path = os.environ.get("TASKS_DB", "tasks.db")
            task = create_task(title, db_path=db_path)
            self._send_json(201, task)

        def _update_task(self, task_id):
            raw = self._read_body()
            if raw is None:
                self._send_error(400, "Missing body")
                return
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                self._send_error(400, "Invalid JSON")
                return
            if not isinstance(data, dict):
                self._send_error(400, "body must be a JSON object")
                return
            done = data.get("done")
            if done is None or not isinstance(done, bool):
                self._send_error(400, "done is required and must be a boolean")
                return
            db_path = os.environ.get("TASKS_DB", "tasks.db")
            task = update_task(task_id, done, db_path=db_path)
            if task is None:
                self._send_error(404, "Not found")
                return
            self._send_json(200, task)

        def _delete_task(self, task_id):
            db_path = os.environ.get("TASKS_DB", "tasks.db")
            deleted = delete_task(task_id, db_path=db_path)
            if not deleted:
                self._send_error(404, "Not found")
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return TaskHandler


def run(port=None):
    """Start the server on the given port."""
    if port is None:
        port = int(os.environ.get("PORT", 8080))
    app = create_app()
    server = HTTPServer(("127.0.0.1", port), app)
    print(f"Server running on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
