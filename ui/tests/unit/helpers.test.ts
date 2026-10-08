import { describe, expect, it } from "vitest";
import { latestStatusLine, maxSequence, mergeEvents, normalizeEvent, summarizeEvent, isErrorEvent } from "../../src/lib/events";
import { controlStates } from "../../src/lib/controls";
import { apiUrl, normalizeApiBase, resolveApiBase } from "../../src/lib/config";
import { matchRoute, stripBase, withBase } from "../../src/lib/routes";
import { formatBytes, formatDuration, formatRelative } from "../../src/lib/format";
import type { HermEvent } from "../../src/api/types";

function ev(sequence: number, event_type = "status", payload: Record<string, string> = { text: `t${sequence}` }, severity = "info"): HermEvent {
  const n = normalizeEvent({ sequence, event_type, payload, severity, ts: "2026-10-08T10:00:00Z", source_type: "runtime" });
  if (!n) throw new Error("bad");
  return n;
}

describe("events", () => {
  it("normalizes REST and SSE shapes", () => {
    expect(normalizeEvent({ sequence: 1, event_type: "x", ts: "A" })?.ts).toBe("A");
    expect(normalizeEvent({ sequence: 1, event_type: "x", timestamp: "B" })?.ts).toBe("B");
    expect(normalizeEvent({ event_type: "x" })).toBeNull();
    expect(normalizeEvent(null)).toBeNull();
  });

  it("merges, dedupes and caps", () => {
    const a = [ev(1), ev(3)];
    expect(mergeEvents(a, [ev(4), ev(5)]).map((e) => e.sequence)).toEqual([1, 3, 4, 5]);
    expect(mergeEvents(a, [ev(2), ev(3)]).map((e) => e.sequence)).toEqual([1, 2, 3]);
    expect(mergeEvents(a, [ev(9), ev(8)], 3).map((e) => e.sequence)).toEqual([3, 8, 9]);
    expect(mergeEvents(a, [])).toBe(a);
    expect(maxSequence([ev(4), ev(2)])).toBe(4);
  });

  it("finds the latest status line and summarizes events", () => {
    const list = [ev(1, "status", { text: "Repository wird inventarisiert" }), ev(2, "job.transition", { from: "queued", to: "planning" }), ev(3, "status", { text: "Gemma erstellt Plan" }), ev(4, "step.created", {})];
    expect(latestStatusLine(list)?.payload.text).toBe("Gemma erstellt Plan");
    expect(summarizeEvent(list[1] as HermEvent)).toBe("queued → planning");
    expect(summarizeEvent(ev(5, "git.operation", { operation: "commit" }))).toBe("commit");
    expect(isErrorEvent(ev(6, "test.failed", {}))).toBe(true);
    expect(isErrorEvent(ev(7, "x", {}, "warning"))).toBe(true);
    expect(isErrorEvent(ev(8, "x", {}))).toBe(false);
  });
});

describe("controlStates", () => {
  it("enables controls per job status", () => {
    const running = controlStates({ status: "running", pause_requested: false, cancel_requested: false });
    expect([running.cancel.enabled, running.pause.enabled, running.resume.enabled, running.retry.enabled, running.replan.enabled]).toEqual([true, true, false, false, true]);
    const paused = controlStates({ status: "running", pause_requested: true, cancel_requested: false });
    expect([paused.pause.enabled, paused.resume.enabled]).toEqual([false, true]);
    const failed = controlStates({ status: "failed", pause_requested: false, cancel_requested: false });
    expect([failed.cancel.enabled, failed.pause.enabled, failed.resume.enabled, failed.retry.enabled, failed.replan.enabled]).toEqual([false, false, false, true, false]);
    expect(failed.cancel.reason).toBe("Job ist bereits beendet");
    const waiting = controlStates({ status: "waiting_for_user", pause_requested: false, cancel_requested: false });
    expect(waiting.resume.enabled).toBe(true);
    const blocked = controlStates({ status: "blocked", pause_requested: false, cancel_requested: true });
    expect([blocked.retry.enabled, blocked.cancel.enabled]).toEqual([true, false]);
    expect(controlStates({ status: "succeeded", pause_requested: false, cancel_requested: false }).retry.enabled).toBe(false);
  });
});

describe("config", () => {
  it("resolves the api base", () => {
    expect(normalizeApiBase(undefined)).toBe("/api");
    expect(normalizeApiBase("https://h/api///")).toBe("https://h/api");
    expect(resolveApiBase("  ", "/x/api")).toBe("/x/api");
    expect(resolveApiBase("/meta", "/env")).toBe("/meta");
    expect(apiUrl("/jobs", { status: "queued", limit: 5, empty: "", none: null }, "/api")).toBe("/api/jobs?status=queued&limit=5");
  });
});

describe("routes", () => {
  it("matches patterns with optional params", () => {
    expect(matchRoute("/jobs/:id/:tab?", "/jobs/abc")).toEqual({ id: "abc" });
    expect(matchRoute("/jobs/:id/:tab?", "/jobs/abc/plan?x=1")).toEqual({ id: "abc", tab: "plan" });
    expect(matchRoute("/jobs/:id/:tab?", "/jobs")).toBeNull();
    expect(matchRoute("/jobs", "/jobs/abc")).toBeNull();
    expect(matchRoute("/", "/")).toEqual({});
    expect(stripBase("/ui/jobs", "/ui/")).toBe("/jobs");
    expect(stripBase("/jobs", "/")).toBe("/jobs");
    expect(withBase("/jobs", "/ui/")).toBe("/ui/jobs");
  });
});

describe("format", () => {
  it("formats durations, bytes and relative times in German", () => {
    expect(formatDuration(250)).toBe("250 ms");
    expect(formatDuration(1500)).toBe("1,5 s");
    expect(formatDuration(125000)).toBe("2 min 5 s");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2,0 KB");
    expect(formatRelative("2026-10-08T10:00:00Z", Date.parse("2026-10-08T10:05:00Z"))).toBe("vor 5 min");
  });
});
