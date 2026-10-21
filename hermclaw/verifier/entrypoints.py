"""Start the delivered program once (BUG-309).

A step can pass its own tests and still ship a program that does not start: the tests build the server in-process and
never run ``__main__.py`` (coder tournament rc62: ``from .server import make_server`` of a renamed function, an
``import sys`` inside a branch that made ``sys`` local to ``main()`` – UnboundLocalError on start, which ruff does not
see). For every Python package with a ``__main__.py`` that the change touches, the verifier runs ``python -m <package>``
in the sandbox:

* from a temporary directory (relative default files such as ``tasks.db`` land there, not in the workspace), with the
  repository root and ``src/`` on ``PYTHONPATH``, stdin closed;
* environment variables the package reads are set when their meaning is clear from the name: ``*PORT*`` a free port,
  ``*HOST*`` 127.0.0.1, ``*_DIR``/``*_DB``/``*_FILE``/``*_PATH`` a path in the temporary directory, ``*TOKEN*``/
  ``*SECRET*``/``*KEY``/``*PASSWORD*`` a placeholder – everything else keeps the program's own default;
* still running after ``policies.verifier.start_seconds`` (a server) or exited with code 0 (a command): passed; exited
  with an error code and no traceback (a documented refusal such as a missing setting): passed with the message;
* a traceback whose exception says the code itself is broken (:data:`START_ERRORS`) fails the step; any other traceback
  is reported without blocking (it may be a missing argument the program does not handle).

Parse-only on the verifier host; the program runs only in the sandbox.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from hermclaw.repo_intelligence.paths import is_test_path

#: exceptions that mean the program cannot start as written, whatever its arguments and settings
START_ERRORS = frozenset(
    {
        "ImportError",
        "ModuleNotFoundError",
        "NameError",
        "UnboundLocalError",
        "SyntaxError",
        "IndentationError",
        "TabError",
        "AttributeError",
    }
)
MAX_PACKAGES = 3
MAX_SCAN_FILES = 60
MAX_SCAN_BYTES = 1024 * 1024
#: variables of the process environment itself, never overridden
_SYSTEM = frozenset({"PATH", "HOME", "PWD", "USER", "SHELL", "TMPDIR", "LANG", "TERM", "PYTHONPATH", "VIRTUAL_ENV"})
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*)(?::|$)")

#: runs in the sandbox with the project's interpreter: ``<module> <seconds> NAME=kind ...`` → one JSON line
START_SCRIPT = """\
import json, os, signal, socket, subprocess, sys, tempfile, time
module, seconds, wanted = sys.argv[1], float(sys.argv[2]), sys.argv[3:]
root = os.getcwd()
tmp = tempfile.mkdtemp(prefix="hermclaw-start-")
env = dict(os.environ)
env["PYTHONPATH"] = os.pathsep.join([root, os.path.join(root, "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
env["PYTHONDONTWRITEBYTECODE"] = "1"
env["PYTHONUNBUFFERED"] = "1"
for item in wanted:
    name, kind = item.split("=", 1)
    if kind == "port":
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        env[name] = str(s.getsockname()[1])
        s.close()
    elif kind == "dir":
        env[name] = os.path.join(tmp, name.lower())
        os.makedirs(env[name], exist_ok=True)
    elif kind == "file":
        env[name] = os.path.join(tmp, name.lower())
    elif kind == "host":
        env[name] = "127.0.0.1"
    else:
        env[name] = "hermclaw-start-check"
err = open(os.path.join(tmp, "stderr"), "w+b")
proc = subprocess.Popen([sys.executable, "-m", module], cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
deadline = time.monotonic() + seconds
while proc.poll() is None and time.monotonic() < deadline:
    time.sleep(0.1)
running = proc.poll() is None
if running:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
            proc.wait(timeout=3)
            break
        except (OSError, subprocess.TimeoutExpired):
            pass
err.seek(0)
print(json.dumps({"running": running, "exit_code": None if running else proc.returncode,
                  "stderr": err.read()[-6000:].decode("utf-8", "replace"),
                  "env": sorted(item.split("=", 1)[0] for item in wanted)}))
"""


@dataclass(frozen=True)
class EntryPoint:
    module: str  # dotted name for ``python -m``
    directory: str  # repository-relative package directory


def _module_of(directory: str) -> str | None:
    parts = directory.split("/")
    if parts and parts[0] == "src":
        parts = parts[1:]
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


def entry_points(root: Path, changed: Iterable[str]) -> list[EntryPoint]:
    """Packages with a ``__main__.py`` that contain a changed file (nearest package first, test packages skipped)."""
    found: dict[str, EntryPoint] = {}
    for path in sorted(changed):
        if not path.endswith(".py") or is_test_path(path):
            continue
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        while parent:
            if (root / parent / "__main__.py").is_file():
                module = _module_of(parent)
                if module is not None and parent not in found:
                    found[parent] = EntryPoint(module, parent)
                break
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
    return list(found.values())[:MAX_PACKAGES]


def _kind(name: str) -> str | None:
    if name in _SYSTEM or not _ENV_NAME.fullmatch(name):
        return None
    if "PORT" in name:
        return "port"
    if "HOST" in name:
        return "host"
    if name.endswith(("_DIR", "_DIRECTORY", "_ROOT")):
        return "dir"
    if name.endswith(("_DB", "_DATABASE", "_FILE", "_PATH")) or name in {"DB", "DATABASE"}:
        return "file"
    if any(word in name for word in ("TOKEN", "SECRET", "PASSWORD")) or name.endswith(("_KEY", "APIKEY")):
        return "secret"
    return None


def _is_environ(node: ast.expr | None) -> bool:
    return (isinstance(node, ast.Name) and node.id == "environ") or (isinstance(node, ast.Attribute) and node.attr == "environ")


def _env_reads(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            target = func.value if isinstance(func, ast.Attribute) else None
            if (name == "getenv" or (name in {"get", "setdefault"} and _is_environ(target))) and isinstance(node.args[0].value, str):
                out.add(node.args[0].value)
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            if _is_environ(node.value):
                out.add(node.slice.value)
    return out


def start_environment(root: Path, entry: EntryPoint) -> dict[str, str]:
    """``{variable: kind}`` for the variables the package reads whose meaning the name makes clear."""
    names: set[str] = set()
    files = sorted((root / entry.directory).rglob("*.py"))[:MAX_SCAN_FILES]
    for file in files:
        try:
            if file.stat().st_size > MAX_SCAN_BYTES:
                continue
            names |= _env_reads(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return {n: k for n in sorted(names) if (k := _kind(n)) is not None}


def start_command(interpreter: Sequence[str], module: str, seconds: float, env: dict[str, str]) -> str:
    launcher = " ".join(shlex.quote(w) for w in interpreter)
    wanted = " ".join(shlex.quote(f"{n}={k}") for n, k in sorted(env.items()))
    return f"{launcher} -c {shlex.quote(START_SCRIPT)} {shlex.quote(module)} {seconds:g} {wanted}".rstrip()


@dataclass(frozen=True)
class StartResult:
    running: bool
    exit_code: int | None
    stderr: str
    exception: str | None  # last exception name of a traceback, ``None`` without one

    @property
    def broken(self) -> bool:
        return self.exception is not None and self.exception.rsplit(".", 1)[-1] in START_ERRORS


def parse_start_output(output: str) -> StartResult | None:
    """The driver's JSON line (the last line that parses), ``None`` when the driver did not report."""
    for line in reversed(output.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "running" in data:
            stderr = str(data.get("stderr") or "")
            return StartResult(bool(data["running"]), data.get("exit_code"), stderr, traceback_exception(stderr))
    return None


def traceback_exception(stderr: str) -> str | None:
    """The exception a Python traceback in ``stderr`` ends with."""
    if "Traceback (most recent call last)" not in stderr:
        return None
    tail = stderr[stderr.rindex("Traceback (most recent call last)") :]
    for line in reversed(tail.splitlines()):
        if line and not line[0].isspace():
            match = _EXCEPTION.match(line)
            if match and not line.startswith("Traceback"):
                return match.group(1)
    return None
