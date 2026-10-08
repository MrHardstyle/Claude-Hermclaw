# HERMCLAW NEXT – VOLLSTÄNDIGER GREENFIELD-BAUPLAN

**Version:** 2.0  
**Stand:** 08.10.2026  
**Ziel:** Vollständiger Neuaufbau von Hermclaw als universelles, lokales Multi-Agent-System.  
**Implementierungsagent:** Claude Code  
**Vorgabe:** Claude Code plant nicht die Zielarchitektur neu, sondern setzt diesen Plan vollständig um.

---

# 0. NICHT VERHANDELBARE GRUNDREGELN

1. Das alte Hermclaw-/Noob2Claw-System wird **nicht weiterentwickelt**.
2. Das neue System entsteht in einem neuen Repository `hermclaw-next`.
3. Alte Quellen dürfen gelesen werden, um Verhalten, Fehler und Lessons Learned zu verstehen.
4. Alter Produktionscode wird nicht blind kopiert.
5. Claude Code darf keine Kernarchitektur, Modellrollen oder Hostrollen eigenmächtig ändern.
6. Kein Schritt dieses Plans darf stillschweigend ausgelassen werden.
7. Jeder Schritt wird im Build-Plan als erledigt/nicht erledigt dokumentiert.
8. Nicht-blockierende Bugs dürfen bis zur finalen Hardening-Phase gesammelt werden.
9. Blockierende Bugs müssen sofort behoben werden.
10. Jeder Bug bekommt Reproduktion, Erwartung, Ist-Zustand, Logs und betroffene Komponente.
11. Alle externen Integrationen werden vor Umsetzung recherchiert.
12. Research verwendet bevorzugt offizielle Primärquellen.
13. Jede Research-Entscheidung wird mit Quelle, Datum und Schlussfolgerung dokumentiert.
14. Kein Benchmark darf Produktionslogik bestimmen.
15. Keine private Chain-of-Thought wird gespeichert oder angezeigt.
16. Alle beobachtbaren Aktionen müssen jedoch vollständig nachvollziehbar sein.
17. Jeder Modellaufruf, Toolaufruf, Test, Git-Diff, Workerwechsel, Ressourcenlease und Research-Schritt erzeugt strukturierte Events.
18. Runtime-State lebt nicht im Kontextfenster eines Modells.
19. Runtime-State lebt persistent in PostgreSQL + Artefaktspeicher.
20. LLMs dürfen niemals selbst `git commit`, `git push` oder destruktive Git-Operationen durchführen.
21. Commit/Push werden ausschließlich durch deterministische Runtime-Komponenten ausgeführt.
22. Scope-Autorisierung wird niemals vom Worker selbst erraten.
23. Tests werden nicht abgeschwächt, nur damit eine Implementierung grün wird.
24. Bei einer bewussten API-Migration müssen alte Tests korrekt auf den neuen Vertrag migriert werden.
25. Wiederholte identische Fehlversuche werden durch Stagnation Detection gestoppt.
26. Claude Code arbeitet den gesamten Bauplan autonom bis zum Ende ab.
27. Claude Code wartet nicht nach jedem Milestone auf Freigabe.
28. Claude Code stoppt nur bei einem echten externen Blocker, der ohne Benutzeraktion technisch nicht lösbar ist.
29. Auch dann werden alle unabhängigen Arbeiten weitergeführt.
30. Am Ende folgt eine vollständige Bugfix-, Regression-, Recovery-, Failure-Injection- und E2E-Phase.

---

# 1. ZIELBILD

Hermclaw Next nimmt einen freien Benutzerprompt entgegen, z. B.:

```text
Baue Feature X in Repository Y.
Analysiere vorher den Bestand.
Recherchiere die benötigte Dokumentation.
Plane die Änderung.
Implementiere sie.
Teste sie.
Reviewe sie.
Dokumentiere die Quellen und Änderungen.
```

Die Runtime soll daraus dynamisch:

1. Job anlegen.
2. Repository erfassen.
3. aktuellen Zustand analysieren.
4. bei Bedarf Research ausführen.
5. Gemma-Planner aufrufen.
6. strukturierten DAG-Plan erzeugen.
7. benötigte Capabilities bestimmen.
8. Worker auswählen.
9. Worker falls nötig per Wake-on-LAN starten.
10. Ressourcen reservieren.
11. Task-spezifischen Scope bestimmen.
12. Arbeitsschritte einzeln ausführen.
13. jeden Schritt nachvollziehbar protokollieren.
14. Ergebnisse persistent speichern.
15. Tests ausführen.
16. deterministic verifier ausführen.
17. Heavy Review ausführen.
18. bei Fehlern gezielte Correction Steps erzeugen.
19. bei Stagnation replannen/escalaten.
20. Commit nur nach erfolgreicher Verifikation erzeugen.
21. vollständigen Abschlussbericht erstellen.

Das System soll ohne Runtime-Patch für sehr verschiedene Aufgaben funktionieren:

- Python REST APIs
- Webinterfaces
- PHP Anwendungen
- TypeScript/React
- Datenbanken
- SQL Migrationen
- Docker
- Reverse Proxy
- YAML/JSON Config
- Linux Administration
- SSH Aufgaben
- Research
- Bildjobs
- Videojobs
- Dokumentation

---

# 2. HOST- UND HARDWARE-TOPOLOGIE

Alle Werte werden zusätzlich durch ein automatisches Inventurskript verifiziert. Abweichungen werden dokumentiert, aber Hostrollen werden nicht selbstständig geändert.

## 2.1 `.225` – Orchestrator / Agent Runtime

Adresse:

```text
192.168.178.225
```

Rolle:

- zentrale Hermclaw Runtime
- API
- Scheduler
- Agent Runtime
- Job State Machine
- Planner-Aufruf
- Verifier
- Review-Koordination
- Model Router
- Resource Manager
- Event Store
- Research-Koordination
- Wake-on-LAN Controller
- Git Job Controller
- Persistenz
- SSE Event Stream

Bekannter Hardwarekontext:

- Serverklasse Xeon E3 v6 / 4C8T
- RAM im vorhandenen Serverumfeld bis ca. 48–64 GB; exakten Wert automatisch inventarisieren
- keine dedizierte GPU als primäre Modellressource

Keine großen Modelle auf `.225` laden, solange `.224` verfügbar ist.

## 2.2 `.223` – WebUI / Presentation Control Plane

Adresse:

```text
192.168.178.223
```

Rolle:

- Hermclaw Webinterface
- Nginx
- statisches React-Build
- Reverse Proxy zur API auf `.225`
- keine Agentenlogik
- keine Modellinferenz
- keine Coding-Workspaces

Vorgesehener Webroot:

```text
/var/www/hermclaw-next
```

Bestehendes `/var/www/noobclaw` nur als Referenz behandeln.

## 2.3 `.222` – Coding / Execution Worker

Adresse:

```text
192.168.178.222
```

Rolle:

- isolierte Coding-Ausführung
- Shell Tools
- Test Runner
- Build Runner
- Docker/Podman Sandbox
- Git Checkout/Workspace-Ausführung nach Runtime-Policy
- SSH-/Administrations-Tasks
- technischer CPU-Fallback

Keine Planungsautorität.

Keine automatische Entscheidung über Scope.

Exakte CPU/RAM-Werte per Inventur erfassen.

## 2.4 `.224` – Model + Video Worker

Adresse:

```text
192.168.178.224
```

Bekannter Hardwarestand:

```text
CPU: Ryzen 3500X-Klasse
RAM: 32 GB
GPU: NVIDIA GTX 1080
VRAM: 8 GB
```

WICHTIG:

```text
Die GTX 1080 Ti des Gaming-PCs gehört NICHT zu diesem System.
```

Dienste:

- Ollama
- Model Worker API
- Image Worker
- Video Worker
- GPU Resource Agent

## 2.5 `.226` – GitLab

Adresse:

```text
192.168.178.226
```

Rolle:

- zentrale Git-Repositories
- Merge Requests
- geschützte Main-Branches
- Job Branches
- CI optional

Regel:

```text
main/master geschützt
keine direkten LLM-Pushes
```

## 2.6 `.60` – Terra / Unraid

Adresse:

```text
192.168.178.60
```

Rolle:

- persistenter Infrastruktur-/Storage-Host
- Backups
- Artefaktbackup
- optional Model-/Dataset-Archiv
- keine reguläre AI-Agentenrolle

Nicht für laufende Agentenarbeit einplanen.

## 2.7 Wake-on-LAN Hosts

Jeder entsprechende Host besitzt Config:

```yaml
wake_on_lan:
  enabled: true
  mac: "..."
  broadcast: "192.168.178.255"
  ping_timeout_seconds: 90
  service_timeout_seconds: 180
```

Readiness:

1. WOL gesendet.
2. Ping verfügbar.
3. SSH verfügbar.
4. Worker API verfügbar.
5. benötigte lokale Dienste verfügbar.
6. Capability Registry aktuell.
7. erst dann Dispatch.

---

# 3. EXAKTE MODELLARCHITEKTUR

Claude Code darf diese Rollen nicht eigenmächtig durch andere Modelle ersetzen.

Modelle werden über LiteLLM-Profile abstrahiert.

## 3.1 Fast Router

Primär:

```text
Qwen3 8B
```

Zielcontext:

```text
16K
```

Aufgaben:

- Intent-Klassifikation
- Task-Triage
- leichte Zusammenfassungen
- Event-Synthese
- einfache Routingentscheidungen
- einfache Research-Synthese

Keine Architekturentscheidungen.

## 3.2 Planner / Replanner – GEMMA

Primärer Planner:

```text
Gemma 4 26B A4B IT
```

Betrieb:

```text
Ollama auf .224
LiteLLM Alias: planner-gemma
Context Startwert: 32K
```

Begründete Architekturvorgabe:

- Gemma ist der zentrale lokale Planner.
- Planer erzeugt strukturierte Pläne, keine Codeänderungen.
- Replanning wird ebenfalls durch Gemma durchgeführt.
- Gemma darf Research-Ergebnisse synthetisieren.
- Gemma darf Scope-Vorschläge machen, aber die Runtime validiert und autorisiert sie.
- Gemma darf keine Git-Mutationen durchführen.

Fallback:

```text
Gemma 4 12B nur als technischer Planner-Fallback
```

Fallback wird nur verwendet, wenn 26B technisch nicht ausführbar ist.

Kein stiller Modellwechsel. Event + Warning + Job-Metadaten müssen Fallback dokumentieren.

## 3.3 Main Coder

Primär:

```text
Qwen3-Coder 30B
```

Betrieb:

```text
Ollama .224
LiteLLM Alias: coder-main
Context: 32K
Max output: 4K–8K konfigurierbar
```

Rolle:

- Implementierung
- Tests
- zielgerichtete Codeänderung
- Migrationen
- Docker/YAML/Python/PHP/TS/JS/SQL

Nicht:

- Gesamtarchitektur umplanen
- Acceptance neu definieren
- Scope autorisieren
- Testsemantik abschwächen

## 3.4 Heavy Reviewer

Primär:

```text
Qwen3.8 27B
```

Context:

```text
24K–32K
```

Rolle:

- Diff Review
- Risikoreview
- Architekturverletzungen erkennen
- Verifier-Ergebnisse prüfen
- Correction-Empfehlungen strukturieren

Invariant:

```text
major > 0 => kein PASS
blocker > 0 => kein PASS
```

## 3.5 Embeddings

Primär:

```text
EmbeddingGemma 2 740M
```

Zwecke:

- semantische Repository-Suche
- Research-Retrieval
- Memory-Retrieval später

Speicher:

```text
PostgreSQL + pgvector
```

Falls der konkrete lokale Serving-Stack das Modell noch nicht nativ unterstützt, wird ein eigener kompatibler Embedding-Adapter implementiert. Claude Code darf nicht eigenmächtig auf eine andere Modellfamilie wechseln, ohne dies als dokumentierten technischen Blocker zu markieren.

## 3.6 Research Synthesis

Einfach:

```text
Qwen3 8B
```

Komplex:

```text
Gemma Planner
```

Research selbst besteht aus deterministischen Browser-/HTTP-/Search-Tools plus Quellenverwaltung, nicht aus Modellwissen allein.

## 3.7 Media

Bild/Video:

- getrennte Media Pipeline
- GPU Lease auf `.224`
- höchste Priorität für Video
- AI-Modelle werden an Checkpoint entladen, wenn Video GPU benötigt

---

# 4. MODEL RESOURCE POLICY

`.224` hat begrenzten RAM/VRAM.

Deshalb:

```text
large-224 = exklusive Resource Group
```

Große Modelle werden standardmäßig nicht gleichzeitig resident gehalten.

Prioritäten:

```text
video: 100
image: 90
planner-gemma: 70
heavy-review: 60
coder-main: 50
fast-router: 30
embedding: 20
```

Ablauf eines Modellwechsels:

1. laufenden Step auf sicheren Checkpoint bringen.
2. State persistieren.
3. Lease freigeben.
4. nicht benötigtes Modell entladen.
5. neues Modell laden.
6. Healthcheck.
7. Context-Konfiguration prüfen.
8. Step starten.

Kein Worker darf Ressourcen eigenmächtig preempten.

---

# 5. SOFTWARE-STACK

## Backend

```text
Python 3.12+
FastAPI
Pydantic v2
SQLAlchemy 2
Alembic
psycopg
asyncio
httpx
```

## Datenbank

```text
PostgreSQL
pgvector
```

Kein Redis als Pflichtkomponente.

Queue/Scheduler darf PostgreSQL verwenden:

```text
FOR UPDATE SKIP LOCKED
```

## Frontend

```text
React
TypeScript
Vite
```

## Echtzeit

```text
Server-Sent Events
```

WebSocket nur wenn später eine Funktion echte bidirektionale Dauerkommunikation benötigt.

## Proxy

```text
Nginx auf .223
```

## Models

```text
Ollama auf .224
LiteLLM Gateway auf .225
```

## Provisionierung

```text
Ansible
```

## Sandbox

```text
Podman rootless bevorzugt
Docker als unterstützter Adapter
```

## Git

```text
GitLab .226
```

## Tests

```text
pytest
pytest-asyncio
Hypothesis für geeignete Contract-/State-Machine-Tests
Playwright für UI E2E
```

## Qualität

```text
Ruff
mypy
ESLint
TypeScript strict
```

---

# 6. NETZWERK- UND PORTPLAN

Vorgesehene interne Ports:

```text
.223:80      Nginx HTTP
.223:443     Nginx HTTPS

.225:8000    Hermclaw API intern
.225:4000    LiteLLM
.225:5432    PostgreSQL lokal/privat; nicht allgemein veröffentlichen
.225:9100    optional Metrics

.222:8787    Hermclaw Execution Worker API
.224:8787    Hermclaw Model/Media Worker API
.224:11434   Ollama

.226:22      Git SSH
.226:80/443  GitLab Web je vorhandener Installation
```

Ports werden durch Inventur gegen den Ist-Zustand geprüft.

Keine vorhandenen Dienste überschreiben.

---

# 7. NEUES REPOSITORY

Name:

```text
hermclaw-next
```

Struktur:

```text
hermclaw-next/
├── README.md
├── BUILD_PLAN.md
├── STATUS.md
├── BUGS.md
├── DECISIONS.md
├── RESEARCH_INDEX.md
├── TEST_MATRIX.md
├── RISK_REGISTER.md
├── CHANGELOG.md
├── pyproject.toml
├── alembic.ini
├── .env.example
├── config/
│   ├── hosts.example.yaml
│   ├── models.example.yaml
│   ├── policies.example.yaml
│   ├── capabilities.example.yaml
│   └── logging.example.yaml
├── hermclaw/
│   ├── api/
│   ├── auth/
│   ├── runtime/
│   ├── scheduler/
│   ├── planner/
│   ├── contracts/
│   ├── persistence/
│   ├── events/
│   ├── repo_intelligence/
│   ├── context_builder/
│   ├── tools/
│   ├── workers/
│   ├── models/
│   ├── verifier/
│   ├── review/
│   ├── research/
│   ├── resources/
│   ├── gitops/
│   ├── wol/
│   ├── security/
│   ├── artifacts/
│   └── telemetry/
├── worker/
│   ├── execution/
│   ├── model/
│   └── media/
├── ui/
│   ├── src/
│   ├── tests/
│   └── package.json
├── migrations/
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── failure/
│   ├── e2e/
│   └── fixtures/
├── infra/
│   ├── ansible/
│   │   ├── inventory/
│   │   ├── playbooks/
│   │   └── roles/
│   ├── systemd/
│   ├── nginx/
│   ├── podman/
│   └── backup/
├── scripts/
└── docs/
    ├── architecture/
    ├── contracts/
    ├── operations/
    ├── research/
    ├── testing/
    └── runbooks/
```

---

# 8. CLAUDE-CODE BUILD CONTROL FILES

Claude muss diese Dateien laufend pflegen.

## `BUILD_PLAN.md`

Enthält jede Phase und jeden Einzelschritt als Checkbox.

Nichts darf übersprungen werden.

## `STATUS.md`

Enthält:

- aktuelle Phase
- letzter abgeschlossener Schritt
- nächster Schritt
- Blocker
- aktive Bugs
- letzte Testresultate
- Deploymentstatus pro Host

## `BUGS.md`

Jeder Bug:

```text
ID
Zeit
Phase
Severity
Komponente
Reproduktionsschritte
Expected
Actual
Logs/Evidence
Workaround
Blocking yes/no
Status
Regression Test
Fix Commit
```

## `DECISIONS.md`

Nur Entscheidungen, die dieser Plan offen lässt.

Claude darf dort keine Kernarchitektur umdefinieren.

## `RESEARCH_INDEX.md`

Index aller Research-Berichte.

## `TEST_MATRIX.md`

Alle Tests + Status + Umgebung.

## `RISK_REGISTER.md`

Risiken + Mitigation.

---

# 9. RESEARCH-FIRST-PROTOKOLL

Vor jeder externen Integration recherchiert Claude.

Primärquellen bevorzugen:

- offizielle Projektdokumentation
- offizielle API-Dokumentation
- offizielle GitHub-Repositories
- Herstellerdokumentation
- RFC/Standards

Sekundärquellen nur ergänzend.

Für jedes Research-Thema:

```text
docs/research/YYYYMMDD-NNN-topic.md
```

Schema:

```text
Question
Why needed
Sources
Source date/version
Relevant facts
Compatibility with our hardware
Compatibility with our versions
Rejected alternatives
Decision fixed by architecture
Implementation consequences
Open risks
```

Research-Pflicht vor mindestens:

1. Debian-Version/Packages.
2. Python/FastAPI.
3. PostgreSQL/pgvector.
4. Alembic.
5. rootless Podman.
6. LiteLLM.
7. Ollama.
8. Gemma 4 26B A4B.
9. Qwen3-Coder 30B.
10. Qwen3 8B.
11. Qwen3.8 27B.
12. EmbeddingGemma 2.
13. Nginx/SSE.
14. GitLab Branch/MR APIs.
15. Wake-on-LAN.
16. NVIDIA GTX1080 driver/runtime.
17. React/Vite.
18. Playwright.
19. systemd credentials.
20. backup mechanism.
21. any API used for web research.

Research wird nicht benutzt, um die Architektur eigenmächtig zu ändern.

---

# 10. PERSISTENZMODELL

PostgreSQL ist Source of Truth.

Tabellen mindestens:

```text
jobs
job_inputs
steps
step_dependencies
step_attempts
events
workers
worker_capabilities
worker_health
model_profiles
model_invocations
resource_leases
repositories
workspaces
artifacts
plans
plan_versions
scope_contracts
tool_calls
command_runs
test_runs
verification_runs
verification_checks
review_runs
review_findings
research_runs
research_sources
research_claims
git_operations
deployments
wake_events
bug_records
memory_entries   # zunächst deaktiviert
```

Jede Tabelle bekommt:

- UUID/ID
- created_at
- updated_at wo sinnvoll
- Foreign Keys
- Indizes
- Constraints

Keine kritischen Retrycounter nur im RAM.

---

# 11. JOB STATE MACHINE

Status:

```text
queued
inventory
discovering
researching
planning
waiting_for_resources
waiting_for_worker
waking_worker
running
testing
verifying
reviewing
correcting
replanning
waiting_for_user
blocked
committing
deploying
succeeded
failed
cancelled
```

Jeder Transition ist deterministisch erlaubt oder verboten.

Ungültige Transition:

```text
hard error + Event
```

Property-Based Tests prüfen State Machine.

---

# 12. STEP STATE MACHINE

Status:

```text
pending
ready
leased
running
checkpointed
testing
verifying
reviewing
completed
failed
blocked
cancelled
```

Step-Kinds:

```text
inventory
discover
research
plan
implement
test
verify
review
replan
ssh
database
docker
deploy
documentation
image
video
```

---

# 13. EVENT- UND AUDIT-SYSTEM

Alle Aktionen werden nachvollziehbar.

Event enthält:

```text
event_id
sequence
timestamp
job_id
step_id
attempt_id
source_type
source_idevent_type
severity
payload
correlation_id
duration_ms optional
```

Beispiele:

```text
job.created
repo.inventory.started
repo.search.executed
research.query.started
research.source.read
research.claim.created
planner.invoked
planner.plan.created
scope.created
resource.requested
worker.wake.sent
worker.ready
model.load.started
model.invocation.started
tool.call.started
tool.call.finished
file.changed
test.started
test.failed
verifier.check.failed
review.finding.created
stagnation.detected
strategy.changed
git.commit.created
deployment.started
job.succeeded
```

Keine Chain-of-Thought.

Aber UI zeigt:

- was passiert
- warum auf High-Level
- welcher Worker
- welches Modell
- welches Tool
- welche Datei
- welches Ergebnis
- welche Quelle

---

# 14. REPOSITORY INTELLIGENCE

Kein statischer Benchmark-Scanner.

## Phase A – deterministic inventory

Erfassen:

- Files
- Größen
- Sprache
- Buildsystem
- Package Manager
- Tests
- Docker
- CI
- Migrationen
- Konfiguration
- Entry Points
- Routes
- README
- branch/head/status

## Phase B – lexical search

- ripgrep
- filenames
- symbols

## Phase C – structural index

- Python AST
- JS/TS Parser
- PHP parser/tree-sitter
- Imports
- function/class references
- routes
- database schema/migrations

## Phase D – semantic retrieval

EmbeddingGemma 2 + pgvector.

Index chunks:

```text
file
symbol
line range
language
embedding
git sha
```

## Phase E – relevance fusion

Ranking aus:

```text
lexical score
symbol score
structural score
semantic score
test-reference score
dependency score
```

## Phase F – context selection

Nur relevante Inhalte in LLM-Kontext.

---

# 15. GEMMA PLANNER

Planner Input:

```json
{
  "job": {},
  "repository_inventory": {},
  "retrieved_context": [],
  "research_summary": {},
  "capabilities": [],
  "constraints": [],
  "existing_tests": [],
  "risk_policy": {}
}
```

Planner Output:

```json
{
  "goal": "...",
  "summary": "...",
  "assumptions": [],
  "risks": [],
  "research_needed": [],
  "steps": [
    {
      "id": "S001",
      "title": "...",
      "kind": "implement",
      "capability": "coding",
      "goal": "...",
      "depends_on": [],
      "repo_hints": [],
      "constraints": [],
      "acceptance": [],
      "preferred_worker_capabilities": [],
      "risk": "low"
    }
  ]
}
```

Output wird via Pydantic/JSON Schema validiert.

Ungültiger Plan:

1. Validation-Fehler an Gemma zurück.
2. maximal 2 strukturierte Repair-Versuche.
3. danach Planner Error.

Keine 60-Turn-Schleife.

Planversionen persistent speichern.

---

# 16. REPLANNING

Auslöser:

- Scope unavailable
- Stagnation
- Test architecture conflict
- missing dependency
- worker unavailable
- research changes assumptions
- repeated verifier failure
- repository changed

Gemma erhält:

- Original goal
- current plan
- completed steps
- failed step
- deterministic evidence
- current repo state
- research evidence

Replanner darf abgeschlossene erfolgreiche Schritte nicht ohne Grund neu ausführen.

---

# 17. SCOPE CONTRACT

Jeder mutierende Step benötigt:

```json
{
  "source": "planner_and_repo_intelligence",
  "strict_target_paths": true,
  "target_paths": [],
  "allowed_new_paths": [],
  "forbidden_paths": [],
  "allowed_operations": [
    "create",
    "modify",
    "delete"
  ]
}
```

Scope wird von Runtime erzeugt/validiert.

Worker darf Scope nicht erweitern.

Scope-Erweiterung:

```text
worker requests scope expansion
-> repository intelligence
-> planner/replanner if semantic
-> runtime validates
-> new scope version
```

Kein stiller Zugriff.

---

# 18. CONTEXT BUILDER

Persistenter State -> frischer Kontext pro Turn.

Context Sections:

```text
SYSTEM CONTRACT
STEP GOAL
SCOPE
CONSTRAINTS
ACCEPTANCE
CURRENT REPO FACTS
RELEVANT CODE
RELEVANT TESTS
CURRENT DIFF
LATEST FAILURE
SHORT STEP HISTORY
AVAILABLE TOOLS
COMPLETION CONDITIONS
```

Budgetmanagement:

- feste Tokenbudgets pro Section
- Relevance ranking
- deduplication
- summary of old tool events
- preserve current failure exactly

Nie:

- gesamten Chat anhängen
- gesamtes Repo anhängen

---

# 19. CODER TOOL LOOP

Max Turns pro Implementierungsstep:

```text
20
```

Typischer Ablauf:

1. inspect relevant file
2. inspect tests
3. implement
4. run targeted test
5. inspect failure
6. correct
7. run targeted test
8. run required broader tests
9. inspect diff
10. `complete_step`

Tools:

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

`complete_step` existiert real.

---

# 20. STAGNATION DETECTION

Fingerprints:

```text
same failing test
same error signature
same changed files
same diff hash
same tool sequence
same decision label
```

Schwellen:

- 2 ähnliche Wiederholungen -> Warning
- 3 -> forced diagnosis
- 4 -> stop strategy
- replan/research/review

Keine 60 identischen Runden.

---

# 21. DETERMINISTIC VERIFIER

Evidence types:

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

`absence` ist First-Class.

Beispiel:

```json
{
  "type": "absence",
  "path_glob": "hermclaw/**/*.py",
  "pattern": "OLD_SPECIAL_CASE",
  "expected_matches": 0
}
```

Verifier prüft:

- target scope
- forbidden paths
- syntax
- compile
- lint
- unit tests
- integration tests
- contract tests
- secret scan
- conflict markers
- generated files
- changed file count
- deleted file policy
- test evidence

Verifier kennt keine fachlichen Benchmarkregeln.

---

# 22. REVIEW

Qwen3.8 27B erhält:

- goal
- plan step
- scope
- diff
- verifier report
- relevant code/tests

Review Output:

```json
{
  "verdict": "pass|fix_required",
  "findings": [
    {
      "severity": "minor|major|blocker",
      "path": "...",
      "summary": "...",
      "evidence": "...",
      "suggested_fix": "..."
    }
  ]
}
```

Deterministische Invariant:

```text
major/blocker -> verdict cannot be pass
```

---

# 23. RESEARCH ENGINE

Research ist eine eigene Pipeline.

## Schritte

1. Research question erzeugen.
2. Search queries planen.
3. Web suchen.
4. Primärquellen priorisieren.
5. Quellen abrufen.
6. Inhalt extrahieren.
7. Claims erzeugen.
8. Claims Quellen zuordnen.
9. Widersprüche erkennen.
10. Aktualität prüfen.
11. Gemma/Qwen Synthese.
12. Research Result speichern.

Source Record:

```text
source_id
title
url
domain
retrieved_at
published_at optional
source_type
authority_score
relevance_score
content_hash
```

Claim:

```text
claim
source_ids
confidence
used_for_decision
```

UI:

```text
"Research sucht..."
"Research liest..."
"Quelle verwendet für..."
```

Keine unsichtbaren Quellen.

---

# 24. WORKER REGISTRY

Worker Daemon auf `.222` und `.224`.

Heartbeat enthält:

```text
worker_id
hostname
state
capabilities
cpu
ram
disk
gpu
loaded_models
active_job
active_step
uptime
service_versions
```

States:

```text
offline
starting
ready
busy
draining
sleeping
waking
error
```

---

# 25. RESOURCE LEASES

PostgreSQL Tabelle.

Lease:

```text
resource
owner_job
owner_step
priority
acquired_at
heartbeat_at
expires_at
preemptible
```

Resource Groups:

```text
gpu-224
large-model-224
video-224
code-executor-222
```

Lease Recovery nach Crash.

---

# 26. WAKE-ON-LAN

Runtime API:

```text
ensure_worker_ready(worker_id)
```

Ablauf vollständig als Events.

Fehler:

```text
WOL_SEND_FAILED
PING_TIMEOUT
SSH_TIMEOUT
WORKER_API_TIMEOUT
MODEL_SERVICE_TIMEOUT
CAPABILITY_MISSING
```

Retry policy begrenzt.

---

# 27. GIT ENGINE

Runtime-controlled.

Operationen:

```text
clone
fetch
resolve_base_sha
create_workspace
create_job_branch
status
diff
stage_allowed
commit_verified
push_job_branch
create_merge_request optional
cleanup
```

Jeder Git-Write erzeugt Audit Event.

LLM kann nur Git Read Tools nutzen.

---

# 28. EXECUTION SANDBOX `.222`

Rootless Podman.

Je Step Container:

```text
workspace bind mount
resource limits
timeout
network policy
environment allowlist
read-only system
```

Default Network:

```text
off
```

Network nur bei Step Capability.

Host SSH nur über dediziertes Tool.

---

# 29. SSH / SERVER ADMINISTRATION

Kein freies SSH via Coder Shell.

Runtime Tool:

```text
ssh_read
ssh_command
ssh_upload
ssh_service_status
```

Policy:

- host allowlist
- command classification
- sudo policy
- timeout
- audit log

Mutierende produktive Commands können separate Approval Policy erhalten.

---

# 30. DATABASE TOOLING

Tools:

```text
db_introspect
db_query_readonly
db_migration_generate
db_migration_apply_sandbox
db_backup
db_restore_test
```

Keine unkontrollierten Produktions-DB-Mutationen.

---

# 31. DOCKER / REVERSE PROXY

Tools:

```text
container_list
container_inspect
compose_validate
compose_up_sandbox
nginx_test
proxy_config_validate
```

Vor Deployment:

```text
syntax
port collision
existing service inventory
rollback config
```

---

# 32. MEDIA PIPELINE

Image/Video Jobs sind eigene Step-Kinds.

Video:

1. acquire video lease
2. drain AI workload
3. unload model if necessary
4. run video
5. save artifacts
6. release GPU
7. AI workloads resume

Keine Änderung an bestehender Videoverarbeitung ohne expliziten Media-Auftrag.

---

# 33. WEB UI

## Dashboard

- Jobs
- Queues
- Worker
- Model status
- GPU
- Resource leases
- Research
- Bugs
- errors

## Job Detail

Tabs:

```text
Overview
Plan
Steps
Live Events
Repository
Changes
Tests
Verifier
Review
Research Sources
Resources
Artifacts
Git
Deployment
Errors
```

## Plan View

DAG visualisieren.

## Live Status

Keine CoT.

Beispiel:

```text
Repository wird inventarisiert
Gemma erstellt Plan
Research liest offizielle Dokumentation
Coder bearbeitet src/api.py
Test fehlgeschlagen
Coder korrigiert
Verifier prüft 12 Checks
Reviewer meldet Major Finding
Correction Step gestartet
```

---

# 34. API

Mindestens:

```text
POST /api/jobs
GET /api/jobs
GET /api/jobs/{id}
POST /api/jobs/{id}/cancel
POST /api/jobs/{id}/pause
POST /api/jobs/{id}/resume
POST /api/jobs/{id}/retry
POST /api/jobs/{id}/replan
GET /api/jobs/{id}/events
GET /api/jobs/{id}/artifacts
GET /api/workers
GET /api/models
GET /api/resources
GET /api/health
```

SSE:

```text
GET /api/jobs/{id}/events/stream
```

---

# 35. SECURITY

## Secrets

Nicht in:

- Git
- Prompt
- Event Payload
- Logs
- Artifacts

Verwenden:

```text
systemd credentials
/etc/hermclaw/secrets
secret references
```

## Worker Authentication

- pro Worker eigener Token/credential
- TLS intern soweit praktikabel
- Credentials rotierbar

## Git SSH

- dedizierte Keys
- restricted permissions

## Sandbox

- rootless
- no host root
- network default off

---

# 36. BACKUP

`.60` Terra/Unraid als Backupziel.

Sichern:

- PostgreSQL dump
- config
- artifacts
- research records
- deployment manifests

Git-Repositories bleiben zusätzlich in GitLab.

Backup Test:

```text
restore into clean temporary environment
```

Nicht nur Backup erzeugen; Restore prüfen.

---

# 37. OBSERVABILITY

Structured JSON Logs.

Metrics:

- job duration
- step duration
- model latency
- tokens
- tool calls
- test pass/fail
- retries
- stagnation
- worker uptime
- GPU utilization
- RAM
- model load time
- queue length
- research source count

Health endpoints.

Optional Prometheus Adapter.

---

# 38. BUG-STRATEGIE

Nicht-blockierende Bugs werden während des Baus gesammelt.

Severity:

```text
P0 data loss/security
P1 core functionality broken
P2 important defect
P3 minor/usability
```

Regel:

```text
P0/P1 blocker -> sofort fixen
P2/P3 non-blocking -> BUGS.md + Regression-Idee -> weiterbauen
```

Am Ende:

```text
FINAL HARDENING / BUG BASH
```

Alle reproduzierbaren offenen Bugs werden erneut geprüft.

Release darf keine offenen P0/P1 haben.

Ziel: alle reproduzierbaren P2 ebenfalls schließen.

---

# 39. TESTSTRATEGIE

## Unit

jede reine Komponente.

## Contract

Pydantic/JSON Verträge.

## Integration

DB, Git, models, workers.

## Failure

Timeouts, Worker down, model down, invalid JSON.

## Recovery

Runtime kill/restart.

## Concurrency

mehrere Jobs/Leases.

## Security

scope, secrets, sandbox.

## UI

Playwright.

## E2E

volle Jobläufe.

---

# 40. GENERALIZATION E2E MATRIX

Mindestens zehn unabhängige Fixture-Repositories:

1. Python FastAPI Feature.
2. SQLite Schema Migration.
3. PostgreSQL Migration.
4. Docker Compose Service.
5. React/CSS Navigation.
6. PHP Backend Form.
7. TypeScript Utility.
8. YAML-only Config.
9. Shell/Linux Admin.
10. Research-only Task.

Zusätzlich Systemjobs:

11. Image Job.12. Video Job.
13. Wake-on-LAN Job.
14. Worker failure/recovery.
15. planner replan after failure.

Kein Fixture darf Runtime-Sondercode verursachen.

---

# 41. FAILURE-INJECTION

Simulieren:

- Planner timeout
- invalid Planner JSON
- Coder timeout
- Worker offline
- Ollama unavailable
- GitLab unavailable
- DB reconnect
- process crash
- full disk warning
- scope violation
- secret found
- merge conflict
- stale base SHA
- verifier crash
- reviewer timeout
- WOL timeout
- research source unavailable
- network failure
- video preemption

Jeder Fall braucht definiertes Verhalten.

---

# 42. BUILD-PHASEN – VOLLSTÄNDIG UND OHNE STOPP

Claude Code arbeitet diese Phasen nacheinander vollständig ab.

## PHASE 0 – Initiale Research- und Inventurphase

0.1 neues `hermclaw-next` Repo erstellen.  
0.2 Build-Control-Dateien anlegen.  
0.3 alte Architektur read-only inventarisieren.  
0.4 Host `.223` inventarisieren.  
0.5 Host `.222` inventarisieren.  
0.6 Host `.224` inventarisieren.  
0.7 Host `.225` inventarisieren.  
0.8 GitLab `.226` inventarisieren.  
0.9 Terra `.60` inventarisieren.  
0.10 Ports erfassen.  
0.11 Services erfassen.  
0.12 Hardware erfassen.  
0.13 SSH-Zugänge testen.  
0.14 WOL-Fähigkeit erfassen.  
0.15 Ollama-Version erfassen.  
0.16 vorhandene Modelle erfassen.  
0.17 LiteLLM-Version/Config erfassen.  
0.18 offizielle Modell-/API-Dokumentation recherchieren.  
0.19 Systemdiagramm erzeugen.  
0.20 Risiko-/Abhängigkeitsregister erzeugen.

## PHASE 1 – Clean System Bootstrap

1.1 Debian-Zielversion festhalten.  
1.2 `hermclaw` User.  
1.3 Verzeichnisse.  
1.4 SSH Keys.  
1.5 Python.  
1.6 venv/tooling.  
1.7 PostgreSQL.  
1.8 pgvector.  
1.9 Git.  
1.10 Podman.  
1.11 Node/Vite Buildtooling auf `.223`.  
1.12 Nginx auf `.223`.  
1.13 systemd units Skeleton.  
1.14 Ansible inventory/roles.  
1.15 reproducible bootstrap test.

## PHASE 2 – Backend Skeleton

2.1 Python package.  
2.2 settings.  
2.3 config loader.  
2.4 structured logging.  
2.5 error model.  
2.6 health endpoint.  
2.7 test framework.  
2.8 lint/type checking.  
2.9 CI base.  
2.10 version endpoint.

## PHASE 3 – Persistence

3.1 DB models.  
3.2 Alembic.  
3.3 all core tables.  
3.4 indexes/constraints.  
3.5 repositories.  
3.6 transaction boundary.  
3.7 migration tests.  
3.8 rollback tests.  
3.9 restart persistence test.

## PHASE 4 – Event Store

4.1 append-only event contract.  
4.2 event service.  
4.3 sequence ordering.  
4.4 correlation IDs.  
4.5 SSE endpoint.  
4.6 reconnect/last-event-id.  
4.7 event retention.  
4.8 UI test client.

## PHASE 5 – State Machines

5.1 Job states.  
5.2 Step states.  
5.3 transition tables.  
5.4 invalid transitions.  
5.5 property tests.  
5.6 recovery mapping.

## PHASE 6 – Git Engine

6.1 repository registry.  
6.2 clone/fetch.  
6.3 base SHA.  
6.4 isolated workspaces.  
6.5 job branches.  
6.6 status/diff.  
6.7 safe staging.  
6.8 Runtime commit.  
6.9 Runtime push.  
6.10 protected branch tests.  
6.11 stale base detection.  
6.12 conflict handling.

## PHASE 7 – Worker Protocol

7.1 Worker API schema.  
7.2 heartbeat.  
7.3 capability registry.  
7.4 health.  
7.5 worker auth.  
7.6 `.222` daemon.  
7.7 `.224` daemon.  
7.8 offline detection.  
7.9 version compatibility.

## PHASE 8 – Model Gateway

8.1 LiteLLM adapter.  
8.2 model profiles.  
8.3 Fast Qwen profile.  
8.4 Gemma Planner profile.  
8.5 Coder profile.  
8.6 Heavy profile.  
8.7 embedding profile.  
8.8 health checks.  
8.9 context validation.  
8.10 load/unload adapter.  
8.11 metrics.

## PHASE 9 – Resource Manager

9.1 lease table.  
9.2 acquisition.  
9.3 release.  
9.4 heartbeat/expiry.  
9.5 priority.  
9.6 safe preemption.  
9.7 large-model exclusivity.  
9.8 video priority.  
9.9 crash recovery.

## PHASE 10 – Wake-on-LAN

10.1 config.  
10.2 WOL send.  
10.3 ping wait.  
10.4 SSH wait.  
10.5 worker API wait.  
10.6 Ollama wait.  
10.7 failure states.  
10.8 UI events.  
10.9 idle sleep hooks.

## PHASE 11 – Repository Intelligence

11.1 inventory.  
11.2 language detection.  
11.3 build/test discovery.  
11.4 lexical search.  
11.5 symbol index.  
11.6 AST adapters.  
11.7 dependency relations.  
11.8 EmbeddingGemma index.  
11.9 pgvector store.  
11.10 fusion ranking.  
11.11 targeted read.  
11.12 incremental reindex by Git SHA.

## PHASE 12 – Research Engine

12.1 query planner.  
12.2 web search interface.  
12.3 HTTP/browser fetch.  
12.4 source records.  
12.5 content extraction.  
12.6 claim extraction.  
12.7 source-to-claim links.  
12.8 freshness.  
12.9 authority/relevance.  
12.10 contradiction detection.  
12.11 Qwen synth.  
12.12 Gemma deep synth.  
12.13 UI source stream.

## PHASE 13 – Contracts

13.1 JobContract.  
13.2 PlanContract.  
13.3 StepContract.  
13.4 ScopeContract.  
13.5 WorkerInput.  
13.6 WorkerResult.  
13.7 ToolCall.  
13.8 VerificationContract.  
13.9 ReviewContract.  
13.10 ResearchContract.  
13.11 ArtifactContract.  
13.12 JSON schema export.

## PHASE 14 – Gemma Planner

14.1 planner prompt contract.  
14.2 structured output.  
14.3 schema validation.  
14.4 plan repair.  
14.5 DAG validation.  
14.6 dependency validation.  
14.7 risk assignment.  
14.8 acceptance generation.  
14.9 research request generation.  
14.10 planner tests across unrelated repos.

## PHASE 15 – Scope Engine

15.1 planner hints intake.  
15.2 repo intelligence evidence.  
15.3 policy merge.  
15.4 scope generation.  
15.5 forbidden paths.  
15.6 scope versioning.  
15.7 expansion request.  
15.8 unavailable handling.  
15.9 scope audit.

## PHASE 16 – Context Builder

16.1 context sections.  
16.2 token budgeting.  
16.3 relevance.  
16.4 deduplication.  
16.5 error preservation.  
16.6 tool summary.  
16.7 current diff.  
16.8 tests.  
16.9 context telemetry.

## PHASE 17 – Tool Engine

17.1 list/read/find/search.  
17.2 git read.  
17.3 file writes.  
17.4 patch.  
17.5 commands.  
17.6 tests.  
17.7 research request.  
17.8 scope request.  
17.9 replan request.  
17.10 checkpoint.  
17.11 complete_step.  
17.12 block_step.  
17.13 policy enforcement.

## PHASE 18 – Execution Sandbox

18.1 rootless Podman.  
18.2 images.  
18.3 mounts.  
18.4 resource limits.  
18.5 timeouts.  
18.6 network-off default.  
18.7 allowed network mode.  
18.8 cleanup.  
18.9 abandoned container recovery.

## PHASE 19 – Main Coder

19.1 worker loop.  
19.2 Qwen3-Coder 30B.  
19.3 32K context.  
19.4 tool protocol.  
19.5 completion.  
19.6 checkpoints.  
19.7 result schema.  
19.8 code task regression matrix.

## PHASE 20 – Stagnation Detection

20.1 action fingerprints.  
20.2 error fingerprints.  
20.3 diff progress.  
20.4 thresholds.  
20.5 forced diagnose.  
20.6 strategy switch.  
20.7 replan.  
20.8 stop conditions.

## PHASE 21 – Deterministic Verifier

21.1 scope.  
21.2 syntax.  
21.3 compile.  
21.4 lint.  
21.5 unit.  
21.6 integration.  
21.7 secrets.  
21.8 conflicts.  
21.9 presence evidence.  
21.10 absence evidence.  
21.11 command evidence.  
21.12 test evidence.  
21.13 diff evidence.  
21.14 report.

## PHASE 22 – Heavy Review

22.1 review prompt.  
22.2 Qwen3.8 profile.  
22.3 structured findings.  
22.4 severity.  
22.5 major/blocker invariant.  
22.6 correction request.

## PHASE 23 – Correction Pipeline

23.1 verifier failure correction.  
23.2 review correction.  
23.3 bounded attempts.  
23.4 evidence passed forward.  
23.5 regression rerun.  
23.6 escalation.

## PHASE 24 – Replanning

24.1 failure package.  
24.2 Gemma replan.  
24.3 plan versioning.  
24.4 completed-step preservation.  
24.5 new dependencies.  
24.6 scope refresh.

## PHASE 25 – Scheduler

25.1 DAG scheduler.  
25.2 ready steps.  
25.3 capabilities.  
25.4 resource leases.  
25.5 worker dispatch.  
25.6 retries.  
25.7 timeouts.  
25.8 parallel steps.  
25.9 dependency failure.

## PHASE 26 – SSH/Admin Tools

26.1 host registry.  
26.2 SSH keys.  
26.3 command policy.  
26.4 read commands.  
26.5 mutation commands.  
26.6 audit.  
26.7 timeout.  
26.8 sandbox vs host separation.

## PHASE 27 – DB Tools

27.1 introspection.  
27.2 readonly query.  
27.3 migrations.  
27.4 sandbox apply.  
27.5 backup.  
27.6 restore test.  
27.7 policy.

## PHASE 28 – Container/Proxy Tools

28.1 inspect.  
28.2 compose validate.  
28.3 sandbox startup.  
28.4 port collision.  
28.5 Nginx test.  
28.6 rollback artifact.

## PHASE 29 – Media

29.1 image step.  
29.2 video step.  
29.3 GPU leases.  
29.4 safe AI drain.  
29.5 model unload.  
29.6 artifacts.  
29.7 resume AI.

## PHASE 30 – API

30.1 job API.  
30.2 controls.  
30.3 workers.  
30.4 models.  
30.5 resources.  
30.6 artifacts.  
30.7 events.  
30.8 research.  
30.9 auth.

## PHASE 31 – UI

31.1 layout.  
31.2 dashboard.  
31.3 jobs.  
31.4 DAG.  
31.5 live events.  
31.6 diff.  
31.7 tests.  
31.8 verifier.  
31.9 review.  
31.10 sources.  
31.11 workers.  
31.12 resources.  
31.13 media.  
31.14 errors.  
31.15 controls.

## PHASE 32 – Deployment

32.1 `.225` runtime systemd.  
32.2 `.223` UI.  
32.3 `.222` worker.  
32.4 `.224` worker.  
32.5 LiteLLM.  
32.6 Ollama models.  
32.7 Nginx.  
32.8 secrets.  
32.9 health checks.  
32.10 Ansible idempotency.

## PHASE 33 – Backup/Restore

33.1 PostgreSQL backup.  
33.2 config backup.  
33.3 artifacts.  
33.4 `.60` target.  
33.5 restore environment.  
33.6 restore verification.

## PHASE 34 – Recovery

34.1 kill runtime during job.  
34.2 restart.  
34.3 reconstruct.  
34.4 leased worker recovery.  
34.5 model load recovery.  
34.6 workspace recovery.  
34.7 Git state recovery.

## PHASE 35 – Failure Injection

Alle Fälle aus Abschnitt 41.

## PHASE 36 – Security Hardening

36.1 secret scan.  
36.2 permissions.  
36.3 worker auth.  
36.4 SSH restrictions.  
36.5 command policy.  
36.6 sandbox escape checks.  
36.7 network policy.  
36.8 dependency audit.

## PHASE 37 – Generalization E2E

Alle Fixture-Klassen aus Abschnitt 40.

Keine Runtime-Sonderpatches zulässig.

## PHASE 38 – Performance / Load

38.1 concurrent jobs.  
38.2 event volume.  
38.3 DB load.  
38.4 context build timings.  
38.5 model load/unload.  
38.6 worker wake.  
38.7 video preemption.  
38.8 memory usage.

## PHASE 39 – FULL BUG BASH / HARDENING

39.1 `BUGS.md` vollständig reviewen.  
39.2 jeden offenen Bug reproduzieren.  
39.3 stale Bugs schließen mit Beleg.  
39.4 P0 fixen.  
39.5 P1 fixen.  
39.6 P2 fixen.  
39.7 P3 soweit reproduzierbar und sinnvoll fixen.  
39.8 für jeden Fix Regressiontest.  
39.9 komplette Testmatrix neu.  
39.10 Failure Tests neu.  
39.11 Recovery neu.  
39.12 Security neu.  
39.13 E2E neu.

## PHASE 40 – SOAK TEST

40.1 System vollständig starten.  
40.2 mehrere reale Jobs.  
40.3 24h/geeigneter Dauerbetrieb soweit Umgebung erlaubt.  
40.4 Ressourcen prüfen.  
40.5 Leaks.  
40.6 stuck leases.  
40.7 worker reconnect.  
40.8 event ordering.  
40.9 DB integrity.

## PHASE 41 – RELEASE CANDIDATE

41.1 Version.  
41.2 Changelog.  
41.3 Architektur-Doku.  
41.4 Admin Runbook.  
41.5 Disaster Recovery.  
41.6 Upgrade Path.  
41.7 known limitations.  
41.8 all tests final.  
41.9 release tag.

## PHASE 42 – CUTOVER PLAN

42.1 altes Hermclaw read-only.  
42.2 keine Daten löschen.  
42.3 neues System produktiv.  
42.4 Rollback dokumentieren.  
42.5 Monitoring.  
42.6 erst nach stabiler Laufzeit Altkomponenten stilllegen.

---

# 43. REGEL FÜR BUGS WÄHREND DES BAUS

Claude Code soll nicht bei jedem kleinen Bug den gesamten Bau unterbrechen.

Wenn Bug nicht blockiert:

1. reproduzieren.
2. in `BUGS.md`.
3. Evidence sichern.
4. Regressiontest-Idee notieren.
5. weiterbauen.

Wenn Bug blockiert:

1. minimalen Fix.
2. Test.
3. weiterbauen.

Finale umfassende Reparatur:

```text
PHASE 39
```

---

# 44. REGEL FÜR CLAUDE CODE – NICHT SELBST UMPLANEN

Claude darf:

- Implementierungsdetails innerhalb der festgelegten Architektur auswählen.
- offizielle aktuelle API-Syntax recherchieren.
- Bugfix-Details wählen.
- interne Klassennamen sinnvoll wählen.

Claude darf nicht:

- Gemma als Planner ersetzen.
- Qwen Coder als Main-Rolle entfernen.
- Hostrollen neu verteilen.
- PostgreSQL durch anderes State Backend ersetzen.
- Event Store weglassen.
- Research weglassen.
- Verifier weglassen.
- Scope Engine weglassen.
- Worker Registry weglassen.
- WOL weglassen.
- Media Priority weglassen.
- Stagnation Detection weglassen.
- Bugphase weglassen.
- E2E/Failure/Recovery Tests weglassen.
- Securityphase weglassen.
- UI weglassen.
- GitLab Integration weglassen.

Wenn eine Vorgabe technisch aktuell nicht umsetzbar ist:

```text
BLOCKER dokumentieren
offizielle Quellen sammeln
technischen Grund beweisen
alle unabhängigen Phasen weiterbauen
keine eigenmächtige Ersatzarchitektur
```

---

# 45. ENDGÜLTIGE DEFINITION OF DONE

Fertig erst wenn:

- alle 42 Phasen abgeschlossen oder formal belegter externer Blocker
- alle Checkboxen nachvollziehbar
- Planner Gemma integriert
- Research Engine produktiv
- Quellen sichtbar
- Qwen Coder integriert
- Heavy Review integriert
- Embedding Retrieval integriert
- Worker `.222` und `.224` integriert
- UI `.223`
- Runtime `.225`
- GitLab `.226`
- Backups `.60`
- WOL
- video priority
- explicit scope
- deterministic verifier
- negative evidence
- stagnation handling
- recovery
- failure injection
- security
- E2E
- Bug Bash
- Release Candidate
- Dokumentation

Keine offenen P0/P1 Bugs.

Keine bekannte Datenverlust-Regressionslücke.

Keine benchmark-spezifische Produktionslogik.