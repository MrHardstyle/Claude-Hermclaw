# R-033 – Planer-Modell-Kandidaten für den 8-GB-Modellhost (2026-10-18)

## Anlass

Testzyklus 1 auf rc59 (STATUS Punkt 17, BUG-277/278): Der Planer zerlegt Aufträge brauchbar. Seine **Prüfkriterien** sind
aber unzuverlässig:
- `must_change` auf Dateien, die der Schritt nur liest (M2: Eingabedaten, Einstiegspunkt, bestehender Test);
- der Neuplaner wiederholt dasselbe defekte Kriterium trotz Blockade-Begründung;
- bei G1 fehlten Abhängigkeiten.

Seit D-016 läuft der Planer auf `qwen3.6:35b` (35B-A3B MoE), demselben Modell wie der Coder. Vorher lief er auf gemma4:26b.
Die Planerqualität wurde beim Wechsel nicht getrennt gemessen.

## Randbedingungen

Hardware:
- GTX 1080 (8 GB, Pascal);
- 24 GB RAM in der VM, davon ~22 GB für das residente qwen3.6:35b (mmap);
- 6 vCPU mit AVX2.

Laufzeit: llama.cpp b11361 (`--n-cpu-moe`) oder Ollama.

Jedes Planer-Modell außer dem qwen3.6-Slot erzwingt einen Modellwechsel (RAM). Planer-Aufrufe sind selten, wenige pro
Auftrag. Langsamere Generierung ist daher vertretbar, Wechselkosten (Laden ~30–60 s) fallen pro Aufruf an.

## Kandidaten (Recherche-Stand 2026-10-18; Herstellerangaben, sofern nicht anders vermerkt)

| # | Modell | Art | Belege | Größe Q4 / Tempo | Hinweise |
|---|---|---|---|---|---|
| 1 | **Qwen3.8-27B** (installiert, `qwen3.8:27b`) – https://huggingface.co/Qwen/Qwen3.8-27B | dicht, Apache-2.0, 2026-08-14 | IFBench 79,5, SWE-bench Pro 61,7, Terminal-Bench 2.1 73,0, LiveCodeBench v6 90,3 (Hersteller; unabhängige Werte fehlen) | ~17 GB / gemessen 4,7 tok/s (Ollama, 2026-10-17) | Thinking standardmäßig an, `reasoning_effort` dokumentiert; `preserve_thinking` für den Planer aus. Ob llama.cpp `reasoning_effort` durchreicht, ist ungeprüft. |
| 2 | **Gemma 4 26B-A4B** (installiert, `gemma4:26b`) – https://huggingface.co/google/gemma-4-26B-A4B-it | MoE 25,2B/3,8B aktiv, Apache-2.0 | Tau2 68,2; **IFBench 77,25 gegenüber 63,71 für Qwen3.6-35B-A3B** im NVIDIA-Harness (unabhängig): https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16 | ~16–17 GB / gemessen 27,6 tok/s (Ollama, AVX2) | Leeren Denkblock bei abgeschaltetem Thinking entfernen. Laut einem Bericht steht das Reasoning-Budget in llama.cpp für Gemma 4 seit 2026-04 standardmäßig auf unbegrenzt: https://github.com/antoinezambelli/forge/issues/54 |
| 3 | Agents-A1 35B-A3B – https://huggingface.co/InternScience/Agents-A1 | MoE (qwen3_5_moe-Ableitung), Apache-2.0 | IFBench 80,6, IFEval 94,8, τ2 79,8 (selbst berichtet) | ~21 GB offizielles GGUF / wie qwen3.6 | Direkter Ersatz im MoE-Slot, kein Wechsel nötig. Kein Thinking-Schalter dokumentiert. |
| 4 | Gemma 4 31B (dicht) | dicht, Apache-2.0 | Tau2 76,9 | ~18–19 GB / ~3–4 tok/s (geschätzt) | Ein Bericht sieht Schwächen bei langer Planung |
| 5 | GLM-4.7-Flash 30B-A3B – https://huggingface.co/zai-org/GLM-4.7-Flash | MoE, MIT | τ²-Bench 79,5, SWE-bench Verified 59,2 | ~18 GB / schnell | Looping-Fehler in llama.cpp am 2026-01-21 behoben; GGUFs neu laden |
| 6 | Qwen3.6-27B (dicht) | dicht, Apache-2.0 | SWE-bench Verified 77,2 | wie #1 | nur Rückfallebene zu #1 |
| – | Nemotron 3.5 Lightning 30B-A3B, gpt-oss-20b | – | SWE-bench Verified 51,6 bzw. τ² 47,7 | – | für Planung zu schwach |

Thinking in llama.cpp:
- `--reasoning-budget` kennt verlässlich nur -1 (unbegrenzt) und 0 (aus).
- Ein Budget auf der Server-Kommandozeile hat Vorrang vor dem Budget je Anfrage (b9982).
- Robust ist entweder Thinking aus plus JSON-Grammatik, oder zwei Durchgänge: eine begrenzte Analyse (`n_predict`), dann ein JSON-Durchgang mit Grammatik.

Nicht verifiziert:
- GGUF-Details zu Qwen3.8;
- unabhängige Werte für Qwen3.8 und Agents-A1;
- IFEval und BFCL der meisten Kandidaten.

## Nachtrag: größere Modelle mit CPU-Anteil (Betreiber, 2026-10-18)

Budget: ~30–34 GB Gewichte (24 GB RAM + 8 GB VRAM). Größen stammen aus der HF-API-Dateiliste.

| Modell | Gesamt/aktiv | Belege (Hersteller) | GGUF | llama.cpp |
|---|---|---|---|---|
| K2-Horizon-MoVA-36B-A4B (IFM, 2026-09-01) | 37,4B/4B | IFEval 90,7 (Q4_K_M 87,0), Terminal-Bench 2.1 58,6 vs. 44,9 für Qwen3.6-35B | IFM/…-Q5_K_M, 26,4 GB | erst ab b11454 (PR #29535); `reasoning_effort` low/medium fehlerhaft (#30246, #30335) |
| Qwen3-Next-80B-A3B-Instruct | 81B/3B | IFEval 87,6, BFCL-v3 70,3; denkt nie | bartowski IQ3_XXS, 31,7 GB | ja; Low-Bit-Risiko |
| Nemotron-Labs-3-Puzzle-75B-A9B | 75B/9,3B | IFBench 71,9 | Community-Q2_K, 31,5 GB | ja; beschnitten + 2 Bit, nicht geladen |
| Nemotron-3.5-Bolt-74B-A9B-24GB | 74B/9B | offizielles 2-Bit-QAT, 22 GB | – | erst ab PR #30520 (2026-10-15); jünger als zwei Wochen → später |

Ausgeschlossen (zu groß, ohne upstream-Unterstützung oder schwächer): Qwen3.5-122B-A10B (≥36,6 GB), GLM-4.5-Air
(≥47 GB), gpt-oss-120b, LongCat-Flash-Lite, Kimi-Linear-48B, Seed-OSS-36B, EXAONE-4.5-33B.

Turnier (Betreiber, 2026-10-18): alle Kandidaten bekommen dieselben Aufträge (Runde 1: K1, M2, M3), bewertet
automatisch (gültig, Reparaturen, Dauer) und blind nach Rubrik (Abdeckung, Zerlegung, Kriterien, Bezug zum Repo); bei
Gleichstand entscheidet ein schwererer Auftrag (G1). Prüfstand: `planbench` (Wegwerf-DB, echter Planer-Code, eigener
llama-server auf Port 18090).

## Bewertung

- Für Kriterienqualität zählt Befolgen von Anweisungen (IFBench) mehr als Codier-Benchmarks.
- Auf dem Papier liegen **qwen3.8:27b** und **gemma4:26b** vor dem aktuellen qwen3.6:35b. Bei gemma4:26b belegt das ein unabhängiger Vergleich (77,25 gegenüber 63,71). Beide sind bereits installiert.
- Agents-A1 wäre der einzige Kandidat ohne Wechselkosten, ist aber nur selbst berichtet belegt.

## Messplan (A/B, nur nach einer Batterie, nie parallel)

Prüfstand mit Wegwerf-Datenbank: echter `Planner.create_plan` mit Prüfung und Anreicherung, die sechs Aufträge K1–M3 und die
Fixture-Repos, je Modell 2–3 Läufe.

Kennzahlen:
- gültig im ersten Versuch und Zahl der Reparaturen;
- `must_change` auf nur gelesene Dateien (M2-Muster);
- Testkriterien für Implementierungsschritte;
- Abhängigkeiten zwischen Schritten;
- Planungsdauer inklusive Modellwechsel.

Zusätzlich die Replan-Fälle aus Zyklus 1 (M2 Plan v1 mit Blockade-Begründung).

Reihenfolge der Kandidaten: qwen3.8:27b, gemma4:26b, Agents-A1. Danach eine Batterie mit dem besten Planer als eigener
Zyklus. Die Entscheidung wird in DECISIONS.md dokumentiert (D-015).

## Ergebnis Planer-Turnier (2026-10-18, rc59-Planercode, Prüfstand `planbench`)

Ablauf:
- Jeder Kandidat lief auf llama.cpp b11361 (K2-Horizon auf b11462) mit `--fit on`, 32K Kontext und Thinking aus, wie in der Produktion.
- Verwendet wurde der echte `Planner.create_plan` mit Prüfung, Reparaturschleife (max. 2) und Anreicherung, auf einer Wegwerf-Datenbank mit den echten Fixture-Repos.
- Bewertet hat ein Claude-Subagent blind nach `rubric.md`: Abdeckung, Zerlegung, Kriterien, Bezug zum Repo, je 0–10 Punkte. Die Zuordnung zu den Modellen wurde erst nach dem Urteil aufgedeckt.

Runde 1 (K1, M2, M3; je ein Lauf, max. 120 Punkte):

| Modell | K1 | M2 | M3 | Summe | Reparaturen | Dauer 3 Pläne | tok/s |
|---|---|---|---|---|---|---|---|
| **qwen3.8-27b (dicht)** | 30 | **35** | 34 | **99** | 0 | 24,9 min | 2,1 |
| Agents-A1 35B-A3B | 34 | 22 | 35 | 91 | 2 | 3,0 min | 28,6 |
| qwen3.6-35b (Produktion) | 35 | 14 | 32 | 81 | 1 | 4,3 min | 27,6 |
| gemma4-26b | 21 | 27 | 27 | 75 | 1 | 11,9 min | 20,6 |
| Qwen3-Next-80B IQ3_XXS | 25 | 17 | 32 | 74 | 1 | 8,9 min | 16,4 |
| gemma4-31b (dicht) | 15 | 27 | 23 | 65 | 0 | 36,5 min | 1,8 |
| GLM-4.7-Flash | 18 | 17 | 23 | 58 | 3 (M2: 12 Schritte) | 7,1 min | 18,6 |
| K2-Horizon-36B Q5 | – | – | ungültig | aus | M2 2, M3 gescheitert | – | 1,8 |

Zu M2:
- Nur qwen3.8 setzt `must_change` richtig: `ledger/parse.py`, `ledger/report.py`, `tests/`, dazu `must_not_change data/buchungen.csv`.
- qwen3.6 verlangt Änderungen an `data/buchungen.csv` und `ledger/__main__.py`. Das ist genau das Muster, das in Zyklus 1 die Replans auslöste (BUG-278). Es stellt auch eine falsche Diagnose.
- Agents-A1, Qwen3-Next und GLM verlangen `must_change` auf `__main__.py`.

Stichentscheid G1 (schwerer Auftrag, max. 40 Punkte):
- qwen3.8: gültig, 9 Schritte, 1 Reparatur, 39 min, **29 Punkte**.
- Agents-A1: 4 Läufe. Einer blieb nach 2 Reparaturen ungültig, drei waren gültig mit 1–2 Reparaturen und bekamen 23, 21 und 19 Punkte.
- qwen3.6 (Produktion): ungültig nach 2 Reparaturen.
- qwen3.8 deckt die G1-Anforderungen am vollständigsten ab: volle Palette, Felder, Testumfang, `n/v`, Geheimnisschutz.
- Lücke aller Pläne: ein Backend-Endpunkt für das Schreiben der Konfiguration und für „Verbindung testen“.

**Sieger: qwen3.8-27b.** Er hat in beiden Runden klar gewonnen und brauchte die wenigsten Reparaturen.
- Preis: ~2 tok/s mit CPU-Anteil, also 6–11 min je mittlerem Plan und ~40 min für G1.
- Er braucht einen Modellwechsel gegenüber dem residenten qwen3.6 für Coder und Review.
- Einschränkung: je Auftrag ein Lauf (Streuung nicht gemessen). Der Vorsprung auf M2 und G1 ist aber inhaltlich begründet: richtige Diagnose und richtige Kriterien.
- Nächster Schritt: Planer und Neuplaner produktiv auf qwen3.8-27b umstellen. Dazu gehören Wechsel und Residenz auf .224 und die Zeitlimits. Danach die Testzyklus-Batterie erneut fahren und die Entscheidung als D-Eintrag in DECISIONS.md festhalten.

## Runde 3: fünf weitere Kandidaten (Betreiber, 2026-10-19)

Kandidaten: Nemotron 3.5 Bolt 74B-A9B (llama.cpp b11696), Qwen3.6-27B (dicht), Granite 4.2 8B (Q8), Ornith 1.5 9B
(Q8), Ornith 1.5 35B-A3B. Ablauf wie oben. Die Referenzpläne von qwen3.8, Agents-A1 und qwen3.6-35b liefen mit in den
blinden Vergleich (8 Pläne je Auftrag). Zusätzlich wurde nach den Freigabekriterien geprüft:
Anforderungsabdeckung 100 %, R2 widersprüchliche Kriterien 0, R3 ungültige Dateien 0, R4 fehlende Abhängigkeiten 0,
R5 Tests je Funktion.

| Modell | K1 | M2 | M3 | Summe | freigabefähig | Dauer 3 Pläne |
|---|---|---|---|---|---|---|
| **qwen3.8-27b** (Referenz) | 32 | **35** | 32 | **99** | 2/3 (M2, M3) | ~25 min |
| Ornith 1.5 9B | **37** | 25 | 32 | 94 | 1/3 (K1) | 7,0 min |
| Agents-A1 (Referenz) | 33 | 22 | 33 | 88 | 0/3 | 3,0 min |
| qwen3.6-35b (Referenz) | 36 | 17 | 28 | 81 | 0/3 | 4,3 min |
| Qwen3.6-27B (dicht) | 20 | 30 | 31 | 81 | 0/3 | 80 min |
| Ornith 1.5 35B-A3B | 31 | 22 | 24 | 77 | 0/3 | 2,8 min |
| Nemotron 3.5 Bolt | 28 | 22 | 24 | 74 | 0/3 | 7,8 min |
| Granite 4.2 8B | 12 | 21 | 24 | 57 | 0/3 | 28 min |

Befunde:
- qwen3.8 bleibt Referenz. Auf M2 ist er der einzige, der alle vier Fehler den richtigen Dateien zuordnet.
  7 von 8 Plänen verfehlen die echte Absturzursache (`parse_date`-Format).
- Ornith 9B liefert den besten K1-Plan, ist auf M2 aber schwach (`must_change` auf `__main__.py`/README).
- Häufigste Freigabe-Verstöße aller Modelle:
  - Dateien in `must_change`, die das Schrittziel nicht nennt;
  - Tests oder Doku, die nur in `must_change` und nicht im Ziel stehen;
  - Anforderungen, die nur vage abgedeckt sind (z. B. „Token“ statt „jede Anfrage braucht das Token“).
- Kein Einzelmodell erreicht 3/3 Freigaben. Das stützt den Ansatz Planer + unabhängige Planprüfung (nächster Abschnitt).

## Kombinationen: Planer + unabhängige Planprüfung (2026-10-19)

Aufbau (`bench2.py`): Der Planer erzeugt einen gültigen Plan (echte Validierung inkl. Reparaturzügen). Ein Prüfmodell
extrahiert die Anforderungen, ordnet sie Schritten und Kriterien zu und meldet Probleme der Arten
uncovered_requirement, contradiction, invalid_file, missing_dependency, missing_test. Mit Problemen geht der Plan samt
Befunden zurück an den Planer, höchstens zwei Überarbeitungen. Router-Benchserver auf .224 (llama.cpp b11696,
`--models-max 1`). Je Auftrag ein Lauf. Blind bewertet mit Rubrik und Freigabeprüfung R1–R5, qwen3.8 allein (Plan aus
Runde 1) als Referenz.

| Kombination (Planer + Prüfer) | K1 | M2 | M3 | Summe | freigabefähig | gültig | Dauer 3 Pläne |
|---|---|---|---|---|---|---|---|
| **qwen3.8 allein** (Referenz) | 29 | **31** ✓ | **34** ✓ | **94** | **2/3** | 3/3 | ~25 min |
| Agents-A1 + qwen3.8 | 22 | 30 ✓ | 33 ✓ | 85 | 2/3 | 3/3 | 42 min |
| Agents-A1 + Agents-A1 | 31 | 24 | 29 | 84 | 0/3 | 3/3 | 11 min |
| qwen3.6 + qwen3.6 | 35 ✓ | 23 | 22 | 80 | 1/3 | 3/3 | 28 min |
| Ornith 9B + qwen3.8 | **36** ✓ | 20 | 0 | 56 | 1/3 | 2/3 | 44 min |
| Ornith 9B + Ornith 9B | 31 ✓ | 23 | 0 | 54 | 1/3 | 2/3 | 30 min |
| qwen3.8 + qwen3.8 | 21 | 0 | 31 | 52 | 0/3 | 2/3 | 87 min |

Befunde:
- **Kein klarer Sieger, keine klare Struktur.** Die LLM-Planprüfung hebt keinen Planer verlässlich über qwen3.8 allein.
  Ihr Nutzen schwankt je Auftrag stärker als zwischen den Kombinationen.
- Die Prüfschleife konvergiert nicht: Nach der ersten Überarbeitung meldete der Prüfer in 8 von 12 Fällen erneut
  Probleme, oft neue. Der Prüfer erzeugt selbst Rauschen, etwa Widersprüche, die keine sind, oder zusätzliche
  Pflichtdateien.
- qwen3.8 streut stark: allein 94 Punkte, als Planer mit Selbstprüfung 52 (K1 mit `must_change` auf fünf nicht
  verlangten Testdaten-Dateien, M2 ungültig). Ein Lauf je Auftrag reicht für feine Rangfolgen nicht.
- Drei der 18 Läufe scheiterten schon in der Planvalidierung, alle an derselben Regel (Tests für den Code eines
  anderen Schritts). Die Modelle setzen die Korrektur in zwei Reparaturzügen nicht um.
- Wiederkehrende Freigabe-Verstöße über alle Modelle: `must_change` auf Dateien, die keine Änderung brauchen
  (README, Einstiegspunkt, Eingabedaten), und vage Ziele (JSON, „jede Anfrage braucht das Token“, Zeilennummern).

Entscheidung:
- Planer und Neuplaner auf qwen3.8-27b (wie nach Runde 1 beschlossen), ohne zusätzliche LLM-Prüfstufe.
- Statt eines zweiten Modells sollen die wiederkehrenden Verstöße als **deterministische** Planprüfung im Planer-Kern
  abgefangen bzw. repariert werden (Rückverfolgung Anforderung → Schritt → Kriterium, unnötige `must_change`,
  verschobene Testpfade). Das ist allgemein und kostet keine Modellzeit. Die LLM-Prüfung wird erneut gemessen, wenn
  diese Stufe steht und mehrere Läufe je Auftrag möglich sind.
