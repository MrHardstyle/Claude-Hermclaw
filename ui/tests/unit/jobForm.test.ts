import { describe, expect, it } from "vitest";
import {
  buildJobCreateBody,
  EMPTY_JOB_FORM,
  isValidBranchName,
  LIMITS,
  parseConstraints,
  REPO_URL_ERROR,
  REPO_URL_RE,
  type JobFormValues,
} from "../../src/lib/jobForm";

function form(over: Partial<JobFormValues> = {}): JobFormValues {
  return { ...EMPTY_JOB_FORM, prompt: "Füge einen Health-Endpoint hinzu", ...over };
}

describe("isValidBranchName (git check-ref-format subset)", () => {
  it("accepts ordinary branch names", () => {
    for (const ok of ["main", "develop", "feature/login-form", "release/2026.10", "fix_123", "a/b/c"]) {
      expect(isValidBranchName(ok), ok).toBe(true);
    }
  });

  it("rejects names git would reject", () => {
    const bad = ["", "-main", "/main", "main/", "ma..in", "ma//in", "main.", "main.lock", "has space", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "@", "a@{b", "feat/.hidden", "tab\tname", "x".repeat(201)];
    for (const name of bad) expect(isValidBranchName(name), JSON.stringify(name)).toBe(false);
  });
});

describe("parseConstraints", () => {
  it("one constraint per non-empty line, trimmed, CRLF tolerant", () => {
    expect(parseConstraints("  keine neuen Abhängigkeiten \r\n\n Tests grün halten\n   \n")).toEqual(["keine neuen Abhängigkeiten", "Tests grün halten"]);
    expect(parseConstraints("")).toEqual([]);
  });
});

describe("REPO_URL_RE mirrors the server (_REGISTRABLE_URL)", () => {
  it("accepts remote and local repository locations", () => {
    for (const ok of ["https://git.example.org/team/app.git", "http://192.168.178.60/x.git", "ssh://git@host/x.git", "git@host:team/x.git", "file:///srv/repos/x", "/srv/repos/x"]) {
      expect(REPO_URL_RE.test(ok), ok).toBe(true);
    }
  });

  it("rejects relative paths and other schemes", () => {
    for (const bad of ["repos/x", "./x", "ftp://host/x", "javascript:alert(1)", "x"]) expect(REPO_URL_RE.test(bad), bad).toBe(false);
  });
});

describe("buildJobCreateBody", () => {
  it("builds a minimal body with defaults", () => {
    const { body, errors } = buildJobCreateBody(form());
    expect(errors).toEqual({});
    expect(body).toEqual({
      prompt: "Füge einen Health-Endpoint hinzu",
      constraints: [],
      priority: 50,
      allow_network_research: true,
      auto_commit: true,
    });
  });

  it("maps every field incl. repository, toggles and merge request choice", () => {
    const { body } = buildJobCreateBody(
      form({
        title: "  Health  ",
        repoName: "demo",
        repoUrl: "/srv/repos/demo",
        baseBranch: "develop",
        constraints: "a\nb",
        priority: "90",
        allowNetworkResearch: false,
        autoCommit: false,
        createMergeRequest: "no",
      }),
    );
    expect(body).toEqual({
      prompt: "Füge einen Health-Endpoint hinzu",
      title: "Health",
      repository: { name: "demo", url: "/srv/repos/demo", base_branch: "develop" },
      constraints: ["a", "b"],
      priority: 90,
      allow_network_research: false,
      auto_commit: false,
      create_merge_request: false,
    });
    expect(buildJobCreateBody(form({ createMergeRequest: "yes" })).body?.create_merge_request).toBe(true);
    expect(buildJobCreateBody(form({ createMergeRequest: "default" })).body).not.toHaveProperty("create_merge_request");
  });

  it("reports one German error per invalid field and no body", () => {
    const { body, errors } = buildJobCreateBody(
      form({ prompt: " a ", title: "t".repeat(LIMITS.title + 1), repoUrl: "repos/x", baseBranch: "bad..name", priority: "101" }),
    );
    expect(body).toBeNull();
    expect(errors.prompt).toMatch(/mindestens 3 Zeichen/);
    expect(errors.title).toMatch(/Höchstens 200 Zeichen/);
    expect(errors.repoUrl).toBe(REPO_URL_ERROR);
    expect(errors.baseBranch).toBe("Ungültiger Branch-Name.");
    expect(errors.priority).toMatch(/zwischen 0 und 100/);
  });

  it("validates priority strictly as an integer 0..100", () => {
    for (const p of ["0", "100", " 7 "]) expect(buildJobCreateBody(form({ priority: p })).errors.priority, p).toBeUndefined();
    for (const p of ["-1", "101", "1.5", "1e2", "", "abc"]) expect(buildJobCreateBody(form({ priority: p })).errors.priority, p).toBeDefined();
  });

  it("enforces the server's constraint and prompt limits", () => {
    const many = Array.from({ length: LIMITS.constraints + 1 }, (_, i) => `c${String(i)}`).join("\n");
    expect(buildJobCreateBody(form({ constraints: many })).errors.constraints).toMatch(/Höchstens 50 Vorgaben/);
    expect(buildJobCreateBody(form({ constraints: "x".repeat(LIMITS.constraintLength + 1) })).errors.constraints).toMatch(/2000 Zeichen/);
    expect(buildJobCreateBody(form({ prompt: "p".repeat(LIMITS.prompt + 1) })).errors.prompt).toMatch(/Höchstens 50000 Zeichen/);
    expect(buildJobCreateBody(form({ prompt: "p".repeat(LIMITS.prompt) })).errors.prompt).toBeUndefined();
  });

  it("a base branch needs a repository", () => {
    expect(buildJobCreateBody(form({ baseBranch: "main" })).errors.baseBranch).toMatch(/nur zusammen mit einem Repository/);
    expect(buildJobCreateBody(form({ baseBranch: "main", repoName: "demo" })).errors.baseBranch).toBeUndefined();
  });
});
