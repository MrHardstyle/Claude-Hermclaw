"""Generic HTTP route extraction (inventory "Routes", structural index kind=route).

Framework conventions only – decorators/registrations of the common web frameworks (FastAPI/Flask/Starlette/Django,
Express/Koa/Fastify/NestJS/Next.js file routes, Laravel/Slim/Symfony, plain PHP superglobal handlers, Go net/http,
gin/echo/chi, Spring, Rails). Nothing here knows about a specific project.
"""

from __future__ import annotations

import ast
import bisect
import re
from collections.abc import Iterable

from hermclaw.repo_intelligence.schemas import Route

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")
_PY_ROUTE_ATTRS = {*HTTP_METHODS, "route", "api_route", "websocket", "view"}
_PY_REGISTER_ATTRS = {"add_url_rule", "add_api_route", "add_route", "add_websocket_route"}
_PY_FRAMEWORKS = ("fastapi", "flask", "starlette", "sanic", "quart", "aiohttp", "django", "litestar", "falcon", "bottle")
MAX_ROUTES_PER_FILE = 500


class _Lines:
    def __init__(self, text: str) -> None:
        self.starts = [0]
        for m in re.finditer("\n", text):
            self.starts.append(m.end())

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)


def _join(prefix: str, path: str) -> str:
    if not prefix:
        return path or "/"
    if not path:
        return prefix
    return prefix.rstrip("/") + "/" + path.lstrip("/")


# ============================================================================================= python
def _py_framework(tree: ast.Module) -> str:
    for node in tree.body:
        mod = None
        if isinstance(node, ast.Import):
            mod = node.names[0].name if node.names else None
        elif isinstance(node, ast.ImportFrom):
            mod = node.module
        if mod:
            top = mod.split(".")[0]
            if top in _PY_FRAMEWORKS:
                return top
    return "generic"


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _kw(call: ast.Call, *names: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg in names:
            return kw.value
    return None


def _methods(node: ast.AST | None) -> list[str]:
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        out = [v.upper() for e in node.elts if (v := _const_str(e))]
        return out
    return []


def _obj_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _python_prefixes(tree: ast.Module) -> dict[str, str]:
    """``router = APIRouter(prefix="/x")`` / ``bp = Blueprint("n", __name__, url_prefix="/x")``."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                prefix = _const_str(_kw(node.value, "prefix", "url_prefix"))
                if prefix:
                    out[target.id] = prefix
    return out


def _python_routes(path: str, text: str) -> list[Route]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _regex_routes(path, text, _PY_FALLBACK, "generic")
    framework = _py_framework(tree)
    prefixes = _python_prefixes(tree)
    routes: list[Route] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                attr = dec.func.attr
                if attr not in _PY_ROUTE_ATTRS:
                    continue
                rpath = _const_str(dec.args[0]) if dec.args else _const_str(_kw(dec, "path", "rule"))
                if rpath is None or not (rpath.startswith("/") or rpath == ""):
                    continue
                prefix = prefixes.get(_obj_name(dec.func.value) or "", "")
                if attr in HTTP_METHODS:
                    methods = [attr.upper()]
                elif attr == "websocket":
                    methods = ["WS"]
                else:
                    methods = _methods(_kw(dec, "methods")) or ["GET"]
                for m in methods:
                    routes.append(
                        Route(method=m, path=_join(prefix, rpath), handler=node.name, file=path, line=dec.lineno, framework=framework)
                    )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _PY_REGISTER_ATTRS:
            rpath = _const_str(node.args[0]) if node.args else _const_str(_kw(node, "path", "rule"))
            if rpath is None or not rpath.startswith("/"):
                continue
            handler_node = node.args[1] if len(node.args) > 1 else _kw(node, "view_func", "endpoint", "route")
            handler = _obj_name(handler_node) if handler_node is not None else None
            methods = _methods(_kw(node, "methods")) or (["WS"] if "websocket" in node.func.attr else ["GET"])
            prefix = prefixes.get(_obj_name(node.func.value) or "", "")
            for m in methods:
                routes.append(Route(method=m, path=_join(prefix, rpath), handler=handler, file=path, line=node.lineno, framework=framework))
        elif (
            path.rsplit("/", 1)[-1] == "urls.py"
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("path", "re_path", "url")
            and node.args
        ):
            rpath = _const_str(node.args[0])
            if rpath is None:
                continue
            handler = None
            if len(node.args) > 1:
                h = node.args[1]
                if isinstance(h, ast.Call) and isinstance(h.func, ast.Attribute) and h.func.attr == "as_view":
                    handler = _obj_name(h.func.value)
                else:
                    handler = _obj_name(h)
            routes.append(
                Route(method="ANY", path="/" + rpath.lstrip("^/"), handler=handler, file=path, line=node.lineno, framework="django")
            )
        if len(routes) >= MAX_ROUTES_PER_FILE:
            break
    return routes


_PY_FALLBACK = [
    (re.compile(r"@\s*([A-Za-z_]\w*)\s*\.\s*(get|post|put|patch|delete|head|options|route|api_route)\s*\(\s*([\"'])(/[^\"']*)\3"), 2, 4),
]


# ============================================================================================= javascript / typescript
_JS_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\.\s*(get|post|put|patch|delete|del|all|options|head|use)\s*\(\s*([\"'`])(/[^\"'`]*|\*)\3")
_JS_ROUTE_CHAIN = re.compile(r"\.\s*route\s*\(\s*([\"'`])(/[^\"'`]*)\1\s*\)")
_JS_CHAIN_METHOD = re.compile(r"\s*\.\s*(get|post|put|patch|delete|all|options|head)\s*\(")
_JS_FASTIFY = re.compile(r"\bmethod\s*:\s*[\"'](\w+)[\"'][^{}]{0,300}?\burl\s*:\s*[\"'](/[^\"']*)[\"']", re.S)
_NEST_CONTROLLER = re.compile(r"@Controller\s*\(\s*(?:[\"'`]([^\"'`]*)[\"'`])?")
_NEST_METHOD = re.compile(r"@(Get|Post|Put|Patch|Delete|Options|Head|All)\s*\(\s*(?:[\"'`]([^\"'`]*)[\"'`])?\s*\)")
_JS_METHOD_NAME = re.compile(r"^\s*(?:(?:public|private|protected|static|async|override)\s+)*([A-Za-z_$][\w$]*)\s*\(", re.M)
_NEXT_EXPORT = re.compile(r"export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b")


def _js_framework(text: str) -> str:
    for fw in ("@nestjs", "express", "fastify", "koa", "hono", "next"):
        if re.search(r"""(?:require\(\s*|from\s+)["']""" + re.escape(fw), text):
            return fw.lstrip("@")
    return "generic"


def _js_routes(path: str, text: str) -> list[Route]:
    lines = _Lines(text)
    fw = _js_framework(text)
    routes: list[Route] = []
    for m in _JS_CALL.finditer(text):
        method = m.group(2).upper()
        method = {"DEL": "DELETE", "ALL": "ANY"}.get(method, method)
        routes.append(Route(method=method, path=m.group(4), handler=None, file=path, line=lines.line(m.start()), framework=fw))
    for m in _JS_ROUTE_CHAIN.finditer(text):
        rest = text[m.end() : m.end() + 2000]
        pos = 0
        while True:
            cm = _JS_CHAIN_METHOD.match(rest, pos)
            if not cm:
                break
            routes.append(Route(method=cm.group(1).upper(), path=m.group(2), file=path, line=lines.line(m.start()), framework=fw))
            # skip to the end of this call's argument list (balanced parentheses)
            depth, i = 0, cm.end() - 1
            while i < len(rest):
                if rest[i] == "(":
                    depth += 1
                elif rest[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            pos = i + 1
    for m in _JS_FASTIFY.finditer(text):
        routes.append(Route(method=m.group(1).upper(), path=m.group(2), file=path, line=lines.line(m.start()), framework=fw))
    ctrl = _NEST_CONTROLLER.search(text)
    if ctrl:
        prefix = "/" + (ctrl.group(1) or "").strip("/")
        for m in _NEST_METHOD.finditer(text):
            name_m = _JS_METHOD_NAME.search(text, m.end())
            routes.append(
                Route(
                    method=m.group(1).upper().replace("ALL", "ANY"),
                    path=_join(prefix, m.group(2) or ""),
                    handler=name_m.group(1) if name_m else None,
                    file=path,
                    line=lines.line(m.start()),
                    framework="nestjs",
                )
            )
    routes.extend(_next_file_routes(path, text, lines))
    return routes[:MAX_ROUTES_PER_FILE]


def _next_file_routes(path: str, text: str, lines: _Lines) -> list[Route]:
    parts = path.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    out: list[Route] = []
    if "app" in parts[:-1] and stem == "route":
        i = parts.index("app")
        segs = [p for p in parts[i + 1 : -1] if not (p.startswith("(") and p.endswith(")"))]
        rpath = "/" + "/".join(segs)
        for m in _NEXT_EXPORT.finditer(text):
            out.append(Route(method=m.group(1), path=rpath, handler=m.group(1), file=path, line=lines.line(m.start()), framework="next"))
    elif "pages" in parts[:-1] and "api" in parts[parts.index("pages") + 1 : -1] + ([stem] if stem == "api" else []):
        i = parts.index("pages")
        segs = parts[i + 1 : -1] + ([] if stem == "index" else [stem])
        out.append(Route(method="ANY", path="/" + "/".join(segs), handler="default", file=path, line=1, framework="next"))
    return out


# ============================================================================================= php
_PHP_LARAVEL = re.compile(
    r"\bRoute::(get|post|put|patch|delete|options|any|match|resource|apiResource|view|redirect)\s*\(\s*"
    r"(?:\[[^\]]*\]\s*,\s*)?([\"'])(.*?)\2\s*(?:,\s*([^;\n]{0,200}))?"
)
_PHP_OBJ = re.compile(
    r"\$([A-Za-z_]\w*)\s*->\s*(get|post|put|patch|delete|options|any|map)\s*\(\s*(?:\[[^\]]*\]\s*,\s*)?([\"'])(/[^\"']*)\3"
)
_PHP_ATTR = re.compile(r"#\[\s*Route\s*\(\s*(?:path\s*:\s*)?([\"'])([^\"']*)\1([^\]]*)\]")
_PHP_ANNOT = re.compile(r"@Route\s*\(\s*\"([^\"]*)\"([^)]*)\)")
_PHP_FUNC = re.compile(r"function\s+&?\s*([A-Za-z_]\w*)\s*\(")
_PHP_SUPERGLOBAL = re.compile(r"\$_(GET|POST|REQUEST|FILES)\s*\[\s*([\"'])([^\"']+)\2\s*\]")
_PHP_METHOD_CHECK = re.compile(r"REQUEST_METHOD['\"]\s*\]\s*={2,3}\s*['\"](GET|POST|PUT|PATCH|DELETE)['\"]", re.I)
_PHP_DOCROOTS = ("public", "web", "htdocs", "www", "public_html", "html")


def _laravel_handler(rest: str | None) -> str | None:
    if not rest:
        return None
    m = re.search(r"([A-Za-z_\\]\w*)::class\s*,\s*['\"](\w+)['\"]", rest)
    if m:
        return f"{m.group(1).rsplit(chr(92), 1)[-1]}@{m.group(2)}"
    m = re.search(r"['\"]([A-Za-z_\\][\w\\]*@\w+)['\"]", rest)
    if m:
        return m.group(1).rsplit("\\", 1)[-1]
    m = re.search(r"([A-Za-z_\\]\w*)::class", rest)
    if m:
        return m.group(1).rsplit("\\", 1)[-1]
    return "closure" if "function" in rest or "fn" in rest else None


def _methods_in(spec: str) -> list[str]:
    return [m.upper() for m in re.findall(r"['\"](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)['\"]", spec, re.I)]


def _php_routes(path: str, text: str) -> list[Route]:
    lines = _Lines(text)
    routes: list[Route] = []
    for m in _PHP_LARAVEL.finditer(text):
        verb = m.group(1)
        rpath = "/" + m.group(3).lstrip("/")
        if verb == "match":
            head = text[m.start() : m.start() + 200]
            methods = _methods_in(head.split(m.group(2) + m.group(3), 1)[0]) or ["ANY"]
        elif verb in ("resource", "apiResource"):
            methods = ["RESOURCE"]
        elif verb in ("any", "view", "redirect"):
            methods = ["ANY" if verb == "any" else "GET"]
        else:
            methods = [verb.upper()]
        for meth in methods:
            routes.append(
                Route(
                    method=meth,
                    path=rpath,
                    handler=_laravel_handler(m.group(4)),
                    file=path,
                    line=lines.line(m.start()),
                    framework="laravel",
                )
            )
    for m in _PHP_OBJ.finditer(text):
        meth = m.group(2).upper()
        routes.append(
            Route(
                method="ANY" if meth in ("ANY", "MAP") else meth, path=m.group(4), file=path, line=lines.line(m.start()), framework="slim"
            )
        )
    for rx, fw in ((_PHP_ATTR, "symfony"), (_PHP_ANNOT, "symfony")):
        for m in rx.finditer(text):
            groups = m.groups()
            rpath = groups[1] if rx is _PHP_ATTR else groups[0]
            spec = groups[2] if rx is _PHP_ATTR else groups[1]
            fn = _PHP_FUNC.search(text, m.end())
            for meth in _methods_in(spec) or ["ANY"]:
                routes.append(
                    Route(
                        method=meth,
                        path=rpath or "/",
                        handler=fn.group(1) if fn else None,
                        file=path,
                        line=lines.line(m.start()),
                        framework=fw,
                    )
                )
    routes.extend(_php_superglobal_routes(path, text, lines))
    return routes[:MAX_ROUTES_PER_FILE]


def _php_superglobal_routes(path: str, text: str, lines: _Lines) -> list[Route]:
    """A plain PHP script reading request superglobals is an endpoint addressed by its file path."""
    found = list(_PHP_SUPERGLOBAL.finditer(text))
    if not found:
        return []
    parts = path.split("/")
    for i, p in enumerate(parts[:-1]):
        if p in _PHP_DOCROOTS:
            parts = parts[i + 1 :]
            break
    url = "/" + "/".join(parts)
    by_method: dict[str, tuple[int, list[str]]] = {}
    for m in found:
        kind = m.group(1)
        meth = {"GET": "GET", "POST": "POST", "FILES": "POST"}.get(kind, "ANY")
        line, params = by_method.get(meth, (lines.line(m.start()), []))
        if m.group(3) not in params:
            params.append(m.group(3))
        by_method[meth] = (line, params)
    for m in _PHP_METHOD_CHECK.finditer(text):
        meth = m.group(1).upper()
        if meth not in by_method:
            by_method[meth] = (lines.line(m.start()), [])
    out = []
    for meth, (line, params) in sorted(by_method.items(), key=lambda kv: kv[1][0]):
        handler = f"$_{meth if meth in ('GET', 'POST') else 'REQUEST'}[{', '.join(params[:20])}]" if params else None
        out.append(Route(method=meth, path=url, handler=handler, file=path, line=line, framework="php"))
    return out


# ============================================================================================= other languages
_GO_ROUTES = [
    (re.compile(r"\.\s*(HandleFunc|Handle)\s*\(\s*\"((?:[A-Z]+\s+)?/[^\"]*)\""), 1, 2),
    (re.compile(r"\.\s*(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD|Any|Get|Post|Put|Patch|Delete)\s*\(\s*\"(/[^\"]*)\""), 1, 2),
]
_JAVA_ROUTES = [
    (re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?\"([^\"]*)\""), 1, 2),
]
_RUBY_ROUTES = [
    (re.compile(r"^\s*(get|post|put|patch|delete|match)\s+[\"']([^\"']+)[\"']", re.M), 1, 2),
    (re.compile(r"^\s*(resources?)\s+:(\w+)", re.M), 1, 2),
]


def _regex_routes(path: str, text: str, patterns: Iterable[tuple[re.Pattern[str], int, int]], framework: str) -> list[Route]:
    lines = _Lines(text)
    out: list[Route] = []
    for rx, mg, pg in patterns:
        for m in rx.finditer(text):
            meth = m.group(mg).upper()
            rpath = m.group(pg)
            if meth in ("HANDLEFUNC", "HANDLE"):
                if " " in rpath:  # Go 1.22 "GET /path" patterns
                    meth, rpath = rpath.split(None, 1)
                else:
                    meth = "ANY"
            elif meth == "REQUEST":
                meth = "ANY"
            elif meth in ("RESOURCE", "RESOURCES"):
                meth, rpath = "RESOURCE", "/" + rpath
            elif meth == "MATCH":
                meth = "ANY"
            if not rpath.startswith("/"):
                rpath = "/" + rpath
            out.append(Route(method=meth, path=rpath, file=path, line=lines.line(m.start()), framework=framework))
            if len(out) >= MAX_ROUTES_PER_FILE:
                return out
    return out


def extract_routes(path: str, language: str | None, text: str) -> list[Route]:
    """All routes declared in one file (deterministic order: line, method, path)."""
    if language == "python":
        routes = _python_routes(path, text)
    elif language in ("javascript", "typescript", "tsx"):
        routes = _js_routes(path, text)
    elif language == "php":
        routes = _php_routes(path, text)
    elif language == "go":
        routes = _regex_routes(path, text, _GO_ROUTES, "go")
    elif language in ("java", "kotlin"):
        routes = _regex_routes(path, text, _JAVA_ROUTES, "spring")
    elif language == "ruby" and path.endswith("routes.rb"):
        routes = _regex_routes(path, text, _RUBY_ROUTES, "rails")
    else:
        routes = []
    seen: set[tuple[str, str, int]] = set()
    unique: list[Route] = []
    for r in sorted(routes, key=lambda r: (r.line, r.method, r.path)):
        key = (r.method, r.path, r.line)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique
