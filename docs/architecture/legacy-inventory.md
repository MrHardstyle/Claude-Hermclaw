# Alte Architektur – Read-only-Inventur (P00 0.3)

Quellen (nur lesend):
- GitHub-Relay `MrHardstyle/hermclaw-control` (Verzeichnisse `requests/`, `results/`, `deployments/`): Kommunikationskanal zwischen externem Assistenten und einer lokalen GitLab-Bridge.
- Altes GitLab-Projekt `local-ai/hermclaw` auf `.226` (Branches `redesign/autonomous-agent`, `chatgpt/model-runtime-v01`) – **aus der Build-Umgebung nicht erreichbar** (BLOCKER-001), daher nicht direkt inventarisiert.

Bekannte Bestandteile laut Relay-Struktur und Bauplan:
- `agent_core/agent_core.py` (eingefrorener Monolith, Agent-Loop mit festem Rundenlimit),
- `model_runtime/` (Shell-Pipeline, Modellwechsel via Ollama, aufgabenspezifische Discovery/Acceptance),
- Bridge-Dienst `hermclaw-patch-watcher.service`, WebUI unter `/var/www/noobclaw` auf `.223`.

Übernommene Lessons Learned (als Anforderungen, nicht als Code):
- Kein Runtime-State unter `/tmp`; PostgreSQL ist Source of Truth.
- Keine benchmark-/aufgabenspezifische Logik im Core (Discovery, Verifier, Acceptance generisch).
- Begrenzte Turns (20) + Stagnation Detection statt 60-Runden-Schleifen.
- Reasoning/Thinking niemals als Aktion interpretieren oder speichern.
- Git-Mutationen nur durch Runtime; Scope explizit; Zeilenenden/Basis-SHA deterministisch.
- Kontext pro Turn frisch aus persistentem State (keine Chat-Historie als State).

Altes System wird nicht weiterentwickelt und im Cutover (P42) nur read-only gestellt.
