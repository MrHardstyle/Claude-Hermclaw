# BUGS

Zentrales Bugregister für Hermclaw Next.

## Severity
- P0 – Datenverlust / Security / katastrophaler Fehler
- P1 – Kernfunktion gebrochen
- P2 – wichtiger Defekt
- P3 – kleiner Defekt / Usability

## Regeln
P0/P1-Blocker werden sofort behoben. Nicht-blockierende Bugs werden reproduziert, mit Evidence dokumentiert und spätestens in P39 erneut abgearbeitet.

## Externe Blocker

### BLOCKER-001 – Zielhosts aus der Build-Umgebung nicht erreichbar
- Zeit: 2026-10-08T14:20Z · Phase: P00 · Severity: extern
- Reproduktion: `bash -c 'exec 3<>/dev/tcp/192.168.178.225/22'` (ebenso .222/.223/.224/.226/.60) → Timeout; `curl -m 6 http://192.168.178.224:11434/api/version` → Timeout nach 6 s.
- Ursache: Cloud-Build-Container hat keine Route ins Heim-LAN 192.168.178.0/24.
- Auswirkung: Live-Inventur, Deployment, Modell-Downloads, GPU-/WOL-/GitLab-Live-Tests, Backup nach `.60`, Soak-Test auf Zielhosts.
- Workaround: Alle Schritte als Skripte/Ansible-Playbooks + Installationsanleitung; lokale Integrationstests mit echten Diensten (PostgreSQL, Podman, Git, LiteLLM) wo möglich, sonst klar gekennzeichnete Fakes ausschließlich im Testcode.
- Fortsetzung: Auf dem Orchestrator `docs/operations/INSTALLATION.md` abarbeiten, danach `scripts/ops/live-acceptance.sh` ausführen.
- Status: **aufgehoben** (2026-10-16): Installation erfolgt, Live-Abnahme auf .225/.222 durchgeführt (STATUS.md, BUG-143–152).

### BLOCKER-002 – Tag-Push aus der Build-Umgebung verweigert
- Zeit: 2026-10-15T05:40Z · Phase: P41 (41.9) · Severity: extern
- Reproduktion: `git push origin v0.1.0rc1` → `error: RPC failed; HTTP 403` (die Git-Anbindung der Build-Sitzung erlaubt nur
  Pushes auf den Arbeits-Branch `claude/hermclaw-next-build`).
- Auswirkung: Das annotierte Tag `v0.1.0rc1` lässt sich lokal anlegen, aber nicht nach GitHub übertragen.
- Fortsetzung (einmalig, in einem Checkout mit Schreibrecht): `git fetch origin claude/hermclaw-next-build && git tag -a v0.1.0rc1 -m "Hermclaw Next 0.1.0rc1 – Release Candidate" $(git log -1 --format=%H origin/claude/hermclaw-next-build -- docs/operations/FINAL_BUILD_REPORT.md) && git push origin v0.1.0rc1`. Das Tag markiert den Commit, in dem
  `docs/operations/FINAL_BUILD_REPORT.md` zuletzt geändert wurde (der Release-Candidate-Stand).

### BLOCKER-003 – Deploy-Key des Orchestrators ohne Schreibrecht im Wegwerf-Projekt `local-ai/hermclaw-test`
- Status: **aufgehoben** (2026-10-16): Schreibrecht vom Betreiber vergeben; GitLab-Suite PASS.
- Zeit: 2026-10-16T00:30+02:00 · Phase: Live-Abnahme (Suite `gitops`) · Severity: extern (GitLab-Einstellung)
- Reproduktion (als `hermclaw` auf .225, Schlüssel `/etc/hermclaw/ssh/id_ed25519`): Push eines Branches nach
  `git@192.168.178.226:local-ai/hermclaw-test.git` → `remote: This deploy key does not have write access to this project.`
  Lesen (`clone`, `ls-remote`) funktioniert.
- Auswirkung: Die GitLab-Suite (Push des Job-Branches, Merge Request) und echte Jobs auf diesem Projekt können nicht pushen.
- Fortsetzung: In GitLab unter *local-ai/hermclaw-test → Settings → Repository → Deploy keys* beim Schlüssel des
  Orchestrators „Grant write permissions to this key“ aktivieren (INSTALLATION.md 6.1), dann
  `sudo HERMCLAW_LIVE_GITLAB_PROJECT=local-ai/hermclaw-test HERMCLAW_LIVE_GITLAB_API=http://192.168.178.226 HERMCLAW_LIVE_GITLAB_REPO_URL=git@192.168.178.226:local-ai/hermclaw-test.git scripts/ops/live-acceptance.sh --suite gitops`.

## Offene Bugs
Noch keine.

## Behobene Bugs

### BUG-340 – „Fast keine Züge mehr“ führt zu `block_step` und damit zu einem 23-Minuten-Neuplan statt zu einem weiteren Versuch
- Phase: Schwierigkeitsleiter auf rc82, 2026-10-23 (A10#5, Job 64562f45) · Komponente: coder (`handler.py`), context_builder (`sections.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_coder_handler.py::test_a_block_for_lack_of_turns_with_work_in_progress_is_verified_not_replanned`)
- Reproduktion: Im Korrekturversuch von S001 sagt die Schlusszeile ab Zug 18 „Almost out of turns: complete_step, block_step or request_replan.“ In Zug 20 ruft der Coder `block_step` mit `reason_code: other` und „Out of turns. The remaining work is: …“ auf. Der Schritt geht in den Neuplan; der Planer braucht 13,5 + 9,4 min (eine Reparatur), danach bricht die Leiter den Job nach 56,9 min ab (versteckt 31/40). Ein dritter Versuch war erlaubt (`max_attempts_per_step: 3`).
- Ursache: Die Schlusszeile bot `block_step` als gleichwertigen Abschluss an. Ein Block wegen fehlender Züge wurde wie ein Planproblem behandelt, während ein aufgebrauchtes Budget nach Änderungen geprüft und als Korrektur fortgesetzt wird (BUG-230).
- Fix: Ein `block_step` mit `reason_code: other` in den drei Zügen, vor denen die Schlusszeile warnt, nach Dateiänderungen, wird wie ein aufgebrauchtes Budget behandelt (Verifier, dann Korrektur). Die Schlusszeile lautet jetzt „Almost out of turns: finish this change or complete_step; leftover work is continued.“ (`block_step` wird dort nicht mehr angeboten; im kleinsten getesteten Fenster 4096 Token sinkt die Ausgabereserve dadurch von 1000 auf 995).

### BUG-339 – Ein Schritt mit 11 Dateien bekommt dieselben 20 Züge wie einer mit zwei
- Phase: Schwierigkeitsleiter auf rc82, 2026-10-23 (A10#5, Job 64562f45) · Komponente: planner (`validation.py`, `persist.py`), coder (`handler.py`), core (`config.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_planner_validation.py::test_a_step_with_many_files_gets_more_turns`)
- Reproduktion: Der Plan hat einen Implement-Schritt mit 6 vorhandenen und 5 neuen Dateien (Datenbank, App, Seite, README, fünf Testdateien). Der Coder schreibt in den Zügen 1–11 alle Dateien, der erste Testlauf kommt in Zug 12; für Tests und Korrekturen bleiben 9 Züge, der Verifier findet den Stand rot.
- Ursache: `turn_budget` war immer `policies.coder.max_turns`. Die Planprüfung begrenzt nur die neuen Dateien eines Schritts (8 bei 20 Zügen), nicht die zu ändernden.
- Fix: `step_turn_budget` gibt einem Schritt, der mehr Dateien anlegt oder nennt (`allowed_new_paths` und `repo_hints` ohne Globs und Verzeichnisse), als `new_files_per_step` vorsieht, fünf Züge je zwei weitere Dateien, bis `policies.coder.max_turns_per_step` (40). 11 Dateien bei 20 Zügen ergeben 28 Züge.

### BUG-338 – Der Verlauf zeigt gelesenen Inhalt abgeschnitten, der Coder liest dieselben Zeilen erneut
- Phase: Schwierigkeitsleiter auf rc82, 2026-10-23 (A10#5, Job 64562f45) · Komponente: context_builder (`builder.py`, `history.py`) · Severity: P3 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_context_builder_relevance.py::test_the_history_names_a_read_the_files_message_shows_instead_of_its_clipped_head`)
- Reproduktion: Der Coder liest `tasks_app/db.py` Zeilen 1–35, obwohl RELEVANT CODE die Datei ganz zeigt (1–160). Im nächsten Zug liest er genau denselben Bereich noch einmal und bekommt READ_REPEATED. Das passiert dreimal in einem Schritt; das Wissen kennt den Fall aus 13 bzw. 17 früheren Jobs.
- Ursache: Die Verlaufszeile eines Lesezugriffs zeigte die ersten 320 Zeichen des Inhalts. Das sah wie eine abgeschnittene Ausgabe aus; dass der ganze Bereich in RELEVANT CODE steht, sagte sie nicht.
- Fix: Der Verlauf wird nach dem Packen von RELEVANT CODE/TESTS erzeugt. Ein Lesezugriff, dessen Zeilen dort vollständig stehen, heißt im Verlauf „the lines read are shown in full in RELEVANT CODE/RELEVANT TESTS above (their current content)“. Ein nicht gezeigter Lesezugriff behält seinen Auszug.

### BUG-337 – Der Hinweis auf entfernte Tests lässt den Coder seine eigene neue Testdatei viermal hin- und herschreiben
- Phase: Schwierigkeitsleiter auf rc82, 2026-10-23 (A10#5, Job 64562f45) · Komponente: tools (`engine.py`, `gitlocal.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_tools_engine_read_write.py::test_a_rewrite_of_the_steps_own_new_test_file_is_not_a_lost_test`)
- Reproduktion: Der Coder legt `tests/test_ui.py` neu an, Lint meldet einen unbenutzten Import. Jede Neufassung behebt den Befund, enthält aber eine andere Auswahl an Tests; der BUG-336-Hinweis verlangt die Tests der vorigen Fassung zurück. Die Züge 6–10 pendeln zwischen zwei Fassungen.
- Ursache: Der Hinweis verglich mit dem Inhalt vor dem Schreiben, also auch mit eigenen Tests des Schritts, die der Coder frei umbauen darf.
- Fix: Als vorhandene Tests zählen nur die Tests, die die Datei am Basis-Commit des Schritts hatte (`LocalGit.file_at`). Eine im Schritt neu angelegte Testdatei löst keinen Hinweis mehr aus; fehlende Basistests werden weiter genannt, auch nach mehreren Überschreibungen.

### BUG-336 – Ein Überschreiben einer Testdatei entfernt vorhandene Tests ohne Hinweis
- Phase: Schwierigkeitsleiter auf rc81, 2026-10-23 (A10#4, Job b9245e25) · Komponente: tools (`engine.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_tools_engine_read_write.py::test_an_overwrite_that_drops_existing_tests_says_which`)
- Reproduktion: Der Coder schreibt `tests/test_api.py` mit `write_file` neu. Drei der vorhandenen Tests (`test_create_array_body`, `test_create_invalid_json`, `test_python_m_starts`) fehlen danach, obwohl der Auftrag sagt „Die vorhandenen Tests bleiben erhalten“. Die Antwort lautet nur „overwrote tests/test_api.py“; die versteckte Prüfung `test_the_existing_tests_are_kept` scheitert (versteckt 37/40).
- Ursache: Ein Überschreiben meldete nicht, was aus der Datei verschwand.
- Fix: Überschreibt `write_file` eine Testdatei und fehlen danach Tests, die sie definierte (pytest-Funktionen, JS/TS `it`/`test`, Pester `It`, Go `Test…`), nennt das Ergebnis sie mit dem Hinweis, sie wieder aufzunehmen, sofern das Ziel ihr Entfernen nicht verlangt. Aus demselben Lauf: `LIKE` mit `%`/`_` aus der Eingabe → Konvention in `config/knowledge/rc81-ladder.yaml`, live importiert.

### BUG-335 – TEXT_NOT_FOUND schickt zum Neulesen, das danach abgelehnt wird, und nennt die falsche Ursache
- Phase: Schwierigkeitsleiter auf rc81, 2026-10-23 (A10#4, Job b9245e25) · Komponente: tools (`engine.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_tools_engine_read_write.py::test_a_replace_that_finds_nothing_shows_the_current_lines_and_allows_a_re_read`)
- Reproduktion: Im dritten Versuch von S001 scheitern vier `replace_text` an `tests/test_db.py` mit TEXT_NOT_FOUND. Der Hinweis lautet „the text exists with different leading/trailing whitespace – copy it exactly (read_range shows it)“. Tatsächlich ist der Text Teil einer längeren Zeile (`updated = db.update_task(…)`). Das Neulesen des Bereichs lehnt die Engine zweimal mit READ_REPEATED ab. Der Schritt stagniert und wird nach 9 Zügen blockiert; der Neuplan kostet 7 min.
- Ursache: Der Hinweis prüfte nur, ob der getrimmte Text irgendwo in der Datei steht, und schloss daraus auf Leerzeichen. Die Wiederholungssperre für Lesezugriffe galt auch, nachdem ein Ersetzen gezeigt hatte, dass das Bild des Coders von der Datei falsch ist.
- Fix: TEXT_NOT_FOUND bringt die aktuellen Zeilen der Datei mit (nummeriert wie bei `read_range`, bis zu drei Fundstellen der ersten Zeile von `old`, so viele Zeilen wie `old` hat). Es sagt, ob die Zeile Teil einer längeren Zeile ist oder sich in Leerzeichen bzw. den Folgezeilen unterscheidet. Nach TEXT_NOT_FOUND zählen frühere Lesezugriffe auf die Datei nicht mehr als Wiederholung.

### BUG-334 – Ein Werkzeugaufruf mit verschachteltem `args` wird abgelehnt und der ganze Inhalt neu geschrieben
- Phase: Schwierigkeitsleiter auf rc79, 2026-10-23 (A10#2, Job 922d0a40) · Komponente: tools (`registry.py`) · Severity: P3 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_tools_units.py::test_arguments_split_over_a_nested_args_object_are_taken_as_one_set`)
- Reproduktion: Der Coder schreibt `tests/test_partial_update.py` als `{"tool": "write_file", "args": {"args": {"path": …, "append": false}, "content": "…"}}`. Die Antwort `ARGS_INVALID` („path: Field required; args: Extra inputs are not permitted“) kostet einen Zug mit 2,5k Ausgabe-Tokens (95 s); im nächsten Zug schreibt er denselben Inhalt noch einmal.
- Ursache: Die Argumente wurden nur in der vorgesehenen Form angenommen, obwohl die Absicht eindeutig war.
- Fix: `validate_args` führt ein verschachteltes `args`-Objekt mit den übrigen Schlüsseln zusammen (die äußeren gewinnen), sofern das Werkzeug kein eigenes Feld `args` hat. Ein `args`, das kein Objekt ist, bleibt ungültig. Aus demselben Lauf: Der Code-/Testteil des Prompts änderte sich in 75 % der Züge, jeder Zug brauchte 35–45 s Prefill → D-026 (drei Cache-Stufen). Der Coder änderte die Signatur einer vorhandenen Funktion und brach sieben bestehende Tests → Konvention in `config/knowledge/rc78-ladder.yaml`, live importiert.

### BUG-333 – Der Plan lässt eine Datei weg, die der Auftrag nur beim Namen nennt
- Phase: Schwierigkeitsleiter auf rc78, 2026-10-23 (A10#1, Job d47e87ab) · Komponente: planner (`validation.py`, `planner.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_planner_validation.py::test_a_file_the_job_names_without_a_path_goes_into_the_step_that_delivers_it`)
- Reproduktion: Der Auftrag sagt „Die README beschreibt die neuen Felder und Parameter.“ Das Plan-Ziel verspricht „update the web UI and README“. Der einzige Implement-Schritt nennt aber weder README noch Oberfläche; nur der Prüfschritt führt `README.md` – als Datei, die sich nicht ändern darf. Der Coder arbeitet nach den Vorgaben des Schritts, die README bleibt unverändert (versteckt: README-Prüfung rot).
- Ursache: Die Rückverfolgung aus BUG-287/312 kennt nur wörtliche Angaben des Auftrags (Pfade, Namen in Backticks, Statuscodes). Ein Dateiname ohne Endung im Fließtext („die README“) ist keine solche Angabe, also bemerkte niemand die Lücke.
- Fix: `carry_job_named_files` vergleicht die Namen der Doku-, Seiten- und Konfigurationsdateien des Repositorys (auch `Dockerfile` ohne Endung) mit dem Auftrag. Nennt der Auftrag eine davon und kein ändernder Schritt die Datei oder ihren Namen, bekommt der liefernde Schritt sie als Hinweis, dazu den Auftragssatz im Ziel. Es gibt keine Reparaturrunde des Planers. Code-Dateien bleiben außen vor (sie erreicht der Coder über Importe), ebenso Namen, die mehr als zwei Dateien tragen. Für die Oberfläche („Weboberfläche“), die der Auftrag ohne Dateinamen beschreibt, gilt das nicht. Der Coder sieht den Auftrag aber weiter wörtlich im Abschnitt STEP GOAL.

### BUG-332 – Ein Review-Befund trägt kilobyteweise Selbstgespräch des Modells zum Coder
- Phase: Schwierigkeitsleiter auf rc78, 2026-10-23 (A10#1, Job d47e87ab) · Komponente: review (`text.py`, `severity.py`, `correction.py`), context_builder (`render.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_review_severity.py::test_untagged_self_talk_is_cut_from_findings`, `tests/unit/test_context_builder_sections.py::test_review_correction_items_are_clipped_and_without_self_talk`)
- Reproduktion: Nach einer Stagnation (derselbe Test 4× unverändert rot) prüft das Heavy Review und meldet einen Blocker. Dessen Beleg besteht aus 3,6 KB Überlegungen ohne Markierung („Actually, …“, „Let's look closer.“, „Why does it fail?“, „Wait, …“). Am Ende steht eine falsche Vermutung (positionelle Argumente), nicht die Ursache: Die Funktion gab die vor dem UPDATE gelesene Zeile zurück. Der Coder bekommt den Text ungekürzt als zu behebenden Befund. Der Job läuft nach 45 min ab (versteckt 31/40).
- Ursache: `strip_reasoning` entfernt nur markierte Überlegungen (`<think>` …). Die Korrekturanforderung kürzte Belege auf 600 Zeichen, die Korrektur-Einträge des Coders (`CorrectionItem.from_review`) dagegen gar nicht.
- Fix: `cut_self_talk` schneidet Zusammenfassung, Beleg und Vorschlag eines Befunds ab dem ersten Satz ab, der mit typischer Selbstrede beginnt, und setzt einen Hinweis dazu. Das gilt beim Einlesen der Review-Antwort, in der Korrekturanforderung und in den Coder-Einträgen. Beginnt ein Text selbst so, bleibt er stehen. `from_review` kürzt jeden Text auf 800 Zeichen. Die Ursache des roten Tests steht als Konvention in `config/knowledge/rc78-ladder.yaml` und ist live importiert.

### BUG-331 – Die Wissensschwelle des Coders lässt bei einer Python-Aufgabe die Hälfte der passenden Einträge weg
- Phase: Schwierigkeitsleiter auf rc77, 2026-10-23 (A8#1, Job 6ca05a5b) · Komponente: config (`MemoryPolicy.coder_start_min_score`) · Severity: P3 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_coder_handler.py::test_the_coder_gets_the_knowledge_that_fits_its_step_from_the_first_turn`)
- Reproduktion: A8#1 wird nach 40 min abgebrochen (versteckt 21/23). Die Ursachen werden zu drei neuen Konventionen in `config/knowledge/rc77-ladder.yaml`: SQLite `:memory:` je Verbindung, JSON-Body muss ein Objekt sein, `shutdown()` ohne laufendes `serve_forever()` blockiert. Gemessen gegen A8 liegen die passenden Einträge bei 0,45–0,57, fremde bei höchstens 0,40. Bei der Schwelle 0,5 hätten nur zwei von sechs den Coder erreicht.
- Ursache: Die Schwelle 0,5 aus BUG-328 war nur an A9 gemessen (passend ab 0,53). Wie ähnlich die Einträge sind, hängt von der Aufgabe ab: Der deutsche Auftrag mit einem englischen Schrittziel liegt bei Python-Einträgen niedriger.
- Fix: `coder_start_min_score` ist jetzt 0,45. Das trennt beide gemessenen Aufgaben: A9 bekommt seine acht PowerShell-Einträge (die Obergrenze), A8 sechs Python-Einträge, fremde Sprachen bleiben draußen.

### BUG-330 – Der Abschnitt STEP GOAL fasst nur drei Wissenseinträge
- Phase: Schwierigkeitsleiter auf rc76, 2026-10-23 (A9#7, Job 0fb0f6f5) · Komponente: context_builder (`budget.py`), config (`MemoryPolicy`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_context_builder_sections.py::test_the_coder_context_holds_six_knowledge_entries`, `tests/unit/test_context_builder_relevance.py::test_huge_inputs_respect_every_budget`)
- Reproduktion: Nach der Pester-Konvention aus A9#7 passen sechs aktive Einträge zur Aufgabe (0,53–0,63). STEP GOAL hat bei 32k Kontext 7 % ≈ 5 600 Zeichen; die Hälfte für Wissen fasst drei Einträge zu je 800–900 Zeichen. Die Coder-Prompts nutzten dabei höchstens 9,7k der rund 25k Tokens.
- Ursache: Der Anteil von STEP GOAL war für Ziel und Auftrag bemessen (BUG-212), nicht für Wissen. Je mehr gelernt wird, desto mehr passende Einträge fallen weg.
- Fix: STEP GOAL bekommt 12 % (Diff 10 → 6 %, letzter Fehler 8 → 7 %; ungenutztes Budget fester Abschnitte geht weiter an Code und Tests). Das Wissen bekommt alles, was Ziel und Auftrag vom Abschnitt übrig lassen, statt höchstens der Hälfte. `coder_start_entries` steht auf 8. Mit dem echten Coder-Fenster (32k, 8k Ausgabe) finden sechs Einträge zu je ~900 Zeichen neben Ziel und Auftrag Platz. Die Grenze setzen jetzt Ähnlichkeit und Budget, nicht eine feste Zahl. Der Ausgleich über Code und Tests ist unverändert, auch beim kleinsten Kontextfenster.

### BUG-329 – Ein Testlauf ohne Fehler-Exit-Code gilt als bestanden, obwohl seine Ausgabe gescheiterte Tests meldet
- Phase: Schwierigkeitsleiter auf rc76, 2026-10-23 (A9#7, Job 0fb0f6f5) · Komponente: tools (`engine.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_tools_engine_exec.py::test_a_command_whose_output_reports_failed_tests_fails_despite_exit_code_0`)
- Reproduktion: Im dritten Versuch ruft der Coder `pwsh -NoProfile -Command "Invoke-Pester -Path ./tests/… -Output Detailed"` ohne `-CI` auf. Die Ausgabe zeigt zehn gescheiterte Tests, der Exit-Code ist 0, `run_command` meldet „ok“. Die Wiederholung antwortet „It succeeded … otherwise call complete_step“, und der Coder schließt den Schritt ab. Der Verifier lehnt ab, der Job läuft nach 24 min ab (versteckt 9/10, alle eigenen Tests rot).
- Ursache: `run_command` entschied nur nach dem Exit-Code. Läufer, die bei Fehlern 0 zurückgeben (Pester ohne `-CI`), wurden als grün gemeldet; `run_test` las die Zusammenfassung schon, `run_command` nicht.
- Fix: Jeder Lauf mit Exit-Code 0 wird mit den Parsern von `run_test` gelesen. Meldet die Ausgabe gescheiterte Tests oder Fehler, gilt der Lauf als `COMMAND_FAILED` mit dem Hinweis, dass der Läufer den Exit-Code nicht setzt (`Invoke-Pester` braucht `-CI`). Die Wiederholung nennt die Ausgabe statt „It succeeded“. Allgemein für alle erkannten Testläufer. Die Ursache der roten Tests (Variable außerhalb von `BeforeAll`, Pester-Discovery) steht als Konvention in `config/knowledge/rc75-ladder.yaml` und ist live importiert.

### BUG-328 – Der Coder bekommt zu Beginn nur zwei Wissenseinträge, auch wenn mehr passen
- Phase: Schwierigkeitsleiter auf rc75, 2026-10-23 (A9#6, Job e9fac208) · Komponente: coder (`handler.py`), context_builder (`render.py`), config (`MemoryPolicy`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_coder_handler.py::test_the_coder_gets_the_knowledge_that_fits_its_step_from_the_first_turn`, `tests/unit/test_context_builder_sections.py::test_step_goal_carries_the_knowledge_that_fits_the_step`)
- Reproduktion: Das Skript ist richtig (versteckt 9/10, die JSON-Konvention hat gewirkt). Die eigenen Pester-Tests rufen es aber über `& $scriptPath @ArgsList` mit einem Array auf. Vier Tests scheitern 30 Züge lang an „The argument "-Status" does not belong to the set“, und der Job läuft nach 20 min ab.
- Ursache: Das Array-Splatting war noch nirgends als Wissen erfasst; es steht jetzt als Konvention in `config/knowledge/rc75-ladder.yaml`. Bei der Prüfung, ob sie den Coder erreichen würde: Zur Aufgabe passen dann fünf aktive PowerShell-Einträge (gemessen 0,52–0,63, fremde höchstens 0,43). `coder_start_entries=2` gab dem Coder davon nur die ersten zwei. Außerdem ließ ein zu langer Eintrag alle folgenden weg, und das Wissen bekam höchstens ein Drittel des Abschnitts.
- Fix: Der Coder bekommt jeden aktiven Eintrag ab `policies.memory.coder_start_min_score` (0,5), nächste zuerst, höchstens `coder_start_entries` (jetzt 5). Das Wissen darf die Hälfte von STEP GOAL belegen (mit BUG-330: alles, was Ziel und Auftrag übrig lassen). Ein Eintrag, der nicht passt, fällt weg, der nächste kommt trotzdem. Die Konvention ist live importiert.

### BUG-327 – `run_test` lehnt einen Sprachnamen als `framework` ab
- Phase: Schwierigkeitsleiter auf rc73, 2026-10-23 (A9#4, Job 2a8f51a9) · Komponente: tools (`registry.py`) · Severity: P3 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_tools_units.py::test_run_test_detects_the_parser_for_an_unknown_framework_name`)
- Reproduktion: Der Coder ruft `run_test` mit `framework: "powershell"` auf. Beide Aufrufe enden mit `ARGS_INVALID` („Input should be 'pytest', … 'pester' or 'generic'“), zwei Züge sind verloren.
- Ursache: `framework` wählt nur den Parser der Ausgabe, und den erkennt das Tool ohnehin aus Befehl und Ausgabe. Trotzdem wurde ein Name außerhalb der Liste als ungültiges Argument abgelehnt.
- Fix: Ein Name, der keinen Parser bezeichnet, überlässt die Wahl der Erkennung; ein bekannter Name zählt unabhängig von Groß- und Kleinschreibung.

### BUG-326 – PowerShell-Skripte werden nie geparst; ein Syntaxfehler überlebt zwei Versuche
- Phase: Schwierigkeitsleiter auf rc74, 2026-10-23 (A9#5, Job c2eadea1) · Komponente: verifier (`syntax.py`, `checks.py`), tools (`engine.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_verifier_engine.py::test_powershell_is_parsed_in_the_sandbox`, `tests/integration/test_tools_engine_exec.py::test_a_write_reports_the_syntax_errors_of_the_written_file`)
- Reproduktion: In `function Get-ServiceReport {` fehlt die schließende Klammer. Der Verifier meldet „no syntax checker for powershell“ und überspringt die Datei. Der Coder arbeitet 2 Versuche und 35 Züge weiter, ohne den Fehler zu sehen; der Job wird nach 20,5 min abgebrochen, versteckt 3/10.
- Ursache: Für PowerShell gab es keinen Syntaxprüfer, obwohl `pwsh` im Sandbox-Image liegt. Nach dem Schreiben lief nur der Lint, und den gibt es nur für Python; Syntaxfehler anderer Sprachen sah der Coder erst, wenn ein Test oder Lauf scheiterte.
- Fix: Allgemein für alle Sprachen mit Parser. Der Verifier parst `.ps1`/`.psm1` mit dem PowerShell-Parser (`[Parser]::ParseFile`) im Sandbox-Image und meldet jeden Fehler mit Zeile und Spalte; ohne `pwsh` wird übersprungen. Jede Sprache in `SANDBOX_TOOLS` wird so geroutet. Jedes Schreiben (`write_file`, `replace_text`, `apply_patch`) prüft die geschriebenen Dateien jetzt auch auf Syntax, wie der Verifier: JSON, YAML, TOML und Python direkt (Python nur ohne Lint-Befehl), PHP, JavaScript, Shell und PowerShell mit einem Sandbox-Befehl je Sprache. Ein Parser, der im Image fehlt, fügt nichts hinzu.

### BUG-325 – Der Coder sieht passendes Wissen erst nach einem Fehlschlag
- Phase: Schwierigkeitsleiter auf rc73, 2026-10-23 (A9#4, Job 2a8f51a9) · Komponente: coder (`handler.py`), context_builder (`render.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_coder_handler.py::test_the_coder_gets_the_knowledge_that_fits_its_step_from_the_first_turn`)
- Reproduktion: Die Konvention „`$items | ConvertTo-Json` macht aus einem Element ein Objekt“ ist aktiv, der Planer ruft sie für A9 ab. Der Plan nennt sie nicht, und der Coder schreibt wieder `$result | ConvertTo-Json`. Versteckt 6/10: ein Dienst, kein Dienst und `-OutFile` scheitern.
- Ursache: Wissen erreichte den Coder nur über `recall` und als Anhang an einen fehlgeschlagenen Lauf, dessen Fehlerzeilen zu einer Lehre passen. Ein Test, der den Fehler gar nicht bemerkt, bringt keine Lehre. Der Planer gibt sein Wissen nur weiter, wenn er es ins Ziel schreibt.
- Fix: Zu Beginn jedes Versuchs holt der Handler die aktiven Lehren und Konventionen, die Schrittziel und Auftrag am nächsten liegen (`policies.memory.coder_start_entries`, Standard 2). Sie stehen in STEP GOAL hinter dem Auftrag, als ganze Einträge in höchstens einem Drittel des Abschnitts. Kandidaten kommen nicht mit.

### BUG-324 – Ein Testlauf, der Dateien ins Repository schreibt, wird abgelehnt, ohne dass der Coder erfährt, wohin sie gehören
- Phase: Schwierigkeitsleiter auf rc73, 2026-10-23 (A9#4, Job 2a8f51a9) · Komponente: tools (`engine.py`) · Severity: P3 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_tool_run_output.py::test_a_run_that_created_files_outside_the_scope_says_where_temporary_files_belong`)
- Reproduktion: Die eigenen Pester-Tests schreiben `test_output.json` ins Repository. Jeder Lauf meldet `COMMAND_SCOPE_VIOLATION` mit „creating 'test_output.json' is outside allowed_new_paths“, die Datei wird entfernt. Der Coder ruft die Tests fünfmal auf, bis der Stall-Monitor stoppt.
- Ursache: Die Meldung sagt, dass die Datei außerhalb des Scopes liegt, aber nicht, dass sie aus dem Test stammt und wohin solche Dateien gehören.
- Fix: Hat ein Lauf Dateien außerhalb des Scopes angelegt, folgt der Hinweis, dass Tests temporäre Dateien in ein temporäres Verzeichnis schreiben (pytest `tmp_path`, `tempfile`, Pester `$TestDrive`, `os.tmpdir()`, `t.TempDir()`).

### BUG-323 – Der Lernlauf macht aus Lint-Korrekturen Lehren und erkennt dieselbe Lehre in anderen Worten nicht
- Phase: erster Lernlauf von D-025 auf rc73, 2026-10-23 · Komponente: memory (`distill.py`, `config.py`) · Severity: P3 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_distill.py::test_a_passing_correction_and_a_red_run_turned_green_are_fixes`)
- Reproduktion: Der erste Durchlauf über die Jobs der letzten zwei Tage schrieb sechs Lehren. Darunter waren „Remove unused variable assignments“ (eine F841-Lint-Korrektur, Ablauf `ruff` rot → grün) sowie zwei Umschreibungen der schon bekannten Lehre „setUp statt setUpClass“ als eigene Kandidaten.
- Ursache: Jede bestandene Korrektur und jeder rot → grün gewordene Lauf ging an das Modell, auch reine Lint-, Geheimnis-, Diff- und Scope-Befunde. Die Ähnlichkeitsschwelle 0,85 war geschätzt. Gemessen mit embeddinggemma liegt dieselbe Lehre in anderen Worten bei 0,76–0,83, verschiedene Lehren höchstens bei 0,58.
- Fix: Eine Korrektur lehrt nur mit mindestens einem dieser Befunde: fehlschlagender Test, Start-, Import-, Syntax-, Compile- oder Befehlsprüfung, Review-Befund `blocker`/`major`, Abdeckungslücke oder Stall. Nur deren Texte gehen an das Modell. Rote Läufe von Lintern und Formatierern zählen nicht. `lesson_similarity` steht jetzt auf 0,72.

### BUG-322 – Ein PowerShell-Schritt bekommt kein Testkriterium und wird ohne Ausweg blockiert
- Phase: Schwierigkeitsleiter auf rc72, 2026-10-23 (A9#1, Job 8273c77a) · Komponente: planner (`enrich.py`, `validation.py`) · Severity: P2 · blockierend: ja (die Leiter scheitert an A9) · Status: **behoben im Repository** (`tests/unit/test_planner_enrich.py::test_steps_that_write_tests_get_test_evidence_even_with_substantive_acceptance`)
- Reproduktion: A9 verlangt ein PowerShell-Skript mit Pester-Tests in einem leeren Repository. Der Plan hat nur Diff-Kriterien. Der Coder liefert Skript, Tests und README. Der Verifier meldet „implement step in a repository with tests has no test evidence“, der Handler blockiert mit `ACCEPTANCE_DEFECT`. Zwei Neupläne fügen wieder kein Testkriterium hinzu, Ergebnis `REPLAN_LIMIT` nach 19,2 min (versteckt 6/10).
- Ursache: Fehlt im Plan ein Testkriterium, leitet die Laufzeit es aus der Sprache der Dateien ab – bisher nur für Python, Go und JavaScript. Für PowerShell gab es keinen Läufer, und `*.Tests.ps1` neben dem Skript galt nicht als Testdatei.
- Fix: Für `.ps1`/`.psm1` nimmt die Laufzeit `pwsh -NoProfile -Command "Invoke-Pester -CI"` (Pester findet alle `*.Tests.ps1`; das PowerShell-Sandbox-Image hat es). `*.Tests.ps1` zählt überall als Testdatei.

### BUG-321 – Der Planer wiederholt ein Diff-Kriterium bis zur Obergrenze des Schemas
- Phase: Schwierigkeitsleiter auf rc72, 2026-10-23 (A7#10, Job 82793296) · Komponente: planner (`planner.py`, `prompt.py`) · Severity: P3 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_planner_loop.py::test_model_facing_schemas_leave_out_what_the_runtime_adds`)
- Reproduktion: Nach BUG-320 ist das Plan-Ziel kurz, der Plan von A7#10 braucht trotzdem 972 Tokens und 5,0 min. Er enthält dasselbe Diff-Kriterium zwölfmal, genau die Obergrenze des Schemas (12). In sechs der zwölf Pläne davor standen 10 Diff-Kriterien.
- Ursache: Wie bei BUG-288 schließt die Grammatik die Liste erst an der Obergrenze. qwen3.8 wiederholt ein Kriterium, statt die Liste zu schließen. Gespeicherte Schritte haben nach dem Entfernen der Duplikate höchstens 10 eigene Kriterien, 95 % höchstens 4.
- Fix: Das Modell sieht höchstens 8 Akzeptanzkriterien je Schritt. Der Prompt verlangt jedes Kriterium nur einmal, mit einem Diff-Kriterium für alle `must_change`- und `must_not_change`-Pfade. Die Laufzeit ergänzt ihre eigenen Kriterien weiterhin bis zur Vertragsgrenze.

### BUG-320 – Der Planer schreibt, was die Laufzeit selbst ergänzt, und wiederholt es bis zu achtmal
- Phase: Schwierigkeitsleiter auf rc71, 2026-10-22 (A7#8, Job 588a32c9) · Komponente: planner (`planner.py`, `prompt.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_planner_loop.py::test_model_facing_schemas_leave_out_what_the_runtime_adds`)
- Reproduktion: Der Planer (qwen3.8, 3–4 tok/s) braucht für den Ein-Schritt-Plan von A7 1.071 Ausgabe-Tokens und 5,9 min; der Job scheitert später am 40-min-Limit. In der Antwort stehen `scope` und `security` je fünfmal im selben Schritt, in 27 Planer-Antworten seit dem 21.10. bis zu achtmal. Das Plan-Ziel wiederholt den ganzen Auftrag (650 Zeichen), dazu vier Annahmen und zwei Risiken.
- Ursache: Die Laufzeit ergänzt Scope- und Security-Kriterien in jedem ändernden Schritt selbst und verwirft Duplikate (`enrich.generate_acceptance`). Prompt und Schema boten sie dem Modell trotzdem an. Plan-Ziel, Zusammenfassung, Annahmen und Risiken liest kein Worker, denn jeder bekommt den Auftragstext selbst. Das Schema erlaubte dafür trotzdem 4.000 Zeichen und je 40 Einträge.
- Fix: Das Schema an das Modell enthält keine Scope- und Security-Kriterien und keine Zusammenfassung mehr. Es begrenzt das Plan-Ziel auf 300 Zeichen und Annahmen und Risiken auf je drei Einträge mit 200 Zeichen. Der Prompt sagt, dass die Laufzeit beide Prüfungen selbst ergänzt und das Ziel ein Satz ist. Die Verträge bleiben unverändert, gilt für Planer und Replaner.

### BUG-319 – Die Abdeckungskorrektur schreibt Tests mit einer Bibliothek, die die Sandbox nicht hat
- Phase: Messung D-024 auf rc70, 2026-10-22 (A8, Job 7f379fe0) · Komponente: coder (`coverage.py`) · Severity: P3 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_coverage.py::test_the_correction_names_the_tools_of_the_existing_tests`)
- Reproduktion: S001 von A8 besteht den Verifier mit Tests nur für die Speicherschicht. Die Abdeckungsprüfung meldet zu Recht über 20 ungetestete Anforderungen der HTTP-API (8 gehen in die Korrektur). Der Coder schreibt `tests/test_http.py` mit `import requests`. Die Sandbox installiert kein Paket ohne Manifest, der Coder blockiert den Step nach 3,5 min. Der Rückfall auf den verifizierten Stand greift (`correction.fell_back`), die neuen HTTP-Tests sind verloren.
- Ursache: Der Korrekturauftrag sagte nur „schreibe einen Test neben den bestehenden“. Womit die bestehenden Tests arbeiten, stand nicht darin.
- Fix: Der Auftrag verlangt das Test-Framework und die Bibliotheken der bestehenden Tests, nennt deren Python-Importe (`they import json, urllib, …`) und schließt neue Pakete aus.

### BUG-318 – Die Maskierung verdeckt einen Variablennamen genau in der fehlerhaften Zeile
- Phase: Schwierigkeitsleiter auf rc68, 2026-10-22 (A7#5, Job 9ee37ef1) · Komponente: core (`redaction.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_config_and_redaction.py::test_a_variable_named_like_a_secret_stays_visible`)
- Reproduktion: Die Test-Fixture des Coders baut `env = {**os.environ, "NOTES_API_TOKEN": token, "PORT": str(port)}`, benutzt das Dict aber nie. Der Server im selben Prozess sieht keinen Token, und jeder autorisierte Test bekommt 401. Der Coder sieht die Zeile als `"NOTES_API_TOKEN": ***REDACTED***`. `replace_text` auf der Zeile wird abgelehnt („nothing would change … ***REDACTED***“). Zwei Stagnationsstopps, ein Heavy Review, ein Replan von 13 min, dann Zeitabbruch nach 40,8 min. Versteckt 30/31: Der Server ist richtig, nur die eigenen Tests scheitern.
- Ursache: Ein unquotierter Wert hinter einem geheimnisartigen Schlüssel galt nur dann als Code, wenn er Punkt oder Klammer enthält (`self.token`, `config["x"]`, BUG-225). Ein nackter Variablenname wie `token` wurde maskiert. Die Regel für Rollenwörter (`test-token-123`, `wrong-token`, BUG-293) galt nur für quotierte Werte.
- Fix: Ein Wert, der nur aus Rollenwörtern besteht, ist auch unquotiert kein Geheimnis (`token`, `auth_token`, `new_password`, `api_key`). Echte Werte wie `hunter2realpw` oder `hX9q2LmP4tR8` bleiben maskiert.

### BUG-317 – Ein Abbruch mitten in der Review-Korrektur hinterlässt halb bearbeiteten Code
- Phase: Schwierigkeitsleiter auf rc67, 2026-10-21 (A7#4, Job 080a8174) · Komponente: coder (`handler.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_coder_handler.py::test_a_job_cancelled_during_the_review_correction_keeps_the_verified_work`)
- Reproduktion: S001 besteht um 23:00 und 23:15 die Verifikation (20 von 21 Prüfungen). Das Review verlangt zweimal eine Korrektur, unter anderem wegen eines angeblich doppelten `main()`, das den Start verhindere, und englischer Doku. `save_verified` sichert den Stand. In der dritten Korrektur entfernt der Coder einen Import aus `notes_api/server.py`, dann bricht das Zeitlimit den Job ab (23:23). Bewertet wird der halb bearbeitete Stand: ImportError, versteckt 2/31, K.o. als unvollständiges Programm. Der gesicherte Stand besteht nachträglich alle 31 versteckten Prüfungen.
- Ursache: Der Rückfall auf den verifizierten Stand (BUG-315) griff nur bei einem blockierten Schritt. Ein abgebrochener Schritt ließ den Arbeitsbaum so, wie der Korrekturversuch ihn gerade hatte.
- Fix: `_keep_verified` stellt den gesicherten Stand auch bei `cancelled` wieder her und schreibt `correction.fell_back` mit `outcome: cancelled`. Ohne erneute Verifikation und ohne Commit, denn der Job ist abgebrochen. Im Arbeitsbaum liegt dann die verifizierte Arbeit statt der halben Änderung.

### BUG-316 – Hängt das gelieferte Programm, wertet das Turnier den Lauf als Infrastrukturfehler
- Phase: Schwierigkeitsleiter auf rc67, 2026-10-21 (A7#4, Job 9273d030) · Komponente: tournament (`runner.py`), versteckte Prüfungen A7/A8 · Severity: P3 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_tournament.py::test_hidden_checks_that_hang_on_the_delivered_program_are_graded_not_repeated`)
- Reproduktion: Die eigenen Tests des gelieferten Servers enden nicht (schon der Coder sah 60 und 120 s `COMMAND_TIMEOUT`). Die versteckten Prüfungen geben ihre Fortschrittszeile aus (`FFFFFFFFFFFFF.FFFFFFFFFFFFFFFF`), dann wartet `test_own_tests_pass` bis zu 600 s auf die eigenen Tests. Genauso lang ist das Limit der ganzen Prüfung, die Sandbox beendet sie also ohne Zusammenfassung. Der Runner meldet „hidden checks produced no result (exit None)“. Der Lauf zählt als Infrastruktur und wird wiederholt, 10 min Bewertung und 40 min Lauf ohne Wertung.
- Ursache: `grade` behandelte jede Ausgabe ohne Zählzeile als Infrastrukturfehler, auch wenn die Sandbox das Programm ausgeführt und erst am Zeitlimit beendet hatte. Dazu war das innere Limit von `test_own_tests_pass` (A7, A8) so lang wie das äußere, sodass die Prüfung ihren eigenen Timeout nie melden konnte.
- Fix: `grade_output` wertet eine am Zeitlimit beendete Prüfung aus der pytest-Fortschrittszeile und zählt die hängende Prüfung als Fehlschlag („hidden checks timed out (the delivered program hangs)“). Infrastruktur bleibt nur eine Prüfung, die gar nicht laufen konnte. `test_own_tests_pass` in A7 und A8 wartet höchstens 300 s, wie schon A4.

### BUG-315 – Eine vom Review verlangte Korrektur zerstört den verifizierten Stand
- Phase: Schwierigkeitsleiter auf rc66, 2026-10-21 (A7#3, Job e9782a8c) · Komponente: coder (`handler.py`), gitops (`engine.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_coder_handler.py::test_a_failed_review_correction_falls_back_to_the_verified_work`, `::test_a_blocker_finding_keeps_no_state_to_fall_back_to`, `tests/integration/test_gitops_engine.py::test_the_verified_state_comes_back_exactly_after_a_failed_correction`)
- Reproduktion: S001 besteht um 21:07 die Verifikation (22 von 23 Prüfungen, 1 übersprungen). Das Review verlangt mit zwei „major“-Befunden eine Korrektur. Einer davon dreht die Fakten um: Die Titelprüfung lasse reine Leerzeichen durch, weil sie `strip()` aufruft – genau das lehnt sie ab. Der Korrekturversuch schwächt die Prüfung, scheitert fünfmal an `replace_text`, schreibt `notes_api/server.py` neu, danach hängen die Tests. Der Coder blockiert den Schritt (`test_conflict`), der Replan braucht 8 min, der neue Schritt bekommt 3 min. Ergebnis: Abbruch nach 40,1 min, versteckt 11/31, Sicherheitsprüfung durchgefallen.
- Ursache: Ein blockierter Korrekturversuch ging immer an den Replanner. Der verifizierte Stand vor der Korrektur war zu dem Zeitpunkt schon überschrieben. So konnte ein falscher oder nicht umsetzbarer Review-Befund fertige Arbeit vernichten.
- Fix: Verlangt das Review eine Korrektur verifizierter Arbeit ohne Blocker-Befund, legt `GitEngine.save_verified` die nicht committeten Änderungen im Git-Verzeichnis des Workspaces ab (geänderte, neue und gelöschte Dateien, je Schritt). Endet der Schritt danach blockiert, stellt `restore_verified` genau diesen Stand wieder her. Der Verifier prüft ihn erneut. Besteht er, wird der Stand committet und das Ereignis `correction.fell_back` geschrieben; sonst bleibt der Schritt blockiert. Bei einem Blocker-Befund wird nichts aufbewahrt.

### BUG-314 – Die Rolle `ollama` startet Ollama mit der alten Unit-Konfiguration neu
- Phase: Rollout von BUG-313 auf .224, 2026-10-21 (`bootstrap.yml --limit model_workers --tags ollama`) · Komponente: infra (Rolle `ollama`, Handler) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_bootstrap_infra.py::test_the_ollama_restarts_reload_systemd_themselves`)
- Reproduktion: Der Rollout schreibt den Drop-in mit `CUDA_VISIBLE_DEVICES=-1` (changed) und startet Ollama um 18:38:34 neu. Der neue Prozess hat die Variable trotzdem nicht, und Ollama meldet weiter `library=CUDA`. `systemctl show ollama -p Environment` zeigt sie erst nach dem Lauf. Im Log läuft `RUNNING HANDLER [ollama : Restart ollama]` vor `RUNNING HANDLER [systemd_skeleton : Reload systemd]`.
- Ursache: Vier Rollen haben einen Handler namens „Reload systemd“. `notify: Reload systemd` landet beim Handler von `systemd_skeleton`, und Handler laufen in der Reihenfolge ihrer Definition, die Ollama-Restarts also zuerst. Systemd startet dann die Unit ohne Neuladen mit der alten Konfiguration. Das betrifft jede Änderung an Ollamas Drop-in und an der llama.cpp-Unit beim ersten Lauf.
- Fix: Die Handler „Restart ollama“ und „Restart llama-server“ setzen `daemon_reload: true` und laden systemd damit selbst unmittelbar vor dem Neustart neu, unabhängig von der Reihenfolge der Handler.

### BUG-313 – Ollama belegt die GPU im Modellwechsel, danach lädt qwen3.6 nicht mehr
- Phase: Schwierigkeitsleiter auf rc66, 2026-10-21 (A7#2, Job 1f6e865f) · Komponente: infra (Rolle `ollama`, `infra/systemd/ollama-hermclaw.conf.j2`) · Severity: P1 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_bootstrap_infra.py::test_ollama_stays_off_the_gpu_while_llamacpp_holds_it`)
- Reproduktion: Der Planer endet um 17:57:30, der Router entlädt qwen3.8:27b. Eine Sekunde später kommt der Embedding-Aufruf des Coder-Zugs bei Ollama an. Ollama sieht die leere GPU und lädt `embeddinggemma:300m` ganz darauf (25/25 Schichten, 587 MiB). qwen3.6:35b scheitert danach mit `cudaMalloc failed: out of memory` an seinen Rechenpuffern (476 MiB). Jeder neue Coder-Versuch ruft zuerst wieder Embeddings auf und verlängert Ollamas Keep-Alive (5 min). So scheitern alle fünf Coder-Aufrufe bis 18:16 mit `MODEL_LOAD_FAILED`, und der Job läuft ins Zeitlimit (versteckt 1/31). Ausgelöst hat das Entladen eine Test-Anfrage aus der Abnahme-Sitzung, die 3 s vor dem Coder-Aufruf kam. Dieselbe Lücke entsteht aber bei jedem Modellwechsel, in den ein Embedding-Aufruf fällt.
- Ursache: Seit D-015 gehört die GPU von .224 dem residenten llama-server. Ollama bedient dort nur noch das Embedding-Modell, durfte aber weiter die GPU nutzen. Um 17:45 lag das Modell noch auf der CPU, weil die GPU voll war. Nach dem Entladen nahm Ollama den freien Speicher und gab ihn nicht mehr her.
- Fix: Ist `ollama_llamacpp_enabled` gesetzt, bekommt Ollamas Drop-in `CUDA_VISIBLE_DEVICES=-1`. Damit rechnet Ollama nur auf der CPU, wie um 17:45, als die 21 Embeddings des Jobs zusammen 0,5 min brauchten. Ohne llama.cpp bleibt alles wie bisher. Die Prüfung der Rolle, dass Ollama die GPU gefunden hat, läuft entsprechend nur noch ohne llama.cpp, sonst würde der Rollout scheitern.

### BUG-312 – Ein fehlendes Auftragsdetail kostet eine ganze Planer-Reparaturrunde
- Phase: Schwierigkeitsleiter auf rc65, 2026-10-21 (A4#2, Job 262a2882; A7#1, Job 0d3b3256) · Komponente: planner (`validation.py`, `planner.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_planner_validation.py::test_a_job_literal_no_step_names_goes_into_the_step_that_delivers_it`)
- Reproduktion: Der erste Plan zu A7 nennt `Authorization: Bearer <token>`, `{"title": ..., "text": ...}` und `{"error": "..."}` aus dem Auftrag nicht. Die Prüfung aus BUG-287 schickt den Plan zweimal zurück an qwen3.8:27b, das bei 2,2 Tokens/s je Runde rund 10 min braucht. Von 40 min gingen 29 an den Planer, der Coder lief mit 29/31 versteckten Prüfungen ins Zeitlimit. Bei A4 dasselbe mit `Giro: 1234.50` (Planer 27 von 36 min, versteckt 9/9, trotzdem abgebrochen).
- Ursache: Ein fehlender Ausdruck ist kein Planungsfehler, den nur das Modell beheben kann. Welcher Schritt ihn liefert, steht im Satz des Auftrags, der ihn nennt. Die Prüfung meldete ihn trotzdem als Fehler und ließ den ganzen Plan neu erzeugen.
- Fix: `carry_job_literals` schreibt jeden fehlenden Ausdruck in das Ziel des liefernden Schritts („Exactly as the job states: …“), bevor die semantische Prüfung läuft. Liefernd ist der arbeitende Schritt, dessen Dateien der Satz des Ausdrucks nennt, sonst der mit den meisten gemeinsamen Wörtern, sonst der erste. Schritte, die nur prüfen, bekommen nichts. Gibt es keinen arbeitenden Schritt, meldet die Prüfung aus BUG-287 den Fehler wie bisher.

### BUG-311 – Ein Review-Befund ohne Pfad lässt den Coder die genannte Datei übergehen
- Phase: Schwierigkeitsleiter auf rc64, 2026-10-21 (A4, Job 69813999) · Komponente: review (`severity.py`, `reviewer.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_review_severity.py::test_a_finding_without_a_path_takes_the_one_known_file_its_text_names`, `tests/integration/test_review_reviewer.py::test_a_finding_without_a_path_names_a_scope_file_in_the_correction`)
- Reproduktion: Das Review meldet als Blocker „The program crashes with IndexError when run without arguments because `__main__.py` accesses `argv[1]`“. Das Feld `path` lässt es leer. Im ersten Korrekturversuch lässt der Coder nur zweimal pytest laufen und schließt ab: „__main__.py … is outside the current scope“. Dabei steht `ledger/__main__.py` in den `target_paths`. Im zweiten Versuch behebt er nur die Formatierung. Die Korrekturen sind aufgebraucht, der Replan kostet weitere 10 min, und der Job läuft ins Zeitlimit (versteckt 8/9).
- Ursache: Ohne Pfad nennt die Korrekturanforderung keine Datei. Der Wächter aus BUG-258 lehnt `complete_step` nur ab, solange eine *genannte* Datei unberührt ist. Hier hatte er also keine Datei zu prüfen. Der Prompt verlangt zwar einen Pfad je Befund, das Modell hält sich aber nicht immer daran.
- Fix: Hat ein Befund keinen Pfad, sucht `normalise_findings` in Zusammenfassung, Beleg und Lösungsvorschlag nach Dateinamen. Gezählt werden nur bekannte Dateien: geänderte Dateien, Scope-Ziele, erlaubte neue Pfade und die dem Reviewer gezeigten Dateien. Ein Name zählt als voller Pfad oder als eindeutiges Pfadende wie `__main__.py`. Nennt der Text genau eine solche Datei, wird sie zum Pfad des Befunds. Bei mehreren oder keiner bleibt der Pfad leer, geraten wird nicht. Damit erscheint die Datei in der Korrektur, und BUG-258 greift.

### BUG-310 – Jeder Gateway-Start meldet die dokumentierten Modellabweichungen als Warnung
- Phase: GLM-Nachtest auf rc63, 2026-10-21 (Log des Turnier-CLI) · Komponente: models (`gateway.py`, `profiles.py`) · Severity: P4 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_models_profiles.py::test_the_gateway_logs_a_documented_deviation_only_at_debug_level`)
- Reproduktion: `hermclaw coder-tournament …` schreibt elfmal „model profile warning“ ins Log, je Gateway erneut. Das sind die elf Profile mit begründeter Abweichung: D-015 und die Turnierkandidaten.
- Ursache: Das Gateway loggt jede Warnung der Architekturprüfung. Eine dokumentierte Abweichung ist aber eine getroffene Entscheidung, keine Auffälligkeit. Das Rauschen verdeckt echte Warnungen.
- Fix: `ProfileIssue.documented` markiert begründete Abweichungen. Das Gateway loggt sie nur noch auf Debug-Ebene. `config-check` und Doctor zeigen sie weiterhin.

### BUG-309 – Der Verifier nimmt ein Programm ab, das nicht startet
- Phase: Coder-Turnier auf rc62, Nachbereitung 2026-10-21 (Ornith 35B A7#3 und A8#2, Agents-A1 A8#1) · Komponente: verifier (`checks.py`, neu `entrypoints.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_verifier_engine.py::test_a_delivered_package_that_does_not_start_fails`, `tests/unit/test_verifier_entrypoints.py`)
- Reproduktion: Ornith A8#2 endet `succeeded`, die eigenen Tests sind grün. `python -m tasks_app` bricht beim Start ab: `ImportError: cannot import name 'make_server' from 'tasks_app.server'`. Von 23 versteckten Prüfungen besteht eine. Ornith A7#3 ebenso: `import sys` steht nur im Fehlerzweig von `main()`, damit ist `sys` in der ganzen Funktion lokal. Beim normalen Start folgt `UnboundLocalError`, 3 von 31 Prüfungen bestehen. ruff meldet beides nicht.
- Ursache: Die Tests der Modelle bauen den Server im Prozess und führen `__main__.py` nie aus. Der Verifier prüfte Syntax, Importe, Lint und die Kriterien des Schritts, startete den gelieferten Startpunkt aber nie. Für den Verifier war das Programm damit fertig.
- Fix: Neue Prüfung `start:<Paket>`. Für jedes geänderte Paket mit `__main__.py` startet der Verifier einmal `python -m <Paket>` in der Sandbox. Er startet aus einem temporären Verzeichnis, mit geschlossenem stdin. Umgebungsvariablen setzt er nur, wenn der Name ihre Bedeutung verrät: Port, Host, Pfade, Token. Bestanden ist der Start, wenn das Programm nach `policies.verifier.start_seconds` (5 s) noch läuft oder ohne Traceback endet; eine dokumentierte Ablehnung zählt also nicht als Fehler. Ein Traceback mit `ImportError`, `NameError`, `UnboundLocalError`, `SyntaxError` oder `AttributeError` lässt den Schritt scheitern, andere Tracebacks werden ohne Blockade gemeldet. Der Coder bekommt die Fehlermeldung und die Anweisung, das Programm selbst einmal zu starten.

### BUG-308 – Ein Client kann seinen Job vom Selbstlernen ausnehmen
- Phase: volle Testsuite vor dem Release rc63, 2026-10-21 (`tests/unit/test_job_metadata.py::test_every_job_metadata_read_is_a_reserved_key`) · Komponente: memory (`learner.py`), core (`job_metadata.py`) · Severity: P3 · blockierend: nein · Status: **behoben im Repository** (`tests/unit/test_job_metadata.py::test_a_client_cannot_keep_its_job_out_of_learning`)
- Reproduktion: `POST /api/jobs` mit `metadata: {"memory_learned_at": "x"}` wird angenommen. Der Lerner hält den beendeten Job für schon gelesen und lernt nichts aus ihm.
- Ursache: Der Lerner liest und schreibt den Metadaten-Schlüssel `memory_learned_at`. Er war nicht in `hermclaw/core/job_metadata.py` reserviert (BUG-140: Laufzeitschlüssel darf ein Client nicht setzen).
- Fix: `MEMORY_LEARNED_AT` ist reserviert, `POST /api/jobs` lehnt ihn mit 422 ab (`JobCreate`-Schema und OpenAPI neu erzeugt). Der Scanner der Metadaten-Tests erkennt das JSON-Merge des Lerners als Schreibzugriff.

### BUG-307 – Selbstlernen: kein Modell bekannt, globale Repo-Recherche, Lehren aus Einzelfällen, Verdacht gegen Schutzregeln
- Phase: Probelauf des Lerners (D-019 Teil 3) gegen eine Kopie der Produktions-DB (200 beendete Jobs), 2026-10-21, vor jedem Release des Lerners · Komponente: memory (`learner.py`), coder (`handler.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_memory_learner.py::test_models_are_known_from_the_coder_turns_and_one_way_past_in_three_is_no_lesson`, `::test_research_on_a_repository_stays_with_that_repository`, `::test_a_guard_refusal_is_no_tool_bug_suspicion`, `tests/integration/test_coder_handler.py::test_implement_step_happy_path_commits_and_pushes`)
- Reproduktion: Lerner mit `learn_since_days=0` über die kopierte DB. (1) `step_attempts.model_alias` ist in allen 692 Versuchen leer: Stalls nennen „model ?“, und kein Werkzeug-Bug-Verdacht kann je zwei Modelle zählen; die automatische Bug-Erkennung war damit tot. (2) Zwölf Recherchen wie „What is the programming language and web framework used in this repository?“ wurden aktives Wissen für alle Jobs. (3) `COMMAND_REPEATED` wurde aktiv mit „What helped: read_file → run_command (1 of 5 recent cases)“. (4) Nach Fix (1) gab es neun Verdachtsfälle, sieben davon gegen Schutzregeln (`COMMAND_REPEATED`, `READ_REPEATED`, `NO_CHANGE`, `CHECK_STILL_FAILING`, `REDACTED_PLACEHOLDER`).
- Ursache: (1) Der Coder-Handler hat das Feld nie gesetzt; das Modell steht nur in `model_invocations` (`purpose = coder_turn`). (2) Recherchen wurden ohne Scope gespeichert. (3) Aktiviert wurde, sobald irgendein Fall weiterkam. (4) Der Verdacht unterschied nicht zwischen einem Werkzeug, das eine richtige Aktion ablehnt, und einer Schutzregel, die eine Wiederholung oder eine verbotene Aktion ablehnt.
- Fix: (1) Der Handler trägt das Coder-Modell in den Versuch ein; für ältere Versuche liest der Lerner es aus den Coder-Turns. (2) Recherchen eines Jobs mit Repository bekommen den Scope `repo:<Name>`. (3) Aktiv wird ein Vorfall erst, wenn in mindestens der Hälfte der letzten Fälle etwas half. (4) Schutzregeln lösen keinen Verdacht aus, und der Verdacht verlangt, dass mindestens die Hälfte der letzten Fälle nicht weiterkam. `COMMAND_SCOPE_VIOLATION` bleibt verdächtig.
- Ergebnis des Probelaufs danach: genau zwei Verdachtsfälle, `replace_text:TEXT_NOT_FOUND` (die Klasse von BUG-301) und `run_test:COMMAND_SCOPE_VIOLATION` (Pester-Bericht, BUG-305). Beide waren echte Werkzeug-Bugs, die bisher von Hand gefunden wurden.

### BUG-306 – Das Selbstlernen zählt rote Testläufe als Werkzeug-Ablehnung
- Phase: Nachbereitung D-019 Teil 3, 2026-10-21 (Durchsicht gegen die Werkzeugaufrufe der Produktions-DB, vor jedem Release des Lerners) · Komponente: memory (`learner.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_memory_learner.py::test_red_test_runs_are_work_in_progress_not_refusals`)
- Reproduktion: Zwei beendete Jobs zweier Modelle mit je zwei `run_test` → `TESTS_FAILED` ohne späteren grünen Lauf. Der Lerner legt den Vorfall `tool:run_test:TESTS_FAILED` an und meldet einen Werkzeug-Bug-Verdacht mit Bug-Eintrag `AUTO-…` gegen `run_test`. In der Produktions-DB sind `TESTS_FAILED` (523) und `COMMAND_FAILED` (369) nach `COMMAND_REPEATED` die häufigsten Fehlercodes; fast jeder Job hätte solche Vorfälle erzeugt.
- Ursache: Der Lerner wertete jeden Werkzeugaufruf mit Fehlercode als Ablehnung. Ein Befehl oder Test, der lief und fehlschlug, ist aber das Ergebnis des Codes in Arbeit, keine Ablehnung des Werkzeugs. Der Status der Zeile (`failed`/`refused`) trennt das nicht: auch `TEXT_NOT_FOUND` ist `failed`.
- Fix: `errors.RUN_FAILED` (`COMMAND_FAILED`, `COMMAND_TIMEOUT`, `TESTS_FAILED`, `TEST_ERROR`) und `SANDBOX_ERROR` (Infrastruktur) zählen für den Lerner nicht als Ablehnung. Was ein fehlgeschlagener Lauf lehrt, erreicht den Coder über das Wissen zu seinen Fehlerzeilen.

### BUG-305 – Ein Testbericht neben den Tests macht jeden Testlauf zum Scope-Verstoß und verdeckt die Fehlschläge
- Phase: Coder-Turnier 0.1.0rc62, 2026-10-21 (Auswertung: coder-main A9, PowerShell) · Komponente: tools (Befehls-Seiteneffekte, `snapshot.py`) · Severity: P2 · blockierend: nein · Status: **behoben im Repository** (`tests/integration/test_tools_engine_exec.py::test_test_reports_a_command_writes_are_cleaned_not_violations`)
- Reproduktion: `pwsh -NoProfile -Command "Invoke-Pester -Path tests -CI"` schreibt `testResults.xml` ins Arbeitsverzeichnis. Jeder Lauf endet mit `COMMAND_SCOPE_VIOLATION` („creating 'testResults.xml' is outside allowed_new_paths … (reverted)“). Die eigentliche Meldung darüber („9 failed – Could not find Command Get-Service“) geht unter; coder-main ändert achtmal die Tests und ruft den Lauf viermal identisch erneut auf (`COMMAND_REPEATED`).
- Ursache: Testberichte zählten nicht zu den erzeugten Dateien, die nach einem Befehl entfernt werden (wie Caches und Laufzeitdaten seit BUG-214). Das Anlegen wurde als Scope-Verstoß gewertet, und der Fehlercode des Verstoßes ersetzt den des Befehls.
- Fix: `testResults.xml`, `junit.xml`, `coverage.xml`, `.coverage.*`, `htmlcov/` und `TestResults/` gehören zu `DEFAULT_GENERATED_GLOBS`. Ungetrackt und außerhalb des Scopes werden sie nach dem Befehl entfernt (`data.cleaned`), der Befehl behält sein Ergebnis (`COMMAND_FAILED`, `TESTS_FAILED`).
- Auswertung dazu: In den 27 coder-main-Läufen des Turniers waren die verschwendeten Werkzeugaufrufe vor allem `COMMAND_REPEATED` (54), `COMMAND_SCOPE_VIOLATION` (17), `READ_REPEATED` (13) und `NO_CHANGE` (7).

### BUG-304 – Turnierwertung: ein Programm, das nicht startet, ist kein K.o.
- Phase: Coder-Turnier 0.1.0rc62, 2026-10-21 (Auswertung des vollen Turniers) · Komponente: tournament (scoring, runner) · Severity: P2 · blockierend: nein (Sieger unverändert) · Status: **behoben im Repository** (`tests/unit/test_tournament.py::test_a_program_the_hidden_checks_cannot_run_is_knocked_out`)
- Reproduktion: Ornith 35B, A7 #3 meldet „succeeded“, der gelieferte Server startet nicht: „3 passed, 28 errors“ in den versteckten Prüfungen (Setup-Fehler `python -m … ` bei jedem Test). Ebenso A8 #1–#3 („1 passed, 22 errors“). Die Läufe bekamen Punkte für „abgeschlossen“ und „keine unnötigen Änderungen“ wie ein laufendes Programm.
- Ursache: Die mit dem Nutzer vereinbarten K.-o.-Regeln nennen „unvollständige Programme“. Umgesetzt war nur, dass Aufwand und Dauer allein korrekte Läufe belohnen; Setup-Fehler zählten wie fehlgeschlagene Prüfungen.
- Fix: Die Bewertung zählt Setup-Fehler getrennt (`hidden_errors`). Erreichen sie mindestens die Hälfte der versteckten Prüfungen, ist der Lauf K.o. („incomplete program: 28 of 31 hidden checks could not run it“). Ältere Läufe lesen die Zahl aus der gespeicherten Bewertungsausgabe nach.
- Auswirkung auf das Turnier (rc62, A1–A9 × 3): coder-main 77,3 (unverändert Sieger), Qwen3.5-9B 52,4, Ornith 35B 51,0 (5 K.o. statt 1). Platz 2 und 3 tauschen; die frühere Aussage „an der Reihenfolge ändert das nichts“ war falsch.

### BUG-303 – Ein Verifier, der seine Befehle nicht ausführen konnte, verbraucht die Korrekturen des Modells
- Phase: Nachbereitung des Coder-Turniers, 2026-10-21 (Code-Durchsicht nach BUG-286; live seit 16.10. nicht aufgetreten, keine Prüfung mit Status `error` in der Datenbank) · Komponente: coder (Implement-Handler), verifier, scheduler · Severity: P2 · blockierend: nein · Status: **behoben im Repository, Auslieferung mit dem Release nach dem Coder-Turnier** (`tests/integration/test_coder_handler.py::test_a_verifier_that_could_not_run_is_repeated_not_corrected`, `tests/failure/test_verifier_failures.py::test_executor_outage_is_an_error_and_redacted`)
- Reproduktion: Der Ausführungs-Worker ist nicht erreichbar oder der Sandbox-Container startet nicht (Podman-Engine-Fehler, BUG-286). Alle Befehlsprüfungen werden `error` („could not be executed“), `test_evidence` wird `fail` („no test command executed and passed“). Der Implement-Handler startet eine Korrekturrunde mit diesen Befunden; nach zwei solchen Runden ist das Korrekturbudget leer und der Schritt geht an den Replanner, obwohl die Änderung nie geprüft wurde.
- Ursache: BUG-286 machte aus einem Engine-Fehler eine `error`-Prüfung statt eines Lint-Befunds. Der Handler unterschied aber nicht zwischen „Prüfung fehlgeschlagen“ und „Prüfung lief nicht“, und `test_evidence` machte aus der fehlenden Ausführung einen echten Fehlschlag.
- Fix: Prüfungen, deren Befehl der Executor nicht ausführen konnte, tragen `executor_error` in den Belegen (Befehl, Test, Kompilieren, Lint, Syntax in der Sandbox; `test_evidence`, wenn kein Testbefehl laufen konnte). Sind alle blockierenden Fehler davon, wiederholt der Handler die Verifikation nach 5 s und 20 s. Scheitert sie weiter, endet der Versuch mit `VERIFIER_UNAVAILABLE`: Der Scheduler behandelt das wie einen Modell-Ausfall (BUG-197), der Versuch zählt nicht und es gibt keine Korrektur.

### BUG-302 – Lint-Befunde erreichen den Coder erst beim Abschluss, er arbeitet nur den ersten ab
- Phase: Coder-Turnier 0.1.0rc62, 2026-10-20 (Agents-A1, A3 Lauf 1) · Komponente: tools (Schreibwerkzeuge, `complete_step`) · Severity: P2 · blockierend: nein (Lauf bestanden, 20 statt ~7 Modellaufrufe) · Status: **behoben im Repository, Auslieferung mit dem Release nach dem Coder-Turnier** (`tests/integration/test_tools_engine_exec.py::test_a_write_reports_the_lint_findings_of_the_written_file`)
- Reproduktion: Der Coder schreibt `messwerte.py` und `tests/test_messwerte.py` mit je einem ungenutzten Import. Erst `complete_step` meldet `CHECK_STILL_FAILING` mit beiden F401-Befunden. Der Coder entfernt nur den ersten und ruft danach siebenmal ruff mit erfundenen Optionen auf (`--config /dev/null`, `--per-file-ignores F401:messwerte.py`), bevor er den zweiten Import entfernt.
- Ursache: Die Lint-Prüfung des Verifiers lief nur beim Abschluss. Dann hält der Coder den Schritt für fertig und behandelt den Befund als Hindernis statt als Teil der Arbeit an der Datei.
- Fix: Jeder erfolgreiche Schreibzugriff (`write_file`, `replace_text`, `apply_patch`) führt den Lint-Befehl des Verifiers auf den geschriebenen Dateien aus und hängt die Befunde an das Ergebnis an („lint, run as the verifier runs it, finds problems in the file(s) just written …“). Ein sauberer Lauf, ein fehlender Linter (126/127) und ein aus einer abgeschnittenen Antwort geretteter Teil (BUG-252, `SALVAGE_DECISION`) fügen nichts an. `complete_step` wertet einen wiederholten Lint-Lauf mit fehlendem Werkzeug weiter als übersprungen.
- Anregung: Nutzer im Turnier („man könnte den Fund in der Testdatei doch früher schon mitgeben“).

### BUG-301 – `replace_text` scheitert an Leerzeilen mit Einrückungsleerzeichen, der Hinweis führt in die Irre
- Phase: Coder-Turnier 0.1.0rc62, 2026-10-20 (GLM-4.7-Flash, A3 Lauf 1) · Komponente: tools (`replace_text`) · Severity: P2 · blockierend: nein (der Lauf endete im Zeitlimit) · Status: **behoben im Repository, Auslieferung mit dem Release nach dem Coder-Turnier** (`tests/integration/test_tools_engine_read_write.py::test_spaces_at_line_ends_do_not_decide_a_match`)
- Reproduktion: Der Coder schreibt `messwerte.py` mit Leerzeilen, die 16 Leerzeichen enthalten. Sein späteres `replace_text` hat in `old` echte Leerzeilen. Viermal `TEXT_NOT_FOUND` mit „its first line appears at line(s) 42 – re-read that range, indentation must match“; `read_range` zeigt keinen Unterschied, das Modell liest die Datei immer wieder und gibt auf.
- Ursache: Der Vergleich war exakt, auch für unsichtbare Leerzeichen am Zeilenende. Der Hinweis nannte die Einrückung, die stimmte.
- Fix: Liefert der exakte Vergleich nichts, gelten Leerzeichen und Tabs vor einem Zeilenumbruch innerhalb eines mehrzeiligen `old` als gleich. Die letzte Zeile wird weiter exakt verglichen, ein einzeiliges `old` also nie lockerer als vorher. `count` gilt wie beim exakten Treffer; das Ergebnis sagt „spaces at line ends were ignored“.
- Turnier: Der GLM-Lauf A3 #1 wird als durch BUG-301 belastet gekennzeichnet; GLM wird mit der neuen Runtime neben coder-main als Referenz nachgetestet.

### BUG-300 – Doctor verlangt Ollama-Tags für Modelle, die llama.cpp aus GGUF-Dateien lädt
- Phase: Deployment 0.1.0rc62, 2026-10-20 · Komponente: doctor · Severity: P2 · blockierend: ja für die Abschlussprüfung des Deployments (`Stop when the installation is not able to run jobs`), nicht für Jobs · Status: **behoben im Repository, Auslieferung mit dem Release nach dem Coder-Turnier** (rc62 bleibt eingefroren) (`tests/integration/test_doctor.py::test_llamacpp_server_is_checked_for_the_profiles_that_use_it`)
- Reproduktion: Profile `coder-ornith-35b` u. a. mit `backend: llamacpp`, Modell aus `ollama_llamacpp_models[].path`. Doctor meldet „FAIL models:model-224 not installed: devstral-small-2:24b, ornith-1.5:35b, …“, während „llamacpp:model-224 healthy, serves … ornith-1.5:35b …“ grün ist.
- Ursache: Die Modellprüfung verlangte für jedes Profil des Hosts einen Ollama-Tag. Profile auf dem llama-server prüft aber schon `llamacpp:<host>` gegen die tatsächlich bedienten Aliase. Ein Modell aus einer GGUF-Datei hat keinen Ollama-Tag.
- Fix: `models:<host>` prüft nur noch Profile, die Ollama bedient; llama.cpp-Profile prüft allein `llamacpp:<host>`.
- Betrieb: Die `ollama`-Rolle (Router-Presets in `models.ini`) läuft nur im Bootstrap. Für rc62 wurde sie einzeln auf .224 angewandt (`roles: [ollama]`, `--tags ollama`), weil der Bootstrap-Preflight 80 GiB frei verlangt (39 GiB frei durch die Kandidaten-Modelle).

### BUG-299 – Abgebrochenes Deployment holt den Neustart nicht nach, alter Release läuft weiter
- Phase: Deployment 0.1.0rc62, 2026-10-20 · Komponente: deploy (Rollen `hermclaw_worker`, `hermclaw_orchestrator`) · Severity: P1 · blockierend: ja (Wiederholung scheitert an „Wait for the worker /health (version 0.1.0rc62)“) · Status: **behoben** (`tests/integration/test_deploy_infra.py::test_nothing_live_changes_before_the_schema_decision`)
- Reproduktion:
  - Lauf 1 schaltet auf .224 den Release-Symlink auf rc62 und bricht dann beim Modell-Download ab (BUG-298).
  - Lauf 2 sieht keine Änderung mehr (`hermclaw_release_switched` false), startet den Worker nur mit `started` und wartet 30-mal vergeblich auf `worker_version 0.1.0rc62`. Der Worker läuft weiter mit rc61.
- Ursache: Die Neustart-Entscheidung beruhte nur auf Änderungen im laufenden Ansible-Lauf. Brach ein Lauf zwischen Umschalten und Start ab, war die Entscheidung verloren.
- Fix:
  - Ein ausstehender Neustart wird als Markierung unter `/run` festgehalten (`/run/hermclaw-restart-pending-<unit>` bzw. `-orchestrator`) und erst nach dem Neustart gelöscht.
  - Der nächste Lauf startet den Dienst dann neu; ein Reboot startet ihn ohnehin auf dem neuen Release.

### BUG-298 – Deployment will Modelle aus GGUF-Dateien über Ollama laden und bricht ab
- Phase: Deployment 0.1.0rc62, 2026-10-20 · Komponente: deploy (Rolle `hermclaw_worker`, models.yml) · Severity: P1 · blockierend: ja (Deployment auf .224 bricht ab) · Status: **behoben** (`tests/integration/test_deploy_infra.py::test_model_pull_follows_models_yaml_and_survives_a_dry_run`)
- Reproduktion:
  - Inventory mit `ollama_llamacpp_models: [{model: "ornith-1.5:35b", path: /var/lib/hcbench/models/….gguf}, …]` und passenden Profilen.
  - Die Rolle will die Tags über `ollama pull` laden: „ornith-1.5:35b, ornith-1.5:9b, qwen3.5:9b, devstral-small-2:24b need about 90.0 GB …, 40 GB are free“.
- Ursache:
  - Die Rolle `ollama` kennt seit rc62 Modelle per GGUF-Pfad, die Rolle `hermclaw_worker` zählte aber jedes Profilmodell als Ollama-Tag.
  - Dazu ein Vorrangfehler im Jinja-Ausdruck: `a + (b) | unique | sort` wendete die Filter nur auf die hostgebundenen Profile an.
- Fix:
  - Modelle mit `path` in `ollama_llamacpp_models` werden nie über Ollama geladen.
  - Der ganze Ausdruck ist geklammert, `unique` und `sort` gelten für alle Tags.

### BUG-297 – Planziel darf geschützte Eingabedaten nicht nennen, ohne ihren Schutz zu verlieren
- Phase: Vorbereitung Coder-Turnier (Planprüfung der Aufgaben A1, A2, A4, A5), 2026-10-20 · Komponente: planner (validation) · Severity: P2 · blockierend: nein (Schutz geht still verloren) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_a_goal_may_name_the_file_it_protects`)
- Reproduktion:
  - Schrittziel: „… die Eingabedaten `data/buchungen.csv` bleiben unverändert“, dazu `must_not_change: [data/**]`.
  - Die Planprüfung verlangte: „its goal names data/buchungen.csv, which is not among the step's files: add it to repo_hints“.
  - Als Hinweis eingetragen, zählte die Datei als eigene Datei des Schritts, und `must_not_change` wurde gestrichen („dropped must_not_change on the step's own files“).
- Ursache: Zwei Regeln widersprachen sich. BUG-268 verlangte jede im Ziel genannte Datei unter den Dateien des Schritts. BUG-292 wertete einen im Ziel genannten Hinweis als „schreibt der Schritt“. Eine Datei, die das Ziel nennt, um sie zu schützen, war damit nie geschützt. Ein Planer-Modell hatte nur die Wahl zwischen Reparaturrunde und verlorenem Schutz.
- Fix:
  - Eine Datei, die ein `must_not_change` des Schritts abdeckt, darf das Ziel nennen, ohne Hinweis zu sein.
  - Der Fehlertext nennt diesen Weg für Dateien, die der Schritt nur liest.

### BUG-296 – Auftrags-Literal mit Anführungszeichen wird in keinem Plan gefunden
- Phase: Vorbereitung Coder-Turnier (Planprüfung der Aufgaben A7, A8, A9), 2026-10-20 · Komponente: planner (validation) · Severity: P1 · blockierend: ja (Plan wird in jeder Reparaturrunde abgelehnt) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_a_job_literal_with_quotes_is_found_in_the_step`)
- Reproduktion:
  - Auftrag mit „Fehlerantworten sind JSON `{"error": "..."}`“.
  - Ein Schrittziel enthält den Text wörtlich, trotzdem meldet die Prüfung „the job names '{"error": "..."}', but no step mentions it“.
- Ursache: Die Rückverfolgung der Auftrags-Literale (BUG-287) durchsuchte die Schritte als JSON-Text (`json.dumps`). Dort steht jedes `"` als `\"` und jeder Backslash doppelt. Literale mit Anführungszeichen oder Backslash konnten nie gefunden werden. Ein API-Auftrag mit JSON-Beispielen in Backticks endete so in Reparaturschleifen bis `PLANNER_INVALID_OUTPUT`.
- Fix: Die Prüfung durchsucht die Zeichenketten der Schritte so, wie sie geschrieben sind.

### BUG-295 – Pester-Tests werden nicht gezählt
- Phase: Vorbereitung Coder-Turnier (Aufgabe A9, PowerShell), 2026-10-20 · Komponente: tools (testparse) · Severity: P2 · blockierend: ja für PowerShell-Schritte mit Testkriterium · Status: **behoben** (`tests/unit/test_tools_testparse.py::test_pester_summary_is_counted`)
- Reproduktion: Ein Testkriterium `pwsh -Command "Invoke-Pester -Path tests -CI"` mit `min_passed: 3` und grünen Tests. Gezählt wurden 0 bestandene Tests, also wurde das Kriterium verfehlt.
- Ursache: `testparse` kannte keine Pester-Zusammenfassung, und für PowerShell gab es nur den Exit-Code. Live K3 (rc59–rc61) hatte zudem kein PowerShell im Sandbox-Image, das Skript lief nie.
- Fix:
  - Neuer Parser für „Tests Passed: N, Failed: M, Skipped: K[, Inconclusive: I]“ (Pester 5 und 6, auch farbig).
  - Neues Sandbox-Image `powershell` mit PowerShell 7 LTS und Pester.

### BUG-294 – Lint-Befund mit Zeilennummer, Coder ändert 35 Minuten den falschen, gleich gebauten Test
- Phase: Testzyklus 3 auf rc61 (M1, Job `6b99148d…`, Plan v3) · Komponente: tools (engine, Ausgabe fehlgeschlagener Befehle) · Severity: P1 · blockierend: ja (`REPLAN_LIMIT` nach 130,9 min) · Status: **behoben** (`tests/unit/test_tools_locations.py`, `tests/integration/test_tools_engine_exec.py::test_a_failed_run_shows_the_source_lines_its_output_names`)
- Reproduktion:
  - Plan v3, S001: nach der Isolationsnotiz (BUG-291, live wirksam) bestehen um 22:29 alle 21 Tests.
  - `complete_step` wird abgelehnt (`CHECK_STILL_FAILING`), Ausgabe vollständig: ``tests/test_app.py:73:9: F841 Local variable `task2` is assigned to but never used`` – Zeile 73 liegt in `test_get_tasks_done`.
  - Der Coder ändert in drei Versuchen sechsmal `test_get_tasks_open` bzw. `test_get_api_tasks_open` (gleich aufgebaut, mit eigener Variable `done_task`), liest Zeilen 60–80 und findet die Zeile nicht; der letzte Versuch pendelt zwischen zwei Fassungen. Stagnation → Replan-Budget erschöpft.
- Ursache: Linter, Compiler und Testläufer nennen nur `pfad:zeile:spalte`. Die Zuordnung der Zeile zur Funktion blieb dem Modell überlassen, das bei zwei gleich gebauten Nachbarn die falsche wählte – eine Zuordnung, die die Runtime ohne Risiko selbst treffen kann.
- Fix: `hermclaw/tools/locations.py` (`source_locations`) liest bei jedem fehlgeschlagenen Befehl die genannten Stellen (`pfad:zeile`, tsc `pfad(zeile,spalte)`, Sandbox-Pfade wie `/workspace/app.py`, relativ zum Befehlsverzeichnis) über die Workspace-Begrenzung und hängt bis zu sechs als `[source]`-Block an: `tests/test_app.py:73 (in def test_get_tasks_done(self):): task2 = app.create_task('Task 2')`. Gilt auch für die Ausgabe in `CHECK_STILL_FAILING`.
- Weitere Beobachtungen M1 (Zyklus 3): Plan v1 mit gemockten HTTP-Handlern (Tests brachen am Konstruktor); die Isolationsprobe lief und meldete korrekt nichts, weil der Einzeltest ebenfalls scheiterte. 709 Modellaufrufe.

### BUG-293 – Redaktion und Geheimnis-Prüfung sind uneins: das Review sieht gültigen und falschen Test-Token gleich maskiert
- Phase: Testzyklus 2 auf rc60 (M3, Job `d5554059…`, Plan v3) · Komponente: core (redaction), verifier (secrets) · Severity: P2 · blockierend: mitursächlich (Review-Befunde gegen die Tests) · Status: **behoben** (`tests/unit/test_config_and_redaction.py::test_role_values_a_test_compares_stay_visible`)
- Reproduktion:
  - `test_api.py` setzt `os.environ["API_TOKEN"] = "test-token-123"` und nutzt `token="test-token-123"` als Standard der Hilfsfunktionen, `token="wrong-token"` im 401-Test.
  - Model-Sicht (`DEFAULT_REDACTOR.text`): die erste Zeile unverändert, beide `token="…"` als `token="***REDACTED***"`.
  - Review (Befund `73627`, blocker): „The diff shows `***REDACTED***` in the test's default token … If the actual token does not match `test-token-123`, the test will fail.“ Der Schritt geht in die nächste Korrektur.
- Ursache: Die Geheimnis-Prüfung des Verifiers erkennt seit BUG-279 Werte aus Rollenwörtern („test“, „wrong“, „valid“, „token“ …) als Platzhalter; die Redaktion für Modell-Eingaben kannte nur `dummy`/`example`/`your-` u. ä. Was der Verifier als harmlos durchlässt, sah das Modell maskiert – und die Werte, die ein Test vergleicht, waren nicht mehr unterscheidbar.
- Fix: Die Regel liegt jetzt einmal in `hermclaw/core/redaction.py` (`is_role_value`) und gilt für beide: Ein in Anführungszeichen stehender Wert nur aus Rollenwörtern (mit höchstens dreistelliger Nummer) wird nicht maskiert. Echte Werte und Werte mit einem Teil, der kein Rollenwort ist, bleiben maskiert.
- Nebenbefund M3 (beobachtet, nicht neu): Das erste Review forderte als `major`, der Server solle ohne `API_TOKEN` gar nicht starten („should likely fail to start“) – der Auftrag verlangt nur 401. Der Neuplaner schrieb das ins neue Ziel. Abgedeckt durch BUG-284 (neue `major`-Befunde in Korrekturen nicht blockierend) und BUG-285 (Review-Wünsche ohne Auftragsgrundlage nicht ins Ziel), beide erst mit rc61 live – Messung in Zyklus 3.

### BUG-292 – Schutz der Eingabedaten aus dem Plan gestrichen, Coder ergänzt eine erfundene Buchung
- Phase: Testzyklus 2 auf rc60, Prüfung der gelieferten Dateien (M2, Job `ece98d3b…`) · Komponente: planner (validation) · Severity: P2 · blockierend: nein (Job `succeeded`, Lieferung fehlerhaft) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_must_not_change_on_a_file_the_step_only_reads_is_kept`)
- Reproduktion:
  - Auftrag: „Mit der beiliegenden Datei data/buchungen.csv bricht es ab … im Monatsbericht fehlt der letzte Monat …“.
  - Plan v1, S001: `repo_hints` enthalten `data/buchungen.csv`, der Planer verbot ihre Änderung (`must_not_change`). Notiz der Normalisierung: „dropped must_not_change on the step's own files: data/buchungen.csv“.
  - Geliefert (Commit `0d7f450`): `data/buchungen.csv` um die Zeile `15.04.2026;Bar;-10,00;Kino` ergänzt. Die Korrekturen im Code selbst sind richtig (`%d.%m.%Y`, `Decimal`, letzter Monat enthalten, keine geteilte Standardliste).
- Ursache: BUG-204 strich `must_not_change` auf allen Dateien des Schritts und zählte dazu jeden `repo_hint`. Ein Hinweis ist aber oft nur zum Lesen da (Eingabedaten, Konfiguration); ihr Schutz ging verloren, und nichts hinderte den Coder daran, die Daten des Nutzers zu ändern.
- Fix: Als eigene Dateien zählen nur die, die der Schritt schreibt: `allowed_new_paths`, `must_change` und Hinweise, die Titel oder Ziel des Schritts nennen. Der Auftragstext zählt hier nicht, denn er nennt die Eingabedaten gerade als Eingabe. Der BUG-204-Fall (verbotene eigene `must_change`-Dateien) wird weiter bereinigt.

### BUG-291 – Tests teilen Zustand, jeder besteht allein: Coder sucht vier Versuche lang im Code und in den Sollwerten
- Phase: Testzyklus 2 auf rc60 (M1, Job `a3cfcb1a…`) · Komponente: coder (loop, isolation) · Severity: P1 · blockierend: ja (Job `failed`, `REPLAN_LIMIT` nach 119 min) · Status: **behoben** (`tests/integration/test_coder_loop.py::test_a_test_that_passes_alone_is_reported_as_shared_state`, `tests/unit/test_coder_isolation.py`)
- Reproduktion:
  - Auftrag: Aufgabenverwaltung mit SQLite, HTTP-Server aus der Standardbibliothek, Tests für Datenhaltung und HTTP-Schnittstelle. Plan v1 mit einem Schritt.
  - `TestHTTPHandler` legt die Datenbank einmal in `setUpClass` an; jeder Test fügt Aufgaben hinzu. `test_get_tasks` erwartet 2 Aufgaben und findet 4 (`AssertionError: 4 != 2`).
  - Im Workspace nachgemessen: `pytest tests/test_app.py` → 2 failed, 22 passed; `pytest tests/test_app.py::TestHTTPHandler::test_get_tasks` allein → 1 passed.
  - Der Coder änderte Code und Tests über vier Versuche und drei Planversionen, ohne den Test einzeln laufen zu lassen; die Stagnationserkennung stoppte jeden Versuch („same failing tests 4x after code changes“), die Hinweise sprachen nur von falschem Code oder falschem Sollwert.
- Ursache: Ein Test, der allein besteht und im Gesamtlauf scheitert, ist von der Reihenfolge abhängig – geteilter Zustand zwischen Tests. Diese Ursache war für den Coder unsichtbar: Die Ausgabe des Gesamtlaufs zeigt nur die falsche Anzahl, und keine Stufe der Eskalation prüfte den Unterschied zwischen Einzel- und Gesamtlauf.
- Fix: Meldet die Stagnationserkennung eine Warnung oder Diagnose nach einem gescheiterten Testlauf mit pytest, führt die Laufzeit den ersten gescheiterten Test einmal allein aus (derselbe Starter, `-q -p no:cacheprovider <node id>`, als `run_test` im Sandbox mit Ereignissen). Besteht er allein, bekommt der Coder den Hinweis, dass die Tests Zustand teilen, und die Anweisung, jedem Test eigenen frischen Zustand zu geben statt Sollwerte oder Code zu ändern. Jeder Test wird pro Versuch höchstens einmal so geprüft; ein schon einzeln ausgeführter Test nie.
- Selbstprüfung: kein weiterer Hinweistext gegen dasselbe Symptom „gleiche Tests scheitern“ (BUG-179), sondern eine gemessene Unterscheidung, die der Coder selbst nicht vornahm.

### BUG-290 – Regeln aus einem Dokument des Repositorys gehen im Plan verloren, das Review prüft gegen die Zusammenfassung
- Phase: Testzyklus 2 auf rc60, Prüfung der gelieferten Dateien (K2, Job `0527d6af…`) · Komponente: review (workspace, prompt), planner (prompt) · Severity: P2 · blockierend: nein (Job `succeeded`) · Status: **behoben** (`tests/unit/test_review_prompt.py::test_documents_the_job_names_as_its_rules_are_part_of_the_review`, `tests/integration/test_review_workspace.py::test_documents_named_as_rules_are_read_from_the_workspace`, `tests/unit/test_planner_prompt.py::test_planner_system_prompt_rules`)
- Reproduktion:
  - Auftrag: „prüft die Eingaben nicht so, wie es die Regeln im README verlangen“. README: „Alter: optional; wenn angegeben, ganze Zahl von 16 bis 120“.
  - Planziel S001: „… die Regeln aus dem README exakt umzusetzen (Trimming, E-Mail-Format mit Punkt, Alter 16-120, Nachricht max 500 Zeichen)“ – „ganze Zahl“ fehlt.
  - Geliefert: `parseInt(value, 10)` – `"17.5"` und `"20abc"` gelten als gültiges Alter. Alle 10 Tests grün, keiner prüft ein nicht ganzzahliges Alter. Das Heavy Review gab frei.
  - Zweiter Mangel: Das Zurücksetzen der Fehlermeldungen nutzt `Object.keys(validate({}))`; ein leeres Alter ist gültig, die Meldung eines korrigierten Alters bleibt stehen.
- Ursache: Die Regeln standen nur im README. Der Planer fasste sie im Ziel zusammen und ließ Details weg; das Review sah das Jobziel („gemäß README“), das zusammengefasste Schrittziel und höchstens zufällig einen Suchtreffer aus dem README. Die Review-Regel „jeder Bereich, jedes Format des JOB GOAL exakt“ griff ins Leere.
- Fix:
  - Review: Dokumentationsdateien in den `repo_hints` des Schritts, die Auftrag oder Schrittziel nennen (Dateiname oder Stamm), kommen ganz aus dem Workspace in den neuen Abschnitt „REQUIREMENT DOCUMENTS“ (gekürzt wie das Jobziel; geschützte Pfade nie). Die Regel „exakt durchgesetzt“ gilt für sie wie für das Jobziel, ausdrücklich auch Typ (ganze Zahl) und optional/Pflicht.
  - Planer-Prompt: Verweist der Auftrag auf Regeln in einem Dokument, nennt der Schritt das Dokument in `repo_hints` und verlangt jede Regel daraus – keine Zusammenfassung im Ziel.

### BUG-289 – Merge Request fehlt trotz erfolgreichem Push: GitLab verarbeitet keine Pushes (Sidekiq gestoppt)
- Phase: Testzyklus 2 auf rc60 (K2 `0527d6af…`, K3 `d8032ccb…`) · Komponente: gitops (engine, gitlab), doctor, acceptance (job_metrics) · Severity: P2 · blockierend: nein (Jobs `succeeded`, Batterie FAIL nur durch fehlenden MR) · Status: **behoben** auf Hermclaw-Seite (`tests/integration/test_gitops_push.py::test_merge_request_is_requested_with_the_push`, `::test_a_server_without_push_options_gets_a_plain_push`, `::test_a_stopped_gitlab_background_processing_is_named`, `tests/integration/test_doctor.py::test_a_stopped_gitlab_sidekiq_fails`, `tests/integration/test_acceptance_jobs.py::test_metrics_of_a_good_job_pass`); **offen auf GitLab .226**: Sidekiq neu starten (kein SSH-Zugang von der Steuermaschine)
- Reproduktion:
  - K2: Push um 14:28:37 erfolgreich, MR-Anlage wiederholt sich 3,5 Minuten und scheitert um 14:32:10 mit `HTTP 400 {'source_branch': ['does not exist']}`. K3 ebenso (Push 14:37:41, Fehler 14:41:13).
  - Die Zweig-API liefert den Zweig (200), der Spiegel hat ihn; ein MR existiert nicht.
  - GitLab-Metriken (lesend, Token des Dienstes): `/sidekiq/process_metrics` → keine Prozesse; `/sidekiq/queue_metrics` → 106 Jobs im Rückstand, ältester seit 60 484 s (16,8 h). `last_activity_at` des Projekts und das letzte Projekt-Ereignis: 18.10. 16:43 UTC – der letzte gelungene MR.
- Ursache: GitLab registriert gepushte Zweige und öffnet MRs in der Hintergrundverarbeitung (Sidekiq). Sie läuft seit dem 18.10. abends nicht mehr; das MR-API prüft den Quellzweig gegen den nicht aktualisierten Zweig-Cache. Die früheren Einzelfälle (J2, X5, K1 aus Zyklus 1; BUG-202/220 verlängerten nur das Warten) passen zu Rückständen derselben Verarbeitung. Hermclaw meldete „source_branch does not exist“ statt der Ursache, und die Batterie zählte es als Fehler des Jobs.
- Fix (Hermclaw):
  - Der Push fordert den MR selbst an (`-o merge_request.create`, Ziel, Titel, Label `hermclaw`): Läuft GitLab hinterher, öffnet es den MR, sobald es den Push verarbeitet, statt dass die API-Anlage nach 3,5 Minuten aufgibt. Die Engine übernimmt den so geöffneten MR (`merge_request.create`) und setzt die mehrzeilige Beschreibung per `PUT`; der API-POST bleibt Rückfallweg, ein Server ohne Push-Optionen bekommt denselben Push ohne `-o`.
  - Scheitert die Anlage weiter, fragt der Client die Sidekiq-Metriken ab und nennt die Ursache („GitLab background processing (Sidekiq) is not running: 106 job(s) waiting, the oldest for 16.8 h“); das Git-Audit trägt sie (`error_details.gitlab_background`).
  - `job_metrics`: Ein so fehlender MR lässt das Kriterium `infrastructure` scheitern und nennt die Ursache beim MR – Infrastruktur, nicht der Job.
  - `hermclaw doctor`: neue Prüfung `gitlab` (API erreichbar, Hintergrundverarbeitung läuft und ist höchstens 10 min im Rückstand).
- Selbstprüfung: kein drittes Verlängern der Wartezeit; die Ursache liegt außerhalb von Hermclaw und wird jetzt gemessen und benannt.

### BUG-288 – Planer wiederholt Akzeptanzkriterien bis zur Vertragsgrenze: 32 Minuten für einen Plan
- Phase: Testzyklus 2 auf rc60 (K2, Job `0527d6af…`, Erstplan) · Komponente: planner (Schema an das Modell) · Severity: P2 · blockierend: nein (Plan war gültig) · Status: **behoben** (`tests/unit/test_planner_loop.py::test_model_facing_schemas_require_the_scope_fields`)
- Reproduktion: qwen3.8 erzeugt 3973 Tokens in 32 min (2 Token/s, 27B-Modell großteils auf der CPU). Der gespeicherte Plan hat 3122 Zeichen (rund 1000 Tokens). Das Modell schrieb dieselben zwei `diff`-Kriterien (`form.js`, `tests/form.test.js`) immer wieder; die Normalisierung entfernte die Duplikate.
- Häufigkeit: Sieben gespeicherte Schritte älterer Pläne haben 28–30 Kriterien mit nur zwei oder drei verschiedenen – dieselbe Schleife bis zur Vertragsgrenze 30. Echte Schritte haben höchstens 13 Kriterien und 13 Pfade.
- Ursache: Das Schema an das Modell übernahm die Vertragsgrenzen (30 Kriterien, 40 Pfade). Die Grammatik ließ die Wiederholung bis dahin zu; bei 2 Token/s kostet jede Wiederholung Minuten.
- Fix: `model_facing_schema` begrenzt die Listen eines Schritts im Modell-Schema (`MODEL_STEP_LIST_BOUNDS`: 12 Kriterien, 20 Pfade/Hinweise/Constraints, 10 Befehlshinweise). Die Grammatik schließt das Array dort. Die Verträge bleiben unverändert; die Laufzeit ergänzt ihre generierten Kriterien weiter bis zur Vertragsgrenze.
- Offen: Live-Nachweis in Zyklus 3 (keine GPU-Versuche während der laufenden Batterie).

### BUG-287 – Erstpläne lassen Dateien und Statuscodes weg, die der Auftrag ausdrücklich nennt
- Phase: Fixpaket nach Zyklus 2 (Planprüfung: Anforderungs-Rückverfolgung, Freigabekriterium „100 % der Pflichtanforderungen abgedeckt“) · Komponente: planner (validation, prompt) · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/unit/test_planner_validation.py::test_every_literal_of_the_job_is_named_by_a_step`, `tests/unit/test_planner_prompt.py::test_planner_system_prompt_rules`)
- Reproduktion: Auswertung aller 90 Erstpläne der Datenbank. J2 („ungültige Eingaben → 400“) und X3 (`inventory.db`): vier Erstpläne nennen den Statuscode bzw. die Datei in keinem Schritt. Alle vier brauchten Replans oder scheiterten; von neun Plänen, die jedes Literal nennen, liefen acht ohne Replan durch.
- Ursache: Der Planer-Kern prüfte nicht, ob die Anforderungen des Auftrags im Plan ankommen. Prosa lässt sich deutsch→englisch nicht deterministisch abgleichen, Literale schon.
- Fix:
  - `job_literals`: Dateipfade (ohne Produktnamen wie `Node.js` und Domains), Namen in Backticks und – nur in einem HTTP-Auftrag – 4xx/5xx-Statuscodes.
  - `_job_literal_errors`: Ein Literal, das kein Schritt nennt (Ziel, Pfade, Hinweise, Akzeptanz; ein Pfad auch über den Dateinamen), ist ein Planfehler mit Reparaturhinweis. Nur Erstpläne; im Replan gilt BUG-285.
  - Planer-Prompt: Jede genannte Datei, jeder Name in Backticks und jeder HTTP-Statuscode gehört in Ziel und Akzeptanz des liefernden Schritts.
- Abwägung: Kosten im Fehlerfall eine Reparaturrunde (qwen3.8 einige Minuten) statt eines Replans nach dem Coden. Integrationstest `test_inventory_paths_outside_files_ground_hints_end_to_end` ließ `config/logging.yaml` aus dem Plan fallen, obwohl der Auftrag die Datei nennt, und `test_triage_failure_is_not_fatal_and_job_without_repository` plante eine TLS-Recherche zum Auftrag „VALUE in src/module.py ändern“ – Testdaten korrigiert.

### BUG-286 – Absturz der Container-Engine (Exit-Code 125) gilt als Lint-Fehler im Code
- Phase: Fixpaket nach Zyklus 2 (Integrationstest des Schedulers mit echtem Podman) · Komponente: contracts (worker), verifier (commands), tools (engine) · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/unit/test_verifier_units.py::test_a_container_engine_failure_is_not_a_failing_command`)
- Reproduktion: Rootless Podman ohne subuid/subgid-Eintrag beantwortet jeden Befehl mit Exit-Code 125 und „Error: …“. Der Verifier meldet `lint:python` „lint reported problems (exit code 125)“ gegen korrekten Code; der Schritt scheitert, der Coder bekäme einen Lint-Befund ohne Befund.
- Ursache: Die Sandbox erkennt den Engine-Fehler (`error="sandbox engine error: …"`), behält aber den Exit-Code 125. Verifier (`failed_to_run`) und Werkzeuge (`sandbox_failed`) werteten nur „kein Exit-Code“ als „lief nicht“.
- Fix: `CommandResult.did_not_run` – kein Exit-Code ohne Timeout oder Engine-Fehler (gemeinsames Präfix `ENGINE_ERROR_PREFIX`). Verifier und Werkzeug-Engine nutzen es: die Prüfung wird `error` („could not run“), `complete_step` überspringt sie, der Coder sieht „[sandbox] sandbox engine error …“. Ein `exit 125` des Befehls selbst bleibt ein Fehlschlag.

### BUG-285 – Neuplaner verliert die Anforderungen des gescheiterten Schritts und schreibt alte Lint-Befunde ins Ziel
- Phase: Testzyklus 2 auf rc60 (K1, Job `582bba63…`, Pläne v2 und v3) · Komponente: planner (replanner, prompt) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_a_replacement_step_keeps_the_requirements_of_the_failed_step`, `tests/unit/test_planner_prompt.py::test_replanner_system_prompt_adds_rerun_rules`)
- Reproduktion:
  - Plan v1: S001 soll `csv_eval.py` mit Spalten, Warnungen mit Zeilennummern, leerer und fehlender Datei liefern. Nach BUG-283 Replan.
  - Plan v2: Das Ziel des Ersatzschritts ist eine Liste von Einzelkorrekturen aus den letzten Befunden („unbenutzte `header`-Variable entfernen (Zeile 39)“). Die `zeitpunkt`-Spalte fehlt im Ziel.
  - Plan v3: Das Ziel verlangt gleichzeitig „Header prüfen“ und „`header` nicht zuweisen“. Der Coder pendelt zwischen NameError und F841, Stagnation.
  - Plan v3 enthält außerdem die Review-Forderung aus v2 „Ausgabe-Labels auf Deutsch“, die der Auftrag nicht stellt. Im letzten Versuch sind alle 22 Prüfungen grün, das Heavy Review blockiert gegen genau dieses Planziel. `REPLAN_LIMIT` nach 90,5 min.
  - Plan v1 war vollständig (alle Spalten, Fehlerfälle, Tests, README); verloren gingen die Anforderungen erst durch die Replans.
- Ursache: Der Neuplaner sah nur das Ziel des zuletzt gescheiterten Schritts und die Befunde. Er schrieb Befunde zeilengenau ins neue Ziel, nichts hielt die ursprünglichen Anforderungen fest. Mit jedem Replan wurde das Ziel enger und widersprüchlicher.
- Fix:
  - Deterministisch (`carry_replaced_goal`): Jeder neue, nicht abgeschlossene Code-Schritt, der an den Dateien des gescheiterten Schritts arbeitet (`allowed_new_paths` oder Datei-Hinweise, ohne Dokumentation), bekommt dessen Ziel als Constraint „Requirements of the step this replaces (still in force …)“. Trägt der gescheiterte Schritt diese Constraint schon, wird sie unverändert weitergegeben – auch nach mehreren Replans gilt das ursprüngliche Ziel. Notiz in `replan.created`.
  - Prompt: Ein Ersatzschritt beschreibt das vollständige Ergebnis, keine Bearbeitungsliste; keine Zeilennummern, Lint-Codes oder Einzeländerungen aus den Belegen. Review-Befunde, die der Auftrag nicht verlangt (Formulierung, Labels, Stil, Zusatzprüfungen), sind keine Anforderungen.
- Abwägung: Dokumentationsschritte, die die Datei nur nennen, bekommen nichts. Verlangt der Neuplaner bewusst weniger als vorher, gilt trotzdem das alte Ziel – Anforderungen des Auftrags fallen nicht durch einen Replan weg.

### BUG-284 – Review-Korrekturen kommen nicht zur Ruhe: jedes Review stellt neue Forderungen, auch an unveränderten Code
- Phase: Testzyklus 2 auf rc60 (K1, Job `582bba63…`, Plan v2, S002 Versuche 1–3) · Komponente: review (invariant, prompt), coder (handler) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_review_severity.py::test_new_major_findings_of_a_correction_review_do_not_block`, `::test_finding_origin_is_normalised`, `tests/unit/test_review_prompt.py::test_a_correction_review_sees_the_findings_it_corrects`, `tests/integration/test_coder_handler.py::test_review_invariant_major_finding_forces_correction`)
- Reproduktion:
  - Review 1 fordert Header-Prüfung und Tests für `inf`/`nan`. Die Korrektur liefert beides, alle 19 Prüfungen bestehen.
  - Review 2 lehnt ab, weil der Code „die Planvorgabe verletzt“: Der Replan hatte den alten Lint-Befund als „unbenutzte `header`-Variable entfernen“ ins Ziel geschrieben, und `header` wird jetzt für die geforderte Prüfung gebraucht.
  - Review 3 (Code unverändert, gleicher Inhalts-Digest) fordert Neues: die `zeitpunkt`-Spalte prüfen und deutsche Ausgabetexte. Letzteres verlangt der Auftrag nicht, und die Prompt-Regel stuft es als minor ein.
  - Ergebnis: Der Schritt ist blockiert, zweiter Replan.
- Ursache: Ein Review im Korrekturversuch kannte die Befunde nicht, die korrigiert werden sollten, und durfte beliebige neue Forderungen blockierend stellen. Die Korrekturschleife hatte damit kein festes Ziel. Gleiche Klasse wie BUG-191, BUG-260 und BUG-274; Prompt-Regeln allein hielten nicht.
- Fix:
  - In einem Korrekturversuch nach einem Review bekommt das Review die früheren Befunde (Abschnitt „EARLIER REVIEW FINDINGS“) und kennzeichnet jeden Befund mit `origin`: `earlier`, `introduced` oder `new`. Unbekannt oder fehlend gilt als `new`.
  - Deterministisch: Ein `major`-Befund mit `origin: new` wird zu `minor` (`downgrade_new_correction_findings`). Blocker bleiben blockierend. Bleiben nur `minor`-Befunde und hat der Verifier bestanden, gilt das Review als bestanden (bestehende Invariante, BUG-191).
- Abwägung: Ein echter neuer Mangel, den erst das zweite Review sieht, blockiert nur noch als Blocker. Das erste Review jedes Schritts behält die volle Strenge.
- Nebenbefund (offen): Der Neuplaner übernimmt Einzelheiten alter Befunde („Variable entfernen“) wörtlich ins neue Schrittziel und lässt dabei Anforderungen weg (`zeitpunkt`).

### BUG-283 – Review-Korrektur bringt einen neuen Lint-Fehler ein, `complete_step` nimmt an, der letzte Versuch ist verbraucht
- Phase: Testzyklus 2 auf rc60 (K1, Job `582bba63…`, S001 Versuch 3) · Komponente: tools (engine) · Severity: P2 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_complete_step_runs_the_lint_of_the_changed_files`)
- Reproduktion: Versuch 1 scheitert an Lint, die Korrektur behebt das. Das Heavy Review fordert berechtigt, `inf`/`nan` abzulehnen und den Header zu prüfen. Die Korrektur dazu führt ein neues F841 ein, `complete_step` nimmt an. Der Verifier blockiert den Schritt (2/2 Korrekturen), es folgt ein Replan von knapp 13 Minuten.
- Ursache: Seit BUG-276 führt `complete_step` nur die Prüfungen erneut aus, die die Korrektur nennt. Bei einer Review-Korrektur ist das keine. Den Lint der geänderten Dateien sah erst der Verifier.
- Fix: `complete_step` führt immer auch den Lint-Befehl des Verifiers über die jetzt geänderten Dateien aus (gleiche Vorlage `policies.verifier.lint_commands`, gleiche Spracherkennung) und lehnt ab, solange Befunde bleiben (`CHECK_STILL_FAILING`, mit Ausgabe). Fehlt das Werkzeug im Image (Exit-Code 126/127), wird übersprungen. Im letzten Zug wird nichts ausgeführt (wie bisher).

### BUG-282 – Pläne verlangen Änderungen an bestehenden Dateien, die weder Schrittziel noch Auftrag nennen
- Phase: Planer-Bench R-033 (Blindbewertung aller Modelle) und Testzyklus 1 auf rc59 (M2) · Komponente: planner (validation) · Severity: P2 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_must_change_on_an_existing_file_the_goal_does_not_name_is_dropped`)
- Reproduktion: In der Blindbewertung war das der häufigste Freigabe-Verstoß aller Planer. Beispiele: `must_change` auf `README.md` oder `ledger/__main__.py`, obwohl das Schrittziel nur die Fehler in `parse.py`/`report.py` beschreibt. In Zyklus 1 brauchte M2 deshalb zwei Replans. BUG-278 fängt das erst zur Laufzeit ab, per Review nach gescheiterten Versuchen.
- Ursache: Der Worker sieht vor allem sein Schrittziel. Eine Pflichtänderung an einer Datei, die dort nicht vorkommt, kann er nicht ableiten. Eine korrekte Lösung scheitert dann am Diff-Kriterium.
- Fix: Die Plan-Bereinigung streicht `must_change` auf bestehende, konkrete Dateien, die weder Titel und Ziel des Schritts noch der Auftrag nennen. Als Nennung zählen der Pfad, der Dateiname oder ein spezifischer Stamm als Wort (`users` für `app/routers/users.py`), nicht aber allgemeine Stämme wie `config` oder `index`. Die Datei bleibt im Scope und darf geändert werden. Neue Dateien und Verzeichnis-Globs bleiben Pflicht. Der Plan erhält eine Notiz.

### BUG-281 – Planer gibt auf, statt Tests in den Schritt ihres Codes zu verschieben
- Phase: Planer-Bench R-033 (Kombinationen, 2026-10-19) · Komponente: planner (validation) · Severity: P2 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_tests_for_another_steps_code_are_moved_into_that_step`)
- Reproduktion: Drei von 18 Bench-Läufen (Ornith 9B auf M3 zweimal, qwen3.8 auf M2) endeten mit `PlannerError: model output invalid after 2 repair attempt(s): step S001 writes tests for code of step S002 (…): move these test paths to step S002`. Jeder Reparaturzug wiederholte den Plan unverändert.
- Ursache: Die Regel aus BUG-250 meldet den Fehler richtig, überlässt die rein mechanische Verschiebung aber dem Modell. Die Modelle setzen sie nicht um.
- Fix: Die Plan-Bereinigung (`prune_unreachable_must_change`, vor der semantischen Prüfung in Planung und Neuplanung) verschiebt Testpfade, die nach dem Code genau eines anderen Schritts benannt sind, in diesen Schritt. Neue Pfade bleiben neue Pfade, Hinweise bleiben Hinweise, und `must_change` auf diesen Pfaden wandert mit. Der Plan erhält dazu eine Notiz. Ist der Stamm mehrdeutig oder der Schritt im Replan erhalten, bleibt es beim Fehler für den Planer.

### BUG-280 – Neues llama.cpp-Archiv unter altem Build-Namen: Deploy meldet Erfolg, der alte llama-server läuft weiter
- Phase: Deploy 0.1.0rc60 (Rolle ollama, `bootstrap.yml --tags ollama` auf .224) · Komponente: infra (ansible, Rolle ollama) · Severity: P2 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_bootstrap_infra.py::test_llamacpp_archive_must_carry_the_build_it_is_installed_as`)
- Reproduktion:
  - Das Inventar zeigt `ollama_llamacpp_archive_src` auf `llamacpp-b11696-sm61.tgz`, `ollama_llamacpp_build` bleibt beim Standard `b11361`.
  - Der Lauf endet mit `failed=0`. Das Entpacken nach `/opt/llamacpp/b11361` wird übersprungen (`creates:` findet den alten `llama-server`).
  - Unter `current` läuft weiter b11361. Im Router-Modus listet `/v1/models` nur das Hauptmodell, qwen3.8 fehlt.
- Ursache: Den Zielordner bestimmt allein `ollama_llamacpp_build`. Die Rolle prüfte nicht, ob das Archiv zu diesem Build gehört.
- Fix: Die Validierung verlangt, dass der Archivname mit `llamacpp-<ollama_llamacpp_build>-` beginnt (Namensschema von `scripts/ops/build-llamacpp.sh`). Ein abweichendes Archiv bricht den Lauf vor jeder Änderung ab. Inventar: `ollama_llamacpp_build: b11696`.
- Nebenbefund beim Testlauf vor rc60: Die Include-Schleife der Router-Modelle trug keine `apply`-Tags. Mit `--tags ollama` wären die Modell-Tasks übersprungen worden. Behoben vor dem Release (`test_dynamic_includes_apply_their_tags`).

### BUG-279 – Gemeldete Beispiel-Tokens bleiben stehen: maskierte Zeilen werden unverändert zurückgeschrieben und als „ersetzt“ quittiert
- Phase: Testzyklus 1 auf rc59 (M3, Job `37213799…`, S001 Versuche 2–3) · Komponente: tools (engine), verifier (secrets), coder (handler) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_read_write.py::test_a_flagged_secret_stays_until_a_placeholder_replaces_it`, `tests/unit/test_verifier_secrets.py::test_secret_findings_in_docs_ask_for_a_placeholder_the_scanner_accepts`)
- Reproduktion:
  - Die REST-API mit Bearer-Token ist fertig, 11 Tests sind grün. Der Verifier meldet mögliche Geheimnisse: README Zeile 20 (`secret_assignment`) und Zeilen 36/43/52/59 (`bearer_token`), `tests/test_api.py` Zeilen 17 und 128.
  - Versuch 2 tauscht in elf Turns nur Zeile 20 zwischen `<your-token>` und `<your-secret-token>` hin und her. Die vier anderen Zeilen bleiben, es folgt Stagnation.
  - Versuch 3 schreibt `token="***REDACTED***"` in den Tests unverändert zurück. Die Engine meldet „masked secret replaced“, obwohl sich nichts geändert hat. Es folgt ein Replan.
- Ursache:
  - Der Coder sieht die gemeldeten Werte nur maskiert. Ein unverändert kopierter Marker behält den echten Wert (BUG-270), die Erfolgsmeldung sagte aber das Gegenteil.
  - Für die README lautete die Abhilfe „aus der Umgebung laden“, das passt nicht zu Dokumentation.
  - Ob eine gemeldete Zeile weiter auffällt, erfuhr der Coder erst nach dem nächsten vollständigen Verifier-Lauf.
- Fix:
  - Ein maskiertes `replace_text`, das nichts ändert, wird abgelehnt (`NO_CHANGE`) mit der Anweisung, an Stelle des Markers einen Platzhalter zu schreiben. Bleibt ein Marker stehen, sagt die Erfolgsmeldung das („kept their real value“). Die Ablehnung identischer Ersetzungen nennt den Fall ebenfalls.
  - Für Dokumentation (`.md/.rst/.txt/.adoc`) rät der Verifier zu `<your-token>` oder `$AUTH_TOKEN`, beides akzeptiert der Scanner.
  - Werte, die nur aus Rollenwörtern bestehen (`valid_token`, `invalid-token`, `wrong_password`, `my-secret-key`, optional nummeriert), gelten als Platzhalter. Die Tests von M3 nutzten genau solche Werte und wurden als Geheimnisse gemeldet. Ein Teil, der kein solches Wort ist (`sk_test_4eC3…`, `valid_token_9f8e…`), lässt den Wert ein Geheimnis bleiben.
  - In einer Korrektur mit Geheimnis-Befunden scannt `complete_step` die gemeldeten Dateien erneut (Zeilen, die nicht in der Basisversion stehen, mit dem Scanner des Verifiers). Solange noch Zeilen auffallen, wird abgelehnt (`CHECK_STILL_FAILING`), mit Zeilennummer, Regel und maskierter Vorschau.
- Selbstprüfung: Die Runtime gab falsche und fehlende Rückmeldungen, das ist keine reine Modellschwäche. Kein Sonderfall: gilt für jede Datei mit gemeldeten Geheimnissen. Aufträge mit Authentifizierung treffen das regelmäßig.

### BUG-278 – Der Plan verlangt Änderungen an Dateien, die der Schritt nur liest; der richtige Code scheitert daran zweimal bis zum Replan-Limit
- Phase: Testzyklus 1 auf rc59 (M2, Job `a4a95ec2…`, S001 in Plan v1 und v2) · Komponente: coder (handler), planner (Kriterien) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_coder_handler.py::test_a_change_demand_on_a_file_the_step_only_reads_is_judged_by_the_review`, `…::test_a_change_demand_the_review_upholds_goes_to_a_correction`)
- Reproduktion:
  - Das Planziel von S001 benennt die vier Fehler korrekt. Die Akzeptanz enthält jedoch zehn `diff`-Kriterien ohne Beschreibung, darunter `must_change` auf `data/buchungen.csv`, `data/`, `ledger/__main__.py` und `tests/test_report.py`. Das sind die Eingabedatei, der Einstiegspunkt und ein bestehender Test, also Kontext zum Lesen.
  - Der Coder behebt alle vier Fehler, die Tests sind grün, das Tool gibt richtige Werte aus. Der Verifier scheitert an den `must_change`-Kriterien.
  - In der Korrektur schreibt der Coder unnötig an `__main__.py` und `tests/test_report.py` herum. Danach blockiert er zu Recht: „Kriterien verlangen eine Änderung an der CSV, das Ziel nicht“.
  - Plan v2 enthält dieselben Kriterien. Zwei Korrekturen scheitern nur an `data/buchungen.csv` und `data/` (27 von 30 Prüfungen bestanden). Es folgt Replan 2, erst Plan v3 läuft durch.
- Ursache:
  - Das Planer-Modell überträgt `repo_hints` (Lesekontext) in `must_change`.
  - Die Anreicherung entfernt nur Globs außerhalb der Schrittdateien.
  - Ein unerfüllbarer `must_change` ist für Coder und Korrektur nicht behebbar. Es bleibt nur der Replan, und der Neuplaner wiederholte das Kriterium trotz der Begründung der Blockade.
- Fix:
  - Bestehen die blockierenden Fehler nur aus `diff`-Prüfungen mit unveränderten `must_change`-Dateien (keine verbotene, leere oder zu große Änderung, sonst nichts fehlgeschlagen), verifiziert der Handler den Schritt ohne diese Globs erneut.
  - Das Heavy Review urteilt über diesen Schritt. Eine Constraint fragt ausdrücklich, ob das Ziel die Änderung dieser Dateien verlangt.
  - Bei `pass` wird das Kriterium protokolliert fallen gelassen (`acceptance.waived`) und committet. Bei `fix_required` folgt eine normale Korrektur mit dem Befund des Reviews.
- Selbstprüfung: Das ist keine Kompensation einer Coder-Schwäche, sondern ein Planer-Defekt. Statt den Planer mit Heuristiken über Datentypen zu raten, entscheidet die Instanz, die Ziel und Diff sieht. Kein Sonderfall, gilt für jede Datei.

### BUG-277 – Ein späterer Schritt in einem leeren Repository bekommt kein Testkriterium, der Verifier blockiert ihn trotzdem
- Phase: Testzyklus 1 auf rc59 (M1, Job `eeb8bf5a…`, S002) · Komponente: planner (enrich) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_enrich.py::test_tests_an_earlier_step_creates_give_a_later_step_its_test_evidence`)
- Reproduktion:
  - Im leeren Repository legt S001 `task_manager.py` und `tests/test_task_manager.py` an. S002 (implement) aktualisiert nur die README.
  - Der Verifier meldet `test_evidence`: „implement step in a repository with tests has no test evidence“. Der Schritt wird mit `ACCEPTANCE_DEFECT` blockiert, es folgt ein Replan. Plan v2 bekam das generierte Testkriterium und lief durch.
- Ursache: `test_evidence_for` sah nur die Dateien des Repositorys zur Planungszeit und die eigenen Pfade des Schritts. Tests, die ein vorheriger Schritt desselben Plans anlegt, kannte die Planung nicht. Der Verifier prüft dagegen den Stand bei Ausführung.
- Fix: Die Anreicherung übergibt `generate_acceptance` die `allowed_new_paths` aller Schritte, von denen der Schritt (transitiv) abhängt. Darunter liegende Testdateien zählen wie vorhandene. Tests eines nicht vorgelagerten Schritts zählen nicht, sie gäbe es beim Lauf noch nicht.
- Selbstprüfung: Runtime-Inkonsistenz zwischen Planung und Verifier, keine Modellschwäche. Kein Sonderfall, gilt für jedes leere oder testlose Repository.

### BUG-276 – Eine Korrektur wird abgeschlossen, obwohl die Lint-Prüfung, die sie beheben soll, weiter fehlschlägt
- Phase: Testzyklus 1 auf rc59 (K1, Job `6cf9bff0…`, S001 Versuche 2–3; M1 `eeb8bf5a…` S001) · Komponente: tools (engine), coder (handler), review (correction) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_complete_step_reruns_the_check_the_correction_names`, `tests/unit/test_review_correction.py::test_failed_lint_and_test_checks_carry_their_command_to_the_correction`)
- Reproduktion:
  - Der Verifier meldet ruff-Befunde (F811 doppelte Testfunktion, F841 unbenutzte Variable) samt Befehl und Ausgabe.
  - Die Korrekturversuche 2 und 3 lassen nur `pytest` laufen. Die Runtime schließt den Schritt ab („pytest passed and was proposed again without any change“, BUG-207).
  - Der Verifier scheitert zweimal an derselben Lint-Ausgabe. Das Korrekturbudget ist erschöpft, es folgt ein Replan (etwa 25 min verloren, K1 überschreitet sein Zeitziel).
- Ursache:
  - Abgeschlossen wurde nach dem Signal „ein Testlauf ist grün“. Ob die Prüfung, an der der letzte Versuch gescheitert war, jetzt besteht, prüfte niemand.
  - Der Befehl dieser Prüfung lag nur als Text in der Korrektur-Evidenz vor.
- Fix:
  - Fehlgeschlagene Lint- und Testprüfungen geben ihren Befehl als `command` an das Korrektur-Item weiter (`VerifierFailure`, `CorrectionItem`, gespeicherte `correction_input`).
  - Der Handler übergibt die Befehle an die Tool-Engine (`expect_rechecks`).
  - `complete_step`, auch der von der Runtime ausgelöste, führt sie erneut aus. Solange einer fehlschlägt, wird abgelehnt (`CHECK_STILL_FAILING`, mit Ausgabe). Ohne Änderung seitdem wird nicht neu ausgeführt, die frühere Ausgabe wird gezeigt.
  - Im letzten Turn gilt keine Sperre. Ein Befehl, der in der Sandbox nicht laufen kann, wird übersprungen.
- Selbstprüfung: Das kompensiert kein Modellsymptom unter neuem Namen. Die Runtime traf selbst die falsche Abschlussentscheidung (BUG-207 ohne Bezug zur offenen Korrektur). Jetzt misst sie den Abschluss an der Prüfung, die gescheitert war.

### BUG-275 – Im Node-Image endet jeder Befehl mit Exit-Code 2 und ohne Ausgabe (Regression aus BUG-267)
- Phase: Testzyklus 1 auf rc59 (K2, Job `f78edfa6…`, S001 Versuche 1–2) · Komponente: tools (engine) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_the_pipefail_prefix_never_ends_a_shell_that_lacks_the_option`)
- Reproduktion:
  - `node --test`, `node --test tests/form.test.js 2>&1; echo "EXIT:$?"` und `… || true` enden alle mit „exit code 2 · (no output)“. Selbst das `echo` am Ende erscheint nicht.
  - Der Coder wiederholt den Befehl, die Stagnation eskaliert zum Heavy Review. Dieses rät einen Fehler in `validateAge`, danach folgt ein Replan.
- Ursache:
  - Seit BUG-267 beginnt jedes Sandbox-Skript mit `set -o pipefail 2>/dev/null`.
  - Das Node-Image basiert auf Debian bookworm. Dessen dash (0.5.12-2) kennt `pipefail` nicht. Ein fehlschlagendes Spezial-Builtin beendet eine nicht-interaktive POSIX-Shell sofort mit Exit-Code 2. `2>/dev/null` versteckt nur die Meldung.
  - Die Python- und Generic-Images (trixie, dash 0.5.12-12 mit pipefail) waren nicht betroffen. Der Kommentar „every sandbox image shell knows it“ war ungeprüft.
- Fix: Der Präfix lautet jetzt `(set -o pipefail) 2>/dev/null && set -o pipefail`. Die Option wird in einer Subshell geprobt und nur gesetzt, wo die Shell sie kennt.
- Selbstprüfung: echte Runtime-Regression, keine Modellschwäche. Der Test prüft den Präfix in `sh` und `bash` mit einer unbekannten Option (Skript läuft weiter) und mit `pipefail` (Pipeline meldet den Fehler). Ohne Fix schlägt er in dash fehl.
- Folge für die Messung: K2 auf rc59 ist durch diese Regression ungültig. Der Fix kommt mit dem Fixpaket nach Zyklus 1 (rc60), K2 wird dort erneut gemessen.

### BUG-274 – Das Heavy Review sieht von fehlschlagenden Tests nur den Exit-Code und erfindet als Blocker ein fehlendes `__init__.py`
- Phase: G1 auf rc56–rc58 (Job `0f3f2a32…`, S003 Versuch 2, S008 Versuch 3) · Komponente: review (prompt) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_review_prompt.py::test_failing_checks_show_why_they_failed`)
- Reproduktion:
  - Die Unraid-Tests scheitern an `KeyError: 'Content-Type'` (urllib schreibt Header als `Content-type`) und an Mocks ohne `status`.
  - Das Heavy Review nennt als Blocker „no `__init__.py` in collector package, `import collector` fails“. Belegt ist das nur mit „exited with code 1“. Der Import funktioniert jedoch (Namespace-Paket, nachgeprüft).
  - Die Korrektur verlangt eine Datei, die einem späteren Schritt gehört. Die echten Ursachen werden nicht genannt.
- Ursache:
  - Die Belege einer fehlschlagenden Prüfung wurden als kompaktes JSON mit 600 Zeichen gezeigt.
  - `output` steht dort zuletzt (bis 4000 Zeichen) und wurde abgeschnitten. Das Review urteilte ohne Fehlermeldung.
- Fix:
  - Fehlschlagende Prüfungen zeigen ihre Belege ohne `output`.
  - Die ersten drei zeigen zusätzlich einen Auszug („failure output (why it failed)“): `E …`-, `FAILED`/`ERROR`- und Fehlerzeilen plus das Ende, höchstens 1800 Zeichen je Prüfung.
- Selbstprüfung: Die Ursache liegt bei der Datenbasis des Reviews, nicht beim Coder. Ein Review ohne die Fehlermeldung muss raten. Die Begrenzung auf drei Auszüge hält den Prompt im Budget.

### BUG-273 – Das Frontend „ruft Daten von der lokalen API ab“, hängt aber nicht vom Schritt ab, der `collector/api.py` schreibt; drei Reviews verwerfen es nacheinander
- Phase: G1 auf rc56/rc57 (Job `0f3f2a32…`, Plan v1, S005 Versuche 1–3) · Komponente: planner (validation) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_an_acronym_names_a_short_component`)
- Reproduktion:
  - Plan v1: S005 (Frontend: „JavaScript für Datenabruf von der lokalen API … Konfigurationsbereich“) läuft ohne Abhängigkeit von S004 (Kern, JSON-API, `collector/api.py`).
  - Die drei Reviews finden jeweils andere Lücken bei Konfiguration und Geheimnissen: Zugangsdaten im Client, fehlender Schutz-Token, „secrets never delivered to frontend“.
  - Die API, die das festlegt, existierte noch nicht. S005 wird blockiert.
- Ursache: Die Regel aus BUG-251 (benannte Komponente → Abhängigkeit) wertete nur Stämme ab fünf Zeichen aus. `api` fiel heraus.
- Fix:
  - Stämme ab drei Zeichen zählen.
  - Kurze Stämme (3–4 Zeichen) zählen nur als Akronym in Großbuchstaben im Text, z. B. „API“, „CLI“. Gewöhnliche Wörter wie „app“ oder „web“ ordnen nichts.
- Selbstprüfung: Kein Kompensieren im Review oder Coder. Die früheste vermeidbare Entscheidung war die fehlende Abhängigkeit im Plan. Die Erweiterung ist allgemein: Akronyme benennen Module.

### BUG-272 – Der Coder sendet viermal dasselbe `replace_text` mit `old == new`; die Ablehnung sagt nicht, dass die Änderung schon in der Datei steht
- Phase: G1 auf rc56 (Job `0f3f2a32…`, S005 Versuch 2, Turns 8–14) · Komponente: tools (engine) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_read_write.py::test_an_identical_replace_says_whether_the_change_is_already_made`)
- Reproduktion:
  - S005 hat `app.js` in Turn 5 neu geschrieben. Danach sendet der Coder viermal `replace_text` mit identischem, 2038 Zeichen langem `old`/`new` für `renderConfigInputs`.
  - Die Antwort lautet jedes Mal nur „'old' and 'new' are identical“.
  - Die Stagnationserkennung stoppt mit `edit_mechanics`, die Heavy-Review-Stufe ist verbraucht.
- Ursache:
  - Die Ablehnung prüfte nur die Argumente, nicht die Datei. Dass die gewünschte Fassung schon drinsteht, erfuhr der Coder nicht.
  - Ohne die Markierung `repeated` ersetzte die Ablehnung außerdem die maßgebliche letzte Rückmeldung.
- Fix:
  - Die Prüfung läuft nach dem Lesen der Datei.
  - Die Ablehnung sagt, ob die Datei den Text enthält („change … already made … go on with the next open point“) oder nicht („copy the current lines“).
  - Sie ist als `repeated` markiert (wie `NO_CHANGE` bei `write_file`, BUG-253).
- Selbstprüfung: Eine Rückmeldung, die den tatsächlichen Zustand nennt, ist ein echter Schritt nach vorn. Kein Sonderfall: gilt für jedes identische `replace_text`.

### BUG-271 – Der Coder fordert nach verweigerten Wiederholungen immer wieder dieselben Zeilen an; drei Versuche ohne eine einzige Änderung, G1 scheitert mit `REPLAN_LIMIT`
- Phase: G1 auf rc50–rc55 (Job `5c3d40a5…`, S008 Versuche 1–3, S014 Versuche 1–4) · Komponente: coder (loop) · Severity: P1 · blockierend: ja (behoben; G1 FAIL nach 2 Replans) · Status: **behoben** (`tests/integration/test_coder_loop.py::test_reads_are_withheld_after_a_refused_re_read_until_a_file_changes`)
- Reproduktion:
  - S014 soll collector.py (845 Zeilen) und test_collector.py (591 Zeilen) reparieren.
  - In 54 Turns über vier Versuche liest der Coder dieselben Bereiche wieder. Es gibt 7 Verweigerungen `READ_REPEATED`, die einzige Änderung wurde abgelehnt (BUG-270), es gibt keinen erfolgreichen Schreibzugriff.
  - Die Stagnationserkennung stoppt erst mit Hinweis, dann per Heavy Review und schließlich mit Block. Das Replan-Budget ist erschöpft, der Job endet mit FAIL.
- Ursache:
  - Die Verweigerung sagte „Act on it now“, ließ aber alle Lesewerkzeuge im Katalog. Das Modell blieb im Analysemodus.
  - Für Prüfbefehle gibt es diese Sperre seit BUG-181. Für Lesezugriffe fehlte sie.
- Fix:
  - Nach einem verweigerten erneuten Lesen verlassen `read_file` und `read_range` den Werkzeugkatalog. Ein Turn-Hinweis nennt die Wege zur Änderung.
  - Die Werkzeuge kommen zurück, sobald sich eine Datei ändert: durch einen Schreibzugriff oder einen Befehl mit geänderten Dateien.
  - Der verweigerte Inhalt steht bereits unter den gelesenen Dateien (BUG-229/266).
- Selbstprüfung:
  - Nicht dasselbe Symptom erneut kompensiert. BUG-264–266 sorgten dafür, dass Gelesenes ankommt; das ist jetzt belegt, die Verweigerungen trafen gezeigte Inhalte.
  - Dieser Fix setzt beim Verhalten danach an, mit derselben bewährten Mechanik wie BUG-181.
  - Bewertung im nächsten G1-Lauf: Schreibt der Coder nach der Sperre, oder blockiert er?

### BUG-270 – Eine Änderung rund um ein maskiertes Test-Passwort wird abgelehnt, weil `new` die unveränderte maskierte Zeile enthält
- Phase: G1 auf rc54 (Job `5c3d40a5…`, Replan 2, S014 Versuch 3, Turn 4) · Komponente: tools (engine) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_read_write.py::test_a_masked_line_left_unchanged_keeps_its_content`)
- Reproduktion:
  - S014 ändert den Aufruf `ProxmoxClient("test_server", {…})` in `ProxmoxClient({…})`.
  - `old` und `new` enthalten beide die Zeile `"password": "***REDACTED***",` unverändert.
  - Die Engine meldet `REDACTED_PLACEHOLDER`. Danach liest der Coder wieder, statt zu ändern.
- Ursache: Die Platzhalter-Prüfung (BUG-248) wies jede Markierung in `new` ab, auch eine Zeile, die nur unverändert aus `old` übernommen wurde.
- Fix:
  - `_masked_replace` ordnet die Zeilen von `old` den echten Zeilen der Datei zu.
  - Eine maskierte Zeile, die `new` unverändert wiederholt, erhält ihren echten Inhalt zurück.
  - Jede andere Markierung in `new` bleibt `REDACTED_PLACEHOLDER`. Das Geheimnis bleibt dem Coder verborgen.
- Selbstprüfung: Ergänzung zu BUG-248, keine Kompensation. Bisher war nur das Ersetzen der maskierten Zeile möglich, nicht das Bearbeiten daneben.

### BUG-269 – `status`/`decision` innerhalb von `args` machen einen gültigen Befehl zu `ARGS_INVALID`; ein verlorener Turn je Vorkommen
- Phase: G1 auf rc52/rc53 (Job `5c3d40a5…`, S008 Versuch 5 Turn 3, S014 Turn 1) · Komponente: contracts (tools) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_contracts.py::test_status_and_decision_inside_args_belong_to_the_action`)
- Reproduktion:
  - Das Modell schreibt `{"tool": "run_command", "args": {"status": "Check tomllib availability", "command": "python -c …", "decision": "check_dependency"}}`.
  - Die Engine meldet „status: Extra inputs are not permitted; decision: Extra inputs are not permitted“.
  - In den letzten drei Tagen kam das 22-mal in 20 Jobs vor.
- Ursache: `status` und `decision` sind Felder der Aktion, nicht des Werkzeugs. Die Argumentprüfung lehnte den ganzen Aufruf ab, obwohl der Befehl eindeutig war.
- Fix:
  - Ein `model_validator(mode="before")` von `CoderAction` hebt die beiden Felder aus `args` heraus.
  - Sie füllen die Aktion nur dort, wo sie leer ist, gekürzt auf 300 bzw. 120 Zeichen.
  - Kein Werkzeug hat ein Argument dieses Namens.
- Selbstprüfung: Ein echter Schritt, kein Symptom-Kompensieren. Die Felder sind eindeutig zuzuordnen, es wird nichts geraten. Unbekannte andere Argumente bleiben `ARGS_INVALID`.

### BUG-268 – Ein Schritt soll laut Ziel in collector.py arbeiten, hat die Datei aber nicht im Scope; er erbt sie als Altlast samt 28 fremden Testfehlern und stagniert
- Phase: G1 auf rc50–rc53 (Job `5c3d40a5…`, Replan 1, S008 Versuche 1–6) · Komponente: planner (validation) · Severity: P1 · blockierend: ja (behoben; Replan 2) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_a_file_the_goal_names_must_be_among_the_steps_files`)
- Reproduktion:
  - Das Ziel von S008 lautet „Implement the CollectorConfig class and data models … in collector.py“. Die `repo_hints` sind nur README.md, config.example.toml und docs/.
  - Scope v1 enthält kein collector.py. S008 erhält collector.py (845 Zeilen) und test_collector.py nur als übernommene Altlasten aus dem abgelösten Plan.
  - Damit wird S008 an den 28 fehlschlagenden Tests der Client-Schritte S009–S012 gemessen. Nach sechs Versuchen stagniert S008, wird blockiert und löst Replan 2 aus.
- Ursache:
  - Die Plan-Validierung prüfte nicht, ob die im Ziel genannten Dateien zu den Dateien des Schritts gehören.
  - Das Ziel versprach Arbeit an einer Datei, die der Scope ausschloss. Der Schritt kam nur über die Altlasten-Übernahme an sie heran, mit allem, was andere Schritte darin offen ließen.
- Fix: Die neue Regel `_goal_file_errors` gilt für workspace-verändernde Schritte. Nennt das Ziel eine bekannte Datei oder eine, die ein anderer Schritt anlegt, muss sie in `repo_hints` oder `allowed_new_paths` stehen, direkt oder über ein Verzeichnis oder Glob. Ein bloßer Dateiname zählt, wenn er eindeutig ist.
- Selbstprüfung:
  - Früheste vermeidbare Entscheidung: der gültig akzeptierte Plan mit widersprüchlichem Ziel und Scope.
  - Das ist keine Kompensation am Coder oder Verifier; die Lücke wird schon bei der Planung geschlossen.
  - Replan 2 (S014) setzte collector.py und test_collector.py selbst in den Scope. Das bestätigt, dass ein konsistenter Plan möglich ist.

### BUG-267 – Ein in `tail` gepiptes pytest mit 28 Fehlschlägen gilt als bestanden; die Runtime schließt den Schritt selbst ab
- Phase: G1 auf rc52 (Job `5c3d40a5…`, Replan 1, S008 Versuch 4, Turns 16–20) · Komponente: tools (engine) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_a_failing_command_piped_into_tail_is_a_failed_run`)
- Reproduktion:
  - S008 führt `python -m pytest test_collector.py -q 2>&1 | tail -80` aus. Ergebnis: Exit-Code 0, obwohl 28 Tests fehlschlagen.
  - Die Wiederholungssperre meldet „It succeeded … otherwise call complete_step“.
  - Nach zwei Wiederholungen schließt die Runtime den Schritt selbst ab (BUG-207).
  - Der Verifier findet 28 fehlschlagende Tests (`VERIFIER_FAILED`). Damit ist der Versuch verbraucht.
- Ursache:
  - `sh -c` liefert den Exit-Code des letzten Pipeline-Glieds, hier `tail`.
  - Alles, was den Exit-Code auswertet, hielt den Lauf deshalb für erfolgreich: `ok`, `COMMAND_REPEATED` mit `passed` und der Runtime-Abschluss.
- Fix:
  - `_sandbox_run` stellt jedem Befehl `set -o pipefail` voran. Alle Sandbox-Images sind Debian-basiert, ihre Shells (dash, bash) kennen das; ältere Shells ignorieren es.
  - Eine Pipeline meldet damit ihr erstes fehlschlagendes Glied.
- Selbstprüfung: Ein echter Schritt nach vorn, kein Kompensieren. Der Exit-Code ist die Wahrheitsquelle für Wiederholungssperre und Runtime-Abschluss, und er war bei Pipelines falsch. Lehre: Die Runtime darf aus einem Exit-Code nur schließen, was er belegt.

### BUG-266 – Seiten einer großen Datei erscheinen im Kontext als Dateianfang; der Coder sieht die Zeilen hinter 400 nie und liest bis zum Stagnationsstopp
- Phase: G1 auf rc51 (Job `5c3d40a5…`, Replan 1, S008 Versuch 2) · Komponente: context_builder, tools (engine) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_context_builder_relevance.py::test_pages_of_a_clipped_file_are_shown_from_where_they_started`, `tests/integration/test_tools_engine_read_write.py::test_reading_a_clipped_file_again_continues_where_the_last_read_stopped`)
- Reproduktion:
  - S008 blättert mit `read_file` durch `collector.py` (845 Zeilen): Turn 2 zeigt Zeilen 1–315, Turn 3 Zeilen 316–657, Turn 6 Zeilen 658–845.
  - Danach liest S008 `read_range 1-100` und `150-250`. Die Wiederholungen werden verweigert, weil der Inhalt „oben gezeigt“ sei. Nach Turn 12 folgt der Stopp `STAGNATION`, ohne dass eine Zeile geschrieben wurde.
- Ursache:
  - Ein erfolgreicher Lesezugriff erreicht den Coder nur über den Kontext des nächsten Turns (`_read_spans`, BUG-229).
  - `_read_spans` setzte für jedes `read_file` die Zeilen 1–400 an. Die Seiten 316–657 und 658–845 wurden so nie gezeigt.
  - Mit BUG-264 wurde das Blättern möglich, aber sein Ergebnis kam nicht an.
- Fix:
  - Jede Seite einer abgeschnittenen Datei beginnt mit `[pfad: lines X-Y of N]`, auch die erste.
  - `_read_spans` liest diesen Kopf aus dem Ergebnis-Digest, der auch über Checkpoints erhalten bleibt, und zeigt die Seite ab Zeile X.
- Selbstprüfung: Kein Kompensieren desselben Symptoms. BUG-264 und BUG-265 machten das Blättern möglich und ungestraft; erst dieser Fix bringt die gelesenen Zeilen zum Modell. Lehre: Eine Werkzeugänderung muss bis in die Darstellung im Kontext durchgeprüft werden, nicht nur bis zum Werkzeugergebnis.

### BUG-265 – Die Stagnationserkennung hält das Blättern durch eine große Datei für dieselbe Aktion und stoppt den Versuch
- Phase: G1 auf rc50 (Job `5c3d40a5…`, S003 Versuch 3) · Komponente: stagnation (detector) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_stagnation_detector.py::test_pages_of_a_clipped_file_are_different_actions`)
- Reproduktion:
  - Mit BUG-264 zeigen drei `read_file collector.py` nacheinander verschiedene Zeilenbereiche.
  - Der Detektor zählt trotzdem „same action 4x without workspace progress: read_file collector.py“ (zusammen mit dem Lesen aus Versuch 2) und stoppt.
  - Da die Heavy-Review-Stufe in Versuch 1 schon verbraucht war, wird S003 blockiert. Replan 1.
- Ursache: Der Aktions-Fingerabdruck bestand nur aus Werkzeug und Argumenten. Seit BUG-264 liefert dasselbe `read_file` aber verschiedene Inhalte.
- Fix: Ein seitenweises `read_file` (abgeschnitten oder ab Zeile > 1) trägt die gezeigten Zeilen als `shown_lines` im Fingerabdruck. Jede Seite ist eine eigene Aktion. Dieselbe Seite erneut zu lesen bleibt eine Wiederholung.
- Selbstprüfung: notwendige Folgekorrektur von BUG-264. Ohne sie hob der Detektor den Nutzen des Blätterns auf. Lehre: Ändert ein Werkzeug sein Verhalten bei gleichen Argumenten, muss der Fingerabdruck der Stagnationserkennung mitziehen; im Test belegt.

### BUG-264 – read_file auf eine große Datei zeigt bei jedem Aufruf denselben Anfang; der Coder liest viermal die ersten 300 Zeilen bis zum Stagnationsstopp
- Phase: G1 auf rc49 (Job `5c3d40a5…`, S003 Versuch 1, Turns 7–13) · Komponente: tools (engine) · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_read_write.py::test_reading_a_clipped_file_again_continues_where_the_last_read_stopped`)
- Reproduktion:
  - `collector.py` hat 845 Zeilen (30 KB). `read_file` zeigt etwa 300 Zeilen und den Hinweis „use read_range(start, end) for the rest“.
  - 28 von 37 Tests schlagen fehl. Der Coder wechselt zwischen `read_file collector.py` (viermal, immer derselbe Anfang) und dem verweigerten Testlauf.
  - Turn 13: Stagnationsstopp (`loop`, „same action 4x without workspace progress: read_file collector.py“). Der Versuch ist verloren.
- Ursache: Seit BUG-229 darf eine abgeschnittene Datei erneut gelesen werden. Eine Verweigerung hatte Inhalt behauptet, den der Coder nie gesehen hatte. Die Wiederholung lieferte aber denselben Ausschnitt. Der Coder nutzte den `read_range`-Hinweis nicht und kam nie über Zeile 300 hinaus, wo die getesteten Klassen standen.
- Fix:
  - Ein erneutes `read_file` derselben abgeschnittenen, unveränderten Datei setzt dort fort, wo das letzte aufhörte („lines 301-600 of 845, continuing your read of turn 7“).
  - Nach der letzten Seite oder nach einer Dateiänderung beginnt es wieder bei Zeile 1.
  - Der Hinweis nennt die gezeigten Zeilen und was das nächste `read_file` zeigt.
- Selbstprüfung: echter Schritt nach vorn, kein Pendeln zwischen BUG-229 (nicht verweigern) und einer erneuten Verweigerung. Die Wiederholung bekommt einen Nutzen: Die Absicht „mehr von der Datei sehen“ wird erfüllt, statt sie zu blockieren oder ins Leere laufen zu lassen.

### BUG-263 – Eine kurze GitLab-Störung beim Holen des Repositorys lässt den Job sofort scheitern, statt die Vorbereitung später zu wiederholen
- Phase: G1 auf rc48 (Job `cbfe8802…`, Vorbereitung) · Komponente: runtime (driver), scheduler · Severity: P2 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_model_outage_defers_planning_and_replanning_instead_of_failing`, `tests/unit/test_finalize_network.py::test_network_failures_are_recognised_and_others_are_not`)
- Reproduktion:
  - 11:38:36 schlägt `git fetch` des Mirrors fehl (exit 128). GitLab (.226) antwortet „remote: Internal API unreachable … Could not read from remote repository“, die Web-Oberfläche liefert HTTP 500.
  - Der Job endet nach 4 s mit `GIT_COMMAND_FAILED` („preparation failed“). Um 11:40:34 antwortet GitLab wieder normal.
- Ursache:
  - Der Abschluss erkannte Netzwerkfehler des Remotes und wiederholte ihn später (`GIT_NETWORK`, BUG-199). Die Vorbereitung tat das nicht, sie wiederholte nur bei Ausfällen des Modell-Hosts (BUG-176).
  - Die GitLab-Meldung „Internal API unreachable“ fehlte außerdem im Netzwerkmuster.
- Fix:
  - Die Vorbereitung bildet Netzwerkfehler von git auf `GIT_NETWORK` ab.
  - Der Scheduler behandelt `GIT_NETWORK` und `GIT_TIMEOUT` in der Vorbereitung wie einen Modellausfall: Der Job bleibt in seiner Phase und läuft nach Backoff erneut, begrenzt durch `model_outage_max_wait_seconds`.
  - Das Muster kennt „Internal API unreachable“. Echte Fehler wie „Permission denied (publickey)“ bleiben sofortige Fehler (im Test belegt).
- Selbstprüfung: echter Schritt nach vorn. Die vorhandene Ausfallbehandlung gilt jetzt durchgängig für eine weitere Phase, ohne neue Mechanik. Externe Ursache: GitLab auf .226, kein Teil dieses Systems.

### BUG-262 – Ein repo_hint auf eine Datei, die ein früherer Schritt desselben Plans anlegt, gilt als erfundener Pfad
- Phase: G1 auf rc47 (Job `99f2f1ff…`, Planung) · Komponente: planner (validation) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_a_hint_to_a_file_an_earlier_step_creates_is_valid_once_ordered`)
- Reproduktion:
  - In allen drei Planner-Antworten nennen spätere Schritte (Skripte, Dokumentation) Dateien des Collector-Schritts in ihren `repo_hints`: `tests/test_collector.py`, `collector.py`, `docs/api.md`.
  - Jede Antwort scheitert mit „does not exist in repository_inventory … never invent paths“. Die Reparaturen tauschen nur den Namen, und der Job endet mit `PLANNER_INVALID_OUTPUT` (6,5 min).
- Ursache: Die Regel kannte nur Pfade, die schon im Repository liegen. Dass ein Schritt das Ergebnis eines Vorgängers lesen soll und es deshalb in den Hinweisen nennt, ist für einen Neubau der Normalfall. Die Regel verbot es trotzdem, und ihre Meldung („files to be created belong in allowed_new_paths“) passte nicht, denn die Datei *wurde* schon von einem anderen Schritt angelegt.
- Fix:
  - Ein Pfad, den ein anderer Schritt des Plans in `allowed_new_paths` führt, ist als Hinweis gültig, wenn der Schritt (auch transitiv) von diesem abhängt.
  - Ohne Abhängigkeit nennt der Fehler den Schritt, der in `depends_on` fehlt („is created by step S001: add S001 to depends_on“).
  - Wirklich erfundene Pfade bleiben Fehler. Testpfade eines fremden Schritts fängt weiter BUG-250.
- Selbstprüfung: echter Schritt nach vorn. Die Regel unterscheidet jetzt „erfunden“ von „von einem Vorgänger geliefert“. Eine Meldung, die der Planner nicht erfüllen konnte, wird zu einer, die er erfüllen kann. Zusammen mit BUG-261 die zweite Planungsblockade in Folge, beide in der Validierung bzw. Reparatur, nicht im Modell.

### BUG-261 – Planner-Reparaturen drehen sich im Kreis: Jede Runde kennt nur die aktuellen Fehler und bricht die Regel der vorigen
- Phase: G1 auf rc46 (Job `28f184c5…`, Planung) · Komponente: planner (loop, prompt) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_loop.py::test_a_repair_turn_keeps_the_rules_earlier_answers_broke`)
- Reproduktion:
  - Erster Plan: S004 schreibt nur Tests für den Code von S002 und S003, was die Validierung ablehnt (BUG-250-Regel).
  - Reparatur 1 legt alles in S002 zusammen. S002 hat nun 10 neue Dateien, erlaubt sind 8.
  - Reparatur 2 teilt wieder auf, und S003 schreibt erneut nur Tests für S002.
  - Nach zwei Reparaturen endet der Job mit `PLANNER_INVALID_OUTPUT` (4,5 min, kein Schritt gelaufen).
- Ursache: Eine Reparaturrunde zeigt dem Modell nur die letzte Antwort und deren Fehler. Was frühere Runden abgelehnt hatten, war vergessen. Bei zwei gegenläufigen Regeln (Tests zum Code, höchstens 8 Dateien je Schritt) pendelte das Modell zwischen beiden Verstößen.
- Fix: Ab der zweiten Reparatur listet die Nachricht zusätzlich die Fehler früherer Antworten, mit der Vorgabe: „the corrected plan must still satisfy these too“. Das gilt auch für den Replanner, der dieselbe Schleife nutzt.
- Selbstprüfung: echter Schritt nach vorn. Die Reparaturschleife bekommt ein Gedächtnis, statt die Regeln zu lockern oder das Reparaturbudget zu erhöhen. Die Lösung (nach Komponenten aufteilen, jede mit ihren Tests) nennt die Dateigrenzen-Meldung bereits.

### BUG-260 – Eine Eskalation, die nie ausgeführt wurde (Heavy Review durch Deploy abgebrochen), verbraucht ihre Stufe; die nächste Stagnation geht direkt zum Replan
- Phase: G1 auf rc45 (Job `3d90ec7b…`, Replan 2, S002 Versuche 2 und 4) · Komponente: stagnation (persistence) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_stagnation_persistence.py::test_an_escalation_an_interrupted_attempt_never_ran_is_not_used_up`)
- Reproduktion:
  - Versuch 2 stoppt um 10:50 (`loop`, Empfehlung `heavy_review`). Das Review startet, der Deploy bricht es ab („fail-closed: the review was cancelled“), und der Versuch endet `checkpointed`.
  - Der fortgesetzte Versuch 4 stoppt um 10:59 (`edit_mechanics`). Die Leiter hält `heavy_review` für verbraucht und empfiehlt `replan`.
  - Der Schritt wird blockiert. Ohne verbleibenden Replan scheitert der Job.
- Ursache: `prior_escalations` zählte jede Empfehlung früherer Versuche, auch wenn der Versuch vor ihrer Ausführung unterbrochen wurde (Checkpoint, Worker-Ausfall, Absturz).
- Fix: Nur Versuche, die über den Stagnationsweg endeten (`error_code = STAGNATION`: Korrektur nach Review oder Recherche, oder Block), verbrauchen ihre Stufe.
- Selbstprüfung: echter Schritt nach vorn. Die Leiter bildet ab, was tatsächlich geschah. Mit BUG-259 und BUG-249 der dritte Fehler, bei dem eine Unterbrechung Zustand verfälschte. Unterbrechungen bleiben ein eigener Prüfpunkt für künftige Änderungen.

### BUG-259 – Ein fortgesetzter Korrekturversuch arbeitet mit veralteten Verifier-Befunden; der Coder behebt längst Behobenes, bis die Stagnationserkennung den Schritt blockiert
- Phase: G1 auf rc45 (Job `3d90ec7b…`, Replan 2, S002 Versuch 4) · Komponente: coder (handler) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_coder_handler.py::test_a_resumed_correction_gets_verifier_items_of_the_current_work_tree`)
- Reproduktion:
  - Korrekturversuch 2 von S002 entfernt die ungenutzten Imports `ssl` und `threading` aus `collector.py`. Danach unterbricht der rc45-Deploy den Versuch (`checkpointed`).
  - Versuch 4 (`resume`) erhält dieselben Korrektur-Items mit der alten Lint-Ausgabe („`ssl` imported but unused“, Zeile 12).
  - Der Coder versucht viermal, die Imports zu entfernen: zweimal `TEXT_NOT_FOUND`, zweimal `'old' and 'new' are identical`. Die Stagnationserkennung stoppt (`edit_mechanics`). S002 wird blockiert, und der Job scheitert mit `REPLAN_LIMIT`.
- Ursache: Ein fortgesetzter Versuch übernimmt die Korrektur-Items des unterbrochenen (BUG-249). Was der unterbrochene Versuch schon geändert hatte, spiegelten die Items nicht mehr wider. Lint- und Secret-Befunde sind aber Zustände des Arbeitsbaums, keine festen Aufgaben.
- Fix:
  - Hat der unterbrochene Versuch laut Checkpoint-Historie Dateien geändert, prüft der Verifier vor dem Fortsetzen den aktuellen Stand.
  - Die Verifier-Items werden durch die aktuellen Befunde ersetzt (keine, wenn er besteht). Review- und Stagnations-Items bleiben.
- Selbstprüfung: echter Schritt nach vorn. Die Korrektur beschreibt wieder den Ist-Zustand, statt den Coder gegen einen vergangenen Zustand arbeiten zu lassen. Kosten: ein Verifier-Lauf je fortgesetztem Korrekturversuch mit Änderungen.

### BUG-258 – Ein Korrekturversuch meldet Erfolg, ohne die Datei anzufassen, deren Befunde die Korrektur nennt
- Phase: G1 auf rc43 (Job `3d90ec7b…`, Replan 1, S002 Versuch 5) · Komponente: tools (engine), coder (handler) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_completion_is_refused_once_while_a_file_the_correction_names_is_untouched`)
- Reproduktion:
  - Die Korrektur nennt Secret-Funde in `tests/test_collector.py` (Zeilen 162/169), Lint (`MagicMock` ungenutzt) und Review-Befunde in `collector.py`.
  - Der letzte Versuch (5/5) schreibt `collector.py` neu, führt die Tests aus (grün) und ruft nach vier Turns `complete_step`.
  - `tests/test_collector.py` bleibt unverändert. Der Verifier meldet dieselben Funde, und S002 wird blockiert (Replan 2 von 2).
- Ursache: `complete_step` war immer terminal. Ob der Versuch die Dateien angefasst hatte, die seine Korrektur nennt, prüfte niemand. Der Coder hielt grüne Tests für „fertig“.
- Fix:
  - Der Handler gibt die Pfade der Korrektur-Items an die Tool-Engine (`expect_corrections`).
  - Ist eine dieser Dateien vorhanden und in diesem Versuch unverändert, verweigert die Engine den ersten `complete_step` mit `CORRECTION_OPEN` und nennt die Dateien.
  - Ein zweiter Aufruf wird angenommen (die Befunde können schon erledigt sein). Im letzten Turn wird nie verweigert.
- Selbstprüfung: echter Schritt nach vorn. Eine deterministische, einmalige Erinnerung zum Zeitpunkt der Entscheidung, keine weitere Prompt-Regel und keine Sperre. Kosten: höchstens ein Turn je Korrekturversuch, in dem der Coder die genannten Dateien bewusst nicht ändert.

### BUG-257 – Nach einem Replan übernimmt der erste laufende Schritt die Altlasten aller abgelösten Schritte, auch Dateien, die der neue Plan anderen Schritten gibt
- Phase: G1 auf rc43 (Job `3d90ec7b…`, Replan 1, S002 Versuch 4) · Komponente: coder (handler), gitops (engine) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_coder_handler.py::test_leftovers_another_open_step_declares_wait_for_that_step`, `tests/integration/test_gitops_engine.py::test_parked_leftovers_return_to_head_and_never_overwrite_a_changed_path`)
- Reproduktion:
  - Vor dem Replan ließen die abgelösten S003 (Frontend) und S004 (Skripte) `frontend.*` und drei `.ps1`-Skripte unfertig im Arbeitsbaum.
  - Der neue Plan gibt diese Dateien S003 und S006 (`allowed_new_paths`). Trotzdem übernimmt S002 (Collector) alle sieben Dateien (`scope.expanded`, `adopted_leftovers`, 09:49).
  - Verifier und Review beurteilen S002 nun auch nach Frontend und Skripten: ein Secret-Fund in `start_homelab_widget.ps1` (BUG-256) und „das Skript verweist auf widget.html statt frontend.html“. Dazu wird der Diff so groß, dass dem Reviewer `tests/test_collector.py` fehlt.
  - S002 scheitert mit `VERIFIER_FAILED`, danach STAGNATION und Korrekturen zu fremder Arbeit. Der letzte Replan ist in Gefahr.
- Ursache: `adopt_leftovers` (BUG-206/255) schreibt jede liegengebliebene Änderung dem Schritt zu, der zufällig als Nächster läuft. Ob ein anderer offener Schritt die Datei laut Plan liefern soll, prüfte niemand.
- Fix:
  - Vor jedem Versuch ermittelt der Handler die `allowed_new_paths` der anderen offenen Schritte (weder `completed`/`cancelled` noch abgelöst).
  - Eine Altlast, die ein anderer Schritt beansprucht und die der laufende Schritt weder in `allowed_new_paths` noch in `repo_hints` nennt, wird mit `GitEngine.park_paths` beiseitegelegt: Kopie nach `.git/hermclaw-parked/`, der Pfad kehrt zu HEAD zurück, auditiert als `workspace.park`.
  - Startet der beanspruchende Schritt, holt `unpark_paths` die Datei zurück (Pfade und Globs). Ein inzwischen geänderter Pfad behält seine Fassung; die geparkte Kopie bleibt erhalten.
  - Nicht beanspruchte Altlasten werden weiter übernommen (BUG-206).
- Selbstprüfung: echter Schritt nach vorn, keine weitere Kompensation. BUG-206 und BUG-255 lösten „wem gehört die Altlast“ mit „dem nächsten Schritt“. Jetzt entscheidet der Plan, und frühere Arbeit geht nicht verloren, sondern erreicht den richtigen Schritt.

### BUG-256 – Ein PowerShell-Befehl als Wert (`$env:HERMCLAW_TOKEN = Get-Content $TokenFile -Raw`) gilt als Secret
- Phase: G1 auf rc42 (Job `3d90ec7b…`, Replan 1, S002 Versuch 1) · Komponente: verifier (secrets, languages) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_verifier_secrets.py::test_ordinary_code_is_not_reported[start.ps1-…]`)
- Reproduktion:
  - S002 übernimmt das liegengebliebene `start_homelab_widget.ps1` des abgelösten S004.
  - Die Secret-Prüfung meldet Zeile 64 `$env:HERMCLAW_TOKEN = Get-Content $TokenFile -Raw` als `secret_assignment` mit der Abhilfe „Remove the value and load it from the environment“. Die Zeile tut genau das.
  - Der Versuch scheitert mit `VERIFIER_FAILED`, und die Korrektur verlangt etwas Unerfüllbares.
- Ursache: Ein ungequoteter Wert nach `token =` gilt nur in `CODE_LANGUAGES` als Ausdruck. PowerShell (`.ps1` → `powershell`) fehlte dort und wurde wie eine Konfigurationsdatei behandelt: Jeder Wert galt als Literal. In PowerShell ist ein ungequotetes Wort aber ein Befehl oder eine Variable.
- Fix: `powershell` gehört zu `CODE_LANGUAGES`. Gequotete Literale (`$ApiToken = "…"`) werden weiter gemeldet (neuer Positivfall im Test).
- Selbstprüfung: echter Schritt nach vorn, eine fehlende Sprache in einer allgemeinen Tabelle, kein Symptom-Workaround. Beobachtung ohne Fix: S002 übernimmt Dateien des abgelösten S004, die nach dem Plan S006 gehören (BUG-206-Design). Ob das Kosten verursacht, zeigt der weitere Lauf.

### BUG-255 – Liegengebliebene Dateien eines Schritts, der zwischen zwei Versuchen eines anderen lief, gelten als dessen Scope-Verstoß
- Phase: G1 auf rc40/41 (Job `3d90ec7b…`, S003 Versuch 5) · Komponente: coder (handler), scope · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_coder_handler.py::test_leftovers_of_a_step_that_ran_between_two_attempts_are_adopted`)
- Reproduktion:
  - S003 (Frontend) startet und wird beim Deploy checkpointed.
  - Danach läuft S002 (Collector), legt `collector.py`, `README.md` und `requirements.txt` an und wird blockiert.
  - S003 setzt fort und scheitert nach 20 Turns mit `SCOPE_VIOLATION` (3 out-of-scope changes) für Dateien, die es nie geändert hat.
- Ursache: `adopt_leftovers` (BUG-206) lief nur beim ersten Versuch eines Schritts. Läuft ein anderer Schritt zwischen zwei Versuchen und bleibt unfertig, wurden seine Dateien dem fortgesetzten Schritt zugeschrieben.
- Fix:
  - Vor jedem Versuch übernimmt der Handler liegengebliebene Änderungen in den Scope.
  - Ausgenommen sind Dateien, die der Schritt selbst geändert hat (seine `file.changed`-Ereignisse). Ein eigener Scope-Verstoß bleibt also ein Verstoß.
- Selbstprüfung: echter Schritt nach vorn. Erweiterung von BUG-206 auf einen Fall, den die Annahme „ein Schritt nach dem anderen“ übersah: Checkpoints und blockierte Schritte verschränken die Versuche zweier Schritte.

### BUG-254 – Das Schrittziel bietet ein nicht angefordertes Drittpaket an („tomllib/tomli_w“); der Coder versucht es und blockiert
- Phase: G1 auf rc40 (Job `3d90ec7b…`, S002 Versuch 3) · Komponente: planner (prompt) · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_prompt.py::test_research_is_for_external_facts_not_the_repository_itself`, Nadel „never names a third-party package the job does not ask for“)
- Reproduktion:
  - Ziel von S002: „Verwende configparser oder tomllib/tomli_w für TOML.“
  - Der Coder importiert `tomli_w`, legt `requirements.txt` an, und `install_dependencies` wird verweigert („Solve it with the standard library … remove the manifest and the imports“).
  - Danach `block_step` (`scope_unavailable`) nach 15 Turns. S002 ist blockiert und braucht einen Replan.
- Ursache: Die Standardbibliotheks-Regel des Planners verbot nicht, ein Drittpaket als *Option* zu nennen. Für TOML kennt Python nur das Lesen (`tomllib`), also bot der Planner ein Schreibpaket an. Der Coder folgte dem Ziel statt der Laufzeitregel.
- Fix: Die Planner-Regel verlangt: Ein Ziel nennt nie ein Drittpaket, das der Auftrag nicht verlangt, auch nicht als Option. Liest die Standardbibliothek ein Format nur, verlangt das Ziel einen kleinen eigenen Writer für die geschriebene Teilmenge, geprüft durch Zurücklesen (passt zur Coder-Regel aus BUG-245).
- Selbstprüfung: Prompt-Regel, keine deterministische Prüfung. Paketnamen im Freitext lassen sich ohne Paketliste nicht verlässlich erkennen. Die Laufzeit verweigert die Installation weiterhin deterministisch. Beobachten, ob Planner-Ziele weiter Pakete anbieten; dann eine Validierung gegen die vom Auftrag genannten Pakete prüfen.

### BUG-253 – Ein Schreibzugriff ohne Änderung gilt als Erfolg; der Coder schreibt dieselbe Datei viermal und löscht dabei LATEST FAILURE
- Phase: G1 auf rc39 (Job `3d90ec7b…`, S004 Versuche 2/3) · Komponente: tools (engine) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_read_write.py::test_write_file_create_modify_scope_and_events`)
- Reproduktion: Nach Review-Befunden schreibt der Coder `start_homelab_widget.ps1` viermal hintereinander mit identischem Inhalt. Antwort jeweils `ok`: „already has this content; nothing written“. Die Stagnationserkennung bricht ab; S004 endet `blocked` (STAGNATION).
- Ursache:
  - Ein No-op-Schreibzugriff war ein Erfolg. Damit löschte er `LATEST FAILURE`, denn ein erfolgreicher Befehl ersetzt die letzte Fehlermeldung.
  - Die Meldung sagte nicht, dass die offenen Befunde so nicht behoben werden.
- Fix: Ein Schreibzugriff, der die Datei nicht ändert, wird verweigert (`NO_CHANGE`, `repeated`). Damit bleibt `LATEST FAILURE` erhalten (Logik aus BUG-175). Die Meldung nennt den Ausweg: die Zeilen, die Korrektur oder Fehlermeldung nennen, mit anderem Inhalt ändern, oder prüfen bzw. abschließen.
- Selbstprüfung: echter Schritt nach vorn. Gleiches Muster wie bei Lese- und Befehlswiederholungen (BUG-179/181): Eine wirkungslose Aktion ist kein Erfolg. Kein Ausgleich eines Symptoms, sondern eine fehlende Fallunterscheidung.

### BUG-252 – Eine am Ausgabelimit abgeschnittene `write_file`-Antwort geht samt aller fertigen Zeilen verloren
- Phase: G1 auf rc39 (Job `3d90ec7b…`, S002 Versuch 1) · Komponente: coder (loop), models (gateway) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_coder_loop.py::test_a_cut_off_write_file_keeps_its_complete_lines`, `tests/unit/test_coder_salvage.py`)
- Reproduktion: Der Coder schreibt `collector.py` in einer Antwort. Zweimal hintereinander bricht sie nach 8192 Tokens ab (je ~5,5 min, `finish_reason=length`), trotz Hinweis aus BUG-223 und der 250-Zeilen-Regel. Nichts wird geschrieben. Ein dritter Abbruch beendet den Versuch mit `model_failed`.
- Ursache: Eine abgeschnittene Antwort ist ungültiges JSON und wurde ganz verworfen. Bei `write_file` lagen die fertigen Zeilen aber vollständig vor. Das Modell befolgt „in Teilen schreiben“ nicht verlässlich.
- Fix:
  - `ModelOutputTruncated` trägt den abgeschnittenen Text (`partial`). Er steht nicht in den `details`, die in Events landen.
  - `salvage_write` liest daraus Pfad, `append` und die vollständigen Zeilen des `content`-Strings, robust gegen abgeschnittene Escapes, und verlangt mindestens 20 Zeilen.
  - Der Coder-Loop schreibt sie über die Engine (Scope, Redaktion und Prüfungen wie bei jedem Schreibzugriff). Der nächste Turn erfährt die letzten geschriebenen Zeilen und setzt mit `"append": true` danach fort.
  - Andere abgeschnittene Antworten behalten das bisherige Verhalten.
- Selbstprüfung: echter Schritt nach vorn. Statt eine weitere Prompt-Regel einzuführen, nutzt die Runtime die bereits erzeugte Arbeit und macht aus dem Abbruch das gewünschte Schreiben in Teilen.

### BUG-251 – Schritte, die eine Komponente eines anderen Schritts nutzen, laufen ohne Abhängigkeit davor und raten deren Schnittstelle
- Phase: G1 auf rc39 (Job `3d90ec7b…`, Plan v1, S003/S004) · Komponente: planner (validation) · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_a_step_naming_another_steps_component_must_be_ordered_with_it`)
- Reproduktion:
  - S004 („Skript, das den Collector … startet“) und S003 („liest Daten von der lokalen JSON-API des Collectors“) hängen nicht von S002 ab, der `collector.py` schreibt.
  - S004 läuft vor S002. Das Review findet `config.json` statt TOML, einen fest verdrahteten Port 8080 und kein Schutz-Token.
- Ursache: Die Schnittstellenregel aus BUG-234 ist nur eine Prompt-Regel; der Planner befolgt sie nicht verlässlich. Die Validierung prüfte nicht, ob ein Schritt eine Komponente eines anderen Schritts nennt, ohne mit ihm geordnet zu sein.
- Fix: Nennt Titel oder Ziel eines arbeitsbaum-ändernden Schritts den Dateistamm einer Code-Datei eines anderen Schritts, muss eine Abhängigkeit beide ordnen, in beliebiger Richtung, auch transitiv.
  - Der Stamm muss mindestens fünf Zeichen haben und darf nicht generisch sein (`config`, `index`, `utils` …); `collector` zählt.
  - Abgeschlossene Schritte eines Replans sind ausgenommen.
  - Die Fehlermeldung nennt die fehlende Abhängigkeit.
- Selbstprüfung: echter Schritt nach vorn. Die Runtime sichert jetzt deterministisch ab, was die Prompt-Regel nicht durchsetzen konnte; das ist keine weitere Prompt-Regel. Ein Test-Fixture (Doku-Schritt über das neue Pagination-Modul) wurde zu Recht beanstandet und bekam die Abhängigkeit. Beobachten: Fehlalarme bei Stämmen, die gewöhnliche Wörter sind.

### BUG-250 – Der Frontend-Schritt schreibt die Tests des Collectors und hängt nicht von dessen Schritt ab
- Phase: G1 auf rc39 (Job `3d90ec7b…`, Plan v1) · Komponente: planner (validation), coder (handler) · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_tests_for_new_code_belong_in_the_same_step`, `tests/integration/test_coder_handler.py::test_dependency_files_fall_back_to_the_jobs_other_completed_steps`)
- Reproduktion:
  - S002 schreibt `collector.py` und `config*.toml`.
  - S003 schreibt `frontend.html/css/js` *und* `tests/test_collector.py` samt Beispieldaten, hängt aber nur von der Recherche S001 ab.
  - Keine Schnittstellendatei, obwohl die Regel aus BUG-234 sie verlangt.
- Ursache:
  1. Die Prüfung aus BUG-237 erfasste nur Schritte, die *ausschließlich* Tests schreiben. Ein Schritt mit eigenem Code und fremden Tests lief durch.
  2. BUG-242 zeigte nur die Dateien von Abhängigkeiten. Fehlt die Abhängigkeit, sieht der Frontend-Schritt den Collector nicht, obwohl dessen Dateien im gemeinsamen Arbeitsbaum liegen.
- Fix:
  - Die Validierung lehnt Testdateien ab, die nach dem Code eines anderen Schritts benannt sind (`tests/test_collector.py` ↔ `collector.py`), auch in Schritten mit eigenem Code. Diese Testpfade gehören in den Schritt, der den Code schreibt.
  - `dependency_files` nennt nach den Abhängigkeiten auch die Dateien der übrigen abgeschlossenen Schritte des Jobs, neueste zuerst. Ersetzte Schritte zählen nicht, die Obergrenze bleibt 12.
- Selbstprüfung: echter Schritt nach vorn, aber Erweiterung eigener Fixes (BUG-237, BUG-242). Beide griffen zu eng, und der Planner befolgt die Schnittstellenregel nicht verlässlich. Deshalb sichert jetzt die Runtime ab, statt einer weiteren Prompt-Regel. Nicht mitten im Lauf deployt: Der Lauf `3d90ec7b` soll rc39 belegen.

### BUG-249 – Nach einem Checkpoint verliert jeder weitere Versuch die Korrektur-Belege und setzt einen veralteten Stand fort
- Phase: G1 auf rc38 (Job `8f394b63…`, S007 Versuche 3–5; ebenso S003 nach dem Deploy) · Komponente: scheduler, coder (handler) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_model_outage_during_a_step_does_not_use_up_its_attempts`, Fälle h5/h6)
- Reproduktion:
  - S007 Versuch 1 wird beim Deploy checkpointed. Versuch 3 scheitert am Verifier: `correction.started`, 1 Element, vier `F401`-Funde.
  - Versuch 4 startet als `resume`, nicht als `correction`. `LATEST FAILURE` ist leer (`omitted_reason: empty`). Der Coder sieht die Lint-Funde nie, ändert nichts und meldet `complete_step`. Ebenso Versuch 5.
- Ursache:
  1. Der Scheduler löschte `steps.checkpoint` nur bei `completed`. Nach einem echten Fehlschlag blieb der alte Checkpoint, und jeder weitere Versuch wurde ein `resume`: alter Verlauf, keine `correction_input`.
  2. Ein Korrekturversuch, der seinerseits unterbrochen wurde, verlor seine Belege im folgenden `resume`.
- Fix:
  - Ein echter Fehlschlag verbraucht den Checkpoint (`st.checkpoint = {}`). Ein Ausfall (Modell/Worker) behält ihn, weil der Versuch nichts getan hat.
  - Ein `resume`-Versuch erbt die ausstehende `correction_input` des Versuchs, den er fortsetzt.
  - Der Coder-Handler nutzt Korrektur-Belege, sobald sie ausstehen, unabhängig von der Versuchsart.
- Selbstprüfung: echter Schritt nach vorn. Die vierte Buchhaltungs-Lücke im Zusammenspiel Deploy/Checkpoint (BUG-232/233/236/249), jede ein eigener Pfad. Diese erklärt die verlorenen Korrekturen von S003 und S007. Weiter gilt: nur deployen, wenn es sich lohnt.

### BUG-248 – Ein maskiertes Geheimnis in einer Datei lässt sich mit keinem Werkzeug ersetzen, obwohl der Verifier es verlangt
- Phase: G1 auf rc36/37 (Job `8f394b63…`, S003 Versuche 3–7) · Komponente: tools (engine) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_read_write.py::test_a_masked_secret_can_be_replaced_by_the_line_as_shown`)
- Reproduktion:
  - `tests/test_collector.py:128` enthält `api_key='…'`. Jeder Lesezugriff zeigt `api_key='***REDACTED***'`.
  - `replace_text` mit der gezeigten Zeile findet den Text nicht. `write_file` mit dem Marker wird verweigert („Leave lines with secrets untouched“).
  - Der Verifier blockiert mit `secret_assignment` jeden Versuch. S003 endet `repeated_verifier_failure`, und der Job geht in den Replan.
- Ursache: Die Redaktion schützt Geheimnisse vor dem Modell, und die Werkzeuge verweigern das Zurückschreiben des Markers. Zusammen machten sie genau die Änderung unmöglich, die der Verifier verlangte. Die Anweisung „edit around them“ widersprach dem Verifier.
- Fix:
  - `replace_text` sucht ein `old` mit Redaktionsmarker in der redigierten Sicht der Datei, zeilenweise. Der Treffer muss eindeutig sein.
  - Der Text um den Treffer auf den betroffenen Zeilen darf keinen maskierten Teil enthalten. So wird nichts Maskiertes zurückgeschrieben, und das Modell sieht das Geheimnis nie.
  - Ist eine Zeile mehrdeutig oder enthält sie ein zweites Geheimnis, wird verweigert, und der Hinweis nennt den Weg: die ganze Zeile wie gezeigt kopieren.
  - Die Meldungen zum Marker beschreiben jetzt das Ersetzen statt „untouched“.
- Selbstprüfung: echter Schritt nach vorn. Ein Widerspruch zwischen zwei Komponenten wurde aufgelöst, ohne den Schutz aufzuweichen: Das Geheimnis wird weder gezeigt noch zurückgeschrieben.

### BUG-247 – Korrekturen aus dem Verifier enthalten nur dessen Kurzmeldung, nicht die Belege (z. B. die Lint-Ausgabe)
- Phase: G1 auf rc36/37 (Job `8f394b63…`, S003 Versuche 3–7) · Komponente: context_builder (render) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_context_builder_sections.py::test_verifier_correction_items_carry_the_check_evidence`)
- Reproduktion: `lint:python` schlägt mit sechs `F401`-Funden fehl (`hashlib`, `hmac`, `sys`, `copy`, `MagicMock`, `StringIO`). Der Coder bekommt als Korrektur nur „lint reported problems (exit code 1)“ und behebt in vier Versuchen keinen Fund.
- Ursache: `CorrectionItem.from_verification` übernahm nur `message`. Die Belege des Checks (`evidence.output`, `stderr` …) kamen nie beim Coder an, obwohl der Review-Pfad dafür `evidence_excerpt` schon hatte.
- Fix: Jedes Korrektur-Element aus dem Verifier enthält einen Auszug der Belege (bis 800 Zeichen, Ausgabe zuerst, redigiert, ohne `path`). Der Auszug wird mit `evidence_excerpt` aus der Review-Korrektur gebildet.
- Selbstprüfung: echter Schritt nach vorn. Die Information lag vor und ging an einer Schnittstelle verloren; kein Symptom-Ausgleich.

### BUG-246 – Die Verweigerung eines wiederholten fehlgeschlagenen Befehls verweist auf LATEST FAILURE, das dessen Ausgabe nicht mehr zeigt
- Phase: G1 auf rc36 (Job `8f394b63…`, S003 Versuch 4, Turns 9–14) · Komponente: tools (engine) · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_a_refused_repeat_of_a_failed_command_carries_its_output`)
- Reproduktion:
  - `python -m pytest -q tests/test_collector.py` schlägt fehl (Turn 1).
  - Ein Diagnosebefehl `python -c …` gelingt (Turn 9), und `LATEST FAILURE` wird geleert.
  - Der Coder schlägt den Testbefehl fünfmal hintereinander erneut vor (je ~45 s). Jedes Mal lautet die Antwort: „command refused … Its exact output is in LATEST FAILURE“. Der Abschnitt zeigt aber nur noch die Verweigerung (505 Zeichen statt 1391).
- Ursache: BUG-175 hält die letzte echte Fehlermeldung über verweigerte Wiederholungen hinweg. Ein *anderer*, erfolgreicher Befehl löscht sie jedoch, obwohl sich keine Datei geändert hat. Die Verweigerung behauptete dann etwas, das der Kontext nicht mehr zeigte.
- Fix:
  - Die Engine merkt sich die gerenderte Ausgabe jedes fehlgeschlagenen Befehls (halbes Ausgabelimit).
  - Die Verweigerung einer Wiederholung enthält diese Ausgabe selbst: „Its output was: …“.
  - Ein erfolgreicher Lauf und eine erfolgreiche Installation verwerfen die gemerkte Ausgabe.
- Selbstprüfung: echter Schritt nach vorn. Die Verweigerung trägt jetzt ihren Beleg selbst, statt sich auf den Zustand einer anderen Komponente zu verlassen. Gleiches Muster wie BUG-229: Eine Sperre darf nur auf Inhalte verweisen, die nachweislich sichtbar sind.

### BUG-245 – „Nur Standardbibliothek“ führt zu handgeschriebenem TOML-Parser und -Writer, der verschachtelte Tabellen zerstört
- Phase: G1 auf rc36 (Job `8f394b63…`, S003 Versuch 3/4) · Komponente: context_builder (System-Vertrag des Coders), review (prompt) · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_prompt.py::test_prompts_explain_how_dependencies_get_into_the_sandbox`, `tests/unit/test_review_prompt.py` Nadel „Hand-written parsers“)
- Reproduktion:
  - `collector.py` enthält einen eigenen TOML-Parser (`_parse_toml`) und -Writer (`save_config`).
  - Der Writer schreibt die verschachtelte Tabelle `proxmox.main` als `main = {…}` per `str(dict)`, also kein gültiges TOML.
  - `test_save_load_roundtrip` schlägt fehl. Der Coder kreist zwei Versuche lang darum (verweigerte Lese- und Befehlswiederholungen, keine Änderung).
- Ursache: Planner und Coder sollen die Standardbibliothek bevorzugen. Python liest TOML mit `tomllib`, schreibt es aber nicht. Ohne Hinweis baute der Coder beide Richtungen selbst, mit Fehlern, die der echte Parser nicht hat. Der Reviewer prüfte das nicht eigens.
- Fix:
  - Der System-Vertrag des Coders schreibt vor: JSON/TOML/CSV/INI/XML mit dem Parser der Sprache lesen, nie handgeschrieben. Bei einem Format, das die Sprache nur liest (tomllib), die geschriebene Ausgabe testen, indem man sie damit zurückliest.
  - Der Reviewer stuft handgeschriebene Parser für Formate, die die Standardbibliothek liest, als major ein.
  - Damit der Vertrag ins kleinste Kontextfenster (4096 Tokens) passt, sind andere Regeln kürzer formuliert, inhaltlich unverändert.
- Selbstprüfung: echter Schritt nach vorn. Die Regel ist allgemein und schließt eine Lücke der Standardbibliotheks-Regel. Sie bekämpft kein Symptom.

### BUG-244 – Der Coder liest Teilbereiche eines Bereichs erneut, den der Kontext bereits vollständig zeigt
- Phase: G1 auf rc36 (Job `8f394b63…`, S003 Versuch 4) · Komponente: tools (engine) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_read_write.py::test_a_part_of_a_visible_read_is_refused_until_a_file_changes`)
- Reproduktion: `collector.py` gelesen als 100–160, danach 110–160, danach 110–145, ohne Schreibzugriff dazwischen. Jeder Lesezugriff kostete einen Turn von etwa 45 s.
- Ursache: Die Wiederholungssperre (BUG-179) verweigerte nur identische Lesezugriffe. Ein Teilbereich eines vollständig sichtbaren Lesezugriffs zeigt nichts Neues, lief aber durch.
- Fix:
  - `read_range` wird verweigert (`READ_REPEATED`), wenn ein vollständig sichtbarer Lesezugriff derselben Datei im 6-Turn-Fenster ohne Schreibzugriff den Bereich enthält: die ganze Datei oder ein umfassender Bereich.
  - Geprüft wird nach der Bereichsprüfung gegen das tatsächliche Dateiende, `RANGE_INVALID` bleibt also vorrangig.
  - Abgeschnittene oder zu lange Lesezugriffe (BUG-229) zählen nicht als sichtbar; deren Teilbereiche bleiben erlaubt.
- Selbstprüfung: dritte Änderung an derselben Sperre (BUG-179, BUG-229, BUG-244), deshalb geprüft:
  - Die Regel nutzt genau das Sichtbarkeitskriterium aus BUG-229 und verweigert nur, was der Kontext nachweislich zeigt.
  - Das ist eine Verallgemeinerung, kein Gegensteuern gegen ein Symptom.
  - Beobachten: Ob verweigerte Teil-Lesezugriffe in einem Live-Lauf zum Kreisen führen.

### BUG-243 – Ein Geheimnis-Fund in einer Testdatei empfiehlt „aus der Umgebung laden“ statt eines Platzhalters
- Phase: G1 auf rc36 (Job `8f394b63…`, S003 Versuch 3) · Komponente: verifier (secrets) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_verifier_secrets.py::test_secret_findings_in_tests_ask_for_a_placeholder_not_the_environment`, `::test_helpers`)
- Reproduktion: `tests/test_collector.py` Zeile 128 enthält `api_key='<7 Kleinbuchstaben>'` als Fixture. Zwei blockierende Prüfungen (`secrets`, `acceptance[1]:security`) melden: „Remove the value and load it from the environment or a secret store.“
- Ursache: Der Verifier gibt dieselbe Abhilfe für Produktivcode und Tests. In einem Test ist „aus der Umgebung laden“ falsch: Ein Test darf keine echten Geheimnisse lesen. Er braucht einen Platzhalter, den der Scanner akzeptiert, und welche Werte der Scanner akzeptiert, sagte die Meldung nicht.
- Fix:
  - Findet die Prüfung ein mögliches Geheimnis in einer Testdatei, verlangt die Abhilfe einen offensichtlichen Platzhalter mit `dummy`, `fake` oder `example` (z. B. `dummy-token`).
  - `fake`, `mock` und `sample` gelten als Platzhalter-Bestandteile.
  - Für Produktivcode bleibt die bisherige Abhilfe.
- Selbstprüfung: echter Schritt nach vorn. Die Erkennung bleibt unverändert; nur die Anweisung passt jetzt zum Dateityp, und die genannten Platzhalter werden tatsächlich akzeptiert.

### BUG-242 – Ein Schritt kennt die Dateien seiner Vorgänger-Schritte nicht und rät deren Schnittstellen
- Phase: G1 auf rc33–35 (Jobs `8e7c4b65…`, `8f394b63…`) · Komponente: coder (handler), context_builder · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_context_builder_relevance.py::test_files_delivered_by_dependency_steps_are_shown`, `tests/integration/test_coder_handler.py::test_two_implement_steps_with_real_verifier_commit_separately`)
- Reproduktion:
  - Die Qualitätsprüfung der G1-Lieferung `8e7c4b65`: `static/app.js` liest `metrics.ram`, der Collector liefert `metrics.mem.usage`. Das Widget zeigt nie Werte.
  - Im Plan `8f394b63` hängt S004 (Frontend) von S003 (Collector) ab, nennt `collector.py` aber weder in `repo_hints` noch im Ziel.
- Ursache: Der Coder sah nur Ziel, Scope und die eigenen Lesezugriffe. Welche Dateien die abgeschlossenen Vorgänger committet hatten (`steps.result.files`), erreichte seinen Kontext nie. Ohne einen Hinweis des Planners rät der Coder das JSON-Format und die Funktionsnamen.
- Fix:
  - Der Handler sammelt die Dateien abgeschlossener Abhängigkeiten (transitiv, nächste zuerst, höchstens 12) in `StepBrief.dependency_files`.
  - Der Context Builder nennt sie in STEP GOAL: „Files earlier steps delivered (use their real interfaces …)“.
  - In RELEVANT CODE zeigt er sie als Dateiköpfe mit Herkunft `[dependency]`, begrenzt durch `max_target_files`.
- Selbstprüfung: echter Schritt nach vorn. Die Information lag in der Runtime vor und kam nur nicht beim Coder an. Das ist allgemein und ergänzt die Planner-Regel aus BUG-234, die der Planner nicht verlässlich befolgt.

### BUG-241 – Ein einziger ungültiger Replan beendet den Job; der Replanner wiederholt alle offenen Schritte und läuft ins Ausgabelimit
- Phase: G1 auf rc34 (Job `2f72c0da…`) · Komponente: planner (prompt), scheduler · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_an_invalid_replan_is_retried_while_replan_budget_is_left`, `tests/unit/test_planner_prompt.py` Nadel „Do not repeat an open step to keep it“)
- Reproduktion: Replan nach blockierter Recherche S010 (9 offene Schritte):
  - Antwort 1 und 2 nach je 8192 Tokens abgeschnitten (`invalid JSON … line 1057`), je 5–8 min.
  - Antwort 3 mit doppeltem Schritt (S019/S009) und `requirements.txt`; keine Reparatur mehr übrig.
  - Folge: Job `failed` mit `PLANNER_INVALID_OUTPUT` bei `replan_count 0` von 2.
- Ursache:
  1. Meine Regel aus BUG-224 („repeat every open step“) ließ den Replanner jeden offenen Schritt vollständig wiederholen, obwohl `carry_over_open_steps` sie ohnehin übernimmt. Bei großen Plänen sprengt das das Ausgabelimit.
  2. Der Scheduler beendete den Job beim ersten ungültigen Replan, obwohl Budget übrig war.
- Fix:
  - Die Replanner-Regel lautet: Offene Schritte, die man weglässt, werden samt Abhängigkeiten übernommen; nur geänderte und neue Schritte angeben.
  - Ein ungültiger Replan zählt als ein Replan. Ist danach noch Budget übrig, bleibt der Job in `replanning`, und der Replan läuft erneut; sonst scheitert der Job wie bisher.
- Selbstprüfung: echter Schritt nach vorn, aber Korrektur eines eigenen früheren Fixes (BUG-224): Die Übernahme ist Aufgabe der Runtime, nicht des Modells. Der Retry nutzt das vorhandene Replan-Budget, ohne neues Budget.

### BUG-240 – Recherche gewichtet fremde Gists und Community-Repos als Primärquellen, die offizielle Doku niedrig; alle Aussagen mit Konfidenz < 0,25
- Phase: G1 auf rc33/rc34 (Job `2f72c0da…`, Recherche S001: 23 Aussagen aus 8 Quellen) · Komponente: research (sources, engine), config · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_research_sources.py::test_subject_decides_official_repositories_and_vendor_docs`, `…::test_query_terms_rescue_relevance_of_a_question_in_another_language`)
- Reproduktion: Quellen und Bewertung:
  - `gist.github.com/ReinforceZwei/…`, `github.com/drumandbytes/pve-metrics-exporter` und `github.com/hanzoskill/unraid`: je `official_repo` mit 0,9.
  - `pve.proxmox.com/wiki/Proxmox_VE_API`: `secondary` mit 0,4.
  - Relevanz 0,02–0,27.
  - Konfidenz aller Aussagen 0,07–0,23, auch für die offizielle Unraid-Doku.
- Ursache:
  1. `primary_domains` enthielt `github.com` ohne Pfad: jede GitHub-Seite, auch fremde Gists, galt als Primärquelle.
  2. Jedes Repository galt als `official_repo`, egal wem es gehört.
  3. Die eigene Domain des Produkts (`proxmox.com`) war für den Klassifizierer eine beliebige Website.
  4. Die Relevanz wurde nur gegen die Begriffe der langen deutschen Frage gemessen („recherchiere“, „aktuellen“, „offiziellen“ …); englische Doku enthält sie nicht.
- Fix:
  - Ein reiner Repo- oder Registry-Host ohne Pfad in `primary_domains` verleiht keinen Primärstatus mehr; im Beispiel ist `github.com` gestrichen.
  - `classify_source(subject=…)` nutzt die Begriffe der Frage samt Kompositum-Teilen: Ein Repository ist nur `official_repo`, wenn der Owner dazugehört, sonst „community repository“ (0,45). Die Produkt-Domain wird `official_docs` (0,85) bei Doku-Pfaden wie `/wiki`, `/docs`, `/pve-docs`, sonst `vendor`.
  - Die Relevanz ist das Maximum aus Frage- und Suchanfragen-Begriffen.
  - Die G1-Quellen werden jetzt richtig eingestuft: Gist und Community-Repos `secondary`, `github.com/unraid/api` `official_repo`, Proxmox-Wiki und `docs.unraid.net` `official_docs`.
- Selbstprüfung: echter Schritt nach vorn. Die Qualität der Recherche, die über BUG-231 beim Coder ankommt, hängt an dieser Bewertung; allgemeine Regeln, kein Produkt-Sonderfall.

### BUG-239 – Englische Suchanfragen zu einer deutschen Frage mit Bindestrich-Komposita werden alle als themenfremd verworfen
- Phase: G1 auf rc33 (Job `2f72c0da…`, Recherche S010) · Komponente: research (planner) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_research_planner_synth.py::test_english_queries_for_a_german_compound_question_are_kept`)
- Reproduktion: Frage „Welche Proxmox-REST-API-Endpunkte liefern GPU-Auslastung und Temperaturen…“. Das schnelle Modell liefert gültige englische Anfragen (187 Zeichen), trotzdem steht `NO_USABLE_QUERIES` im Verlauf. Der Fallback sucht mit „proxmox-rest-api-endpunkte liefern gpu-auslastung …“ und liest Podcasts, ein Verlagsshop-Angebot und Wiktionary („welche“): 5 Quellen, 0 Aussagen, Schritt fehlgeschlagen.
- Ursache: `key_terms` hält Komposita mit Bindestrich als ein Token („proxmox-rest-api-endpunkte“). `sanitize_queries` verlangt einen gemeinsamen Begriff mit der Frage; „Proxmox VE API GPU“ teilte keinen.
- Fix: `term_parts` ergänzt die Teile von Komposita (proxmox, rest, api, endpunkte, gpu, auslastung). Der Themenabgleich vergleicht diese Mengen; `key_terms` bleibt unverändert.
- Selbstprüfung: echter Schritt nach vorn, an der frühesten Stelle der Recherche (Anfragen). Allgemein für Komposita, nicht für Proxmox.

### BUG-238 – Gesperrte Suchmaschinen gelten als „keine Quellen“: Recherche-Schritte verbrauchen alle Versuche und scheitern
- Phase: G1 auf rc33 (Job `2f72c0da…`, S001) · Komponente: research (search), runtime (research_step), scheduler, infra (SearXNG) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_research_fetch_search.py::test_no_results_while_engines_are_suspended_is_an_outage`, `tests/integration/test_research_step.py::test_search_outage_is_a_step_outage_not_missing_sources`)
- Reproduktion: SearXNG antwortet für jede Anfrage mit 200 und 0 Ergebnissen, dazu `unresponsive_engines: brave (too many requests), duckduckgo (CAPTCHA), google cse (too many requests)`. Folge: S001 „0 Quellen gelesen“, `RESEARCH_NO_SOURCES`, Versuche 1 und 2 nach je 12 s verbraucht.
- Ursache:
  1. Nur drei Web-Engines aktiv, und alle drei waren gleichzeitig gesperrt (SearXNG sperrt 180 s nach 429 und 1 h nach CAPTCHA).
  2. Ein leeres Ergebnis bei gesperrten Engines galt als „es gibt nichts“, statt als Ausfall.
- Fix:
  - Das SearXNG-Template aktiviert zusätzlich `bing` und `yahoo`, live geprüft: 17 Treffer, während die drei anderen gesperrt waren. `qwant` lieferte „access denied“ und bleibt aus.
  - `SearxngSearchProvider.search` meldet `SEARCH_UNAVAILABLE`, wenn es keine Treffer gibt und Engines als nicht erreichbar gemeldet sind.
  - Scheitern alle Suchen einer Recherche so, meldet der Schritt `SEARCH_UNAVAILABLE`. Das gehört zu `STEP_OUTAGE_CODES`: Wiederholung mit Backoff, ohne Versuchsverbrauch.
- Selbstprüfung: echter Schritt nach vorn. Ein Ausfall wird als Ausfall behandelt wie bei Modell und Worker (BUG-197/227), dazu Redundanz an der Quelle. Kein G1-Sonderfall.

### BUG-237 – Planner umgeht die Regel „Code und Tests in einen Schritt“, indem er die Abhängigkeit weglässt
- Phase: G1 auf rc29 (Job `364a9c3c…`, Plan v1) · Komponente: planner (validation) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_tests_for_new_code_belong_in_the_same_step`, erweitert)
- Reproduktion: S003 schreibt `collector.py`, S006 (implement) nur `tests/test_collector.py` und Fixtures. Beide hängen nur von den Recherchen ab und laufen unabhängig voneinander; S006 kennt den Code nicht. In der vorigen Lieferung (Job `8e7c4b65…`) entstanden so hohle Tests, die an den Code angepasst waren (siehe BUG-234).
- Ursache: `_split_test_errors` prüfte nur direkte `depends_on`-Kanten. Ein reiner Testschritt ohne Kante zum Code-Schritt galt als eigenständig.
- Fix: Ein reiner Testschritt ist auch ohne Kante ein Fehler, wenn seine Testdateien den Code eines anderen neuen Schritts benennen: `test_X`, `X_test` oder `X.test`/`X.spec` zu `X`. Die Reparaturmeldung verlangt wie bisher das Zusammenlegen. Tests für Code abgeschlossener Schritte (Replan) bleiben erlaubt.
- Selbstprüfung: echter Schritt nach vorn. Eine bestehende Regel bekommt die fehlende Hälfte; es gibt keinen projektspezifischen Sonderfall.

### BUG-236 – Abhängige Schritte werden „dependency failed“ blockiert, obwohl für die Abhängigkeit ein Retry geplant ist
- Phase: G1 auf rc32 (Job `364a9c3c…`) · Komponente: scheduler · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_a_failed_dependency_with_a_scheduled_retry_does_not_block_its_dependents`)
- Reproduktion: 04:57:33 S003 Versuch 5 (`resume` nach dem rc32-Deployment, Checkpoint noch unter rc31 ohne BUG-233) → `WORKER_UNREACHABLE`, Retry in 60 s geplant. 04:57:34 S007 und S008 `blocked` mit „dependency failed“. 04:58:34 läuft S003 als Versuch 6 weiter.
- Ursache: Zwei Kriterien für „wird wiederholt“. Die Retry-Auswahl nimmt jeden fehlgeschlagenen Schritt mit `not_before`. Die Abhängigkeitsprüfung verlangte zusätzlich `attempt_count < max_attempts`. Ein Ausfall-Retry jenseits des Versuchsbudgets (Fortsetzung nach einem Checkpoint) wurde ausgeführt, galt für die Abhängigen aber als endgültig gescheitert.
- Fix: Die Abhängigkeitsprüfung nutzt dasselbe Kriterium wie die Retry-Auswahl (`not_before` gesetzt).
- Selbstprüfung: echter Schritt nach vorn. Eine Inkonsistenz zwischen zwei Codepfaden beseitigt; zusammen mit BUG-233 entsteht der Zustand nicht mehr.

### BUG-235 – Der Coder fordert nur 6144 Ausgabe-Tokens an, obwohl Profil und Kontextbudget 8192 vorsehen
- Phase: G1 auf rc31 (Job `364a9c3c…`, S003 Turn 1) · Komponente: coder (handler) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_coder_handler.py::test_implement_step_happy_path_commits_and_pushes`, erweitert)
- Reproduktion: `model.invocation.finished` mit `completion: 6144`, `status: invalid` (abgeschnitten), etwa 4 min Modellzeit verloren. Der Kontextbericht desselben Turns nennt `max_output: 8192`. `/etc/hermclaw/models.yaml` setzt `coder-main.max_output_tokens: 8192` (Inventory-Override aus BUG-223).
- Ursache: Zwei Quellen für denselben Wert. Der Kontext-Builder reserviert `max(Profil, policies.coder.max_output_tokens)` = 8192. Der Coder-Loop fordert `CoderSettings.max_output_tokens` an, dessen Klassenvorgabe 6144 der Handler nie überschrieb. Die Anhebung auf 8192 für BUG-223 kam deshalb nie beim Modell an.
- Fix: Der Handler setzt `CoderSettings.max_output_tokens` aus der Kontext-Builder-Konfiguration. Angefordert wird genau, was das Budget reserviert; eine einzige Quelle.
- Selbstprüfung: echter Schritt nach vorn. Ein Konfigurationswert wirkte nicht; der Fix beseitigt die doppelte Quelle, statt eine Zahl zu erhöhen.

### BUG-234 – Frontend und Backend aus verschiedenen Schritten passen nicht zusammen; Tests prüfen nichts
- Phase: Qualitätsprüfung der G1-Lieferung (rc26–rc28) · Komponente: planner (prompt), review (prompt) · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_prompt.py` Nadel „need one written interface“, `tests/unit/test_review_prompt.py::test_system_prompt_rules` Nadeln „Hollow tests“, „real network connections“)
- Reproduktion: Job `8e7c4b65…`, Lieferung unter `/var/lib/hermclaw/workspaces/8e7c4b65-…/hermclaw-test`.
  - Schnittstelle: `static/app.js` liest `metrics.ram`, `inst.metrics.cpu` und `s.name`; `collector.py` liefert `metrics.mem.usage`, flaches `inst.cpu` und `id`. Folge: Jede VM zeigt „n/v“, RAM erscheint nie.
  - Hohle Tests: `tests/test_collector.py` patcht `urllib.request.urlopen`, während der Code anders auflöst, oder patcht `collector.urlopen`, das der Code nie aufruft. Ein Test öffnet eine echte Verbindung (10 s Timeout) und besteht trotzdem, weil er nur Server zählt. Der Storage-Parser liefert immer `[]`, sein Test besteht dennoch.
- Ursache: Der Planner verteilte Anbieter (API) und Verbraucher (Seite) auf Schritte ohne gemeinsame, geschriebene Schnittstelle; jeder Coder riet das JSON-Format. Der Reviewer hatte keine Regel für hohle Tests (Mock am falschen Ort, Zusicherungen, die auch bei Fehlern bestehen, echte Netzwerkzugriffe).
- Fix:
  - Planner-Regel: Komponenten aus verschiedenen Schritten, die miteinander sprechen, bekommen eine geschriebene Schnittstelle (z. B. `docs/api.md` mit Endpunkten und Beispiel-JSON). Sie entsteht im Anbieter-Schritt; der Verbraucher-Schritt hängt davon ab und nennt sie in Ziel und `repo_hints`.
  - Reviewer-Regel: hohle Tests sind major.
- Selbstprüfung: echter Schritt nach vorn. Die Ursache sitzt in der Planung, an der frühesten Stelle; Regeln ohne projektspezifische Sonderfälle.

### BUG-233 – Ein Checkpoint (Deployment, Pause, Verdrängung) kostet einen Versuch: der fortgesetzte Versuch zählt neu
- Phase: G1 auf rc30/rc31 · Komponente: scheduler · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_model_outage_during_a_step_does_not_use_up_its_attempts`, erweitert um `checkpoint`)
- Reproduktion: Job `364a9c3c-78ab-453e-b984-2936ca887c02`, S003:
  - Versuch 1 `WORKER_UNREACHABLE` (rc30-Deployment, vor BUG-232);
  - Versuch 2 `retry`, beim rc31-Deployment `checkpointed`;
  - Versuch 3 `resume` `WORKER_UNREACHABLE`;
  - Versuch 4 `resume`.

  Ergebnis: `attempt_count 4 / max_attempts 4`, obwohl nur Versuch 2/4 Arbeit enthielt. Für Korrekturen bleibt nichts.
- Ursache: Die Fortsetzung nach einem Checkpoint ist ein neuer Versuchsdatensatz mit eigener Nummer. Sie zählte gegen `max_attempts`, obwohl sie die Arbeit desselben Versuchs vom Checkpoint aus weiterführt. Jedes Deployment, jede Pause und jede Verdrängung durch einen Videojob verbrauchte so einen der drei Versuche, die sich der Schritt mit seinen Korrekturen teilt.
- Fix: Ein `checkpointed`-Ergebnis erhöht `max_attempts` um eins, wie ein Ausfall (BUG-232). Das Turn-Budget im Checkpoint begrenzt die Arbeit weiterhin.
- Selbstprüfung: echter Schritt nach vorn. Die Regel aus BUG-197 („Versuche ohne eigene Arbeit sind frei“) gilt jetzt auch für Unterbrechungen; kein neuer Mechanismus.

### BUG-232 – Ein früher Modell- oder Worker-Ausfall verbraucht trotz BUG-197/227 einen Versuch des Schritts
- Phase: G1 auf rc28/rc29 · Komponente: scheduler · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_model_outage_during_a_step_does_not_use_up_its_attempts`, erweitert)
- Reproduktion: Job `8e7c4b65…`, S003. Versuch 2 war `WORKER_UNREACHABLE` (Deployment), danach blockierte der Schritt mit „correction budget exhausted (1/2)“ bei 3/3 Versuchen. Im Folgelauf `364a9c3c…` haben S003–S006 nach dem rc30-Deployment je einen `WORKER_UNREACHABLE`-Versuch und weiterhin `max_attempts = 3`.
- Ursache: Nach einem Ausfall setzte der Scheduler `max_attempts = max(max_attempts, attempt_count + 1)`. Das garantiert nur einen weiteren Versuch, gibt den verbrauchten aber nicht zurück. Frei war ein Ausfall nur beim letzten Versuch.
- Fix: `max_attempts += 1` je Ausfallversuch. Begrenzt bleibt das durch `model_outage_max_wait_seconds`. Der Regressionstest hat erst einen Ausfall, dann zwei Fehlschläge, dann Erfolg: mit dem Fix bestanden, ohne ihn gescheitert.
- Selbstprüfung: echter Schritt nach vorn. Die Absicht von BUG-197 („das Budget wächst um eins“) war falsch umgesetzt; der Fix ändert eine Zeile, ohne neuen Mechanismus.

### BUG-231 – Recherche-Ergebnisse erreichen den Coder nur verstümmelt: drei Kürzungen hintereinander schneiden die Fakten ab
- Phase: Qualitätsprüfung der G1-Lieferung (rc26–rc28) · Komponente: research, context_builder · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_context_builder_sections.py::test_research_findings_reach_the_coder_with_their_facts`)
- Reproduktion: Job `8e7c4b65-c9ff-46dd-a2a6-6898a2357bb6`. Die Recherchen S001/S002 fanden das Header-Format `PVEAPIToken=USER@REALM!TOKENID=UUID`, den Header `x-api-key` und die Root-Queries `info`/`array`. Trotzdem schrieb der Coder `Authorization: Bearer`, einen Token-Header ohne Secret und erfundene GraphQL-Felder. Laut Kontextbericht hatte CONSTRAINTS mit beiden Recherchen nur 996 Zeichen.
- Ursache: Drei Kürzungen hintereinander:
  1. `worker_summary` (1400 Zeichen) begann mit der vollständigen Frage (~350 Zeichen) und der Synthese, sodass die belegten Aussagen nie hineinpassten.
  2. `research_findings` kappte bei 1500 Zeichen.
  3. `render_constraints` kürzte jedes Element auf 500 Zeichen, in einem Abschnitt mit 3 % Budget.

  Beim Coder kam nur die wiederholte Frage an. Er schrieb die API aus dem Gedächtnis, und Fixtures in derselben falschen Form bestätigten den Fehler.
- Fix: `format_for_worker` stellt die belegten Aussagen voran, kürzt die Frage auf 200 Zeichen und hängt Synthese und Quellen danach an. Eine Längengrenze trifft damit nicht mehr die Fakten. `worker_summary` und Findings bekommen 2400 Zeichen, Constraint-Elemente bis 2400. CONSTRAINTS erhält 8 % statt 3 %, genommen von LATEST FAILURE (12 → 8 %) und SHORT STEP HISTORY (6 → 5 %). Ungenutztes Budget geht wie bisher an die elastischen Abschnitte. Nachtrag:
  - Der Heavy Reviewer zeigt Constraints mit bis zu 2400 statt 800 Zeichen (`ReviewSettings.constraint_chars`).
  - Zwei neue Prüfregeln: Externe APIs, Header und Felder müssen den belegten Recherche-Aussagen entsprechen; Fixtures in der Form des Codes statt der dokumentierten Antworten sind major. Gelesene, aber nie angewandte Konfigurationsoptionen (z. B. `verify_ssl`) sind ebenfalls major.
  - Test: `tests/unit/test_review_prompt.py::test_research_findings_reach_the_reviewer_in_full`.
- Selbstprüfung: echter Schritt nach vorn, an der frühesten vermeidbaren Stelle. Die korrekte Recherche war vorhanden und wurde auf dem Weg zum Coder verworfen; kein Sonderfall für Proxmox oder Unraid.

### BUG-230 – Arbeit, die im letzten Turn fertig wird, geht verloren: Turn-Budget erschöpft, Schritt blockiert, Job scheitert
- Phase: Großauftrag G1 auf 0.1.0rc28 (S003, Plan v3, Versuch 3) · Komponente: coder (handler) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_coder_handler.py::test_work_finished_in_the_last_turn_is_verified_not_thrown_away`, `…::test_failed_verification_after_the_turn_budget_corrects_with_both_notes`, `…::test_turn_budget_without_changes_skips_the_verifier`)
- Reproduktion: Job `8e7c4b65-c9ff-46dd-a2a6-6898a2357bb6`. In Turn 20 von 20 meldet `python -m pytest -q tests/test_collector.py` „17 passed“. Danach `attempt.finished` mit `TURN_BUDGET_EXHAUSTED`, „correction budget exhausted (1/2)“, S003 `blocked`, S004/S005 `dependency failed`, Job `failed` mit `REPLAN_LIMIT` nach 98,3 min (Bericht `20261018T021717Z-jobs.md`).
- Ursache: Nach einem erschöpften Turn-Budget ging der Handler direkt in die Korrektur bzw. bei erschöpften Versuchen in den Replan. Der Verifier lief nur nach `complete_step`. Ob ein Schritt fertig war, entschied damit die Selbstauskunft des Coders und nicht der Verifier: fertige, geprüfte Arbeit wurde verworfen, weil der Turn für die Meldung fehlte. Die Meldung „(1/2)“ verschwieg zudem, dass das Versuchsbudget (nicht das Korrekturbudget) erschöpft war.
- Fix: Endet ein Versuch am Turn-Budget und hat er Dateien geändert, laufen Verifier und Review wie nach `complete_step`. Bestehen sie, wird der Schritt abgeschlossen und committet. Scheitern sie, folgt die Korrektur mit Turn-Budget-Hinweis und Verifier-Befund zusammen. Ohne Änderungen bleibt es bei der bisherigen Korrektur. Die Blockier-Meldung nennt Korrekturen und Versuche getrennt.
- Selbstprüfung: echter Schritt nach vorn. Die Entscheidung „fertig?“ liegt jetzt beim objektiven Prüfer, nicht beim Turn-Zähler. Das Turn-Budget wird nicht erhöht und nichts kompensiert; das spätest mögliche Scheitern eines Jobs wird vermieden.

### BUG-229 – Wiederholungssperre lehnt Lesezugriffe ab, deren Inhalt der Kontext nur teilweise zeigt; Coder dreht sich, Schritt stagniert
- Phase: Großauftrag G1 auf 0.1.0rc27 (S003, Plan v2) · Komponente: context_builder, tools · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_context_builder_relevance.py::test_reads_are_shown_as_read_not_as_a_short_head`, `tests/integration/test_tools_engine_read_write.py::test_a_read_the_context_cannot_show_in_full_may_be_repeated`)
- Reproduktion: Job `8e7c4b65-c9ff-46dd-a2a6-6898a2357bb6`, S003 v2: etwa 9 von 19 Turns „read refused: … its content is shown above“. Danach `blocked` mit `replan_reason: stagnation`, Verlauf nur aus `read_file`/`read_range`. `collector.py` hat 624 Zeilen (21 453 Bytes).
- Ursache: Der Kontext-Builder zeigte gelesene Dateien nur als Kopf (160 Zeilen, Tests 120) und merkte sich nur den Pfad, nicht den gelesenen Bereich. Die Sperre aus BUG-179 lehnte die identische Wiederholung trotzdem mit „its content is shown above“ ab. Bei großen Dateien und bei Bereichen außerhalb des Kopfes stimmte das nicht.
- Fix: Der Builder übernimmt jeden Lesezugriff des Versuchs als (Pfad, Start, Ende): die ganze Datei bzw. den genauen Bereich, höchstens `max_snippet_lines` (400). Die Engine sperrt eine Wiederholung nur noch, wenn der Inhalt vollständig sichtbar war: nicht abgeschnitten und höchstens `READ_VISIBLE_LINES` (400) Zeilen. Größere Lesezugriffe dürfen wiederholt werden.
- Selbstprüfung: echter Schritt nach vorn. Behoben ist die Ursache: Die Sperre behauptete etwas Falsches, und der Kontext zeigt jetzt, was gelesen wurde. Die Sperre bleibt für kleine Dateien wirksam (BUG-179); es gibt keinen neuen Sonderfall.

### BUG-228 – Coder schreibt frisch angelegte Dateien identisch ein zweites Mal; ihm fehlt eine Fortschrittsliste
- Phase: Großauftrag G1 auf 0.1.0rc26 (S003) · Komponente: context_builder · Severity: P3 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_context_builder_sections.py::test_scope_marks_written_new_files_and_lists_the_missing_ones`)
- Reproduktion: Job `8e7c4b65-c9ff-46dd-a2a6-6898a2357bb6`, S003 (13 neue Dateien). Turn 2 „created config.toml“, Turn 3 dieselbe Datei erneut („already has this content; nothing written“). Turn 4/5 ebenso für `example_config.toml`. 2 von 14 Turns ohne Wirkung, bei einem 20-Turn-Budget.
- Ursache: Der SCOPE-Abschnitt listete unter „May create“ nur die erlaubten Pfade, ohne Angabe, welche schon geschrieben waren. Bei vielen neuen Dateien verlor das Modell den Überblick über den Rest.
- Fix: Neu angelegte Dateien dieses Versuchs werden im SCOPE-Abschnitt als „(written)“ markiert. Sobald eine geschrieben ist, listet „Not written yet: …“ die noch fehlenden konkreten Pfade auf (Globs nicht). Die Daten kommen aus den ohnehin ermittelten geänderten Dateien; kein zusätzlicher Werkzeugaufruf.
- Selbstprüfung: echter Schritt nach vorn, eine kleine Ergänzung des Kontexts. Zusammen mit BUG-226 (kleinere Schritte) verringert sie verlorene Turns.

### BUG-227 – Nach einem Deployment scheitert der fortgesetzte Schritt, weil der Sandbox-Worker noch nicht bereit ist
- Phase: Großauftrag G1, Deployments von rc25 und rc27 bei laufendem Job · Komponente: scheduler · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_model_outage_during_a_step_does_not_use_up_its_attempts`, erweitert um `worker_down`)
- Reproduktion: Job `8e7c4b65-c9ff-46dd-a2a6-6898a2357bb6`, S003 nach 14 Turns. Deployment von rc27 um 03:04: `checkpointed` (paused) um 03:04:33, fortgesetzt um 03:05:19 (`resume`). Sofort `HANDLER_CRASHED: WorkerUnreachable: no ready execution worker with capability sandbox`, Schritt `failed`, Retry nach 120 s. Bei rc25 (Job `b3441414…`, S003) ebenso: checkpointed → failed.
- Ursache: Der Scheduler startet nach dem Deployment schneller als sich der Ausführungs-Worker auf .222 wieder als bereit meldet. Der Fehler kam als generischer Handler-Absturz an und verbrauchte einen der drei Versuche des Schritts, die er sich mit den Korrekturen teilt. Bei einem Schritt kurz vor dem Ende kann das den letzten Versuch kosten und einen Replan auslösen.
- Fix: `WORKER_UNREACHABLE` gehört zu `STEP_OUTAGE_CODES`. Ein Ausfall, der tief aus einem Handler geworfen wird, behält seinen Code, statt `HANDLER_CRASHED` zu werden. Wie bei Modellausfällen (BUG-197) zählt der Versuch nicht gegen das Budget. Wiederholt wird mit Backoff bis `model_outage_max_wait_seconds`; der Checkpoint bleibt erhalten (`resume`).
- Selbstprüfung: echter Schritt nach vorn. Die vorhandene Ausfallbehandlung wird auf den zweiten Infrastrukturpfad (Worker statt Modell) ausgedehnt; kein neuer Mechanismus.

### BUG-226 – Planner legt einen ganzen Großauftrag in einen Schritt (13 neue Dateien), der am Turn-Budget scheitern muss
- Phase: Großauftrag G1 auf 0.1.0rc26 · Komponente: planner/validation, planner/prompt · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_a_step_creating_more_files_than_one_attempt_can_write_is_split`, Prompt-Regel in `tests/unit/test_planner_prompt.py`)
- Reproduktion: Job `8e7c4b65-c9ff-46dd-a2a6-6898a2357bb6`, Plan v1. Der erste Entwurf hatte getrennte Schritte für Collector, Frontend, Tests und Doku. Die Regel „Code und Tests in einen Schritt“ (BUG-209) meldete: „merge S005 into S003“. Die Reparatur legte daraufhin alles in S003 „Implement Collector, Frontend, Tests and Documentation“: 13 neue Dateien (collector.py, Konfigs, drei Frontend-Dateien, zwei PowerShell-Skripte, Tests, zwei Fixtures, README). Ein Coder-Schritt hat 20 Turns: mindestens ein Schreib-Turn pro Datei (große in Teilen, BUG-223) plus Testläufe und Korrekturen. Das scheitert spät am Turn-Budget.
- Ursache: Es gab keine Obergrenze für den Umfang eines Schritts, nur für die Zahl der Schritte. Die Reparaturmeldung der Test-Regel sagte nicht, dass nur die Tests zusammengelegt werden sollen.
- Fix:
  - Semantikregel: Ein Schritt legt höchstens `max_new_files_per_step` neue Dateien an. Der Wert wird aus `policies.coder.max_turns` abgeleitet (2 von 5 Turns, 20 → 8); X4 hatte 8 und brauchte schon mehrere Versuche. Bei Überschreitung soll der Plan nach Komponenten geteilt werden, jeder Teil mit den Tests seines Codes.
  - Planner-Prompt nennt die Grenze.
  - Die Meldung der Test-Regel ergänzt: „Merge only these tests: other components stay in their own steps“.
- Selbstprüfung: Nebenwirkung des BUG-209-Fixes. Sie wird aber nicht durch Lockern der Test-Regel kompensiert, sondern durch die fehlende Gegenkraft (Schrittgröße) geschlossen. Beide Regeln zusammen ergeben „pro Komponente ein Schritt mit ihren Tests“.

### BUG-225 – Redaction maskiert Code und Platzhalter; der Coder kopiert den Marker und jeder Schreibversuch wird abgelehnt
- Phase: Großauftrag G1 auf 0.1.0rc25 (S011) · Komponente: core/redaction · Severity: P1 · blockierend: ja für Aufträge mit Zugangsdaten (behoben) · Status: **behoben** (`tests/unit/test_config_and_redaction.py::test_code_reading_secrets_and_placeholders_stay_readable`)
- Reproduktion: Job `b3441414-9422-4887-8cb1-b0f7c43e5ced`, S011 Turn 3 (02:09). Nach zwei abgeschnittenen Antworten (BUG-223) kam eine gültige Antwort mit `collector.py` (5751 Tokens, 234 s). `write_file` lehnte sie ab: „the content contains the redaction marker ***REDACTED***“. Der vorige Schritt hatte `config.example.toml` mit `api_token_secret = "your-secret-here"` und `api_key = "your-unraid-api-key-here"` angelegt; im Kontext stand dort `***REDACTED***`.
- Ursache: Die `key = value`-Regel des Redactors maskierte jeden Wert ab 4 Zeichen hinter einem Schlüssel wie token/secret/password/api_key, auch:
  - offensichtliche Platzhalter;
  - Code, der ein Geheimnis liest: `api_key = config["unraid"]["api_key"]` wurde zu `api_key = ***REDACTED***"unraid"]["api_key"]`, `token = self.token` zu `token = ***REDACTED***`.

  Der Coder sah kaputten Code und Marker, kopierte sie in neue Dateien, und die Platzhalter-Sperre verwarf die ganze Antwort. Das betrifft jeden Auftrag, der mit Zugangsdaten umgeht (APIs, Datenbanken, Monitoring).
- Fix: `is_benign_value` lässt zwei Fälle stehen:
  - offensichtliche Platzhalter: `<…>`, `${…}`, `your-…`, `…-here`, `example`, `changeme`, `placeholder`, `dummy`, nur `x`/`*`;
  - unquotierte, kurze Code-Referenzen: Bezeichner mit `.`, `(`, `[` usw.

  Gequotete Literale, `PASSWORD=hunter2realpw`, Token-Formate (`ghp_`, `sk-`, `glpat-`), JWTs und Schlüssel bleiben maskiert. Der Secret-Scan des Verifiers nutzt die Muster direkt mit eigener Literal-/Platzhalter-Logik und ist unverändert.
- Selbstprüfung: echter Schritt nach vorn. Die Redaction war auf Logs ausgelegt; dass ihr Ergebnis dem Coder als Code gezeigt wird, war nicht berücksichtigt.

### BUG-224 – Replan verwirft offene Schritte stillschweigend; der Job würde mit einem Bruchteil des Auftrags „succeeded“ enden
- Phase: Großauftrag G1 auf 0.1.0rc25 · Komponente: planner/replanner · Severity: P0 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_open_steps_without_a_replacement_are_carried_over_with_remapped_dependencies`, `tests/integration/test_planner_replan.py::test_open_steps_the_replan_leaves_out_are_carried_over`, Prompt-Regel in `tests/unit/test_planner_prompt.py`; Simulation mit den echten Plänen v1/v2 von G1: S004–S008 übernommen, Abhängigkeiten von S003 auf S011 umgehängt)
- Reproduktion: Job `b3441414-9422-4887-8cb1-b0f7c43e5ced`. S003 (Collector) scheiterte an BUG-223, Replan `step_failed` um 01:55. Der Replanner lieferte einen einzigen Schritt S011 „Implement Collector Core and Config (Fallback)“ (608 Completion-Tokens). Die Runtime brach S004 bis S008 (Frontend, PowerShell-Skripte, Tests, README, Testlauf) als „superseded by plan version 2“ ab. Plan v2 enthält nur noch die Recherchen und S011. Nach S011 wäre der Job ohne Widget, Skripte, Tests und README erfolgreich beendet worden.
- Ursache: Ein Replan ersetzt die gesamte restliche Arbeit. Die offenen Schritte standen zwar im Prompt, aber nichts stellte sicher, dass sie im neuen Plan wieder auftauchen. Ein Modell, das sich auf den gescheiterten Schritt konzentriert, lässt den Rest weg, und die Runtime übernahm das ungeprüft. Das trifft genau die langen Aufträge: Jeder Replan kann still Umfang verlieren.
- Fix: `carry_over_open_steps` übernimmt deterministisch jeden offenen, nicht finalen Schritt des alten Plans (außer dem gescheiterten), den kein neuer Schritt abdeckt. Abgedeckt heißt:
  - gleiche ID;
  - eine gemeinsame neu angelegte Datei;
  - mindestens 60 % seiner Inhaltswörter;
  - bei review/verify/test: ein neuer Prüfschritt derselben Art.

  Abhängigkeiten auf weggefallene Schritte zeigen auf die neuen Schritte des Modells. Jede Übernahme steht als Notiz im `replan.created`-Event. Die Replanner-Regel sagt außerdem, dass offene Schritte zum Auftrag gehören und ausgelassene übernommen werden.
- Selbstprüfung: echter Schritt nach vorn. Bisher gab es keine Garantie, dass ein Replan den Auftragsumfang erhält. Die Übernahme ist deterministisch und beruht nicht auf einem weiteren Prompt-Appell.

### BUG-223 – Coder scheitert an großen Dateien: Antwort am Output-Limit abgeschnitten, Reparaturen wiederholen es
- Phase: Großauftrag G1 auf 0.1.0rc24 (S003 Collector) · Komponente: models/gateway, coder/loop, tools · Severity: P1 · blockierend: ja für große Module (behoben) · Status: **behoben** (`tests/unit/test_models_gateway.py::test_structured_cut_off_answers_get_a_concise_repair_then_raise`, `tests/integration/test_coder_loop.py::test_cut_off_answer_asks_for_writing_in_parts_and_gets_one_extra_turn`, `tests/integration/test_tools_engine_read_write.py::test_write_file_append_writes_large_files_in_parts`, Kontrakt-Regel in `tests/unit/test_planner_prompt.py`)
- Reproduktion: Job `b3441414-9422-4887-8cb1-b0f7c43e5ced`, S003. Nach README und `config.example.toml` wollte der Coder `collector.py` in einem `write_file` schreiben. Vier Aufrufe in Folge endeten mit `finish_reason=length` bei 6144/6144 Tokens, Status `invalid`, je 240–262 s (01:11–01:28). Zwei davon waren Reparaturen mit identischem Request-Hash.
- Ursache:
  - Der Gateway behandelte ein abgeschnittenes JSON wie einen Formatfehler und reparierte mit demselben Limit, wodurch die Antwort erneut abgeschnitten wurde.
  - Der Coder-Loop meldete danach nur „answer with exactly one tool call“; das Modell erfuhr nie, dass es zu viel geschrieben hatte.
  - Es gab keinen Weg, eine Datei in Teilen zu schreiben.
  - Ergebnis: Pro Turn bis zu 12 Minuten ohne Fortschritt, nach zwei Turns `model_failed`. Je größer das Programm, desto sicherer passiert das; genau die langen Aufträge scheitern so spät.
- Fix:
  - Gateway: `finish_reason=length` wird erkannt. Höchstens `truncation_repairs` Reparaturen (Standard 1) mit dem ausdrücklichen Hinweis „abgeschnitten, deutlich kürzer antworten“, danach `ModelOutputTruncated` (`MODEL_OUTPUT_TRUNCATED`).
  - Coder: keine Reparatur (`truncation_repairs=0`). Stattdessen sagt der nächste Turn konkret, dass die Antwort am Limit abgeschnitten wurde und große Dateien in Teilen geschrieben werden. Nach einem Abbruch gibt es einen Turn zusätzlich.
  - `write_file` hat `append`, um eine Datei Teil für Teil zu schreiben. Scope, Platzhalter-Prüfung und Events gelten wie bisher.
  - Systemkontrakt des Coders: höchstens etwa 250 Zeilen pro Antwort, größere Dateien in Teilen oder als Module.
  - Nachtrag (live S011 auf rc25): Das Modell hielt die 250-Zeilen-Regel im ersten Turn nicht ein. Der Hinweis-Turn half erst im Zusatz-Turn, dort mit einer vollständigen Antwort von 5751 Tokens. `coder-main.max_output_tokens` steigt deshalb von 6144 auf 8192 (etwa 340 s bei rund 24 Tokens/s, Timeout 900 s, über 20k Tokens Eingabebudget bleiben). Ein vollständiges Modul passt damit meist in eine Antwort; größere Dateien laufen über `append`.
- Selbstprüfung: echter Schritt nach vorn. Bisher gab es keine Behandlung des Output-Limits. Das Limit wird bewusst nicht einfach erhöht: Das verschiebt das Problem nur zur nächstgrößeren Datei und verlängert jeden Fehlversuch.

### BUG-222 – Recherche verwirft fast alle Claims, wenn die Frage nicht in der Sprache der Quelle gestellt ist
- Phase: Großauftrag G1 auf 0.1.0rc24 · Komponente: research/claims · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_research_claims.py::test_claims_are_written_in_the_language_of_the_source`; Live-Nachweis mit dem deployten Modell und der echten Seite: vorher 2/6 Claims grounded, danach 11/11 in zwei Läufen)
- Reproduktion: Job `b3441414-9422-4887-8cb1-b0f7c43e5ced`, S002 „Recherchiere die aktuelle Unraid GraphQL-API-Dokumentation …“ endete `partial`: „Für die Authentifizierung liegen keine spezifischen Informationen zu API-Keys vor“. Die gelesene offizielle Seite `docs.unraid.net/API/how-to-use-the-api/` beschreibt den Header `x-api-key`. Von ihr kam genau ein Claim (das Fehlerformat).
- Ursache: Bei einer deutschen Frage schreibt das Extraktionsmodell die Claims auf Deutsch. Die Grounding-Prüfung verlangt, dass mindestens 60 % der Inhaltswörter wörtlich in der (englischen) Quelle stehen. Übersetzte Claims fallen deshalb durch; übrig bleiben nur Claims, die fast ganz aus Code bestehen. Das betrifft jede Recherche mit einer anderen Sprache als die der Quellen, also praktisch jeden deutschen Auftrag.
- Fix: Die Extraktion nennt am Ende der Nachricht die erkannte Sprache der Quelle (`guess_language`: Englisch/Deutsch über Funktionswörter, sonst „die Sprache des Quelltexts“) und verlangt Claims in dieser Sprache, mit dem Wortlaut der Quelle. Die Synthese antwortet weiterhin in der Sprache der Frage. Eine allgemeine Regel im System-Prompt allein wurde vom Modell ignoriert (live gemessen: weiterhin 3/6 verworfen).
- Selbstprüfung: echter Schritt nach vorn. Die Grounding-Prüfung bleibt unverändert streng; die Ursache war die Sprache der Eingabe, nicht die Prüfung.

### BUG-221 – Doppelte Recherche-Schritte, wenn research_needed eine geplante Recherche umformuliert
- Phase: Großauftrag G1 auf 0.1.0rc24 · Komponente: planner/validation · Severity: P3 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_research_request_rephrasing_an_existing_research_step_is_covered`)
- Reproduktion: Job `b3441414-9422-4887-8cb1-b0f7c43e5ced`, Plan v1. Der Planner schrieb eigene Recherche-Schritte (S001 „Recherchiere die aktuelle Proxmox VE REST-API-Dokumentation …“, S002 Unraid) und stellte dieselben Themen anders formuliert unter `research_needed`. Die Anreicherung erzeugte daraus S009/S010. Vier Recherchen statt zwei, alle parallel auf dem Research-Worker; zwei Schritte des 20-Schritte-Budgets verbraucht.
- Ursache: `find_research_step` erkannte eine Abdeckung nur über Teilstring-Gleichheit der normalisierten Texte. Eine Umformulierung desselben Themas galt als offene Frage.
- Fix: Eine Frage gilt auch dann als abgedeckt, wenn ein Recherche-Schritt mindestens 60 % ihrer Inhaltswörter nennt. Gezählt werden mindestens 4 Inhaltswörter, ohne deutsche/englische Funktionswörter; gewählt wird der beste Treffer. Beim G1-Plan: 0,85/0,81 für das richtige Thema gegenüber 0,46/0,44 für das andere System.
- Selbstprüfung: echter Schritt nach vorn. Die Deduplizierung war bisher nur für wörtliche Wiederholungen gebaut. Das ist keine Kompensation eines früheren Fixes.

### BUG-220 – Merge Request fehlt, weil GitLab den Branch nach 30 s noch nicht kannte
- Phase: Erweiterte Batterie auf 0.1.0rc23 (X5) · Komponente: gitops/gitlab · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_gitops_units.py`, Wartebudget ≥ 180 s)
- Reproduktion: Job `3992cff9-0117-41f6-a970-2f90ed9bdb98`. Push um 00:51:50. Die MR-Anlage wurde nach BUG-202 fünfmal wiederholt (30 s) und scheiterte um 00:52:23 mit `HTTP 400 {'source_branch': ['does not exist']}`. Der Job endete `succeeded` ohne MR.
- Ursache: GitLab registriert gepushte Branches asynchron über Sidekiq. Bei Last auf .226 dauerte das länger als das Wartefenster von BUG-202.
- Fix: Wartefenster mit wachsenden Abständen auf rund 3,5 Minuten verlängert (1, 2, 4, 8, 15, 30, 30, 30, 45, 45 s). Jeder Versuch ist ein billiger POST, der beim ersten Erfolg endet. Die Finalisierung hat kein eigenes Zeitlimit, das kollidieren könnte.
- Selbstprüfung: Fortsetzung von BUG-202, dieselbe Ursache mit zu knapper Grenze. Ich erhöhe die Grenze und baue keinen neuen Mechanismus, weil das Signal (400 mit „does not exist“) eindeutig bleibt.

### BUG-219 – Programme werden nie wie vom Nutzer ausgeführt; das Review sieht keine echte Ausgabe
- Phase: Qualitätsprüfung der gelieferten Dateien, Batterie auf 0.1.0rc23 (X4) · Komponente: planner/review · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_review_prompt.py::test_passing_command_checks_show_their_output`, Planner-Regel in `tests/unit/test_planner_prompt.py`; Live-Nachweis: Review mit der echten X4-Ausgabe → major „prints the function object instead of the plugin name“, Fix `module.name()`)
- Reproduktion: Job `8ed97532-ab1b-4ee3-ae7a-310313c99835` (Plugin-System, `succeeded`, PASS). `python plugin_tool.py --list` gibt `- uppercase: <function name at 0x7fa1…>` aus, weil `module.name` nicht aufgerufen wird. 8 Unit-Tests grün, Verifier und Review pass. Außerdem sucht das Tool `plugins/` relativ zum Arbeitsverzeichnis.
- Ursache:
  - Kein Plan der bisherigen Batterien enthielt ein `command`-Kriterium. Geprüft wurden nur Unit-Tests, die die CLI-Ausgabe nicht abdecken.
  - Das Review sah bei bestandenen Checks nur die Statuszeile, nie die Ausgabe des Programms.
- Fix:
  - Planner-Regel: Liefert ein Schritt ein ausführbares Programm, bekommt er ein `command`-Kriterium, das es wie ein Nutzer mit den Beispieldaten ausführt. `stdout_pattern` nur für einen Wert, der vorkommen muss, nie für exakte Formatierung; keine Server.
  - Beispiel im Evidence-Katalog.
  - Review: Bestandene `command`-Checks zeigen ihre Ausgabe („judge it against the JOB GOAL“); falsche Werte und Objektdarstellungen gelten als Defekt.
- Selbstprüfung: echter Schritt nach vorn. Schließt die Lücke zwischen „Tests grün“ und „Programm funktioniert“ für jede Art von Programm.

### BUG-218 – Neue Programme, Optionen und Umgebungsvariablen bleiben undokumentiert
- Phase: Qualitätsprüfung der gelieferten Dateien, Batterie auf 0.1.0rc23 (X1, X3) · Komponente: planner · Severity: P3 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_prompt.py::test_research_is_for_external_facts_not_the_repository_itself`, prüft die Regel im Prompt)
- Reproduktion: Job `47d60d3e-…` (X1) liefert ein neues CLI mit `--format`, Job `1dbe5ea5-…` (X3) führt `INVENTORY_DB` ein. Beide Repositorys haben eine README, keine wurde ergänzt. Das Ergebnis ist ohne Lesen des Codes nicht bedienbar.
- Ursache: Der Planner plante nur, was der Auftrag wörtlich nennt. Dokumentation gehörte nicht zum Begriff „fertig“.
- Fix: Planner-Regel „Deliver complete work“. Führt der Auftrag ein Programm, einen Befehl, Optionen, Konfiguration oder Umgebungsvariablen ein und hat das Repository eine README, dokumentiert der umsetzende Schritt deren Nutzung dort (README in den repo_hints). Das Review meldet fehlende Doku zusätzlich als minor (BUG-216).
- Selbstprüfung: echter Schritt nach vorn. Eine neue Qualitätsdimension (Vollständigkeit), kein Symptom eines früheren Fixes.

### BUG-217 – Review meldet Syntaxfehler in einer Datei, die der Verifier fehlerfrei geparst hat
- Phase: Erweiterte Batterie auf 0.1.0rc23 (X4) · Komponente: review · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_review_severity.py::test_syntax_claims_against_a_parsed_file_are_downgraded`)
- Reproduktion: Job `8ed97532-ab1b-4ee3-ae7a-310313c99835` (Plugin-System).
  - `plugins/broken_plugin.py` enthielt den Kommentar „Force a syntax error by having an unclosed string“ über einer korrekt geschlossenen Zeichenkette. Der Verifier meldete `syntax:plugins/broken_plugin.py` = pass.
  - Das Review meldete trotzdem zweimal einen blocker „unclosed string literal“ und schrieb selbst: „The verifier report claims syntax checks passed, which contradicts the code content“.
  - S001 scheiterte (`REVIEW_FIX_REQUIRED`), Replan.
- Ursache: Die Prompt-Regel „never contradict the verifier“ war nur ein Hinweis. Kein deterministischer Schritt prüfte Findings gegen die Werkzeug-Fakten.
- Fix: Neue Review-Invariante 4. Ein major- oder blocker-Finding, das einen Syntaxfehler in einer Datei behauptet, deren Syntax-Check bestanden hat, wird auf minor herabgestuft und mit „[contradicts the verifier …]“ markiert; eine Normalisierungsnotiz bleibt erhalten. Andere Behauptungen und andere Dateien bleiben unberührt.
- Selbstprüfung: echter Schritt nach vorn. Fakten schlagen Meinungen wie bei Invariante 2, jetzt auch in der Gegenrichtung; keine weitere Prompt-Bitte.

### BUG-216 – Review übersieht Randfall-Fehler bei Auftragsvorgaben (lockeres Parsen, Import-Seiteneffekte)
- Phase: Qualitätsprüfung der gelieferten Dateien, Batterie auf 0.1.0rc23 (X2, X3) · Komponente: review · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_review_prompt.py::test_system_prompt_rules`; Live-Nachweis: Review des echten X2-Diffs mit dem neuen Prompt → `fix_required`, major „parseInt accepts '3abc' and '2.5'“)
- Reproduktion:
  - Job `d40f4494-dae2-41ee-a2c0-6ca3fa4d9e8b` (X2) verlangt „Anzahl Personen 1–10“. `validateRegistration` nutzt `parseInt`, das „2.5“ und „3abc“ als gültig durchlässt. Die Fehlermeldungen sind englisch auf einer deutschen Seite. Das Review gab `pass`.
  - Job `1dbe5ea5-ca00-4938-8cfd-9215976b5098` (X3): Schon der Import von `inventory/server.py` legt `inventory.db` an, Verbindungen werden nie geschlossen (`ResourceWarning`), und `INVENTORY_DB` steht nicht in der README.
- Ursache: Der Review-Prompt nannte Randfälle nur allgemein („wrong edge cases“). Das Modell prüfte die Anforderungen auf Vorhandensein, nicht auf exakte Durchsetzung.
- Fix: Konkrete Prüfpunkte im Review-Prompt:
  - Wertebereiche, Formate und Limits des Auftrags werden exakt durchgesetzt (beide Grenzen; scheinbar gültige Eingaben wie `parseInt("2.5")` werden abgelehnt), auch ohne passende Tests.
  - Keine Seiteneffekte beim Import, Ressourcen werden geschlossen.
  - Texte in der Sprache des Auftrags; neue Optionen und Umgebungsvariablen sind dokumentiert.
  - Schweregrade: nicht exakt durchgesetzte Auftragsvorgabe = major, Sprache, Doku und harmlose Import-Effekte = minor (erzwingen keine Korrektur, BUG-191).
- Selbstprüfung: Prompt-Änderung, aber kein Kreisen um ein Symptom. Neue Prüfdimension aus der Qualitätsprüfung, live am echten Diff nachgewiesen.

### BUG-215 – Gelieferter Code mit unbenutzten Variablen und Importen; kein Lint aktiv
- Phase: Qualitätsprüfung der gelieferten Dateien, Batterie auf 0.1.0rc23 (X1) · Komponente: verifier/config · Severity: P3 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_verifier_engine.py::test_default_python_lint_reports_defects_only`)
- Reproduktion: Job `47d60d3e-c914-4101-9e47-4741d974efab`. `sales_analyzer.py:64` `except (ValueError, KeyError) as e` mit unbenutztem `e`, `test_sales_analyzer.py:22` importiert `main` ohne Nutzung. Verifier und Review meldeten nichts.
- Ursache: `policies.verifier.lint_commands` war leer (Standard `{}`). Der Lint-Mechanismus (P21 21.4) lief in Produktion nie, obwohl das Python-Sandbox-Image ruff mitbringt.
- Fix:
  - Standard-Lint für Python: `ruff check --select F,E9`, nur Defekte (undefinierte Namen, unbenutzte Importe und Variablen, Neudefinitionen, Syntax), keine Stilregeln. Ausgenommen sind Re-Exporte in `__init__.py` und Stern-Importe.
  - Fehlt ruff im Image (z. B. Node-Image), wird der Lint übersprungen.
  - System-Contract des Coders: keine unbenutzten Importe oder Variablen, keine undefinierten Namen, kein toter Code, weil der Verifier lintet.
- Selbstprüfung: echter Schritt nach vorn. Ein vorhandener, deterministischer Mechanismus wird aktiviert; nur Defektregeln, damit keine Korrekturschleifen um Stilfragen entstehen.

### BUG-214 – Laufzeitdaten eines Testlaufs (SQLite-Datei) gelten als Scope-Verstoß
- Phase: Erweiterte Batterie auf 0.1.0rc23 (X3) · Komponente: tools/snapshot · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_runtime_data_files_a_command_creates_are_cleaned_not_violations`)
- Reproduktion: Job `1dbe5ea5-ca00-4938-8cfd-9215976b5098` (Inventar-Server auf SQLite).
  - Jeder Testlauf legte über den Standardpfad `inventory.db` an. Die Runtime meldete `COMMAND_SCOPE_VIOLATION` („creating 'inventory.db' is outside allowed_new_paths“), machte die Datei rückgängig und markierte auch grüne Läufe als Fehler.
  - Der Coder wiederholte die Läufe: Wiederholungs-Metrik 5 (max. 2), der Job brauchte 22 Minuten und drei Versuche pro Schritt. Ergebnis trotzdem `succeeded`.
- Ursache: Nur Caches (`__pycache__`, `.pytest_cache` …) galten als generiert und wurden still aufgeräumt. Datenbank-, Journal- und Log-Dateien, die ein Programm beim Laufen erzeugt, galten als Arbeit des Coders.
- Fix: `*.db`, `*.sqlite`, `*.sqlite3`, deren Journal-, WAL- und SHM-Dateien, `*.log` und `.nyc_output/` gehören zu den generierten Dateien. Nicht versionierte Exemplare, die der Scope nicht erlaubt, werden nach dem Befehl entfernt (`[cleanup]`), statt den Lauf scheitern zu lassen. Erlaubt der Scope die Datei, bleibt sie erhalten.
- Selbstprüfung: echter Schritt nach vorn. Die Ursache der Wiederholungen liegt in der Runtime-Rückmeldung, nicht im Modell; es ist keine weitere Wiederholungssperre.

### BUG-213 – Festes Replan-Budget pro Job lässt lange Jobs an ihrem dritten unabhängigen Problem scheitern
- Phase: Audit später Fehlerpfade (2026-10-17) · Komponente: scheduler · Severity: P1 · blockierend: ja für lange Aufträge (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_replans_that_make_progress_get_a_fresh_budget`)
- Reproduktion: Ein Job, dessen Replans jeweils funktionieren (danach schließen weitere Schritte ab) und der später auf ein neues, unabhängiges Problem trifft. Mit `max_replans_per_job: 2` endet er beim dritten Problem mit `REPLAN_LIMIT`, egal ob nach 10 Minuten oder nach 3 Stunden und 25 abgeschlossenen Schritten.
- Ursache: Das Budget zählte alle Replans des Jobs. Es unterschied nicht zwischen Replans, die nichts bringen (Schleife), und Replans, die den Job weiterbrachten.
- Fix:
  - `max_replans_per_job` zählt nur noch Replans in Folge ohne Fortschritt. Beim Start eines Replans merkt sich der Scheduler die Zahl abgeschlossener Schritte (`replan_progress_mark`). Sind beim nächsten Replan mehr Schritte abgeschlossen, beginnt das Budget neu (`replan_budget_base`, wie beim Retry).
  - Neue harte Obergrenze `max_replans_total` (12) über den ganzen Job.
  - Ein erneut ausgeführter Schritt zählt nicht als Fortschritt, denn die Zahl abgeschlossener Schritte muss die Marke übersteigen.
- Selbstprüfung: echter Schritt nach vorn. Eine strukturelle Grenze, die genau die vom Nutzer beschriebenen späten Abbrüche verursacht. Der Schleifenschutz bleibt erhalten.

### BUG-212 – Coder kennt nur das Schrittziel, nicht den Auftrag, und baut am Auftrag vorbei
- Phase: Erweiterte Batterie auf 0.1.0rc22 (X2) · Komponente: coder/context_builder · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_context_builder_sections.py::test_step_goal_carries_the_job_request`)
- Reproduktion: Job `20b80f9b-b56b-478d-9bab-a1f5cdc73fa9`.
  - Auftrag: Veranstaltungsanmeldung mit Name, E-Mail, Anzahl Personen 1–10.
  - Schrittziel des Planners: „Create the static website files … validateRegistration function“, ohne Domäne, Felder und Bereich.
  - Der Coder baute ein Benutzer-Registrierungsformular mit Passwortprüfung. Das Review (sieht den Auftrag) lehnte ab: „user registration form with password validation instead of an event registration form with participant count“. Folge: Korrekturversuch.
- Ursache: Der Kontext des Coders enthielt nur Schrittziel, Scope und Kriterien. Der Auftragstext erreichte ihn nie, nur die Paketprüfung. Jede Zusammenfassung des Planners verliert Details, die das Review dann prüft.
- Fix:
  - `StepBrief.job_request` enthält Titel und Prompt des Jobs. Der Abschnitt STEP GOAL zeigt ihn unter dem Schrittziel mit dem Hinweis, Namen, Felder, Bereiche und Formate von dort zu nehmen.
  - Der Auftrag bekommt höchstens die Hälfte des Abschnitts und wird in der Mitte gekürzt, nie das Schrittziel.
  - Budgetanteil STEP GOAL von 5 % auf 7 %. Ungenutztes Budget fließt wie bisher in RELEVANT CODE/TESTS.
- Selbstprüfung: echter Schritt nach vorn. Eine Informationslücke zwischen Coder und Reviewer, die jede Job-Art trifft, keine Kompensation eines Modellverhaltens.

### BUG-211 – Wiederholungssperre umgangen, sobald derselbe Befehl ein Timeout angibt
- Phase: Erweiterte Batterie auf 0.1.0rc22 (X2) · Komponente: tools · Severity: P3 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_identical_command_on_unchanged_files_is_not_rerun`)
- Reproduktion: Job `20b80f9b-b56b-478d-9bab-a1f5cdc73fa9`, Versuch 2. Turn 3 `node --test app.test.js 2>&1` war grün. Turn 4 lief derselbe Befehl mit `timeout_seconds: 30` erneut, statt als Wiederholung verweigert zu werden. Erst Turn 5 wurde verweigert.
- Ursache: Der Schlüssel der Sperre enthielt das effektive Timeout (180 s Standard gegenüber 30 s).
- Fix: Abgeschlossene Läufe werden ohne Timeout geschlüsselt. Ein beendeter Befehl ändert sein Ergebnis nicht durch ein anderes Timeout. Für Timeouts bleibt der Schlüssel mit Timeout, denn ein längeres Timeout ist ein echter neuer Versuch.
- Selbstprüfung: Lücke in der vorhandenen Sperre (BUG-179/207), keine neue Kompensation.

### BUG-210 – Konflikt mit geänderter Base verwirft beim Abschluss die gesamte verifizierte Arbeit
- Phase: Audit später Fehlerpfade (2026-10-17) · Komponente: runtime/finalize · Severity: P1 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_runtime_driver.py::test_conflicting_base_change_delivers_the_work_on_its_old_base`, `::test_failed_regression_fails_job_but_delivers_a_draft`)
- Reproduktion: Während ein Job läuft, ändert jemand auf dem Ziel-Branch dieselbe Datei. Beim Abschluss scheitert `update_to_base` mit `MERGE_CONFLICT`, der Job endet `failed` und nichts wird gepusht. Ebenso bei `REGRESSION_FAILED` nach einem erfolgreichen Rebase.
- Auswirkung: Ein mehrstündiger Job verliert ganz am Ende seine komplette, bereits verifizierte Arbeit. Ein Retry trifft auf denselben Konflikt.
- Ursache: Der Abschluss behandelte eine Merge-Entscheidung, die die Runtime nie trifft, weil sie nie merged, als Fehler des Jobs.
- Fix:
  - Bei Konflikt bleibt der Workspace auf der alten Base. Der Branch wird als Draft-MR gepusht (`Draft:`-Titel, Konfliktdateien in Beschreibung und Bericht). Der Job endet `succeeded` mit dem Hinweis „Base-Konflikt – vor dem Merge auflösen“.
  - Bei Regression nach dem Base-Update bleibt der Job `failed (REGRESSION_FAILED)`, der Branch wird aber ebenfalls als Draft-MR geliefert.
- Selbstprüfung: echter Schritt nach vorn. Ein neuer Fehlerpfad, der vorher Arbeit vernichtete, kein Symptom eines früheren Fixes.

### BUG-209 – Tests in einem `test`-Schritt geplant, der nie Dateien schreibt
- Phase: Erweiterte Batterie auf 0.1.0rc22 (X1) · Komponente: planner/validation · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_a_check_only_step_cannot_create_files`, `::test_tests_for_new_code_belong_in_the_same_step`)
- Reproduktion: Job `673d680c-7934-45ed-b34d-2d9820d1792a`.
  - Plan v1: S001 `implement` (`sales_analyzer.py`, `sample_sales.csv`), S002 `test` mit `allowed_new_paths: [test_sales_analyzer.py]`.
  - Ein `test`-Schritt läuft im `VerifyStepHandler` und führt nur Checks aus. Die Testdatei wäre nie entstanden.
  - Das Review von S001 verlangte die im Auftrag geforderten Tests („lacks the required unit tests“). Es folgten zwei Fehlversuche, `repeated_verifier_failure` und ein Replan mit derselben Aufteilung (S003/S004).
- Ursache:
  - Die Validierung prüfte `allowed_new_paths` nur auf Form, nicht darauf, ob der Schritttyp überhaupt schreibt.
  - Die Regel „Code und Tests in einen Schritt“ (U3) prüfte nur schreibende Schritttypen. Zusätzlich ließ ein `README.md` unter den Hints des Testschritts die Aufteilung als Code-Schritt erscheinen.
- Fix:
  - Neuer Validierungsfehler für `test`/`verify`/`review`-Schritte mit `allowed_new_paths`. Die Meldung nennt die Lösung: die Dateien gehören in den Schritt, der den Code schreibt.
  - In der Aufteilungsregel zählen Dokumentationspfade (`*.md`, `README*` …) nicht als Code.
  - Planner-Prompt: test/verify/review schreiben nie Dateien; der Code-Schritt schreibt seine Tests selbst.
- Selbstprüfung: Planner-Ebene wie BUG-188, 203 und 204, aber eine eigene Klasse: Erreichbarkeit nach Schritttyp statt Diff-Kriterien. Die Regel ist deterministisch und gilt für jedes Projekt.

### BUG-208 – Bestätigungsfrage des Replanners stoppt einen Job mit fertigem Plan
- Phase: Erweiterte Batterie auf 0.1.0rc22 (X1) · Komponente: planner/replanner · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_runtime_driver.py::test_a_confirmation_question_next_to_a_plan_does_not_stop_the_job`)
- Reproduktion: Job `673d680c-7934-45ed-b34d-2d9820d1792a`. Der Replanner lieferte gültige Schritte S003/S004 und zusätzlich `user_question`: „… Please confirm if this approach is acceptable or if you have specific constraints on the test structure.“ Der Job ging nach 17,9 min in `waiting_for_user`, obwohl nichts eine Entscheidung des Nutzers brauchte.
- Ursache: `Replanner` behandelte jede gesetzte `user_question` als Rückfrage (Pfad aus BUG-187) und verwarf die Schritte. Das Modell nutzt das Feld auch für Höflichkeitsbestätigungen.
- Fix:
  - Eine Rückfrage zählt nur noch, wenn `steps` leer ist.
  - Eine Frage neben einem Plan wird als Plan-Notiz im Ereignis `replan.created` festgehalten, und die Schritte laufen.
  - Schema-Beschreibung und Replanner-Prompt sagen ausdrücklich: nie fragen, um einen lieferbaren Plan bestätigen zu lassen.
- Selbstprüfung: echter Schritt nach vorn. Die Rückfrage aus BUG-187 bleibt für echte Blocker erhalten; nur der Fehlgebrauch hält keinen Job mehr an.

### BUG-207 – Grüner Check wird nach der Verweigerung immer wieder vorgeschlagen, statt den Schritt abzuschließen
- Phase: Batterie-Lauf 1 auf 0.1.0rc21 (J5) · Komponente: coder · Severity: P2 · blockierend: ja für die Abnahme (behoben) · Status: **behoben** (`tests/integration/test_coder_loop.py::test_a_passing_check_proposed_again_after_the_refusal_completes_the_step`)
- Reproduktion: Job `78c08cc2-5706-4c28-963c-bae59ddc1acb`. Nach grünem `node --test src/slugify.test.js 2>&1` schlug der Coder denselben Befehl noch dreimal vor. Der Guard verweigerte jedes Mal mit „It succeeded … call complete_step“. Metrik `repeats` 4 (max 2), FAIL. Dasselbe passierte bei rotem Test, dort mit Stagnation und Korrekturversuch als Folge.
- Ursache: Modellschwäche (qwen3.6, Mehrturn-Zustand). Hinweis (BUG-175) und Katalogentzug (BUG-181) halfen nicht zuverlässig, denn das Modell ruft das zurückgehaltene Tool trotzdem auf.
- Fix: Schlägt der Coder einen bestandenen Check nach der ersten Verweigerung erneut vor, ohne dass sich seither etwas geändert hat, meldet die Runtime `complete_step` für ihn. Das ist ein echter Tool-Aufruf über die Engine mit Zusammenfassung „Completed by the runtime …“. Verifier und Review prüfen den Schritt danach wie sonst. Rote Checks werden nie so abgeschlossen.
- Selbstprüfung: dritte Maßnahme gegen dasselbe Modellverhalten (nach BUG-175, BUG-181). Anders als die früheren ist sie kein weiterer Hinweis an das Modell, sondern die deterministische Konsequenz: grün und unverändert heißt fertig. Keine Heuristik.

### BUG-206 – Liegengebliebene Änderungen eines abgebrochenen Schritts gelten als Scope-Verstoß des nächsten Schritts
- Phase: Batterie-Lauf 1 auf 0.1.0rc20 (J1) · Komponente: scope/coder · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_scope_engine.py::test_uncommitted_changes_of_an_unfinished_step_are_adopted`)
- Reproduktion: Job `a71399e6-e15f-486c-ad1e-73dcb0ad068a`.
  - S001 änderte `README.md`, scheiterte und wurde durch Replan ersetzt; die Änderung blieb uncommittet im Work Tree.
  - Die neuen Schritte S003 und S005 hatten `README.md` nicht im Scope. Jeder ihrer Versuche scheiterte mit „1 out-of-scope change(s)“ (`README.md`), also an einer Änderung, die sie nie gemacht hatten.
  - Es folgten zwei Replans bis `REPLAN_LIMIT`. Ein einfacher Health-Endpoint-Job lief über 30 Minuten.
- Ursache: Schritte eines Jobs laufen nacheinander und committen nach der Verifikation. Eine Änderung beim Start eines Schritts stammt deshalb immer von einem unfertigen früheren Versuch. Der Scope-Check misst gegen den HEAD beim Schrittstart und schrieb sie dem neuen Schritt zu.
- Fix: Beim ersten Versuch eines Schritts übernimmt `ScopeEngine.adopt_leftovers` die liegengebliebenen Dateien in den Scope.
  - Es entsteht eine neue Scope-Version (Quelle `replanner`, Ereignis `scope.expanded` mit `adopted_leftovers`). Verbotene Pfade werden nie übernommen.
  - Verifier und Review beurteilen die Dateien mit dem Rest der Änderung. Vorhandene Arbeit geht nicht verloren.

### BUG-205 – Hängender Test blockiert einen Coder-Turn für das volle Sandbox-Limit von 600 s
- Phase: Batterie-Lauf 2 auf 0.1.0rc19 (J1, abgebrochener Lauf) · Komponente: tools · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_commands_without_a_timeout_get_the_short_coder_default`)
- Reproduktion: Job `f6af3249-155a-4fc0-a504-c44ecf199c95`. Der Coder schrieb einen Test, der einen `BaseHTTPRequestHandler` auf einem `BytesIO` aufruft und nie zurückkehrt. Turn 3 `python -m pytest -q` lief bis zum Timeout nach 600,2 s (20:14 → 20:24).
- Ursache: Ohne `timeout_seconds` bekam ein Coder-Befehl das Maximum (`sandbox.default_timeout_seconds` = 600 s) als Standard.
- Fix:
  - Neuer Standard `policies.coder.default_command_timeout_seconds` (180 s) für `run_command`/`run_test` ohne Angabe; das Maximum bleibt.
  - Die Tool-Beschreibung sagt, dass der Standard kurz ist und längere Suiten ihn erhöhen.
  - Installationen behalten ihr eigenes Limit von 900 s.

### BUG-204 – Plan verbietet die eigenen Dateien des Schritts (`must_not_change`): Bugfix-Job endet mit REPLAN_LIMIT
- Phase: Batterie-Lauf 1 auf 0.1.0rc19 (J6) · Komponente: planner · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_must_not_change_on_the_steps_own_files_is_dropped`)
- Reproduktion: Job `1fc45bb9-fd09-4ed6-8b27-d2735d98c370`.
  - Gemmas Plan-Antwort wiederholte ihre Acceptance-Blöcke (entartete Ausgabe) und enthielt `must_not_change: ["inventory/store.py", "tests/test_store.py"]`. Genau diese beiden Dateien musste der Schritt laut `must_change` ändern.
  - Jeder Versuch scheiterte mit „files that must not change were changed“. Der Replanner übernahm das Kriterium zweimal, der Job endete nach 10,3 min mit `REPLAN_LIMIT`.
  - Code und Test waren schon im ersten Versuch korrekt.
- Ursache: Die Validierung erkannte den Selbstwiderspruch nicht. Reparaturrunden helfen bei entarteter Ausgabe nicht, sie wiederholen sie.
- Fix: Die Plan-Validierung (Planner und Replanner) streicht `must_not_change`-Globs, die eine eigene konkrete Datei des Schritts treffen oder in seinem `must_change` stehen, und vermerkt das als Notiz. Andere geschützte Dateien bleiben geschützt.
- Selbstprüfung: Das ist der dritte Fix an planner-erzeugten Diff-Kriterien (BUG-188, BUG-203, BUG-204). Jeder deckt eine eigene, statisch erkennbare Fehlerklasse ab; es wird also nicht dasselbe Symptom erneut kompensiert. Die Häufung wird in den nächsten Läufen beobachtet.

### BUG-203 – Planner verlangt eine Änderung an einem korrekten Manifest: der Coder ändert package.json nur fürs Kriterium
- Phase: Batterie-Lauf 1 auf 0.1.0rc19 (J5) · Komponente: planner · Severity: P2 · blockierend: ja für die Abnahme (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_existing_manifest_may_change_but_is_not_required_to`)
- Reproduktion: Job `e15dac33-285f-4208-a0cb-4a14b6978b25` (slugify mit `node --test`).
  - Plan v1 enthielt `diff must_change: ["package.json"]`, obwohl `package.json` schon `"test": "node --test"` hatte.
  - Versuch 1 scheiterte nur daran. Versuch 2 fügte ein `description`-Feld ein, nur um das Kriterium zu erfüllen.
  - Die Metrik `no_new_dependencies` schlug an (FAIL).
- Ursache: Gemma setzt Ziel-Dateien vorsorglich als `must_change`. Bei einem bestehenden Manifest erzwingt das eine sinnlose Änderung.
- Fix: Die Plan-Validierung (Planner und Replanner, `prune_unreachable_must_change`) streicht `must_change` auf bestehende Dependency-Manifeste und Lockfiles, sofern der Auftrag die Datei nicht nennt. Die Datei bleibt im Scope änderbar. Ob eine Abhängigkeit nötig ist, zeigen Tests und Import-Prüfung (BUG-193).
- Beobachtung (nicht blockierend): Nach einem grünen Testlauf versuchte der Coder denselben Befehl noch zweimal, statt `complete_step` zu rufen. Der Guard verweigerte beide Male mit dem Hinweis auf `complete_step` (BUG-175/181). Kosten etwa 20 s je Vorkommen, kein Fehlschlag.

### BUG-202 – Merge Request direkt nach dem Push: GitLab kennt den Branch noch nicht, der Job endet ohne MR
- Phase: Batterie-Lauf 1 auf 0.1.0rc19 (J2) · Komponente: gitops · Severity: P2 · blockierend: ja für die Abnahme (behoben) · Status: **behoben** (`tests/unit/test_gitops_units.py::test_merge_request_waits_until_gitlab_knows_the_pushed_branch`)
- Reproduktion: Job `ee3bd92c-f3a2-4beb-939b-566268717dea`. Push um 19:31:49.36, MR-Anlage um 19:31:49.86. GitLab antwortete `HTTP 400 {'source_branch': ['does not exist']}`. Der Job war `succeeded`, aber ohne Merge Request; die Batterie wertete FAIL.
- Ursache: GitLab registriert gepushte Branches asynchron. Der Client wiederholte nur 429/5xx und Transportfehler; die 400 in diesem Zeitfenster ist aber vorübergehend.
- Fix: `GitLabClient.create_merge_request` wiederholt die Anlage bei genau dieser Antwort mit 1/2/4/8/15 s Abstand. Danach wird der Fehler wie bisher gemeldet.

### BUG-201 – Manuelles Replan bei aufgebrauchtem Budget beendet den Job
- Phase: Code-Audit lange Aufträge · Komponente: api/planner · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_runtime_driver.py::test_replanner_question_makes_the_job_wait_and_the_answer_resumes_it`)
- Ursache: Der Scheduler übersprang für ein angefordertes Replan die Budgetprüfung. `Replanner.replan` prüfte das Budget aber erneut und warf `ReplanLimitReached`. Ein Replan, das ein Mensch anforderte, endete so bei 2 verbrauchten Replans mit dem Scheitern des Jobs.
- Fix: `POST /replan` setzt `REPLAN_BUDGET_BASE` auf die aktuelle Replan-Zahl, wie `retry` (BUG-189). Ein menschliches Eingreifen startet das automatische Budget neu.

### BUG-200 – Regressionslauf nach Base-Update prüft Text-Belege früherer Schritte auf dem Endstand
- Phase: Code-Audit lange Aufträge · Komponente: verifier · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_verifier_engine.py::test_job_level_rerun_does_not_fail_on_text_evidence_a_later_step_changed`)
- Reproduktion: Schritt S2 belegt `presence: ^def add\(`, Schritt S6 benennt die Funktion legitim um. Bewegt sich die Basis während des Jobs, verifiziert `finalize` alle Schritte erneut auf dem Endstand. S2 scheitert, der Job endet mit `REGRESSION_FAILED`, obwohl die Tests grün sind. Ohne Base-Update wäre der Job erfolgreich gewesen.
- Fix: Auf Job-Ebene (`job_level`) wird ein fehlschlagender `presence`/`absence`-Beleg zu `skip` mit Hinweis. Tests, Commands, Syntax, Secrets und Importe entscheiden über den Gesamtstand.

### BUG-199 – Netzwerk- oder Git-Fehler im Abschluss beenden den Job nach Stunden Arbeit
- Phase: Code-Audit lange Aufträge · Komponente: runtime/scheduler · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_transient_remote_failure_in_finalize_runs_finalize_again`, `tests/unit/test_finalize_network.py`)
- Ursache: Jeder `GitError` in `finalize` war terminal: Mirror-Fetch, `ls-remote`, Push-Timeout, Lock-Timeout, Push abgelehnt weil die Basis sich bewegte. Wiederholt wurden nur DB-Fehler.
- Fix:
  - Der Driver erkennt Netzwerkfehler am stderr (DNS, Connection refused/reset, TLS, HTTP 5xx, „remote end hung up“) als `GIT_NETWORK`.
  - Für `GIT_NETWORK`, `GIT_TIMEOUT`, `GIT_LOCK_TIMEOUT` und `PUSH_REJECTED` bleibt der Job in `committing`. Der Scheduler startet `finalize` nach Backoff erneut (60/120/300/600 s, höchstens `model_outage_max_wait_seconds`). `finalize` ist idempotent; die Basis wird dabei neu geprüft.
  - Deterministische Fehler wie `MERGE_CONFLICT` scheitern weiter sofort.

### BUG-198 – Nicht erreichbarer Modell-Host im Review kostet eine Korrekturrunde oder ein Replan
- Phase: Code-Audit lange Aufträge · Komponente: review · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_review_workspace.py::test_unreachable_model_host_is_no_verdict_but_a_retry`)
- Ursache: Das Review ist fail-closed. Auch `MODEL_UNAVAILABLE`/`MODEL_LOAD_FAILED` ergaben ein `fix_required` ohne Befunde. Im Implement-Schritt kostete das eine Korrektur; im Gesamt-Review blockierte der Schritt und löste ein Replan aus. Beides passierte, obwohl niemand die Änderung angesehen hatte.
- Fix: `WorkspaceReviewer.review` wirft bei diesen Codes `ResourceUnavailable`. Beide Handler machen daraus einen wiederholbaren Fehlschlag, der nach BUG-197 kein Budget kostet. Alle anderen Fehler bleiben fail-closed, auch Timeouts und ungültige Ausgaben. Ein Pass entsteht nie.

### BUG-197 – Modell-Ausfall während eines Schritts verbraucht dessen Versuche, dann ein Replan
- Phase: Code-Audit lange Aufträge · Komponente: scheduler · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_model_outage_during_a_step_does_not_use_up_its_attempts`)
- Ursache: Die Ausfall-Toleranz (BUG-176, bis 4 h) galt nur für Planung und Replan. Im Schritt zählte jeder `MODEL_UNAVAILABLE`/`MODEL_TIMEOUT`/`MODEL_LOAD_FAILED`/`RESOURCE_UNAVAILABLE`-Versuch gegen `max_attempts_per_step` (3, geteilt mit Korrekturen). Ein Neustart des Modell-Hosts von wenigen Minuten ließ den Schritt scheitern und verbrauchte ein Replan.
- Fix: Solche Versuche haben keine Arbeit geleistet. Der Scheduler erhöht `max_attempts` um eins, die Versuchsnummer bleibt eindeutig. Der Schritt läuft nach Backoff (60/120/300/600 s) erneut, höchstens `model_outage_max_wait_seconds` lang je Schritt; danach gilt das alte Verhalten.

### BUG-196 – Job-Scope übernimmt das „nicht anfassen“ eines Schritts für alle Schritte
- Phase: Code-Audit lange Aufträge · Komponente: runtime · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_merge_scopes.py`)
- Reproduktion: S2 hat `forbidden_paths: ["tests/**"]`, S7 legt `tests/test_x.py` an. `merge_scopes` behielt `tests/**` als verboten, weil das Muster nur bei exakt gleichem String verworfen wurde. Gesamt-Review und Regressionslauf scheiterten deterministisch am Scope. Das führte zu Replans bis `REPLAN_LIMIT` bzw. zu `REGRESSION_FAILED`.
- Fix: Im Job-Scope bleibt nur verboten, was *jeder* Schritt verbietet. Was kein Schritt erlaubt hat, wird weiterhin abgelehnt; `always_forbidden` gilt immer.

### BUG-195 – Gesamt-Review und Regressionslauf messen die ganze Job-Änderung an Pro-Schritt-Grenzen: große Jobs scheitern ganz am Ende
- Phase: Code-Review nach BUG-194 (Ziel lange Aufträge) · Komponente: verifier/runtime · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_verifier_engine.py::test_job_level_verification_does_not_apply_per_step_change_limits`)
- Reproduktion: Ein Job ändert über mehrere Schritte zusammen mehr als `policies.verifier.max_changed_files` (40) Dateien, etwa eine Webseite oder ein Plugin-System. Oder ein späterer Schritt ändert eine Datei, die ein früherer Schritt per `must_not_change` nicht anfassen durfte. Das Gesamt-Review (`step_baseline=False`, Job-Basis) schlägt dann deterministisch mit `changed_file_count` bzw. `acceptance[i]:diff` fehl. Ebenso der Regressionslauf nach einem Base-Update in `finalize`. Beides passiert nach Stunden Arbeit.
- Ursache: Die Grenzen begrenzen die Änderung *eines* Schritts. Sie wurden auf die Summe aller Schritte angewandt.
- Fix:
  - `VerificationStep.job_level` ist für Gesamt-Review und Regressionslauf gesetzt.
  - Dort werden `changed_file_count`, `max_changed_files` und `must_not_change` der Diff-Kriterien nicht erneut erzwungen; sie galten bereits für die Änderung jedes Schritts.
  - `must_change`, Scope (Vereinigung), Secrets, Syntax, Tests und Import-Prüfung bleiben aktiv.

### BUG-194 – Import-Prüfung mit dem falschen Interpreter: grüne Schritte mit Projekt-venv als „nicht auflösbar“ abgelehnt
- Phase: Full-Suite vor 0.1.0rc19 · Komponente: verifier · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_verifier_imports.py::test_imports_resolve_with_the_interpreter_the_tests_use`, e2e `test_37_1`, `test_37_13`, `test_37_14`, `test_37_15`)
- Reproduktion: Die e2e-Szenarien testen mit `<venv>/bin/python -m pytest`. `imports:python` (BUG-193) prüfte dagegen mit dem nackten `python3`, in dem fastapi fehlt. Der Implement-Schritt scheiterte deshalb mit `VERIFIER_FAILED`. Das Gesamt-Review (eigener Schritt ohne Test-Kriterien) scheiterte ebenso.
- Ursache: Der Interpreter der Prüfung war fest `python3`. Er war nicht der, mit dem das Projekt seine Tests ausführt (venv, `python3.12`, `uv run`, `.venv/bin/pytest`). Projekte, die ihre Tests über `make` oder `tox` starten, wären produktiv genauso fälschlich gescheitert, oft spät in einem langen Job.
- Fix:
  - Der Interpreter kommt aus den Test- und Command-Kriterien des Schritts. Beim Gesamt-Review kommen die Kriterien aus den Code-Schritten des Jobs (`command_hints`).
  - Ein Fehlschlag gilt nur, wenn ein Kriterium den Interpreter benennt.
  - Ein nur geratenes `python3` meldet nicht gefundene Module als `skip` mit Hinweis, nicht als Fehler.

### BUG-193 – Erfundenes Paket per Import in einer Funktion: Tests grün, Merge Request mit Code, der beim Start abstürzt
- Phase: Batterie-Lauf 1 auf 0.1.0rc18 (J4) · Komponente: verifier · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_verifier_engine.py::test_new_import_that_does_not_resolve_fails`, `tests/unit/test_verifier_imports.py`)
- Reproduktion: Job `cee8d8e8-dd4f-4d9e-bbf3-94f8a0ed91e1` (J4, „Binde das Python-Paket "hermclaw-nonexistent-sdk-zz9" ein …“).
  - Der Coder setzte `import hermclaw_nonexistent_sdk_zz9` und `connect()` in `main()`.
  - Die Tests rufen `main()` nie auf; deshalb bestanden 3/3, Verifier und Review bestanden.
  - Der Job endete `succeeded` mit Merge Request. Der Server stürzt beim Start mit `ModuleNotFoundError` ab.
- Ursache: Keine Prüfung stellte fest, ob neu importierte Module überhaupt existieren. Syntax, Compile und Tests decken einen Import in einer nicht getesteten Funktion nicht ab. Ein erfundenes oder nicht installiertes Paket ist ein typischer Modellfehler.
- Fix: Neue Verifier-Prüfung `imports:python`.
  - Geprüft werden absolute Importe auf hinzugefügten Zeilen geänderter `.py`-Dateien.
  - Übersprungen werden Importe in `try/except ImportError` und unter `if TYPE_CHECKING`, außerdem die Standardbibliothek und Module des Repositories (Root, `src/`, Verzeichnis der Datei).
  - Der Rest muss in der Sandbox per `importlib.util.find_spec` auflösbar sein, mit denselben installierten Abhängigkeiten wie die Tests.
  - Sonst schlägt die Prüfung fehl, mit Ort und Anweisung: deklarieren und installieren, Vorhandenes nutzen oder den Schritt blockieren.
  - Damit führt ein unbeschaffbares Paket zu Blockade und Rückfrage (BUG-187) statt zu kaputtem Code im MR.

### BUG-192 – Research: Bei inhaltsgleichen Quellen gewinnt die schnellere statt der maßgeblichen
- Phase: Full-Suite für 0.1.0rc18 · Komponente: research/engine · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_research_engine.py::test_duplicate_text_keeps_the_authoritative_source_even_when_the_mirror_answers_first`)
- Reproduktion: `test_run_links_job_and_identical_claims_from_two_sources_are_merged` war flaky. Einzeln wiederholt schlug er etwa jeden dritten Lauf fehl, weil die Doku statt des Mirrors als Duplikat übersprungen wurde.
- Ursache: Die Quellen werden parallel geladen. Die Duplikatprüfung (`content_hash`) markierte die Quelle als gelesen, die zuerst antwortete. In der Produktion konnte so ein schneller Mirror oder ein Forum die offizielle Dokumentation verdrängen.
- Fix:
  - Weiterhin paralleles Laden, aber die Auswertung läuft in fester Reihenfolge: höhere Autorität zuerst, bei Gleichstand der Suchrang.
  - Ereignisse und Persistenz folgen dieser Reihenfolge.
  - Der neue Test lässt die Doku absichtlich langsamer antworten. Ohne Fix schlägt er fehl; der alte Test bestand mit Fix 15 von 15 Läufen.

### BUG-191 – Review verlangt eine Korrekturrunde für Kleinigkeiten („minor“)
- Phase: Batterie-Lauf 1 auf 0.1.0rc17 (J5) · Komponente: review/invariant · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_review_severity.py::test_fix_required_with_only_minor_findings_passes`)
- Reproduktion: Job `0532105b-2b1e-4a85-9a0f-3726d8aa8196` (J5), Versuch 2 von S001.
  - Der Verifier bestand (15/17, 2 übersprungen).
  - Das Review urteilte trotzdem `fix_required`: „The implementation is functionally correct and tests pass, but the test file contains a duplicate test case which is a minor code quality issue.“
  - Es folgte ein ganzer Korrekturversuch; die Korrekturbudgets sind auf 2 pro Schritt begrenzt.
- Ursache: Der Review-Prompt definiert `minor` als „does not block acceptance“. Die deterministische Invariante setzte aber nur die Gegenrichtung durch: major/blocker → kein `pass`.
- Fix:
  - Urteilt das Modell `fix_required` und nennt nur Minor-Befunde, wird daraus `pass` (`only_minor_findings`); die Befunde bleiben als Hinweise.
  - Nicht betroffen: ein fehlgeschlagener Verifier, ein `fix_required` ohne Befund (Fail-Closed- und Runtime-Entscheidungen) und jeder major/blocker-Befund.
  - In 7 Tagen trat der Fall einmal auf (J5); alle anderen `fix_required` nannten major oder blocker.

### BUG-190 – Batterie wertet eine korrekte Rückfrage als FAIL („None min“); Rückfrage mitten im Satz abgeschnitten
- Phase: Batterie-Lauf 1 auf 0.1.0rc17 (J4) · Komponente: acceptance/job_metrics, planner/replan_contract · Severity: P2 · blockierend: ja für die Abnahme (behoben) · Status: **behoben** (`tests/integration/test_acceptance_jobs.py::test_a_job_waiting_for_the_user_gets_a_duration`)
- Reproduktion: Job `f50ee894-4e8c-423e-80ab-724e0dfa0a4e` (J4).
  - Nach 3,1 min stand er korrekt in `waiting_for_user` mit `USER_INPUT_REQUIRED` und einer klaren Frage (BUG-187 wirkt). Trotzdem lautete das Ergebnis FAIL: `duration: None min`.
  - Die Frage füllte die 1000 Zeichen von `user_question` und endete mitten im Satz („The specific question is: How do I“). Das JSON-Schema wird per Grammatik erzwungen und kappt deshalb hart.
- Ursache:
  - Die Dauer wurde nur aus `finished_at` berechnet, das `waiting_for_user` und `blocked` nicht setzen.
  - Für die Länge der Frage gab es keine Vorgabe.
- Fix:
  - Für Jobs, die die Batterie als beendet zählt, endet die Dauer mit dem letzten `job.transition`.
  - `user_question` und der Replanner-Prompt verlangen ein bis zwei kurze Sätze (höchstens 400 Zeichen).

### BUG-189 – Retry eines gescheiterten Jobs scheitert sofort wieder; lange Aufträge ließen sich nicht fortsetzen
- Phase: Analyse nach Batterie-Lauf 1 auf 0.1.0rc16 · Komponente: api/jobs, runtime/driver, scheduler, planner/replanner · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_runtime_driver.py::test_retry_after_the_replan_limit_continues_with_a_fresh_budget`)
- Reproduktion: Ein Job endet mit `REPLAN_LIMIT`, dann folgt `POST /api/jobs/{id}/retry`.
  - Erster Defekt: Die Vorbereitung erkennt den vorhandenen Plan und will direkt `queued -> running`. Diesen Übergang verbietet die State-Machine, also folgt `PREPARE_INVALID_STATE`.
  - Zweiter Defekt, ohne den ersten: Der blockierte Schritt bleibt blockiert und `replan_count` steht noch am Limit. Damit folgt sofort wieder `REPLAN_LIMIT`.
  - Die Antwort „failed steps will be retried“ stimmte nie: `retry_requested` wurde gesetzt, aber nirgends gelesen.
- Ursache: Retry war nur ein Statuswechsel. Weder Schritte noch Budget wurden zurückgesetzt, und der Driver-Pfad für einen bestehenden Plan passte nur zur Wiederherstellung aus `planning`.
- Fix:
  - Bei einem Retry bleiben erledigte Schritte erhalten.
  - Fehlgeschlagene und blockierte Schritte bekommen ein frisches Versuchsbudget (`max_attempts` ab dem bisherigen Zähler, damit die Versuchsnummern eindeutig bleiben; Korrekturzähler 0) und starten als `ready` bzw. `pending`.
  - Das Replan-Budget zählt ab dem Retry (`replan_budget_base`, `job_metadata.replans_used`; gilt für Scheduler und Replanner).
  - Ein Retry mit Begründung lässt stattdessen die offene Arbeit mit diesem Hinweis neu planen.
  - Bei vorhandenem Plan läuft der Driver über `planning`, wie jeder Job; die State-Machine bleibt unverändert.
  - UI: Retry fragt nach einem optionalen Hinweis.

### BUG-188 – Planer-Wiederholungsschleife: erfundene Dateien in `must_change`, identische Reparaturantworten
- Phase: Batterie-Lauf 1 auf 0.1.0rc16 (J5) · Komponente: planner/validation, planner/loop · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_invented_must_change_files_are_dropped_when_the_step_keeps_a_reachable_one`, `tests/unit/test_planner_loop.py::test_a_repair_that_changes_nothing_raises_the_temperature`)
- Reproduktion: Job `f4239236-3277-4d7b-9b55-6b74327bf3ae` (J5, Node-Funktion slugify) scheiterte nach 2,7 min mit `PLANNER_INVALID_OUTPUT`.
  - Der Plan selbst war richtig: ein Schritt mit `src/slugify.js` und `test/slugify.test.js`.
  - Daneben standen Dutzende Diff-Kriterien für erfundene Dateien (`src/index.js`, `src/utils/encoders.js`, …).
  - Beide Reparaturantworten waren byteidentisch (gleicher md5, je 923 Tokens).
- Ursache:
  - Die Validierung verwarf den ganzen Plan wegen Kriterien, die der Schritt ohnehin nie erfüllen kann.
  - Die Reparatur lief mit derselben niedrigen Temperatur (0,2) auf fast demselben Prompt und reproduzierte die Schleife.
- Fix:
  - `prune_unreachable_must_change` entfernt `must_change`-Muster außerhalb der Dateien des Schritts, mit einer Notiz in `planner.plan.created` bzw. `replan.created`.
  - Das gilt nur, solange der Schritt mindestens ein erreichbares `must_change` behält. Verfehlen alle, bleibt es der Fehler aus U4 (falscher Pfad), den der Planer korrigieren muss.
  - Ändert eine Reparatur nichts (gleiche Antwort oder gleiche Fehlerliste), läuft der nächste Versuch mit `repeat_temperature` 0,7.

### BUG-187 – Replanner plant gegen eine ausdrückliche Vorgabe des Auftrags, statt den Nutzer zu fragen
- Phase: Batterie-Lauf 1 auf 0.1.0rc16 (J4) · Komponente: planner/replanner, runtime/driver, scheduler, api · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_runtime_driver.py::test_replanner_question_makes_the_job_wait_and_the_answer_resumes_it`, `tests/integration/test_planner_replan.py::test_replan_contract_needs_steps_unless_it_asks_the_user`)
- Reproduktion: Job `4a0c04c6-6f1d-46ca-af53-21cd161161e7` (J4, „Binde das Python-Paket "hermclaw-nonexistent-sdk-zz9" ein … Verwende genau dieses Paket.“).
  - S001 blockierte korrekt mit `missing_dependency`: Das Paket existiert in keiner Registry.
  - Gemma plante darauf „Remove impossible import and call from server.py“. Dieser Import war nie im Code; die Diff-Abnahme konnte also nie erfüllt werden. Ein zweiter Replan folgte.
  - Frühere Läufe von J4 endeten jedes Mal erst mit `REPLAN_LIMIT` (18,1 min bzw. 35,0 min), nie mit einem sauberen Halt.
- Ursache:
  - Der Replanner kannte nur zwei Ausgänge: einen neuen Plan oder einen Fehler.
  - Sein Prompt verlangte bei einer verweigerten Abhängigkeit ausdrücklich „the standard-library solution without that package“, auch wenn der Auftrag genau dieses Paket fordert.
  - Kein Runtime-Pfad erzeugte `waiting_for_user`. Ein unlösbares externes Hindernis verbrauchte deshalb immer das ganze Replan-Budget; bei langen Aufträgen sind das Stunden.
- Fix:
  - `ReplanContract.user_question`: Ist die Frage gesetzt, entfallen die Steps.
  - Der Replanner legt dann keine Planversion an und verbraucht kein Replan-Budget. Er schreibt `replan.user_question`.
  - Der Driver liefert `waiting_for_user` mit `USER_INPUT_REQUIRED`; die Frage steht in `error_message` und als Statuszeile.
  - Ein `POST /api/jobs/{id}/replan?reason=<Antwort>` setzt den wartenden Job fort. Die Antwort geht als Replan-Grund an Gemma. Fehlercode und Frage werden beim Verlassen von `waiting_for_user` gelöscht.
  - Prompt-Regel: Niemals um eine ausdrückliche Vorgabe herum planen. Fehlt das Geforderte nachweislich oder widersprechen sich Vorgaben, wird eine konkrete Frage gestellt.

### BUG-186 – Coder rät vor jedem Testlauf ein absolutes Arbeitsverzeichnis
- Phase: Batterie-Lauf 1 auf 0.1.0rc16 (J3, J4) · Komponente: context_builder, tools/registry · Severity: P3 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_prompt.py`, Vertragsprüfung)
- Reproduktion: Job `b339a2c8-a255-417c-9792-69692a7a43d9` Zug 4 `cd /home/user/repo && python -m pytest -q` → `can't cd`; ebenso J4 `cd /home/user/repos/hermclaw-test`. In 7 Tagen 5 von 570 Befehlen, alle fehlgeschlagen, je ein verlorener Zug.
- Ursache: Weder der Vertrag noch die Werkzeugbeschreibung sagen, dass Befehle im Repository-Wurzelverzeichnis starten.
- Fix: Eine Vertragsregel: Befehle starten im Repository-Root, nur relative Pfade, kein `cd`. `run_command` nennt das Startverzeichnis.

### BUG-185 – Batterie-Metrik verlangt `complete_step` auch von reinen Verifikationsschritten
- Phase: Batterie-Lauf 1 auf 0.1.0rc16 (J3) · Komponente: acceptance/job_metrics · Severity: P2 · blockierend: ja für die Abnahme (behoben) · Status: **behoben** (`tests/integration/test_acceptance_jobs.py::test_metrics_of_a_good_job_pass`)
- Reproduktion: J3 endete `succeeded` mit MR und ohne Replan, wurde aber als FAIL gewertet: „2 implementing step(s) completed, 1 without complete_step“. S002 (`kind: test`) lief über `VerifyStepHandler`, ohne Coder.
- Ursache: `IMPLEMENTING_KINDS` enthielt `test`, obwohl `test`/`verify` reine Verifikationsschritte sind und nie `complete_step` aufrufen.
- Fix: `IMPLEMENTING_KINDS = TOOL_STEP_KINDS`, die Schrittarten, die der Coder-Loop ausführt. Eine einzige Quelle, keine zweite Liste.

### BUG-184 – Abbruch eines Auftrags wartet auf die laufende Modellantwort samt Reparaturen
- Phase: Batterie-Lauf 1 auf 0.1.0rc14 (J6) · Komponente: coder/loop · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_coder_loop.py::test_a_cancel_interrupts_the_running_model_call`)
- Reproduktion: Job `bfb59aa6-8ab8-49de-8499-5bf037d7a20b` (J6): Der Abbruch wurde um ~14:52 angefordert (`job.control.cancel`). Der Coder lief noch zwei Modellaufrufe zu je 6144 Tokens (ungültig, Reparaturversuch) weiter; `cancelled` erst um 15:04.
- Ursache: Der Coder prüft das Abbruch-Token nur zu Beginn eines Zuges; ein Modellaufruf mit bis zu zwei Reparaturen kann bei einer Endlosausgabe ~15 min dauern.
- Fix: Der Modellaufruf eines Zuges läuft gegen das Abbruch-Token; ein Abbruch bricht den laufenden HTTP-Aufruf ab und beendet den Versuch sofort als `cancelled`. `CancelToken.wait_cancelled()` ist neu.

### BUG-183 – Planer-Hinweis zu neuen Manifesten strich das Paket, das der Auftrag ausdrücklich verlangt
- Phase: Batterie-Lauf 1 auf 0.1.0rc14 (J4) · Komponente: planner/validation · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_a_new_manifest_in_a_repository_without_one_gets_advice_once`)
- Reproduktion: Job `20c71889-2881-45a6-97c1-ae710366709e` (J4, „Binde das Python-Paket "hermclaw-nonexistent-sdk-zz9" ein …“, `REPLAN_LIMIT` nach 35,0 min, erwartet: sauberes Blockieren in höchstens 30 min). Gemmas erster Plan legte requirements.txt an. Nach dem Hinweis aus BUG-178 plante Gemma ohne Manifest, ebenso in beiden Replans. Der Coder konnte das Paket so nie installieren: `install_dependencies` lieferte NOT_FOUND, das Anlegen der Datei wurde als SCOPE_VIOLATION abgewiesen, die Scope-Erweiterung abgelehnt, dann folgte ein Replan. Die Installation, die das Fehlen des Pakets gezeigt hätte, kam nie zustande.
- Ursache: Der Hinweis erkennt einen verlangten Paketnamen nur am Dateinamen des Manifests im Auftragstext. Der Satz „wenn der Auftrag jedes Paket nennt, antworte unverändert“ reichte Gemma nicht.
- Fix: Nennt der Auftrag einen Namen in Anführungszeichen (`"…"`, `„…“`, `` `…` ``, `'…'`, `«…»`) und nennt der Schritt (Titel/Ziel) denselben Namen, entfällt der Hinweis. Der Abgleich behandelt `-`, `_` und `.` gleich. Für nicht genannte Pakete bleibt der Hinweis.

### BUG-182 – Coder sieht nicht, welche geforderten Dateiänderungen schon erfolgt sind; Schreiben mit gleichem Inhalt gab die Prüfungen wieder frei
- Phase: Batterie-Lauf 1 auf 0.1.0rc14 (J3) · Komponente: context_builder/render, coder/loop · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_context_builder_sections.py::test_acceptance_shows_which_required_changes_are_still_missing`, `tests/integration/test_coder_loop.py::test_checks_are_withheld_after_a_refused_repeat_until_a_file_changes`)
- Reproduktion: Job `016b19c0-7669-4434-aa17-a5839b68ed02` (J3, `REPLAN_LIMIT` nach 54,8 min). Der Plan forderte in vier diff-Kriterien Änderungen an `inventory/config.py`, `inventory/server.py`, `tests/test_config.py` und `tests/test_server.py`. In allen drei Plan-Versionen waren die Tests grün (8 passed), `tests/test_server.py` blieb aber bis zum letzten Versuch unverändert, und `complete_step` kam nie. Der Coder variierte stattdessen Testbefehle. In Versuch 1 schrieb er dreimal `inventory/config.py` mit identischem Inhalt („already has this content; nothing written“).
- Ursache: Der Kontext listet die Abnahmekriterien, aber nicht, welche davon der aktuelle Workspace schon erfüllt. Das Modell konnte weder erkennen, dass eine geforderte Datei fehlt, noch dass alles erfüllt ist. Zusätzlich galt ein erfolgreiches `write_file` ohne Änderung als Änderung und hätte die nach BUG-181 zurückgehaltenen Prüfungen wieder freigegeben.
- Fix: Jedes diff-Kriterium mit `must_change` trägt im Abschnitt ACCEPTANCE den Stand gegen den Workspace: „met (already changed)“ oder „NOT YET CHANGED: <Pfade>“. Grundlage sind geänderte und unversionierte Dateien, abgeglichen wie im Verifier. Ein Schreiben mit `changed: false` gibt die Prüfungen nicht frei.

### BUG-181 – Coder schlägt nach einer verweigerten Wiederholung dieselbe Prüfung immer wieder vor
- Phase: Batterie-Lauf 1 auf 0.1.0rc14 (J1) · Komponente: coder/loop · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_coder_loop.py::test_checks_are_withheld_after_a_refused_repeat_until_a_file_changes`)
- Reproduktion: Job `2adc4aec-ee1d-4995-b0dc-cedc6582a894` (J1, `succeeded` in 20,2 min, MR erstellt). Versuch 1: Nach einem `replace_text` ohne `path`/`old` (ARGS_INVALID) schlug der Coder `run_test` viermal in Folge vor, jedes Mal `COMMAND_REPEATED`; die Stagnationserkennung stoppte den Versuch nach Heavy Review. Versuch 2: Die Tests waren grün, trotzdem kamen drei verweigerte Wiederholungen vor `complete_step`. Das Batterie-Kriterium „repeats“ (4, max. 2) scheiterte. Derselbe Fehler ließ J2 (`35125cf6-a64a-4c14-9523-47479887fea5`, 77,2 min) mit `REPLAN_LIMIT` scheitern: In drei Plan-Versionen waren die Tests grün (7 passed), der Coder schlug aber dreimal in Folge denselben `run_test` vor, statt `complete_step` aufzurufen. Jeder Versuch endete in der Stagnation.
- Ursache: Die Verweigerung erklärt, dass sich nichts ändern kann, aber das Werkzeug blieb im Katalog, und das Modell griff weiter danach.
- Fix: Nach einer verweigerten Wiederholung von `run_test`/`run_command` stehen beide nicht mehr im Werkzeugkatalog des nächsten Zuges. Ein Hinweis sagt, warum, und bietet Ändern, `complete_step` oder `block_step` an. Jede erfolgreiche ändernde Aktion (Datei, Installation) bringt sie zurück; Lesen nicht.

### BUG-180 – Verify-Schritt mit übernommenem `must_change` scheitert nach korrekter Umsetzung
- Phase: Batterie-Lauf 1 auf 0.1.0rc14 (J1) · Komponente: planner/enrich · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_planner_enrich.py::test_a_verify_step_never_demands_changes_it_cannot_make`)
- Reproduktion: Job `2adc4aec…`: Gemma gab dem Verify-Schritt S002 das diff-Kriterium von S001 (`must_change` main.py, test_main.py, `allow_empty` false). S001 war korrekt fertig; S002 scheiterte mit „expected changes … but none were found; the change is empty“ → Replan.
- Ursache: Ein Schritt, der keine Workspace-Dateien ändert, kann ein diff-Kriterium mit geforderten Änderungen nie erfüllen; die Validierung prüft `must_change` nur bei ändernden Schritten.
- Fix: Die Anreicherung entfernt bei nicht ändernden Schritten (verify, review, research, remote) geforderte Änderungen aus diff-Kriterien. Haltbare Teile bleiben: `must_not_change` und `max_changed_files`, mit leerer Änderung erlaubt. Kriterien ohne haltbaren Rest fallen weg.

### BUG-179 – Coder liest unveränderte Dateien wiederholt und hält einen eigenen falschen Testsollwert für einen Codefehler
- Phase: Batterie-Lauf 1 auf 0.1.0rc13 (J6, J3, J5) · Komponente: tools/engine, stagnation · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_read_write.py::test_identical_read_of_an_unchanged_file_is_refused_within_the_context_window`, `tests/unit/test_stagnation_actions.py::test_forced_diagnosis_and_strategy_switch`, `::test_none_and_warning_decisions`)
- Reproduktion: Job `35ec82fa-54ea-46c7-b730-328dfa086a01` (J6 „Fehlerbericht Gesamtmenge“). Der Coder schrieb den Test `test_total_quantity_non_empty` mit Sollwert 9 für 5 + 3, korrigierte `total_quantity` richtig auf `sum(...)` und las danach `inventory/store.py` in vier Zügen hintereinander erneut („verify the fix“), ohne etwas zu ändern; zwei Replans, `REPLAN_LIMIT`. Ebenso J3 (dreimal derselbe `read_range`).
- Ursache: Der Kontext jedes Zuges zeigt die im Versuch gelesenen Dateien bereits (BUG-171), doch nichts hinderte das erneute Lesen. Die Stagnationsstrategie für „Tests weiter rot nach einer Änderung“ war dieselbe allgemeine wie für Schleifen; dass der eigene Test falsch sein kann, kam nicht vor.
- Fix: Ein Lesezugriff, der einem der letzten 6 Züge gleicht, während sich keine Datei geändert hat, wird mit `READ_REPEATED` (als Wiederholung, der echte letzte Fehler bleibt stehen) verweigert, mit der Aufforderung zu handeln. Neue Strategie `recheck_test_expectation` für die Ursache `test_after_change`: fehlschlagende Assertion gegen das STEP GOAL prüfen und einen falschen Sollwert im Test korrigieren; die Stagnationswarnung nennt das ebenfalls.

### BUG-178 – Planner plant unverlangte Abhängigkeiten trotz Regel; Replanner erfährt nichts von der Verweigerung
- Phase: Batterie-Lauf 1 auf 0.1.0rc13 (J1) · Komponente: planner (validation, failure_package, prompt) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_a_new_manifest_in_a_repository_without_one_gets_advice_once`, `tests/integration/test_planner_replan.py::test_failure_package_contents`)
- Reproduktion: Job `19648586-7711-4f91-9baa-054fb963d0c0` (J1 „Health-Endpunkt mit Test“, leeres Repository). Gemma plante Flask mit `requirements.txt` (Annahme „Flask and pytest are acceptable choices“). `install_dependencies` wurde neunmal mit `DEPENDENCY_NOT_REQUESTED` verweigert (BUG-174). Der erste Replan sah nur den `ModuleNotFoundError` und plante Flask erneut; erst der zweite wechselte auf `http.server`, verlangte aber weiter `requirements.txt` in `must_change`. `REPLAN_LIMIT` nach 52,7 min.
- Ursache: Die Einfachheitsregel steht im Planner-Prompt, wird aber nicht geprüft. Das Replan-Paket enthielt Verifier-, Test- und Befehlsevidenz, aber keine Verweigerungen der Runtime-Policy.
- Fix: Ein Plan, dessen Step ein Abhängigkeitsmanifest (`requirements.txt`, `package.json`) anlegt, das das Repository nicht hat und der Auftrag nicht nennt, bekommt einmalig einen Reparaturhinweis: Pakete nur, wenn der Auftrag sie nennt, sonst Standardbibliothek. Derselbe Plan danach wird angenommen, weil welche Pakete gemeint sind nur in der Prosa steht. Gilt für Planung und Neuplanung. Das Replan-Paket enthält `deterministic_evidence.runtime_refusals` (Policy-Verweigerungen des Auftrags, je Werkzeug und Code einmal), und der Replanner-Prompt verlangt einen Plan ohne die verweigerte Aktion.

### BUG-177 – Ausgabe von `node --test` wird nicht ausgewertet („0 passed, 0 failed“)
- Phase: Batterie-Lauf 1 auf 0.1.0rc13 (J5) · Komponente: tools/testparse · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/unit/test_tools_testparse.py::test_parse_counts_auto_detects`, `::test_failing_node_test_run_reports_its_counts`)
- Reproduktion: Job `d4551f37-ad83-42c2-b443-e2a38d32465c` (J5 „Node-Funktion mit Tests“): Jeder rote Lauf meldete `generic: failed – 0 passed, 0 failed, 0 errors, 0 skipped (exit code 1)`, obwohl die TAP-Ausgabe `# pass 1 / # fail 1` enthielt; der Coder sah keine Zählung des fehlschlagenden Tests.
- Ursache: Es gab Parser für pytest, unittest, jest, vitest, mocha, phpunit, go und cargo, aber keinen für den eingebauten Node-Test-Runner (TAP ohne TTY, Spec-Reporter mit `ℹ`).
- Fix: Parser `node` für die Zusammenfassungszeilen `tests/pass/fail/cancelled/skipped/todo` beider Reporter; automatisch erkannt.

### BUG-176 – Ausfall des Modell-Hosts beendet Aufträge sofort endgültig; Batterie wertet ihn als erwartetes Scheitern
- Phase: Batterie-Lauf auf 0.1.0rc11 mit AVX2 (J2–J6) · Komponente: scheduler, acceptance · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_scheduler.py::test_model_outage_defers_planning_and_replanning_instead_of_failing`, `tests/integration/test_acceptance_jobs.py::test_metrics_of_a_live_like_failed_job_fail_the_criteria`)
- Reproduktion: Am 2026-10-17 um 01:13 lief der LVM-thin-Pool `local-lvm` auf tower3 voll, QEMU pausierte VM 104 (.224, `io-error`). Der Replan von J2 hing bis zum 1200-s-Timeout und scheiterte danach mit `MODEL_UNAVAILABLE`; J3–J6 scheiterten je nach etwa 25 s in der Vorbereitung. J4 (erwartet: Scheitern) wurde als PASS gezählt.
- Ursache: Der Scheduler behandelte jeden Fehler von Planung und Neuplanung als endgültig, auch wenn nur der Modell-Host fehlte. Die Batterie prüfte nur den Endstatus, nicht den Grund.
- Fix: Scheitern Planung oder Neuplanung mit `MODEL_UNAVAILABLE`, `MODEL_TIMEOUT` oder `MODEL_LOAD_FAILED`, behält der Auftrag seine Phase; sie läuft nach Backoff (60/120/300/600 s) erneut, höchstens 4 h lang, mit Statuszeile „Modell nicht erreichbar … erneut versucht“. Danach scheitert der Auftrag wie bisher. Die Batterie hat das Kriterium `infrastructure`: Ein Auftrag, der mit einem Infrastrukturfehler endet, besteht nie. Betrieb: Der Pool wurde durch Umzug der gestoppten VMs 101/224 auf die HDD (Storage `hdd`) entlastet.

### BUG-175 – Verweigerte Wiederholung verdrängt den echten Testfehler aus dem Kontext; doppelte Kriterien in Verify-Schritten
- Phase: Batterie-Lauf auf 0.1.0rc11 mit AVX2 (J2) · Komponente: coder/loop, tools/engine, planner/enrich · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_coder_loop.py::test_refused_repeat_keeps_the_real_failure_in_the_next_prompt`, `tests/unit/test_planner_enrich.py::test_duplicate_criteria_are_dropped_from_the_enriched_plan_of_a_verify_step`)
- Reproduktion: Job `137c9c41-06c8-46fd-99e3-c4cade7785bd` (J2 „POST /items mit Validierung“). Zweiter Versuch: Tests geschrieben, `run_test` rot (3 Fehler), danach fünfmal derselbe Befehl, jedes Mal `COMMAND_REPEATED` („Running unit tests to verify the current state“), bis die Stagnationserkennung den Step stoppte → Replan. Beide Planversionen trugen im Verify-Schritt 29 identische leere diff-Kriterien.
- Ursache: Die Coder-Schleife setzte LATEST FAILURE auf das Ergebnis des jüngsten Werkzeugaufrufs. Die Verweigerung ersetzte so den Traceback, und jeder erfolgreiche Lesezugriff löschte ihn ganz; das Modell führte die Tests erneut aus, um den Fehler wiederzusehen. Die Deduplizierung der Akzeptanzkriterien (BUG-170) wurde nur übernommen, wenn zugleich Kriterien ergänzt wurden – bei Verify-Schritten nie.
- Fix: Wiederholungsverweigerungen (`COMMAND_REPEATED`, Timeout-Wiederholung, `INSTALL_UNCHANGED`) und erfolgreiche Lesewerkzeuge lassen den letzten echten Fehler stehen; die Verweigerung erscheint als Hinweis darüber. Die Verweigerung unterscheidet „lief grün → fehlende Änderung machen oder complete_step“ von „lief rot → Ausgabe steht in LATEST FAILURE, Code oder Tests korrigieren“. Die deduplizierte Kriterienliste gilt für jeden Step-Typ.

### BUG-174 – Modelle führen Frameworks und Pakete ein, die der Auftrag nicht verlangt (Durchsetzung)
- Phase: Batterie-Lauf auf 0.1.0rc11 mit AVX2 (J1) · Komponente: tools/engine (`install_dependencies`), coder/handler, Planner-Prompt, Coder-Vertrag · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_new_dependencies_only_when_the_job_names_them`, `tests/integration/test_tools_engine_exec.py::test_manifest_packages_and_mentions`)
- Reproduktion: Job `d50dc59e-c88e-4d66-876f-71323f4c5081` (J1 „Erstelle einen funktionierenden Health-Endpunkt mit Test“, erstmals `succeeded`, 10,3 min): Der Coder legte `requirements.txt` mit fastapi/uvicorn an – trotz Planner-Regel „simplest solution“ (BUG-169). Frühere Jobs scheiterten genau an solchen Abhängigkeiten (fehlendes httpx, pytest-Pins, Installationsschleifen).
- Ursache: Die Einfachheitsregel war nur ein Prompt; nichts hinderte die Modelle an unnötigen Abhängigkeiten.
- Fix: `install_dependencies` installiert in einem Repository, das zu Schrittbeginn kein Manifest hatte, nur, wenn der Auftrag (Titel, Prompt, Constraints) mindestens eines der Pakete nennt; sonst `DEPENDENCY_NOT_REQUESTED` mit Verweis auf Standardbibliothek und vorhandene Test-Runner. Planner-Prompt und Coder-Vertrag nennen die Regel. Bestehende Manifeste bleiben frei erweiterbar.

### BUG-173 – Wiederholungssperre verweigerte Tests nach erfolgreicher Installation
- Phase: Batterie-Lauf auf 0.1.0rc11 (J1) · Komponente: tools/engine · Severity: P3 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_a_successful_install_allows_rerunning_the_same_command`)
- Reproduktion: Job `d50dc59e…`, Zug 11 `install_dependencies` erfolgreich, Zug 12 `run_test python -m pytest -q` → `COMMAND_REPEATED` (Fix BUG-171), obwohl sich die Umgebung (`/deps`) geändert hatte; der Coder wich auf `-v` aus.
- Ursache: Die Sperre zählte nur Workspace-Dateiänderungen.
- Fix: Eine erfolgreiche Installation verwirft die gemerkten Befehlsergebnisse.

### BUG-172 – DNS nach Neustart weg: statische IPv4 ohne Nameserver, DNS nur aus IPv6-Router-Advertisements
- Phase: Betrieb, Neustart von .222/.224 für die CPU-Umstellung (2026-10-17) · Komponente: infra/ansible (Rolle common) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (Rolle `common`, `tasks/dns.yml`; live: Sandbox-Suite auf .222 nach dem Fix 29/29)
- Reproduktion: Nach dem Neustart von VM 102 (.222) schrieb dhcpcd eine `/etc/resolv.conf` ohne Nameserver (IPv4 statisch in `/etc/network/interfaces`, DNS kam bisher nur per IPv6-RA). Folge: Image-Pulls und der Egress-Proxy scheiterten, die Sandbox-Suite übersprang alle 29 Tests („no non-loopback nameserver“); doctor meldete trotzdem 19 ok.
- Ursache: Die Basisrolle verwaltete DNS nicht; die Hosts hingen vom zeitlichen Eintreffen der IPv6-RAs ab.
- Fix: `common_dns_servers` (Inventar: LAN-Router) landen in `/etc/resolv.conf.head`, die dhcpcd jeder erzeugten `resolv.conf` voranstellt; die Rolle prüft danach, dass ein Nicht-Loopback-Nameserver vorhanden ist (geteilte Hosts wie .223 ausgenommen).

### BUG-171 – Coder ohne Gedächtnis über Züge: liest dieselben Dateien und wiederholt grüne Testläufe
- Phase: Live-Auftragsbatterie auf 0.1.0rc9 (J2) · Komponente: context_builder, tools/engine, Coder-Vertrag · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_context_builder_relevance.py::test_files_read_in_this_attempt_stay_in_the_context`, `tests/integration/test_tools_engine_exec.py::test_identical_command_on_unchanged_files_is_not_rerun`)
- Reproduktion: Job `50784df5-d1b3-41c0-b200-853729b28219` (J2 „POST /items mit Validierung“, Fixture `acceptance/service`). Erster Versuch: neunmal `read_file` auf dieselben drei Dateien („Reading store.py to understand the current implementation“), kein Edit → Stagnation. Zweiter Versuch: achtmal lesen, dann `server.py` geschrieben, dreimal dieselbe grüne Testsuite („Running tests to verify current state“), keine neuen Tests. Nach dem Replan viermal dieselbe Suite vor jeder Änderung.
- Ursache: Jeder Zug bekommt einen frischen Kontext; Ergebnisse erfolgreicher Werkzeugaufrufe erscheinen nur als kurzer Digest im Verlauf. Gelesene Dateien außerhalb der Scope-Ziele verschwinden, und die Vertragsregel „read a file before changing it“ führte zu erneutem Lesen auch der gezeigten Ziel-Dateien. Identische Befehle auf unverändertem Arbeitsbaum liefen beliebig oft.
- Fix: Dateien, die im Versuch gelesen wurden, stehen in RELEVANT CODE/TESTS (Arbeitsmenge). Coder-Vertrag: gezeigte Dateien direkt bearbeiten, nicht erneut lesen. Ein identischer `run_test`/`run_command` ohne Dateiänderung seit dem letzten Lauf wird mit dem früheren Ergebnis abgelehnt (`COMMAND_REPEATED`) und nennt den nächsten sinnvollen Schritt.

### BUG-170 – Erfüllter Step scheitert am fehlenden Test-Kriterium des Plans; Korrekturen können es nie beheben
- Phase: Live-Auftragsbatterie auf 0.1.0rc9 (J1) · Komponente: planner/enrich, coder/handler · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_enrich.py::test_steps_that_write_tests_get_test_evidence_even_with_substantive_acceptance`, `tests/integration/test_coder_handler.py::test_missing_test_criterion_goes_to_the_replanner_not_to_a_correction`, `tests/unit/test_planner_enrich.py::test_identical_acceptance_criteria_are_dropped`)
- Reproduktion: Job `04820ff9-05f3-44ec-bc16-a9ac2a51d986` (J1, 0.1.0rc9): Erstplan mit 2 Steps, ohne Recherche, Code und Test in einem Step (BUG-168/169 wirken). Der Coder schrieb `main.py`/`test_main.py`, installierte, `pytest` → 1 passed, `complete_step` im ersten Versuch. Der Verifier lehnte ab: „implement step in a repository with tests has no test evidence: add a test criterion“ – der Plan hatte nur diff-Evidence. Drei Korrekturversuche liefen grüne Tests erneut bis zur Stagnation, zwei Replans erzeugten wieder Pläne ohne Test-Kriterium → `REPLAN_LIMIT`. In J2 (Job `50784df5…`) wiederholte Gemma zudem ein leeres diff-Kriterium 28-mal.
- Ursache: Die Anreicherung ergänzte ein Test-Kriterium nur, wenn der Plan gar keine substantielle Akzeptanz hatte und das Repository einen erkannten Testbefehl besaß; der Verifier verlangt es aber für jeden Umsetzungs-Step in einem Repository mit Tests (auch mit erst im Step angelegten Tests). Ein solcher Plan-Defekt wurde wie ein Code-Fehler in Korrekturen geschickt (Ursache A6 der Analyse).
- Fix: Die Anreicherung ergänzt Umsetzungs-Steps, die Tests schreiben oder in einem Repository mit Tests arbeiten, um ein Test-Kriterium (Testbefehl des Repositorys, sonst der Standard-Runner der Sprache: `python -m pytest -q`, `node --test`, `go test ./...`). Verifier-Fehler, die nur der Plan beheben kann (kein Test-Kriterium), gehen ohne Korrekturversuch an den Replanner (`ACCEPTANCE_DEFECT`). Identische Akzeptanzkriterien werden zusammengefasst.

### BUG-169 – Planner trennt Code und Test in eigene Steps und wählt unnötig komplexe Technik (alle Live-Jobs)
- Phase: Ursachenanalyse nach 0.1.0rc8 (Ursachenanalyse `docs/analysis/live-job-root-causes-2026-10-16.md`, U2/U3) · Komponente: planner/validation, planner/prompt, Coder-Vertrag · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_tests_for_new_code_belong_in_the_same_step`, `tests/unit/test_planner_loop.py::test_live_first_plan_is_repaired_into_one_step_without_web_research`, `tests/unit/test_planner_prompt.py::test_research_is_for_external_facts_not_the_repository_itself`)
- Reproduktion: Alle Erstpläne (rc1–rc8) folgen „Implement endpoint“ → „Implement test“ → „Verify“ mit 5–7 Steps für einen Ein-Datei-Auftrag. Der Umsetzungs-Step konnte nicht testen, der Test-Step den Code nicht korrigieren (rc7: S007 wartete drei Versuche auf einen Test, den nur S009 ändern durfte). In rc4/rc5/rc7/rc8 wählten die Modelle FastAPI; dessen TestClient braucht das nicht deklarierte `httpx` – Ursache der Abhängigkeitsschleifen BUG-161/162/165.
- Ursache: Keine Regel „Code und seine Tests in einem Step“; keine Präferenz für die einfachste ausreichende Lösung (nur „Prefer the standard library“ bei den Abhängigkeitsregeln).
- Fix: Validierung: ein Step, der nur Tests schreibt, darf nicht von einem (nicht abgeschlossenen) Umsetzungs-Step mit Nicht-Test-Code abhängen – Reparaturhinweis „merge into one step“. Planner-Prompt: ein Step je Änderung inklusive Tests, Verify-/Review-Steps nur bei Zusatznutzen; „simplest solution that fully meets the job“ (Standardbibliothek und vorhandene Werkzeuge, Abhängigkeiten nur mit Begründung als Annahme). Coder-Vertrag entsprechend.

### BUG-168 – Web-Recherche für Fragen über das eigene Repository und die Technologiewahl (früheste Fehlentscheidung aller Live-Jobs)
- Phase: Ursachenanalyse nach 0.1.0rc8 (Ursachenanalyse `docs/analysis/live-job-root-causes-2026-10-16.md`, U1) · Komponente: planner/validation, research/questions, tools/engine (`request_research`) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_research_about_the_repository_or_a_technology_choice_is_rejected`, `tests/unit/test_planner_validation.py::test_external_research_questions_stay_allowed`, `tests/integration/test_tools_engine_exec.py::test_request_research_refuses_repository_and_technology_choice_questions`)
- Reproduktion: Jeder Erstplan enthielt Research-Steps wie „What is the programming language and web framework used in this repository?“ (von der Runtime aus `research_needed` erzeugt). SearXNG lieferte Fremdprojekte (Next.js/Ruby/Rust, Go/iris, ASP.NET, „vanillajs“ → Node-Server in rc6) und Popularitätsrankings (FastAPI + `rest-health`); Suchanfragen enthielten Platzhalter („in [framework]“). rc8: der Coder verbrauchte sein Research-Budget für „most popular web frameworks“ und forderte dann einen Replan an. Der Prompt-Fix BUG-144 hatte das nicht verhindert.
- Ursache: Nur eine Prompt-Regel, keine Durchsetzung.
- Fix: Deterministische Prüfung `hermclaw/research/questions.py` (Fragen über das eigene Repository/Projekt/die Umgebung, über eine abgeleitete Technik „identified framework“, Technologiewahl, unaufgelöste Platzhalter). Plan-Validierung lehnt solche `research_needed`-Einträge und Research-Steps mit Reparaturhinweis ab; `request_research` lehnt sie ab (`RESEARCH_REFUSED`, ohne das Budget zu verbrauchen). Externe Fragen (Dokumentation, APIs, Fehlermeldungen) bleiben erlaubt.

### BUG-167 – Projekteigene Pakete in der Sandbox nur mit `python -m pytest` importierbar
- Phase: Live-Abnahme 0.1.0rc8 (Ursachenanalyse `docs/analysis/live-job-root-causes-2026-10-16.md`, U5) · Komponente: worker/execution (Sandbox-Umgebung) · Severity: P2 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_sandbox_command_line.py::test_the_repository_root_is_importable_in_every_container`, live: `tests/integration/test_egress_live.py::test_live_project_package_is_importable_with_plain_pytest`)
- Reproduktion: Job `0a98a5eb-d83a-41cb-840f-c013f1b453f0`, S006: `pytest app/test_main.py` → `from app.main import app` → `ModuleNotFoundError: No module named 'app'`. Der Coder hielt es für ein Abhängigkeitsproblem (`pip install -e .`, neue Pins) → Stagnation.
- Ursache: Plain `pytest` setzt das Arbeitsverzeichnis nicht auf `sys.path`; `PYTHONPATH` enthielt nur `/deps/python`.
- Fix: Jeder Container hat `PYTHONPATH=/workspace` (mit Abhängigkeitsverzeichnis `/workspace:/deps/python`).

### BUG-166 – Verifier lehnt erfüllten Step ab: `must_change: ["*"]` passte nur auf Wurzeldateien
- Phase: Ursachenanalyse nach 0.1.0rc8 (Ursachenanalyse `docs/analysis/live-job-root-causes-2026-10-16.md`, U4) · Komponente: verifier/evidence, planner/validation · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_verifier_engine.py::test_file_name_globs_in_diff_evidence_match_in_any_directory`, `tests/unit/test_planner_validation.py::test_must_change_has_to_match_the_files_of_the_step`)
- Reproduktion: Job `e3cab55e-422d-46ab-86f0-c28dc78227c5`, S003, 15:23:21: der einzige `complete_step` aller acht Live-Jobs (`src/app.js` angelegt) wurde mit „[diff] expected changes matching ['*'] but none were found“ abgelehnt; die folgende Korrektur hatte nichts zu korrigieren und geriet in die Server-Schleife (BUG-164) → `REPLAN_LIMIT`.
- Ursache: Akzeptanz-Globs des Planners wurden mit Scope-Semantik ausgewertet (`*` überquert kein `/`); die Plan-Validierung prüfte nicht, ob `must_change` auf die Pfade des Steps passen kann.
- Fix: Akzeptanz-Globs (diff, presence, absence) ohne `/` gelten wie bei gitignore für den Dateinamen in jeder Tiefe; Literalpfade bleiben exakt. Plan-Validierung: jeder `must_change`-Glob muss auf einen Pfad aus `repo_hints`/`allowed_new_paths` des Steps passen können.

### BUG-165 – Coder installiert das unveränderte Manifest wieder und wieder statt das fehlende Paket einzutragen
- Phase: Live-Abnahme nach Deployment 0.1.0rc7 · Komponente: tools/engine (`install_dependencies`, Befehlsausgabe), Coder-Vertrag · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_reinstalling_an_unchanged_manifest_is_refused_and_missing_modules_are_named`, `tests/integration/test_tools_engine_exec.py::test_missing_modules_from_python_and_node_output`, `tests/unit/test_planner_prompt.py::test_prompts_explain_how_dependencies_get_into_the_sandbox`)
- Reproduktion: Job `837dcd50-839f-4f50-b61a-689c6eb4e6fb` (Testauftrag „/health-Endpunkt“, 0.1.0rc7, 2026-10-16). BUG-163/164 traten nicht mehr auf. S002: `requirements.txt` mit fastapi/uvicorn, Test mit `fastapi.testclient` → `ModuleNotFoundError: No module named 'httpx'`; der Coder rief `install_dependencies` sechsmal mit unverändertem Manifest auf → `STAGNATION`. Nach dem Replan trug S007 („Abhängigkeiten ergänzen“) httpx ein (Import-Fehler behoben), wartete dann aber über drei Versuche auf einen Assertion-Fehler im Test, den erst S009 beheben durfte, und installierte erneut, statt `complete_step` aufzurufen → `REPLAN_LIMIT`.
- Ursache: Eine Installation des unveränderten Manifests lief jedes Mal komplett durch (≈ 20 s, „Successfully installed …“ wirkte wie Fortschritt); die Testausgabe nannte das fehlende Paket nicht als Installationsfrage; der Vertrag sagte nicht, dass ein erreichtes Step-Ziel abgeschlossen wird, auch wenn Tests außerhalb des Scopes noch scheitern.
- Fix: `install_dependencies` lehnt ein seit der letzten erfolgreichen Installation (im selben Versuch) unverändertes Manifest mit `INSTALL_UNCHANGED` ab. Läufe, die an `No module named 'x'` / `Cannot find module 'x'` scheitern, enden mit einem Hinweis, das Paket ins Manifest einzutragen (projekteigene Module ausgenommen). Coder-Vertrag: `complete_step`, sobald Ziel und Akzeptanz erfüllt sind; Fehler außerhalb des Scopes im Summary nennen.

### BUG-164 – Server im Vordergrund blockieren `run_test` je 600 s, wiederholt (Stunden verloren)
- Phase: Live-Abnahme nach Deployment 0.1.0rc6 · Komponente: tools/engine (Befehlsausführung), Coder-Vertrag · Severity: P2 · blockierend: nein (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_a_command_that_timed_out_is_not_rerun_on_an_unchanged_workspace`, `tests/unit/test_planner_prompt.py::test_prompts_explain_how_dependencies_get_into_the_sandbox`)
- Reproduktion: Job `e3cab55e-422d-46ab-86f0-c28dc78227c5` (Testauftrag „/health-Endpunkt“, 0.1.0rc6, 2026-10-16). Der Planner wählte Node (leeres Repository). Der Coder rief in S003/S007 über mehrere Versuche mehr als 15-mal `node src/app.js` (ein HTTP-Server) bzw. `node src/app.test.js` (Test ohne `server.close()`) mit `run_test` auf; jeder Lauf endete erst nach dem vollen Timeout von 600 s („timed out after 600.2s (killed)“ mit „Health endpoint test passed!“ in stdout). Die Stagnationserkennung griff erst nach mehreren Wiederholungen.
- Ursache: Die Timeout-Meldung erklärte nicht, warum der Befehl nicht endete, und derselbe Befehl durfte auf unverändertem Arbeitsbaum beliebig oft erneut blockieren.
- Fix: Ein Timeout liefert den Hinweis, dass Befehle von selbst enden müssen (Server/Watcher/REPL nie starten; Tests starten die App im Prozess auf Port 0 und schließen sie). Derselbe Befehl (gleiches cwd, gleicher Timeout) wird nach einem Timeout sofort mit `COMMAND_TIMEOUT` abgelehnt, solange sich keine Datei geändert hat. Coder-Vertrag um diese Regel ergänzt.

### BUG-163 – Uncommittete Datei als Repo-Hinweis: jede Änderung als „create“ abgelehnt (Stagnation)
- Phase: Live-Abnahme nach Deployment 0.1.0rc6 · Komponente: scope/engine · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_scope_engine.py::test_uncommitted_repo_hint_target_stays_creatable`, `tests/integration/test_scope_expansion.py::test_untracked_leftover_file_is_granted_as_creation`)
- Reproduktion: Job `e3cab55e-422d-46ab-86f0-c28dc78227c5`. Der abgebrochene Step S003 hinterließ `src/app.js` uncommittet im Arbeitsbaum. Die Replans erzeugten S007 und S010 mit `repo_hints: ["src/app.js"]` und leerem `allowed_new_paths`. Der Scope enthielt die Datei als Ziel mit nur „modify“; Werkzeug-Guard und Verifier werten gegen die Step-Basis (HEAD), wo die Datei fehlt, also als „create“ → jedes `replace_text` wurde mit `SCOPE_VIOLATION: operation 'create' not allowed` abgelehnt, `request_scope_expansion` meldete „already in scope“ → `STAGNATION`, `REPLAN_LIMIT`. Ebenso in S007: die Erweiterung um das liegengebliebene `src/app.test.js` mit „create“ wurde als „modify“ bewilligt – Schreiben blieb verboten.
- Ursache: Der Fix zu BUG-158 erlaubte „create“ nur für bestehende Dateien aus `allowed_new_paths`, nicht für Ziele aus Repo-Hinweisen, die git nicht verfolgt.
- Fix: Die Workspace-Auflistung merkt sich untracked Dateien (`git ls-files -t`); jedes Ziel, das git nicht verfolgt, erlaubt „create“ für genau diesen Pfad, und die Scope-Erweiterung bewilligt untracked Dateien als Neuanlage (`allowed_new_paths`).

### BUG-162 – Coder wiederholt `pip install` in `run_command` statt `install_dependencies` (Stagnation)
- Phase: Live-Abnahme nach Deployment 0.1.0rc5 · Komponente: tools/engine (Befehlsprüfung) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_tools_engine_exec.py::test_package_installs_in_commands_are_refused_without_running`, `tests/unit/test_tools_units.py::test_package_install_detection`)
- Reproduktion: Job `b0080372-df78-461f-af3a-e91bb7479088` (Testauftrag „/health-Endpunkt“, 0.1.0rc5, 2026-10-16 12:30). S002 nutzte `install_dependencies` korrekt (BUG-160/161 wirken: Proxy-Start sofort, keine Toolchain-Überdeckung), `httpx` fehlte im Manifest. Nach dem Replan rief der Coder in S006 über vier Versuche 25-mal `pip install …` in `run_command`/`run_test` auf (je 9 s Netz-Timeout; Ausgabe „Requirement already satisfied: fastapi … in /deps/python“ wirkte wie ein Teilerfolg), nie `install_dependencies` → `STAGNATION`, `REPLAN_LIMIT`. Das Werkzeug war angeboten.
- Ursache: Paket-Installationen ohne Netz liefen bis in die Sandbox; der Hinweis im Systemvertrag allein reichte dem Modell nicht.
- Fix: `run_command`/`run_test` erkennen Paket-Installationen (`pip install`, `python -m pip install`, `uv pip install`, `poetry add`, `npm install/ci/i`, `yarn/pnpm add`, `apt-get install` …, auch in `bash -c`) und lehnen sie ohne Netz sofort mit `PACKAGE_INSTALL_REFUSED` ab – mit dem Weg, der funktioniert (Manifest + `install_dependencies`, sonst Standardbibliothek/`block_step`). Steps mit Netz und der Runtime-Befehl von `install_dependencies` sind ausgenommen.

### BUG-160 – Erster Start des Egress-Proxys läuft in den Timeout, obwohl der Container läuft
- Phase: Live-Abnahme nach Deployment 0.1.0rc4 · Komponente: worker/execution (`engine_exec`) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_sandbox_command_line.py::test_engine_exec_returns_when_the_engine_exits_even_if_a_daemon_keeps_the_pipes`)
- Reproduktion: Job `bec4c5e9-2382-4e49-8c19-0621bed965eb` (Testauftrag „/health-Endpunkt“ auf `hermclaw-test`, 0.1.0rc4, 2026-10-16 11:22). Erster Aufruf von `install_dependencies` (S002, Zug 9): Netze angelegt 11:23:40, Proxy-Container läuft ab 11:23:40 (`podman ps`: „Up“, Log „listening“), aber `podman run -d` kehrt nicht zurück → nach 300 s `EGRESS_PROXY_FAILED: cannot start the egress proxy: podman run timed out after 300s`; der Coder forderte deshalb einen Replan an. Spätere Installationen nutzten den laufenden Proxy und gelangen.
- Ursache: `engine_exec` wartete mit `communicate()` auf das Ende beider Pipes. Beim ersten Bridge-Netz startet rootless Podman einen dauerhaften Hintergrundprozess (pasta/rootless-netns), der stdout/stderr erbt – das Ende der Pipes kommt nie. Im Live-Test existierte dieser Prozess schon (aus dem Prototyp), daher nicht aufgefallen.
- Fix: `engine_exec` nutzt dieselbe Prozessüberwachung wie die Sandbox-Befehle (`_supervise`/`wait_exit`): Rückkehr, sobald der Engine-Prozess selbst beendet ist; Restausgabe höchstens 2 s.

### BUG-161 – Installierte Pakete überdecken die Test-Toolchain des Images (pytest-Pin bricht pytest)
- Phase: Live-Abnahme nach Deployment 0.1.0rc4 · Komponente: tools/engine (`install_dependencies`), Coder-Vertrag · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_egress_live.py::test_live_install_keeps_the_image_test_toolchain`, `tests/integration/test_tools_engine_exec.py`, `tests/unit/test_planner_prompt.py::test_prompts_explain_how_dependencies_get_into_the_sandbox`)
- Reproduktion: Job `bec4c5e9…`, S006/S009: `TestClient` scheiterte an `ModuleNotFoundError: No module named 'httpx'`; der Coder installierte mehrmals neu, ohne `httpx` ins Manifest zu schreiben, ergänzte dann `httpx==0.25.1` **und `pytest==7.4.3`**. Danach: `ImportError: cannot import name '_console_main' from '_pytest.config' (/deps/python/…)`, mit `python -m pytest` ebenso (`pytest_asyncio` des Images: `cannot import name 'FixtureDef' from 'pytest'`). Job `failed` mit `REPLAN_LIMIT`.
- Ursache: `/deps/python` steht über `PYTHONPATH` vor den site-packages des Images; ein Paket, das das Image schon mitbringt (pytest, seine Plugins), wird teilweise ersetzt und bricht die Toolchain. Außerdem blieben Pakete früherer Installationen liegen (`--upgrade --target` entfernt nichts).
- Fix: Der Installationsbefehl leert `/deps/python`, schreibt die Pakete des Images (ohne `PYTHONPATH` gelistet, ohne pip/setuptools/wheel) als pip-Constraints und installiert mit `-c`: gleiche Versionen sind erlaubt, ein widersprechender Pin scheitert mit pips `ResolutionImpossible`; das Werkzeug-Ergebnis erklärt dann, dass Image-Pakete fest sind. Der Coder-Vertrag sagt: fehlendes Modul zuerst ins Manifest, dann neu installieren; Test-Werkzeuge des Images nicht pinnen. Live auf .222 geprüft (Pin scheitert, ohne Pin läuft der `TestClient`-Test).

### BUG-158 – Liegengebliebene Datei eines abgebrochenen Versuchs: Scope erlaubt nur „modify“, die Schreibprüfung verlangt „create“
- Phase: Live-Abnahme nach Deployment 0.1.0rc3 · Komponente: scope/engine · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_scope_engine.py::test_declared_new_file_left_in_the_work_tree_stays_creatable`)
- Reproduktion: Job `c04f9fb8-fa18-4416-847a-cb7f593a9404` (erster Testauftrag „/health-Endpunkt“ auf `hermclaw-test`, 0.1.0rc3, 2026-10-16 08:35). S002 legte `src/main.py` und `src/requirements.txt` an und forderte dann einen Replan an (Versuch ohne Commit, Dateien bleiben untracked im Work Tree). Der Replan-Step S007 deklarierte beide in `allowed_new_paths`. Scope v1: `target_paths=[src/main.py, src/requirements.txt]`, `allowed_operations=[modify]` („already exists: promoted to target_paths“). Jede Änderung → `SCOPE_VIOLATION: operation 'create' not allowed`, nach 4 Runden `STAGNATION`.
- Ursache: Die Scope-Engine prüft die Existenz im Work Tree (tracked + untracked), Tool-Guard und Verifier beurteilen Operationen gegen die Step-Baseline (HEAD), in der die Dateien fehlen → „create“.
- Fix: Für deklarierte neue Dateien, die schon im Work Tree liegen, bleibt „create“ erlaubt (zusätzlich zu „modify“). Der Guard lässt „create“ nur für ausdrücklich gelistete Ziel-Dateien zu; der Scope bleibt explizit.

### BUG-159 – Gemma kann übersprungene Scope-Felder im Replan nicht nachtragen (Grammatik in Schema-Reihenfolge)
- Phase: Live-Abnahme nach Deployment 0.1.0rc3 · Komponente: planner (Modell-Schema) · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_loop.py::test_model_facing_schemas_require_the_scope_fields`)
- Reproduktion: Job `c04f9fb8-fa18-4416-847a-cb7f593a9404` (erster Testauftrag „/health-Endpunkt“ auf `hermclaw-test`, 0.1.0rc3, 2026-10-16 08:35), zweiter Replan: drei Antworten, in denen S009/S010 nur `id … risk, rerun_reason` enthalten; `rerun_reason` endet in der Schleife „I will also include 'src/main.py' in allowed_new_paths …“ bis zur Längengrenze. Die gesammelte Fehlermeldung (BUG-153) kam an, wurde aber nicht umgesetzt → `PLANNER_INVALID_OUTPUT`.
- Ursache: Ollama erzeugt aus dem JSON-Schema eine Grammatik, die Eigenschaften in Schema-Reihenfolge ausgibt und optionale überspringen lässt. `repo_hints`/`allowed_new_paths` stehen vor `risk`/`rerun_reason`; einmal übersprungen, sind sie im selben Objekt nicht mehr erreichbar.
- Fix: Das an das Modell gesendete Schema (Plan und Replan) verlangt `depends_on`, `repo_hints`, `allowed_new_paths` und `acceptance` (leere Liste erlaubt). Die Contracts selbst und die Validierung bleiben unverändert.

### BUG-153 – Replan-Validierung meldet Fehler stufenweise: Repair-Budget verbraucht, bevor Gemma die Scope-Fehler sieht
- Phase: Live-Abnahme nach Deployment 0.1.0rc2 · Komponente: planner/replanner · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/integration/test_planner_replan.py::test_replan_reports_rule_and_scope_errors_in_one_repair_turn`)
- Reproduktion: Job `eadf1b6f-3a82-475c-bbca-c92acd878d6f` (erneuter erster Testauftrag „/health-Endpunkt“ auf `hermclaw-test`, 0.1.0rc2, 2026-10-16 02:31). S002 (Coder) forderte einen Replan an.
- Ablauf (`events` `planner.repair`): Versuch 1 → Schemafehler (`S002_NEW` ist keine gültige Step-ID); Versuch 2 → nur die Replan-Regel („rerun_reason is only allowed for completed steps“, 5×); Versuch 3 → erst jetzt die semantischen Fehler („step S002: implement step names no files to work on …“) → keine Reparatur mehr übrig → `PLANNER_INVALID_OUTPUT`, Job `failed`. Gemma hat jede gemeldete Fehlerklasse korrekt behoben.
- Erwartet: Eine Repair-Runde nennt alle prüfbaren Fehler.
- Ursache: `Replanner._validator` brach nach den Replan-Regeln ab, ohne den zusammengeführten Plan semantisch zu prüfen.
- Fix: Replan-Regelfehler, Schemafehler des zusammengeführten Plans und semantische Fehler werden gemeinsam gemeldet (abgeschlossene Steps ohne `rerun_reason` werden dafür aus dem Kandidaten genommen). Nur ein Schemafehler der Modellantwort selbst bleibt eine eigene Runde (ohne gültiges Schema ist nichts prüfbar).

### BUG-154 – `rerun_reason` an einem nicht abgeschlossenen Step kostet eine Repair-Runde
- Phase: Live-Abnahme nach Deployment 0.1.0rc2 · Komponente: planner/replanner · Severity: P2 · blockierend: nein (Teil von BUG-153) · Status: **behoben** (gleicher Test)
- Reproduktion: wie BUG-153, Versuch 2: Gemma setzte `rerun_reason` an S002–S006 (blockiert, nicht abgeschlossen).
- Erwartet: Das Feld wirkt nur bei abgeschlossenen Steps; an anderen Steps ist es bedeutungslos und wird nie gespeichert (`merge_plan` verwirft es).
- Fix: An nicht abgeschlossenen Steps wird `rerun_reason` ignoriert statt abgelehnt. Abgeschlossene Steps ohne Begründung bleiben ein Fehler.

### BUG-155 – Sandbox-Image eines leeren Repositorys bleibt für den ganzen Job das generische Image
- Phase: Live-Abnahme nach Deployment 0.1.0rc2 · Komponente: runtime/images · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_sandbox_images.py::test_empty_repository_gets_the_toolchain_once_it_has_code`)
- Reproduktion: Job `eadf1b6f-3a82-475c-bbca-c92acd878d6f` (erneuter erster Testauftrag „/health-Endpunkt“ auf `hermclaw-test`, 0.1.0rc2, 2026-10-16 02:31). Das Repository enthält nur `README.md`; S002 legt `app/main.py` an und ruft `python3 -m pytest` auf → `No module named pytest` (`hermclaw-sandbox-generic:1` statt `hermclaw-sandbox-python:1`).
- Ursache: `SandboxImageSelector.image_for` cachte auch das Ergebnis „keine Sprache erkannt“ (`None`) pro Workspace.
- Fix: Nur eine tatsächliche Auswahl (Repository-Image oder Sprach-Alias) wird gecacht; ohne erkannte Sprache wird beim nächsten Executor neu gefragt.

### BUG-156 – Replan-Trigger nennt einen nur durch Abhängigkeit blockierten Step als fehlgeschlagen
- Phase: Live-Abnahme nach Deployment 0.1.0rc2 · Komponente: runtime/driver · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_runtime_driver.py::test_failed_step_keys_put_the_cause_before_its_dependents`)
- Reproduktion: Job `eadf1b6f-3a82-475c-bbca-c92acd878d6f` (erneuter erster Testauftrag „/health-Endpunkt“ auf `hermclaw-test`, 0.1.0rc2, 2026-10-16 02:31). S002 blockiert mit `CODER_REQUESTED_REPLAN`, S003/S004 mit `DEPENDENCY_FAILED`; `replan.started` meldete `failed_step: S004`. Gemmas Zusammenfassung lautete entsprechend „the previous plan failed because S004 depended on S003 …“.
- Fix: `driver.failed_step_keys` ordnet: fehlgeschlagene Steps, dann blockierte mit eigenem Fehler, zuletzt `DEPENDENCY_FAILED`.

### BUG-157 – Planner und Coder wissen nicht, dass die Sandbox offline ist und keine Pakete installiert
- Phase: Live-Abnahme nach Deployment 0.1.0rc2 · Komponente: planner/prompt, context_builder · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/unit/test_planner_prompt.py::test_prompts_state_that_the_sandbox_cannot_install_packages`)
- Reproduktion: Job `eadf1b6f-3a82-475c-bbca-c92acd878d6f` (erneuter erster Testauftrag „/health-Endpunkt“ auf `hermclaw-test`, 0.1.0rc2, 2026-10-16 02:31). Der Coder plante FastAPI, rief `pip install pytest` auf (`pip: not found`, Sandbox ohne Netz und read-only) und forderte einen Replan an.
- Fix: Planner-Regel (Code-Steps laufen offline mit fester Toolchain, Standardbibliothek und deklarierte Abhängigkeiten bevorzugen, neue Drittabhängigkeit braucht ein Projekt-Image des Betreibers → in assumptions/risks) und Coder-Systemvertrag (keine Paketinstallation; fehlt eine Abhängigkeit: Standardbibliothek oder `block_step` mit Namen). Keine Sonderfälle, Modellrollen unverändert. Neue Drittabhängigkeiten in einem neuen Projekt bleiben eine Betreiberaufgabe (Projekt-Image, `infra/sandbox-images/README.md`, R-022).

### BUG-152 – Abgelehnter Push meldet nur „see stderr“ statt der Begründung des Servers
- Phase: Live-Abnahme (Kapitel 12, Suite `gitops`, 2026-10-16) · Komponente: gitops · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_gitops_errors_hint.py`, `tests/integration/test_gitops_push.py::test_server_side_refusal_names_the_servers_reason`)
- Reproduktion: `scripts/ops/live-acceptance.sh --suite gitops` gegen `local-ai/hermclaw-test`, dessen Deploy-Key kein Schreibrecht hat.
- Erwartet: Die Fehlermeldung (Job-Fehler, UI, Test) nennt den Grund.
- Tatsächlich: `PushRejectedError: push of 'hermclaw/…' was rejected: see stderr`; der Grund („remote: This deploy key does not have write access to this project.“) stand nur in `details.stderr`.
- Fix: `gitops.engine.push_stderr_hint` zieht die `remote:`-Zeilen (sonst die `fatal:`/`error:`-Zeile, auch deutsche Locale) redigiert und auf 300 Zeichen gekürzt in die Meldung.

### BUG-151 – WoL-Live-Test schlägt fehl, solange Wake-on-LAN bewusst deaktiviert ist
- Phase: Live-Abnahme (Suite `wol`) · Komponente: tests/wol · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_wol_live.py::test_live_probes`, `::test_live_magic_packet`)
- Reproduktion: `hosts.yaml` mit `wake_on_lan.enabled: false` (WoL noch nicht eingerichtet, Platzhalter-MAC), `live-acceptance.sh --suite wol`.
- Erwartet: Probes (Ping/SSH/API/Dienste) laufen; das Magic Packet wird mit Grund übersprungen.
- Tatsächlich: `AssertionError: host has no Wake-on-LAN configuration`, die Probes liefen nie.
- Fix: Test geteilt. `test_live_probes` prüft Konfiguration und Erreichbarkeit immer, `test_live_magic_packet` überspringt bei `enabled: false` mit Grund und verlangt bei `enabled: true` eine echte MAC.

### BUG-150 – Planner-Fallback `gemma4:12b` lädt mit 32K Kontext nicht auf der GTX 1080 (CUDA-Allokationsfehler)
- Phase: Live-Abnahme (Suite `models`, `test_live_planner_fallback_alias_serves`) · Komponente: models/config · Severity: P2 · blockierend: nein (nur der technische Fallback des Planners ist betroffen) · Status: **behoben im Code/Config**, Produktivwirkung erst nach dem nächsten Deployment (`tests/integration/test_models_litellm_proxy.py::test_gpu_layers_reach_ollama_as_num_gpu`, `tests/unit/test_models_profiles.py::test_example_fallback_caps_gpu_layers`)
- Reproduktion (auf .224, auch bei leerer GPU): `curl -s localhost:11434/api/generate -d '{"model":"gemma4:12b","prompt":"Reply with: ok","stream":false,"options":{"num_ctx":32768}}'` → `CUDA error: the resource allocation failed`. Mit `num_ctx` 8192/16384 oder `num_gpu: 30` lädt das Modell (≈15 s); mit `num_gpu: 40` meldet llama.cpp `cudaMalloc failed: out of memory` („Gemma4Assistant requires ctx_other…“ in der Speicherschätzung). Ollama 0.40.4 überschätzt, wie viele Layer passen.
- Erwartet: Der Fallback antwortet mit dem konfigurierten 32K-Kontext (CPU-Offload für den Rest).
- Fix: Neues optionales Profilfeld `gpu_layers` (→ Ollama `num_gpu`; ungesetzt = Ollama entscheidet wie bisher). Der Gateway sendet es pro Request, `litellm_config` setzt es als Deployment-Default; empirisch gegen echten LiteLLM 1.104.2 geprüft. `config/models.example.yaml`: `planner-gemma-fallback.gpu_layers: 30`.
- Live-Nachweis ohne Produktionseingriff: temporärer LiteLLM-Proxy mit der neuen Konfiguration gegen das echte Ollama auf .224 → `planner-gemma-fallback` antwortet `ok` (32K).
- Offen: Deployment des Orchestrators (neues Release), damit `/etc/hermclaw/models.yaml` und `litellm.yaml` das Feld bekommen.
- Nebenbefund (kein Fehler, dokumentiert): `ModelResidency` (P08 8.10) ist nicht in die Runtime verdrahtet; Modellwechsel macht Ollama selbst (`OLLAMA_MAX_LOADED_MODELS=2`). Planner 26b, Coder 30b und Heavy 27b (24K) laden live korrekt.

### BUG-149 – `podman-compose` fehlt auf dem Execution Worker; Compose-Sandbox meldet INFRA_SANDBOX_UNAVAILABLE
- Phase: Live-Abnahme (Suite `infratools`, `test_live_sandbox_on_execution_worker`) · Komponente: deployment (Ansible-Rolle podman) · Severity: P2 · blockierend: nein · Status: **behoben** und auf .222 angewendet (`tests/integration/test_bootstrap_infra.py::test_bootstrap_playbook_structure`, `::test_role_input_validation` Fälle `podman-extra-packages*`)
- Reproduktion: `compose_up_sandbox(..., host="exec-222")` → `no usable container engine: no compose implementation installed (docker compose, docker-compose, podman-compose, podman compose)`.
- Ursache: Die podman-Rolle installierte kein `podman-compose`, obwohl die Container-Tools (P28) und `known-limitations.md` darauf bauen.
- Fix: `podman_extra_packages` (validiert, Default `[]`), im Play „Execution worker“ `[podman-compose]`. Angewendet mit `playbooks/bootstrap.yml --limit hermclaw-exec-222 --tags podman` → `podman-compose version 1.3.0`.

### BUG-148 – SearXNG-Live-Test nutzt eine feste LAN-Adresse statt der deployten URL
- Phase: Live-Abnahme (Suite `research`) · Komponente: tests/research · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_research_fetch_search.py::test_live_searxng_on_target_host`)
- Reproduktion: `live-acceptance.sh --suite research` → `SearchUnavailable: SearXNG unreachable: ConnectError` auf `http://192.168.178.225:8888`.
- Ursache: SearXNG lauscht wie vorgesehen nur auf `127.0.0.1:8888` (`policies.research.searxng_url`); der Test hatte die LAN-Adresse fest eingetragen. Die Research des ersten Testauftrags lief über die richtige URL.
- Fix: Default aus `load_config().policies.research.searxng_url`, `HERMCLAW_LIVE_SEARXNG_URL` überschreibt weiterhin.

### BUG-147 – Docker-Adapter kann unter einer rootless Engine nicht in den Workspace schreiben
- Phase: Live-Abnahme (Suite `sandbox` auf .222, `test_docker_adapter_flags_run_on_podman_cli`) · Komponente: worker/execution sandbox · Severity: P3 · blockierend: nein (Produktion nutzt den Podman-Adapter) · Status: **behoben** (`tests/unit/test_sandbox_command_line.py::test_docker_user_follows_the_engine_rootless_state`, `::test_docker_name_answered_by_podman_is_parsed_as_podman`, verschärfter Live-Test)
- Reproduktion: `DockerSandbox(executable="podman")` als `hermclaw` (rootless) → `echo d > docker.txt` scheitert („can't create docker.txt“); der alte Test verdeckte das, weil der Befehl mit `;` verkettet war (Exit 0).
- Ursache: (1) `--user=<Host-uid des Besitzers>` ist nur bei rootful Docker richtig; rootless wird diese uid auf eine subuid ohne Schreibrecht abgebildet. (2) `docker info --format '{{json .}}'` liefert bei Podman (auch beim `podman-docker`-Shim) Podmans Format; `parse_docker_info` meldete dann immer „rootful“.
- Fix: `health()/preflight()` merken den Rootless-Zustand; rootless läuft der Container als uid 0 (= Workspace-Besitzer, ohne Capabilities, no-new-privileges). `engine_info` erkennt Podman-Format hinter dem Docker-Namen.

### BUG-146 – Abnahmeskript: Sandbox-Suite prüft nichts und meldet trotzdem PASS
- Phase: Live-Abnahme (Suite `sandbox` auf .222) · Komponente: scripts/ops/live-acceptance.sh · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_live_acceptance_script.py::test_sandbox_suite_runs_every_podman_test_not_only_the_live_one`, `::test_junit_counts_feed_the_skip_classification`)
- Reproduktion: `sudo scripts/ops/live-acceptance.sh` auf .222 → `1 skipped, 24 deselected`, Summary `PASS  sandbox`.
- Ursache: (1) `-m live` deselektierte 24 der 25 Podman-Tests, obwohl genau sie die Live-Verifikation auf .222 sind (Modul-Docstring). (2) Das Testimage `alpine:3.20` fehlte im Speicher des Dienstbenutzers; Kapitel 12 erwähnte es nicht. (3) Eine Suite mit ausschließlich übersprungenen Tests galt als PASS.
- Fix: Marker je Suite (`sandbox`: `integration or live`), Testimage wird bei Bedarf als Dienstbenutzer gezogen und danach wieder entfernt, Ergebnis aus der JUnit-XML: `SKIP (all N test(s) skipped)` bzw. `PASS (N tests, M skipped)`; `-rs` zeigt die Skip-Gründe. Danach auf .222: 25 passed.

### BUG-145 – Dev-venv aus Kapitel 12 kann die Testsuite nicht sammeln
- Phase: Live-Abnahme (Vorbereitung) · Komponente: pyproject/tests · Severity: P2 · blockierend: ja für lokale Regressionstests auf den Hosts (behoben) · Status: **behoben**
- Reproduktion: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest --collect-only tests` als Nicht-root auf .225.
- Tatsächlich: (1) `ModuleNotFoundError: No module named 'aiohttp'` in 22 Testmodulen (Fake-Server); in der Build-Umgebung war aiohttp nur zufällig installiert. (2) `tests/integration/test_backup_live.py` wertet `Path('/etc/hermclaw/backup.yaml').is_file()` beim Import aus; `/etc/hermclaw` ist `0750` → `PermissionError` bricht die ganze Sammlung ab.
- Fix: `aiohttp>=3.10` im Extra `dev`; der Backup-Live-Test behandelt eine unlesbare Konfiguration als „nicht vorhanden“ (Skip mit Grund). Danach: `3336 tests collected`, keine Sammelfehler.

### BUG-144 – Planner plant Web-Research über das eigene Repository
- Phase: Live-Abnahme (erster Testauftrag `4af00159…`) · Komponente: planner/prompt · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_planner_prompt.py::test_research_is_for_external_facts_not_the_repository_itself`)
- Reproduktion: Job „/health-Endpunkt mit Test“ auf dem README-only-Repository `hermclaw-test`.
- Tatsächlich: Gemma legte `research_needed: "What is the programming language and web framework used in this repository?"` an; die Web-Suche lieferte widersprüchliche Aussagen über fremde Projekte (Next.js, Ruby, Rust) und kostete ≈5 min.
- Fix: Regel in Planner- und Replanner-Prompt: Research nur für externe Fakten; Fakten über das Repository kommen aus `repository_inventory`/`retrieved_context`; ein (fast) leeres Repository hat noch keinen Stack – einen passenden wählen und als Annahme nennen.

### BUG-143 – Erster Testauftrag scheitert: Plan mit Implement-Steps ohne jede Scope-Quelle wird angenommen
- Phase: Live-Abnahme (erster Testauftrag `4af00159-69f9-49c1-9527-8741dea3f631`, 2026-10-15 23:50) · Komponente: planner/validation · Severity: P1 · blockierend: ja (behoben) · Status: **behoben** (`tests/unit/test_planner_validation.py::test_workspace_steps_without_any_scope_source_are_rejected`, `::test_checking_step_error_names_candidate_dependencies`, `tests/unit/test_planner_loop.py::test_unscoped_implement_step_is_repaired_before_it_can_block`)
- Reproduktion: Repository `hermclaw-test` (nur `README.md`), Auftrag „Füge einen /health-Endpunkt hinzu, der {"status": "ok"} liefert, mit Test.“
- Ablauf (Events/`model_invocations`): Gemma plante S003/S004 (`implement`) ohne `repo_hints` und ohne `allowed_new_paths` (nur `diff.must_change: ["*"]`). Die semantische Validierung nahm den Plan an. S003 wurde nach ≈6 min Research mit `SCOPE_UNAVAILABLE / no_resolvable_scope` blockiert (`journalctl -u hermclaw-scheduler`: „scope generated … scope_status unavailable“). Der Replan (Gemma) lieferte vier Steps ganz ohne `depends_on`; der Fehler „step S010: verify step must depend on the step(s) it checks“ kam dreimal identisch zurück → `PLANNER_INVALID_OUTPUT`, Job `failed`.
- Erwartet: Ein Plan, dessen schreibende Steps keine Dateien nennen, wird schon beim Planen abgelehnt; die Repair-Meldung sagt, was zu tun ist.
- Ursache: Die Scope-Engine baut den Scope ausschließlich aus `repo_hints`, `allowed_new_paths` und konkreter `presence`-Evidence (Scope bleibt explizit); die Validierung prüfte nicht, dass ein workspace-schreibender Step mindestens eine dieser Quellen hat. Die Abhängigkeitsmeldung für Prüf-Steps nannte keine Kandidaten.
- Fix: `validation._scope_source_errors`: `implement`/`database`/`docker`/`documentation`-Steps ohne Hints, ohne neue Pfade und ohne `presence`-Evidence für eine noch nicht existierende Datei → semantischer Fehler mit Anleitung (im leeren Repository gehört jede Datei in `allowed_new_paths`). Die Meldung für review/verify ohne Abhängigkeit nennt die davor liegenden schreibenden Steps (`"depends_on": ["S007", "S008"]`). Keine Sonderfälle, keine Änderung an Scope-Engine oder Modellrollen.

### BUG-142 – The optional SSH gate cannot read its default allow-list as the runtime user
- Phase: D-013 (Koexistenz `.223`, Analyse) · Komponente: sshtools/deploy · Severity: P3 · blockierend: nein (das Gate ist standardmäßig nicht aktiv, `common_ssh_forced_command: ""`) · Status: **behoben** (`tests/unit/test_sshtools_gate.py::test_default_allowlist_is_readable_outside_the_config_dir`, `::test_gate_reads_a_root_owned_allowlist_as_an_unprivileged_user`)
- Reproduktion:
  1. On a bootstrapped target host, set `common_ssh_forced_command: /usr/local/bin/hermclaw-ssh-gate` and a
     non-empty `hermclaw_release_ssh_gate_allowlist`.
  2. Run bootstrap and deploy.
  3. Run `ssh hermclaw-admin@<host> uptime` from `.225` with an allow-list rule `^uptime$`.
- Erwartet: the gate runs `uptime`.
- Tatsächlich: the gate runs as `hermclaw-admin` and reads `/etc/hermclaw/ssh-allowlist`. `/etc/hermclaw` is
  `0750 root:hermclaw` (`roles/common/defaults/main.yml`, `common_directories`), and `hermclaw-admin` is not in the
  group, so `[ -f "$ALLOWLIST" ]` fails. Every command is rejected with `allowlist-missing` (exit 126).
- Workaround: keep the gate off (default). To keep the runtime away from a host completely, remove it from
  `policies.ssh.allowed_hosts` (`hermclaw_config_overrides`, RISK R-038).
- Fix (umgesetzt): move the allow-list to a root-owned file the runtime user can reach, e.g.
  `/etc/hermclaw-ssh-allowlist`. That means changing the gate's default path, `hermclaw_release_ssh_gate_allowlist_file`
  and `CRITICAL_REMOTE_FILES` in `hermclaw/sshtools/tools.py`. Add a test that runs the gate as a non-root user
  against the deployed layout.
- Umgesetzt: default path `/etc/hermclaw-ssh-allowlist` (root:root 0644) in the gate, `hermclaw_release_ssh_gate_allowlist_file` and `CRITICAL_REMOTE_FILES`; the gate test runs the script as `nobody` against a root-owned list and shows that a list inside a 0750 directory is refused as `allowlist-missing`.

### BUG-001 – CommandRequest.workspace path traversal on the execution daemon
- Phase: P07 · Komponente: workers · Severity: P0 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: Send a signed POST /v1/commands with workspace="/" (the id comes from the JSON body, so the router's path pattern never checks it).
- Erwartet: 400 WORKSPACE_ID_INVALID; the runner is never called.
- Tatsächlich: Before the fix the command ran with workspace_dir=/ (the probe printed '/'), and '..' addressed the data dir. Fixed: ExecutionService.workspace_dir() validates every id against WORKSPACE_ID_PATTERN, and the client validates too. Regression test: test_workers_execution_daemon.py::test_command_workspace_id_cannot_escape_workspaces_root, which fails on the old code.

### BUG-002 – RWLock counted as idle while a reader was still waiting; delete_workspace dropped it
- Phase: P07 · Komponente: workers · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: Reader A holds the lock, DELETE waits, reader B queues behind it. After the delete releases, B has been woken but has not run yet, and lk.idle was already True, so the lock was popped. A later upload got a fresh lock and could swap the tree under B's command.
- Erwartet: The lock stays registered while anyone holds or waits on it.
- Tatsächlich: Fixed: a _users counter is incremented synchronously on entry. Regression test: ::test_rwlock_waiting_reader_keeps_lock_registered, which fails on the old code.

### BUG-003 – a cancelled waiting writer left readers blocked
- Phase: P07 · Komponente: workers · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: Reader holds the lock, a writer waits, a second reader queues (writer preference), then the writer is cancelled (client disconnect during upload).
- Erwartet: The second reader proceeds.
- Tatsächlich: Before the fix the second reader hung until some unrelated notify. Fixed: notify_all when the waiting-writer count drops to 0. Regression test: ::test_rwlock_cancelled_writer_releases_waiting_readers, which fails on the old code.

### BUG-004 – concurrent first heartbeats of an unknown worker raised IntegrityError (HTTP 500)
- Phase: P07 · Komponente: workers · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: Four parallel transactions call ingest_heartbeat for the same new worker_id.
- Erwartet: One creates the row (one worker.registered event); the others update it.
- Tatsächlich: Fixed with INSERT ... ON CONFLICT DO NOTHING RETURNING followed by SELECT ... FOR UPDATE. Regression test: test_workers_registry.py::test_concurrent_first_heartbeats_register_once.

### BUG-005 – bearer token sent in clear over plain HTTP next to the HMAC signature
- Phase: P07 · Komponente: workers · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: WorkerRequestSigner defaulted to include_bearer=True on http:// LAN URLs.
- Erwartet: The shared secret never crosses an unencrypted link.
- Tatsächlich: Fixed: the default include_bearer=None sends the bearer only over https; verifiers still accept and check it when present. Regression test: test_workers_auth.py::test_request_signer_sends_bearer_only_over_https_by_default.

### BUG-006 – SignedRequestMiddleware passed websocket scopes through unauthenticated
- Phase: P07 · Komponente: workers · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: A websocket route added through extra_routers (media extension) received a handshake with no signature.
- Erwartet: Fail closed.
- Tatsächlich: Fixed: every websocket handshake is closed with code 1008. Regression test: test_workers_daemon_common.py::test_middleware_refuses_websocket_handshakes.

### BUG-007 – heartbeat body was read before the header-only auth checks
- Phase: P07 · Komponente: workers · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: An unsigned 256 KiB POST /api/workers/heartbeat returned 413 after buffering.
- Erwartet: 401 without reading the body; 413 only for signed requests.
- Tatsächlich: Fixed: the router runs precheck_signed_request, then reads the size-limited body, then verify_prechecked. Regression test: test_workers_api.py::test_unauthenticated_heartbeat_rejected_before_body_is_read.

### BUG-008 – graceful shutdown did not emit worker.offline; select_worker returned workers without api_url
- Phase: P07 · Komponente: workers · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: (1) The daemon sends its final offline heartbeat: only worker.state was emitted, so the scheduler waited for the sweep. (2) A heartbeat-only registered worker (api_url NULL) could be selected, and WorkerClient.for_worker then raised WORKER_UNREACHABLE.
- Erwartet: (1) worker.offline with reason=worker_shutdown, active_job and active_step. (2) Only dispatchable workers are selected.
- Tatsächlich: Both fixed. Regression tests: test_workers_registry.py::test_graceful_offline_heartbeat_emits_worker_offline, ::test_select_worker_skips_heartbeat_only_registrations_without_api_url.

### BUG-009 – Runtime git executed workspace-controlled config (filter/merge/diff drivers, remote.*.uploadpack, include.path, url.insteadOf, core.worktree) -> sandbox-to-orchestrator code execution
- Phase: P06 · Komponente: gitops · Severity: P0 · blockierend: ja · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: append [filter "evil"] clean="touch marker; cat" to <ws>/.git/config, add '* filter=evil' to .gitattributes, call engine.stage_allowed
- Erwartet: runtime refuses to run git in a tampered workspace
- Tatsächlich: FIXED: ops.integrity_problems allow-list check in engine._load / reader._path / cleanup -> WORKSPACE_TAMPERED (refused op + event); confirmed marker was created before the fix; regression tests in test_gitops_hardening.py and test_gitops_review.py

### BUG-010 – push force-with-lease against just-seen remote SHA overwrote foreign commits on the job branch
- Phase: P06 · Komponente: gitops · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: push job branch, reviewer pushes fix-up onto it, runtime commits again and pushes
- Erwartet: PUSH_REJECTED (foreign_commits), remote unchanged
- Tatsächlich: FIXED: non-fast-forward only if remote SHA was pushed by the runtime for this job or seen at workspace creation; test_push_refuses_to_overwrite_foreign_commits_on_the_job_branch

### BUG-011 – commit_verified accepted verification runs older than the workspace or a base rebase
- Phase: P06 · Komponente: gitops · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: create verification run, update_to_base, stage, commit with old run
- Erwartet: COMMIT_NOT_VERIFIED
- Tatsächlich: FIXED: freshness rule (run.created_at >= max(workspace.created_at, last ok base.rebase/merge)); concurrent commits with one run serialized via per-run lock

### BUG-012 – Operations validated workspace state before taking the lock (cleanup/update race); in-process lock dict grew unbounded
- Phase: P06 · Komponente: gitops · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: stage_allowed waiting on the lock while the workspace is cleaned
- Erwartet: WORKSPACE_STATE after lock acquisition
- Tatsächlich: FIXED: _locked() reloads the row inside the lock; KeyedLocks refcounted; test_operation_waiting_for_the_lock_sees_a_concurrent_cleanup

### BUG-013 – Workspace clones hardlinked mirror object files; info/attributes write followed symlinks; push used workspace-configurable 'origin'
- Phase: P06 · Komponente: gitops · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: stat object files (nlink>1); symlink .git/info/attributes to an outside file
- Erwartet: isolated objects, no writes outside the tree, push to registered URL
- Tatsächlich: FIXED: clone --no-hardlinks, symlink checks + O_NOFOLLOW write, push/ls-remote to repo.url; tests in test_gitops_hardening.py

### BUG-014 – Minor: scope.violation event payload not redacted; http(s) URLs with token-as-username accepted; GitLab X-Next-Page malformed/looping header; pushed workspace stayed 'pushed' after rebase; show_base_file used git show
- Phase: P06 · Komponente: gitops · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: see tests
- Erwartet: redacted/rejected/terminating/committed/raw blob
- Tatsächlich: FIXED with regression tests (test_scope_violation_event_payload_is_redacted, test_http_urls_with_userinfo_are_rejected, test_gitlab_pagination_stops_on_malformed_or_looping_header, test_rebasing_a_pushed_workspace_marks_it_committed_again); show_base_file now uses cat-file blob

### BUG-015 – Repair turn could echo prose or reasoning-like text from an unparseable answer
- Phase: P14/P24 · Komponente: planner · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: Model answers '{"goal":..} I considered deleting the tests.. {"note":1}'; the old json_candidate echoed everything from the first '{' to the last '}' as the assistant turn
- Erwartet: Only a parsed JSON object is echoed (redacted, canonical); nothing is echoed for unparseable text
- Tatsächlich: FIXED: loop.py echoes compact_json(redact(parsed)) only; json_candidate removed. Regression test: tests/unit/test_planner_loop.py::test_prose_or_reasoning_inside_a_broken_answer_is_never_echoed

### BUG-016 – Validation error lists were not redacted before plan_versions, events and repair turns
- Phase: P14/P24 · Komponente: planner · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: repo_hint 'token=ghp_...' appears verbatim in the error text
- Erwartet: Errors are redacted
- Tatsächlich: FIXED: loop._error_list runs DEFAULT_REDACTOR. Test: test_planner_loop.py::test_error_lists_are_redacted_everywhere

### BUG-017 – Hints to paths listed outside the inventory's files/paths keys were rejected as invented, though the prompt allows them
- Phase: P14/P24 · Komponente: planner · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: inventory {'entrypoints':['app/main.py']} and hint app/main.py: error 'does not exist' and a needless repair turn
- Erwartet: Every path that appears in repository_inventory grounds hints
- Tatsächlich: FIXED: inputs.collect_known_paths harvests path-shaped strings (values and keys) from the whole inventory and strips '::' test ids. Tests: test_planner_validation.py::test_paths_anywhere_in_the_inventory_ground_hints, test_planner_repos.py::test_inventory_paths_outside_files_ground_hints_end_to_end

### BUG-018 – Risk floor counted a directory or glob hint as one path
- Phase: P14/P24 · Komponente: planner · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: implement step with hint 'src/pkg/' covering 8 files and no test evidence stayed low risk
- Erwartet: high (touches >= 6 paths without tests)
- Tatsächlich: FIXED: enrich.touched_paths counts the known paths a glob or directory matches. Test: test_planner_enrich.py::test_directory_and_glob_hints_count_the_paths_they_match

### BUG-019 – step.network and CommandEvidence.network were not checked against the capability network policy
- Phase: P14/P24 · Komponente: planner · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: an implement/coding step with network=true was accepted
- Erwartet: Bauplan §30: network only via the step capability
- Tatsächlich: FIXED: new semantic rule plus a prompt rule. Test: test_planner_validation.py::test_network_only_for_capabilities_that_allow_it

### BUG-020 – Catch-all allowed_new_paths/repo_hints ('**', '*') were accepted
- Phase: P14/P24 · Komponente: planner · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: allowed_new_paths ['**'] passed validation
- Erwartet: Scope stays explicit
- Tatsächlich: FIXED: is_catch_all rule. Test: test_planner_validation.py::test_catch_all_patterns_are_rejected

### BUG-021 – Prompt redacted only the job goal, not the title or other job fields
- Phase: P14/P24 · Komponente: planner · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: a job title containing a token reached the prompt unredacted
- Erwartet: The whole job section is redacted
- Tatsächlich: FIXED. Test: test_planner_prompt.py::test_planner_payload_keys_redaction_and_test_command

### BUG-022 – risk_policy override with an unknown step kind was silently accepted; DiffEvidence globs were not path-checked
- Phase: P14/P24 · Komponente: planner · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: {'min_risk_by_kind': {'rollout': 'high'}}; diff must_change ['../x']
- Erwartet: ValidationFailed / semantic error
- Tatsächlich: FIXED. Tests: test_planner_enrich.py::test_invalid_risk_policy_overrides_are_rejected, test_planner_validation.py::test_diff_globs_must_be_repository_relative

### BUG-023 – extract_json let RecursionError escape on deeply nested model output
- Phase: P08 · Komponente: models · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den models-Tests)
- Reproduktion: extract_json('[' * 100000), or structured() where the model answers with 60k '[' characters
- Erwartet: ValueError, so the output is treated as invalid and a repair call follows
- Tatsächlich: RecursionError propagated, and the invocation was recorded as INTERNAL_ERROR. FIXED in gateway.py (RecursionError and the int-digit limit now map to ValueError, and the candidate scan is capped at 256 start positions). Regression tests: test_models_gateway.py::test_extract_json_hostile_inputs, test_structured_hostile_nesting_triggers_repair

### BUG-024 – Repair call could hit CONTEXT_OVERFLOW because of the echoed answer
- Phase: P08 · Komponente: models · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den models-Tests)
- Reproduktion: structured() on fast-router with a prompt of about 13.5K tokens where the first answer is invalid and long
- Erwartet: Repair still possible with the validation error alone
- Tatsächlich: Repeating the rejected answer (up to 6000 chars) pushed the request over the context window and raised ValidationFailed. FIXED: the echo is dropped when it does not fit (LiteLLMGateway._repair_conversation). Regression tests: test_structured_repair_drops_echo_when_context_is_tight, test_structured_repair_keeps_echo_when_it_fits

### BUG-025 – Failed unload of a conflicting group member left no event
- Phase: P08 · Komponente: models · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den models-Tests)
- Reproduktion: ensure_loaded('coder-main') while gemma4:26b stays resident after keep_alive=0
- Erwartet: Switch aborted, target not loaded, failure visible in the event log
- Tatsächlich: ModelError raised with no event recorded. FIXED: model.load.finished is now written with ok=false, phase=unload, error_code and severity error (the load-phase failure event now carries phase=load too). Regression test: test_models_failures.py::test_failed_conflict_unload_blocks_switch_and_is_evented

### BUG-026 – Empirical LiteLLM proxy tests skip in the repo venv
- Phase: P08 · Komponente: models · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den models-Tests)
- Reproduktion: .venv/bin/pytest tests/integration/test_models_litellm_proxy.py
- Erwartet: Tests run against a real proxy
- Tatsächlich: 12 tests skip because .venv lacks the litellm[proxy] extras. Test-infrastructure issue only; fix is the shared pyproject change listed above.

### BUG-027 – Docker adapter: docker CLI injects proxy env from ~/.docker/config.json 'proxies' into containers
- Phase: P18 · Komponente: sandbox · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den sandbox-Tests)
- Reproduktion: Configure 'proxies' in ~/.docker/config.json for the worker user, use policies.sandbox.engine=docker, run `env` in a command
- Erwartet: No host proxy variables in the container (podman gets --http-proxy=false)
- Tatsächlich: Docker has no flag to turn this off. The docs say not to configure 'proxies' for the worker user; podman, the preferred engine, is not affected

### BUG-028 – sh -lc (required by spec) resets PATH on Debian-based images via /etc/profile
- Phase: P18 · Komponente: sandbox · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den sandbox-Tests)
- Reproduktion: Use an image whose ENV PATH contains e.g. /opt/venv/bin and run `which <tool>` in the sandbox
- Erwartet: The image's PATH is preserved
- Tatsächlich: Debian's /etc/profile overwrites PATH with the standard directories. Workaround: ContainerSandbox(shell=('sh','-c')); this is documented in docs/architecture/sandbox.md section 4


### BUG-029 – Folge-Steps eines Jobs sahen die Commits früherer Steps als eigene Änderungen
- Phase: P23 (Integration) · Komponente: coder/handler + verifier · Severity: P1 · blockierend: ja · Status: **behoben**
- Reproduktion: Plan mit zwei Implement-Steps (S001 ändert src/module.py, S002 ändert src/util.py); nach dem Commit von S001 meldet der Verifier für S002 eine Scope-Verletzung (src/module.py), weil `changed_files` gegen die Job-Basis gerechnet wird.
- Erwartet: Jeder Step wird gegen den HEAD zu Step-Beginn geprüft.
- Fix: ImplementStepHandler setzt die Step-Baseline (`WorkspaceHandle.base_sha = workspace.head_sha`); Regression-Rerun nach Rebase prüft mit der Vereinigung aller Step-Scopes (`merge_scopes`).
- Regressionstest: tests/integration/test_coder_handler.py::test_two_implement_steps_with_real_verifier_commit_separately (schlägt ohne Fix fehl, verifiziert per Mutation).

### BUG-030 – Cross-member budget queue rule guesses why a waiter is blocked (requests carry no weight or exclusive flag)
- Phase: P09 · Komponente: resources · Severity: P3 · blockierend: nein · Status: **behoben** in P39 (`tests/integration/test_resources_budget_queue.py`: Requests speichern weight/exclusive/metadata, Migration 0002; abgebrochene/abgelaufene Requests emittieren `resource.request.cancelled`, zurückgezogene Preemption `resource.preempt.withdrawn`)
- Reproduktion: Budget {large, small}. A high-priority shared request on 'small' waits only because the shared holders there are not exclusive-compatible, not because of capacity. A lower-priority 'large' acquisition that would fit is then held back as budget_queued.
- Erwartet: A lower-priority request on another budget member is blocked only when the higher waiter actually waits for capacity.
- Tatsächlich: It is blocked whenever the higher waiter's resource has no exclusive holder. This is safe (it never lets a lower waiter overtake), but it can delay lower-priority work. Fixing it needs the shared schema change (weight and exclusive columns on resource_requests).

### BUG-031 – Primary domains listed with www. prefix (e.g. www.postgresql.org in policies.example.yaml) never matched
- Phase: P12 · Komponente: research · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den research-Tests)
- Reproduktion: classify_source('https://www.postgresql.org/docs/16/', ['www.postgresql.org'])
- Erwartet: official_docs, authority 0.95 (primary source)
- Tatsächlich: Fixed in this session (was documentation-path 0.75); regression test tests/failure/test_research_failures.py::test_primary_domain_listed_with_www_prefix_matches

### BUG-032 – SSRF guard treated RFC 8215 local-use NAT64 (64:ff9b:1::/48) addresses as public when their low 32 bits looked public
- Phase: P12 · Komponente: research · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den research-Tests)
- Reproduktion: is_public_address(ip_address('64:ff9b:1:7f00:1::808:808'))
- Erwartet: False (embedded IPv4 position depends on operator prefix length)
- Tatsächlich: Fixed in this session (was True); regression tests test_nat64_local_use_prefix_is_never_public / test_nat64_local_use_answer_is_blocked_by_fetcher

### BUG-033 – Raw source URLs (possibly containing tokens) were kept in ResearchOutcome.errors and research_sources.url
- Phase: P12 · Komponente: research · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den research-Tests)
- Reproduktion: search result URL with ?api_key=<secret> that fails to fetch
- Erwartet: secret redacted everywhere
- Tatsächlich: Fixed in this session: URLs and error extras pass through DEFAULT_REDACTOR; regression test test_secrets_in_question_and_urls_never_reach_events_db_or_prompts

### BUG-034 – Test fixture server ignored routes when the request carried a query string (2 SearXNG tests failed)
- Phase: P12 · Komponente: research · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den research-Tests)
- Reproduktion: pytest tests/integration/test_research_fetch_search.py
- Erwartet: route '/search' serves '/search?q=...'
- Tatsächlich: Fixed in tests/integration/test_research_support.py

### BUG-035 – Shared redactor misses secrets assigned to prefixed identifiers
- Phase: P11 · Komponente: repo_intelligence · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den repo_intelligence-Tests)
- Reproduktion: from hermclaw.core.redaction import DEFAULT_REDACTOR; DEFAULT_REDACTOR.text("DB_PASSWORD = '<literal value>'")
- Erwartet: the literal is masked
- Tatsächlich: returned unchanged (\bpassword\b does not match inside DB_PASSWORD). Repo intelligence works around it with hermclaw/repo_intelligence/redact.py::redact_code; other components using DEFAULT_REDACTOR on code are still affected.

### BUG-036 – Draining worker waited the whole service_timeout before CAPABILITY_MISSING
- Phase: P10 · Komponente: wol · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest tests/failure/test_wol_regressions.py)
- Reproduktion: ensure_worker_ready for a worker in state draining that lacks the required capability
- Erwartet: immediate failure CAPABILITY_MISSING, worker stays draining
- Tatsächlich: waited service_timeout_seconds (180 s default) before failing

### BUG-037 – Concurrent wake runs for one worker with different capability sets sent double magic packets and raced on state writes
- Phase: P10 · Komponente: wol · Severity: P2 · blockierend: nein · Status: **behoben** (per-worker asyncio.Lock; regression test in tests/failure/test_wol_regressions.py)
- Reproduktion: two ensure_worker_ready calls for the same worker with different required_capabilities at the same time
- Erwartet: one wake, one state writer; each run's budget starts when it acquires the worker
- Tatsächlich: two parallel runs, duplicate packets, interleaved state transitions

### BUG-038 – Failed quick probe on an awake worker with a live heartbeat flipped it to waking and then error
- Phase: P10 · Komponente: wol · Severity: P2 · blockierend: nein · Status: **behoben** (regression test in tests/failure/test_wol_regressions.py)
- Reproduktion: worker API crashed on an awake host (heartbeat state still live), ensure_worker_ready
- Erwartet: live state is kept, failure reported without a wake state change
- Tatsächlich: state set to waking, then error

### BUG-039 – wol_send retry loop ignored the overall deadline
- Phase: P10 · Komponente: wol · Severity: P3 · blockierend: nein · Status: **behoben** (regression test in tests/failure/test_wol_regressions.py)
- Reproduktion: deadline_seconds shorter than the packet retry schedule
- Erwartet: retries stop at the deadline
- Tatsächlich: retries continued past the deadline

### BUG-040 – Idle re-check after committing 'sleeping' ignored a heartbeat that moved the worker or reported active work
- Phase: P10 · Komponente: wol · Severity: P2 · blockierend: nein · Status: **behoben** (row-locked _recheck aborts the sleep; regression test in tests/integration/test_wol_idle.py)
- Reproduktion: heartbeat with an active job arrives between the idle decision and the sleep command
- Erwartet: sleep aborted
- Tatsächlich: worker was put to sleep anyway

### BUG-041 – Verifier secret scanner flagged secret *references* as secrets (systemd LoadCredential paths, cred: refs, "ghp_..." placeholders)
- Phase: P36 · Komponente: verifier · Severity: P1 · blockierend: ja (a deploy job writing systemd units with LoadCredential= would be blocked by its own verifier) · Status: **behoben** (regression cases in tests/unit/test_verifier_secrets.py::test_ordinary_code_is_not_reported; found by tests/security/test_security_checks.py::test_repository_contains_no_secrets)
- Reproduktion: SecretScanner().scan_line("x.service", 1, "LoadCredential=worker-token:/etc/hermclaw-worker/worker-token")
- Erwartet: no finding – the value is a path to the credential, not the credential
- Tatsächlich: secret_assignment finding; likewise for "api_token_ref: cred:…" and documentation placeholders like "token=ghp_..."

### BUG-042 – Planner rejected correct repo_hints as "invented": the driver took known paths from the prompt-sized inventory summary, which has no file list
- Phase: P37 · Komponente: runtime driver / repo_intelligence · Severity: P1 · blockierend: ja (any job whose goal text does not lexically hit the files it names failed preparation) · Status: **behoben** (RepoIntelligence.known_paths + driver uses it; regression: tests/e2e/test_e2e_code_tasks.py::test_37_7_typescript_utility)
- Reproduktion: TypeScript repo with src/strings.ts, job "Add a slugify(s) utility", Gemma plan with repo_hints ["src/strings.ts"]
- Erwartet: hint accepted (file exists)
- Tatsächlich: semantic repair "repo_hint 'src/strings.ts' does not exist in repository_inventory/retrieved_context", plan rejected; only files that retrieval happened to return were known

### BUG-043 – POST /api/jobs: repository auto-registration by URL crashes (HTTP 500) on a name collision and bypasses the registry validation
- Phase: P40 (found by the local soak) · Komponente: api/jobs · Severity: P2 · blockierend: nein · Status: **behoben** in P39 (`tests/integration/test_api_repository_registration.py`: `validate_remote_url`, eindeutige Namen bei gleichem URL-Ende, file:// nur mit admin)
- Reproduktion: two jobs with different repository URLs that share the basename, e.g. file:///a/remote.git and file:///b/remote.git → second POST /api/jobs fails with UniqueViolation uq_repositories_name (HTTP 500)
- Erwartet: the repository is registered through RepositoryRegistry (URL validation: no userinfo, allowed schemes) with a unique name (collision → suffix), or a clear 409/422
- Tatsächlich: _slug_name(url) is used as the unique name directly and the row is inserted without RepositoryRegistry checks

### BUG-044 – Staged renames reported as unexplained changes (reader dropped the rename origin)
- Phase: P39 · Komponente: scope · Severity: P1 · blockierend: nein · Status: **behoben** (Regressionstest: tests/integration/test_scope_rename_audit.py)
- Reproduktion: git mv legacy/old_module.py src/app/feature/old_module.py; ScopeAuditor.audit_status(step, await WorkspaceGitReader(...).status(ws))
- Erwartet: Rename judged by both paths: (legacy/old_module.py, delete) and (src/app/feature/old_module.py, create)
- Tatsächlich: Before 74ef977: violation 'staged rename without a known origin'. The source fix was already in HEAD; only the real-git regression test was missing (added now).

### BUG-045 – Escaped literal '[[]' in _glob_regex caused FutureWarning 'Possible nested set'
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_escaped_bracket_file_names_match_only_themselves)
- Reproduktion: path_matches('routes/[id].tsx', glob_escape('routes/[id].tsx')) with -W error::FutureWarning, using the pre-review guard.py
- Erwartet: Matches, no warning
- Tatsächlich: FutureWarning (an error under -W error). Already fixed in HEAD; regression tests added.

### BUG-046 – Odd path segments ('src//a.py', 'src/./a.py') were not canonicalised
- Phase: P39 · Komponente: scope · Severity: P1 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_odd_spelling_never_bypasses_a_forbidden_path (plus the related odd_spelling and normalise_path tests))
- Reproduktion: ScopeGuard(forbidden_paths=['lib/secret.py']).decide('lib//secret.py', 'modify') with the pre-review contracts/scope.py
- Erwartet: Judged as lib/secret.py, i.e. forbidden
- Tatsächlich: Pre-review: not recognised as the forbidden path. The path collapse was already in HEAD; tests added.

### BUG-047 – normalise_path('./') returned the absolute path '/', and ScopeGuard.decide('./') raised ValueError
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_normalise_path_rejects_invalid_paths, ::test_contract_rejects_root_and_nul_entries, ::test_invalid_paths_are_refused_not_raised)
- Reproduktion: normalise_path('./'); ScopeContract(allowed_new_paths=['./']); ScopeGuard(c).decide('./', 'create')
- Erwartet: ValueError from normalise_path; decide returns (False, reason)
- Tatsächlich: Returned '/' (not idempotent). The contract stored '/'. decide raised ValueError from is_forbidden instead of refusing.

### BUG-048 – normalise_path accepted NUL bytes
- Phase: P39 · Komponente: scope · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_normalise_path_rejects_invalid_paths[src/a\x00.py])
- Reproduktion: normalise_path('src/a\x00.py')
- Erwartet: ValueError
- Tatsächlich: Returned 'src/a\x00.py'

### BUG-049 – Malformed glob classes raised re.error and crashed ScopeGuard
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_malformed_classes_in_a_contract_never_crash_the_guard, ::test_character_class_semantics)
- Reproduktion: ScopeGuard(ScopeContract(target_paths=['a[]'])).decide('a[]', 'modify'); also path_matches(']', '[]]') and path_matches('a', '[z-a]')
- Erwartet: A decision: a class that is never closed is literal, a leading ']' is a member, an inverted range is empty
- Tatsächlich: re.error: unterminated character set / bad character range. re.error is not a ValueError, so the gitops first_match and review _safe_any_match guards do not catch it either.

### BUG-050 – Glob negation '[!..]' was treated as a literal '!' (inverted semantics)
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_character_class_semantics[[!a]x-*])
- Reproduktion: path_matches('bx', '[!a]x'); path_matches('ax', '[!a]x')
- Erwartet: True / False (glob negation)
- Tatsächlich: False / True

### BUG-051 – Character classes could match '/' and cross a path segment
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_character_classes_never_cross_a_path_segment)
- Reproduktion: path_matches('x/y', 'x[^a]y'), path_matches('x/y', 'x[/]y'), path_matches('x/y', 'x[+-0]y')
- Erwartet: False (a class stays within one segment, as documented)
- Tatsächlich: True

### BUG-052 – Scope patterns were not canonicalised (policy globs with odd segments never matched)
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_patterns_are_compared_in_canonical_form_too)
- Reproduktion: ScopeGuard(ScopeContract(target_paths=['**'], allowed_operations=['modify']), ScopePolicy(always_forbidden=['./vault//**'])).allowed('vault/k.txt', 'modify')
- Erwartet: False (forbidden)
- Tatsächlich: True. Only a leading './' was stripped from patterns, so a 'vault//**' pattern never matched.

### BUG-053 – Creating an explicitly listed but glob-escaped target was refused
- Phase: P39 · Komponente: scope · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_escaped_bracket_target_guards_modify_and_explicit_create)
- Reproduktion: ScopeGuard(ScopeContract(target_paths=[glob_escape('routes/[id].tsx'), 'src/a.py'])).allowed('routes/[id].tsx', 'create')
- Erwartet: True, the same as for the explicitly listed src/a.py
- Tatsächlich: False, because the raw check 'p in target_paths' misses the escaped spelling

### BUG-054 – Replanner left 'unavailable' scope rows of superseded steps open (24.6 invariant broken)
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/integration/test_planner_replan_scope.py::test_replan_supersedes_unavailable_scope_versions)
- Reproduktion: Superseded step with current scope v2 status 'unavailable'; Replanner.replan(...)
- Erwartet: v2 marked superseded; at most one non-superseded scope row per step
- Tatsächlich: v2 stayed 'unavailable'; only 'active' rows were updated

### BUG-055 – mypy --strict errors in tests/unit/test_scope_guard.py (missing annotations / '-> None')
- Phase: P39 · Komponente: scope · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest: mypy run on the file)
- Reproduktion: .venv/bin/mypy tests/unit/test_scope_guard.py
- Erwartet: No issues
- Tatsächlich: 2 errors [no-untyped-def]

### BUG-056 – Stale comments in non-owned tests claim GitReader.status drops the rename origin
- Phase: P39 · Komponente: scope · Severity: P3 · blockierend: nein · Status: **behoben** (Kommentare im Integrations-Commit korrigiert)
- Reproduktion: tests/unit/test_scope_hardening_units.py:146 and tests/integration/test_scope_hardening.py:205
- Erwartet: Comments describe the current reader, which fills orig_path
- Tatsächlich: They say the origin is dropped. The tests still pass because they build their entries by hand.

### BUG-057 – A file literally named with a backslash is audited under the slash path
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_scope_audit_backslash.py`, 3 Tests – vor dem Fix alle rot; auch eine gestagte Umbenennung von `legacy\old_module.py` wurde als designierte Löschung von `legacy/old_module.py` akzeptiert)
- Reproduktion: A shell command creates the root-level file 'src\a.py'; git status reports '?? src\a.py'; audit_status with src/a.py in scope
- Erwartet: Refused, or judged as its literal name
- Tatsächlich: normalise_path turns '\' into '/', so the audit allows it as 'src/a.py'. gitops staging (normalise_repo_path) still refuses backslash paths, so it cannot be committed. Fix: `ScopeAuditor.audit_changes` meldet solche Namen als Verletzung unter ihrem wörtlichen Namen (`_display` wandelt `\` nicht mehr um); Contract-/Tool-Parsing unverändert.

### BUG-058 – GET /api/workers was registered twice (system read route and workers admin route)
- Phase: P39 · Komponente: api/workers · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_api_routes_unique.py`)
- Reproduktion: `create_app().openapi()` with warnings as errors → FastAPI "Duplicate Operation ID list_workers_api_workers_get" (found by the P31 UI agent)
- Erwartet: one route per method/path; the UI list uses the read-scope system route (display_state, capabilities)
- Tatsächlich: `hermclaw/workers/api.py` also registered an admin-only `GET /api/workers`; the system router won by order, the second route was dead and the OpenAPI document ambiguous
- Fix: `create_workers_router(include_list=False)` in the Hermclaw app; the standalone worker router (tests, embedding) keeps its list route

### BUG-059 – read_diff/changed_files missed a same-size edit made in the same second as the index write (racy git)
- Phase: P39 · Komponente: gitops · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_gitops_racy_index.py`)
- Reproduktion: commit `module.py` (`VALUE = 1`), rewrite it to `VALUE = 3` (same size) in the same second, wait 1 s, `ops.changed_files` → `[]` (found by the P39 scope agent: `test_reader_implements_git_reader_protocol` failed ~25 %)
- Erwartet: every change that `git status` reports is in the diff, the verifier's changed files and the push decision
- Tatsächlich: `_scratch_index` copied `.git/index` with `shutil.copyfile`; the fresh mtime made git treat the entry as clean (stat data matches at second granularity) instead of "racily clean" (content compare)
- Fix: `gitops/_fs.copy_if_exists` uses `shutil.copy2` (keeps the mtime); deterministic regression test aligns commit and edit to one second

### BUG-060 – A wheel installation had no Alembic migrations: `hermclaw migrate` failed outside a source checkout
- Phase: P32 · Komponente: packaging/persistence · Severity: P1 · blockierend: ja (Deployment) · Status: **behoben** (`tests/unit/test_persistence_migrate.py`; manuell: Wheel in frische venv installiert, `hermclaw migrate` + `alembic -c <venv>/share/hermclaw/alembic.ini downgrade 0001` gegen leere DB)
- Reproduktion: `pip wheel .` → install into `/opt/hermclaw/venv-<version>` → `hermclaw migrate` (cwd ≠ checkout)
- Erwartet: the release migrates its database; the rollback in `docs/runbooks/upgrade.md` uses `<venv>/share/hermclaw/alembic.ini`
- Tatsächlich: the wheel contained only the packages; `cmd_migrate` looked for `migrations/` next to the installed package (site-packages) and found nothing
- Fix: pyproject `[tool.setuptools.data-files]` ships `alembic.ini` + `migrations/` to `<venv>/share/hermclaw/`; `hermclaw.persistence.migrate` resolves checkout → installed data files (`HERMCLAW_MIGRATIONS_DIR` overrides)

### BUG-061 – Verification binding ignored the git file mode (symlink swap / chmod +x after verification was committed)
- Phase: P39 · Komponente: gitops/verifier · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_verification_binding.py`, 3 Tests rot ohne Fix)
- Reproduktion: verify a change with the regular file `src/alias.txt` (content `module.py`); replace it by a symlink to `module.py` or `chmod +x` a verified file; `stage_allowed` + `commit_verified`
- Erwartet: refused with `VERIFICATION_STALE` – the commit would record another type/mode than the verified one
- Tatsächlich: committed as `120000`/`100755`; the content digest covered blob shas only
- Fix: `gitops.engine.content_token` includes the mode for non-regular files (worktree: lstat, index: `ls-files --stage`); new columns `verification_runs.workspace_id` + `content_digest` (migration 0002)

### BUG-062 – The verifier hashed with its own GitRunner instead of the GitEngine runner that re-computes the digest at commit
- Phase: P39 · Komponente: runtime/verifier · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_verification_binding.py::test_adapter_hashes_with_the_git_runner_of_the_engine_reader`)
- Erwartet: verifier and `commit_verified` hash with the same git binary/environment · Tatsächlich: the verifier built `GitRunner("git")` · Fix: `VerifierAdapter` passes the reader's runner

### BUG-063 – Five event types had no live SSE listener in the UI (arrived only through the 15 s REST reconciliation)
- Phase: P39 · Komponente: ui/events · Severity: P4 · blockierend: nein · Status: **behoben** (`tests/unit/test_event_types.py`: Contract ↔ alle `append_event`-Literale ↔ UI-Listenerliste)
- Reproduktion: compare `EventType` with `KNOWN_EVENT_TYPES` in `ui/src/lib/events.ts`: `resource.preempt.withdrawn`, `resource.request.cancelled`, `system.disk`, `worker.sleep`, `workspace.cleaned` missing
- Fix: listeners added; `job.recovered`, `job.control.*` and `repository.registered` were emitted as literals only and are now `EventType` members

### BUG-064 – Any control principal could register file:// or local-path repositories through POST /api/jobs
- Phase: P39 · Komponente: api/jobs · Severity: P1 · blockierend: nein · Status: **behoben** (`tests/integration/test_api_repository_registration.py::test_control_principal_cannot_register_local_repository`)
- Reproduktion: control-scoped token, `POST /api/jobs {"repository": {"url": "file:///etc/..."}}` → registered; the runtime would clone anything the orchestrator user can read
- Fix: local repositories (file://, absolute paths) need the admin scope (403 FORBIDDEN otherwise); already registered ones stay usable

### BUG-065 – Repository URLs with embedded credentials were stored and emitted; same-tail remotes collided on the repository name
- Phase: P39 · Komponente: api/jobs · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_api_repository_registration.py`)
- Fix: remote URLs go through `gitops.urls.validate_remote_url` (422 for userinfo/transport helpers), distinct names for `a/demo.git` / `b/demo.git`

### BUG-066 – Finished jobs' workspaces and their code index were never removed (no retention)
- Phase: P39 · Komponente: runtime · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_runtime_maintenance.py`, `tests/integration/test_runtime_janitor_process.py`)
- Fix: `hermclaw/runtime/maintenance.py` WorkspaceJanitor (policies.git.workspace_retention_hours, default 72): drops the workspace index first, then `GitEngine.cleanup`, emits `workspace.cleaned`; unpushed verified commits are kept; runs in `SchedulerProcess`

### BUG-067 – CommandRequest.workspace accepted '/', '..', '.' (no contract-level pattern)
- Phase: P39 · Komponente: contracts/worker · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/unit/test_contracts_command_workspace.py`) – defense in depth next to the daemon/client checks; raw bad ids now get 422 VALIDATION_FAILED

### BUG-068 – worker.sleep was never emitted by the idle sleep hook
- Phase: P39 · Komponente: wol · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_wol_idle.py::test_sleep_success` + negative cases)

### BUG-069 – RepoIntelligence kept per-workspace in-memory state after drop_workspace_index (unbounded growth)
- Phase: P39 · Komponente: repo_intelligence · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_repo_intelligence_drop_state.py`)

### BUG-070 – Janitor crash window left orphaned index rows (directory removed before the index was dropped)
- Phase: P39 · Komponente: runtime/maintenance · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_runtime_maintenance.py::test_crash_right_after_the_directory_removal_leaves_no_orphaned_index`)

### BUG-071 – IdleSleepPolicy (P10.9 idle sleep) is never instantiated by any process
- Phase: P39 · Komponente: wol/runtime · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_runtime_ops.py::test_scheduler_process_runs_idle_sleep_over_the_ssh_runner`): `SchedulerProcess` runs `IdleSleepPolicy.run_forever` over the SSH runner of `hermclaw.runtime.ops` when Wake-on-LAN is on and a host has an `idle_sleep_command`
- Reproduktion: `grep -rn 'IdleSleepPolicy(' hermclaw worker` → only the class itself

### BUG-072 – Scheduler fails a job permanently (PREPARE_CRASHED) on a transient DB OperationalError during preparation
- Phase: P39 · Komponente: scheduler · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_scheduler.py::test_transient_db_errors_in_prepare_and_finalize_rerun_the_phase`; `tests/failure/test_runtime_failures.py -k db_connections_killed` 6/6 grün, vorher 1/3)
- Fix: connection-level DB errors (`OperationalError`, `InterfaceError`, invalidated `DBAPIError`) in the re-entrant phases prepare/finalize re-run the phase (bounded `prepare_transient_retries`=5, exponential backoff) instead of PREPARE_CRASHED/FINALIZE_CRASHED; a database that stays down still fails the job
- Reproduktion: `tests/failure/test_runtime_failures.py -k db_connections_killed` fails ~2 of 3 runs: AdminShutdown inside `driver.prepare` is caught in `Scheduler._run_prepare` as PREPARE_CRASHED
- Erwartet: killed DB connections are re-established and the job finishes (35.7)

### BUG-073 – Test hygiene: test_api_skeleton pinned schema revision 0001; untyped helper in test_gitops_support
- Phase: P39 · Komponente: tests · Severity: P4 · blockierend: nein · Status: **behoben** (assert against `hermclaw.persistence.migrate.head_revision()`; annotations)

### BUG-074 – GET /api/jobs/{id}/diff loaded the whole diff file into memory, cut it silently and did not confine the path
- Phase: P39 · Komponente: api/jobs · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_api.py::test_diff_is_bounded_flagged_and_confined`)
- Tatsächlich: `p.read_text()[:2_000_000]` (whole artifact in memory, no flag; found by the P31 UI agent); the diff path was not checked against the artifact store like the download is
- Fix: reads at most `DIFF_CHAR_LIMIT` characters, returns `truncated` + `size_bytes`; artifacts outside the store are never read (`missing: true`)

### BUG-075 – docs/contracts/openapi.json and five contract schemas were stale
- Phase: P39 · Komponente: contracts/docs · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_contract_docs.py` fails on any drift; `hermclaw schemas docs/contracts/schemas` regenerates schemas + OpenAPI)
- Tatsächlich: worker detail/drain/heartbeat and `/api/metrics` missing in the OpenAPI document; CoderAction/ToolCall/ToolResult/CommandRequest/VerificationReport schemas outdated

### BUG-076 – ssh_read classification bypass: VAR=value prefix and non-word-boundary tool names were treated as read
- Phase: P26 · Komponente: sshtools · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den sshtools-Tests)
- Reproduktion: SshCommandPolicy.classify(host, "cat=1 python3 -c 'import os'") or classify(host, 'cat.sh /x') with the example policies
- Erwartet: mutate (approval required; ssh_read refuses it)
- Tatsächlich: Before the fix: read, so arbitrary code ran through ssh_read without approval. Fixed in policy.py (_PLAIN_TOOL_RE plus _matches_read); regression test tests/unit/test_sshtools_policy.py::test_read_bypasses_are_not_reads.

### BUG-077 – Read tools with executing, writing or remote-hop flags classified as read (ip abbreviations/-batch, ss -D, systemctl -H/-M, journalctl --cursor-file, nginx -g/-c, rg --hostname-bin)
- Phase: P26 · Komponente: sshtools · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den sshtools-Tests)
- Reproduktion: classify(host, 'ip a a 10.0.0.1/24 dev eth0'), classify(host, 'systemctl status -H root@other nginx')
- Erwartet: mutate
- Tatsächlich: Before the fix: read. Fixed: extended escalation tables and an ip read-command check in policy.py; covered by the same regression test.

### BUG-078 – known_hosts/key file paths with whitespace or %/~ tokens accepted by the registry
- Phase: P26 · Komponente: sshtools · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den sshtools-Tests)
- Reproduktion: HostConfig ssh.known_hosts='/etc/hermclaw/known hosts'
- Erwartet: SshConfigError at startup
- Tatsächlich: Before the fix: accepted, and ssh would split UserKnownHostsFile into two files. Fixed with SAFE_PATH_RE in registry.py; test tests/unit/test_sshtools_registry_keys.py::test_registry_validates_entries_at_startup.

### BUG-079 – Order-dependent test failures caused by global redactor literals registered by tests
- Phase: P26 · Komponente: sshtools · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den sshtools-Tests)
- Reproduktion: Run the unit tests before test_runner_satisfies_remote_command_runner_protocol: SecretStore registers 'hello' as a redaction literal
- Erwartet: Tests independent of order
- Tatsächlich: Fixed: the tests now use unique secret values.

### BUG-080 – Materialised cred: keys could survive a SIGKILL of the orchestrator
- Phase: P26 · Komponente: sshtools · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den sshtools-Tests)
- Reproduktion: Kill the process between writing the key and deleting it
- Erwartet: The leftover key is removed at the next start
- Tatsächlich: Fixed: keys are named key-<pid>-* and purge_stale_keys runs on first use of the runtime dir; test ::test_stale_materialised_keys_of_dead_processes_are_purged.

### BUG-081 – Mutating SSH commands could never be approved (no operator endpoint for `ssh_approvals`)
- Phase: P39 · Komponente: api/sshtools · Severity: P1 · blockierend: ja (für SSH-Mutationen) · Status: **behoben** (`tests/integration/test_runtime_ops.py::test_operator_approves_and_revokes_a_pending_ssh_command`)
- Reproduktion: a step runs `ssh_command` on an allow-listed host with `require_approval_for_mutations: true` → `approval_required` + token; nothing could write `jobs.metadata.ssh_approvals`
- Fix: `GET/POST /api/jobs/{id}/ssh-approvals`, `DELETE …/{token}` (read / admin), pending tokens from `ssh.command` events, events `ssh.approval.granted|revoked`

### BUG-082 – Operations provider skip log used the reserved LogRecord key `module` (scheduler start crashed at INFO level)
- Phase: P39 · Komponente: runtime/ops · Severity: P1 · blockierend: ja · Status: **behoben** before release (`tests/integration/test_runtime_ops.py::test_missing_optional_provider_is_skipped_with_info_logging`; found by `tests/failure/test_recovery_process_kill.py`)

### BUG-083 – test_remote_execution_without_a_ready_worker… was order-dependent (shared test DB had a ready worker without token)
- Phase: P39 · Komponente: tests · Severity: P4 · blockierend: nein · Status: **behoben** (precondition explicit: execution workers set offline; `test_workers_execution_daemon.py` + `test_runtime_services.py` together green; found by the P27 agent)
- Tatsächlich: with a ready worker left by another test the pool selected it and raised WorkerAuthFailed (correct runtime behaviour) instead of the expected WorkerUnreachable

### BUG-084 – Sandbox refused the environment of DB migration commands (`SANDBOX_ENV_NOT_ALLOWED`)
- Phase: P39 · Komponente: worker/execution + dbtools · Severity: P1 · blockierend: ja (für `database`-Schritte) · Status: **behoben** (`tests/unit/test_sandbox_runtime_env.py`)
- Reproduktion: a `database` step calls `db_migration_apply_sandbox`; the DB tools run `alembic upgrade head` in the step's sandbox with `DATABASE_URL`/`SQLALCHEMY_DATABASE_URI`/`HERMCLAW_SANDBOX_URL`/`HERMCLAW_ALEMBIC_*` and libpq `PG*` variables
- Erwartet: the migration runs against the throw-away sandbox database
- Tatsächlich: `validate_request_env` only allowed `policies.sandbox.env_allowlist` (PATH, HOME, LANG, LC_ALL, CI, PYTHONDONTWRITEBYTECODE) → every sandbox migration failed; the unit tests used fake executors and missed it
- Fix: `hermclaw.core.config.RUNTIME_SANDBOX_ENV` (names only the runtime sets – tool calls of the model carry no environment) + `SandboxPolicy.request_env_allowlist`; the podman/docker sandbox validates against it, so an operator override of `env_allowlist` cannot break migrations or the verifier's `CI=1`. A test scans `hermclaw/dbtools` for every env name it sets.

### BUG-085 – PostgreSQL config validation treated pending-restart settings as errors (first live run on .225 would always fail)
- Phase: P01 · Komponente: bootstrap · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den bootstrap-Tests)
- Reproduktion: Running cluster with default shared_buffers; write conf.d/hermclaw.conf with shared_buffers=2GB; run the old validation query on pg_file_settings before the restart handler.
- Erwartet: Validation passes; the restart handler applies the new value.
- Tatsächlich: pg_file_settings returns 'setting could not be applied' for postmaster-context parameters, so the role failed with 'PostgreSQL configuration errors'. FIXED: new roles/postgresql/tasks/validate_config.yml ignores restart-only rows that are merely pending, validates all values with 'postgres -C', and rolls back pg_hba.conf and the drop-in (backup: true, a new broken drop-in is removed). Regression tests in test_bootstrap_postgresql.

### BUG-086 – nginx baseline site always listened on [::]; nginx -t fails on kernels without IPv6
- Phase: P01 · Komponente: bootstrap · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den bootstrap-Tests)
- Reproduktion: Render hermclaw-next.conf.j2 and run nginx -t on a host without IPv6 (ipv6.disable=1), e.g. this build sandbox.
- Erwartet: Config test succeeds.
- Tatsächlich: 'socket() [::]:80 failed (97: Address family not supported by protocol)'. FIXED: listen [::] only when nginx_listen_ipv6 is true, which defaults to whether the host has an IPv6 address. Regression test test_ipv6_listener_follows_the_host; real nginx -t passes.

### BUG-087 – No Secure Boot check before building the DKMS NVIDIA driver on .224
- Phase: P01 · Komponente: bootstrap · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den bootstrap-Tests)
- Reproduktion: .224 with Secure Boot enabled and no MOK enrolled; run the nvidia role.
- Erwartet: A clear refusal up front with remediation steps.
- Tatsächlich: The driver would build but its unsigned modules would not load; nvidia-smi fails only after the install. FIXED: roles/nvidia/tasks/secureboot.yml reads the EFI SecureBoot variable and refuses unless nvidia_secureboot_mok_enrolled is true. Test test_nvidia_secure_boot_guard (5 cases).

### BUG-088 – ansible-lint production profile: 2 name[template] violations
- Phase: P01 · Komponente: bootstrap · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den bootstrap-Tests)
- Reproduktion: ansible-lint --offline --profile=production playbooks/*.yml roles
- Erwartet: 0 violations
- Tatsächlich: Fixed in roles/ollama/tasks/gpu_check.yml:40 and main.yml:135 (Jinja moved to the end of the task name).

### BUG-089 – Repository intelligence: a quoted "phrase" across lines disabled the lexical signal
- Phase: P39 · Komponente: repo_intelligence · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/unit/test_repo_intelligence_structure.py::test_query_phrases_are_single_line_and_ignore_apostrophes`; found by the 37.3 E2E run)
- Reproduktion: a step query like `title: orders.status'` + newline + `… DEFAULT 'new' …` (or any apostrophe as in "user's" followed later by a quoted word)
- Erwartet: lexical search with the step's words and literals
- Tatsächlich: the single-quote phrase pattern matched from the apostrophe across the line break; ripgrep refuses multi-line fixed patterns, so the whole lexical signal failed (`REPO_PATTERN_INVALID`, ranking degraded)
- Fix: phrases are single-line, a single quote must not touch a word (apostrophes), and `lexical_patterns` never emits a pattern with a newline/NUL

### BUG-090 – `hermclaw doctor` accepted a production install running on the shipped example configuration
- Phase: P39 · Komponente: doctor · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_doctor.py::test_production_refuses_example_configuration_and_flags_placeholder_macs`)
- Reproduktion: production install without `/etc/hermclaw/hosts.yaml` (only `hosts.example.yaml`), or with the inventory's placeholder MACs
- Erwartet: doctor fails / warns before the first job
- Tatsächlich: `load_config` silently falls back to `<name>.example.yaml` (meant for checkouts and tests) and doctor reported everything ok; Wake-on-LAN with `00:00:00:00:..` MACs never wakes a worker
- Fix: check `config:files` (production: fail when any of hosts/models/policies/capabilities/logging is only available as example) and `config:wake_on_lan` (warn on placeholder MACs)

### BUG-091 – Installed release looked for its configuration in `<site-packages>/config`
- Phase: P39 · Komponente: core/settings · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_config_and_redaction.py::test_default_config_dir_is_the_checkout_or_etc_hermclaw`)
- Reproduktion: `sudo -u hermclaw /opt/hermclaw/venv/bin/hermclaw config-check` without `HERMCLAW_CONFIG_DIR` (the units set it, a manual admin call does not)
- Erwartet: the production configuration in `/etc/hermclaw`
- Tatsächlich: `config_dir` defaulted to `REPO_ROOT/config`, which for a wheel install is `<venv>/lib/python3.13/site-packages/config` → "missing configuration file"
- Fix: default is the checkout's `config/` only when it exists (examples), otherwise `/etc/hermclaw`

### BUG-092 – `ollama` CLI not on PATH with the pinned tarball install (.224)
- Phase: P39 · Komponente: bootstrap/ollama · Severity: P3 · blockierend: nein · Status: **behoben** (role task "Link the ollama CLI into /usr/local/bin"; `tests/integration/test_bootstrap_infra.py` lint/syntax/idempotency green)
- Reproduktion: tarball install to `/opt/ollama/<version>` + `current` symlink, then `ollama ps` or `hermclaw-model-pull` (default `OLLAMA_BIN=ollama`)
- Erwartet: the CLI works for operators and the pull script
- Tatsächlich: `command not found` – only the unit used the absolute path
- Fix: `/usr/local/bin/ollama` → `/opt/ollama/current/bin/ollama`

### BUG-093 – Repository secret scan red: false positives in new docs and a fixed SearXNG `secret_key` placeholder
- Phase: P39 · Komponente: docs/bootstrap · Severity: P2 · blockierend: ja (Security-Test rot) · Status: **behoben** (`tests/security/test_security_checks.py::test_repository_contains_no_secrets`; found by research R-027 Y5)
- Reproduktion: `pytest tests/security/test_security_checks.py`
- Tatsächlich: `docs/operations/bootstrap.md:213` and `INSTALLATION.md` ("token: …" prose read as an assignment), `docs/research/20261014-026…:101` (`CMAKE_CUDA_ARCHITECTURES=…` read as high entropy), `searxng-settings.yml.j2:8` (a fixed `secret_key` placeholder – SearXNG would have started with that known key if the podman secret were missing)
- Fix: prose rephrased; the settings template sets no `secret_key` (only the podman secret `SEARXNG_SECRET`, SearXNG refuses to start on its shipped default); the research table writes the CMake value without `=`. The scanner stays strict (it also guards job diffs).

### BUG-094 – Same-second backup sets were ordered by their random id suffix, so retention could keep the wrong 'newest' set
- Phase: P33 · Komponente: backup · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den backup-Tests)
- Reproduktion: Two create_backup runs within the same UTC second, then retention with keep_last=1 (test_prune_after_backup_protects_the_new_set failed intermittently).
- Erwartet: Sets sort by creation time.
- Tatsächlich: Fixed: set ids now carry milliseconds (hermclaw-YYYYmmddTHHMMSSmmmZ-<6hex>). Ids with second precision stay valid. Regression test: test_backup_units.py::test_set_ids_and_signature; the formerly flaky test passed 3 repeat runs.

### BUG-095 – Partial-attempt build_tarball fsynced before the tar end blocks and gzip trailer were written
- Phase: P33 · Komponente: backup · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den backup-Tests)
- Reproduktion: Code review of the interrupted attempt's archive.py.
- Erwartet: fsync after the TarFile closes.
- Tatsächlich: Fixed (fsync moved after the tar close).

### BUG-096 – A misconfigured staging_dir equal to or nested with a local target could let the stale-staging cleanup delete real backup sets
- Phase: P33 · Komponente: backup · Severity: P1 · blockierend: nein · Status: **behoben** (Regressionstest in den backup-Tests)
- Reproduktion: BackupConfig(staging_dir=<local target path>).
- Erwartet: Refused.
- Tatsächlich: Fixed: check_staging_separate raises BACKUP_CONFIG_INVALID. Regression test: test_backup_failures.py::test_staging_inside_the_target_is_refused.

### BUG-097 – A wrong LiteLLM master key was classified as a bad request (DB-less proxy answers 400 / 500)
- Phase: P39 · Komponente: models/gateway · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/unit/test_models_gateway.py` cases for 400 "No connected db." and 500 "No module named 'prisma'"; `tests/integration/test_models_litellm_proxy.py::test_wrong_key_is_rejected_without_fallback` now requires `MODEL_AUTH_FAILED`, green against real LiteLLM 1.104.2 with and without prisma)
- Reproduktion: LiteLLM proxy without database (our setup), gateway with a wrong key
- Erwartet: `MODEL_AUTH_FAILED` (operator problem, no retry/fallback, clear doctor message)
- Tatsächlich: 400 `no_db_connection` → `MODEL_BAD_REQUEST`; without the `prisma` package auth errors are HTTP 500 (research R-027 L10)
- Fix: both shapes are authentication failures; `prisma==0.15.0` joins the `litellm-proxy` extra (401 for a missing key)


### BUG-098 – JSON output redacted before parsing broke `podman inspect` (container sandboxes never saw their containers)
- Phase: P28 · Komponente: infratools · Severity: P1 · blockierend: ja · Status: **behoben** (`tests/unit/test_infratools_containers.py::test_inspect_secret_like_json_keys_do_not_break_parsing`)
- Tatsächlich: `"Passwd": true` became invalid JSON after redaction
- Fix: JSON is parsed raw, only extracted fields are redacted; `loads_lenient` repairs transport-redacted output; raw output never reaches `command_runs`

### BUG-099 – Podman healthchecks stayed `starting` without systemd timers
- Phase: P28 · Komponente: infratools · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_infratools_compose_real.py::test_sandbox_up_healthy_remapped_ports_logs_and_teardown`)
- Tatsächlich: compose sandbox waited for `healthy` forever on a host without systemd user timers
- Fix: the sandbox runs `podman healthcheck run` itself

### BUG-100 – `nginx -t` bound the real listen sockets and wrote log/pid paths
- Phase: P28 · Komponente: infratools · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_infratools_nginx_real.py::test_listen_on_foreign_address_and_busy_port_is_rewritten`)
- Tatsächlich: other hosts' addresses fail with EADDRNOTAVAIL, ports < 1024 without root, paths created on the host
- Fix: `rewrite_for_test` moves sockets and paths into a private prefix and maps messages back to the original lines

### BUG-101 – SSE regex locations were not recognised as SSE
- Phase: P28 · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_infratools_nginx.py`)
- Tatsächlich: `~ ^/api/(jobs/[^/]+/)?events/stream$` skipped the buffering/timeout rules
- Fix: probe paths matched with nginx location semantics

### BUG-102 – Billion-laughs YAML in a compose file
- Phase: P28 · Komponente: infratools · Severity: P1 · blockierend: nein · Status: **behoben** (`tests/unit/test_infratools_compose.py`)
- Tatsächlich: an alias-count limit did not stop the expansion
- Fix: cap on the expanded node count

### BUG-103 – Duplicate host ports: IPv4/IPv6 and wildcard overlap not told apart
- Phase: P28 · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_infratools_ports.py`)
- Tatsächlich: false duplicates / missed overlaps between `0.0.0.0` and a specific address
- Fix: address-family aware overlap check

### BUG-104 – Local compose staging directory leaked when copying failed
- Phase: P28 · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/failure/test_infratools_failures.py`)
- Tatsächlich: temp dir left behind
- Fix: always removed

### BUG-105 – Remote port allocator could fail although free ports existed
- Phase: P28 · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_infratools_sandbox_model.py`)
- Tatsächlich: allocation gave up early
- Fix: fixed retry over the range

### BUG-106 – Predeploy check crashed on invalid YAML
- Phase: P28 · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/failure/test_infratools_failures.py`)
- Tatsächlich: exception instead of a finding
- Fix: reported as validation error

### BUG-107 – A failed rollback-artifact step left the deployment row in `started`
- Phase: P28 · Komponente: infratools · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_infratools_rollback.py::test_failed_rollback_artifact_blocks_the_write_and_releases_the_claim`)
- Tatsächlich: target blocked for later deployments
- Fix: claim released, deployment marked failed

### BUG-108 – Sandbox ran images whose metadata defeats its isolation (Podman CVE-2026-57231 / CVE-2026-94603, unfixed in trixie's podman 5.4.2)
- Phase: P39 · Komponente: worker/execution (P18) · Severity: P1 · blockierend: nein · Status: **behoben** (`tests/integration/test_sandbox_podman.py::test_images_that_defeat_the_sandbox_are_refused` with a real podman-built image, `tests/unit/test_sandbox_runtime_env.py::test_image_safety_issues`; found by research R-028 C3/C3b)
- Reproduktion: an allowed image with `Env: ["SECRET"]` (no value) or the label/annotation `io.podman.annotations.checkpoint.runtime.name`
- Erwartet: refused before anything runs
- Tatsächlich: podman copies the engine process's variable values into the container (CVE-2026-57231); with the checkpoint annotation it silently ignores `--cap-drop=ALL` & co. (CVE-2026-94603; rootless not affected per advisory)
- Fix: every image is inspected once per worker process before its first run (`SANDBOX_IMAGE_UNSAFE`); the worker selftest (`hermclaw doctor`) reports such images

### BUG-109 – Offline grammar download of tree-sitter-language-pack retried (and timed out) for every file
- Phase: P39 · Komponente: repo_intelligence · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_repo_intelligence_structure.py::test_unavailable_grammar_is_not_retried_per_file`; found via research R-027 Y6)
- Reproduktion: tree-sitter-language-pack ≥ 1.0 without a cached grammar bundle and without network; index a repository with JS/TS/PHP files
- Tatsächlich: `get_parser` attempts the ~23 MB GitHub download per file (failure is caught, structure skipped), so indexing stalls on timeouts
- Fix: a failed load marks the language unavailable for the process (one warning); dependency pinned to `>=1.21,<2` (cache path is per version); the deployment pre-fetches the bundle

### BUG-110 – Node.js minimum allowed 22.x releases without the latest security fixes (.223 UI build)
- Phase: P39 · Komponente: bootstrap/nodejs · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_bootstrap_tooling.py::test_node_version_gate`; found by research R-028 B2/B9)
- Tatsächlich: `nodejs_min_version` was Vite's minimum 22.12.0; NodeSource has lagged security releases, so an outdated Node could pass
- Fix: minimum 22.23.2 (last 22.x security release); the failure message points to `--only-upgrade` and the checksum-verified tarball fallback (INSTALLATION.md 5.3)

### BUG-111 – PostgreSQL bootstrap: main cluster created with the system locale; PGDG key fetched at deploy time
- Phase: P39 · Komponente: bootstrap/postgresql · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_bootstrap_postgresql.py::test_pgdg_keyring_fingerprint_check`; found by research R-027 P4/P7)
- Reproduktion: fresh `.225`: installing `postgresql-17` runs its postinst, which creates `17/main` with the system locale before the role's `pg_createcluster --locale=C.UTF-8` (which then skips: config exists)
- Tatsächlich: cluster locale depended on the installer's locale; the PGDG key was downloaded from postgresql.org without a fingerprint check
- Fix: `create_main_cluster = false` in `/etc/postgresql-common/createcluster.conf` before the package install; the repository uses Debian's shipped keyring `/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg`, asserted to hold exactly fingerprint `B97B0AFC…4CF8`

### BUG-112 – Worker console scripts pointed to non-existent functions (`worker.execution.app:main`)
- Phase: P32 · Komponente: packaging · Severity: P1 · blockierend: ja (jedes Deployment) · Status: **behoben** (`tests/unit/test_packaging.py::test_console_scripts_resolve_to_callables`; found by the P32 release canary `test_this_checkout_builds_a_deployable_release`)
- Reproduktion: install the wheel, run `hermclaw-exec-worker` (the unit's ExecStart)
- Tatsächlich: `AttributeError` at start – the `main` functions live in `worker/execution/__main__.py` / `worker/model/__main__.py`; the dev setup always used `python -m worker.execution`
- Fix: entry points `worker.execution.__main__:main` / `worker.model.__main__:main`; a test resolves every console script

### BUG-113 – API trusted X-Forwarded-For/-Proto from any client (`forwarded_allow_ips="*"`)
- Phase: P32 · Komponente: api · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/unit/test_config_and_redaction.py::test_api_trusts_forwarded_headers_only_from_configured_proxies`; research R-029 E2/E3, R-028 A16)
- Tatsächlich: any client reaching `.225:8000` directly could forge its address in logs and audit (authentication is by token and unaffected)
- Fix: `HERMCLAW_API_FORWARDED_ALLOW_IPS` (default `127.0.0.1`; production: the nginx host `.223`)

### BUG-114 – `hermclaw-build-sandbox-images` installed in /opt/hermclaw/bin looked for its Containerfiles in /opt/infra/sandbox-images
- Phase: P32 · Komponente: scripts/ops · Severity: P3 · blockierend: nein · Status: **behoben** (fallback to the release payload `/opt/hermclaw/current/sandbox-images`; `HERMCLAW_SANDBOX_IMAGES_DIR` still wins; shellcheck clean)

### BUG-115 – Exec worker unit implied NoNewPrivileges, breaking rootless podman
- Phase: P32 · Komponente: deploy · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den deploy-Tests)
- Reproduktion: Deploy `.222` and run a sandbox command through the exec worker: rootless podman inside the unit fails at `newuidmap` (setuid helper blocked by NoNewPrivileges implied by `PrivateDevices=`/`ProtectKernel*=`/`RestrictNamespaces=` etc.).
- Erwartet: No NNP-implying directives; ProtectSystem=full stays. Fixed in infra/systemd/hermclaw-exec-worker.service.j2, asserted by test_deploy_infra.py and test_bootstrap_infra.py.
- Tatsächlich: Directives that imply NoNewPrivileges in hermclaw-exec-worker.service.j2 block the setuid newuidmap that rootless podman needs.

### BUG-116 – Upgrade runbook rollback used the old release's Alembic for the downgrade
- Phase: P32 · Komponente: deploy · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-Tests)
- Reproduktion: Upgrade to a release with a new Alembic revision, then follow the rollback section of `docs/runbooks/upgrade.md`.
- Erwartet: Downgrade with the newer release (`hermclaw --venv /opt/hermclaw/venv-<new> --alembic downgrade <old-head>`). Fixed in the doc and implemented in hermclaw_orchestrator/tasks/database.yml.
- Tatsächlich: docs/runbooks/upgrade.md ran the downgrade with venv-<old>, which does not contain the downgrade() of the newer revisions.

### BUG-117 – Two diverging backup service units
- Phase: P32 · Komponente: deploy · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-Tests)
- Reproduktion: diff infra/backup/hermclaw-backup.service infra/systemd/hermclaw-backup.service.j2
- Erwartet: One source; the integrator reconciles them. The rendered unit now carries ConditionPathExists=/etc/hermclaw/backup.yaml so the timer is harmless until P33's config exists.
- Tatsächlich: infra/backup/hermclaw-backup.service (P33: backup-manifest-key credential, 'create' argument) differs from infra/systemd/hermclaw-backup.service.j2, which the bootstrap/deploy renders.

### BUG-118 – Dry run (deploy.yml --check --diff) of a first deployment or an upgrade aborts
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: Bootstrapped host, then: ansible-playbook playbooks/deploy.yml -e hermclaw_version=<new> --check --diff. This is step 2 of docs/runbooks/upgrade.md and the first command of deployment.md section 3.
- Erwartet: The dry run lists what would change, and nothing on disk changes.
- Tatsächlich: Failed at 'Copy and unpack the runtime payload' (dest must be an existing dir, because check mode never created it). Once that was guarded, the next failures were: sha256sum chdir into the missing dir; the symlink switch onto a target that is not installed ('src file does not exist'); the schema decision and the final doctor calling a venv/operator CLI that does not exist yet; the model check asserting tags that a dry run never pulls. All fixed: unarchive/sha256sum run only for real, links use force=ansible_check_mode, the database and verify steps are skipped until their inputs exist, and the model assert is skipped in check mode. Regression tests: test_dry_run_of_a_new_release_unpacks_and_

### BUG-119 – Pruning deletes the previously active release after a rollback followed by an upgrade; keep=0 deletes it always
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P1 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: With hermclaw_keep_releases=1: install A, B, C; roll back to B; deploy D.
- Erwartet: B (active before the last switch) is kept, so D can be rolled back.
- Tatsächlich: Releases were ranked by install mtime, so B was deleted and C kept. hermclaw_keep_releases: 0 was accepted and removed the previous release. Any venv-* directory, such as a manual venv-backup, was also eligible for deletion. Fixed: every switch touches the release it leaves and the one it activates (venvs, UI markers, LiteLLM venvs). keep must be 1-99. Only directories matching hermclaw_version_pattern are pruned. Tests: test_switch_and_prune_keep_the_previously_active_release, test_ui_build_switch_rollback_pruning_and_dry_run, validation cases release-keep-0 and orch-litellm-keep-0.

### BUG-120 – Live configuration and LiteLLM switched before the schema decision and the drain
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P1 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: Upgrade to v3 (new revision), then deploy.yml -e hermclaw_version=v2 together with a config change, without allow_schema_downgrade.
- Erwartet: The run stops, and services, venvs, /etc/hermclaw and LiteLLM are unchanged.
- Tatsächlich: hermclaw_config had already installed v2's /etc/hermclaw/*.yaml, and litellm.yml had switched /opt/litellm/venv and litellm.yaml. The newer release kept running with the older configuration while the message claimed nothing had been switched. Likewise, a failed migration restarted the old release with the new config (extra=forbid, so it may fail to start). Fixed: hermclaw_config only stages and validates (install.yml runs from the host role at its switch, hermclaw_config_install_at_switch); LiteLLM is split into install and activate; the schema decision, migrate and the post-migration check use the new release's staged config through the new CLI --config-dir. Tests: test_nothing_live_changes

### BUG-121 – site.yml: unit/env changes made by the bootstrap part never restart the services
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: Change a unit template or hermclaw.env, then run playbooks/site.yml.
- Erwartet: API, scheduler, LiteLLM and workers restart with the new unit/env.
- Tatsächlich: systemd_skeleton runs again in the deploy plays and its registered results (unchanged) overwrote the bootstrap's, so no restart happened. Fixed: systemd_skeleton_units_changed and systemd_skeleton_env_changed accumulate across plays, and the restart decisions use them. Test: test_nothing_live_changes_before_the_schema_decision (no restart decision reads a registered systemd_skeleton result).

### BUG-122 – API and scheduler started before LiteLLM was checked
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: LiteLLM without its master key, or failing to start, during a deployment.
- Erwartet: The scheduler does not start against a broken or open model gateway.
- Tatsächlich: Liveliness and the auth check ran only after API and scheduler were (re)started. Fixed: health_litellm.yml runs right after LiteLLM's restart, before SearXNG, API and scheduler. Test: ordering assertion in test_nothing_live_changes_before_the_schema_decision.

### BUG-123 – Model tags of models.yaml outside hermclaw-model-pull's fixed set were never pulled
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: hermclaw_config_overrides.models changes the coder tag (documented in upgrade.md as a pure config change).
- Erwartet: The deployment pulls the new tag.
- Tatsächlich: hermclaw-model-pull pulls only its hard-coded role set, so the play failed 'still missing' after a potentially multi-hour pull. Tags were not validated before being passed to ollama pull. Fixed: pull_tag.yml pulls each remaining tag in its own transient unit (hermclaw-model-pull-tag); tags must be plain name[:tag]. Test: test_model_pull_follows_models_yaml_and_survives_a_dry_run (real script plus doubles).

### BUG-124 – Deployed hermclaw-backup.service incomplete; backup runbook conflicts with the deployment; backup commands ran outside the services' context
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: Configure backup.yaml with manifest_signing_key_ref: cred:backup-manifest-key as docs/operations/backup-restore.md describes; or copy infra/backup/hermclaw-backup.{service,timer} over the deployed units as that runbook says.
- Erwartet: The nightly backup loads its credential. Bootstrap and deploy keep working. upgrade.md's backup commands run with hermclaw.env and the credentials.
- Tatsächlich: The deployed unit (enabled by the deployment) had no LoadCredentialEncrypted, no HERMCLAW_BACKUP_CONFIG and no SuccessExitStatus=75. The hand-copied units lack 'Managed by Hermclaw', so systemd_skeleton refuses every later run. The operator CLI lacked backup-manifest-key although the backup docs route 'sudo hermclaw backup ...' through it. upgrade.md used 'sudo -u hermclaw python -m hermclaw.backup' (no env, no credentials). Fixed: the unit is the deployed form of infra/backup's (credential only when present); the CLI loads optional backup credentials; upgrade.md and the doctor refusal message use 'sudo hermclaw backup create'. Doc conflict reported as a shared change. Tests: test_units_carr

### BUG-125 – API does not trust nginx's X-Forwarded-For/-Proto (HERMCLAW_API_FORWARDED_ALLOW_IPS never set)
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: Deploy and open the UI through .223.
- Erwartet: The API sees the real client address and scheme (settings: 'in production the nginx host .223 only').
- Tatsächlich: The default of 127.0.0.1 applied, so every request was logged and audited as coming from .223 over http. Fixed: hermclaw.env sets it to the webui hosts' ansible_host (systemd_skeleton_api_forwarded_allow_ips, validated as IP addresses, never '*'). Tests: converge asserts HERMCLAW_API_FORWARDED_ALLOW_IPS=192.168.178.223; test_bootstrap_infra env test.

### BUG-126 – Runs limited to workers or the UI could put them ahead of the orchestrator
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: ansible-playbook playbooks/deploy.yml -e hermclaw_version=<new> --limit execution_workers (or --tags hermclaw_ui).
- Erwartet: Refused unless the orchestrator already runs <new>; the docs promise workers and UI never get ahead of it.
- Tatsächlich: Deployed the newer worker or UI. Fixed: preflight includes orchestrator_first.yml, which reads the orchestrator's /api/health from the control node when the run does not deploy it (hosts or tags), with an explicit opt-out for disaster recovery. Test: test_workers_and_ui_never_get_ahead_of_the_orchestrator (stub API: same version passes; newer, --tags, --skip-tags and unreachable are refused; opt-out works).

### BUG-127 – Preflight wrote the 'deployed' fact before the inventory checks ran
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: Placeholder wol_mac, then deploy.yml.
- Erwartet: Nothing changes on any host.
- Tatsächlich: hermclaw.fact (deployed=true) was already written on every host before hermclaw_config/data.yml failed, so the next bootstrap renders SearXNG's [Install]. Fixed: mark_deployed.yml is the last task of the preflight play. Test: test_deploy_playbook_structure (preflight files use read-only modules only; the fact is written last).

### BUG-128 – Docs: wrong or missing operator commands
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: Follow deployment.md and upgrade.md.
- Erwartet: Correct commands.
- Tatsächlich: The manual sandbox rebuild lacked --version (the tag must match policies.yaml). The schema-rule wording named the wrong release. The order, dry-run and keep semantics were outdated, and the backup and CLI options (--config-dir, backup create) were missing. All fixed in deployment.md, the architecture deployment.md, upgrade.md, ui.md and hosts.example.yml.

### BUG-129 – Weekly restore test not deployed; restore environment (CREATEDB, template database) only by hand
- Phase: P32 (Review) · Komponente: deploy-review · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den deploy-review-Tests)
- Reproduktion: Deploy .225 with playbooks/deploy.yml and configure /etc/hermclaw/backup.yaml from backup.example.yaml (restore_test.template: hermclaw_restore_template).
- Erwartet: Backup and weekly restore test both run by timer; the restore test finds a role with CREATEDB and the template database with vector (P33 33.5, Prompt 26: restore test is mandatory).
- Tatsächlich: Only hermclaw-backup.service/.timer were deployed; hermclaw-restore-test units, ALTER ROLE ... CREATEDB and the template database had to be installed by hand (backup-restore.md), and a hand-copied unit without the 'Managed by Hermclaw' marker made every later deployment fail. Fixed: infra/systemd/hermclaw-restore-test.{service,timer}.j2 rendered by systemd_skeleton and enabled by deploy.yml; postgresql role grants CREATEDB and maintains hermclaw_restore_template (owner postgres, IS_TEMPLATE, no connections, extensions updated with pgvector); release ships backup/backup.example.yaml. Tests: test_bootstrap_postgresql (clone as the service role), test_bootstrap_infra (unit plan), test_deploy_co

### BUG-130 – Mounted backup target (Unraid share variant B) not writable for the deployed backup units
- Phase: P32/P33 (Integration) · Komponente: deploy · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in test_deploy_infra.py)
- Reproduktion: Mount the Unraid share on `.225` as in `infra/backup/unraid-share-setup.md` variant B and set `target: {kind: local, path: /mnt/terra-backup/hermclaw, require_mount: true}` in `backup.yaml`; start `hermclaw-backup.service`.
- Erwartet: The backup is written to the share.
- Tatsächlich: `ProtectSystem=strict` with `ReadWritePaths=/var/lib/hermclaw /var/log/hermclaw` makes the mount read-only inside the unit, so every run failed with "Read-only file system"; only the hand-installed reference unit had a commented hint. Fixed: `systemd_skeleton_backup_write_paths` (validated absolute paths) adds `-<path>` to `ReadWritePaths=` of the backup and restore-test units; documented in INSTALLATION.md 11 and backup-restore.md.

### BUG-131 – Repository secret scan red after P32 (doc/role comments, PGDG fingerprint)
- Phase: P32 (Integration) · Komponente: security · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/security/test_security_checks.py::test_repository_contains_no_secrets` grün)
- Reproduktion: `pytest tests/security/test_security_checks.py` after the P32 merge.
- Erwartet: no findings in tracked files.
- Tatsächlich: `secret_assignment` for three comment lines of the form "admin token: …" (`docs/architecture/deployment.md`, `roles/hermclaw_orchestrator/defaults/main.yml`, `tasks/token.yml`) and `high_entropy` for the 40-hex PGDG fingerprint in `roles/postgresql/defaults/main.yml`. Fixed: comments reworded; the fingerprint is written in gpg's grouped form and normalised (`replace(' ', '') | upper`) before the comparison (fingerprint test still passes and refuses other/extra/missing keys).

### BUG-132 – Proxy validator flagged the HTTP→HTTPS redirect server as "SSE not proxied"; template test could not render the P32 template
- Phase: P39 (finale Matrix) · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_infratools_nginx.py::test_sse_probe_skips_redirect_servers_but_not_static_locations`, `::test_repo_nginx_template_passes_the_proxy_rules[tls-hsts]`)
- Reproduktion: `proxy_config_validate` with SSE probe paths on the rendered site with `nginx_tls_enabled: true`; and `pytest tests/unit/test_infratools_nginx.py` after the P32 template rewrite (Jinja macros).
- Erwartet: the redirect-only HTTP server (`location / { return 301 https://… }`) is not an SSE path; the template test renders the real template.
- Tatsächlich: two `SSE_PROBE_NOT_PROXIED` warnings for the port-80 server of every TLS site; the unit test rendered the template with string replacements and failed with `NGINX_SYNTAX` on the macro-based template. Fixed: servers/locations that answer with `return 3xx` are skipped by the probe; the test renders the template with Jinja (trim_blocks, Ansible filters/tests) from the role defaults in three variants (HTTP + default_server + IPv6, TLS + HSTS, IPv4 only).

### BUG-133 – Git over SSH to GitLab used neither the runtime key nor the pinned known_hosts (clone/push fail)
- Phase: P41 (Doku-Abgleich) · Komponente: config/gitops · Severity: P1 · blockierend: ja · Status: **behoben** (`tests/unit/test_gitops_units.py::test_ssh_options_from_config`, `tests/unit/test_sshtools_registry_keys.py`)
- Reproduktion: Deploy with the shipped `config/hosts.example.yaml`, register `git@192.168.178.226:<group>/<project>.git` (INSTALLATION.md 10.3) and run a job.
- Erwartet: `GIT_SSH_COMMAND` carries `-i /etc/hermclaw/ssh/id_ed25519` and `UserKnownHostsFile=/etc/hermclaw/ssh/known_hosts` (the deploy key from INSTALLATION.md 6.1, GitLab's host key from 4.3).
- Tatsächlich: `ssh_options_from_config` reads the `ssh` block of the gitlab host; the example's `gitlab-226` had none, so ssh ran as `hermclaw` without key and known_hosts and every clone/push failed with "Host key verification failed". Fixed: `gitlab-226` carries `ssh: {user: git}` (key and known_hosts from the `SshConfig` defaults); GitLab stays outside `policies.ssh.allowed_hosts` (the SSH tools still refuse it). Found by the documentation cross-check (workflow `install-docs-verify`).

### BUG-134 – Inventory group vars had no effect for `hermclaw_keep_releases`, `hermclaw_sandbox_image_version`, `hermclaw_timezone`
- Phase: P41 (Doku-Abgleich) · Komponente: deploy · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_deploy_infra.py::test_shared_inventory_settings_are_role_defaults_that_agree`, `::test_inventory_example_settings_are_not_playbook_group_vars`, `::test_inventory_group_vars_take_effect_in_the_roles`)
- Reproduktion: set `hermclaw_keep_releases: 7` or `hermclaw_sandbox_image_version: "2"` under `all.vars` in `inventory/hosts.yml` (as INSTALLATION.md 8.2 and admin.md say), then `ansible-inventory --playbook-dir playbooks --host <h>`.
- Erwartet: the inventory value.
- Tatsächlich: `playbooks/group_vars/all.yml` defined both (and the timezone); playbook group_vars/all outrank inventory-file group vars, so the shipped 3 / "1" won silently – no image rebuild after a bump, pruning not as configured. Fixed: the three settings are defaults of every role that reads them (equal values, asserted); `hermclaw_release_root` stays a playbook default and is documented as `-e` only.

### BUG-135 – `check-host.sh` rejected hosts with exactly the documented RAM minimum
- Phase: P41 (Doku-Abgleich) · Komponente: bootstrap · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_bootstrap_infra.py::test_check_host_ram_minimum_tolerates_kernel_reserved_memory`)
- Reproduktion: `.225` with 8 GB installed RAM (MemTotal ≈ 7.7 GiB) → `scripts/bootstrap/check-host.sh --role orchestrator`.
- Erwartet: the documented minimum (INSTALLATION.md 2.1) passes.
- Tatsächlich: MemTotal is compared with 8192 MiB and is always below the installed RAM → `ram FAIL`. Fixed: the check passes at ≥ 90 % of the minimum (message names the tolerance); `usage()` no longer cuts the help text.

### BUG-136 – Disaster-recovery procedure for `.225` could not work as written
- Phase: P41 (Doku-Abgleich) · Komponente: docs/backup · Severity: P2 · blockierend: nein · Status: **behoben** (zwei unabhängige Prüfdurchläufe gegen den Code, zuletzt ohne Befund)
- Reproduktion: follow `docs/operations/backup-restore.md` "Disaster recovery" / `docs/runbooks/disaster-recovery.md` §1 on a reinstalled `.225`.
- Erwartet: a running Hermclaw with the newest good backup.
- Tatsächlich: deployment before the credentials (preflight stops on the missing `gitlab-token`), `restore-full` into the database the deployment had already migrated (refused: not empty), backup commands in a plain `sudo -u hermclaw` shell (no database URL, no manifest key), credentials written to the wrong file name, a recursive `chown` that made the SSH keys unreadable for the services, `migrate` without the services' environment, the admin machine still pinning the old host key, the old runtime key left authorised on the workers, the new admin token not valid after the restore. Fixed: one 12-step procedure (bootstrap → credentials → deploy → stop → empty database → `sudo hermclaw backup restore-test/restore-full` → files with correct ownership → `sudo hermclaw migrate` → workers/GitLab key → start → new admin token → fresh backup), the same order in the runbook and in INSTALLATION.md 11.7.

### BUG-137 – Installation guide and runbooks: wrong commands, paths and references
- Phase: P41 (Doku-Abgleich) · Komponente: docs · Severity: P3 · blockierend: nein · Status: **behoben** (55 bestätigte Befunde des Workflows `install-docs-verify` + 16 der Nachprüfung, Abschlussprüfung ohne Befund)
- Reproduktion: commands of INSTALLATION.md, admin.md, cutover.md, upgrade.md, deployment.md checked against the code and roles.
- Erwartet: every command, flag, path, variable and route as the code implements it.
- Tatsächlich (Auswahl): Secure-Boot/MOK order impossible (also in the role's own message), web server name without the address (requests by IP hit Debian's default site), worker-token rotation without worker restart, `hermclaw-credential` called without its path/sudo, `curl -d` without JSON content type (422), `limit_except` in server context, `check-host.sh` without `--role`, `hermclaw-model-pull --dry-run` as go criterion (never fails), missing `-K`, non-existent `/api/events` route, artifact download route and UI tab name, `logging.yaml` keys that do not exist, `credstore_dirs` that the unit cannot read, a sandbox database on `.225` (localhost only), `idle_sleep_command` "remove" (only `null` works). All fixed in the documents and role messages.

### BUG-138 – `hermclaw config-check` accepted a models.yaml that violates the fixed model architecture
- Phase: P41 (Doku-Abgleich) · Komponente: cli/deploy · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_cli.py::test_config_check_refuses_a_violation_of_the_fixed_model_architecture`)
- Reproduktion: inventory override that disables the profile `planner-gemma-fallback` (as the guide used to suggest), then `playbooks/deploy.yml`.
- Erwartet: the deployment stops in its staging check (`hermclaw config-check` of the new release) before anything live changes.
- Tatsächlich: config-check validated only the schema; `hermclaw litellm-config` refused the configuration (`MODEL_ARCHITECTURE_VIOLATION`) after the drain, the migration and the switch of the live configuration – `.225` was left without running services. Fixed: config-check runs `assert_architecture` (exit 2 with the violation on stderr, warnings in the JSON); docs say the fallback planner is mandatory.

### BUG-139 – The scheduler could suspend `.224` in the middle of the deployment's model download
- Phase: P41 (Doku-Abgleich) · Komponente: deploy/wol · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_deploy_infra.py::test_model_pull_follows_models_yaml_and_survives_a_dry_run`)
- Reproduktion: `playbooks/deploy.yml` with the models missing on `.224` (≈ 70 GB, 1–3 h) while the scheduler runs on `.225`.
- Erwartet: the download finishes.
- Tatsächlich: the role started `hermclaw-model-worker` before the pulls; its heartbeats made `.224` `ready`, and after `idle_after_minutes` without steps the idle policy suspended the host over SSH – a download does not count as activity. Fixed: `models.yml` runs before the daemon start and stops a running daemon while tags are missing (an offline worker is never suspended); the daemon starts right after the pulls. Dry runs and runs without missing tags leave the daemon alone (asserted).

### BUG-140 – Control-scope tokens could plant runtime-reserved job metadata keys, including SSH approvals (privilege escalation)
- Phase: P41 (Sicherheitsprüfung nach RC) · Komponente: api/runtime · Severity: P1 · blockierend: ja · Status: **behoben** (`tests/integration/test_api_job_metadata.py::test_control_token_cannot_pre_approve_an_ssh_mutation`, `::test_each_reserved_key_is_refused_before_anything_is_written`, `::test_spelling_variants_and_reserved_prefixes_are_refused`, `::test_admin_cannot_plant_reserved_keys_either`, `::test_reserved_key_is_checked_before_repository_resolution`, `::test_free_form_metadata_is_stored_and_the_typed_fields_win`; `tests/unit/test_job_metadata.py::test_every_job_metadata_write_is_a_reserved_key`, `::test_every_job_metadata_read_is_a_reserved_key`, `::test_runtime_uses_the_registry_constants`, `::test_scan_finds_the_known_writers`, `::test_scan_sees_reads_through_local_names_and_wrappers`)
- Reproduktion: with a token that has only the `control` scope, send `POST /api/jobs` with `{"prompt": "…", "metadata": {"ssh_approvals": ["ssh-<token>"]}}`. Other plants: `{"triage": "x"}`, `{"replan_requested": false}`, or `{" ssh_approvals ": […]}` (the contract strips the whitespace from the key).
- Erwartet: Runtime state in `jobs.metadata` is not client-settable. SSH mutation approvals need the `admin` scope (`POST /api/jobs/{id}/ssh-approvals`).
- Tatsächlich: `create_job` stored `{**body.metadata, …}` without any filter:
  - `ssh_approvals` was accepted by `consume_approval` as an operator approval. The token is predictable for a call without a step id. Planted approvals also hid real pending requests in the listing.
  - `ssh_approvals_consumed` forged the `approved_by` attribution.
  - `triage` skipped the fast router and steered the planner. A non-dict value crashed prepare, replan or finalize.
  - A falsy `replan_requested` stopped dispatch for good (`has_key`) without ever starting a replan, so the job hung in `running`. A truthy value forced a replan outside the replan budget.
  - The planner provenance markers (`planner_model_fallback` and others) could be forged.
- Fix:
  - New registry `hermclaw/core/job_metadata.py`. It holds every key the runtime or API writes or trusts: the SSH approval keys, triage, replan/retry, recovery, planner provenance and the three typed per-job flags. It also reserves the prefixes `_` and `hermclaw.`.
  - `POST /api/jobs` checks the metadata first, before repository auto-registration, the job row or any event (pinned by `test_reserved_key_is_checked_before_repository_resolution`; the row counts alone cannot show the order, because the API session rolls back every refusal). A reserved key gets 422 `JOB_METADATA_RESERVED_KEY` in the API error format, naming the offending keys. Keys are compared after NFKC normalisation and case folding, with whitespace and format characters removed.
  - Every writer and reader uses the registry constants.
  - A guard test scans `hermclaw/` for job metadata writes and reads with keys that are not reserved or are string literals. Reads also count through a local name the function loads via `select(Job.metadata_)` or `_locked_metadata` (`meta.get(K)`, `meta[0]`, `_list(meta, K)`): that is how the SSH approval readers in `sshtools/approvals.py`, `sshtools/tools.py` and `api/approvals.py` read, and the scan's sanity test asserts that it finds them. The scanned forms are listed in `docs/architecture/api.md` §2.
  - The `JobCreate.metadata` schema description lists the keys (`docs/contracts/openapi.json` regenerated). The API doc is `docs/architecture/api.md`.
- Hinweis: free-form keys such as `ticket` and a job-level `sandbox_image` remain client-settable. The runtime reads `sandbox_image` only from repository metadata, checked against `policies.sandbox`. No in-repo client (UI, CLI, soak, E2E seed) sends metadata, so none is affected. No release has been deployed (BLOCKER-001), so there are no existing rows with planted keys.

### BUG-141 – Bootstrap and deployment of `.223` would have failed on, or harmed, the legacy portal sharing the host
- Phase: D-013 (Betreiberentscheidung: `.223` behält das Portal Noob2Claw/Kowacore) · Komponente: infra/ansible, deploy, infratools · Severity: P2 · blockierend: ja (für `.223`) · Status: **behoben**
- Reproduktion: `playbooks/bootstrap.yml --limit webui` and `playbooks/deploy.yml` against a Debian 13 host whose
  Apache 2.4 owns port 80. The host also has Debian's `nodejs` + `npm` and an active ufw.
- Erwartet: Hermclaw's nginx runs next to Apache, and nothing of the portal changes.
- Tatsächlich:
  - **Preflight:** `check-host.sh` failed on `port:80`, because webui planned 80/443. The fail-safe then aborted
    every host's deployment (`any_errors_fatal`). Any workaround (`--allow-conflicts`, only `nginx_listen_port`)
    led to the following problems.
  - **Node.js:** The `nodejs` role pinned NodeSource over Debian's nodejs and then failed its assert. An upgrade
    would have removed Debian's `npm`, which the portal uses.
  - **nginx package:** The fresh install enabled Debian's default site (`listen 80 default_server`) and
    `nginx.service`, so nginx competed with Apache for port 80 at every boot.
  - **Port check:** `nginx -t` accepts a busy port. 'Enable and start nginx' sat outside the rescue block, so a
    bind failure left a `:80` site enabled.
  - **Time:** The time zone of PHP/MariaDB/cron was force-set.
  - **Accounts and directories:** Existing accounts and directories with Hermclaw names would have been rewritten.
  - **Host header:** The API saw `Host` without the port, so an absolute redirect (e.g. a trailing slash) pointed at
    the portal on port 80.
  - **Cookies:** The portal's session and 2FA-device cookies were forwarded to the API.
  - **Probe and port plan:** The `hosts.yaml` probe and the runtime port plan named `.223:80` as nginx.
- Fix (D-013):
  - **Ports:** Hermclaw uses `hermclaw_webui_http_port`/`hermclaw_webui_https_port` (8080/8443). The port plan,
    check-host's table, the nginx defaults, `hosts.yaml`, the health URLs and the portal button follow them.
  - **Shared-host mode** (`hermclaw_webui_shared_host`):
    - 80/443 and HSTS are refused;
    - nginx is installed with `policy_rc_d: 101`, and the package's default-site symlink is disabled;
    - every listener of `nginx -T` is checked, and so are the live sockets after the start (nginx is stopped again
      if it took a reserved port), with the start inside the rollback block;
    - NodeSource is skipped, and a guard refuses to replace Debian's npm;
    - the time setup is only read;
    - foreign accounts and directories are refused before anything changes;
    - ufw `allow` rules are added only for the sources in `nginx_firewall_allow_from` (default on the shared host:
      its own IPv4 LAN) and only when ufw is active; an active ufw that allows neither nginx port while the list
      is empty stops the role on the shared host (a rule for every port from `.225` alone does not count).
  - **API proxy:** It sends `Host: $host:$server_port`, drops `Cookie` and hides `Set-Cookie`.
  - **Runtime port plan:** `.223:8080/8443` belong to nginx, `.223:80/443` to `legacy-portal`, plus the `hosts.yaml`
    ports.
- Regression Tests:
  - `tests/integration/test_bootstrap_infra.py::test_port_plan_is_in_sync_and_matches_bauplan`,
    `::test_role_defaults_per_group`, `::test_check_host_reports_json_and_refuses_non_debian`, the `nginx-shared-*`
    cases of `::test_role_input_validation`;
  - `tests/integration/test_bootstrap_roles.py::test_shared_host_*`, `::test_firewall_*`, `::test_nodejs_*`;
  - `tests/integration/test_bootstrap_nginx.py::test_shared_host_nginx_runs_next_to_a_foreign_server_on_port_80`,
    `::test_listen_ports_follow_the_web_host_ports`, `::test_api_proxy_headers_and_keepalive`;
  - `tests/integration/test_deploy_infra.py::test_hosts_yaml_probes_the_configured_nginx_port`,
    `::test_ui_health_and_portal_urls_follow_the_nginx_ports`;
  - `tests/unit/test_infratools_ports.py::test_legacy_portal_ports_collide_even_for_hermclaws_nginx`,
    `::test_port_plan_from_hosts_follows_the_deployed_ports`;
  - `tests/security/test_security_checks.py::test_nginx_site_never_forwards_cookies_to_the_api`.

## Template

### BUG-XXX – Titel
- ID / Zeit / Phase / Severity / Komponente
- Reproduktionsschritte:
- Expected:
- Actual:
- Logs/Evidence:
- Workaround:
- Blocking: yes/no
- Status: open/fixed/closed
- Regression Test:
- Fix Commit:
