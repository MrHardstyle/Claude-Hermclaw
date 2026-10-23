"""SQLite database access for the task manager."""
import sqlite3
import os


def get_db_path():
    """Return the database file path from the TASKS_DB environment variable."""
    return os.environ.get("TASKS_DB", "tasks.db")


def get_connection(db_path=None):
    """Return a new sqlite3 connection. Creates the table if it does not exist."""
    if db_path is None:
        db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "title TEXT NOT NULL, "
        "done INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.commit()
    return conn


def get_tasks(status="all", db_path=None):
    """Return a list of task dicts, optionally filtered by status."""
    conn = get_connection(db_path)
    if status == "open":
        cursor = conn.execute("SELECT id, title, done FROM tasks WHERE done = 0 ORDER BY id")
    elif status == "done":
        cursor = conn.execute("SELECT id, title, done FROM tasks WHERE done = 1 ORDER BY id")
    else:
        cursor = conn.execute("SELECT id, title, done FROM tasks ORDER BY id")
    tasks = []
    for row in cursor:
        tasks.append({"id": row[0], "title": row[1], "done": bool(row[2])})
    conn.close()
    return tasks


def create_task(title, db_path=None):
    """Create a new task with the given title. Returns the new task dict."""
    conn = get_connection(db_path)
    cursor = conn.execute("INSERT INTO tasks (title, done) VALUES (?, 0)", (title.strip(),))
    conn.commit()
    new_id = cursor.lastrowid
    task = {"id": new_id, "title": title.strip(), "done": False}
    conn.close()
    return task


def update_task(task_id, done, db_path=None):
    """Update the done status of a task. Returns the updated task dict or None."""
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT id, title, done FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return None
    conn.execute("UPDATE tasks SET done = ? WHERE id = ?", (int(done), task_id))
    conn.commit()
    task = {"id": row[0], "title": row[1], "done": bool(done)}
    conn.close()
    return task


def delete_task(task_id, db_path=None):
    """Delete a task by id. Returns True if deleted, False if not found."""
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return False
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return True
