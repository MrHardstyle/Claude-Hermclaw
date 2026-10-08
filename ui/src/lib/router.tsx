import { createContext, useCallback, useContext, useMemo, useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent, type ReactNode } from "react";
import { stripBase, withBase } from "./routes";

/** Minimal History-API router (no dependency); the deployment base comes from Vite's BASE_URL. */
const BASE = import.meta.env.BASE_URL;
const NAV_EVENT = "hermclaw:navigate";

function subscribe(cb: () => void): () => void {
  window.addEventListener("popstate", cb);
  window.addEventListener(NAV_EVENT, cb);
  return () => {
    window.removeEventListener("popstate", cb);
    window.removeEventListener(NAV_EVENT, cb);
  };
}

function snapshot(): string {
  return `${window.location.pathname}${window.location.search}`;
}

export function navigate(to: string, opts: { replace?: boolean } = {}): void {
  const url = withBase(to, BASE);
  if (opts.replace) window.history.replaceState(null, "", url);
  else window.history.pushState(null, "", url);
  window.dispatchEvent(new Event(NAV_EVENT));
}

interface RouterValue {
  path: string;
  search: URLSearchParams;
  navigate: typeof navigate;
}

const RouterContext = createContext<RouterValue | null>(null);

export function RouterProvider({ children }: { children: ReactNode }) {
  const loc = useSyncExternalStore(subscribe, snapshot, () => "/");
  const value = useMemo<RouterValue>(() => {
    const [p, q] = loc.split("?");
    return { path: stripBase(p ?? "/", BASE), search: new URLSearchParams(q ?? ""), navigate };
  }, [loc]);
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

export function useRouter(): RouterValue {
  const v = useContext(RouterContext);
  if (!v) throw new Error("useRouter outside RouterProvider");
  return v;
}

type LinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { to: string; replace?: boolean };

export function Link({ to, replace, onClick, children, ...rest }: LinkProps) {
  const handle = useCallback(
    (e: MouseEvent<HTMLAnchorElement>) => {
      onClick?.(e);
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      if (rest.target && rest.target !== "_self") return;
      e.preventDefault();
      navigate(to, { replace });
    },
    [onClick, to, replace, rest.target],
  );
  return (
    <a href={withBase(to, BASE)} onClick={handle} {...rest}>
      {children}
    </a>
  );
}
