# Research Engine (P12)

`hermclaw/research/` covers Bauplan §23 "Research Engine", Full-Build-Prompt §13, Phase 12 (steps 12.1–12.13), research
note `docs/research/20261008-023-web-research-api.md` and decision D-004 (self-hosted SearXNG). Research is its own
pipeline:

```
question → queries → search → (primary sources first) → fetch (HTTP, optionally browser) → extract → source records
        → claims → claim↔source links → freshness → contradiction check → synthesis (fast | deep) → decision linkage
```

Each step is persisted in PostgreSQL (`research_runs`, `research_sources`, `research_claims`, `research_claim_sources`)
and emitted as an event, so nothing happens with an invisible source ("Keine unsichtbaren Quellen").

Invariants:

- **Fixed model roles.** Query planning, claim extraction and simple syntheses use role `fast` (Qwen3 8B, alias
  `fast-router`). Complex syntheses use role `planner` (Gemma, alias `planner-gemma`). Aliases come from
  `models.yaml` (`ResearchModels.from_config`).
- **Deterministic fallbacks.** No model failure stops a run. Query planning falls back to key-term queries, claim
  extraction to a sentence heuristic, synthesis to a fully cited summary.
- **Citations are enforced.** Every sentence and every key point of a synthesis must cite claims as `[n]`. Unknown
  claim numbers and uncited text are rejected. After one repair round, any text that is still uncited is removed.
  `used_claims` is always recomputed from the citations; the model's own list is never trusted.
- **Generic code.** The package has no project- or benchmark-specific rules. Authority comes only from URL patterns
  and the `primary_domains` policy.
- **Web text is untrusted data.** It is wrapped in a `<source>…</source>` block in prompts, and a page cannot close
  that block early. Model claims must be *grounded*: every number or version they contain must appear in the
  source, and ≥ 60 % of their content terms must too.
- **Secrets never leak.** Questions, source titles, URLs, prompt passages and error messages pass through
  `hermclaw.core.redaction`. `append_event` redacts payloads again. URLs are redacted before they are stored or shown.
- **SSRF protection** covers every network access, including the requests a browser makes while rendering a page
  (see "Fetch").
- No model reasoning is stored. Only structured outputs (queries, claims, synthesis) are persisted.

## Modules

| Module | Content | Steps |
|---|---|---|
| `planner.py` | `QueryPlanner.plan` (fast model, `QueryPlan` schema, ≤ `max_queries`, sanitising), `fallback_queries`, `sanitize_queries` | 12.1 |
| `search.py` | `SearchProvider` protocol, `SearxngSearchProvider`, `StaticSearchProvider`, `DisabledSearchProvider`, `canonical_url`, `dedupe_results`, `merge_results`, `parse_searxng_results` | 12.2 |
| `fetch.py` | `HttpFetcher` (SSRF guard, DNS pinning, redirects, limits, content-type allowlist), `FetchResult`, `Fetcher` protocol, `is_public_address` | 12.3 |
| `browser.py` | `RenderingFetcher` (HTTP first, browser for pages with little static text), `BrowserRenderer` (headless Chromium `--dump-dom`), `GuardProxy`, `find_chromium` | 12.3 |
| `extract.py` | `extract_document` / `extract_html` (BeautifulSoup + lxml main text, title, language), dates from JSON-LD / meta / `<time>`, `content_hash`, `parse_date` | 12.5, 12.8 |
| `sources.py` | `classify_source` (source type + authority), `relevance_score` (BM25-style), `prior_score`, `assess_freshness` | 12.4, 12.8, 12.9 |
| `claims.py` | `ClaimExtractor` (fast model, `ClaimExtraction`, grounding check, heuristic fallback), `merge_claims` (cross-source links, noisy-OR confidence) | 12.6, 12.7 |
| `contradictions.py` | `detect_contradictions` (value / negation / antonym conflicts, union-find groups, preferred claim), `LlmContradictionConfirmer` hook | 12.10 |
| `synth.py` | `Synthesizer` (mode choice, citation validation, repair, strip, fallback chain), `fallback_synthesis` | 12.11, 12.12 |
| `store.py` | persistence helpers, `load_contract` (rebuilds `ResearchContract` from the DB) | 12.4, 12.7 |
| `engine.py` | `ResearchEngine`, `build_research_engine`, `format_for_worker`, `research_callback`, events | all, 12.13 |
| `text.py` | tokens, key terms, values (numbers/versions), negation, sentence split (deterministic, no dependencies) | – |
| `errors.py` | error classes with stable codes (see "Failure behaviour") | – |

## Public interfaces

```python
class ResearchEngine:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], chat: ChatModel, search: SearchProvider,
                 fetcher: Fetcher, policy: ResearchPolicy, *, models: ResearchModels | None = None,
                 settings: ResearchSettings | None = None, confirmer: ContradictionConfirmer | None = None,
                 clock: Callable[[], datetime] | None = None) -> None
    async def run(self, question: str, job_id: UUID | None = None, step_id: UUID | None = None,
                  deep: bool = False, *, decision_ref: str | None = None) -> ResearchContract
    async def run_detailed(self, question: str, *, job_id=None, step_id=None, deep=False,
                           decision_ref=None) -> ResearchOutcome   # run_id, contract, claim_ids, synthesis, planned, contradictions, errors
    async def link_decision(self, run_id: UUID, claim_indices: Sequence[int], decision_ref: str) -> int
    async def answer_for_worker(self, question: str, *, job_id=None, step_id=None) -> str
    async def aclose(self) -> None

def build_research_engine(sessionmaker, chat: ChatModel, config: HermclawConfig | None = None, *,
                          settings: ResearchSettings | None = None, render_js: bool = False,
                          chromium: str | None = None, **fetcher_overrides) -> ResearchEngine
def research_callback(engine, *, job_id=None, step_id=None) -> Callable[[str], Awaitable[str]]  # ToolCallbacks.on_research
def format_for_worker(contract: ResearchContract, *, max_chars: int = 6000) -> str

class SearchProvider(Protocol):
    name: str
    async def search(self, query: str, *, limit: int) -> list[SearchResult]
SearxngSearchProvider(base_url, *, timeout_seconds=15.0, user_agent=..., categories=(), language=None,
                      safesearch=1, time_range=None, engines=(), transport=None)
StaticSearchProvider(results_by_query={...}, default=[...], failures={...})

class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult
HttpFetcher(timeout_seconds=20.0, max_bytes=2_000_000, user_agent=..., max_redirects=5, allow_private=False,
            allowed_networks=(), allowed_content_types=ALLOWED_CONTENT_TYPES, resolver=None, pin_dns=True,
            verify=True, total_timeout_seconds=None, transport=None)
    .from_policy(policy, **overrides);  async check_url(url) -> (scheme, host, port, pinned_ip)
RenderingFetcher(http: HttpFetcher, renderer: BrowserRenderer, min_text_chars=400)
BrowserRenderer(guard: HttpFetcher, executable=None, virtual_time_budget_ms=5000, timeout_seconds=30.0,
                max_output_bytes=2_000_000, max_requests=150, max_proxy_bytes=20_000_000, concurrency=2,
                extra_args=())
    async render(url) -> RenderResult(html, truncated, elapsed_ms, stats)
```

`ResearchContract`, `SourceRecord`, `Claim`, `QueryPlan`, `ClaimExtraction` and `ResearchSynthesis` live in
`hermclaw/contracts/research.py`. In `ResearchContract.contradictions`, claim indices are 0-based. In UI texts and
synthesis citations, claims are numbered from 1.

## Data and event flow

All events use `source_type="research"` and `source_id=<research_run_id>`, and carry `job_id`/`step_id` when known.
Every payload has `research_run_id` and a German UI text in `text`:

| Event | When | UI text / key payload |
|---|---|---|
| `research.started` | run row created | "Research startet: …"; `question`, `deep`, `provider` |
| `research.query.started` | before each query | "Research sucht: <query>"; `index`, `total` |
| `research.source.read` | per candidate (read, failed **and** skipped) | "Research liest: <title> (<domain>)" / "Research konnte <domain> nicht lesen (<code>)" / "Research überspringt …"; `status`, `source_type`, `authority_score`, `relevance_score`, `published_at`, `freshness`, `age_days`, `content_hash`, `queries`, `bytes`, `truncated`, `rendered`, `error` |
| `research.claim.created` | per claim | "Quelle verwendet für: <claim>"; `claim_index`, `confidence`, `method`, `contradiction_group`, `sources[]` |
| `research.decision.linked` | claims used by the synthesis (and every later `link_decision`) | "Quellen verwendet für: <decision_ref>"; `claims`, `sources` |
| `research.finished` | end (also on abort, severity `error`) | "Research abgeschlossen / teilweise abgeschlossen / fehlgeschlagen: …"; `status`, `queries` (per-query results/errors), `sources_used[]` (with `used_for_claims` / `used_in_synthesis`), `contradictions[]`, `synthesis_mode`, `synthesis`, `errors` |

`research.decision.linked` is a string constant (`RESEARCH_DECISION_LINKED`). Adding it to `EventType` is requested
as a shared change.

Pipeline details:

1. **Queries (12.1).** The fast model returns a `QueryPlan`. Queries are cleaned: numbering is removed, duplicates
   are dropped, each query is at most 200 characters and must share a key term with the question. At most
   `max_queries` (≤ 6) are kept. If none remain, the fallback is the question itself, its key terms, and
   "<names> <versions> documentation", plus a "release notes" or "example" variant.
2. **Search (12.2).** The engine sends `GET {searxng_url}/search?q=…&format=json&pageno=1&safesearch=1` with
   optional categories, language, time range and engines. URLs are canonicalised: fragment, tracking parameters
   (`utm_*`, `fbclid` …) and default ports are removed, and URLs with credentials are dropped. Duplicates are
   merged by URL with `www.` and trailing `/` ignored. Each entry keeps its best rank and the list of queries that
   found it. Candidates are ordered by `prior_score`: 60 % authority, 30 % search rank, 10 % multi-query hits. So
   primary sources come first (§23.4). Each domain gets at most `max_sources_per_domain` (3) candidates, and at
   most `max_sources` are kept overall.
3. **Fetch (12.3).** Up to `fetch_concurrency` sources are fetched at once with `HttpFetcher`, optionally wrapped in
   `RenderingFetcher`.
4. **Extract (12.5 / 12.8).** Boilerplate is dropped: script, style, nav, footer, aside, form, hidden elements, ARIA
   landmark roles, and the page-level `<header>`. The text comes from `<main>` / `[role=main]`, else from the
   top-level `<article>`s, else from `<body>`. The title comes from `og:title`, then `<title>`, then `<h1>`. Dates
   come from JSON-LD (`datePublished` / `dateModified`), then meta tags, then `<time>`. The content hash is the
   SHA-256 of the extracted text. A source is *skipped*, not failed, when its text is shorter than
   `min_text_chars` or when its content hash duplicates another source in the run.
5. **Source records (12.4 / 12.9).** `classify_source` decides the type and authority:
   - `primary_domains` (with an optional path prefix such as `github.com/org/repo`, and `www.` ignored): 0.9–0.95;
   - standards bodies: 0.9;
   - documentation hosts or paths (`docs.*`, `*.readthedocs.io`, `/docs/`): 0.75–0.85;
   - repositories (`github.com/<org>/<repo>`; issue and discussion threads count as forum): 0.75;
   - registries: 0.7;
   - the vendor's own site (same registrable domain as a primary domain): 0.7;
   - secondary sites and blogs: 0.4–0.5;
   - forum and Q&A sites: 0.3.

   Relevance is 55 % weighted term coverage, 25 % BM25-saturated term frequency and 20 % title hits.
6. **Claims (12.6 / 12.7).** For each source, up to `max_claim_input_chars` of the most question-relevant
   paragraphs go to the fast model (`ClaimExtraction`). Ungrounded claims are dropped. If the model fails or
   produces no grounded claim, the sentence heuristic takes over: informative sentences sharing key terms with the
   question, with numbers and versions preferred. An empty model answer means "irrelevant source" and is accepted.
   Identical claims from several sources are merged into one claim linked to all of them in
   `research_claim_sources`. Confidence is authority × relevance × freshness factor (× 0.85 for heuristic claims),
   combined across sources with noisy-OR and capped at 0.99. At most `max_claims_total` claims are kept, in a
   stable order.
7. **Freshness (12.8).** The most recent of these dates counts: published or modified date, `Last-Modified`, or the
   search engine's `publishedDate`.

   | Age | Status | Confidence factor |
   |---|---|---|
   | up to 365 days | `fresh` | 1.0 |
   | up to 1095 days | `aging` | 0.93 |
   | older | `stale` | 0.75–0.85 |
   | no date | `unknown` | 0.95 |

   When claims contradict each other, the newer source wins ties.
8. **Contradictions (12.10).** Two claims are compared when they share at least 2 subject terms with an overlap
   coefficient ≥ 0.6. They conflict when they state different numbers or versions, have opposite polarity, or use
   an antonym pair (enabled/disabled, required/optional, …). Claims from the same single source are never compared.
   Conflicting pairs are joined with union-find; in each group the claim with the highest confidence, then the
   newest source, is *preferred*. The other claims in the group lose 20 % confidence. Groups are persisted in
   `research_claims.contradiction_group` and `research_runs.contradictions`. An optional `ContradictionConfirmer`
   (`LlmContradictionConfirmer`, `ResearchSettings.confirm_contradictions`) can veto pairs; if the confirmer
   fails, the deterministic verdict stands.
9. **Synthesis (12.11 / 12.12).** The mode is `deep` (role `planner`, Gemma) when `deep=True`, when there is any
   contradiction, when there are ≥ 6 sources, or when there are ≥ 16 claims. Otherwise it is `fast` (role `fast`).
   The fallback chain is deep → fast → deterministic `fallback_synthesis`, which is fully cited and turns
   disagreements into open questions. The rendered Markdown is stored in `research_runs.synthesis`.
10. **Decision linkage.** Claims cited by the synthesis get `used_for_decision = true` and
    `decision_ref = <decision_ref or "research_run:<id>">`. Use `link_decision` to link claims to a plan or step
    decision later.

The run status is `completed`; `partial` when a source failed, a query failed or the synthesis used the
deterministic fallback; and `failed` when no source or claim was found or the run was aborted.

## Fetch and SSRF protection

`HttpFetcher` only fetches `http` and `https` URLs without credentials. Every hop, including each redirect up to
`max_redirects`, is resolved, and **all** resolved addresses must be globally routable. IPv4-mapped, NAT64
`64:ff9b::/96` and 6to4 addresses are unwrapped first. Local-use NAT64 `64:ff9b:1::/48`, Teredo, multicast and
reserved addresses are never public. Private addresses are allowed only when they fall in `allowed_networks` or
when `allow_private` is set; both are for tests and trusted mirrors only.

The connection goes to the validated IP, with the `Host` header and TLS SNI set to the original host name (DNS
pinning against rebinding). Keep-alive is disabled while pinning. The fetcher also enforces:

- a content-type allowlist: `text/html`, `text/plain`, `application/json`, `application/xhtml+xml`;
- a `Content-Length` pre-check and a streamed body capped at `max_fetch_bytes` (result marked `truncated`);
- per-phase timeouts and a total time budget;
- the policy user agent, and no inherited proxy or env configuration.

The browser fetch (`RenderingFetcher` + `BrowserRenderer`) runs only for HTML pages whose static text is shorter
than `min_text_chars` (default 400), i.e. pages rendered by JavaScript. Chromium (`--dump-dom`,
`--virtual-time-budget`) runs with a throw-away profile, a minimal environment, its own process session (killed on
timeout) and images disabled. All its traffic goes through `GuardProxy`, a loopback proxy that calls
`HttpFetcher.check_url` for **every** request, including subresources, fetch/XHR, WebSocket `CONNECT` and IP
literals:

- `--proxy-bypass-list=<-loopback>` removes Chromium's implicit loopback bypass.
- `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1` makes any direct DNS lookup fail.
- WebRTC is forced onto proxied TCP.
- Only `GET`, `HEAD` and `CONNECT` are forwarded, so a page script cannot POST anywhere.
- Requests and bytes per render are capped.

A render failure is not an error: `RenderingFetcher` keeps the HTTP result. When running as root (containers),
`--no-sandbox` is added; the proxy guard still applies.

## Config keys used

- `policies.research.search_provider` (`searxng` | `none`), `searxng_url`, `max_queries`, `max_sources`,
  `fetch_timeout_seconds` (fetch and SearXNG timeout; render timeout = 2× that, at least 20 s), `max_fetch_bytes`,
  `primary_domains`, `user_agent`.
- `models.profiles[role=fast]` and `models.profiles[role=planner]`: `alias`, `max_output_tokens`, `temperature`,
  `timeout_seconds`.
- `HERMCLAW_CHROMIUM`: optional path to the Chromium or headless-shell executable. Without it, the Playwright
  caches (`/opt/pw-browsers`, `~/.cache/ms-playwright`) and `$PATH` are searched.
- Tuning without a config key lives in `ResearchSettings`: results per query, per-domain cap, concurrency, claim
  limits, text and excerpt sizes, freshness windows, synthesis thresholds, LLM confirmation.

## Failure behaviour

| Code | Where | Effect |
|---|---|---|
| `SEARCH_JSON_DISABLED` | SearXNG answers 403 or HTML instead of JSON | Logged for the first query only; no further queries (configuration error). Fix: add `json` to `search.formats` in SearXNG `settings.yml`. |
| `SEARCH_UNAVAILABLE` / `SEARCH_FAILED` | timeout, unreachable, 429, other HTTP or JSON errors | The query is recorded as failed and the next query runs; run status `partial`, or `failed` without sources. |
| `SEARCH_DISABLED` | `search_provider: none` | The run fails fast. |
| `FETCH_BLOCKED` | SSRF guard: scheme, credentials, private or rebinding address, redirect into a private network | The source is stored with `status=failed`; the run continues. |
| `FETCH_TIMEOUT` / `FETCH_TOO_LARGE` / `FETCH_UNSUPPORTED_CONTENT` / `FETCH_HTTP_ERROR` / `FETCH_TOO_MANY_REDIRECTS` / `FETCH_FAILED` | fetch | The source is stored with `status=failed`; the run continues. An unexpected exception from a custom fetcher is isolated the same way. |
| model errors / invalid output | planner, claims, synthesis | Deterministic fallback; the stage is recorded in `errors` of `research.finished`. |
| `SYNTHESIS_UNCITED` | – | Reserved code; uncited syntheses are repaired, stripped or replaced, never persisted. |
| cancellation / DB error | anywhere | The run is marked `failed` and `research.finished` (severity `error`) is written under `asyncio.shield`; the exception is re-raised. |

## Operating and testing

Tests use a real PostgreSQL database, real local HTTP servers and a real headless Chromium. The only fake is the
scripted `ChatModel` in test code (`tests/integration/test_research_support.py`).

- `tests/unit/test_research_*.py`: planner, synthesis, claims, contradictions, sources, extraction and text.
- `tests/integration/test_research_fetch_search.py`: SSRF guard, DNS pinning, redirects, limits, charset, the fake
  SearXNG server, `SEARCH_JSON_DISABLED`.
- `tests/integration/test_research_engine.py`: the end-to-end pipeline with an official docs page, a forum page
  with a conflicting version and a JSON-LD-dated blog. Also covers fallbacks, partial failures, SearXNG
  end-to-end, decision linkage, the worker callback, concurrency, cancellation, secret redaction and wiring.
- `tests/integration/test_research_browser.py`: JavaScript rendering; blocked private targets, IP literals,
  WebSocket and POST; HTTPS `CONNECT`; timeout kill; request budget; the engine with a rendered source.
- `tests/failure/test_research_failures.py`: hostile HTML, prompt-injection fence, NAT64 local-use, `www.` primary
  domains, misbehaving collaborators, uncited or unknown citations, oversized pages.

Run them with `.venv/bin/pytest -q tests/unit/test_research_*.py tests/integration/test_research_*.py tests/failure/test_research_*.py`.

On the target hosts (blocked from the build environment, BLOCKER-001):

1. On `.225`, run the SearXNG container (D-004, internal only), with `search.formats: [html, json]` in
   `settings.yml` and the limiter set to allow the orchestrator. Set `policies.research.searxng_url`.
2. Make sure `.225` has outbound HTTPS to the internet. The SSRF guard blocks LAN targets on purpose. If an
   internal documentation mirror is needed, pass it explicitly as `allowed_networks` in `build_research_engine(...,
   allowed_networks=[...])`.
3. The model gateway (LiteLLM) needs enabled profiles for roles `fast` and `planner`.
4. Optional browser fetch: install Chromium or the Playwright headless shell on `.225`, set `HERMCLAW_CHROMIUM` if
   needed, and build with `render_js=True`.
5. Run the live checks: `HERMCLAW_LIVE_SEARXNG_URL=http://192.168.178.225:8888 .venv/bin/pytest -m live -q
   tests/integration/test_research_fetch_search.py tests/integration/test_research_engine.py`. They cover the
   SearXNG JSON API and a full research run through LiteLLM with real web sources.
6. Watch the UI or event stream: each run shows "Research sucht …", "Research liest …" and "Quelle verwendet für …",
   and `research.finished` lists every source (including failed and skipped ones) with how it was used.

## Shared changes requested

- Add `RESEARCH_DECISION_LINKED = "research.decision.linked"` to `hermclaw.contracts.events.EventType`. The engine
  currently uses the string constant.
- Optional policy keys `policies.research.browser_render: bool` and `browser_executable: str | None`, so that
  `build_research_engine(render_js=..., chromium=...)` can be driven from `policies.yaml`.
