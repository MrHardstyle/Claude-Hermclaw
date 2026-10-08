import { useMemo, useState } from "react";
import { diffStats, parseUnifiedDiff, type DiffFile } from "../lib/diff";
import { Badge, Empty } from "./ui";

const STATUS_LABELS: Record<DiffFile["status"], string> = {
  added: "neu",
  deleted: "gelöscht",
  modified: "geändert",
  renamed: "umbenannt",
  copied: "kopiert",
  binary: "binär",
  mode: "Rechte",
};

function fileAnchor(i: number): string {
  return `diff-file-${i}`;
}

function DiffFileView({ file, index, defaultOpen }: { file: DiffFile; index: number; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const title =
    file.status === "renamed" || file.status === "copied" ? `${file.oldPath ?? ""} → ${file.newPath ?? ""}` : file.path;
  return (
    <article className="diff-file" id={fileAnchor(index)} data-testid="diff-file">
      <header className="diff-file-head">
        <button type="button" className="diff-toggle" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          <span aria-hidden="true">{open ? "▾" : "▸"}</span>
          <span className="diff-path">{title}</span>
        </button>
        <Badge tone={file.status === "deleted" ? "danger" : file.status === "added" ? "success" : "neutral"}>
          {STATUS_LABELS[file.status]}
        </Badge>
        <span className="diff-counts">
          <span className="diff-add-count" aria-label={`${file.additions} Zeilen hinzugefügt`}>
            +{file.additions}
          </span>{" "}
          <span className="diff-del-count" aria-label={`${file.deletions} Zeilen entfernt`}>
            −{file.deletions}
          </span>
        </span>
      </header>
      {open ? (
        file.binary ? (
          <p className="empty">Binärdatei – keine Textanzeige.</p>
        ) : file.hunks.length === 0 ? (
          <p className="empty">Keine Inhaltsänderung ({file.headers.slice(1).join(", ") || "nur Metadaten"}).</p>
        ) : (
          <div className="diff-scroll">
            <table className="diff-table">
              <caption className="sr-only">Änderungen in {file.path}</caption>
              <thead className="sr-only">
                <tr>
                  <th scope="col">Alte Zeile</th>
                  <th scope="col">Neue Zeile</th>
                  <th scope="col">Inhalt</th>
                </tr>
              </thead>
              {file.hunks.map((h, hi) => (
                <tbody key={hi}>
                  <tr className="diff-hunk">
                    <td colSpan={3}>{h.header}</td>
                  </tr>
                  {h.lines.map((l, li) => (
                    <tr key={li} className={`diff-line diff-${l.kind}`}>
                      <td className="ln">{l.oldNo ?? ""}</td>
                      <td className="ln">{l.newNo ?? ""}</td>
                      <td className="code">
                        <span className="sign" aria-hidden="true">
                          {l.kind === "add" ? "+" : l.kind === "del" ? "-" : l.kind === "note" ? "" : " "}
                        </span>
                        <span className="sr-only">{l.kind === "add" ? "hinzugefügt: " : l.kind === "del" ? "entfernt: " : ""}</span>
                        {l.content}
                      </td>
                    </tr>
                  ))}
                </tbody>
              ))}
            </table>
          </div>
        )
      ) : null}
    </article>
  );
}

export function DiffViewer({ diff }: { diff: string }) {
  const files = useMemo(() => parseUnifiedDiff(diff), [diff]);
  const stats = useMemo(() => diffStats(files), [files]);
  if (!files.length) return <Empty>Keine Änderungen vorhanden.</Empty>;
  return (
    <div className="diff-viewer">
      <p className="diff-summary" data-testid="diff-summary">
        {stats.files} {stats.files === 1 ? "Datei" : "Dateien"} geändert, <span className="diff-add-count">+{stats.additions}</span>{" "}
        <span className="diff-del-count">−{stats.deletions}</span>
      </p>
      <nav aria-label="Geänderte Dateien" className="diff-index">
        <ul>
          {files.map((f, i) => (
            <li key={i}>
              <a href={`#${fileAnchor(i)}`}>{f.path}</a>{" "}
              <span className="diff-add-count">+{f.additions}</span> <span className="diff-del-count">−{f.deletions}</span>
            </li>
          ))}
        </ul>
      </nav>
      {files.map((f, i) => (
        <DiffFileView key={`${f.path}-${i}`} file={f} index={i} defaultOpen={files.length <= 30} />
      ))}
    </div>
  );
}
