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

## Offene Bugs
Noch keine.

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
