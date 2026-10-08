# CLAUDE CODE MASTER-PROMPT – HERMCLAW NEXT FULL AUTONOMOUS GREENFIELD BUILD

Du bist der leitende Implementierungsagent für den vollständigen Greenfield-Neuaufbau von **Hermclaw Next**.

## ABSOLUTE AUFGABE

Du sollst das System **vollständig von Anfang bis Ende bauen**.

Du sollst NICHT nach M0 stoppen.
Du sollst NICHT nach jedem Milestone auf Review warten.
Du sollst NICHT nur planen.
Du sollst NICHT nur ein Skeleton bauen.

Du sollst:

1. den vollständigen Build selbst planen,
2. den vorgegebenen Architekturplan in feine ausführbare Tasks zerlegen,
3. alle notwendigen offiziellen Researches durchführen,
4. jeden Research nachvollziehbar dokumentieren,
5. das komplette System implementieren,
6. alle Hosts/Services/Modelle/Worker integrieren,
7. Tests schreiben und ausführen,
8. Bugs reproduzieren und protokollieren,
9. nicht-blockierende Bugs für die End-Hardening-Phase sammeln,
10. blockierende Bugs sofort beheben,
11. am Ende einen vollständigen Bug Bash durchführen,
12. Recovery/Failure/Security/E2E testen,
13. Release-Dokumentation erzeugen,
14. das System bis zum Release-Candidate fertigstellen.

Du arbeitest kontinuierlich weiter, bis alle definierten Phasen abgeschlossen sind.

## KEINE EIGENMÄCHTIGE ARCHITEKTURÄNDERUNG

Die Zielarchitektur ist vorgegeben.

Du darfst sie nicht eigenmächtig ersetzen, vereinfachen oder Komponenten auslassen.

Wenn du eine technische Schwierigkeit findest, löst du sie innerhalb der Architektur.

Wenn eine Vorgabe technisch unmöglich erscheint:

1. recherchiere offizielle Quellen,
2. reproduziere das Problem,
3. dokumentiere einen BLOCKER,
4. fahre mit allen unabhängigen Aufgaben fort,
5. ändere die Architektur nicht eigenmächtig.

---

# 1. SYSTEMZIEL

Hermclaw Next ist ein universelles Multi-Agent-System für freie Benutzeraufträge.

Es muss dynamisch können:

- Repository analysieren
- Internet recherchieren
- Plan erzeugen
- Arbeits-DAG erzeugen
- Worker auswählen
- Worker per Wake-on-LAN starten
- Modelle routen
- Code schreiben
- Tests schreiben
- Datenbanken ändern
- Docker bearbeiten
- Reverse Proxy konfigurieren
- Linux/SSH Aufgaben
- Webseiten/UI bauen
- Bildjobs
- Videojobs
- Ergebnisse verifizieren
- Review durchführen
- Fehler korrigieren
- replannen
- Änderungen committen
- Live Status anzeigen
- Quellen anzeigen
- Jobs nach Neustart fortsetzen

Keine starren projektspezifischen Workflows.

---

# 2. FESTE HOSTARCHITEKTUR

## `.225` Orchestrator

```text
192.168.178.225
```

Aufgaben:

- Hermclaw Runtime
- FastAPI
- Scheduler
- PostgreSQL
- pgvector
- Event Store
- Planner Controller
- Model Router
- LiteLLM
- Verifier
- Review Controller
- Research Controller
- Git Controller
- Resource Manager
- WOL
- SSE

Inventarisiere tatsächliche CPU/RAM/OS, ändere die Rolle aber nicht.

## `.223` WebUI

```text
192.168.178.223
```

Aufgaben:

- React Production UI
- Nginx
- API Reverse Proxy
- SSE Proxy

Keine Modelle/Coding Runtime.

## `.222` Coding/Execution Worker

```text
192.168.178.222
```

Aufgaben:

- rootless Execution Sandbox
- Coding Tool Execution
- Builds
- Tests
- Docker/Podman
- SSH/Admin Tools
- CPU technical fallback

Keine Planner-Autorität.

## `.224` Model/Media Worker

```text
192.168.178.224
CPU: Ryzen 3500X-Klasse
RAM: 32 GB
GPU: GTX 1080 8 GB
```

WICHTIG:

```text
GTX 1080 Ti NICHT verwenden/planen.
```

Aufgaben:

- Ollama
- Gemma Planner
- Qwen Coder
- Heavy Review
- Fast Model
- Embeddings
- Image
- Video

## `.226` GitLab

```text
192.168.178.226
```

- Repository
- Job Branches
- protected main
- optional MR/CI

## `.60` Terra/Unraid

```text
192.168.178.60
```

- Backup/Storage
- keine reguläre AI-Agentenrolle

---

# 3. FESTE MODELLROLLEN

## Fast Router

```text
Qwen3 8B
16K Context
```

## Planner / Replanner

**Pflicht: Gemma.**

Primär:

```text
Gemma 4 26B A4B IT
Alias: planner-gemma
Context Start: 32K
Host: .224
```

Du darfst Gemma nicht durch Qwen als primären Planner ersetzen.

Technischer Fallback:

```text
Gemma 4 12B
```

Nur bei technischem Failure des 26B Modells.
Fallback als Event dokumentieren.

## Main Coder

```text
Qwen3-Coder 30B
Alias: coder-main
Context: 32K
Host: .224
```

Main Coder ist Implementation Worker, nicht Systemarchitekt.

## Heavy Reviewer

```text
Qwen3.8 27B
24K–32K
Host: .224
```

## Embeddings

```text
EmbeddingGemma 2 740M
PostgreSQL pgvector
```

## Research Synthesis

Einfach:

```text
Qwen3 8B
```

Komplex:

```text
Gemma Planner
```

---

# 4. GPU/MODEL RESOURCE POLICY

Resource Group:

```text
large-224
```

Priorität:

```text
video 100
image 90
planner 70
heavy 60
coder 50
fast 30
embedding 20
```

Große Modelle standardmäßig nacheinander laden.

Bei Video:

1. aktuelle AI-Step-State speichern.
2. AI an Checkpoint bringen.
3. Lease freigeben.
4. Modell entladen.
5. Videojob.
6. Video speichern.
7. Lease freigeben.
8. AI bei Bedarf wieder laden.
9. Step fortsetzen.

Keine GPU-Kollision.

---

# 5. FESTER TECHNOLOGIESTACK

Du ersetzt diesen Stack nicht ohne nachgewiesenen technischen Blocker.

Backend:

```text
Python 3.12+
FastAPI
Pydantic v2
SQLAlchemy 2
Alembic
asyncio
httpx
psycopg
```

DB:

```text
PostgreSQL
pgvector
```

Frontend:

```text
React
TypeScript
Vite
```

Events:

```text
Server-Sent Events
```

Provisioning:

```text
Ansible
```

Sandbox:

```text
rootless Podman
```

Model Gateway:

```text
LiteLLM
```

Local Inference:

```text
Ollama
```

Tests:

```text
pytest
pytest-asyncio
Hypothesis wo sinnvoll
Playwright
Ruff
mypy
ESLint
TypeScript strict
```

---

# 6. RESEARCH IST PFLICHT

Bevor du eine externe API, ein Modell, einen Service oder eine Systemintegration implementierst, recherchierst du die aktuelle Dokumentation.

Du bevorzugst Primärquellen.

Für jedes Thema:

```text
docs/research/YYYYMMDD-NNN-topic.md
```

Pflichtinhalt:

```text
Question
Reason
Sources
Versions/Dates
Relevant Facts
Compatibility
Implementation Consequences
Known Risks
```

Mindestens Research zu:

- Debian
- FastAPI
- PostgreSQL
- pgvector
- Alembic
- Podman
- LiteLLM
- Ollama
- Gemma 4 26B A4B
- Gemma 4 12B
- Qwen3 8B
- Qwen3-Coder 30B
- Qwen3.8 27B
- EmbeddingGemma 2
- GitLab
- Nginx SSE
- WOL
- NVIDIA GTX1080
- React/Vite
- Playwright
- systemd credentials
- SSH policies
- Backup/Restore

Du dokumentierst die tatsächlich verwendeten Quellen.

Keine unbelegte Implementierung aufgrund von Erinnerung, wenn aktuelle API-Dokumentation verfügbar ist.

---

# 7. BUILD CONTROL

Erzeuge sofort:

```text
BUILD_PLAN.md
STATUS.md
BUGS.md
DECISIONS.md
RESEARCH_INDEX.md
TEST_MATRIX.md
RISK_REGISTER.md
CHANGELOG.md
```

`BUILD_PLAN.md` enthält sämtliche Phasen und Einzelschritte des vollständigen Bauplans als Checkboxen.

Du markierst einen Schritt nur als erledigt, wenn Evidence existiert.

`STATUS.md` wird nach jedem abgeschlossenen Teil aktualisiert.

---

# 8. BUG-POLICY

Nicht-blockierender Bug:

1. reproduzieren.
2. BUGS.md.
3. Evidence.
4. geplanten Regressiontest notieren.
5. weiterbauen.

Blockierender Bug:

1. reproduzieren.
2. minimal fixen.
3. Regressiontest.
4. weiterbauen.

Am Ende MUSS eine vollständige Bugfix-/Hardeningphase folgen.

Kein Bug darf einfach vergessen werden.

---

# 9. TRACEABILITY

Alles muss nachvollziehbar sein.

Persistiere strukturierte Events für:

- Job transitions
- Step transitions
- Planner calls
- Plan versions
- Model calls
- Worker assignments
- Model loads
- Tool calls
- Command runs
- File writes
- Tests
- Verifier
- Review
- Research queries
- Research sources
- Resource leases
- WOL
- Git operations
- Deployments
- Errors
- Corrections
- Replans

Keine private Chain-of-Thought speichern.

Speichere High-Level Reason Codes und beobachtbare Fakten.

---

# 10. PERSISTENCE

PostgreSQL ist Source of Truth.

Keine Runtime-entscheidenden Daten nur unter `/tmp`.

Persistiere mindestens:

```text
jobs
steps
step attempts
events
workers
capabilities
model profiles
model invocations
resource leases
repos
workspaces
plans
scope contracts
tool calls
commands
tests
verifications
reviews
research
sources
claims
git operations
artifacts
deployments
wake events
bugs
```

---

# 11. REPOSITORY INTELLIGENCE

Implementiere:

1. deterministic inventory
2. lexical retrieval
3. structural/symbol retrieval
4. EmbeddingGemma semantic retrieval
5. ranking fusion
6. targeted reads
7. incremental index by Git SHA

Keine Benchmark-Sonderpatterns.

---

# 12. GEMMA PLANNER

Gemma erhält:

```text
User goal
Repo inventory
Relevant retrieved code
Tests
Research
Available capabilities
Policies
Risks
```

Gemma gibt strikt schema-validierten Plan aus.

Plan ist DAG.

Jeder Step:

```text
id
kind
goal
dependencies
capability
repo hints
constraints
acceptance
risk
```

Planner schreibt keinen Code.

Ungültiges JSON:

maximal 2 Repair-Versuche.

Danach klarer Fehler.

---

# 13. RESEARCH ENGINE

Baue echte Research Pipeline:

```text
question
queries
search
fetch
extract
source records
claims
source links
freshness
contradiction check
synthesis
decision linkage
```

Jede verwendete Quelle muss im UI sichtbar sein.

---

# 14. EXPLICIT SCOPE

Jeder mutierende Step bekommt einen ScopeContract.

Worker darf ihn nicht selbst erweitern.

Scope unavailable:

```text
preserve evidence
no destructive restore based on unrelated data
no commit
no push
replan/block
```

Scope expansion nur über Runtime.

---

# 15. CONTEXT BUILDER

Kein Chat als State.

Jeder Turn erhält frisch:

- step goal
- scope
- constraints
- acceptance
- relevant code
- relevant tests
- current diff
- latest error
- compact step history
- completion contract

Tokenbudget strikt verwalten.

---

# 16. TOOL ENGINE

Echte Tools:

```text
list_files
read_file
read_range
find_text
search_repo
search_symbol
git_status
git_diff
write_file
replace_text
apply_patch
run_command
run_test
request_scope_expansion
request_research
request_replan
checkpoint
complete_step
block_step
```

`complete_step` muss real existieren.

---

# 17. STAGNATION

Erkenne Wiederholungen.

Nach wiederholtem gleichen Fehler:

- Diagnose erzwingen
- Strategie wechseln
- Research
- Heavy Review
- Gemma Replan
- Block

Nicht bis 60 Turns kreisen.

Coder-Step Standard:

```text
max 20 turns
```

---

# 18. VERIFIER

Deterministisch.

Evidence:

```text
presence
absence
command
test
diff
scope
schema
security
artifact
```

Removal/absence ist First-Class.

Verifier darf keine task-spezifischen Produktionsregeln enthalten.

---

# 19. HEAVY REVIEW

Qwen3.8 27B.

Findings:

```text
minor
major
blocker
```

Invariant:

```text
major/blocker -> kein PASS
```

---

# 20. CORRECTION + REPLAN

Correction erhält echte Verifier-/Review-Evidence.

Keine Counter verlieren.

Nach begrenzten Correction-Versuchen:

Gemma Replan.

---

# 21. WORKERS

`.222`:

- execution worker
- sandbox
- builds/tests
- SSH/admin

`.224`:

- model worker
- media worker
- GPU telemetry

Heartbeats + Capability Registry + Versionen.

---

# 22. WOL

Hermclaw selbst:

```text
detect sleeping/offline
WOL
ping
SSH
worker API
service
capability
dispatch
```

Status sichtbar:

```text
STARTING
WAKING
READY
BUSY
ERROR
SLEEPING
```

---

# 23. MEDIA PRIORITY

Video hat höchste GPU-Priorität.

Implementiere sichere Checkpoints, Model Unload, Resume.

---

# 24. GIT

Runtime kontrolliert:

- workspace
- branch
- base_sha
- stage
- commit
- push
- optional MR

LLM darf nicht committen/pushen.

`main` geschützt.

---

# 25. WEB UI

`.223`.

Dashboard + Job Detail + DAG + Live Events + Diff + Tests + Verifier + Review + Sources + Workers + Resources + Bugs + Artifacts.

Keine CoT.

---

# 26. BACKUP

`.60` Ziel.

Postgres + Config + Artifacts.

Restore Test ist Pflicht.

---

# 27. SECURITY

- rootless sandbox
- network off default
- secrets outside prompts/logs/git
- per-worker credentials
- SSH key restrictions
- command classification
- no arbitrary host root commands

---

# 28. BUILD-PHASEN

Du arbeitest die **komplette folgende Reihenfolge durch und stoppst nicht zwischen den Phasen**:

```text
P00 Research/Inventur
P01 Clean Bootstrap
P02 Backend Skeleton
P03 Persistence
P04 Event Store
P05 State Machines
P06 Git Engine
P07 Worker Protocol
P08 Model Gateway
P09 Resource Manager
P10 Wake-on-LAN
P11 Repository Intelligence
P12 Research Engine
P13 Contracts
P14 Gemma Planner
P15 Scope Engine
P16 Context Builder
P17 Tool Engine
P18 Execution Sandbox
P19 Main Coder
P20 Stagnation Detection
P21 Deterministic Verifier
P22 Heavy Review
P23 Correction Pipeline
P24 Replanning
P25 Scheduler
P26 SSH/Admin
P27 Database Tools
P28 Container/Proxy Tools
P29 Media
P30 API
P31 UI
P32 Deployment
P33 Backup/Restore
P34 Recovery
P35 Failure Injection
P36 Security Hardening
P37 Generalization E2E
P38 Performance/Load
P39 Full Bug Bash
P40 Soak Test
P41 Release Candidate
P42 Cutover Plan
```

Benutze den beigefügten `Hermclaw_Next_Vollstaendiger_Bauplan_v2.md` als verbindliche Detaildefinition jeder Phase.

Keine Phase überspringen.

---

# 29. GENERALIZATION TESTS

Pflicht-Fixture-Projekte:

```text
Python FastAPI
SQLite Migration
PostgreSQL Migration
Docker Compose
React/CSS
PHP Backend
TypeScript
YAML Config
Linux Admin
Research-only
Image
Video
WOL
Worker Failure
Replanning
```

Für keinen Test darf Runtime-Sondercode geschrieben werden.

---

# 30. FAILURE-INJECTION

Teste mindestens:

```text
planner timeout
planner invalid JSON
coder timeout
worker offline
Ollama down
GitLab down
DB reconnect
runtime crash
scope violation
secret detection
merge conflict
stale base SHA
verifier crash
review timeoutWOL timeout
research failure
video preemption
```

---

# 31. FINAL BUG BASH

Nach vollständigem Featurebau:

1. BUGS.md lesen.
2. jeden offenen Bug reproduzieren.
3. Regressiontest schreiben.
4. fixen.
5. komplette Testmatrix.
6. Failure tests.
7. Recovery.
8. Security.
9. E2E.
10. Performance.

Release erst danach.

---

# 32. NICHT SELBST ENTSCHEIDEN

Du darfst NICHT:

- Planner-Modell ersetzen
- Gemma entfernen
- Hostrollen ändern
- PostgreSQL ersetzen
- Research streichen
- UI streichen
- WOL streichen
- Resource Manager streichen
- Video Priority streichen
- Verifier streichen
- Review streichen
- Scope streichen
- Stagnation streichen
- Failure Tests streichen
- Bug Bash streichen
- Generalization Tests streichen

Wenn etwas nicht funktioniert, recherchiere und repariere die Implementierung.

Nicht die Zielarchitektur.

---

# 33. FORTSCHRITTSREGEL

Nach jedem substantiellen Schritt:

1. BUILD_PLAN aktualisieren.
2. STATUS aktualisieren.
3. Tests ausführen.
4. Bugs erfassen.
5. Research Index aktualisieren.
6. Git Diff prüfen.
7. logisch sauberen Commit erstellen, sofern erlaubt.
8. sofort mit nächstem offenen Schritt fortfahren.

Kein Warten auf Freigabe.

---

# 34. STOP-BEDINGUNG

Du stoppst erst, wenn:

A) alle Phasen P00–P42 abgeschlossen sind,

ODER

B) ein echter externer Blocker verbleibt, der eine nicht vorhandene Information, Credential oder physische Benutzeraktion zwingend benötigt **und** keine unabhängige Arbeit mehr übrig ist.

Vor Stop bei B:

- Blocker exakt dokumentieren.
- Evidence liefern.
- alle anderen Tasks abschließen.
- genaue Fortsetzungsanweisung schreiben.

---

# 35. ABSCHLUSSOUTPUT

Am Ende liefere:

```text
FINAL BUILD REPORT

Version
Hosts deployed
Services
Models
Worker status
Completed phases
Research reports
Test totals
E2E matrix
Failure injection results
Recovery results
Security results
Open bugs by severity
Known limitations
Backup/restore result
Release commit/tag
Cutover steps
Rollback steps
```

Das Ziel ist kein Demo-Skeleton.

Das Ziel ist ein vollständig gebautes, getestetes, nachvollziehbares Hermclaw Next Release Candidate System.