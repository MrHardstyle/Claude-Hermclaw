"""Tests for the database layer of tasks_app."""
import os
import pytest

from tasks_app import db


@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary database file path."""
    path = str(tmp_path / "tasks.db")
    os.environ["TASKS_DB"] = path
    return path


@pytest.fixture
def conn(db_path):
    """Provide a database connection with the tasks table created."""
    c = db.get_connection(db_path)
    yield c
    c.close()


def test_get_connection_creates_table(db_path):
    """get_connection should create the tasks table if it doesn't exist."""
    c = db.get_connection(db_path)
    assert os.path.exists(db_path)
    cursor = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
    assert cursor.fetchone() is not None
    c.close()


def test_create_task(db_path):
    """create_task should insert a task and return it with an id."""
    task = db.create_task("Test task", db_path)
    assert task["title"] == "Test task"
    assert task["done"] is False
    assert task["id"] is not None
    # Verify persistence
    c = db.get_connection(db_path)
    cursor = c.execute("SELECT id, title, done FROM tasks WHERE id=?", (task["id"],))
    row = cursor.fetchone()
    c.close()
    assert row is not None
    assert row[0] == task["id"]
    assert row[1] == "Test task"
    assert row[2] == 0


def test_create_task_strips_title(db_path):
    """create_task should strip leading/trailing whitespace from the title."""
    task = db.create_task("  Stripped task  ", db_path)
    assert task["title"] == "Stripped task"


def test_get_tasks_all(conn):
    """get_tasks with status='all' should return all tasks."""
    db.create_task("Task 1", None)
    db.create_task("Task 2", None)
    tasks = db.get_tasks("all", None)
    assert len(tasks) == 2
    assert tasks[0]["id"] < tasks[1]["id"]


def test_get_tasks_open(conn):
    """get_tasks with status='open' should return only undone tasks."""
    db.create_task("Open task", None)
    task = db.create_task("Done task", None)
    db.update_task(task["id"], True, None)
    tasks = db.get_tasks("open", None)
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Open task"


def test_get_tasks_done(conn):
    """get_tasks with status='done' should return only done tasks."""
    db.create_task("Open task", None)
    task = db.create_task("Done task", None)
    db.update_task(task["id"], True, None)
    tasks = db.get_tasks("done", None)
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Done task"


def test_update_task(conn):
    """update_task should change the done status of a task."""
    task = db.create_task("Updatable task", None)
    updated = db.update_task(task["id"], True, None)
    assert updated["done"] is True
    # Verify persistence
    c = db.get_connection(None)
    cursor = c.execute("SELECT done FROM tasks WHERE id=?", (task["id"],))
    row = cursor.fetchone()
    c.close()
    assert row[0] == 1


def test_update_task_nonexistent(db_path):
    """update_task for a nonexistent id should return None."""
    result = db.update_task(99999, True, db_path)
    assert result is None


def test_delete_task(conn):
    """delete_task should remove a task and return True."""
    task = db.create_task("Deletable task", None)
    result = db.delete_task(task["id"], None)
    assert result is True
    # Verify deletion
    c = db.get_connection(None)
    cursor = c.execute("SELECT id FROM tasks WHERE id=?", (task["id"],))
    assert cursor.fetchone() is None
    c.close()


def test_delete_task_nonexistent(db_path):
    """delete_task for a nonexistent id should return False."""
    result = db.delete_task(99999, db_path)
    assert result is False
