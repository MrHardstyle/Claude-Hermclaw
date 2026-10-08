/**
 * Which job controls are available for a job – mirrors the server rules in hermclaw/api/jobs.py::_control
 * and hermclaw/runtime/state_machines.py (retry = transition to `queued`, only from `failed`/`blocked`).
 */
import type { ControlAction, Job } from "../api/types";

export const TERMINAL_JOB_STATUSES: ReadonlySet<string> = new Set(["succeeded", "failed", "cancelled"]);
const RETRYABLE: ReadonlySet<string> = new Set(["failed", "blocked"]);

export interface ControlState {
  action: ControlAction;
  enabled: boolean;
  /** why the button is disabled (German, shown as tooltip/description) */
  reason: string;
  /** destructive actions get a stronger confirmation */
  destructive: boolean;
  /** whether the dialog asks for an optional reason */
  askReason: boolean;
}

export const CONTROL_ORDER: readonly ControlAction[] = ["pause", "resume", "retry", "replan", "cancel"];

export function controlStates(job: Pick<Job, "status" | "pause_requested" | "cancel_requested">): Record<ControlAction, ControlState> {
  const terminal = TERMINAL_JOB_STATUSES.has(job.status);
  const make = (action: ControlAction, enabled: boolean, reason: string, destructive = false, askReason = false): ControlState => ({
    action,
    enabled,
    reason: enabled ? "" : reason,
    destructive,
    askReason,
  });
  return {
    cancel: make(
      "cancel",
      !terminal && !job.cancel_requested,
      terminal ? "Job ist bereits beendet" : "Abbruch wurde bereits angefordert",
      true,
      true,
    ),
    pause: make(
      "pause",
      !terminal && !job.pause_requested,
      terminal ? "Job ist bereits beendet" : "Job ist bereits pausiert",
    ),
    resume: make(
      "resume",
      !terminal && (job.pause_requested || job.status === "waiting_for_user"),
      terminal ? "Job ist bereits beendet" : "Job ist weder pausiert noch wartend",
    ),
    retry: make("retry", RETRYABLE.has(job.status), "Nur fehlgeschlagene oder blockierte Jobs können erneut versucht werden"),
    replan: make("replan", !terminal, "Job ist bereits beendet", false, true),
  };
}

export const CONTROL_CONFIRM_TEXT: Record<ControlAction, string> = {
  cancel: "Den Job wirklich abbrechen? Laufende Schritte werden am nächsten Checkpoint beendet.",
  pause: "Den Job pausieren? Laufende Schritte laufen bis zum nächsten Checkpoint, neue Schritte werden nicht gestartet.",
  resume: "Den Job fortsetzen?",
  retry: "Den Job erneut in die Warteschlange stellen? Fehlgeschlagene Schritte werden wiederholt.",
  replan: "Eine Neuplanung durch den Planer (Gemma) anfordern?",
};
