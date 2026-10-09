# Model Gateway (P08)

Status: implemented and tested locally (real LiteLLM 1.104.2 proxy against a fake Ollama HTTP server, real
PostgreSQL 16). Live verification on `.225`/`.224` is blocked by BLOCKER-001 (`tests/integration/test_models_live.py`,
marker `live`).

## Purpose

The model gateway is the single access path from the orchestrator (`.225`) to the local models on the model host
(`.224`). It

- implements the `ChatModel` and `EmbeddingModel` protocols (`hermclaw/models/protocols.py`) over LiteLLM's
  OpenAI-compatible HTTP API (`hermclaw/models/gateway.py`);
- enforces the fixed role → model architecture (`hermclaw/models/profiles.py`);
- validates the context window before every call (`hermclaw/models/tokens.py`, DECISIONS D-008);
- controls model residency (load/unload, exclusive resource groups, `num_ctx` verification) on the model host
  (`hermclaw/models/residency.py`);
- reports health without triggering inference and aggregates invocation metrics (`hermclaw/models/health.py`);
- generates the LiteLLM proxy `config.yaml` from `models.yaml` (`hermclaw/models/litellm_config.py`).

Reasoning/thinking text is **never** returned, stored, logged or re-sent. Only its length (`reasoning_chars`) is
kept as a metric.

## Fixed model architecture (8.2–8.7)

| role               | model family       | requirement                                      | step |
|--------------------|--------------------|--------------------------------------------------|------|
| `fast`             | Qwen3 8B           | chat, context ≥ 16K                              | 8.3  |
| `planner`          | Gemma 4 26B A4B    | chat, context ≥ 32K                              | 8.4  |
| `planner_fallback` | Gemma 4 12B        | chat, `fallback_for` = planner alias             | 8.4  |
| `coder`            | Qwen3-Coder 30B    | chat, context ≥ 32K, output 4K–8K                | 8.5  |
| `heavy`            | Qwen3.8 27B        | chat, context 24K–32K                            | 8.6  |
| `embedding`        | EmbeddingGemma 2   | embedding, `embedding_dimensions` set            | 8.7  |

`check_architecture()` returns `ProfileIssue`s; `assert_architecture()` raises
`ConfigError(MODEL_ARCHITECTURE_VIOLATION)` on errors (wrong model family for a role, missing role, duplicate or
malformed alias, fallback pointing at an unknown/different-kind alias, `max_output_tokens >= context_tokens`).
Context/output deviations from the targets are warnings. The gateway constructor and the config generator refuse to
start on errors – there is no silent model replacement.

## Public interfaces

```python
# hermclaw/models/gateway.py
class GatewayOptions:  # frozen dataclass
    keep_alive: str | None = "10m"; ollama_passthrough: bool = True; timeout_retries: int = 1
    timeout_grace_seconds: float = 5.0; enforce_context: bool = True; context_reserve_tokens: int = 0
    embed_batch_size: int = 64; validate_architecture: bool = True; max_excerpt_chars: int = 4000

class LiteLLMGateway:  # ChatModel + EmbeddingModel
    def __init__(self, models: ModelsConfig, *, api_key: str | None = None,
                 session_factory: Callable[[], AsyncSession] | None = None,
                 http_client: httpx.AsyncClient | None = None, options: GatewayOptions | None = None,
                 base_url: str | None = None, embedding_alias: str | None = None, redactor: Redactor | None = None)
    @classmethod
    def from_config(cls, config: HermclawConfig | None = None, *, session_factory=None, http_client=None,
                    options=None) -> LiteLLMGateway          # resolves models.litellm.api_key_ref
    async def chat(self, alias, messages, *, ctx: CallContext, max_tokens=None, temperature=None,
                   json_schema: dict | None = None, timeout_seconds=None) -> ChatResult
    async def structured(self, alias, messages, schema: type[T], *, ctx, max_repairs=2, max_tokens=None,
                         temperature=None, timeout_seconds=None) -> StructuredResult[T]
    async def embed(self, texts: list[str], *, ctx: CallContext) -> list[list[float]]
    async def call_with_fallback(self, alias, fn: Callable[[ModelProfileConfig, bool], Awaitable[R]], *,
                                 ctx: CallContext) -> R
    dimensions: int; model_name: str
    async def aclose(self) -> None   # also usable as `async with`

def is_technical_failure(exc: BaseException) -> bool
def extract_json(text: str, *, max_candidates: int = 256) -> Any      # raises ValueError only
def strip_reasoning(content: str) -> tuple[str, int]
def classify_http_error(status: int, message: str) -> str
TECHNICAL_FAILURE_CODES = {MODEL_UNAVAILABLE, MODEL_LOAD_FAILED, MODEL_NOT_FOUND, MODEL_TIMEOUT, MODEL_PROTOCOL_ERROR}

# hermclaw/models/profiles.py
def resolve_api_key(ref: str | None, *, required: bool = True, env: str | None = None) -> str | None
def check_architecture(models: ModelsConfig) -> list[ProfileIssue]
def assert_architecture(models: ModelsConfig) -> list[ProfileIssue]
class ProfileRegistry:  get(alias) / by_role(role) / fallback_for(alias) / by_model(model, host=None)
                        group_members(group, host=None) / enabled(kind=None) / embedding_profile() / hosts()
async def sync_profiles(session: AsyncSession, models: ModelsConfig) -> SyncReport
async def list_profile_rows(session, *, enabled_only=False) -> list[ModelProfile]
async def get_profile_row(session, alias) -> ModelProfile
async def get_profile_row_by_role(session, role) -> ModelProfile
def profile_from_row(row: ModelProfile) -> ModelProfileConfig
def validate_http_url(url: str, *, what: str = "url") -> str
def ollama_base_url(hosts: HostsConfig, host_id: str) -> str

# hermclaw/models/tokens.py
CHARS_PER_TOKEN = 3.2
def estimate_tokens(text: str) -> int
def estimate_messages_tokens(messages, *, extra_texts=()) -> int
def context_budget(profile, messages, *, max_tokens=None, reserve_tokens=0, extra_texts=()) -> ContextBudget
def validate_context(profile, messages, *, max_tokens=None, reserve_tokens=0, extra_texts=()) -> ContextBudget
def max_prompt_chars(profile, *, max_tokens=None, reserve_tokens=0) -> int

# hermclaw/models/residency.py
class ModelHostClient(Protocol):
    async def loaded_models(self) -> list[LoadedModel]
    async def load(self, model: str, context_tokens: int, keep_alive: str) -> None
    async def unload(self, model: str) -> None
class OllamaHostClient(base_url, *, http_client=None, load_timeout_seconds=900, unload_wait_seconds=60, poll_seconds=0.5)
class WorkerModelHostClient(worker: ModelWorkerClient-like, *, load_timeout_seconds=900)
class ModelResidency(models, host_client: ModelHostClient | Mapping[str, ModelHostClient], *,
                     session_factory=None, keep_alive="10m", require_context_match=True)
    async def ensure_loaded(alias, *, lease_id=None, job_id=None, step_id=None) -> ResidencyResult
    async def unload(alias, *, lease_id=None, job_id=None, step_id=None, reason="requested") -> bool
    async def unload_group(resource_group, *, host=None, reason="requested") -> list[str]
    async def status(host=None) -> list[ResidentModel]

# hermclaw/models/health.py
class ModelHealthChecker(models, *, ollama_urls: Mapping[str, str] | None = None, api_key=None,
                         http_client=None, base_url=None, timeout_seconds=10.0)
    async def check(self) -> ModelHealthReport      # + liveliness() / readiness() / proxy_models() / ollama_host(h)
async def invocation_metrics(session, *, since=None, until=None, alias=None, job_id=None, purpose=None) -> list[AliasMetrics]
async def error_breakdown(session, *, since=None, alias=None) -> dict[str, dict[str, int]]

# hermclaw/models/litellm_config.py
def build_litellm_config(models, *, ollama_urls_by_host, keep_alive="10m", master_key_env="LITELLM_MASTER_KEY",
                         check_architecture=True) -> dict
def write_litellm_config(path: Path, config: Mapping) -> Path        # atomic, 0644, no secrets inside
python -m hermclaw.models.litellm_config OUT|- [--config-dir DIR] [--keep-alive 10m] [--master-key-env NAME]
                                         [--ollama-url HOST=URL ...]
```

## Request mapping – empirical finding (8.1)

Verified by `tests/integration/test_models_litellm_proxy.py` against a **real** LiteLLM 1.104.2 proxy started from the
generated config, with a fake Ollama HTTP server that records every request body:

| gateway request field (`/v1/chat/completions`) | arrives at Ollama `/api/chat` as        | test |
|------------------------------------------------|-----------------------------------------|------|
| `model` = Hermclaw alias                        | `model` = Ollama tag                    | `test_chat_request_fields_reach_ollama` |
| `max_tokens`                                    | `options.num_predict`                   | same |
| `temperature`                                   | `options.temperature`                   | same |
| `response_format: {type: json_schema, json_schema: {name, schema, strict}}` | `format` = the JSON schema (Ollama structured outputs) | same |
| top-level `think` (bool)                        | top-level `think` (request beats config default) | `test_request_level_think_overrides_proxy_default` |
| top-level `num_ctx`                             | `options.num_ctx` (request beats config default) | `test_request_level_num_ctx_and_keep_alive_override_proxy_defaults` |
| top-level `keep_alive`                          | top-level `keep_alive` (request beats config)    | same |
| top-level `timeout` (seconds)                   | not forwarded – the proxy aborts the upstream call and answers **HTTP 408** | `test_proxy_timeout_maps_to_model_timeout` |
| `/v1/embeddings` `options.num_ctx`, `keep_alive` | `/api/embed` `options.num_ctx`, `keep_alive` | `test_embeddings_through_proxy` |

No `extra_body` wrapper is needed: LiteLLM's `ollama_chat` provider maps the top-level fields above directly.
The proxy config additionally sets `num_ctx`, `keep_alive` and `think` per deployment (`litellm_params`), so a client
that sends nothing Ollama-specific still gets the correct values (`test_proxy_config_defaults_apply_without_passthrough`).

Ollama's `message.thinking` is exposed by LiteLLM as `reasoning_content`; the gateway counts it (and
`reasoning`/`thinking`/`provider_specific_fields`, plus inline `<think>…</think>` blocks) into `reasoning_chars` and
discards it.

Second finding: with LiteLLM's default settings a failing or slow deployment is retried **silently** (three upstream
requests for one client call were observed). The generated config therefore sets `num_retries: 0` (litellm and router
settings) and `disable_cooldowns: true`; retries and fallbacks are decided – and evented – only by the Hermclaw
gateway (`test_load_error_falls_back_once_without_litellm_retries` asserts exactly one upstream call per model).

Third finding: LiteLLM's `/health` endpoint sends real completions to every deployment (which would load models on
`.224` behind the resource manager's back). The generated config sets `background_health_checks: false` and the health
checker only uses `/health/liveliness`, `/health/readiness` and `/v1/models`.

## Data and event flow

```
caller (planner/coder/review/…)
  └─ LiteLLMGateway.chat/structured/embed(alias, …, ctx=CallContext)
       ├─ ProfileRegistry.get(alias)                       (unknown/disabled → ConfigError)
       ├─ validate_context(profile, messages, max_tokens)  (overflow → ValidationFailed CONTEXT_OVERFLOW, no HTTP call)
       ├─ INSERT model_invocations(status=started) + event model.invocation.started      (one transaction)
       ├─ POST {litellm}/v1/chat/completions | /v1/embeddings   (Bearer master key)
       │     └─ LiteLLM → Ollama /api/chat | /api/embed on .224
       ├─ parse: content only; reasoning counted + dropped; usage, finish_reason
       ├─ UPDATE model_invocations(status=succeeded|invalid|failed|timeout|cancelled, tokens, latency,
       │                           finish_reason, response_valid, response_excerpt ≤ 4000 chars, error_code)
       │   + event model.invocation.finished (severity info | warning)
       └─ technical failure → call_with_fallback → event planner.fallback.used (warning) → retry once on fallback
```

`model_invocations` columns written: `alias, model, role, purpose, job_id, step_id, attempt_id, status,
prompt_tokens, completion_tokens, reasoning_chars, latency_ms, finish_reason, response_valid, repair_attempt,
fallback_used, request_hash (sha256 of the canonical request body), response_excerpt (final content only, redacted,
≤ 4000 chars), error_code, error_message (redacted, ≤ 2000 chars), started_at, finished_at`.
Event payloads carry ids, alias/model/role/purpose, counts, latency, status and the request hash – never prompt or
response text. Without a session factory (unit tests, tools) the gateway works without persistence.

`structured()`: JSON-schema constrained call (`response_format`) → `extract_json` (plain JSON, fenced block, or the
first embedded value; bounded scan; all failures incl. pathological nesting surface as invalid output) → Pydantic
validation. On failure up to `max_repairs` repair calls follow: original conversation + the rejected *final content*
(≤ 6000 chars; dropped when it would overflow the context window) + the validation errors. Empty content (everything
went into thinking) counts as invalid. Budget exhausted → `ModelOutputInvalid` with all errors and invocation ids. Once
the fallback served a call, the repairs stay on the fallback (no switching back and forth).

Profile sync: `sync_profiles()` upserts every configured profile into `model_profiles`
(`INSERT … ON CONFLICT DO UPDATE`), disables rows that are no longer configured (never deletes them – invocations
reference aliases historically) and reports created/updated/disabled.

## Residency (8.10)

`ModelResidency.ensure_loaded(alias, lease_id=…)` implements steps 4–7 of the model switch protocol (Bauplan §4):

1. `loaded_models()` (Ollama `/api/ps`); already resident with the right `context_length` and no conflict → no-op.
2. Unload every resident member of the alias' exclusive resource group on the same host (`keep_alive: 0`, then poll
   `/api/ps` until it is gone) → `model.unloaded` per model. A resident target with the wrong `num_ctx` is unloaded
   and reloaded explicitly.
3. `model.load.started` → load with `options.num_ctx = profile.context_tokens` and `keep_alive`
   (`/api/generate` with empty prompt; embedding models via `/api/embed` with empty input).
4. Verify via `/api/ps`: resident and `context_length == context_tokens` (else `MODEL_CONTEXT_MISMATCH`).
5. `model.load.finished` (`ok: true` + result, or `ok: false`, `phase: unload|load`, error code, severity error).
   If a conflicting model cannot be unloaded the target is **not** loaded next to it.

Operations are serialised per host inside the process (`asyncio.Lock`). Whether a switch may happen at all (leases,
priorities, preemption across processes) is decided by the resource manager; the lease id is only recorded.
`WorkerModelHostClient` adapts the model worker daemon client (`hermclaw.workers.client.ModelWorkerClient`) to the
same protocol for when residency goes through the worker on `.224`.

## Health (8.8) and metrics (8.11)

`ModelHealthChecker.check()` → `ModelHealthReport`: LiteLLM liveliness/readiness, registered aliases (`/v1/models`,
needs the key), per model host Ollama `/api/version`, `/api/tags` (installed) and `/api/ps` (resident, context
length), and per profile `registered_in_proxy`, `installed`, `loaded`, `context_matches`, `available`, `reason`.
`healthy` = proxy ready and every enabled profile available. Health never sends an inference request.

`invocation_metrics()` aggregates `model_invocations` per alias (optionally filtered by time window, alias, job,
purpose): calls, succeeded/invalid/failed/timeouts/cancelled/in-flight, fallback and repair calls, prompt/completion
tokens, reasoning chars, avg/p50/p95/p99/max latency (PostgreSQL `percentile_cont`), `error_rate`, `invalid_rate`.
`error_breakdown()` returns `{alias: {error_code: count}}`.

## Configuration keys

- `models.litellm.base_url` – LiteLLM proxy URL (absolute http(s), no credentials/query; default `http://127.0.0.1:4000`).
- `models.litellm.api_key_ref` – secret reference for the master key (`cred:` systemd credential, `file:` not group/world readable in production;
  `env:`/`literal:` only outside production) – resolved through `hermclaw.security.secrets.SecretStore`, which also
  registers the value with the redactor.
- `models.litellm.request_timeout_seconds` – upper bound for any single call.
- `models.model_host_capacity_gb` – capacity warning threshold for resident models.
- `models.profiles[]`: `alias, role, model, kind, host, context_tokens, max_output_tokens, temperature, think,
  timeout_seconds, priority, resource_group, exclusive, memory_gb, fallback_for, embedding_dimensions, enabled`.
- `hosts.yaml`: the `ollama` service port of the model host (default 11434) for residency/health/config generation.
- LiteLLM process environment: `LITELLM_MASTER_KEY` (from a systemd credential; never written into the config file).

## Failure behaviour

| situation | result | fallback? |
|-----------|--------|-----------|
| LiteLLM unreachable / connection error | `ModelError(MODEL_UNAVAILABLE)` | yes |
| Ollama cannot load the model (5xx, "requires more system memory", runner errors) | `ModelError(MODEL_LOAD_FAILED)` | yes |
| model not installed (404) | `ModelError(MODEL_NOT_FOUND)` | yes |
| malformed response body | `ModelError(MODEL_PROTOCOL_ERROR)` | yes |
| timeout (client or proxy 408/504) | `ModelTimeout`; retried `timeout_retries` (1) times on the same alias, then fallback | after repeated timeout |
| 401/403 | `ModelError(MODEL_AUTH_FAILED)` | no |
| 429 | `ModelError(MODEL_RATE_LIMITED)` | no |
| other 4xx | `ModelError(MODEL_BAD_REQUEST)` | no |
| invalid structured output after repairs | `ModelOutputInvalid` | no (never a reason to switch) |
| context overflow (estimate) | `ValidationFailed(CONTEXT_OVERFLOW)` before any HTTP call | no |
| embedding dimension mismatch | `ModelError(EMBEDDING_DIMENSION_MISMATCH)` | embeddings never fall back |
| DB failure before the call | the call is refused (no untracked model calls) | – |
| DB failure after the call | result is returned, row stays `started` (visible orphan), error logged | – |
| cancellation | row `cancelled`, finish event written (shielded) | – |

Every fallback emits `planner.fallback.used` (warning) with primary/fallback alias and model, role, purpose, redacted
reason, error code and timeout count; a fallback that also fails raises its own error with `primary_alias`,
`primary_error_code` and `fallback_alias` in `details`. Error messages from LiteLLM/Ollama pass through the redactor
before they reach exceptions, rows or events; the API key is registered with the redactor on construction.

## Operating on the real hosts

1. Generate the proxy config on `.225`:
   `python -m hermclaw.models.litellm_config /etc/hermclaw/litellm/config.yaml` (reads `HERMCLAW_CONFIG_DIR`).
2. Run LiteLLM with the master key from a systemd credential, e.g.
   `LITELLM_MASTER_KEY=$(cat $CREDENTIALS_DIRECTORY/litellm-master-key) litellm --config /etc/hermclaw/litellm/config.yaml --host 127.0.0.1 --port 4000`.
   LiteLLM must be installed with the proxy extras (`litellm[proxy]`).
3. Ollama on `.224`: `OLLAMA_HOST=0.0.0.0:11434`, `OLLAMA_MAX_LOADED_MODELS=2` (see research 007).
4. Sync profiles at startup: `await sync_profiles(session, cfg.models)`.
5. Live verification (BLOCKER-001):
   `HERMCLAW_LIVE_LITELLM_KEY_REF=file:/etc/hermclaw/secrets/litellm-master-key .venv/bin/pytest -m live tests/integration/test_models_live.py`
   (optional `HERMCLAW_LIVE_LITELLM_URL`, `HERMCLAW_LIVE_OLLAMA_URL`). It checks health, fast-router content-only
   answers, planner structured output, the planner fallback alias, coder residency with verified `num_ctx` and the
   embedding dimensions. It loads models on `.224` – run it only when no job holds a model lease.

## Testing locally

```
.venv/bin/pytest -q tests/unit/test_models_gateway.py tests/unit/test_models_profiles.py \
    tests/integration/test_models_gateway_db.py tests/integration/test_models_residency.py \
    tests/integration/test_models_litellm_proxy.py tests/failure/test_models_failures.py
```

The empirical proxy tests need a `litellm` CLI with the proxy extras. The repo venv currently has `litellm` without
them (`backoff` etc. missing), so they skip unless `HERMCLAW_TEST_LITELLM_BIN` points at a venv with
`litellm[proxy]==1.104.2` (shared change requested: add `litellm[proxy]` to the dev/test dependencies).
