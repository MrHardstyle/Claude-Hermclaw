import type { MemoryEntry, MemoryKind, MemoryStatus, MemoryWriteBody } from "../api/types";
import type { Tone } from "./labels";

/** Kinds and states of the knowledge (D-019) in the order the "Wissen" page offers them. */
export const MEMORY_KINDS: { value: MemoryKind; label: string }[] = [
  { value: "lesson", label: "Lehre" },
  { value: "research", label: "Recherche" },
  { value: "incident", label: "Vorfall" },
  { value: "tool_bug", label: "Werkzeug-Bug" },
  { value: "convention", label: "Konvention" },
  { value: "note", label: "Notiz" },
];

export const MEMORY_STATUSES: { value: MemoryStatus; label: string }[] = [
  { value: "active", label: "aktiv" },
  { value: "candidate", label: "Kandidat" },
  { value: "retired", label: "zurückgezogen" },
];

export const MEMORY_SOURCES: Record<string, string> = { system: "System", operator: "Operator", research: "Recherche" };

export const MEMORY_LIMITS = { title: 300, content: 20000, scope: 300, tags: 30 } as const;

export function memoryStatusLabel(status: string): string {
  return MEMORY_STATUSES.find((s) => s.value === status)?.label ?? status;
}

export function memoryStatusTone(status: string): Tone {
  if (status === "active") return "success";
  if (status === "candidate") return "warning";
  return "muted";
}

export interface MemoryFormValues {
  kind: MemoryKind;
  title: string;
  content: string;
  status: MemoryStatus;
  scope: string;
  tags: string;
}

export type MemoryFormErrors = Partial<Record<keyof MemoryFormValues, string>>;

export const EMPTY_MEMORY_FORM: MemoryFormValues = { kind: "lesson", title: "", content: "", status: "active", scope: "", tags: "" };

export function memoryFormFrom(entry: MemoryEntry): MemoryFormValues {
  return {
    kind: entry.kind,
    title: entry.title,
    content: entry.content,
    status: entry.status,
    scope: entry.scope ?? "",
    tags: entry.tags.join(", "),
  };
}

/** Tags are separated by commas or line breaks; duplicates and empty ones are dropped. */
export function parseTags(raw: string): string[] {
  return [...new Set(raw.split(/[,\n]/).map((t) => t.trim()).filter(Boolean))];
}

/** Scope: empty = everywhere, otherwise `repo:<key>` or `model:<alias>`. */
export const SCOPE_RE = /^(repo|model):\S+$/;

export function buildMemoryBody(values: MemoryFormValues): { body: MemoryWriteBody | null; errors: MemoryFormErrors } {
  const errors: MemoryFormErrors = {};
  const title = values.title.trim();
  const content = values.content.trim();
  const scope = values.scope.trim();
  const tags = parseTags(values.tags);
  if (!title) errors.title = "Titel fehlt.";
  else if (title.length > MEMORY_LIMITS.title) errors.title = `Höchstens ${String(MEMORY_LIMITS.title)} Zeichen.`;
  if (!content) errors.content = "Text fehlt.";
  else if (content.length > MEMORY_LIMITS.content) errors.content = `Höchstens ${String(MEMORY_LIMITS.content)} Zeichen.`;
  if (scope && (scope.length > MEMORY_LIMITS.scope || !SCOPE_RE.test(scope))) errors.scope = "Leer, repo:<Schlüssel> oder model:<Alias>.";
  if (tags.length > MEMORY_LIMITS.tags) errors.tags = `Höchstens ${String(MEMORY_LIMITS.tags)} Schlagwörter.`;
  if (Object.keys(errors).length) return { body: null, errors };
  return { body: { kind: values.kind, title, content, status: values.status, scope: scope || null, tags }, errors };
}
