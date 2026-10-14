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

1. Copy `ui/dist/` to `/srv/hermclaw/ui/` on `.223`. Do this atomically: rsync to `ui.new/`, then `mv`.
2. Optional: to point the UI at another API origin without a rebuild, edit
   `<meta name="hermclaw-api-base" content="">` in `index.html`. An empty value means same origin `/api`, which is
   the recommended setup.
3. Nginx site (template). The SSE blocks follow research R-014: no buffering, no gzip, and a long read timeout.

```nginx
upstream hermclaw_api { server 192.168.178.225:8000; keepalive 16; }

server {
    listen 80;
    server_name hermclaw.lan 192.168.178.223;
    root /srv/hermclaw/ui;

    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;

    # SPA: client-side routes (/jobs/<id>/plan …) fall back to index.html.
    # Caching via `expires`, not add_header: an add_header inside a location would drop the security headers above.
    location / {
        try_files $uri /index.html;
        expires -1;
    }
    location /assets/ {
        expires max;   # file names are content-hashed
    }

    # SSE streams: unbuffered, long-lived; the URL carries ?access_token= → never write it to the access log
    location ~ ^/api/(jobs/[^/]+/)?events/stream$ {
        proxy_pass http://hermclaw_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        proxy_read_timeout 1h;
        access_log off;
    }

    location /api/ {
        proxy_pass http://hermclaw_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 2m;
    }
}
```

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

```bash
curl -fsS http://192.168.178.223/ | grep -q '<div id="root">'
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
| Deep link `/jobs/<id>` gives 404 on reload | Missing `try_files $uri /index.html` |
| Worker detail shows "benötigt ein Admin-Token" | Token without the `admin` scope (expected for read tokens) |
| Media preview shows an error | Artifact file missing under `HERMCLAW_DATA_DIR/artifacts` on `.225`, or no read access |
