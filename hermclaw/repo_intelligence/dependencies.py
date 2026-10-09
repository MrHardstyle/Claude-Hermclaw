"""Dependency relations: imports -> workspace files (P11 11.7).

Resolution follows the module systems' own rules, not project conventions:
* Python – package chains (``__init__.py``) and repository-root dotted paths, relative imports by level.
* JavaScript/TypeScript – relative specifiers with the usual extension/``index`` probing (bare specifiers are
  external packages and stay unresolved).
* PHP – ``include``/``require`` literals (``__DIR__``-relative or include-path style) and ``use`` statements via the
  composer PSR-4 ``autoload`` map, falling back to a unique path-suffix match.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from hermclaw.repo_intelligence.schemas import ImportRecord

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".d.ts", ".json", ".vue", ".svelte")
_JS_LANGS = {"javascript", "typescript", "tsx", "vue", "svelte"}


def _norm(p: str) -> str | None:
    n = posixpath.normpath(p)
    if n.startswith("../") or n == ".." or n.startswith("/"):
        return None
    return "" if n == "." else n


class ModuleResolver:
    def __init__(self, files: Iterable[str], *, psr4: Mapping[str, Iterable[str]] | None = None) -> None:
        self.files = set(files)
        self.psr4 = sorted(((k, list(v)) for k, v in (psr4 or {}).items()), key=lambda kv: -len(kv[0]))
        self._py: dict[str, list[str]] = defaultdict(list)
        self._by_suffix: dict[str, list[str]] = defaultdict(list)
        dirs_with_init = {posixpath.dirname(f) for f in self.files if f.endswith("/__init__.py") or f == "__init__.py"}
        for f in sorted(self.files):
            if f.endswith(".php"):
                parts = f.split("/")
                for i in range(max(0, len(parts) - 3), len(parts)):
                    self._by_suffix["/".join(parts[i:])].append(f)
            if not f.endswith((".py", ".pyi")):
                continue
            stem = f.rsplit(".", 1)[0]
            parts = stem.split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            # repository-root dotted path (namespace packages, scripts run from the root)
            self._py[".".join(parts)].append(f)
            # package chain: walk up while the directory is a package
            d = posixpath.dirname(f)
            i = len(parts) - (0 if f.endswith("__init__.py") or f.endswith("__init__.pyi") else 1)
            start = i
            while start > 0 and "/".join(parts[:start]) in dirs_with_init:
                start -= 1
            chain = parts[start:]
            if chain and chain != parts:
                self._py[".".join(chain)].append(f)
            del d
        for k in self._py:
            self._py[k].sort(key=lambda p: (p.endswith(".pyi"), p.count("/"), p))

    # ---- python
    def _py_lookup(self, dotted: str, importer: str) -> str | None:
        cands = self._py.get(dotted)
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        top = importer.split("/", 1)[0]
        same = [c for c in cands if c.split("/", 1)[0] == top]
        return (same or cands)[0]

    def _py_path(self, base_dir: str, dotted: str) -> str | None:
        rel = dotted.replace(".", "/")
        for cand in (f"{rel}.py", f"{rel}/__init__.py", f"{rel}.pyi", f"{rel}/__init__.pyi"):
            full = _norm(posixpath.join(base_dir, cand)) if base_dir else _norm(cand)
            if full and full in self.files:
                return full
        return None

    def resolve_python(self, importer: str, imp: ImportRecord) -> str | None:
        if imp.level > 0:
            base = posixpath.dirname(importer)
            for _ in range(imp.level - 1):
                base = posixpath.dirname(base)
            if imp.module:
                for name in imp.names:
                    hit = self._py_path(base, f"{imp.module}.{name}")
                    if hit:
                        return hit
                return self._py_path(base, imp.module)
            for name in imp.names:
                hit = self._py_path(base, name)
                if hit:
                    return hit
            init = _norm(posixpath.join(base, "__init__.py")) if base else "__init__.py"
            return init if init in self.files else None
        if not imp.module:
            return None
        if imp.kind == "from":
            for name in imp.names:
                if name != "*":
                    hit = self._py_lookup(f"{imp.module}.{name}", importer)
                    if hit:
                        return hit
        hit = self._py_lookup(imp.module, importer)
        if hit:
            return hit
        # ``import a.b.c`` where only a package prefix is part of the workspace
        parts = imp.module.split(".")
        for i in range(len(parts) - 1, 0, -1):
            hit = self._py_lookup(".".join(parts[:i]), importer)
            if hit:
                return hit
        return None

    # ---- javascript / typescript
    def resolve_js(self, importer: str, imp: ImportRecord) -> str | None:
        spec = imp.module.split("?", 1)[0].split("#", 1)[0]
        if not spec.startswith((".", "/")):
            return None
        base = posixpath.dirname(importer)
        target = _norm(posixpath.join(base, spec)) if not spec.startswith("/") else _norm(spec.lstrip("/"))
        if target is None:
            return None
        cands = [target]
        if target.endswith((".js", ".jsx", ".mjs", ".cjs")):
            stem = target.rsplit(".", 1)[0]
            cands += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
        cands += [target + e for e in _JS_EXTS]
        cands += [f"{target}/index{e}" for e in _JS_EXTS]
        for c in cands:
            if c in self.files:
                return c
        return None

    # ---- php
    def resolve_php(self, importer: str, imp: ImportRecord) -> str | None:
        if imp.kind in ("include", "include-dir"):
            lit = imp.module
            cands: list[str | None] = []
            base = posixpath.dirname(importer)
            if imp.kind == "include-dir" or lit.startswith("./") or lit.startswith("../"):
                cands.append(_norm(posixpath.join(base, lit.lstrip("/")) if imp.kind == "include-dir" else posixpath.join(base, lit)))
            else:
                cands.append(_norm(posixpath.join(base, lit)))
                cands.append(_norm(lit.lstrip("/")))
            for c in cands:
                if c and c in self.files:
                    return c
            return None
        if imp.kind == "use":
            fqcn = imp.module.lstrip("\\")
            for prefix, dirs in self.psr4:
                pfx = prefix.rstrip("\\")
                if fqcn == pfx or fqcn.startswith(pfx + "\\"):
                    rest = fqcn[len(pfx) :].lstrip("\\").replace("\\", "/")
                    for d in dirs:
                        cand = _norm(posixpath.join(d, rest + ".php")) if d else _norm(rest + ".php")
                        if cand and cand in self.files:
                            return cand
            parts = fqcn.split("\\")
            for n in (3, 2):
                if len(parts) >= n:
                    hits = self._by_suffix.get("/".join(parts[-n:]) + ".php") or []
                    if len(hits) == 1:
                        return hits[0]
            hits = self._by_suffix.get(parts[-1] + ".php") or []
            return hits[0] if len(hits) == 1 else None
        return None

    def resolve(self, importer: str, language: str, imp: ImportRecord) -> str | None:
        if language == "python":
            hit = self.resolve_python(importer, imp)
        elif language in _JS_LANGS:
            hit = self.resolve_js(importer, imp)
        elif language == "php":
            hit = self.resolve_php(importer, imp)
        else:
            hit = None
        return hit if hit != importer else None


_VENDOR_PARTS = frozenset({"vendor", "node_modules", ".venv", "venv", "site-packages", "bower_components"})


def composer_manifests(files: Iterable[str], *, limit: int = 50) -> list[str]:
    """``composer.json`` files of the workspace itself (not of installed dependencies), shallowest first."""
    found = [f for f in files if f.rsplit("/", 1)[-1] == "composer.json" and not (_VENDOR_PARTS & set(f.split("/")[:-1]))]
    return sorted(found, key=lambda f: (f.count("/"), f))[:limit]


def psr4_from_composer(data: Mapping[str, object] | None, *, base_dir: str = "") -> dict[str, list[str]]:
    """PSR-4 ``prefix -> [dirs]`` of one composer manifest; dirs are made workspace-relative via ``base_dir``."""
    out: dict[str, list[str]] = {}
    if not isinstance(data, Mapping):
        return out
    for section in ("autoload", "autoload-dev"):
        sec = data.get(section)
        if not isinstance(sec, Mapping):
            continue
        psr = sec.get("psr-4")
        if not isinstance(psr, Mapping):
            continue
        for prefix, dirs in psr.items():
            vals = [dirs] if isinstance(dirs, str) else [d for d in dirs if isinstance(d, str)] if isinstance(dirs, list) else []
            for v in vals:
                rel = _norm(posixpath.join(base_dir, v.strip("/"))) if base_dir else _norm(v.strip("/") or ".")
                if rel is not None:
                    out.setdefault(str(prefix), []).append(rel)
    return out


def merge_psr4(maps: Iterable[Mapping[str, Iterable[str]]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in maps:
        for prefix, dirs in m.items():
            bucket = out.setdefault(prefix, [])
            bucket.extend(d for d in dirs if d not in bucket)
    return out


@dataclass
class DependencyGraph:
    """``imports[a]`` = files ``a`` imports; ``importers[b]`` = files importing ``b``."""

    imports: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    importers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add(self, src: str, dst: str) -> None:
        if src != dst:
            self.imports[src].add(dst)
            self.importers[dst].add(src)

    def neighbors(self, path: str) -> set[str]:
        return set(self.imports.get(path, ())) | set(self.importers.get(path, ()))

    @classmethod
    def from_imports(cls, imports: Mapping[str, Iterable[ImportRecord]], *, drop: Iterable[str] = ()) -> DependencyGraph:
        g = cls()
        dropped = set(drop)
        for src, recs in imports.items():
            if src in dropped:
                continue
            for r in recs:
                if r.resolved and r.resolved not in dropped:
                    g.add(src, r.resolved)
        return g

    def edge_count(self) -> int:
        return sum(len(v) for v in self.imports.values())
