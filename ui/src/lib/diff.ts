/**
 * Unified diff parser (git `diff --git` output and plain `---/+++` unified diffs).
 *
 * Hunk bodies are consumed by line counts from the `@@ -a,b +c,d @@` header, so content lines that start
 * with `---`/`+++` or `diff ` are never mistaken for file headers.
 */

export type DiffLineKind = "context" | "add" | "del" | "note";

export interface DiffLine {
  kind: DiffLineKind;
  content: string;
  oldNo: number | null;
  newNo: number | null;
}

export interface DiffHunk {
  header: string;
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  section: string;
  lines: DiffLine[];
}

export type DiffFileStatus = "added" | "deleted" | "modified" | "renamed" | "copied" | "binary" | "mode";

export interface DiffFile {
  oldPath: string | null;
  newPath: string | null;
  path: string;
  status: DiffFileStatus;
  binary: boolean;
  headers: string[];
  hunks: DiffHunk[];
  additions: number;
  deletions: number;
}

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/;

function unquote(p: string): string {
  const s = p.trim();
  if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) {
    return s
      .slice(1, -1)
      .replace(/\\t/g, "\t")
      .replace(/\\n/g, "\n")
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, "\\");
  }
  return s;
}

/** `a/src/x.py` → `src/x.py`, `/dev/null` → null; strips a trailing tab+timestamp of plain diffs. */
export function stripPrefix(raw: string): string | null {
  let p = raw.replace(/\t.*$/, "");
  p = unquote(p);
  if (p === "/dev/null") return null;
  if (p.startsWith("a/") || p.startsWith("b/")) return p.slice(2);
  return p;
}

function parseGitHeaderPaths(line: string): { oldPath: string | null; newPath: string | null } {
  const rest = line.slice("diff --git ".length);
  // quoted form: "a/x y" "b/x y"
  const quoted = /^"((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"$/.exec(rest);
  if (quoted) return { oldPath: stripPrefix(`"${quoted[1] ?? ""}"`), newPath: stripPrefix(`"${quoted[2] ?? ""}"`) };
  // unquoted: split at " b/" (paths without spaces are the overwhelmingly common case)
  const idx = rest.indexOf(" b/");
  if (rest.startsWith("a/") && idx > 0) return { oldPath: rest.slice(2, idx), newPath: rest.slice(idx + 3) };
  const parts = rest.split(" ");
  return { oldPath: stripPrefix(parts[0] ?? ""), newPath: stripPrefix(parts[1] ?? "") };
}

function newFile(oldPath: string | null, newPath: string | null): DiffFile {
  return {
    oldPath,
    newPath,
    path: newPath ?? oldPath ?? "(unbekannt)",
    status: "modified",
    binary: false,
    headers: [],
    hunks: [],
    additions: 0,
    deletions: 0,
  };
}

function finalize(f: DiffFile): DiffFile {
  if (f.binary) f.status = f.status === "modified" ? "binary" : f.status;
  else if (f.oldPath === null && f.newPath !== null && f.status === "modified") f.status = "added";
  else if (f.newPath === null && f.oldPath !== null && f.status === "modified") f.status = "deleted";
  else if (f.status === "modified" && f.oldPath && f.newPath && f.oldPath !== f.newPath) f.status = "renamed";
  else if (f.status === "modified" && f.hunks.length === 0 && f.headers.some((h) => h.startsWith("old mode"))) f.status = "mode";
  f.path = f.newPath ?? f.oldPath ?? "(unbekannt)";
  return f;
}

export function parseUnifiedDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  if (!text) return files;
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();

  let cur: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldLeft = 0;
  let newLeft = 0;
  let oldNo = 0;
  let newNo = 0;

  const push = () => {
    if (cur) files.push(finalize(cur));
    cur = null;
    hunk = null;
    oldLeft = 0;
    newLeft = 0;
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? "";

    // inside a hunk: consume body lines by count
    if (hunk && cur && (oldLeft > 0 || newLeft > 0)) {
      const h: DiffHunk = hunk;
      const f: DiffFile = cur;
      const tag = line[0];
      if (tag === "+") {
        h.lines.push({ kind: "add", content: line.slice(1), oldNo: null, newNo: newNo++ });
        newLeft--;
        f.additions++;
        continue;
      }
      if (tag === "-") {
        h.lines.push({ kind: "del", content: line.slice(1), oldNo: oldNo++, newNo: null });
        oldLeft--;
        f.deletions++;
        continue;
      }
      if (tag === " " || line === "") {
        h.lines.push({ kind: "context", content: line.slice(1), oldNo: oldNo++, newNo: newNo++ });
        oldLeft--;
        newLeft--;
        continue;
      }
      if (tag === "\\") {
        h.lines.push({ kind: "note", content: line, oldNo: null, newNo: null });
        continue;
      }
      // malformed/truncated hunk – fall through to header handling
      oldLeft = 0;
      newLeft = 0;
    }

    if (line.startsWith("\\") && hunk) {
      (hunk as DiffHunk).lines.push({ kind: "note", content: line, oldNo: null, newNo: null });
      continue;
    }

    if (line.startsWith("diff --git ")) {
      push();
      const { oldPath, newPath } = parseGitHeaderPaths(line);
      cur = newFile(oldPath, newPath);
      cur.headers.push(line);
      continue;
    }

    if (line.startsWith("--- ") && lines[i + 1]?.startsWith("+++ ")) {
      // plain unified diff (or the ---/+++ pair of a git diff)
      const oldPath = stripPrefix(line.slice(4));
      const newPath = stripPrefix((lines[i + 1] ?? "").slice(4));
      if (!cur || (cur as DiffFile).hunks.length > 0) {
        push();
        cur = newFile(oldPath, newPath);
      } else {
        const f: DiffFile = cur;
        f.oldPath = oldPath;
        f.newPath = newPath;
      }
      const f: DiffFile = cur;
      f.headers.push(line, lines[i + 1] ?? "");
      i++;
      continue;
    }

    const m = HUNK_RE.exec(line);
    if (m) {
      if (!cur) cur = newFile(null, null);
      const f: DiffFile = cur;
      const oldStart = Number(m[1]);
      const oldLines = m[2] === undefined ? 1 : Number(m[2]);
      const newStart = Number(m[3]);
      const newLines = m[4] === undefined ? 1 : Number(m[4]);
      hunk = { header: line, oldStart, oldLines, newStart, newLines, section: m[5] ?? "", lines: [] };
      f.hunks.push(hunk);
      oldLeft = oldLines;
      newLeft = newLines;
      oldNo = oldStart;
      newNo = newStart;
      continue;
    }

    if (cur) {
      const f: DiffFile = cur;
      f.headers.push(line);
      if (line.startsWith("new file mode")) f.status = "added";
      else if (line.startsWith("deleted file mode")) f.status = "deleted";
      else if (line.startsWith("rename from ")) {
        f.status = "renamed";
        f.oldPath = line.slice("rename from ".length);
      } else if (line.startsWith("rename to ")) {
        f.status = "renamed";
        f.newPath = line.slice("rename to ".length);
      } else if (line.startsWith("copy from ")) {
        f.status = "copied";
        f.oldPath = line.slice("copy from ".length);
      } else if (line.startsWith("copy to ")) {
        f.status = "copied";
        f.newPath = line.slice("copy to ".length);
      } else if (line.startsWith("Binary files ") || line === "GIT binary patch") {
        f.binary = true;
      }
    }
    // text outside any file (e.g. commit message preamble) is ignored
  }
  push();
  // an added file's git header says `a/x b/x`; the ---/+++ pair or "new file mode" decides
  for (const f of files) {
    if (f.status === "added") f.oldPath = null;
    if (f.status === "deleted") f.newPath = null;
    f.path = f.newPath ?? f.oldPath ?? "(unbekannt)";
  }
  return files;
}

export function diffStats(files: readonly DiffFile[]): { files: number; additions: number; deletions: number } {
  return files.reduce(
    (acc, f) => ({ files: acc.files + 1, additions: acc.additions + f.additions, deletions: acc.deletions + f.deletions }),
    { files: 0, additions: 0, deletions: 0 },
  );
}
