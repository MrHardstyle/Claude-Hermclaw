import { useEffect, useState } from "react";
import { artifactDownloadPath, describeError, fetchBlob } from "../api/client";
import type { Artifact } from "../api/types";

/** Download button: the API needs the bearer header, so the file is fetched and handed over as blob URL. */
export function DownloadButton({ artifact }: { artifact: Pick<Artifact, "id" | "name"> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      const blob = await fetchBlob(artifactDownloadPath(artifact.id));
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = artifact.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => {
        URL.revokeObjectURL(url);
      }, 30000);
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="download">
      <button type="button" className="btn btn-small" onClick={() => void download()} disabled={busy} aria-label={`${artifact.name} herunterladen`}>
        {busy ? "Lädt …" : "Herunterladen"}
      </button>
      {error ? (
        <span className="error-text" role="alert">
          {error}
        </span>
      ) : null}
    </span>
  );
}

/** Image/video preview from an authenticated blob URL (revoked on unmount). */
export function MediaPreview({ artifact }: { artifact: Artifact }) {
  const [state, setState] = useState<{ id: string; url: string | null; error: string | null }>({ id: "", url: null, error: null });
  useEffect(() => {
    let url: string | null = null;
    let active = true;
    const ctrl = new AbortController();
    fetchBlob(artifactDownloadPath(artifact.id), ctrl.signal).then(
      (blob) => {
        if (!active) return;
        url = URL.createObjectURL(blob);
        setState({ id: artifact.id, url, error: null });
      },
      (e: unknown) => {
        if (!active) return;
        setState({ id: artifact.id, url: null, error: describeError(e) });
      },
    );
    return () => {
      active = false;
      ctrl.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [artifact.id]);
  const current = state.id === artifact.id ? state : { url: null, error: null };
  const isVideo = artifact.media_type.startsWith("video/") || artifact.kind === "video";
  return (
    <figure className="media-item" data-testid="media-item">
      <div className="media-frame">
        {current.error ? (
          <p className="error-text" role="alert">
            {current.error}
          </p>
        ) : !current.url ? (
          <span className="muted">Vorschau lädt …</span>
        ) : isVideo ? (
          <video src={current.url} controls preload="metadata" aria-label={artifact.name}>
            <track kind="captions" />
          </video>
        ) : (
          <img src={current.url} alt={artifact.name} loading="lazy" />
        )}
      </div>
      <figcaption>
        <span className="media-name">{artifact.name}</span> <span className="muted">{artifact.media_type}</span>
        <DownloadButton artifact={artifact} />
      </figcaption>
    </figure>
  );
}
