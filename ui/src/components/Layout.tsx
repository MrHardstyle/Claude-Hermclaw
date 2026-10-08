import { useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { Link, useRouter } from "../lib/router";
import { useApi } from "../lib/useApi";

const NAV: { to: string; label: string; match: (p: string) => boolean }[] = [
  { to: "/", label: "Dashboard", match: (p) => p === "/" },
  { to: "/jobs", label: "Jobs", match: (p) => p.startsWith("/jobs") },
  { to: "/workers", label: "Worker", match: (p) => p.startsWith("/workers") },
  { to: "/resources", label: "Ressourcen", match: (p) => p.startsWith("/resources") },
  { to: "/media", label: "Medien", match: (p) => p.startsWith("/media") },
  { to: "/bugs", label: "Bugs", match: (p) => p.startsWith("/bugs") },
];

export function Layout({ children, onLogout }: { children: ReactNode; onLogout: () => void }) {
  const { path } = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);
  const version = useApi("version", (s) => api.version(s));
  const mainRef = useRef<HTMLElement>(null);
  const [lastPath, setLastPath] = useState(path);
  if (lastPath !== path) {
    // close the mobile menu on navigation (state adjustment during render, no effect needed)
    setLastPath(path);
    setMenuOpen(false);
  }
  // page identity without the tab segment (/jobs/<id>/plan → /jobs/<id>) so tab switches keep focus
  const pageKey = path.split("/").slice(0, 3).join("/");
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    // move focus to the main region after client-side navigation (screen readers announce the new page)
    mainRef.current?.focus({ preventScroll: true });
  }, [pageKey]);

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Zum Inhalt springen
      </a>
      <header className="topbar">
        <Link to="/" className="brand">
          <span className="brand-mark" aria-hidden="true">
            H
          </span>
          Hermclaw Next
        </Link>
        <button
          type="button"
          className="btn menu-toggle"
          aria-expanded={menuOpen}
          aria-controls="main-nav"
          onClick={() => setMenuOpen((o) => !o)}
        >
          Menü
        </button>
        <div className="topbar-right">
          {version.data ? (
            <span className="muted version" title={`Instanz ${version.data.instance}`}>
              v{version.data.version} · {version.data.env}
            </span>
          ) : null}
          <button type="button" className="btn btn-small" onClick={onLogout} data-testid="logout">
            Abmelden
          </button>
        </div>
      </header>
      <div className="shell">
        <nav id="main-nav" className={`sidenav ${menuOpen ? "is-open" : ""}`} aria-label="Hauptnavigation">
          <ul>
            {NAV.map((n) => {
              const active = n.match(path);
              return (
                <li key={n.to}>
                  <Link to={n.to} className={active ? "is-active" : undefined} aria-current={active ? "page" : undefined}>
                    {n.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
        <main id="main" ref={mainRef} tabIndex={-1} className="main">
          {children}
        </main>
      </div>
    </div>
  );
}
