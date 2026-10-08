# DECISIONS

Entscheidungen, die der verbindliche Bauplan offen lässt. Die Kernarchitektur wird hier nicht verändert.

## Fest vorgegeben (unverändert)
Greenfield · PostgreSQL + pgvector · Gemma 4 26B A4B Planner/Replanner (Fallback Gemma 4 12B) · Qwen3-Coder 30B Main Coder · Qwen3.8 27B Heavy Reviewer · Qwen3 8B Fast Router · EmbeddingGemma 2 · FastAPI/Python · React/TS/Vite · LiteLLM + Ollama · rootless Podman · expliziter Scope · deterministic Verifier · Research mit Quellen · Stagnation Detection · Runtime-kontrollierte Git-Operationen.

## Offene Punkte – getroffene Entscheidungen

| ID | Datum | Entscheidung | Begründung |
|---|---|---|---|
| D-001 | 2026-10-08 | Das vom Nutzer angelegte Repository `MrHardstyle/Claude-Hermclaw` ist das Repo `hermclaw-next`; Entwicklung auf Branch `claude/hermclaw-next-build`. | Nutzervorgabe; Bauplan nennt nur den Namen. Python-Paket heißt `hermclaw`. |
| D-002 | 2026-10-08 | Produktions-PostgreSQL 17 + pgvector ≥ 0.8.2 aus PGDG statt Debian-Paket. | Debian-trixie-pgvector 0.8.0 ist von CVE-2026-3172 betroffen (R-003). |
| D-003 | 2026-10-08 | Server-Sent Events über FastAPI-native `fastapi.sse`; Live-Benachrichtigung über PostgreSQL `LISTEN/NOTIFY`, Replay über `sequence`. | Kein Redis (Bauplan §5); Reconnect über `Last-Event-ID`. |
| D-004 | 2026-10-08 | Standard-Suchprovider der Research Engine: selbst gehostetes SearXNG (JSON-API) auf `.225`, intern; weitere Provider über Interface. | Bauplan fordert Research, lässt API offen (R-023). |
| D-005 | 2026-10-08 | Workspaces und alle Git-Mutationen auf `.225`; Ausführung in der Sandbox auf `.222` über Workspace-Sync (rsync über Worker-API-Upload/Download eines Tar-Streams). | Git-Hoheit der Runtime (§27) + Sandbox auf `.222` (§28). |
| D-006 | 2026-10-08 | Worker-Authentifizierung: pro Worker ein zufälliges Bearer-Token (systemd credential), Requests zusätzlich HMAC-signiert (Zeitstempel + Body-Hash), TLS optional über Nginx/Stunnel. | §35 „pro Worker eigener Token, rotierbar“. |
| D-007 | 2026-10-08 | Media-Backends: `ffmpeg` (Video) und `comfyui` (Bild) als konfigurierbare Adapter des Media-Workers. | §32 lässt Backend offen; Pascal-kompatibel (R-024). |
| D-008 | 2026-10-08 | Token-Schätzung im Context Builder: konservativ 3,2 Zeichen/Token plus Sicherheitsreserve; exakte Zählung über Ollama `prompt_eval_count` als Telemetrie-Rückkopplung. | Kein Tokenizer-Download auf `.225` nötig. |
| D-009 | 2026-10-08 | Build-/Testumgebung (Cloud-Container, Ubuntu 24.04, PG 16 + pgvector 0.6) ersetzt keine Zielhost-Tests; alle Live-Schritte sind als `[~]` mit BLOCKER-001 markiert. | LAN 192.168.178.0/24 aus der Build-Umgebung nicht erreichbar. |
| D-006a | 2026-10-08 | Ergänzung zu D-006: Über reines HTTP sendet der `WorkerRequestSigner` nur die HMAC-Signatur, kein `Authorization: Bearer` (Bearer nur über HTTPS); Verifier prüfen einen vorhandenen Bearer weiterhin. | Ein mitgeschnittener Bearer würde das Fälschen von Signaturen erlauben (P07-Review). |
