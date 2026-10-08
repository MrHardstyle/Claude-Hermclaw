# Hermclaw Next – Systemdiagramm (P00 0.19)

```text
                         Browser (LAN)
                              │ HTTPS
                              ▼
 ┌────────────────────────────────────────────┐
 │ .223  WebUI                                │
 │  Nginx :80/:443  → /var/www/hermclaw-next  │
 │  /api/*  ──────────────┐  (SSE ohne Buffer) │
 └────────────────────────┼───────────────────┘
                          ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │ .225  Orchestrator                                               │
 │  hermclaw-api (FastAPI :8000)      hermclaw-scheduler (Runtime)   │
 │   ├ Job/Step State Machines         ├ DAG Scheduler (SKIP LOCKED) │
 │   ├ Event Store + SSE               ├ Planner (Gemma) / Replan    │
 │   ├ Research Engine ── SearXNG      ├ Scope Engine / Context      │
 │   ├ Resource Manager (Leases)       ├ Coder Loop / Stagnation     │
 │   ├ WOL Controller                  ├ Verifier / Heavy Review     │
 │   └ Git Engine ──────────── SSH ──► │ GitLab .226                 │
 │  PostgreSQL 17 + pgvector :5432 (lokal)                           │
 │  LiteLLM Gateway :4000 ──────────────────────────────┐           │
 └───────────┬───────────────────────────┬──────────────┼───────────┘
             │ Worker-API (Token, :8787) │ WOL (UDP 9)  │ OpenAI-API
             ▼                           ▼              ▼
 ┌──────────────────────────────┐  ┌──────────────────────────────────┐
 │ .222  Execution Worker       │  │ .224  Model/Media Worker          │
 │  hermclaw-exec-worker :8787  │  │  hermclaw-model-worker :8787      │
 │  rootless Podman Sandbox     │  │  Ollama :11434 (GTX 1080 8 GB)    │
 │  (Netz aus, read-only)       │  │   gemma4:26b  qwen3-coder:30b     │
 │  Builds/Tests, SSH-Tools     │  │   qwen3.8:27b qwen3:8b            │
 └──────────────────────────────┘  │   gemma4:12b  embeddinggemma-2    │
                                   │  Media: ffmpeg (NVENC), ComfyUI   │
                                   └──────────────────────────────────┘
 ┌──────────────────────────────┐  ┌──────────────────────────────────┐
 │ .226  GitLab (Remote, MR)    │  │ .60  Terra/Unraid (Backups)       │
 └──────────────────────────────┘  └──────────────────────────────────┘
```

## Datenflüsse
1. UI → `.223` Nginx → `.225` API (`/api/...`, SSE `/api/jobs/{id}/events/stream`).
2. Scheduler → LiteLLM (`.225:4000`) → Ollama (`.224:11434`) für alle Modellrollen.
3. Scheduler → Model-Worker (`.224:8787`) für Load/Unload/GPU-Telemetrie/Media.
4. Scheduler → Execution-Worker (`.222:8787`) für Sandbox-Kommandos/Tests in Workspaces.
5. Git Engine (`.225`) → GitLab (`.226`) per SSH; nur Runtime committet/pusht.
6. Backup-Timer (`.225`) → `.60` per rsync/SSH.
7. WOL-Controller (`.225`) → Broadcast `192.168.178.255:9` → `.222`/`.224`.

## Workspaces
Workspaces liegen auf `.225` (Git-Engine-Hoheit) unter `/var/lib/hermclaw/workspaces/<job>/<repo>` und werden für Sandbox-Läufe auf `.222` per rsync-Spiegel (`/var/lib/hermclaw-exec/workspaces/...`) synchronisiert; Ergebnisse (Diff) fließen zurück. Git-Mutationen finden ausschließlich auf `.225` statt.
