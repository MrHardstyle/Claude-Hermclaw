"""Static read-only check of one SQL statement (P27 27.2, first line of defence).

The statement is tokenised with the lexical rules of the target dialect (strings, ``E''`` escape strings, dollar
quotes, quoted identifiers, nested block comments in PostgreSQL, non-nested in SQLite). It is accepted only when

- it is exactly one statement (an optional trailing ``;``),
- it starts with ``SELECT``/``WITH``/``VALUES``/``TABLE`` (PostgreSQL: also ``SHOW``; SQLite: allow-listed read
  ``PRAGMA``s without assignment) or ``EXPLAIN`` of such a statement without ``ANALYZE``,
- no data-modifying keyword appears anywhere (``INSERT``/``UPDATE``/``DELETE``/``MERGE``/``INTO``/``COPY`` – this
  covers data-modifying CTEs and ``SELECT … INTO``), no row-locking clause (``FOR UPDATE/SHARE``),
- no function of the deny-list is called (``pg_terminate_backend``, ``lo_import``, ``dblink*``, ``set_config`` …).

Anything the lexer cannot classify (unterminated literal/comment) is rejected. The database enforces read-only on top
(``SET TRANSACTION READ ONLY`` / SQLite ``mode=ro`` + authorizer), so this check only has to be *conservative*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from hermclaw.dbtools.errors import DB_QUERY_REJECTED, DbPolicyError

Dialect = Literal["postgresql", "sqlite"]
TokenKind = Literal["word", "qident", "string", "number", "param", "punct", "op"]

READ_STARTS = frozenset({"SELECT", "WITH", "VALUES", "TABLE"})
FORBIDDEN_ANYWHERE = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "UPSERT",
        "INTO",
        "COPY",
        "TRUNCATE",
        "DROP",
        "ALTER",
        "CREATE",
        "GRANT",
        "REVOKE",
        "ATTACH",
        "DETACH",
        "VACUUM",
        "REINDEX",
        "CLUSTER",
        "CALL",
        "EXECUTE",
        "PREPARE",
        "DEALLOCATE",
        "LISTEN",
        "NOTIFY",
        "UNLISTEN",
        "DECLARE",
        "SAVEPOINT",
        "ROLLBACK",
        "COMMIT",
    }
)
EXPLAIN_FORBIDDEN = frozenset({"ANALYZE", "ANALYSE"})
LOCK_AFTER_FOR = frozenset({"UPDATE", "SHARE", "KEY", "NO"})
# functions with side effects or file/network/server access; matched on the unqualified, lower-cased name
DENIED_FUNCTIONS = frozenset(
    {
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_promote",
        "pg_switch_wal",
        "pg_switch_xlog",
        "pg_create_restore_point",
        "pg_start_backup",
        "pg_stop_backup",
        "pg_backup_start",
        "pg_backup_stop",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_stat_file",
        "pg_file_write",
        "pg_file_unlink",
        "pg_file_rename",
        "pg_file_sync",
        "pg_logdir_ls",
        "pg_notify",
        "pg_logical_emit_message",
        "pg_import_system_collations",
        "pg_wal_replay_pause",
        "pg_wal_replay_resume",
        "pg_drop_replication_slot",
        "pg_copy_physical_replication_slot",
        "pg_copy_logical_replication_slot",
        "pg_log_backend_memory_contexts",
        "pg_export_snapshot",
        "set_config",
        "nextval",
        "setval",
        "lo_import",
        "lo_export",
        "lo_unlink",
        "lo_create",
        "lo_creat",
        "lo_open",
        "lo_put",
        "lo_from_bytea",
        "lo_truncate",
        "lo_truncate64",
        "lowrite",
        "loread",
        "query_to_xml",
        "query_to_xml_and_xmlschema",
        "query_to_xmlschema",
        "cursor_to_xml",
        "table_to_xml",
        "database_to_xml",
        "schema_to_xml",
        "txid_current",
        "pg_current_xact_id",
        # SQLite
        "load_extension",
        "writefile",
        "readfile",
        "edit",
        "fts3_tokenizer",
        "sqlite_dbpage",
    }
)
DENIED_FUNCTION_PREFIXES = (
    "dblink",
    "pg_ls_",
    "pg_advisory_",
    "pg_try_advisory_",
    "pg_create_",
    "pg_replication_origin_",
    "pg_logical_slot_",
    "pg_replication_slot_advance",
    "pg_sync_replication_slots",
    "pg_stat_reset",
    "brin_summarize_",
    "brin_desummarize_",
    "gin_clean_pending_list",
)
# statistics/state resets of extensions (pg_stat_statements_reset, pg_stat_reset_shared …)
_RESET_RE = re.compile(r"(?:^|_)reset(?:_|$)")
# read-only SQLite PRAGMAs (no '=' assignment allowed)
SQLITE_READ_PRAGMAS = frozenset(
    {
        "table_info",
        "table_xinfo",
        "table_list",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
        "foreign_key_check",
        "database_list",
        "collation_list",
        "function_list",
        "compile_options",
        "integrity_check",
        "quick_check",
        "user_version",
        "schema_version",
        "page_count",
        "page_size",
        "freelist_count",
        "encoding",
        "journal_mode",
        "foreign_keys",
    }
)

_WORD_RE = re.compile(r"[A-Za-z_\u0080-￿][A-Za-z0-9_$\u0080-￿]*")
_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|0[xX][0-9A-Fa-f_]+")
_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_\u0080-￿][A-Za-z0-9_\u0080-￿]*)?\$")
# placeholders: psycopg (%s, %(name)s) and $n for PostgreSQL; ?, ?n, :name, @name, $name for SQLite
_PG_PARAM_RE = re.compile(r"\$\d+|%\([A-Za-z_][A-Za-z0-9_]{0,99}\)s|%s")
_SQLITE_PARAM_RE = re.compile(r"\?\d*|[:@$][A-Za-z_][A-Za-z0-9_]*")
_OP_CHARS = "+-*/<>=~!@#%^&|`?:"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    value: str
    start: int
    end: int

    @property
    def upper(self) -> str:
        return self.value.upper() if self.kind == "word" else ""


@dataclass(frozen=True)
class CheckedStatement:
    sql: str  # the statement without a trailing ';'
    statement: str  # leading keyword, e.g. SELECT / WITH / EXPLAIN / SHOW / PRAGMA
    server_cursor: bool  # can be wrapped in DECLARE … CURSOR (PostgreSQL)
    tokens: tuple[Token, ...]


def reject(message: str) -> DbPolicyError:
    return DbPolicyError(f"query rejected: {message}", code=DB_QUERY_REJECTED)


def tokenize(sql: str, dialect: Dialect) -> list[Token]:
    """Tokens without whitespace and comments. Raises :class:`DbPolicyError` for unterminated literals/comments."""
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if sql.startswith("/*", i):
            i = _skip_block_comment(sql, i, nested=dialect == "postgresql")
            continue
        start = i
        if ch == "'":
            i = _skip_quoted(sql, i, "'", backslash=False)
            tokens.append(Token("string", sql[start:i], start, i))
            continue
        if ch in "eE" and i + 1 < n and sql[i + 1] == "'" and dialect == "postgresql":
            i = _skip_quoted(sql, i + 1, "'", backslash=True)
            tokens.append(Token("string", sql[start:i], start, i))
            continue
        if ch in "bBxXnN" and i + 1 < n and sql[i + 1] == "'":
            i = _skip_quoted(sql, i + 1, "'", backslash=False)
            tokens.append(Token("string", sql[start:i], start, i))
            continue
        if ch in "uU" and sql.startswith("&'", i + 1):
            i = _skip_quoted(sql, i + 2, "'", backslash=False)
            tokens.append(Token("string", sql[start:i], start, i))
            continue
        if ch in "uU" and sql.startswith('&"', i + 1):
            i = _skip_quoted(sql, i + 2, '"', backslash=False)
            tokens.append(Token("qident", sql[start + 3 : i - 1].replace('""', '"'), start, i))
            continue
        if ch == '"':
            i = _skip_quoted(sql, i, '"', backslash=False)
            tokens.append(Token("qident", sql[start + 1 : i - 1].replace('""', '"'), start, i))
            continue
        if ch == "`" and dialect == "sqlite":
            i = _skip_quoted(sql, i, "`", backslash=False)
            tokens.append(Token("qident", sql[start + 1 : i - 1].replace("``", "`"), start, i))
            continue
        if ch == "[" and dialect == "sqlite":
            end = sql.find("]", i + 1)
            if end < 0:
                raise reject("unterminated [identifier]")
            i = end + 1
            tokens.append(Token("qident", sql[start + 1 : end], start, i))
            continue
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m and dialect == "postgresql":
                tag = m.group(0)
                close = sql.find(tag, m.end())
                if close < 0:
                    raise reject("unterminated dollar-quoted string")
                i = close + len(tag)
                tokens.append(Token("string", sql[start:i], start, i))
                continue
        pm = (_PG_PARAM_RE if dialect == "postgresql" else _SQLITE_PARAM_RE).match(sql, i)
        if pm:
            i = pm.end()
            tokens.append(Token("param", sql[start:i], start, i))
            continue
        wm = _WORD_RE.match(sql, i)
        if wm:
            i = wm.end()
            tokens.append(Token("word", wm.group(0), start, i))
            continue
        nm = _NUMBER_RE.match(sql, i)
        if nm and nm.end() > i:
            i = nm.end()
            tokens.append(Token("number", nm.group(0), start, i))
            continue
        if ch in "(),;[]{}.":
            i += 1
            tokens.append(Token("punct", ch, start, i))
            continue
        if ch in _OP_CHARS or ch == "$":
            while i < n and (sql[i] in _OP_CHARS) and not sql.startswith("--", i) and not sql.startswith("/*", i):
                i += 1
            if i == start:
                i += 1
            tokens.append(Token("op", sql[start:i], start, i))
            continue
        raise reject(f"unexpected character {ch!r} at offset {i}")
    return tokens


def _skip_block_comment(sql: str, i: int, *, nested: bool) -> int:
    depth, j, n = 0, i, len(sql)
    while j < n:
        if sql.startswith("/*", j):
            depth = depth + 1 if (nested or depth == 0) else depth
            j += 2
            continue
        if sql.startswith("*/", j):
            depth -= 1
            j += 2
            if depth == 0:
                return j
            continue
        j += 1
    raise reject("unterminated block comment")


def _skip_quoted(sql: str, i: int, quote: str, *, backslash: bool) -> int:
    """``sql[i]`` is the opening quote; returns the index after the closing quote (doubled quotes are escapes)."""
    j, n = i + 1, len(sql)
    while j < n:
        c = sql[j]
        if backslash and c == "\\":
            j += 2
            continue
        if c == quote:
            if j + 1 < n and sql[j + 1] == quote:
                j += 2
                continue
            return j + 1
        j += 1
    raise reject("unterminated quoted literal or identifier")


def check_readonly(sql: str, dialect: Dialect) -> CheckedStatement:
    """Validate ``sql`` as one read-only statement; returns it without trailing ``;`` or raises ``DbPolicyError``."""
    if "\x00" in sql:
        raise reject("NUL byte in statement")
    tokens = tokenize(sql, dialect)
    if not tokens:
        raise reject("empty statement")
    semis = [k for k, t in enumerate(tokens) if t.kind == "punct" and t.value == ";"]
    if semis:
        if len(semis) > 1 or semis[0] != len(tokens) - 1:
            raise reject("exactly one statement is allowed (no ';' chaining)")
        cut = tokens[-1].start
        tokens = tokens[:-1]
        if not tokens:
            raise reject("empty statement")
        body = sql[:cut].rstrip()
    else:
        body = sql.strip()

    first = _first_keyword(tokens)
    if first is None:
        raise reject("statement must start with SELECT, WITH, VALUES, TABLE or EXPLAIN")
    keyword = first.upper
    _scan_forbidden(tokens, dialect)

    if keyword == "EXPLAIN":
        _check_explain(tokens, dialect)
        return CheckedStatement(body, "EXPLAIN", False, tuple(tokens))
    if keyword == "SHOW" and dialect == "postgresql":
        if len(tokens) > 4:
            raise reject("SHOW takes one setting name")
        return CheckedStatement(body, "SHOW", False, tuple(tokens))
    if keyword == "PRAGMA" and dialect == "sqlite":
        _check_pragma(tokens)
        return CheckedStatement(body, "PRAGMA", False, tuple(tokens))
    if keyword in READ_STARTS and not (keyword == "TABLE" and dialect != "postgresql"):
        return CheckedStatement(body, keyword, dialect == "postgresql", tuple(tokens))
    raise reject(f"'{first.value[:40]}' statements are not read-only queries (allowed: SELECT, WITH, VALUES, TABLE, EXPLAIN)")


def _first_keyword(tokens: list[Token]) -> Token | None:
    for tok in tokens:
        if tok.kind == "punct" and tok.value == "(":
            continue
        return tok if tok.kind == "word" else None
    return None


def _scan_forbidden(tokens: list[Token], dialect: Dialect) -> None:
    for k, tok in enumerate(tokens):
        nxt = tokens[k + 1] if k + 1 < len(tokens) else None
        prev = tokens[k - 1] if k > 0 else None
        if tok.kind == "word":
            word = tok.upper
            qualified = prev is not None and prev.kind == "punct" and prev.value == "."
            is_call = nxt is not None and nxt.kind == "punct" and nxt.value == "("
            if word in FORBIDDEN_ANYWHERE and not qualified:
                raise reject(f"'{tok.value}' is not allowed in a read-only query")
            if word == "REPLACE" and not is_call and not qualified and dialect == "sqlite":
                raise reject("'REPLACE' is not allowed in a read-only query")
            if word == "FOR" and nxt is not None and nxt.upper in LOCK_AFTER_FOR:
                raise reject("row-locking clauses (FOR UPDATE/SHARE) are not allowed in a read-only query")
        if tok.kind in ("word", "qident") and nxt is not None and nxt.kind == "punct" and nxt.value == "(":
            name = tok.value.lower()
            if name in DENIED_FUNCTIONS or name.startswith(DENIED_FUNCTION_PREFIXES) or _RESET_RE.search(name):
                raise reject(f"function '{tok.value[:60]}' is not allowed in a read-only query")


def _check_explain(tokens: list[Token], dialect: Dialect) -> None:
    if any(t.upper in EXPLAIN_FORBIDDEN for t in tokens):
        raise reject("EXPLAIN ANALYZE executes the statement and is not allowed")
    k = next(i for i, t in enumerate(tokens) if t.upper == "EXPLAIN") + 1
    if k < len(tokens) and tokens[k].kind == "punct" and tokens[k].value == "(":
        depth = 0
        while k < len(tokens):
            t = tokens[k]
            if t.kind == "punct" and t.value == "(":
                depth += 1
            elif t.kind == "punct" and t.value == ")":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
    while k < len(tokens) and tokens[k].upper in ("VERBOSE", "QUERY", "PLAN"):
        k += 1
    inner = _first_keyword(tokens[k:])
    if inner is None or inner.upper not in READ_STARTS or (inner.upper == "TABLE" and dialect != "postgresql"):
        raise reject("EXPLAIN is only allowed for SELECT/WITH/VALUES/TABLE statements")


def _check_pragma(tokens: list[Token]) -> None:
    if any(t.kind == "op" and "=" in t.value for t in tokens):
        raise reject("PRAGMA assignments are not allowed")
    words = [t for t in tokens[1:] if t.kind in ("word", "qident")]
    name_tok = words[0] if words else None
    if len(tokens) > 2 and tokens[2].kind == "punct" and tokens[2].value == "." and len(words) > 1:
        name_tok = words[1]  # PRAGMA schema.name
    if name_tok is None or name_tok.value.lower() not in SQLITE_READ_PRAGMAS:
        raise reject("only read-only PRAGMAs (table_info, index_list, foreign_key_list, …) are allowed")


__all__ = [
    "DENIED_FUNCTIONS",
    "SQLITE_READ_PRAGMAS",
    "CheckedStatement",
    "Token",
    "check_readonly",
    "tokenize",
]
