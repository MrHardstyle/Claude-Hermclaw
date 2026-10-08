import { useState } from "react";
import { api, describeError } from "../api/client";
import type { ControlAction, Job } from "../api/types";
import { CONTROL_CONFIRM_TEXT, CONTROL_ORDER, controlStates } from "../lib/controls";
import { CONTROL_LABELS } from "../lib/labels";
import { ConfirmDialog } from "./ConfirmDialog";

export function JobControls({ job, onDone }: { job: Job; onDone: () => void }) {
  const states = controlStates(job);
  const [pending, setPending] = useState<ControlAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const run = async (action: ControlAction, reason: string) => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.control(job.id, action, reason);
      setMessage(`${CONTROL_LABELS[action] ?? action}: ${res.detail || "angenommen"}`);
      setPending(null);
      onDone();
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  };

  const current = pending ? states[pending] : null;
  return (
    <div className="controls" role="group" aria-label="Job-Steuerung">
      {CONTROL_ORDER.map((action) => {
        const st = states[action];
        const descId = `control-${action}-reason`;
        return (
          <span key={action} className="control">
            <button
              type="button"
              className={`btn ${st.destructive ? "btn-danger-outline" : ""}`}
              disabled={!st.enabled}
              aria-describedby={st.enabled ? undefined : descId}
              title={st.enabled ? undefined : st.reason}
              onClick={() => {
                setError(null);
                setMessage(null);
                setPending(action);
              }}
              data-testid={`control-${action}`}
            >
              {CONTROL_LABELS[action]}
            </button>
            {!st.enabled ? (
              <span id={descId} className="sr-only">
                {st.reason}
              </span>
            ) : null}
          </span>
        );
      })}
      {job.pause_requested ? <span className="badge tone-warning">Pause angefordert</span> : null}
      {job.cancel_requested ? <span className="badge tone-danger">Abbruch angefordert</span> : null}
      <p className="control-message" role="status" aria-live="polite" data-testid="control-message">
        {message}
      </p>
      <ConfirmDialog
        open={pending !== null}
        title={pending ? `${CONTROL_LABELS[pending] ?? pending}?` : ""}
        message={pending ? CONTROL_CONFIRM_TEXT[pending] : ""}
        confirmLabel={pending ? (CONTROL_LABELS[pending] ?? pending) : ""}
        destructive={current?.destructive}
        askReason={current?.askReason}
        busy={busy}
        error={error}
        onCancel={() => {
          setPending(null);
          setError(null);
        }}
        onConfirm={(reason) => {
          if (pending) void run(pending, reason);
        }}
      />
    </div>
  );
}
