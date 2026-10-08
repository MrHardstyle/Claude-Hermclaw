import { describe, expect, it } from "vitest";
import { diffStats, parseUnifiedDiff, stripPrefix } from "../../src/lib/diff";

const GIT_DIFF = `diff --git a/src/api.py b/src/api.py
index 1111111..2222222 100644
--- a/src/api.py
+++ b/src/api.py
@@ -1,4 +1,5 @@ def handler():
 import os
-import sys
+import sys  # noqa
+import json
 
 def main():
@@ -10,2 +11,2 @@
--- not a header, a deleted line starting with dashes
+++ not a header, an added line starting with pluses
diff --git a/docs/new.md b/docs/new.md
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/docs/new.md
@@ -0,0 +1,2 @@
+# Neu
+Zeile
\\ No newline at end of file
diff --git a/old.txt b/old.txt
deleted file mode 100644
index 4444444..0000000
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-weg
diff --git a/a.txt b/b.txt
similarity index 100%
rename from a.txt
rename to b.txt
diff --git a/img.png b/img.png
index 5555555..6666666 100644
Binary files a/img.png and b/img.png differ
`;

describe("parseUnifiedDiff", () => {
  const files = parseUnifiedDiff(GIT_DIFF);

  it("splits files and detects statuses", () => {
    expect(files.map((f) => [f.path, f.status])).toEqual([
      ["src/api.py", "modified"],
      ["docs/new.md", "added"],
      ["old.txt", "deleted"],
      ["b.txt", "renamed"],
      ["img.png", "binary"],
    ]);
    expect(files[2]?.newPath).toBeNull();
    expect(files[1]?.oldPath).toBeNull();
    expect(files[3]?.oldPath).toBe("a.txt");
    expect(files[4]?.binary).toBe(true);
  });

  it("counts additions/deletions and numbers lines", () => {
    const api = files[0];
    expect(api?.additions).toBe(3);
    expect(api?.deletions).toBe(2);
    expect(api?.hunks).toHaveLength(2);
    const h1 = api?.hunks[0];
    expect(h1?.section).toBe("def handler():");
    expect(h1?.lines.map((l) => [l.kind, l.oldNo, l.newNo])).toEqual([
      ["context", 1, 1],
      ["del", 2, null],
      ["add", null, 2],
      ["add", null, 3],
      ["context", 3, 4],
      ["context", 4, 5],
    ]);
  });

  it("treats ---/+++ inside hunk bodies as content", () => {
    const h2 = files[0]?.hunks[1];
    expect(h2?.lines.map((l) => l.kind)).toEqual(["del", "add"]);
    expect(h2?.lines[0]?.content).toBe("-- not a header, a deleted line starting with dashes");
    expect(h2?.lines[0]?.oldNo).toBe(10);
    expect(h2?.lines[1]?.newNo).toBe(11);
  });

  it("keeps 'no newline' markers as notes", () => {
    const notes = files[1]?.hunks[0]?.lines.filter((l) => l.kind === "note");
    expect(notes).toHaveLength(1);
  });

  it("parses plain unified diffs without git headers", () => {
    const plain = "--- a.txt\t2026-01-01\n+++ a.txt\t2026-01-02\n@@ -1 +1 @@\n-x\n+y\n--- b.txt\n+++ b.txt\n@@ -2,0 +3 @@\n+z\n";
    const f = parseUnifiedDiff(plain);
    expect(f.map((x) => x.path)).toEqual(["a.txt", "b.txt"]);
    expect(diffStats(f)).toEqual({ files: 2, additions: 2, deletions: 1 });
    expect(f[1]?.hunks[0]?.oldLines).toBe(0);
    expect(f[1]?.hunks[0]?.lines[0]?.newNo).toBe(3);
  });

  it("handles CRLF, empty input and quoted paths", () => {
    expect(parseUnifiedDiff("")).toEqual([]);
    const crlf = 'diff --git "a/with space.txt" "b/with space.txt"\r\n--- "a/with space.txt"\r\n+++ "b/with space.txt"\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n';
    const f = parseUnifiedDiff(crlf);
    expect(f[0]?.path).toBe("with space.txt");
    expect(f[0]?.additions).toBe(1);
  });

  it("survives truncated hunks", () => {
    const f = parseUnifiedDiff("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,5 +1,5 @@\n a\n-b\n");
    expect(f[0]?.deletions).toBe(1);
  });

  it("strips prefixes", () => {
    expect(stripPrefix("a/src/x.py")).toBe("src/x.py");
    expect(stripPrefix("/dev/null")).toBeNull();
    expect(stripPrefix("plain.txt\t2026")).toBe("plain.txt");
  });
});
