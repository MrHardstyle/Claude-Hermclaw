# Hermclaw Web UI

React 19 + TypeScript (strict) + Vite single-page app for Hermclaw Next (P31). It talks only to the
Hermclaw FastAPI backend (`/api`, Bearer token, SSE) and has no runtime dependency on external CDNs.

```bash
export PATH=/opt/node22/bin:$PATH
npm ci
npm run dev        # dev server on :5173, /api proxied to HERMCLAW_UI_API_TARGET (default http://127.0.0.1:8000)
npm run build      # type check + production build into dist/
npm run preview    # serve dist/ with the same /api proxy
npm run lint       # ESLint (incl. jsx-a11y), zero warnings
npm run typecheck  # tsc -b
npm run test:unit  # vitest: diff parser, DAG layout, SSE reconnect, helpers
npm test           # Playwright E2E against the real backend + a throw-away PostgreSQL database
```

- Architecture, pages, data flow: [`docs/architecture/ui.md`](../docs/architecture/ui.md)
- Build, deployment behind Nginx on `.223`, E2E harness, troubleshooting: [`docs/operations/ui.md`](../docs/operations/ui.md)
