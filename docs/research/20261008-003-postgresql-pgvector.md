# 20261008-003 – PostgreSQL + pgvector

## Question
Welche PostgreSQL-/pgvector-Version, welcher Index, welche Dimensionen?

## Why needed
P03 Persistence, P11 Embedding-Index.

## Sources
- pgvector HNSW / Limits (Supabase-Doku): https://supabase.com/docs/guides/ai/vector-indexes/hnsw-indexes
- pgvector auf PGXN: https://pgxn.org/dist/vector
- CVE-2026-3172 (EDB Assessment): https://www.enterprisedb.com/docs/security/assessments/cve-2026-3172
- Debian Security Tracker: https://security-tracker.debian.org/tracker/CVE-2026-3172
- PGDG Debian: https://www.postgresql.org/download/linux/debian/ , https://download.postgresql.org/pub/repos/apt/README
- PostgreSQL pg_restore Referenz: https://www.postgresql.org/docs/current/app-pgrestore.html

## Source date/version
pgvector 0.8.2 Fix vom 2026-02-25; Abruf 2026-10-08.

## Relevant facts
- HNSW-Index: `CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)`; Operator `<=>` = Cosinus-Distanz.
- HNSW für `vector` bis 2.000 Dimensionen (ab 0.7.0), `halfvec` bis 4.000.
- CVE-2026-3172: Buffer Overflow im parallelen HNSW-Build, betrifft 0.6.0–0.8.1; Fix 0.8.2. Nach Paket-Upgrade `ALTER EXTENSION vector UPDATE;` je Datenbank.
- `FOR UPDATE SKIP LOCKED` ist Standard-PostgreSQL und eignet sich als Queue.

## Compatibility with our hardware
`.225` (Xeon E3, 48–64 GB) ausreichend.

## Compatibility with our versions
EmbeddingGemma 2 liefert 768 Dimensionen (siehe 013) → `vector(768)` mit HNSW möglich.

## Rejected alternatives
Separater Vektor-Store (Qdrant etc.) – durch Architektur ausgeschlossen.

## Decision fixed by architecture
PostgreSQL + pgvector als Source of Truth.

## Implementation consequences
- Produktion: PostgreSQL 17 + `postgresql-17-pgvector` ≥ 0.8.2 aus PGDG.
- Migration legt `CREATE EXTENSION IF NOT EXISTS vector` an; Index-Build in der Migration ohne parallele Worker (`SET max_parallel_maintenance_workers = 0`) als zusätzliche Absicherung gegen CVE-2026-3172 auf alten Versionen.
- Health-Check meldet die pgvector-Version und warnt bei < 0.8.2.
- Build-Umgebung: PostgreSQL 16.15 + pgvector 0.6.0 (nur Tests, Warnung aktiv).

## Open risks
pgvector-Updates müssen in jeder DB per `ALTER EXTENSION` nachgezogen werden.
