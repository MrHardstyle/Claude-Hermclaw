# Dependency audit (P36.8)

| Date (UTC) | Scope | Tool | Packages | Result |
|---|---|---|---|---|
| 2026-10-13 | runtime venv (`.venv`, Python 3.12, all extras incl. dev) | pip-audit (PyPI advisory DB / OSV) | 98 | **No known vulnerabilities** |
| 2026-10-13 | LiteLLM venv (`litellm[proxy]==1.104.2`, separate venv per DECISIONS) | pip-audit | 115 | **No known vulnerabilities** |
| pending UI merge | `ui/` npm dependencies (production) | `npm audit --omit=dev` | – | run by `scripts/dev/dependency-audit.sh` once the UI lockfile is final |

## Re-run

```bash
scripts/dev/dependency-audit.sh .venv /opt/hermclaw/litellm-venv   # reports in ./audit-reports/
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
