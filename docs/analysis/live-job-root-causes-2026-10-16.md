# Ursachenanalyse: Warum der erste autonome Programmierauftrag nicht abschließt

Stand: 2026-10-16. Grundlage sind alle acht Live-Jobs des Testauftrags „Füge einen /health-Endpunkt hinzu, der
`{"status": "ok"}` liefert, mit Test" auf `local-ai/hermclaw-test`, einem leeren Repository mit nur `README.md`.
Ausgewertet wurden die Tabellen `plan_versions`, `steps`, `tool_calls`, `command_runs`, `research_runs`,
`verification_runs`/`verification_checks` und `events`.

Die Runtime-Fixes BUG-143 bis BUG-165 haben jeweils das letzte sichtbare Symptom behoben. Diese Analyse sucht die
**früheste vermeidbare Entscheidung** je Job und die gemeinsamen Ursachen dahinter.

## 1. Überblick der Jobs

| Job | Version | Ergebnis | früheste vermeidbare Entscheidung | sichtbares Ende |
|---|---|---|---|---|
| `4af00159` | rc1 | Planner ungültig | – (Schema-/Validierungsfehler, BUG-143 ff.) | `PLANNER_INVALID_OUTPUT` |
| `eadf1b6f` | rc2 | Planner ungültig | Web-Recherche „Sprache dieses Repos?“ → ASP.NET/Spring | Replan ungültig |
| `c04f9fb8` | rc3 | Planner ungültig | dto. (Go/iris, ASP.NET) | Replan ohne Scope-Felder |
| `bec4c5e9` | rc4 | `REPLAN_LIMIT` | Recherche empfiehlt FastAPI + `rest-health` → Framework | Proxy-Timeout, pytest-Pin |
| `b0080372` | rc5 | `REPLAN_LIMIT` | Recherche „bevorzugtes Framework“ → FastAPI | `httpx` fehlt, `pip install` im Befehl |
| `e3cab55e` | rc6 | `REPLAN_LIMIT` | Recherche „Repo nutzt vanillajs“ → Node-Server | **fertiger Step vom Verifier fälschlich abgelehnt** |
| `837dcd50` | rc7 | `REPLAN_LIMIT` | FastAPI; Code und Test in getrennten Steps | `httpx` fehlt, erledigter Step nie abgeschlossen |
| `0a98a5eb` | rc8 | `REPLAN_LIMIT` | Coder recherchiert „populärste Frameworks“ → FastAPI | `httpx` fehlt, eigenes Paket `app` nicht importierbar |

Kennzahlen der Werkzeugaufrufe (rc2–rc8, aus `tool_calls`):

| Job | Aufrufe | nicht ok | install_dependencies | run_test/run_command | Edits | complete_step | Minuten in Werkzeugen |
|---|---|---|---|---|---|---|---|
| rc4 | 51 | 24 | 12 | 28 | 5 | 0 | 14 |
| rc5 | 74 | 46 | 2 | 57 | 10 | 0 | 4 |
| rc6 | 53 | 26 | 0 | 31 | 10 | **1** | 151 |
| rc7 | 70 | 35 | 20 | 26 | 12 | 0 | 3 |
| rc8 | 45 | 21 | 6 | 23 | 5 | 0 | 11 |

In acht Jobs wurde **kein einziger Umsetzungs-Step erfolgreich abgeschlossen**. Der einzige `complete_step`-Aufruf
(rc6) scheiterte am Verifier (Ursache U4).

## 2. Ursachen (nach Reihenfolge im Ablauf)

### U1 – Web-Recherche für Fragen, die das Repository beantwortet (früheste Entscheidung, alle Jobs)
- **Beleg:** Jeder Erstplan enthält einen bis zwei Research-Steps: „What is the programming language and web framework
  used in this repository?“, „What is the preferred language/framework for this project?“. Die Runtime erzeugt sie
  selbst aus `research_needed` (`planner.plan.created`: „research step S006 generated from research_needed“).
  SearXNG beantwortet sie mit Fremdprojekten:
  - rc1: „Next.js … Ruby … Rust“;
  - rc3/rc7: „Go … iris“;
  - rc6: „The repository uses JavaScript … vanillajs“.

  Die Suchanfragen enthielten unaufgelöste Platzhalter („Add JSON health endpoint in [framework] with test“).
- **Folge:** Die Technologiewahl folgt Zufallsfunden. In rc6 führte „vanillajs“ zu einem Node-Server, in
  rc4/rc5/rc8 die Empfehlung „FastAPI (+ rest-health)“ zu einem Framework mit Zusatzabhängigkeiten.
- **Warum der Prompt nicht reicht:** Regel 11 des Planner-Prompts sagt bereits, dass Repository-Fakten aus
  `repository_inventory` kommen und ein leeres Repository noch keinen Stack hat. Gemma hält sich nicht daran, und
  nichts erzwingt die Regel.

### U2 – Keine Präferenz für die einfachste ausreichende Lösung (Technologiewahl)
- **Beleg:** In rc4, rc5, rc7 und rc8 wählten die Modelle FastAPI. Der Test mit `fastapi.testclient` braucht das
  nicht deklarierte `httpx`, und daran scheiterten drei Jobs (`ModuleNotFoundError: No module named 'httpx'`). In
  rc6 wählten sie einen Node-Server, der sich im Vordergrund nie beendet (BUG-164).
  Die Aufgabe ist mit der Standardbibliothek lösbar (`http.server` bzw. `node:http`) und mit `unittest` oder dem
  pytest des Images testbar, ganz ohne Installation.
- **Folge:** Jede externe Abhängigkeit bringt neue Fehlerquellen: Proxy, Pins, transitive Test-Abhängigkeiten,
  Installationsschleifen. Allein in den Jobs rc4–rc8 kamen 40 `install_dependencies`-Aufrufe zusammen.
- **Prompt-Lage:** Der Planner-Prompt sagt nur „Prefer the standard library and dependencies the repository already
  declares“ (bei den Abhängigkeitsregeln). Er sagt nicht, dass die einfachste vollständige Lösung zu wählen ist.
  Der Coder-Vertrag sagt dazu nichts.

### U3 – Code und sein Test in getrennten Steps (Planstruktur, alle Erstpläne)
- **Beleg:** Alle Erstpläne folgen dem Muster „Implement endpoint“ → „Implement test“ → „Verify“. Der
  Umsetzungs-Step kann nichts testen, weil der Test-Step noch nicht gelaufen ist. Er schreibt trotzdem Tests außerhalb
  seines Scopes (rc8: `app/test_main.py`) oder prüft per `python -c`. Umgekehrt darf der Test-Step den Code nicht
  korrigieren.
  - rc7: S007 sollte Abhängigkeiten ergänzen und tat das auch. Danach wartete der Step drei Versuche lang auf einen
    Assertion-Fehler im Test, den nur S009 beheben durfte (`replace_text` auf `tests/test_health.py`: Scope-Verletzung;
    Erweiterung „semantic“ abgelehnt).
- **Folge:** Übergaben zwischen Steps scheitern, und der Coder schließt nie ab. Das führt zu Stagnation und Replans.
  Die Replans wiederholen dasselbe Muster.

### U4 – Verifier lehnt erfüllte Steps ab: Akzeptanz-Globs mit Scope-Semantik (rc6, entscheidend)
- **Beleg:** rc6, S003, 15:23:21 – `complete_step` nach erfolgreicher Umsetzung (`src/app.js` angelegt). Der Verifier
  meldete „[diff] expected changes matching ['*'] but none were found“, obwohl `changed_files: ["src/app.js"]`
  vorlag. Der Planner hatte `must_change: ["*"]` geschrieben und meinte „irgendeine Änderung“. Nach Scope-Semantik
  passt `*` aber nur auf Dateien im Wurzelverzeichnis.
- **Folge:** Es folgte ein Korrekturversuch ohne lösbare Aufgabe, darin die Server-Schleife und schließlich
  `REPLAN_LIMIT`. Der einzige Erfolg des ganzen Testlaufs ging dadurch verloren.
- **Lücke:** Die Plan-Validierung prüft nicht, ob `must_change`-Globs überhaupt auf die Pfade des Steps passen können.

### U5 – Projekteigene Module im Sandbox-Test nicht importierbar (rc8)
- **Beleg:** rc8, S006: `pytest app/test_main.py` → `from app.main import app` → `ModuleNotFoundError: No module named
  'app'`. `pytest` ohne `python -m` setzt das Arbeitsverzeichnis nicht auf `sys.path`. `PYTHONPATH` enthält nur
  `/deps/python`, nicht das Repository.
- **Folge:** Ein korrekter Test scheitert je nach Aufrufform. Der Coder hält das für ein Abhängigkeitsproblem
  (`pip install -e .`, neue Pins).

### U6 – Wirkungslose Wiederholungen (Coder-Schleifen)
- **Beleg:**
  - rc5: 25-mal `pip install` im Befehl (BUG-162).
  - rc6: über 15-mal ein Server im Vordergrund, je 600 s (BUG-164).
  - rc7/rc8: `install_dependencies` mit unverändertem Manifest (BUG-165).
  - rc8: Recherche „populärste Frameworks“ dreimal, dann `request_replan`.

  Die Stagnationserkennung greift, aber erst nach mehreren teuren Wiederholungen.
- **Stand:** Die Runtime-Wächter aus BUG-162/164/165 wirken (rc8: `INSTALL_UNCHANGED` griff). Sie behandeln aber die
  Folgen von U1–U3, nicht die Ursache.

### U7 – Übergabe Coder → Verifier → Korrektur → Replanner
- Eine Korrektur nach einem Verifier-Fehler, den der Coder nicht beheben kann (U4: Defekt in der Akzeptanz), endet
  zwangsläufig in Stagnation. Ein solcher Fehler gehört an den Replanner, nicht in einen Korrekturversuch.
- Der Replanner wiederholt die Planstruktur (U3) und die Technologiewahl. Gescheiterte Steps hinterlassen
  uncommittete Dateien, die der nächste Plan erben muss (BUG-158/163).
- Verify-Steps (Kind `verify`) prüfen nur, was die Umsetzungs-Steps hätten selbst prüfen können. Bei getrennten
  Test-Steps kommen sie nie an die Reihe.

## 3. Warum die E2E-Tests das nicht gefunden haben
- `tests/e2e` spielt **ideale, geskriptete Modellantworten** ab (`ScenarioModels`):
  - ein Step je Änderung mit Code und Test zusammen;
  - exakte Pfade in `must_change`;
  - kein Research-Step;
  - Repositories mit vorhandenem Code und installierten Abhängigkeiten (FastAPI liegt im Test-venv);
  - ein unisolierter Executor.

  Keines der realen Modellmuster (U1–U5) kommt darin vor.
- Die Live-Abnahme prüft Modelle nur auf Erreichbarkeit und Antwortformat (`--suite models`), nicht auf Planqualität
  oder Abschluss eines echten Auftrags. Einen Live-Auftrag gibt es nur manuell.
- Es fehlen Regressionstests aus **aufgezeichneten echten Modellantworten**, etwa dem Plan aus rc6 mit
  `must_change: ["*"]` oder Research zu „this repository“.

## 4. Vorgesehene Änderungen (allgemein, nicht auf den Testauftrag zugeschnitten)

| # | Ursache | Änderung | Wo |
|---|---|---|---|
| A1 | U1 | Research-Fragen über das Repository/Projekt selbst und Fragen zur Wahl eines Frameworks sind ein Validierungsfehler mit Reparaturhinweis („aus repository_inventory beantworten bzw. als Annahme festlegen“). Das gilt für `research_needed` und für Steps des Kinds research, deterministisch und für jeden Auftrag. Auch der Coder-Vertrag beschränkt `request_research` auf externe Fakten. | `planner/validation.py`, Coder-Vertrag |
| A2 | U2 | Planner- und Coder-Regel „einfachste vollständige Lösung“: In einem Repository ohne Stack zuerst Standardbibliothek und vorhandene Test-Werkzeuge; Frameworks/Abhängigkeiten nur, wenn der Auftrag sie verlangt oder das Repository sie schon nutzt. Externe Abhängigkeiten werden als Annahme mit Begründung festgehalten. | `planner/prompt.py`, Coder-Vertrag |
| A3 | U3 | Validierung: Ein Step, der nur Tests schreibt, darf nicht von einem Umsetzungs-Step abhängen, der den getesteten Code anlegt. Code und Tests gehören in einen Step. Der Prompt fordert „ein Step je Änderung inklusive ihrer Tests“; Verify-Steps nur, wenn sie etwas zusätzlich prüfen. | `planner/validation.py`, `planner/prompt.py` |
| A4 | U4 | Plan-Validierung: Jeder `must_change`-Glob eines Steps muss auf einen seiner Pfade (`repo_hints`/`allowed_new_paths`) passen können, sonst Reparaturfehler. Verifier: Ein Akzeptanz-Glob ohne `/` gilt wie bei gitignore für den Dateinamen in jeder Tiefe (`*` = jede Änderung, `*.py` = jede Python-Datei). | `planner/validation.py`, `verifier/evidence.py` |
| A5 | U5 | Sandbox: Das Repository-Wurzelverzeichnis steht vor `/deps/python` auf `PYTHONPATH`, wie bei `python -m pytest` und in üblicher CI. | Sandbox-Kommandozeile (worker) |
| A6 | U7 | Ein Verifier-Fehler, den eine Korrektur nicht beheben kann (Akzeptanz passt auf keinen Pfad des Steps), geht direkt an den Replanner. | Verifier/Scheduler (nach A4 prüfen, ob noch nötig) |
| A7 | §3 | Regressionstests aus aufgezeichneten Live-Antworten (Plan rc6, Research-Fragen rc1–rc8); E2E-Szenario mit leerem Repository und realistisch fehlerhaftem Plan. | `tests/` |
| A8 | §3 | Neue Live-Abnahme-Suite `jobs`: eine Batterie verschiedener Aufträge gegen die echten Modelle mit messbaren Kriterien (§5). | `scripts/ops/live-acceptance.sh`, `tests/live/` |

Ausdrücklich **nicht** vorgesehen: Sonderfälle für `/health`, feste Dateinamen, Framework-Verbote oder auf den
Testauftrag zugeschnittene Prompts. Beispiele in Prompts bleiben generisch.

## 5. Erfolgskriterien (messbar)

Gemessen mit der Live-Suite `jobs` auf den echten Hosts. Jeder Auftrag läuft gegen ein frisches Test-Repository:

| Auftrag | Ausgangslage |
|---|---|
| J1 Endpunkt mit Test | leeres Repository |
| J2 Endpunkt mit Validierung und Fehlerfällen | bestehender Code (Ergebnis von J1) |
| J3 Konfiguration aus Umgebungsvariablen | Refactoring über mehrere Dateien |
| J4 unmögliche Abhängigkeit | muss sauber blockieren |
| J5 Node/TypeScript-Funktion mit Test | – |
| J6 Bugfix nur aus Fehlerbeschreibung | – |

| Kriterium | Ziel |
|---|---|
| Erfolgsquote J1–J3, J5, J6 | jeder Auftrag `succeeded`, Merge Request erstellt, Tests im MR grün |
| J4 | `blocked`/`waiting_for_user` mit benannter Ursache, < 30 min, keine Schleife |
| Laufzeit je erfolgreichem Auftrag | ≤ 45 min |
| Replans je Auftrag | ≤ 1 |
| Steps im Erstplan (einfacher Auftrag) | ≤ 3 |
| Web-Research zu Fragen über das Repository | 0 |
| neue externe Abhängigkeiten bei Aufträgen, die keine verlangen | 0 |
| Verifier-Ablehnungen eines erfüllten Steps (False Negatives) | 0 (manuelle Stichprobe aller Ablehnungen) |
| identische Werkzeugaufrufe ohne Workspace-Änderung | ≤ 2 je Step |
| Umsetzungs-Steps mit `complete_step` | 100 % der abgeschlossenen Umsetzungs-Steps |

Ein Release gilt erst als abgenommen, wenn die Suite die Kriterien in **zwei aufeinanderfolgenden Läufen** erfüllt.

## 6. Umsetzungsstand (0.1.0rc9)

| # | Stand | Belege |
|---|---|---|
| A1 | umgesetzt (BUG-168) | `hermclaw/research/questions.py`; Plan-Validierung; `request_research` → `RESEARCH_REFUSED`; Tests mit allen Live-Fragen und erlaubten externen Fragen |
| A2 | umgesetzt (BUG-169) | Planner-Regel „simplest solution“, Coder-Vertrag |
| A3 | umgesetzt (BUG-169) | Validierung „only writes tests for the code of step …“; Replans mit abgeschlossenem Code-Step ausgenommen |
| A4 | umgesetzt (BUG-166) | `acceptance_glob_matches` (gitignore-Konvention für Dateinamen-Globs); `must_change`-Prüfung im Plan |
| A5 | umgesetzt (BUG-167) | `PYTHONPATH=/workspace[:/deps/python]`; live auf .222 (`test_live_project_package_is_importable_with_plain_pytest`) |
| A6 | zurückgestellt | Nach A4 gibt es keinen bekannten Fall mehr. Die Batterie misst Verifier-Ablehnungen (`verifier_failures`); tritt ein unbehebbarer Fall auf, wird A6 umgesetzt. |
| A7 | umgesetzt | `test_planner_loop.py::test_live_first_plan_is_repaired_into_one_step_without_web_research` (Plan v1 aus rc6), Validierungstests mit den aufgezeichneten Fragen |
| A8 | umgesetzt | `hermclaw job-metrics`, `hermclaw acceptance-jobs`, `config/acceptance-jobs.example.yaml`, `tests/live/fixtures/`, `scripts/ops/acceptance-fixtures.sh` |

Gegenprobe: `hermclaw job-metrics` auf die Jobs rc6 und rc8 verfehlt erwartungsgemäß die Kriterien.
- rc6: Status, Dauer 223,7 min, 2 Replans, 6 Steps im Erstplan, 2/2 unbeantwortbare Recherchen, 7 Wiederholungen.
- rc8: dazu ein neu angelegtes `requirements.txt`.

Die Kennzahlen messen also genau die beobachteten Fehlerbilder.
