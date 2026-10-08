# CLAUDE.md

## Hermclaw Next – verbindliche Arbeitsanweisung für Claude Code

Dieses Repository ist ein Greenfield-Neuaufbau.

Lies vor Beginn vollständig:

1. `docs/Claude_Code_Hermclaw_Next_FULL_BUILD_PROMPT_v2.md`
2. `docs/Hermclaw_Next_Vollstaendiger_Bauplan_v2.md`
3. `BUILD_PLAN.md`
4. `STATUS.md`
5. `BUGS.md`
6. `DECISIONS.md`
7. `RESEARCH_INDEX.md`
8. `TEST_MATRIX.md`
9. `RISK_REGISTER.md`

Die beiden Dateien unter `docs/` sind die verbindliche Zielarchitektur und der verbindliche Full-Build-Auftrag.

### Ausführung

- Arbeite P00 bis P42 vollständig durch.
- Stoppe nicht künstlich nach einem Milestone.
- Überspringe keine Phase.
- Führe vor externen Integrationen aktuelle Researches durch.
- Verwende bevorzugt offizielle Primärquellen.
- Dokumentiere Research unter `docs/research/`.
- Pflege `BUILD_PLAN.md`, `STATUS.md`, `BUGS.md`, `RESEARCH_INDEX.md`, `TEST_MATRIX.md` und `RISK_REGISTER.md` fortlaufend.
- Blockierende Bugs sofort beheben.
- Nicht-blockierende Bugs reproduzierbar dokumentieren und spätestens in P39 bearbeiten.
- Ändere die vorgegebene Kernarchitektur nicht eigenmächtig.
- Baue keine benchmark- oder projektspezifischen Sonderfälle in den Runtime-Core ein.
- Gemma bleibt Planner/Replanner.
- Qwen3-Coder 30B bleibt Implementation Worker.
- Qwen3.8 27B bleibt Heavy Reviewer.
- Qwen3 8B bleibt Fast Router.
- Scope bleibt explizit.
- Git-Mutationen bleiben Runtime-kontrolliert.
- Keine private Chain-of-Thought speichern oder anzeigen.

Wenn ein echter externer Blocker auftritt, dokumentiere ihn vollständig und arbeite alle unabhängigen Aufgaben weiter ab.

Das Ziel ist ein vollständig getesteter Hermclaw Next Release Candidate, kein Demo-Skeleton.
