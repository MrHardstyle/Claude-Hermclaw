# 20261008-020 – systemd Credentials für Secrets

## Sources
- systemd Credentials: https://systemd.io/CREDENTIALS/
- systemd-creds(1): https://man7.org/linux/man-pages/man1/systemd-creds.1.html

## Relevant facts
- `LoadCredential=name:/pfad` bzw. `LoadCredentialEncrypted=` (ab systemd 250), Dienst liest `$CREDENTIALS_DIRECTORY/name`.
- `systemd-creds encrypt --name=… klartext /etc/credstore.encrypted/name` nutzt Host-Schlüssel (`/var/lib/systemd/credential.secret`, optional TPM2).
- `SetCredential=` nur für Nicht-Geheimes (Unit-Dateien sind lesbar). Limit ~1 MB pro Dienst. `PrivateMounts=` empfohlen.

## Implementation consequences
- `hermclaw.security.secrets.SecretStore` liest Secret-Referenzen `cred:<name>` aus `$CREDENTIALS_DIRECTORY`, Fallback `/etc/hermclaw/secrets/<name>` (0400), sonst Umgebungsvariable nur im Dev-Modus.
- Units: `LoadCredentialEncrypted=db-password:/etc/credstore.encrypted/hermclaw-db-password` usw.
- Secrets werden in Logs/Events/Prompts maskiert (Redactor).
