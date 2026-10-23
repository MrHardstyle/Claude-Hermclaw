# hermclaw-test

Test repository for Hermclaw.

## tasks_app – Task Manager

A small task manager web application using only the Python standard library and SQLite.

### Running

```bash
python -m tasks_app
```

The server listens on `127.0.0.1:8080` by default.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | TCP port to listen on |
| `TASKS_DB` | `tasks.db` | SQLite database file path |

### API

- `GET /api/tasks?status=all|open|done` → list tasks (JSON array)
- `POST /api/tasks` with `{"title": "..."}` → create task
- `PATCH /api/tasks/<id>` with `{"done": true|false}` → update task
- `DELETE /api/tasks/<id>` → delete task

### Tests

```bash
python -m pytest -q tests/
```
