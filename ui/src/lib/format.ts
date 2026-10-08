/** Formatting helpers (German locale). */

const dateTimeFmt = new Intl.DateTimeFormat("de-DE", { dateStyle: "short", timeStyle: "medium" });
const timeFmt = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const numberFmt = new Intl.NumberFormat("de-DE");

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : dateTimeFmt.format(d);
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : timeFmt.format(d);
}

export function formatNumber(n: number | null | undefined): string {
  return n === null || n === undefined ? "–" : numberFmt.format(n);
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "–";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0).replace(".", ",")} s`;
  const m = Math.floor(s / 60);
  const rs = Math.round(s % 60);
  if (m < 60) return `${m} min ${rs} s`;
  const h = Math.floor(m / 60);
  return `${h} h ${m % 60} min`;
}

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${i === 0 ? String(v) : v.toFixed(v < 10 ? 1 : 0).replace(".", ",")} ${units[i] ?? "B"}`;
}

/** "vor 5 s", "vor 3 min", … relative to `now` (ms). */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "–";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const diff = Math.round((now - t) / 1000);
  if (diff < 0) return `in ${formatDuration(-diff * 1000)}`;
  if (diff < 60) return `vor ${diff} s`;
  if (diff < 3600) return `vor ${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `vor ${Math.floor(diff / 3600)} h`;
  return `vor ${Math.floor(diff / 86400)} d`;
}

export function formatPercent(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  return `${(v * 100).toFixed(digits).replace(".", ",")} %`;
}

export function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return "–";
  return id.length > n ? id.slice(0, n) : id;
}

export function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export function boolLabel(v: boolean | null | undefined): string {
  if (v === null || v === undefined) return "–";
  return v ? "Ja" : "Nein";
}
