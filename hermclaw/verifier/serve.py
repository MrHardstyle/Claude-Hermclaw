"""Start the delivered web server and load what its user opens (BUG-346).

Every step of a job can pass its own checks while the delivered program does not work as a whole: in live G1 on Strata
the collector served a page built into ``collector.py`` instead of the ``frontend/`` folder another step built, and the
frontend called ``/api/token``, a route a later hardening step had removed – unit tests, review and acceptance passed.
For a Python program in the project that runs an HTTP server under ``if __name__ == "__main__"`` – or, when there is
none, a module whose public start function (``serve()``, ``main()``) runs one without arguments (BUG-365) – the
verifier starts it once in the sandbox the way the job's own acceptance commands run it (one-shot options such as
``--once`` dropped) and

* waits until it listens (the port is read from ``/proc`` for the process group; ``*PORT*`` variables it reads get a
  free port, ``*HOST*`` 127.0.0.1);
* loads ``/`` (when the project has HTML pages; a JSON API has no start page) and every local script, stylesheet and
  image the page references – each must answer 200;
* calls every route the page's scripts name (``"/api/…"`` string literals, with the ``method`` written next to them or
  passed as an argument, ``requestJson('/api/test', 'POST')``) – 404, 405 and 501 mean the route does not exist;
  401/403/400 are fine, the probe has no token or body; a route that does not answer within the probe's time waits on
  something the sandbox lacks (BUG-358);
* checks that every HTML page of the project (outside tests, docs and templates) is served by it.

Programs that do not listen within ``policies.verifier.serve_seconds`` are skipped (they may need settings the sandbox
does not have). A problem fails a job-level review (the whole change must work together), and a single step that
changed the server program or the page/script the problem comes from (BUG-361); other problems of a single step are
reported without blocking, since a later step may still connect the parts. Parse-only on the verifier host; the
program runs only in the sandbox.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hermclaw.repo_intelligence.paths import is_test_path
from hermclaw.tools.shellread import core_command
from hermclaw.verifier.entrypoints import MAX_SCAN_BYTES, _env_reads, _kind

MAX_SERVERS = 2
MAX_SCAN_PY = 400
MAX_PAGES = 10
#: constructs of the standard library and the common frameworks that run an HTTP server
SERVER_MARKERS = re.compile(
    r"\b(?:HTTPServer|ThreadingHTTPServer|TCPServer|serve_forever|make_server|uvicorn\.run|web\.run_app|run_simple)\b|\bapp\.run\("
)
MAIN_GUARD = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", re.M)
#: options that make a server program do one thing and exit instead of serving
ONE_SHOT = frozenset({"--once", "--help", "-h", "--version", "--check", "--dry-run", "--list", "--print-config", "--validate", "--test"})
_SKIPPED_DIRS = ("docs/", "doc/", "node_modules/", ".git/", ".venv/", "venv/", "templates/", "__pycache__/")
_PYTHON = re.compile(r"(?:^|/)python(?:3(?:\.\d+)?)?$")
#: calls that serve until the program is stopped (``server.serve_forever()``, aiohttp ``web.run_app``, werkzeug)
_RUN_CALLS = frozenset({"serve_forever", "run_app", "run_simple"})
#: start functions in the order a user would look for them
_START_NAMES = ("main", "serve", "run", "start", "run_server", "serve_forever")

#: runs in the sandbox from the workspace root: ``<spec json>`` → one JSON line
SERVE_SCRIPT = r"""
import html.parser, json, os, re, signal, socket, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
spec = json.loads(sys.argv[1])
env = dict(os.environ)
probe = socket.socket()
probe.bind(("127.0.0.1", 0))
free = probe.getsockname()[1]
probe.close()
tmp = tempfile.mkdtemp(prefix="hermclaw-serve-")
for name, kind in spec["env"].items():
    env[name] = {"port": str(free), "host": "127.0.0.1", "dir": tmp, "file": os.path.join(tmp, name.lower())}.get(kind, "placeholder")
err = tempfile.TemporaryFile()
proc = subprocess.Popen(spec["command"], shell=True, env=env, stdin=subprocess.DEVNULL, stdout=err, stderr=err, start_new_session=True)

def group():
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                rest = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if rest[2] == str(proc.pid):
                out.append(d)
    return out

def listening():
    inodes = set()
    for pid in group():
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
        except OSError:
            continue
    ports = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(table).read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) > 9 and parts[3] == "0A" and parts[9] in inodes:
                ports.add(int(parts[1].rsplit(":", 1)[1], 16))
    return sorted(ports)

def fetch(method, url, body=None):
    req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", "") if exc.headers else "", exc.read(200_000).decode("utf-8", "replace")
    except Exception as exc:
        return None, "", f"{type(exc).__name__}: {exc}"

class Refs(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs, self.inline, self._script = [], [], False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self._script = not a.get("src")
            if a.get("src"):
                self.refs.append(a["src"])
        elif tag == "link" and a.get("href") and any(r in (a.get("rel") or "") for r in ("stylesheet", "icon", "manifest")):
            self.refs.append(a["href"])
        elif tag == "img" and a.get("src"):
            self.refs.append(a["src"])
    def handle_endtag(self, tag):
        if tag == "script":
            self._script = False
    def handle_data(self, data):
        if self._script:
            self.inline.append(data)

def local(ref):
    return ref and not re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", ref, re.I)

ROUTE = re.compile(r"(['" + '"' + r"`])(/[A-Za-z_][\w\-./]*)\1")
ASSET = re.compile(r"\.(?:js|mjs|css|png|jpe?g|gif|svg|ico|woff2?|ttf|map|html?)$", re.I)
result = {"serve": True, "listening": False, "port": None, "problems": [], "sources": [], "probes": []}
deadline = time.monotonic() + spec["seconds"]
ports = []
while time.monotonic() < deadline and proc.poll() is None:
    ports = listening()
    if ports:
        break
    time.sleep(0.2)
try:
    base = None
    for port in ports:
        if fetch("GET", f"http://127.0.0.1:{port}/")[0] is not None:
            base = f"http://127.0.0.1:{port}"
            result.update(listening=True, port=port)
            break
    if base is not None:
        problems, sources, probes = result["problems"], result["sources"], result["probes"]
        checked, routes = set(), set()

        def problem(text, *where):
            problems.append(text)
            sources.append([w for w in where if w])

        def check_page(page, body, ctype):
            if page in checked or not ("html" in ctype.lower() or body.lstrip()[:15].lower().startswith(("<!doctype html", "<html"))):
                return
            checked.add(page)
            parser = Refs()
            parser.feed(body)
            code = [(f"the page {page}", page, "\n".join(parser.inline))]
            for ref in dict.fromkeys(r for r in parser.refs if local(r)):
                url = urllib.parse.urljoin(base + page, ref)
                path = urllib.parse.urlparse(url).path
                st, ct, text = fetch("GET", url)
                probes.append(["GET", path, st])
                if st != 200:
                    problem(f"the page {page} loads {ref}, but GET {path} answered {st}", page, path)
                elif ref.split("?")[0].endswith((".js", ".mjs")) or "javascript" in ct:
                    code.append((path, path, text))
            for source, where, text in code:
                for m in ROUTE.finditer(text):
                    route = m.group(2)
                    if route in routes or ASSET.search(route) or len(routes) >= 20:
                        continue
                    routes.add(route)
                    near = re.search(r"method\s*:\s*['\"](\w+)", text[m.end(): m.end() + 300])
                    # requestJson('/api/test', 'POST', body) / request('POST', '/api/test'): the method as an argument
                    after = re.match(r"\s*,\s*['\"`](GET|POST|PUT|PATCH|DELETE)['\"`]", text[m.end(): m.end() + 40], re.I)
                    before = re.search(r"['\"`](GET|POST|PUT|PATCH|DELETE)['\"`]\s*,\s*$", text[max(m.start() - 40, 0): m.start()], re.I)
                    found = after or before or near
                    method = found.group(1).upper() if found else "GET"
                    st, _, err = fetch(method, base + route, b"{}" if method != "GET" else None)
                    probes.append([method, route, st])
                    if st in (404, 405, 501):
                        problem(f"{source} calls {method} {route}, but the server answered {st}: the route does not exist", where)
                    elif st is None and "timed out" not in err.lower():
                        problem(f"{source} calls {method} {route}, but the server gave no answer ({err[:160]})", where)

        status, ctype, body = fetch("GET", base + spec["path"])
        probes.append(["GET", spec["path"], status])
        if status == 200:
            check_page(spec["path"], body, ctype)
        elif spec["pages"]:  # a JSON API without pages has no start page (BUG-365)
            problem(f"GET {spec['path']} answered {status}: the page the user opens does not load")
        for rel in spec["pages"]:
            try:
                want = " ".join(open(rel, encoding="utf-8", errors="replace").read().split())
            except OSError:
                continue
            tries = ["/" + rel]
            if "/" in rel:
                tries.append("/" + rel.split("/", 1)[1])
            if rel.rsplit("/", 1)[-1] in ("index.html", "index.htm"):
                tries += ["/" + rel.rsplit("/", 1)[0] + "/" if "/" in rel else "/", "/"]
            tries = list(dict.fromkeys(tries))
            served = None
            for path in tries:
                st, ct, text = fetch("GET", base + path)
                if st == 200 and " ".join(text.split()) == want:
                    served = path
                    check_page(path, text, ct)
                    break
            probes.append(["PAGE", rel, served])
            if served is None:
                problem(f"{rel} is part of the project, but the running server does not serve it (tried {', '.join(tries)})", rel)
finally:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    result["exit_code"] = proc.poll()
    err.seek(0)
    result["stderr"] = err.read()[-3000:].decode("utf-8", "replace")
print(json.dumps(result))
"""


@dataclass(frozen=True)
class Server:
    path: str  # repository-relative script
    env: dict[str, str] = field(default_factory=dict)  # {variable: kind} it reads
    call: str | None = None  # a module without ``__main__`` guard: its start function, called without arguments


def _runs_server(func: ast.expr) -> bool:
    if not isinstance(func, ast.Attribute):
        return False
    return func.attr in _RUN_CALLS or (func.attr == "run" and isinstance(func.value, ast.Name) and func.value.id in ("uvicorn", "app"))


def start_function(source: str) -> str | None:
    """The public module-level function that runs a server and needs no argument (``serve()``, ``main()``)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    found: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        args = node.args
        if len(args.posonlyargs) + len(args.args) > len(args.defaults) or any(d is None for d in args.kw_defaults):
            continue
        if any(isinstance(n, ast.Call) and _runs_server(n.func) for n in ast.walk(node)):
            found.append(node.name)
    return min(found, key=lambda n: _START_NAMES.index(n) if n in _START_NAMES else len(_START_NAMES), default=None)


def _skipped(path: str) -> bool:
    return is_test_path(path) or any(f"/{d}" in f"/{path}" for d in _SKIPPED_DIRS)


def servers(root: Path, files: Iterable[str]) -> list[Server]:
    """Python scripts that run an HTTP server when started (``__main__`` guard and a server construct); without such a
    script, modules whose public start function runs one (BUG-365: live G1 rc91, ``api.py`` had ``serve()`` and no
    ``__main__`` block, so nothing was checked while the page it was meant to serve was never served)."""
    scripts: list[Server] = []
    modules: list[Server] = []
    for path in sorted(f for f in files if f.endswith(".py") and not _skipped(f))[:MAX_SCAN_PY]:
        try:
            target = root / path
            if target.stat().st_size > MAX_SCAN_BYTES:
                continue
            source = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not SERVER_MARKERS.search(source):
            continue
        env = {n: k for n in sorted(_env_reads(source)) if (k := _kind(n)) is not None}
        if MAIN_GUARD.search(source):
            scripts.append(Server(path, env))
            if len(scripts) >= MAX_SERVERS:
                break
        elif not path.endswith("__init__.py") and all(p.isidentifier() for p in path[:-3].split("/")):
            if (call := start_function(source)) is not None:
                modules.append(Server(path, env, call))
    # a script that starts the program wins: a module's helper next to it is not what the user runs
    return scripts or modules[:MAX_SERVERS]


def pages(files: Iterable[str]) -> list[str]:
    """The project's HTML pages a server is expected to serve (tests, docs and templates excluded)."""
    return sorted(f for f in files if f.lower().endswith((".html", ".htm")) and not _skipped(f))[:MAX_PAGES]


def serve_command(server: Server, interpreter: Sequence[str], hints: Iterable[str]) -> tuple[str, str]:
    """``(command, how)``: the server started as one of the job's commands runs it, one-shot options dropped; else
    the script alone with the project's interpreter."""
    for hint in hints:
        core = core_command(hint)
        if server.path not in core or any(sep in core for sep in (";", "|")):
            continue
        try:
            words = shlex.split(core)
        except ValueError:
            continue
        if not words or not _PYTHON.search(words[0]) or server.path not in words:
            continue
        kept = [w for w in words if w not in ONE_SHOT]
        return " ".join(shlex.quote(w) for w in kept), f"as the job's command '{hint.strip()[:120]}' runs it"
    if server.call is not None:
        module = server.path[:-3].replace("/", ".")
        launcher = [*interpreter, "-c", f"import {module}; {module}.{server.call}()"]
        how = f"through its start function {server.call}(), the module has no __main__ block"
        return " ".join(shlex.quote(w) for w in launcher), how
    launcher = [*interpreter, server.path]
    return " ".join(shlex.quote(w) for w in launcher), "with the project's interpreter"


def serve_driver(interpreter: Sequence[str], command: str, server: Server, page_paths: Sequence[str], seconds: float) -> str:
    spec = {"command": command, "path": "/", "env": server.env, "pages": list(page_paths), "seconds": seconds}
    launcher = " ".join(shlex.quote(w) for w in interpreter)
    return f"{launcher} -c {shlex.quote(SERVE_SCRIPT)} {shlex.quote(json.dumps(spec))}"


@dataclass(frozen=True)
class ServeResult:
    listening: bool
    port: int | None
    problems: list[str]
    probes: list[list[object]]
    exit_code: int | None
    stderr: str
    #: per problem, the page/script URL paths (``/app.js``) or project files that the problem comes from
    sources: list[list[str]] = field(default_factory=list)

    def caused_by(self, server: str, changed: Iterable[str]) -> list[str]:
        """The problems the change caused or must answer for: all of them when it changed the server program, else
        those whose page or script it changed (``/app.js`` is ``static/app.js``, ``/`` an ``index.html``)."""
        changed = set(changed)
        if server in changed:
            return list(self.problems)
        out = []
        for i, text in enumerate(self.problems):
            where = self.sources[i] if i < len(self.sources) else []
            if any(_serves(url, path) for url in where for path in changed):
                out.append(text)
        return out


def _serves(url: str, path: str) -> bool:
    """Whether the URL path (or project file) ``url`` of a problem is the project file ``path``."""
    if not url.startswith("/"):
        return url == path
    if url.endswith("/"):
        url += "index.html"
    return f"/{path}".endswith(url) or (url == "/index.html" and path.rsplit("/", 1)[-1] in ("index.html", "index.htm"))


def parse_serve_output(output: str) -> ServeResult | None:
    """The driver's JSON line (the last line that parses), ``None`` when the driver did not report."""
    for line in reversed(output.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("serve") is True:
            return ServeResult(
                bool(data.get("listening")),
                data.get("port"),
                [str(p) for p in data.get("problems") or []],
                [list(p) for p in data.get("probes") or [] if isinstance(p, list)],
                data.get("exit_code"),
                str(data.get("stderr") or ""),
                [[str(w) for w in ws] for ws in data.get("sources") or [] if isinstance(ws, list)],
            )
    return None


__all__ = [
    "SERVE_SCRIPT",
    "ServeResult",
    "Server",
    "pages",
    "parse_serve_output",
    "serve_command",
    "serve_driver",
    "servers",
    "start_function",
]
