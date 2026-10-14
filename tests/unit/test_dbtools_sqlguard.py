"""Static read-only check of db_query_readonly (P27 27.2, first line of defence; the database enforces on top)."""

from __future__ import annotations

import pytest

from hermclaw.dbtools.errors import DB_QUERY_REJECTED, DbPolicyError
from hermclaw.dbtools.sqlguard import check_readonly, tokenize

ACCEPTED_PG = [
    ("SELECT 1", "SELECT", True),
    ("select * from customers where email = 'x;y' ;", "SELECT", True),
    ("WITH c AS (SELECT id FROM customers) SELECT count(*) FROM c", "WITH", True),
    ("WITH RECURSIVE t(n) AS (VALUES (1) UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT * FROM t", "WITH", True),
    ("VALUES (1, 'a'), (2, 'b')", "VALUES", True),
    ("TABLE customers", "TABLE", True),
    ("(SELECT 1) UNION (SELECT 2)", "SELECT", True),
    ("EXPLAIN SELECT * FROM orders", "EXPLAIN", False),
    ("EXPLAIN (FORMAT JSON, VERBOSE) SELECT * FROM orders", "EXPLAIN", False),
    ("SHOW search_path", "SHOW", False),
    ("SELECT $$ DELETE FROM x; $$ AS txt", "SELECT", True),
    ("SELECT $tag$ DROP TABLE x $tag$", "SELECT", True),
    ("SELECT E'it\\'s ; INSERT' AS s", "SELECT", True),
    ('SELECT "delete", t.update, "INSERT" FROM t', "SELECT", True),
    ("SELECT update_count, inserted_at FROM stats", "SELECT", True),
    ("SELECT 1 /* ; DROP TABLE x; /* nested */ still comment */", "SELECT", True),
    ("SELECT 1 -- ; DROP TABLE x\n", "SELECT", True),
    ("SELECT * FROM t WHERE id = %(id)s AND name = %s", "SELECT", True),
    ("SELECT pg_sleep(0.1)", "SELECT", True),
    ("SELECT U&'d\\0061t' AS unicode_string", "SELECT", True),
]

REJECTED_PG = [
    ("INSERT INTO t VALUES (1)", "INSERT"),
    ("UPDATE t SET a = 1", "UPDATE"),
    ("DELETE FROM t", "DELETE"),
    ("DROP TABLE t", "DROP"),
    ("CREATE TABLE x (a int)", "CREATE"),
    ("ALTER TABLE t ADD COLUMN b int", "ALTER"),
    ("TRUNCATE t", "TRUNCATE"),
    ("GRANT SELECT ON t TO public", "GRANT"),
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "DELETE"),
    ("WITH u AS (UPDATE t SET a = 1 RETURNING *) SELECT 1", "UPDATE"),
    ("WITH i AS (INSERT INTO t DEFAULT VALUES RETURNING *) SELECT 1", "INSERT"),
    ("SELECT 1; SELECT 2", "one statement"),
    ("SELECT 1; DELETE FROM t", "one statement"),
    ("SELECT 1;;", "one statement"),
    ("SELECT * INTO newtable FROM t", "INTO"),
    ("SELECT * FROM t FOR UPDATE", "row-locking"),
    ("SELECT * FROM t FOR NO KEY UPDATE", "row-locking"),
    ("SELECT * FROM t FOR SHARE", "row-locking"),
    ("COPY t TO '/tmp/x'", "COPY"),
    ("COPY (SELECT 1) TO PROGRAM 'id'", "COPY"),
    ("EXPLAIN ANALYZE SELECT 1", "EXPLAIN ANALYZE"),
    ("EXPLAIN (ANALYZE, BUFFERS) SELECT 1", "EXPLAIN ANALYZE"),
    ("EXPLAIN DELETE FROM t", "DELETE"),
    ("SELECT pg_terminate_backend(123)", "pg_terminate_backend"),
    ("SELECT pg_catalog.pg_terminate_backend(123)", "pg_terminate_backend"),
    ('SELECT "pg_terminate_backend"(123)', "pg_terminate_backend"),
    ("SELECT pg_cancel_backend /* hidden */ (1)", "pg_cancel_backend"),
    ("SELECT lo_import('/etc/passwd')", "lo_import"),
    ("SELECT * FROM dblink('host=x', 'DELETE FROM t') AS t(a int)", "dblink"),
    ("SELECT dblink_exec('host=x', 'DROP TABLE t')", "dblink"),
    ("SELECT set_config('transaction_read_only', 'off', true)", "set_config"),
    ("SELECT nextval('customers_id_seq')", "nextval"),
    ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
    ("SELECT pg_ls_dir('.')", "pg_ls_dir"),
    ("SELECT pg_advisory_lock(1)", "pg_advisory_lock"),
    ("SELECT pg_stat_statements_reset()", "reset"),
    ("SELECT query_to_xml('delete from t', true, true, '')", "query_to_xml"),
    ('SELECT U&"\\0070g_terminate_backend"(1)', "Unicode-escaped"),
    ("SET search_path TO evil", "not read-only"),
    ("DO $$ BEGIN DELETE FROM t; END $$", "not read-only"),
    ("DO $$ BEGIN PERFORM 1; END $$", "not read-only"),
    ("CALL proc()", "CALL"),
    ("LOCK TABLE t", "not read-only"),
    ("VACUUM t", "VACUUM"),
    ("BEGIN", "not read-only"),
    ("SELECT 'unterminated", "unterminated"),
    ("SELECT $$ unterminated", "unterminated"),
    ("SELECT 1 /* unterminated", "unterminated"),
    ("", "empty"),
    ("   ;  ", "empty"),
    ("-- only a comment", "empty"),
    ("SELECT 1\x00", "NUL"),
    ("SHOW a b c d e", "SHOW"),
]


@pytest.mark.parametrize(("sql", "statement", "cursor"), ACCEPTED_PG)
def test_accepts_read_statements(sql: str, statement: str, cursor: bool) -> None:
    checked = check_readonly(sql, "postgresql")
    assert checked.statement == statement
    assert checked.server_cursor is cursor
    assert not checked.sql.rstrip().endswith(";"), "the trailing ';' is cut off (DECLARE … CURSOR FOR <sql>)"


@pytest.mark.parametrize(("sql", "reason"), REJECTED_PG)
def test_rejects_everything_else(sql: str, reason: str) -> None:
    with pytest.raises(DbPolicyError) as exc:
        check_readonly(sql, "postgresql")
    assert exc.value.code == DB_QUERY_REJECTED
    assert reason.lower() in exc.value.message.lower(), exc.value.message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM books",
        "PRAGMA table_info(books)",
        "PRAGMA main.index_list(books)",
        "PRAGMA user_version",
        "PRAGMA foreign_key_list('books')",
        "EXPLAIN QUERY PLAN SELECT * FROM books",
        "SELECT [weird name], `other` FROM t",
        "SELECT * FROM t WHERE a = ? AND b = :name AND c = ?2",
    ],
)
def test_sqlite_accepts(sql: str) -> None:
    check_readonly(sql, "sqlite")


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("PRAGMA journal_mode = WAL", "assignments"),
        ("PRAGMA writable_schema = 1", "assignments"),
        ("PRAGMA writable_schema", "read-only PRAGMAs"),
        ("ATTACH DATABASE '/tmp/x.db' AS x", "ATTACH"),
        ("REPLACE INTO t VALUES (1)", "REPLACE"),
        ("SELECT load_extension('/tmp/evil.so')", "load_extension"),
        ("SELECT writefile('/tmp/x', 'data')", "writefile"),
        ("VACUUM INTO '/tmp/copy.db'", "VACUUM"),
        ("TABLE books", "not read-only"),
        ("SELECT * FROM [unterminated", "unterminated"),
    ],
)
def test_sqlite_rejects(sql: str, reason: str) -> None:
    with pytest.raises(DbPolicyError) as exc:
        check_readonly(sql, "sqlite")
    assert reason.lower() in exc.value.message.lower()


def test_sqlite_replace_function_is_fine() -> None:
    check_readonly("SELECT replace(title, 'a', 'b') FROM books", "sqlite")


def test_tokenizer_keeps_literals_and_positions() -> None:
    sql = "SELECT 'a''b', \"Q\"\"x\", $f$;$f$ -- c\n/* d */ FROM t;"
    tokens = tokenize(sql, "postgresql")
    kinds = [(t.kind, t.value) for t in tokens]
    assert ("string", "'a''b'") in kinds
    assert ("qident", 'Q"x') in kinds
    assert ("string", "$f$;$f$") in kinds
    assert kinds[-1] == ("punct", ";")
    assert all(sql[t.start : t.end] for t in tokens)
