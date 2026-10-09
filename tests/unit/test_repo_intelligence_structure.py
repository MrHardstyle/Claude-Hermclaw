"""P11 11.5-11.8 (pure parts): AST adapters (Python ast, tree-sitter JS/TS/TSX/PHP), tables, dependency resolution,
symbol-aligned chunking and query analysis."""

from __future__ import annotations

from hermclaw.repo_intelligence.chunking import CHARS_PER_TOKEN, Chunker, content_hash, document_text
from hermclaw.repo_intelligence.config import RepoIntelConfig
from hermclaw.repo_intelligence.dependencies import (
    DependencyGraph,
    ModuleResolver,
    composer_manifests,
    merge_psr4,
    psr4_from_composer,
)
from hermclaw.repo_intelligence.query import analyze, split_identifier, stem
from hermclaw.repo_intelligence.schemas import ImportRecord
from hermclaw.repo_intelligence.symbols import extract_file_symbols, row_to_symbol, symbol_rows
from hermclaw.repo_intelligence.tables import extract_tables


def _defs(fs: object) -> set[tuple[str, str, str | None]]:
    return {(s.kind, s.name, s.parent) for s in fs.symbols}  # type: ignore[attr-defined]


def test_python_ast_adapter() -> None:
    src = (
        "import os\nfrom . import sibling\nfrom ..pkg.mod import thing as alias, other\n\n"
        "MAX_SIZE = 10\n\n\n"
        "@decorator\nasync def fetch(url: str, *, timeout: float = 1.0) -> bytes:\n    return download(url)\n\n\n"
        "class Repo(Base, metaclass=Meta):\n    def save(self):\n        self.validate()\n        write_row(self)\n\n"
        "    class Inner:\n        def deep(self):\n            pass\n"
    )
    fs = extract_file_symbols("pkg/sub/mod.py", "python", src)
    assert fs.parse_error is None
    assert _defs(fs) >= {
        ("constant", "MAX_SIZE", None),
        ("function", "fetch", None),
        ("class", "Repo", None),
        ("method", "save", "Repo"),
        ("class", "Inner", "Repo"),
        ("method", "deep", "Repo.Inner"),
    }
    fetch = next(s for s in fs.symbols if s.name == "fetch")
    assert fetch.start_line == 8 and fetch.end_line == 10  # decorator line included
    assert fetch.signature == "async def fetch(url: str, *, timeout: float=1.0) -> bytes"
    assert fetch.references == ["download"]
    save = next(s for s in fs.symbols if s.name == "save")
    assert save.references == ["validate", "write_row"]
    assert [(i.module, i.kind, i.level, i.names) for i in fs.imports] == [
        ("os", "import", 0, []),
        ("", "from", 1, ["sibling"]),
        ("pkg.mod", "from", 2, ["thing", "other"]),
    ]


def test_python_syntax_errors_and_deep_nesting_do_not_raise() -> None:
    bad = extract_file_symbols("bad.py", "python", "def broken(:\n    pass\n")
    assert bad.parse_error and bad.parse_error.startswith("SyntaxError") and bad.symbols == []
    deep = "x = " + "[" * 5000 + "]" * 5000 + "\n"
    fs = extract_file_symbols("deep.py", "python", deep)
    assert fs.parse_error is not None


def test_javascript_typescript_tsx_adapters() -> None:
    js = (
        "import React, { useState } from 'react';\nimport * as util from './util.js';\nexport * from './reexport';\n"
        "const fs = require('fs');\nconst lazy = () => import('./lazy');\n\n"
        "export class Store extends Base {\n  items = [];\n  handler = (e) => this.flush(e);\n"
        "  async load(id) {\n    return fetchItem(id);\n  }\n  static create() { return new Store(); }\n}\n\n"
        "export function helper(a) {\n  return a.map(transform);\n}\n\nconst API_URL = 'x';\n"
        "const compute = function (x) { return square(x); };\n"
    )
    fs = extract_file_symbols("src/store.js", "javascript", js)
    assert _defs(fs) >= {
        ("class", "Store", None),
        ("method", "load", "Store"),
        ("method", "create", "Store"),
        ("method", "handler", "Store"),
        ("function", "helper", None),
        ("function", "compute", None),
        ("function", "lazy", None),
        ("constant", "API_URL", None),
    }
    assert {(i.module, i.kind) for i in fs.imports} == {
        ("react", "import"),
        ("./util.js", "import"),
        ("./reexport", "export-from"),
        ("fs", "require"),
        ("./lazy", "dynamic"),
    }
    react = next(i for i in fs.imports if i.module == "react")
    assert react.names == ["React", "useState"]
    assert "fetchItem" in next(s for s in fs.symbols if s.name == "load").references
    assert "Store" in next(s for s in fs.symbols if s.name == "create").references

    ts = (
        "export interface User { id: number }\nexport type Id = string | number;\nexport enum Role { Admin, User }\n"
        "export abstract class Service<T> {\n  abstract run(input: T): Promise<void>;\n  protected log(msg: string): void {}\n}\n"
        "declare function external(x: number): void;\n"
    )
    tfs = extract_file_symbols("src/service.ts", "typescript", ts)
    assert _defs(tfs) >= {
        ("interface", "User", None),
        ("type", "Id", None),
        ("enum", "Role", None),
        ("class", "Service", None),
        ("method", "log", "Service"),
    }
    tsx = "import { useState } from 'react';\nexport function Counter() {\n  const [n, setN] = useState(0);\n  return <button onClick={() => setN(n + 1)}>{n}</button>;\n}\n"
    xfs = extract_file_symbols("src/Counter.tsx", "tsx", tsx)
    assert ("function", "Counter", None) in _defs(xfs) and xfs.parse_error is None
    assert "useState" in next(s for s in xfs.symbols if s.name == "Counter").references


def test_php_adapter() -> None:
    php = (
        "<?php\nnamespace App\\Http;\n\nuse App\\Models\\{User, Post};\nuse Psr\\Log\\LoggerInterface as Logger;\n"
        "require_once __DIR__ . '/bootstrap.php';\ninclude 'lib/legacy.php';\n\n"
        "const VERSION = '1.0';\n\ninterface Handler { public function handle(); }\ntrait Loggable { public function log() {} }\n"
        "enum Status { case Active; }\n\n"
        "final class UserController extends Controller implements Handler\n{\n"
        "    public function handle()\n    {\n        $user = new User();\n        return $this->render($user->name());\n    }\n}\n\n"
        "function helper($x) { return strtoupper($x); }\n"
    )
    fs = extract_file_symbols("app/Http/UserController.php", "php", php)
    assert _defs(fs) >= {
        ("interface", "Handler", "App\\Http"),
        ("trait", "Loggable", "App\\Http"),
        ("enum", "Status", "App\\Http"),
        ("class", "UserController", "App\\Http"),
        ("method", "handle", "UserController"),
        ("function", "helper", "App\\Http"),
        ("constant", "VERSION", "App\\Http"),
    }
    handle = next(s for s in fs.symbols if s.name == "handle" and s.parent == "UserController")
    assert {"User", "render", "name"} <= set(handle.references)
    assert {(i.module, i.kind) for i in fs.imports} >= {
        ("App\\Models\\User", "use"),
        ("App\\Models\\Post", "use"),
        ("Psr\\Log\\LoggerInterface", "use"),
        ("/bootstrap.php", "include-dir"),
        ("lib/legacy.php", "include"),
    }


def test_tables_from_sql_and_migration_dsls() -> None:
    sql = 'CREATE TABLE IF NOT EXISTS public."orders" (\n id int\n);\nCREATE OR REPLACE VIEW active_orders AS SELECT 1;\n'
    assert extract_tables(sql) == [("public.orders", "table", 1, 3), ("active_orders", "view", 4, 4)]
    assert ("users", "table", 1, 1) in extract_tables("Schema::create('users', function (Blueprint $t) {});")
    assert ("posts", "table", 1, 1) in extract_tables("exports.up = knex => knex.schema.createTable('posts', t => {});")
    assert ("tags", "table", 1, 1) in extract_tables("migrations.CreateModel(name='tags', fields=[])")
    fs = extract_file_symbols("db/schema.sql", "sql", sql)
    assert {(s.kind, s.name) for s in fs.symbols} == {("table", "public.orders"), ("view", "active_orders")}


def test_symbol_rows_round_trip() -> None:
    fs = extract_file_symbols("m.py", "python", "import os\n\ndef run(x):\n    return go(x)\n")
    rows = symbol_rows("key", "sha", fs)
    assert {r["kind"] for r in rows} == {"function", "import"}  # no module-level calls: no "module" row
    script = extract_file_symbols("run.py", "python", "import app\n\napp.main()\nconfigure(app)\n")
    module = next(r for r in symbol_rows("key", "sha", script) if r["kind"] == "module")
    assert module["name"] == "run.py" and module["references"] == ["main", "configure"]
    func = next(r for r in rows if r["kind"] == "function")

    class Row:
        def __init__(self, d: dict[str, object]) -> None:
            self.__dict__.update(d)

    rec = row_to_symbol(Row(func))
    assert rec.signature == "def run(x)" and rec.references == ["go"] and rec.start_line == 3


def test_module_resolution_python_js_php() -> None:
    files = [
        "src/app/__init__.py",
        "src/app/core.py",
        "src/app/sub/__init__.py",
        "src/app/sub/leaf.py",
        "scripts/tool.py",
        "web/lib/util.ts",
        "web/lib/index.js",
        "web/main.js",
        "web/components/Button/index.tsx",
        "php/src/Models/User.php",
        "php/lib/legacy.php",
        "php/public/index.php",
    ]
    r = ModuleResolver(files, psr4={"App\\": ["php/src"]})
    # python: package chain (src layout), relative imports, dotted from-imports, external modules
    assert r.resolve("src/app/sub/leaf.py", "python", ImportRecord(module="app.core", kind="import")) == "src/app/core.py"
    assert r.resolve("src/app/sub/leaf.py", "python", ImportRecord(module="core", kind="from", level=2, names=["x"])) == "src/app/core.py"
    assert r.resolve("src/app/core.py", "python", ImportRecord(module="", kind="from", level=1, names=["sub"])) == "src/app/sub/__init__.py"
    assert r.resolve("scripts/tool.py", "python", ImportRecord(module="app.sub", kind="from", names=["leaf"])) == "src/app/sub/leaf.py"
    assert r.resolve("scripts/tool.py", "python", ImportRecord(module="requests", kind="import")) is None
    # js/ts: extension and index probing, .js -> .ts mapping, bare specifiers stay external
    assert r.resolve("web/main.js", "javascript", ImportRecord(module="./lib/util.js")) == "web/lib/util.ts"
    assert r.resolve("web/main.js", "javascript", ImportRecord(module="./lib")) == "web/lib/index.js"
    assert r.resolve("web/main.js", "javascript", ImportRecord(module="./components/Button")) == "web/components/Button/index.tsx"
    assert r.resolve("web/main.js", "javascript", ImportRecord(module="react")) is None
    assert r.resolve("web/main.js", "javascript", ImportRecord(module="../../outside")) is None
    # php: PSR-4, include literals relative to the file or the include path
    assert r.resolve("php/public/index.php", "php", ImportRecord(module="App\\Models\\User", kind="use")) == "php/src/Models/User.php"
    assert r.resolve("php/public/index.php", "php", ImportRecord(module="/../lib/legacy.php", kind="include-dir")) == "php/lib/legacy.php"
    assert r.resolve("php/public/index.php", "php", ImportRecord(module="php/lib/legacy.php", kind="include")) == "php/lib/legacy.php"
    assert r.resolve("php/public/index.php", "php", ImportRecord(module="Vendor\\Pkg\\Thing", kind="use")) is None
    # never a self edge
    assert r.resolve("web/lib/index.js", "javascript", ImportRecord(module="./index.js")) is None


def test_composer_psr4_maps_are_workspace_relative() -> None:
    data = {"autoload": {"psr-4": {"App\\": "src/"}}, "autoload-dev": {"psr-4": {"Tests\\": ["tests/", "more/"]}}}
    assert psr4_from_composer(data, base_dir="php") == {"App\\": ["php/src"], "Tests\\": ["php/tests", "php/more"]}
    assert psr4_from_composer({"autoload": {"psr-4": {"X\\": "../escape"}}}) == {}
    assert psr4_from_composer(None) == {}
    assert merge_psr4([{"A\\": ["a"]}, {"A\\": ["a", "b"]}]) == {"A\\": ["a", "b"]}
    assert composer_manifests(["composer.json", "php/composer.json", "vendor/x/composer.json", "node_modules/y/composer.json"]) == [
        "composer.json",
        "php/composer.json",
    ]


def test_dependency_graph() -> None:
    g = DependencyGraph.from_imports(
        {
            "a.py": [ImportRecord(module="b", resolved="b.py"), ImportRecord(module="x", resolved=None)],
            "c.py": [ImportRecord(module="b", resolved="b.py")],
            "gone.py": [ImportRecord(module="b", resolved="b.py")],
        },
        drop=["gone.py"],
    )
    assert g.imports["a.py"] == {"b.py"} and g.importers["b.py"] == {"a.py", "c.py"}
    assert g.neighbors("b.py") == {"a.py", "c.py"} and g.edge_count() == 2


def test_symbol_aligned_chunking() -> None:
    cfg = RepoIntelConfig(chunk_max_tokens=120, chunk_min_tokens=10, chunk_overlap_lines=2)
    funcs = []
    for name in ("first", "second", "third"):
        body = "".join(f"    v{i} = compute_{name}({i})\n" for i in range(8))
        funcs.append(f"def {name}():\n{body}    return v0\n")
    huge_body = "".join(f"        x{i} = {i} * 2  # filler line {i}\n" for i in range(60))
    src = "import os\n\n\n" + "\n\n".join(funcs) + f"\n\nclass Big:\n    def m1(self):\n{huge_body}\n    def m2(self):\n        return 1\n"
    fs = extract_file_symbols("mod.py", "python", src)
    assert fs.parse_error is None
    chunks = Chunker(cfg).chunk("mod.py", "python", src, fs.symbols)
    max_chars = int(cfg.chunk_max_tokens * CHARS_PER_TOKEN)
    assert all(len(c.content) <= max_chars for c in chunks)
    by_symbol = {name: c for c in chunks if c.symbol for name in c.symbol.split(", ")}
    assert {"first", "second", "third"} <= set(by_symbol)  # small neighbours may share a chunk
    second = by_symbol["second"]
    assert second.symbol == "second" and second.content.startswith("def second():")
    assert src.splitlines()[second.start_line - 1] == "def second():" and second.content.rstrip().endswith("return v0")
    # an oversized method is split into overlapping windows, named after class + member
    m1 = [c for c in chunks if c.symbol == "Big.m1"]
    assert len(m1) >= 2
    assert m1[1].start_line <= m1[0].end_line  # overlap
    assert any(c.symbol == "Big.m2" or (c.symbol and "Big.m2" in c.symbol) for c in chunks)
    # line ranges cover the file content without gaps between consecutive non-overlapping chunks
    lines = src.splitlines()
    for c in chunks:
        assert c.content.splitlines()[0] == lines[c.start_line - 1]
    # content hash is over the exact (redacted) embedding input
    c0 = chunks[0]
    assert c0.content_hash == content_hash(document_text("mod.py", c0.symbol, c0.content, cfg))
    assert Chunker(cfg).chunk("empty.py", "python", "", []) == []
    # files without any symbol (docs, SQL, config) are windowed completely, not cut after the first chunk
    doc = "".join(f"Paragraph {i} explains part {i} of the system in plain words.\n" for i in range(80))
    doc_chunks = Chunker(cfg).chunk("README.md", "markdown", doc, [])
    assert len(doc_chunks) > 5 and doc_chunks[-1].end_line == 80 and doc_chunks[0].start_line == 1
    assert all(len(c.content) <= max_chars for c in doc_chunks)


def test_chunk_embedding_input_is_redacted() -> None:
    src = "DB_PASSWORD = 'hunter2-very-secret'\napi_key = sk-abcdefghijklmnopqrstuvwxyz\n"
    [chunk] = Chunker().chunk("settings.py", "python", src, [])
    assert "hunter2-very-secret" in chunk.content  # stored content stays exact (patches need it)
    assert "hunter2-very-secret" not in chunk.embed_text and "sk-abcdefghijklmnopqrstuvwxyz" not in chunk.embed_text


def test_query_analysis() -> None:
    q = analyze("Fix `calculate_invoice_total` in App\\Services\\Billing and the POST /api/v1/orders route for users")
    assert "calculate_invoice_total" in q.phrases
    assert {"calculate_invoice_total", "App\\Services\\Billing"} <= set(q.identifiers)
    assert "/api/v1/orders" in q.routes
    assert {"calculate", "invoice", "total", "billing", "order", "user"} <= set(q.terms)
    assert "the" not in q.terms and "fix" not in q.terms
    assert split_identifier("getHTTPResponseCode") == ["get", "http", "response", "code"]
    assert split_identifier("user_id") == ["user", "id"]
    assert stem("entries") == "entry" and stem("classes") == "class" and stem("users") == "user" and stem("class") == "class"
    german = analyze("Bitte die Funktion für Rechnungen anpassen")
    assert "rechnungen" in german.terms and "bitte" not in german.terms
    only_stop = analyze("the and or")
    assert not only_stop.is_empty()
    assert analyze("").is_empty()
