import type { HermEvent, Json, JsonObject } from "../api/types";

/**
 * Canonical event types (hermclaw/contracts/events.py) plus types emitted outside the enum.
 * The SSE endpoints send `event: <event_type>`; EventSource has no wildcard listener, so the stream client
 * subscribes to every known name and the job view additionally reconciles via REST (see sse.ts / useJobEvents).
 */
export const KNOWN_EVENT_TYPES: readonly string[] = [
  "job.created",
  "job.transition",
  "job.succeeded",
  "job.failed",
  "job.cancelled",
  "job.control.cancel",
  "job.control.pause",
  "job.control.resume",
  "job.control.retry",
  "job.control.replan",
  "step.created",
  "step.transition",
  "attempt.started",
  "attempt.finished",
  "repository.registered",
  "repo.inventory.started",
  "repo.inventory.finished",
  "repo.search.executed",
  "repo.index.updated",
  "research.started",
  "research.query.started",
  "research.source.read",
  "research.claim.created",
  "research.finished",
  "planner.invoked",
  "planner.repair",
  "planner.fallback.used",
  "planner.plan.created",
  "planner.failed",
  "replan.started",
  "replan.created",
  "scope.created",
  "scope.expansion.requested",
  "scope.expanded",
  "scope.unavailable",
  "scope.violation",
  "resource.requested",
  "resource.acquired",
  "resource.released",
  "resource.preempt.requested",
  "resource.expired",
  "worker.registered",
  "worker.state",
  "worker.offline",
  "worker.wake.sent",
  "worker.wake.stage",
  "worker.ready",
  "worker.wake.failed",
  "worker.assigned",
  "model.load.started",
  "model.load.finished",
  "model.unloaded",
  "model.invocation.started",
  "model.invocation.finished",
  "tool.call.started",
  "tool.call.finished",
  "command.run",
  "file.changed",
  "test.started",
  "test.passed",
  "test.failed",
  "verifier.started",
  "verifier.check.failed",
  "verifier.finished",
  "review.started",
  "review.finding.created",
  "review.finished",
  "correction.started",
  "stagnation.detected",
  "strategy.changed",
  "checkpoint.created",
  "git.operation",
  "git.commit.created",
  "git.pushed",
  "git.merge_request.created",
  "deployment.started",
  "deployment.finished",
  "media.started",
  "media.finished",
  "ssh.command",
  "db.tool",
  "error",
  "status",
  "message",
];

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Accepts REST `EventOut` (ts) and SSE `EventEnvelope` (timestamp); returns null for malformed data. */
export function normalizeEvent(raw: unknown): HermEvent | null {
  if (!isObject(raw)) return null;
  const sequence = num(raw.sequence);
  const eventType = str(raw.event_type);
  if (sequence === null || eventType === null) return null;
  return {
    sequence,
    event_id: str(raw.event_id) ?? `seq-${sequence}`,
    ts: str(raw.ts) ?? str(raw.timestamp) ?? new Date(0).toISOString(),
    job_id: str(raw.job_id),
    step_id: str(raw.step_id),
    attempt_id: str(raw.attempt_id),
    source_type: str(raw.source_type) ?? "unknown",
    source_id: str(raw.source_id),
    event_type: eventType,
    severity: str(raw.severity) ?? "info",
    payload: isObject(raw.payload) ? (raw.payload as JsonObject) : {},
    correlation_id: str(raw.correlation_id),
    duration_ms: num(raw.duration_ms),
  };
}

/** Merge event batches: dedupe by sequence, keep ascending order, keep at most `max` newest. */
export function mergeEvents(existing: readonly HermEvent[], incoming: readonly HermEvent[], max = 20000): HermEvent[] {
  if (incoming.length === 0) return existing as HermEvent[];
  const last = existing.length ? (existing[existing.length - 1]?.sequence ?? 0) : 0;
  // fast path: strictly newer, ordered batch
  let ordered = true;
  let prev = last;
  for (const ev of incoming) {
    if (ev.sequence <= prev) {
      ordered = false;
      break;
    }
    prev = ev.sequence;
  }
  let merged: HermEvent[];
  if (ordered) {
    merged = existing.concat(incoming);
  } else {
    const map = new Map<number, HermEvent>();
    for (const ev of existing) map.set(ev.sequence, ev);
    for (const ev of incoming) map.set(ev.sequence, ev);
    merged = [...map.values()].sort((a, b) => a.sequence - b.sequence);
  }
  return merged.length > max ? merged.slice(merged.length - max) : merged;
}

export function maxSequence(events: readonly HermEvent[]): number {
  return events.reduce((m, e) => (e.sequence > m ? e.sequence : m), 0);
}

export function statusText(ev: HermEvent): string | null {
  if (ev.event_type !== "status") return null;
  const t = ev.payload.text;
  return typeof t === "string" && t.trim() ? t : null;
}

export function latestStatusLine(events: readonly HermEvent[]): HermEvent | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev && statusText(ev)) return ev;
  }
  return null;
}

export function eventCategory(type: string): string {
  if (type === "status") return "status";
  if (type === "error") return "error";
  const head = type.split(".")[0] ?? type;
  return head;
}

export const CATEGORY_LABELS: Record<string, string> = {
  status: "Status",
  job: "Job",
  step: "Schritt",
  attempt: "Versuch",
  repo: "Repository",
  repository: "Repository",
  research: "Research",
  planner: "Planer",
  replan: "Neuplanung",
  scope: "Scope",
  resource: "Ressourcen",
  worker: "Worker",
  model: "Modell",
  tool: "Tool",
  command: "Befehl",
  file: "Datei",
  test: "Test",
  verifier: "Verifier",
  review: "Review",
  correction: "Korrektur",
  stagnation: "Stagnation",
  strategy: "Strategie",
  checkpoint: "Checkpoint",
  git: "Git",
  deployment: "Deployment",
  media: "Medien",
  ssh: "SSH",
  db: "Datenbank",
  error: "Fehler",
};

export function isErrorEvent(ev: HermEvent): boolean {
  return (
    ev.severity === "error" ||
    ev.severity === "critical" ||
    ev.severity === "warning" ||
    ev.event_type === "error" ||
    ev.event_type.endsWith(".failed") ||
    ev.event_type === "scope.violation" ||
    ev.event_type === "stagnation.detected"
  );
}

function fmtJson(v: Json | undefined): string {
  if (v === undefined || v === null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

/** One-line human readable summary of an event payload (never contains model reasoning – there is none). */
export function summarizeEvent(ev: HermEvent): string {
  const p = ev.payload;
  const st = statusText(ev);
  if (st) return st;
  if (ev.event_type === "job.transition" || ev.event_type === "step.transition") {
    const key = typeof p.step_key === "string" ? `${p.step_key}: ` : "";
    const reason = typeof p.reason === "string" && p.reason ? ` – ${p.reason}` : "";
    return `${key}${fmtJson(p.from)} → ${fmtJson(p.to)}${reason}`;
  }
  for (const k of ["message", "summary", "title", "text", "error", "reason", "operation", "command", "path", "tool", "query", "url"]) {
    const v = p[k];
    if (typeof v === "string" && v.trim()) return v;
  }
  const keys = Object.keys(p);
  if (keys.length === 0) return "";
  return keys
    .slice(0, 4)
    .map((k) => `${k}=${fmtJson(p[k]).slice(0, 60)}`)
    .join(", ");
}
