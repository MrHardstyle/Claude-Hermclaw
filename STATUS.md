# STATUS

## Aktueller Zustand (2026-10-18)
- Projekt: Hermclaw Next · Branch: `claude/hermclaw-next-build` · Version `0.1.0rc26`; **deployt: 0.1.0rc26** auf .222–.225 (Deployment durch Claude, Become-Passwort per systemd-creds)
- Modell-Runtime seit rc16 (D-015): alle Chat-Rollen auf `qwen3.6:35b` in einem residenten `llama-server` auf .224 (`--n-cpu-moe 30`, 32K); Embeddings auf Ollama.
- **Live-Abnahme nach Kapitel 12 durchgeführt** (2026-10-16): 396 Schritte erledigt, 34 mit offenem Live-Teil (`[~]`), 0 offen.
- 0.1.0rc2 deployt (vom Betreiber, alle Hosts inkl. .224): doctor 19 ok, models 6/6 (Fallback, BUG-150 wirksam), workers 3/3.
- Erster Testauftrag erneut eingereicht (Job `eadf1b6f…`): Die Erstplanung ist jetzt gültig (BUG-143 wirkt, Steps mit Scope),
  der Job scheiterte aber im Replan → BUG-153 bis BUG-157 (Replan-Fehler stufenweise gemeldet, falscher failed_step,
  generisches Sandbox-Image im leeren Repository, Coder versuchte `pip install`). Behoben in 0.1.0rc3 (deployt).
- Testauftrag auf 0.1.0rc3 (Job `c04f9fb8…`): erster Replan gültig (BUG-153/156 wirken), dann Stagnation durch eine liegengebliebene
  Datei (BUG-158) und ein zweiter Replan ohne Scope-Felder (BUG-159, Ollama-Grammatik). Behoben in 0.1.0rc4 (deployt, doctor 19 ok, `egress.enabled: true` auf .225/.222).
- Testauftrag auf 0.1.0rc4 (Job `bec4c5e9…`): Erstplanung und Replans gültig, Installationen über den Proxy gelangen; der Job
  scheiterte (`REPLAN_LIMIT`) am ersten Proxy-Start (300-s-Timeout, BUG-160) und an einem `pytest==7.4.3`-Pin, der die
  Test-Toolchain des Images überdeckte (BUG-161). Behoben in 0.1.0rc5 (deployt).
- Testauftrag auf 0.1.0rc5 (Job `b0080372…`): Proxy-Start und Installation fehlerfrei; nach dem Replan wiederholte der Coder
  `pip install` in `run_command` statt `install_dependencies` (Stagnation, BUG-162). Behoben in 0.1.0rc6 (deployt, doctor 19 ok).
- Testauftrag auf 0.1.0rc6 (Job `e3cab55e…`): keine `pip install`-Schleife mehr; der Planner wählte Node. Eine von einem
  abgebrochenen Step liegengebliebene Datei war als Repo-Hinweis nicht beschreibbar (BUG-163), und Server/Tests ohne
  `close()` blockierten `run_test` je 600 s (BUG-164). Behoben in 0.1.0rc7 (deployt, doctor 19 ok).
- Testauftrag auf 0.1.0rc7 (Job `837dcd50…`): BUG-163/164 wirken; der Coder installierte das unveränderte Manifest
  wiederholt, statt das fehlende `httpx` einzutragen, und schloss einen erledigten Step nicht ab (BUG-165). Behoben in 0.1.0rc8 (deployt).
- Testauftrag auf 0.1.0rc8 (Job `0a98a5eb…`): gescheitert (Recherche „populärste Frameworks“, FastAPI, eigenes Paket nicht importierbar).
- **Ursachenanalyse aller acht Live-Jobs** (`docs/analysis/live-job-root-causes-2026-10-16.md`, Betreiberauftrag): früheste
  vermeidbare Entscheidung war jedes Mal eine Web-Recherche über das eigene Repository bzw. die Framework-Wahl; dazu Code und
  Test in getrennten Steps, keine Präferenz für einfache Lösungen, ein Verifier-False-Negative (`must_change: ["*"]`) und
  nicht importierbare Projektpakete. Allgemeine Fixes BUG-166 bis BUG-169 in **0.1.0rc9**; neue Live-Auftragsbatterie
  `hermclaw acceptance-jobs` mit gemessenen Kriterien (J1–J6). 0.1.0rc9 deployt, Fixtures gepusht.
- Batterie-Lauf 1 auf 0.1.0rc9, J1 (Job `04820ff9…`): Erstplan 2 Steps, keine Recherche, Code+Test in einem Step; Coder
  fertig mit grünen Tests im ersten Versuch – Verifier lehnte wegen fehlendem Test-Kriterium ab (BUG-170). Lauf nach J2
  abgebrochen; Fix in 0.1.0rc10. J2 (Job `50784df5…`): Coder ohne Gedächtnis über Züge (liest dieselben Dateien,
  wiederholt grüne Testläufe, BUG-171); Fix in 0.1.0rc11 (enthält rc10, deployt).
- **Batterie auf 0.1.0rc11 mit AVX2: J1 erstmals `succeeded`** (Job `d50dc59e…`, 10,3 min, 2 Steps, kein Replan, Verifier
  bestanden). Offen laut Kriterien: kein MR (Policy aus → im Inventar eingeschaltet), neue Abhängigkeit FastAPI (BUG-174),
  Wiederholungssperre nach Installation (BUG-173). Fixes in **0.1.0rc12**.
- **Hardware (2026-10-17):** Alle VMs liefen mit CPU-Typ `x86-64-v2-AES` (kein AVX/AVX2/FMA); die 18-GB-Modelle rechnen
  größtenteils auf der CPU. Mit Freigabe des Betreibers VM 102 (.222) und VM 104 (.224) über die Proxmox-API auf
  `x86-64-v3` umgestellt (VM 105/.225 folgt später). Messung auf .224 (Generierung): qwen3-coder:30b 2 → 18,9 Token/s,
  gemma4:26b 5–7 → 27,6, qwen3:8b 4 → 20,0, qwen3.8:27b 0,8 → 4,7. Beim Neustart fiel DNS auf .222 aus (BUG-172, behoben).
- **D-014 (Betreiberentscheidung 2026-10-16): Abhängigkeits-Installation** über einen Registry-Proxy auf .222 (P43). Live auf .222
  verifiziert (pip und npm durch den Proxy, alles andere blockiert); in 0.1.0rc4, eingeschaltet mit `hermclaw_config_dependency_installs: true`.
- Blocker: BLOCKER-001 und BLOCKER-003 aufgehoben (GitLab-Suite PASS); BLOCKER-002 (Tag-Push) unverändert.
- Release-Status: **RC – live abgenommen bis auf** Live-Auftragsbatterie J1–J6 (`hermclaw acceptance-jobs`, zwei Läufe in Folge),
  Wake-on-LAN (nicht eingerichtet), Backup nach Terra (nicht eingerichtet), Soak-Test, Cutover.

## Fortschritt BUILD_PLAN (automatisch: `python scripts/dev/plan_progress.py --markdown`)
| Phase | erledigt | erledigt, live offen (BLOCKER-001) | offen |
|---|---|---|---|
| P00 – Initiale Research- und Inventurphase | 10 | 10 | 0 |
| P01 – Clean System Bootstrap | 14 | 1 | 0 |
| P02 – Backend Skeleton | 9 | 1 | 0 |
| P03 – Persistence | 9 | 0 | 0 |
| P04 – Event Store | 8 | 0 | 0 |
| P05 – State Machines | 6 | 0 | 0 |
| P06 – Git Engine | 12 | 0 | 0 |
| P07 – Worker Protocol | 9 | 0 | 0 |
| P08 – Model Gateway | 10 | 1 | 0 |
| P09 – Resource Manager | 9 | 0 | 0 |
| P10 – Wake-on-LAN | 2 | 7 | 0 |
| P11 – Repository Intelligence | 12 | 0 | 0 |
| P12 – Research Engine | 13 | 0 | 0 |
| P13 – Contracts | 12 | 0 | 0 |
| P14 – Gemma Planner | 10 | 0 | 0 |
| P15 – Scope Engine | 9 | 0 | 0 |
| P16 – Context Builder | 9 | 0 | 0 |
| P17 – Tool Engine | 13 | 0 | 0 |
| P18 – Execution Sandbox | 9 | 0 | 0 |
| P19 – Main Coder | 7 | 1 | 0 |
| P20 – Stagnation Detection | 8 | 0 | 0 |
| P21 – Deterministic Verifier | 14 | 0 | 0 |
| P22 – Heavy Review | 5 | 1 | 0 |
| P23 – Correction Pipeline | 6 | 0 | 0 |
| P24 – Replanning | 5 | 1 | 0 |
| P25 – Scheduler | 9 | 0 | 0 |
| P26 – SSH/Admin Tools | 8 | 0 | 0 |
| P27 – DB Tools | 7 | 0 | 0 |
| P28 – Container/Proxy Tools | 6 | 0 | 0 |
| P29 – Media | 7 | 0 | 0 |
| P30 – API | 9 | 0 | 0 |
| P31 – UI | 15 | 0 | 0 |
| P32 – Deployment | 9 | 1 | 0 |
| P33 – Backup/Restore | 5 | 1 | 0 |
| P34 – Recovery | 7 | 0 | 0 |
| P35 – Failure Injection | 19 | 0 | 0 |
| P36 – Security Hardening | 8 | 0 | 0 |
| P37 – Generalization E2E | 15 | 0 | 0 |
| P38 – Performance / Load | 8 | 0 | 0 |
| P39 – FULL BUG BASH / HARDENING | 13 | 0 | 0 |
| P40 – SOAK TEST | 6 | 3 | 0 |
| P41 – RELEASE CANDIDATE | 8 | 1 | 0 |
| P42 – CUTOVER PLAN | 2 | 4 | 0 |
| P43 – DEPENDENCY INSTALLS (D-014, Betreiberentscheidung 2026-10-16) | 5 | 1 | 0 |
| **Summe** | **396** | **34** | **0** |

## Deploymentstatus pro Host
| Host | Rolle | Status (2026-10-16) |
|---|---|---|
| 192.168.178.225 | Orchestrator | deployt 0.1.0rc3; API/Scheduler/LiteLLM/SearXNG aktiv; doctor 19 ok / 0 fail; Live-Suiten s. u. |
| 192.168.178.222 | Coding / Execution Worker | deployt 0.1.0rc3; Sandbox-Suite 25/25; `podman-compose` 1.3.0 nachinstalliert (BUG-149, per Ansible-Rolle) |
| 192.168.178.224 | Model / Media Worker | deployt 0.1.0rc3; Ollama 0.40.4, 6 Rollenmodelle; gemma4:12b@32K braucht `gpu_layers: 30` (BUG-150, wirksam) |
| 192.168.178.223 | Kontrollraum 8080/8443 neben dem Portal (Apache :80, D-013) | deployt; 8080→8443, API-Proxy ok; Portal unverändert |
| 192.168.178.226 | GitLab | Push + Merge Request ok (gitops-Suite PASS) |
| 192.168.178.60 | Terra / Unraid Backup | noch nicht eingerichtet (Backup-Suite nicht ausgeführt) |

Leerlauf-Suspend von exec-222/model-224: aus (`idle_sleep_command: null`, Wake-on-LAN `enabled: false`) – war schon vor der
Abnahme so deployt. Einschalten erst mit funktionierendem WoL (R-042).

## Live-Abnahme 2026-10-16 (Kapitel 12)
| Suite | Host | Ergebnis |
|---|---|---|
| doctor | .225 / .222 | PASS (19 ok) / PASS |
| workers | .225 | PASS 3/3 |
| models | .225 | 6/6 nach Deployment 0.1.0rc2 (vorher 5/6, BUG-150) |
| repo_intelligence | .225 | PASS 1/1 |
| research | .225 | PASS 2/2 (nach BUG-148) |
| sshtools | .225 | PASS 4/4 |
| infratools | .225 | PASS 3 + 1 Skip (`nginx -t` ohne Sudo auf .223, gewollt R-038) (nach BUG-149) |
| wol (ohne `--full-wol`) | .225 | PASS `test_live_probes`; Magic Packet Skip (WoL deaktiviert), DB-Pipeline nicht ausgeführt |
| gitops (`local-ai/hermclaw-test`) | .225 | PASS 1/1 nach Vergabe des Schreibrechts (vorher BLOCKER-003); offener Test-MR in `hermclaw-test` kann geschlossen werden |
| sandbox | .222 | PASS 25/25 (nach BUG-146/147); mit D-014-Live-Tests 28/28 (Stand rc5-Quellen, inkl. BUG-161) |
| backup, `--full-wol` | – | nicht ausgeführt (nicht eingerichtet, Auftrag) |

Lokale Gesamtsuite auf .225: 3137 passed, 146 skipped; 57 vorbestehende Umgebungsfehler (identisch am Ausgangsstand, s. TEST_MATRIX).
Lint, mypy, ShellCheck, ansible-lint (production) sauber.

## Modellrollen (`config/models.example.yaml`; Abweichung nur mit `deviation`, D-015 – produktiv seit rc16 alle Chat-Rollen auf `qwen3.6:35b`/llama.cpp, siehe oben)
- Planner/Replanner: `gemma4:26b` (32K) · Planner-Fallback (technisch): `gemma4:12b`
- Implementation Worker: `qwen3-coder:30b` (32K) · Heavy Reviewer: `qwen3.8:27b` · Fast Router: `qwen3:8b` (16K)
- Embeddings: `embeddinggemma:300m` (768 dim; EmbeddingGemma 2 erst, wenn Ollama es unter Linux/CUDA ausführt, D-010)

## Bugs
- 310 gefunden, **310 behoben** – `BUGS.md`. Live-Abnahme: BUG-143 bis BUG-310. BUG-275–280 sind mit 0.1.0rc60 behoben (Fixpaket nach Zyklus 1), BUG-281–293 mit 0.1.0rc61 (Fixpaket nach Zyklus 2); BUG-294–299 kommen mit 0.1.0rc62 (BUG-298/299 nur Ansible) (Fixpaket nach Zyklus 3 und Vorbereitung des Coder-Turniers, D-018); BUG-300–308 mit 0.1.0rc63 (Doctor, `replace_text`, Lint nach dem Schreiben, Verifier-Ausfall, Turnierwertung, Testberichte, Selbstlernen; Release nach dem Coder-Turnier mit Gedächtnis D-019 und Schwierigkeitsleiter D-020); BUG-309/310 mit 0.1.0rc64 (Startprüfung im Verifier, Log-Rauschen; dazu der stabile Prompt-Anfang D-021).
  BUG-143–293 sind produktiv (0.1.0rc61, deployt 2026-10-19 19:50: Fixpaket BUG-281–293 nach Testzyklus 2; der abschließende Doctor-Lauf scheiterte nur an der neuen Prüfung `gitlab`: Sidekiq auf .226 läuft nicht, 139 Jobs seit 22 h im Rückstand – Neustart durch den Betreiber offen; rc60 2026-10-19 12:19: Fixpaket BUG-275–280, Planer/Neuplaner auf qwen3.8:27b im llama.cpp-Router b11696, D-017; rc59 2026-10-18 16:46 mit laufendem G1; rc58 16:32; rc57 15:14; rc56 14:25; rc55 14:18; rc54 13:49; rc53 13:25; rc52 13:02; rc51 12:39; rc50 12:26; rc49 11:46; rc48 11:38; rc47 11:24; rc46 11:11; rc45 10:53; rc44 10:35; rc43 10:00; rc42 09:48; rc41 09:17; rc40 08:41; rc39 07:26; rc38 07:03; rc37 06:44; rc36 06:00; SearXNG mit Bing/Yahoo; rc33 05:06; rc29 04:23; rc28 03:48 mit laufendem G1, Worker-Ausfall beim Fortsetzen ohne Versuchsverlust (BUG-227 live bestätigt); rc26 02:55, `coder-main.max_output_tokens` 8192 im Inventory-Override; rc25 deployt 01:50 mit laufendem G1, Schritt per Checkpoint unterbrochen und fortgesetzt; rc24 deployt 2026-10-18 01:10; rc20 deployt 2026-10-17 20:40, Scheduler-Neustart mit laufendem Job live geprüft: Job lief nach dem Deployment weiter).
- Externe Blocker: BLOCKER-002 (Tag-Push)

## Letzte Testresultate (2026-10-15, Build-Host)
- Finale Testmatrix `docs/testing/test-matrix-results.md`: **208 Dateien, 3232 bestanden, 0 fehlgeschlagen**, 24 Live-Tests
  deselektiert (unit 1956, integration 1042, failure 204, security 4, e2e 18, load 8); UI: vitest 48, Playwright 35
- Deployment: `test_deploy_release` 22, `test_deploy_infra` 38, `test_deploy_converge` 8 (30 min, Debian 13), Bootstrap-Suiten grün,
  ansible-lint (production) sauber
- Dependency-Audit 2026-10-15: keine bekannten Schwachstellen (runtime 107, LiteLLM 116 Pakete, UI 0)

## Betreiberentscheidung D-013 (2026-10-15)
`.223` bleibt das Portal Noob2Claw/Kowacore (Login/2FA, Wiki; wird nicht neu installiert). Hermclaws nginx läuft
daneben auf 8080/8443. Ins Portal kommt nur der Menüpunkt **Arbeit → KI-Kontrollraum** mit dem Button zum
Kontrollraum. Umsetzung:

- Ansible: Rollen nginx, common, nodejs und hermclaw_ui, Portplan, `check-host.sh`;
- `hosts.yaml`-Probe, Laufzeit-Portplan;
- Doku: INSTALLATION, cutover.md, Runbooks, Architektur;
- Risiken R-036–R-040, BUG-141/142.

Tests 2026-10-15, einzeln ausgeführt: `test_bootstrap_infra` 63, `test_bootstrap_roles` 15, `test_bootstrap_nginx` 10,
`test_bootstrap_converge` 1, `test_deploy_infra` 64, `test_infratools_ports` 14 und `test_security_checks` 6, alle
bestanden; ansible-lint (production) sauber. `test_deploy_converge` ist angepasst; ihn führt der Integrator aus.

## Nächster Schritt
1. Batterie-Lauf 1 auf rc16 (erster Lauf mit qwen3.6/llama.cpp, `20261017T142643Z-jobs.md`): J1, J2, J3, J6 erfolgreich
   (3,3/4,7/10,1/2,9 min; J3/J6 nur durch die Metrik BUG-185 als FAIL gewertet), J4 `REPLAN_LIMIT` nach 12,4 min statt
   Rückfrage (BUG-187), J5 `PLANNER_INVALID_OUTPUT` (BUG-188). rc14 brauchte für J1–J6 Stunden bei 1/6.
2. Batterie-Lauf 1 auf rc17 (`20261017T152819Z-jobs.md`): J1/J2/J3/J5/J6 erfolgreich (3,4/3,7/9,6/11,5/3,1 min),
   J4 stellte nach 3,1 min korrekt die Rückfrage, FAIL nur durch die Dauer-Metrik (BUG-190); unnötige Korrekturrunde
   durch ein Minor-Review in J5 (BUG-191).
3. Batterie-Lauf 1 auf rc18 (`20261017T162518Z-jobs.md`): J1/J2/J3/J5/J6 erfolgreich (2,6/3,8/8,2/9,1/2,5 min);
   J4 lieferte nach 5,4 min Code mit einem Lazy-Import eines nicht existierenden Pakets in `main()`, den kein Test
   aufrief → BUG-193 (Verifier-Check `imports:python`).
4. Batterie-Lauf 1 auf rc19 (`20261017T181154Z-jobs.md`): J1/J3 erfolgreich (5,8/15,3 min), J4 korrekte Rückfrage nach
   2,5 min; J2 ohne MR (GitLab-Race, BUG-202), J5 unnötige Manifest-Änderung (BUG-203), J6 `REPLAN_LIMIT` durch
   widersprüchliches `must_not_change` (BUG-204).
5. rc20: J1 `REPLAN_LIMIT` durch liegengebliebene Änderungen eines ersetzten Schritts (BUG-206, auch J5 des
   verwaisten Parallel-Laufs).
6. Batterie-Lauf 1 auf rc21 (`20261017T201512Z-jobs.md`): J1–J4 und J6 bestanden (4,6/4,3/13,9/2,4/2,6 min), J5
   erfolgreich, aber Wiederholungs-Metrik 4 > 2 (BUG-207).
7. Erweiterte Batterie auf rc22: X1 (Datenanalyse-CLI) `waiting_for_user` nach 17,9 min. Die Tests waren in einen
   `test`-Schritt geplant, der nie schreibt (BUG-209), und der Replanner stellte neben einem fertigen Plan eine
   Bestätigungsfrage (BUG-208). Davor J1–J6 eines Laufs auf rc22 bestanden.
8. Erweiterte Batterie auf rc23 (`20261017T225223Z-jobs.md`): X1/X2/X4 PASS (12,3/5,6/25,3 min), X3 und X5 erfolgreich,
   aber Wiederholungs-Metrik (BUG-214) bzw. ohne MR (BUG-220). Die Qualitätsprüfung der gelieferten Dateien ergab
   BUG-215–219 (Lint, Randfälle im Review, README, Review gegen Werkzeug-Fakten, End-to-End-Aufruf von Programmen).
9. Großauftrag G1 (Windows-11-Widget für Proxmox VE und Unraid, `config/acceptance-jobs-grand.example.yaml`) auf rc24/rc25
   (`20261018T001631Z-jobs.md`): nach 78 min abgebrochen. Gefunden:
   - doppelte Recherchen (BUG-221);
   - deutsche Recherchen verloren ihre Belege (BUG-222);
   - große Dateien am Output-Limit abgeschnitten und sinnlos repariert (BUG-223);
   - der Replan verwarf Frontend, Tests und README still (BUG-224);
   - die Redaction maskierte Code und Platzhalter, sodass der Coder den Marker kopierte (BUG-225).

   Alle in rc25/rc26 behoben. Neustart von G1 auf rc26.
10. G1 auf rc26–rc28 (Job `8e7c4b65…`, `20261018T021717Z-jobs.md`): FAIL nach 98,3 min, `REPLAN_LIMIT`.
    Die Recherche ist jetzt belastbar: Unraid-API mit `x-api-key`, 11 von 11 Aussagen belegt. Gefunden:
    - Mega-Schritt mit 13 Dateien (BUG-226);
    - Deployment bei laufendem Schritt (BUG-227);
    - doppelt geschriebene Dateien (BUG-228);
    - die Wiederholungssperre lehnte Lesezugriffe ab, die der Kontext nur als Kopf zeigte, sodass der Schritt stagnierte (BUG-229);
    - zuletzt bestanden in Turn 20 von 20 alle 17 Tests, der Schritt wurde trotzdem verworfen (BUG-230).

    Mit rc28 gab es keine abgelehnten Lesezugriffe mehr. rc29 deployt 2026-10-18; Neustart von G1 auf rc29.
11. Qualitätsprüfung der G1-Lieferung von Job `8e7c4b65…` (eigener Review-Agent, schreibgeschützt):
    - Der Proxmox-Token-Header hat kein Secret, und Unraid nutzt `Bearer` statt `x-api-key`.
    - Fixtures in der falschen Form; die Tests prüfen nichts.
    - Pfad-Traversal im Static-Server; der Einstellungsbereich fehlt.
    - Frontend und Backend passen nicht zusammen.

    Daraus abgeleitet:
    - Die Recherche kam beim Coder nur verstümmelt an (BUG-231, auch beim Reviewer).
    - Keine geschriebene Schnittstelle zwischen Schritten, keine Regel gegen hohle Tests (BUG-234).
    - Der Planner umging die Regel „Code und Tests in einen Schritt“ (BUG-237).

    G1 auf rc29–rc32 (Job `364a9c3c…`) wurde abgebrochen, weil der Plan vor diesen Fixes entstand. Gefunden:
    - Ausfälle (BUG-232) und Checkpoints (BUG-233) verbrauchten Versuche;
    - Abhängige Schritte wurden trotz geplantem Retry blockiert (BUG-236);
    - Das Ausgabelimit des Coders lag bei 6144 statt 8192 (BUG-235).

    Mit rc30+ kommen die Recherche-Aussagen an: CONSTRAINTS 4488 statt 996 Zeichen. Neustart von G1 auf rc33.
12. G1 auf rc33/rc34 (Job `2f72c0da…`, `PLANNER_INVALID_OUTPUT` nach 25 min). Der Plan war gut geschnitten: Komponenten mit eigenen Tests, Schritt für Schritt. Gefunden:
    - Alle drei Web-Suchmaschinen waren gesperrt (Rate-Limit/CAPTCHA), und das galt als „keine Quellen“ (BUG-238). Behoben mit Bing/Yahoo und Ausfall-Retry.
    - Englische Suchanfragen zu deutschen Komposita wurden verworfen (BUG-239).
    - Fremde Gists und Community-Repos galten als Primärquellen, die offizielle Doku stand niedrig (BUG-240).
    - Der Replanner wiederholte alle offenen Schritte, lief ins Ausgabelimit, und ein ungültiger Replan beendete den Job (BUG-241).

    Neustart von G1 auf rc35.
13. G1 auf rc35–rc39 (Job `8f394b63…`, FAIL nach 135,5 min, `REPLAN_LIMIT`; nur die Recherche-Schritte abgeschlossen). Früheste vermeidbare Entscheidung: S003 schrieb einen eigenen TOML-Parser und -Writer, der verschachtelte Tabellen zerstörte (BUG-245). Danach:
    - Korrekturen aus dem Verifier enthielten keine Belege (BUG-247), und nach einem Deploy-Checkpoint setzte jeder Versuch veralteten Stand ohne Korrektur fort (BUG-249).
    - Ein maskiertes Test-Geheimnis ließ sich mit keinem Werkzeug ersetzen (BUG-248).
    - Weitere Funde: Schritte kannten die Dateien ihrer Vorgänger nicht (BUG-242), falsche Abhilfe bei Test-Geheimnissen (BUG-243), Teil-Lesezugriffe (BUG-244), Befehls-Verweigerung ohne sichtbare Ausgabe (BUG-246).
    - Auf rc39 kamen die Korrekturen an, und das maskierte Geheimnis wurde ersetzt (BUG-248 live bestätigt). Der Reviewer verlangte den Ersatz des eigenen Parsers (BUG-245-Regel live bestätigt); im letzten Versuch gelang das nicht mehr.
    - Mehrere Deploys mitten im Lauf erzeugten den Checkpoint-Zustand hinter BUG-249. Der Lauf ist deshalb kein Beleg für rc39 als Ganzes.

14. G1 auf rc39–rc45 (Job `3d90ec7b…`, FAIL nach ~180 min, `REPLAN_LIMIT`; nur die Recherche abgeschlossen). Neun Funde, jeder allgemein behoben:
    - Erster Plan: Stagnation durch identische Schreibzugriffe in S004 (BUG-253); `tomli_w` aus dem Schrittziel in S002 (BUG-254); Altlasten von S002 als Scope-Verstoß von S003 (BUG-255).
    - Replan 1: PowerShell-Befehl als Secret (BUG-256); S002 übernahm Frontend und Skripte der abgelösten Schritte (BUG-257, auf rc44 live bestätigt: sechs Dateien geparkt); der letzte Korrekturversuch fasste die beanstandete Testdatei nicht an (BUG-258).
    - Replan 2: Der Deploy von rc45 unterbrach einen Korrekturversuch. Der fortgesetzte Versuch arbeitete mit veralteter Lint-Ausgabe (BUG-259), und das abgebrochene Heavy Review galt als verbraucht, sodass die nächste Stagnation direkt zum Replan führte (BUG-260).
    - Lehre: Drei der Funde (BUG-249/259/260) zeigten sich erst durch Deploys mitten im Lauf. Es sind echte Fehler (ein Worker-Ausfall unterbricht genauso), der Lauf ist aber verzerrt.
    - Beobachtung ohne Fix: Der Reviewer meldete einmal eine Testdatei als fehlend, deren Diff gekürzt war (der Prompt verbietet das bereits). Wieder aufgreifen, falls es erneut auftritt.

    Neustart auf rc46 (`28f184c5`) scheiterte nach 4,5 min in der Planung (BUG-261). Neustart auf rc47 (`99f2f1ff`) scheiterte ebenfalls in der Planung (BUG-262). Neustart auf rc48 (`cbfe8802`) scheiterte nach 4 s an einer GitLab-Störung (BUG-263). Neustart von G1 auf rc49. Während des Laufs wird nur bei blockierenden Fehlern deployt.
15. G1 auf rc49–rc55 (Job `5c3d40a5…`, FAIL nach 150,5 min, `REPLAN_LIMIT`; nur die Recherche abgeschlossen). Plan v1 war nach 2 Reparaturen gültig (BUG-261/262 live bestätigt).
    - Früheste vermeidbare Entscheidung: S003 las sein eigenes, 845 Zeilen langes collector.py immer wieder von vorn. Das führte zu BUG-264 (Blättern), BUG-265 (Blättern als Schleife gewertet) und BUG-266 (geblätterte Seiten nicht im Kontext); Replan 1.
    - Replan 1 gab S008 ein Ziel in collector.py ohne die Datei im Scope (BUG-268). Gefunden wurden außerdem `pytest | tail` als bestanden gewertet (BUG-267) und verrutschte Felder (BUG-269); Replan 2.
    - In Replan 2 las S014 in vier Versuchen nur. Eine Änderung wurde am maskierten Passwort abgelehnt (BUG-270), danach forderte der Coder gezeigte Zeilen immer wieder an (BUG-271).
    - Neustart von G1 auf rc56.
16. G1 auf rc56–rc59 (Job `0f3f2a32…`) auf Wunsch des Betreibers abgebrochen (Replan 1; S002/S003/S008 an Testfehlern blockiert, S005 an der fehlenden API-Abhängigkeit → BUG-272–274).
17. **Strategiewechsel (Betreiber, 2026-10-18):** keine Einzel-Patches nach jedem Fehlschlag. Ablauf je Zyklus:
    - eine feste Version ausrollen;
    - darauf die Testzyklus-Batterie K1–K3/M1–M3 laufen lassen (`config/acceptance-jobs-cycle.example.yaml`, Fixtures `acceptance/form` und `acceptance/legacy`);
    - Fehler sammeln und gemeinsame Ursachen analysieren;
    - ein gebündeltes Fixpaket mit Regressionstests bauen;
    - die neue Version mit denselben Aufträgen erneut prüfen.
    - Gemessen wird die Erfolgsquote je Version. G1 läuft erst wieder, wenn kurze und mittlere Aufträge zuverlässig laufen. Sofortreaktion nur bei Datenverlust oder Sicherheitsproblemen.
    - **Zyklus 1 auf rc59 (abgeschlossen 2026-10-18 19:35):** Prozess-PASS **2 von 6** (K3, M1). Mit Codeprüfung **0 von 6** voll korrekt.
      - K1 succeeded, aber 42 min statt 30 und 1 Replan (BUG-276). Im Code: Zeilennummern der Warnungen um eins verschoben, `nan` wird akzeptiert.
      - K2 ungültig durch BUG-275 (jeder Befehl im Node-Image Exit-Code 2).
      - K3 PASS in 2,2 min. Im Code: ein einzelnes Ergebnis wird Objekt statt Array, kein Treffer gibt keine Ausgabe statt `[]`.
      - M1 PASS mit 1 Replan (BUG-277). Im Code: eine erledigte Aufgabe lässt sich nicht wieder öffnen, ein JSON-Body, der kein Objekt ist, führt zu 500.
      - M2 succeeded, aber erst nach 2 Replans (BUG-278: `must_change` auf nur gelesene Dateien, vom Neuplaner wiederholt). Der Code ist sehr gut.
      - M3 failed `REPLAN_LIMIT`: Testwerte wie `valid_token` und Beispiel-Tokens im README wurden als Geheimnis gemeldet und nie ersetzt (BUG-279).
      - Gemeinsame Ursachen:
        - Runtime-Prüfungen, die ein korrektes Ergebnis nicht erkennen (BUG-275, 276, 279);
        - Prüfkriterien des Planers auf Dateien, die nicht geändert werden müssen (BUG-277, 278);
        - fachliche Randfälle, die weder Plan noch Review einfordern.
      - Fixpaket BUG-275–279 ist fertig, die volle Suite zeigt keine neuen Fehler gegenüber der Basis.
      - Vor Zyklus 2 läuft das Planer-Turnier (R-033, Betreiberwunsch).
3. 0.1.0rc18 ausrollen, J1–J6 zweimal in Folge bestehen, dann die erweiterte Batterie X1–X5
   (`config/acceptance-jobs-extended.example.yaml`); danach Abbruch, parallele Jobs und Worker-Neustart prüfen.
4. Wake-on-LAN einrichten (MACs, BIOS, Kapitel 13.1), danach `--suite wol --full-wol` und erst dann den Leerlauf-Suspend einschalten.
5. Backup nach Terra einrichten (Kapitel 11), Backup-Suite; danach Soak-Test und Cutover (`docs/runbooks/cutover.md`).
6. Drittabhängigkeiten eines Projekts installiert der Coder über `install_dependencies` (D-014); ein Projekt-Sandbox-Image
   (`infra/sandbox-images/README.md`) bleibt für Systempakete nötig.
