# Web UI operations

Architecture: `docs/architecture/ui.md`. Topology (`config/hosts.example.yaml`): Nginx on `webui-223`
(192.168.178.223:80) serves the static build and proxies `/api/` to the API on `orchestrator-225`
(192.168.178.225:8000).

## 1. Build

Requirements: Node ≥ 22.23.2 (last 22.x security release; Vite itself needs ≥ 22.12; the build host uses `/opt/node22/bin`), npm, and the lockfile in `ui/package-lock.json`.

```bash
cd ui
npm ci                      # exact versions from the lockfile
npm run typecheck           # tsc -b (strict)
npm run lint                # eslint, 0 warnings allowed
npm run test:unit           # vitest
npm run build               # tsc -b && vite build → ui/dist/
```

`ui/dist/` is fully self-contained: hashed JS/CSS under `assets/`, `index.html`, and `favicon.svg`. There are no CDN
or font requests at runtime. Optional build variables:

* `HERMCLAW_UI_BASE=/hermclaw/` for a sub-path deployment.
* `VITE_HERMCLAW_API_BASE` for a build-time API base.

Usually neither is needed.

## 2. Deploy on `.223`

The UI is deployed by `infra/ansible/playbooks/deploy.yml` (role `hermclaw_ui`, P32 32.2; operator guide:
`docs/operations/deployment.md`). Do not copy files by hand.

1. **Build once per release on the control node**: `scripts/deploy/build-release.sh` runs `npm ci && npm run build`
   on a clean copy of `ui/` and packs `ui/dist/` into `dist/release-<version>/hermclaw-ui-<version>.tar.gz` (sha256
   in the release's `SHA256SUMS`). The script refuses a build whose `index.html` contains an inline `<script>`,
   `<style>`, `style=` or `on…=` attribute, because the Content-Security-Policy below would block it. `.223` needs no
   npm registry access and builds nothing at deploy time. The deployed bundle is the one the E2E suite ran against, and
   a rollback reuses the exact old bundle. Node 22 on `.223` (role `nodejs`) stays available for manual rebuilds,
   e.g. with `HERMCLAW_UI_BASE`.
2. **Versioned directories, atomic switch**: every release is unpacked into
   `<hermclaw_webroot>-releases/<version>/` (default `/var/www/hermclaw-next-releases/0.1.0rc1/`, owner
   `root:www-data`). The marker file `<version>.release` sits next to that directory, outside the served tree.
   `hermclaw_webroot` (`/var/www/hermclaw-next`, `playbooks/group_vars/all.yml`) is a **symlink** to the active
   release. It is replaced with one `rename(2)`, so a request never sees a half-copied tree. The first deployment
   replaces the bootstrap's placeholder directory. A webroot that holds anything other than that placeholder is kept
   as `hermclaw-next.pre-release-<time>`. The active release and `hermclaw_keep_releases` (default 3) previous ones
   are kept. After a switch the playbook runs `nginx -t` and only then reloads nginx.
3. Optional: to point the UI at another API origin without a rebuild, edit
   `<meta name="hermclaw-api-base" content="">` in `index.html` of the active release (it is overwritten by the next
   deployment; prefer a rebuild with `VITE_HERMCLAW_API_BASE`). An empty value means same origin `/api`, which is
   the recommended setup.
4. **Nginx site**: one template, `infra/ansible/roles/nginx/templates/hermclaw-next.conf.j2`, rendered identically by
   the bootstrap and the deployment into `/etc/nginx/sites-available/hermclaw-next.conf`. It contains:
   * `root /var/www/hermclaw-next` (the symlink), SPA fallback `try_files $uri $uri/ /index.html`, `index.html` with
     `Cache-Control: no-cache`, `/assets/` with `public, max-age=31536000, immutable`, and a 404 for missing assets;
   * the SSE location `^/api/(jobs/[^/]+/)?events/stream$` (research R-014/R-028): HTTP/1.1 with upstream keepalive,
     no buffering, no cache, no gzip, `proxy_read_timeout 3600s`; `/api/` with `X-Forwarded-*` headers;
   * the security headers in every location that sets its own `add_header`. nginx 1.26 does not merge
     `add_header` levels. The headers are `Content-Security-Policy` (`nginx_csp`: `default-src 'none'; script-src
     'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self';
     manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'`, plus
     `upgrade-insecure-requests` with TLS), `X-Content-Type-Options`, `X-Frame-Options: DENY`,
     `Referrer-Policy: same-origin`, `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy` and
     `Permissions-Policy`;
   * `GET /healthz` returns `200 ok` for the readiness probe in `hosts.yaml` (`webui-223`, `http_path: /healthz`);
   * an access log **without query strings** (`log_format hermclaw_noquery`), because the SSE URLs carry
     `?access_token=`;
   * optional TLS (`nginx_tls_enabled: true`): port 443 with `http2 on` (lifts the six-connections limit for the
     EventSources), TLS 1.2/1.3. Port 80 then only serves `/healthz` and a 301 to HTTPS. The certificate is either
     generated on `.223` (`nginx_tls_source: self-signed`: ECDSA P-256, SAN = the server names and the host address,
     397 days, renewed 30 days before expiry) or yours (`nginx_tls_source: provided`, `nginx_tls_cert_src` /
     `nginx_tls_key_src` on the control node, e.g. from the LAN CA of research 20261014-028).
     `Strict-Transport-Security` is sent only with `nginx_hsts_max_age` > 0, and only once every client trusts the
     certificate.

   Set `hermclaw_web_server_name` in the inventory (default `hermclaw.lan 192.168.178.223`). The site is not the
   default server, so a request to a name that is not listed reaches Debian's default site instead.

Notes:

* **Heartbeats.** The API sends SSE heartbeat comments every `policies.events.heartbeat_seconds`. Keep
  `proxy_read_timeout` well above that interval.
* **Proxy buffering.** The API also sets `X-Accel-Buffering: no` on SSE responses, so a missing `proxy_buffering off`
  degrades gracefully.
* **Uvicorn access log.** If uvicorn runs with an access log, start it with `--no-access-log` or mask query strings.
  `?access_token=` is the only place the token appears in a URL. All other requests send it in the
  `Authorization` header.
* **Token storage.** The token lives in the browser's `sessionStorage` only, and it is gone when the tab closes.
  Give operators read or control tokens per person. The worker detail view and drain need the `admin` scope; without
  it, the UI shows a hint instead.

## 3. Smoke test on the real hosts (blocked here by BLOCKER-001)

From a LAN machine:

The deployment runs the first three checks itself (role `hermclaw_ui`, from the control node). With TLS, use
`https://` and `-k` (or `--cacert`) for a self-signed certificate.

```bash
curl -fsS http://192.168.178.223/healthz                     # ok (also with TLS: port 80 keeps answering it)
curl -fsS http://192.168.178.223/ | grep -q '<div id="root">'
curl -fsSI http://192.168.178.223/ | grep -i content-security-policy
curl -fsS http://192.168.178.223/api/health
curl -fsS -H "Authorization: Bearer $TOKEN" http://192.168.178.223/api/stats
# SSE through Nginx: events must arrive immediately (not in blocks), heartbeats as ": heartbeat"
curl -N -H "Accept: text/event-stream" "http://192.168.178.223/api/events/stream?access_token=$TOKEN"
```

In the browser:

1. Open `http://192.168.178.223/` and log in with the token.
2. The dashboard shows "Live verbunden" next to the live status.
3. Create a small job and watch the status lines ("Gemma erstellt Plan", "Coder bearbeitet …") appear in its Live
   Events tab.
4. Reload Nginx (`systemctl reload nginx`). The badge briefly shows "Verbindung unterbrochen – verbinde neu" and
   then returns to "Live verbunden", with no events missing.

The whole E2E suite can also run against the deployed stack. Point `use.baseURL` at `.223` and seed through the API.
The seed script needs direct DB access, so it is meant for a disposable test database only. Do not run it against
production.

## 4. Local development

```bash
# API (repository root): test env, literal token, local PostgreSQL
HERMCLAW_ENV=test HERMCLAW_API_TOKEN_REF=literal:dev-token \
HERMCLAW_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/hermclaw_dev \
  .venv/bin/python -m uvicorn hermclaw.api.app:create_app --factory --port 8000 --no-access-log
# UI with hot reload, /api proxied to :8000
cd ui && HERMCLAW_UI_API_TARGET=http://127.0.0.1:8000 npm run dev    # http://127.0.0.1:5173
```

## 5. E2E tests (Playwright, real backend)

```bash
cd ui
PATH=/opt/node22/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm test
```

`tests/e2e/global-setup.ts` runs these steps:

1. `seed.py prepare` creates a fresh database `hermclaw_ui_e2e_<hex>` on `postgresql://postgres@127.0.0.1:5432`
   (override with `HERMCLAW_TEST_ADMIN_URL`) and runs `alembic upgrade head`.
2. Builds the UI (`HERMCLAW_UI_SKIP_BUILD=1` skips this).
3. Starts uvicorn with `HERMCLAW_ENV=test` and a random literal token.
4. Seeds the data.
5. Starts `vite preview` behind two chaos proxies.

Teardown stops everything and drops the database (`HERMCLAW_E2E_KEEP_DB=1` keeps it). Logs go to
`ui/tests/.runtime/{api,ui}.log`, which is git-ignored, as is `state.json` with the throw-away token.

Ports, defaults first:

* API: 18765
* UI: 14173
* vite: 14174
* chaos proxies: 18766–18768

If a port is taken, setup fails fast and names the variable to change. Do not run `playwright install`; the
browsers are pre-installed. If the `@playwright/test` version does not match the installed build, set
`HERMCLAW_E2E_CHROMIUM=/opt/pw-browsers/chromium-<rev>/chrome-linux/chrome`.

## 6. Troubleshooting

| Symptom | Cause / action |
|---|---|
| Login says "Nicht angemeldet oder Token ungültig" | Wrong token, or the API uses another `HERMCLAW_API_TOKEN_REF`. Check `GET /api/stats` with curl |
| Badge stays "Verbindung unterbrochen" | `/api/.../events/stream` is blocked or 404 at the proxy. Check the Nginx SSE location and the `access_token` query parameter |
| Events arrive in bursts | Proxy buffering. Check `proxy_buffering off` and `gzip off` in the SSE location |
| Stream "Live verbunden" but silent | Half-open upstream. The job view detects it within one reconciliation round (≤ 15 s + 5 s) and reconnects. Check proxy timeouts |
| Deep link `/jobs/<id>` gives 404 on reload | Missing `try_files $uri $uri/ /index.html` (the site is generated by the nginx role; re-run `playbooks/deploy.yml --limit webui`) |
| Blank page, console reports a Content-Security-Policy violation | An inline script or style crept into the build. `build-release.sh` refuses such an `index.html`; fix the source and rebuild the release |
| `http://192.168.178.223/` shows the Debian default page | The address is not in `hermclaw_web_server_name` |
| Worker detail shows "benötigt ein Admin-Token" | Token without the `admin` scope (expected for read tokens) |
| Media preview shows an error | Artifact file missing under `HERMCLAW_DATA_DIR/artifacts` on `.225`, or no read access |
