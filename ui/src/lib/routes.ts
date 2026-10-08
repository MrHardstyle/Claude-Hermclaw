/** Pure route matching (kept separate from the React router component for unit tests). */

export type RouteParams = Record<string, string>;

/** Match `/jobs/:id/:tab?` style patterns. Returns params or null. */
export function matchRoute(pattern: string, path: string): RouteParams | null {
  const pp = pattern.split("/").filter(Boolean);
  const sp = path.split("?")[0]?.split("#")[0]?.split("/").filter(Boolean) ?? [];
  const params: RouteParams = {};
  let i = 0;
  for (; i < pp.length; i++) {
    const seg = pp[i] ?? "";
    const val = sp[i];
    if (seg.startsWith(":")) {
      const optional = seg.endsWith("?");
      const name = seg.slice(1, optional ? -1 : undefined);
      if (val === undefined) {
        if (optional) continue;
        return null;
      }
      try {
        params[name] = decodeURIComponent(val);
      } catch {
        return null;
      }
    } else if (seg !== val) {
      return null;
    }
  }
  if (sp.length > pp.length) return null;
  return params;
}

/** Strip the deployment base (`import.meta.env.BASE_URL`) from a pathname. */
export function stripBase(pathname: string, base: string): string {
  const b = base.endsWith("/") ? base.slice(0, -1) : base;
  if (b && pathname.startsWith(b)) {
    const rest = pathname.slice(b.length);
    return rest.startsWith("/") ? rest : `/${rest}`;
  }
  return pathname || "/";
}

export function withBase(path: string, base: string): string {
  const b = base.endsWith("/") ? base.slice(0, -1) : base;
  return `${b}${path.startsWith("/") ? path : `/${path}`}`;
}
