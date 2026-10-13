import { useId, useState, type SyntheticEvent } from "react";
import { api, ApiError, describeError } from "../api/client";
import type { Job, Repository } from "../api/types";
import { buildJobCreateBody, EMPTY_JOB_FORM, LIMITS, type JobFormErrors, type JobFormValues } from "../lib/jobForm";

function FieldError({ id, error }: { id: string; error: string | undefined }) {
  if (!error) return null;
  return (
    <small id={id} className="field-error" role="alert">
      {error}
    </small>
  );
}

export function JobCreateForm({ repositories, onCreated }: { repositories: Repository[]; onCreated: (job: Job) => void }) {
  const [values, setValues] = useState<JobFormValues>(EMPTY_JOB_FORM);
  const [errors, setErrors] = useState<JobFormErrors>({});
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const uid = useId();
  const id = (name: string) => `${uid}-${name}`;

  const set = <K extends keyof JobFormValues>(key: K, value: JobFormValues[K]) => {
    setValues((v) => ({ ...v, [key]: value }));
    if (errors[key]) setErrors((e) => ({ ...e, [key]: undefined }));
  };

  const submit = async (e: SyntheticEvent) => {
    e.preventDefault();
    setServerError(null);
    const { body, errors: errs } = buildJobCreateBody(values);
    setErrors(errs);
    if (!body) return;
    setBusy(true);
    try {
      const job = await api.createJob(body);
      setValues(EMPTY_JOB_FORM);
      onCreated(job);
    } catch (err) {
      setServerError(err instanceof ApiError && err.status === 422 ? `Eingaben ungültig: ${err.message}` : describeError(err));
    } finally {
      setBusy(false);
    }
  };

  const described = (name: keyof JobFormValues, hint?: string) => {
    const ids = [hint, errors[name] ? id(`${name}-err`) : undefined].filter(Boolean).join(" ");
    return { "aria-describedby": ids || undefined, "aria-invalid": errors[name] ? true : undefined };
  };

  return (
    <form className="job-form" onSubmit={(e) => void submit(e)} noValidate aria-label="Neuen Job anlegen" data-testid="job-form">
      <div className="field">
        <label htmlFor={id("prompt")}>Aufgabe (Prompt) *</label>
        <textarea
          id={id("prompt")}
          name="prompt"
          rows={6}
          value={values.prompt}
          maxLength={LIMITS.prompt}
          onChange={(e) => set("prompt", e.target.value)}
          required
          {...described("prompt", id("prompt-hint"))}
        />
        <small id={id("prompt-hint")} className="muted">
          Freie Beschreibung der Aufgabe. Der Planer (Gemma) erstellt daraus einen Plan.
        </small>
        <FieldError id={id("prompt-err")} error={errors.prompt} />
      </div>
      <div className="field">
        <label htmlFor={id("title")}>Titel</label>
        <input
          id={id("title")}
          name="title"
          type="text"
          value={values.title}
          maxLength={LIMITS.title}
          onChange={(e) => set("title", e.target.value)}
          {...described("title", id("title-hint"))}
        />
        <small id={id("title-hint")} className="muted">
          Optional – sonst die erste Zeile der Aufgabe.
        </small>
        <FieldError id={id("title-err")} error={errors.title} />
      </div>
      <fieldset className="fieldset">
        <legend>Repository</legend>
        <div className="field-row">
          <div className="field">
            <label htmlFor={id("repoName")}>Name</label>
            <input
              id={id("repoName")}
              name="repoName"
              type="text"
              list={id("repo-list")}
              value={values.repoName}
              onChange={(e) => set("repoName", e.target.value)}
              autoComplete="off"
              {...described("repoName")}
            />
            <datalist id={id("repo-list")}>
              {repositories.map((r) => (
                <option key={r.id} value={r.name}>
                  {r.url}
                </option>
              ))}
            </datalist>
            <FieldError id={id("repoName-err")} error={errors.repoName} />
          </div>
          <div className="field">
            <label htmlFor={id("repoUrl")}>URL</label>
            <input
              id={id("repoUrl")}
              name="repoUrl"
              type="text"
              inputMode="url"
              placeholder="ssh://git@… oder https://…"
              value={values.repoUrl}
              onChange={(e) => set("repoUrl", e.target.value)}
              {...described("repoUrl", id("repoUrl-hint"))}
            />
            <small id={id("repoUrl-hint")} className="muted">
              Nicht registrierte Repositories werden mit URL angelegt.
            </small>
            <FieldError id={id("repoUrl-err")} error={errors.repoUrl} />
          </div>
          <div className="field">
            <label htmlFor={id("baseBranch")}>Basis-Branch</label>
            <input
              id={id("baseBranch")}
              name="baseBranch"
              type="text"
              placeholder="main"
              value={values.baseBranch}
              onChange={(e) => set("baseBranch", e.target.value)}
              {...described("baseBranch")}
            />
            <FieldError id={id("baseBranch-err")} error={errors.baseBranch} />
          </div>
        </div>
      </fieldset>
      <div className="field">
        <label htmlFor={id("constraints")}>Vorgaben (eine pro Zeile)</label>
        <textarea
          id={id("constraints")}
          name="constraints"
          rows={3}
          value={values.constraints}
          onChange={(e) => set("constraints", e.target.value)}
          {...described("constraints")}
        />
        <FieldError id={id("constraints-err")} error={errors.constraints} />
      </div>
      <div className="field-row">
        <div className="field field-narrow">
          <label htmlFor={id("priority")}>Priorität (0–100)</label>
          <input
            id={id("priority")}
            name="priority"
            type="number"
            min={0}
            max={100}
            step={1}
            value={values.priority}
            onChange={(e) => set("priority", e.target.value)}
            {...described("priority")}
          />
          <FieldError id={id("priority-err")} error={errors.priority} />
        </div>
        <div className="field field-narrow">
          <label htmlFor={id("mr")}>Merge Request</label>
          <select
            id={id("mr")}
            name="createMergeRequest"
            value={values.createMergeRequest}
            onChange={(e) => set("createMergeRequest", e.target.value as JobFormValues["createMergeRequest"])}
          >
            <option value="default">Standard (Policy)</option>
            <option value="yes">Ja, MR anlegen</option>
            <option value="no">Nein</option>
          </select>
        </div>
      </div>
      <div className="checks">
        <label className="check">
          <input
            type="checkbox"
            name="allowNetworkResearch"
            checked={values.allowNetworkResearch}
            onChange={(e) => set("allowNetworkResearch", e.target.checked)}
          />{" "}
          Web-Research erlauben
        </label>
        <label className="check">
          <input type="checkbox" name="autoCommit" checked={values.autoCommit} onChange={(e) => set("autoCommit", e.target.checked)} /> Automatisch
          committen
        </label>
      </div>
      {serverError ? (
        <p className="error-box" role="alert" data-testid="job-form-error">
          {serverError}
        </p>
      ) : null}
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={busy} data-testid="job-submit">
          {busy ? "Wird angelegt …" : "Job anlegen"}
        </button>
        <button
          type="button"
          className="btn"
          disabled={busy}
          onClick={() => {
            setValues(EMPTY_JOB_FORM);
            setErrors({});
            setServerError(null);
          }}
        >
          Zurücksetzen
        </button>
      </div>
    </form>
  );
}
