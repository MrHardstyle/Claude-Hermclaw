# 20261008-022 – Backup/Restore nach `.60` (Terra/Unraid)

## Sources
- pg_dump: https://www.postgresql.org/docs/current/app-pgdump.html
- pg_restore: https://www.postgresql.org/docs/current/app-pgrestore.html
- BUG #16458 (`--list` ist kein Restore-Test): https://www.postgrespro.com/list/thread-id/2493670
- Linuxize pg_dump/pg_restore: https://linuxize.com/post/postgresql-backup-with-pg-dump-and-pg-restore/

## Relevant facts
- `pg_dump -Fc` (Custom-Format, komprimiert, TOC); `pg_restore -l` nur Katalogprüfung; echter Test = Restore in leere DB + Zählvergleich.
- Rollen separat: `pg_dumpall --globals-only`.
- Restore ohne Rollen: `--no-owner --no-privileges`.

## Implementation consequences
- `hermclaw-backup` erzeugt: DB-Dump (`-Fc`), Globals, Config-Tarball, Artefakt-Tarball, Manifest mit SHA-256; Transfer per `rsync` über SSH auf `.60` (Unraid-Share) oder auf einen gemounteten Pfad.
- `hermclaw-restore-test` stellt in eine temporäre DB wieder her, vergleicht Zeilenzahlen aller Tabellen und Checksummen, schreibt Ergebnis als Event/Datei.
