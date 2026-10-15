# Dependency audit (P36.8)

| Date (UTC) | Scope | Tool | Packages | Result |
|---|---|---|---|---|
| 2026-10-13 | runtime venv (`.venv`, Python 3.12, all extras incl. dev) | pip-audit (PyPI advisory DB / OSV) | 98 | **No known vulnerabilities** |
| 2026-10-13 | LiteLLM venv (`litellm[proxy]==1.104.2`, separate venv per DECISIONS) | pip-audit | 115 | **No known vulnerabilities** |
| 2026-10-15 (RC) | runtime venv (`.venv`, all extras incl. dev) | pip-audit | 107 | **No known vulnerabilities** |
| 2026-10-15 (RC) | LiteLLM venv (`litellm[proxy]==1.104.2` + `prisma==0.15.0`) | pip-audit | 116 | **No known vulnerabilities** |
| 2026-10-15 (RC) | `ui/` npm dependencies (production, `package-lock.json`) | `npm audit --omit=dev` | – | **0 vulnerabilities** (info/low/moderate/high/critical: 0) |

## Re-run

```bash
scripts/dev/dependency-audit.sh .venv /opt/litellm/venv   # reports in ./audit-reports/
```

The script installs pip-audit into a throw-away venv (never into the runtime venv), freezes each venv (pip or uv
venvs) and audits the exact installed versions with `--no-deps`. A non-zero exit code means at least one advisory;
the JSON reports name package, version, advisory id and fixed versions. Run it before every release candidate
(P41) and after dependency updates.

## Policy

* Pins: runtime dependencies are pinned with lower bounds in `pyproject.toml`; LiteLLM is pinned exactly
  (`1.104.2`) because the gateway relies on verified pass-through behaviour (docs/research LiteLLM notes).
* A finding with a fixed version is fixed before the RC; a finding without a fix is recorded in
  `RISK_REGISTER.md` with the mitigation (network exposure of the affected service, compensating control).
