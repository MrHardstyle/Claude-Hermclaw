"""Runtime-reserved job metadata keys (BUG-140, ``hermclaw.core.job_metadata``).

Clients may store free-form keys in ``jobs.metadata`` on ``POST /api/jobs``; keys the runtime or the API write and
later trust (SSH approvals, triage, replan/retry requests, recovery markers, planner provenance, the typed per-job
flags) are refused. The guard tests at the bottom scan ``hermclaw/`` so a new runtime key cannot be forgotten in the
registry.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hermclaw.core import job_metadata as jm
from hermclaw.core.errors import ValidationFailed

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "hermclaw"


# ------------------------------------------------------------------------------------------------ registry


def test_registry_lists_every_runtime_key_once() -> None:
    constants = {getattr(jm, name) for name in jm.__all__ if name.isupper() and isinstance(getattr(jm, name), str)}
    constants -= {jm.RESERVED_KEY_ERROR}
    assert constants == set(jm.RESERVED_KEYS)
    assert set(jm.RESERVED_KEYS) >= {
        "ssh_approvals",
        "ssh_approvals_consumed",
        "triage",
        "replan_requested",
        "replan_reason",
        "retry_requested",
        "recovered_prepare_from",
        "planner_model_alias",
        "planner_model_fallback",
        "planner_fallback_alias",
        "last_replan",
        "allow_network_research",
        "auto_commit",
        "create_merge_request",
    }
    assert all(jm.RESERVED_KEYS.values()), "every reserved key names its owner"
    with pytest.raises(TypeError):
        jm.RESERVED_KEYS["x"] = "y"  # type: ignore[index]


def test_writers_import_the_registry_constants() -> None:
    from hermclaw.planner.persist import FALLBACK_METADATA_KEY
    from hermclaw.sshtools.approvals import APPROVALS_KEY, CONSUMED_KEY

    assert APPROVALS_KEY is jm.SSH_APPROVALS and CONSUMED_KEY is jm.SSH_APPROVALS_CONSUMED
    assert FALLBACK_METADATA_KEY is jm.PLANNER_MODEL_FALLBACK


@pytest.mark.parametrize(
    "key",
    [
        "ssh_approvals",
        " SSH_Approvals",
        "SSH_APPROVALS\t",
        "ssh_approvals\u200b",  # zero-width space
        "ｓｓｈ＿ａｐｐｒｏｖａｌｓ",  # fullwidth (NFKC)
        "Triage",
        "replan_requested ",
        "ssh_ approvals_consumed",
        "Allow_Network_Research",
        "_internal",
        " _x",
        "hermclaw.deploy_state",
        "HermClaw.anything",
    ],
)
def test_reserved_keys_match_in_any_spelling(key: str) -> None:
    assert jm.is_reserved(key)


@pytest.mark.parametrize("key", ["ticket", "sandbox_image", "scratch", "labels", "triage_note", "my_ssh_approvals", "hermclaw", "x_"])
def test_free_form_keys_stay_client_settable(key: str) -> None:
    assert not jm.is_reserved(key)


def test_check_client_metadata_names_the_offending_keys() -> None:
    jm.check_client_metadata({})
    jm.check_client_metadata({"ticket": "OPS-1", "sandbox_image": "node", "labels": ["a"]})
    with pytest.raises(ValidationFailed) as info:
        jm.check_client_metadata({"ticket": 1, " SSH_Approvals": ["ssh-" + "0" * 48], "triage": {"risk": "low"}, "_x": 1})
    exc = info.value
    assert exc.http_status == 422 and exc.code == jm.RESERVED_KEY_ERROR
    assert exc.details["reserved_keys"] == sorted([" SSH_Approvals", "triage", "_x"])
    assert exc.details["field"] == "metadata" and exc.details["reserved_prefixes"] == list(jm.RESERVED_PREFIXES)
    assert "' SSH_Approvals'" in exc.message and "'triage'" in exc.message and "ticket" not in exc.message


def test_job_create_schema_documents_the_reserved_keys() -> None:
    from hermclaw.contracts.job import JobCreate

    description = JobCreate.model_json_schema()["properties"]["metadata"]["description"]
    for key in jm.RESERVED_KEYS:
        assert key in description
    for prefix in jm.RESERVED_PREFIXES:
        assert f"'{prefix}'" in description


# ------------------------------------------------------------------------------------------------ guard scan


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    kind: str  # write | read
    key: str
    literal: bool

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.kind} {self.key!r}{' (string literal)' if self.literal else ''}"


def _module(path: Path) -> ModuleType:
    return importlib.import_module(".".join(path.relative_to(ROOT).with_suffix("").parts))


def _resolve(node: ast.expr, path: Path) -> tuple[str, bool]:
    """A key expression: a string literal, or a (dotted) name of a module-level constant of the scanned module."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    chain: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        chain.insert(0, cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        raise AssertionError(
            f"{path.relative_to(ROOT)}:{node.lineno}: job metadata key is not a literal or a constant: {ast.unparse(node)}"
        )
    value: Any = _module(path)
    for part in [cur.id, *chain]:
        if not hasattr(value, part):
            raise AssertionError(
                f"{path.relative_to(ROOT)}:{node.lineno}: job metadata key {ast.unparse(node)} is not a module-level constant "
                "(use the constants of hermclaw/core/job_metadata.py)"
            )
        value = getattr(value, part)
    assert isinstance(value, str), f"{path.relative_to(ROOT)}:{node.lineno}: {ast.unparse(node)} is not a str"
    return value, False


def _is_job_ref(node: ast.expr) -> bool:
    """``job`` / ``Job`` / ``<something>_job`` – the jobs table, not repository/worker/lease rows."""
    return isinstance(node, ast.Name) and (node.id in ("job", "Job") or node.id.endswith("_job"))


def _is_job_column(node: ast.expr) -> bool:
    """``Job.metadata_`` – the column itself."""
    return isinstance(node, ast.Attribute) and node.attr == "metadata_" and isinstance(node.value, ast.Name) and node.value.id == "Job"


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
Scope = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef
# zero-argument calls that hand on the loaded metadata (the dict itself, or the result row that holds it)
_PASS_THROUGH = frozenset({"copy", "first", "one", "one_or_none", "scalar", "scalar_one", "scalar_one_or_none"})
# calls that look a key up – their result is a key's value, not the metadata
_LOOKUPS = frozenset({"get", "has_key", "pop", "setdefault"})


def _is_row_index(node: ast.expr) -> bool:
    """``row[0]`` / ``rows[1:]``: positions in a result row, not metadata keys."""
    return isinstance(node, ast.Slice) or (isinstance(node, ast.Constant) and isinstance(node.value, int))


def _unwrap(node: ast.expr) -> ast.expr:
    """Strip what keeps the metadata dict: ``x or {}``, ``await x``, ``dict(x)``, ``row[0]``, ``result.first()``."""
    while True:
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and node.values:
            node = node.values[0]
        elif isinstance(node, ast.Await) or (isinstance(node, ast.Subscript) and _is_row_index(node.slice)):
            node = node.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and node.args:
            node = node.args[0]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _PASS_THROUGH
            and not node.args
            and not node.keywords
        ):
            node = node.func.value
        else:
            return node


def _loads_job_metadata(node: ast.Call) -> bool:
    """A query or helper that loads the column: ``select(Job.metadata_)``, ``session.scalar(select(Job.metadata_)…)``,
    ``_locked_metadata(…)``. A key lookup (``….get(K)``) yields a value, not the metadata."""
    name = _call_name(node)
    if name in _LOOKUPS:
        return False
    if name == "_locked_metadata" or any(_is_job_column(a) for a in node.args):
        return True
    inner = [*node.args, *(kw.value for kw in node.keywords)]
    if isinstance(node.func, ast.Attribute):
        inner.append(node.func.value)  # select(Job.metadata_).where(…)
    return any(isinstance(sub := _unwrap(x), ast.Call) and _loads_job_metadata(sub) for x in inner)


def _job_metadata_expr(node: ast.expr, names: frozenset[str] = frozenset()) -> bool:
    """A job's metadata: ``job.metadata_`` / ``Job.metadata_``, a query loading the column, or a local name of the
    current function bound to one of these – each also wrapped as ``(… or {})``, ``dict(…)``, ``await …``,
    ``row[0]`` or ``result.first()``."""
    node = _unwrap(node)
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return node.attr == "metadata_" and _is_job_ref(node.value)
    return isinstance(node, ast.Call) and _loads_job_metadata(node)


def _scope_nodes(scope: Scope) -> Iterator[ast.AST]:
    """The nodes of one module or function body in source order, without the bodies of nested functions (their own
    scopes); decorators, defaults and annotations of a nested function belong to the enclosing scope."""
    stack: list[ast.AST] = list(reversed(scope.body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _FUNCTIONS):
            children: list[ast.AST] = [*node.decorator_list, node.args, *([node.returns] if node.returns else []), *node.type_params]
        else:
            children = list(ast.iter_child_nodes(node))
        stack.extend(reversed(children))


def _metadata_names(scope: Scope) -> frozenset[str]:
    """Local names a scope binds to a job's metadata, e.g. ``meta = (await session.scalar(select(Job.metadata_)…)) or {}``
    or ``meta = await _locked_metadata(…)`` (the SSH approval readers), or an alias ``m = dict(job.metadata_ or {})``."""
    names: set[str] = set()
    for node in _scope_nodes(scope):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if _job_metadata_expr(value, frozenset(names)):
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return frozenset(names)


def _scan_tree(tree: ast.Module, path: Path) -> tuple[list[Site], list[str]]:
    sites: list[Site] = []
    problems: list[str] = []
    rel = str(path.relative_to(ROOT))

    def add(kind: str, key_node: ast.expr) -> None:
        key, literal = _resolve(key_node, path)
        sites.append(Site(rel, key_node.lineno, kind, key, literal))

    def add_dict(value: ast.expr, where: str) -> None:
        if not isinstance(value, ast.Dict):
            problems.append(f"{rel}:{value.lineno}: {where} with a non-literal dict – keys cannot be checked: {ast.unparse(value)}")
            return
        for k in value.keys:
            if k is not None:  # ``**spread`` entries carry existing keys
                add("write", k)

    for scope in [tree, *(n for n in ast.walk(tree) if isinstance(n, _FUNCTIONS))]:
        names = _metadata_names(scope)
        for node in _scope_nodes(scope):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "metadata_" and _is_job_ref(target.value):
                        add_dict(node.value, "job.metadata_ assignment")
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                if name == "merge_job_metadata" and len(node.args) >= 2:
                    add_dict(node.args[1], "merge_job_metadata")
                elif name == "_set_meta":
                    for arg in node.args[1:]:
                        add_dict(arg, "_set_meta")
                    for kw in node.keywords:
                        if kw.arg is not None:
                            sites.append(Site(rel, node.lineno, "write", kw.arg, True))
                elif name == "_patch" and path.name == "approvals.py" and len(node.args) >= 3:
                    add_dict(node.args[2], "approvals._patch (metadata || patch)")
                elif name == "Job":
                    for kw in node.keywords:
                        if kw.arg == "metadata_":
                            add_dict(kw.value, "Job(metadata_=…)")
                elif (
                    name in ("get", "has_key")
                    and isinstance(node.func, ast.Attribute)
                    and _job_metadata_expr(node.func.value, names)
                    and node.args
                ):
                    add("read", node.args[0])
                elif name == "_list" and len(node.args) >= 2 and _job_metadata_expr(node.args[0], names):
                    add("read", node.args[1])  # sshtools/approvals.py: _list(meta, KEY)
            elif isinstance(node, ast.Subscript) and not _is_row_index(node.slice) and _job_metadata_expr(node.value, names):
                add("read", node.slice)
            elif (
                isinstance(node, ast.Compare)
                and len(node.ops) == 1
                and isinstance(node.ops[0], (ast.In, ast.NotIn))
                and _job_metadata_expr(node.comparators[0], names)
            ):
                add("read", node.left)
    return sites, problems


def _scan() -> tuple[list[Site], list[str]]:
    sites: list[Site] = []
    problems: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        found, issues = _scan_tree(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), path)
        sites += found
        problems += issues
    return sites, problems


@pytest.fixture(scope="module")
def scan() -> tuple[list[Site], list[str]]:
    return _scan()


def test_scan_finds_the_known_writers(scan: tuple[list[Site], list[str]]) -> None:
    """Sanity check of the scanner itself: the known write sites must be found, or the guard below proves nothing."""
    sites, _ = scan
    by_key = {(s.path, s.key) for s in sites if s.kind == "write"}
    assert {
        ("hermclaw/sshtools/approvals.py", jm.SSH_APPROVALS),
        ("hermclaw/sshtools/approvals.py", jm.SSH_APPROVALS_CONSUMED),
        ("hermclaw/runtime/driver.py", jm.TRIAGE),
        ("hermclaw/api/jobs.py", jm.REPLAN_REQUESTED),
        ("hermclaw/api/jobs.py", jm.RETRY_REQUESTED),
        ("hermclaw/api/jobs.py", jm.ALLOW_NETWORK_RESEARCH),
        ("hermclaw/scheduler/scheduler.py", jm.REPLAN_REASON),
        ("hermclaw/scheduler/scheduler.py", jm.RECOVERED_PREPARE_FROM),
        ("hermclaw/planner/planner.py", jm.PLANNER_MODEL_ALIAS),
        ("hermclaw/planner/replanner.py", jm.LAST_REPLAN),
    } <= by_key
    reads = {(s.path, s.key) for s in sites if s.kind == "read"}
    assert ("hermclaw/scheduler/scheduler.py", jm.REPLAN_REQUESTED) in reads  # has_key + .get
    assert ("hermclaw/runtime/driver.py", jm.TRIAGE) in reads
    # the SSH approval readers – the trust anchor of BUG-140 – load the metadata into a local name first
    assert {
        ("hermclaw/sshtools/approvals.py", jm.SSH_APPROVALS),  # _locked_metadata / approval_state + _list(meta, KEY)
        ("hermclaw/sshtools/approvals.py", jm.SSH_APPROVALS_CONSUMED),
        ("hermclaw/sshtools/tools.py", jm.SSH_APPROVALS_CONSUMED),  # _approver: meta.get(CONSUMED_KEY)
        ("hermclaw/api/approvals.py", jm.SSH_APPROVALS),  # (meta[0] or {}).get(APPROVALS_KEY)
        ("hermclaw/api/approvals.py", jm.SSH_APPROVALS_CONSUMED),
    } <= reads


def test_scan_sees_reads_through_local_names_and_wrappers() -> None:
    """Sanity check of the read detection on the forms the runtime uses (and close variants): a new unreserved key
    read in e.g. ``_approver`` must not slip past the guard."""
    source = """
async def approver(session, job_id):
    meta = (await session.scalar(select(Job.metadata_).where(Job.id == job_id))) or {}
    return meta.get("r_scalar")

async def listing(session, job_id):
    meta = (await session.execute(select(Job.metadata_).where(Job.id == job_id))).first()
    return (meta[0] or {}).get("r_row"), meta[0]

async def locked(session, job_id):
    meta = await _locked_metadata(session, job_id)
    row = await session.execute(select(Job.id, Job.metadata_).with_for_update())
    found = row.first()
    return _list(meta, "r_list"), "r_in" in dict(found[0] or {}), meta["r_subscript"], meta.copy().get("r_copy")

async def inline(session, job):
    value = ((await session.scalar(select(Job.metadata_))) or {}).get("r_inline")
    alias = dict(job.metadata_ or {})
    return value.get("value_of_a_key"), alias.get("r_alias"), job.metadata_.get("r_direct"), str(value)

async def unrelated(session, meta, row):
    rows = (await session.execute(select(Step, Job.priority).where(not_(Job.metadata_.has_key("r_has_key"))))).all()
    def nested():
        return meta.get("parameter")
    return meta.get("parameter"), row.metadata_.get("other_table"), rows[0], rows[1:], nested
"""
    sites, problems = _scan_tree(ast.parse(source), PACKAGE / "example.py")
    assert not problems
    assert sorted(s.key for s in sites if s.kind == "read") == sorted(
        ["r_scalar", "r_row", "r_list", "r_in", "r_subscript", "r_copy", "r_inline", "r_alias", "r_direct", "r_has_key"]
    )


def test_every_job_metadata_write_is_a_reserved_key(scan: tuple[list[Site], list[str]]) -> None:
    sites, problems = scan
    assert not problems, "\n".join(problems)
    unreserved = [str(s) for s in sites if s.kind == "write" and not jm.is_reserved(s.key)]
    assert not unreserved, (
        "runtime writes job metadata keys a client could plant on POST /api/jobs – declare them in "
        "hermclaw/core/job_metadata.py (RESERVED_KEYS):\n" + "\n".join(unreserved)
    )


def test_every_job_metadata_read_is_a_reserved_key(scan: tuple[list[Site], list[str]]) -> None:
    """The runtime must not trust a job metadata key that only a client sets."""
    sites, _ = scan
    unreserved = [str(s) for s in sites if s.kind == "read" and not jm.is_reserved(s.key)]
    assert not unreserved, (
        "runtime reads job metadata keys that are not reserved – a client controls them; reserve the key in "
        "hermclaw/core/job_metadata.py or validate the client value explicitly:\n" + "\n".join(unreserved)
    )


def test_runtime_uses_the_registry_constants(scan: tuple[list[Site], list[str]]) -> None:
    sites, _ = scan
    literal = [str(s) for s in sites if s.literal]
    assert not literal, "use the constants of hermclaw/core/job_metadata.py instead of string literals:\n" + "\n".join(literal)


def test_registry_has_no_stale_keys(scan: tuple[list[Site], list[str]]) -> None:
    sites, _ = scan
    written = {s.key for s in sites if s.kind == "write"}
    assert set(jm.RESERVED_KEYS) - written == set(), "reserved keys without a writer in hermclaw/ – stale registry entry?"
