# Hermclaw Next – architecture overview (P41.3)

This page connects the component documents. The binding target architecture is
`docs/Hermclaw_Next_Vollstaendiger_Bauplan_v2.md`; deviations are recorded in `DECISIONS.md`. The host layout is
drawn in [system-diagram.md](system-diagram.md).

## 1. Processes and hosts

| Host | Process (systemd unit) | Code | Purpose |
|---|---|---|---|
| `.225` orchestrator | `hermclaw-api` | `hermclaw/api/` (`hermclaw api`) | REST + SSE for the UI, worker heartbeats (`/api/workers/heartbeat`, HMAC-signed) |
| `.225` | `hermclaw-scheduler` | `hermclaw/runtime/main.py` (`hermclaw scheduler`) | the runtime: scheduler, planner, coder loop, verifier, review, git, research, resources |
| `.225` | `hermclaw-litellm` | separate venv, `litellm[proxy]==1.104.2` | OpenAI-compatible gateway to Ollama; one alias per model role |
| `.225` | `postgresql` | PostgreSQL 17 + pgvector | single source of runtime state (jobs, steps, events, leases, research, index) |
| `.225` | `hermclaw-searxng` (podman) | SearXNG | web search for the research engine (JSON API, local) |
| `.222` execution worker | `hermclaw-exec-worker` | `worker/execution/` | rootless Podman sandbox: commands/tests on a synced workspace copy (no `.git`) |
| `.224` model/media worker | `hermclaw-model-worker`, `ollama` | `worker/model/`, `worker/media/` | model load/unload/GPU telemetry, ffmpeg/ComfyUI media jobs (GTX 1080 8 GB) |
| `.223` WebUI | `nginx` | `ui/` (React/Vite build) | static UI, reverse proxy `/api/*` to `.225:8000` (SSE unbuffered) |
| `.226` GitLab | – | – | remote repositories, merge requests (runtime pushes job branches only) |
| `.60` Terra/Unraid | – | – | backup target (`hermclaw-backup.timer` on `.225`) |

## 2. Job lifecycle

```text
POST /api/jobs ─► queued
  scheduler: admission (disk/space gate) ─► prepare (RuntimeJobDriver)
     workspace (GitEngine: mirror → isolated clone on job branch hermclaw/<id8>-<slug>)
     triage (fast router qwen3:8b) ─► inventory + context (RepoIntelligence) ─► plan (Gemma gemma4:26b,
     validated + repaired, research steps enriched) ─► running
  steps (DAG, SKIP LOCKED leases, retries, timeouts, mutating steps serialised per job):
     implement/documentation  ImplementStepHandler: scope (ScopeEngine) → CoderLoop (Qwen3-Coder 30B,
                              one JSON tool action per turn, ToolEngine) → scope audit → deterministic
                              verifier → heavy review (Qwen3.8 27B, major/blocker ⇒ no pass) → stage_allowed
                              → commit_verified (only verified content) | correction | replan
     test/verify              VerifyStepHandler (acceptance on the committed state)
     review                   ReviewStepHandler (whole job diff vs. job base)
     research                 ResearchStepHandler (SearXNG → fetch → claims → synthesis, cited)
     inventory/discover       RepoInfoStepHandler
     image/video              MediaStepHandler (GPU lease, AI preemption, .224 media job, verified artifacts)
     ssh/database/docker/deploy  ImplementStepHandler with the operations tools of P26–P28 (capability-gated:
                              SSH on allow-listed hosts with approvals, DB sandboxes, compose sandbox, nginx -t);
                              their outputs are ops_output artifacts (acceptance evidence)
  blocked/failed steps ─► Replanner (Gemma) with the failure package (max replans per job)
  all steps completed ─► finalize: base update (rebase) → regression rerun → push → merge request → final report
  ─► succeeded | failed | cancelled
```

Every transition is an event (`hermclaw/events/store.py`, redacted, ordered by a global sequence); the UI follows
them live via SSE with replay after reconnect (`Last-Event-ID`).

## 3. Model roles (fixed, `config/models.yaml`)

| Role | Alias | Ollama tag | Context | Used by |
|---|---|---|---|---|
| fast router | `fast-router` | `qwen3:8b` | 16K | triage, research queries/claims, media specs |
| planner | `planner-gemma` | `gemma4:26b` | 32K | plan, replan, deep research synthesis |
| planner fallback | `planner-gemma-fallback` | `gemma4:12b` | 32K | only on technical failure of 26B (event `planner.fallback.used`) |
| coder | `coder-main` | `qwen3-coder:30b` | 32K | coder loop (implementation worker) |
| heavy reviewer | `heavy-review` | `qwen3.8:27b` | 24K | heavy review, stagnation escalation |
| embedding | `embedding` | `embeddinggemma:300m` (EmbeddingGemma 2 once Ollama runs it on Linux/CUDA, D-010) | 2K, 768 dim | repository intelligence vectors |

Only one *large* model is resident at a time (`large-model-224` budget, ResourceManager); video/image jobs preempt
AI leases (the coder checkpoints at its next turn).

## 4. Safety boundaries

* **Scope is explicit**: the worker never writes outside the versioned scope contract; expansions are decided by the
  runtime (`hermclaw/scope/`), staging re-checks the index (`hermclaw/gitops/scope_guard.py`).
* **Git mutations are runtime-controlled**: only `GitEngine` commits (bound to a passed verification run of the
  same step and content) and pushes the job branch; protected branches are refused.
* **No unisolated execution in production**: commands run in the rootless sandbox on `.222` (network off unless
  the capability allows it), `.git` never leaves `.225`.
* **Secrets**: references only (`cred:`/`file:`), systemd credentials in production, redaction of every event,
  prompt, tool output and log (`hermclaw/core/redaction.py`); verifier secret scan on every change.
* **Runtime state in `jobs.metadata` is not client-settable**: `POST /api/jobs` refuses metadata keys that the runtime
  writes and trusts (SSH approvals, triage, replan/retry requests, planner provenance, the typed per-job flags;
  registry `hermclaw/core/job_metadata.py`, [api.md](api.md) §2).
* **No chain-of-thought** is stored or shown: models run with `think:false`; only results and short status lines
  are persisted.

## 5. Component documents

| Area | Document |
|---|---|
| job API, reserved job metadata keys | [api.md](api.md) |
| runtime composition, driver, reports | [runtime.md](runtime.md) |
| scheduler, recovery | [scheduler.md](scheduler.md) |
| planner/replanner | [planner.md](planner.md) |
| scope engine | [scope_engine.md](scope_engine.md) |
| context builder | [context_builder.md](context_builder.md) |
| coder loop, implement handler | [coder.md](coder.md) |
| tool engine, permissions | [tools.md](tools.md) |
| execution sandbox | [sandbox.md](sandbox.md) |
| deterministic verifier | [verifier.md](verifier.md) |
| heavy review | [review.md](review.md) |
| stagnation detection | [stagnation.md](stagnation.md) |
| git engine | [gitops.md](gitops.md) |
| model gateway, residency | [models.md](models.md) |
| resource manager | [resources.md](resources.md) |
| repository intelligence | [repo_intelligence.md](repo_intelligence.md) |
| research engine | [research.md](research.md) |
| workers, protocol, auth | [workers.md](workers.md) |
| Wake-on-LAN | [wol.md](wol.md) |
| SSH/admin tools (P26) | [sshtools.md](sshtools.md) |
| database tools (P27) | [dbtools.md](dbtools.md) |
| container/proxy tools (P28) | [infratools.md](infratools.md) |
| backup/restore (P33) | [backup.md](backup.md), [../operations/backup-restore.md](../operations/backup-restore.md) |
| web UI (P31) | [ui.md](ui.md), [../operations/ui.md](../operations/ui.md) |
| bootstrap (P01) | [bootstrap.md](bootstrap.md), [../operations/bootstrap.md](../operations/bootstrap.md) |
| deployment (P32) | [deployment.md](deployment.md), [../operations/deployment.md](../operations/deployment.md) |
| installation preflight | `hermclaw doctor` (`hermclaw/doctor.py`) |
| runbooks | [upgrade](../runbooks/upgrade.md), [disaster recovery](../runbooks/disaster-recovery.md), [cutover](../runbooks/cutover.md) |
| known limitations | [../operations/known-limitations.md](../operations/known-limitations.md) |
| performance baseline | [../operations/performance-baseline.md](../operations/performance-baseline.md) |
| installation | [../operations/INSTALLATION.md](../operations/INSTALLATION.md) |
