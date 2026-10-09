# Repository Intelligence (P11)

`hermclaw/repo_intelligence/` – deterministic inventory, lexical/structural/semantic retrieval, fusion ranking,
targeted reads and incremental indexing by Git SHA (Bauplan §14 "Repository Intelligence", Phase 11, steps
11.1–11.12). It is the `RepoContextProvider` (`hermclaw/core/interfaces.py`) used by the planner, scope engine,
context builder, tool engine and reviewer.

Nothing in here knows a specific project or benchmark: every rule is a generic, documented convention (module
systems, framework route declarations, manifest formats, test naming conventions).

## Modules

| Module | Content | Steps |
|---|---|---|
| `inventory.py` | `build_inventory(root, cfg) -> RepoInventory`: files, sizes, languages, build systems, package managers (+lockfiles), tests (dirs, frameworks, scripts), Docker, CI, migrations, config files, entry points, routes, README excerpt, git branch/head/status | 11.1–11.3 |
| `languages.py` | language detection by name/extension/shebang, binary detection, categories | 11.2 |
| `routes.py` | generic route extraction: FastAPI/Flask/Starlette/Django (Python `ast`), Express/Koa/Fastify/NestJS/Next.js, Laravel/Slim/Symfony, plain-PHP `$_GET`/`$_POST` endpoints, Go net/http/gin, Spring, Rails | 11.1, 11.6 |
| `lexical.py` | `LexicalSearcher`: ripgrep `--json` (fixed/regex, globs, word, case, limits, timeout), Python fallback with identical semantics, file-name search | 11.4 |
| `symbols.py` | AST adapters (Python `ast`; JS/TS/TSX/PHP via `tree_sitter_language_pack.get_parser`), `code_symbols` persistence and queries (`query_symbols`, `referencing_files`, `load_imports`) | 11.5, 11.6 |
| `tables.py` | DB-schema awareness: `CREATE TABLE/VIEW` in SQL and migration DSLs (alembic, Django, Laravel, knex/sequelize/typeorm, Rails, Doctrine, SQLAlchemy `__tablename__`) → symbols `kind=table/view` | 11.5 |
| `dependencies.py` | `ModuleResolver` (imports → workspace files: Python packages/relative imports, JS/TS relative specifiers with extension/index probing, PHP include literals + composer PSR-4 of every workspace `composer.json`), `DependencyGraph` | 11.7 |
| `chunking.py` | symbol-aligned chunks (≤ `chunk_max_tokens` ≈ 1 500 tokens, member split, line windows with overlap, small-segment merging); `content_hash` over the exact (redacted) embedding input | 11.8 |
| `embeddings.py` | `code_chunks` storage (`vector(768)` + `embedding_model`), batched embedding of pending chunks, vector re-use by `content_hash`, `semantic_search` with pgvector cosine `<=>` (exact scan for small indexes, HNSW `vector_cosine_ops` + `hnsw.ef_search` otherwise) | 11.8, 11.9 |
| `query.py` | query analysis: identifiers, sub-terms (camel/snake split, plural folding, EN/DE stop words), phrases, URL paths | 11.10 |
| `ranking.py` | signal builders + Reciprocal Rank Fusion | 11.10 |
| `reader.py` | `FileReader`: line ranges, character budget, binary detection, size limit, workspace confinement | 11.11 |
| `indexer.py` | `RepoIndexer`: full / incremental / noop runs by Git SHA, `repo_index_runs`, events, locks, embedding, purge | 11.12 |
| `service.py` | `RepoIntelligence` facade (`RepoContextProvider`) + `BoundRepo`; overlay for uncommitted edits; context selection | Phase F |
| `redact.py` | `redact_code`: shared redactor + quoted secret literals assigned to secret-named identifiers | – |
| `paths.py`, `fileio.py`, `sources.py`, `_proc.py` | path normalisation/containment/globs, `O_NOFOLLOW` reads, file listings (`git ls-files` / `rg --files` / walk), committed tree reader (`git ls-tree` + `git cat-file --batch`), argv-only subprocesses with timeouts and process-group kill | – |
| `config.py`, `schemas.py` | `RepoIntelConfig` (all knobs, defaults), Pydantic result models | – |

## Public interfaces

```python
# facade (implements hermclaw.core.interfaces.RepoContextProvider)
RepoIntelligence(sessionmaker=None, *, embedder: EmbeddingModel | None = None, config: RepoIntelConfig | None = None, emit_events=True)
await svc.inventory_summary(ws: WorkspaceHandle) -> dict[str, Any]              # RepoInventory.summary() + index state
await svc.search(ws, query: str, *, k=20) -> list[RepoHit]                      # fused ranking, redacted snippets
await svc.find_symbol(ws, name: str, *, k=20) -> list[RepoHit]                  # "Class.method", "Ns\\Cls::m", "fn()" …
await svc.read(ws, path, start=1, end=None, *, max_chars=12_000) -> str        # raw lines (exact, for patches)
await svc.context_for(ws, goal: str, *, budget_chars=24_000) -> list[RepoHit]  # Phase F, redacted, within budget
svc.index_key(ws) -> str ; svc.target_of(ws) -> IndexTarget
await svc.drop_workspace_index(ws) -> dict[str, int]                           # call on workspace cleanup
svc.bind(ws) / svc.open(root, repository_key) -> BoundRepo
await svc.aclose() ; await svc.wait_background()

# root-bound API
BoundRepo.inventory() -> RepoInventory
BoundRepo.search(query, k=20) -> list[FileHit]          # per-signal raw scores, ranks, reasons, line range
BoundRepo.semantic(query, k=20) -> list[ChunkHit]       # cosine similarity + distance, nearest first
BoundRepo.symbols(name, k=20) -> list[RepoHit]
BoundRepo.read(path, start=1, end=None, *, max_chars=None) -> ReadResult   # .text raw, .numbered() for display
BoundRepo.context_for(goal_text, budget=24_000) -> list[RepoHit]
BoundRepo.grep(pattern | [patterns], regex=False, globs=(), exclude_globs=(), case_sensitive=None, word=False, max_results=200) -> LexicalResult
BoundRepo.find_files(query, *, limit=50) -> list[FileMatch]
BoundRepo.index(*, force_full=False) -> IndexStats

# lower level
build_inventory(root: Path, cfg=None, *, paths=None) -> RepoInventory
LexicalSearcher(cfg).search(root, patterns, *, mode="fixed"|"regex", globs, exclude_globs, paths, case_sensitive, word, max_results, max_per_file, timeout_s, engine="auto"|"ripgrep"|"python") -> LexicalResult
RepoIndexer(sessionmaker, *, embedder=None, config=None)
await indexer.index(IndexTarget(root, repository_key, workspace_id=None, repository_id=None, job_id=None, repository=None), *, force_full=False, embed=True) -> IndexStats
await indexer.embed(target, *, run_id=None, max_chunks=None) -> EmbedOutcome
await indexer.purge(target) -> {"symbols": n, "chunks": n}
await semantic_search(session, embedder, repository_key, query, *, k, cfg, ctx, exclude_paths=()) -> list[ChunkHit]
extract_file_symbols(path, language, text) -> FileSymbols
FileReader(cfg).read(root, path, start=1, end=None, *, max_chars=None) -> ReadResult
RepoIntelConfig.from_hermclaw(hermclaw_config, **overrides) -> RepoIntelConfig   # merges policies.scope.always_forbidden
```

## Data and event flow

```
query ──► refresh(target) ──► RepoIndexer.index(embed=False)   (bounded wait: index_wait_seconds)
              │                     │ lock: asyncio.Lock + pg_try_advisory_lock(sha256(key)) on an AUTOCOMMIT connection
              │                     │ mode: noop (same SHA + INDEX_VERSION + index_config) | incremental | full
              │                     │ git ls-tree/cat-file of HEAD (never uncommitted content)
              │                     │ parse (thread) → symbols/imports/routes/tables → resolve imports → chunks
              │                     │ one transaction: swap rows of touched paths, re-resolve imports of untouched
              │                     │   files when the file set changed, finish run (inventory + stats), events
              │                     └► embed pending chunks (inline ≤ inline_embed_seconds, rest in background)
              ▼
          _state: git status → overlay (dirty files parsed live, deleted files hidden)
              ▼
  signals (each guarded by query_timeout_seconds; failure → "degraded", never an exception)
    lexical      rg over the live working tree (idf × occurrences × coverage)
    symbol       code_symbols name match (+ overlay)
    structural   path tokens, routes, tables, query-matching definitions used by other files (JSONB @>)
    semantic     pgvector cosine kNN (+ similarity floors)
    dependency   import-graph neighbours of the preliminary top files
    test_reference tests importing / mentioning / named after a candidate (× preliminary relevance)
              ▼
  Reciprocal Rank Fusion: Σ w_s·boost / (k + rank_s), per-signal relative floors, normalised to [0, 1]
              ▼
  FileHit list → search() snippets / context_for() fair-share budget reads (redacted)
```

Persistence: `code_symbols` (one row per definition, route, table/view, import (`kind=import`, resolution in
`references[0]`) and per-file module-level calls (`kind=module`)), `code_chunks` (content, line range, symbol,
language, `content_hash`, `embedding vector(768)`, `embedding_model`, `git_sha` of the run that produced the row),
`repo_index_runs` (`git_sha`, `base_index_sha`, status `running|finished|failed|purged`, `inventory`, `stats` incl.
`repository_key`, `repository`, `mode`, `index_version`, `index_config`, embedding counters/errors).

Events (`append_event`, `source_type="repo_intelligence"`, `source_id=<index key>`):
`repo.inventory.started` / `repo.inventory.finished` (index runs and live inventories), `repo.index.updated`
(`phase=index|embedding|purge`, failed runs with `severity=error`, embedding failures with `severity=warning`),
`repo.search.executed` (`kind=fusion|symbol|lexical|semantic|filename`, redacted query, top hits, degraded signals).

Index keys: with `index_scope="workspace"` (default) every job workspace has its own key
`<repository>@ws:<workspace-id-prefix>`, so concurrent jobs on different commits never read each other's rows; vectors
of identical embedding inputs are copied from sibling keys of the same repository (`embedding_reuse_max_keys`)
instead of calling the model again. `index_scope="repository"` shares one index per repository.

## Configuration

`RepoIntelConfig` (dataclass, `config.py`); there is no YAML section yet – `from_hermclaw()` takes the security
relevant part from `policies.scope.always_forbidden` (merged into `sensitive_globs`). Main knobs:
`sensitive_globs`, `index_exclude_globs`, `max_files`, `max_index_file_bytes` (512 kB), `max_read_file_bytes`,
`inventory_read_budget_bytes`, `git_timeout_seconds`, `rg_timeout_seconds`, `rg_binary`, `git_binary`,
`lexical_*` (results, per file, terms, columns, `lexical_sort_paths`), `chunk_max_tokens` (1 500),
`chunk_min_tokens`, `chunk_overlap_lines`, `embed_batch_size`, `embed_document_template` /
`embed_query_template` (EmbeddingGemma prompt format), `semantic_exact_scan_max_rows`, `hnsw_ef_search`,
`semantic_min_similarity`, `semantic_relative_floor`, `rrf_k`, `signal_weights`, `signal_relative_floors`,
`signal_depth`, `read_default_max_chars`, `snippet_*`, `context_*`, `index_scope`, `embedding_reuse_max_keys`,
`auto_index`, `inline_embed_seconds`, `embed_retry_seconds`, `index_wait_seconds`, `query_timeout_seconds`,
`index_lock_timeout_seconds`, `overlay_max_files`.

## Security

* Subprocesses: argv only (no shell), `--` before paths, patterns via `--regexp`, hard timeouts, output caps,
  process-group kill. Without ripgrep, caller-supplied regexes run in a short-lived isolated child interpreter
  (`python -I`, killed at the deadline) because Python's `re` holds the GIL while backtracking.
  Git is read-only (`GIT_OPTIONAL_LOCKS=0`, hooks/fsmonitor disabled); Git mutations stay with
  the runtime-controlled Git engine.
* Paths: normalised, no absolute paths / `..` / control characters, symlinks never followed out of the workspace
  (reads resolve and re-check; listings and explicit search paths drop symlinks), `.git` never readable.
* Secrets: files matching `sensitive_globs` are listed (flagged) but never read, parsed, chunked, embedded or searched.
  Snippets, README excerpts, embedding inputs, semantic `content`, event queries and error messages are redacted
  (`redact_code` = shared `Redactor` + secret-literal assignments). `read()` returns exact content for patches; the
  caller (tool engine) redacts tool output.
* SQL: SQLAlchemy parameters only; `LIKE` patterns escaped; the only formatted statement is an integer `SET LOCAL`.
* No model reasoning is stored; the embedding model receives only redacted chunk text.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| embedding host down / invalid vectors (count, dimension, NaN, zero) | index run finishes; chunks stay pending (`embedding_pending`, `embedding_error`, warning event); semantic signal degraded; retried by `indexer.embed()` / next query after `embed_retry_seconds`; invalid vectors are never stored |
| database down | queries degrade to live retrieval (ripgrep, live parse for `find_symbol`, live inventory, reads); `degraded`/`warnings` name the failed parts |
| index run raises | run row `failed` (redacted error), `repo.index.updated` with `severity=error`; next run is full |
| lock busy (another process) | `ResourceUnavailable(REPO_INDEX_BUSY)` after `index_lock_timeout_seconds`; queries degrade (`index: …`) |
| previous SHA unknown / index version or index-relevant config changed / non-git workspace | full rebuild |
| unparsable / deeply nested / huge / binary / empty files | chunked without structure (`parse_error`) or skipped with a reason in `files_skipped` |
| ripgrep missing / slow | Python fallback / truncated result after `rg_timeout_seconds` |
| git missing | non-git listing (ripgrep `--files` honours `.gitignore`, else directory walk) |

## Tests

```
.venv/bin/pytest -q tests/unit/test_repo_intelligence_*.py tests/integration/test_repo_intelligence_*.py tests/failure/test_repo_intelligence_failures.py
```

Unit: inventory/routes/languages (real git), lexical (real ripgrep + fallback), structure (AST adapters, resolver,
chunking, query analysis), ranking (RRF math), reader/paths/redaction. Integration (fresh PostgreSQL 16 + pgvector):
full + incremental index (only changed files re-chunked/re-embedded, deletions removed, re-resolution), vector
re-use, fallbacks to full, concurrency, workspace keys + purge, service facade (fusion, pgvector cosine order, HNSW
path, symbols, reads, context budget, overlay, events). Failure: embedding down/invalid, slow embedding, database
down, failing run, busy lock, pathological files, missing git. The fixture repository contains a FastAPI app, an
Express/TypeScript app, a PHP app (Laravel routes + plain superglobal endpoints), SQL/alembic migrations,
Dockerfile/compose, GitLab/GitHub CI, configs, a secret key file and a binary.

## Operating on the real hosts

The orchestrator (`.225`) runs the service in-process with the production PostgreSQL 17 + pgvector ≥ 0.8.2 and the
EmbeddingGemma alias of the LiteLLM gateway (`LiteLLMGateway` implements `EmbeddingModel`, 768 dimensions).
Live check (BLOCKER-001 in the build environment; run on the LAN):

```
HERMCLAW_LIVE_LITELLM_KEY_REF=file:/etc/hermclaw/secrets/litellm-master-key \
  .venv/bin/pytest -m live tests/integration/test_repo_intelligence_live.py
```

It indexes the fixture repository with the real model and asserts zero pending/failed embeddings and correct
nearest neighbours. Requirements on the host: `git` and `ripgrep` (`apt install ripgrep`; without it the Python
fallback is used), `tree_sitter_language_pack` in the venv. Inspect state with
`SELECT status, git_sha, stats->>'mode', stats->>'embedding_pending' FROM repo_index_runs ORDER BY created_at DESC`
and the `repo.*` events; a stuck index is recovered by `RepoIndexer.index(target, force_full=True)`, a workspace
index is removed with `RepoIntelligence.drop_workspace_index(handle)`.
