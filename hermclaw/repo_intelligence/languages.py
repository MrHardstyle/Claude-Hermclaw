"""Language detection by file name, extension and shebang (P11 11.2)."""

from __future__ import annotations

import re

#: language -> category (programming languages drive "primary languages"; data/markup/config do not)
CATEGORY: dict[str, str] = {}

_EXT: dict[str, str] = {}


def _reg(lang: str, category: str, *exts: str) -> None:
    CATEGORY[lang] = category
    for e in exts:
        _EXT[e] = lang


_reg("python", "programming", ".py", ".pyi", ".pyw")
_reg("javascript", "programming", ".js", ".mjs", ".cjs", ".jsx")
_reg("typescript", "programming", ".ts", ".mts", ".cts")
_reg("tsx", "programming", ".tsx")
_reg("php", "programming", ".php", ".phtml", ".php5", ".php7", ".phps")
_reg("go", "programming", ".go")
_reg("rust", "programming", ".rs")
_reg("java", "programming", ".java")
_reg("kotlin", "programming", ".kt", ".kts")
_reg("scala", "programming", ".scala")
_reg("ruby", "programming", ".rb", ".rake", ".gemspec")
_reg("c", "programming", ".c", ".h")
_reg("cpp", "programming", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx")
_reg("csharp", "programming", ".cs")
_reg("swift", "programming", ".swift")
_reg("objective-c", "programming", ".m", ".mm")
_reg("dart", "programming", ".dart")
_reg("lua", "programming", ".lua")
_reg("perl", "programming", ".pl", ".pm")
_reg("r", "programming", ".r")
_reg("elixir", "programming", ".ex", ".exs")
_reg("erlang", "programming", ".erl", ".hrl")
_reg("haskell", "programming", ".hs")
_reg("clojure", "programming", ".clj", ".cljs", ".cljc")
_reg("shell", "programming", ".sh", ".bash", ".zsh", ".ksh")
_reg("powershell", "programming", ".ps1", ".psm1")
_reg("vue", "programming", ".vue")
_reg("svelte", "programming", ".svelte")
_reg("sql", "data", ".sql")
_reg("html", "markup", ".html", ".htm", ".xhtml")
_reg("template", "markup", ".twig", ".jinja", ".jinja2", ".j2", ".hbs", ".mustache", ".ejs", ".erb")
_reg("css", "markup", ".css", ".scss", ".sass", ".less")
_reg("markdown", "prose", ".md", ".markdown", ".mdx", ".rst", ".adoc")
_reg("text", "prose", ".txt")
_reg("yaml", "config", ".yaml", ".yml")
_reg("toml", "config", ".toml")
_reg("json", "data", ".json", ".jsonc", ".json5")
_reg("ini", "config", ".ini", ".cfg", ".conf", ".properties")
_reg("xml", "data", ".xml", ".xsd", ".xsl")
_reg("terraform", "config", ".tf", ".tfvars", ".hcl")
_reg("protobuf", "data", ".proto")
_reg("graphql", "data", ".graphql", ".gql")
_reg("csv", "data", ".csv", ".tsv")
_reg("dockerfile", "config", ".dockerfile")
_reg("makefile", "config", ".mk")
_reg("cmake", "config", ".cmake")

_NAMES: dict[str, str] = {
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "makefile": "makefile",
    "gnumakefile": "makefile",
    "cmakelists.txt": "cmake",
    "jenkinsfile": "groovy",
    "vagrantfile": "ruby",
    "gemfile": "ruby",
    "rakefile": "ruby",
    "procfile": "config",
    "justfile": "config",
    ".gitignore": "config",
    ".dockerignore": "config",
    ".editorconfig": "ini",
    ".htaccess": "config",
    "pipfile": "toml",
    "go.mod": "go-module",
    "go.sum": "go-module",
}
CATEGORY.update({"groovy": "programming", "config": "config", "go-module": "config"})

BINARY_EXTS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff", ".psd", ".svgz",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt",
        ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".zst", ".jar", ".war", ".whl", ".egg",
        ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a", ".lib", ".class", ".pyc", ".pyo", ".wasm",
        ".mp3", ".mp4", ".wav", ".ogg", ".flac", ".avi", ".mov", ".mkv", ".webm",
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
        ".sqlite", ".sqlite3", ".db", ".parquet", ".npy", ".npz", ".pkl", ".pt", ".onnx", ".gguf", ".safetensors",
        ".lockb",
    }
)  # fmt: skip

_SHEBANG_RE = re.compile(rb"^#!\s*(?:/usr/bin/env\s+(?:-\S+\s+)*)?(\S+)")
_SHEBANG_LANG: tuple[tuple[str, str], ...] = (
    ("python", "python"),
    ("pypy", "python"),
    ("node", "javascript"),
    ("nodejs", "javascript"),
    ("deno", "typescript"),
    ("ts-node", "typescript"),
    ("tsx", "typescript"),
    ("bun", "javascript"),
    ("php", "php"),
    ("ruby", "ruby"),
    ("perl", "perl"),
    ("bash", "shell"),
    ("sh", "shell"),
    ("zsh", "shell"),
    ("dash", "shell"),
    ("ksh", "shell"),
    ("lua", "lua"),
    ("Rscript", "r"),
    ("pwsh", "powershell"),
)

#: languages with a structural (AST/tree-sitter) adapter
STRUCTURAL_LANGUAGES = frozenset({"python", "javascript", "typescript", "tsx", "php"})


def _suffix(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".blade.php"):
        return ".php"
    dot = lower.rfind(".")
    return lower[dot:] if dot > 0 else ""


def language_from_name(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    lower = name.lower()
    if lower in _NAMES:
        return _NAMES[lower]
    if lower.startswith("dockerfile.") or lower.endswith(".dockerfile") or lower.startswith("containerfile."):
        return "dockerfile"
    if lower.startswith(".env"):
        return "dotenv"
    if lower.startswith("requirements") and lower.endswith((".txt", ".in")):
        return "pip-requirements"
    return _EXT.get(_suffix(name))


def language_from_shebang(head: bytes) -> str | None:
    if not head.startswith(b"#!"):
        return None
    m = _SHEBANG_RE.match(head.split(b"\n", 1)[0])
    if not m:
        return None
    prog = m.group(1).decode("utf-8", errors="replace").rsplit("/", 1)[-1]
    for prefix, lang in _SHEBANG_LANG:
        if prog == prefix or (prog.startswith(prefix) and prog[len(prefix) :].replace(".", "").isdigit()):
            return lang
    return None


def is_binary_name(path: str) -> bool:
    return _suffix(path.rsplit("/", 1)[-1]) in BINARY_EXTS


def looks_binary(head: bytes) -> bool:
    """NUL bytes or a high share of control characters in the first block mean binary."""
    if not head:
        return False
    if b"\0" in head:
        return True
    sample = head[:8192]
    ctrl = sum(1 for b in sample if b < 9 or (13 < b < 32 and b != 27))
    return ctrl / len(sample) > 0.3


def detect_language(path: str, head: bytes | None = None) -> str | None:
    """Language for ``path``; ``head`` (first bytes) is consulted for extension-less scripts."""
    lang = language_from_name(path)
    if lang is not None:
        return lang
    if head:
        return language_from_shebang(head)
    return None


def category(language: str | None) -> str:
    if language is None:
        return "unknown"
    return CATEGORY.get(language, "config" if language in ("dotenv", "pip-requirements") else "unknown")
