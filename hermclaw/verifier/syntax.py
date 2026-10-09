"""Per-file syntax checks (P21 21.2).

- in-process (parse only, never executes project code): Python (``compile``), JSON (strict, JSONC fallback for
  comment-style config files), YAML (safe loader that tolerates application tags), TOML (``tomllib``);
- local parse-only tool: shell via ``bash -n`` (falls back to the sandbox when bash is missing locally);
- in the sandbox, batched per language: PHP (``php -l``) and JavaScript (``node --check``);
- TypeScript/JSX are covered by the compile check (``tsc``); other languages are skipped with a reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import tomllib
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hermclaw.verifier.commands import CommandRunner

SYNTAX_TIMEOUT_SECONDS = 300
_TEMPLATE_MARKERS = ("{{", "{%")
_JSX = re.compile(r"(?:^|[=(,:?&|]\s*|return\s+)<[A-Za-z][\w.]*(?:\s[^<>]*)?/?>|</[A-Za-z][\w.]*>", re.M)
_ESM_HINTS = ("Cannot use import statement outside a module", "ERR_REQUIRE_ESM", "Unexpected token 'export'")


@dataclass
class SyntaxResult:
    path: str
    language: str
    status: str  # pass | fail | skip | error
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------------------------- in-process parsers
def check_python(path: str, text: str) -> SyntaxResult:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(text, path, "exec", dont_inherit=True)
    except SyntaxError as exc:
        return SyntaxResult(path, "python", "fail", f"SyntaxError: {exc.msg}", {"line": exc.lineno, "column": exc.offset})
    except ValueError as exc:  # e.g. source with NUL bytes
        return SyntaxResult(path, "python", "fail", f"invalid source: {exc}")
    except (RecursionError, MemoryError, OverflowError) as exc:
        return SyntaxResult(path, "python", "fail", f"source cannot be compiled ({type(exc).__name__}: too deeply nested or too large)")
    return SyntaxResult(path, "python", "pass", "compiles")


def strip_jsonc(text: str) -> str:
    """Remove ``//`` / ``/* */`` comments and trailing commas outside strings (JSON-with-comments config files)."""
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def check_json(path: str, text: str) -> SyntaxResult:
    try:
        json.loads(text)
    except RecursionError:
        return SyntaxResult(path, "json", "fail", "JSON document is too deeply nested")
    except json.JSONDecodeError as exc:
        try:
            json.loads(strip_jsonc(text))
        except json.JSONDecodeError:
            return SyntaxResult(path, "json", "fail", f"invalid JSON: {exc.msg}", {"line": exc.lineno, "column": exc.colno})
        return SyntaxResult(path, "json", "pass", "valid JSON with comments (JSONC)")
    return SyntaxResult(path, "json", "pass", "valid JSON")


class TolerantYamlLoader(yaml.SafeLoader):
    """SafeLoader that accepts application-specific tags (``!Ref``, ``!vault`` …) as plain values."""


def _construct_tagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return None  # pragma: no cover - PyYAML only produces the three node kinds


TolerantYamlLoader.add_multi_constructor("!", _construct_tagged)
TolerantYamlLoader.add_multi_constructor("tag:", _construct_tagged)


def load_yaml_documents(text: str) -> list[Any]:
    return list(yaml.load_all(text, Loader=TolerantYamlLoader))


def check_yaml(path: str, text: str) -> SyntaxResult:
    if any(m in text for m in _TEMPLATE_MARKERS):
        return SyntaxResult(path, "yaml", "skip", "templated YAML (Jinja/Helm/Go template markers) cannot be parsed without rendering")
    try:
        load_yaml_documents(text)
    except RecursionError:
        return SyntaxResult(path, "yaml", "fail", "YAML document is too deeply nested")
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        return SyntaxResult(path, "yaml", "fail", f"invalid YAML: {problem}", {"line": line})
    return SyntaxResult(path, "yaml", "pass", "valid YAML")


def check_toml(path: str, text: str) -> SyntaxResult:
    try:
        tomllib.loads(text)
    except RecursionError:
        return SyntaxResult(path, "toml", "fail", "TOML document is too deeply nested")
    except tomllib.TOMLDecodeError as exc:
        return SyntaxResult(path, "toml", "fail", f"invalid TOML: {exc}")
    return SyntaxResult(path, "toml", "pass", "valid TOML")


IN_PROCESS = {"python": check_python, "json": check_json, "yaml": check_yaml, "toml": check_toml}


# ------------------------------------------------------------------------------------------- shell (bash -n)
async def check_shell_local(root: Path, paths: Sequence[str], *, timeout_seconds: float = 30.0) -> list[SyntaxResult] | None:
    """``bash -n`` (parse only) on the orchestrator copy; ``None`` when bash is not installed locally."""
    bash = shutil.which("bash")
    if bash is None:
        return None
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C"}
    results: list[SyntaxResult] = []
    for rel in paths:
        proc = await asyncio.create_subprocess_exec(
            bash,
            "--norc",
            "--noprofile",
            "-n",
            "./" + rel,
            cwd=str(root),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), timeout_seconds)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            results.append(SyntaxResult(rel, "shell", "error", "bash -n timed out"))
            continue
        if proc.returncode == 0:
            results.append(SyntaxResult(rel, "shell", "pass", "bash -n: no syntax errors"))
        else:
            msg = err.decode("utf-8", errors="replace").strip()[:2000]
            results.append(SyntaxResult(rel, "shell", "fail", f"bash -n: {msg or 'syntax error'}", {"exit_code": proc.returncode}))
    return results


# ------------------------------------------------------------------------------------------- sandbox batches
SANDBOX_TOOLS: dict[str, tuple[str, str]] = {
    # language -> (tool, command template applied to one quoted "./path")
    "php": ("php", "php -l {path}"),
    "javascript": ("node", "node --check {path}"),
    "shell": ("bash", "bash -n {path}"),
}


def build_batch_script(language: str, paths: Sequence[str], nonce: str) -> str:
    _tool, template = SANDBOX_TOOLS[language]
    parts = []
    for idx, rel in enumerate(paths):
        cmd = template.format(path=shlex.quote("./" + rel))
        parts.append(f"printf '\\n@@HC-{nonce}-B {idx}@@\\n'; {cmd} 2>&1; rc=$?; printf '\\n@@HC-{nonce}-E {idx} %s@@\\n' \"$rc\"")
    return "; ".join(parts) + "; exit 0"


def parse_batch_output(output: str, count: int, nonce: str) -> dict[int, tuple[int | None, str]]:
    """``{index: (exit code, output)}`` for every file whose end marker was seen."""
    results: dict[int, tuple[int | None, str]] = {}
    pattern = re.compile(rf"@@HC-{nonce}-B (\d+)@@\n(.*?)\n@@HC-{nonce}-E \1 (-?\d+)@@", re.S)
    for m in pattern.finditer(output):
        idx = int(m.group(1))
        if 0 <= idx < count:
            results[idx] = (int(m.group(3)), m.group(2).strip())
    return results


async def check_in_sandbox(runner: CommandRunner, language: str, paths: Sequence[str], *, network: bool = False) -> list[SyntaxResult]:
    tool = SANDBOX_TOOLS[language][0]
    nonce = secrets.token_hex(6)
    script = build_batch_script(language, paths, nonce)
    outcome = await runner.run(script, purpose="verifier", timeout_seconds=SYNTAX_TIMEOUT_SECONDS, network=network)
    if outcome.failed_to_run or outcome.timed_out:
        reason = "timed out" if outcome.timed_out else (outcome.problem or "executor error")
        return [SyntaxResult(p, language, "error", f"{tool} syntax check could not run: {reason}") for p in paths]
    parsed = parse_batch_output(outcome.stdout, len(paths), nonce)
    results: list[SyntaxResult] = []
    for idx, rel in enumerate(paths):
        if idx not in parsed:
            results.append(SyntaxResult(rel, language, "error", f"{tool} produced no result for this file"))
            continue
        rc, text = parsed[idx]
        text = runner.clip(text, 2000)
        if rc == 0:
            results.append(SyntaxResult(rel, language, "pass", f"{tool}: no syntax errors"))
        elif rc in (126, 127):
            results.append(SyntaxResult(rel, language, "skip", f"{tool} is not available in the sandbox image"))
        elif language == "javascript" and any(h in text for h in _ESM_HINTS):
            results.append(SyntaxResult(rel, language, "skip", "module-type detection failed (ESM syntax in a CommonJS context)"))
        else:
            results.append(SyntaxResult(rel, language, "fail", f"{tool}: {text or 'syntax error'}", {"exit_code": rc}))
    return results


def javascript_needs_compile_check(path: str, text: str) -> str | None:
    """Reason to leave a JavaScript file to the compile/lint stage (JSX cannot be parsed by ``node --check``)."""
    if path.endswith(".jsx"):
        return "JSX file: covered by the compile check / lint"
    if _JSX.search(text):
        return "contains JSX: covered by the compile check / lint"
    return None
