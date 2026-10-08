import { useId, useState, type FormEvent } from "react";
import { api, ApiError, describeError } from "../api/client";

export function Login({ onLogin, notice }: { onLogin: (token: string) => void; notice?: string | null }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();
  const hintId = useId();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const t = token.trim();
    if (!t) {
      setError("Bitte ein API-Token eingeben.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.checkToken(t);
      onLogin(t);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "Token ungültig." : describeError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login" id="main">
      <form className="card login-card" onSubmit={(e) => void submit(e)} aria-labelledby="login-title">
        <h1 id="login-title">Hermclaw Next</h1>
        <p className="muted">Anmeldung mit API-Token (Bearer). Das Token wird nur für diese Browser-Sitzung gespeichert.</p>
        {notice ? (
          <p className="warning-text" role="status">
            {notice}
          </p>
        ) : null}
        <div className="field">
          <label htmlFor={inputId}>API-Token</label>
          <input
            id={inputId}
            name="token"
            type="password"
            autoComplete="current-password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            aria-describedby={hintId}
            aria-invalid={error ? true : undefined}
            required
          />
          <small id={hintId} className="muted">
            Admin-Token oder ein Token mit Scope read/control.
          </small>
        </div>
        {error ? (
          <p className="error-box" role="alert" data-testid="login-error">
            {error}
          </p>
        ) : null}
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Prüfe …" : "Anmelden"}
        </button>
      </form>
    </main>
  );
}
