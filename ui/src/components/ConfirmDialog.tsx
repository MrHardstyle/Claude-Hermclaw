import { useEffect, useId, useRef, useState, type SyntheticEvent } from "react";

/** Modal confirmation built on the native <dialog> (focus trap, Esc to close, inert background). */
export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  destructive,
  askReason,
  busy,
  confirmDisabled,
  error,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  destructive?: boolean;
  askReason?: boolean;
  busy?: boolean;
  /** the action is no longer allowed (job state changed while the dialog was open) */
  confirmDisabled?: boolean;
  error?: string | null;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState("");
  const titleId = useId();
  const descId = useId();
  const reasonId = useId();

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      if (typeof d.showModal === "function") d.showModal();
      else d.setAttribute("open", "");
    } else if (!open && d.open) {
      d.close();
    }
  }, [open]);

  const submit = (e: SyntheticEvent) => {
    e.preventDefault();
    if (busy || confirmDisabled) return;
    onConfirm(reason.trim());
  };

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby={titleId}
      aria-describedby={descId}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onCancel();
      }}
      onClose={() => {
        setReason("");
      }}
    >
      <form method="dialog" onSubmit={submit}>
        <h2 id={titleId}>{title}</h2>
        <p id={descId}>{message}</p>
        {askReason ? (
          <div className="field">
            <label htmlFor={reasonId}>Begründung (optional)</label>
            <input id={reasonId} type="text" value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
          </div>
        ) : null}
        {error ? (
          <p className="error-box" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            Zurück
          </button>
          <button type="submit" className={`btn ${destructive ? "btn-danger" : "btn-primary"}`} disabled={busy || confirmDisabled} data-testid="confirm-action">
            {busy ? "Wird ausgeführt …" : confirmLabel}
          </button>
        </div>
      </form>
    </dialog>
  );
}
