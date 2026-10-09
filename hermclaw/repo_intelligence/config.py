"""Configuration of repository intelligence (P11).

There is no dedicated ``repo_intelligence`` section in ``config/*.yaml`` yet; every knob has a conservative default
here and :meth:`RepoIntelConfig.from_hermclaw` derives the security-relevant part (secret/forbidden globs) from
``policies.scope.always_forbidden`` so the index never contains what the scope engine forbids.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

#: Bump whenever parsing/chunking output changes – an index with another version is rebuilt fully.
INDEX_VERSION = 1

DEFAULT_SENSITIVE_GLOBS: tuple[str, ...] = (
    ".git/**",
    "**/.env",
    "**/.env.local",
    "**/.env.*.local",
    "**/.env.production",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.keystore",
    "**/*.jks",
    "**/id_rsa*",
    "**/id_dsa*",
    "**/id_ecdsa*",
    "**/id_ed25519*",
    "**/.netrc",
    "**/.pgpass",
    "**/.htpasswd",
    "**/*.kdbx",
)

#: Files that are listed in the inventory but never parsed, chunked or embedded (noise for retrieval).
DEFAULT_INDEX_EXCLUDE_GLOBS: tuple[str, ...] = (
    "**/package-lock.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
    "**/composer.lock",
    "**/poetry.lock",
    "**/uv.lock",
    "**/Cargo.lock",
    "**/go.sum",
    "**/Gemfile.lock",
    "**/Pipfile.lock",
    "**/pdm.lock",
    "**/bun.lock",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/node_modules/**",
    "**/vendor/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/dist/**",
    "**/build/**",
    "**/.next/**",
    "**/coverage/**",
)

#: Directories skipped by the plain directory walk (non-git workspaces without ripgrep).
DEFAULT_WALK_SKIP_DIRS: tuple[str, ...] = (
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    ".next",
    ".idea",
    ".vscode",
    "vendor",
)

SIGNALS: tuple[str, ...] = ("lexical", "symbol", "structural", "semantic", "test_reference", "dependency")
PRIMARY_SIGNALS: tuple[str, ...] = ("lexical", "symbol", "structural", "semantic")


@dataclass(frozen=True)
class RepoIntelConfig:
    # ---- file selection
    max_files: int = 100_000  # inventory/index stop listing beyond this (truncated flag)
    max_index_file_bytes: int = 512_000  # larger files are inventoried but not parsed/chunked/embedded
    max_read_file_bytes: int = 20_000_000  # targeted reads refuse larger files
    inventory_read_budget_bytes: int = 64_000_000  # total bytes the inventory may read for routes/entry points
    inventory_max_listed_files: int = 5_000  # file entries persisted in repo_index_runs.inventory
    sensitive_globs: tuple[str, ...] = DEFAULT_SENSITIVE_GLOBS
    index_exclude_globs: tuple[str, ...] = DEFAULT_INDEX_EXCLUDE_GLOBS
    walk_skip_dirs: tuple[str, ...] = DEFAULT_WALK_SKIP_DIRS
    # ---- subprocesses
    git_timeout_seconds: float = 60.0
    rg_timeout_seconds: float = 20.0
    rg_binary: str = "rg"
    git_binary: str = "git"
    max_output_bytes: int = 64_000_000
    # ---- lexical search
    lexical_max_results: int = 2_000  # matching lines considered per query
    lexical_max_per_file: int = 50
    lexical_max_terms: int = 12
    lexical_max_columns: int = 400
    # ---- chunking / embeddings
    chunk_max_tokens: int = 1_500
    chunk_min_tokens: int = 120  # adjacent small segments are merged up to the max
    chunk_overlap_lines: int = 6
    embed_batch_size: int = 32
    embed_max_chunks_per_run: int = 50_000
    embed_document_template: str = "title: {title} | text: {text}"
    embed_query_template: str = "task: code retrieval | query: {query}"
    semantic_exact_scan_max_rows: int = 50_000  # below this many embedded chunks the search is exact (no HNSW)
    hnsw_ef_search: int = 200
    # ---- ranking
    rrf_k: int = 20
    signal_weights: dict[str, float] = field(
        default_factory=lambda: {
            "lexical": 1.0,
            "symbol": 1.2,
            "structural": 0.8,
            "semantic": 1.0,
            "test_reference": 0.5,
            "dependency": 0.5,
        }
    )
    signal_depth: int = 50  # candidates kept per signal before fusion
    dependency_seeds: int = 5
    test_reference_candidates: int = 20
    # ---- context selection / reads
    read_default_max_chars: int = 12_000
    snippet_max_lines: int = 80
    snippet_context_lines: int = 3
    context_max_snippets_per_file: int = 2
    context_search_k: int = 30
    # ---- service
    auto_index: bool = True  # queries bring the index to the workspace HEAD first
    overlay_max_files: int = 500  # dirty working-tree files parsed live per query
    index_lock_timeout_seconds: float = 600.0
    inline_embed_seconds: float = 20.0  # a query waits at most this long for embeddings, the rest runs in background
    embed_retry_seconds: float = 120.0  # pending embeddings (model was unreachable) are retried at most this often
    index_wait_seconds: float = 120.0  # a query waits at most this long for a running (re)index, then degrades
    query_timeout_seconds: float = 30.0  # per signal; a slow signal is dropped, never fails the query

    def weight(self, signal: str) -> float:
        return float(self.signal_weights.get(signal, 1.0))

    def with_overrides(self, **overrides: Any) -> RepoIntelConfig:
        return replace(self, **overrides)

    @classmethod
    def from_hermclaw(cls, config: Any, **overrides: Any) -> RepoIntelConfig:
        """Derive from a loaded :class:`~hermclaw.core.config.HermclawConfig` (scope-forbidden globs are merged in)."""
        globs = list(DEFAULT_SENSITIVE_GLOBS)
        try:
            extra = list(config.policies.scope.always_forbidden)
        except AttributeError:
            extra = []
        for g in extra:
            if isinstance(g, str) and g.strip() and g not in globs:
                globs.append(g.strip())
        return cls(sensitive_globs=tuple(globs), **overrides)
