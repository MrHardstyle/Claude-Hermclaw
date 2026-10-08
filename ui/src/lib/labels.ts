/** German UI wording (single source so every page uses the same terms). */

export const JOB_STATUS_LABELS: Record<string, string> = {
  queued: "In Warteschlange",
  inventory: "Inventarisierung",
  discovering: "Analyse",
  researching: "Research",
  planning: "Planung",
  waiting_for_resources: "Wartet auf Ressourcen",
  waiting_for_worker: "Wartet auf Worker",
  waking_worker: "Worker wird geweckt",
  running: "Läuft",
  testing: "Tests laufen",
  verifying: "Verifikation",
  reviewing: "Review",
  correcting: "Korrektur",
  replanning: "Neuplanung",
  waiting_for_user: "Wartet auf Benutzer",
  blocked: "Blockiert",
  committing: "Commit",
  deploying: "Deployment",
  succeeded: "Erfolgreich",
  failed: "Fehlgeschlagen",
  cancelled: "Abgebrochen",
};

export const STEP_STATUS_LABELS: Record<string, string> = {
  pending: "Ausstehend",
  ready: "Bereit",
  leased: "Zugeteilt",
  running: "Läuft",
  checkpointed: "Checkpoint",
  testing: "Tests",
  verifying: "Verifikation",
  reviewing: "Review",
  completed: "Abgeschlossen",
  failed: "Fehlgeschlagen",
  blocked: "Blockiert",
  cancelled: "Abgebrochen",
};

export const WORKER_STATE_LABELS: Record<string, string> = {
  STARTING: "STARTING",
  WAKING: "WAKING",
  READY: "READY",
  BUSY: "BUSY",
  ERROR: "ERROR",
  SLEEPING: "SLEEPING",
  OFFLINE: "OFFLINE",
  DRAINING: "DRAINING",
};

export const WORKER_STATE_HINTS: Record<string, string> = {
  STARTING: "Worker startet",
  WAKING: "Wake-on-LAN gesendet, Worker fährt hoch",
  READY: "Bereit für Aufgaben",
  BUSY: "Bearbeitet gerade eine Aufgabe",
  ERROR: "Fehlerzustand",
  SLEEPING: "Ruhezustand (per Wake-on-LAN weckbar)",
  OFFLINE: "Nicht erreichbar",
  DRAINING: "Nimmt keine neuen Aufgaben an",
};

export const SEVERITY_LABELS: Record<string, string> = {
  debug: "Debug",
  info: "Info",
  warning: "Warnung",
  error: "Fehler",
  critical: "Kritisch",
  minor: "Minor",
  major: "Major",
  blocker: "Blocker",
};

export const CONTROL_LABELS: Record<string, string> = {
  cancel: "Abbrechen",
  pause: "Pausieren",
  resume: "Fortsetzen",
  retry: "Erneut versuchen",
  replan: "Neu planen",
};

export const KIND_LABELS: Record<string, string> = {
  inventory: "Inventar",
  discover: "Analyse",
  research: "Research",
  plan: "Plan",
  implement: "Implementierung",
  test: "Test",
  verify: "Verifikation",
  review: "Review",
  replan: "Neuplanung",
  ssh: "SSH",
  database: "Datenbank",
  docker: "Docker",
  deploy: "Deployment",
  documentation: "Dokumentation",
  image: "Bild",
  video: "Video",
};

export function jobStatusLabel(s: string): string {
  return JOB_STATUS_LABELS[s] ?? s;
}

export function stepStatusLabel(s: string): string {
  return STEP_STATUS_LABELS[s] ?? s;
}

export function severityLabel(s: string): string {
  return SEVERITY_LABELS[s] ?? s;
}

/** Visual tone for a status / state (maps to CSS classes `tone-*`). */
export type Tone = "neutral" | "info" | "active" | "success" | "warning" | "danger" | "muted";

export function jobTone(status: string): Tone {
  switch (status) {
    case "succeeded":
      return "success";
    case "failed":
      return "danger";
    case "cancelled":
      return "muted";
    case "blocked":
    case "waiting_for_user":
      return "warning";
    case "queued":
      return "neutral";
    case "waiting_for_resources":
    case "waiting_for_worker":
    case "waking_worker":
      return "info";
    default:
      return "active";
  }
}

export function stepTone(status: string): Tone {
  switch (status) {
    case "completed":
      return "success";
    case "failed":
      return "danger";
    case "blocked":
      return "warning";
    case "cancelled":
      return "muted";
    case "pending":
      return "neutral";
    case "ready":
    case "leased":
      return "info";
    default:
      return "active";
  }
}

export function workerTone(display: string): Tone {
  switch (display.toUpperCase()) {
    case "READY":
      return "success";
    case "BUSY":
      return "active";
    case "STARTING":
    case "WAKING":
      return "info";
    case "ERROR":
      return "danger";
    case "DRAINING":
      return "warning";
    case "SLEEPING":
    case "OFFLINE":
      return "muted";
    default:
      return "neutral";
  }
}

export function severityTone(sev: string): Tone {
  switch (sev) {
    case "critical":
    case "error":
    case "blocker":
      return "danger";
    case "warning":
    case "major":
      return "warning";
    case "debug":
      return "muted";
    case "minor":
      return "info";
    default:
      return "neutral";
  }
}

export function resultTone(status: string): Tone {
  const s = status.toLowerCase();
  if (["passed", "ok", "succeeded", "completed", "read", "approve", "approved", "pass", "success"].includes(s)) return "success";
  if (["failed", "error", "refused", "reject", "rejected", "fail"].includes(s)) return "danger";
  if (["skipped", "warning", "changes_requested", "request_changes", "needs_changes"].includes(s)) return "warning";
  if (["running", "started"].includes(s)) return "active";
  return "neutral";
}
