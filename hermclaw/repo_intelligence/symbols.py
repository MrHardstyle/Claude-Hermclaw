"""Structural index (Bauplan §14 Phase C; P11 11.5/11.6).

AST adapters:
* Python – stdlib :mod:`ast` (functions, async functions, classes, methods, module constants, imports, calls).
* JavaScript / TypeScript / TSX / PHP – tree-sitter (``tree_sitter_language_pack.get_parser``).
* every language – routes (:mod:`routes`) and tables/views (:mod:`tables`, SQL + migration DSLs).

Persistence (``code_symbols``): one row per symbol, keyed by ``repository_key`` + ``path``; ``git_sha`` is the index
revision that produced the row. Imports are stored as ``kind="import"`` rows whose ``references`` hold the import
metadata incl. the resolved workspace file (11.7, see :mod:`dependencies`).
"""

from __future__ import annotations

import ast
import threading
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.core.logging import get_logger
from hermclaw.persistence.models import CodeSymbol
from hermclaw.repo_intelligence.routes import extract_routes
from hermclaw.repo_intelligence.schemas import FileSymbols, ImportRecord, SymbolRecord
from hermclaw.repo_intelligence.tables import extract_tables

MAX_REFS = 100
MAX_SYMBOLS_PER_FILE = 5_000
DEFINITION_KINDS = ("function", "method", "class", "interface", "trait", "enum", "type", "constant")
#: rows that are not definitions: imports (11.7) and per-file module-level call references
AUX_KINDS = ("import", "module")
_TS_LANGS = {"javascript": "javascript", "typescript": "typescript", "tsx": "tsx", "php": "php"}
_local = threading.local()
log = get_logger(__name__)


def _parser(lang: str) -> Any:
    cache: dict[str, Any] = getattr(_local, "parsers", None) or {}
    _local.parsers = cache
    if lang not in cache:
        from tree_sitter_language_pack import get_parser

        cache[lang] = get_parser(lang)  # type: ignore[arg-type]
    return cache[lang]


def _dedup(items: Iterable[str], limit: int = MAX_REFS) -> list[str]:
    seen: dict[str, None] = {}
    for i in items:
        if i and i not in seen:
            seen[i] = None
            if len(seen) >= limit:
                break
    return list(seen)


# ============================================================================================= python (ast)
class _PyCollector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.symbols: list[SymbolRecord] = []
        self.imports: list[ImportRecord] = []
        self.stack: list[tuple[str, str]] = []  # (name, kind)

    @staticmethod
    def _calls(node: ast.AST) -> list[str]:
        names: list[str] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name):
                    names.append(f.id)
                elif isinstance(f, ast.Attribute):
                    names.append(f.attr)
        return _dedup(names)

    def _parent(self) -> str | None:
        return ".".join(n for n, _ in self.stack) or None

    def _def(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, kind: str, signature: str | None) -> None:
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        self.symbols.append(
            SymbolRecord(
                name=node.name,
                kind=kind,
                language="python",
                path=self.path,
                start_line=start,
                end_line=getattr(node, "end_lineno", None) or node.lineno,
                parent=self._parent(),
                signature=signature,
                references=self._calls(node),
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = ", ".join(ast.unparse(b) for b in node.bases)[:200]
        self._def(node, "class", f"class {node.name}({bases})" if bases else f"class {node.name}")
        self.stack.append((node.name, "class"))
        self.generic_visit(node)
        self.stack.pop()

    def _func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = "method" if self.stack and self.stack[-1][1] == "class" else "function"
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        try:
            args = ast.unparse(node.args)
        except Exception:  # pragma: no cover - unparse is total for parsed trees
            args = "..."
        ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        self._def(node, kind, f"{prefix} {node.name}({args}){ret}"[:300])
        self.stack.append((node.name, "function"))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._func(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(ImportRecord(module=alias.name, names=[], line=node.lineno, kind="import"))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.append(
            ImportRecord(
                module=node.module or "",
                names=[a.name for a in node.names][:50],
                line=node.lineno,
                kind="from",
                level=node.level or 0,
            )
        )

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.stack:
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 1:
                    self.symbols.append(
                        SymbolRecord(
                            name=t.id,
                            kind="constant",
                            language="python",
                            path=self.path,
                            start_line=node.lineno,
                            end_line=getattr(node, "end_lineno", None) or node.lineno,
                        )
                    )
        self.generic_visit(node)


def _python(path: str, text: str) -> FileSymbols:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return FileSymbols(path=path, language="python", parse_error=f"{type(exc).__name__}: {str(exc)[:200]}")
    col = _PyCollector(path)
    try:
        col.visit(tree)
    except RecursionError:  # pathologically nested code: keep what was collected
        return FileSymbols(
            path=path, language="python", symbols=col.symbols, imports=col.imports, parse_error="RecursionError: nesting too deep"
        )
    module_calls: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            module_calls.extend(_PyCollector._calls(node))
    return FileSymbols(path=path, language="python", symbols=col.symbols, imports=col.imports, calls=_dedup(module_calls))


# ============================================================================================= tree-sitter
def _txt(node: Any, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _string_value(node: Any, src: bytes) -> str | None:
    """Literal content of a JS/PHP string node (no interpolation)."""
    if node is None:
        return None
    if node.type in ("string", "template_string", "encapsed_string"):
        frags = [c for c in node.children if c.type in ("string_fragment", "string_content", "string_value")]
        if node.type == "template_string" and any(c.type == "template_substitution" for c in node.children):
            return None
        if frags:
            return "".join(_txt(c, src) for c in frags)
        raw = _txt(node, src)
        return raw[1:-1] if len(raw) >= 2 else None
    return None


class _TSCollector:
    def __init__(self, path: str, lang: str, src: bytes) -> None:
        self.path = path
        self.lang = lang
        self.src = src
        self.symbols: list[SymbolRecord] = []
        self.imports: list[ImportRecord] = []
        self.module_calls: list[str] = []
        self.namespace: str | None = None

    # ---- helpers
    def _name(self, node: Any) -> str | None:
        n = node.child_by_field_name("name")
        return _txt(n, self.src) if n is not None else None

    def _add(self, node: Any, name: str, kind: str, parent: str | None, sig_node: Any | None = None) -> SymbolRecord:
        head = _txt(sig_node or node, self.src).split("{", 1)[0].strip().replace("\n", " ")
        rec = SymbolRecord(
            name=name,
            kind=kind,
            language=self.lang,
            path=self.path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            parent=parent,
            signature=" ".join(head.split())[:300] or None,
        )
        self.symbols.append(rec)
        return rec

    def _call_name(self, node: Any) -> str | None:
        t = node.type
        if self.lang == "php":
            if t == "function_call_expression":
                f = node.child_by_field_name("function")
                return _txt(f, self.src).rsplit("\\", 1)[-1] if f is not None else None
            if t in ("member_call_expression", "scoped_call_expression", "nullsafe_member_call_expression"):
                n = node.child_by_field_name("name")
                return _txt(n, self.src) if n is not None else None
            if t == "object_creation_expression":
                for c in node.children:
                    if c.type in ("name", "qualified_name"):
                        return _txt(c, self.src).rsplit("\\", 1)[-1]
            return None
        if t == "call_expression":
            f = node.child_by_field_name("function")
            if f is None:
                return None
            if f.type == "identifier":
                return _txt(f, self.src)
            if f.type == "member_expression":
                p = f.child_by_field_name("property")
                return _txt(p, self.src) if p is not None else None
            return None
        if t == "new_expression":
            c = node.child_by_field_name("constructor")
            if c is not None:
                return _txt(c, self.src).rsplit(".", 1)[-1]
        return None

    def _refs(self, node: Any) -> list[str]:
        names: list[str] = []
        stack = [node]
        while stack:
            n = stack.pop()
            name = self._call_name(n)
            if name:
                names.append(name)
            stack.extend(reversed(n.children))
        return _dedup(names)

    # ---- js/ts
    def _js_import(self, node: Any) -> None:
        line = node.start_point[0] + 1
        if node.type == "import_statement":
            src = _string_value(node.child_by_field_name("source"), self.src)
            if src is None:
                return
            names: list[str] = []
            for c in node.children:
                if c.type == "import_clause":
                    stack = [c]
                    while stack:
                        n = stack.pop()
                        if n.type in ("identifier",):
                            names.append(_txt(n, self.src))
                        stack.extend(n.children)
            self.imports.append(ImportRecord(module=src, names=sorted(set(names))[:50], line=line, kind="import"))
        elif node.type == "export_statement":
            src = _string_value(node.child_by_field_name("source"), self.src)
            if src is not None:
                self.imports.append(ImportRecord(module=src, line=line, kind="export-from"))

    def _js_call_import(self, node: Any) -> None:
        f = node.child_by_field_name("function")
        if f is None:
            return
        kind = "require" if f.type == "identifier" and _txt(f, self.src) == "require" else "dynamic" if f.type == "import" else None
        if kind is None:
            return
        args = node.child_by_field_name("arguments")
        if args is None:
            return
        for a in args.children:
            v = _string_value(a, self.src)
            if v is not None:
                self.imports.append(ImportRecord(module=v, line=node.start_point[0] + 1, kind=kind))
                break

    def walk_js(self, node: Any, parent: str | None, in_class: bool) -> None:
        for child in node.children:
            t = child.type
            if t in ("function_declaration", "generator_function_declaration", "function_signature"):
                name = self._name(child)
                if name:
                    rec = self._add(child, name, "method" if in_class else "function", parent)
                    rec.references = self._refs(child)
                    self.walk_js(child, f"{parent}.{name}" if parent else name, False)
                    continue
            elif t in ("class_declaration", "abstract_class_declaration", "class"):
                name = self._name(child)
                if name:
                    rec = self._add(child, name, "class", parent)
                    rec.references = self._refs(child)
                    body = child.child_by_field_name("body")
                    if body is not None:
                        self.walk_js(body, f"{parent}.{name}" if parent else name, True)
                    continue
            elif t in ("method_definition", "method_signature", "abstract_method_signature"):
                name = self._name(child)
                if name:
                    rec = self._add(child, name, "method", parent)
                    rec.references = self._refs(child)
                    self.walk_js(child, f"{parent}.{name}" if parent else name, False)
                    continue
            elif t == "interface_declaration":
                name = self._name(child)
                if name:
                    self._add(child, name, "interface", parent)
                    continue
            elif t == "type_alias_declaration":
                name = self._name(child)
                if name:
                    self._add(child, name, "type", parent)
                    continue
            elif t == "enum_declaration":
                name = self._name(child)
                if name:
                    self._add(child, name, "enum", parent)
                    continue
            elif t == "variable_declarator":
                value = child.child_by_field_name("value")
                name_node = child.child_by_field_name("name")
                if value is not None and name_node is not None and name_node.type == "identifier":
                    name = _txt(name_node, self.src)
                    if value.type in ("arrow_function", "function_expression", "function", "generator_function"):
                        rec = self._add(child, name, "function", parent)
                        rec.references = self._refs(value)
                        self.walk_js(value, f"{parent}.{name}" if parent else name, False)
                        continue
                    if value.type == "class":
                        rec = self._add(child, name, "class", parent)
                        body = value.child_by_field_name("body")
                        if body is not None:
                            self.walk_js(body, f"{parent}.{name}" if parent else name, True)
                        continue
                    if parent is None and name.isupper() and len(name) > 1:
                        self._add(child, name, "constant", None)
            elif t in ("public_field_definition", "field_definition") and in_class:
                value = child.child_by_field_name("value")
                pname = child.child_by_field_name("property") or child.child_by_field_name("name")
                if value is not None and pname is not None and value.type in ("arrow_function", "function_expression", "function"):
                    name = _txt(pname, self.src)
                    rec = self._add(child, name, "method", parent)
                    rec.references = self._refs(value)
                    continue
            elif t in ("import_statement", "export_statement"):
                self._js_import(child)
            elif t == "call_expression":
                self._js_call_import(child)
                if parent is None:
                    cname = self._call_name(child)
                    if cname:
                        self.module_calls.append(cname)
            self.walk_js(child, parent, in_class and t in ("class_body",))

    # ---- php
    def _php_include(self, node: Any) -> None:
        literal: str | None = None
        dir_relative = False
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in ("string", "encapsed_string"):
                literal = _string_value(n, self.src) or literal
            elif (n.type == "name" and _txt(n, self.src) == "__DIR__") or (
                n.type == "function_call_expression" and _txt(n, self.src).replace(" ", "").startswith("dirname(__FILE__")
            ):
                dir_relative = True
            stack.extend(n.children)
        if literal:
            self.imports.append(
                ImportRecord(module=literal, line=node.start_point[0] + 1, kind="include-dir" if dir_relative else "include")
            )

    def walk_php(self, node: Any, parent: str | None, in_class: bool) -> None:
        for child in node.children:
            t = child.type
            if t == "namespace_definition":
                name = self._name(child)
                self.namespace = name
                body = child.child_by_field_name("body")
                if body is not None:
                    self.walk_php(body, None, False)
                continue
            if t == "function_definition":
                name = self._name(child)
                if name:
                    rec = self._add(child, name, "function", parent or self.namespace)
                    rec.references = self._refs(child)
                    self.walk_php(child, name, False)
                    continue
            elif t in ("class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"):
                name = self._name(child)
                if name:
                    kind = {"class_declaration": "class", "interface_declaration": "interface", "trait_declaration": "trait"}.get(t, "enum")
                    rec = self._add(child, name, kind, self.namespace)
                    rec.references = self._refs(child)
                    body = child.child_by_field_name("body")
                    if body is not None:
                        self.walk_php(body, name, True)
                    continue
            elif t == "method_declaration":
                name = self._name(child)
                if name:
                    rec = self._add(child, name, "method", parent)
                    rec.references = self._refs(child)
                    continue
            elif t == "const_declaration" and parent is None:
                for c in child.children:
                    if c.type == "const_element":
                        for n in c.children:
                            if n.type == "name":
                                self._add(c, _txt(n, self.src), "constant", self.namespace)
                                break
            elif t == "namespace_use_declaration":
                prefix = ""
                for c in child.children:
                    if c.type == "namespace_name":
                        prefix = _txt(c, self.src)
                for c in child.children:
                    stack = [c]
                    while stack:
                        n = stack.pop()
                        if n.type in ("namespace_use_clause", "namespace_use_group_clause"):
                            target = None
                            for q in n.children:
                                if q.type in ("qualified_name", "name", "namespace_name"):
                                    target = _txt(q, self.src)
                                    break
                            if target:
                                full = f"{prefix}\\{target}" if prefix else target
                                self.imports.append(ImportRecord(module=full.lstrip("\\"), line=n.start_point[0] + 1, kind="use"))
                            continue
                        stack.extend(n.children)
                continue
            elif t in ("include_expression", "include_once_expression", "require_expression", "require_once_expression"):
                self._php_include(child)
                continue
            elif parent is None and t in ("function_call_expression", "member_call_expression", "scoped_call_expression"):
                cname = self._call_name(child)
                if cname:
                    self.module_calls.append(cname)
            self.walk_php(child, parent, in_class and t == "declaration_list")


def _tree_sitter(path: str, lang: str, text: str) -> FileSymbols:
    src = text.encode("utf-8", errors="replace")
    try:
        tree = _parser(_TS_LANGS[lang]).parse(src)
    except Exception as exc:  # parser unavailable/crash: no structure, never a failed index run
        return FileSymbols(path=path, language=lang, parse_error=f"{type(exc).__name__}: {str(exc)[:200]}")
    col = _TSCollector(path, lang, src)
    err = "syntax errors (partial structure)" if tree.root_node.has_error else None
    try:
        if lang == "php":
            col.walk_php(tree.root_node, None, False)
        else:
            col.walk_js(tree.root_node, None, False)
    except RecursionError:  # pathologically nested code: keep what was collected
        err = "RecursionError: nesting too deep (partial structure)"
    return FileSymbols(path=path, language=lang, symbols=col.symbols, imports=col.imports, calls=_dedup(col.module_calls), parse_error=err)


# ============================================================================================= entry
def extract_file_symbols(path: str, language: str | None, text: str) -> FileSymbols:
    """Structure of one file: definitions, imports, calls, routes and tables (deterministic)."""
    lang = language or "text"
    if lang == "python":
        fs = _python(path, text)
    elif lang in _TS_LANGS:
        fs = _tree_sitter(path, lang, text)
    else:
        fs = FileSymbols(path=path, language=lang)
    for r in extract_routes(path, lang, text):
        fs.symbols.append(
            SymbolRecord(
                name=f"{r.method} {r.path}"[:300],
                kind="route",
                language=lang,
                path=path,
                start_line=r.line,
                end_line=r.line,
                parent=r.handler,
                signature=r.framework,
            )
        )
    if lang in ("sql", "python", "php", "javascript", "typescript", "tsx", "ruby", "go", "java", "kotlin"):
        for name, kind, start, end in extract_tables(text):
            fs.symbols.append(SymbolRecord(name=name[:300], kind=kind, language=lang, path=path, start_line=start, end_line=end))
    fs.symbols.sort(key=lambda s: (s.start_line, s.end_line, s.kind, s.name))
    fs.symbols = fs.symbols[:MAX_SYMBOLS_PER_FILE]
    return fs


# ============================================================================================= persistence
def symbol_rows(repository_key: str, git_sha: str, fs: FileSymbols) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in fs.symbols:
        rows.append(
            {
                "repository_key": repository_key,
                "git_sha": git_sha,
                "path": fs.path,
                "name": s.name[:300],
                "kind": s.kind[:32],
                "language": s.language[:32],
                "start_line": s.start_line,
                "end_line": max(s.start_line, s.end_line),
                "parent": s.parent[:300] if s.parent else None,
                "references": [{"signature": s.signature}, *s.references[:MAX_REFS]] if s.signature else s.references[:MAX_REFS],
            }
        )
    for imp in fs.imports:
        rows.append(
            {
                "repository_key": repository_key,
                "git_sha": git_sha,
                "path": fs.path,
                "name": (imp.module or "")[:300] or ".",
                "kind": "import",
                "language": fs.language[:32],
                "start_line": imp.line,
                "end_line": imp.line,
                "parent": ",".join(imp.names)[:300] or None,
                "references": [imp.model_dump(exclude={"line"})],
            }
        )
    if fs.calls:  # module-level call references (scripts, registrations) – a "module" row keeps them queryable
        rows.append(
            {
                "repository_key": repository_key,
                "git_sha": git_sha,
                "path": fs.path,
                "name": fs.path.rsplit("/", 1)[-1][:300],
                "kind": "module",
                "language": fs.language[:32],
                "start_line": 1,
                "end_line": 1,
                "parent": None,
                "references": fs.calls[:MAX_REFS],
            }
        )
    return rows


def row_to_symbol(row: Any) -> SymbolRecord:
    refs = list(row.references or [])
    signature = None
    if refs and isinstance(refs[0], dict) and "signature" in refs[0]:
        signature = refs[0].get("signature")
        refs = refs[1:]
    return SymbolRecord(
        name=row.name,
        kind=row.kind,
        language=row.language,
        path=row.path,
        start_line=row.start_line,
        end_line=row.end_line,
        parent=row.parent,
        signature=signature,
        references=[r for r in refs if isinstance(r, str)],
    )


async def replace_file_symbols(
    session: AsyncSession,
    repository_key: str,
    git_sha: str,
    files: Sequence[FileSymbols],
    *,
    remove_paths: Iterable[str] = (),
    replace_all: bool = False,
    batch: int = 2_000,
) -> tuple[int, int]:
    """Replace the rows of the given files (and drop ``remove_paths``) in the caller's transaction."""
    if replace_all:
        await session.execute(delete(CodeSymbol).where(CodeSymbol.repository_key == repository_key))
    else:
        paths = sorted({f.path for f in files} | set(remove_paths))
        for i in range(0, len(paths), 1_000):
            await session.execute(
                delete(CodeSymbol).where(CodeSymbol.repository_key == repository_key, CodeSymbol.path.in_(paths[i : i + 1_000]))
            )
    rows: list[dict[str, Any]] = []
    for fs in files:
        rows.extend(symbol_rows(repository_key, git_sha, fs))
    for i in range(0, len(rows), batch):
        await session.execute(insert(CodeSymbol), rows[i : i + batch])
    n_imports = sum(1 for r in rows if r["kind"] == "import")
    n_aux = sum(1 for r in rows if r["kind"] in AUX_KINDS)
    return len(rows) - n_aux, n_imports


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def query_symbols(
    session: AsyncSession,
    repository_key: str,
    terms: Sequence[str],
    *,
    exact: bool = False,
    include_imports: bool = False,
    exclude_paths: Iterable[str] = (),
    limit: int = 2_000,
) -> list[SymbolRecord]:
    """Symbols whose name equals (``exact``, case-insensitive) or contains one of ``terms``."""
    terms = [t for t in terms if t]
    if not terms:
        return []
    stmt = select(
        CodeSymbol.name,
        CodeSymbol.kind,
        CodeSymbol.language,
        CodeSymbol.path,
        CodeSymbol.start_line,
        CodeSymbol.end_line,
        CodeSymbol.parent,
        CodeSymbol.references,
    ).where(CodeSymbol.repository_key == repository_key)
    if not include_imports:
        stmt = stmt.where(CodeSymbol.kind.not_in(AUX_KINDS))
    if exact:
        stmt = stmt.where(func.lower(CodeSymbol.name).in_([t.lower() for t in terms]))
    else:
        stmt = stmt.where(or_(*(CodeSymbol.name.ilike(f"%{_like_escape(t)}%", escape="\\") for t in terms)))
    excluded = set(exclude_paths)
    stmt = stmt.order_by(CodeSymbol.path, CodeSymbol.start_line, CodeSymbol.name).limit(limit + len(excluded) * 20)
    rows = (await session.execute(stmt)).all()
    return [row_to_symbol(r) for r in rows if r.path not in excluded][:limit]


async def referencing_files(
    session: AsyncSession, repository_key: str, names: Sequence[str], *, exclude_paths: Iterable[str] = (), limit: int = 5_000
) -> dict[str, set[str]]:
    """``name -> files`` whose definitions or module-level code call/instantiate ``name`` (JSONB ``@>`` on references)."""
    wanted = [n for n in dict.fromkeys(names) if n][:25]
    if not wanted:
        return {}
    excluded = set(exclude_paths)
    stmt = (
        select(CodeSymbol.path, CodeSymbol.references)
        .where(
            CodeSymbol.repository_key == repository_key,
            CodeSymbol.kind != "import",
            or_(*(CodeSymbol.references.contains([n]) for n in wanted)),
        )
        .order_by(CodeSymbol.path)
        .limit(limit)
    )
    out: dict[str, set[str]] = {}
    for path, refs in (await session.execute(stmt)).all():
        if path in excluded or not isinstance(refs, list):
            continue
        present = {r for r in refs if isinstance(r, str)}
        for n in wanted:
            if n in present:
                out.setdefault(n, set()).add(path)
    return out


async def load_imports(session: AsyncSession, repository_key: str) -> dict[str, list[ImportRecord]]:
    stmt = (
        select(CodeSymbol.path, CodeSymbol.start_line, CodeSymbol.references)
        .where(CodeSymbol.repository_key == repository_key, CodeSymbol.kind == "import")
        .order_by(CodeSymbol.path, CodeSymbol.start_line)
    )
    out: dict[str, list[ImportRecord]] = {}
    for path, line, refs in (await session.execute(stmt)).all():
        meta = refs[0] if refs and isinstance(refs[0], dict) else {}
        try:
            rec = ImportRecord(line=line, **{k: v for k, v in meta.items() if k in ("module", "names", "kind", "level", "resolved")})
        except Exception as exc:  # malformed row: no dependency edge, never a failed query
            log.debug("skipping malformed import row of %s: %s", path, type(exc).__name__)
            continue
        out.setdefault(path, []).append(rec)
    return out


async def indexed_paths(session: AsyncSession, repository_key: str) -> set[str]:
    rows = await session.execute(select(CodeSymbol.path).where(CodeSymbol.repository_key == repository_key).distinct())
    return {r[0] for r in rows.all()}


async def symbols_by_kind(
    session: AsyncSession, repository_key: str, kinds: Sequence[str], *, exclude_paths: Iterable[str] = (), limit: int = 5_000
) -> list[SymbolRecord]:
    excluded = set(exclude_paths)
    stmt = (
        select(
            CodeSymbol.name,
            CodeSymbol.kind,
            CodeSymbol.language,
            CodeSymbol.path,
            CodeSymbol.start_line,
            CodeSymbol.end_line,
            CodeSymbol.parent,
            CodeSymbol.references,
        )
        .where(CodeSymbol.repository_key == repository_key, CodeSymbol.kind.in_(list(kinds)))
        .order_by(CodeSymbol.path, CodeSymbol.start_line)
        .limit(limit)
    )
    return [row_to_symbol(r) for r in (await session.execute(stmt)).all() if r.path not in excluded]
