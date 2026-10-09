"""Deterministic workspace inventory (Bauplan §14 Phase A; P11 11.1-11.3).

Everything is derived from file names and file contents with generic, documented conventions – identical input
produces an identical :class:`RepoInventory` (all lists are sorted; no timestamps inside the model).
"""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hermclaw.repo_intelligence import _proc
from hermclaw.repo_intelligence.config import RepoIntelConfig
from hermclaw.repo_intelligence.fileio import lstat_regular, read_bytes, read_text
from hermclaw.repo_intelligence.languages import category, detect_language, is_binary_name, looks_binary
from hermclaw.repo_intelligence.paths import is_test_path, matches_any
from hermclaw.repo_intelligence.redact import redact_code
from hermclaw.repo_intelligence.routes import extract_routes
from hermclaw.repo_intelligence.schemas import (
    BuildSystem,
    CIConfig,
    ConfigFile,
    DockerInfo,
    EntryPoint,
    FileEntry,
    GitInfo,
    LanguageStat,
    MigrationSet,
    PackageManager,
    ReadmeInfo,
    RepoInventory,
    Route,
    TestFramework,
    TestInfo,
)
from hermclaw.repo_intelligence.sources import list_worktree_files
from hermclaw.repo_intelligence.tables import extract_tables

MANIFEST_MAX_BYTES = 2_000_000
README_EXCERPT_CHARS = 2_000
_ROUTE_LANGS = frozenset({"python", "javascript", "typescript", "tsx", "php", "go", "java", "kotlin", "ruby"})
_ENTRY_SCAN_LANGS = frozenset({"python", "go", "rust", "java", "php", "javascript", "typescript"})

_BUILD_FILES: dict[str, str] = {
    "pyproject.toml": "python-pyproject",
    "setup.py": "setuptools",
    "setup.cfg": "setuptools-cfg",
    "package.json": "npm-package",
    "composer.json": "composer",
    "makefile": "make",
    "gnumakefile": "make",
    "cmakelists.txt": "cmake",
    "meson.build": "meson",
    "go.mod": "go-modules",
    "cargo.toml": "cargo",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "settings.gradle": "gradle",
    "settings.gradle.kts": "gradle",
    "workspace": "bazel",
    "workspace.bazel": "bazel",
    "module.bazel": "bazel",
    "gemfile": "bundler",
    "mix.exs": "mix",
    "deno.json": "deno",
    "justfile": "just",
    "taskfile.yml": "task",
    "build.xml": "ant",
}
_LOCKFILES: dict[str, str] = {
    "uv.lock": "uv",
    "poetry.lock": "poetry",
    "pdm.lock": "pdm",
    "pipfile.lock": "pipenv",
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
    "bun.lockb": "bun",
    "bun.lock": "bun",
    "composer.lock": "composer",
    "go.sum": "go",
    "cargo.lock": "cargo",
    "gemfile.lock": "bundler",
}
_LOCK_MANIFEST = {
    "uv": "pyproject.toml",
    "poetry": "pyproject.toml",
    "pdm": "pyproject.toml",
    "pipenv": "Pipfile",
    "npm": "package.json",
    "yarn": "package.json",
    "pnpm": "package.json",
    "bun": "package.json",
    "composer": "composer.json",
    "go": "go.mod",
    "cargo": "Cargo.toml",
    "bundler": "Gemfile",
}
_COMPOSE_RE = re.compile(r"^(?:docker-|podman-)?compose(?:\.[\w-]+)*\.ya?ml$", re.I)
_DOCKERFILE_RE = re.compile(r"^(?:dockerfile|containerfile)(?:\.[\w.-]+)?$|\.dockerfile$", re.I)
_GITLAB_RESERVED = {
    "stages", "variables", "default", "include", "workflow", "image", "services", "before_script", "after_script",
    "cache", "pages",
}  # fmt: skip
_ENV_EXAMPLE_RE = re.compile(r"^\.env\.(example|sample|template|dist|defaults)$|^env\.example$|\.env\.example$", re.I)
_CONFIG_TOOLING_RE = re.compile(
    r"^(tsconfig(\..+)?\.json|jsconfig\.json|\.eslintrc(\..+)?|eslint\.config\.\w+|\.prettierrc(\..+)?|prettier\.config\.\w+"
    r"|babel\.config\.\w+|\.babelrc|webpack\.config\.\w+|vite\.config\.\w+|rollup\.config\.\w+|next\.config\.\w+"
    r"|nuxt\.config\.\w+|svelte\.config\.\w+|tailwind\.config\.\w+|postcss\.config\.\w+|jest\.config\.\w+"
    r"|vitest\.config\.\w+|playwright\.config\.\w+|phpunit\.xml(\.dist)?|phpstan\.neon(\.dist)?|psalm\.xml|\.php-cs-fixer(\.dist)?\.php"
    r"|mypy\.ini|pytest\.ini|tox\.ini|\.flake8|\.pylintrc|ruff\.toml|\.ruff\.toml|\.pre-commit-config\.yaml|\.editorconfig"
    r"|\.htaccess|alembic\.ini|knexfile\.\w+|\.sequelizerc)$",
    re.I,
)
_FLYWAY_RE = re.compile(r"^[VUR]\d+(?:[._]\d+)*__.+\.sql$", re.I)
_DJANGO_MIG_RE = re.compile(r"(^|/)migrations/\d{4}_[\w]+\.py$")
_PY_MAIN_RE = re.compile(r"^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:", re.M)
_GO_MAIN_RE = re.compile(r"^package\s+main\b[\s\S]*?^func\s+main\s*\(", re.M)
_JAVA_MAIN_RE = re.compile(r"public\s+static\s+void\s+main\s*\(\s*(?:final\s+)?String")
_SETUP_CONSOLE_RE = re.compile(r"['\"]\s*([\w.-]+)\s*=\s*([\w.]+(?::[\w.]+)?)\s*['\"]")
_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9][\w.-]*)\s*:(?!=)", re.M)


@dataclass
class _Ctx:
    root: Path
    cfg: RepoIntelConfig
    files: list[FileEntry]
    by_name: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    paths: set[str] = field(default_factory=set)
    texts: dict[str, str] = field(default_factory=dict)
    budget: int = 0
    warnings: list[str] = field(default_factory=list)

    def text(self, path: str, limit: int = MANIFEST_MAX_BYTES) -> str | None:
        if path in self.texts:
            return self.texts[path]
        if matches_any(path, self.cfg.sensitive_globs):
            return None
        if self.budget <= 0:
            return None
        t = read_text(self.root, path, min(limit, self.budget))
        if t is not None:
            self.budget -= len(t)
            self.texts[path] = t
        return t


def _depth(p: str) -> int:
    return p.count("/")


def _dir(p: str) -> str:
    return p.rsplit("/", 1)[0] if "/" in p else ""


def _join(d: str, name: str) -> str:
    return f"{d}/{name}" if d else name


def _skip_vendor(p: str) -> bool:
    parts = p.split("/")
    return any(x in ("node_modules", "vendor", ".venv", "venv", "site-packages", "bower_components") for x in parts[:-1])


# ============================================================================================= file entries
def _file_entries(root: Path, paths: list[str], cfg: RepoIntelConfig) -> list[FileEntry]:
    out: list[FileEntry] = []
    for p in paths:
        st = lstat_regular(root, p)
        if st is None:
            continue
        sensitive = matches_any(p, cfg.sensitive_globs)
        lang = detect_language(p)
        binary = is_binary_name(p)
        if not sensitive and not binary and (lang is None or lang in ("text",)) and st.st_size > 0:
            head = read_bytes(root, p, 1024) or b""
            if looks_binary(head):
                binary = True
            elif lang is None:
                lang = detect_language(p, head)
        out.append(
            FileEntry(
                path=p,
                size=st.st_size,
                language=None if binary else lang,
                category="binary" if binary else category(lang),
                binary=binary,
                sensitive=sensitive,
                test=is_test_path(p) and category(lang) == "programming",
            )
        )
    return out


def _language_stats(files: list[FileEntry]) -> tuple[list[LanguageStat], list[str]]:
    counts: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    for f in files:
        if f.language and not f.binary:
            counts[f.language] += 1
            sizes[f.language] += f.size
    stats = [LanguageStat(language=lang, category=category(lang), files=counts[lang], bytes=sizes[lang]) for lang in counts]
    stats.sort(key=lambda s: (-s.bytes, -s.files, s.language))
    prog = [s for s in stats if s.category == "programming"]
    total = sum(s.bytes for s in prog) or 1
    primary = [s.language for s in prog if s.bytes / total >= 0.05][:5] or [s.language for s in prog[:1]]
    return stats, primary


# ============================================================================================= manifests
def _load_json(ctx: _Ctx, path: str) -> dict[str, Any] | None:
    t = ctx.text(path)
    if t is None:
        return None
    try:
        data = json.loads(t)
    except ValueError:
        ctx.warnings.append(f"{path}: invalid JSON")
        return None
    return data if isinstance(data, dict) else None


def _load_toml(ctx: _Ctx, path: str) -> dict[str, Any] | None:
    t = ctx.text(path)
    if t is None:
        return None
    try:
        return tomllib.loads(t)
    except tomllib.TOMLDecodeError:
        ctx.warnings.append(f"{path}: invalid TOML")
        return None


def _load_yaml(ctx: _Ctx, path: str) -> Any:
    t = ctx.text(path)
    if t is None:
        return None
    try:
        docs = [d for d in yaml.safe_load_all(t) if d is not None]
    except yaml.YAMLError:
        ctx.warnings.append(f"{path}: invalid YAML")
        return None
    if not docs:
        return None
    return docs[0] if len(docs) == 1 else docs


def _as_dict(value: Any) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _deps_text(*parts: Any) -> str:
    return json.dumps(parts, default=str).lower()


@dataclass
class _Manifests:
    build: list[BuildSystem] = field(default_factory=list)
    pms: list[PackageManager] = field(default_factory=list)
    entry: list[EntryPoint] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)
    dep_text: dict[str, str] = field(default_factory=dict)  # dir -> lowercased dependency text
    package_json: dict[str, dict[str, Any]] = field(default_factory=dict)
    composer: dict[str, dict[str, Any]] = field(default_factory=dict)
    pyproject: dict[str, dict[str, Any]] = field(default_factory=dict)


def _manifests(ctx: _Ctx) -> _Manifests:
    m = _Manifests()
    for f in ctx.files:
        if _skip_vendor(f.path) or f.sensitive:
            continue
        name = f.path.rsplit("/", 1)[-1]
        lower = name.lower()
        d = _dir(f.path)
        kind = _BUILD_FILES.get(lower)
        if kind is None and (lower.endswith((".csproj", ".fsproj", ".sln"))):
            kind = "dotnet"
        if kind is None:
            continue
        detail: str | None = None
        if lower == "pyproject.toml":
            data = _load_toml(ctx, f.path) or {}
            m.pyproject[d] = data
            backend = (data.get("build-system") or {}).get("build-backend")
            detail = str(backend) if backend else None
            project = data.get("project") or {}
            poetry = (data.get("tool") or {}).get("poetry") or {}
            for section, k in (
                (project.get("scripts"), "console-script"),
                (project.get("gui-scripts"), "gui-script"),
                (poetry.get("scripts"), "console-script"),
            ):
                if isinstance(section, dict):
                    for sname, target in section.items():
                        m.entry.append(EntryPoint(kind=k, name=str(sname), path=f.path, target=str(target)[:300]))
            m.dep_text[d] = m.dep_text.get(d, "") + _deps_text(
                project.get("dependencies"), project.get("optional-dependencies"), data.get("dependency-groups"),
                poetry.get("dependencies"), poetry.get("dev-dependencies"), poetry.get("group"),
                (data.get("tool") or {}).get("pytest"),
            )  # fmt: skip
        elif lower == "setup.py":
            t = ctx.text(f.path) or ""
            if "console_scripts" in t or "entry_points" in t:
                for sname, target in _SETUP_CONSOLE_RE.findall(t.split("entry_points", 1)[-1])[:50]:
                    m.entry.append(EntryPoint(kind="console-script", name=sname, path=f.path, target=target))
            m.dep_text[d] = m.dep_text.get(d, "") + t.lower()
        elif lower == "package.json":
            data = _load_json(ctx, f.path) or {}
            m.package_json[d] = data
            detail = str(data.get("name")) if data.get("name") else None
            scripts = _as_dict(data.get("scripts"))
            for sname, cmd in sorted(scripts.items()):
                m.entry.append(EntryPoint(kind="npm-script", name=str(sname), path=f.path, target=str(cmd)[:300]))
                if "test" in str(sname).lower():
                    m.scripts[f"npm({d}):{sname}" if d else f"npm:{sname}"] = str(cmd)[:300]
            if isinstance(data.get("main"), str):
                m.entry.append(EntryPoint(kind="npm-main", name=str(data.get("name") or data["main"]), path=f.path, target=data["main"]))
            b = data.get("bin")
            if isinstance(b, str):
                m.entry.append(EntryPoint(kind="npm-bin", name=str(data.get("name") or b), path=f.path, target=b))
            elif isinstance(b, dict):
                for bname, target in sorted(b.items()):
                    m.entry.append(EntryPoint(kind="npm-bin", name=str(bname), path=f.path, target=str(target)))
            m.dep_text[d] = m.dep_text.get(d, "") + _deps_text(
                sorted((data.get("dependencies") or {}).keys()) if isinstance(data.get("dependencies"), dict) else None,
                sorted((data.get("devDependencies") or {}).keys()) if isinstance(data.get("devDependencies"), dict) else None,
                "jest-config" if "jest" in data else None,
            )
            pm = data.get("packageManager")
            if isinstance(pm, str) and pm:
                m.pms.append(PackageManager(name=pm.split("@", 1)[0], manifest=f.path))
        elif lower == "composer.json":
            data = _load_json(ctx, f.path) or {}
            m.composer[d] = data
            detail = str(data.get("name")) if data.get("name") else None
            scripts = _as_dict(data.get("scripts"))
            for sname, cmd in sorted(scripts.items()):
                cmd_s = " && ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)
                m.entry.append(EntryPoint(kind="composer-script", name=str(sname), path=f.path, target=cmd_s[:300]))
                if "test" in str(sname).lower():
                    m.scripts[f"composer({d}):{sname}" if d else f"composer:{sname}"] = cmd_s[:300]
            bins = data.get("bin")
            for b in [bins] if isinstance(bins, str) else bins if isinstance(bins, list) else []:
                m.entry.append(EntryPoint(kind="composer-bin", name=str(b).rsplit("/", 1)[-1], path=f.path, target=str(b)))
            m.dep_text[d] = m.dep_text.get(d, "") + _deps_text(data.get("require"), data.get("require-dev"))
        elif lower == "go.mod":
            t = ctx.text(f.path) or ""
            mm = re.search(r"^module\s+(\S+)", t, re.M)
            detail = mm.group(1) if mm else None
        elif lower in ("makefile", "gnumakefile"):
            t = ctx.text(f.path) or ""
            targets = sorted({x for x in _MAKE_TARGET_RE.findall(t) if not x.startswith(".")})
            detail = ",".join(targets[:20]) or None
        m.build.append(BuildSystem(name=kind, path=f.path, detail=detail))
    # package managers from lockfiles / manifests
    have_lock: set[tuple[str, str]] = set()
    for f in ctx.files:
        if _skip_vendor(f.path):
            continue
        lower = f.path.rsplit("/", 1)[-1].lower()
        pm = _LOCKFILES.get(lower)
        if pm:
            d = _dir(f.path)
            manifest = _join(d, _LOCK_MANIFEST[pm])
            m.pms.append(PackageManager(name=pm, manifest=manifest if manifest in ctx.paths else None, lockfile=f.path))
            have_lock.add((d, _LOCK_MANIFEST[pm].lower()))
        elif lower.startswith("requirements") and lower.endswith((".txt", ".in")):
            m.pms.append(PackageManager(name="pip", manifest=f.path))
            t = ctx.text(f.path) or ""
            m.dep_text[_dir(f.path)] = m.dep_text.get(_dir(f.path), "") + t.lower()
        elif lower == "pipfile":
            m.pms.append(
                PackageManager(
                    name="pipenv",
                    manifest=f.path,
                    lockfile=_join(_dir(f.path), "Pipfile.lock") if _join(_dir(f.path), "Pipfile.lock") in ctx.paths else None,
                )
            )
            have_lock.add((_dir(f.path), "pipfile"))
    for b in m.build:
        d = _dir(b.path)
        lower = b.path.rsplit("/", 1)[-1].lower()
        if (d, lower) in have_lock:
            continue
        fallback = {
            "package.json": "npm", "composer.json": "composer", "go.mod": "go", "cargo.toml": "cargo",
            "pom.xml": "maven", "build.gradle": "gradle", "build.gradle.kts": "gradle", "gemfile": "bundler",
        }.get(lower)  # fmt: skip
        if lower == "pyproject.toml":
            tool = (m.pyproject.get(d) or {}).get("tool") or {}
            fallback = "poetry" if "poetry" in tool else "pdm" if "pdm" in tool else "hatch" if "hatch" in tool else "pip"
        if fallback and not any(p.name == fallback and p.manifest == b.path for p in m.pms):
            m.pms.append(PackageManager(name=fallback, manifest=b.path))
    return m


# ============================================================================================= tests
def _tests(ctx: _Ctx, man: _Manifests) -> TestInfo:
    test_files = sorted(f.path for f in ctx.files if f.test and not _skip_vendor(f.path))
    dirs: set[str] = set()
    for p in test_files:
        parts = p.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.lower() in ("test", "tests", "__tests__", "spec", "specs", "e2e"):
                dirs.add("/".join(parts[: i + 1]))
                break
    fw: dict[str, TestFramework] = {}

    def add(name: str, evidence: str, command: str | None) -> None:
        if name not in fw:
            fw[name] = TestFramework(name=name, evidence=[], command=command)
        if evidence not in fw[name].evidence and len(fw[name].evidence) < 8:
            fw[name].evidence.append(evidence)

    deps_all = " ".join(man.dep_text.values())
    for p in sorted(ctx.paths):
        if _skip_vendor(p):
            continue
        name = p.rsplit("/", 1)[-1].lower()
        if name in ("pytest.ini", "conftest.py"):
            add("pytest", p, "pytest")
        elif name in ("setup.cfg", "tox.ini"):
            t = ctx.text(p) or ""
            if "[tool:pytest]" in t or "[pytest]" in t:
                add("pytest", p, "pytest")
        elif re.match(r"^jest\.config\.\w+$", name):
            add("jest", p, "npx jest")
        elif re.match(r"^vitest\.config\.\w+$", name) or re.match(r"^vitest\.workspace\.\w+$", name):
            add("vitest", p, "npx vitest run")
        elif re.match(r"^\.mocharc(\.\w+)?$", name):
            add("mocha", p, "npx mocha")
        elif re.match(r"^playwright\.config\.\w+$", name):
            add("playwright", p, "npx playwright test")
        elif re.match(r"^cypress\.config\.\w+$", name):
            add("cypress", p, "npx cypress run")
        elif name in ("phpunit.xml", "phpunit.xml.dist"):
            add("phpunit", p, "vendor/bin/phpunit")
        elif name == ".rspec":
            add("rspec", p, "bundle exec rspec")
    for d, data in sorted(man.pyproject.items()):
        if "pytest" in (data.get("tool") or {}):
            add("pytest", _join(d, "pyproject.toml") + " [tool.pytest]", "pytest")
    if re.search(r"\bpytest\b", deps_all):
        add("pytest", "dependency: pytest", "pytest")
    if re.search(r"\"jest\"|jest-config", deps_all):
        add("jest", "dependency: jest", "npx jest")
    if '"vitest"' in deps_all:
        add("vitest", "dependency: vitest", "npx vitest run")
    if '"mocha"' in deps_all:
        add("mocha", "dependency: mocha", "npx mocha")
    if "phpunit/phpunit" in deps_all:
        add("phpunit", "dependency: phpunit/phpunit", "vendor/bin/phpunit")
    if "pestphp/pest" in deps_all:
        add("pest", "dependency: pestphp/pest", "vendor/bin/pest")
    py_tests = [p for p in test_files if p.endswith(".py")]
    if py_tests and "pytest" not in fw:
        for p in py_tests[:200]:
            t = ctx.text(p, 200_000) or ""
            if re.search(r"^\s*(import pytest|from pytest\b)", t, re.M):
                add("pytest", p, "pytest")
                break
            if re.search(r"^\s*(import unittest|from unittest\b)", t, re.M):
                add("unittest", p, "python -m unittest discover")
    if any(p.endswith("_test.go") for p in test_files):
        add("go-test", next(p for p in test_files if p.endswith("_test.go")), "go test ./...")
    if any(b.name == "cargo" for b in man.build):
        add("cargo-test", next(b.path for b in man.build if b.name == "cargo"), "cargo test")
    if any(p.endswith((".java", ".kt")) for p in test_files):
        tool = (
            "mvn test"
            if any(b.name == "maven" for b in man.build)
            else "gradle test"
            if any(b.name == "gradle" for b in man.build)
            else None
        )
        add("junit", next(p for p in test_files if p.endswith((".java", ".kt"))), tool)
    if any(p.endswith("_spec.rb") for p in test_files):
        add("rspec", next(p for p in test_files if p.endswith("_spec.rb")), "bundle exec rspec")
    return TestInfo(
        frameworks=sorted(fw.values(), key=lambda f: f.name),
        test_dirs=sorted(dirs)[:50],
        test_file_count=len(test_files),
        test_files=test_files[:200],
        scripts=dict(sorted(man.scripts.items())),
    )


# ============================================================================================= docker / ci
def _docker(ctx: _Ctx) -> DockerInfo:
    info = DockerInfo()
    images: set[str] = set()
    ports: set[str] = set()
    services: set[str] = set()
    for p in sorted(ctx.paths):
        if _skip_vendor(p):
            continue
        name = p.rsplit("/", 1)[-1]
        if _DOCKERFILE_RE.search(name):
            info.dockerfiles.append(p)
            t = ctx.text(p) or ""
            stages: set[str] = set()
            for m in re.finditer(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", t, re.M | re.I):
                if m.group(1).lower() not in stages:
                    images.add(m.group(1))
                if m.group(2):
                    stages.add(m.group(2).lower())
            for m in re.finditer(r"^\s*EXPOSE\s+(.+)$", t, re.M | re.I):
                ports.update(m.group(1).split())
        elif _COMPOSE_RE.match(name):
            info.compose_files.append(p)
            data = _load_yaml(ctx, p)
            if isinstance(data, dict) and isinstance(data.get("services"), dict):
                services.update(str(k) for k in data["services"])
                for svc in data["services"].values():
                    if isinstance(svc, dict) and isinstance(svc.get("image"), str):
                        images.add(svc["image"])
    info.base_images = sorted(images)[:50]
    info.exposed_ports = sorted(ports)[:50]
    info.compose_services = sorted(services)[:100]
    return info


def _ci(ctx: _Ctx) -> list[CIConfig]:
    out: list[CIConfig] = []
    for p in sorted(ctx.paths):
        lower = p.lower()
        provider: str | None = None
        if lower == ".gitlab-ci.yml" or (lower.startswith(".gitlab/ci/") and lower.endswith((".yml", ".yaml"))):
            provider = "gitlab"
        elif lower.startswith(".github/workflows/") and lower.endswith((".yml", ".yaml")):
            provider = "github"
        elif lower.rsplit("/", 1)[-1] == "jenkinsfile":
            provider = "jenkins"
        elif lower == ".circleci/config.yml":
            provider = "circleci"
        elif lower in ("azure-pipelines.yml", "azure-pipelines.yaml"):
            provider = "azure"
        elif lower == ".travis.yml":
            provider = "travis"
        elif lower == "bitbucket-pipelines.yml":
            provider = "bitbucket"
        elif lower == ".drone.yml":
            provider = "drone"
        elif lower == ".woodpecker.yml" or (lower.startswith(".woodpecker/") and lower.endswith((".yml", ".yaml"))):
            provider = "woodpecker"
        if provider is None:
            continue
        jobs: list[str] = []
        stages: list[str] = []
        if provider == "jenkins":
            t = ctx.text(p) or ""
            stages = re.findall(r"stage\s*\(\s*['\"]([^'\"]+)['\"]", t)[:50]
        else:
            data = _load_yaml(ctx, p)
            if isinstance(data, dict):
                if provider == "gitlab":
                    stages = [str(s) for s in data.get("stages") or [] if isinstance(s, str | int)]
                    jobs = [
                        str(k)
                        for k, v in data.items()
                        if str(k) not in _GITLAB_RESERVED and not str(k).startswith(".") and isinstance(v, dict)
                    ]
                elif isinstance(data.get("jobs"), dict):
                    jobs = [str(k) for k in data["jobs"]]
                elif provider in ("drone", "woodpecker") and isinstance(data.get("steps"), dict | list):
                    steps = data["steps"]
                    jobs = (
                        [str(k) for k in steps]
                        if isinstance(steps, dict)
                        else [str(s.get("name")) for s in steps if isinstance(s, dict) and s.get("name")]
                    )
        out.append(CIConfig(provider=provider, path=p, jobs=sorted(jobs)[:100], stages=stages[:50]))
    return out


# ============================================================================================= migrations
def _migrations(ctx: _Ctx) -> list[MigrationSet]:
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for p in sorted(ctx.paths):
        if _skip_vendor(p):
            continue
        name = p.rsplit("/", 1)[-1]
        d = _dir(p)
        parts = p.split("/")
        tool: str | None = None
        if p.endswith(".py") and parts[-2:-1] == ["versions"] and name != "__init__.py":
            t = ctx.text(p, 200_000) or ""
            if re.search(r"^(down_)?revision\s*[:=]", t, re.M) and ("alembic" in t or "down_revision" in t):
                tool = "alembic"
        elif _DJANGO_MIG_RE.search(p):
            tool = "django"
        elif p.endswith(".php") and "/migrations/" in f"/{d}/":
            t = ctx.text(p, 200_000) or ""
            tool = "doctrine" if "AbstractMigration" in t else "laravel" if ("Schema::" in t or "Migration" in t) else "php"
        elif p.endswith(".rb") and d.endswith("db/migrate"):
            tool = "rails"
        elif p.endswith(".sql") and "prisma/migrations/" in p:
            tool, d = "prisma", p.split("prisma/migrations/")[0] + "prisma/migrations"
        elif p.endswith(".sql") and _FLYWAY_RE.match(name):
            tool = "flyway"
        elif p.endswith((".js", ".ts", ".mjs", ".cjs")) and any(x in ("migrations", "migration", "migrate") for x in parts[:-1]):
            t = ctx.text(p, 200_000) or ""
            if "queryInterface" in t:
                tool = "sequelize"
            elif "MigrationInterface" in t:
                tool = "typeorm"
            elif re.search(r"exports\.up\b|export\s+(async\s+)?function\s+up\b|knex", t):
                tool = "knex"
        elif p.endswith(".sql") and any(
            x.lower() in ("migrations", "migration", "migrate", "sql", "schema", "db", "database") for x in parts[:-1]
        ):
            tool = "sql"
        if tool:
            groups[(tool, d)].append(p)
    out: list[MigrationSet] = []
    for (tool, d), files in sorted(groups.items()):
        tables: set[str] = set()
        for p in files[:500]:
            t = ctx.text(p, 500_000) or ""
            for name, _kind, _s, _e in extract_tables(t):
                tables.add(name)
        out.append(MigrationSet(tool=tool, path=d or ".", count=len(files), tables=sorted(tables)[:200]))
    return out


# ============================================================================================= config / entry points / routes
def _config_files(ctx: _Ctx, ci_paths: set[str], compose: set[str]) -> list[ConfigFile]:
    out: list[ConfigFile] = []
    for f in ctx.files:
        p = f.path
        if _skip_vendor(p) or p in ci_paths or p in compose:
            continue
        name = p.rsplit("/", 1)[-1]
        lower = name.lower()
        kind: str | None = None
        if _ENV_EXAMPLE_RE.search(name):
            kind = "env-example"
        elif f.sensitive:
            continue
        elif ("nginx" in p.lower() and lower.endswith((".conf", ".conf.template", ".template"))) or lower == "nginx.conf":
            kind = "nginx"
        elif _CONFIG_TOOLING_RE.match(name):
            kind = "tooling"
        elif lower.endswith((".yaml", ".yml")):
            kind = "yaml"
        elif lower.endswith(".toml") and lower not in ("pyproject.toml", "cargo.toml"):
            kind = "toml"
        elif lower.endswith((".ini", ".cfg")) and lower != "setup.cfg":
            kind = "ini"
        elif lower.endswith(".conf"):
            kind = "conf"
        elif lower.endswith(".properties"):
            kind = "properties"
        elif lower.endswith(".neon"):
            kind = "neon"
        elif lower.endswith(".json") and any(x in ("config", "configs", "conf", "settings") for x in p.lower().split("/")[:-1]):
            kind = "json"
        if kind:
            out.append(ConfigFile(path=p, kind=kind))
    out.sort(key=lambda c: (_depth(c.path), c.path))
    return out[:300]


def _entry_points(ctx: _Ctx, man: _Manifests) -> list[EntryPoint]:
    eps = list(man.entry)
    for f in ctx.files:
        p = f.path
        if _skip_vendor(p) or f.binary or f.sensitive:
            continue
        name = p.rsplit("/", 1)[-1]
        d = _dir(p)
        if name == "__main__.py":
            eps.append(EntryPoint(kind="python-module-main", name=d.replace("/", ".") or "__main__", path=p))
            continue
        if name in ("manage.py", "wsgi.py", "asgi.py") and f.language == "python":
            eps.append(EntryPoint(kind={"manage.py": "django-manage", "wsgi.py": "wsgi", "asgi.py": "asgi"}[name], name=name, path=p))
        if name == "Procfile":
            for line in (ctx.text(p) or "").splitlines():
                if ":" in line and not line.lstrip().startswith("#"):
                    proc, cmd = line.split(":", 1)
                    eps.append(EntryPoint(kind="procfile", name=proc.strip(), path=p, target=cmd.strip()[:300]))
            continue
        if _DOCKERFILE_RE.search(name):
            for m in re.finditer(r"^\s*(CMD|ENTRYPOINT)\s+(.+)$", ctx.text(p) or "", re.M | re.I):
                eps.append(
                    EntryPoint(kind="container-" + m.group(1).lower(), name=m.group(1).upper(), path=p, target=m.group(2).strip()[:300])
                )
            continue
        if (
            f.language == "php"
            and name == "index.php"
            and (d == "" or d.split("/")[-1] in ("public", "web", "htdocs", "www", "public_html"))
        ):
            eps.append(EntryPoint(kind="php-front-controller", name=p, path=p))
            continue
        if f.language == "rust" and (p.endswith("src/main.rs") or "/src/bin/" in f"/{p}"):
            eps.append(
                EntryPoint(
                    kind="rust-bin",
                    name=name.removesuffix(".rs") if name != "main.rs" else (d.split("/")[-2] if d.count("/") else d or "main"),
                    path=p,
                )
            )
            continue
        top = d.split("/")[0] if d else ""
        if f.language in _ENTRY_SCAN_LANGS and f.size <= ctx.cfg.max_index_file_bytes and not f.test:
            t = ctx.text(p) if f.language in ("python", "go", "java") or top in ("bin", "scripts") else None
            if t is None:
                continue
            if f.language == "python" and _PY_MAIN_RE.search(t):
                eps.append(EntryPoint(kind="python-main", name=name, path=p))
            elif f.language == "go" and _GO_MAIN_RE.search(t):
                eps.append(EntryPoint(kind="go-main", name=d or ".", path=p))
            elif f.language == "java" and _JAVA_MAIN_RE.search(t):
                eps.append(EntryPoint(kind="java-main", name=name.removesuffix(".java"), path=p))
            elif top in ("bin", "scripts") and t.startswith("#!"):
                eps.append(EntryPoint(kind="script", name=name, path=p))
        elif top in ("bin", "scripts") and f.language in ("shell", "perl", "ruby", None) and not f.binary:
            head = (ctx.text(p, 256) or "") if f.size else ""
            if head.startswith("#!"):
                eps.append(EntryPoint(kind="script", name=name, path=p))
    uniq = {(e.kind, e.name, e.path or "", e.target or ""): e for e in eps}
    return sorted(uniq.values(), key=lambda e: (e.kind, e.name, e.path or "", e.target or ""))[:500]


def _routes(ctx: _Ctx) -> list[Route]:
    out: list[Route] = []
    for f in ctx.files:
        if f.language not in _ROUTE_LANGS or f.binary or f.sensitive or _skip_vendor(f.path) or f.test:
            continue
        if f.size > ctx.cfg.max_index_file_bytes:
            continue
        t = ctx.text(f.path)
        if t is None:
            continue
        out.extend(extract_routes(f.path, f.language, t))
    out.sort(key=lambda r: (r.file, r.line, r.method, r.path))
    return out[:5000]


def _readme(ctx: _Ctx) -> ReadmeInfo | None:
    cands = sorted(p for p in ctx.paths if "/" not in p and re.match(r"^readme(\.(md|markdown|rst|txt|adoc))?$", p, re.I))
    if not cands:
        return None
    pref = sorted(cands, key=lambda p: (not p.lower().endswith(".md"), p))[0]
    t = ctx.text(pref, 200_000) or ""
    title = None
    for line in t.splitlines():
        s = line.strip()
        if s:
            title = s.lstrip("#").strip()[:200] or None
            break
    return ReadmeInfo(path=pref, title=redact_code(title) if title else None, excerpt=redact_code(t[:README_EXCERPT_CHARS]))


# ============================================================================================= git
async def git_info(root: Path, cfg: RepoIntelConfig, *, max_changed: int = 50) -> GitInfo:
    if not await _proc.is_git_repo(root, timeout_s=cfg.git_timeout_seconds, git_binary=cfg.git_binary):
        return GitInfo(is_repo=False)
    t = cfg.git_timeout_seconds
    branch_res = await _proc.git(root, "symbolic-ref", "--short", "-q", "HEAD", timeout_s=t, git_binary=cfg.git_binary)
    branch = branch_res.text().strip() if branch_res.ok else None
    head = await _proc.head_sha(root, timeout_s=t, git_binary=cfg.git_binary)
    entries = await status_entries(root, cfg)
    counts: Counter[str] = Counter()
    for xy, _p, _o in entries:
        counts[_status_bucket(xy)] += 1
    changed = [f"{xy} {p}" for xy, p, _o in entries[:max_changed]]
    return GitInfo(
        is_repo=True,
        branch=branch or None,
        head=head,
        detached=branch_res.returncode != 0 and head is not None,
        dirty=bool(entries),
        status_counts=dict(sorted(counts.items())),
        changed=changed,
    )


def _status_bucket(xy: str) -> str:
    if xy == "??":
        return "untracked"
    if "U" in xy or xy in ("AA", "DD"):
        return "conflicted"
    if "R" in xy:
        return "renamed"
    if "C" in xy:
        return "copied"
    if "D" in xy:
        return "deleted"
    if "A" in xy:
        return "added"
    return "modified"


async def status_entries(root: Path, cfg: RepoIntelConfig) -> list[tuple[str, str, str | None]]:
    """``git status --porcelain=v1 -z`` as ``(XY, path, orig_path)`` (ignored files excluded)."""
    out = await _proc.git_ok(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all", timeout_s=cfg.git_timeout_seconds, git_binary=cfg.git_binary
    )
    items = out.split(b"\0")
    res: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(items):
        raw = items[i]
        i += 1
        if len(raw) < 4:
            continue
        xy = raw[:2].decode("ascii", errors="replace")
        try:
            path = raw[3:].decode("utf-8")
        except UnicodeDecodeError:
            continue
        orig = None
        if ("R" in xy or "C" in xy) and i < len(items):
            try:
                orig = items[i].decode("utf-8")
            except UnicodeDecodeError:
                orig = None
            i += 1
        res.append((xy, path, orig))
    res.sort(key=lambda e: e[1])
    return res


# ============================================================================================= entry
def _analyze(root: Path, paths: list[str], cfg: RepoIntelConfig, truncated: bool) -> RepoInventory:
    files = _file_entries(root, paths, cfg)
    ctx = _Ctx(root=root, cfg=cfg, files=files, budget=cfg.inventory_read_budget_bytes)
    ctx.paths = {f.path for f in files}
    stats, primary = _language_stats(files)
    man = _manifests(ctx)
    tests = _tests(ctx, man)
    docker = _docker(ctx)
    ci = _ci(ctx)
    migrations = _migrations(ctx)
    configs = _config_files(ctx, {c.path for c in ci}, set(docker.compose_files))
    entry = [e.model_copy(update={"target": redact_code(e.target)}) if e.target else e for e in _entry_points(ctx, man)]
    tests.scripts = {k: redact_code(v) for k, v in tests.scripts.items()}  # commands may carry credentials
    routes = _routes(ctx)
    readme = _readme(ctx)
    if ctx.budget <= 0:
        ctx.warnings.append("inventory read budget exhausted: routes/entry points may be incomplete")
    if truncated:
        ctx.warnings.append(f"file list truncated at {cfg.max_files} files")
    top = sorted({(p.split("/", 1)[0] + "/") if "/" in p else p for p in ctx.paths})
    listed = files[: cfg.inventory_max_listed_files]
    uniq_pm = {(p.name, p.manifest or "", p.lockfile or ""): p for p in man.pms}
    return RepoInventory(
        root_name=root.name,
        file_count=len(files),
        total_bytes=sum(f.size for f in files),
        truncated=truncated,
        files=listed,
        files_listed_truncated=len(files) > len(listed),
        top_level=top[:100],
        languages=stats,
        primary_languages=primary,
        build_systems=sorted(man.build, key=lambda b: (_depth(b.path), b.path, b.name)),
        package_managers=sorted(uniq_pm.values(), key=lambda p: (p.name, p.manifest or "", p.lockfile or "")),
        tests=tests,
        docker=docker,
        ci=ci,
        migrations=migrations,
        config_files=configs,
        entry_points=entry,
        routes=routes,
        readme=readme,
        warnings=sorted(set(ctx.warnings)),
    )


async def build_inventory(root: Path, cfg: RepoIntelConfig | None = None, *, paths: Iterable[str] | None = None) -> RepoInventory:
    """Inventory of the working tree at ``root`` (tracked + untracked, ignored files excluded)."""
    cfg = cfg or RepoIntelConfig()
    root = Path(root)
    git = await git_info(root, cfg)
    if paths is None:
        listing = await list_worktree_files(root, cfg, git_repo=git.is_repo)
        plist, truncated = listing.paths, listing.truncated
    else:
        plist, truncated = sorted(set(paths)), False
    inv = await asyncio.to_thread(_analyze, root, plist, cfg, truncated)
    inv.git = git
    return inv
