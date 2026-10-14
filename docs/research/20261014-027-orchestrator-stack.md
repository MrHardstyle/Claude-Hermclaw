# 20261014-027 – Orchestrator stack on .225: PostgreSQL 17 + pgvector (PGDG), LiteLLM proxy pin, SearXNG container, Python 3.13

## Question
1. PostgreSQL on Debian 13 "trixie": which PostgreSQL is in trixie main, how is the PGDG apt repository set up
   for trixie today (postgresql-common's `apt.postgresql.org.sh` or keyring + sources entry), package names
   `postgresql-17` / `postgresql-17-pgvector`, current pgvector version in PGDG and in trixie main, and which
   pgvector CVE forces ">= 0.8.2" (verify id and fixed version upstream) – plus anything newer.
2. LiteLLM proxy: latest stable release vs. our pin `litellm[proxy]==1.104.2`; security advisories (GHSA/CVE)
   affecting <= 1.104.2; health endpoints; `ollama_chat/` prefix and `json_schema` / `response_format` mapping
   for Ollama; running it as a systemd service in its own venv; master key and database-less mode.
3. SearXNG: official image `docker.io/searxng/searxng` tag scheme, `settings.yml` keys for the JSON API,
   limiter/botdetection for a private instance, secret key, bind address; rootless Podman Quadlet in the user
   manager with lingering.
4. Python 3.13 (trixie) compatibility with our dependencies (fastapi >= 0.135, SQLAlchemy 2, psycopg 3,
   pydantic 2, tree-sitter-language-pack).

## Why needed
P32 (deployment playbook: roles `postgresql`, `podman`, `systemd_skeleton`), INSTALLATION.md (orchestrator
`.225`), runbook `docs/runbooks/upgrade.md`, CI (`.gitlab-ci.yml`). The roles were written from R-001/R-003/
R-006/R-023 (2026-10-08). This re-check found one new pgvector CVE that raises the minimum, three LiteLLM
deployment details, one SearXNG start failure mode, and two Python 3.13 / dependency effects (see Decision).

## Sources (accessed 2026-10-14)
Access notes: `*.debian.org` (packages, tracker, security-tracker, lists), `www.postgresql.org`,
`apt.postgresql.org` and its mirrors, `docs.litellm.ai`, `docs.searxng.org`, `docs.python.org`, `osv.dev`,
`nvd.nist.gov` and `cve.org` did **not resolve** from this sandbox (DNS failure / egress policy). `ollama.com`
was not needed for this topic. Reachable and used: GitHub web pages via WebFetch (advisories, issues,
releases), `raw.githubusercontent.com` (source files at exact tags/commits), PyPI JSON API and downloads,
Docker Hub API and `registry-1.docker.io` (anonymous pull). Facts about Debian and postgresql.org pages come
from search-engine snippets of those primary pages unless marked otherwise. [P] = primary, read directly;
[P/snippet] = primary page seen only through a search snippet; [M] = own measurement in this sandbox;
[S] = secondary.

Verification (adversarial re-check, 2026-10-14, separate agent): every version, CVE/GHSA id, advisory range,
package name, Docker tag/digest and config key above was re-checked against the listed primary sources
(pgvector issues/CHANGELOG/GHSA, docker-library versions.json, Launchpad Debian mirror, PyPI JSON, LiteLLM
repo advisories and installed 1.104.2/1.105.1 source, SearXNG source at `f4822b3fc` and registry image config,
podman v5.4.2 docs, CPython `Lib/http`). Confirmed unless a row says "corrected on verification" or
"unverified". Corrections: sid upload history (P8), forky status (P8, now unverified), 1.82.7 vs 1.82.8
malware mechanics (L4), postgresql-common version caveat for the PGDG helper (P5/P4), unmaintained `prisma`
(L5), restart wording (P16). The [M] measurements (L6, L10, S4, S6, Y3) were not re-run.

PostgreSQL / pgvector
- [P] pgvector CHANGELOG (master): https://raw.githubusercontent.com/pgvector/pgvector/master/CHANGELOG.md
- [P] pgvector issue #959 "Buffer overflow with parallel HNSW index build": https://github.com/pgvector/pgvector/issues/959
- [P] pgvector issue #1036 "Buffer overflow with IVFFlat index build": https://github.com/pgvector/pgvector/issues/1036
- [P] GitHub Advisory Database GHSA-789c-mgqf-5hwx (CVE-2026-3172): https://github.com/advisories/GHSA-789c-mgqf-5hwx
- [P] GitHub Advisory Database GHSA-pw7q-9788-x566 (CVE-2026-103484): https://github.com/advisories/GHSA-pw7q-9788-x566
- [P] pgvector repo security tab (no repository advisories published): https://github.com/pgvector/pgvector/security/advisories
- [P] pgvector Dockerfile at master (builds v0.8.7 from source): https://raw.githubusercontent.com/pgvector/pgvector/master/Dockerfile
- [P] Docker Hub tags of `pgvector/pgvector` (0.8.7-pg17-trixie pushed 2026-10-01): https://hub.docker.com/v2/repositories/pgvector/pgvector/tags
- [P] Launchpad mirror of Debian `pgvector` (sid 0.8.7-1, 2026-10-02) and its changelog:
  https://launchpad.net/debian/+source/pgvector , https://launchpad.net/debian/+source/pgvector/+changelog
- [P] Official `postgres` image definition (docker-library) 17/trixie, `PG_VERSION 17.11-1.pgdg13+2`, PGDG
  repository line and signing-key fingerprint: https://raw.githubusercontent.com/docker-library/postgres/master/17/trixie/Dockerfile ,
  https://raw.githubusercontent.com/docker-library/postgres/master/versions.json
- [P] `/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh` and its keyrings from Debian-derived
  `postgresql-common` 257build1.1 (Ubuntu 24.04 build host): options, written deb822 file, keyring path;
  `/usr/share/postgresql-common/maintscripts-functions` and `/etc/postgresql-common/createcluster.conf`
  (main-cluster auto-creation)
- [P/snippet] PostgreSQL Debian download page (trixie supported; quick start with `postgresql-common`):
  https://www.postgresql.org/download/linux/debian/ ; repository README: https://apt.postgresql.org/pub/repos/apt/README ;
  wiki https://wiki.postgresql.org/wiki/Apt
- [P/snippet] packages.debian.org trixie `postgresql-17-pgvector` 0.8.0-1: https://packages.debian.org/trixie/postgresql-17-pgvector
- [P/snippet] Debian security tracker: source package pgvector, CVE-2026-3172, CVE-2026-103484, DSA-6438-1
  (postgresql-17 17.11-0+deb13u1), DSA-6270-1 (17.10-0+deb13u1):
  https://security-tracker.debian.org/tracker/source-package/pgvector , https://security-tracker.debian.org/tracker/CVE-2026-3172 ,
  https://security-tracker.debian.org/tracker/CVE-2026-103484 , https://security-tracker.debian.org/tracker/DSA-6438-1
- [P/snippet] PostgreSQL news "pgvector 0.8.7 Released": https://www.postgresql.org/about/news/pgvector-087-released-3392/
- [S] Third-party Debian 13 install guide reporting candidate `0.8.6-1.pgdg13+1` (found via search, not opened);
  Pigsty extension catalogue https://pigsty.io/ext/e/vector/ (snippet, dated ~55 days)
- [P] pgvector issue #1006 (CVE-2026-18022, 32-bit IVFFlat build): https://github.com/pgvector/pgvector/issues/1006 ;
  fix commit for #1036: https://github.com/pgvector/pgvector/commit/ee00d39ab30c2a7cf9510abf9288c624d91db156
- [P] Launchpad Debian mirror, publishing history of `pgvector`: https://launchpad.net/debian/+source/pgvector/+publishinghistory ;
  `postgresql-common` changelog (288: `apt.postgresql.org.sh` gains `-a`): https://launchpad.net/debian/+source/postgresql-common/+changelog
- [P/snippet] pgsql-pkg-debian repository notices on www.postgresql.org/message-id (trixie-pgdg pgvector `0.8.6-1.pgdg13+2`);
  tracker.debian.org `postgresql-common` (stable 278, testing 293); postgresql.org Debian download page (architectures)
- [S] aptpeek.dev index of apt.postgresql.org (trixie-pgdg `postgresql-common` 289/290/291.pgdg13+1), snippets only

LiteLLM
- [P] PyPI JSON for `litellm` (release list, upload dates, `requires_python`, `requires_dist`, wheel digests,
  OSV `vulnerabilities` field): https://pypi.org/pypi/litellm/json , https://pypi.org/pypi/litellm/1.104.2/json
- [P] Repository advisories: https://github.com/BerriAI/litellm/security/advisories (pages 1–2) and the advisories
  GHSA-7hp6-4w63-5g45, GHSA-g5ff-637f-6q2m, GHSA-hhww-mrg2-969h, GHSA-3cv6-jpf6-8222, GHSA-hx8v-g79f-8w5f,
  GHSA-4xpc-pv4p-pm3w (https://github.com/BerriAI/litellm/security/advisories/<id>)
- [P] GitHub Advisory Database, pip ecosystem "litellm" (47 entries): https://github.com/advisories?query=litellm+ecosystem%3Apip ;
  GHSA-6wvf-77m9-58rm, GHSA-5mg7-485q-xm76 (malicious 1.82.7/1.82.8), GHSA-98x5-vq43-vc5p
- [P] Release notes v1.104.2, v1.104.3, v1.105.1 and the release list: https://github.com/BerriAI/litellm/releases
- [P] Installed source of litellm 1.104.2 (repo `.venv` and a fresh Python 3.13 venv):
  `litellm/proxy/health_endpoints/_health_endpoints.py`, `litellm/llms/ollama/chat/transformation.py`,
  `litellm/llms/ollama/completion/handler.py`, `litellm/proxy/auth/user_api_key_auth.py`,
  `litellm/proxy/proxy_server.py`, `litellm/proxy/db/exception_handler.py`, `litellm/proxy/proxy_cli.py`,
  `litellm/litellm_core_utils/get_model_cost_map.py`, `litellm/proxy/management_endpoints/ui_sso.py`;
  `exception_handler.py` of the 1.105.1 wheel
- [M] Proxy 1.104.2 started on Python 3.13.16 (DB-less, master key from env), probed with curl; a fake Ollama
  `/api/chat` endpoint recorded the forwarded request body
- [P/snippet] LiteLLM docs (health, Ollama provider): https://docs.litellm.ai/docs/proxy/health ,
  https://docs.litellm.ai/docs/providers/ollama
- [P] Prisma Client Python repository (archived 2025-04-15, "no longer being maintained"):
  https://github.com/RobertCraigie/prisma-client-py ; PyPI JSON https://pypi.org/pypi/prisma/0.15.0/json
- [P] LiteLLM PRs #46313 / #44243 / #44508 (content of the 1.105.1 backport): https://github.com/BerriAI/litellm/pull/46313
- [S] Wiz, "TeamPCP trojanizes LiteLLM" (1.82.7 vs 1.82.8 payload mechanics):
  https://wiz.io/blog/threes-a-crowd-teampcp-trojanizes-litellm-in-continuation-of-campaign

SearXNG
- [P] Docker Hub tag list and tag detail: https://hub.docker.com/v2/repositories/searxng/searxng/tags
- [P] Image index, amd64 manifest and image config of `2026.10.9-f4822b3fc` via `registry-1.docker.io` (anonymous token)
- [P] Source at the image revision `f4822b3fc`: `container/dist.dockerfile`, `container/builder.dockerfile`,
  `container/entrypoint.sh`, `searx/settings.yml`, `searx/settings_defaults.py`, `searx/settings_loader.py`,
  `searx/webapp.py`, `searx/limiter.py`, `searx/limiter.toml`, `docs/admin/installation-docker.rst`,
  `docs/admin/settings/settings_search.rst` (https://github.com/searxng/searxng/tree/f4822b3fc)
- [P] Podman v5.4.2 docs `podman-systemd.unit.5.md`, `options/userns.container.md` and source
  `pkg/specgen/generate/namespaces.go` (keep-id sets the process UID/GID): https://github.com/containers/podman/tree/v5.4.2
- [M] Quadlet dry run of the repo's `hermclaw-searxng.container.j2` (podman 4.9.3 quadlet); the pinned image
  pulled by tag@digest and run with the repo's `searxng-settings.yml.j2` (podman 4.9.3, keep-id 977)
- [P] Image config + build history of the amd64 manifest `sha256:f3e63958…` via `registry-1.docker.io`
  (re-read during verification: Void Linux/xbps base, BusyBox applets linked into `/usr/bin`, no `USER`, no `HEALTHCHECK`)
- [P/snippet] Debian podman in trixie `5.4.2+ds1-2` (packages.debian.org / piuparts snippets)

Python
- [P] PyPI JSON of fastapi, starlette, pydantic, pydantic-core, pydantic-settings, SQLAlchemy, alembic,
  psycopg, psycopg-binary, pgvector, uvicorn, uvloop, httptools, httpx, lxml, beautifulsoup4, tree-sitter,
  tree-sitter-language-pack, jsonschema, PyYAML (latest versions, `requires_python`, cp313/abi3 wheels)
- [P] CPython `Lib/http/__init__.py` at v3.12.0 and v3.13.0: https://github.com/python/cpython/blob/v3.13.0/Lib/http/__init__.py
- [P] tree-sitter-language-pack README (on-demand parser downloads): https://raw.githubusercontent.com/xberg-io/tree-sitter-language-pack/main/README.md ;
  installed 1.21.0 (`_native.pyi`, strings of `_native.abi3.so`, downloaded `manifest.json`)
- [M] Repo test suites `tests/unit`, `tests/contract`, `tests/security` run with Python 3.13.16 against the
  dependency set resolved from `pyproject.toml` (bytecode and pytest cache disabled, no repo writes)
- [P/snippet] Debian 13 Python 3.13 (R-001; Grid5000 trixie environment notes)

## Relevant facts

### PostgreSQL and pgvector

| # | Fact | Source |
|---|---|---|
| P1 | trixie main ships PostgreSQL **17** (source `postgresql-17`); current trixie-security version **17.11-0+deb13u1** (DSA-6438-1, 2026-08-13, 28 CVEs; previous DSA-6270-1 → 17.10-0+deb13u1). | [P/snippet] security tracker DSA-6438-1, DSA-6270-1 |
| P2 | PGDG `trixie-pgdg` currently serves **postgresql-17 `17.11-1.pgdg13+2`** (PGDG revision suffix for Debian 13 is `pgdg13+N`); 17.11 is the current 17.x minor (versions.json: 18.6, 17.11, 16.15, 15.19, 14.24, 19beta4; release of 2026-08-13 with 28 CVEs). PGDG supports trixie (amd64, arm64, ppc64el; the download page additionally lists loong64 for trixie and newer – snippet only). Note: the docker-library image uses the per-major component (`trixie-pgdg main 17`); `Components: main` also carries all majors. | [P] docker-library 17/trixie Dockerfile + versions.json; [P/snippet] postgresql.org download page and 2026-08-13 release news |
| P3 | Package names (identical in Debian and PGDG): `postgresql-17`, `postgresql-client-17`, `postgresql-17-pgvector`, `postgresql-common`, `libpq5`. | [P/snippet] packages.debian.org; [P] docker-library Dockerfile |
| P4 | PGDG signing key: file `ACCC4CF8.asc` (https://www.postgresql.org/media/keys/ACCC4CF8.asc), fingerprint B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8, uid "PostgreSQL Debian Repository", no expiry (re-checked: `gpg --show-keys --with-colons` shows an empty expiry field). `postgresql-common` ships it as `/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg` and `.asc` – verified with `dpkg -S` only on the Ubuntu 24.04 build `257build1.1`; that trixie's Debian `postgresql-common` **278** ships the same files is expected (the PGDG README's trixie sample uses that `.gpg` path) but was not checked against the trixie file list. | [P] docker-library Dockerfile; [M] `gpg --show-keys`, `dpkg -S` on 257build1.1; [P/snippet] PGDG README |
| P5 | Official helper `/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh` (package `postgresql-common`): options `-c` extra components, `-f` sources file, `-h` host, `-i -v N` install version N, `-p` purge, `-s` deb-src, `-t` pgdg-testing, `-y` no confirmation. Writes **`/etc/apt/sources.list.d/pgdg.sources`** (deb822: `URIs: https://apt.postgresql.org/pub/repos/apt`, `Suites: <codename>-pgdg`, `Components: main`, `Signed-By:` the postgresql-common keyring, `.gpg` preferred, `.asc` fallback), removes an old `pgdg.list` (and `/etc/apt/trusted.gpg.d/apt.postgresql.org.gpg`), runs `apt-get update`. In 257 `-y` is a no-op flag: *any* option suppresses the prompt. **Version caveat (verification):** this option list is from 257 (Ubuntu); trixie ships `postgresql-common` **278** (tracker: stable 278, testing 293), whose script was not inspected; upstream **288** (sid, 2026-01-26) added an **`-a` architecture option** (default: native arch), so after `postgresql-common` is upgraded from trixie-pgdg (289+ seen in a secondary index) the helper has `-a` as well. | [P] script source (postgresql-common 257); [P] Launchpad changelog 288; [P/snippet] download page quick start, tracker |
| P6 | Manual method per README/wiki: key to `/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc`, sources entry with `signed-by` pointing to that file and suite `trixie-pgdg`. Two entries for the same repo with different `Signed-By` make apt fail ("conflicting values") – use exactly one file. | [P/snippet] README, wiki, itsfoss thread |
| P7 | `postgresql-17` postinst creates cluster **17/main** automatically on first install (system locale via `set_system_locale`) unless `create_main_cluster` in `/etc/postgresql-common/createcluster.conf` is off. | [P] `maintscripts-functions`, `createcluster.conf` (postgresql-common 257) |
| P8 | **trixie main `postgresql-17-pgvector` = 0.8.0-1** – vulnerable to CVE-2026-3172 (tracker: `[trixie] - pgvector <no-dsa> (Minor issue)`) and, by version range, to CVE-2026-103484. **sid 0.8.7-1** (uploaded 2026-10-02 11:30 +0200); the sid version before it was **0.8.6-1** (uploaded 2026-08-02, migrated to testing 2026-08-05); 0.8.2-1 (the CVE-2026-3172 fix) was uploaded 2026-02-26. **forky: unverified** – older tracker snapshots show 0.8.6-1, while a security-tracker snippet already lists forky and sid at 0.8.7-1 (0.8.6-1 needed 3 days to migrate, so 0.8.7-1 in forky by 2026-10-14 is likely). Whether the tracker marks CVE-2026-103484 as fixed in sid/forky could not be confirmed (pages unreachable; snippets contradictory). | [P/snippet] packages.debian.org, security tracker, tracker.debian.org news; [P] Launchpad changelog + publishing history |
| P9 | **pgvector upstream latest = 0.8.7 (2026-10-01)**; 0.8.6 (07-29), 0.8.5 (07-08), 0.8.4 (06-30), 0.8.3 (06-17, HNSW vacuum index-corruption fix), 0.8.2 (02-25). | [P] CHANGELOG |
| P10 | **CVE-2026-3172** = GHSA-789c-mgqf-5hwx: buffer overflow (integer wraparound) in **parallel HNSW index build**; affected **0.6.0–0.8.1**, fixed **0.8.2**; a DB user who can create/reindex an HNSW index with parallel workers can leak data of other relations or crash the server; CVSS 3.1 8.1. | [P] issue #959, GHSA |
| P11 | **NEW: CVE-2026-103484** = GHSA-pw7q-9788-x566 (published 2026-10-01; reported by Compass Security): out-of-bounds write in **IVFFlat index build**, "0.8.6 and below", fixed **0.8.7**; a DB user able to create an IVFFlat index can write out of bounds → **arbitrary code execution** in the server process; CVSS 3.1 8.8 (`AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H`). The issue lists no workaround, only "upgrade". The fix commit `ee00d39` also adds dimension checks to HNSW build/insert/scan ("hardened index entry points"); the advisory itself names only IVFFlat. The GHSA entry has no machine-readable affected/patched ranges ("Unknown"), so scanners keyed on GHSA metadata will not flag it. postgresql.org announcement dated 2026-10-05 (snippet). | [P] issue #1036, GHSA, commit ee00d39; [P/snippet] postgresql.org news |
| P12 | CVE-2026-18022 (issue #1006, 2026-07-29): IVFFlat build overflow via integer wraparound **on 32-bit systems**, affected 0.8.5 and below, fixed 0.8.6; the mitigation text addresses 32-bit users only (the issue does not state explicitly that 64-bit is unaffected) – not relevant on amd64, and superseded by the 0.8.7 floor anyway. | [P] issue #1006, CHANGELOG 0.8.6 entry |
| P13 | PGDG's current trixie pgvector build could **not** be confirmed: the newest version seen is 0.8.6 – `0.8.6-1.pgdg13+1` (secondary install guide) and `0.8.6-1.pgdg13+2` (pgsql-pkg-debian repository notices for trixie-pgdg, search snippet, undated). No notice or index entry for a 0.8.7 PGDG build was found. A `0.8.7-1.pgdg13+N` build is plausible because PGDG rebuilds the Debian sid packaging (sid has 0.8.7-1 since 2026-10-02), but unverified; the revision suffix `+N` must not be hard-coded. | [S] install guide; [P/snippet] pgsql-pkg-debian notices; [P] Launchpad |
| P14 | Docker image `pgvector/pgvector:0.8.7-pg17-trixie` exists (pushed 2026-10-01); it compiles pgvector v0.8.7 from source on top of `postgres:17-trixie` (PGDG). | [P] Docker Hub API, pgvector Dockerfile |
| P15 | Hermclaw itself only builds **HNSW** indexes (`ix_code_chunks_embedding_hnsw`, `vector(768)`, `vector_cosine_ops`) with `max_parallel_maintenance_workers = 0`; no IVFFlat index exists. CVE-2026-103484 therefore needs SQL execution as a role that can create a table/index (the `hermclaw` role owns its database). | [P] `migrations/versions/20261008_0001_initial_schema.py`, `..._0002_...py` |
| P16 | A pgvector package upgrade replaces `vector.so`; backends that already loaded the old library keep running old code until they exit, and `ALTER EXTENSION vector UPDATE` only updates SQL objects. New backends load the new file (vector is not in `shared_preload_libraries` here), so *strictly* recycling all sessions suffices; a **server restart is the simple, reliable way** to guarantee no old code remains (pooled/long-lived connections). | [S] analysis found via search ("running sessions" note); consistent with PostgreSQL's dynamic-loading model |

### LiteLLM proxy

| # | Fact | Source |
|---|---|---|
| L1 | Latest stable **1.105.1** (2026-10-14); 1.105.0 (10-11); **1.104.3** (10-11); pin **1.104.2** (2026-10-08 08:34 UTC). Pre-releases: 1.106.0rc2 (10-13), 1.107.0.dev1 (10-14). Patch releases of older lines continue (1.101.6, 1.102.4, 1.103.4). | [P] PyPI JSON |
| L2 | 1.104.3 = feature backports only (decision-model guardrail, decisions follow-ups, auto-router UI, UI bundle); 1.105.1 = one proxy backport (#46313 = #44243 batched daily model-usage writes + #44508 spend logged once on the chat→Responses bridge; both regressions/perf, DB-mode only). Neither mentions a security fix. | [P] GitHub release notes, PR #46313 |
| L3 | **No published advisory affects 1.104.2.** Newest critical: GHSA-7hp6-4w63-5g45 (2026-09-30, CVSS 9.9, internal_user → proxy admin via salt-key reuse): affected `>=1.91.0,<1.100.4`, `>=1.101.0,<1.101.3`, `>=1.102.0,<1.102.2`, `>=1.103.0,<1.103.1`, `>=1.104.0rc1,<1.104.0rc2`; patched 1.104.0rc2 → 1.104.2 is outside the range. Others: GHSA-g5ff-637f-6q2m (high, 10-01) < 1.95.0; GHSA-hhww-mrg2-969h (moderate) 1.65.5–<1.85.0; GHSA-3cv6-jpf6-8222 / CVE-2026-84377 < 1.94.0; GHSA-hx8v-g79f-8w5f / CVE-2026-59823 <= 1.83.8; GHSA-4xpc-pv4p-pm3w / CVE-2026-49468 < 1.84.0; GHSA-6wvf-77m9-58rm / CVE-2026-37004 < 1.83.7. Also checked during verification: GHSA-7488-6r32-c95q (CVE-2026-59822) < 1.84.0, GHSA-4jcj-7x88-m979 (CVE-2026-12773) < 1.84.0, GHSA-5jmr-gcrj-2c9q (CVE-2026-59820) < 1.83.7-stable, GHSA-4g5m-c9r5-49xf < 1.83.10-stable, GHSA-72m8-9m7m-h278 < 1.82.0-stable, GHSA-wpfp-gwwc-vwq6 < 1.83.10, GHSA-w2mh-qq9q-453x <= 1.82.2; repo-advisory page 2 lists only April/May 2026 items. Caveat: GHSA-3cv6-jpf6-8222 metadata is inconsistent (affected "<1.94.0" but patched list includes 1.94.3/1.95.1/1.96.2), irrelevant for 1.104.2. The three newest repo advisories (7hp6, g5ff, hhww) were not yet in the reviewed GitHub Advisory DB listing. PyPI's OSV `vulnerabilities` list for 1.104.2 and 1.105.1 is empty. | [P] repo advisories, GitHub Advisory DB, PyPI JSON |
| L4 | Supply chain: GHSA-5mg7-485q-xm76 (published 2026-03-25, CWE-506, critical, affected `>=1.82.7,<=1.82.8`, no patched version) – **litellm 1.82.7 and 1.82.8 on PyPI contained credential-harvesting malware**. *Corrected on verification:* the `litellm_init.pth` interpreter-start hook (exfiltrates env vars, SSH/cloud credentials) was only in **1.82.8**; **1.82.7** carried the payload inside `litellm/proxy/proxy_server.py` (runs on `import litellm.proxy…` / `litellm --proxy`) [S: Wiz]. A `.pth` check therefore does not detect the 1.82.7 variant – the hash-pinned lock is the real control. The versions were removed from PyPI (secondary reports). | [P] GHSA-5mg7-485q-xm76, GHSA-98x5-vq43-vc5p; [S] Wiz |
| L5 | `requires_python >=3.10,<3.15`; for glibc x86_64 there is one abi3 wheel `litellm-1.104.2-cp310-abi3-manylinux_2_28_x86_64.whl`, sha256:416c3538ea78b08d637ec45564a919232aa2093e73adb9dd7752f7cff2ec585e (other abi3 wheels exist for aarch64, musllinux, macOS, Windows). The `proxy` extra pins `fastapi>=0.136.3`, `starlette>=1.0.1`, `uvicorn`, `granian`, `gunicorn`, `mcp`, `polars`, `litellm-proxy-extras==0.4.102.post1`, `litellm-enterprise==0.1.71`; `prisma` (`>=0.11.0,<1.0`) is only in the `extra-proxy` extra. **`prisma` (Prisma Client Python) is archived since 2025-04-15 and "no longer being maintained"; 0.15.0 (2024-08-16) is its last release and depends on `nodeenv` (its CLI can fetch Node/engines – not exercised by a bare `import prisma`, unverified).** | [P] PyPI JSON 1.104.2 and prisma 0.15.0; [P] prisma-client-py repo |
| L6 | `pip install 'litellm[proxy]==1.104.2'` into a fresh **Python 3.13** venv works (627 MB; resolved fastapi 0.143.0, starlette 1.8.0, pydantic 2.14.1, granian 2.8.4, openai 2.54.0). | [M] |
| L7 | Health: `/health/liveliness` and alias `/health/liveness` – unauthenticated, no dependency checks, `"I'm alive!"` 200 (503 while shutting down). `/health/readiness` – unauthenticated, returns `{"status":"healthy","db":"Not connected"}` **200 in DB-less mode**; 503 only if a configured DB is unreachable or during shutdown. `/health/readiness/details` and `/health` require auth; `/health` makes a real LLM call per model. | [P] `_health_endpoints.py` 1.104.2; [M] curl |
| L8 | `ollama_chat/<tag>` → Ollama `/api/chat`; `response_format {"type":"json_schema","json_schema":{"schema":S}}` → Ollama **`format: S`**; `{"type":"json_object"}` → `format: "json"`. Embeddings via `ollama/<tag>` → `/api/embed`. Re-verified on Python 3.13: the forwarded body contained `"format": {"type":"object",...,"required":["a"]}`. | [P] `ollama/chat/transformation.py`, `ollama/completion/handler.py`; [M] fake Ollama |
| L9 | Master key: `general_settings.master_key: os.environ/LITELLM_MASTER_KEY` is resolved at startup; **if unresolved, LiteLLM logs CRITICAL "LITELLM_MASTER_KEY is not set! All requests will be treated as INTERNAL_USER with no admin access. …" and runs in no-auth dev mode** (`is_no_auth_dev_mode`: master key unset and no JWT/OAuth2 auth → any api_key gets an INTERNAL_USER token, so LLM routes are open). The key is compared with `secrets.compare_digest`; **no `sk-` prefix is required** (a non-`sk-` key worked) and the raw key is replaced by an alias before logging. | [P] `proxy_server.py`, `user_api_key_auth.py`; [M] |
| L10 | **DB-less mode without the `prisma` package: requests with no Authorization header get HTTP 500** (`ModuleNotFoundError: No module named 'prisma'` raised in `proxy/db/exception_handler.py::is_database_infrastructure_error` while formatting the auth error). Access is still denied. With `prisma 0.15.0` installed: missing key → **401** `auth_error`; a wrong key → **400** `no_db_connection` ("No connected db."). The unguarded `import prisma` is unchanged in 1.105.1. | [M] curl + proxy log; [P] 1.104.2 and 1.105.1 source |
| L11 | At startup LiteLLM fetches `model_prices_and_context_window.json` from `raw.githubusercontent.com/BerriAI/litellm/main` unless `LITELLM_LOCAL_MODEL_COST_MAP=True`; tiktoken encodings are bundled. Further remote fetches exist with their own switches (lazily/on use, not all at startup): Anthropic beta-header config (`LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=True`), auto-router presets (`LITELLM_LOCAL_AUTOROUTER_PRESETS=True`), policy templates (`LITELLM_LOCAL_POLICY_TEMPLATES=true`), UI blog feed (`LITELLM_LOCAL_BLOG_POSTS=True`). `LITELLM_TELEMETRY` is not read anywhere in 1.104.2 (`--telemetry` is a "deprecated no-op"). The Admin UI is served at `/ui/` (200); `DISABLE_ADMIN_UI=True` disables its login path. | [P] `get_model_cost_map.py`, `__init__.py`, `proxy_cli.py`, `ui_sso.py`; [M] |
| L12 | No first-party systemd guidance exists in the reachable sources; the documented non-container start is the `litellm --config … --host … --port …` CLI from a pip install. | [P] `proxy_cli.py`; [P/snippet] docs deploy page |

### SearXNG

| # | Fact | Source |
|---|---|---|
| S1 | Tag scheme: **`YYYY.M.D-<9-hex commit>`** plus `latest`, no semver; several builds per day; multi-arch index (amd64, arm64, arm/v7). All 100 newest tags match the scheme. Newest: `2026.10.13-22c64b8d3` (2026-10-13, index sha256:9b61996edcc679fb6665290f95fda15389a14eda6060e41a25c63ab2758f2436). Mirror: `ghcr.io/searxng/searxng` (docs: Docker Hub rate limits). | [P] Docker Hub API, installation-docker.rst |
| S2 | Our pin **`2026.10.9-f4822b3fc`** exists and is active: index sha256:85492ba800a861e3e8ff6014c430e81369fe6ca766a9990595f616d1ce95b272, amd64 manifest sha256:f3e639582391a481a7a9965d8a3611319cc8561f03929277cca0a8b4cd610830, revision label f4822b3fc46726bb259d14c6332b702c76b98f82, created 2026-10-09. Pull by `tag@digest` works. | [P] registry; [M] podman pull |
| S3 | Image: entrypoint `/usr/local/searxng/entrypoint.sh` → `granian searx.webapp:app` (WSGI); `GRANIAN_HOST=::`, `GRANIAN_PORT=8080`; volumes `/etc/searxng` (config) and `/var/cache/searxng` (sqlite caches, must be writable); no `USER` (root) in the image config, no `HEALTHCHECK`; base is Void Linux (xbps removed) with all BusyBox applets symlinked into `/usr/bin` (image history), so a BusyBox `wget` is very likely present (not executed). "Python 3.14 inside" comes from the `.venv` layer and was not re-checked in verification. `SEARXNG_PORT` (numeric only) is copied to `GRANIAN_PORT`; `SEARXNG_BIND_ADDRESS` is **not** mapped to `GRANIAN_HOST`. | [P] image config + history, dist.dockerfile, entrypoint.sh |
| S4 | **With IPv6 unavailable in the container network namespace the container exits** (`RuntimeError: Address family not supported by protocol (os error 97)` from granian binding `::`); with `GRANIAN_HOST=0.0.0.0` it starts. errno 97 = `EAFNOSUPPORT`, i.e. the sandbox kernel has no IPv6 at all; on a stock Debian kernel with IPv6 this failure is unlikely, but the setting removes the dependency. | [M] podman 4.9.3, sandbox without IPv6 |
| S5 | `--userns=keep-id:uid=977,gid=977` maps the host user to 977 **and sets the process UID/GID to 977** (`searxng`); `id` in the container: `uid=977(searxng) gid=977(searxng)`. The entrypoint only prints an ownership WARNING for mounts not owned `searxng:searxng` when not root (no chown); `FORCE_OWNERSHIP` (default true) acts only as root. | [P] podman `namespaces.go`, entrypoint.sh; [M] |
| S6 | `search.formats` defaults to `[html]` via the shipped `searx/settings.yml` (relevant with `use_default_settings: true`; the schema fallback in `settings_defaults.py` would be `[html, csv, json, rss]`); a format not listed → `/search?format=…` **403**. With `[html, json]`: `GET /search?q=…&format=json` → 200 `application/json` (`query, results, answers, corrections, infoboxes, suggestions, unresponsive_engines`); csv → 403. `/healthz` → `OK` 200. | [P] webapp.py, settings_search.rst; [M] |
| S7 | `server.limiter` (env `SEARXNG_LIMITER`) and `server.public_instance` default **false**. The limiter requires Valkey; enabled without Valkey it logs an error and stays off (with `public_instance` it exits). Botdetection still initialises (for the self_info plugin) and reads `/etc/searxng/limiter.toml` if present (defaults: `trusted_proxies` 127.0.0.0/8, ::1). A private instance needs neither Valkey nor `limiter.toml`. | [P] limiter.py, limiter.toml, settings.yml |
| S8 | `server.secret_key` is overridden by env **`SEARXNG_SECRET`** (env wins over settings.yml); in non-debug mode the value `ultrasecretkey` (shipped default) makes SearXNG **exit(1)**. Other env overrides: `SEARXNG_BASE_URL`, `SEARXNG_BIND_ADDRESS`, `SEARXNG_PORT`, `SEARXNG_IMAGE_PROXY`, `SEARXNG_METHOD`, `SEARXNG_VALKEY_URL`. `server.port`/`bind_address` (8888/127.0.0.1) apply to the built-in dev server only; the container listens per `GRANIAN_*`. | [P] settings_defaults.py, webapp.py |
| S9 | The repo template `searxng-settings.yml.j2` sets `secret_key` to a fixed placeholder string. Because env wins this is harmless while the podman secret exists, but if the secret were missing SearXNG would start with that known string instead of refusing to start; the repo's own secret scan (`tests/security/test_security_checks.py::test_repository_contains_no_secrets`) also flags that line (`secret_assignment`). (At verification time the template in the repo had already been changed to set no `secret_key`.) | [P] repo template; [M] test run |
| S10 | Quadlet (podman 5.4.2 = trixie's `5.4.2+ds1-2`): rootless search paths include `/etc/containers/systemd/users/$(UID)`; `[Install]` supports `WantedBy`/`RequiredBy`/`Alias`; `Image=` accepts tag or digest; `UserNS=`, `Secret=` (env or file), `PublishPort=`, `ReadOnly=` documented. The repo quadlet dry-runs cleanly (podman 4.9.3 quadlet). A generator-created `WantedBy=default.target` link is honoured at the next start of the user manager; `daemon-reload` alone does not start the unit (systemd semantics, not measured here). | [P] podman-systemd.unit.5.md v5.4.2; [M] quadlet -dryrun |
| S11 | At startup SearXNG fetches tracker-pattern rules (`cdn.jsdelivr.net`, `rules2.clearurls.xyz`); engines need outbound HTTPS. Podman copies the host's proxy variables into containers by default (seen in the sandbox). | [M] container log |

### Python 3.13

| # | Fact | Source |
|---|---|---|
| Y1 | trixie's default Python is **3.13** (`python3`, `python3-venv` separate package). | [P/snippet] R-001, Grid5000 |
| Y2 | All runtime deps have cp313 or pure/abi3 wheels for linux x86_64: fastapi 0.143.1 (>=3.10), starlette 1.8.0 (**>=3.11**), pydantic 2.14.1 / pydantic-core 2.50.1, pydantic-settings 2.16.0 (>=3.11), SQLAlchemy 2.1.4 (**>=3.11**), alembic 1.20.0, psycopg + psycopg-binary 3.3.6, pgvector 0.5.1, uvicorn 0.54.0, uvloop 0.23.0, httptools 0.9.0, lxml 6.1.3, tree-sitter 0.26.0, tree-sitter-language-pack 1.22.1 (abi3, **manylinux_2_34** → glibc >= 2.34, trixie has a newer glibc). | [P] PyPI JSON |
| Y3 | Test run on Python 3.13.16 (deps resolved from `pyproject.toml`: fastapi 0.143.0, pydantic 2.13.5, SQLAlchemy 2.1.4, psycopg 3.3.6, tree-sitter-language-pack 1.21.0): **1735 passed, 2 failed** in `tests/unit tests/contract tests/security`. Integration tests (PostgreSQL/podman) were not run under 3.13. | [M] |
| Y4 | Failure 1 is **3.13-specific**: `tests/unit/test_contract_docs.py::test_openapi_document_is_current` – CPython 3.13 renamed `HTTPStatus` phrases per RFC 9110 (413 "Request Entity Too Large" → **"Content Too Large"**, 422 "Unprocessable Entity" → **"Unprocessable Content"**, also 414/416); FastAPI uses the phrase as OpenAPI response description, so `docs/contracts/openapi.json` generated on 3.12 never matches on 3.13. No runtime code depends on the phrases (grep). | [P] CPython `Lib/http/__init__.py` v3.12.0/v3.13.0; [M] diff of both generated documents |
| Y5 | Failure 2 is **not** 3.13-specific (fails identically on 3.12): the repo secret scan flags `docs/operations/bootstrap.md:213`, `docs/research/20261014-026-gpu-ollama-stack.md:101` (hex checksum in a line with a keyword) and the SearXNG settings template (S9). | [M] |
| Y6 | **tree-sitter-language-pack >= 1.0.0 (2026-03-21) no longer ships grammars**: the wheel shrank from ~51 MB (0.13.0) to ~2–6 MB; the first `get_parser("javascript")` downloads a ~23 MB bundle `parsers-linux-x86_64.tar.zst` from `https://github.com/xberg-io/tree-sitter-language-pack/releases/download/v<ver>/` (sha256 checked against a downloaded `manifest.json`) into `$TREE_SITTER_LANGUAGE_PACK_CACHE_DIR` or `$XDG_CACHE_HOME`/`~/.cache` + `tree-sitter-language-pack/v<ver>/libs`, and loads the `.so` files. `pyproject.toml` allows `>=0.6`, so the installed version (and its cache path) floats. | [P] README, PyPI release sizes, `_native.pyi`; [M] fresh-cache test |
| Y7 | Removed-in-3.13 stdlib modules (PEP 594: cgi, crypt, telnetlib, pipes, …) are not imported anywhere in `hermclaw/`, `worker/`, `scripts/`, `tests/`. | [M] grep |

## Decision / recommendation for Hermclaw

Roles and architecture unchanged (PostgreSQL + pgvector as source of truth, LiteLLM gateway on `.225:4000`,
Ollama on `.224`, SearXNG as default search provider). Changes are versions, guards and deployment details.

1. **PostgreSQL 17 from PGDG, pgvector minimum raised to 0.8.7.**
   - Source: `https://apt.postgresql.org/pub/repos/apt`, suite `trixie-pgdg`, component `main`, arch amd64.
     Packages `postgresql-17 postgresql-client-17 postgresql-17-pgvector` (expected
     `17.11-1.pgdg13+2` / pgvector `0.8.7-1.pgdg13+N` or later – the latter unverified, P13; compare with
     `dpkg --compare-versions … ge 0.8.7`, never against a fixed `+N` revision).
   - **`postgresql_pgvector_min_version: "0.8.7"`** (CVE-2026-103484, P11) everywhere the repo encodes
     0.8.2 (role default + validate floor, `hermclaw/doctor.py MIN_PGVECTOR`, `hermclaw/persistence/db.py`
     flag, tests). Keep the HNSW-only, serial-build migrations (P15). Debian's own 0.8.0 stays blocked.
   - If PGDG still offers only 0.8.6 at deployment time: stop the play (the existing assert does that) and
     either wait for the PGDG build or, as a documented exception, build v0.8.7 from the upstream tag with
     `postgresql-server-dev-17` (as the official pgvector image does, P14). Do not lower the minimum.
   - Trust anchor for the repository: prefer the keyring shipped in Debian's `postgresql-common`
     (`/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg`, signed via the Debian archive) over a
     download at deploy time; in either case assert fingerprint B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8.
   - Disable automatic creation of 17/main before installing `postgresql-17`, so the role's
     `pg_createcluster --locale=C.UTF-8` actually runs (P7). Either `pg_conftool
     /etc/postgresql-common/createcluster.conf set create_main_cluster false` (absolute path accepted, checked in
     257 source) or – avoiding a ucf-managed file – a drop-in `/etc/postgresql-common/createcluster.d/hermclaw.conf`
     with `create_main_cluster = false` (the shipped `createcluster.conf` has `include_dir
     '/etc/postgresql-common/createcluster.d'` and `read_conf_file` merges it; checked in 257, not on 278).
2. **LiteLLM: keep `litellm[proxy]==1.104.2`** – no advisory affects it (L3); 1.104.3 / 1.105.1 bring no
   security fix (L2). Re-check the advisory list before every bump. Add to its venv:
   **`prisma==0.15.0`** (fixes 500-instead-of-401, L10) – note that this package is archived/unmaintained
   (L5); it is only imported for its exception classes here and no `prisma generate`/CLI is run. If that is not
   acceptable, the alternative is to omit it and have the gateway/doctor treat HTTP 500 on an unauthenticated
   probe as "denied but misconfigured". Install from a **hash-pinned lock file**
   (`pip install --require-hashes`) because of the 1.82.7/1.82.8 incident (L4); after install assert that no
   `litellm_init.pth` exists in `site-packages` (catches only the 1.82.8 variant; the hash lock is the primary
   control). Unit environment: `LITELLM_LOCAL_MODEL_COST_MAP=True`,
   `DISABLE_ADMIN_UI=True`; drop the no-op `LITELLM_TELEMETRY`. Keep the generated config (`ollama_chat/`,
   `num_retries: 0`, `disable_cooldowns`, `background_health_checks: false`, master key from env, no
   `database_url`). Probes: liveness `/health/liveliness`, readiness `/health/readiness` (expect
   `"db":"Not connected"` = normal in DB-less mode). The gateway must treat 401 and 400 `no_db_connection` as
   authentication failures.
3. **SearXNG: keep the pinned image but pin by digest** –
   `docker.io/searxng/searxng:2026.10.9-f4822b3fc@sha256:85492ba800a861e3e8ff6014c430e81369fe6ca766a9990595f616d1ce95b272`
   (tested here, S2/S6). Updating to `2026.10.13-22c64b8d3@sha256:9b61996e…` is optional (untested).
   Quadlet additions: `Environment=GRANIAN_HOST=0.0.0.0` (S4). Settings: keep `use_default_settings: true`,
   `search.formats: [html, json]`, `server.limiter: false`, `server.public_instance: false`,
   `server.image_proxy: false`, `general.enable_metrics: false`; **remove the `secret_key` line** so a
   missing podman secret makes SearXNG refuse to start (S8/S9). No Valkey, no `limiter.toml`. Health check:
   `GET http://127.0.0.1:8888/healthz` → `OK`; functional check `GET /search?q=debian&format=json` → 200 JSON.
4. **Python 3.13 (trixie system Python) for the orchestrator venv and the LiteLLM venv.** No dependency is
   incompatible (Y2/Y3). Make the OpenAPI export independent of the interpreter (Y4), add a Python 3.13 CI job,
   and pin + pre-fetch tree-sitter grammars (Y6).

## Implementation consequences

### INSTALLATION.md – orchestrator `.225` (Debian 13, as root)

```sh
apt-get update
apt-get install -y ca-certificates curl gnupg postgresql-common python3-venv jq podman
# PGDG repository – variant A (official helper; -y = no prompt; options inspected in postgresql-common 257,
# trixie ships 278 – not inspected; writes /etc/apt/sources.list.d/pgdg.sources and runs apt-get update)
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
# variant B (what the playbook does) – one deb822 file, keyring from postgresql-common
gpg --show-keys --with-colons /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg | awk -F: '$1=="fpr"{print $10; exit}'
#   -> must print B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8
cat >/etc/apt/sources.list.d/pgdg.sources <<'EOF'
Types: deb
URIs: https://apt.postgresql.org/pub/repos/apt
Suites: trixie-pgdg
Components: main
Architectures: amd64
Signed-By: /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg
EOF
rm -f /etc/apt/sources.list.d/pgdg.list          # never two entries for the same repository
apt-get update
apt-cache policy postgresql-17 postgresql-17-pgvector   # candidate must come from apt.postgresql.org, pgvector >= 0.8.7
pg_conftool /etc/postgresql-common/createcluster.conf set create_main_cluster false
#   (alternative: install -d /etc/postgresql-common/createcluster.d &&
#    printf 'create_main_cluster = false\n' >/etc/postgresql-common/createcluster.d/hermclaw.conf)
pg_conftool /etc/postgresql-common/createcluster.conf show -bs create_main_cluster   # must print: off
apt-get install -y --no-install-recommends postgresql-17 postgresql-client-17 postgresql-17-pgvector
#   (postgresql-common moves to the newer PGDG build now or at the next apt upgrade; createcluster.conf is
#    ucf-managed – keep the local version if asked; the createcluster.d drop-in avoids that question)
pg_createcluster --port=5432 --encoding=UTF8 --locale=C.UTF-8 --start 17 main
dpkg-query -W -f='${Version}\n' postgresql-17-pgvector     # >= 0.8.7
sudo -u postgres psql -d hermclaw -XtAc "SELECT extversion FROM pg_extension WHERE extname='vector'"
```

After any pgvector package upgrade: `ALTER EXTENSION vector UPDATE;` in every database (the role does this)
**and** `systemctl restart postgresql@17-main` (P16).

LiteLLM venv (path must match the unit, see repo inconsistency below):

```sh
python3 -m venv /opt/hermclaw/litellm-venv
/opt/hermclaw/litellm-venv/bin/pip install --require-hashes -r /opt/hermclaw/share/litellm-requirements.lock
#   lock generated from: litellm[proxy]==1.104.2  prisma==0.15.0  (pip-compile/uv --generate-hashes, Python 3.13)
find /opt/hermclaw/litellm-venv -name 'litellm_init.pth' | grep -q . && echo "COMPROMISED WHEEL" && exit 1   # 1.82.8 marker only
systemctl enable --now hermclaw-litellm.service
curl -fsS http://127.0.0.1:4000/health/liveliness           # "I'm alive!"
curl -fsS http://127.0.0.1:4000/health/readiness            # {"status":"healthy","db":"Not connected"}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/v1/models   # must be 401 (500 = prisma missing; 200 = no master key!)
```

SearXNG (rootless, user `hermclaw`, lingering):

```sh
loginctl enable-linger hermclaw
U=$(id -u hermclaw)
install -d -m 0755 /etc/containers/systemd/users/$U
# quadlet + settings rendered by the playbook; then, as the user manager:
systemctl --user --machine=hermclaw@.host daemon-reload
systemctl --user --machine=hermclaw@.host start hermclaw-searxng.service
curl -fsS http://127.0.0.1:8888/healthz                                   # OK
curl -fsS 'http://127.0.0.1:8888/search?q=debian&format=json' | jq '.results | length'
```

### Deployment playbook / roles
- `roles/postgresql/defaults/main.yml`: `postgresql_pgvector_min_version: "0.8.7"`; `tasks/validate.yml`
  floor `0.8.7`; messages mention CVE-2026-3172 **and** CVE-2026-103484.
- `roles/postgresql/tasks/install.yml`: install `postgresql-common` from Debian first; `signed_by:
  /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg` (or keep the URL and add a fingerprint assert);
  set `create_main_cluster` off (`pg_conftool … set create_main_cluster false`) **before** installing
  `postgresql-17`; on an existing cluster assert `datcollate`/`datctype` of `template1` = `C.UTF-8` instead of
  silently accepting the system locale.
- `roles/postgresql`: a handler/runbook step "restart the cluster when `postgresql-17-pgvector` changed".
- `roles/systemd_skeleton/defaults/main.yml`: image
  `docker.io/searxng/searxng:2026.10.9-f4822b3fc@sha256:85492ba800a861e3e8ff6014c430e81369fe6ca766a9990595f616d1ce95b272`;
  `systemd_skeleton_litellm_venv` aligned with RISK_REGISTER/upgrade runbook (`/opt/hermclaw/litellm-venv`
  vs. the current default `/opt/litellm/venv`).
- `infra/systemd/hermclaw-searxng.container.j2`: add `Environment=GRANIAN_HOST=0.0.0.0`; health is probed
  from the host (`/healthz`); a Quadlet `HealthCmd=` could use BusyBox `wget -q -O- http://127.0.0.1:8080/healthz`
  (the image links all BusyBox applets into `/usr/bin`, S3 – not executed) or the image's own Python
  (`/usr/local/searxng/.venv/bin/python`); test either before relying on it.
- `roles/systemd_skeleton/templates/searxng-settings.yml.j2`: delete the `secret_key` line.
- Deployment phase: explicitly start `hermclaw-searxng.service` in the hermclaw user manager after
  `daemon-reload` (`try-restart` in the handler does not start an inactive unit; S10).
- `infra/systemd/hermclaw-litellm.service.j2`: `Environment=LITELLM_LOCAL_MODEL_COST_MAP=True`,
  `Environment=DISABLE_ADMIN_UI=True`; remove `LITELLM_TELEMETRY`; keep `LoadCredentialEncrypted` (a missing
  credential fails the unit, which is required because LiteLLM itself would fall back to no-auth, L9).
- LiteLLM install task: lock file with hashes, `--require-hashes`, `litellm_init.pth` check, post-start
  check that unauthenticated `/v1/models` returns 401.
- Orchestrator venv: set `TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/opt/hermclaw/ts-pack` in `hermclaw.env`, and
  pre-fetch at deploy time (network allowed there), e.g.
  `TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/opt/hermclaw/ts-pack /opt/hermclaw/venv/bin/python -c "import tree_sitter_language_pack as t; t.download(['javascript','typescript','tsx','php'])"`
  (`download()` exists in the 1.21/1.22 `_native.pyi`; the README documents `prefetch()` and the CLI
  `ts-pack download` for the same purpose); the directory stays read-only for the services.

### Code / CI
- `pyproject.toml`: pin `tree-sitter-language-pack` to the tested version (e.g. `==1.21.0`; its cache path
  contains the version) or at least `>=1.21,<2`; the `litellm-proxy` extra gets `prisma==0.15.0`.
- `hermclaw/contracts/schema_export.py` (`openapi_document`): normalise response descriptions to the RFC 9110
  phrases (or set them explicitly) so the document is identical on 3.12 and 3.13; regenerate
  `docs/contracts/openapi.json`.
- `.gitlab-ci.yml`: service image `pgvector/pgvector:0.8.7-pg17-trixie` (was `0.8.2-pg17`); add a
  `python:3.13-slim-trixie` test job (production interpreter).
- `hermclaw/doctor.py` / `hermclaw/persistence/db.py`: minimum (0, 8, 7) with both CVE ids; doctor may also
  check LiteLLM's unauthenticated `/v1/models` = 401 and SearXNG `/healthz`.
- Research docs: write hex digests with a `sha256:` prefix (the repo secret scan skips hash context, Y5).

## Open risks
- **PGDG pgvector 0.8.7 for trixie is unverified** (P13); until it is available the playbook stops by design.
  A source build is a maintenance burden (rebuild on every PostgreSQL minor).
- CVE-2026-103484 exploitation needs SQL access as a role that can create an index on a vector column; any SQL
  injection or credential leak of the `hermclaw` role would turn into code execution in PostgreSQL until 0.8.7
  is installed and the server restarted.
- Whether Debian's security tracker marks CVE-2026-103484 fixed in sid/forky (0.8.7-1) is unclear (tracker
  unreachable, snippets contradictory, P8); trixie 0.8.0-1 is affected by version range either way. PGDG and
  Debian versions must be checked on the host (`apt-cache policy`), not inferred.
- `prisma` 0.15.0 (needed only for the 401 status code, L10) is an archived, unmaintained package with a Node
  tool-chain dependency (`nodeenv`); it will get no security fixes. Track LiteLLM upstream for a guarded import
  so it can be dropped.
- The `apt.postgresql.org.sh` option list and the keyring path were verified on postgresql-common 257 only;
  trixie's 278 and PGDG's 289+ (which adds `-a`) were not inspected – variant B (explicit deb822 file +
  fingerprint assert) does not depend on the helper and is the safer path for the playbook.
- LiteLLM releases very frequently (several per day across lines) and has about 40 advisories in the GitHub
  Advisory Database (47 hits for "litellm" in the pip ecosystem, a few of them other packages), incl. a
  malicious PyPI release; the pin plus hash lock protects only if every bump re-runs the
  advisory check and `tests/integration/test_models_litellm_proxy.py`.
- The 500-on-unauthenticated behaviour (L10) is an upstream bug still present in 1.105.1; if a future version
  changes the DB-less error paths, the status codes the gateway sees may change again.
- The master key falls back to "no auth" inside LiteLLM if the environment variable is empty (L9); the
  systemd credential is the only guard, so the post-start 401 check is mandatory.
- SearXNG has no stable release line; every pin is a dated snapshot. Upstream engine breakage (blocked by
  search providers) shows up as `unresponsive_engines`, not as an HTTP error – the research engine must
  treat empty results plus unresponsive engines as a degraded provider.
- Whether `.225` has IPv6 in the rootless network namespace was not checked; `GRANIAN_HOST=0.0.0.0` removes
  the dependency.
- tree-sitter grammar bundles are native code downloaded from GitHub; pre-fetching moves that download to
  deploy time but the integrity anchor is still the downloaded manifest.
- Integration tests (PostgreSQL, podman, git) were not executed under Python 3.13 in this research; they
  must run on `.225` or in the new 3.13 CI job.
- Debian/postgresql.org facts are from search snippets (pages unreachable here); exact versions on the host
  may differ (e.g. a newer `17.x-0+deb13uN` or `pgdg13+N`).
