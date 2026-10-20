import { describe, expect, it } from "vitest";
import type { MemoryEntry } from "../../src/api/types";
import { buildMemoryBody, EMPTY_MEMORY_FORM, memoryFormFrom, memoryStatusTone, parseTags } from "../../src/lib/memoryForm";

describe("Wissen-Formular", () => {
  it("baut den Body mit getrimmten Feldern, Schlagwörtern und leerem Geltungsbereich als null", () => {
    const { body, errors } = buildMemoryBody({
      ...EMPTY_MEMORY_FORM,
      title: "  ruff-Funde sofort beheben ",
      content: "Jeden Fund beheben, auch in Tests.\n",
      tags: "ruff, lint\nruff, ",
    });
    expect(errors).toEqual({});
    expect(body).toEqual({
      kind: "lesson",
      title: "ruff-Funde sofort beheben",
      content: "Jeden Fund beheben, auch in Tests.",
      status: "active",
      scope: null,
      tags: ["ruff", "lint"],
    });
  });

  it("meldet fehlende Pflichtfelder und einen ungültigen Geltungsbereich", () => {
    const { body, errors } = buildMemoryBody({ ...EMPTY_MEMORY_FORM, title: " ", scope: "irgendwo" });
    expect(body).toBeNull();
    expect(Object.keys(errors).sort()).toEqual(["content", "scope", "title"]);
    expect(buildMemoryBody({ ...EMPTY_MEMORY_FORM, title: "t", content: "c", scope: "repo:gitlab/x" }).body?.scope).toBe("repo:gitlab/x");
  });

  it("übernimmt einen Eintrag zum Bearbeiten", () => {
    const entry = { kind: "tool_bug", title: "T", content: "C", status: "candidate", scope: null, tags: ["a", "b"] } as unknown as MemoryEntry;
    expect(memoryFormFrom(entry)).toEqual({ kind: "tool_bug", title: "T", content: "C", status: "candidate", scope: "", tags: "a, b" });
    expect(parseTags("")).toEqual([]);
    expect([memoryStatusTone("active"), memoryStatusTone("candidate"), memoryStatusTone("retired")]).toEqual(["success", "warning", "muted"]);
  });
});
