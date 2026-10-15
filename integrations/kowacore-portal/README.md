# Kontrollraum-Button im Portal Noob2Claw/Kowacore

Das bestehende PHP-Portal auf `192.168.178.223` (Apache 2.4 + mod_php auf Port 80, MariaDB, Login/2FA, Wiki; von
außen über Nginx Proxy Manager auf `.2` als `app.kowacore.com`/`wiki.kowacore.com`) bleibt unverändert in Betrieb
(DECISIONS.md D-013). Hermclaw Next fügt dem Portal genau **eine Datei** hinzu: den Menüpunkt
**Arbeit → KI-Kontrollraum**. Seine Seite trägt den Button „KI-Kontrollraum öffnen“. Er öffnet die Weboberfläche von
Hermclaw Next, also den Kontrollraum des autonomen KI-Workers, in einem neuen Tab: `http://192.168.178.223:8080/`,
mit TLS `https://192.168.178.223:8443/`.

| Datei | Zweck |
|---|---|
| `nav/work_ki_kontrollraum.php` | Statische Kopie des Menüpunkts für `http://192.168.178.223:8080/`. Sie ist identisch mit dem, was das Deployment mit den Standardwerten schreibt; ein Test prüft das. |
| `../../infra/ansible/roles/hermclaw_ui/templates/portal-kontrollraum.php.j2` | Vorlage, die das Deployment rendert (Adresse, Ports, Titel aus dem Inventar) |
| `../../infra/ansible/roles/hermclaw_ui/tasks/portal_link.yml` | Installation durch `playbooks/deploy.yml` |

## So findet das Portal den Menüpunkt

Das Portal hat keine Menütabelle. `inc/funktionen_navigation.php` liest per `glob()` jede Datei `nav/*.php` und
sucht in ihren **ersten 700 Bytes** die Felder `ICON:`, `TITLE:`, `PRIO:`, `BEREICH:` und `RECHT:`, optional
`URL:`. Fehlt eines der fünf Pflichtfelder, ist die Datei kein Menüpunkt. Angezeigt wird der Punkt nur Konten, die
das Recht `RECHT` haben. Das Menü sortiert nach `PRIO`, dann nach `TITLE`, und gruppiert nach `BEREICH` (`work` =
„Arbeit“). Der Button verwendet:

- `ICON: cpu` (Glyphe ◉, existiert in `css/navigation.css`);
- `PRIO: 19`, also vor „Agenten“ (20);
- `BEREICH: work`;
- `RECHT: dashboard_anzeigen`. Dieses Recht schützt die Startseite des Portals und ist bei jedem arbeitenden Konto
  vorhanden.

Es gibt keine Datenbankänderung und keine Migration. Ein erneutes Anmelden ist nicht nötig, und Apache wird nicht neu
geladen.

Die Datei enthält außer dem Kommentar-Kopf kein PHP, keinen Zugangsschlüssel und kein iframe. Hermclaw verbietet
Einbetten (`frame-ancestors 'none'`, `X-Frame-Options: DENY`), deshalb ist der Button ein normaler Link. Die Zeile
`Managed by Hermclaw` markiert die Datei als Hermclaw-eigen.

## Installation durch das Deployment (Standard)

`playbooks/deploy.yml` schreibt die Datei nach den Health-Checks der Weboberfläche, aber nur, wenn das Portal unter
`hermclaw_ui_legacy_portal_root` (Standard `/var/www/noobclaw`) gefunden wird, also `index.php` und `nav/` dort
existieren. Dabei gilt:

- **Rechte:** Die Datei bekommt Besitzer und Gruppe von `nav/` und Modus `0644`.
- **Idempotent:** Ein zweiter Lauf meldet `changed=0`.
- **Fremde Dateien:** Eine Datei gleichen Namens ohne Hermclaw-Markierung wird nie überschrieben.
- **Rest des Portals:** Alles andere im Portal bleibt unberührt.

Nur den Button neu schreiben, z. B. nach einer Wiederherstellung des Portals:

```bash
cd infra/ansible
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=0.1.0rc1 --limit webui --tags portal_link -K
```

Variablen (Host-Variablen von `hermclaw-webui`, Standardwerte in `roles/hermclaw_ui/defaults/main.yml`):

| Variable | Standard | Bedeutung |
|---|---|---|
| `hermclaw_ui_portal_link` | `true` | `false` entfernt Hermclaws Button wieder, aber nur die markierte Datei |
| `hermclaw_ui_legacy_portal_root` | `/var/www/noobclaw` | Webroot des Portals |
| `hermclaw_ui_portal_link_name` | `work_ki_kontrollraum` | Dateiname ohne `.php` |
| `hermclaw_ui_portal_link_title` | `KI-Kontrollraum` | Menü- und Seitentitel |
| `hermclaw_ui_portal_link_url` | `http://<ansible_host>:<Port>/` | Ziel des Buttons; folgt `hermclaw_webui_http_port`/`hermclaw_webui_https_port` und TLS |
| `hermclaw_ui_portal_link_direct` | `false` | `true`: Der Menüpunkt springt direkt in den Kontrollraum, im selben Tab (Kopffeld `URL:`). |
| `hermclaw_ui_portal_link_right` | `dashboard_anzeigen` | Recht, das den Menüpunkt zeigt |

## Manuelle Installation (ohne Deployment)

Auf `.223` als root. Vorher die Adresse in der Datei prüfen und bei Bedarf anpassen, zum Beispiel für andere Ports
oder `https://192.168.178.223:8443/`, an beiden Stellen im Text:

```bash
P=/var/www/noobclaw
install -m 0644 nav/work_ki_kontrollraum.php "$P/nav/work_ki_kontrollraum.php"
chown --reference="$P/nav" "$P/nav/work_ki_kontrollraum.php"
```

Prüfen:

```bash
# Login-Schutz des Portals: 302 auf login.php
curl -s -o /dev/null -w '%{http_code}\n' 'http://192.168.178.223/index.php?seite=work_ki_kontrollraum'
# der Kontrollraum selbst
curl -s http://192.168.178.223:8080/healthz
```

Im Browser:

1. Im Portal anmelden.
2. **Arbeit → KI-Kontrollraum** öffnen.
3. **KI-Kontrollraum öffnen** drücken.
4. Im neuen Tab mit einem persönlichen Hermclaw-Zugangstoken anmelden (`hermclaw token-create …`,
   docs/operations/INSTALLATION.md 10.2).

Entfernen: `rm /var/www/noobclaw/nav/work_ki_kontrollraum.php`. Das Portal ist danach wieder genau im alten Zustand.

## Hinweise

- **Erreichbarkeit:** Der Kontrollraum ist nur im Heimnetz erreichbar. Von außen (über `app.kowacore.com`) führt
  der Button ins Leere, solange kein eigener NPM-Proxy-Host mit Zugriffsliste dafür existiert. Das wäre eine eigene
  Entscheidung, siehe RISK_REGISTER R-036.
- **Kein Single Sign-on:** Portal-Login und Hermclaw-Token sind getrennt. Die Cookies des Portals erreichen die
  Hermclaw-API nicht, weil nginx den `Cookie`-Header verwirft.
- **Direkter Abruf:** `nav/` hat im Portal keinen Zugriffsschutz per `.htaccess`. Die Datei ist deshalb auch direkt
  abrufbar, sie zeigt aber nur die LAN-Adresse.
- **Schreibrechte:** `nav/` gehört dem Portal (`www-data`). Wer das Portal kompromittiert, kann auch diesen Link
  ändern. Der Link ist keine Sicherheitsgrenze; Hermclaw verlangt immer sein eigenes Token.
