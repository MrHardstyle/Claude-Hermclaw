# Session-Übergabe Hermclaw Next (Stand 2026-10-18, 17:05)

Dieses Dokument trägt den Arbeitsauftrag, die Arbeitsregeln des Betreibers und den aktuellen Stand in neue
Claude-Code-Sessions (CLI oder Web). Es ersetzt das lokale Gedächtnis (`~/.claude/projects/.../memory/`), das nur auf
dem Orchestrator-Host (.225) liegt. Verbindlich bleiben `CLAUDE.md` und die Dateien, die es nennt.

## 1. Auftrag

- Live-Abnahme und Weiterentwicklung von Hermclaw Next bis zum vollständig getesteten Release Candidate.
- Ziel: ein stabiles, effizientes System, das auch lange und schwere Aufträge autonom zu Ende bringt.
- Branch `claude/hermclaw-next-build`. Commits enden mit `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## 2. Teststrategie (Betreiber, 2026-10-18) – gilt ab sofort

Nicht nach jedem Fehlschlag einzeln patchen. Ein Zyklus läuft so ab:

1. Eine feste Runtime-Version ausrollen.
2. Darauf die Testzyklus-Batterie laufen lassen (`config/acceptance-jobs-cycle.example.yaml`):

   | Auftrag | Ziel | Fixture |
   |---|---|---|
   | K1 – Python | CSV-Dateien auswerten, fehlerhafte Eingaben behandeln | `acceptance/empty` |
   | K2 – JavaScript | bestehendes Formular reparieren, Randfälle testen | `acceptance/form` |
   | K3 – PowerShell | Windows-Dienste auslesen, als JSON ausgeben (Sandbox ohne pwsh) | `acceptance/empty` |
   | M1 – Webanwendung | Aufgabenverwaltung mit SQLite und Weboberfläche | `acceptance/empty` |
   | M2 – Fremdprojekt | Fehler in bestehendem Code finden und beheben | `acceptance/legacy` |
   | M3 – API | REST-API mit Authentifizierung, Tests, Dokumentation | `acceptance/empty` |

3. Fehler über den ganzen Zyklus sammeln und ihre gemeinsamen Ursachen analysieren.
4. Allgemeine Fixes mit Regressionstests als **ein** Fixpaket entwickeln.
5. Neue Version ausrollen, dieselben Aufträge erneut prüfen.
6. Den Großauftrag G1 (`config/acceptance-jobs-grand.example.yaml`) erst wieder starten, wenn kurze und mittlere
   Aufträge zuverlässig laufen.

Weitere Regeln:
- Während eines Zyklus wird nicht deployt. Ausnahme: Datenverlust oder Sicherheitsproblem, dann sofort reagieren.
- Gemessen und berichtet wird die **Erfolgsquote je Version**, etwa „18 von 20 Aufträgen“, nicht die Zahl neuer
  Bugnummern.
- „Erfolg“ heißt Prozess-PASS der Batterie **und** fachlich korrekter Code. Je Auftrag wird der gelieferte Code
  geprüft: Programm und Tests ausführen, ruff, jede Anforderung des Auftrags einzeln.

## 3. Arbeitsregeln (aus dem Betreiber-Gedächtnis)

- **Ursache zuerst:** Bei Fehlschlägen zuerst alle Events und Tool-Calls der Jobs analysieren. Gesucht ist die
  früheste vermeidbare Entscheidung, nicht der letzte Fehler. Nur allgemeine Fixes, nie auf einen konkreten Testauftrag
  zugeschnitten (keine festen Dateinamen, Frameworks oder Testfall-Umgehungen). Jeder Fix bekommt einen
  Regressionstest, der ohne den Fix fehlschlägt.
- **Selbstprüfung je Fix:** Kompensiere ich wieder dasselbe Symptom unter neuem Namen, etwa eine Schwäche des
  Coder-Modells? Dann lieber die Ursache eine Ebene höher angehen: Modell, Runtime, Architektur. Das Urteil steht im
  BUGS-Eintrag.
- **Autonomie:** Committen, pushen, Release bauen und per Ansible deployen ist freigegeben. Zwischenbemerkungen
  pausieren die Arbeit nie; gestoppt wird nur auf ausdrückliche Anweisung. Änderungen außerhalb des schriftlichen
  Auftrags und Modellversuche sind vorab freigegeben.
- **Modellfreiheit:** Modelle, Rollen und Inferenz-Runtime dürfen gewechselt werden, auch abweichend von den Rollen in
  CLAUDE.md. Die Entscheidung fällt nach Batterie-Messung (gleiche Aufträge, Erfolgsquote, Dauer, Wiederholungen) und
  wird in DECISIONS.md dokumentiert.
  - Produktiv laufen seit rc16 alle Chat-Rollen auf `qwen3.6:35b` per llama.cpp auf .224 (D-015/D-016), mit 32K Kontext.
  - Embeddings laufen weiter auf Ollama.
- **Pro Bug:** Eintrag in BUGS.md (deutsch, neueste oben) und Fix mit Regressionstest. Außerdem aktualisieren:
  CHANGELOG `[Unreleased]`, STATUS (Zähler „N gefunden, N behoben“ und Produktivzeile), TEST_MATRIX und
  `docs/architecture/*`.

## 4. Harte Grenzen (wörtlich vom Betreiber)

- „NICHT ausführen: volle WoL-Suite (--full-wol) und Backup-Suite (beides noch nicht eingerichtet).“
- „Keine Geheimnisse ausgeben. Das Portal auf .223 (Apache :80, /var/www/noobclaw) nicht verändern. Vor Aktionen, die
  Daten löschen oder Dienste dauerhaft abschalten, nachfragen.“
- Suspend bleibt aus.
- Keine direkten DB-Schreibzugriffe auf Produktionsjobs.
- Keine benchmark- oder projektspezifischen Sonderfälle im Runtime-Core.

## 5. Aktueller Stand

- Produktiv ist **0.1.0rc59** (deployt 2026-10-18 16:46). Bugs: 274 gefunden, 274 behoben (BUGS.md).
- **Zyklus 1 läuft auf rc59** seit 2026-10-18 ~17:00: K1–K3 und M1–M3 nacheinander. Der Bericht entsteht als
  `/var/lib/hermclaw/acceptance/<stamp>-jobs.md` auf .225.
- G1 (Windows-11-Widget Proxmox/Unraid): letzter Lauf `0f3f2a32` abgebrochen (Strategiewechsel), davor `5c3d40a5` FAIL
  `REPLAN_LIMIT`. Die Befunde stehen in STATUS (Punkte 13–17) und BUGS (BUG-261–274).
- Beobachtete Hauptschwäche: Das Coder-Modell konvergiert schlecht beim Debuggen. Code und Tests werden zusammen
  geschrieben und widersprechen sich; danach stoppt die Stagnationserkennung. Ein Modell- oder Kontextversuch wäre
  ein Kandidat für ein Fixpaket nach Zyklus 1, zu entscheiden nach Messung.
- Offen und später: ein alter Test-MR; Vergrößerung der Partition von VM 104 (braucht Freigabe); alte
  `waiting_for_user`-Jobs.

## 6. Was nur auf dem Orchestrator-Host (.225) geht

Die Infrastruktur liegt im Heim-LAN (192.168.178.0/24) und ist von außen nicht erreichbar:
- PostgreSQL;
- GitLab .226 (Testrepository und Fixture-Branches);
- Modell-Host .224;
- Sandbox-Host .222;
- Ansible-Deploy und die verschlüsselten Zugangsdaten (systemd-creds).

Batterie-Läufe, Job-Analysen in der DB, Releases und Deploys gehen deshalb nur in einer Session auf .225.

Eine Cloud- bzw. Web-Session kann:
- Code ändern;
- Unit-Tests und lint ausführen (ohne die PostgreSQL-Integrationstests);
- Fixpakete auf einem Branch vorbereiten.

Sie kann dagegen nichts deployen und keine Live-Jobs ausführen.

Kommandos auf .225:
- Batterie: `sudo -n -u hermclaw hermclaw acceptance-jobs /var/lib/hermclaw/acceptance/jobs-cycle.yaml --out /var/lib/hermclaw/acceptance`.
- Fixtures: `scripts/ops/acceptance-fixtures.sh <repo-url> [name…]`.
- Release-Ablauf: Version anheben (pyproject, README, openapi.json, `hermclaw/__init__.py`, CHANGELOG), Schemas
  erzeugen, committen und pushen. Danach in `~/hermclaw-next` `git pull` und
  `scripts/deploy/build-release.sh`, das Inventory `hermclaw_version` anheben und
  `ansible-playbook playbooks/deploy.yml --become-password-file <aus systemd-creds>` ausführen.
- Prüfen mit `hermclaw --version` und `hermclaw doctor`.

## 7. Web-Session starten und zurückholen

- Start aus der CLI im Repo: `claude --cloud "Lies CLAUDE.md und docs/SESSION_HANDOFF.md, dann <Auftrag>"`.
  - Die Session klont den **gepushten** Stand des aktuellen Branches von GitHub. Ungepushte Commits fehlen.
  - Folgenachrichten: `claude -p "<Nachricht>" --cloud <session-id>`.
- Mitgenommen werden `CLAUDE.md`, `.claude/`-Projektdateien und alle Repo-Dateien, also auch dieses Dokument, STATUS
  und BUGS.
- Nicht mitgenommen werden das lokale Gedächtnis, `~/.claude/CLAUDE.md` und Benutzereinstellungen. Deshalb gibt es
  dieses Dokument.
- Zurück ins Terminal auf .225: `claude --teleport <session-id>` oder `/teleport`. Voraussetzungen: sauberer
  Git-Stand, Branch gepusht, gleiches claude.ai-Konto.
- Eine Web-Session soll ihre Änderungen auf einen eigenen Branch pushen. Release und Deploy macht danach eine Session
  auf .225.
