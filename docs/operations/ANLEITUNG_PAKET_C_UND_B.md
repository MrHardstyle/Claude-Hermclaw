# Anleitung: Paket C (llama.cpp-Tuning) und Paket B (Modellfamilien) mit der Claude-CLI auf .225

Stand 2026-10-21 (Repo-Stand 0.1.0rc65). Für den Betreiber.

**Paket C** misst, ob llama.cpp auf .224 mit anderen Batch-, MoE- oder Spekulations-Einstellungen schneller rechnet.
Übernommen wird nur, was messbar besser ist.

**Paket B** macht Modellfamilien außer Qwen lauffähig (Gemma 4, gpt-oss, GLM-Flash) und misst sie im Coder-Turnier.

Hintergrund und Aufwandsschätzung stehen in der Entscheidungsvorlage vom 2026-10-19 (Strata, andere Modellfamilien,
Pro-Plan). Strata wurde verworfen: Es braucht ab 12 GB VRAM und 32–64 GB RAM, erzwingt JSON nicht per Grammatik und
kann nur Qwen.

## Ablauf

1. **Vorbereiten** auf .225:
   - `cd ~/hermclaw-next && git pull`;
   - `claude` starten;
   - `/model sonnet` (schont das Pro-Kontingent; Opus nur, wenn die Sitzung an einem Fehler hängt);
   - Modus **Auto**.
2. **Paket C starten.** Du musst den Prompt nicht kopieren. Tippe in die Sitzung:
   `Lies docs/operations/ANLEITUNG_PAKET_C_UND_B.md, Abschnitt „Prompt C“, und führe ihn aus.`
   - In Schritt 0 stellt dir die Sitzung **eine** Sammelfrage: laufende Jobs oder Leiter (D-020), Wartungsfenster für Stopps
     des Modellservers auf .224 (je höchstens 60 min) und der Weg zu Root-Rechten auf .224 (siehe unten).
   - Danach arbeitet sie ohne Rückfragen bis zur Übernahme.
3. **Nach Paket C:** `/clear`, dann tippen:
   `Lies docs/operations/ANLEITUNG_PAKET_C_UND_B.md, Abschnitt „Prompt B“, und führe ihn aus.`
   - Paket B verteilt sich auf mehrere Sitzungen (Teile 1a, 1b, 1c, 2, 3a, 3b, optional 4).
   - Nach jedem Teil sagt die Sitzung, dass du `/clear` ausführen sollst. Danach tippst du denselben Satz erneut ein.
   - Der Fortschritt steht in `STATUS.md` im Abschnitt „Paket B“.

### Root-Rechte auf .224

Seit dem Deploy-Wrapper (`hermclaw-deploy`) geht das Become-Passwort nicht mehr durch die Shell der Sitzung. Für die
Messungen (Dienst stoppen, `llama-bench` als `ollama`) und für den Rollout der Rolle `ollama` (`bootstrap.yml`) braucht
die Sitzung trotzdem Root auf .224. Sie fragt dich in Schritt 0 nach einer dieser Optionen:

- **(a)** Die Sitzung schreibt fertige Skripte, und du führst sie auf .224 bzw. .225 selbst aus.
- **(b)** Du legst für die Dauer des Pakets auf .224 eine sudoers-Datei für `kiworker` an und entfernst sie danach:
  ```bash
  echo 'kiworker ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/90-hermclaw-tuning && sudo chmod 0440 /etc/sudoers.d/90-hermclaw-tuning
  sudo rm /etc/sudoers.d/90-hermclaw-tuning    # nach dem Paket
  ```
- **(c)** Die Sitzung erweitert `hermclaw-deploy` um einen Modus für `bootstrap.yml --tags ollama`, und du installierst den
  Wrapper danach neu. Für die Messungen auf .224 braucht es trotzdem (a) oder (b).

### Was die Prompts für diese Sitzungen ändern

Beide Prompts heben ausdrücklich zwei Regeln auf, um das Pro-Kontingent zu schonen:

- Die Sitzung liest nicht alle neun Dateien aus CLAUDE.md, das wären rund 150.000 Tokens pro Start.
- Die Handoff-Regel „gestoppt wird nur auf Anweisung“ gilt hier nicht: B hört nach jedem Teil auf, und beide fragen an
  festen Punkten.

Willst du das nicht, streich in den Prompts die Zeilen unter „Rahmen und Vorrang“.

### Wann die Sitzung dich fragt

- **C:** Sammelfrage in Schritt 0; Downloads ab 5 GB; Dienst startet nach 10 min nicht wieder.
- **B:**
  - knapper Plattenplatz: einmal, mit Zahlen und drei Optionen;
  - Community-GGUFs;
  - Login oder Token für ein Repo;
  - Release während einer offenen Leiterstufe;
  - Umstellung von `coder-main`;
  - Teil 4.

### Grober Zeitbedarf (Schätzung vom 2026-10-19)

| Paket | Claude-Arbeitszeit | Laufzeit auf .224 | deine Zeit |
|---|---|---|---|
| C | 2–4 h | 3–6 h (in Stoppfenstern) | ~1 h |
| B | 8–16 h (verteilt auf 6–7 Sitzungen) | 1–1,5 Tage | 2–4 h |

---

## Prompt C

```text
Paket C: llama.cpp auf .224 nachjustieren. Erst messen, dann nur übernehmen, was messbar besser ist.

## Rahmen und Vorrang
- Du arbeitest in der CLI-Sitzung auf .225 im Repo `~/hermclaw-next`, Branch `claude/hermclaw-next-build` (Remote `origin` auf GitHub, nicht GitLab).
- Ausdrückliche Anweisung für diese Sitzung, sie geht CLAUDE.md und `docs/SESSION_HANDOFF.md` §3 vor:
  - Du liest zu Beginn nur:
    - `docs/SESSION_HANDOFF.md` §3, §4 und §6;
    - in `DECISIONS.md` die Zeilen D-015 bis D-021 (grep);
    - `docs/architecture/tournament.md`;
    - in `docs/operations/deployment.md` den Abschnitt „Deploy wrapper“;
    - in `STATUS.md` nur den Abschnitt „## Bugs“.
  - „Aktueller Zustand“ in STATUS und §5 im Handoff sind veraltet.
  - Meine Rückfragepunkte unten gelten, obwohl §3 sagt, gestoppt werde nur auf Anweisung.
  - Alle anderen Regeln aus CLAUDE.md und §4 gelten.
- Sparsam arbeiten (Pro-Plan):
  - keine Workflows, keine Agent-Teams, keine parallelen Subagenten;
  - `BUGS.md`, `BUILD_PLAN.md` und `CHANGELOG.md` öffnest du nie ganz. Du liest nur den Kopf oder den betroffenen Block mit grep bzw. offset/limit.
  - Logs liest du mit `tail -n 40` oder grep.
  - Jede Messstufe ist ein Skript mit einer Ergebnisdatei, und du liest nur deren Zusammenfassung.
  - Lange Läufe startest du losgelöst von der Sitzung (`systemd-run` oder `setsid nohup … < /dev/null &`). Du pollst nicht mit `sleep`.
- Ziel: Prefill und Decode von llama-server auf .224 schneller machen, ohne Qualitätsverlust.
- Bekannte Fakten, nicht neu recherchieren:
  - Produktiv läuft llama.cpp b11696 im Router-Modus (`--models-preset /opt/llamacpp/models.ini --models-max 1`).
  - Resident ist qwen3.6:35b mit `--n-cpu-moe 30` und 32K Kontext. Der Planer qwen3.8:27b ist ein Zusatzmodell mit `settings: {fit: "on"}`.
  - `-b` und `-ub` setzt das Repo nicht, es gelten die Standardwerte von b11696: `-b 2048`, `-ub 512`. Prüfe im Inventar, ob etwas überschrieben ist.
  - Im Router-Modus erben alle Modellinstanzen die Kommandozeilenargumente des Routers, und diese haben **Vorrang vor jedem Preset-Abschnitt** (tools/server/README „Model presets“). Modellspezifische Werte gehören deshalb nie in `ollama_llamacpp_extra_args`.
  - D-021: Der Coder-Prompt besteht aus Systemnachricht, stabiler Nutzernachricht und wechselnder letzter Nachricht. Bei Qwen3.6, einem Hybridmodell, setzt llama.cpp einen Cache-Checkpoint nur am Beginn der letzten Nachricht. Der Folgezug-Test bildet genau diesen Aufbau nach.
  - Das Coder-Turnier rc62 (D-018) ist abgeschlossen. Die Schwierigkeitsleiter (D-020, `coder-tournament --model system --ladder`) läuft nie gleichzeitig mit einem GPU-Benchmark.
  - Deploys laufen über `sudo -n /usr/local/sbin/hermclaw-deploy <version>`. Das Become-Passwort fasst du nie an: nicht entschlüsseln, nicht ausgeben, nicht in Dateien schreiben.
  - R-032 (`docs/research/20261017-032-model-runtime.md`) hat auf b11361 gemessen: pp4096 bei d8192 283 tok/s, tg128 30,3 tok/s, kalter 10,7k-Prompt 39,4 s, gleicher Präfix 4,6 s. Das sind nur Plausibilitätswerte. Die Basis misst du neu.
  - Optionsnamen in b11696:
    - llama-bench: `-b`, `-ub`, `-ncmoe`, `-fa`, `-d`, `-r`, `-o`, `-fitt`, `-fitc`. Werte als Kommaliste.
    - llama-server: dieselben Optionen plus `--moe-cache-mib`, `--spec-type`, `--spec-draft-n-ubatch`.
    - Bestätige sie nur gezielt, etwa mit `--help | grep -E 'ubatch|n-cpu-moe|moe-cache|spec-type|fitt'`. Lies nicht die volle Hilfe.
  - Kein Build-Wechsel in diesem Paket: Jeder Build nach b11696 (16.10.) ist bis Ende Oktober jünger als zwei Wochen.

## Schritt 0 – Lage prüfen, alle Fragen auf einmal
1. Ist-Stand live ermitteln:
   - `sudo -n -u hermclaw hermclaw --version`, `git log --oneline -3`;
   - `pgrep -af 'coder-tournament|acceptance-jobs'`;
   - `ls -l /var/lib/hermclaw/tournament/*/runs.jsonl /var/lib/hermclaw/tournament/*/report.md /var/lib/hermclaw/tournament/*/ladder.md`.
   - Aktive Jobs nur lesend: alle mit Status außer `succeeded`, `failed`, `cancelled`, `waiting_for_user` und `blocked`, dazu `waiting_for_user`-Jobs der letzten 24 h. Die alten `waiting_for_user`-Jobs zählen nicht.
2. Rechte prüfen:
   - `sudo -n -l` auf .225;
   - auf .224 (`hermclaw-model-224`, 192.168.178.224) per SSH ebenfalls `sudo -n -l`.
   - Notiere, ob du auf .224 ohne Passwort Root bekommst.
   - Notiere, ob es einen Weg für `bootstrap.yml --tags ollama` gibt, ohne dass das Become-Passwort durch deine Shell geht.
3. Auf .224 notierst du, soweit deine Rechte reichen:
   - `/opt/llamacpp/models.ini` und `systemctl cat hermclaw-llamacpp.service`;
   - `NRestarts` des Dienstes;
   - `nvidia-smi`, `ollama ps`, `free -g`, `df -h`.
   - Die Inventarwerte `ollama_llamacpp_*` in `infra/ansible/inventory/hosts.yml`.
4. Dann stellst du mir **eine** Nachricht mit allen Fragen:
   - Laufen Jobs, ein Turnier oder eine Leiterstufe? Ist eine Leiterstufe offen, darf sich die llama.cpp-Einstellung zwischen Versuch und Wiederholung ändern?
   - Welches Wartungsfenster? Nenne die geschätzte Gesamtdauer der Dienststopps, jeder höchstens 60 min.
   - Fehlt Root auf .224 oder ein Weg für den Rollout: Welche Option nehme ich?
     - (a) Du schreibst fertige Skripte, und ich führe sie aus.
     - (b) Ich lege vorübergehend `/etc/sudoers.d/90-hermclaw-tuning` für `kiworker` auf .224 an.
     - (c) Du erweiterst `scripts/deploy/hermclaw-deploy.sh` um einen Modus für `bootstrap.yml --limit model_workers --tags ollama`, mit gleicher Absicherung und Test in `tests/unit/test_deploy_script.py`. Ich installiere den Wrapper danach neu. Für die Messungen braucht es trotzdem (a) oder (b).
   Danach arbeitest du ohne Rückfragen bis zur Übernahme. Sage ich „warten“, startest du einen einzigen losgelösten Befehl, der endet, sobald nichts mehr läuft (Schleife mit 5-min-Intervall).
5. Vor jeder Inventaränderung legst du eine datierte Kopie an: `infra/ansible/inventory/hosts.yml.bak-<JJJJMMTT>-pre-llamacpp-tuning`.

## Schritt 1 – Messverfahren und Sweep
**Stoppfenster** (dieses Verfahren schreibst du als Codeblock in Research R-034, damit Paket B es wiederverwendet; alles als root auf .224):
- Vor jedem Stopp prüfst du Schritt 0.1 erneut.
- Stopp und Messung laufen als transiente Unit, nicht als Kind deiner Sitzung:
  `systemd-run --unit=hc-tune-<stamp> -p ExecStopPost='/bin/systemctl start hermclaw-llamacpp.service' /bin/bash /var/tmp/hc-tune/<block>.sh`
- Zusätzlich setzt du einen Sicherheitstimer: `systemd-run --on-active=<Fenster+15min> /bin/systemctl start hermclaw-llamacpp.service`.
- Vor der Messung: `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv` zeigt keinen anderen Prozess. Ein geladenes Ollama-Modell entlädst du mit `ollama stop <modell>`. Ollama selbst bleibt an.
- Programme laufen als `sudo -u ollama /opt/llamacpp/current/bin/…`. Die GGUFs liegen in Ollamas Store und sind nur für `ollama` lesbar.
- **Gesund** heißt danach:
  - `systemctl is-active` meldet active, und `NRestarts` ist unverändert;
  - kein llama-bench- oder Testprozess steht mehr in `nvidia-smi`;
  - eine kurze Chat-Anfrage an `qwen3.6:35b` auf `127.0.0.1:18080` antwortet.
  - Startet der Dienst nicht, liest du `journalctl -u hermclaw-llamacpp -n 50`, beendest Reste und startest neu. Läuft er nach 10 min nicht, fragst du mich.

**Sweep qwen3.6:35b** (dieselbe GGUF wie in `models.ini`; `-ngl 99 -fa 1 -t 6 -r 3 -o csv`, eine Ergebnisdatei je Aufruf):
- Ein Aufruf mit Werteliste bricht beim ersten CUDA-OOM ganz ab. Deshalb gilt:
  - je `-ncmoe`-Wert ein eigener Aufruf, `-ub` aufsteigend;
  - je Aufruf nur eine Tiefe, weil llama-bench den Zustand nur einer Tiefe zwischenspeichert.
- Stufe 1, Prefill: `-ncmoe 30` und `-ncmoe 28` und `-ncmoe 32`, jeweils `-b 2048 -ub 512,1024,2048 -p 4096 -n 0 -d 8192`.
- Stufe 2, Decode: einmal je `-ncmoe`-Wert `-p 0 -n 128 -d 8192`. `-b`/`-ub` ändern das Decode nicht.
- Stufe 3:
  - die zwei besten Kombinationen und die heutige (`-ncmoe 30 -b 2048 -ub 512`) bei `-d 24576`;
  - bei klarem Trend zusätzlich `-b 4096 -ub 4096`;
  - 29 und 31 nur zur Feinjustierung um den besten Wert.
- Die Basis ist die heutige Kombination, gemessen im selben Sweep.

**Planer qwen3.8:27b**, nach allen Coder-Messungen:
- `-fitt 1024 -fitc 32768 -fa 1 -t 6 -r 3 -ub 512,1024,2048 -p 4096 -n 128 -d 8192` (fit wie der Server, ohne `-ngl 99`).
- Miss Prefill **und** Decode. Ein größeres `-ub` vergrößert den Compute-Puffer, und fit legt dann weniger Schichten auf die GPU.

**Betrieb mit 32K Kontext**:
- Die beste Kombination und die heutige startest du je als Test-llama-server auf `127.0.0.1:18090` (`-c 32768 --jinja`, `--alias qwen3.6:35b`).
- Prüfe im Log `CUDA0 compute buffer size` und mit `nvidia-smi`, dass alles passt.
- Messung je Variante dreimal, Ergebnis ist der Median. Die Werte nimmst du nur aus dem Objekt `timings` der Antwort (`prompt_n`, `cache_n`, `prompt_per_second`, `predicted_per_second`).
  - kalter Prompt mit ~10k Tokens;
  - Folgezug im Aufbau von D-021: gleiche Systemnachricht und stabile Nutzernachricht, neue letzte Nachricht.
- Die festen Prompts legst du einmal unter `/var/tmp/hc-tune/prompts/` ab.

## Schritt 2 – Zusatzoptionen, je ein kurzer Versuch gegen den Sieger aus Schritt 1
- **MTP:** nur, wenn die Ollama-GGUF MTP-Tensoren hat (`head -c 64M <gguf> | grep -a -c 'nextn\.'` > 0). Sonst überspringen; ein anderes GGUF braucht meine Freigabe.
  - Die Option steht nur im Abschnitt des Coders (`spec-type = draft-mtp`, `spec-draft-n-max = 2` oder `3`), nie als Router-Argument.
  - MTP braucht zusätzlichen VRAM, also eventuell ein höheres `-ncmoe`; bei großem `-ub` zusätzlich `spec-draft-n-ubatch = 512`. Miss auch den Prefill und ob der Cache-Checkpoint aus D-021 weiter greift (`cache_n`).
  - Bei einem CUDA-Fehler versuchst du es einmal mit `GGML_CUDA_CUBLAS_COMPUTE_TYPE=f32`. Hilft das nicht, brichst du ab.
- **n-gram ohne Zusatzmodell:** `--spec-type ngram-mod --spec-ngram-mod-n-min 8 --spec-ngram-mod-n-max 24`, kein `--spec-default`.
  - Drafts ab 32 Tokens laden mit `--n-cpu-moe` die CPU-Experten über PCIe hoch. Das ist aus dem Quellcode abgeleitet und nicht gemessen.
- **`--moe-cache-mib`** (seit b11480): GPU-Cache für CPU-Experten, wirkt nur beim Decode und kann auf 8 GB auch bremsen. Ein Versuch, etwa `-ncmoe 32 --moe-cache-mib 768`.
- Draft-Modell oder EAGLE-3 nur, wenn es einen veröffentlichten Draft genau für Qwen3.6-35B-A3B gibt, der in den freien Speicher passt. Sonst dokumentierst du „passt nicht“.
- Neue Dateien nur aus der Hersteller-Quelle oder von ggml-org:
  - sha256 gegen die LFS-oid aus `https://huggingface.co/api/models/<repo>/tree/main`;
  - die Datei ist mindestens zwei Wochen alt;
  - Platz vorher prüfen;
  - ab 5 GB fragst du mich vorher.
- Spekulatives Dekodieren lässt die Grammatik wirksam: Der Ziel-Sampler prüft jedes Draft-Token. Eine validierte Schema-Anfrage je Variante genügt.

## Schritt 3 – Übernahmekriterien
Alles muss zutreffen:
1. Der kalte 10k-Prompt oder das Decode ist mindestens 10 % schneller als die Basis, und zwar mehr als die Streuung der Basis. Der andere Wert und der Folgezug sind höchstens 3 % langsamer.
2. Wirkt eine Einstellung auch auf den Planer, verliert er höchstens 3 % bei Prefill und Decode und lädt weiter mit `fit on`. Sonst setzt du den Wert nur beim Coder.
3. **Schema:** Die Anfrage mit genau dem Body des Gateways ist gültig: `response_format` wie in `response_format_for` (`hermclaw/models/gateway.py`) mit `docs/contracts/schemas/CoderAction.schema.json`, `chat_template_kwargs: {enable_thinking: false}`, `temperature: 0.2`. Die Antwort validierst du mit jsonschema.
4. **Gleichwertig:** Bei 5 festen Prompts entstehen schema-gültige `CoderAction`-Antworten mit derselben Aktion (Werkzeug und Pfad). Byte-Gleichheit ist nicht gefordert, weil llama.cpp bei anderer Batchgröße keine bitgleichen Logits garantiert.
5. **Last:** 30 min lang laufen Anfragen nacheinander mit 20–28k Tokens Prompt, jede dritte mit `json_schema`. Dabei wird einmal der Planer geladen und wieder zurückgewechselt, und eine Embedding-Anfrage an Ollama gelingt. Danach ist `NRestarts` unverändert, und im Journal stehen kein `out of memory` und kein `CUDA error`.

Wird ein Kriterium verfehlt, bleibt alles, wie es ist. Die Messung dokumentierst du trotzdem.

## Schritt 4 – Übernahme
- **Planer:** nur Konfiguration in `ollama_llamacpp_models`, etwa `settings: {fit: "on", ubatch-size: <N>}`.
- **Coder (residentes Modell):** Es fehlt eine Stelle für Preset-Werte. Ergänze in der Rolle eine Variable `ollama_llamacpp_model_settings: {}`:
  - im Abschnitt des Hauptmodells in `infra/ansible/roles/ollama/templates/llamacpp-models.ini.j2`;
  - im ExecStart für den Einzelmodell-Betrieb in `infra/systemd/hermclaw-llamacpp.service.j2`;
  - mit derselben Validierung wie `ollama_llamacpp_models[].settings` in `infra/ansible/roles/ollama/tasks/llamacpp.yml`.
  - Test in `tests/integration/test_bootstrap_infra.py` neben `test_llamacpp_router_mode_serves_further_models_under_their_ollama_tags`: Ohne Werte entstehen dieselbe `models.ini` und dieselbe Unit wie heute.
  - Danach ruff, die betroffenen Tests, committen, pushen und auf .225 `git pull`. Das ist kein Release: Die Rolle läuft aus dem Checkout, also kein Versionssprung und kein `deploy.yml`, und CHANGELOG unter `[Unreleased]`.
- **Rollout** über den Weg aus Schritt 0 (Option a, b oder c). Der Befehl selbst lautet in `infra/ansible`:
  - `ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit model_workers --tags ollama` mit Become.
  - Nicht `deploy.yml`, nicht `--tags llamacpp` allein.
  - `--check` überspringt die llama.cpp-Tasks und beweist hier nichts. Sichere deshalb vorher `/opt/llamacpp/models.ini` und die Unit und vergleiche danach mit `diff`.
- **Danach:**
  - `hermclaw doctor` (der Root-Wrapper `/usr/local/sbin/hermclaw` braucht sudo; fehlt es dir, bitte mich darum). Ein Fehlschlag nur bei `gitlab` (Sidekiq auf .226) ist bekannt und zählt nicht.
  - Eine Schema-Anfrage über LiteLLM an den Alias `coder-main`.
- **Praxistest:**
  - `sudo -n -u hermclaw hermclaw coder-tournament /var/lib/hermclaw/tournament/coder-tournament.yaml --model coder-main --task A1 --task A3 --task A5 --reps 1 --out /var/lib/hermclaw/tournament/llamacpp-<einstellung>-<JJJJMMTT>`, losgelöst gestartet, mit neuem, leerem `--out`.
  - Gibt es keine Ergebnisse von `coder-main` auf derselben Hermclaw-Version und mit den alten Einstellungen, läuft derselbe Befehl vorher einmal als Basis. Ergebnisse aus rc62 zählen nicht, weil D-021 den Coder-Prompt geändert hat.
  - Kriterium: mindestens gleich viele korrekte Läufe. Dauer und Modellaufrufe sind nur informativ.
- **Rückweg sofort**, wenn eines davon eintritt:
  - doctor scheitert bei `llamacpp:*` oder `models`;
  - die LiteLLM-Schema-Anfrage scheitert;
  - `NRestarts` steigt;
  - der Praxistest hat weniger korrekte Läufe.
  - Befehl: `cp inventory/hosts.yml.bak-<stamp> inventory/hosts.yml`, danach derselbe Rollout. Bei einer Rollenänderung vorher zusätzlich `git revert`.

## Schritt 5 – Dokumentation und Abschluss
- **Research R-034:** `docs/research/<JJJJMMTT>-034-llamacpp-tuning.md` mit Frage, Quellen mit Datum, Messtabellen, Stoppfenster-Skript, gewähltem Root-Weg und Ergebnis.
  - Jede Aussage, die du nicht selbst auf .224 gemessen oder im Quellcode von b11696 gesehen hast, markierst du als „unbestätigt“.
  - Schreibe immer „Research R-034“, weil RISK_REGISTER dieselben Nummern als Risiken führt.
- Eintrag in `RESEARCH_INDEX.md`.
- Bei einer Übernahme: DECISIONS-Eintrag mit der nächsten freien D-Nummer (zurzeit D-022), inklusive Rückweg; neue Restrisiken in `RISK_REGISTER.md`.
- `STATUS.md`: ein neuer Abschnitt `## Paket C – llama.cpp-Tuning (Research R-034)` direkt nach „## Bugs“, höchstens 10 Zeilen. Die Produktivzeile unter „Bugs“ änderst du nur bei einer Übernahme.
- **Bugs:** Runtime-Bugs trägst du mit Reproduktion in BUGS.md ein. Behoben werden sie im nächsten Fixpaket bzw. an der Leiter (D-020), nicht in diesem Paket. Ausnahme sind Datenverlust oder Sicherheit. Fehler in deinen Skripten, deiner Rollen- oder Wrapper-Änderung behebst du direkt.
- **Denken der Modelle:** Du dokumentierst nur Ort und Länge (Feld, Zeichenzahl), nie den Inhalt.
- **Rohantworten und Messdateien** bleiben unter `/var/tmp/hc-tune/`.
- **Commit:**
  - Committe nur gezielt (`git add <pfad>`), nie `git add -A` oder `git add .`.
  - Prüfe vorher `git status --short`.
  - `infra/ansible/inventory/hosts.yml`, seine `.bak-*`-Kopien, GGUFs und Messdateien kommen nie ins Repo.
  - Push auf `claude/hermclaw-next-build`.
- Dein Abschlussbericht an mich:
  - die Messtabelle in Kurzform;
  - was übernommen wurde;
  - der Rückweg in einem Befehl;
  - offene Punkte, etwa ob die sudoers-Datei aus Option (b) wieder weg muss.

## Harte Grenzen (`docs/SESSION_HANDOFF.md` §4)
- kein `--full-wol`, keine Backup-Suite;
- keine Geheimnisse ausgeben;
- das Portal auf .223 nicht anfassen;
- Suspend bleibt aus;
- keine DB-Schreibzugriffe auf Produktionsjobs;
- keine benchmark- oder projektspezifischen Sonderfälle im Runtime-Core.
- Vor Aktionen, die Daten löschen oder Dienste dauerhaft abschalten, fragst du mich.
```

---

## Prompt B

```text
Paket B: Modellfamilien außer Qwen auf .224 lauffähig machen und im Coder-Turnier messen.

## Rahmen und Vorrang
- Du arbeitest in der CLI-Sitzung auf .225 im Repo `~/hermclaw-next`, Branch `claude/hermclaw-next-build` (Remote `origin` auf GitHub, nicht GitLab).
- **Das Paket verteilt sich auf mehrere Sitzungen.** Lies zuerst den Abschnitt „## Paket B – Modellfamilien (Research R-035)“ am Ende von `STATUS.md`.
  - Fehlt der Abschnitt, legst du ihn an und beginnst mit Teil 1a.
  - Fehlt `docs/research/*-034-llamacpp-tuning.md` (Paket C), hörst du sofort auf und sagst es mir.
- Arbeite genau den Teil ab, der unter „Nächster Teil“ steht. Danach:
  - Abschnitt aktualisieren, committen, pushen;
  - aufhören und mir sagen, dass ich `/clear` ausführen und den Startsatz erneut eintippen soll.
  - Endet mein Kontingent mitten in einem Teil, committest du einen Zwischenstand und trägst ein, was erledigt und was offen ist.
  - Meine Antworten gelten nach `/clear` nur, wenn sie im Abschnitt stehen.
- Der Abschnitt hat höchstens 15 Zeilen. Ergebnisse stehen in Research R-035, nicht dort. Er beginnt immer mit drei Zeilen:
  - `Nächster Teil: <1a|1b|1c|2|3a|3b|4|fertig>`
  - `Hintergrundlauf: <Unit/PID, Verzeichnis, Startzeit – oder „–“>`
  - `Offene Fragen: <… – oder „–“>`
- Ausdrückliche Anweisung, sie geht CLAUDE.md und `docs/SESSION_HANDOFF.md` §3 vor:
  - Das Aufhören nach einem Teil und meine Rückfragepunkte gelten, obwohl §3 sagt, gestoppt werde nur auf Anweisung.
  - Du liest nicht alle Dateien aus CLAUDE.md, sondern nur die Leseliste unten. „Aktueller Zustand“ in STATUS und §5 im Handoff sind veraltet.
  - Alle anderen Regeln aus CLAUDE.md und §4 gelten.
- Leseliste je Teil:
  - Immer: `docs/SESSION_HANDOFF.md` §3, §4 und §6; in `DECISIONS.md` die Zeilen D-015 bis D-021 (grep); den Abschnitt „Paket B“.
  - Teil 1: zusätzlich R-034, aber nur Ergebnis, Stoppfenster-Skript und Root-Weg.
  - Teil 2: nur die betroffenen Dateien.
  - Teil 3: `docs/architecture/tournament.md` und in `docs/operations/deployment.md` den Abschnitt „Deploy wrapper“.
  - Teil 4: aus `docs/research/20261018-033-planner-candidates.md` nur die Zeilen zu Gemma 4 (grep).
- Sparsam arbeiten (Pro-Plan):
  - keine Workflows, keine Agent-Teams, keine parallelen Subagenten;
  - `BUGS.md`, `BUILD_PLAN.md` und `CHANGELOG.md` öffnest du nie ganz; Logs liest du mit `tail -n 40` oder grep.
  - Lange Läufe startest du losgelöst von der Sitzung (`systemd-run` oder `setsid nohup … < /dev/null &`). Du pollst nicht mit `sleep`.

## Bekannte Fakten, nicht neu recherchieren
**Hermclaw:**
- Es gibt keine nativen Tool-Calls. Der Coder bekommt den Werkzeugkatalog im Prompt.
- Der Gateway schickt nur `messages`, `max_tokens`, `temperature` und `response_format`: `{"type":"json_schema","json_schema":{"name":…,"schema":…,"strict":true}}` aus `response_format_for` in `hermclaw/models/gateway.py`.
- LiteLLM ergänzt `extra_body.chat_template_kwargs.enable_thinking` aus `profile.think` (`hermclaw/models/litellm_config.py`).
- Das echte Schema für Tests ist `docs/contracts/schemas/CoderAction.schema.json`.
- `strip_reasoning` im Gateway entfernt bereits `<think>`-Blöcke und zählt `reasoning_content`.
- Das Coder-Turnier rc62 (D-018) ist abgeschlossen; die Ergebnisse liegen unter `/var/lib/hermclaw/tournament/` (`ls */report.md`).
- Seit D-021 (stabiler Prompt-Anfang) sind Dauer und Modellaufrufe aus rc62 nicht mehr vergleichbar. `coder-main` läuft deshalb immer im selben Lauf als Referenz mit.
- Die Schwierigkeitsleiter (D-020) läuft nie gleichzeitig mit Turnier oder GPU-Benchmark.
- Deploys laufen über `sudo -n /usr/local/sbin/hermclaw-deploy <version>`. Das Become-Passwort fasst du nie an.

**llama.cpp b11696:**
- Router-Argumente (`ollama_llamacpp_extra_args`) gelten für jedes Modell und schlagen jeden Preset-Abschnitt. Modellwerte gehören deshalb in `ollama_llamacpp_models[].settings`.
  - Schlüssel sind llama-server-Optionen ohne Striche.
  - Ein unbekannter Schlüssel lässt den Router beim Start scheitern.
  - Die Werte-Validierung der Rolle (`^[A-Za-z0-9._:,/-]+$`) lässt kein JSON zu.
- `n-gpu-layers = 99` setzt das Template nur beim Hauptmodell. Ein Zusatzmodell braucht eigene `settings`, etwa `{n-gpu-layers: 99, n-cpu-moe: N}` oder `{fit: "on"}`.
- Die Parser in b11696 trennen Denken und Kanal-Markup selbst (`common/parsers/gpt-oss.cpp`, `gemma4.cpp`, Autoparser für `<think>`), solange `--jinja` aktiv ist und `reasoning-format` nicht `none`. Der R-033-Befund zum leeren Denkblock bei Gemma 4 stammt von b11361.
- `enable_thinking` muss ein JSON-Boolean sein. Als String lehnt llama.cpp die Anfrage ab.
- `reasoning_effort` liest llama.cpp sowohl auf oberster Ebene als auch in `chat_template_kwargs`.
- Die Grammatik für `json_schema` lässt vor dem JSON einen unbegrenzten Denkblock zu. Langes Denken kann also an `max_tokens` scheitern.
- Kein Build-Wechsel: Jeder Build nach b11696 (16.10.) ist bis Ende Oktober jünger als zwei Wochen.

**Kandidaten** (Recherche vom 2026-10-19; vor Gebrauch nur Lizenz, Datei und Datum prüfen):
- **Gemma 4 26B-A4B** (MoE, Apache-2.0):
  - liegt als `gemma4:26b` im Ollama-Store; kein Download, Eintrag ohne `path`;
  - lief in R-033 schon auf llama.cpp;
  - Denken über `enable_thinking`, Standard aus;
  - meldet das Log `detected an outdated gemma4 chat template`, testest du zusätzlich das offizielle Template per `chat-template-file`.
- **gpt-oss-20b** (MoE, Apache-2.0, Harmony-Format):
  - offizielles MXFP4-GGUF, nicht Q4_K_M;
  - Denken über `reasoning_effort` low|medium|high, Standard medium;
  - lässt sich nicht abschalten; `"none"` fällt auf medium zurück. Für Tests nimmst du `low`.
- **GLM-Flash** (MoE, MIT):
  - die neueste Flash-Version, die b11696 lädt und die mindestens zwei Wochen alt ist; sonst GLM-4.7-Flash;
  - `enable_thinking`, Standard an, dazu `clear_thinking`;
  - ältere GGUFs haben die falsche Gating-Funktion und laufen in Schleifen. Das Ladelog muss `expert_gating_func = sigmoid` zeigen, sonst verwirfst du die Datei.
  - Vor dem Download prüfst du, ob die Datei aus R-033 noch auf .224 liegt.
- Devstral Small 2 24B (Mistral) wurde im rc62-Turnier schon gemessen und wird hier nicht geprüft.
- Weitere Modelle nur, wenn sie mindestens zwei Wochen alt sind.

**Dateien:**
- Quelle zuerst das Hersteller-Repo, dann ggml-org. Community-GGUFs (unsloth, bartowski …) nur nach meiner Freigabe.
- sha256 gegen die LFS-oid aus `https://huggingface.co/api/models/<repo>/tree/main`. Dieselbe Abfrage liefert Größe und Datum. Die Datei muss mindestens zwei Wochen alt sein.
- Download auf .224 losgelöst nach `<datei>.part`, umbenennen erst nach bestandener Prüfung.
- Ablage unter `/var/lib/hcbench/models/`, lesbar für `ollama`, nie unter /home (die Unit hat `ProtectHome=yes`). Name aus `[a-z0-9._-]` mit Endung `.gguf`.
- Braucht ein Repo Login oder Token, fragst du mich. Tokens erscheinen nie in Befehlen oder Logs.

## Teil 1a/1b/1c – Screening je Kandidat
1a ist Gemma 4, 1b gpt-oss-20b, 1c GLM-Flash. Die Runtime bleibt dauerhaft unverändert: kein Eintrag im Inventar, keine Anfragen an den Produktions-Router auf 18080 (sie würden die Modell-Lease umgehen).

Vorbereitung:
1. Prüfe den Ist-Stand:
   - `sudo -n -u hermclaw hermclaw --version`;
   - `pgrep -af 'coder-tournament|acceptance-jobs'`;
   - aktive Jobs nur lesend: alle mit Status außer `succeeded`, `failed`, `cancelled`, `waiting_for_user` und `blocked`, dazu `waiting_for_user`-Jobs der letzten 24 h.
   - Läuft etwas, fragst du mich.
2. Root auf .224 bekommst du über den Weg, der in R-034 steht. Fehlt er, fragst du mich (Optionen wie in Paket C, Schritt 0).
3. Nur in 1a: Rechne den Plattenbedarf aller Kandidaten aus und schlage 10 GB Reserve auf. Reicht der Platz auf .224 nicht, stellst du mir **eine** Frage mit Zahlen und drei Optionen:
   - (a) die Partition von VM 104 vergrößern;
   - (b) bestimmte Dateien löschen, namentlich genannt;
   - (c) nacheinander screenen und jeden Durchgefallenen nach meiner Zustimmung löschen.
4. Den Produktionsdienst stoppst du nur nach dem Stoppfenster-Verfahren aus R-034: transiente Unit mit Wiederanlauf, Sicherheitstimer, höchstens 60 min, Gesundheitsprüfung danach.

Prüfung, vom Billigen zum Teuren; der erste Ausschluss beendet die Prüfung des Kandidaten:
1. **Tempo:** `llama-bench -fitt 1024 -fitc 32768 -fa 1 -t 6 -r 3 -ub <R-034> -p 4096 -n 128 -d 8192`, als `sudo -u ollama`. Unter 10 tok/s Decode scheidet der Kandidat aus.
   - Ein festes `-ncmoe` probierst du nur, wenn es messbar schneller ist als fit.
2. **Testserver:** `sudo -u ollama /opt/llamacpp/current/bin/llama-server -m <gguf> --host 127.0.0.1 --port 18090 -c 32768 -fa on -t 6 -np 1 --jinja` plus die Werte aus Punkt 1. Alle Anfragen gehen mit genau dem Body des Gateways und den Denk-Einstellungen des geplanten Profils an `/v1/chat/completions`. Tempowerte nimmst du aus `timings`.
3. **Schema:** 20 Anfragen mit `CoderAction.schema.json`, `temperature: 0.2`, `max_tokens` wie bei `coder-main` (8192).
   - Als Fehler zählt: `finish_reason: length`, leerer `content` oder ungültig gegen das Schema.
   - Erlaubt sind null Fehler. Die Länge von `reasoning_content` notierst du.
4. **Denken und Markup:** Wirkt der Schalter? Bleibt Markup im `content`?
5. **Cache:** Folgezug im Aufbau von D-021 (Systemnachricht, stabile Nutzernachricht, neue letzte Nachricht). Notiere `cache_n`.
6. **Schleifen:** zwei Anfragen mit bis zu 8k Ausgabe ohne Schema. Gibt es Wiederholungen, und wie verhält sich der Stopp?

Ergebnis in `docs/research/<JJJJMMTT>-035-model-families.md`, beim ersten Kandidaten neu angelegt, dazu ein Eintrag in `RESEARCH_INDEX.md`:
- je Kandidat eine Zeile: Tempo, VRAM/RAM, Punkte 1–6, bestanden ja/nein;
- die Lücken in Hermclaw;
- jede nicht selbst gemessene Aussage als „unbestätigt“ markiert.

Nächster Teil:
- nach 1a/1b der nächste Kandidat;
- nach 1c „2“, wenn Lücken eine Code-Änderung brauchen, sonst „3a“;
- „fertig“, wenn kein Kandidat besteht.

## Teil 2 – Lücken generisch schließen
1. Zuerst prüfst du, ob die Preset-`settings` die Lücke ohne Code schließen: `top-k`, `top-p`, `min-p`, `repeat-penalty`, `reasoning-format`, `reasoning-budget`, `chat-template-file`. Das ist reine Konfiguration, also kein Release.
2. Code nur für das, was so nicht geht. Typisch:
   - `chat_template_kwargs` je Profil, validiert auf JSON-Skalare mit erhaltenem Typ;
   - `reasoning_effort` je Profil (optional), über `extra_body` statt über das Parameter-Mapping von LiteLLM.
   - Ansatzpunkte:
     - `ModelProfileConfig` in `hermclaw/core/config.py`;
     - `_llamacpp_deployment` in `hermclaw/models/litellm_config.py`;
     - `strip_reasoning` in `hermclaw/models/gateway.py` erweitern statt neu bauen.
   - Neue, ungesetzte Felder dürfen die Metadaten bestehender Profile in der DB-Synchronisierung nicht ändern (`_row_values`, `profile_from_row` in `hermclaw/models/profiles.py`).
   - Keine Sonderfälle je Modellname.
3. Regressionstest in `tests/unit/test_models_profiles.py`: Bestehende Profile erzeugen dieselbe LiteLLM-Konfiguration wie heute.
   - Dazu Tests der neuen Felder, ruff und mypy auf die betroffenen Dateien.
   - Mit Code-Änderung: BUGS-/CHANGELOG-Eintrag unter `[Unreleased]`, `TEST_MATRIX.md` und `docs/architecture/*`.
4. **Release** (Änderung unter `hermclaw/`):
   - nach §6 bauen;
   - ausrollen mit `sudo -n /usr/local/sbin/hermclaw-deploy <version> --check`, danach ohne `--check`;
   - nur, wenn kein Job, keine Turnierphase und keine Leiterstufe läuft. Ist eine Leiterstufe offen (D-020), fragst du mich vorher.
   - Rückweg: `hermclaw_version` im Inventar auf die Vorversion setzen und den Wrapper mit der Vorversion aufrufen.

## Teil 3a – Kandidaten ins Turnier
1. Ausrollen nur, wenn kein Job, keine Turnierphase und keine Leiterstufe läuft (Prüfung wie in Teil 1).
2. Vorher eine datierte Kopie von `infra/ansible/inventory/hosts.yml`.
3. **Modelle** in `ollama_llamacpp_models`:
   - `{model: <name>, path: /var/lib/hcbench/models/<datei>.gguf, settings: {…aus Teil 1/2}}`;
   - `gemma4:26b` ohne `path`;
   - keine Werte in `ollama_llamacpp_extra_args`.
4. **Profile** unter `hermclaw_config_overrides.models.profiles`:
   - Die Liste ersetzt die Beispiel-Liste ganz. Alle bestehenden Profile bleiben, die Kandidaten hängst du **nach** `coder-main` an. Das erste aktive Coder-Profil ist der produktive Coder.
   - Kopiere `coder-main` und ändere nur `alias` (`coder-<name>`), `model` (der Name aus `ollama_llamacpp_models`) und `deviation` (`"D-018: optionaler Turnier-Kandidat, Research R-035"`).
   - Neue Felder aus Teil 2 setzt du nur, wenn nötig. Kein `fallback_for`.
5. **Rollout:**
   - zuerst die Rolle `ollama` auf .224 über den Root-Weg aus R-034: `bootstrap.yml --limit model_workers --tags ollama`. `--check` überspringt dort die llama.cpp-Tasks; sichere `models.ini` vorher und vergleiche mit `diff`.
   - danach `models.yaml` und LiteLLM auf .225 mit `sudo -n /usr/local/sbin/hermclaw-deploy <aktuelle Version> --check`, dann ohne `--check` (gleiche Version, nur neue Konfiguration);
   - dann `hermclaw doctor`. Ein Fehlschlag nur bei `gitlab` (Sidekiq) ist bekannt;
   - dann eine Schema-Anfrage über LiteLLM je neuem Alias.
6. **Lauf:**
   - Die Spezifikation bleibt unverändert. Kandidaten wählst du nur mit `--model`, `coder-main` läuft als Referenz mit.
   - Neues Ausgabeverzeichnis `/var/lib/hermclaw/tournament/prelim-families`.
   - Befehl, losgelöst: `sudo -n -u hermclaw setsid nohup hermclaw coder-tournament /var/lib/hermclaw/tournament/coder-tournament.yaml --model coder-main --model coder-<a> … --task A1 --task A3 --task A5 --reps 1 --out /var/lib/hermclaw/tournament/prelim-families > /var/lib/hermclaw/tournament/prelim-families.log 2>&1 < /dev/null &`.
   - Unter „Hintergrundlauf“ trägst du Startzeit, PID, Verzeichnis und die erwartete Zahl der Läufe ein. Nächster Teil: 3b.

## Teil 3b – Auswertung
- Läuft der Prozess noch, meldest du den Fortschritt (Zeilen in `runs.jsonl`) und hörst auf, ohne zu warten. Bricht der Lauf ab, setzt du ihn mit demselben `--out` fort.
- Punkte vergleichst du nur innerhalb eines Berichts. Mit den rc62-Ergebnissen vergleichst du nur Rohwerte je Aufgabe, und nur zur Orientierung:
  - korrekte Läufe x/y, Anteil bestandener versteckter Prüfungen;
  - Modellaufrufe, Dauer;
  - Hermclaw-Version und llama.cpp-Einstellungen.
- Rangliste der Kandidaten gegen `coder-main` aus demselben Lauf: erst korrekte Läufe, dann Modellaufrufe, dann Dauer. Ergebnis in R-035.
- Ist ein Kandidat mindestens so korrekt wie `coder-main`, schlägst du mir als nächsten Test einen größeren Lauf vor: `--reps 3` über alle Turnieraufgaben, oder die Leiter (D-020) mit dem Kandidaten als Coder.
  - Du startest ihn nicht selbst.
  - `coder-main` stellst du nur nach meiner Freigabe um, mit DECISIONS-Eintrag (nächste freie D-Nummer) und Rückweg.
- Nächster Teil: „4“ oder „fertig“, je nach meiner Antwort.

## Teil 4 – optional, nur nach Rückfrage
- Das beste Modell außerhalb der Qwen-Familie als Reviewer oder in der Fast-Rolle: nur ein Smoke-Test mit Schema-Antworten über ein zusätzliches Profil mit `deviation`.
- Die produktiven Profile stellst du nicht um. Eine Umstellung braucht nach D-015 eine Messung, einen DECISIONS-Eintrag und meine Freigabe.
- Für den Planer gelten die Werte aus R-033 (Gemma 4 26B: 75/120).

## Für jeden Teil
- **Bugs:** Runtime-Bugs trägst du mit Reproduktion in BUGS.md ein. Behoben werden sie im nächsten Fixpaket bzw. an der Leiter (D-020). Ausnahme sind Datenverlust oder Sicherheit. Fehler in deinen Skripten behebst du direkt.
- Neue Restrisiken (Community-GGUF, Plattenplatz, VRAM-Reserve) kommen in `RISK_REGISTER.md`.
- Schreibe immer „Research R-035“, weil RISK_REGISTER dieselben Nummern als Risiken führt.
- **Denken der Modelle:** nur Ort und Länge dokumentieren, nie den Inhalt.
- **Rohantworten** bleiben unter `/var/tmp/hc-screen/`.
- **Commit:**
  - nur gezielt (`git add <pfad>`), nie `git add -A` oder `git add .`;
  - vorher `git status --short`;
  - `infra/ansible/inventory/hosts.yml`, seine `.bak-*`-Kopien, GGUFs und Messdateien kommen nie ins Repo.
  - Push auf `claude/hermclaw-next-build`.
- Kurzer Bericht an mich: Ergebnis, nächster Teil, was ich tun muss.

## Harte Grenzen (`docs/SESSION_HANDOFF.md` §4)
- kein `--full-wol`, keine Backup-Suite;
- keine Geheimnisse ausgeben;
- das Portal auf .223 nicht anfassen;
- Suspend bleibt aus;
- keine DB-Schreibzugriffe auf Produktionsjobs;
- keine benchmark-, projekt- oder modellspezifischen Sonderfälle im Runtime-Core.
- Vor Aktionen, die Daten löschen oder Dienste dauerhaft abschalten, fragst du mich.
```
