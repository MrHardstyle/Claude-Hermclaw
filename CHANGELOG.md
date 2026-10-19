# CHANGELOG

Format: [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionen nach PEP 440.

## [Unreleased]

### Behoben
- **llama.cpp-Archiv und Build passen zusammen (BUG-280):** Die Rolle `ollama` lehnt ein Archiv ab, dessen Name einen anderen Build nennt als `ollama_llamacpp_build`. Vorher lief nach einem Deploy unbemerkt der alte llama-server weiter.

## [0.1.0rc60] – Release Candidate (2026-10-19)

### Geändert
- **Planer auf eigenem Modell (R-033):** Der llama-server auf dem Modell-Host kann mehrere Modelle bedienen, eines nach dem anderen (Router-Modus, `ollama_llamacpp_models`, `--models-max 1`). Eine Anfrage an ein anderes Modell entlädt das geladene und lädt das angeforderte. Planer und Neuplaner halten dafür die Lease der exklusiven Modellgruppe, wie Coder und Review. Ein Planungsaufruf wechselt das Modell also nie unter einem laufenden Schritt. Doctor erkennt die Modellnamen der Presets (Aliase). Die Validierung „ein llama-server bedient genau ein Modell“ entfällt.

### Behoben
- **Gemeldete Beispiel-Tokens werden wirklich ersetzt (BUG-279):** Unverändert zurückgeschriebene maskierte Zeilen werden abgelehnt statt als „ersetzt“ gemeldet. Dokumentation bekommt Platzhalter-Hinweise (`<your-token>`, `$AUTH_TOKEN`), und `complete_step` nennt in einer Korrektur die Zeilen, die noch auffallen.
- **Unerfüllbare Änderungsforderungen des Plans entscheidet das Review (BUG-278):** Scheitert ein Schritt nur daran, dass vom Plan geforderte Dateien unverändert blieben, prüft das Heavy Review den Schritt ohne diese Forderung. Bei `pass` wird sie protokolliert fallen gelassen (`acceptance.waived`), statt in Korrekturen und Replans zu enden.
- **Testkriterium für spätere Schritte im leeren Repository (BUG-277):** Tests, die ein vorgelagerter Schritt anlegt, zählen bei der Planung wie vorhandene Tests. Ein späterer Schritt wird nicht mehr am fehlenden Testnachweis blockiert.
- **Korrekturen enden erst, wenn die gescheiterte Prüfung besteht (BUG-276):** `complete_step` führt in einem Korrekturversuch die Lint- und Testbefehle der gescheiterten Prüfungen erneut aus und lehnt ab, solange einer fehlschlägt. Das gilt auch, wenn die Runtime den Schritt für den Coder abschließt.
- **Befehle im Node-Image laufen wieder (BUG-275):** Der `pipefail`-Präfix wird nur gesetzt, wo die Shell die Option kennt. In dash von Debian bookworm beendete er jedes Skript mit Exit-Code 2 und ohne Ausgabe.

## [0.1.0rc59] – Release Candidate (2026-10-18)

### Behoben
- **Heavy Review sieht, warum Tests scheitern (BUG-274):** Fehlgeschlagene Prüfungen zeigen im Review einen Auszug ihrer Ausgabe (Assertion-, FAILED- und Fehlerzeilen) statt nur des Exit-Codes.

## [0.1.0rc58] – Release Candidate (2026-10-18)

### Behoben
- **Kurze Komponentennamen ordnen Schritte (BUG-273):** Ein Schritt, dessen Ziel „die lokale API“ nennt, muss mit dem Schritt geordnet sein, der `api.py` schreibt; kurze Stämme zählen als Akronym in Großbuchstaben.

## [0.1.0rc57] – Release Candidate (2026-10-18)

### Behoben
- **Identisches replace_text erklärt sich (BUG-272):** Die Ablehnung nennt, ob die Datei den Text schon enthält (Änderung bereits gemacht → weiter mit dem nächsten Punkt) oder nicht.

## [0.1.0rc56] – Release Candidate (2026-10-18)

### Behoben
- **Vom Lesen zum Ändern (BUG-271):** Nach einer verweigerten Wiederholung eines Lesezugriffs verlassen `read_file`/`read_range` den Werkzeugkatalog, bis sich eine Datei ändert.

## [0.1.0rc55] – Release Candidate (2026-10-18)

### Behoben
- **Bearbeiten neben maskierten Zeilen (BUG-270):** Eine maskierte Zeile, die `new` unverändert aus `old` übernimmt, behält bei `replace_text` ihren echten Inhalt, statt den Aufruf als `REDACTED_PLACEHOLDER` abzulehnen.

## [0.1.0rc54] – Release Candidate (2026-10-18)

### Behoben
- **Keine verlorenen Turns durch verrutschte Felder (BUG-269):** `status`/`decision` innerhalb von `args` werden in die Aktion gehoben, statt den Aufruf als `ARGS_INVALID` abzulehnen.

### Behoben
- **Im Ziel genannte Dateien gehören zum Schritt (BUG-268):** Die Plan-Validierung lehnt einen Schritt ab, dessen Ziel eine vorhandene oder geplante Datei nennt, die weder in `repo_hints` noch in `allowed_new_paths` steht.

## [0.1.0rc53] – Release Candidate (2026-10-18)

### Behoben
- **Pipelines verschweigen keine Fehler (BUG-267):** Befehle laufen mit `set -o pipefail`; `pytest … | tail -80` mit fehlschlagenden Tests ist ein fehlgeschlagener Lauf statt eines Erfolgs, den die Runtime als Abschluss meldet.

## [0.1.0rc52] – Release Candidate (2026-10-18)

### Behoben
- **Gelesene Seiten erreichen den Coder (BUG-266):** Jede Seite eines abgeschnittenen `read_file` beginnt mit ihrem Zeilenbereich; der Kontext des nächsten Turns zeigt genau diese Seite statt erneut den Dateianfang.

## [0.1.0rc51] – Release Candidate (2026-10-18)

### Behoben
- **Blättern ist keine Schleife (BUG-265):** Die Stagnationserkennung unterscheidet die Seiten eines seitenweisen `read_file`; nur dieselbe Seite erneut zählt als Wiederholung.

## [0.1.0rc50] – Release Candidate (2026-10-18)

### Behoben
- **read_file blättert weiter (BUG-264):** Ein erneutes `read_file` einer abgeschnittenen, unveränderten Datei zeigt die nächsten Zeilen statt wieder den Anfang; nach dem Ende oder einer Änderung beginnt es bei Zeile 1.

## [0.1.0rc49] – Release Candidate (2026-10-18)

### Behoben
- **Remote-Ausfall in der Vorbereitung (BUG-263):** Ist GitLab kurz nicht erreichbar (Netzwerk, HTTP 5xx, „Internal API unreachable“), bleibt der Job in der Vorbereitung und versucht es nach Backoff erneut, statt sofort zu scheitern.

## [0.1.0rc48] – Release Candidate (2026-10-18)

### Behoben
- **Hinweise auf Dateien früherer Schritte (BUG-262):** Ein `repo_hint` auf eine Datei, die ein anderer Schritt des Plans in `allowed_new_paths` anlegt, ist gültig, wenn der Schritt von diesem abhängt; sonst nennt der Fehler die fehlende Abhängigkeit statt „never invent paths“.

## [0.1.0rc47] – Release Candidate (2026-10-18)

### Behoben
- **Planner-Reparaturen mit Gedächtnis (BUG-261):** Ab der zweiten Reparatur nennt die Nachricht auch die Fehler früherer Antworten als weiter geltende Regeln; die Reparaturen pendeln nicht mehr zwischen zwei Verstößen.

## [0.1.0rc46] – Release Candidate (2026-10-18)

### Behoben
- **Fortgesetzte Korrekturen mit aktuellen Befunden (BUG-259):** Hat ein unterbrochener Korrekturversuch Dateien geändert, prüft der Verifier vor dem Fortsetzen erneut; behobene Befunde verschwinden aus der Korrektur.
- **Abgebrochene Eskalationen verbrauchen keine Stufe (BUG-260):** Nur Versuche, die über den Stagnationsweg endeten, zählen für die Eskalationsleiter; ein durch Deploy oder Ausfall abgebrochenes Heavy Review steht beim nächsten Stopp wieder zur Verfügung.

## [0.1.0rc45] – Release Candidate (2026-10-18)

### Behoben
- **Korrekturversuche fassen die genannten Dateien an (BUG-258):** `complete_step` wird in einem Korrekturversuch einmal verweigert (`CORRECTION_OPEN`), solange eine Datei unverändert ist, die die Korrektur-Items nennen; ein zweiter Aufruf wird angenommen.

## [0.1.0rc44] – Release Candidate (2026-10-18)

### Behoben
- **Altlasten gehen an den Schritt, der sie laut Plan liefert (BUG-257):** Uncommittete Dateien abgelöster Schritte, die ein anderer offener Schritt in `allowed_new_paths` führt, werden geparkt (`.git/hermclaw-parked`, auditiert) und beim Start dieses Schritts zurückgelegt, statt dem zufällig nächsten Schritt Verifier- und Review-Befunde zu fremder Arbeit einzubringen.

## [0.1.0rc43] – Release Candidate (2026-10-18)

### Behoben
- **PowerShell-Befehle sind keine Secrets (BUG-256):** `$token = Get-Content $file -Raw` in `.ps1`-Dateien gilt als Ausdruck statt als Literal; gequotete Werte werden weiter gemeldet.

## [0.1.0rc42] – Release Candidate (2026-10-18)

### Behoben
- **Fremde Altlasten bei jedem Versuch übernommen (BUG-255):** Hat ein anderer Schritt zwischen zwei Versuchen Dateien liegen gelassen, übernimmt der nächste Versuch sie in den Scope statt am Scope-Verstoß zu scheitern; eigene Änderungen des Schritts bleiben geprüft.

## [0.1.0rc41] – Release Candidate (2026-10-18)

### Behoben
- **Keine angebotenen Drittpakete (BUG-254):** Planner-Ziele nennen keine nicht angeforderten Drittpakete mehr, auch nicht als Option; für nur lesbare Formate verlangen sie einen kleinen eigenen Writer.
- **Wirkungslose Schreibzugriffe (BUG-253):** Ein `write_file`, der die Datei nicht ändert, wird als Wiederholung verweigert, statt als Erfolg die letzte Fehlermeldung zu löschen.

## [0.1.0rc40] – Release Candidate (2026-10-18)

### Behoben
- **Abgeschnittene Schreibzugriffe gerettet (BUG-252):** Bricht eine `write_file`-Antwort am Ausgabelimit ab, schreibt der Coder ihre vollständigen Zeilen und setzt im nächsten Turn mit `"append": true` fort, statt minutenlange Arbeit zu verwerfen.
- **Komponenten-Verweise brauchen Reihenfolge (BUG-251):** Nennt ein Schritt eine Komponente, die ein anderer Schritt schreibt, verlangt die Plan-Validierung eine Abhängigkeit zwischen beiden, damit der nutzende Schritt die echte Schnittstelle sieht.
- **Fremde Tests und Nachbar-Dateien (BUG-250):** Die Plan-Validierung lehnt Testdateien für den Code eines anderen Schritts auch in Schritten mit eigenem Code ab; ein Schritt sieht neben den Dateien seiner Abhängigkeiten auch die der übrigen abgeschlossenen Schritte des Jobs.

## [0.1.0rc39] – Release Candidate (2026-10-18)

### Behoben
- **Korrekturen überleben Checkpoints (BUG-249):** Ein fehlgeschlagener Versuch verbraucht den Checkpoint, sodass der nächste Versuch die Korrektur mit Belegen bekommt statt eines veralteten `resume`; ein fortgesetzter Korrekturversuch behält seine Belege.

## [0.1.0rc38] – Release Candidate (2026-10-18)

### Behoben
- **Belege in Verifier-Korrekturen (BUG-247):** Der Coder bekommt zu jedem fehlgeschlagenen Check einen Auszug der Belege (z. B. die Lint-Ausgabe mit Datei und Zeile), nicht nur die Kurzmeldung.
- **Maskierte Geheimnisse ersetzbar (BUG-248):** `replace_text` ersetzt eine Zeile mit maskiertem Geheimnis, wenn `old` sie wie angezeigt (mit Marker) enthält; nichts Maskiertes wird zurückgeschrieben.

## [0.1.0rc37] – Release Candidate (2026-10-18)

### Behoben
- **Verweigerte Befehlswiederholung mit Beleg (BUG-246):** Die Verweigerung eines wiederholten fehlgeschlagenen Befehls enthält dessen frühere Ausgabe, auch wenn ein anderer Befehl LATEST FAILURE inzwischen ersetzt hat.
- **Keine handgeschriebenen Parser (BUG-245):** Der Coder liest JSON/TOML/CSV/INI/XML mit dem Parser der Sprache und prüft selbst geschriebene Formate durch Zurücklesen; der Reviewer stuft handgeschriebene Parser als major ein.
- **Teil-Lesezugriffe (BUG-244):** Ein `read_range` innerhalb eines vollständig sichtbaren Lesezugriffs derselben Datei wird wie ein identischer Lesezugriff verweigert, solange keine Datei geändert wurde.
- **Geheimnis-Funde in Tests (BUG-243):** Für Testdateien verlangt die Geheimnis-Prüfung einen offensichtlichen Platzhalter (`dummy`, `fake`, `example`) statt „aus der Umgebung laden“; `fake`, `mock` und `sample` gelten als Platzhalter.

## [0.1.0rc36] – Release Candidate (2026-10-18)

### Behoben
- **Schnittstellen der Vorgänger (BUG-242):** Ein Schritt sieht die Dateien, die seine abgeschlossenen Abhängigkeiten geliefert haben, im Schrittziel und als Code-Ausschnitt `[dependency]`. Er nutzt deren echte Funktionen und JSON-Felder, statt sie zu raten.

## [0.1.0rc35] – Release Candidate (2026-10-18)

### Behoben
- **Replan robuster (BUG-241):** Der Replanner gibt nur geänderte und neue Schritte an; offene Schritte übernimmt die Runtime. Ein ungültiger Replan zählt als ein Replan und wird wiederholt, solange Budget übrig ist, statt den Job sofort zu beenden.
- **Quellenbewertung der Recherche (BUG-240):** Nur Repositories des Produkts selbst gelten als offiziell, nicht fremde Gists oder Community-Repos. Die Domain des Produkts zählt als Doku bzw. Hersteller, und die Relevanz misst auch gegen die Suchanfragen. Ein reiner Host wie `github.com` in `primary_domains` verleiht keinen Primärstatus mehr.

## [0.1.0rc34] – Release Candidate (2026-10-18)

### Behoben
- **Gesperrte Suchmaschinen (BUG-238):** SearXNG nutzt zusätzlich Bing und Yahoo. Liefert die Suche nichts, weil Engines gesperrt sind, gilt das als Ausfall (`SEARCH_UNAVAILABLE`, Wiederholung ohne Versuchsverbrauch) statt als „keine Quellen“.
- **Suchanfragen zu deutschen Komposita (BUG-239):** Englische Anfragen zu Fragen wie „Proxmox-REST-API-Endpunkte“ werden nicht mehr als themenfremd verworfen.

## [0.1.0rc33] – Release Candidate (2026-10-18)

### Behoben
- **Code und Tests in einem Schritt (BUG-237):** Ein reiner Testschritt, dessen Testdateien den Code eines anderen Schritts benennen (`test_collector.py` ↔ `collector.py`), wird auch ohne Abhängigkeit abgewiesen.
- **Abhängigkeiten bei geplantem Retry (BUG-236):** Ein fehlgeschlagener Schritt, für den ein Retry geplant ist, blockiert seine abhängigen Schritte nicht mehr.

## [0.1.0rc32] – Release Candidate (2026-10-18)

### Behoben
- **Ausgabelimit des Coders (BUG-235):** Der Coder fordert so viele Ausgabe-Tokens an, wie das Kontextbudget reserviert (Profil `coder-main`: 8192), statt fest 6144.
- **Schnittstellen zwischen Schritten und hohle Tests (BUG-234):** Der Planner lässt bei zusammenarbeitenden Komponenten aus verschiedenen Schritten eine Schnittstellendatei schreiben, an die sich beide Seiten halten. Der Reviewer wertet Mocks am falschen Ort, Zusicherungen, die auch bei Fehlern bestehen, und echte Netzwerkzugriffe in Tests als major.
- **Unterbrechungen kosten keinen Versuch (BUG-233):** Ein Schritt, der durch Deployment, Pause oder Verdrängung einen Checkpoint schreibt, bekommt für die Fortsetzung einen Versuch dazu.

## [0.1.0rc31] – Release Candidate (2026-10-18)

### Behoben
- **Reviewer prüft gegen die Recherche (BUG-231, Nachtrag):** Er sieht die belegten Aussagen vollständig. Widersprechen APIs, Header oder Felder im Code der Recherche, oder werden Konfigurationsoptionen nie angewandt, ist das ein major-Befund.
- **Ausfälle kosten keinen Versuch (BUG-232):** Jeder Modell- oder Worker-Ausfallversuch erhöht das Versuchsbudget des Schritts um eins, auch wenn der Ausfall früh im Schritt passiert.

## [0.1.0rc30] – Release Candidate (2026-10-18)

### Behoben
- **Recherche beim Coder (BUG-231):** Die belegten Aussagen einer Recherche stehen jetzt vorn und kommen vollständig im Abschnitt CONSTRAINTS an (2400 Zeichen je Recherche, 8 % Budget), statt auf 500 Zeichen gekürzt zu werden.

## [0.1.0rc29] – Release Candidate (2026-10-18)

### Behoben
- **Fertige Arbeit am Turn-Limit (BUG-230):** Endet ein Coder-Versuch am Turn-Budget nach Dateiänderungen, prüfen Verifier und Review die Änderungen. Bestehen sie, ist der Schritt abgeschlossen, statt verworfen zu werden.

## [0.1.0rc28] – Release Candidate (2026-10-18)

### Behoben
- **Lesesperre und Kontext (BUG-229):** Gelesene Dateien erscheinen im Kontext so, wie sie gelesen wurden (ganze Datei oder genauer Bereich, bis 400 Zeilen). Eine Wiederholung wird nur noch abgelehnt, wenn der Inhalt vollständig sichtbar war.
- **Fortschrittsliste im Coder-Kontext (BUG-228):** Geschriebene neue Dateien sind markiert, noch fehlende werden aufgelistet.
- **Deployment bei laufendem Job (BUG-227):** Ist der Sandbox-Worker nach einem Neustart noch nicht bereit, wartet der fortgesetzte Schritt mit Backoff, ohne einen Versuch zu verbrauchen.

## [0.1.0rc27] – Release Candidate (2026-10-18)

Fix aus dem Großauftrag G1 auf 0.1.0rc26.

### Behoben
- **Schrittgröße (BUG-226):** Ein Planschritt legt höchstens so viele neue Dateien an, wie ein Coder-Versuch schreiben kann (aus `policies.coder.max_turns`, Standard 8). Größere Aufträge werden nach Komponenten geteilt, jede mit ihren Tests.

## [0.1.0rc26] – Release Candidate (2026-10-18)

Weitere Fixes aus dem Großauftrag G1 auf 0.1.0rc25.

### Behoben
- **Größeres Antwortbudget des Coders (BUG-223, Nachtrag):** `coder-main.max_output_tokens` 8192 statt 6144.
- **Redaction lässt Code lesbar (BUG-225):** Code, der Geheimnisse liest (`api_key = config["x"]`), und offensichtliche Platzhalter (`"your-secret-here"`) werden nicht mehr maskiert. Vorher kopierte der Coder den Marker in neue Dateien, und jeder Schreibversuch wurde abgelehnt.
- **Replan erhält den Auftragsumfang (BUG-224):** Offene Schritte, die ein Replan weder wiederholt noch ersetzt, werden mit umgehängten Abhängigkeiten übernommen. Vorher konnte ein Replan nach einem gescheiterten Schritt Frontend, Tests und README still verwerfen.

## [0.1.0rc25] – Release Candidate (2026-10-18)

Fixes aus dem Großauftrag G1 (Windows-Widget für Proxmox VE und Unraid) auf 0.1.0rc24.

### Behoben
- **Große Dateien (BUG-223):** Antworten, die am Output-Limit abgeschnitten werden, werden erkannt (`MODEL_OUTPUT_TRUNCATED`) statt sinnlos repariert. Der Coder schreibt große Dateien in Teilen (`write_file` mit `append`) und bekommt nach einem Abbruch eine konkrete Anleitung.
- **Recherche bei deutschen Aufträgen (BUG-222):** Claims werden in der Sprache der Quelle extrahiert und gegen sie geprüft; die Antwort bleibt in der Sprache der Frage. Vorher wurden übersetzte Claims als nicht belegt verworfen, z. B. die Unraid-Authentifizierung per `x-api-key`.
- **Keine doppelten Recherchen (BUG-221):** Eine `research_needed`-Frage, die einen geplanten Recherche-Schritt nur umformuliert, erzeugt keinen zweiten Recherche-Schritt mehr.

## [0.1.0rc24] – Release Candidate (2026-10-18)

Fixes aus der erweiterten Batterie auf 0.1.0rc23.

### Behoben
- **Merge Request bei langsamem GitLab (BUG-220):** Die MR-Anlage wartet bis zu etwa 3,5 Minuten darauf, dass GitLab den gepushten Branch kennt.
- **Programme werden wie vom Nutzer ausgeführt (BUG-219):** Pläne prüfen ausführbare Programme mit einem echten Aufruf samt Beispieldaten. Das Review sieht die tatsächliche Ausgabe und bewertet sie gegen den Auftrag.
- **Vollständige Lieferung (BUG-218):** Neue Programme, Optionen und Umgebungsvariablen werden in der vorhandenen README dokumentiert, im selben Schritt.
- **Review gegen Werkzeug-Fakten (BUG-217):** Behauptet das Review einen Syntaxfehler in einer Datei, die der Verifier fehlerfrei geparst hat, blockiert das Finding nicht mehr.
- **Strengeres Review bei Auftragsvorgaben (BUG-216):** Das Review prüft, ob Wertebereiche und Formate des Auftrags exakt durchgesetzt werden (z. B. `parseInt("2.5")` bei „1–10 Personen“), außerdem Import-Seiteneffekte, offene Ressourcen, die Sprache der Texte und die Dokumentation neuer Optionen.
- **Python-Lint standardmäßig aktiv (BUG-215):** Der Verifier prüft geänderte Python-Dateien mit ruff auf Defekte (unbenutzte Importe und Variablen, undefinierte Namen), ohne Stilregeln.
- **Laufzeitdaten von Testläufen (BUG-214):** SQLite-Dateien, Journale und Logs, die ein Programm beim Laufen anlegt, werden nach dem Befehl aufgeräumt, statt den Lauf als Scope-Verstoß scheitern zu lassen.

## [0.1.0rc23] – Release Candidate (2026-10-17)

Fixes aus der erweiterten Batterie auf 0.1.0rc22 (X1 Datenanalyse-CLI) und dem Audit später Fehlerpfade.

### Behoben
- **Bestätigungsfragen des Replanners (BUG-208):** Liefert der Replanner einen Plan und fragt trotzdem nach Bestätigung, laufen die Schritte weiter. Die Frage bleibt als Notiz erhalten. Nur ein Replan ohne Schritte wartet auf den Nutzer.
- **Tests in Prüfschritten (BUG-209):** Pläne, die Dateien in einen `test`-, `verify`- oder `review`-Schritt legen, werden abgelehnt, mit dem Hinweis, Code und Tests in einen Schritt zu legen. Vorher wurden die Tests nie geschrieben und der Code-Schritt scheiterte am Review.
- **Base-Konflikt beim Abschluss (BUG-210):** Hat sich der Ziel-Branch während des Jobs konfliktbehaftet geändert, wird die verifizierte Arbeit als Draft-MR auf der alten Base geliefert, statt verworfen zu werden. Eine Regression nach dem Base-Update lässt den Job weiterhin scheitern, liefert den Branch aber ebenfalls als Draft.
- **Auftrag im Coder-Kontext (BUG-212):** Der Coder sieht unter dem Schrittziel den Originalauftrag. Vorher kannte er nur die Zusammenfassung des Planners und baute bei X2 ein Benutzer-Registrierungsformular statt einer Veranstaltungsanmeldung.
- **Replan-Budget nach Fortschritt (BUG-213):** `max_replans_per_job` zählt Replans in Folge ohne neu abgeschlossenen Schritt. Lange Jobs scheitern nicht mehr an ihrem dritten unabhängigen Problem. Neue Obergrenze `policies.correction.max_replans_total` (12).
- **Wiederholungssperre (BUG-211):** Derselbe Befehl mit anderem `timeout_seconds` gilt auf unveränderten Dateien als Wiederholung.

## [0.1.0rc22] – Release Candidate (2026-10-17)

Fix aus Batterie-Lauf 1 auf 0.1.0rc21 (J1–J4 bestanden, J5 nur an der Wiederholungs-Metrik gescheitert).

### Behoben
- **Wiederholte grüne Checks (BUG-207):** Schlägt der Coder einen bestandenen Check ohne zwischenzeitliche Änderung erneut vor, schließt die Runtime den Schritt für ihn ab. Verifier und Review prüfen danach wie sonst.

## [0.1.0rc21] – Release Candidate (2026-10-17)

Fix aus Batterie-Lauf 1 auf 0.1.0rc20.

### Behoben
- **Liegengebliebene Änderungen nach einem Replan (BUG-206):** Ein neuer Schritt übernimmt die uncommitteten Änderungen eines abgebrochenen Vorgängers in seinen Scope. Vorher scheiterte er an einem „Scope-Verstoß“, den er nicht begangen hatte, bis zum Replan-Limit.

## [0.1.0rc20] – Release Candidate (2026-10-17)

Fixes aus Batterie-Lauf 1 auf 0.1.0rc19. J1 und J3 waren erfolgreich, J4 stellte nach 2,5 min die richtige Rückfrage. J2 endete ohne MR, J5 änderte package.json unnötig, J6 scheiterte an einem widersprüchlichen Plan.

### Behoben
- **Merge Request direkt nach dem Push (BUG-202):** Kennt GitLab den gerade gepushten Branch noch nicht, wird die MR-Anlage nach kurzer Wartezeit wiederholt. Vorher endete ein Job gelegentlich ohne Merge Request.
- **Bestehende Manifeste (BUG-203):** Der Plan verlangt keine Änderung mehr an einem vorhandenen `package.json`, `pyproject.toml` usw., wenn der Auftrag die Datei nicht nennt. Vorher änderte der Coder eine korrekte Datei nur, um das Kriterium zu erfüllen.
- **Widersprüchliche Diff-Kriterien (BUG-204):** Ein Plan, der die eigenen Dateien eines Schritts als „darf sich nicht ändern“ markiert, wird bereinigt. Vorher endete ein korrekt gelöster Bugfix-Job mit `REPLAN_LIMIT`.
- **Hängende Tests (BUG-205):** Coder-Befehle ohne eigenes Timeout enden nach 180 s statt nach 600 s (`policies.coder.default_command_timeout_seconds`).

## [0.1.0rc19] – Release Candidate (2026-10-17)

Fix aus Batterie-Lauf 1 auf 0.1.0rc18 (J4 endete `succeeded` mit einem Merge Request, dessen Code ein erfundenes Paket importiert) sowie ein Audit auf Pfade, an denen lange Aufträge spät scheitern.

### Behoben
- **Nicht auflösbare Importe (BUG-193):** Neue Verifier-Prüfung `imports:python`. Ein neu importiertes Modul muss Standardbibliothek, Teil des Repositories oder in der Sandbox installiert sein; sonst wird der Schritt nicht abgenommen. Vorher landete ein erfundenes Paket in einer ungetesteten Funktion im Merge Request.
- **Interpreter der Import-Prüfung (BUG-194):** Die Prüfung nutzt den Interpreter, den die Test-Kriterien nennen, etwa ein Projekt-venv oder `uv run`. Beim Gesamt-Review nutzt sie den der Code-Schritte. Ein nur geratenes `python3` lässt den Schritt nicht scheitern.
- **Große Jobs am Ende (BUG-195):** Gesamt-Review und Regressionslauf nach einem Base-Update wenden die Pro-Schritt-Grenzen nicht mehr auf die gesamte Job-Änderung an. Das betrifft die Dateianzahl und `must_not_change` früherer Schritte. Vorher scheiterte ein Job mit mehr als 40 geänderten Dateien erst ganz am Ende.
- **Job-Scope (BUG-196):** Das Verbot eines Schritts („tests/ nicht anfassen“) gilt nicht mehr für die Dateien anderer Schritte im Gesamt-Review.
- **Modell-Ausfall im Schritt (BUG-197):** Versuche, die am nicht erreichbaren Modell oder an der GPU-Lease scheitern, verbrauchen keine Versuche des Schritts mehr. Sie werden bis zu 4 h mit Backoff wiederholt.
- **Review bei Ausfall des Modell-Hosts (BUG-198):** Ein solcher Ausfall ist kein Urteil über die Änderung, also keine Korrekturrunde und kein Replan. Der Schritt wird wiederholt.
- **Abschluss bei Netzwerkproblemen (BUG-199):** Netzwerk-, Timeout- und Lock-Fehler sowie ein abgelehnter Push lösen einen späteren neuen Abschlussversuch aus, statt den Job scheitern zu lassen.
- **Regressionslauf (BUG-200):** Text-Belege früherer Schritte, die ein späterer Schritt legitim geändert hat, lassen den Abschluss nicht mehr scheitern.
- **Manuelles Replan (BUG-201):** Ein vom Menschen angefordertes Replan scheitert nicht mehr am aufgebrauchten Budget.

## [0.1.0rc18] – Release Candidate (2026-10-17)

Fixes aus Batterie-Lauf 1 auf 0.1.0rc17: J1/J2/J3/J5 erfolgreich (3,4/3,7/9,6/11,5 min). J4 stellte nach 3,1 min korrekt eine Rückfrage, wurde aber als FAIL gewertet.

### Behoben
- **Batterie-Dauer bei Rückfragen (BUG-190):** Die Dauer von Jobs in `waiting_for_user` oder `blocked` endet mit dem letzten Statuswechsel. Die Rückfrage ist auf ein bis zwei kurze Sätze begrenzt; vorher wurde sie bei 1000 Zeichen mitten im Satz abgeschnitten.
- **Review mit nur „minor“-Befunden (BUG-191):** Das Review erzwingt keine Korrekturrunde mehr; die Befunde bleiben als Hinweise.
- **Research-Duplikate (BUG-192):** Bei inhaltsgleichen Quellen wird die maßgebliche gelesen, nicht die schnellere.

## [0.1.0rc17] – Release Candidate (2026-10-17)

Fixes aus Batterie-Lauf 1 auf 0.1.0rc16. Das war der erste Lauf mit qwen3.6:35b auf llama.cpp; Coder-Züge brauchten im Schnitt 14,9 s statt 39 s. Ergebnis mit korrigierter Metrik: J1, J2, J3 und J6 erfolgreich, J4 mit `REPLAN_LIMIT` statt sauberem Halt, J5 mit `PLANNER_INVALID_OUTPUT`. Die sechs Aufträge dauerten zusammen 36 min.

### Neu
- **Rückfrage statt Workaround (BUG-187):**
  - Der Replanner kann `user_question` stellen; der Job wartet dann in `waiting_for_user`, ohne Replan-Budget zu verbrauchen.
  - Eine Neuplanung mit Begründung (`POST /api/jobs/{id}/replan?reason=…`) beantwortet die Frage und setzt den Job fort.
  - Gemma plant nicht mehr um ausdrückliche Vorgaben des Auftrags herum.
- **Retry setzt fort (BUG-189):**
  - Erledigte Schritte bleiben; fehlgeschlagene und blockierte Schritte bekommen ein frisches Versuchs- und Replan-Budget.
  - Mit Hinweis wird stattdessen die offene Arbeit neu geplant. Die UI fragt beim Retry nach einem optionalen Hinweis.
- **Erweiterte Batterie** `config/acceptance-jobs-extended.example.yaml` mit fünf langen Aufträgen:
  - X1: Datenanalyse-CLI;
  - X2: Webseite mit getesteter Client-Logik;
  - X3: SQLite-Persistenz;
  - X4: Plugin-System;
  - X5: REST-CRUD mit Doku.

### Behoben
- **Planer-Wiederholungsschleife (BUG-188):**
  - Erfundene `must_change`-Dateien außerhalb des Schritts werden mit einer Notiz entfernt, solange eine erreichbare Datei bleibt.
  - Eine Reparatur, die nichts ändert, läuft mit höherer Temperatur weiter.
- **Batterie-Metrik (BUG-185):** `complete_step` wird nur von Schritten verlangt, die der Coder ausführt, nicht von `test`/`verify`.
- **Arbeitsverzeichnis (BUG-186):** Coder-Vertrag und `run_command` sagen, dass Befehle im Repository-Root starten (vorher in rund 11 % der Befehle ein geratenes `cd /home/user/repo`).

## [0.1.0rc16] – Release Candidate (2026-10-17)

Modell- und Runtime-Wechsel nach Messung (D-015, `docs/research/20261017-032-model-runtime.md`).

### Neu
- **llama.cpp-Backend:** Chat-Profile mit `backend: llamacpp` laufen auf einem residenten `llama-server` des Modellhosts
  (OpenAI-kompatibel über LiteLLM `openai/`, Denken über `chat_template_kwargs`, JSON-Schema per Grammatik erzwungen).
  Gemessen auf der GTX 1080 mit `--n-cpu-moe 30`: kalter 10,7k-Prompt 39 s statt 75 s (Ollama), Decode 30 statt 14 tok/s.
- Die Rolle `ollama` installiert `hermclaw-llamacpp.service` neben Ollama (`ollama_llamacpp_enabled`); der Port liegt in
  derselben Firewall-Tabelle wie Ollamas. `scripts/ops/build-llamacpp.sh` baut die Binärdateien aus dem Quell-Tag im
  CUDA-12.8-Image (sm_61). `hermclaw doctor` prüft `llamacpp:<host>`.

### Behoben
- **Abbruch während eines Modellaufrufs (BUG-184):** Ein Abbruch beendet den laufenden Coder-Modellaufruf sofort.

## [0.1.0rc15] – Release Candidate (2026-10-17)

Fixes aus Batterie-Lauf 1 auf 0.1.0rc14 (J1 erfolgreich, aber zu viele Wiederholungen; J2–J5 mit `REPLAN_LIMIT`; J6 wegen
der Modellversuche abgebrochen; kein Infrastrukturfehler).

### Behoben
- **Verify-Schritt mit geforderten Änderungen (BUG-180):** Nicht ändernde Schritte verlieren übernommene `must_change`-
  Forderungen; `must_not_change`/`max_changed_files` bleiben.
- **Wiederholte Prüfungen (BUG-181):** Nach einer verweigerten Wiederholung stehen `run_test`/`run_command` bis zur
  nächsten echten Änderung nicht im Werkzeugkatalog.
- **Offene Abnahmekriterien sichtbar (BUG-182):** Jedes diff-Kriterium zeigt im Coder-Kontext „met“ oder „NOT YET
  CHANGED: <Pfade>“. Ein Schreiben mit identischem Inhalt gilt nicht als Änderung.
- **Verlangtes Paket gestrichen (BUG-183):** Der Manifest-Hinweis entfällt, wenn der Schritt ein Paket einbindet, das
  der Auftrag in Anführungszeichen nennt.

### Geändert
- **Modellrollen konfigurierbar (D-015):** Ein Profil darf mit `deviation: "<Begründung>"` ein anderes Modell als die
  Architektur nennen; die Prüfung meldet das als Warnung.

## [0.1.0rc14] – Release Candidate (2026-10-17)

Fixes aus Batterie-Lauf 1 auf 0.1.0rc13 (J1, J3, J5, J6 mit `REPLAN_LIMIT`, kein Infrastrukturfehler).

### Behoben
- **Node-Testausgabe (BUG-177):** `node --test` (TAP und Spec-Reporter) wird gezählt; vorher „0 passed, 0 failed“.
- **Unverlangte Abhängigkeiten im Plan (BUG-178):** Ein Plan, der in einem Repository ohne Manifest eines anlegt, bekommt
  einmalig den Hinweis „nur Pakete, die der Auftrag nennt, sonst Standardbibliothek“. Das Replan-Paket enthält die
  Policy-Verweigerungen der Runtime (`runtime_refusals`).
- **Lesen statt Handeln (BUG-179):** Identische Lesezugriffe auf unveränderte Dateien innerhalb von 6 Zügen werden mit
  `READ_REPEATED` verweigert. Bleiben Tests nach einer Änderung rot, verlangt die Stagnationsstrategie, auch den Sollwert
  des eigenen Tests zu prüfen.

### Betrieb
- tower3: VM 224 (Snapshot als interner qcow2-Snapshot) liegt auf `hdd`; Thin-Pool von 98,5 % auf 27,5 %. Die Disk von
  VM 104 (.224) ist 300 GB groß; Partition und Dateisystem im Gast sind noch nicht vergrößert.

## [0.1.0rc13] – Release Candidate (2026-10-17)

Fixes aus dem Batterie-Lauf auf 0.1.0rc11 (J2–J6); enthält 0.1.0rc12 (nicht separat ausgerollt).

### Behoben
- **Verlorener Testfehler (BUG-175):** Verweigerte Wiederholungen und erfolgreiche Lesezugriffe ersetzen den letzten
  echten Fehler im Coder-Kontext nicht mehr; die Verweigerung sagt, ob der Befehl grün oder rot war. Doppelte
  Akzeptanzkriterien werden in jedem Step-Typ entfernt.
- **Modell-Host-Ausfall (BUG-176):** Planung und Neuplanung warten bei `MODEL_UNAVAILABLE`/`MODEL_TIMEOUT`/
  `MODEL_LOAD_FAILED` mit Backoff (bis 4 h), statt den Auftrag zu beenden. Batterie-Kriterium `infrastructure`.

### Betrieb
- tower3: LVM-thin-Pool `local-lvm` war voll und pausierte VM 104. Die gestoppten VMs 101 und 224 liegen jetzt auf der
  HDD (Storage `hdd`, `/mnt/hdd`, vorher Durchreich-Disk von VM 224); VM 104 hat Discard aktiviert.

## [0.1.0rc12] – Release Candidate (2026-10-17)

Erster erfolgreicher Auftrag der Batterie (J1 auf 0.1.0rc11 mit AVX2-fähigen VMs: `succeeded` in 10,3 min, 2 Steps, kein
Replan). Fixes der verbleibenden Kriterien (BUG-173, BUG-174); DNS-Verwaltung in der Rolle common (BUG-172).

### Behoben
- **Wiederholungssperre (BUG-173):** nach erfolgreicher Installation laufen Befehle wieder.
- **Unverlangte Abhängigkeiten (BUG-174):** in einem Repository ohne Manifest werden nur Pakete installiert, die der
  Auftrag nennt; sonst Standardbibliothek.

### Betrieb
- VM 102 (.222) und 104 (.224) auf CPU-Typ `x86-64-v3` (AVX2/FMA): Generierung auf .224 bis ~9× schneller.
- Merge Requests: `policies.git.create_merge_request: true` über das Inventar (`hermclaw_config_overrides`).

## [0.1.0rc11] – Release Candidate (2026-10-16)

Fix aus J2 des ersten Batterie-Laufs (BUG-171); enthält 0.1.0rc10 (nicht separat ausgerollt).

### Behoben
- **Coder-Gedächtnis (BUG-171):** Im Versuch gelesene Dateien bleiben im Kontext; identische Befehle auf unveränderten
  Dateien werden nicht erneut ausgeführt (`COMMAND_REPEATED`); Vertrag: gezeigte Dateien nicht erneut lesen.

## [0.1.0rc10] – Release Candidate (2026-10-16)

Fix aus dem ersten Lauf der Live-Auftragsbatterie (J1 auf 0.1.0rc9: Plan und Umsetzung korrekt, Verifier-Ablehnung
wegen fehlendem Test-Kriterium, BUG-170).

### Behoben
- **Test-Kriterium (BUG-170):** Umsetzungs-Steps, die Tests schreiben oder in einem Repository mit Tests arbeiten,
  erhalten automatisch ein Test-Kriterium; Plan-Defekte gehen ohne Korrekturversuch an den Replanner; doppelte
  Akzeptanzkriterien werden zusammengefasst.

## [0.1.0rc9] – Release Candidate (2026-10-16)

Ursachenanalyse aller Live-Jobs (Ursachenanalyse `docs/analysis/live-job-root-causes-2026-10-16.md`) und allgemeine Fixes der Ursachen (BUG-166 bis BUG-169) statt weiterer
Symptom-Fixes. Neue Live-Abnahme mit gemessenen Kriterien.

### Behoben
- **Verifier-False-Negative (BUG-166):** `must_change: ["*"]` und andere Dateinamen-Globs passen in jeder Tiefe;
  die Plan-Validierung weist `must_change`-Globs zurück, die auf keinen Pfad des Steps passen können.
- **Projekt-Importe (BUG-167):** Das Repository-Wurzelverzeichnis steht in jedem Container auf `PYTHONPATH`.
- **Recherche (BUG-168):** Fragen über das eigene Repository und zur Technologiewahl werden in Plan und Coder-Werkzeug
  abgelehnt (Web-Recherche kann sie nicht beantworten); externe Fragen bleiben erlaubt.
- **Planstruktur und Einfachheit (BUG-169):** Code und seine Tests gehören in einen Step (Validierung); Planner und
  Coder wählen die einfachste vollständige Lösung (Standardbibliothek und vorhandene Werkzeuge zuerst).

### Hinzugefügt
- `hermclaw job-metrics JOB_ID` und `hermclaw acceptance-jobs SPEC`: Live-Auftragsbatterie mit gemessenen Kriterien
  (Status/MR, Dauer, Replans, Erstplan-Größe, unbeantwortbare Recherche, neue Abhängigkeiten, Wiederholungen,
  `complete_step`); Spezifikation `config/acceptance-jobs.example.yaml`, Fixtures `tests/live/fixtures/`,
  `scripts/ops/acceptance-fixtures.sh`.

## [0.1.0rc8] – Release Candidate (2026-10-16)

Fix aus dem Testauftrag nach dem Deployment von 0.1.0rc7 (BUG-165).

### Behoben
- **Leerlauf-Installationen (BUG-165):** `install_dependencies` lehnt ein unverändertes Manifest ab (`INSTALL_UNCHANGED`);
  scheitert ein Lauf an einem fehlenden Modul, nennt die Ausgabe es mit dem Weg ins Manifest. Coder-Vertrag: Step
  abschließen, sobald sein Ziel erreicht ist, auch wenn Tests außerhalb des Scopes noch scheitern.

## [0.1.0rc7] – Release Candidate (2026-10-16)

Fixes aus dem Testauftrag nach dem Deployment von 0.1.0rc6 (BUG-163, BUG-164).

### Behoben
- **Liegengebliebene Datei als Repo-Hinweis (BUG-163):** Ein Step, dessen Ziel eine uncommittete Datei eines
  abgebrochenen Vorgängers ist, darf sie wieder schreiben; bisher wurde jede Änderung als „create“ abgelehnt.
- **Server blockieren Tests (BUG-164):** Ein Timeout erklärt, dass Befehle von selbst enden müssen; derselbe Befehl
  wird auf unverändertem Arbeitsbaum nicht erneut für den vollen Timeout gestartet. Coder-Vertrag: keine Server,
  Watcher oder REPLs; Tests starten die App im Prozess und schließen sie.

## [0.1.0rc6] – Release Candidate (2026-10-16)

Fix aus dem Testauftrag nach dem Deployment von 0.1.0rc5 (BUG-162).

### Behoben
- **Paket-Installation im Befehl (BUG-162):** `pip install`, `npm install` & Co. in `run_command`/`run_test` werden ohne
  Netz sofort abgelehnt (`PACKAGE_INSTALL_REFUSED`) und verweisen auf `install_dependencies`; vorher liefen sie in
  einen Netz-Timeout und der Coder stagnierte.

## [0.1.0rc5] – Release Candidate (2026-10-16)

Fixes aus dem Testauftrag nach dem Deployment von 0.1.0rc4 mit eingeschalteter Abhängigkeits-Installation (BUG-160, BUG-161).

### Behoben
- **Egress-Proxy-Start (BUG-160):** Der erste Start des Proxy-Containers lief in den 300-s-Timeout, obwohl der
  Container lief (ein Podman-Hintergrundprozess hielt die Ausgabe-Pipes offen). Engine-Hilfsbefehle kehren jetzt
  zurück, sobald der Engine-Prozess beendet ist.
- **Installation und Test-Toolchain (BUG-161):** `install_dependencies` (Python) beginnt mit leerem `/deps/python`
  und hält die Pakete des Sandbox-Images auf deren Versionen (pip-Constraints). Ein Pin wie `pytest==7.4.3` scheitert
  jetzt verständlich, statt pytest und seine Plugins zu brechen. Coder-Vertrag: fehlendes Modul zuerst ins Manifest,
  Test-Werkzeuge des Images nicht pinnen.

## [0.1.0rc4] – Release Candidate (2026-10-16)

Fixes aus dem ersten Testauftrag nach dem Deployment von 0.1.0rc3 (BUG-158, BUG-159) und Abhängigkeits-Installation (D-014).

### Neu
- **Abhängigkeiten installieren (D-014):** Werkzeug `install_dependencies` für den Coder (pip/npm aus dem Manifest,
  Befehl von der Runtime gebaut). Netz nur für diesen Befehl über den Proxy-Container `hermclaw-egress-proxy` auf
  .222, der nur `pypi.org`, `files.pythonhosted.org` und `registry.npmjs.org` erreicht; Pakete pro Workspace unter
  `/deps` (für alle anderen Befehle read-only). Standard aus, Inventar-Schalter `hermclaw_config_dependency_installs`.

### Behoben
- **Scope (BUG-158):** Eine als neu deklarierte Datei, die ein abgebrochener Versuch im Work Tree hinterlassen hat,
  bleibt anlegbar (vorher nur „modify“ erlaubt, Schreibprüfung verlangte „create“ → Stagnation).
- **Planner-Schema (BUG-159):** Das Schema für Gemma verlangt `depends_on`, `repo_hints`, `allowed_new_paths` und
  `acceptance` an jedem Step; übersprungene Scope-Felder waren im Replan nicht mehr nachtragbar.

## [0.1.0rc3] – Release Candidate (2026-10-16)

Fixes aus dem erneuten ersten Testauftrag nach dem Deployment von 0.1.0rc2 (BUG-153–157).

### Behoben
- **Replanner (BUG-153, BUG-154):** Eine Repair-Runde meldet Replan-Regel-, Schema- und Scope-Fehler gemeinsam
  (vorher eine Fehlerklasse pro Runde, Repair-Budget erschöpft); `rerun_reason` an nicht abgeschlossenen Steps wird
  ignoriert statt abgelehnt.
- **Sandbox-Image (BUG-155):** Ein leeres Repository bekommt das Toolchain-Image, sobald es Code enthält.
- **Replan-Trigger (BUG-156):** Der fehlgeschlagene Step ist die Ursache, nicht ein nur abhängig blockierter Step.
- **Prompts (BUG-157):** Planner und Coder wissen, dass Code-Steps offline mit fester Toolchain laufen und keine
  Pakete installieren können.

## [0.1.0rc2] – Release Candidate (2026-10-16)

Fixes aus der Live-Abnahme auf den Ziel-Hosts (INSTALLATION.md Kapitel 12, BUG-143–152).

### Behoben
- **Planner (BUG-143):** Pläne mit schreibenden Steps ohne `repo_hints`, `allowed_new_paths` oder Presence-Evidence
  werden beim Planen abgelehnt (vorher `SCOPE_UNAVAILABLE` zur Laufzeit, erster Testauftrag gescheitert); die Meldung
  für Prüf-Steps ohne Abhängigkeit nennt Kandidaten-IDs.
- **Planner-Prompt (BUG-144):** Web-Research nur für externe Fakten, nicht für das eigene Repository.
- **Modelle (BUG-150):** optionales Profilfeld `gpu_layers` (Ollama `num_gpu`); `planner-gemma-fallback` lädt mit
  32K auf der GTX 1080 nur mit 30 GPU-Layern.
- **Git (BUG-152):** abgelehnte Pushes nennen die Begründung des Servers.
- **Sandbox (BUG-147):** Docker-Adapter unter rootless Engines.
- **Deployment (BUG-149):** `podman-compose` auf dem Execution Worker.
- **Abnahme/Tests (BUG-145, -146, -148, -151):** Dev-Extra mit `aiohttp`, Sandbox-Suite prüft alle Podman-Tests,
  SKIP statt PASS bei vollständig übersprungenen Suiten, SearXNG-URL aus der Konfiguration, WoL-Test ohne WoL.

## [0.1.0rc1] – Release Candidate (2026-10-15)

Erster vollständiger Aufbau von Hermclaw Next (Greenfield, Bauplan v2, Phasen P00–P42). Lokal vollständig
getestet; die Live-Abnahme auf den Ziel-Hosts steht aus (BLOCKER-001, `scripts/ops/live-acceptance.sh`).

### Hinzugefügt – Kern (P02–P25)

- **Backend und Persistenz:** FastAPI-App, typisierte YAML-Konfiguration (`hosts`, `models`, `policies`,
  `capabilities`, `logging`, optional `databases`, `backup`), Settings über `HERMCLAW_*`, PostgreSQL 17 + pgvector
  mit Alembic-Migrationen (im Wheel unter `share/hermclaw`), Event Store mit fortlaufender Sequenz und SSE-Replay
  (`Last-Event-ID`), Zustandsautomaten für Jobs, Schritte und Versuche.
- **Git Engine:** Runtime-kontrollierte Git-Mutationen (Workspaces pro Job, Job-Branches `hermclaw/*`, geschützte
  Branches, Push, Merge Requests über die GitLab-API, Locks, Wiederaufnahme nach Abbruch).
- **Worker-Protokoll:** Execution-, Model- und Media-Worker-Daemons mit Bearer-Token + HMAC-Signatur,
  Heartbeats, Protokollversion, Workspace-Sync per Tar-Stream.
- **Model Gateway:** LiteLLM-Proxy (gepinnt `litellm[proxy]==1.104.2`) vor Ollama, feste Modellrollen
  (Gemma-Planner, Qwen3-Coder-30B-Worker, Qwen3.8-27B-Reviewer, Qwen3-8B-Router, EmbeddingGemma), strukturierte
  Ausgaben über JSON-Schema, technische Fallbacks nur bei Infrastrukturfehlern, Residenzsteuerung.
- **Resource Manager, Wake-on-LAN und Idle-Sleep:** PostgreSQL-Leases (exklusiv für große Modelle), Weckpipeline
  (Magic Packet → Ping → SSH → API → Dienste), Leerlauf-Suspend der Worker.
- **Repository Intelligence:** Inventar, lexikalische Suche (ripgrep), Symbole (Python-AST, tree-sitter für
  JS/TS/TSX/PHP), Abhängigkeiten, semantischer Index (pgvector), Ranking-Fusion.
- **Research Engine:** SearXNG-Suche, sicheres Fetching, Quellenbewertung, Zitate im Abschlussbericht.
- **Planner/Replanner (Gemma):** Plan-Vertrag mit Schritten, Abhängigkeiten, Capabilities und maschinell
  prüfbaren Akzeptanzkriterien; Reparaturschleife; Neuplanung nach Fehlschlägen.
- **Scope Engine:** expliziter Scope pro Schritt, ScopeGuard für jeden Dateizugriff, Scope-Erweiterungsanträge,
  Audit des Diffs inkl. Umbenennungen.
- **Context Builder, Tool Engine, Coder-Loop:** frischer Kontext pro Runde, Werkzeuge als einziger Weg in den
  Workspace (Dateien, Befehle, Tests, Ops-Tools), Stagnationserkennung, Abschluss- und Blockade-Berichte.
- **Deterministic Verifier und Heavy Review:** generische Prüfungen (Tests, Befehle, Präsenz, Diff, Scope, Schema,
  Secrets, Artefakte) und Review jedes Schritts vor dem Commit; Korrektur-Pipeline.
- **Scheduler und Runtime:** Composition Root, `hermclaw scheduler`/`hermclaw api`, Recovery nach Neustart,
  Scratch-Workspaces für repository-lose Ops-Jobs.

### Hinzugefügt – Betrieb und Werkzeuge (P26–P33)

- **SSH/Admin-Tools (P26):** Host-Allowlist, gepinnte Host-Keys, Klassifikation read/mutate/destructive/forbidden,
  Einmal-Freigaben durch Admins (`/api/jobs/{id}/ssh-approvals`), Forced-Command-Gate `hermclaw-ssh-gate`.
- **DB-Tools (P27):** Introspektion und Read-only-Abfragen auf Projektdatenbanken, Migrationen nur in
  Wegwerf-Sandbox-Datenbanken (Alembic, Roh-SQL, Upgrade/Downgrade/Upgrade), Backups und Restore-Tests.
- **Container/Proxy-Tools (P28):** Container-Inventar, Compose-Validierung und Compose-Sandbox (127.0.0.1-Ports,
  Healthchecks, garantierter Abbau), Portkollisionen, `nginx -t` mit Proxy-Regeln (SSE), Rollback-Artefakte.
- **Media (P29):** ffmpeg/NVENC-Video und ComfyUI-Bildadapter des Media-Workers.
- **API (P30) und Web-UI (P31):** REST + SSE, Token-Scopes read/control/admin, React/TypeScript-Oberfläche mit
  Plan-DAG, Live-Status ohne Chain-of-Thought, Diffs, Freigaben, Artefakten; Playwright-Tests.
- **Deployment (P32):** Ansible-Bootstrap (P01) für Debian 13 auf allen Hosts (PostgreSQL 17 aus PGDG, rootless
  Podman mit cgroup-Delegation, Node.js 22, nginx, NVIDIA R580 per Runfile/DKMS, Ollama 0.40.4) und Deployment-
  Playbook `deploy.yml`: Offline-Release (`scripts/deploy/build-release.sh`: CPython-3.13-Wheelhouse mit Hash-Lock,
  UI-Build, tree-sitter-Grammatiken, `SHA256SUMS`, Selbsttest), versionierte venvs mit atomarem Symlink-Wechsel,
  Konfiguration aus den Beispielen + Inventar mit `config-check` vor dem Einspielen, Schema-Entscheidung vor jeder
  Live-Änderung, Migration einmal vor dem Neustart, LiteLLM-Gate (lebendig und authentifiziert), Health-Gates,
  Upgrade/Rollback mit Schema-Downgrade-Opt-in, Prune nach Aktivierungsreihenfolge, Trockenlauf ohne Änderungen,
  Operator-CLI `sudo hermclaw …`; `hermclaw doctor` als Installations-Preflight.
- **Backup/Restore (P33):** konsistenter `pg_dump`, Konfiguration, Artefakte, signiertes Manifest, rsync nach
  Terra (.60), Retention, wöchentlicher Restore-Test in eine Wegwerf-Datenbank (Units und Restore-Template vom
  Deployment eingerichtet), Disaster-Recovery `restore-full`.

### Hinzugefügt – Qualität (P34–P42)

- Recovery-, Failure-Injection-, Security-, E2E- (15 Generalisierungs-Szenarien), Last- und Soak-Tests;
  Testmatrix Datei für Datei (`scripts/dev/test_matrix.py`); Cutover-Plan; Live-Abnahmeskript
  `scripts/ops/live-acceptance.sh`; vollständige Installationsanleitung `docs/operations/INSTALLATION.md`.

### Geändert (Entscheidungen, `DECISIONS.md`)

- D-010: Embedding-Modell `embeddinggemma:300m` statt EmbeddingGemma 2 (in Ollama nur auf dem MLX-Runner, nicht
  unter Linux/Pascal).
- D-011: NVIDIA-Treiber R580 über den Runfile-Installer auf `.224` (kein paketierter Treiber ≥ 570 für Pascal in
  Debian 13).
- D-012: pgvector ≥ 0.8.7 als Standard, dokumentierte Betreiber-Ausnahme ab 0.8.2, solange PGDG für trixie noch
  kein 0.8.7 liefert.
- SearXNG-Image per Tag und Digest gepinnt, `GRANIAN_HOST=0.0.0.0` (Research R-027).

### Sicherheit

- Geheimnisse nur als verschlüsselte systemd-Credentials; Redaktion in Logs, Events, Prompts und Diffs;
  Secret-Scan des Repositorys und jedes Job-Diffs.
- Sandbox: rootless, `--read-only`, `--network=none` ohne Netz-Capability, `--cap-drop=ALL`, Ressourcenlimits,
  Image-Sicherheitsgate gegen Podman CVE-2026-57231/CVE-2026-94603 (BUG-108).
- pgvector ≥ 0.8.7 (CVE-2026-3172, CVE-2026-103484; Untergrenze 0.8.2, D-012), LiteLLM ohne bekannte Advisories in der
  gepinnten Version (Research R-027).
- `POST /api/jobs` lehnt laufzeitreservierte Metadaten-Schlüssel (u. a. `ssh_approvals`, `triage`, `replan_requested`)
  mit 422 ab – ein `control`-Token kann keine SSH-Freigaben mehr vorab setzen (BUG-140, `hermclaw/core/job_metadata.py`).

### Behoben

- 142 Fehler wurden während des Aufbaus gefunden und behoben, jeweils mit Regressionstest bzw. (bei reinen
  Dokumentationsfehlern) einer Prüfung gegen den Code (`BUGS.md`, BUG-001 bis BUG-142; P0: 2, P1: 26, P2: 62,
  P3: 49, P4: 3). Es gibt keine offenen Bugs.
- Installationsanleitung und Runbooks wurden Befehl für Befehl gegen Code und Rollen geprüft (71 bestätigte Befunde,
  alle behoben; BUG-133 bis BUG-139).

### Bekannte Einschränkungen

Siehe `docs/operations/known-limitations.md`.
