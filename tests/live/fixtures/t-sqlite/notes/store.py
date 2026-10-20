"""Notizen in einer SQLite-Datei."""

import sqlite3


class NoteStore:
    def __init__(self, path):
        self.path = path
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS notes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)"
            )

    def _connect(self):
        return sqlite3.connect(self.path)

    def add(self, text):
        text = (text or "").strip()
        if not text:
            raise ValueError("text must not be empty")
        with self._connect() as db:
            cur = db.execute("INSERT INTO notes (text) VALUES (?)", (text,))
            return cur.lastrowid

    def all(self, only_open=False):
        query = "SELECT id, text, done FROM notes"
        if only_open:
            query += " WHERE done = 0"
        with self._connect() as db:
            rows = db.execute(query + " ORDER BY id").fetchall()
        return [{"id": r[0], "text": r[1], "done": bool(r[2])} for r in rows]

    def mark_done(self, note_id):
        with self._connect() as db:
            cur = db.execute("UPDATE notes SET done = 1 WHERE id = ?", (note_id,))
            return cur.rowcount == 1

    def delete(self, note_id):
        with self._connect() as db:
            cur = db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            return cur.rowcount == 1
