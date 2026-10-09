"""Typed results of repository intelligence (inventory, symbols, search hits)."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ============================================================================================= inventory
class FileEntry(_M):
    path: str
    size: int
    language: str | None = None
    category: str = "unknown"  # programming|markup|data|config|prose|unknown
    binary: bool = False
    sensitive: bool = False  # matches a secret/forbidden glob: listed, never read
    test: bool = False


class LanguageStat(_M):
    language: str
    category: str
    files: int
    bytes: int


class BuildSystem(_M):
    name: str  # python-pyproject|setuptools|npm-package|composer|make|cmake|go-modules|cargo|maven|gradle|...
    path: str
    detail: str | None = None  # e.g. build backend


class PackageManager(_M):
    name: str  # pip|poetry|uv|pdm|pipenv|npm|yarn|pnpm|bun|composer|go|cargo|maven|gradle|bundler
    manifest: str | None = None
    lockfile: str | None = None


class TestFramework(_M):
    __test__: ClassVar[bool] = False
    name: str  # pytest|unittest|jest|vitest|mocha|phpunit|pest|go-test|cargo-test|junit|rspec
    evidence: list[str] = Field(default_factory=list)
    command: str | None = None  # suggested invocation (the verifier decides what actually runs)


class TestInfo(_M):
    __test__: ClassVar[bool] = False
    frameworks: list[TestFramework] = Field(default_factory=list)
    test_dirs: list[str] = Field(default_factory=list)
    test_file_count: int = 0
    test_files: list[str] = Field(default_factory=list)  # capped sample
    scripts: dict[str, str] = Field(default_factory=dict)  # package.json/composer test scripts


class DockerInfo(_M):
    dockerfiles: list[str] = Field(default_factory=list)
    compose_files: list[str] = Field(default_factory=list)
    base_images: list[str] = Field(default_factory=list)
    exposed_ports: list[str] = Field(default_factory=list)
    compose_services: list[str] = Field(default_factory=list)


class CIConfig(_M):
    provider: str  # gitlab|github|jenkins|circleci|azure|travis|bitbucket|drone|woodpecker
    path: str
    jobs: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)


class MigrationSet(_M):
    tool: str  # alembic|django|knex|laravel|flyway|rails|prisma|sequelize|typeorm|doctrine|sql
    path: str
    count: int
    tables: list[str] = Field(default_factory=list)


class ConfigFile(_M):
    path: str
    kind: str  # yaml|toml|ini|env-example|nginx|json|xml|properties|editorconfig|dotfile


class EntryPoint(_M):
    kind: str  # python-main|python-module-main|console-script|npm-script|npm-bin|npm-main|composer-bin|go-main|...
    name: str
    path: str | None = None
    target: str | None = None


class Route(_M):
    method: str  # GET|POST|...|ANY|USE
    path: str
    handler: str | None = None
    file: str
    line: int
    framework: str = "generic"


class ReadmeInfo(_M):
    path: str
    title: str | None = None
    excerpt: str = ""


class GitInfo(_M):
    is_repo: bool = False
    branch: str | None = None
    head: str | None = None
    detached: bool = False
    dirty: bool = False
    status_counts: dict[str, int] = Field(default_factory=dict)
    changed: list[str] = Field(default_factory=list)  # capped "XY path" entries


class RepoInventory(_M):
    """Deterministic inventory of one workspace (Bauplan §14 Phase A)."""

    root_name: str
    file_count: int
    total_bytes: int
    truncated: bool = False
    files: list[FileEntry] = Field(default_factory=list)
    files_listed_truncated: bool = False
    top_level: list[str] = Field(default_factory=list)
    languages: list[LanguageStat] = Field(default_factory=list)
    primary_languages: list[str] = Field(default_factory=list)
    build_systems: list[BuildSystem] = Field(default_factory=list)
    package_managers: list[PackageManager] = Field(default_factory=list)
    tests: TestInfo = Field(default_factory=TestInfo)
    docker: DockerInfo = Field(default_factory=DockerInfo)
    ci: list[CIConfig] = Field(default_factory=list)
    migrations: list[MigrationSet] = Field(default_factory=list)
    config_files: list[ConfigFile] = Field(default_factory=list)
    entry_points: list[EntryPoint] = Field(default_factory=list)
    routes: list[Route] = Field(default_factory=list)
    readme: ReadmeInfo | None = None
    git: GitInfo = Field(default_factory=GitInfo)
    warnings: list[str] = Field(default_factory=list)

    def summary(self, *, max_items: int = 30) -> dict[str, object]:
        """Compact, prompt-sized view for the planner (no full file list)."""
        return {
            "root": self.root_name,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "top_level": self.top_level[:max_items],
            "primary_languages": self.primary_languages,
            "languages": [{"language": s.language, "files": s.files} for s in self.languages[:12]],
            "build_systems": [b.model_dump(exclude_none=True) for b in self.build_systems[:max_items]],
            "package_managers": [p.model_dump(exclude_none=True) for p in self.package_managers[:max_items]],
            "tests": {
                "frameworks": [f.model_dump(exclude_none=True) for f in self.tests.frameworks],
                "test_dirs": self.tests.test_dirs[:max_items],
                "test_file_count": self.tests.test_file_count,
                "scripts": dict(list(self.tests.scripts.items())[:10]),
            },
            "docker": self.docker.model_dump(),
            "ci": [c.model_dump() for c in self.ci[:10]],
            "migrations": [m.model_dump() for m in self.migrations[:10]],
            "config_files": [c.path for c in self.config_files[:max_items]],
            "entry_points": [e.model_dump(exclude_none=True) for e in self.entry_points[:max_items]],
            "routes": [f"{r.method} {r.path} -> {r.file}:{r.line}" for r in self.routes[: max_items * 2]],
            "route_count": len(self.routes),
            "readme": self.readme.model_dump() if self.readme else None,
            "git": self.git.model_dump(),
            "warnings": self.warnings[:10],
        }


# ============================================================================================= structure
SymbolKind = Literal["function", "method", "class", "interface", "trait", "enum", "type", "constant", "route", "table", "view", "import"]


class SymbolRecord(_M):
    name: str
    kind: str
    language: str
    path: str
    start_line: int
    end_line: int
    parent: str | None = None
    signature: str | None = None
    references: list[str] = Field(default_factory=list)  # call names inside the body / resolved files for imports


class ImportRecord(_M):
    module: str
    names: list[str] = Field(default_factory=list)
    line: int = 1
    kind: str = "import"  # import|from|require|dynamic|include|use
    level: int = 0  # python relative-import level
    resolved: str | None = None  # workspace path when resolvable (11.7)


class FileSymbols(_M):
    path: str
    language: str
    symbols: list[SymbolRecord] = Field(default_factory=list)
    imports: list[ImportRecord] = Field(default_factory=list)
    calls: list[str] = Field(default_factory=list)  # module-level call references
    parse_error: str | None = None


# ============================================================================================= search results
class LexicalHit(_M):
    path: str
    line: int
    column: int = 1
    text: str
    matches: list[str] = Field(default_factory=list)


class LexicalResult(_M):
    hits: list[LexicalHit] = Field(default_factory=list)
    truncated: bool = False
    engine: Literal["ripgrep", "python"] = "ripgrep"
    elapsed_ms: int = 0


class FileMatch(_M):
    path: str
    score: float


class ChunkHit(_M):
    path: str
    start_line: int
    end_line: int
    symbol: str | None = None
    language: str = "text"
    score: float  # cosine similarity (1 - distance)
    distance: float
    content: str = ""
    git_sha: str = ""


class SignalHit(_M):
    """One candidate of one ranking signal (raw score: higher is better)."""

    path: str
    score: float
    start_line: int = 1
    end_line: int = 1
    detail: str = ""


class FileHit(_M):
    """Fused result (Reciprocal Rank Fusion over all signals)."""

    path: str
    score: float  # normalised to [0, 1]
    rrf: float
    signals: dict[str, float] = Field(default_factory=dict)  # raw per-signal scores
    ranks: dict[str, int] = Field(default_factory=dict)
    start_line: int = 1
    end_line: int = 1
    reasons: list[str] = Field(default_factory=list)
    snippet: str = ""


class IndexStats(_M):
    repository_key: str
    git_sha: str
    base_sha: str | None = None
    mode: Literal["full", "incremental", "noop"]
    source: Literal["git", "worktree"] = "git"
    run_id: str | None = None
    files_total: int = 0
    files_indexed: int = 0
    files_deleted: int = 0
    files_skipped: dict[str, int] = Field(default_factory=dict)
    changed_paths: list[str] = Field(default_factory=list)  # capped
    deleted_paths: list[str] = Field(default_factory=list)  # capped
    symbols: int = 0
    imports: int = 0
    chunks: int = 0
    embeddings_reused: int = 0
    embedded: int = 0
    embedding_pending: int = 0
    embedding_model: str | None = None
    embedding_error: str | None = None
    duration_ms: int = 0
    index_version: int = 0
