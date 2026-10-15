# Web UI (P31)

Bauplan §33 (WEB UI), master prompt §25 (WEB UI: Dashboard, Job Detail, DAG, Live Events, Diff, Tests, Verifier,
Review, Sources, Workers, Resources, Bugs, Artifacts; no CoT), Phase 31 (steps 31.1–31.15).
Research: R-018 React/Vite (`docs/research/20261008-018-react-vite.md`), R-019 Playwright
(`20261008-019-playwright.md`), R-014 Nginx + SSE (`20261008-014-nginx-sse.md`).
Backend contract: `docs/contracts/openapi.json`, `hermclaw/api/{jobs,events,system,auth}.py`, `hermclaw/workers/api.py`.

## 1. Purpose

The operator console for Hermclaw Next, the **Kontrollraum** of the autonomous AI worker. It is served by nginx on
the web UI host `.223`, on its own ports (`http://192.168.178.223:8080/`, with TLS `:8443`). On the same host the legacy
portal Noob2Claw/Kowacore keeps Apache on port 80 (D-013) and links here from its menu entry **Arbeit →
KI-Kontrollraum** (a top-level link in a new tab: the UI forbids framing with `frame-ancestors 'none'`, and there
is no single sign-on, the UI asks for its own bearer token). It **only reads and controls** state through the public
API. It never plans, never edits scope, and never mutates git. It shows:

* **Overview.** Jobs, queue, workers, model profiles, GPU leases, research, bugs, and errors on one dashboard.
* **Job detail.** Plan DAG, steps (attempts, scope versions, tool calls, commands, tests, verifier checks, review
  findings), live events with human-readable status lines, the unified diff, research sources with claims and
  contradictions, resource leases, artifacts and media, git events, and errors.
* **Controls.** Cancel, pause, resume, retry, and replan, each with a confirmation and per-status enablement. A worker
  drain toggle sits on the Workers page.

Model reasoning or chain-of-thought is **never displayed**. The API does not carry any, and the UI only renders
`status` lines (`payload.text`), structured evidence, and event payload summaries.

Stack (fixed): React 19, TypeScript strict (`noUncheckedIndexedAccess`, `erasableSyntaxOnly`, …), Vite 8, and
ESLint 10 with `typescript-eslint`, `react-hooks`, and `jsx-a11y`. There is no UI framework and no router library:
the router is about 70 lines on the History API, and the DAG is our own SVG layout. The build is fully self-contained
(no CDN, no web fonts) and runs under a strict CSP (see the ops doc).

## 2. Code map

| Path | Role |
|---|---|
| `ui/src/main.tsx`, `App.tsx` | bootstrap, token gate (login vs. layout), route table |
| `ui/src/api/client.ts` | typed `fetch` client (`api.*`), `ApiError`, `fetchBlob` (authenticated downloads), `describeError` (German messages) |
| `ui/src/api/types.ts` | TypeScript mirror of the API response models (jobs, plan, steps, evidence, research, workers, resources, bugs) |
| `ui/src/lib/config.ts` | API base resolution: `<meta name="hermclaw-api-base">` → `VITE_HERMCLAW_API_BASE` → `/api` |
| `ui/src/lib/auth.ts` | bearer token in `sessionStorage` (`hermclaw.apiToken`), `UNAUTHORIZED_EVENT` on any 401 |
| `ui/src/lib/sse.ts` | `ReconnectingEventStream` (backoff, cursor, de-dup, stale-stream hooks), `buildStreamUrl`, `undeliveredSequence` |
| `ui/src/lib/useEvents.ts` | `useJobEvents` (REST history, SSE live, REST reconciliation, watchdog), `useGlobalEvents` (dashboard) |
| `ui/src/lib/events.ts` | event normalisation, merge/sort, categories, status-line extraction, payload summaries, `KNOWN_EVENT_TYPES` |
| `ui/src/lib/dag.ts` | layered DAG layout (`layoutDag`, `assignLayers`, `edgePath`) |
| `ui/src/lib/diff.ts` | unified diff parser (`parseUnifiedDiff`, `diffStats`) |
| `ui/src/lib/plan.ts`, `aggregate.ts` | plan versions → DAG nodes; aggregation of tests/verifier/review over steps; research views; GPU usage; `safeExternalUrl` |
| `ui/src/lib/controls.ts` | `controlStates(job)` (which control is enabled per status, with reason), confirmation texts |
| `ui/src/lib/jobForm.ts` | create-job form validation → `JobCreateBody` (limits, branch-name check, constraints parsing) |
| `ui/src/lib/useApi.ts` | `useApi(key, fetcher, {pollMs, version})` (abortable, keeps data on refresh), `useNow`, `useThrottled` |
| `ui/src/components/*` | `Layout` (nav, skip link, focus management), `Tabs` (ARIA tabs, arrow/Home/End), `DagView`, `DiffViewer`, `EventList`, `StepDetailView`, `Evidence`, `JobControls`, `ConfirmDialog` (native `<dialog>`), `Artifacts`/`MediaGallery`, `JobCreateForm`, `ui.tsx` primitives |
| `ui/src/pages/*` | `Dashboard`, `Jobs`, `JobDetail` (+ `job/*` tabs), `Workers`, `Resources`, `Media`, `Bugs`, `Login`, `NotFound` |
| `ui/tests/unit/*.test.ts` | vitest: diff parser, DAG layout, SSE client (reconnect/backoff/de-dup/watchdog), helpers (events, controls, config, routes, URL sanitising) |
| `ui/tests/e2e/*` | Playwright against the real backend: `global-setup.ts`, `seed.py`, `net-chaos.ts`, `fixtures.ts`, `*.spec.ts` |

## 3. Pages and step mapping

| Step | UI | API used |
|---|---|---|
| 31.1 layout | top bar (version · env, logout), side nav with `aria-current`, skip link, focus moves to `<main>` on navigation, mobile menu toggle (≤ 820 px), login screen | `GET /api/version`, `GET /api/stats` (token check) |
| 31.2 dashboard | jobs by status (links to filtered list), queue (queued jobs by priority), worker state badges `STARTING/WAKING/READY/BUSY/DRAINING/ERROR/SLEEPING/OFFLINE`, model profiles + invocation stats, GPU (active leases, waiting requests, capacity meter), recent errors and live status ticker (global SSE), research counters, open bugs | `/api/stats`, `/api/jobs`, `/api/workers`, `/api/models`, `/api/resources`, `/api/bugs`, `/api/events/stream` |
| 31.3 jobs | list with status filter and pagination; create form (prompt, title, repository name/url/base branch, constraints one per line, priority, research/commit toggles) with client validation and server error display | `GET/POST /api/jobs`, `GET /api/repositories` |
| 31.4 DAG | Plan tab: SVG DAG of the selected plan version (status colour + stripe + text, so colour is not the only cue), edges between dependencies, legend, plan-version selector with source/reason/raw JSON; nodes are keyboard-focusable and open the step | `GET /api/jobs/{id}/plan`, `/steps` |
| 31.5 live events | Steps tab (step detail: attempts, scope versions, tool calls, commands, tests, verifier checks, review findings); Live Events tab: newest-first list, type/category/text filters, prominent "now" status line + status history, stream-state badge | `GET /api/steps/{id}`, `GET /api/jobs/{id}/events`, `/events/stream` |
| 31.6 diff | Changes tab: unified diff viewer (file index, file headers with status badge and +/− counts, hunk headers, old/new line numbers, +/− colouring with sign column, binary/rename handling, huge files start collapsed) | `GET /api/jobs/{id}/diff` |
| 31.7–31.9 | Tests / Verifier / Review tabs aggregate the step evidence over all steps (totals, failed first, links to the step) | `GET /api/steps/{id}` for each step |
| 31.10 sources | Research tab: runs, sources sorted by authority/relevance (meters), every source rendered and linked (`http(s)` only, `rel="noopener noreferrer nofollow"`), claims with numbered citations to their sources, contradiction groups | `GET /api/jobs/{id}/research` |
| 31.11 workers | Workers page: registry table (state badge + hint, address, version/protocol, heartbeat age, active job, capabilities); detail (admin): health sample, GPUs (VRAM meter, utilisation, temperature), loaded models, drain toggle with confirmation | `GET /api/workers`, `GET /api/workers/{id}`, `POST /api/workers/{id}/drain` |
| 31.12 resources | Resources page: capacity/used meter per model host, active leases, waiting requests, model profiles; job Resources tab filters to the job | `GET /api/resources`, `GET /api/models` |
| 31.13 media | Media page + job Media tab: image/video previews through authenticated blob URLs, download, media events | `GET /api/jobs/{id}/artifacts`, `GET /api/artifacts/{id}/download`, events `media.*` |
| 31.14 errors | Errors tab: job error (code/message), error/warning/critical events, `*.failed`, scope violations, stagnation; count shown on the tab | job + events |
| 31.15 controls | cancel/pause/resume/retry/replan buttons in the job header; enabled per status (`controlStates`) with the reason as tooltip and screen-reader text; confirmation dialog (reason for cancel/replan); 409 conflicts shown in the dialog and the job reloaded | `POST /api/jobs/{id}/{action}?reason=` |

Additional tabs from Bauplan §33: Overview, Repository (repo/branch/commit metadata, repo events), Artifacts
(downloads), Git (git events), Deployment (deployment events/artifacts).

## 4. Data and event flow

```
REST (fetch, Bearer header) ─┐
                             ├─> useApi / useJobEvents ─> React state ─> pages/tabs
SSE  (EventSource, ?access_token=, Last-Event-ID) ─┘
```

* **Auth.** The login screen checks the token against `GET /api/stats` and stores it in `sessionStorage`. Each request
  sends `Authorization: Bearer …`. A 401 clears the token and returns to login with a notice. `EventSource` cannot set
  headers, so the stream endpoints get `?access_token=` (the only place the token appears in a URL; see the ops doc
  for access-log masking). Artifact files are fetched with the header and handed over as `blob:` URLs, so no token
  ever appears in `<img src>` or download links.
* **Job events (`useJobEvents`).**
  1. Load the full REST history (`after=` paging, 5000 per page).
  2. Open the SSE stream from that cursor. The server sends `id=sequence`, `event=event_type`, and `data=EventEnvelope`.
  3. Every 15 s (only while the tab is visible), and after every reconnect, run a REST reconciliation that re-reads
     the last 500 sequences. This catches events whose transaction committed after a higher sequence, and event types
     the `EventSource` has no listener for (it has no wildcard). Events are merged and de-duplicated by `sequence`.
* **Reconnect (`ReconnectingEventStream`).**
  * While `readyState` is CONNECTING, the browser's own retry runs and sends the `Last-Event-ID` header.
  * When the stream is closed for good (an HTTP error from the proxy, for example), the client reconnects itself.
    The backoff is 1 s, 2 s, 4 s, and so on, capped at 30 s, with `?last_event_id=` set to the cursor.
  * The server prefers the header, then the query parameter, and replays everything after the cursor.
  * The UI shows the state as "Verbinde …", "Live verbunden", or "Verbindung unterbrochen – verbinde neu".
* **Half-open watchdog.** A proxy can keep the browser side of a stream open after its upstream died (observed with
  the Node http-proxy used by `vite preview`). SSE heartbeats are comments and are invisible to `EventSource`.
  1. `seen` tracks everything the stream covered: messages received, duplicates included, plus the cursor of the
     current connection.
  2. If a reconciliation returns a listened-for event with `sequence > seen`, a 5 s grace timer starts.
  3. If the stream still has not delivered that event when the timer fires, `reconnectNow()` replaces the connection.
  4. No clocks are compared, so client/server clock skew cannot cause false alarms.
* **Derived refreshes.** Job and step data refetch when relevant events arrive (`jobDataVersion`, `stepDataVersion`,
  throttled to 1 s), and also poll slowly (15 s), so the header status, DAG colours, and evidence tabs follow the
  runtime without a reload.
* **Dashboard.** `useGlobalEvents` (`/api/events/stream`, starts "now") keeps the newest 300 events for the live
  ticker and recent errors. Counters come from polled REST endpoints.

## 5. Configuration

| Key | Where | Default | Meaning |
|---|---|---|---|
| `<meta name="hermclaw-api-base" content="">` | `dist/index.html` (editable after deployment) | empty → `/api` | API base URL (absolute or path) |
| `VITE_HERMCLAW_API_BASE` | build env | – | build-time API base (meta wins) |
| `HERMCLAW_UI_BASE` | build env | `/` | sub-path deployment (`vite build --base`) |
| `HERMCLAW_UI_API_TARGET` | dev/preview env | `http://127.0.0.1:8000` | proxy target for `/api` in `vite` / `vite preview` |
| `HERMCLAW_UI_PORT` | preview env | `4173` | preview port |
| Server side | `config/policies*.yaml` → `policies.events.heartbeat_seconds` | – | SSE heartbeat comments (keeps proxies from timing out) |
| Ports on `.223` | inventory `hermclaw_webui_http_port` / `hermclaw_webui_https_port` | `8080` / `8443` | where nginx serves the UI; the portal button (`hermclaw_ui_portal_link_url`) and the health checks follow them |

E2E harness variables: `HERMCLAW_E2E_API_PORT` (18765), `HERMCLAW_E2E_UI_PORT` (14173, browser-facing chaos
proxy), `HERMCLAW_E2E_VITE_PORT` (UI port + 1), `HERMCLAW_E2E_CHAOS_PORT` (18766, +1/+2 control),
`HERMCLAW_E2E_PYTHON`, `HERMCLAW_TEST_ADMIN_URL`, `HERMCLAW_UI_SKIP_BUILD=1`, `HERMCLAW_E2E_KEEP_DB=1`,
`HERMCLAW_E2E_CHROMIUM` (explicit browser binary).

## 6. Failure behaviour

* **API unreachable.** Each block shows a German error box with an "Erneut laden" button. Data already loaded stays
  visible during refreshes and poll failures.
* **401.** The token is cleared and the user returns to login ("Sitzung abgelaufen oder Token ungültig"). **403**
  (for example, worker detail without the admin scope) shows a hint instead of an error.
* **409 on a control.** The message stays in the dialog, the job is reloaded, and the confirm button disables if the
  action is no longer allowed.
* **Validation errors.** FastAPI `detail[]` and Hermclaw `{error:{code,message}}` are both mapped to readable
  messages, and the create form shows server errors next to the form.
* **SSE.** The stream recovers from network loss, HTTP errors, and half-open connections (watchdog). Missed events
  are replayed by cursor or reconciled over REST. Events are applied at most once.
* **Malformed data.**
  * Diff: hunks are consumed by their line counts, and truncated hunks fall back gracefully. Files with more than
    3000 lines (or diffs with more than 30 files) start collapsed. `GET /api/jobs/{id}/diff` cuts the artifact at
    2 000 000 characters without a flag; `isDiffTruncated` (`lib/diff.ts`) detects that (code points, like Python)
    or honours a future `truncated` field and the Changes tab shows a "gekürzt" notice pointing to the download.
  * DAG: cycles go into an extra layer and are reported; unknown dependencies are reported and dropped.
  * Events: unknown event types are shown generically; events that fail normalisation are dropped.
* **Untrusted content.** Research URLs are linked only if they are `http(s)`. All text goes through React escaping
  (no `dangerouslySetInnerHTML`). The app runs under a strict CSP (`script-src 'self'`, no inline script).

Known limitation: the global dashboard stream has no REST counterpart, so the watchdog cannot cover it. Its counters
poll anyway. A server-side named heartbeat event (for example `event: heartbeat`) would let the client detect a silent
stream directly (suggested as a shared change).

## 7. Tests

* **Unit (vitest, `npm run test:unit`).** Covers the diff parser (renames, binary, no-newline, `---` content lines,
  truncation), the DAG layout (layers, ordering, cycles, missing dependencies), and the SSE client (URL, backoff,
  de-dup, reconnect cursor, `seen`/`reconnectNow`/`undeliveredSequence`) and diff truncation detection
  (`isDiffTruncated`, code point counting). Helper tests cover event merge/summary,
  status lines, control states, API base, routes, formatting, and URL sanitising.
* **E2E (Playwright, `npm test`).** Runs against the real FastAPI app, a fresh PostgreSQL 16 database (migrations
  applied), and the built UI served by `vite preview`. `seed.py` creates realistic data:
  * one job through `POST /api/jobs`;
  * plan versions, steps with dependencies, attempts, scope versions, tool calls, commands, test runs, verification
    checks, review findings, a research run with sources, claims and contradictions, artifacts including a diff file
    and a PNG under `HERMCLAW_DATA_DIR/artifacts`, workers with health and GPUs, leases, waiting requests, bugs, model
    invocations, and events including status lines.

  Live events are produced from a separate process (`seed.py emit`), so they travel the real path: commit, NOTIFY,
  SSE. Real network failures are injected with `net-chaos.ts`.
* **Projects.** `chromium` (Desktop Chrome) runs every spec except the responsive one. `mobile` (Pixel 7) runs
  `responsive.spec.ts` (menu toggle, no horizontal page scroll).
